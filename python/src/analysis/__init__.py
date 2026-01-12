"""
Analysis module for Flash Arbitrage Bot.

This module contains engines for market analysis including:
- Trend analysis across multiple timeframes
- Pin bar detection
- Alignment validation between trend and pin bars
"""

from .trend_analyzer import (
    TrendAnalyzer,
    TrendResult,
    TimeFrame,
    TrendDirection,
)
from .pin_detector import (
    PinDetector,
    PinDirection,
    PinType,
    PinSignal,
    TickEvent,
    PIN_BAR_DEFINITION,
    analyze_kline_for_pin,
)

__all__ = [
    "TrendAnalyzer",
    "TrendResult",
    "TimeFrame",
    "TrendDirection",
    "PinDetector",
    "PinDirection",
    "PinType",
    "PinSignal",
    "TickEvent",
    "PIN_BAR_DEFINITION",
    "analyze_kline_for_pin",
]
