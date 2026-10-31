"""Accounts for several people on this computer.

- Each user has a folder users/<id>/ with ALL their data: profile, resume, jobs, AI verdicts,
  applications, visited list, messages sent, token usage. Nothing is shared between users except
  public company-contact search results.
- Passwords are stored only as salted PBKDF2 hashes.
- API keys (Apify, Groq, Gmail, Serper) are encrypted with a key derived from the user's password, so
  they can only be used while that user is logged in (they are kept in memory for the session).
- Logins survive app restarts: users/sessions.json keeps each session under a hash of its token, with
  the key-unlock secret encrypted by the token itself. Only the browser's cookie (or the extension's
  token) can unlock it, so the file alone can't decrypt anyone's API keys.
- The first account created takes over the data that existed before accounts (the owner's files and
  .env keys). Other users never fall back to .env.
- Sign in with Google (invite-only): only emails on the owner's allowlist can create an account.
  Google users have no password, so their key-encryption key is wrapped with the server secret
  (FP_SECRET_KEY in .env, or users/server.key, made on first use). A password account can link
  Google in Settings and then use either.
- Consent to the terms / privacy policy (TERMS_VERSION) is recorded per account.

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
SESSION_DAYS = 30  # renewed whenever the app is opened (see touch())
SESSIONS_FILE = USERS_DIR / "sessions.json"
TERMS_VERSION = "2026-10-31"  # bump when terms.html / privacy.html change materially
ALLOWLIST = USERS_DIR / "allowlist.json"
# GROQ_API_KEY / NVIDIA_API_KEY may hold several keys (comma-separated), tried in order.
KEY_NAMES = ["APIFY_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "SERPER_API_KEY"]

# Files that belonged to the single-user app; moved into the first account.
LEGACY_FILES = ["jobs_cache.json", "llm_verdicts.json", "embeddings_cache.npz", "applicant_profile.json",
                "applykit_cache.json", "applications.json", "visited.json", "ai_usage.jsonl", "autoapply_log.jsonl",
                "outreach_log.json", "outreach_settings.json"]

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("jf_user", default=None)
_lock = threading.Lock()
_master_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}     # token -> {"uid", "expires", "fkey", "ext"?}
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
    if not u:
        return None
    return {"id": uid, "username": u["username"], "owner": bool(u.get("owner")), "created": u.get("created"),
            "email": u.get("email") or "", "google": bool(u.get("google_sub")), "has_password": bool(u.get("hash")),
            "consent": u.get("consent")}


def list_users() -> list[dict[str, Any]]:
    return [get(uid) for uid in _registry()]


def list_usernames() -> list[str]:
    return [u["username"] for u in _registry().values()]


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 300_000).hex()


def _fernet_key(password: str, salt: bytes) -> str:
    """The key that encrypts a user's API keys (derived from the password; never stored in clear)."""
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b"jf-keys" + salt, 300_000)
    return base64.urlsafe_b64encode(key).decode()


def _fernet(password: str, salt: bytes):
    from cryptography.fernet import Fernet

    return Fernet(_fernet_key(password, salt).encode())


# ---------------------------------------------------------------- saved sessions (survive restarts)

def _token_id(token: str) -> str:
    return hashlib.sha256(b"jf-session" + token.encode()).hexdigest()


def _token_fernet(token: str):
    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"jf-wrap" + token.encode()).digest()))


def _read_sessions() -> dict[str, Any]:
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8")) if SESSIONS_FILE.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _persist(token: str | None = None, drop: list[str] | None = None) -> None:
    """Save one session (and forget dropped / expired ones) in users/sessions.json."""
    with _lock:
        data = {k: v for k, v in _read_sessions().items() if v.get("expires", 0) > time.time()}
        for t in drop or []:
            data.pop(_token_id(t), None)
        s = _sessions.get(token or "")
        if token and s:
            f = _token_fernet(token)
            rec = {"uid": s["uid"], "expires": s["expires"], "fkey": f.encrypt(s["fkey"].encode()).decode()}
            if s.get("ext"):
                rec["ext"] = f.encrypt(s["ext"].encode()).decode()
            data[_token_id(token)] = rec
        USERS_DIR.mkdir(exist_ok=True)
        tmp = SESSIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(SESSIONS_FILE)


