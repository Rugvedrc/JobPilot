"""Jobs straight from company career pages (Greenhouse, Lever, Ashby public job-board APIs).

Free (no Apify), and it finds startup / product-company roles the big boards often don't carry.
Only India-based (or India-remote) openings are kept; the AI filter then checks them like any other job.
Edit the company list in search_config.CAREER_PAGES.
"""

from __future__ import annotations

import html
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from fresherpilot.core import search_config as cfg
from fresherpilot.jobs.ranker import strip_html

INDIA = re.compile(r"\b(india|bengaluru|bangalore|hyderabad|pune|mumbai|navi mumbai|delhi|new delhi|ncr|gurugram|gurgaon|noida|"
                   r"chennai|kolkata|ahmedabad|kochi|jaipur|indore|coimbatore|chandigarh|trivandrum|thiruvananthapuram)\b", re.I)
# (connect, read): big boards like Paytm's Lever feed are ~3 MB and can take a while to arrive.
TIMEOUT = (10, 90)
_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(max_retries=requests.adapters.Retry(
    total=2, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))))


def _get_json(url: str) -> Any:
    """GET with retries; a read timeout is retried once more (slow boards often answer the 2nd time)."""
    for attempt in range(2):
        try:
            r = _session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.ConnectionError):
            if attempt:
                raise
            time.sleep(3)
    return None


def _days_ago(dt: datetime | None) -> int | None:
    return None if dt is None else max(0, (datetime.now(timezone.utc) - dt).days)


def _iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _job(company: str, jid: str, title: str, location: str, posted: datetime | None, url: str, desc: str, work_type: str,
         board: str) -> dict[str, Any]:
    return {
        "id": f"{board}:{company}:{jid}",
        "source": "Careers",
        "title": strip_html(title),
        "company": company,
        "location": location,
        "experience": "Not specified",
        "salary": "Not disclosed",
        "posted": posted.isoformat() if posted else "",
        "posted_days": _days_ago(posted),
        "skills": [],
        "url": url,
        "description": desc,
        "work_type": work_type,
    }


def _greenhouse(token: str, name: str) -> list[dict[str, Any]]:
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        offices = " ".join(o.get("location") or o.get("name") or "" for o in j.get("offices") or [])
        if not INDIA.search(f"{loc} {offices}"):
            continue
        posted = _iso(j.get("first_published") or j.get("updated_at"))
        desc = strip_html(html.unescape(j.get("content") or ""))
        out.append(_job(name, str(j["id"]), j.get("title", ""), loc, posted, j.get("absolute_url", ""),
                        desc, "", "gh"))
    return out


def _lever(token: str, name: str) -> list[dict[str, Any]]:
    data = _get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    out = []
    for j in data if isinstance(data, list) else []:
        cat = j.get("categories") or {}
        loc = ", ".join(cat.get("allLocations") or [cat.get("location") or ""])
        if not (INDIA.search(loc) or j.get("country") == "IN"):
            continue
        posted = datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc) if j.get("createdAt") else None
        parts = [j.get("descriptionPlain") or strip_html(j.get("description") or "")]
        for lst in j.get("lists") or []:
            parts.append(f"{lst.get('text', '')}: {strip_html(lst.get('content') or '')}")
        parts.append(j.get("additionalPlain") or "")
        out.append(_job(name, j["id"], j.get("text", ""), loc, posted, j.get("hostedUrl", ""), "\n".join(p for p in parts if p),
                        cat.get("commitment", ""), "lv"))
    return out


def _ashby(token: str, name: str) -> list[dict[str, Any]]:
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry", "")
        loc = ", ".join(x for x in locs if x)
        if not INDIA.search(f"{loc} {country}"):
            continue
        out.append(_job(name, j["id"], j.get("title", ""), loc, _iso(j.get("publishedAt")), j.get("jobUrl", ""),
                        j.get("descriptionPlain") or strip_html(j.get("descriptionHtml") or ""), j.get("employmentType", ""), "ab"))
    return out


def _smartrecruiters(token: str, name: str, since: datetime | None = None) -> list[dict[str, Any]]:
    """SmartRecruiters public Posting API: India postings; full text only for new ones (one call each)."""
    base = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
    found, offset = [], 0
    while True:
        data = _get_json(f"{base}?country=in&limit=100&offset={offset}")
        page = data.get("content") or []
        found += page
        offset += len(page)
        if not page or offset >= (data.get("totalFound") or 0):
            break
    out = []
    for j in found:
        posted = _iso(j.get("releasedDate"))
        if since and posted and posted < since:
            continue
        loc = j.get("location") or {}
        place = ", ".join(x for x in (loc.get("city"), loc.get("region"), "Remote" if loc.get("remote") else "") if x) or "India"
        detail = _get_json(f"{base}/{j['id']}") or {}
        sections = ((detail.get("jobAd") or {}).get("sections")) or {}
        desc = "\n".join(strip_html((sections.get(k) or {}).get("text") or "")
                          for k in ("jobDescription", "qualifications", "additionalInformation"))
        exp = (j.get("experienceLevel") or {}).get("label") or ""
        url = detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{token}/{j['id']}"
        job = _job(name, str(j["id"]), j.get("name", ""), place, posted, url, desc,
                   (j.get("typeOfEmployment") or {}).get("label", ""), "sr")
        if exp:
            job["experience"] = exp
        out.append(job)
    return out


BOARDS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "smartrecruiters": _smartrecruiters}


def fetch(hours: float) -> tuple[list[dict[str, Any]], list[str]]:
    """India openings published in the last `hours`, from every company in CAREER_PAGES."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    errors: list[str] = []

    def one(entry: tuple[str, str, str]) -> list[dict[str, Any]]:
        board, token, name = entry
        try:
            if board == "smartrecruiters":
                return BOARDS[board](token, name, since)
            return BOARDS[board](token, name)
        except Exception as exc:  # noqa: BLE001 — one company failing never stops the rest
            errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:120]}")
            return []

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [j for batch in pool.map(one, cfg.CAREER_PAGES) for j in batch]
    fresh = [j for j in jobs if not j["posted"] or _iso(j["posted"]) >= since]
    return fresh, errors
