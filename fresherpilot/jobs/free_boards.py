"""Free job sources (no Apify credit): LinkedIn's public job search, Indeed (python-jobspy),
Internshala and Wellfound public pages, and public Telegram job channels (t.me/s/<channel> web preview).

Each returns jobs already in the app's format. Apify is only the fallback when these fail.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup

from fresherpilot.jobs.ranker import strip_html

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
_HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}


def _days_ago(dt: datetime | None) -> int | None:
    return None if dt is None else max(0, (datetime.now(timezone.utc) - dt).days)


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class _Throttle:
    """At most one request per `gap` seconds across threads (polite, avoids LinkedIn's 429s)."""

    def __init__(self, gap: float) -> None:
        self.gap, self.lock, self.last = gap, threading.Lock(), 0.0

    def wait(self) -> None:
        with self.lock:
            delay = self.last + self.gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()


class Blocked(RuntimeError):
    """The site keeps refusing requests (rate limit): stop and let the caller fall back."""


# ---------------------------------------------------------------- LinkedIn (public job search)

_LI_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
_LI_JOB = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
_LI_PAGES = 40          # 10 jobs per page: at most 400 jobs per search (LinkedIn stops near 1,000)
# Measured: ~1 request / 3 s is accepted almost always; faster gets HTTP 429 after ~7 requests.
_li_throttle = _Throttle(2.5)


def _li_get(session: requests.Session, url: str, params: dict[str, Any] | None = None) -> requests.Response | None:
    for attempt, pause in enumerate((5, 10, 20, 40)):
        _li_throttle.wait()
        try:
            r = session.get(url, params=params, headers=_HEADERS, timeout=(10, 30))
        except requests.RequestException:
            time.sleep(pause)
            continue
        if r.status_code == 200:
            return r
        if r.status_code in (400, 404):
            return None
        time.sleep(pause)  # 429 / 999 = rate limit: back off
    raise Blocked("LinkedIn is rate-limiting this computer right now")


def _li_cards(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("div.base-search-card, div.job-search-card"):
        urn = card.get("data-entity-urn") or ""
        jid = urn.rsplit(":", 1)[-1]
        title = card.select_one(".base-search-card__title")
        if not jid.isdigit() or not title:
            continue
        company = card.select_one(".base-search-card__subtitle")
        loc = card.select_one(".job-search-card__location")
        posted = card.select_one("time")
        out.append({
            "id": jid,
            "title": title.get_text(" ", strip=True),
            "company": company.get_text(" ", strip=True) if company else "Unknown",
            "location": loc.get_text(" ", strip=True) if loc else "",
            "posted": posted.get("datetime", "") if posted else "",
        })
    return out


def _li_details(session: requests.Session, jid: str) -> dict[str, str]:
    r = _li_get(session, _LI_JOB.format(jid))
    if r is None:
        return {}
    soup = BeautifulSoup(r.text, "html.parser")
    desc = soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text")
    criteria = {}
    for item in soup.select(".description__job-criteria-item"):
        h, v = item.select_one("h3"), item.select_one("span")
        if h and v:
            criteria[h.get_text(strip=True).lower()] = v.get_text(strip=True)
    return {"description": desc.get_text("\n", strip=True) if desc else "",
            "level": criteria.get("seniority level", ""), "type": criteria.get("employment type", "")}


def linkedin(searches: list[tuple[str, str]], hours: float, skip_ids: set[str], keep_title,
             minutes: float = 10, seconds: int | None = None, stop_on_known: bool = False,
             on_batch=None, max_new: int | None = None) -> dict[str, Any]:
    """searches = [(title, place)], place "Remote" = India + remote filter. Jobs already in the list
    (skip_ids) and titles that keep_title() rejects are not opened (no detail request).
    Stops starting new searches after `minutes` (LinkedIn only allows ~1 request / 2.5 s); the
    searches not done are returned so the caller can carry them over.
    seconds = exact "posted within" window (the live poller); stop_on_known = results are newest
    first, so a page with only known jobs means the rest is old too. on_batch(jobs) gets every 10
    opened jobs as soon as they are ready (the live poller saves them right away). max_new = open at
    most this many new jobs (the rest wait for the next turn; result "more" = True)."""
    deadline = time.monotonic() + minutes * 60
    session = requests.Session()
    seconds = int(seconds) if seconds else max(3600, int(hours * 3600))
    cards: dict[str, dict[str, Any]] = {}
    done: list[tuple[str, str]] = []
    blocked = None
    skipped_title = 0
    more = False
    for title, place in searches:
        # every new job also needs one detail request: leave time for those
        if time.monotonic() + len(cards) * _li_throttle.gap > deadline:
            break
        params = {"keywords": title, "location": "India" if place == "Remote" else place,
                  "f_TPR": f"r{seconds}", "f_JT": "F", "sortBy": "DD"}
        if place == "Remote":
            params["f_WT"] = "2"
        try:
            for page in range(_LI_PAGES):
                r = _li_get(session, _LI_SEARCH, {**params, "start": page * 10})
                found = _li_cards(r.text) if r is not None else []
                if not found:
                    break
                for c in found:
                    if c["id"] in skip_ids or c["id"] in cards:
                        continue
                    if not keep_title(c["title"]):
                        skipped_title += 1
                        continue
                    cards[c["id"]] = c
                if stop_on_known and all(c["id"] in skip_ids for c in found):
                    break
                if max_new and len(cards) >= max_new:
                    more = True
                    break
                if len(found) < 10:
                    break
            done.append((title, place))
        except Blocked as exc:
            blocked = str(exc)
            break

    def detail(card: dict[str, Any]) -> dict[str, Any] | None:
        try:
            d = _li_details(session, card["id"])
        except Blocked:
            d = {}
        posted = _parse_date(card["posted"])
        return {
            "id": card["id"],
            "source": "LinkedIn",
            "title": card["title"],
            "company": card["company"],
            "location": card["location"],
            "experience": d.get("level") or "Not specified",
            "salary": "Not disclosed",
            "posted": card["posted"],
            "posted_days": _days_ago(posted),
            "skills": [],
            "url": f"https://www.linkedin.com/jobs/view/{card['id']}/",
            "description": d.get("description", ""),
            "work_type": d.get("type", ""),
        }

    todo = list(cards.values())
    jobs: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=2) as pool:  # the throttle still paces them
        for start in range(0, len(todo), 10):
            batch = [j for j in pool.map(detail, todo[start:start + 10]) if j]
            jobs += batch
            if on_batch and batch:
                on_batch(batch)
    return {"jobs": jobs, "done": done, "blocked": blocked, "skipped_title": skipped_title, "more": more}


# ---------------------------------------------------------------- Indeed (python-jobspy)

def _place(loc: str, searched: str, remote: bool) -> str:
    """Indeed often gives only the state ("MH, India"): keep the city that was searched."""
    if searched == "Remote" or remote:
        return f"Remote, {loc}" if "remote" not in loc.lower() else loc
    if searched != "India" and searched.lower() not in loc.lower():
        return f"{searched}, {loc}" if loc else searched
    return loc


def _text(value: Any) -> str:
    """A pandas cell as text ("" for None / NaN)."""
    return "" if value is None or value != value else str(value)


def indeed(queries: list[str], places: list[str], hours: float) -> list[dict[str, Any]]:
    """Indeed India via python-jobspy (free). places: city names, "Remote" or "India"."""
    from jobspy import scrape_jobs

    jobs: dict[str, dict[str, Any]] = {}
    errors = []
    for q in queries:
        for place in places:
            try:
                df = scrape_jobs(site_name=["indeed"], search_term=q, country_indeed="India",
                                 location="India" if place == "Remote" else place, is_remote=place == "Remote",
                                 hours_old=max(1, int(hours)), results_wanted=300, description_format="markdown",
                                 verbose=0)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                continue
            for row in df.to_dict("records"):
                jid = str(row.get("id") or row.get("job_url") or "")
                if not jid or jid in jobs or not row.get("title"):
                    continue
                posted = row.get("date_posted")
                posted_dt = datetime(posted.year, posted.month, posted.day, tzinfo=timezone.utc) if hasattr(posted, "year") else None
                jtype = row.get("job_type")
                jobs[jid] = {
                    "id": jid.removeprefix("in-"),
                    "source": "Indeed",
                    "title": str(row["title"]),
                    "company": _text(row.get("company")) or "Unknown",
                    "location": _place(_text(row.get("location")), place, bool(row.get("is_remote"))),
                    "experience": _text(row.get("experience_range")) or "Not specified",
                    "salary": "Not disclosed",
                    "posted": posted_dt.date().isoformat() if posted_dt else "",
                    "posted_days": _days_ago(posted_dt),
                    "skills": [],
                    "url": _text(row.get("job_url")),
                    "description": strip_html(_text(row.get("description"))),
                    "work_type": str(jtype) if isinstance(jtype, str) else "",
                }
            time.sleep(1.0)
    if not jobs and errors:
        raise RuntimeError(errors[0])
    return list(jobs.values())


# ---------------------------------------------------------------- Internshala (public job pages)

_IS_BASE = "https://internshala.com"
_is_throttle = _Throttle(3.0)


def _ago_days(text: str) -> int | None:
    """"Few hours ago" / "Today" / "3 days ago" / "2 weeks ago" / "1 month ago" -> days."""
    t = (text or "").lower()
    if not t:
        return None
    if "hour" in t or "today" in t or "just" in t or "minute" in t:
        return 0
    m = re.search(r"(\d+)\s*(day|week|month)", t)
    if not m:
        return None
    return int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]


