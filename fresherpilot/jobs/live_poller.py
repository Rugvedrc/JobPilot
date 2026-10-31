"""Live job poller: reads the free boards all the time, so new jobs show up minutes after posting.

- LinkedIn: one title x city search at a time, in turns, with a "posted since this search was last
  read" window (+5 min overlap). Results are newest first, so reading stops at the first page that
  holds only known jobs. LinkedIn allows ~1 request / 2.5 s, which keeps every search under ~2 min old.
- Indeed (python-jobspy), Internshala and Wellfound: every cfg.LIVE_EVERY_MIN minutes, each in its
  own thread (so a slow board never delays the others).
- The regular fetch (Naukri on Apify, career pages, Telegram) every cfg.SCHEDULED_FETCH_HOURS.
- run_now() (the "Fetch new jobs" button): every board right away, and a full LinkedIn round.

New jobs are merged into the user's list (like an extension import) and the AI check starts once
10 are waiting or after cfg.LIVE_CHECK_MIN minutes, so Groq calls stay full (fewer tokens per job).
One poller per user, started when the user opens the app; it runs with that user's settings.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from fresherpilot.core import search_config as cfg
from fresherpilot.core import users

_lock = threading.Lock()
_state: dict[str, dict[str, Any]] = {}  # uid -> poller state
_SLOW_BOARDS = ("indeed", "internshala", "wellfound")


def status() -> dict[str, Any]:
    s = _state.get(users.current() or "")
    if not s:
        return {"running": False, "busy": []}
    busy = [b for b, info in s["boards"].items() if info.get("busy")]
    sweep = s.get("sweep")
    return {"running": s["running"], "boards": s["boards"], "pending_check": s["pending"], "busy": busy,
            "linkedin_round": dict(sweep) if sweep else None}


def ensure_running(start_checker: Callable[[], Any], start_fetch: Callable[[], Any]) -> None:
    """Start this user's poller (once). Call from a request, so the threads run as that user."""
    if not cfg.LIVE_POLLER:
        return
    uid = users.current()
    if not uid or not users.prefs().get("onboarded", True):
        return  # not before the user has set up their search (it would search with default settings)
    with _lock:
        if uid in _state and _state[uid]["running"]:
            return
        s = _state[uid] = {"running": True, "boards": {}, "pending": 0, "first_pending": None, "li_last": {}, "li_more": set(),
                           "due": {}, "sweep": None, "known": {}, "known_at": 0.0,
                           "check": start_checker, "fetch": start_fetch}
    users.thread(_linkedin_loop, s)
    for board in _SLOW_BOARDS:
        if board in cfg.LIVE_BOARDS:
            users.thread(_board_loop, s, board)
    users.thread(_fetch_loop, s)


def run_now() -> bool:
    """Every live board now (the "Fetch new jobs" button), plus one full round of LinkedIn searches."""
    s = _state.get(users.current() or "")
    if not s:
        return False
    for board in _SLOW_BOARDS:
        if not s["boards"].get(board, {}).get("busy"):
            s["due"][board] = 0
    if "linkedin" in cfg.LIVE_BOARDS:
        s["sweep"] = {"done": 0, "total": len(_li_searches())}
        _note(s, "linkedin", busy=True)
    return True


def stop(uid: str | None) -> None:
    """Stop a user's poller (account deleted). Its threads end after their current step."""
    s = _state.pop(uid or "", None)
    if s:
        s["running"] = False


# ---------------------------------------------------------------- helpers

def _known(s: dict[str, Any], source: str) -> set[str]:
    """IDs of jobs already in the list. Read from the cache file every 10 minutes (it can hold
    thousands of jobs); jobs this poller adds are added to the set right away."""
    from fresherpilot.jobs import apify_jobs

    if time.time() - s["known_at"] > 600:
        by: dict[str, set[str]] = {}
        for j in (apify_jobs.load_cache() or {}).get("jobs") or []:
            by.setdefault(str(j.get("source")), set()).add(str(j.get("id")))
        s["known"], s["known_at"] = by, time.time()
    return s["known"].setdefault(source, set())


def _places() -> list[str]:
    from fresherpilot.jobs.apify_jobs import _locations

    cities, remote = _locations()
    return cities + (["Remote"] if remote else []) if (cities or remote) else ["India"]


def _li_searches() -> list[tuple[str, str]]:
    return [(t, p) for t in users.prefs()["linkedin_titles"] for p in _places()]


def _note(s: dict[str, Any], board: str, **info: Any) -> None:
    b = s["boards"].setdefault(board, {"added": 0})
    b.update(info, at=datetime.now(timezone.utc).isoformat())


def _add(s: dict[str, Any], board: str, jobs: list[dict[str, Any]]) -> None:
    from fresherpilot.jobs import apify_jobs

    added = apify_jobs.merge_external(jobs, "live")["added"] if jobs else 0
    for j in jobs:
        _known(s, str(j.get("source"))).add(str(j.get("id")))
    b = s["boards"].setdefault(board, {"added": 0})
    b["added"] = b.get("added", 0) + added
    _note(s, board, last_added=added, error=None)
    if added:
        s["pending"] += added
        s["first_pending"] = s["first_pending"] or time.time()
    _maybe_check(s)


