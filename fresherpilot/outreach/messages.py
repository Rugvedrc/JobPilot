"""Reach out to the people found for a job: one email (Gmail) and/or WhatsApp message each.

Messages come from YOUR templates with placeholders ({name}, {role}, {company}, {skills}…), filled
in by code, so sending costs no AI tokens. Emails go out through Gmail's SMTP with an app password;
WhatsApp messages are sent by the browser extension through WhatsApp Web (your logged-in account).

Guard rails: an address is contacted once (re-sending needs an explicit tick), daily caps, a short
random pause between emails, and only real-looking addresses / Indian mobile numbers are used.
"""

from __future__ import annotations

import json
import os
import random
import re
import smtplib
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from fresherpilot.apply import applicant
from fresherpilot.outreach import contacts

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
def _p(name: str) -> Path:
    from fresherpilot.core import users

    return users.path(name)
_lock = threading.Lock()

DAILY_EMAIL_CAP = 60       # Gmail allows ~500/day; cold email from a personal account should stay far lower
DAILY_WHATSAPP_CAP = 15    # WhatsApp bans numbers that message many strangers; keep it small
EMAIL_GAP = (4.0, 9.0)     # seconds between two emails
WHATSAPP_GAP = (25, 45)    # seconds between two WhatsApp messages (used by the extension)

DEFAULT_SETTINGS: dict[str, Any] = {
    "email_subject": "Application for {role} at {company} | {my_name}",
    "email_body": (
        "Hi {name},\n\n"
        "I came across the {role} opening at {company} and I'm very interested. I'm a {headline} "
        "and I work with {skills}.\n\n"
        "I've attached my resume. I'd be glad to talk if my profile fits what you're looking for.\n\n"
        "Thanks,\n{my_name}\n{phone} | {linkedin}\n{github}"
    ),
    "whatsapp_body": (
        "Hi {name}, I'm {first_name}, a {headline}. I saw the {role} opening at {company} "
        "and I'm interested. I work with {skills}. Could I share my resume?\n{linkedin}"
    ),
    "attach_resume": True,
    "show_draft": True,  # show the draft before sending; turned off with "don't show again"
}

PLACEHOLDERS = {
    "name": "first name of the person, or 'there'",
    "role": "job title",
    "company": "company name",
    "skills": "your skills this job asks for (up to 3)",
    "headline": "who you are, e.g. \'2026 B.E. IT graduate\' (Settings)",
    "my_name": "your full name",
    "first_name": "your first name",
    "phone": "your phone",
    "email": "your email",
    "linkedin": "your LinkedIn",
    "github": "your GitHub",
    "portfolio": "your portfolio",
    "job_link": "link to the job post",
}

# Inboxes that never reach a hiring person.
_BAD_INBOX = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|privacy|abuse|legal|unsubscribe|press|media|investor|ir|sales|billing|"
    r"support|help|helpdesk|security|postmaster|webmaster|admin|accounts?|customercare|feedback|marketing|grievance)"
    r"([._-].*)?@", re.I)
_EMAIL_OK = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
_FILE_TLD = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js)$", re.I)
# Placeholder addresses copied from website templates / examples.
_FAKE_DOMAIN = re.compile(r"@(example|mysite|yoursite|yourdomain|domain|yourcompany|company|email|test|sample|website|mail)\.(com|in|org|net|co)$", re.I)


# ---------------------------------------------------------------- storage


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except json.JSONDecodeError:
        return default


def settings() -> dict[str, Any]:
    return {**DEFAULT_SETTINGS, **_read(_p("outreach_settings.json"), {})}


