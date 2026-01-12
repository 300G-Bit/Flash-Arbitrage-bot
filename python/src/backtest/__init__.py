"""Backtest package for trade simulation."""

from .trade_simulator import (
    TradeSimulator,
    BatchSimulator,
    TradeResult,
    SimulationResult,
    SIMULATOR_CONFIG,
)

__all__ = [
    "TradeSimulator",
    "BatchSimulator",
    "TradeResult",
    "SimulationResult",
    "SIMULATOR_CONFIG",
]