def _restore(token: str) -> dict[str, Any] | None:
    """A session saved before the app restarted: unlock it with the token and load the user's keys."""
    rec = _read_sessions().get(_token_id(token))
    if not rec or rec.get("expires", 0) < time.time() or rec.get("uid") not in _registry():
        return None
    try:
        f = _token_fernet(token)
        s = {"uid": rec["uid"], "expires": rec["expires"], "fkey": f.decrypt(rec["fkey"].encode()).decode()}
        if rec.get("ext"):
            s["ext"] = f.decrypt(rec["ext"].encode()).decode()
    except Exception:  # noqa: BLE001 — wrong token / damaged record
        return None
    _sessions[token] = s
    if s["uid"] not in _keys:
        _unlock_keys(s["uid"], s["fkey"])
    return s


def _unlock_keys(uid: str, fkey: str) -> None:
    from cryptography.fernet import Fernet

    enc = user_dir(uid) / "keys.enc"
    try:
        _keys[uid] = json.loads(Fernet(fkey.encode()).decrypt(enc.read_bytes())) if enc.exists() else {}
    except Exception:  # noqa: BLE001 — unreadable key file: start empty rather than lock the user out
        _keys[uid] = {}


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

def register(username: str, password: str, consent: bool = False) -> dict[str, Any]:
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
        if consent:
            reg[uid]["consent"] = {"version": TERMS_VERSION, "at": time.time()}
        _save_registry(reg)
    d = user_dir(uid)
    if first:
        _adopt_legacy(d)
    _write_keys(uid, _fernet_key(password, salt), _legacy_env_keys() if first else {})
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


def _write_keys(uid: str, fkey: str, keys: dict[str, str]) -> None:
    from cryptography.fernet import Fernet

    token = Fernet(fkey.encode()).encrypt(json.dumps(keys).encode("utf-8"))
    (user_dir(uid) / "keys.enc").write_bytes(token)
    _keys[uid] = dict(keys)


def login(username: str, password: str) -> str:
    reg = _registry()
    uid = next((i for i, u in reg.items() if u["username"].lower() == username.strip().lower()), None)
    if (not uid or not reg[uid].get("hash")
            or not secrets.compare_digest(_hash(password, bytes.fromhex(reg[uid]["salt"])), reg[uid]["hash"])):
        time.sleep(0.5)  # slows down password guessing
        raise ValueError("Wrong username or password." if not uid or reg[uid].get("hash")
                         else "This account signs in with Google.")
    return _new_session(uid, _fernet_key(password, bytes.fromhex(reg[uid]["salt"])))


def _new_session(uid: str, fkey: str) -> str:
    _unlock_keys(uid, fkey)
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"uid": uid, "expires": time.time() + SESSION_DAYS * 86400, "fkey": fkey}
    _persist(token)
    return token


# ---------------------------------------------------------------- Google sign-in (invite-only)

class ConsentRequired(ValueError):
    """A new account must accept the terms and privacy policy first."""


def _master():
    """The server secret that wraps Google users' key-encryption keys."""
    from cryptography.fernet import Fernet

    k = (os.getenv("FP_SECRET_KEY") or "").strip()
    if not k:
        f = USERS_DIR / "server.key"
        with _master_lock:  # its own lock: callers may already hold _lock
            if not f.exists():
                USERS_DIR.mkdir(exist_ok=True)
                f.write_text(Fernet.generate_key().decode(), encoding="utf-8")
        k = f.read_text(encoding="utf-8").strip()
    return Fernet(k.encode())


def _emails(text: str) -> set[str]:
    return {e for e in re.split(r"[,;\s]+", (text or "").lower()) if "@" in e}


def allowed_emails() -> list[str]:
    """Invited emails: the owner's list (Settings) + FP_ALLOWED_EMAILS / FP_OWNER_EMAIL in .env."""
    try:
        saved = json.loads(ALLOWLIST.read_text(encoding="utf-8")) if ALLOWLIST.exists() else []
    except (json.JSONDecodeError, OSError):
        saved = []
    env = _emails(os.getenv("FP_ALLOWED_EMAILS", "")) | _emails(os.getenv("FP_OWNER_EMAIL", ""))
    return sorted(env | {str(e).strip().lower() for e in saved if "@" in str(e)})


def save_allowlist(emails: list[str]) -> list[str]:
    USERS_DIR.mkdir(exist_ok=True)
    clean = sorted({str(e).strip().lower() for e in emails if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(e).strip())})
    ALLOWLIST.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    return allowed_emails()


def _unique_username(reg: dict[str, Any], email: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]", "", email.split("@")[0])[:24] or "user"
    base = base if len(base) >= 3 else base + "user"
    taken = {u["username"].lower() for u in reg.values()}
    name, n = base, 1
    while name.lower() in taken:
        n += 1
        name = f"{base}{n}"
    return name


