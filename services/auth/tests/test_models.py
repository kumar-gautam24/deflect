import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from auth.models import AdminUser, Base


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


async def test_autogenerate_would_not_drop_either_index(session):
    """The models must describe every index the migration creates.

    An index that exists only in the migration looks, to `alembic revision --autogenerate`,
    like one somebody added by hand -- so the next generated migration drops it. For
    admin_users_email_lower_idx that is not a performance regression but a correctness one:
    it is the sole enforcement of the case-insensitive uniqueness the test above pins, and
    that test would then start failing with no visible cause.
    """

    def compare(connection):
        return compare_metadata(MigrationContext.configure(connection), Base.metadata)

    diffs = await (await session.connection()).run_sync(compare)

    index_changes = [diff for diff in diffs if "index" in str(diff[0])]
    assert index_changes == []
