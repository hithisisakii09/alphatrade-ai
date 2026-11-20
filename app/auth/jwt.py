"""JWT creation and decoding using PyJWT."""

from datetime import datetime, timezone, timedelta
import os
import jwt

_JWT_SECRET = os.environ.get("ALPHATRADE_JWT_SECRET") or os.environ.get("SECRET_KEY") or "ALPHATRADE_AI_V2_JWT_SECRET_KEY_CHANGE_IN_PROD"
_JWT_ALGORITHM = "HS256"
_DEFAULT_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
  """Encodes a dictionary into a signed JWT access token."""
  to_encode = data.copy()
  now = datetime.now(timezone.utc)
  expire = now + (expires_delta if expires_delta else timedelta(minutes=_DEFAULT_EXPIRE_MINUTES))
  to_encode.update({
      "exp": expire,
      "iat": now,
  })
  encoded_jwt = jwt.encode(to_encode, _JWT_SECRET, algorithm=_JWT_ALGORITHM)
  return encoded_jwt


def decode_access_token(token: str) -> dict:
  """Decodes and validates a signed JWT access token."""
  try:
    payload = jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
    return payload
  except jwt.ExpiredSignatureError:
    raise ValueError("Token has expired.")
  except jwt.PyJWTError as e:
    raise ValueError(f"Invalid token: {e}")