def google_login(info: dict[str, Any], consent: bool) -> tuple[str, bool]:
    """Sign in (or, for an invited email, create an account) with a verified Google ID token's
    claims. Returns (session token, created)."""
    from cryptography.fernet import Fernet

    sub, email = str(info.get("sub") or ""), str(info.get("email") or "").strip().lower()
    if not sub or not email or not info.get("email_verified"):
        raise ValueError("Your Google account's email isn't verified.")
    created = first = False
    with _lock:
        reg = _registry()
        uid = next((i for i, u in reg.items() if u.get("google_sub") == sub), None)
        if uid is None:
            if any((u.get("email") or "").lower() == email for u in reg.values()):
                raise ValueError("This email belongs to an account that isn't linked to Google yet. "
                                 "Sign in with your password and link Google in Settings.")
            owner_email = _emails(os.getenv("FP_OWNER_EMAIL", ""))
            first = not reg
            if first and owner_email and email not in owner_email:
                raise ValueError("This app isn't open for sign-ups.")
            if not first and email not in allowed_emails():
                raise ValueError("This app is invite-only. Ask the owner to add your email.")
            if not consent:
                raise ConsentRequired("Please accept the Terms and Privacy Policy to create your account.")
            uid, fkey = secrets.token_hex(6), Fernet.generate_key().decode()
            reg[uid] = {"username": _unique_username(reg, email), "email": email, "google_sub": sub,
                        "gwrap": _master().encrypt(fkey.encode()).decode(), "created": time.time(), "owner": first,
                        "consent": {"version": TERMS_VERSION, "at": time.time()}}
            _save_registry(reg)
            created = True
        else:
            fkey = _master().decrypt(reg[uid]["gwrap"].encode()).decode()
    if created:
        d = user_dir(uid)
        if first:
            _adopt_legacy(d)
        _write_keys(uid, fkey, _legacy_env_keys() if first else {})
    return _new_session(uid, fkey), created


def link_google(token: str, info: dict[str, Any]) -> dict[str, Any]:
    """Let a password account also sign in with this Google account."""
    s = _sessions.get(token or "")
    if not s:
        raise ValueError("Not logged in.")
    sub, email = str(info.get("sub") or ""), str(info.get("email") or "").strip().lower()
    if not sub or not info.get("email_verified"):
        raise ValueError("Your Google account's email isn't verified.")
    with _lock:
        reg = _registry()
        other = next((i for i, u in reg.items() if u.get("google_sub") == sub and i != s["uid"]), None)
        if other:
            raise ValueError("That Google account is already used by another account.")
        reg[s["uid"]].update(google_sub=sub, email=email, gwrap=_master().encrypt(s["fkey"].encode()).decode())
        _save_registry(reg)
    return get(s["uid"])


def record_consent(uid: str) -> None:
    with _lock:
        reg = _registry()
        if uid in reg:
            reg[uid]["consent"] = {"version": TERMS_VERSION, "at": time.time()}
            _save_registry(reg)


# ---------------------------------------------------------------- local mode (no sign-in on this computer)
#
# FP_MODE=local (the default): opening the app on this computer signs in as the owner automatically.
# The owner's key-unlock secret is kept wrapped with the server secret ("lwrap"), saved the first time
# the owner is signed in locally (a password sign-in, or a session that is still valid).

def local_mode() -> bool:
    return os.getenv("FP_MODE", "local").strip().lower() != "cloud"


_local_token: list[str] = []


def remember_local(token: str | None) -> None:
    """Keep the owner's unlock secret for automatic local sign-in (owner sessions only)."""
    s = _sessions.get(token or "")
    if not s:
        return
    with _lock:
        reg = _registry()
        u = reg.get(s["uid"])
        if not u or not u.get("owner"):
            return
        wrapped = u.get("lwrap")
        try:
            if wrapped and _master().decrypt(wrapped.encode()).decode() == s["fkey"]:
                return
        except Exception:  # noqa: BLE001 — stale / unreadable: replace it
            pass
        u["lwrap"] = _master().encrypt(s["fkey"].encode()).decode()
        _save_registry(reg)


def local_token() -> str | None:
    """A session for the owner without a sign-in (None until the owner has signed in here once)."""
    if _local_token and session_user(_local_token[0]):
        return _local_token[0]
    uid = owner_id()
    u = _registry().get(uid or "") or {}
    wrapped = u.get("lwrap") or u.get("gwrap")
    if not wrapped:
        return None
    try:
        fkey = _master().decrypt(wrapped.encode()).decode()
    except Exception:  # noqa: BLE001 — server secret changed: ask for the password once more
        return None
    token = _new_session(uid, fkey)
    _local_token[:] = [token]
    return token


