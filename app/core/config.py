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
  max_positions_per_sector: int = Field(default=1)
  atr_volatility_multiplier: Decimal = Field(default=Decimal("1.5"))
  adaptive_atr_trailing: bool = Field(default=False)
  adaptive_atr_trail_multiplier: Decimal = Field(default=Decimal("2.0"))
  require_portfolio_var_cap: bool = Field(default=False)
  max_portfolio_var_95_pct: Decimal = Field(default=Decimal("0.025"))
  require_participation_rate_cap: bool = Field(default=False)
  max_participation_rate_pct: Decimal = Field(default=Decimal("0.05"))
  require_regime_filter: bool = Field(default=False)
  allowed_regimes: list[str] = Field(default_factory=lambda: ["BULL", "NEUTRAL"])
  opening_range_minutes: int | None = Field(default=None)
  min_rvol_threshold: Decimal | None = Field(default=None)
  require_macro_trend_alignment: bool | None = Field(default=None)
  require_two_day_high_breakout: bool | None = Field(default=None)
  require_vwap_volatility_cap: bool | None = Field(default=None)
  max_vwap_std_multiplier: Decimal | None = Field(default=None)


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
  require_macro_trend_alignment: bool = Field(default=False)
  macro_timeframe_minutes: int = Field(default=60)
  macro_ema_period: int = Field(default=50)
  require_two_day_high_breakout: bool = Field(default=False)
  require_vwap_volatility_cap: bool = Field(default=False)
  max_vwap_std_multiplier: Decimal = Field(default=Decimal("2.5"))
  require_regime_filter: bool = Field(default=False)
  regime_min_bars: int = Field(default=30)
  allowed_regimes: list[str] = Field(default_factory=lambda: ["BULL", "NEUTRAL"])
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
  enable_market_impact: bool = Field(default=False)
  impact_coefficient_eta: Decimal = Field(default=Decimal("0.5"))
  impact_coefficient_gamma: Decimal = Field(default=Decimal("0.25"))


class AppConfig(BaseModel):
  execution_mode: str = Field(default="PAPER")
  database_url: str = Field(default="sqlite:///./alphatrade_v2.db")
  active_risk_profile: str = Field(default="STANDARD_RESEARCH_100K")
  active_cost_profile: str = Field(default="PERCENTAGE_CAPPED_DEFAULT")
  data_provider: str = Field(default="FYERS_V3")
  strategy: StrategyConfigModel
  costs: dict[str, CostProfileConfig]
  cost_version: str


def _load_env_file() -> None:
  base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
  env_path = os.path.join(base_dir, ".env")
  if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
      for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
          continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("'\"")
        if k and k not in os.environ:
          os.environ[k] = v


def load_app_config(
    execution_mode: str | None = None,
    risk_profile_name: str | None = None,
    cost_profile_name: str | None = None,
    database_url: str | None = None,
) -> AppConfig:
  _load_env_file()
  mode = assert_paper_mode(execution_mode)
  base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
  with open(os.path.join(base_dir, "config/strategy/orb_v2.json"), "r") as f:
    strat_raw = json.load(f)
  with open(os.path.join(base_dir, "config/costs/nse_intraday_costs.json"), "r") as f:
    cost_raw = json.load(f)

  active_profile = risk_profile_name or os.environ.get("ALPHATRADE_CAPITAL_PROFILE", "STANDARD_RESEARCH_100K")
  if active_profile in strat_raw.get("risk_profiles", {}):
    prof_data = strat_raw["risk_profiles"][active_profile]
    for k in (
        "opening_range_minutes",
        "min_rvol_threshold",
        "require_macro_trend_alignment",
        "require_two_day_high_breakout",
        "require_vwap_volatility_cap",
        "max_vwap_std_multiplier",
        "require_regime_filter",
    ):
      if k in prof_data and prof_data[k] is not None:
        strat_raw[k] = prof_data[k]

  return AppConfig(
      execution_mode=mode,
      database_url=database_url or os.environ.get("ALPHATRADE_DATABASE_URL", "sqlite:///./alphatrade_v2.db"),
      active_risk_profile=active_profile,
      active_cost_profile=cost_profile_name or "PERCENTAGE_CAPPED_DEFAULT",
      data_provider=os.environ.get("ALPHATRADE_DATA_PROVIDER", "FYERS_V3"),
      strategy=StrategyConfigModel(**strat_raw),
      costs={k: CostProfileConfig(**v) for k, v in cost_raw["profiles"].items()},
      cost_version=cost_raw["version"],
  )
