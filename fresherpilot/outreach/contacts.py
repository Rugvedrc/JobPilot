"""Find people and contact details to reach out to for a job.

For each company (searched on Google via websearch.py):
  - emails / phone numbers / LinkedIn links written in the job description itself
  - LinkedIn profiles of recruiters / HR / talent acquisition at the company
  - LinkedIn profiles of hiring leaders (engineering managers, CTO, founders)
  - public HR / careers emails and phone numbers, and the company website

When a job board hides the employer (e.g. Indeed shows "Indeed"), the employer is
identified first: a distinctive sentence of the JD is searched on Google to find the
same job posted elsewhere, and an LLM reads those results to name the company.

Everything comes from public search-result snippets; nothing is scraped from
personal pages. Results are cached (per company for a week).
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from fresherpilot.outreach import domains
from fresherpilot.outreach import websearch
from fresherpilot.outreach.websearch import SearchBlocked

ROOT = Path(__file__).resolve().parents[2]
CACHE_PATH = ROOT / "contacts_cache.json"
CACHE_DAYS = 7

_lock = threading.Lock()

# ---------------------------------------------------------------- patterns

EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?![\w-])", re.I)
PHONE = re.compile(r"(?<!\d)(?:\+91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)|(?<!\d)\+\d{1,3}[\s-]?\d{2,5}[\s-]?\d{3,4}[\s-]?\d{3,4}(?!\d)")
LINKEDIN_PROFILE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%-]+/?", re.I)

JUNK_EMAIL = re.compile(r"^[^@]@|example\.|\.(?:png|jpe?g|gif|svg|webp)$|sentry|noreply|no-reply|@(?:email|domain)\.com$", re.I)
GENERIC_INBOX = re.compile(r"^(?:hr|careers?|jobs?|recruit\w*|talent|hiring|resume|cv|people|info|contact|hello)[\w.-]*@", re.I)
RECRUITER = re.compile(r"recruit|talent|\bhr\b|human resource|hiring|people (?:partner|operations)|staffing|acquisition", re.I)
LEADER = re.compile(r"founder|\bceo\b|\bcto\b|\bvp\b|director|head of|engineering manager|\bmanager\b|tech(?:nical)? lead|\blead\b|principal", re.I)

# Words that don't identify a company ("Pvt Ltd", "Technologies"...)
COMPANY_NOISE = {
    "pvt", "private", "ltd", "limited", "llp", "inc", "llc", "corp", "corporation", "co", "company", "the",
    "technologies", "technology", "tech", "solutions", "services", "software", "systems", "labs", "global",
    "india", "consulting", "consultancy", "group", "international", "infotech", "it", "and", "of",
}
NOT_COMPANY_SITES = {
    "linkedin.com", "naukri.com", "indeed.com", "glassdoor.com", "glassdoor.co.in", "ambitionbox.com", "wellfound.com",
    "cutshort.io", "instahyre.com", "foundit.in", "monster.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "wikipedia.org", "crunchbase.com", "zaubacorp.com", "justdial.com", "apollo.io", "zoominfo.com",
    "rocketreach.co", "signalhire.com", "lusha.com", "contactout.com", "theorg.com", "google.com",
}
# Job boards that put their own name in the company field when the employer is hidden.
HIDDEN_COMPANY = {"unknown", "confidential", "indeed", "naukri", "linkedin", "hidden", "not disclosed", "stealth", "stealth startup", ""}


# ---------------------------------------------------------------- helpers

def company_tokens(company: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", (company or "").lower())
    return [w for w in words if w not in COMPANY_NOISE and len(w) > 1]


def company_key(company: str) -> str:
    return " ".join(company_tokens(company)) or re.sub(r"\W+", " ", (company or "").lower()).strip()


def _mentions_company(text: str, tokens: list[str]) -> bool:
    if not tokens:
        return False
    low = text.lower()
    # Every distinctive word must appear (so "Infosys" doesn't match "Info Edge").
    return all(re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", low) for t in tokens)


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _clean_phone(raw: str) -> str:
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("0") and len(digits) == 11:
        digits = "+91" + digits[1:]
    elif len(digits) == 10 and digits[0] in "6789":
        digits = "+91" + digits
    return digits


def _job_id(job: dict[str, Any]) -> str:
    return f"{job.get('source')}:{job.get('id')}"


# ---------------------------------------------------------------- cache

_mem: dict[str, Any] = {"mtime": None, "data": {}}


def _load() -> dict[str, Any]:
    """Contacts cache, re-read only when the file changed (it's consulted for every job in the list)."""
    mtime = CACHE_PATH.stat().st_mtime if CACHE_PATH.exists() else None
    if mtime != _mem["mtime"]:
        try:
            _mem["data"] = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}
        except json.JSONDecodeError:
            _mem["data"] = {}
        _mem["mtime"] = mtime
    return _mem["data"]


