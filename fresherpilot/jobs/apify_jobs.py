"""Fetch jobs from Naukri, LinkedIn, Indeed, Internshala, Wellfound, company career pages and Telegram.

LinkedIn, Indeed, career pages and Telegram are free (free_boards.py / ats_jobs.py); Apify is used for
Naukri, Internshala and Wellfound, and for LinkedIn / Indeed only when their free fetch fails.

What is searched lives in search_config.py. Each fetch is MERGED into the saved list
(boards in "only new jobs" mode return only what's new), and jobs are kept for
KEEP_JOBS_DAYS after they were first seen.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

from apify_client import ApifyClient
from dotenv import load_dotenv

from fresherpilot.core import search_config as cfg
from fresherpilot.jobs.ranker import strip_html

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

def _cache_path() -> Path:
    from fresherpilot.core import users

    return users.path("jobs_cache.json")
ACTOR_TIMEOUT = timedelta(minutes=15)


def _api_token() -> str:
    from fresherpilot.core import users

    raw = users.key("APIFY_API_KEY")
    return raw.strip().strip('"').strip("'")


def _client() -> ApifyClient:
    token = _api_token()
    if not token:
        raise RuntimeError("APIFY_API_KEY is missing from .env")
    return ApifyClient(token)


def _dataset_id(run: Any) -> str | None:
    if run is None:
        return None
    if isinstance(run, dict):
        return run.get("defaultDatasetId")
    return getattr(run, "default_dataset_id", None) or getattr(run, "defaultDatasetId", None)


def _dataset_items(client: ApifyClient, run: Any) -> list[dict[str, Any]]:
    dataset_id = _dataset_id(run)
    if not dataset_id:
        return []
    return list(client.dataset(dataset_id).iterate_items())


def _posted_days_from_text(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).lower()
    if "hour" in text or "minute" in text or "just" in text or "today" in text:
        return 0
    if "yesterday" in text:
        return 1
    match = re.search(r"(\d+)", text)
    if not match:
        return None
    n = int(match.group(1))
    if "week" in text:
        return n * 7
    if "month" in text:
        return n * 30
    return n


def _join(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v)
    return str(value)


# Naukri departments that are never developer work (a free backstop for the source's own filter).
_NAUKRI_NOT_TECH = re.compile(r"sales|business development|customer success|service & operations|bpo|marketing|teaching|"
                              r"healthcare|doctor|nursing|finance|accounting|human resources|\bhr\b|legal|retail|"
                              r"hospitality|construction|production|manufacturing|procurement|supply chain|administration|"
                              r"content|editorial|media|merchandising|sports|beauty|security services", re.I)


def normalize_naukri(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("_type") in {"summary", "info"}:
        return None
    if "jobId" in item:  # blackfalcondata/naukri-jobs-feed
        return _naukri_feed(item)
    title = item.get("title") or item.get("jobTitle")
    if not title:
        return None
    exp_min = item.get("experience_min_years")
    exp_max = item.get("experience_max_years")
    if exp_min is not None and exp_max is not None:
        experience = f"{exp_min}-{exp_max} years"
    else:
        experience = _join(item.get("experience") or item.get("experience_text"))

    salary_bits = []
    if item.get("salary_min_lakhs") is not None:
        salary_bits.append(str(item.get("salary_min_lakhs")))
    if item.get("salary_max_lakhs") is not None:
        salary_bits.append(str(item.get("salary_max_lakhs")))
    salary = item.get("salary_text") or ("–".join(salary_bits) + " LPA" if salary_bits else "")

    return {
        "id": str(item.get("job_id") or item.get("apply_url") or title),
        "source": "Naukri",
        "title": strip_html(str(title)),
        "company": item.get("company") or item.get("companyName") or "Unknown",
        "location": _join(item.get("location") or item.get("locations")),
        "experience": experience or "Not specified",
        "salary": salary or "Not disclosed",
        "posted": item.get("posted_date_text") or (f"{item.get('posted_days_ago')} days ago" if item.get("posted_days_ago") is not None else ""),
        "posted_days": item.get("posted_days_ago") if isinstance(item.get("posted_days_ago"), (int, float)) else _posted_days_from_text(item.get("posted_date_text")),
        "skills": item.get("skills") if isinstance(item.get("skills"), list) else [],
        "url": item.get("apply_url") or item.get("source_url") or item.get("url") or "",
        "description": strip_html(item.get("description") or ""),
    }


def _naukri_feed(item: dict[str, Any]) -> dict[str, Any] | None:
    title = item.get("title")
    if not title or _NAUKRI_NOT_TECH.search(str(item.get("functionalArea") or "")):
        return None
    skills = item.get("skills") if isinstance(item.get("skills"), list) else []
    return {
        "id": str(item["jobId"]),
        "source": "Naukri",
        "title": strip_html(str(title)),
        "company": item.get("companyName") or "Unknown",
        "location": item.get("location") or "",
        "experience": (item.get("experienceText") or "Not specified").replace("Yrs", "years").replace("Yr", "year"),
        "salary": item.get("salary") or "Not disclosed",
        "posted": item.get("createdDate") or "",
        "posted_days": _posted_days_from_iso(item.get("createdDate")),
        "skills": [str(s) for s in skills],
        "url": item.get("portalUrl") or item.get("staticUrl") or "",
        "description": strip_html(item.get("description") or item.get("shortDescription") or ""),
        "work_type": item.get("employmentType") or item.get("jobType") or "",
        "department": item.get("functionalArea") or "",
    }


def normalize_linkedin(item: dict[str, Any]) -> dict[str, Any] | None:
    title = item.get("title")
    if not title:
        return None
    salary = item.get("salary")
    if not salary and item.get("salaryMin"):
        currency = item.get("salaryCurrency") or ""
        salary = f"{currency} {item.get('salaryMin')}–{item.get('salaryMax')}".strip()

    return {
        "id": str(item.get("id") or item.get("jobUrl") or title),
        "source": "LinkedIn",
        "title": strip_html(str(title)),
        "company": item.get("companyName") or "Unknown",
        "location": item.get("location") or "",
        "experience": item.get("experienceLevel") or "Not specified",
        "salary": salary or "Not disclosed",
        "posted": item.get("postedDate") or item.get("postedTime") or item.get("publishedAt") or "",
        "posted_days": (_posted_days_from_iso(item["postedDate"]) if item.get("postedDate") else
                        _posted_days_from_text(item.get("postedTime") or item.get("postedTimeAgo") or item.get("publishedAt"))),
        "skills": [],
        "url": item.get("jobUrl") or item.get("url") or item.get("applyUrl") or "",
        "description": strip_html(item.get("description") or ""),
        "work_type": item.get("contractType") or item.get("workType") or "",
    }


def _posted_days_from_iso(value: Any) -> int | None:
    if not value:
        return None
    text = str(value)
    try:
        posted_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if posted_dt.tzinfo is None:
            posted_dt = posted_dt.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - posted_dt).days)
    except ValueError:
        return _posted_days_from_text(value)


def normalize_cutshort(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("status") not in (None, "ok"):
        return None
    title = item.get("title")
    if not title:
        return None
    experience = "Not specified"
    low, high = item.get("experienceMinYears"), item.get("experienceMaxYears")
    if low is not None and high is not None:
        experience = f"{low}-{high} years"
    return {
        "id": str(item.get("jobId") or item.get("url") or title),
        "source": "Cutshort",
        "title": strip_html(str(title)),
        "company": item.get("companyName") or "Unknown",
        "location": _join(item.get("locations")) or ("Remote" if item.get("remote") else ""),
        "experience": experience,
        "salary": item.get("salaryText") or "Not disclosed",
        "posted": item.get("postedAt") or "",
        "posted_days": _posted_days_from_iso(item.get("postedAt")),
        "skills": item.get("skills") if isinstance(item.get("skills"), list) else [],
        "url": item.get("url") or "",
        "description": strip_html(item.get("description") or ""),
        "work_type": _join(item.get("jobTypes")) or item.get("workMode") or "",
    }


def _wellfound_experience(item: dict[str, Any]) -> str:
    low, high = item.get("yearsExperienceMin"), item.get("yearsExperienceMax")
    if low is not None and high is not None:
        return f"{low}-{high} years"
    if low is not None:
        return f"{low}+ years"
    return "Not specified"


def normalize_wellfound(item: dict[str, Any]) -> dict[str, Any] | None:
    title = item.get("jobTitle") or item.get("title")
    if not title:
        return None
    locations = item.get("locations") or item.get("acceptedRemoteLocations") or []
    salary = item.get("compensation") or "Not disclosed"
    return {
        "id": str(item.get("jobId") or item.get("jobUrl") or title),
        "source": "Wellfound",
        "title": strip_html(str(title)),
        "company": item.get("companyName") or "Unknown",
        "location": _join(locations) or ("Remote" if item.get("remote") else ""),
        "experience": _wellfound_experience(item),
        "salary": salary,
        "posted": item.get("postedAt") or "",
        "posted_days": _posted_days_from_iso(item.get("postedAt")),
        "skills": [],
        "url": item.get("jobUrl") or item.get("url") or "",
        "description": strip_html(item.get("descriptionText") or item.get("description") or ""),
        "work_type": item.get("jobType") or "",
    }


def normalize_indeed(item: dict[str, Any]) -> dict[str, Any] | None:
    title = item.get("title")
    if not title:
        return None
    posted = item.get("postedAt") or item.get("datePosted") or ""
    posted_days = _posted_days_from_iso(posted)
    attributes = item.get("attributes") or []
    return {
        "id": str(item.get("id") or item.get("url") or title),
        "source": "Indeed",
        "title": strip_html(str(title)),
        "company": item.get("companyName") or item.get("company") or "Unknown",
        "location": item.get("location") or "",
        "experience": ", ".join(str(x) for x in attributes if "year" in str(x).lower()) or "Not specified",
        "salary": item.get("salaryText") or "Not disclosed",
        "posted": str(posted),
        "posted_days": posted_days,
        "skills": attributes if isinstance(attributes, list) else [],
        "url": item.get("applyUrl") or item.get("url") or "",
        "description": strip_html(item.get("descriptionText") or item.get("description") or ""),
        "work_type": ", ".join(item.get("jobTypes") or []) if isinstance(item.get("jobTypes"), list) else "",
    }


def _submit(pool: ThreadPoolExecutor, fn: Callable[..., Any], *args: Any):
    """pool.submit that keeps the signed-in user (prefs, keys, files) in the worker thread."""
    return pool.submit(contextvars.copy_context().run, fn, *args)


def _ctx_map(pool: ThreadPoolExecutor, fn: Callable[..., Any], items: list[Any]) -> list[Any]:
    return [f.result() for f in [_submit(pool, fn, x) for x in items]]


_GOOGLE_ROTATION = [0]  # which city each role searches this run (changes every run)


def normalize_google(item: dict[str, Any]) -> dict[str, Any] | None:
    """A Google Jobs result (litescrape/google-jobs dataset row). No full description: title, company,
    place, posted age and the site it was listed on."""
    d = item.get("data") or {}
    if item.get("group") not in (None, "jobs_results") or not d.get("title"):
        return None
    via = str(d.get("via") or "").removeprefix("via ").strip()
    types = ", ".join(d.get("employment_types") or []).replace("\u2013", "-")
    return {
        "id": str(d.get("job_id") or d.get("link")),
        "source": "Google",
        "title": strip_html(str(d["title"])),
        "company": d.get("company_name") or "Unknown",
        "location": d.get("location") or "",
        "experience": "Not specified",
        "salary": "Not disclosed",
        "posted": d.get("posted") or "",
        "posted_days": _posted_days_from_text(d.get("posted")),
        "skills": [],
        "url": d.get("link") or item.get("url") or "",
        "description": (f"Listed on {via} (found through Google Jobs; open the link for the full description "
                        f"and apply options).") if via else "",
        "work_type": types.replace("Full–time", "Full-time"),
        "listed_on": via,
    }


# Apify's free plan runs at most 5 actors at once: more are queued here instead of failing.
_ACTOR_SLOTS = threading.BoundedSemaphore(4)


def _run_actor(name: str, actor_id: str, run_input: dict[str, Any], max_charge: float) -> tuple[str, list[dict[str, Any]], str | None, float | None]:
    with _ACTOR_SLOTS:
        return _run_actor_now(name, actor_id, run_input, max_charge)


def _run_actor_now(name: str, actor_id: str, run_input: dict[str, Any], max_charge: float) -> tuple[str, list[dict[str, Any]], str | None, float | None]:
    client = _client()
    try:
        run = client.actor(actor_id).call(
            run_input=run_input,
            run_timeout=ACTOR_TIMEOUT,
            wait_duration=ACTOR_TIMEOUT,
            max_total_charge_usd=max_charge,  # safety cap per run
            logger=None,
        )
        items = _dataset_items(client, run)
        cost = None
        try:  # re-read the finished run for its final cost
            run_id = run["id"] if isinstance(run, dict) else run.id
            info = client.run(run_id).get()
            cost = (info.get("usageTotalUsd") if isinstance(info, dict) else getattr(info, "usage_total_usd", None))
        except Exception:  # noqa: BLE001
            pass
        return name, items, None, cost
    except Exception as exc:  # noqa: BLE001 — surface actor errors to the UI
        return name, [], str(exc), None


def normalize_internshala(item: dict[str, Any]) -> dict[str, Any] | None:
    title = item.get("title")
    if not title or item.get("listingType", "job") != "job":
        return None
    posted = item.get("postedAt") or ""
    # "Who can apply" / requirements carry the experience rule ("minimum 1 years of experience").
    extra = "\n".join(f"{label}: {item[k]}" for k, label in
                       (("whoCanApply", "Who can apply"), ("otherRequirements", "Other requirements")) if item.get(k))
    return {
        "id": str(item.get("jobKey") or item.get("jobId") or item.get("canonicalUrl") or title),
        "source": "Internshala",
        "title": strip_html(str(title)),
        "company": item.get("company") or "Unknown",
        "location": item.get("location") or "",
        "experience": item.get("experienceText") or "Not specified",
        "salary": item.get("stipendText") or "Not disclosed",
        "posted": str(posted),
        "posted_days": _posted_days_from_iso(f"{posted}T00:00:00+05:30" if len(str(posted)) == 10 else posted),
        "skills": item.get("skills") if isinstance(item.get("skills"), list) else [],
        "url": item.get("canonicalUrl") or item.get("applyUrl") or "",
        "description": strip_html(f"{item.get('description') or ''}\n{extra}"),
        "work_type": str(item.get("employmentType") or "").replace("_", " ").lower()
                     + (", part time" if item.get("isPartTime") else ""),
    }


_FREE_STATS: dict[str, dict[str, Any]] = {}  # boards whose jobs come back already normalized (+ their stats)


def _apify_allowance(old: dict[str, Any], now: datetime) -> float:
    """USD all boards together may still spend on Apify today.

    Uses Apify's own numbers: credit left at the start of today / days left in the cycle, minus what
    the account already spent today. Apify counts every run made with this key (test copies of the
    app too), so the month's $5 can't be used up early by runs this file doesn't know about."""
    local_spent = (old.get("apify_spend") or {}).get(now.astimezone().date().isoformat(), 0.0)
    if cfg.APIFY_USD_PER_DAY is not None:
        return max(0.0, float(cfg.APIFY_USD_PER_DAY) - local_spent)
    try:
        import requests

        headers = {"Authorization": f"Bearer {_api_token()}"}
        d = requests.get("https://api.apify.com/v2/users/me/limits", headers=headers, timeout=20).json()["data"]
        left = d["limits"]["maxMonthlyUsageUsd"] - d["current"]["monthlyUsageUsd"]
        end = datetime.fromisoformat(d["monthlyUsageCycle"]["endAt"].replace("Z", "+00:00"))
        days_left = max(1, (end - now).days + 1)
        spent_today = local_spent
        try:
            usage = requests.get("https://api.apify.com/v2/users/me/usage/monthly", headers=headers, timeout=20).json()["data"]
            today = now.date().isoformat()  # Apify's days are UTC
            spent_today = next((float(x.get("totalUsageCreditsUsd") or 0) for x in usage.get("dailyServiceUsages") or []
                                if str(x.get("date", ""))[:10] == today), 0.0)
        except Exception:  # noqa: BLE001
            pass
        return max(0.0, (left + spent_today) / days_left - spent_today)
    except Exception:  # noqa: BLE001 — unknown credit: be careful
        return max(0.0, 0.1 - local_spent)


