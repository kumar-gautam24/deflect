"""Account creation.

There is no signup route, deliberately: this system has operators, not users, and an
endpoint that creates privileged accounts is a liability with no upside. Accounts are made
here, deliberately, by someone with database access.
"""

import argparse
import asyncio
import getpass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.db import SessionFactory
from auth.models import AdminUser
from auth.passwords import hash_password

ROLES = ("admin", "viewer")


async def create_admin(session: AsyncSession, email: str, password: str, role: str) -> AdminUser:
    if role not in ROLES:
        # Caught here rather than at the first request: an account with an unknown role
        # satisfies no principal and would fail in a way nothing explains.
        raise ValueError(f"role must be one of {', '.join(ROLES)}, got {role!r}")
    if not password:
        raise ValueError("password must not be empty")

    existing = (
        await session.execute(
            select(AdminUser).where(func.lower(AdminUser.email) == email.lower())
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"an account for {email} already exists")

    user = AdminUser(email=email, password_hash=hash_password(password), role=role)
    session.add(user)
    await session.flush()
    return user


async def _run(email: str, role: str) -> None:
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Confirm: "):
        raise SystemExit("passwords did not match")

    async with SessionFactory() as session:
        user = await create_admin(session, email, password, role)
        await session.commit()
        print(f"created {user.email} as {user.role}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="auth.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-admin", help="create an account")
    create.add_argument("--email", required=True)
    create.add_argument("--role", default="admin", choices=ROLES)

    args = parser.parse_args()
    # Prompted rather than taken as an argument: a password in argv is visible in the
    # process list and lands in shell history.
    asyncio.run(_run(args.email, args.role))


if __name__ == "__main__":
    main()
