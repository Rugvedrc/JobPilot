"""Auto-apply brain. The clicking happens in YOUR browser, inside the FresherPilot extension, so your
existing logins are used. This module (served by main.py) does the thinking and keeps the state:

  - builds the queue (one job from its "Auto-apply" button, or your best matches for "Auto-apply all")
  - decides each next step: the extension labels every clickable element with a Vimium-style key and
    sends a snapshot; the LLM answers click <key> / fill / done / skip / needs_user
  - code-level guards (never click Follow/Save/Sign up..., confirmation detection)
  - progress, log, and the Continue / Skip / Stop commands from the app

When a site needs you (sign in, CAPTCHA, verification code, or a question your profile can't
answer), the extension pauses, brings the tab to the front, and the app asks you to finish it and
click Continue.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
def _log_path() -> Path:
    from fresherpilot.core import users

    return users.path("autoapply_log.jsonl")

STATE: dict[str, Any] = {
    "running": False, "mode": None, "queue": [], "total": 0, "index": 0,
    "applied": 0, "skipped": 0, "failed": 0, "current": None, "waiting": None,
    "log": [], "command": None,
}
_lock = threading.Lock()

# Pacing used by the extension (seconds).
# The extension also waits for each page to be ready (loaded, no spinners, DOM/network quiet);
# these are only the short human-like pauses on top of that.
SETTINGS = {"step_delay": [0.8, 2.0], "before_submit": [1.5, 3.0], "between_jobs": [20, 40], "max_steps": 28}


# ---------------------------------------------------------------- queue

def eligible_jobs(include_with_instructions: bool = False) -> list[dict[str, Any]]:
    """AI-approved jobs not applied yet, best matches first."""
    from fresherpilot.apply import applicant
    from fresherpilot.outreach import contacts
    from fresherpilot.jobs import llm_filter
    from fresherpilot.jobs.apify_jobs import load_cache
    from fresherpilot.server import _normalized_jobs
    from fresherpilot.jobs.ranker import score_jobs

    apps = applicant.applications()
    out = []
    for job in _normalized_jobs(load_cache()):
        if not (job.get("url") or "").startswith("http"):
            continue
        uid = f"{job.get('source')}:{job.get('id')}"
        verdict = llm_filter.cached_verdict(job) or {}
        if verdict.get("decision") != "keep" or uid in apps:
            continue
        if verdict.get("apply_instructions") and not include_with_instructions:
            continue
        job["uid"] = uid
        job["employer"] = contacts.resolve_company(job)
        out.append(job)
    score_jobs(out)
    out.sort(key=lambda j: -(j.get("score") or 0))
    return out


def start(uids: list[str] | None, max_jobs: int = 25) -> dict[str, Any]:
    with _lock:
        if STATE["running"]:
            raise RuntimeError("Auto-apply is already running")
    single = bool(uids)
    jobs = eligible_jobs(include_with_instructions=single)
    if uids:
        wanted = set(uids)
        jobs = [j for j in jobs if j["uid"] in wanted]
        if not jobs:
            raise RuntimeError("That job isn't eligible (already applied, or not approved).")
    jobs = jobs[: max(1, min(max_jobs, 50))]
    queue = [{"uid": j["uid"], "url": j["url"], "title": j["title"], "company": j.get("employer") or j.get("company"),
              "source": j.get("source")} for j in jobs]
    with _lock:
        STATE.update(running=True, mode="single" if single else "bulk", queue=queue, total=len(queue), index=0,
                     applied=0, skipped=0, failed=0, current=None, waiting=None, command=None)
    _log("start", None, f"{len(queue)} job(s) queued ({'this job' if single else 'best matches first'})")
    return {"queue": queue, "settings": SETTINGS, "mode": STATE["mode"]}


def _log(event: str, job: dict[str, Any] | None, detail: str = "") -> None:
    entry = {"at": datetime.now(timezone.utc).isoformat(), "event": event, "detail": detail,
             "uid": (job or {}).get("uid"), "title": (job or {}).get("title"), "company": (job or {}).get("company")}
    with _lock:
        STATE["log"] = (STATE["log"] + [entry])[-200:]
    with _log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def report(event: str, uid: str | None, detail: str = "", waiting: dict[str, Any] | None = None) -> dict[str, Any]:
    """Progress from the extension: job_start / applied / skipped / failed / waiting / resumed / finished."""
    from fresherpilot.apply import applicant

    job = next((q for q in STATE["queue"] if q["uid"] == uid), None) if uid else None
    with _lock:
        if event == "job_start" and job:
            STATE["current"] = job
            STATE["index"] = STATE["queue"].index(job) + 1
            STATE["waiting"] = None
        elif event in ("applied", "skipped", "failed"):
            STATE[event] += 1
            STATE["waiting"] = None
        elif event == "waiting":
            STATE["waiting"] = waiting or {"reason": detail}
        elif event == "resumed":
            STATE["waiting"] = None
        elif event == "finished":
            STATE.update(running=False, current=None, waiting=None, command=None)
    if event == "applied" and uid:
        applicant.set_application(uid, "applied", note="auto-applied")
        from fresherpilot.jobs import visited
        from fresherpilot.jobs.apify_jobs import load_cache
        from fresherpilot.server import _normalized_jobs

        match = [j for j in _normalized_jobs(load_cache()) if f"{j.get('source')}:{j.get('id')}" == uid]
        if match:
            visited.mark(match, True)
    if event != "job_start" or detail:
        _log(event, job, detail)
    return status()


def command(action: str) -> None:
    with _lock:
        STATE["command"] = action


def take_command() -> str | None:
    with _lock:
        cmd, STATE["command"] = STATE["command"], None
        return cmd


def status() -> dict[str, Any]:
    with _lock:
        return {k: v for k, v in STATE.items() if k not in ("log", "queue")} | {"log": STATE["log"][-40:]}


# ---------------------------------------------------------------- deciding the next step

NAV_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["click", "fill", "done", "skip", "needs_user"]},
        "key": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["action", "key", "reason"],
    "additionalProperties": False,
}

NAV_PROMPT = """You drive a browser to submit ONE job application, either through the job board's quick-apply flow
(LinkedIn "Easy Apply", Naukri "Apply", Indeed "Apply now", Wellfound) or, when the job only offers
"Apply on company site", through the company's own careers page / applicant tracking system (Greenhouse, Lever,
Workday, Ashby...). The candidate is logged in to the job boards in this browser.
Each step you see the page URL and title, the visible text (of the open dialog if there is one), how many form
fields are visible and empty, the filler status, previous actions, and clickable elements with hint keys.
Reply with ONE action:

- fill       : application form fields are visible and the filler has NOT run on this step yet.
- click      : press the element with that key to move the application forward (elements marked "further down
               the page" are fine; clicking scrolls to them): Easy Apply / Apply / Apply now /
               Apply on company site / Apply for this job / I'm interested, Next, Continue, Save and continue,
               Review, Submit application / Submit. Prefer elements inside the dialog. On a careers page, open
               THIS job's application (not other openings). Accepting necessary cookies is fine.
- done       : the page clearly confirms the application was submitted / sent / applied.
- needs_user : the candidate must act: sign in / log in page, create an account, CAPTCHA, email/OTP/phone code,
               or a question that can't be answered from their profile. Say exactly what they need to do.
- skip       : the job is closed / no longer accepting applications / already applied, or the flow needs something
               that can't be done (video interview, live test). Give the reason.

Never click: Save (job), Follow, Share, Message, Dismiss, Discard, Cancel, Back, Sign in / Log in / Create account /
Sign up (ask the candidate instead), social login buttons, ads, other job listings, premium/upsell, profile/settings
links, cookie "reject"/settings. key is "" unless action is click."""

CONFIRMED = re.compile(
    r"application (has been |was )?(successfully )?(submitted|sent|received)|thank(s| you) for (applying|your application)|"
    r"successfully applied|you('ve| have) (successfully )?applied|we('ve| have) received your application|your application was sent",
    re.I,
)
NEVER_CLICK = re.compile(
    r"^\s*save\s*(job)?\s*$|follow|share|message|dismiss|discard|cancel|premium|sign ?(out|in|up)|log ?(out|in)|"
    r"create (an )?account|register|continue with (google|linkedin|facebook|apple)",
    re.I,
)
# Buttons that only move an application forward (used for the no-AI shortcut).
FORWARD = re.compile(
    r"^(next|continue|continue to next step|save and continue|review|review (your )?application|"
    r"submit|submit (your )?application|send application)$",
    re.I,
)
# Links that leave the application (opening them loses the form).
LEAVES_FORM = re.compile(r"terms|privacy|legal|policy|cookie|help|faq|support|about|blog|learn more|guidelines", re.I)
BLOCK_TEXT = re.compile(r"captcha|verify you are (a )?human|are you a robot|security check|unusual activity", re.I)
LOGIN_URL = re.compile(r"/login|/authwall|nlogin|secure\.indeed\.com/auth|/signin|/sign-in|/register|/signup|accounts\.google", re.I)


def decide(body: dict[str, Any]) -> dict[str, Any]:
    """Next action for the extension, from a page snapshot."""
    from fresherpilot.jobs import llm_filter

    snap = body.get("snapshot") or {}
    url, text = snap.get("url", ""), snap.get("text", "")
    full = snap.get("full_text", text)
    filled, submitted = bool(body.get("filled")), bool(body.get("submitted"))
    history = body.get("history") or []

    # Code-level checks first (cheap and reliable).
    if submitted and CONFIRMED.search(full):
        return {"action": "done", "key": "", "reason": "confirmation shown after submit"}
    # The extension detects real challenges (visible CAPTCHA widgets / "verify you are human"), not just
    # the word "captcha", which many normal pages show in their footer.
    if snap.get("blocked") == "challenge":
        return {"action": "needs_user", "key": "", "reason": "Solve the security check (CAPTCHA) in the tab"}
    if snap.get("blocked") == "login" or LOGIN_URL.search(url):
        return {"action": "needs_user", "key": "", "reason": "Sign in on this site in the opened tab"}

    items = snap.get("items") or []

    # Obvious step, no AI call: inside a dialog, nothing left to fill, and exactly one forward button.
    if snap.get("dialog") and (filled or not snap.get("fields")):
        forward = [it for it in items if it.get("in_dialog") and it.get("tag") != "a"
                   and FORWARD.match((it.get("text") or it.get("label") or "").strip())]
        if len(forward) == 1:
            label = forward[0].get("text") or forward[0].get("label") or ""
            return {"action": "click", "key": forward[0]["key"], "reason": f"only forward button: {label[:30]}",
                    "is_submit": bool(re.search(r"submit|send application|finish", label, re.I))}
    # With a dialog open, the application lives in it: send its elements plus a few page buttons.
    if snap.get("dialog"):
        inside = [it for it in items if it.get("in_dialog")]
        outside = [it for it in items if not it.get("in_dialog") and it.get("tag") != "a"][:8]
        items = inside + outside if inside else items[:60]
    else:
        items = items[:70]
    listing = "\n".join(
        f"{it['key']} {'L' if it.get('tag') == 'a' else 'B'}{'d' if it.get('in_dialog') else ''}{'v' if it.get('below') else ''} "
        f"{(it.get('text') or it.get('label'))[:60]}"
        + (f" ({it['label'][:40]})" if it.get("label") and it.get("label") != it.get("text") else "")
        for it in items
    )
    job = body.get("job") or {}
    user = (
        f"JOB: {job.get('title')} at {job.get('company')} ({job.get('source')})\nURL: {url}\nTITLE: {snap.get('title')}\n"
        f"Dialog open: {snap.get('dialog')}. Visible form fields: {snap.get('fields')} (empty text fields: {snap.get('empty_fields')}).\n"
        + ("FILLER STATUS: the form on this step is ALREADY FILLED and all required fields have values. Do NOT answer "
           "'fill'; press Next / Continue / Review / Submit (or 'done' if submitted).\n" if filled
           else "FILLER STATUS: not run on this step yet.\n")
        + f"Previous: {'; '.join(history[-4:]) or 'none'}\n\nTEXT:\n{text[:1300]}\n\nCLICKABLE (key, B=button L=link, d=in dialog, v=further down):\n{listing}"
    )
    messages = [{"role": "system", "content": NAV_PROMPT}, {"role": "user", "content": user}]
    client = llm_filter._client()
    for _attempt in range(3):
        # Picking a button is easy: small model, low effort first; keeps the big model's quota for job checks.
        content, _model = llm_filter._create(client, messages, schema=NAV_SCHEMA, name="next_action",
                                             models=["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"], effort="low")
        decision = json.loads(content)
        if decision["action"] != "click":
            break
        item = next((it for it in items if it["key"] == decision.get("key")), None)
        if not item:
            problem = f"'{decision.get('key')}' is not in the list."
        else:
            label = f"{item.get('text', '')} {item.get('label', '')}"
            if NEVER_CLICK.search(label) and re.search(r"sign ?(in|up)|log ?in|create (an )?account|register", label, re.I):
                return {"action": "needs_user", "key": "", "reason": "This site needs you to sign in / create an account"}
            if NEVER_CLICK.search(label) or (item.get("tag") == "a" and LEAVES_FORM.search(label)):
                problem = f"'{label.strip()[:50]}' ({item['key']}) leaves or derails the application; it is not allowed."
            else:
                decision["is_submit"] = bool(re.search(r"submit|send application|finish", label, re.I))
                return decision
        # Ask again with the problem explained (the Submit button may be further down the list).
        messages += [{"role": "assistant", "content": content},
                     {"role": "user", "content": f"{problem} Choose a different element that moves the application forward "
                                                 "(Next / Continue / Review / Submit, possibly further down the page), or another action."}]
    else:
        return {"action": "skip", "key": "", "reason": "couldn't find a safe button to press"}
    if decision["action"] == "skip" and submitted and CONFIRMED.search(full):
        return {"action": "done", "key": "", "reason": "confirmation shown after submit"}
    return decision