def _is_get(session: requests.Session, url: str) -> requests.Response | None:
    _is_throttle.wait()
    try:
        r = session.get(url, headers=_HEADERS, timeout=(10, 30))
    except requests.RequestException:
        return None
    return r if r.status_code == 200 else None


def _is_slug(query: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")


def _is_row(card: Any, icon: str) -> str:
    """Text next to an icon in an Internshala card (salary, experience)."""
    i = card.select_one(f"i.{icon}")
    box = i.find_parent(class_="row-1-item") if i else None
    if not box:
        return ""
    span = box.select_one("span.desktop") or box.select_one("span")
    return span.get_text(" ", strip=True) if span else ""


def internshala(queries: list[str], skip_ids: set[str], pages: int = 2) -> list[dict[str, Any]]:
    """Internshala jobs (not internships) for each keyword, read from its public listing pages.
    The listing isn't sorted by date, so the first `pages` pages are read; only jobs not in
    skip_ids get a detail request (full description)."""
    session = requests.Session()
    cards: dict[str, dict[str, Any]] = {}
    for q in queries:
        for page in range(1, pages + 1):
            url = f"{_IS_BASE}/jobs/keywords-{_is_slug(q)}/" + (f"page-{page}/" if page > 1 else "")
            r = _is_get(session, url)
            if r is None:
                break
            found = BeautifulSoup(r.text, "html.parser").select("div.individual_internship[internshipid]")
            for c in found:
                jid, link = c.get("internshipid", ""), c.select_one("a.job-title-href")
                if not jid or not link or (c.get("employment_type") or "job") != "job" or jid in cards:
                    continue
                status = c.select_one(".detail-row-2 .color-labels span")
                company = c.select_one(".company-name")
                cards[jid] = {
                    "id": jid, "title": link.get_text(" ", strip=True),
                    "company": company.get_text(" ", strip=True) if company else "Unknown",
                    "location": ", ".join(a.get_text(strip=True) for a in c.select(".locations a")),
                    "salary": _is_row(c, "ic-16-money"), "experience": _is_row(c, "ic-16-briefcase"),
                    "posted": status.get_text(" ", strip=True) if status else "",
                    "skills": [s.get_text(strip=True) for s in c.select(".job_skill")][:20],
                    "url": _IS_BASE + (c.get("data-href") or link.get("href") or ""),
                }
            if len(found) < 40:
                break
    jobs = []
    for jid, c in cards.items():
        if jid in skip_ids:
            continue
        desc = ""
        r = _is_get(session, c["url"])
        if r is not None:
            box = BeautifulSoup(r.text, "html.parser").select_one(".internship_details")
            desc = box.get_text("\n", strip=True) if box else ""
        jobs.append({
            "id": jid, "source": "Internshala", "title": c["title"], "company": c["company"],
            "location": c["location"], "experience": c["experience"] or "Not specified",
            "salary": c["salary"] or "Not disclosed", "posted": c["posted"], "posted_days": _ago_days(c["posted"]),
            "skills": c["skills"], "url": c["url"], "description": desc, "work_type": "full time",
        })
    return jobs


# ---------------------------------------------------------------- Wellfound (public role pages)

_WF_BASE = "https://wellfound.com"
_wf_throttle = _Throttle(8.0)  # Wellfound blocks fast readers: keep it slow


class WellfoundBlocked(Exception):
    pass


def _wf_slug(text: str) -> str:
    c = text.strip().lower()
    c = {"bengaluru": "bangalore", "gurugram": "gurgaon", "new delhi": "delhi"}.get(c, c)
    return re.sub(r"[^a-z0-9]+", "-", c).strip("-")


def _wf_jobs(html: str) -> list[dict[str, Any]]:
    """Job listings embedded in a Wellfound page (its __NEXT_DATA__ Apollo state)."""
    import json

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    try:
        state = json.loads(m.group(1))["props"]["pageProps"].get("apolloState") or {}
    except (ValueError, KeyError, TypeError):
        return []
    data = state.get("data", state)
    company_of: dict[str, str] = {}
    for v in data.values():
        if isinstance(v, dict) and v.get("__typename") == "StartupResult":
            for ref in v.get("highlightedJobListings") or []:
                company_of[str(ref.get("__ref", "")).split(":")[-1]] = v.get("name") or "Unknown"
    out = []
    for v in data.values():
        if not isinstance(v, dict) or v.get("__typename") != "JobListingSearchResult" or not v.get("title"):
            continue
        jid = str(v.get("id") or "")
        low, high = v.get("yearsExperienceMin"), v.get("yearsExperienceMax")
        exp = (f"{low}-{high} years" if low is not None and high is not None else
               f"{low}+ years" if low is not None else "Not specified")
        live = v.get("liveStartAt")
        posted_dt = datetime.fromtimestamp(live, tz=timezone.utc) if isinstance(live, (int, float)) else None
        locs = list(v.get("locationNames") or []) + (["Remote"] if v.get("remote") else [])
        out.append({
            "id": jid, "source": "Wellfound", "title": strip_html(str(v["title"])),
            "company": company_of.get(jid, "Unknown"), "location": ", ".join(dict.fromkeys(locs)),
            "experience": exp, "salary": v.get("compensation") or "Not disclosed",
            "posted": posted_dt.isoformat() if posted_dt else "", "posted_days": _days_ago(posted_dt),
            "skills": [], "url": f"{_WF_BASE}/jobs/{jid}-{v.get('slug') or ''}".rstrip("-"),
            "description": strip_html(str(v.get("description") or "")),
            "work_type": str(v.get("jobType") or ""),
        })
    return out


def wellfound(roles: list[str], places: list[str], skip_ids: set[str]) -> list[dict[str, Any]]:
    """Wellfound jobs from its public role pages (/role/l/<role>/<city>, /role/r/<role> = remote).
    Unknown role pages (404) are skipped; a block (403 / 429) raises WellfoundBlocked."""
    session = requests.Session()
    jobs: dict[str, dict[str, Any]] = {}
    for role in roles:
        for place in places:
            path = f"/role/r/{_wf_slug(role)}" if place == "Remote" else f"/role/l/{_wf_slug(role)}/{_wf_slug(place)}"
            _wf_throttle.wait()
            try:
                r = session.get(_WF_BASE + path, headers=_HEADERS, timeout=(10, 30))
            except requests.RequestException:
                continue
            if r.status_code in (403, 429):
                raise WellfoundBlocked(f"Wellfound blocked the request (HTTP {r.status_code})")
            if r.status_code == 404:
                break  # no page for this role: skip its other cities
            if r.status_code != 200:
                continue
            for job in _wf_jobs(r.text):
                if job["id"] and job["id"] not in skip_ids:
                    jobs.setdefault(job["id"], job)
    return list(jobs.values())


# ---------------------------------------------------------------- Telegram public channels

_SKIP_LINK = re.compile(r"t\.me/|telegram\.me|whatsapp\.com|chat\.whatsapp|instagram\.com|youtube\.com|youtu\.be|"
                        r"facebook\.com|twitter\.com|x\.com/|linkedin\.com/(company|in|groups)/", re.I)
_LABELS = {
    "company": r"company(?: name)?|organi[sz]ation|employer|hiring company",
    "title": r"role|job role|profile|position|post|designation|job title|title|opening",
    "experience": r"experience|exp|eligibility|batch|qualification",
    "location": r"location|job location|place",
    "salary": r"salary|package|ctc|stipend",
}
_HIRING = re.compile(r"^(?P<company>[A-Z0-9][\w&.,'\- ]{1,50}?)\s+(?:is\s+)?(?:hiring|recruiting|off[\s-]?campus(?: drive)?|"
                     r"careers?)\b(?:\s+(?:for|freshers?|\d{4}|batch|drive|–|-|:|!)+)*\s*(?P<title>[^!\n|]{3,80})?", re.I)


_DRIVE = re.compile(r"^(?P<company>[A-Z0-9][\w&.,' ]{1,50}?)\s+(?:recruitment|off[\s-]?campus(?: drive)?|hiring|careers?)\b"
                    r"[^–|:\-]*[–|:\-]\s*(?P<title>.{3,100})$", re.I)


def _tg_lines(text: str) -> list[str]:
    """Message lines without emoji-only lines; "Label:" alone on a line is joined with the next."""
    raw = [ln.strip(" \t•*-–") for ln in text.splitlines()]
    raw = [ln for ln in raw if re.search(r"[A-Za-z0-9]", ln)]
    out: list[str] = []
    for ln in raw:
        if out and out[-1].endswith(":") and len(out[-1]) < 30:
            out[-1] = f"{out[-1]} {ln}"
        else:
            out.append(ln)
    return out


def _labeled(lines: list[str], kind: str) -> str:
    pat = re.compile(rf"^\W*(?:{_LABELS[kind]})\s*[:\-–]\s*(.+)$", re.I)
    for ln in lines:
        m = pat.match(ln)
        if m and not m.group(1).lower().startswith("http"):
            return m.group(1).strip()
    return ""


def parse_telegram_post(text: str, links: list[str]) -> dict[str, str] | None:
    """Best-effort fields from a free-form job post. None = not a job post (no usable link)."""
    apply = [u for u in links if u.startswith("http") and not _SKIP_LINK.search(u)]
    if not apply:
        return None
    lines = _tg_lines(text)
    if not lines:
        return None
    company, title = _labeled(lines, "company"), _labeled(lines, "title")
    head = lines[0]
    d = _DRIVE.match(head)  # "Emerson Recruitment 2026 – Applications Engineer I"
    if d:
        company = company or d.group("company").strip()
        title = title or d.group("title").strip(" -–:|")
    m = _HIRING.match(head)
    if m:
        company = company or m.group("company").strip()
        t = (m.group("title") or "").strip(" ,-–:|!")
        if t and not re.fullmatch(r"(?i)(freshers?|apply|apply now|now)", t):
            title = title or re.sub(r"(?i)^(for|freshers?)\s+", "", t)
    if not title:
        title = re.sub(r"(?i)\b(apply now|apply link|hurry)\b.*$", "", head).strip(" -–:|!") or head
    return {
        "title": title.strip(" ,-–:|!")[:120],
        "company": (company or "Unknown")[:80],
        "experience": _labeled(lines, "experience") or "Not specified",
        "location": _labeled(lines, "location"),
        "salary": _labeled(lines, "salary") or "Not disclosed",
        "url": apply[0],
    }


def _channel_name(value: str) -> str:
    v = value.strip().rstrip("/")
    v = re.sub(r"^(https?://)?(www\.)?(t|telegram)\.me/(s/)?", "", v)
    return v.lstrip("@").split("/")[0].split("?")[0]


def telegram(channels: Iterable[str], hours: float, pages: int = 5) -> tuple[list[dict[str, Any]], list[str]]:
    """Job posts from public Telegram channels in the last `hours` (newest ~20 posts per page)."""
    since = datetime.now(timezone.utc).timestamp() - hours * 3600
    jobs: list[dict[str, Any]] = []
    errors: list[str] = []
    for raw in channels:
        name = _channel_name(raw)
        if not name:
            continue
        before = None
        try:
            for _ in range(pages):
                url = f"https://t.me/s/{name}" + (f"?before={before}" if before else "")
                r = requests.get(url, headers=_HEADERS, timeout=(10, 30))
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "html.parser")
                msgs = soup.select(".tgme_widget_message[data-post]")
                if not msgs:
                    if before is None:
                        errors.append(f"{name}: no public posts (private channel or wrong name)")
                    break
                oldest = None
                for m in msgs:
                    post = m["data-post"]
                    pid = int(post.rsplit("/", 1)[-1])
                    oldest = pid if oldest is None else min(oldest, pid)
                    t = m.select_one("time[datetime]")
                    when = _parse_date(t["datetime"]) if t else None
                    if when and when.timestamp() < since:
                        continue
                    body = m.select_one(".tgme_widget_message_text")
                    if not body:
                        continue
                    text = body.get_text("\n", strip=True)
                    links = [a["href"] for a in body.select("a[href]")]
                    links += re.findall(r"https?://\S+", text)
                    fields = parse_telegram_post(text, list(dict.fromkeys(links)))
                    if not fields:
                        continue
                    jobs.append({
                        "id": f"tg:{post}",
                        "source": "Telegram",
                        **fields,
                        "posted": when.isoformat() if when else "",
                        "posted_days": _days_ago(when),
                        "skills": [],
                        "description": f"{text}\n\n(Posted in Telegram channel @{name}: https://t.me/{post})",
                        "work_type": "",
                    })
                first = msgs[0].select_one("time[datetime]")
                if not oldest or (first and _parse_date(first["datetime"]) and _parse_date(first["datetime"]).timestamp() < since):
                    break
                before = oldest
                time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001 — one channel failing never stops the rest
            errors.append(f"{name}: {exc}")
    return jobs, errors