def _maybe_check(s: dict[str, Any]) -> None:
    if s["pending"] and (s["pending"] >= 10 or time.time() - (s["first_pending"] or 0) >= cfg.LIVE_CHECK_MIN * 60):
        s["check"]()
        s["pending"], s["first_pending"] = 0, None


# ---------------------------------------------------------------- LinkedIn (continuous)

def _linkedin_loop(s: dict[str, Any]) -> None:
    from fresherpilot.jobs import free_boards, llm_filter

    i = 0
    while s["running"]:
        if "linkedin" not in cfg.LIVE_BOARDS:
            time.sleep(60)
            continue
        try:
            searches = _li_searches()
            if not searches:
                time.sleep(60)
                continue
            search = searches[i % len(searches)]
            i += 1
            last = s["li_last"].get(search)
            now = time.time()
            # first read of a search: the last 24 h; later: since the last read (+5 min overlap)
            window = 86400 if last is None else int(min(86400, max(900, now - last + 300)))
            _note(s, "linkedin", busy=bool(s.get("sweep")), searches=len(searches), last_search=f"{search[0]} @ {search[1]}")
            res = free_boards.linkedin([search], window / 3600, _known(s, "LinkedIn"),
                                       lambda t: llm_filter.title_excluded(t) is None,
                                       minutes=5, seconds=window, stop_on_known=search not in s["li_more"],
                                       on_batch=lambda jobs: _add(s, "linkedin", jobs), max_new=25)
            if res["blocked"]:
                _note(s, "linkedin", busy=False, error=res["blocked"])
                time.sleep(120)  # rate-limited: wait, then carry on
                continue
            if res.get("more"):  # more left: same window next turn, read past the known jobs
                s["li_more"].add(search)
            else:
                s["li_more"].discard(search)
                s["li_last"][search] = now
            sweep = s.get("sweep")
            if sweep:
                sweep["done"] += 1
                if sweep["done"] >= sweep["total"]:
                    s["sweep"] = None
            _note(s, "linkedin", busy=bool(s.get("sweep")), ok_at=time.time())
            _maybe_check(s)
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            _note(s, "linkedin", busy=False, error=str(exc)[:200])
            time.sleep(60)


# ---------------------------------------------------------------- other boards (every few minutes)

def _indeed(s: dict[str, Any], since_h: float | None) -> list[dict[str, Any]]:
    from fresherpilot.jobs import free_boards

    hours = 24 if since_h is None else max(1, min(24, since_h + 0.5))
    return free_boards.indeed(list(users.prefs()["roles"])[:6], _places(), hours)


def _internshala(s: dict[str, Any], since_h: float | None) -> list[dict[str, Any]]:
    from fresherpilot.jobs import free_boards

    return free_boards.internshala(list(users.prefs()["internshala_queries"]), _known(s, "Internshala"))


def _wellfound(s: dict[str, Any], since_h: float | None) -> list[dict[str, Any]]:
    from fresherpilot.jobs import free_boards

    return free_boards.wellfound(list(users.prefs()["linkedin_titles"]), _places(), _known(s, "Wellfound"))


_SLOW = {"indeed": _indeed, "internshala": _internshala, "wellfound": _wellfound}


def _board_loop(s: dict[str, Any], board: str) -> None:
    from fresherpilot.jobs import free_boards

    while s["running"]:
        now = time.time()
        if now < s["due"].get(board, 0):
            time.sleep(5)
            continue
        last = s["boards"].get(board, {}).get("ok_at")
        _note(s, board, busy=True)
        try:
            jobs = _SLOW[board](s, None if last is None else (now - last) / 3600)
            _add(s, board, jobs)
            _note(s, board, busy=False, ok_at=time.time())
            s["due"][board] = time.time() + cfg.LIVE_EVERY_MIN.get(board, 30) * 60
        except free_boards.WellfoundBlocked as exc:
            _note(s, board, busy=False, error=str(exc))
            s["due"][board] = time.time() + 3 * 3600  # blocked: leave it alone for 3 hours
        except Exception as exc:  # noqa: BLE001
            _note(s, board, busy=False, error=str(exc)[:200])
            s["due"][board] = time.time() + 10 * 60
        _maybe_check(s)


def _fetch_loop(s: dict[str, Any]) -> None:
    """The regular fetch: Naukri (Apify, budget-capped), career pages, Telegram. Its own boards'
    fetch times count (the poller's merges also touch the list's "fetched_at")."""
    from fresherpilot.jobs import apify_jobs

    while s["running"]:
        try:
            stamps = ((apify_jobs.load_cache() or {}).get("source_fetched_at") or {}).values()
            ages = [h for h in (apify_jobs._hours_since(t, datetime.now(timezone.utc)) for t in stamps) if h is not None]
            fetched = min(ages) if ages else None
            if fetched is None or fetched >= cfg.SCHEDULED_FETCH_HOURS:
                s["fetch"]()
                wait = cfg.SCHEDULED_FETCH_HOURS * 3600
            else:
                wait = (cfg.SCHEDULED_FETCH_HOURS - fetched) * 3600
        except Exception:  # noqa: BLE001
            wait = 600
        end = time.time() + max(60, wait)
        while s["running"] and time.time() < end:
            _maybe_check(s)
            time.sleep(30)
