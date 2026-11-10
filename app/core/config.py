"""Versioned configuration loader with Pydantic validation and PAPER-only enforcement."""

from decimal import Decimal
import json
import os
from pydantic import BaseModel, Field
from app.core.execution_guard import assert_paper_mode


class RiskProfileConfig(BaseModel):
  reference_capital: Decimal
  max_risk_per_trade_pct: Decimal
  max_daily_loss_pct: Decimal
  max_capital_deployed_pct: Decimal
  max_single_position_pct: Decimal
  max_open_positions: int
  exit_mode: str
  initial_stop_pct: Decimal
  min_stop_pct: Decimal
  max_stop_pct: Decimal
  trailing_activation_pct: Decimal
  trailing_drop_pct: Decimal
  max_cost_to_risk_ratio: Decimal


class StrategyConfigModel(BaseModel):
  strategy_id: str
  version: str
  direction: str
  base_interval_minutes: int
  signal_interval_minutes: int
  opening_range_minutes: int
  min_rvol_threshold: Decimal
  min_daily_turnover_cr: Decimal
  max_spread_pct: Decimal
  min_upper_circuit_headroom_pct: Decimal
  signal_expiry_bars_5m: int
  entry_cutoff_ist: str
  liquidation_start_ist: str
  liquidation_deadline_ist: str
  risk_profiles: dict[str, RiskProfileConfig]


class CostProfileConfig(BaseModel):
  brokerage_pct: Decimal
  brokerage_flat_cap_inr: Decimal
  stt_buy_pct: Decimal
  stt_sell_pct: Decimal
  exchange_txn_pct: Decimal
  sebi_turnover_pct: Decimal
  ipft_pct: Decimal
  stamp_duty_buy_pct: Decimal
  stamp_duty_sell_pct: Decimal
  gst_pct: Decimal
  half_spread_pct: Decimal
  slippage_pct: Decimal


class AppConfig(BaseModel):
  execution_mode: str = Field(default="PAPER")
  database_url: str = Field(default="sqlite:///./alphatrade_v2.db")
  active_risk_profile: str = Field(default="STANDARD_RESEARCH_100K")
  active_cost_profile: str = Field(default="PERCENTAGE_CAPPED_DEFAULT")
  data_provider: str = Field(default="FYERS_V3")
  strategy: StrategyConfigModel
  costs: dict[str, CostProfileConfig]
  cost_version: str


def load_app_config(
    execution_mode: str | None = None,
    risk_profile_name: str | None = None,
    cost_profile_name: str | None = None,
    database_url: str | None = None,
) -> AppConfig:
  mode = assert_paper_mode(execution_mode)
  base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
  with open(os.path.join(base_dir, "config/strategy/orb_v2.json"), "r") as f:
    strat_raw = json.load(f)
  with open(os.path.join(base_dir, "config/costs/nse_intraday_costs.json"), "r") as f:
    cost_raw = json.load(f)

  return AppConfig(
      execution_mode=mode,
      database_url=database_url or os.environ.get("ALPHATRADE_DATABASE_URL", "sqlite:///./alphatrade_v2.db"),
      active_risk_profile=risk_profile_name or os.environ.get("ALPHATRADE_CAPITAL_PROFILE", "STANDARD_RESEARCH_100K"),
      active_cost_profile=cost_profile_name or "PERCENTAGE_CAPPED_DEFAULT",
      data_provider=os.environ.get("ALPHATRADE_DATA_PROVIDER", "FYERS_V3"),
      strategy=StrategyConfigModel(**strat_raw),
      costs={k: CostProfileConfig(**v) for k, v in cost_raw["profiles"].items()},
      cost_version=cost_raw["version"],
  )
