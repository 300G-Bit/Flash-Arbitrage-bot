"""
Multi-timeframe trend analysis engine.

This module implements trend analysis using:
- EMA (Exponential Moving Average) crossover
- MACD (Moving Average Convergence Divergence)
- Price structure analysis (higher highs/lows, lower highs/lows)
- Momentum analysis

The analyzer provides a comprehensive trend score and direction
by combining multiple indicators across different timeframes.
"""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import structlog

from ..config import get_settings

logger = structlog.get_logger(__name__)


class TrendDirection(str, Enum):
    """Trend direction enum."""

    UP = "UP"
    DOWN = "DOWN"
    SIDEWAYS = "SIDEWAYS"
    UNKNOWN = "UNKNOWN"


@dataclass
class TimeFrame:
    """Timeframe configuration."""

    name: str  # e.g., "1m", "5m", "15m", "1h", "4h"
    minutes: int  # Duration in minutes
    weight: float  # Weight for multi-timeframe analysis
    required: bool = False  # Whether this timeframe is required

    def __post_init__(self):
        """Validate timeframe configuration."""
        if self.weight <= 0:
            raise ValueError("Weight must be positive")


# Standard timeframes used in the system
TIMEFRAMES = {
    "1m": TimeFrame("1m", 1, 0.5, False),
    "5m": TimeFrame("5m", 5, 1.0, True),
    "15m": TimeFrame("15m", 15, 1.5, False),
    "30m": TimeFrame("30m", 30, 2.0, False),
    "1h": TimeFrame("1h", 60, 3.0, False),
    "4h": TimeFrame("4h", 240, 4.0, True),
}


@dataclass
class IndicatorResult:
    """Result from a single indicator analysis."""

    name: str
    direction: TrendDirection
    confidence: float  # 0-1
    value: float  # Raw indicator value


@dataclass
class TrendResult:
    """Complete trend analysis result for a symbol."""

    symbol: str
    timeframe: str
    timestamp: datetime

    # Overall trend
    direction: TrendDirection
    strength: int  # 0-100
    confidence: str  # LOW, MEDIUM, HIGH

    # Individual indicators
    ema_trend: IndicatorResult
    structure_trend: IndicatorResult
    macd_trend: IndicatorResult
    momentum_value: float

    # Key levels
    resistance: float
    support: float
    range_size: float


@dataclass
class MultiTimeFrameAnalysis:
    """Multi-timeframe trend analysis result."""

    symbol: str
    timestamp: datetime

    # Overall results
    overall_direction: TrendDirection
    overall_strength: int
    is_tradeable: bool
    alignment_score: int
    recommendation: str

    # Per-timeframe results
    timeframes: Dict[str, TrendResult]


