from __future__ import annotations

import faulthandler
import os
import threading
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from fresherpilot.apply import applicant
from fresherpilot.outreach import contacts
from fresherpilot.jobs import dashboard
from fresherpilot.jobs import llm_filter
from fresherpilot.outreach import messages as outreach
from fresherpilot.core import users
from fresherpilot.jobs import visited
from fresherpilot.outreach import websearch
from fresherpilot.jobs.apify_jobs import fetch_latest_jobs, load_cache
from fresherpilot.jobs.ranker import quick_scores, score_jobs, strip_html

ROOT = Path(__file__).resolve().parents[1]
# If the process ever dies from a native crash, the stack of every thread is written here.
faulthandler.enable(open(ROOT / "crash.log", "a", encoding="utf-8"), all_threads=True)
STATIC = ROOT / "web"

app = FastAPI(title="FresherPilot")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
# The autofill browser extension calls this local API from its background worker.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome-extension|moz-extension)://.*$|^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

# Two independent background jobs: fetching (scraping the boards) and the AI checker. Fetching is
# never blocked by a long AI-check backlog; the checker picks up newly fetched jobs by itself.
# Each user has their own fetch + checker state (and they run with that user's keys and files).
_tasks: dict[str, dict[str, dict[str, Any]]] = {}
_task_lock = threading.Lock()


def _T() -> dict[str, dict[str, Any]]:
    return _tasks.setdefault(users.current() or "_", {
        "fetch": {"running": False, "done": 0, "total": 0, "waiting": [], "started": None, "error": None},
        "check": {"running": False, "stage": "idle", "done": 0, "total": 0, "error": None, "paused_until": 0.0},
    })


def _status() -> dict[str, Any]:
    return {"fetch": dict(_T()["fetch"]), "check": dict(_T()["check"]), "running": _T()["fetch"]["running"] or _T()["check"]["running"]}


def _normalized_jobs(payload: dict | None) -> list[dict[str, Any]]:
    return [
        {**job, "title": strip_html(job.get("title") or ""), "description": strip_html(job.get("description") or "")}
        for job in (payload or {}).get("jobs") or []
    ]


AI_BATCH = 10  # jobs per AI step; results appear in the list after each step (~1 minute)
_contacts_thread: threading.Thread | None = None


def _prefetch_contacts(jobs: list[dict[str, Any]]) -> None:
    """Look up people to contact in its own thread, so it never blocks fetching or the AI check."""
    global _contacts_thread
    if _contacts_thread and _contacts_thread.is_alive():
        return
    _contacts_thread = users.thread(contacts.prefetch, jobs)


def _run_fetch() -> None:
    try:
        started = time.time()
        fetch_latest_jobs(progress=lambda d, n, pending: _T()["fetch"].update(
            done=d, total=n, waiting=[p.capitalize() for p in pending], started=started))
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _T()["fetch"]["error"] = str(exc)
    finally:
        _T()["fetch"].update(running=False, waiting=[], started=None)
    _T()["check"]["paused_until"] = 0.0
    _start_checker()  # check the new jobs


