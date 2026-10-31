""""I saw a job you missed": check a job link against the saved list, import it if it's missing,
and log every miss per source (users/<id>/missed_jobs.jsonl) so gaps in coverage show up.

Reading the job: LinkedIn's public job page (free), any page with schema.org JobPosting data
(most company career sites, Greenhouse, Lever, Workday...; free), and Naukri through the Apify
Naukri actor's single-job lookup (Naukri pages need a browser).
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup

from fresherpilot.jobs.ranker import strip_html

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/130.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}


def source_of(url: str) -> tuple[str, str]:
    """(source name, the board's job id or "") for a job link."""
    u = url.strip()
    if "linkedin.com" in u:
        m = re.search(r"currentJobId=(\d+)", u) or re.search(r"/jobs/view/(?:[^/?]*?-)?(\d{6,})", u)
        return "LinkedIn", m.group(1) if m else ""
    if "naukri.com" in u:
        m = re.search(r"-(\d{9,})(?:[?#/]|$)", u) or re.search(r"jobId=(\d+)", u)
        return "Naukri", m.group(1) if m else ""
    if "indeed." in u:
        m = re.search(r"jk=([0-9a-f]{10,})", u)
        return "Indeed", m.group(1) if m else ""
    for host, name in (("internshala.com", "Internshala"), ("wellfound.com", "Wellfound"), ("greenhouse.io", "Careers"),
                       ("lever.co", "Careers"), ("ashbyhq.com", "Careers"), ("myworkdayjobs.com", "Careers"),
                       ("foundit.in", "Foundit"), ("instahyre.com", "Instahyre"), ("cutshort.io", "Cutshort"),
                       ("hirist", "Hirist"), ("t.me", "Telegram")):
        if host in u:
            return name, ""
    return "Other", ""


def _norm_url(url: str) -> str:
    return re.sub(r"[?#].*$", "", url.strip()).rstrip("/").lower()


def find(url: str, jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The saved job this link points to (same board id, or same link)."""
    src, jid = source_of(url)
    target = _norm_url(url)
    for job in jobs:
        if jid and job.get("source") == src and str(job.get("id")).removeprefix("in-") == jid:
            return job
        if job.get("url") and _norm_url(job["url"]) == target:
            return job
        if jid and src == "LinkedIn" and jid in str(job.get("url") or ""):
            return job
    return None


def _from_json_ld(soup: BeautifulSoup) -> dict[str, Any] | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for d in items:
            if isinstance(d, dict) and "JobPosting" in str(d.get("@type")):
                org = d.get("hiringOrganization") or {}
                locs = d.get("jobLocation") or []
                locs = locs if isinstance(locs, list) else [locs]
                place = ", ".join(filter(None, (
                    ((l.get("address") or {}).get("addressLocality") if isinstance(l.get("address"), dict) else str(l.get("address") or ""))
                    for l in locs if isinstance(l, dict))))
                exp = d.get("experienceRequirements")
                if isinstance(exp, dict):
                    months = exp.get("monthsOfExperience")
                    exp = f"{float(months) / 12:g} years" if months else ""
                return {
                    "title": html.unescape(str(d.get("title") or "")),
                    "company": org.get("name") if isinstance(org, dict) else str(org or ""),
                    "location": place or ("Remote" if d.get("jobLocationType") == "TELECOMMUTE" else ""),
                    "experience": str(exp or "") or "Not specified",
                    "posted": str(d.get("datePosted") or ""),
                    "description": strip_html(html.unescape(str(d.get("description") or ""))),
                    "work_type": ", ".join(d["employmentType"]) if isinstance(d.get("employmentType"), list) else str(d.get("employmentType") or ""),
                }
    return None


def _read_ats(url: str) -> dict[str, Any] | None:
    """Greenhouse / Lever / Ashby job links through their official public APIs."""
    from fresherpilot.jobs import ats_jobs

    if m := re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)", url):
        token, jid = m.groups()
        j = ats_jobs._get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{jid}")
        return {"title": j.get("title", ""), "company": token, "location": (j.get("location") or {}).get("name", ""),
                "experience": "Not specified", "posted": j.get("first_published") or j.get("updated_at") or "",
                "description": strip_html(html.unescape(j.get("content") or "")), "work_type": ""}
    if m := re.search(r"jobs\.lever\.co/([\w-]+)/([0-9a-f-]{36})", url):
        site, jid = m.groups()
        j = ats_jobs._get_json(f"https://api.lever.co/v0/postings/{site}/{jid}")
        cat = j.get("categories") or {}
        created = j.get("createdAt")
        return {"title": j.get("text", ""), "company": site, "location": cat.get("location", ""), "experience": "Not specified",
                "posted": datetime.fromtimestamp(created / 1000, timezone.utc).isoformat() if created else "",
                "description": "\n".join(filter(None, [j.get("descriptionPlain") or strip_html(j.get("description") or "")]
                                                 + [f"{x.get('text', '')}: {strip_html(x.get('content') or '')}" for x in j.get("lists") or []]
                                                 + [j.get("additionalPlain") or strip_html(j.get("additional") or "")])),
                "work_type": cat.get("commitment", "")}
    if m := re.search(r"jobs\.ashbyhq\.com/([\w.-]+)/([0-9a-f-]{36})", url):
        board, jid = m.groups()
        data = ats_jobs._get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
        j = next((x for x in data.get("jobs", []) if x.get("id") == jid), None)
        if j:
            return {"title": j.get("title", ""), "company": board, "location": j.get("location", ""), "experience": "Not specified",
                    "posted": j.get("publishedAt", ""), "description": j.get("descriptionPlain") or "",
                    "work_type": j.get("employmentType", "")}
    return None


def _read_page(url: str) -> dict[str, Any] | None:
    r = requests.get(url, headers=_UA, timeout=(10, 20))
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    job = _from_json_ld(soup)
    if job and job["title"]:
        return job
    title = soup.find("meta", property="og:title")
    desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
    if not title:
        return None
    return {"title": title.get("content", ""), "company": "", "location": "", "experience": "Not specified", "posted": "",
            "description": desc.get("content", "") if desc else "", "work_type": ""}


def _read_linkedin(jid: str) -> dict[str, Any] | None:
    from fresherpilot.jobs import free_boards

    s = requests.Session()
    r = free_boards._li_get(s, free_boards._LI_JOB.format(jid))
    if r is None:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    t = soup.select_one(".top-card-layout__title, h2")
    c = soup.select_one(".topcard__org-name-link, .topcard__flavor")
    loc = soup.select_one(".topcard__flavor--bullet")
    d = free_boards._li_details(s, jid)
    if not t:
        return None
    return {"title": t.get_text(" ", strip=True), "company": c.get_text(" ", strip=True) if c else "",
            "location": loc.get_text(" ", strip=True) if loc else "", "experience": d.get("level") or "Not specified",
            "posted": "", "description": d.get("description", ""), "work_type": d.get("type", "")}


def _read_naukri(jid: str) -> dict[str, Any] | None:
    from fresherpilot.core import search_config as cfg
    from fresherpilot.jobs import apify_jobs

    _, items, err, _ = apify_jobs._run_actor("naukri", cfg.NAUKRI["actor"], {
        "jobIds": [jid], "fetchDetails": True, "descriptionFormat": "text", "excludeEmptyFields": True}, 0.02)
    for item in items:
        job = apify_jobs.normalize_naukri(item)
        if job:
            return job
    return None


def _log(entry: dict[str, Any]) -> None:
    from fresherpilot.core import users

    with users.path("missed_jobs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def history() -> list[dict[str, Any]]:
    from fresherpilot.core import users

    p = users.path("missed_jobs.jsonl")
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def report(url: str) -> dict[str, Any]:
    """Check one job link. Returns what happened, for the app to show."""
    from fresherpilot.jobs import apify_jobs, llm_filter

    url = url.strip()
    if not re.match(r"https?://", url):
        raise ValueError("Paste the full job link (starting with https://).")
    src, jid = source_of(url)
    payload = apify_jobs.load_cache() or {}
    now = datetime.now(timezone.utc).isoformat()
    saved = find(url, payload.get("jobs") or [])
    if saved:
        v = llm_filter.cached_verdict(saved)
        status = (v or {}).get("decision", "unchecked")
        result = {"found": True, "source": src, "title": saved.get("title"), "company": saved.get("company"),
                  "status": status, "category": (v or {}).get("category"), "reason": (v or {}).get("reason") or (v or {}).get("fit_reason")}
        _log({"at": now, "url": url, "source": src, "already_had": True, "status": status, "category": result["category"]})
        return result

    job = None
    error = ""
    try:
        if src == "LinkedIn" and jid:
            job = _read_linkedin(jid)
        elif src == "Naukri" and jid:
            job = _read_naukri(jid)
        elif src == "Careers":
            job = _read_ats(url)
        if not job:
            job = _read_page(url)
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:200]
    added = 0
    if job and job.get("title"):
        job.update(source=job.get("source") or src if src != "Other" else "Added by you", url=job.get("url") or url,
                   id=job.get("id") or jid or url, via="manual")
        job.setdefault("salary", "Not disclosed")
        job.setdefault("skills", [])
        job["posted_days"] = apify_jobs._posted_days_from_iso(job.get("posted")) if job.get("posted") else None
        added = apify_jobs.merge_external([job], "manual", check_location=False)["added"]
    _log({"at": now, "url": url, "source": src, "already_had": False, "imported": bool(added),
          "title": (job or {}).get("title"), "company": (job or {}).get("company"), "error": error})
    return {"found": False, "source": src, "imported": bool(added), "title": (job or {}).get("title"),
            "company": (job or {}).get("company"), "error": error or ("" if job else "Couldn't read that page")}


def summary() -> dict[str, Any]:
    """Misses per source (jobs you found that the app didn't have)."""
    out: dict[str, dict[str, int]] = {}
    for e in history():
        s = out.setdefault(e.get("source") or "Other", {"reported": 0, "missed": 0})
        s["reported"] += 1
        s["missed"] += 0 if e.get("already_had") else 1
    return out
