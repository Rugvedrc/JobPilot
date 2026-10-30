"""Remember which jobs you've already looked at, across fetches.

A job is matched by its platform id and also by title + company, so the same job
re-appearing in a later run (or on another platform) still shows as visited.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

def _path() -> Path:
    from fresherpilot.core import users

    return users.path("visited.json")
_lock = threading.Lock()


def uid(job: dict[str, Any]) -> str:
    return f"{job.get('source')}:{job.get('id')}"


def _alt(job: dict[str, Any]) -> str:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()  # noqa: E731
    return f"tc:{norm(job.get('title'))}|{norm(job.get('company'))}"


def _load() -> dict[str, str]:
    try:
        return json.loads(_path().read_text(encoding="utf-8")) if _path().exists() else {}
    except json.JSONDecodeError:
        return {}


def visited_at(job: dict[str, Any], store: dict[str, str] | None = None) -> str | None:
    store = _load() if store is None else store
    return store.get(uid(job)) or store.get(_alt(job))


def load_all() -> dict[str, str]:
    return _load()


def mark(jobs: Iterable[dict[str, Any]], visited: bool) -> int:
    with _lock:
        store = _load()
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        for job in jobs:
            for key in (uid(job), _alt(job)):
                if visited:
                    store.setdefault(key, now)
                else:
                    store.pop(key, None)
            count += 1
        _path().write_text(json.dumps(store, indent=1), encoding="utf-8")
        return count