def _run_checker() -> None:
    """Keep checking unchecked jobs (best matches first, 10 at a time) until none are left or the
    Groq quota runs out. New jobs from a fetch that happens meanwhile are picked up automatically."""
    from fresherpilot.apply import autoapply

    errors: dict[str, str] = {}
    checked = 0
    try:
        while True:
            while autoapply.STATE.get("running"):  # auto-apply gets the AI quota first
                _T()["check"].update(stage="paused_for_autoapply")
                time.sleep(5)
            jobs = _normalized_jobs(load_cache())
            unchecked = [j for j in jobs if llm_filter.cached_verdict(j) is None and llm_filter.job_key(j) not in errors]
            if not unchecked:
                break
            _T()["check"].update(stage="ranking" if checked == 0 else "ai_check", total=checked + len(unchecked), done=checked)
            order = quick_scores(unchecked)  # cached after the first time
            batch = [j for _, j in sorted(zip(order, unchecked), key=lambda x: -x[0])][:AI_BATCH]
            _T()["check"].update(stage="ai_check")
            llm_filter.prescreen(batch)  # board data + title screen: cheap rejects
            errors.update(llm_filter.judge_jobs(batch))
            errors.update(llm_filter.judge_intern_credit(batch))  # 1-year jobs: do internships count?
            errors.update(llm_filter.judge_fit(batch))
            errors.update(llm_filter.judge_instructions(batch))
            score_jobs([j for j in batch if (llm_filter.cached_verdict(j) or {}).get("decision") == "keep"])
            checked += len(batch)
            _T()["check"].update(done=checked)
        if errors:
            _T()["check"]["error"] = f"AI check failed for {len(errors)} job(s); they stay 'Pending AI check' and are retried later."
        approved_new = [j for j in _normalized_jobs(load_cache())
                        if (llm_filter.cached_verdict(j) or {}).get("decision") == "keep" and not visited.visited_at(j)]
        if approved_new:
            _prefetch_contacts(approved_new)
    except llm_filter.DailyLimitReached:
        left = sum(1 for j in _normalized_jobs(load_cache()) if llm_filter.cached_verdict(j) is None)
        _T()["check"]["error"] = (f"Groq's free daily AI limit is used up. {left} job(s) stay 'Pending AI check' and are "
                           "checked automatically when it resets.")
        _T()["check"]["paused_until"] = time.time() + 30 * 60  # try again in 30 minutes
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _T()["check"]["error"] = str(exc)
        _T()["check"]["paused_until"] = time.time() + 5 * 60
    finally:
        _T()["check"].update(running=False, stage="idle")


def _start_fetch() -> bool:
    with _task_lock:
        if _T()["fetch"]["running"]:
            return False
        _T()["fetch"].update(running=True, error=None, done=0, total=0, waiting=[], started=time.time())
    users.thread(_run_fetch)
    return True


def _start_checker() -> bool:
    with _task_lock:
        if _T()["check"]["running"] or time.time() < _T()["check"].get("paused_until", 0):
            return False
        _T()["check"].update(running=True, error=None, done=0, total=0, stage="starting")
    users.thread(_run_checker)
    return True


def _pack(payload: dict | None) -> dict[str, Any]:
    jobs = _normalized_jobs(payload)
    seen = visited.load_all()
    apps = applicant.applications()
    reached = outreach.sent_by_job()
    for job in jobs:
        verdict = llm_filter.cached_verdict(job) or {}
        job["uid"] = visited.uid(job)
        job["visited_at"] = visited.visited_at(job, seen)
        job["status"] = verdict.get("decision", "unchecked")  # keep | reject | unchecked
        job["ai_category"] = verdict.get("category")
        job["ai_reason"] = verdict.get("reason")
        job["ai_years"] = verdict.get("min_years_required")
        job["ai_freshers"] = verdict.get("freshers_welcome")
        job["ai_evidence"] = verdict.get("experience_evidence")
        job["ai_employment"] = verdict.get("employment_type")
        job["ai_seniority"] = verdict.get("seniority")
        job["ai_role"] = verdict.get("role_type")
        job["ai_fit"] = verdict.get("fit")
        job["ai_stack"] = verdict.get("main_stack") or []
        job["ai_fit_reason"] = verdict.get("fit_reason")
        job["ai_intern_credit"] = verdict.get("intern_credit")
        job["apply_instructions"] = verdict.get("apply_instructions")
        job["contacts_summary"] = contacts.summary(job) if job["status"] != "reject" else None
        job["employer"] = contacts.resolve_company(job)  # real employer; "" when the board hides it
        job["application"] = apps.get(job["uid"])
        job["outreach"] = reached.get(job["uid"])
    # Only AI-approved jobs are scored here (their embeddings are already cached by the
    # background task); embedding hundreds of unchecked jobs would stall the page for minutes.
    score_jobs([j for j in jobs if j["status"] == "keep"])
    for job in jobs:
        job.setdefault("score", None)
        job.setdefault("matched_skills", [])
    jobs.sort(key=lambda j: (-(j["score"] or 0), j.get("posted_days") if j.get("posted_days") is not None else 99))
    counts = Counter(j["status"] for j in jobs)
    return {
        "fetched_at": (payload or {}).get("fetched_at"),
        "raw_counts": (payload or {}).get("raw_counts") or {},
        "errors": (payload or {}).get("errors") or {},
        "total_fetched": len(jobs),
        "total_kept": counts["keep"],
        "total_rejected": counts["reject"],
        "total_unchecked": counts["unchecked"],
        "search_provider": websearch.provider(),
        "total_new": sum(1 for j in jobs if j["status"] == "keep" and not j["visited_at"]),
        "rejected_by_category": dict(Counter(j["ai_category"] for j in jobs if j["status"] == "reject")),
        "fetch_report": (payload or {}).get("fetch_report") or {},
        "skipped_sources": (payload or {}).get("skipped_sources") or [],
        "task": _status(),
        "jobs": jobs,
    }


