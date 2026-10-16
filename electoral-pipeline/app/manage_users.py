"""Create and manage sign-in accounts from the server, without the web UI.

The /admin/users page needs a signed-in superadmin. If that password is lost,
or nobody has an account yet, this is the way back in:

    docker compose exec web python -m app.manage_users list
    docker compose exec web python -m app.manage_users create alice
    docker compose exec web python -m app.manage_users create ops --superadmin
    docker compose exec web python -m app.manage_users reset-password alice
    docker compose exec web python -m app.manage_users disable alice
    docker compose exec web python -m app.manage_users enable alice

A password is generated and printed once unless --password is given.
"""
from __future__ import annotations

import argparse
import re
import secrets
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AdminUser

USERNAME_RE = re.compile(r"[A-Za-z0-9._@-]{3,128}")
MIN_PASSWORD = 14


def _hash(password: str) -> str:
    # Same scheme the web app verifies against; imported lazily so this
    # module stays usable even while app.main is mid-refactor.
    from app.main import _hash_password
    return _hash_password(password)


def _password_or_generate(given: str | None) -> str:
    if given:
        if len(given) < MIN_PASSWORD:
            sys.exit(f"Password must be at least {MIN_PASSWORD} characters.")
        return given
    return secrets.token_urlsafe(18)


def cmd_list(_args) -> None:
    with SessionLocal() as db:
        users = db.scalars(select(AdminUser).order_by(AdminUser.username)).all()
    if not users:
        print("No accounts yet.")
        return
    for u in users:
        flags = ("superadmin " if u.is_superadmin else "") + ("" if u.is_active else "DISABLED")
        last = u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "never"
        print(f"{u.username:30} last login {last:17} {flags}")


def cmd_create(args) -> None:
    if not USERNAME_RE.fullmatch(args.username):
        sys.exit("Username must be 3-128 characters: letters, digits, . _ @ -")
    password = _password_or_generate(args.password)
    with SessionLocal() as db:
        if db.scalar(select(AdminUser.id).where(AdminUser.username == args.username)):
            sys.exit(f"User {args.username!r} already exists. Use reset-password instead.")
        db.add(AdminUser(username=args.username, password_hash=_hash(password), is_superadmin=args.superadmin))
        db.commit()
    print(f"Created {'superadmin' if args.superadmin else 'user'} {args.username}")
    print(f"Password: {password}")
    print("Store this now; it is not shown again.")


def _get_user(db, username: str) -> AdminUser:
    user = db.scalar(select(AdminUser).where(AdminUser.username == username))
    if not user:
        sys.exit(f"No user named {username!r}.")
    return user


def cmd_reset(args) -> None:
    password = _password_or_generate(args.password)
    with SessionLocal() as db:
        user = _get_user(db, args.username)
        user.password_hash = _hash(password)
        user.is_active = True
        db.commit()
    print(f"Password reset for {args.username}")
    print(f"Password: {password}")


def cmd_set_active(active: bool):
    def run(args) -> None:
        with SessionLocal() as db:
            user = _get_user(db, args.username)
            if not active and user.is_superadmin:
                others = db.scalar(
                    select(AdminUser.id).where(
                        AdminUser.is_superadmin.is_(True),
                        AdminUser.is_active.is_(True),
                        AdminUser.id != user.id,
                    )
                )
                if not others:
                    sys.exit("Refusing: this is the only active superadmin.")
            user.is_active = active
            db.commit()
        print(f"{'Enabled' if active else 'Disabled'} {args.username}")
    return run


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.manage_users", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Show all accounts").set_defaults(func=cmd_list)

    create = sub.add_parser("create", help="Create an account")
    create.add_argument("username")
    create.add_argument("--password", help="Use this password instead of generating one")
    create.add_argument("--superadmin", action="store_true", help="Can manage other accounts")
    create.set_defaults(func=cmd_create)

    reset = sub.add_parser("reset-password", help="Set a new password and re-enable the account")
    reset.add_argument("username")
    reset.add_argument("--password")
    reset.set_defaults(func=cmd_reset)

    disable = sub.add_parser("disable", help="Block sign-in for an account")
    disable.add_argument("username")
    disable.set_defaults(func=cmd_set_active(False))

    enable = sub.add_parser("enable", help="Allow sign-in again")
    enable.add_argument("username")
    enable.set_defaults(func=cmd_set_active(True))

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
