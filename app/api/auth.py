"""Multi-tenant authentication, registration, JWT authorization, and broker credential vault endpoints."""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.auth.jwt import create_access_token, decode_access_token
from app.auth.security import hash_password, verify_password
from app.core.config import load_app_config
from app.core.crypto import encrypt_secret, decrypt_secret
from app.core.database import (
    UserRecord,
    UserBrokerCredentialRecord,
    init_db,
    _db_lock,
)

router = APIRouter(prefix="/auth", tags=["Multi-Tenant Authentication"])
_cfg = load_app_config()
_engine, _SessionLocal = init_db(_cfg.database_url)
_security_bearer = HTTPBearer(auto_error=False)


# --- Pydantic Schemas ---

class UserRegisterRequest(BaseModel):
  email: str = Field(..., min_length=5, max_length=255, description="Valid email address")
  password: str = Field(..., min_length=8, description="Password (at least 8 characters)")
  full_name: str = Field("", max_length=128)
  role: str = Field("TRADER", pattern="^(TRADER|ADMIN)$")


class UserLoginRequest(BaseModel):
  email: str
  password: str


class TokenResponse(BaseModel):
  access_token: str
  token_type: str = "bearer"
  expires_in_seconds: int
  user_id: str
  email: str
  role: str


class UserProfileResponse(BaseModel):
  user_id: str
  email: str
  full_name: str
  role: str
  is_active: bool
  has_broker_connected: bool
  created_at_utc: str


class BrokerCredentialCreateRequest(BaseModel):
  broker_name: str = Field("ANGEL_ONE", description="Broker identifier (e.g. ANGEL_ONE)")
  client_code: str = Field(..., description="Broker User ID / Client Code (e.g. S54949978)")
  api_key: str = Field(..., description="SmartAPI Key")
  totp_secret: str = Field(..., description="2FA TOTP Base32 Secret Key")
  password: str = Field(..., description="Trading / PIN / Password")
  risk_profile: str = Field("ASYMMETRIC_25PCT_MOMENTUM", description="Risk & Allocation Profile")
  reference_capital: Decimal = Field(Decimal("5000.00"), description="Target trading capital in INR")
  is_live_authorized: bool = Field(False, description="Authorize live real execution")
  fno_enabled: bool = Field(True, description="Enable F&O derivative contracts")


class BrokerCredentialResponse(BaseModel):
  credential_id: str
  broker_name: str
  client_code: str
  api_key_masked: str
  totp_secret_configured: bool
  risk_profile: str
  reference_capital: str
  is_live_authorized: bool
  fno_enabled: bool
  updated_at_utc: str


# --- Auth Dependency ---

def get_current_user(
    cred: Annotated[HTTPAuthorizationCredentials | None, Depends(_security_bearer)]
) -> UserRecord:
  """FastAPI dependency to extract and validate the current authenticated user from Bearer JWT."""
  if not cred or not cred.credentials:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing authentication credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )
  try:
    payload = decode_access_token(cred.credentials)
    user_id: str = payload.get("sub")
    if not user_id:
      raise HTTPException(
          status_code=status.HTTP_401_UNAUTHORIZED,
          detail="Invalid token payload.",
      )
  except ValueError as e:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=str(e),
        headers={"WWW-Authenticate": "Bearer"},
    )

  with _db_lock:
    with _SessionLocal() as session:
      user = session.query(UserRecord).filter(UserRecord.user_id == user_id).first()
      if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
      if not getattr(user, "is_active", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user account.")
      return user


# --- Endpoints ---

@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register_user(req: UserRegisterRequest):
  """Registers a new trader/user and issues an initial JWT bearer token."""
  now = datetime.now(timezone.utc).isoformat()
  with _db_lock:
    with _SessionLocal() as session:
      existing = session.query(UserRecord).filter(UserRecord.email == req.email.lower()).first()
      if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email address already exists.",
        )

      user_id = f"USR_{uuid.uuid4().hex[:12].upper()}"
      hashed = hash_password(req.password)
      new_user = UserRecord(
          user_id=user_id,
          email=req.email.lower(),
          hashed_password=hashed,
          full_name=req.full_name.strip(),
          role=req.role,
          is_active=True,
          created_at_utc=now,
          updated_at_utc=now,
      )
      session.add(new_user)
      session.commit()

  expire_delta = timedelta(days=7)
  token = create_access_token(
      data={"sub": user_id, "email": req.email.lower(), "role": req.role},
      expires_delta=expire_delta,
  )
  return TokenResponse(
      access_token=token,
      expires_in_seconds=int(expire_delta.total_seconds()),
      user_id=user_id,
      email=req.email.lower(),
      role=req.role,
  )


