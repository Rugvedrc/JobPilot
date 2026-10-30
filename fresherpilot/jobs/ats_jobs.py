"""Jobs straight from company career pages (Greenhouse, Lever, Ashby public job-board APIs).

Free (no Apify), and it finds startup / product-company roles the big boards often don't carry.
Only India-based (or India-remote) openings are kept; the AI filter then checks them like any other job.
Edit the company list in search_config.CAREER_PAGES.
"""

from __future__ import annotations

import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from fresherpilot.core import search_config as cfg
from fresherpilot.jobs.ranker import strip_html

INDIA = re.compile(r"\b(india|bengaluru|bangalore|hyderabad|pune|mumbai|navi mumbai|delhi|new delhi|ncr|gurugram|gurgaon|noida|"
                   r"chennai|kolkata|ahmedabad|kochi|jaipur|indore|coimbatore|chandigarh|trivandrum|thiruvananthapuram)\b", re.I)
TIMEOUT = 25


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
    data = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true", timeout=TIMEOUT).json()
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
    data = requests.get(f"https://api.lever.co/v0/postings/{token}?mode=json", timeout=TIMEOUT).json()
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
    data = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}", timeout=TIMEOUT).json()
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


BOARDS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


def fetch(hours: float) -> tuple[list[dict[str, Any]], list[str]]:
    """India openings published in the last `hours`, from every company in CAREER_PAGES."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    errors: list[str] = []

    def one(entry: tuple[str, str, str]) -> list[dict[str, Any]]:
        board, token, name = entry
        try:
            return BOARDS[board](token, name)
        except Exception as exc:  # noqa: BLE001 — one company failing never stops the rest
            errors.append(f"{name}: {exc}")
            return []

    with ThreadPoolExecutor(max_workers=12) as pool:
        jobs = [j for batch in pool.map(one, cfg.CAREER_PAGES) for j in batch]
    fresh = [j for j in jobs if not j["posted"] or _iso(j["posted"]) >= since]
    return fresh, errors