def _save_entry(key: str, entry: dict[str, Any]) -> None:
    with _lock:
        data = _load()
        data[key] = entry
        CACHE_PATH.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def _fresh(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not entry:
        return None
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(entry["fetched_at"])
    except (KeyError, ValueError):
        return None
    return entry if age < timedelta(days=CACHE_DAYS) else None


def cached_company(company: str) -> dict[str, Any] | None:
    return _fresh(_load().get(company_key(company)))


# ---------------------------------------------------------------- hidden employers

def _jd_phrase(description: str) -> str:
    """A distinctive sentence from the JD, to find the same job posted elsewhere."""
    text = re.sub(r"[*_#•]+", " ", description or "")
    boring = re.compile(r"^(?:we are|we're|job (?:description|summary|type)|about (?:the|us)|location|experience|salary|responsibilit)", re.I)
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        words = sentence.split()
        if 9 <= len(words) <= 40 and not boring.match(sentence.strip()) and not re.search(r"\d{4,}|@|http", sentence):
            return " ".join(words[:14]).strip(" ,;:-")
    return ""


def _ask_llm(prompt: str) -> dict[str, Any]:
    """Small JSON question to Groq, using whichever model still has daily quota."""
    from fresherpilot.jobs import llm_filter
    from groq import RateLimitError

    client = llm_filter._client()
    for model in reversed(llm_filter.MODELS):  # smallest model first: this task is easy
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_completion_tokens": 1500,
        }
        if model.startswith("openai/gpt-oss"):
            kwargs["reasoning_effort"] = "low"
        try:
            content = client.chat.completions.create(**kwargs).choices[0].message.content or "{}"
            return json.loads(content)
        except (RateLimitError, json.JSONDecodeError):
            continue
    return {}


