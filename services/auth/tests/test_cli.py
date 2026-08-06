import pytest
from sqlalchemy import select

from auth.cli import create_admin
from auth.models import AdminUser
from auth.passwords import verify_password


async def test_creating_an_admin_stores_a_hashed_password(session):
    await create_admin(session, email="a@x.com", password="pw", role="admin")

    user = (await session.execute(select(AdminUser))).scalars().one()

    assert user.email == "a@x.com"
    assert user.role == "admin"
    assert verify_password("pw", user.password_hash)
    assert user.password_hash != "pw"


async def test_a_duplicate_email_is_refused_with_a_clear_message(session):
    await create_admin(session, email="a@x.com", password="pw", role="admin")

    with pytest.raises(ValueError, match="already exists"):
        await create_admin(session, email="a@x.com", password="pw2", role="viewer")


async def test_a_duplicate_differing_only_in_case_is_refused(session):
    await create_admin(session, email="Gautam@x.com", password="pw", role="admin")

    with pytest.raises(ValueError, match="already exists"):
        await create_admin(session, email="gautam@x.com", password="pw", role="admin")


async def test_an_unknown_role_is_refused(session):
    """A typo would otherwise create an account that satisfies no principal at all and
    fails only at the first request."""
    with pytest.raises(ValueError, match="role"):
        await create_admin(session, email="a@x.com", password="pw", role="superuser")


async def test_an_empty_password_is_refused(session):
    with pytest.raises(ValueError, match="password"):
        await create_admin(session, email="a@x.com", password="", role="admin")
