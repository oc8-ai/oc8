"""Local password authentication utilities for Community single-instance deployments.

This module provides secure password hashing and verification using Argon2id,
the OWASP-recommended key derivation function. Community ships exactly one
identity provider -- this one -- so every member authenticates directly via
password.

Password hashes are stored in the OrgMember model and validated on login.
Never log or expose plaintext passwords, hashes, or tokens. All operations
are fail-closed on any error.
"""

from __future__ import annotations

import hashlib

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# Shared instance with OWASP-recommended parameters
# (argon2-cffi uses secure defaults: memory=65540, time cost=3, parallelism=4)
_hasher = PasswordHasher()


class PasswordHashingError(Exception):
    """Base exception for password hashing failures."""

    pass


class InvalidPasswordHash(PasswordHashingError):
    """Raised when a stored hash is malformed or corrupted."""

    pass


class PasswordVerificationFailed(PasswordHashingError):
    """Raised when password verification fails (wrong password, rehash needed, etc.)."""

    pass


def hash_password(plaintext: str) -> str:
    """Hash a plaintext password using Argon2id (OWASP standard).

    Args:
        plaintext: the password to hash (should be validated for length/complexity by caller).

    Returns:
        An Argon2id hash string safe for storage in OrgMember.password_hash.

    Raises:
        PasswordHashingError: if hashing fails (memory pressure, etc.).

    Note:
        - Never log the plaintext or the returned hash.
        - OWASP parameters are hardcoded; do not pass time cost, memory, or parallelism
          to the caller to avoid misconfigurations that weaken security.
    """
    try:
        return _hasher.hash(plaintext)
    except Exception as exc:
        # Catch all exceptions from Argon2: convert to our type to prevent
        # leaking implementation details up the stack.
        raise PasswordHashingError("Failed to hash password") from exc


def verify_password(plaintext: str, stored_hash: str) -> bool:
    """Verify a plaintext password against a stored Argon2id hash.

    Args:
        plaintext: the plaintext password to check.
        stored_hash: the Argon2id hash from OrgMember.password_hash.

    Returns:
        True if the password matches; False otherwise (including on any hash error).

    Note:
        - Returns False (not an exception) on verification failure to prevent
          timing attacks and keep failed login handling uniform.
        - A corrupted hash returns False, not an exception, to avoid exposing
          database integrity errors to the API caller.
        - Never log the plaintext, hash, or verification result.
    """
    if not plaintext or not stored_hash:
        return False

    try:
        _hasher.verify(stored_hash, plaintext)
        return True
    except (VerificationError, InvalidHashError):
        # Wrong password (VerificationError) or corrupted hash (InvalidHashError).
        # Both mean "no match"; we return False rather than raising, so the
        # caller treats failed password like any other auth failure.
        return False
    except Exception:
        # Any other exception from Argon2 (e.g., memory pressure during verify).
        # Fail closed: return False to deny access rather than escalating.
        return False


def password_fingerprint(stored_hash: str) -> str:
    """A short digest of a stored password hash, carried in a session token.

    Renewing a session compares it with the member's current hash, so changing
    the password -- what a person does after a token leaked -- stops every
    session minted before the change from being renewed. A digest of the salted
    Argon2 hash, never of the password: it says nothing about the password.
    """
    return hashlib.sha256(stored_hash.encode()).hexdigest()[:32]