def save_settings(changes: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        cur = {**_read(_p("outreach_settings.json"), {}), **{k: v for k, v in changes.items() if k in DEFAULT_SETTINGS}}
        _p("outreach_settings.json").write_text(json.dumps(cur, indent=1, ensure_ascii=False), encoding="utf-8")
    return settings()


def log() -> list[dict[str, Any]]:
    return _read(_p("outreach_log.json"), [])


def record(uid: str, channel: str, to: str, ok: bool, error: str = "", company: str = "", role: str = "") -> dict[str, Any]:
    entry = {"at": datetime.now(timezone.utc).isoformat(), "uid": uid, "channel": channel, "to": to,
             "ok": ok, "error": error[:300], "company": company, "role": role}
    with _lock:
        data = _read(_p("outreach_log.json"), [])
        data.append(entry)
        _p("outreach_log.json").write_text(json.dumps(data[-5000:], indent=1, ensure_ascii=False), encoding="utf-8")
    return entry


def _sent_map() -> dict[str, str]:
    """address -> when it was last contacted successfully."""
    return {e["to"].lower(): e["at"] for e in log() if e.get("ok")}


def _sent_today(channel: str) -> int:
    today = datetime.now().astimezone().date()
    return sum(1 for e in log() if e.get("ok") and e["channel"] == channel
               and datetime.fromisoformat(e["at"]).astimezone().date() == today)


def for_job(uid: str) -> dict[str, int]:
    out = {"email": 0, "whatsapp": 0}
    for e in log():
        if e.get("ok") and e["uid"] == uid and e["channel"] in out:
            out[e["channel"]] += 1
    return out


def sent_by_job() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for e in log():
        if e.get("ok"):
            out.setdefault(e["uid"], {"email": 0, "whatsapp": 0})[e["channel"]] += 1
    return out


# ---------------------------------------------------------------- recipients


def whatsapp_number(raw: str) -> str:
    """Digits for wa.me (country code + number) if this looks like a mobile number, else ''."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "91" + digits
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return digits
    return ""


def _email_ok(addr: str) -> bool:
    a = (addr or "").strip()
    return (bool(_EMAIL_OK.match(a)) and not _BAD_INBOX.match(a) and not _FILE_TLD.search(a)
            and not _FAKE_DOMAIN.search(a) and "example." not in a.lower())


def recipients(job: dict[str, Any], found: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Emails and WhatsApp numbers from the contacts search, with where each came from."""
    sent = _sent_map()
    jd = found.get("from_description") or {}
    c = found.get("company") or {}
    g = found.get("guessed") or {}
    emails: dict[str, dict[str, Any]] = {}

    def add_email(addr: str, origin: str, name: str = "", default: bool = True) -> None:
        addr = (addr or "").strip().strip(".").lower()
        if not _email_ok(addr) or addr in emails:
            return
        emails[addr] = {"to": addr, "origin": origin, "name": name, "selected": default and addr not in sent,
                        "sent_at": sent.get(addr)}

    for e in jd.get("emails") or []:
        add_email(e, "job post")
    for e in c.get("emails") or []:
        add_email(e.get("value", ""), "found online")
    # Guessed inboxes may bounce (bounces hurt your Gmail reputation): offered, but not ticked.
    for e in g.get("emails") or []:
        add_email(e, "guessed", default=False)

    phones: dict[str, dict[str, Any]] = {}
    for raw, origin in [(p, "job post") for p in jd.get("phones") or []] + \
                       [(p.get("value", ""), "found online") for p in c.get("phones") or []]:
        num = whatsapp_number(raw)
        if num and num not in phones:
            phones[num] = {"to": num, "display": raw, "origin": origin, "name": "",
                           "selected": num not in sent, "sent_at": sent.get(num)}
    return {"emails": list(emails.values()), "phones": list(phones.values())}


# ---------------------------------------------------------------- templates


def clean_role(title: str) -> str:
    t = re.sub(r"^\s*(urgent(ly)?\s+)?(we\s+are\s+)?(hiring|opening|vacancy|requirement)\s+((for|of)\s+)?(an?\s+)?", "",
               title or "", flags=re.I)
    t = re.sub(r"\s*[-|–,:]?\s*\(?20\d\d\s+(batch|pass ?outs?|graduates?)\)?.*$", "", t, flags=re.I)
    t = re.sub(r"[\(\[][^)\]]*(yrs?|years?|exp|fresher|lpa|ctc|remote|wfh|hybrid|onsite)[^)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(r"\s*[-|–,:]\s*(freshers?|entry level|0\s*-\s*\d+\s*(yrs?|years?)|immediate joiners?).*$", "", t, flags=re.I)
    t = re.sub(r"\b(freshers?|trainees? hiring)\b\s*$", "", t, flags=re.I)
    return re.sub(r"\s{2,}", " ", t).strip(" -|,:") or (title or "").strip()


def _skills_for(job: dict[str, Any]) -> str:
    from fresherpilot.jobs import candidate

    text = f"{job.get('title', '')}\n{job.get('description', '')}"
    # The skills this job mentions most (ties: resume order); one per family (SQL / PostgreSQL / MySQL).
    counts = [(len(pat.findall(text)), i, skill) for i, (skill, pat) in enumerate(candidate.skill_patterns().items())]
    picks: list[str] = []
    for n, _, skill in sorted((c for c in counts if c[0]), key=lambda c: (-c[0], c[1])):
        if not any(skill.lower() in p.lower() or p.lower() in skill.lower() or
                   {skill, p} <= {"SQL", "PostgreSQL", "MySQL"} for p in picks):
            picks.append(skill)
    picks = picks[:3] or ["Python", "LLMs", "RAG"]
    return picks[0] if len(picks) == 1 else ", ".join(picks[:-1]) + " and " + picks[-1]


def _first_name(full: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z]+", full or "") if len(w) > 1]
    return words[0].capitalize() if words else ""


def _headline() -> str:
    from fresherpilot.core import users

    return users.prefs().get("headline") or "recent graduate"


def values(job: dict[str, Any], person: str = "") -> dict[str, str]:
    p = applicant.load_profile()
    me, links = p.get("personal", {}), p.get("links", {})
    phone = f"{me.get('phone_country_code', '')} {me.get('phone', '')}".strip()
    return {
        "name": _first_name(person) or "there",
        "role": clean_role(job.get("title", "")),
        "company": contacts.resolve_company(job) or job.get("company") or "your company",
        "skills": _skills_for(job),
        "headline": _headline(),
        "my_name": " ".join(x for x in [me.get("first_name"), me.get("last_name")] if x) or me.get("full_name", ""),
        "first_name": me.get("first_name", ""),
        "phone": phone,
        "email": me.get("email", ""),
        "linkedin": links.get("linkedin", ""),
        "github": links.get("github", ""),
        "portfolio": links.get("portfolio", ""),
        "job_link": job.get("url", ""),
    }


def render(template: str, vals: dict[str, str]) -> str:
    out = re.sub(r"\{(\w+)\}", lambda m: vals.get(m.group(1), m.group(0)), template or "")
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", out)).strip()


def preview(job: dict[str, Any], found: dict[str, Any]) -> dict[str, Any]:
    s = settings()
    vals = values(job)
    return {
        "settings": s,
        "placeholders": PLACEHOLDERS,
        "values": vals,
        "recipients": recipients(job, found),
        "email_ready": email_configured(),
        "sender": sender_address(),
        "sent_today": {"email": _sent_today("email"), "whatsapp": _sent_today("whatsapp")},
        "caps": {"email": DAILY_EMAIL_CAP, "whatsapp": DAILY_WHATSAPP_CAP},
        "rendered": {
            "email_subject": render(s["email_subject"], vals),
            "email_body": render(s["email_body"], vals),
            "whatsapp_body": render(s["whatsapp_body"], vals),
        },
    }


# ---------------------------------------------------------------- email (Gmail SMTP)


def sender_address() -> str:
    from fresherpilot.core import users

    return (users.key("GMAIL_ADDRESS") or applicant.load_profile().get("personal", {}).get("email") or "").strip()


def email_configured() -> bool:
    from fresherpilot.core import users

    return bool(sender_address() and users.key("GMAIL_APP_PASSWORD"))


def send_emails(job: dict[str, Any], uid: str, items: list[dict[str, str]], subject_tpl: str, body_tpl: str,
                attach_resume: bool = True) -> list[dict[str, Any]]:
    """Send one email per recipient (never CC'd together). items: [{"to":..., "name":...}]."""
    if not email_configured():
        raise RuntimeError("Gmail isn't set up: add GMAIL_APP_PASSWORD to .env (see the README).")
    left = DAILY_EMAIL_CAP - _sent_today("email")
    if left <= 0:
        raise RuntimeError(f"Daily email limit reached ({DAILY_EMAIL_CAP}); try again tomorrow.")
    items = [i for i in items if _email_ok(i.get("to", ""))][: min(10, left)]
    me = sender_address()
    profile = applicant.load_profile()
    my_name = " ".join(x for x in [profile["personal"].get("first_name"), profile["personal"].get("last_name")] if x)
    resume = applicant.resume_path(profile) if attach_resume else None
    pdf = resume.read_bytes() if resume and resume.exists() else None
    company, role = contacts.resolve_company(job) or job.get("company", ""), job.get("title", "")
    results = []
    from fresherpilot.core import users

    password = re.sub(r"\s", "", users.key("GMAIL_APP_PASSWORD"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(me, password)
        for n, item in enumerate(items):
            vals = values(job, item.get("name", ""))
            msg = EmailMessage()
            msg["From"] = f"{my_name} <{me}>" if my_name else me
            msg["To"] = item["to"]
            msg["Subject"] = render(subject_tpl, vals).replace("\n", " ")
            msg.set_content(render(body_tpl, vals))
            if pdf:
                msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=resume.name)
            try:
                smtp.send_message(msg)
                results.append(record(uid, "email", item["to"], True, company=company, role=role))
            except smtplib.SMTPException as exc:
                results.append(record(uid, "email", item["to"], False, str(exc), company, role))
            if n < len(items) - 1:
                time.sleep(random.uniform(*EMAIL_GAP))
    return results


def whatsapp_jobs(job: dict[str, Any], uid: str, items: list[dict[str, str]], body_tpl: str) -> list[dict[str, Any]]:
    """Messages for the extension to send through WhatsApp Web."""
    left = DAILY_WHATSAPP_CAP - _sent_today("whatsapp")
    if left <= 0:
        raise RuntimeError(f"Daily WhatsApp limit reached ({DAILY_WHATSAPP_CAP}); try again tomorrow.")
    out = []
    for item in items[:left]:
        num = whatsapp_number(item.get("to", ""))
        if num:
            out.append({"uid": uid, "phone": num, "text": render(body_tpl, values(job, item.get("name", "")))})
    return out
