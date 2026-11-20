"""Unit tests for Phase 1 multi-tenant authentication, password security, JWT, and encrypted credential vault."""

from decimal import Decimal
import uuid
import pytest
from starlette.testclient import TestClient

from app.auth.jwt import create_access_token, decode_access_token
from app.auth.security import hash_password, verify_password
from app.core.crypto import SecretVault, encrypt_secret, decrypt_secret
from app.main import app


@pytest.fixture
def client():
  return TestClient(app)


# --- 1. Cryptographic Vault Tests ---

def test_secret_vault_encryption_decryption():
  vault = SecretVault()
  secret = "MyAngelSmartApiKey123456"
  encrypted = vault.encrypt(secret)
  assert encrypted != secret
  assert len(encrypted) > 20
  decrypted = vault.decrypt(encrypted)
  assert decrypted == secret


def test_secret_vault_empty_strings():
  vault = SecretVault()
  assert vault.encrypt("") == ""
  assert vault.decrypt("") == ""


def test_secret_vault_tamper_detection():
  vault = SecretVault()
  encrypted = vault.encrypt("SecretData")
  tampered = encrypted[:-4] + "AAAA"
  with pytest.raises(ValueError, match="Invalid ciphertext"):
    vault.decrypt(tampered)


# --- 2. Password Security Tests ---

def test_password_hashing_and_verification():
  password = "SecureTraderPass123!"
  hashed = hash_password(password)
  assert "$" in hashed
  assert hashed != password
  assert verify_password(password, hashed) is True
  assert verify_password("WrongPassword123!", hashed) is False
  assert verify_password("", hashed) is False


def test_password_hashing_uses_unique_salts():
  p1 = hash_password("SamePassword")
  p2 = hash_password("SamePassword")
  assert p1 != p2  # Different salt produces distinct hash


# --- 3. JWT Token Lifecycle Tests ---

def test_jwt_token_generation_and_decoding():
  payload = {"sub": "USR_TEST_123", "email": "trader@example.com", "role": "TRADER"}
  token = create_access_token(payload)
  assert isinstance(token, str)
  decoded = decode_access_token(token)
  assert decoded["sub"] == "USR_TEST_123"
  assert decoded["email"] == "trader@example.com"
  assert decoded["role"] == "TRADER"
  assert "exp" in decoded


def test_jwt_token_tamper_rejection():
  token = create_access_token({"sub": "USR_VALID"})
  tampered = token[:-5] + "XXXXX"
  with pytest.raises(ValueError, match="Invalid token"):
    decode_access_token(tampered)


# --- 4. Multi-Tenant Auth API Endpoints Tests ---

def test_user_registration_and_login_flow(client):
  unique_id = uuid.uuid4().hex[:8]
  email = f"trader_{unique_id}@alphatrade.ai"
  reg_payload = {
      "email": email,
      "password": "StrongPassword2026!",
      "full_name": "Alpha Trader",
      "role": "TRADER",
  }

  # 1. Register
  reg_resp = client.post("/auth/register", json=reg_payload)
  assert reg_resp.status_code == 201
  data = reg_resp.json()
  assert "access_token" in data
  assert data["email"] == email.lower()
  token = data["access_token"]

  # 2. Duplicate registration rejected
  dup_resp = client.post("/auth/register", json=reg_payload)
  assert dup_resp.status_code == 409

  # 3. Login with correct password
  login_resp = client.post("/auth/login", json={"email": email, "password": "StrongPassword2026!"})
  assert login_resp.status_code == 200
  login_data = login_resp.json()
  assert "access_token" in login_data

  # 4. Login with wrong password rejected
  bad_login = client.post("/auth/login", json={"email": email, "password": "WrongPassword!"})
  assert bad_login.status_code == 401

  # 5. Access protected /auth/me
  me_resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
  assert me_resp.status_code == 200
  me_data = me_resp.json()
  assert me_data["email"] == email.lower()
  assert me_data["full_name"] == "Alpha Trader"
  assert me_data["has_broker_connected"] is False


def test_broker_credential_encryption_and_storage(client):
  unique_id = uuid.uuid4().hex[:8]
  email = f"angel_{unique_id}@alphatrade.ai"
  client.post("/auth/register", json={
      "email": email,
      "password": "Password12345!",
      "full_name": "Angel User",
      "role": "TRADER",
  })
  login = client.post("/auth/login", json={"email": email, "password": "Password12345!"}).json()
  token = login["access_token"]
  auth_headers = {"Authorization": f"Bearer {token}"}

  cred_payload = {
      "broker_name": "ANGEL_ONE",
      "client_code": "S54949978",
      "api_key": "LIVE_API_KEY_ABC123",
      "totp_secret": "PQQNAFSM44DJQV2HFVF33LBCPQ",
      "password": "TradingPassword999",
      "risk_profile": "ASYMMETRIC_25PCT_MOMENTUM",
      "reference_capital": 5000.00,
      "is_live_authorized": True,
      "fno_enabled": True,
  }

  # 1. Save credentials (encrypted at rest)
  save_resp = client.post("/auth/broker-credentials", json=cred_payload, headers=auth_headers)
  assert save_resp.status_code == 200
  saved_data = save_resp.json()
  assert saved_data["client_code"] == "S54949978"
  # Plaintext key should NOT be in response
  assert saved_data["api_key_masked"] == "LIVE****23"
  assert saved_data["totp_secret_configured"] is True
  assert saved_data["is_live_authorized"] is True

  # 2. Retrieve credentials status (masked)
  get_resp = client.get("/auth/broker-credentials", headers=auth_headers)
  assert get_resp.status_code == 200
  cred_status = get_resp.json()
  assert cred_status["client_code"] == "S54949978"
  assert cred_status["api_key_masked"] == "LIVE****23"

  # 3. Check /auth/me reflects broker connection
  me_resp = client.get("/auth/me", headers=auth_headers)
  assert me_resp.json()["has_broker_connected"] is True

  # 4. Delete credentials
  del_resp = client.delete("/auth/broker-credentials", headers=auth_headers)
  assert del_resp.status_code == 200

  # 5. Verify deleted
  get_after = client.get("/auth/broker-credentials", headers=auth_headers)
  assert get_after.status_code == 200
  assert get_after.json() is None