def _run_linkedin(name: str, conf: dict[str, Any], hours: float, old: dict[str, Any], now: datetime, cap: float,
                  use_apify: bool = True) -> tuple[str, list[dict[str, Any]], str | None, float | None]:
    """LinkedIn's public job search first (free). Searches it couldn't finish (rate limit) go to Apify
    (valig, ~$0.0004/job) in two rounds; jobs fetched before are skipped and not charged."""
    from fresherpilot.core import users
    from fresherpilot.jobs import free_boards, llm_filter

    titles = list(users.prefs()["linkedin_titles"])
    cities, remote = _locations()
    # One search per title and city (LinkedIn takes one location per search); remote = India + remote filter.
    places = cities + (["Remote"] if remote else []) if (cities or remote) else ["India"]
    all_searches = [(t, p) for t in titles for p in places]
    # Searches the last fetch didn't reach go first, so no search is starved by the time limit.
    pending_file = users.path("linkedin_pending.json")
    try:
        pending = [tuple(x) for x in json.loads(pending_file.read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        pending = []
    all_searches = [s for s in pending if s in all_searches] + [s for s in all_searches if s not in pending]
    known = {str(j.get("id")) for j in old.get("jobs") or [] if j.get("source") == "LinkedIn"}
    seen = {str(j.get("id")) for j in old.get("jobs") or [] if j.get("source") == "LinkedIn"
            and (j.get("first_seen") or "") >= (now - timedelta(days=5)).isoformat()}
    jobs: dict[str, dict[str, Any]] = {}
    free_stats: dict[str, Any] = {}
    try:
        free = free_boards.linkedin(all_searches, hours, known, lambda t: llm_filter.title_excluded(t) is None,
                                    minutes=cfg.LINKEDIN_FREE_MINUTES)
        jobs.update({j["id"]: j for j in free["jobs"]})
        searches = [x for x in all_searches if x not in free["done"]]
        free_stats = {"free_jobs": len(free["jobs"]), "free_title_skipped": free["skipped_title"]}
        if free["blocked"]:
            free_stats["free_blocked"] = free["blocked"]
    except Exception as exc:  # noqa: BLE001 — free fetch broke: Apify does all searches
        searches = all_searches
        free_stats = {"free_error": str(exc)[:200]}
    not_done = len(searches)
    try:
        pending_file.write_text(json.dumps([list(s) for s in searches]), encoding="utf-8")
    except OSError:
        pass
    if not use_apify or cfg.LINKEDIN_FREE_ONLY:
        searches = []
    base = _input_for(name, conf, hours)
    cost, failed = 0.0, []
    half = (len(searches) + 1) // 2
    for wave in (searches[:half], searches[half:]):
        skip = sorted(seen | set(jobs))

        def one(search: tuple[str, str], skip: list[str] = skip):
            title, place = search
            run_input = {**base, "keywords": title, "skipJobId": skip, "location": place}
            if place == "Remote":
                run_input.update(location="India", urlParam=base["urlParam"] + [{"key": "f_WT", "value": "2"}])
            label = title if place == "India" else f"{title} @ {place}"
            return label, _run_actor(name, conf["actor"], run_input, max(cfg.MIN_RUN_USD, cap / max(1, len(searches))))

        if not wave:
            continue
        with ThreadPoolExecutor(max_workers=3) as pool:
            for title, (_, items, error, c) in _ctx_map(pool, one, wave):
                cost += c or 0.0
                if error:
                    failed.append((title, error))
                for item in items:
                    job = normalize_linkedin(item)
                    if job:
                        jobs.setdefault(job["id"], job)
    stats: dict[str, Any] = {"searches": [t if p == "India" else f"{t} @ {p}" for t, p in all_searches],
                             "apify_searches": len(searches), **free_stats,
                             "apify_jobs": len(jobs) - free_stats.get("free_jobs", 0)}
    if failed:
        stats["apify_failed"] = [t for t, _ in failed]
    _FREE_STATS[name] = stats
    _LINKEDIN_SPEND[0] = cost
    error = None if jobs or not failed else failed[0][1]
    left = not_done if not use_apify or cfg.LINKEDIN_FREE_ONLY else len(failed)
    if left and not error:  # keep LinkedIn's "last fetched" time, so the next fetch covers these searches
        error = (f"{left} of {len(all_searches)} LinkedIn searches left for the next fetch "
                 f"(LinkedIn's free search is rate-limited; {len(jobs)} jobs fetched)")
    return name, list(jobs.values()), error, cost


_LINKEDIN_SPEND = [0.0]


def _spend_log(old: dict[str, Any], now: datetime, spent: float) -> dict[str, float]:
    """USD spent on Apify per day (last 7 days), so several fetches a day share one daily budget."""
    log = dict(old.get("apify_spend") or {})
    day = now.astimezone().date().isoformat()
    log[day] = round(log.get(day, 0.0) + spent, 4)
    return {d: v for d, v in sorted(log.items())[-7:]}


def _run_indeed(name: str, conf: dict[str, Any], hours: float, cap: float, use_apify: bool = True
                ) -> tuple[str, list[dict[str, Any]], str | None, float | None]:
    """Indeed free via python-jobspy; Apify only if that fails."""
    from fresherpilot.core import users
    from fresherpilot.jobs import free_boards

    cities, remote = _locations()
    places = cities + (["Remote"] if remote else []) if (cities or remote) else ["India"]
    try:
        jobs = free_boards.indeed(list(users.prefs()["roles"]), places, hours)
        _FREE_STATS[name] = {"free_jobs": len(jobs)}
        return name, jobs, None, 0.0
    except Exception as exc:  # noqa: BLE001
        if not use_apify:
            return name, [], f"Free Indeed fetch failed ({exc}) and there is no Apify credit left today", 0.0
        _FREE_STATS[name] = {"fallback": f"free fetch failed: {str(exc)[:150]}"}
        return _run_actor(name, conf["actor"], _input_for(name, conf, hours), cap)


def _run_naukri(name: str, conf: dict[str, Any], hours: float, cap: float
                ) -> tuple[str, list[dict[str, Any]], str | None, float | None]:
    """One run per experience value (search_config.NAUKRI_EXPERIENCE_PASSES), each with its own
    "only new jobs" state."""
    base = _input_for(name, conf, hours)
    passes = list(cfg.NAUKRI_EXPERIENCE_PASSES) or [base.get("experience", "0")]
    shares = [0.6, 0.4] if len(passes) == 2 else [1 / len(passes)] * len(passes)
    items: list[dict[str, Any]] = []
    cost, errors = 0.0, []
    for exp, share in zip(passes, shares):
        # pass "0" keeps the original state key, so its history from before this change still counts
        state = base["stateKey"] if exp == "0" else f"{base['stateKey']}-exp{exp}"
        _, got, err, c = _run_actor(name, conf["actor"], {**base, "experience": exp, "stateKey": state},
                                    max(cfg.MIN_RUN_USD, cap * share))
        items += got
        cost += c or 0.0
        if err:
            errors.append(f"experience {exp}: {err}")
    return name, items, (None if items or not errors else "; ".join(errors)), cost


def _run_telegram(name: str, hours: float) -> tuple[str, list[dict[str, Any]], str | None, float | None]:
    """Public Telegram job channels from the user's settings (free)."""
    from fresherpilot.jobs import free_boards

    channels = [str(c) for c in _prefs().get("telegram_channels") or [] if str(c).strip()]
    try:
        jobs, errs = free_boards.telegram(channels, hours)
        _FREE_STATS[name] = {"channels": len(channels), **({"problems": errs[:5]} if errs else {})}
        return name, jobs, ("; ".join(errs[:3]) if errs and not jobs else None), 0.0
    except Exception as exc:  # noqa: BLE001
        return name, [], str(exc), None


def _run_careers(name: str, hours: float) -> tuple[str, list[dict[str, Any]], str | None, float | None]:
    """Company career pages (free, no Apify): already in the app's job format."""
    from fresherpilot.jobs import ats_jobs

    try:
        jobs, errs = ats_jobs.fetch(hours)
        return name, jobs, ("; ".join(errs[:3]) if errs and not jobs else None), 0.0
    except Exception as exc:  # noqa: BLE001
        return name, [], str(exc), None


SOURCES: dict[str, tuple[dict[str, Any], Callable[[dict[str, Any]], dict[str, Any] | None]]] = {
    "naukri": (cfg.NAUKRI, normalize_naukri),
    "linkedin": (cfg.LINKEDIN, normalize_linkedin),
    "indeed": (cfg.INDEED, normalize_indeed),
    "wellfound": (cfg.WELLFOUND, normalize_wellfound),
    "internshala": (cfg.INTERNSHALA, normalize_internshala),
    "careers": (cfg.CAREERS, lambda job: job),
    "telegram": (cfg.TELEGRAM, lambda job: job),
    "google": (cfg.GOOGLE_JOBS, normalize_google),
}
FREE_RUNNERS = {"careers", "telegram"}        # never use Apify
FREE_FIRST = {"linkedin", "indeed"}           # free fetch, Apify only as a fallback


def _hours_since(iso: str | None, now: datetime) -> float | None:
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 3600 if iso else None
    except ValueError:
        return None


def _window_hours(name: str, last_fetch: dict[str, str], now: datetime) -> float:
    """Search only since this board's last successful fetch (+2 h overlap), at most POSTED_WITHIN_DAYS.
    Boards without an "only new jobs" mode (LinkedIn, Indeed) otherwise re-sell jobs already bought."""
    since = _hours_since(last_fetch.get(name), now)
    full = cfg.POSTED_WITHIN_DAYS * 24
    if name == "careers":  # company pages keep openings up for weeks: the first fetch looks further back
        full = cfg.CAREERS_FIRST_DAYS * 24 if since is None else full
    if name == "telegram":
        full = cfg.TELEGRAM_FIRST_DAYS * 24
    return full if since is None else max(6.0, min(full, since + 2))


def _prefs() -> dict[str, Any]:
    from fresherpilot.core import users

    return users.prefs()


def _locations() -> tuple[list[str], bool]:
    """(cities, include_remote) from the user's settings. No cities and no remote = all of India."""
    from fresherpilot.core import users

    raw = [str(x).strip() for x in users.prefs().get("locations") or [] if str(x).strip()]
    remote = any(x.lower() in ("remote", "wfh", "work from home") for x in raw)
    return [x for x in raw if x.lower() not in ("remote", "wfh", "work from home", "india")], remote


def _slug(city: str) -> str:
    """City name as Wellfound / Internshala spell it."""
    c = city.strip().lower()
    return {"bengaluru": "bangalore", "gurugram": "gurgaon", "new delhi": "delhi"}.get(c, c)


_REMOTE = re.compile(r"remote|work from home|\bwfh\b|anywhere")


def in_wanted_location(job: dict[str, Any]) -> bool:
    """Free check of a job's location text against the user's cities (catches what a board's own
    location search let through, and the free career pages). Unknown / "India"-only locations stay."""
    cities, remote = _locations()
    if not cities and not remote:
        return True
    loc = str(job.get("location") or "").lower()
    if not loc.strip() or loc.strip(" ,") in ("india", "in"):
        return True
    if remote and _REMOTE.search(loc):
        return True
    words = {w for c in cities for w in cfg.CITY_ALIASES.get(c.lower(), [c.lower()])}
    return any(w in loc for w in words)


def _input_for(name: str, conf: dict[str, Any], hours: float | None = None) -> dict[str, Any]:
    import math

    from fresherpilot.core import users

    run_input = dict(conf["input"])
    prefs, uid = users.prefs(), users.current() or "local"
    cities, remote = _locations()
    # Location goes into each board's own search, so jobs elsewhere are never fetched (or charged).
    if name == "naukri":
        run_input.update(searchQueries=prefs["roles"], departmentIds=prefs["naukri_departments"],
                         stateKey=f"jobfinder-naukri-{uid}")
        if cities or remote:
            run_input["location"] = ", ".join(cities + (["Remote"] if remote else []))
    elif name == "internshala":
        run_input.update(query=json.dumps(prefs["internshala_queries"]), stateKey=f"jobfinder-internshala-{uid}")
        if cities:
            run_input["location"] = json.dumps([_slug(c) for c in cities])
    elif name == "wellfound":
        run_input["keywords"] = prefs["wellfound_keywords"]
        if cities:
            run_input["locations"] = [_slug(c) for c in cities]
    hours = hours or cfg.POSTED_WITHIN_DAYS * 24
    days = max(1, math.ceil(hours / 24))
    if name == "indeed":
        url = conf["search_url"].replace(f"fromage={cfg.POSTED_WITHIN_DAYS}", f"fromage={min(days, cfg.POSTED_WITHIN_DAYS)}")
        places = cities + (["Remote"] if remote else []) if (cities or remote) else ["India"]
        run_input["startUrls"] = [{"url": url.replace("l=India", "l=" + quote_plus(p)).format(query=quote_plus(q))}
                                  for q in prefs["roles"][:6] for p in places]
    elif name == "linkedin":
        words = [w for cat, ws in cfg.TITLE_EXCLUDE.items() if cat != "off_stack" for w in ws] + list(prefs.get("off_stack") or [])
        run_input["titleExclude"] = sorted({w.strip() for w in words if len(w.strip()) > 3 and w.strip() not in ("java", "head", "intern")})
        seconds = int(hours * 3600)
        run_input["urlParam"] = [p if p["key"] != "f_TPR" else {"key": "f_TPR", "value": f"r{seconds}"} for p in run_input["urlParam"]]
    elif name == "naukri":
        run_input["freshness"] = 1 if days <= 1 else (3 if days <= 3 else 7)
    elif name == "wellfound":
        run_input["postedWithinDays"] = min(days, cfg.POSTED_WITHIN_DAYS)
    if name == "google":  # one search per role; the city rotates with each run so all pairs get covered
        places = cities + (["Remote"] if remote else []) if (cities or remote) else ["India"]
        shift = _GOOGLE_ROTATION[0]
        run_input["requests"] = [{"q": f"{role} jobs in {places[(i + shift) % len(places)]}" if places[(i + shift) % len(places)] != "Remote"
                                  else f"remote {role} jobs India"} for i, role in enumerate(prefs["roles"][:10])]
        run_input["maxRequests"] = len(run_input["requests"])
    return run_input


def _dedupe_key(job: dict[str, Any]) -> str:
    norm = lambda v: re.sub(r"[^a-z0-9]+", " ", str(v or "").lower()).strip()  # noqa: E731
    return f"{norm(job['title'])}|{norm(job['company'])}"


def _stamp(job: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Store the posting date, so "posted N days ago" stays right in later sessions."""
    if job.get("posted_days") is not None and not job.get("posted_on"):
        job["posted_on"] = (now - timedelta(days=int(job["posted_days"]))).date().isoformat()
    job.setdefault("first_seen", now.isoformat())
    return job


def fetch_latest_jobs(progress: Callable[[int, int, list[str]], None] | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    old = load_cache() or {}
    last_fetch: dict[str, str] = dict(old.get("source_fetched_at") or {})
    errors: dict[str, str] = {}
    raw: dict[str, list[dict[str, Any]]] = {name: [] for name in SOURCES}
    costs: dict[str, float | None] = {}

    # Wellfound charges a start fee per run: once per day (the first fetch of each day, your local
    # time), and never twice within WELLFOUND_MIN_GAP_HOURS.
    active = dict(SOURCES)
    since_wf = _hours_since(last_fetch.get("wellfound"), now)
    if since_wf is not None:
        last_day = datetime.fromisoformat(last_fetch["wellfound"]).astimezone().date()
        if last_day == now.astimezone().date() or since_wf < cfg.WELLFOUND_MIN_GAP_HOURS:
            active.pop("wellfound")
    # Google Jobs: the free plan allows 25 searches a day for this actor (10 per fetch).
    runs = [t for t in (old.get("google_runs") or []) if t[:10] == now.date().isoformat()]
    since_google = _hours_since(runs[-1], now) if runs else None
    _GOOGLE_ROTATION[0] = len(old.get("google_runs_all") or [])
    if len(runs) >= cfg.GOOGLE_RUNS_PER_DAY or (since_google is not None and since_google < cfg.GOOGLE_MIN_GAP_HOURS):
        active.pop("google", None)
    if cfg.LIVE_POLLER:  # read all the time by the live poller instead
        for n in cfg.LIVE_BOARDS:
            active.pop(n, None)
    for n in [n for n in active if n not in FREE_RUNNERS | FREE_FIRST | cfg.LIVE_BOARDS | cfg.APIFY_BOARDS]:
        active.pop(n)  # Apify-only boards that aren't allowed to spend credit (e.g. Google Jobs)
    windows = {name: _window_hours(name, last_fetch, now) for name in active}
    budget = _apify_allowance(old, now)
    caps = {name: min(conf.get("max_charge_usd", 1.0), max(cfg.MIN_RUN_USD, budget * cfg.BUDGET_SHARE.get(name, 0.1)))
            for name, (conf, _) in active.items()}
    if "wellfound" in active and budget * cfg.BUDGET_SHARE["wellfound"] < 0.07:  # its start fee alone is $0.06
        active.pop("wellfound")
        caps.pop("wellfound")

    # No Apify credit left today: paid-only boards wait (a run would fail or eat tomorrow's share);
    # LinkedIn / Indeed still run free. Naukri is then covered by the extension's Naukri sweep.
    has_apify = bool(_api_token()) and budget >= cfg.MIN_RUN_USD
    no_credit = [] if has_apify else [n for n in list(active) if n not in FREE_RUNNERS | FREE_FIRST]
    for n in no_credit:
        active.pop(n)
        caps.pop(n, None)
        windows.pop(n, None)
    if not _prefs().get("telegram_channels"):
        active.pop("telegram", None)

    with ThreadPoolExecutor(max_workers=max(1, len(active))) as pool:
        _FREE_STATS.clear()
        futures = [
            _submit(pool, _run_careers, name, windows[name]) if name == "careers" else
            _submit(pool, _run_telegram, name, windows[name]) if name == "telegram" else
            _submit(pool, _run_linkedin, name, conf, windows[name], old, now, caps[name], has_apify) if name == "linkedin" else
            _submit(pool, _run_indeed, name, conf, windows[name], caps[name], has_apify) if name == "indeed" else
            _submit(pool, _run_naukri, name, conf, windows[name], caps[name]) if name == "naukri" else
            _submit(pool, _run_actor, name, conf["actor"], _input_for(name, conf, windows[name]), caps[name])
            for name, (conf, _) in active.items()
        ]
        pending = list(active)
        if progress:
            progress(0, len(active), pending)
        for future in as_completed(futures):
            name, items, error, cost = future.result()
            pending.remove(name)
            if progress:
                progress(len(active) - len(pending), len(active), pending)
            raw[name] = items
            costs[name] = cost
            if error:
                errors[name] = error
            else:
                last_fetch[name] = now.isoformat()

    # Merge: this fetch first (fresher data wins, original first-seen date kept), then earlier jobs
    # still within KEEP_JOBS_DAYS.
    previous = old.get("jobs") or []
    prev_by_key = {_dedupe_key(j): j for j in previous}
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    report: dict[str, dict[str, Any]] = {}
    for name, (_, normalize) in SOURCES.items():
        if name not in active:
            continue
        r = report[name] = {"cost_usd": costs.get(name), "items": len(raw[name]), "duplicates": 0, "already_had": 0,
                            "new": 0, "other_city": 0, "window_hours": round(windows[name])}
        if name in _FREE_STATS:
            r["free"] = _FREE_STATS[name]
        free_jobs = name in _FREE_STATS and "fallback" not in _FREE_STATS[name]
        for item in raw[name]:
            job = item if free_jobs else normalize(item)
            if not job:
                continue
            if not in_wanted_location(job):  # before the AI check: no Groq tokens spent on it
                r["other_city"] += 1
                continue
            if name == "google" and (job.get("posted_days") or 0) > max(1, windows[name] / 24 + 1):
                r.setdefault("too_old", 0)
                r["too_old"] += 1
                continue
            key = _dedupe_key(job)
            if key in seen:
                r["duplicates"] += 1
                continue
            seen.add(key)
            if key in prev_by_key:
                r["already_had"] += 1
                job["first_seen"] = prev_by_key[key].get("first_seen") or now.isoformat()
            else:
                r["new"] += 1
            jobs.append(_stamp(job, now))
    new_count = sum(r["new"] for r in report.values())
    cutoff = now - timedelta(days=cfg.KEEP_JOBS_DAYS)
    for job in previous:
        key = _dedupe_key(job)
        first_seen = job.get("first_seen")
        try:
            fresh = first_seen is None or datetime.fromisoformat(first_seen) >= cutoff
        except ValueError:
            fresh = True
        if key not in seen and fresh and in_wanted_location(job):  # changing cities also hides old jobs
            seen.add(key)
            jobs.append(_stamp(job, now))

    payload = {
        "fetched_at": now.isoformat(),
        "source_fetched_at": last_fetch,
        "apify_spend": _spend_log(old, now, sum(c for c in costs.values() if isinstance(c, (int, float)))),
        "apify_budget_today": round(budget, 3),
        "raw_counts": {name: len(items) for name, items in raw.items() if name in active},
        "skipped_sources": [n for n in SOURCES if n not in active],
        "no_apify_credit": no_credit,
        "google_runs": runs + ([now.isoformat()] if "google" in active and "google" not in errors else []),
        "google_runs_all": ((old.get("google_runs_all") or []) + ([now.isoformat()] if "google" in active else []))[-50:],
        "fetch_report": report,
        "new_this_fetch": new_count,
        "errors": errors,
        "jobs": jobs,
    }
    with _CACHE_LOCK:
        latest = load_cache() or {}
        for job in latest.get("jobs") or []:
            if job.get("via") in ("extension", "manual", "live") and _dedupe_key(job) not in seen:
                seen.add(_dedupe_key(job))
                jobs.append(job)
        payload["extension_sweeps"] = latest.get("extension_sweeps") or old.get("extension_sweeps") or {}
        _cache_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


_CACHE_LOCK = threading.RLock()


# ---------------------------------------------------------------- jobs sent in by the browser extension

_NAUKRI_ID = re.compile(r"-(\d{9,})(?:[?#/]|$)")


def normalize_naukri_card(item: dict[str, Any]) -> dict[str, Any] | None:
    """A job card read from a Naukri search page by the extension (title, company, exp, ...)."""
    title = strip_html(str(item.get("title") or "")).strip()
    url = str(item.get("url") or "").split("?")[0]
    if not title or "naukri.com" not in url:
        return None
    m = _NAUKRI_ID.search(url)
    jid = str(item.get("id") or (m.group(1) if m else url))
    exp = re.sub(r"\s*Yrs?\b", " years", str(item.get("experience") or "")).strip()
    return {
        "id": jid,
        "source": "Naukri",
        "title": title,
        "company": strip_html(str(item.get("company") or "")).strip() or "Unknown",
        "location": str(item.get("location") or ""),
        "experience": exp or "Not specified",
        "salary": str(item.get("salary") or "") or "Not disclosed",
        "posted": str(item.get("posted") or ""),
        "posted_days": _posted_days_from_text(item.get("posted")),
        "skills": [str(x) for x in item.get("skills") or []][:20],
        "url": url,
        "description": strip_html(str(item.get("description") or "")),
        "work_type": "",
        "via": "extension",
    }


def merge_external(items: list[dict[str, Any]], kind: str, check_location: bool = True) -> dict[str, int]:
    """Add jobs from outside a fetch to the saved list (dedupe like a fetch): "naukri" = cards the
    extension read in the user's browser, "manual" = a job the user reported as missed."""
    normalize = {"naukri": normalize_naukri_card, "manual": lambda j: j, "live": lambda j: {**j, "via": "live"}}[kind]
    now = datetime.now(timezone.utc)
    with _CACHE_LOCK:
        payload = load_cache() or {"jobs": [], "fetched_at": now.isoformat()}
        jobs = payload.get("jobs") or []
        keys = {_dedupe_key(j) for j in jobs}
        added = dupes = other_city = 0
        for item in items:
            job = normalize(item)
            if not job:
                continue
            if check_location and not in_wanted_location(job):
                other_city += 1
                continue
            key = _dedupe_key(job)
            if key in keys:
                dupes += 1
                continue
            keys.add(key)
            jobs.append(_stamp(job, now))
            added += 1
        payload["jobs"] = jobs
        if kind == "naukri":
            payload.setdefault("extension_sweeps", {})[kind] = {"at": now.isoformat(), "added": added}
        payload["new_this_fetch"] = (payload.get("new_this_fetch") or 0) + added
        _cache_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {"added": added, "duplicates": dupes, "other_city": other_city}


def naukri_sweep_plan() -> dict[str, Any]:
    """Naukri search pages for the extension to read in the user's own browser (free, no Apify).
    Every role, experience 0 and 1 (so "1-3 years" jobs come in too), the user's cities (+ Remote),
    newest first, posted since the last sweep (1, 3 or 7 days)."""
    prefs = _prefs()
    cities, remote = _locations()
    payload = load_cache() or {}
    last = ((payload.get("extension_sweeps") or {}).get("naukri") or {}).get("at")
    hours = _hours_since(last, datetime.now(timezone.utc))
    age = 1 if hours is not None and hours < 22 else (3 if hours is None or hours < 70 else 7)
    loc = ", ".join([c.lower() for c in cities] + (["remote"] if remote else []))
    urls = []
    for role in prefs["roles"]:
        slug = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")
        for exp in cfg.NAUKRI_EXPERIENCE_PASSES:
            q = f"k={quote_plus(role)}&experience={exp}&jobAge={age}" + (f"&l={quote_plus(loc)}" if loc else "")
            urls.append(f"https://www.naukri.com/{slug}-jobs?{q}")
    return {"urls": urls, "max_pages": 5, "gap_seconds": [4, 8], "job_age_days": age}


def load_cache() -> dict[str, Any] | None:
    if not _cache_path().exists():
        return None
    try:
        payload = json.loads(_cache_path().read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    today = date.today()
    for job in payload.get("jobs") or []:
        if job.get("posted_on"):
            try:
                job["posted_days"] = max(0, (today - date.fromisoformat(job["posted_on"])).days)
            except ValueError:
                pass
    return payload
