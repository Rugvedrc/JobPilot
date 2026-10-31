"""Reset a forgotten password: python -m scripts.reset_password

Saved API keys are encrypted with the old password, so they can't be kept. The owner's keys are
refilled from .env; other users re-enter theirs in Settings after signing in.
"""
import getpass

from fresherpilot.core import users


def main() -> None:
    reg = users._registry()
    if not reg:
        print("No accounts yet: open http://localhost:8000 and create one.")
        return
    print("Accounts:", ", ".join(u["username"] + (" (owner)" if u.get("owner") else "") for u in reg.values()))
    name = input("Username: ").strip().lower()
    uid = next((i for i, u in reg.items() if u["username"].lower() == name), None)
    if not uid:
        print("No such account.")
        return
    pw = getpass.getpass("New password (8+ characters, not shown): ")
    if len(pw) < 8 or pw != getpass.getpass("Repeat it: "):
        print("Too short or didn't match. Nothing changed.")
        return
    reg[uid]["hash"] = users._hash(pw, bytes.fromhex(reg[uid]["salt"]))
    users._save_registry(reg)
    keys = users._legacy_env_keys() if reg[uid].get("owner") else {}
    users._write_keys(uid, users._fernet_key(pw, bytes.fromhex(reg[uid]["salt"])), keys)
    users._drop_user_records(uid)  # old logins hold the old key: sign them out
    print(f"Password changed. Sign in at http://localhost:8000 as {reg[uid]['username']}.")
    print(f"API keys restored from .env: {', '.join(keys) or 'none'} (add others in Settings).")


if __name__ == "__main__":
    main()