class TrendAnalyzer:
    """
    Multi-timeframe trend analyzer.

    Analyzes market trends using multiple indicators across different
    timeframes and provides a comprehensive trend assessment.
    """

    def __init__(self, symbol: str):
        """Initialize trend analyzer for a symbol.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT").
        """
        self.symbol = symbol
        self.settings = get_settings()

        # Kline buffers for each timeframe
        self.kline_buffers: Dict[str, Deque[dict]] = {
            tf.name: deque(maxlen=200) for tf in TIMEFRAMES.values()
        }

        # Cache for analysis results
        self._analysis_cache: Dict[str, TrendResult] = {}
        self._last_analysis_time: Dict[str, datetime] = {}

        # EMA periods
        self.ema_fast = self.settings.trend_analysis.ema_fast_period
        self.ema_slow = self.settings.trend_analysis.ema_slow_period

        # MACD periods
        self.macd_fast = self.settings.trend_analysis.macd_fast_period
        self.macd_slow = self.settings.trend_analysis.macd_slow_period
        self.macd_signal = self.settings.trend_analysis.macd_signal_period

        self.logger = logger.bind(symbol=symbol)

    def update_kline(self, kline: dict) -> None:
        """Update kline data for the appropriate timeframe.

        Args:
            kline: Kline data dict with keys: symbol, interval, open_time,
                   open, high, low, close, volume, is_closed
        """
        interval = kline.get("interval")
        if not interval or interval not in self.kline_buffers:
            return

        buffer = self.kline_buffers[interval]

        # Check if updating existing kline or adding new one
        if buffer and buffer[-1]["open_time"] == kline["open_time"]:
            buffer[-1] = kline
        else:
            buffer.append(kline)

    def get_kline_count(self, timeframe: str) -> int:
        """Get number of klines for a timeframe."""
        return len(self.kline_buffers.get(timeframe, []))

    def analyze_timeframe(self, timeframe: str) -> TrendResult:
        """Analyze trend for a single timeframe.

        Args:
            timeframe: Timeframe name (e.g., "5m", "1h", "4h").

        Returns:
            Trend analysis result for the timeframe.
        """
        klines = list(self.kline_buffers.get(timeframe, []))

        now = datetime.now(timezone.utc)

        if len(klines) < 30:
            return self._create_unknown_result(
                timeframe,
                now,
                reason=f"Insufficient data: {len(klines)} < 30"
            )

        # Convert to DataFrame for easier calculation
        df = self._klines_to_df(klines)

        # Run individual analyses
        ema_result = self._analyze_ema(df)
        structure_result = self._analyze_price_structure(df)
        macd_result = self._analyze_macd(df)
        momentum = self._calculate_momentum(df)

        # Combine indicators for final direction
        direction, confidence = self._combine_indicators([
            ema_result.direction,
            structure_result.direction,
            macd_result.direction,
        ])

        # Calculate trend strength
        strength = self._calculate_strength(
            df, direction, ema_result, structure_result, momentum
        )

        # Identify key levels
        resistance, support, range_size = self._identify_key_levels(df, direction)

        result = TrendResult(
            symbol=self.symbol,
            timeframe=timeframe,
            timestamp=now,
            direction=direction,
            strength=strength,
            confidence=confidence,
            ema_trend=ema_result,
            structure_trend=structure_result,
            macd_trend=macd_result,
            momentum_value=momentum,
            resistance=resistance,
            support=support,
            range_size=range_size,
        )

        # Cache result
        self._analysis_cache[timeframe] = result
        self._last_analysis_time[timeframe] = now

        return result

    def analyze_multi_timeframe(self) -> MultiTimeFrameAnalysis:
        """Analyze trend across all timeframes.

        Returns:
            Multi-timeframe analysis result.
        """
        results: Dict[str, TrendResult] = {}

        # Analyze each timeframe
        for tf_name in TIMEFRAMES:
            if self.get_kline_count(tf_name) > 0:
                results[tf_name] = self.analyze_timeframe(tf_name)

        # Get 4H trend as base direction
        tf_4h = results.get("4h")
        if not tf_4h or tf_4h.direction in (TrendDirection.UNKNOWN, TrendDirection.SIDEWAYS):
            return self._create_non_tradeable_result(
                results, "4H趋势不明确，建议观望"
            )

        base_direction = tf_4h.direction

        # Calculate alignment score
        alignment_score = self._calculate_alignment_score(results, base_direction)

        # Calculate overall strength (weighted average)
        overall_strength = self._calculate_weighted_strength(results)

        # Determine if tradeable
        min_alignment = self.settings.trend_analysis.min_alignment_score
        min_strength = self.settings.trend_analysis.min_trend_strength
        is_tradeable = (
            base_direction in (TrendDirection.UP, TrendDirection.DOWN)
            and alignment_score >= min_alignment
            and overall_strength >= min_strength
        )

        # Generate recommendation
        if is_tradeable:
            if alignment_score >= 80:
                recommendation = f"强烈建议交易: 多时间框架高度对齐({base_direction.value})"
            else:
                recommendation = f"可以交易: 趋势{base_direction.value}，注意控制仓位"
        else:
            if alignment_score < min_alignment:
                recommendation = f"建议观望: 对齐度不足({alignment_score}%)"
            else:
                recommendation = f"建议观望: 强度不足({overall_strength})"

        return MultiTimeFrameAnalysis(
            symbol=self.symbol,
            timestamp=datetime.now(timezone.utc),
            overall_direction=base_direction,
            overall_strength=overall_strength,
            is_tradeable=is_tradeable,
            alignment_score=alignment_score,
            recommendation=recommendation,
            timeframes=results,
        )

    def _klines_to_df(self, klines: List[dict]) -> pd.DataFrame:
        """Convert kline list to pandas DataFrame."""
        data = {
            "open_time": [k["open_time"] for k in klines],
            "open": [float(k["open"]) for k in klines],
            "high": [float(k["high"]) for k in klines],
            "low": [float(k["low"]) for k in klines],
            "close": [float(k["close"]) for k in klines],
            "volume": [float(k["volume"]) for k in klines],
        }

        df = pd.DataFrame(data)
        return df

    def _analyze_ema(self, df: pd.DataFrame) -> IndicatorResult:
        """Analyze trend using EMA crossover."""
        closes = df["close"].values

        if len(closes) < self.ema_slow + 5:
            return IndicatorResult("EMA", TrendDirection.SIDEWAYS, 0.0, 0.0)

        # Calculate EMAs
        ema_fast = self._calculate_ema(closes, self.ema_fast)
        ema_slow = self._calculate_ema(closes, self.ema_slow)

        # Get latest values
        fast_latest = ema_fast[-1]
        slow_latest = ema_slow[-1]

        # Calculate EMA slope (rate of change)
        if len(ema_fast) >= 5:
            ema_slope = (ema_fast[-1] - ema_fast[-5]) / ema_fast[-5]
        else:
            ema_slope = 0.0

        # Determine direction (slope threshold: 0.05% for stronger sensitivity)
        slope_threshold = 0.0005  # Reduced from 0.001 for better detection

        if fast_latest > slow_latest and ema_slope > slope_threshold:
            direction = TrendDirection.UP
            confidence = min(1.0, abs(ema_slope) * 200)  # Increased confidence multiplier
        elif fast_latest < slow_latest and ema_slope < -slope_threshold:
            direction = TrendDirection.DOWN
            confidence = min(1.0, abs(ema_slope) * 200)
        else:
            # Still check for crossover even if slope is weak
            if fast_latest > slow_latest:
                direction = TrendDirection.UP
                confidence = 0.4  # Lower confidence for weak signal
            elif fast_latest < slow_latest:
                direction = TrendDirection.DOWN
                confidence = 0.4
            else:
                direction = TrendDirection.SIDEWAYS
                confidence = 0.0

        return IndicatorResult(
            name="EMA",
            direction=direction,
            confidence=confidence,
            value=ema_slope
        )

    def _analyze_price_structure(self, df: pd.DataFrame) -> IndicatorResult:
        """Analyze trend using price structure (HH/HL or LH/LL)."""
        highs = df["high"].values
        lows = df["low"].values

        if len(highs) < 10:
            return IndicatorResult("Structure", TrendDirection.SIDEWAYS, 0.0, 0.0)

        # Find swing highs and lows
        swing_highs = self._find_swing_points(highs, is_high=True, lookback=3)
        swing_lows = self._find_swing_points(lows, is_high=False, lookback=3)

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return IndicatorResult("Structure", TrendDirection.SIDEWAYS, 0.0, 0.0)

        # Check for higher highs and higher lows (uptrend)
        latest_hh = swing_highs[-1] > swing_highs[-2]
        latest_hl = swing_lows[-1] > swing_lows[-2]

        # Check for lower lows and lower highs (downtrend)
        latest_ll = swing_lows[-1] < swing_lows[-2]
        latest_lh = swing_highs[-1] < swing_highs[-2]

        if latest_hh and latest_hl:
            direction = TrendDirection.UP
            confidence = 0.7
        elif latest_ll and latest_lh:
            direction = TrendDirection.DOWN
            confidence = 0.7
        else:
            direction = TrendDirection.SIDEWAYS
            confidence = 0.0

        return IndicatorResult(
            name="Structure",
            direction=direction,
            confidence=confidence,
            value=1.0 if direction == TrendDirection.UP else (-1.0 if direction == TrendDirection.DOWN else 0.0)
        )

    def _analyze_macd(self, df: pd.DataFrame) -> IndicatorResult:
        """Analyze trend using MACD."""
        closes = df["close"].values

        if len(closes) < self.macd_slow + self.macd_signal:
            return IndicatorResult("MACD", TrendDirection.SIDEWAYS, 0.0, 0.0)

        # Calculate MACD
        macd_line, signal_line, histogram = self._calculate_macd(closes)

        if len(macd_line) < 2:
            return IndicatorResult("MACD", TrendDirection.SIDEWAYS, 0.0, 0.0)

        # Get latest values
        macd_latest = macd_line[-1]
        signal_latest = signal_line[-1]
        hist_latest = histogram[-1]

        # Determine direction
        if macd_latest > 0 and macd_latest > signal_latest:
            direction = TrendDirection.UP
            confidence = min(1.0, abs(hist_latest) * 10)
        elif macd_latest < 0 and macd_latest < signal_latest:
            direction = TrendDirection.DOWN
            confidence = min(1.0, abs(hist_latest) * 10)
        else:
            direction = TrendDirection.SIDEWAYS
            confidence = 0.0

        return IndicatorResult(
            name="MACD",
            direction=direction,
            confidence=confidence,
            value=hist_latest
        )

    def _calculate_momentum(self, df: pd.DataFrame) -> float:
        """Calculate momentum (rate of change)."""
        closes = df["close"].values

        if len(closes) < 14:
            return 0.0

        # 14-period Rate of Change
        roc = (closes[-1] - closes[-14]) / closes[-14] * 100
        return roc

    def _calculate_strength(
        self,
        df: pd.DataFrame,
        direction: TrendDirection,
        ema_result: IndicatorResult,
        structure_result: IndicatorResult,
        momentum: float
    ) -> int:
        """Calculate overall trend strength (0-100)."""
        strength = 0

        # Factor 1: Direction consistency (30 points)
        if ema_result.direction == direction:
            strength += 15
        if structure_result.direction == direction:
            strength += 15

        # Factor 2: Momentum strength (25 points)
        if (direction == TrendDirection.UP and momentum > 0) or \
           (direction == TrendDirection.DOWN and momentum < 0):
            momentum_score = min(25, abs(momentum) * 5)
            strength += momentum_score

        # Factor 3: Consecutive bars in direction (25 points)
        consecutive = self._count_consecutive_bars(df, direction)
        strength += min(25, consecutive * 5)

        # Factor 4: Average body size (20 points)
        avg_body_ratio = self._calculate_avg_body_ratio(df.tail(20))
        strength += min(20, avg_body_ratio * 40)

        return int(min(100, max(0, strength)))

    def _identify_key_levels(
        self, df: pd.DataFrame, direction: TrendDirection
    ) -> Tuple[float, float, float]:
        """Identify key support and resistance levels."""
        recent_highs = df["high"].tail(20).values
        recent_lows = df["low"].tail(20).values

        if direction == TrendDirection.UP:
            support = recent_lows.min()
            resistance = recent_highs.max()
        elif direction == TrendDirection.DOWN:
            support = recent_lows.min()
            resistance = recent_highs.max()
        else:
            support = recent_lows.min()
            resistance = recent_highs.max()

        range_size = resistance - support
        return resistance, support, range_size

    @staticmethod
    def _calculate_ema(data: np.ndarray, period: int) -> np.ndarray:
        """Calculate EMA."""
        if len(data) < period:
            return np.array([])

        alpha = 2 / (period + 1)
        ema = np.zeros(len(data))
        ema[0] = data[0]

        for i in range(1, len(data)):
            ema[i] = alpha * data[i] + (1 - alpha) * ema[i-1]

        return ema

    @staticmethod
    def _calculate_macd(
        closes: np.ndarray,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Calculate MACD indicator."""
        ema_fast = TrendAnalyzer._calculate_ema(closes, fast)
        ema_slow = TrendAnalyzer._calculate_ema(closes, slow)

        # Align arrays
        min_len = min(len(ema_fast), len(ema_slow))
        ema_fast = ema_fast[-min_len:]
        ema_slow = ema_slow[-min_len:]

        macd_line = ema_fast - ema_slow
        signal_line = TrendAnalyzer._calculate_ema(macd_line, signal)

        # Align signal line
        min_len = min(len(macd_line), len(signal_line))
        macd_line = macd_line[-min_len:]
        signal_line = signal_line[-min_len:]

        histogram = macd_line - signal_line

        return macd_line, signal_line, histogram

    @staticmethod
    def _find_swing_points(
        prices: np.ndarray, is_high: bool, lookback: int = 3
    ) -> np.ndarray:
        """Find swing high or low points."""
        swings = []

        for i in range(lookback, len(prices) - lookback):
            window = prices[i - lookback:i + lookback + 1]

            if is_high:
                if prices[i] == window.max():
                    swings.append(prices[i])
            else:
                if prices[i] == window.min():
                    swings.append(prices[i])

        return np.array(swings)

    @staticmethod
    def _count_consecutive_bars(df: pd.DataFrame, direction: TrendDirection) -> int:
        """Count consecutive bars in the given direction."""
        count = 0

        for _, row in df.tail(20).iterrows():
            if direction == TrendDirection.UP and row["close"] > row["open"]:
                count += 1
            elif direction == TrendDirection.DOWN and row["close"] < row["open"]:
                count += 1
            else:
                break

        return count

    @staticmethod
    def _calculate_avg_body_ratio(df: pd.DataFrame) -> float:
        """Calculate average candle body ratio."""
        if df.empty:
            return 0.0

        body_ratios = []

        for _, row in df.iterrows():
            full_range = row["high"] - row["low"]
            body = abs(row["close"] - row["open"])

            if full_range > 0:
                body_ratios.append(body / full_range)

        return np.mean(body_ratios) if body_ratios else 0.0

    def _combine_indicators(
        self, directions: List[TrendDirection]
    ) -> Tuple[TrendDirection, str]:
        """Combine multiple indicator directions."""
        votes = {d: 0 for d in TrendDirection}

        for d in directions:
            votes[d] = votes.get(d, 0) + 1

        max_votes = max(votes.values())

        # Determine final direction
        if votes[TrendDirection.UP] == max_votes and max_votes >= 2:
            direction = TrendDirection.UP
        elif votes[TrendDirection.DOWN] == max_votes and max_votes >= 2:
            direction = TrendDirection.DOWN
        else:
            direction = TrendDirection.SIDEWAYS

        # Determine confidence
        if max_votes >= 3:
            confidence = "HIGH"
        elif max_votes >= 2:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        return direction, confidence

    def _calculate_alignment_score(
        self, results: Dict[str, TrendResult], base_direction: TrendDirection
    ) -> int:
        """Calculate multi-timeframe alignment score."""
        if not results:
            return 0

        total_weight = 0.0
        aligned_weight = 0.0

        for tf_name, result in results.items():
            tf = TIMEFRAMES.get(tf_name)
            if not tf:
                continue

            total_weight += tf.weight

            if result.direction == base_direction:
                aligned_weight += tf.weight

        if total_weight == 0:
            return 0

        return int((aligned_weight / total_weight) * 100)

    def _calculate_weighted_strength(self, results: Dict[str, TrendResult]) -> int:
        """Calculate weighted average strength across timeframes."""
        if not results:
            return 0

        total_weight = 0.0
        weighted_strength = 0.0

        for tf_name, result in results.items():
            tf = TIMEFRAMES.get(tf_name)
            if not tf:
                continue

            total_weight += tf.weight
            weighted_strength += result.strength * tf.weight

        if total_weight == 0:
            return 0

        return int(weighted_strength / total_weight)

    def _create_unknown_result(
        self, timeframe: str, timestamp: datetime, reason: str
    ) -> TrendResult:
        """Create a result indicating insufficient data."""
        return TrendResult(
            symbol=self.symbol,
            timeframe=timeframe,
            timestamp=timestamp,
            direction=TrendDirection.UNKNOWN,
            strength=0,
            confidence="LOW",
            ema_trend=IndicatorResult("EMA", TrendDirection.UNKNOWN, 0.0, 0.0),
            structure_trend=IndicatorResult("Structure", TrendDirection.UNKNOWN, 0.0, 0.0),
            macd_trend=IndicatorResult("MACD", TrendDirection.UNKNOWN, 0.0, 0.0),
            momentum_value=0.0,
            resistance=0.0,
            support=0.0,
            range_size=0.0,
        )

    def _create_non_tradeable_result(
        self, results: Dict[str, TrendResult], reason: str
    ) -> MultiTimeFrameAnalysis:
        """Create a non-tradeable analysis result."""
        return MultiTimeFrameAnalysis(
            symbol=self.symbol,
            timestamp=datetime.now(timezone.utc),
            overall_direction=TrendDirection.SIDEWAYS,
            overall_strength=0,
            is_tradeable=False,
            alignment_score=0,
            recommendation=reason,
            timeframes=results,
        )