def identify_employer(job: dict[str, Any], refresh: bool = False) -> dict[str, Any]:
    """Name the employer of a listing whose board hides it. Cached per job."""
    key = f"employer::{_job_id(job)}"
    if not refresh:
        cached = _load().get(key)
        if cached:
            return cached
    title = job.get("title") or ""
    city = (job.get("location") or "").split(",")[0].strip()
    phrase = _jd_phrase(job.get("description") or "")
    queries = [f'"{phrase}"'] if phrase else []
    queries.append(f'"{title}" {city} hiring')

    results: list[dict[str, Any]] = []
    for query in queries:
        for r in websearch.search(query):
            if "indeed." not in r["url"] and r["url"] not in {x["url"] for x in results}:
                results.append(r)
        if len(results) >= 6:
            break

    answer: dict[str, Any] = {}
    if results:
        listing = "\n".join(f"{i + 1}. {r['title']} | {r['url']} | {r['content'][:200]}" for i, r in enumerate(results[:10]))
        answer = _ask_llm(
            "A job board hid the employer of this job posting. Using ONLY the search results below, which company "
            "posted it? The same posting is often found on LinkedIn, Naukri, the company's careers page or other boards.\n\n"
            f"Job title: {title}\nLocation: {job.get('location')}\nPosting text: {(job.get('description') or '')[:500]}\n\n"
            f"Search results:\n{listing}\n\n"
            'Reply as JSON: {"company": "<name or empty>", "confidence": "high|medium|low", "evidence_url": "<url>"}. '
            "high = a result is clearly this same posting; low = guessing. Never name a job board or recruiting site as the company."
        )
    company = str(answer.get("company") or "").strip()
    ok = company and answer.get("confidence") in ("high", "medium") and company.lower() not in HIDDEN_COMPANY
    entry = {
        "company": company if ok else "",
        "confidence": answer.get("confidence") or "low",
        "evidence": answer.get("evidence_url") or "",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if answer or not results:  # no answer because the LLM was unavailable -> try again next time
        _save_entry(key, entry)
    return entry


def resolve_company(job: dict[str, Any]) -> str:
    """The real employer name (no searching). "" when the board hides it and it isn't known yet."""
    company = (job.get("company") or "").strip()
    if company.lower() not in HIDDEN_COMPANY:
        return company
    m = re.search(
        r"\bCompany(?: name)?\s*[:\-]\s*\*?\s*([A-Z][\w&.,' -]{2,60}?)\s*(?:\*|\n|\||Job Type|Location|$)",
        job.get("description") or "",
    )
    if m:
        return m.group(1).strip(" .,-")
    found = _load().get(f"employer::{_job_id(job)}") or {}
    return found.get("company") or ""


# ---------------------------------------------------------------- extraction

def from_description(job: dict[str, Any]) -> dict[str, list[str]]:
    """Contacts written in the JD itself ("send your CV to hr@acme.com")."""
    text = job.get("description") or ""
    emails = sorted({e.lower().rstrip(".") for e in EMAIL.findall(text) if not JUNK_EMAIL.search(e)})
    phones = sorted({_clean_phone(p) for p in PHONE.findall(text)})
    links = sorted({u.rstrip("/") for u in LINKEDIN_PROFILE.findall(text)})
    return {"emails": emails, "phones": phones, "linkedin": links}


def _parse_profile(result: dict[str, Any]) -> dict[str, str] | None:
    url = result.get("url") or ""
    m = LINKEDIN_PROFILE.match(url)
    if not m:
        return None
    title = re.sub(r"\s*[|\-–]\s*LinkedIn.*$", "", result.get("title") or "", flags=re.I).strip()
    name, sep, headline = title.partition(" - ")
    if not sep or re.search(r"\b(?:at|@)\b", name) or len(name) > 60:
        # Title has no "Name - Headline" shape: take the name from the profile URL instead.
        slug = m.group(0).rstrip("/").rsplit("/in/", 1)[1]
        parts = [p for p in re.split(r"[-_]", requests.utils.unquote(slug)) if p and not re.search(r"\d", p)]
        name, headline = " ".join(p.capitalize() for p in parts[:3]), title
    if not name:
        return None
    snippet = re.sub(r"\s+", " ", result.get("content") or "").strip()
    return {
        "name": name.strip(),
        "headline": (headline.strip() or snippet[:90]),
        "url": "https://www.linkedin.com/in/" + m.group(0).rstrip("/").rsplit("/in/", 1)[1],
        "snippet": snippet[:220],
    }


def _role_of(text: str) -> str:
    if RECRUITER.search(text):
        return "recruiter"
    if LEADER.search(text):
        return "leader"
    return "employee"


def search_company(company: str) -> dict[str, Any]:
    """Search Google for people and contact details at one company."""
    tokens = company_tokens(company)
    quoted = f'"{company.strip()}"'
    queries = [
        f'{quoted} (recruiter OR "talent acquisition" OR HR) site:linkedin.com/in',
        f'{quoted} ("engineering manager" OR CTO OR founder OR "head of engineering") site:linkedin.com/in',
        f"{quoted} HR careers email contact number",
    ]
    people: dict[str, dict[str, Any]] = {}
    emails: dict[str, dict[str, str]] = {}
    phones: dict[str, dict[str, str]] = {}
    websites: dict[str, int] = {}
    errors: list[str] = []

    def absorb(results: list[dict[str, Any]], require_company: bool = True) -> None:
        for r in results:
            url = r.get("url") or ""
            text = f"{r.get('title') or ''} {r.get('content') or ''}"
            if require_company and not _mentions_company(text + " " + url, tokens):
                continue
            profile = _parse_profile(r)
            if profile:
                # The headline is the person's own title; the snippet can mention other people's roles.
                profile["role"] = _role_of(profile["headline"]) if profile["headline"] else _role_of(profile["snippet"])
                people.setdefault(profile["url"], profile)
                continue
            dom = _domain(url)
            if dom and not any(dom == s or dom.endswith("." + s) for s in NOT_COMPANY_SITES) and any(t in dom for t in tokens):
                websites[dom] = websites.get(dom, 0) + 1
            for e in EMAIL.findall(text):
                e = e.lower().rstrip(".")
                if not JUNK_EMAIL.search(e):
                    emails.setdefault(e, {"value": e, "source": url})
            for p in PHONE.findall(text):
                p = _clean_phone(p)
                if len(re.sub(r"\D", "", p)) >= 10:
                    phones.setdefault(p, {"value": p, "source": url})

    for query in queries:
        try:
            absorb(websearch.search(query))
        except SearchBlocked:
            if not (people or emails or phones):
                raise  # nothing found yet: report the block instead of "no contacts"
            errors.append("Some searches were blocked")
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{query[:40]}…: {exc}")

    website = max(websites, key=websites.get) if websites else ""
    # Once the company's own domain is known, look for addresses on it directly.
    if website and not any(e["value"].endswith("@" + website) for e in emails.values()):
        try:
            absorb(websearch.search(f'"@{website}" (hr OR careers OR recruitment OR jobs OR talent)'), require_company=False)
        except Exception:  # noqa: BLE001 — optional extra search
            pass

    site_root = website.split(".")[0] if website else ""

    def email_rank(e: dict[str, str]) -> tuple[int, int]:
        dom = e["value"].split("@")[1]
        on_company_domain = bool(website and (dom == website or dom.endswith("." + website) or (site_root and site_root in dom)))
        return (0 if on_company_domain else 1, 0 if GENERIC_INBOX.search(e["value"]) else 1)

    role_order = {"recruiter": 0, "leader": 1, "employee": 2}
    return {
        "company": company,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "provider": websearch.provider(),
        "website": f"https://{website}" if website else "",
        "people": sorted(people.values(), key=lambda p: role_order[p["role"]])[:8],
        "emails": sorted(emails.values(), key=email_rank)[:6],
        "phones": list(phones.values())[:4],
        "errors": errors,
        "searches": [websearch.google_url(q) for q in queries[:2]],
    }


def _domain_info(company: str, job: dict[str, Any], website: str, refresh: bool) -> dict[str, Any]:
    """Company email domain + MX check, cached for a month (domains rarely change)."""
    key = f"domain::{company_key(company)}"
    cached = None if refresh else _load().get(key)
    if cached:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched_at"]) < timedelta(days=30):
                return cached
        except (KeyError, ValueError):
            pass
    found = domains.find_domain(company, job, known_website=website)
    info = {**found, "mx": domains.has_mx(found["domain"]) if found["domain"] else False,
            "fetched_at": datetime.now(timezone.utc).isoformat()}
    _save_entry(key, info)
    return info


def get_contacts(job: dict[str, Any], refresh: bool = False) -> dict[str, Any]:
    """Contacts for one job: JD contacts, web-search results, and guessed inboxes.

    Web search may be blocked; guessed contacts (from the company's domain) are
    still returned in that case, with the search problem in "search_error".
    """
    jd = from_description(job)
    result: dict[str, Any] = {
        "from_description": jd, "company": None, "guessed": None, "status": "ok",
        "employer": "", "employer_evidence": "", "search_error": "",
    }
    company = resolve_company(job)
    if not company:
        # The board hid the employer: try to find out who posted it (needs web search).
        try:
            found = identify_employer(job, refresh=refresh)
            company = found["company"]
            result["employer_evidence"] = found.get("evidence", "")
        except SearchBlocked as exc:
            result["search_error"] = str(exc)
    result["employer"] = company
    if not company or not company_tokens(company):
        result["status"] = "no_company"
        return result

    entry = None if refresh else cached_company(company)
    if entry is None:
        try:
            entry = search_company(company)
            if entry["errors"] and not (entry["people"] or entry["emails"] or entry["phones"]):
                result["search_error"] = entry["errors"][0]
                entry = None  # don't cache a failed search
            else:
                _save_entry(company_key(company), entry)
        except SearchBlocked as exc:
            result["search_error"] = str(exc)
            entry = None
    result["company"] = entry

    # Guesses from the company's domain: always available, even when search is blocked.
    try:
        info = _domain_info(company, job, (entry or {}).get("website", ""), refresh)
        guessed = domains.guess_contacts(info["domain"], (entry or {}).get("people")) if info.get("mx") else {"emails": [], "people": []}
        public = {e["value"] for e in (entry or {}).get("emails", [])} | set(jd["emails"])
        result["guessed"] = {
            "domain": info["domain"],
            "source": info["source"],
            "mx": info.get("mx", False),
            "emails": [e for e in guessed["emails"] if e not in public],
            "people": guessed["people"],
        }
    except Exception as exc:  # noqa: BLE001 — guessing is a bonus; never break the response
        result["guessed"] = {"domain": "", "source": "", "mx": False, "emails": [], "people": [], "error": str(exc)}
    return result


def summary(job: dict[str, Any]) -> dict[str, int] | None:
    """Counts for the job list (no searching). None when not searched yet."""
    company = resolve_company(job)
    entry = cached_company(company) if company else None
    jd = from_description(job)
    jd_count = len(jd["emails"]) + len(jd["phones"]) + len(jd["linkedin"])
    if entry is None:
        return {"people": 0, "emails": len(jd["emails"]), "phones": len(jd["phones"]), "searched": False} if jd_count else None
    return {
        "people": len(entry["people"]),
        "emails": len(entry["emails"]) + len(jd["emails"]),
        "phones": len(entry["phones"]) + len(jd["phones"]),
        "searched": True,
    }


def prefetch(jobs: list[dict[str, Any]], progress: Callable[[int, int], None] | None = None) -> None:
    """Search contacts for each distinct company (or hidden-employer job) not searched yet."""
    todo: dict[str, dict[str, Any]] = {}
    for job in jobs:
        company = resolve_company(job)
        if company:
            key = company_key(company)
            if company_tokens(company) and key not in todo and cached_company(company) is None:
                todo[key] = job
        elif f"employer::{_job_id(job)}" not in _load():
            todo[f"job::{_job_id(job)}"] = job
    total = len(todo)
    for i, job in enumerate(todo.values()):
        if progress:
            progress(i, total)
        try:
            get_contacts(job)  # if web search is blocked it fails fast; guesses are still saved
        except Exception:  # noqa: BLE001 — one company failing must not stop the rest
            pass
    if progress:
        progress(total, total)
