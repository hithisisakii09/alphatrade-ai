"""Cryptographic vault for encrypting sensitive multi-tenant broker credentials at rest."""

import base64
import hashlib
import os
from cryptography.fernet import Fernet, InvalidToken


def _get_master_key() -> bytes:
  """Derives a deterministic 32-byte Fernet key from environment or creates fallback."""
  raw_key = os.environ.get("ALPHATRADE_SECRET_KEY") or os.environ.get("SECRET_KEY")
  if not raw_key:
    # Use deterministic fallback derived from machine/env context if secret key not set
    raw_key = "ALPHATRADE_AI_V2_DEFAULT_ENCRYPTION_MASTER_SEED_DO_NOT_EXPOSE"
  # Use SHA256 to ensure exact 32 bytes required for Fernet base64 urlsafe encoding
  digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
  return base64.urlsafe_b64encode(digest)


class SecretVault:
  """Encrypts and decrypts sensitive broker credentials (API keys, TOTP secrets, passwords)."""

  def __init__(self, key: bytes | None = None):
    self._key = key or _get_master_key()
    self._fernet = Fernet(self._key)

  def encrypt(self, plaintext: str) -> str:
    """Encrypts plaintext string into an encrypted base64 token string."""
    if not plaintext:
      return ""
    token_bytes = self._fernet.encrypt(plaintext.encode("utf-8"))
    return token_bytes.decode("utf-8")

  def decrypt(self, ciphertext: str) -> str:
    """Decrypts ciphertext token string back to plaintext string."""
    if not ciphertext:
      return ""
    try:
      plain_bytes = self._fernet.decrypt(ciphertext.encode("utf-8"))
      return plain_bytes.decode("utf-8")
    except InvalidToken:
      raise ValueError("Invalid ciphertext or encryption key mismatch.")


# Global singleton instance
vault = SecretVault()


def encrypt_secret(plaintext: str) -> str:
  """Helper to encrypt a single sensitive string."""
  return vault.encrypt(plaintext)


def decrypt_secret(ciphertext: str) -> str:
  """Helper to decrypt a single sensitive string."""
  return vault.decrypt(ciphertext)
