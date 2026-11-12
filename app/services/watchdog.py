"""Watchdog Daemon & Dead-Man's Switch for AlphaTrade AI v2.0.

Monitors real-time feed freshness during active NSE trading windows (09:15 - 15:30 IST).
Trips the dead-man's switch if market ticks stall for longer than staleness_threshold_seconds (default 180s).
"""

from datetime import datetime, time, timezone
import logging
from typing import Any, Callable

from app.core.clock import NSECalendar, to_ist
from app.services.notifications import WebhookNotificationService

logger = logging.getLogger("WatchdogService")


class FeedWatchdog:
  """Monitors tick arrival timestamps and raises alerts / trips kill-switch on feed stalls."""

  def __init__(
      self,
      staleness_threshold_seconds: int = 180,
      notification_service: WebhookNotificationService | None = None,
      on_stall_callback: Callable[[str], None] | None = None,
      on_recovery_callback: Callable[[], None] | None = None,
      calendar: NSECalendar | None = None,
  ):
    self.staleness_threshold_seconds = staleness_threshold_seconds
    self.notification_service = notification_service
    self.on_stall_callback = on_stall_callback
    self.on_recovery_callback = on_recovery_callback
    self.calendar = calendar or NSECalendar()
    self.last_tick_time_utc: datetime | None = None
    self.is_stalled: bool = False
    self.stall_event_count: int = 0
    self.last_alert_time_utc: datetime | None = None

  def record_tick(self, tick_time_utc: datetime | None = None) -> None:
    """Invoked by the runner or data feeder whenever a valid market tick is processed."""
    ts = tick_time_utc or datetime.now(timezone.utc)
    self.last_tick_time_utc = ts
    if self.is_stalled:
      logger.info("Watchdog: Feed recovered from stalled state at %s", ts.isoformat())
      self.is_stalled = False
      if self.on_recovery_callback:
        try:
          self.on_recovery_callback()
        except Exception as e:
          logger.warning("Error executing watchdog on_recovery_callback: %s", e)

  def is_in_active_window(self, now_utc: datetime | None = None) -> bool:
    """Checks if current time is within active NSE trading window (09:15 - 15:30 IST on a trading day)."""
    ts = now_utc or datetime.now(timezone.utc)
    if not self.calendar.is_trading_day(ts):
      return False
    dt_ist = to_ist(ts)
    t = dt_ist.time()
    return time(9, 15) <= t < time(15, 30)

  def check_health(self, now_utc: datetime | None = None) -> dict[str, Any]:
    """Evaluates feed health against staleness threshold and triggers fail-closed actions if stalled."""
    ts = now_utc or datetime.now(timezone.utc)
    active = self.is_in_active_window(ts)

    if not active:
      return {
          "status": "IDLE_OFF_HOURS",
          "is_healthy": True,
          "active_window": False,
          "seconds_since_last_tick": (
              (ts - self.last_tick_time_utc).total_seconds() if self.last_tick_time_utc else None
          ),
          "staleness_threshold_seconds": self.staleness_threshold_seconds,
          "last_tick_time": self.last_tick_time_utc.isoformat() if self.last_tick_time_utc else None,
      }

    # Within active trading window
    if self.last_tick_time_utc is None:
      elapsed = float("inf")
    else:
      elapsed = (ts - self.last_tick_time_utc).total_seconds()

    if elapsed > self.staleness_threshold_seconds:
      if not self.is_stalled:
        self.is_stalled = True
        self.stall_event_count += 1
        reason = (
            f"DEAD_MAN_SWITCH: Feed stalled! No market ticks received for {elapsed:.1f}s "
            f"(threshold: {self.staleness_threshold_seconds}s) during active trading hours."
        )
        logger.error(reason)

        # Dispatch notification
        if self.notification_service:
          alert_msg = f"🚨 **DEAD-MAN SWITCH TRIPPED**\nMarket tick feed stalled for {elapsed:.1f}s. Halting order entry."
          try:
            if hasattr(self.notification_service, "send_message_background"):
              self.notification_service.send_message_background(alert_msg)
            else:
              self.notification_service.send_message_sync(alert_msg)
          except Exception as ne:
            logger.warning("Failed to dispatch watchdog alert: %s", ne)

        # Fire callback (e.g. trigger risk engine kill switch)
        if self.on_stall_callback:
          try:
            self.on_stall_callback(reason)
          except Exception as ce:
            logger.error("Watchdog callback error: %s", ce)

      return {
          "status": "FEED_STALLED",
          "is_healthy": False,
          "active_window": True,
          "seconds_since_last_tick": elapsed if elapsed != float("inf") else 999999.0,
          "staleness_threshold_seconds": self.staleness_threshold_seconds,
          "last_tick_time": self.last_tick_time_utc.isoformat() if self.last_tick_time_utc else None,
      }

    return {
        "status": "HEALTHY",
        "is_healthy": True,
        "active_window": True,
        "seconds_since_last_tick": round(elapsed, 2),
        "staleness_threshold_seconds": self.staleness_threshold_seconds,
        "last_tick_time": self.last_tick_time_utc.isoformat() if self.last_tick_time_utc else None,
    }
