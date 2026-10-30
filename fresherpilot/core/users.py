"""Accounts for several people on this computer.

- Each user has a folder users/<id>/ with ALL their data: profile, resume, jobs, AI verdicts,
  applications, visited list, messages sent, token usage. Nothing is shared between users except
  public company-contact search results.
- Passwords are stored only as salted PBKDF2 hashes.
- API keys (Apify, Groq, Gmail, Serper) are encrypted with a key derived from the user's password, so
  they can only be used while that user is logged in (they are kept in memory for the session).
- The first account created takes over the data that existed before accounts (the owner's files and
  .env keys). Other users never fall back to .env.

Which user a piece of code works for comes from a context variable, set per web request and copied
into background threads (see run_as / thread). Command-line tools use JF_USER or the owner.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
USERS_DIR = ROOT / "users"
REGISTRY = USERS_DIR / "users.json"
SESSION_DAYS = 14
KEY_NAMES = ["APIFY_API_KEY", "GROQ_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "SERPER_API_KEY"]

# Files that belonged to the single-user app; moved into the first account.
LEGACY_FILES = ["jobs_cache.json", "llm_verdicts.json", "embeddings_cache.npz", "applicant_profile.json",
                "applykit_cache.json", "applications.json", "visited.json", "ai_usage.jsonl", "autoapply_log.jsonl",
                "outreach_log.json", "outreach_settings.json"]

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("jf_user", default=None)
_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}     # token -> {"uid", "expires"}
_keys: dict[str, dict[str, str]] = {}         # uid -> decrypted API keys (memory only)


# ---------------------------------------------------------------- registry

def _registry() -> dict[str, Any]:
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8")) if REGISTRY.exists() else {}
    except json.JSONDecodeError:
        return {}


def _save_registry(data: dict[str, Any]) -> None:
    USERS_DIR.mkdir(exist_ok=True)
    REGISTRY.write_text(json.dumps(data, indent=1), encoding="utf-8")


def owner_id() -> str | None:
    return next((uid for uid, u in _registry().items() if u.get("owner")), None)


def get(uid: str) -> dict[str, Any] | None:
    u = _registry().get(uid)
    return {"id": uid, "username": u["username"], "owner": bool(u.get("owner")), "created": u.get("created")} if u else None


def list_usernames() -> list[str]:
    return [u["username"] for u in _registry().values()]


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 300_000).hex()


def _fernet(password: str, salt: bytes):
    from cryptography.fernet import Fernet

    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b"jf-keys" + salt, 300_000)
    return Fernet(base64.urlsafe_b64encode(key))


# ---------------------------------------------------------------- current user + paths

def current() -> str | None:
    uid = _current.get()
    if uid:
        return uid
    env_user = os.getenv("JF_USER")
    if env_user:
        return next((i for i, u in _registry().items() if u["username"] == env_user), None)
    return owner_id()  # command-line tools (recheck.py, check_filter.py) work for the owner


def user_dir(uid: str | None = None) -> Path:
    uid = uid or current()
    if not uid:  # before any account exists: the old single-user layout
        return ROOT
    d = USERS_DIR / uid
    d.mkdir(parents=True, exist_ok=True)
    return d


def path(name: str) -> Path:
    """A data file of the current user."""
    return user_dir() / name


def set_current(uid: str | None) -> contextvars.Token:
    return _current.set(uid)


def thread(target: Callable[..., Any], *args: Any) -> threading.Thread:
    """A daemon thread that keeps working for the user who started it."""
    ctx = contextvars.copy_context()
    t = threading.Thread(target=lambda: ctx.run(target, *args), daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- accounts

def register(username: str, password: str) -> dict[str, Any]:
    username = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,30}", username):
        raise ValueError("Username: 3-30 letters, numbers, dot, dash or underscore.")
    if len(password) < 8:
        raise ValueError("Password: at least 8 characters.")
    with _lock:
        reg = _registry()
        if any(u["username"].lower() == username.lower() for u in reg.values()):
            raise ValueError("That username is taken.")
        uid = secrets.token_hex(6)
        salt = os.urandom(16)
        first = not reg
        reg[uid] = {"username": username, "salt": salt.hex(), "hash": _hash(password, salt), "created": time.time(),
                    "owner": first}
        _save_registry(reg)
    d = user_dir(uid)
    if first:
        _adopt_legacy(d)
    _write_keys(uid, password, _legacy_env_keys() if first else {})
    return get(uid)


def _adopt_legacy(d: Path) -> None:
    """The first account takes over the data of the single-user app."""
    for name in LEGACY_FILES:
        src = ROOT / name
        if src.exists() and not (d / name).exists():
            shutil.copy2(src, d / name)
            src.rename(src.with_name(src.name + ".moved-to-account"))
    profile = d / "applicant_profile.json"
    if profile.exists():  # copy the resume into the account too
        try:
            resume = json.loads(profile.read_text(encoding="utf-8")).get("resume_file")
            if resume and (ROOT / resume).exists() and not (d / resume).exists():
                shutil.copy2(ROOT / resume, d / resume)
        except (json.JSONDecodeError, OSError):
            pass


def _legacy_env_keys() -> dict[str, str]:
    from dotenv import dotenv_values

    env = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}
    return {k: str(v).strip().strip('"').strip("'") for k, v in env.items() if k in KEY_NAMES and v}


def _write_keys(uid: str, password: str, keys: dict[str, str]) -> None:
    salt = bytes.fromhex(_registry()[uid]["salt"])
    token = _fernet(password, salt).encrypt(json.dumps(keys).encode("utf-8"))
    (user_dir(uid) / "keys.enc").write_bytes(token)
    _keys[uid] = dict(keys)


def login(username: str, password: str) -> str:
    reg = _registry()
    uid = next((i for i, u in reg.items() if u["username"].lower() == username.strip().lower()), None)
    if not uid or not secrets.compare_digest(_hash(password, bytes.fromhex(reg[uid]["salt"])), reg[uid]["hash"]):
        time.sleep(0.5)  # slows down password guessing
        raise ValueError("Wrong username or password.")
    enc = user_dir(uid) / "keys.enc"
    try:
        _keys[uid] = json.loads(_fernet(password, bytes.fromhex(reg[uid]["salt"])).decrypt(enc.read_bytes())) if enc.exists() else {}
    except Exception:  # noqa: BLE001 — unreadable key file: start empty rather than lock the user out
        _keys[uid] = {}
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"uid": uid, "expires": time.time() + SESSION_DAYS * 86400, "password": password}
    return token


def session_user(token: str | None) -> str | None:
    s = _sessions.get(token or "")
    if not s or s["expires"] < time.time():
        _sessions.pop(token or "", None)
        return None
    return s["uid"]


def logout(token: str | None) -> None:
    s = _sessions.pop(token or "", None)
    if s and not any(v["uid"] == s["uid"] for v in _sessions.values()):
        _keys.pop(s["uid"], None)


def change_password(token: str, old: str, new: str) -> None:
    s = _sessions.get(token or "")
    if not s:
        raise ValueError("Not logged in.")
    uid = s["uid"]
    reg = _registry()
    salt = bytes.fromhex(reg[uid]["salt"])
    if not secrets.compare_digest(_hash(old, salt), reg[uid]["hash"]):
        raise ValueError("Current password is wrong.")
    if len(new) < 8:
        raise ValueError("Password: at least 8 characters.")
    keys = dict(_keys.get(uid) or {})
    reg[uid]["hash"] = _hash(new, salt)
    _save_registry(reg)
    _write_keys(uid, new, keys)
    s["password"] = new


# ---------------------------------------------------------------- API keys

def key(name: str) -> str:
    """An API key of the current user. Only the owner may fall back to .env (command-line tools)."""
    uid = current()
    if uid and uid in _keys:
        return (_keys[uid].get(name) or "").strip()
    if uid is None or uid == owner_id():
        return (os.getenv(name) or "").strip().strip('"').strip("'")
    return ""


def keys_status() -> dict[str, bool]:
    return {k: bool(key(k)) for k in KEY_NAMES}


def save_keys(token: str, changes: dict[str, str]) -> dict[str, bool]:
    s = _sessions.get(token or "")
    if not s:
        raise ValueError("Not logged in.")
    uid = s["uid"]
    keys = dict(_keys.get(uid) or {})
    for k, v in changes.items():
        if k in KEY_NAMES and v is not None:
            v = str(v).strip()
            if v:
                keys[k] = v
            else:
                keys.pop(k, None)
    _write_keys(uid, s["password"], keys)
    return keys_status()


# ---------------------------------------------------------------- search preferences (per user)

def prefs() -> dict[str, Any]:
    """What to search for and how to judge jobs, for the current user (made from the resume)."""
    from fresherpilot.core import search_config as cfg

    p = path("search_prefs.json")
    mtime = p.stat().st_mtime if p.exists() else 0.0
    hit = _prefs_cache.get(str(p))
    if hit and hit[0] == mtime:
        return hit[1]
    saved: dict[str, Any] = {}
    if p.exists():
        try:
            saved = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            saved = {}
    merged = {**cfg.DEFAULT_PREFS, **saved}
    _prefs_cache[str(p)] = (mtime, merged)
    return merged


_prefs_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def save_prefs(changes: dict[str, Any]) -> dict[str, Any]:
    from fresherpilot.core import search_config as cfg

    cur = prefs()
    cur.update({k: v for k, v in changes.items() if k in cfg.DEFAULT_PREFS})
    path("search_prefs.json").write_text(json.dumps(cur, indent=1, ensure_ascii=False), encoding="utf-8")
    return cur


def extension_token(token: str | None) -> str | None:
    """A separate token for the browser extension (sent as X-JF-Token), tied to this login session."""
    s = _sessions.get(token or "")
    if not s:
        return None
    ext = s.get("ext")
    if not ext or ext not in _sessions:
        ext = secrets.token_urlsafe(32)
        _sessions[ext] = {"uid": s["uid"], "expires": s["expires"], "password": s["password"]}
        s["ext"] = ext
    return ext