@router.post("/login", response_model=TokenResponse)
def login_user(req: UserLoginRequest):
  """Authenticates user credentials and issues a JWT token."""
  with _db_lock:
    with _SessionLocal() as session:
      user = session.query(UserRecord).filter(UserRecord.email == req.email.lower()).first()
      if not user or not verify_password(req.password, getattr(user, "hashed_password", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
      if not getattr(user, "is_active", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user account.")

      user_id = getattr(user, "user_id")
      user_role = getattr(user, "role", "TRADER")

  expire_delta = timedelta(days=7)
  token = create_access_token(
      data={"sub": user_id, "email": req.email.lower(), "role": user_role},
      expires_delta=expire_delta,
  )
  return TokenResponse(
      access_token=token,
      expires_in_seconds=int(expire_delta.total_seconds()),
      user_id=user_id,
      email=req.email.lower(),
      role=user_role,
  )


@router.get("/me", response_model=UserProfileResponse)
def get_user_profile(current_user: Annotated[UserRecord, Depends(get_current_user)]):
  """Returns the authenticated user profile and broker connection status."""
  user_id = getattr(current_user, "user_id")
  with _db_lock:
    with _SessionLocal() as session:
      cred = session.query(UserBrokerCredentialRecord).filter(UserBrokerCredentialRecord.user_id == user_id).first()
      has_broker = cred is not None

  return UserProfileResponse(
      user_id=user_id,
      email=getattr(current_user, "email"),
      full_name=getattr(current_user, "full_name", ""),
      role=getattr(current_user, "role", "TRADER"),
      is_active=bool(getattr(current_user, "is_active", True)),
      has_broker_connected=has_broker,
      created_at_utc=str(getattr(current_user, "created_at_utc", "")),
  )


@router.post("/broker-credentials", response_model=BrokerCredentialResponse)
def save_broker_credentials(
    req: BrokerCredentialCreateRequest,
    current_user: Annotated[UserRecord, Depends(get_current_user)],
):
  """Securely encrypts and stores broker SmartAPI credentials for the authenticated tenant."""
  user_id = getattr(current_user, "user_id")
  now = datetime.now(timezone.utc).isoformat()

  # Encrypt sensitive credentials with AES-256 Fernet vault
  enc_key = encrypt_secret(req.api_key)
  enc_totp = encrypt_secret(req.totp_secret)
  enc_pw = encrypt_secret(req.password)

  with _db_lock:
    with _SessionLocal() as session:
      existing = session.query(UserBrokerCredentialRecord).filter(
          UserBrokerCredentialRecord.user_id == user_id,
          UserBrokerCredentialRecord.broker_name == req.broker_name,
      ).first()

      if existing:
        existing.client_code = req.client_code.strip()
        existing.encrypted_api_key = enc_key
        existing.encrypted_totp_secret = enc_totp
        existing.encrypted_password = enc_pw
        existing.is_live_authorized = req.is_live_authorized
        existing.risk_profile = req.risk_profile
        existing.reference_capital = req.reference_capital
        existing.fno_enabled = req.fno_enabled
        existing.updated_at_utc = now
        cred_id = getattr(existing, "credential_id")
      else:
        cred_id = f"CRED_{uuid.uuid4().hex[:12].upper()}"
        new_cred = UserBrokerCredentialRecord(
            credential_id=cred_id,
            user_id=user_id,
            broker_name=req.broker_name,
            client_code=req.client_code.strip(),
            encrypted_api_key=enc_key,
            encrypted_totp_secret=enc_totp,
            encrypted_password=enc_pw,
            is_live_authorized=req.is_live_authorized,
            risk_profile=req.risk_profile,
            reference_capital=req.reference_capital,
            fno_enabled=req.fno_enabled,
            created_at_utc=now,
            updated_at_utc=now,
        )
        session.add(new_cred)
      session.commit()

  # Mask API key for response (e.g. "AB12...9Z")
  masked_key = req.api_key[:4] + "****" + req.api_key[-2:] if len(req.api_key) > 6 else "****"
  return BrokerCredentialResponse(
      credential_id=cred_id,
      broker_name=req.broker_name,
      client_code=req.client_code.strip(),
      api_key_masked=masked_key,
      totp_secret_configured=bool(req.totp_secret),
      risk_profile=req.risk_profile,
      reference_capital=f"{req.reference_capital:.2f}",
      is_live_authorized=req.is_live_authorized,
      fno_enabled=req.fno_enabled,
      updated_at_utc=now,
  )


@router.get("/broker-credentials", response_model=BrokerCredentialResponse | None)
def get_broker_credentials_status(current_user: Annotated[UserRecord, Depends(get_current_user)]):
  """Retrieves masked credential info for the active user without leaking secret plaintext."""
  user_id = getattr(current_user, "user_id")
  with _db_lock:
    with _SessionLocal() as session:
      cred = session.query(UserBrokerCredentialRecord).filter(
          UserBrokerCredentialRecord.user_id == user_id
      ).first()
      if not cred:
        return None

      try:
        raw_key = decrypt_secret(getattr(cred, "encrypted_api_key", ""))
        masked_key = raw_key[:4] + "****" + raw_key[-2:] if len(raw_key) > 6 else "****"
      except Exception:
        masked_key = "****"

      return BrokerCredentialResponse(
          credential_id=getattr(cred, "credential_id"),
          broker_name=getattr(cred, "broker_name"),
          client_code=getattr(cred, "client_code"),
          api_key_masked=masked_key,
          totp_secret_configured=bool(getattr(cred, "encrypted_totp_secret", "")),
          risk_profile=getattr(cred, "risk_profile"),
          reference_capital=f"{Decimal(str(getattr(cred, 'reference_capital', 5000))):.2f}",
          is_live_authorized=bool(getattr(cred, "is_live_authorized", False)),
          fno_enabled=bool(getattr(cred, "fno_enabled", True)),
          updated_at_utc=str(getattr(cred, "updated_at_utc", "")),
      )


@router.delete("/broker-credentials")
def delete_broker_credentials(current_user: Annotated[UserRecord, Depends(get_current_user)]):
  """Removes stored broker credentials for the authenticated user."""
  user_id = getattr(current_user, "user_id")
  with _db_lock:
    with _SessionLocal() as session:
      cur = session.conn.cursor()
      cur.execute("DELETE FROM user_broker_credentials WHERE user_id = ?", (user_id,))
      session.commit()
  return {"status": "SUCCESS", "message": "Broker credentials removed."}


@router.post("/broker-credentials/test")
def test_broker_connectivity(
    current_user: Annotated[UserRecord, Depends(get_current_user)],
    payload: BrokerCredentialCreateRequest | None = None,
):
  """Tests live connectivity to Angel One SmartAPI using provided or stored credentials."""
  user_id = getattr(current_user, "user_id")
  api_key = payload.api_key if payload else None
  client_code = payload.client_code if payload else None
  totp_secret = payload.totp_secret if payload else None
  password = payload.password if payload else None

  if not api_key:
    with _db_lock:
      with _SessionLocal() as session:
        cred = session.query(UserBrokerCredentialRecord).filter(
            UserBrokerCredentialRecord.user_id == user_id
        ).first()
        if not cred:
          raise HTTPException(status_code=400, detail="No broker credentials configured to test.")
        api_key = decrypt_secret(getattr(cred, "encrypted_api_key"))
        client_code = getattr(cred, "client_code")
        totp_secret = decrypt_secret(getattr(cred, "encrypted_totp_secret"))
        password = decrypt_secret(getattr(cred, "encrypted_password"))

  from app.execution.gateway import LiveAngelSmartAPIGateway
  try:
    gw = LiveAngelSmartAPIGateway(
        api_key=api_key,
        client_code=client_code,
        totp_secret=totp_secret,
        password=password,
    )
    rms = gw.get_margins()
    return {
        "status": "SUCCESS",
        "message": f"Successfully authenticated with Angel One SmartAPI for client {client_code}",
        "client_code": client_code,
        "available_cash_inr": str(rms.available_cash),
        "total_equity_inr": str(rms.total_equity),
    }
  except Exception as e:
    return {
        "status": "FAILED",
        "message": f"Angel One authentication failed: {e}",
    }
