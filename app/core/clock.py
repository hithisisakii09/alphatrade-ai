"""UTC storage and Asia/Kolkata (IST) boundary clock & NSE trading calendar service."""

from datetime import date, datetime, time, timezone
import json
import os
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


def ensure_utc(dt: datetime) -> datetime:
  """Ensures a datetime is timezone-aware and normalized to UTC."""
  if dt.tzinfo is None:
    raise ValueError("Naive datetime is prohibited; provide timezone-aware datetime.")
  return dt.astimezone(UTC)


def to_ist(dt: datetime) -> datetime:
  """Converts a timezone-aware datetime to Asia/Kolkata (IST)."""
  return ensure_utc(dt).astimezone(IST)


def ist_datetime(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime:
  """Constructs an IST datetime and returns its UTC representation for storage/comparison."""
  local_dt = datetime(year, month, day, hour, minute, second, tzinfo=IST)
  return local_dt.astimezone(UTC)


class NSECalendar:
  """Versioned NSE trading calendar handling holidays, weekends, and special sessions."""

  def __init__(self, calendar_path: str | None = None):
    if calendar_path is None:
      base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
      calendar_path = os.path.join(base, "config/calendar/nse_calendar_2026.json")
    with open(calendar_path, "r") as f:
      data = json.load(f)
    self.version: str = data["version"]
    self.holidays: set[str] = set(data.get("holidays_2026", []))
    self.special_sessions: dict[str, dict] = {
        s["date"]: s for s in data.get("special_sessions", [])
    }
    self.regular_open = time.fromisoformat(data["regular_session"]["open"])
    self.regular_close = time.fromisoformat(data["regular_session"]["close"])
    self.orb_start = time.fromisoformat(data["orb_window"]["start"])
    self.orb_end = time.fromisoformat(data["orb_window"]["end"])
    self.entry_cutoff = time.fromisoformat(data["entry_cutoff"])
    self.liquidation_start = time.fromisoformat(data["liquidation_start"])
    self.liquidation_deadline = time.fromisoformat(data["liquidation_deadline"])

  def is_trading_day(self, d: date | datetime) -> bool:
    if isinstance(d, datetime):
      d = to_ist(d).date()
    d_str = d.isoformat()
    if d_str in self.special_sessions:
      return True
    if d.weekday() >= 5:
      return False
    return d_str not in self.holidays

  def is_orb_eligible_session(self, d: date | datetime) -> bool:
    if isinstance(d, datetime):
      d = to_ist(d).date()
    d_str = d.isoformat()
    if d_str in self.special_sessions:
      return bool(self.special_sessions[d_str].get("allow_intraday_orb", False))
    return self.is_trading_day(d)

  def get_session_state(self, dt_utc: datetime) -> str:
    dt_ist = to_ist(dt_utc)
    d = dt_ist.date()
    t = dt_ist.time()
    if not self.is_trading_day(d):
      return "CLOSED_HOLIDAY_OR_WEEKEND"
    d_str = d.isoformat()
    if d_str in self.special_sessions:
      spec = self.special_sessions[d_str]
      s_open = time.fromisoformat(spec["open"])
      s_close = time.fromisoformat(spec["close"])
      if s_open <= t < s_close:
        return "SPECIAL_SESSION"
      return "CLOSED"
    if t < self.regular_open or t >= self.regular_close:
      return "CLOSED"
    if self.orb_start <= t < self.orb_end:
      return "OPENING_RANGE_FORMATION"
    if self.orb_end <= t < self.entry_cutoff:
      return "ACTIVE_TRADING_WINDOW"
    if self.entry_cutoff <= t < self.liquidation_start:
      return "NO_NEW_ENTRIES_MANAGE_ONLY"
    if self.liquidation_start <= t <= self.liquidation_deadline:
      return "EOD_LIQUIDATION_WINDOW"
    return "CLOSING_AUCTION_WINDOW"