# ---------------------------------------------------------------- accounts
#
# Every /api call runs for the logged-in user: the session cookie (the app) or the X-JF-Token header
# (the browser extension). Pages redirect to /login when nobody is logged in.

PUBLIC = ("/api/auth/", "/static/", "/login", "/favicon")
COOKIE = "jf_session"


class UserContext:
    """Pure ASGI middleware: sets the current user for the whole request (incl. its worker thread)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        cookies = dict(c.strip().split("=", 1) for c in headers.get("cookie", "").split(";") if "=" in c)
        token = headers.get("x-jf-token") or cookies.get(COOKIE)
        uid = users.session_user(token)
        path = scope.get("path", "")
        if not uid and not path.startswith(PUBLIC):
            if path.startswith("/api/"):
                return await JSONResponse({"detail": "Please log in"}, status_code=401)(scope, receive, send)
            if path in ("/", "/onboarding", "/account") or path.endswith(".html"):
                return await RedirectResponse("/login")(scope, receive, send)
        reset = users.set_current(uid)
        try:
            await self.app(scope, receive, send)
        finally:
            users._current.reset(reset)


app.add_middleware(UserContext)


def _token(request: Request) -> str | None:
    return request.headers.get("x-jf-token") or request.cookies.get(COOKIE)


@app.get("/")
def index():
    if not users.prefs().get("onboarded", True):
        return RedirectResponse("/onboarding")
    return FileResponse(STATIC / "index.html")


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(STATIC / "login.html")


@app.get("/onboarding")
def onboarding_page() -> FileResponse:
    return FileResponse(STATIC / "onboarding.html")


@app.get("/account")
def account_page() -> FileResponse:
    return FileResponse(STATIC / "account.html")


class Credentials(BaseModel):
    username: str
    password: str


def _with_cookie(token: str, body: dict) -> JSONResponse:
    resp = JSONResponse(body)
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=users.SESSION_DAYS * 86400)
    return resp


@app.get("/api/auth/state")
def auth_state() -> dict:
    return {"has_accounts": bool(users.list_usernames())}


@app.post("/api/auth/register")
def register(body: Credentials) -> JSONResponse:
    try:
        user = users.register(body.username, body.password)
        token = users.login(body.username, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not user["owner"]:
        reset = users.set_current(user["id"])
        try:
            users.save_prefs({"onboarded": False})
        finally:
            users._current.reset(reset)
    return _with_cookie(token, {"user": user, "next": "/" if user["owner"] else "/onboarding"})


@app.post("/api/auth/login")
def do_login(body: Credentials) -> JSONResponse:
    try:
        token = users.login(body.username, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    uid = users.session_user(token)
    reset = users.set_current(uid)
    try:
        nxt = "/" if users.prefs().get("onboarded", True) else "/onboarding"
    finally:
        users._current.reset(reset)
    return _with_cookie(token, {"user": users.get(uid), "next": nxt})


@app.post("/api/auth/logout")
def do_logout(request: Request) -> JSONResponse:
    users.logout(_token(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
def me(request: Request) -> dict:
    uid = users.current()
    return {"user": users.get(uid), "keys": users.keys_status(), "prefs": users.prefs(),
            "extension_token": users.extension_token(_token(request))}


@app.put("/api/account/keys")
def put_keys(request: Request, body: dict) -> dict:
    try:
        return {"keys": users.save_keys(_token(request), body)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/account/prefs")
def put_prefs(body: dict) -> dict:
    return {"prefs": users.save_prefs(body)}


class PasswordChange(BaseModel):
    old: str
    new: str


@app.post("/api/account/password")
def change_password(request: Request, body: PasswordChange) -> dict:
    try:
        users.change_password(_token(request), body.old, body.new)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/onboarding/resume")
async def onboarding_resume(file: UploadFile | None = File(None), about: str = Form("")) -> dict:
    """Save the resume (PDF) or 'about me' text, and let the AI draft the profile + search settings."""
    from fresherpilot.apply import onboarding

    data = await file.read() if file is not None else b""
    if file is not None and len(data) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Resume too large (max 8 MB)")
    try:
        return onboarding.analyse(data, file.filename if file is not None else "", about)
    except llm_filter.DailyLimitReached as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/onboarding/save")
def onboarding_save(body: dict) -> dict:
    from fresherpilot.apply import onboarding

    return onboarding.save(body.get("profile") or {}, body.get("prefs") or {})


@app.get("/api/jobs")
def cached_jobs() -> dict:
    payload = load_cache()
    # Unchecked jobs (new, or left over when Groq's daily limit ran out) start the checker; after a
    # quota error it waits 30 minutes before trying again.
    if (payload and not _T()["check"]["running"] and not os.getenv("JF_NO_AUTOCHECK")
            and any(llm_filter.cached_verdict(j) is None for j in _normalized_jobs(payload))):
        _start_checker()
    return _pack(payload)


@app.get("/api/usage")
def usage() -> dict:
    """Groq tokens used today, per AI step and per model."""
    return llm_filter.usage_today()


@app.get("/api/status")
def status() -> dict:
    return _status()


@app.post("/api/fetch", status_code=202)
def fetch_jobs() -> dict:
    if not _start_fetch():
        raise HTTPException(status_code=409, detail="A fetch is already running")
    return _status()


@app.get("/api/contacts")
def job_contacts(uid: str, refresh: bool = False) -> dict:
    """People / emails / phones to reach out to for one job (searches the web if not cached)."""
    job = next((j for j in _normalized_jobs(load_cache()) if visited.uid(j) == uid), None)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found in the latest fetch")
    if refresh:
        websearch.reset_block()
    result = contacts.get_contacts(job, refresh=refresh)
    result["provider"] = websearch.provider()
    return result


class VisitedRequest(BaseModel):
    uids: list[str]
    visited: bool = True


@app.post("/api/visited")
def set_visited(body: VisitedRequest) -> dict:
    wanted = set(body.uids)
    jobs = [j for j in _normalized_jobs(load_cache()) if visited.uid(j) in wanted]
    return {"updated": visited.mark(jobs, body.visited)}


# ---------------------------------------------------------------- applying


def _job_by_uid(uid: str) -> dict[str, Any]:
    job = next((j for j in _normalized_jobs(load_cache()) if visited.uid(j) == uid), None)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found in the saved list")
    job["employer"] = contacts.resolve_company(job)
    return job


@app.get("/api/profile")
def get_profile() -> dict:
    profile = applicant.load_profile()
    return {"profile": profile, "missing": applicant.missing_fields(profile)}


@app.put("/api/profile")
def put_profile(body: dict) -> dict:
    profile = applicant.save_profile(body.get("profile") or body)
    return {"profile": profile, "missing": applicant.missing_fields(profile)}


@app.get("/api/resume")
def get_resume() -> FileResponse:
    path = applicant.resume_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Resume file not found")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


class AutofillRequest(BaseModel):
    uid: str = ""
    url: str = ""
    title: str = ""
    page_text: str = ""
    fields: list[dict[str, Any]]


@app.post("/api/autofill")
def autofill(body: AutofillRequest) -> dict:
    """Answers for the fields of an application form (sent by the browser extension)."""
    jobs = _normalized_jobs(load_cache())
    for j in jobs:
        j["employer"] = contacts.resolve_company(j)
    job = next((j for j in jobs if visited.uid(j) == body.uid), None) if body.uid else None
    job = job or applicant.find_job(body.url, f"{body.title}\n{body.page_text}", jobs)
    if job:
        context = (f"Job: {job['title']} at {job.get('employer') or job.get('company')} ({job.get('location')})\n"
                   f"{(job.get('description') or '')[:4000]}")
    else:
        context = f"Page: {body.title}\n{body.url}\n{body.page_text[:4000]}"
    answers = applicant.autofill(body.fields, context)
    instructions = (llm_filter.cached_verdict(job) or {}).get("apply_instructions") if job else None
    return {
        "answers": answers,
        "job": {"uid": visited.uid(job), "title": job["title"], "company": job.get("employer") or job.get("company"),
                "instructions": instructions} if job else None,
        "missing_profile": applicant.missing_fields(applicant.load_profile()),
    }


@app.get("/api/applykit")
def get_apply_kit(uid: str, refresh: bool = False, generate: bool = True) -> dict:
    job = _job_by_uid(uid)
    if not generate and not refresh:
        return {"kit": applicant.cached_kit(job)}
    try:
        return {"kit": applicant.apply_kit(job, refresh=refresh)}
    except llm_filter.DailyLimitReached as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


class ApplicationRequest(BaseModel):
    uid: str
    status: str | None = "applied"
    note: str | None = None


@app.post("/api/application")
def set_application(body: ApplicationRequest) -> dict:
    if body.status and body.status not in applicant.STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {applicant.STATUSES}")
    job = _job_by_uid(body.uid)
    if body.status:
        visited.mark([job], True)  # applying implies you've seen it
    return {"application": applicant.set_application(body.uid, body.status, body.note)}


# ---------------------------------------------------------------- auto-apply
# The extension does the clicking in the user's own browser; these endpoints are its brain.


class AutoApplyRequest(BaseModel):
    uids: list[str] | None = None  # one job (its Auto-apply button) or None = best matches
    max: int = 25


@app.post("/api/autoapply")
def start_autoapply(body: AutoApplyRequest) -> dict:
    from fresherpilot.apply import autoapply

    try:
        return autoapply.start(body.uids, body.max)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/autoapply")
def autoapply_status() -> dict:
    from fresherpilot.apply import autoapply

    return autoapply.status()


@app.post("/api/autoapply/decide")
def autoapply_decide(body: dict) -> dict:
    from fresherpilot.apply import autoapply

    try:
        return autoapply.decide(body)
    except llm_filter.DailyLimitReached as exc:
        return {"action": "needs_user", "key": "", "reason": f"AI quota used up for today: {exc}"}


class AutoApplyEvent(BaseModel):
    event: str
    uid: str | None = None
    detail: str = ""
    waiting: dict[str, Any] | None = None


@app.post("/api/autoapply/event")
def autoapply_event(body: AutoApplyEvent) -> dict:
    from fresherpilot.apply import autoapply

    return autoapply.report(body.event, body.uid, body.detail, body.waiting)


class AutoApplyCommand(BaseModel):
    action: str  # continue | skip | stop


@app.post("/api/autoapply/command")
def autoapply_command(body: AutoApplyCommand) -> dict:
    from fresherpilot.apply import autoapply

    if body.action not in ("continue", "skip", "stop"):
        raise HTTPException(status_code=400, detail="action must be continue, skip or stop")
    autoapply.command(body.action)
    return {"ok": True}


@app.get("/api/autoapply/command")
def autoapply_take_command() -> dict:
    from fresherpilot.apply import autoapply

    return {"command": autoapply.take_command()}


# ---------------------------------------------------------------- dashboard + outreach


@app.get("/api/dashboard")
def get_dashboard() -> dict:
    return dashboard.build(_normalized_jobs(load_cache()))


@app.get("/api/outreach")
def outreach_preview(uid: str) -> dict:
    """Draft + recipients for one job (uses the saved contacts search; searches if there is none)."""
    job = _job_by_uid(uid)
    return outreach.preview(job, contacts.get_contacts(job))


@app.put("/api/outreach/settings")
def outreach_settings(body: dict) -> dict:
    return outreach.save_settings(body)


class OutreachSend(BaseModel):
    uid: str
    emails: list[dict[str, str]] = []
    phones: list[dict[str, str]] = []
    email_subject: str | None = None
    email_body: str | None = None
    whatsapp_body: str | None = None
    save_template: bool = True
    show_draft: bool | None = None


@app.post("/api/outreach/send")
def outreach_send(body: OutreachSend) -> dict:
    """Send the emails now; return the WhatsApp messages for the extension to send."""
    job = _job_by_uid(body.uid)
    s = outreach.settings()
    changes = {k: v for k, v in {"email_subject": body.email_subject, "email_body": body.email_body,
                                 "whatsapp_body": body.whatsapp_body, "show_draft": body.show_draft}.items()
               if v is not None}
    if body.save_template and changes:
        s = outreach.save_settings(changes)
    else:
        s = {**s, **changes}
    result: dict[str, Any] = {"emails": [], "whatsapp": [], "errors": [], "whatsapp_gap": outreach.WHATSAPP_GAP}
    if body.emails:
        try:
            result["emails"] = outreach.send_emails(job, body.uid, body.emails, s["email_subject"], s["email_body"],
                                                    s.get("attach_resume", True))
        except Exception as exc:  # noqa: BLE001 — shown to the user
            msg = str(exc)
            if "Username and Password not accepted" in msg or "535" in msg:
                msg = "Gmail rejected the app password. Create a new one at myaccount.google.com/apppasswords."
            result["errors"].append(f"Email: {msg}")
    if body.phones:
        try:
            result["whatsapp"] = outreach.whatsapp_jobs(job, body.uid, body.phones, s["whatsapp_body"])
        except RuntimeError as exc:
            result["errors"].append(f"WhatsApp: {exc}")
    if result["emails"] or result["whatsapp"]:
        visited.mark([job], True)
    return result


class OutreachEvent(BaseModel):
    uid: str
    channel: str = "whatsapp"
    to: str
    ok: bool
    error: str = ""


@app.post("/api/outreach/event")
def outreach_event(body: OutreachEvent) -> dict:
    """Result of one WhatsApp message, reported by the extension."""
    job = next((j for j in _normalized_jobs(load_cache()) if visited.uid(j) == body.uid), {})
    return outreach.record(body.uid, body.channel, body.to, body.ok, body.error,
                           contacts.resolve_company(job) if job else "", job.get("title", ""))


@app.get("/api/outreach/job")
def outreach_job(uid: str) -> dict:
    return {"sent": [e for e in outreach.log() if e["uid"] == uid], "counts": outreach.for_job(uid)}


def run() -> None:
    import uvicorn

    print("FresherPilot running at http://localhost:8000  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    run()
