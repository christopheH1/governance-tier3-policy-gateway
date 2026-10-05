"""Console accounts: one per person, so the audit ledger can name who asked for what.

Each account has a name, a salted scrypt hash of its password, and roles:
  requester   gives agents tasks and replies to them
  reviewer    approves or rejects calls the policy gateway has held
  operator    enrols agents

The accounts live in one JSON file on the console's own volume. Passwords are
generated here and shown once. Nothing else stores them.

  python users.py add NAME [--roles requester,reviewer,operator]
  python users.py reset NAME
  python users.py remove NAME
  python users.py list
"""
import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path

USERS_FILE = Path(os.environ.get("CONSOLE_USERS_FILE", "/keys/users.json"))
ROLES = ("requester", "reviewer", "operator")


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


def load() -> dict:
    try:
        return json.loads(USERS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save(users: dict) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = USERS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(users, indent=2))
    temporary.chmod(0o600)
    temporary.replace(USERS_FILE)


def valid_name(name: str) -> bool:
    return 1 <= len(name) <= 64 and all(c.isalnum() or c in "._-" for c in name)


def set_password(users: dict, name: str, roles: list) -> str:
    password = secrets.token_urlsafe(12)
    salt = secrets.token_bytes(16)
    users[name] = {"salt": salt.hex(), "hash": _hash(password, salt), "roles": roles}
    return password


def check(users: dict, name: str, password: str):
    """Returns the account's roles if the name and password match, else None."""
    account = users.get(name)
    if not account:
        _hash(password, b"0" * 16)          # spend the same time whether or not the name exists
        return None
    if hmac.compare_digest(_hash(password, bytes.fromhex(account["salt"])), account["hash"]):
        return list(account.get("roles", []))
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("add", "reset", "remove", "list"))
    parser.add_argument("name", nargs="?")
    parser.add_argument("--roles", default=",".join(ROLES))
    args = parser.parse_args()
    users = load()
    if args.action == "list":
        for name, account in sorted(users.items()):
            print(f"{name:<24} {', '.join(account.get('roles', []))}")
        print(f"{len(users)} account(s)")
        return 0
    if not args.name or not valid_name(args.name):
        print("give a name of letters, digits, dot, dash or underscore", file=sys.stderr)
        return 2
    if args.action == "remove":
        if users.pop(args.name, None) is None:
            print("no such account", file=sys.stderr)
            return 1
        save(users)
        print(f"removed {args.name}")
        return 0
    if args.action == "add" and args.name in users:
        print("that account exists already: use reset to give it a new password", file=sys.stderr)
        return 1
    if args.action == "reset" and args.name not in users:
        print("no such account", file=sys.stderr)
        return 1
    roles = users[args.name]["roles"] if args.action == "reset" else [r.strip() for r in args.roles.split(",") if r.strip()]
    if not roles or any(role not in ROLES for role in roles):
        print(f"roles must be chosen from: {', '.join(ROLES)}", file=sys.stderr)
        return 2
    password = set_password(users, args.name, roles)
    save(users)
    print(f"account:  {args.name}")
    print(f"roles:    {', '.join(roles)}")
    print(f"password: {password}")
    print("This password is shown once and is not stored anywhere. Note it now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
