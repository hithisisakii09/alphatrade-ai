"""Unit tests for WebhookNotificationService (Discord & Telegram)."""

from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest

from app.services.notifications import WebhookNotificationService


def test_notification_service_configuration():
  svc_none = WebhookNotificationService()
  assert svc_none.is_configured is False

  svc_discord = WebhookNotificationService(discord_webhook_url="https://discord.com/api/webhooks/test")
  assert svc_discord.is_configured is True

  svc_tg_incomplete = WebhookNotificationService(telegram_bot_token="123456:ABC")
  assert svc_tg_incomplete.is_configured is False

  svc_tg_complete = WebhookNotificationService(telegram_bot_token="123456:ABC", telegram_chat_id="-100123")
  assert svc_tg_complete.is_configured is True


def test_message_formatting_integrity():
  svc = WebhookNotificationService()

  # 1. Order fill formatting
  fill_msg = svc.format_order_fill_message(
      symbol="RELIANCE",
      side="BUY",
      quantity=40,
      fill_price=Decimal("1250.50"),
      total_fees=Decimal("24.15"),
      reason="15M_ORB_BREAKOUT_ABOVE_ORH",
  )
  assert "RELIANCE" in fill_msg
  assert "`40` shares" in fill_msg
  assert "₹1,250.50" in fill_msg
  assert "₹50,020.00" in fill_msg  # Gross turnover
  assert "₹24.15" in fill_msg
  assert "15M_ORB_BREAKOUT_ABOVE_ORH" in fill_msg

  # 2. Trailing stop formatting
  trail_msg = svc.format_trailing_stop_message(
      symbol="TCS",
      highest_price=Decimal("4250.00"),
      new_stop_price=Decimal("4218.12"),
      trailing_drop_pct=Decimal("0.0075"),
  )
  assert "TCS" in trail_msg
  assert "₹4,250.00" in trail_msg
  assert "₹4,218.12" in trail_msg
  assert "0.75%" in trail_msg

  # 3. Kill switch formatting
  ks_msg = svc.format_kill_switch_message(
      session_date="2026-11-10",
      daily_realized_pnl=Decimal("-1540.00"),
      loss_limit_inr=Decimal("1500.00"),
  )
  assert "EMERGENCY KILL SWITCH TRIPPED" in ks_msg
  assert "₹1,540.00" in ks_msg
  assert "₹1,500.00" in ks_msg

  # 4. EOD report formatting
  eod_msg = svc.format_eod_summary_message(
      session_date="2026-11-10",
      starting_capital=Decimal("100000.00"),
      ending_cash=Decimal("101450.25"),
      total_realized_pnl=Decimal("1450.25"),
      total_fees_paid=Decimal("48.50"),
      trades_count=3,
  )
  assert "EOD SESSION REPORT" in eod_msg
  assert "₹100,000.00" in eod_msg
  assert "₹101,450.25" in eod_msg
  assert "₹1,450.25" in eod_msg
  assert "+1.45%" in eod_msg
  assert "Closed Trades Today:** `3`" in eod_msg

  # 5. Position exit formatting
  exit_msg = svc.format_position_exit_message(
      symbol="RELIANCE",
      entry_price=Decimal("1250.50"),
      exit_price=Decimal("1281.75"),
      quantity=40,
      net_pnl_inr=Decimal("1201.85"),
      exit_reason="TRAILING_STOP_EXIT",
  )
  assert "PAPER POSITION CLOSED" in exit_msg
  assert "RELIANCE" in exit_msg
  assert "₹1,250.50" in exit_msg
  assert "₹1,281.75" in exit_msg
  assert "₹1,201.85" in exit_msg
  assert "TRAILING_STOP_EXIT" in exit_msg


def test_send_message_sync_mocked():
  svc = WebhookNotificationService(
      discord_webhook_url="https://discord.com/api/webhooks/mock",
      telegram_bot_token="mock_token",
      telegram_chat_id="mock_chat",
  )

  with patch("httpx.Client.post") as mock_post:
    mock_post.return_value = MagicMock(status_code=200)
    res = svc.send_message_sync("Test Alert")
    assert res["discord"] is True
    assert res["telegram"] is True
    assert mock_post.call_count == 2

  # Test fail-safe error suppression (never throws exception even if network fails)
  with patch("httpx.Client.post", side_effect=Exception("Network Timeout")):
    fail_res = svc.send_message_sync("Test Alert Under Failure")
    assert fail_res["discord"] is False
    assert fail_res["telegram"] is False


def test_send_message_background_returns_immediately():
  import time
  svc = WebhookNotificationService(
      discord_webhook_url="https://discord.com/api/webhooks/mock",
  )

  def slow_post(*args, **kwargs):
    time.sleep(0.5)
    return MagicMock(status_code=200)

  with patch("httpx.Client.post", side_effect=slow_post):
    start = time.perf_counter()
    svc.send_message_background("Background Alert")
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    # Must return immediately (< 100ms) without waiting for the 500ms slow_post
    assert elapsed_ms < 100.0
