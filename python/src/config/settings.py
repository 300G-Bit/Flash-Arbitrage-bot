"""
Configuration management for Flash Arbitrage Bot.

This module handles loading and validating configuration from YAML files
and environment variables.
"""

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
import yaml


def load_yaml_config(config_path: Optional[Path] = None) -> dict:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config file. If None, uses default location.

    Returns:
        Dictionary containing configuration values.
    """
    if config_path is None:
        # Try default locations
        for candidate in [
            Path("config/config.yaml"),
            Path("../config/config.yaml"),
            Path("../../config/config.yaml"),
        ]:
            if candidate.exists():
                config_path = candidate
                break

    if config_path is None or not config_path.exists():
        return {}

    with open(config_path, "r") as f:
        return yaml.safe_load(f)


class BaseConfig(BaseSettings):
    """Base configuration with common settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="FLASH_ARB_",
        extra="allow",
    )


class ExchangeConfig(BaseConfig):
    """Configuration for a specific exchange."""

    enabled: bool = True
    testnet: bool = False
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    passphrase: Optional[str] = None  # For OKX


class TrendAnalysisConfig(BaseConfig):
    """Trend analysis configuration."""

    timeframes: List[str] = ["4h", "1h", "30m", "15m", "5m", "1m"]
    min_alignment_score: int = 60
    min_trend_strength: int = 50

    # EMA settings
    ema_fast_period: int = 9
    ema_slow_period: int = 21

    # MACD settings
    macd_fast_period: int = 12
    macd_slow_period: int = 26
    macd_signal_period: int = 9


class PinDetectionConfig(BaseConfig):
    """Pin bar detection configuration."""

    detection_window_ms: int = 500
    velocity_threshold: float = 0.003  # 0.3%
    volume_spike_factor: float = 3.0
    min_amplitude: float = 0.002  # 0.2%
    max_amplitude: float = 0.05  # 5%
    cooldown_ticks: int = 50


class EntryStrategyConfig(BaseConfig):
    """Two-phase entry strategy configuration."""

    confirmation_retracement: float = 0.003  # 0.3%
    confirmation_timeout_ms: int = 15000
    callback_depth_ratio: float = 0.5
    callback_timeout_ms: int = 60000
    rebound_confirmation: float = 0.1
    peak_tolerance: float = 0.005  # 0.5%


class CloseStrategyConfig(BaseConfig):
    """Position closing strategy configuration."""

    first_leg_profit_threshold: float = 0.002
    second_leg_profit_threshold: float = 0.002
    stop_loss_threshold: float = 0.01  # 1%
    max_hold_time_ms: int = 60000
    urgent_close_time_ms: int = 45000


class PositionConfig(BaseConfig):
    """Position management configuration."""

    base_position_usdt: float = 15.0
    max_position_usdt: float = 30.0
    min_position_usdt: float = 5.0
    default_leverage: int = 20
    max_leverage: int = 50
    risk_per_trade: float = 0.02


class RiskControlConfig(BaseConfig):
    """Risk control configuration."""

    # Circuit breaker
    consecutive_loss_limit: int = 5
    daily_loss_percent_limit: float = 0.10  # 10%
    hourly_loss_percent_limit: float = 0.05  # 5%
    api_error_limit: int = 5
    network_timeout_limit: int = 3
    short_breaker_duration: int = 300  # 5 minutes
    medium_breaker_duration: int = 1800  # 30 minutes
    long_breaker_duration: int = 3600  # 1 hour

    # Daily limits
    max_daily_trades: int = 50
    max_consecutive_losses: int = 5
    cooldown_after_loss_streak: int = 300


class CoinScreeningConfig(BaseConfig):
    """Coin screening configuration."""

    min_24h_volume_usdt: float = 10_000_000
    max_spread_percent: float = 0.2
    min_daily_volatility: float = 0.02
    max_daily_volatility: float = 0.30
    enable_auto_screening: bool = True
    screening_interval_minutes: int = 5


class LoggingConfig(BaseConfig):
    """Logging configuration."""

    level: str = "INFO"
    log_dir: str = "logs"
    format_json: bool = False


class RedisConfig(BaseConfig):
    """Redis configuration."""

    url: str = "redis://localhost:6379"
    channels_tick: str = "flash_arb:tick"
    channels_kline: str = "flash_arb:kline"
    channels_depth: str = "flash_arb:depth"
    channels_ticker: str = "flash_arb:ticker"


class DatabaseConfig(BaseConfig):
    """Database configuration."""

    url: str = "postgresql://user:pass@localhost/flash_arb"
    pool_size: int = 10


class Settings(BaseSettings):
    """Main application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="FLASH_ARB_",
        extra="allow",
    )

    # Base settings
    account_balance: float = 100.0
    mode: str = "simulation"  # simulation or live
    symbols: List[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])

    # Sub-configurations
    trend_analysis: TrendAnalysisConfig = Field(default_factory=TrendAnalysisConfig)
    pin_detection: PinDetectionConfig = Field(default_factory=PinDetectionConfig)
    entry_strategy: EntryStrategyConfig = Field(default_factory=EntryStrategyConfig)
    close_strategy: CloseStrategyConfig = Field(default_factory=CloseStrategyConfig)
    position: PositionConfig = Field(default_factory=PositionConfig)
    risk_control: RiskControlConfig = Field(default_factory=RiskControlConfig)
    coin_screening: CoinScreeningConfig = Field(default_factory=CoinScreeningConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)

    # Exchange configs (loaded separately)
    exchanges_binance: ExchangeConfig = Field(default_factory=lambda: ExchangeConfig(enabled=True))
    exchanges_okx: ExchangeConfig = Field(default_factory=lambda: ExchangeConfig(enabled=True))

    @classmethod
    def from_yaml(cls, config_path: Optional[Path] = None) -> "Settings":
        """Load settings from YAML file and environment variables.

        Args:
            config_path: Path to YAML config file.

        Returns:
            Settings instance.
        """
        yaml_data = load_yaml_config(config_path)

        if yaml_data:
            # Flatten nested dictionaries for Pydantic
            flattened = {}

            # Base settings
            base = yaml_data.get("base", {})
            flattened.update({k: v for k, v in base.items() if k != "exchanges"})

            # Nested configs
            for key in [
                "trend_analysis", "pin_detection", "entry_strategy",
                "close_strategy", "position", "risk_control",
                "coin_screening", "logging", "redis", "database"
            ]:
                if key in yaml_data:
                    flattened[key] = yaml_data[key]

            # Exchange configs
            exchanges = yaml_data.get("exchanges", {})
            if "binance" in exchanges:
                flattened["exchanges_binance"] = ExchangeConfig(**exchanges["binance"])
            if "okx" in exchanges:
                flattened["exchanges_okx"] = ExchangeConfig(**exchanges["okx"])

            return cls(**flattened)

        return cls()


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Returns:
        Settings instance loaded from config file and environment.
    """
    return Settings.from_yaml()
