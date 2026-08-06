"""Password hashing.

argon2id rather than bcrypt: it is memory-hard, so an attacker with GPUs gains far less
than they would against a purely CPU-bound function. The parameters are the library's
defaults, which track current guidance -- pinning our own numbers here would mean they
silently rot as hardware improves.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str | None) -> bool:
    """Whether the password matches, false for anything unusable.

    A malformed or absent hash returns False rather than raising: a corrupted row must
    fail one login, not take the endpoint down. An account with no password set can never
    authenticate, which is what makes a future SSO-only account safe to add.
    """
    if not hashed:
        return False

    try:
        return _hasher.verify(hashed, plain)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    """Whether this hash was made with weaker parameters than the current defaults.

    Called after a successful login so a password migrates to stronger settings the next
    time its owner signs in, without anyone having to reset anything.
    """
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return True
