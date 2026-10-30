"""Web search used by the contact finder. No Docker, no server needed.

Providers, in order:
  1. Serper.dev (optional) - Google results via an official API, only if SERPER_API_KEY is set in .env.
  2. ddgs (default, free)  - a Python library that queries Bing, Yahoo, Brave, DuckDuckGo, Google, Mojeek...
                             directly and rotates between them, skipping engines that block us.

All providers return results as {"title", "url", "content"}.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

def _serper_key() -> str:
    from fresherpilot.core import users

    return users.key("SERPER_API_KEY").strip('"').strip("'")
GAP_SECONDS = 3.0  # space out free searches so engines don't flag us as a bot
BLOCK_COOLDOWN_SECONDS = 10 * 60


class SearchBlocked(RuntimeError):
    """No provider could search right now (blocked, out of quota, or no results anywhere)."""


_lock = threading.Lock()
_last_search = 0.0
_serper_disabled_reason = ""  # set when the key is invalid or out of credits
_blocked_until = 0.0
_blocked_reason = ""


# ---------------------------------------------------------------- Serper (optional)

def _serper(query: str) -> list[dict[str, Any]]:
    global _serper_disabled_reason
    resp = requests.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": _serper_key(), "Content-Type": "application/json"},
        json={"q": query, "gl": "in", "hl": "en", "num": 10},
        timeout=20,
    )
    if resp.status_code in (401, 403):
        _serper_disabled_reason = "Serper API key was rejected"
        raise SearchBlocked(_serper_disabled_reason)
    if resp.status_code in (400, 402, 429) and re.search(r"credit|quota|limit", resp.text, re.I):
        _serper_disabled_reason = "Serper searches are used up"
        raise SearchBlocked(_serper_disabled_reason)
    resp.raise_for_status()
    return [
        {"title": r.get("title") or "", "url": r.get("link") or "", "content": r.get("snippet") or ""}
        for r in resp.json().get("organic") or []
    ]


# ---------------------------------------------------------------- ddgs (free)

def _ddgs(query: str) -> list[dict[str, Any]]:
    global _last_search, _blocked_until, _blocked_reason
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException

    with _lock:
        wait = GAP_SECONDS - (time.time() - _last_search)
        if wait > 0:
            time.sleep(wait)
        try:
            results = DDGS(timeout=15).text(query, region="in-en", max_results=10, backend="auto")
        except DDGSException as exc:
            if re.search(r"no results", str(exc), re.I) and _engines_answer():
                return []  # engines work; this query just has no results
            if re.search(r"ratelimit|no results|timed? ?out|403|429", str(exc), re.I):
                _blocked_reason = "Web search engines are rate-limiting your network for now, so only guessed contacts are shown. Try “Search again” later."
                _blocked_until = time.time() + BLOCK_COOLDOWN_SECONDS
                raise SearchBlocked(_blocked_reason) from exc
            raise
        finally:
            _last_search = time.time()
    cleaned = []
    for r in results or []:
        if not r.get("href"):
            continue
        # Some engines glue the next results' titles on ("Name - HR at X ...Other Name - ..."); keep the first.
        title = re.split(r"\.\.\.|…|\|\s*LinkedIn", r.get("title") or "")[0].strip(" -|")
        cleaned.append({"title": title, "url": r["href"], "content": r.get("body") or ""})
    return cleaned


def _engines_answer() -> bool:
    """Do the engines answer a query that always has results? (tells "no results" from "blocked")."""
    from ddgs import DDGS

    try:
        time.sleep(1)
        return bool(DDGS(timeout=10).text("linkedin", region="in-en", max_results=1, backend="auto"))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- public

def provider() -> str:
    if _serper_key() and not _serper_disabled_reason:
        return "Google (Serper)"
    return "web (Bing/Yahoo/DDG via ddgs)"


def available() -> bool:
    return True  # ddgs needs no setup


def search(query: str) -> list[dict[str, Any]]:
    """Search the web with the best available provider."""
    if _serper_key() and not _serper_disabled_reason:
        try:
            return _serper(query)
        except (SearchBlocked, requests.RequestException):
            pass  # fall through to the free search
    if time.time() < _blocked_until:
        raise SearchBlocked(_blocked_reason)
    return _ddgs(query)


def reset_block() -> None:
    """Forget a previous block (used by an explicit "Search again")."""
    global _blocked_until
    _blocked_until = 0.0


def google_url(query: str) -> str:
    return "https://www.google.com/search?q=" + requests.utils.quote(query)
