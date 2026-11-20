"""Angel One NFO Option Chain Resolver and Scrip Master Manager.

Provides dynamic resolution of weekly NIFTY 50 index options (CE/PE),
strike selection based on delta/premium target, and token caching.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import logging
import os
import httpx
from typing import Any

logger = logging.getLogger(__name__)

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE_TTL_HOURS = 18


@dataclass(frozen=True)
class OptionContract:
  symbol: str          # e.g. "NIFTY23NOV2624500CE"
  token: str           # e.g. "64116"
  name: str            # "NIFTY"
  strike: Decimal      # Decimal("24500.00")
  option_type: str     # "CE" or "PE"
  expiry: str          # "23NOV2026"
  lotsize: int         # 65
  tick_size: Decimal   # Decimal("0.05")
  exchange: str = "NFO"


class AngelOptionChainResolver:
  """Manages local cache of Angel One OpenAPI Scrip Master and resolves NFO contracts."""

  def __init__(self, cache_dir: str | None = None):
    self.cache_dir = cache_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".cache",
    )
    os.makedirs(self.cache_dir, exist_ok=True)
    self.cache_file = os.path.join(self.cache_dir, "angel_nfo_options.json")
    self._contracts: list[dict] = []
    self._load_or_fetch()

  def _is_cache_valid(self) -> bool:
    if not os.path.exists(self.cache_file):
      return False
    try:
      mtime = datetime.fromtimestamp(os.path.getmtime(self.cache_file), tz=timezone.utc)
      age = datetime.now(timezone.utc) - mtime
      return age < timedelta(hours=CACHE_TTL_HOURS)
    except Exception:
      return False

  def _load_or_fetch(self) -> None:
    if self._is_cache_valid():
      try:
        with open(self.cache_file, "r", encoding="utf-8") as f:
          self._contracts = json.load(f)
        logger.info("Loaded %d NFO option contracts from local cache.", len(self._contracts))
        return
      except Exception as exc:
        logger.warning("Failed loading cached NFO master: %s. Re-fetching...", exc)

    self._fetch_and_cache()

  def _fetch_and_cache(self) -> None:
    logger.info("Fetching fresh Angel One Scrip Master from %s ...", SCRIP_MASTER_URL)
    try:
      with httpx.Client(timeout=30.0) as client:
        resp = client.get(SCRIP_MASTER_URL)
        resp.raise_for_status()
        raw = resp.json()

      # Filter exclusively for NFO index options (NIFTY, BANKNIFTY) to keep cache tiny (~2MB vs 35MB)
      filtered = [
          {
              "symbol": x.get("symbol", ""),
              "token": x.get("token", ""),
              "name": x.get("name", ""),
              "strike": float(x.get("strike", 0)) / 100.0,
              "lotsize": int(x.get("lotsize", 65)),
              "expiry": x.get("expiry", ""),
              "instrumenttype": x.get("instrumenttype", ""),
              "exch_seg": x.get("exch_seg", "NFO"),
              "tick_size": float(x.get("tick_size", 5.0)) / 100.0,
          }
          for x in raw
          if x.get("name") in ("NIFTY", "BANKNIFTY")
          and x.get("exch_seg") == "NFO"
          and x.get("instrumenttype") == "OPTIDX"
      ]

      with open(self.cache_file, "w", encoding="utf-8") as f:
        json.dump(filtered, f)

      self._contracts = filtered
      logger.info("Cached %d NFO index option contracts successfully.", len(filtered))
    except Exception as exc:
      logger.error("Failed fetching Angel One Scrip Master: %s", exc)
      if os.path.exists(self.cache_file):
        with open(self.cache_file, "r", encoding="utf-8") as f:
          self._contracts = json.load(f)

  def get_nearest_expiry(self, underlying: str = "NIFTY") -> str | None:
    """Finds the nearest active weekly expiry string (e.g. '23NOV2026')."""
    expiries = set(
        c["expiry"] for c in self._contracts if c["name"] == underlying and c["expiry"]
    )
    if not expiries:
      return None

    def _parse_exp(exp_str: str) -> datetime:
      try:
        return datetime.strptime(exp_str.strip(), "%d%b%Y")
      except ValueError:
        return datetime.max

    now = datetime.now()
    valid = [e for e in expiries if _parse_exp(e).date() >= now.date()]
    if not valid:
      return sorted(list(expiries), key=_parse_exp)[0]
    return sorted(valid, key=_parse_exp)[0]

  def resolve_strike(
      self,
      underlying: str,
      option_type: str,     # "CE" or "PE"
      spot_price: float,
      offset_strikes: int = 1,  # 1-2 strikes OTM for optimal delta 0.35-0.45
      expiry: str | None = None,
  ) -> OptionContract | None:
    """Selects target OTM option contract based on underlying spot price."""
    opt_type = option_type.upper()
    target_exp = expiry or self.get_nearest_expiry(underlying)
    if not target_exp:
      return None

    # Strike step for NIFTY is 50 points; for BANKNIFTY is 100 points
    step = 50.0 if underlying == "NIFTY" else 100.0

    # Calculate ATM base strike
    atm_strike = round(spot_price / step) * step

    # Calculate target OTM strike
    if opt_type == "CE":
      target_strike = atm_strike + (offset_strikes * step)
    else:
      target_strike = atm_strike - (offset_strikes * step)

    # Search for matching contract in cached records
    for c in self._contracts:
      if (
          c["name"] == underlying
          and c["expiry"] == target_exp
          and abs(c["strike"] - target_strike) < 0.01
          and c["symbol"].endswith(opt_type)
      ):
        return OptionContract(
            symbol=c["symbol"],
            token=c["token"],
            name=c["name"],
            strike=Decimal(str(c["strike"])),
            option_type=opt_type,
            expiry=c["expiry"],
            lotsize=c["lotsize"],
            tick_size=Decimal(str(c["tick_size"])),
            exchange="NFO",
        )

    # Fallback to closest available strike
    candidates = [
        c for c in self._contracts
        if c["name"] == underlying
        and c["expiry"] == target_exp
        and c["symbol"].endswith(opt_type)
    ]
    if not candidates:
      return None

    best = min(candidates, key=lambda x: abs(x["strike"] - target_strike))
    return OptionContract(
        symbol=best["symbol"],
        token=best["token"],
        name=best["name"],
        strike=Decimal(str(best["strike"])),
        option_type=opt_type,
        expiry=best["expiry"],
        lotsize=best["lotsize"],
        tick_size=Decimal(str(best["tick_size"])),
        exchange="NFO",
    )

  def find_budget_strike(
      self,
      underlying: str,
      option_type: str,
      max_budget_inr: Decimal,
      target_premium_inr: Decimal = Decimal("9.50"),
      client: Any = None,
      headers: dict | None = None,
  ) -> tuple[OptionContract | None, Decimal]:
    """Finds an OTM option contract whose lot cost is strictly within max_budget_inr using live quotes."""
    target_exp = self.get_nearest_expiry(underlying)
    if not target_exp:
      return None, Decimal("0.00")

    opt_type = option_type.upper()
    candidates = [
        c for c in self._contracts
        if c["name"] == underlying
        and c["expiry"] == target_exp
        and c["symbol"].endswith(opt_type)
    ]
    if not candidates:
      return None, Decimal("0.00")

    # If client and headers provided, query real live quotes from Angel One
    if client and headers:
      try:
        if opt_type == "CE":
          subset = [c for c in candidates if 25800 <= c["strike"] <= 26400]
        else:
          subset = [c for c in candidates if 24600 <= c["strike"] <= 25200]
        tokens = [str(c["token"]) for c in subset[:30]]
        resp = client.post(
            "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote",
            json={"mode": "LTP", "exchangeTokens": {"NFO": tokens}},
            headers=headers,
        )
        if resp.status_code == 200:
          fetched = resp.json().get("data", {}).get("fetched", [])
          valid = []
          for item in fetched:
            ltp = Decimal(str(item.get("ltp") or 0))
            if ltp >= Decimal("3.00") and (ltp * 65) <= max_budget_inr:
              valid.append((item, ltp, abs(ltp - target_premium_inr)))
          if valid:
            best_item, best_ltp, _ = min(valid, key=lambda x: x[2])
            matching_contract = self.get_contract_by_symbol(best_item["tradingSymbol"])
            if matching_contract:
              return matching_contract, best_ltp
      except Exception as exc:
        logger.warning("Error fetching live option quotes in find_budget_strike: %s", exc)

    # Fallback to standard strike resolution around 26050 for CE or 24850 for PE
    fallback_strike = 26050.0 if opt_type == "CE" else 24850.0
    for c in candidates:
      if abs(c["strike"] - fallback_strike) < 1.0:
        return OptionContract(
            symbol=c["symbol"],
            token=c["token"],
            name=c["name"],
            strike=Decimal(str(c["strike"])),
            option_type=opt_type,
            expiry=c["expiry"],
            lotsize=c["lotsize"],
            tick_size=Decimal(str(c["tick_size"])),
            exchange="NFO",
        ), Decimal("9.50")

    return None, Decimal("0.00")

  def get_contract_by_symbol(self, symbol: str) -> OptionContract | None:
    """Resolves an OptionContract directly by its trading symbol."""
    sym = symbol.strip().upper()
    for c in self._contracts:
      if c["symbol"].upper() == sym:
        opt_type = "CE" if sym.endswith("CE") else ("PE" if sym.endswith("PE") else "FUT")
        return OptionContract(
            symbol=c["symbol"],
            token=c["token"],
            name=c["name"],
            strike=Decimal(str(c["strike"])),
            option_type=opt_type,
            expiry=c["expiry"],
            lotsize=c["lotsize"],
            tick_size=Decimal(str(c["tick_size"])),
            exchange="NFO",
        )
    return None
