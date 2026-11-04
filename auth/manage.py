"""Command line for what has to happen before anyone can sign in.

    python -m auth.manage create-owner <username> [--name "Display Name"]
    python -m auth.manage reset-password <username>
    python -m auth.manage list
    python -m auth.manage new-key            # a random device key or broker password

Run from roverhub-backend/ with its venv. Reads USERS_FILE like the server.
Passwords are prompted for, never taken as arguments (they would land in shell
history).
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys

from dotenv import load_dotenv

from .users import ALL_SITES, DEFAULT_USERS_FILE, PasswordTooShort, UserStore


def _prompt_password() -> str:
    first = getpass.getpass("New password: ")
    if first != getpass.getpass("Again: "):
        sys.exit("passwords did not match")
    return first


def main(argv=None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m auth.manage")
    sub = parser.add_subparsers(dest="cmd", required=True)
    owner = sub.add_parser("create-owner", help="add an organisation owner with access to every site")
    owner.add_argument("username")
    owner.add_argument("--name", default="")
    owner.add_argument("--site-role", default="supervisor",
                       choices=["viewer", "operator", "supervisor", "technician", "engineer"],
                       help="their role at every site (default: supervisor)")
    reset = sub.add_parser("reset-password", help="set a user's password and sign them out everywhere")
    reset.add_argument("username")
    sub.add_parser("list", help="list users")
    sub.add_parser("new-key", help="print a random key for DEVICE_KEYS or BROKER_SERVICE_PASSWORD")
    args = parser.parse_args(argv)

    if args.cmd == "new-key":
        print(secrets.token_urlsafe(32))
        return 0

    store = UserStore(os.environ.get("USERS_FILE", DEFAULT_USERS_FILE))
    try:
        if args.cmd == "create-owner":
            user = store.create(username=args.username, display_name=args.name or args.username,
                                password=_prompt_password(), org_role="owner",
                                site_roles={ALL_SITES: args.site_role}, audit_access=True)
            print(f"created owner {user.username} ({user.user_id})")
        elif args.cmd == "reset-password":
            user = store.by_username(args.username)
            if user is None:
                sys.exit(f"no user {args.username!r}")
            store.set_password(user.user_id, _prompt_password())
            print(f"password set for {user.username}; their existing sessions are signed out")
        elif args.cmd == "list":
            for u in store.list():
                roles = ", ".join(f"{s}={r}" for s, r in u.site_roles.items()) or "no site access"
                print(f"{u.username:20} {u.org_role:7} {'disabled ' if u.disabled else ''}{roles}")
    except (ValueError, PasswordTooShort) as exc:
        sys.exit(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