# ---------------------------------------------------------------- data rights (export / delete)

def export_zip(uid: str) -> bytes:
    """Everything stored for a user, as a zip (API keys left out: they are secrets, shown in Settings)."""
    import io
    import zipfile

    buf = io.BytesIO()
    d = user_dir(uid)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("account.json", json.dumps(get(uid), indent=1))
        for f in d.rglob("*"):
            if f.is_file() and f.name != "keys.enc":
                z.write(f, f.relative_to(d).as_posix())
    return buf.getvalue()


def delete_account(uid: str) -> None:
    """Remove an account and all its data for good (the owner's account can't be deleted)."""
    with _lock:
        reg = _registry()
        if uid not in reg:
            raise ValueError("No such account.")
        if reg[uid].get("owner"):
            raise ValueError("The owner's account can't be deleted.")
        reg.pop(uid)
        _save_registry(reg)
    for t in [t for t, v in _sessions.items() if v["uid"] == uid]:
        _sessions.pop(t, None)
    _keys.pop(uid, None)
    _drop_user_records(uid)
    shutil.rmtree(USERS_DIR / uid, ignore_errors=True)


def session_user(token: str | None) -> str | None:
    if not token:
        return None
    s = _sessions.get(token) or _restore(token)
    if not s or s["expires"] < time.time():
        _sessions.pop(token, None)
        return None
    return s["uid"]


def touch(token: str | None) -> bool:
    """Renew a session that is past half its life (the app calls this on every page open)."""
    s = _sessions.get(token or "")
    if not s or s["expires"] - time.time() > SESSION_DAYS * 86400 / 2:
        return False
    s["expires"] = time.time() + SESSION_DAYS * 86400
    ext = _sessions.get(s.get("ext") or "")
    if ext:
        ext["expires"] = s["expires"]
        _persist(s["ext"])
    _persist(token)
    return True


def logout(token: str | None) -> None:
    s = _sessions.pop(token or "", None) or (_restore(token) and _sessions.pop(token or "", None))
    drop = [token] if token else []
    if s and s.get("ext"):
        _sessions.pop(s["ext"], None)
        drop.append(s["ext"])
    _persist(drop=drop)
    if s and not any(v["uid"] == s["uid"] for v in _sessions.values()):
        _keys.pop(s["uid"], None)


def change_password(token: str, old: str, new: str) -> None:
    s = _sessions.get(token or "")
    if not s:
        raise ValueError("Not logged in.")
    uid = s["uid"]
    reg = _registry()
    if not reg[uid].get("hash"):
        raise ValueError("This account signs in with Google and has no password.")
    salt = bytes.fromhex(reg[uid]["salt"])
    if not reg[uid].get("hash") or not secrets.compare_digest(_hash(old, salt), reg[uid]["hash"]):
        raise ValueError("Current password is wrong.")
    if len(new) < 8:
        raise ValueError("Password: at least 8 characters.")
    keys = dict(_keys.get(uid) or {})
    reg[uid]["hash"] = _hash(new, salt)
    fkey = _fernet_key(new, salt)
    if reg[uid].get("gwrap"):  # Google sign-in unlocks the same keys
        reg[uid]["gwrap"] = _master().encrypt(fkey.encode()).decode()
    if reg[uid].get("lwrap"):  # so does automatic local sign-in
        reg[uid]["lwrap"] = _master().encrypt(fkey.encode()).decode()
    _save_registry(reg)
    _write_keys(uid, fkey, keys)
    # Other logins of this user (other browsers) hold the old key: sign them out.
    others = [t for t, v in _sessions.items() if v["uid"] == uid and t not in (token, s.get("ext"))]
    for t in others:
        _sessions.pop(t, None)
    s["fkey"] = fkey
    if s.get("ext") in _sessions:
        _sessions[s["ext"]]["fkey"] = fkey
    _drop_user_records(uid)
    _persist(token)
    if s.get("ext"):
        _persist(s["ext"])


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
    _write_keys(uid, s["fkey"], keys)
    return keys_status()


def _drop_user_records(uid: str) -> None:
    """Forget every saved session of a user (after a password change)."""
    with _lock:
        data = {k: v for k, v in _read_sessions().items() if v.get("uid") != uid}
        SESSIONS_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")


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
    if ext and ext not in _sessions:
        _restore(ext)
    if not ext or ext not in _sessions:
        ext = secrets.token_urlsafe(32)
        _sessions[ext] = {"uid": s["uid"], "expires": s["expires"], "fkey": s["fkey"]}
        s["ext"] = ext
        _persist(ext)
        _persist(token)
    return ext
