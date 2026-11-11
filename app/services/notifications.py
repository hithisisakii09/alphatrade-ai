"""Zero-trust asynchronous webhook notification service for Discord and Telegram."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import os
from typing import Any
import httpx

from app.core.clock import to_ist
from app.core.execution_guard import assert_paper_mode

logger = logging.getLogger(__name__)


class WebhookNotificationService:
  """Dispatches trading events, risk alerts, and daily EOD summaries via Discord & Telegram.

  Enforces fail-safe delivery: notification dispatch is asynchronous and errors never impede order execution.
  """

  def __init__(
      self,
      discord_webhook_url: str | None = None,
      telegram_bot_token: str | None = None,
      telegram_chat_id: str | None = None,
  ):
    assert_paper_mode()
    self.discord_webhook_url = discord_webhook_url or os.environ.get("DISCORD_WEBHOOK_URL")
    self.telegram_bot_token = telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    self.telegram_chat_id = telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID")

  @property
  def is_configured(self) -> bool:
    has_discord = bool(self.discord_webhook_url and self.discord_webhook_url.startswith("http"))
    has_telegram = bool(self.telegram_bot_token and self.telegram_chat_id)
    return has_discord or has_telegram

  def format_order_fill_message(
      self,
      symbol: str,
      side: str,
      quantity: int,
      fill_price: Decimal,
      total_fees: Decimal,
      reason: str | None = None,
  ) -> str:
    """Formats order execution message with exact Decimal arithmetic."""
    direction_emoji = "🟢 BUY" if side.upper() == "BUY" else "🔴 SELL"
    gross_val = (fill_price * Decimal(quantity)).quantize(Decimal("0.01"))
    msg = (
        f"**[PAPER TRADE] {direction_emoji} Fill Executed**\n"
        f"• **Symbol:** `{symbol}`\n"
        f"• **Quantity:** `{quantity}` shares\n"
        f"• **Fill Price:** `₹{fill_price:,.2f}`\n"
        f"• **Gross Turnover:** `₹{gross_val:,.2f}`\n"
        f"• **Statutory Fees:** `₹{total_fees:,.2f}`\n"
    )
    if reason:
      msg += f"• **Reason / Trigger:** `{reason}`\n"
    return msg

  def format_position_exit_message(
      self,
      symbol: str,
      entry_price: Decimal,
      exit_price: Decimal,
      quantity: int,
      net_pnl_inr: Decimal,
      exit_reason: str,
  ) -> str:
    """Formats position exit and realized PnL alert."""
    pnl_emoji = "🟢 PROFIT" if net_pnl_inr >= Decimal("0.00") else "🔴 LOSS"
    return (
        f"**[PAPER POSITION CLOSED] {pnl_emoji}**\n"
        f"• **Symbol:** `{symbol}`\n"
        f"• **Quantity:** `{quantity}` shares\n"
        f"• **Entry Price:** `₹{entry_price:,.2f}`\n"
        f"• **Exit Price:** `₹{exit_price:,.2f}`\n"
        f"• **Net Realized PnL:** `₹{net_pnl_inr:,.2f}`\n"
        f"• **Exit Reason:** `{exit_reason}`\n"
    )

  def format_trailing_stop_message(
      self,
      symbol: str,
      highest_price: Decimal,
      new_stop_price: Decimal,
      trailing_drop_pct: Decimal,
  ) -> str:
    """Formats trailing stop adjustment notification."""
    return (
        f"**[TRAILING STOP UPDATED] 🎯 {symbol}**\n"
        f"• **Peak Session High:** `₹{highest_price:,.2f}`\n"
        f"• **New Ratcheted Stop:** `₹{new_stop_price:,.2f}`\n"
        f"• **Trail Distance:** `{trailing_drop_pct * Decimal('100'):,.2f}%` from peak\n"
        f"• **Intraday Rule:** Stop strictly rises; never loosens.\n"
    )

  def format_kill_switch_message(
      self,
      session_date: str,
      daily_realized_pnl: Decimal,
      loss_limit_inr: Decimal,
  ) -> str:
    """Formats emergency risk limit trip alert."""
    return (
        f"**🚨 [EMERGENCY KILL SWITCH TRIPPED] 🚨**\n"
        f"• **Date:** `{session_date}`\n"
        f"• **Daily Realized Loss:** `₹{abs(daily_realized_pnl):,.2f}`\n"
        f"• **1.5% Loss Budget Threshold:** `₹{loss_limit_inr:,.2f}`\n"
        f"• **Action Taken:** Entry pipeline immediately halted. All open positions locked for 15:00 IST liquidation.\n"
    )

  def format_eod_summary_message(
      self,
      session_date: str,
      starting_capital: Decimal,
      ending_cash: Decimal,
      total_realized_pnl: Decimal,
      total_fees_paid: Decimal,
      trades_count: int,
  ) -> str:
    """Formats end-of-day ledger reconciliation report."""
    pnl_emoji = "📈" if total_realized_pnl >= Decimal("0.00") else "📉"
    net_return_pct = (
        (total_realized_pnl / starting_capital) * Decimal("100")
        if starting_capital > Decimal("0.00")
        else Decimal("0.00")
    ).quantize(Decimal("0.01"))
    return (
        f"**[EOD SESSION REPORT] {pnl_emoji} {session_date}**\n"
        f"• **Starting Capital:** `₹{starting_capital:,.2f}`\n"
        f"• **Ending Cash Balance:** `₹{ending_cash:,.2f}`\n"
        f"• **Closed Trades Today:** `{trades_count}`\n"
        f"• **Net Realized PnL:** `₹{total_realized_pnl:,.2f}` (`{net_return_pct:+,.2f}%`)\n"
        f"• **Total Statutory Taxes & Fees:** `₹{total_fees_paid:,.2f}`\n"
        f"• **Accounting:** Double-entry ledger balanced & reconciled against SQLite.\n"
    )

  def send_message_sync(self, content: str) -> dict[str, bool]:
    """Synchronously sends message to configured webhooks without raising exceptions."""
    results = {"discord": False, "telegram": False}
    if not self.is_configured:
      return results

    # 1. Dispatch to Discord
    if self.discord_webhook_url and self.discord_webhook_url.startswith("http"):
      try:
        with httpx.Client(timeout=6.0) as client:
          resp = client.post(self.discord_webhook_url, json={"content": content})
          results["discord"] = (200 <= resp.status_code < 300)
      except Exception as e:
        logger.warning(f"Failed to dispatch Discord webhook: {e}")

    # 2. Dispatch to Telegram
    if self.telegram_bot_token and self.telegram_chat_id:
      try:
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.telegram_chat_id,
            "text": content,
            "parse_mode": "Markdown",
        }
        with httpx.Client(timeout=6.0) as client:
          resp = client.post(url, json=payload)
          results["telegram"] = (200 <= resp.status_code < 300)
      except Exception as e:
        logger.warning(f"Failed to dispatch Telegram notification: {e}")

    return results

  async def send_message_async(self, content: str) -> dict[str, bool]:
    """Asynchronously dispatches message to Discord and Telegram without blocking."""
    return await asyncio.to_thread(self.send_message_sync, content)
