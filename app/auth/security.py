"""Password hashing, verification, and security utilities."""

import hashlib
import hmac
import os
import secrets

_ITERATIONS = 600_000
_ALGORITHM = "sha256"


def hash_password(password: str) -> str:
  """Hashes a password using PBKDF2-HMAC-SHA256 with a random 16-byte salt (NIST recommended)."""
  if not password:
    raise ValueError("Password cannot be empty.")
  salt = secrets.token_hex(16)
  derived = hashlib.pbkdf2_hmac(
      _ALGORITHM,
      password.encode("utf-8"),
      salt.encode("utf-8"),
      _ITERATIONS,
  )
  return f"{salt}${derived.hex()}"


def verify_password(password: str, hashed_password: str) -> bool:
  """Verifies a plain password against the stored salt$hash string using constant-time comparison."""
  if not password or not hashed_password or "$" not in hashed_password:
    return False
  try:
    salt, expected_hash_hex = hashed_password.split("$", 1)
    computed = hashlib.pbkdf2_hmac(
        _ALGORITHM,
        password.encode("utf-8"),
        salt.encode("utf-8"),
        _ITERATIONS,
    )
    return hmac.compare_digest(computed.hex(), expected_hash_hex)
  except Exception:
    return False
