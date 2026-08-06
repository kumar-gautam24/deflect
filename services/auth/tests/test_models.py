import pytest
from sqlalchemy.exc import IntegrityError

from auth.models import AdminUser


def _user(email: str = "a@x.com", role: str = "admin") -> AdminUser:
    return AdminUser(email=email, password_hash="$argon2id$fake", role=role)


async def test_a_user_starts_unlocked_with_no_failures(session):
    user = _user()
    session.add(user)
    await session.flush()

    assert user.failed_login_count == 0
    assert user.locked_until is None


async def test_two_accounts_cannot_share_an_email(session):
    session.add(_user("a@x.com"))
    await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(_user("a@x.com"))


async def test_emails_differing_only_in_case_cannot_both_exist(session):
    """Otherwise one of the two can never log in reliably, and which one wins depends on
    how the lookup happens to be written."""
    session.add(_user("Gautam@x.com"))
    await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(_user("gautam@x.com"))
