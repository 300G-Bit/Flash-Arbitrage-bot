"""
Pin Bar Detection Engine.

This module implements the pin bar detection algorithm as specified in the
Flash Arbitrage Bot design document.

Key Features:
- Tick-level velocity calculation
- Detection window (500ms) for tracking price extremes
- Retracement confirmation (30% of pin amplitude)
- Confidence scoring based on velocity, volume, and morphology

Design Document Reference:
- velocity_threshold: 0.003 (0.3%)
- detection_window_ms: 500
- retracement_threshold: 0.3 (30%)
- min_pin_amplitude: 0.002 (0.2%)
- max_pin_amplitude: 0.05 (5%)
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np
import structlog

logger = structlog.get_logger(__name__)


class PinDirection(str, Enum):
    """Pin bar direction."""
    UP = "UP"           # Bullish pin (向下插针后反弹)
    DOWN = "DOWN"       # Bearish pin (向上插针后回落)
    NONE = "NONE"       # No pin detected


class PinType(str, Enum):
    """Pin bar type."""
    # 向上插针：价格快速上涨后回落
    UP_PIN = "UP_PIN"           # Bearish wick
    # 向下插针：价格快速下跌后反弹
    DOWN_PIN = "DOWN_PIN"       # Bullish wick


# ============== 配置参数 (按设计文档) ==============

PIN_BAR_DEFINITION = {
    "velocity_threshold": 0.003,      # 速度阈值（0.3%）
    "detection_window_ms": 500,       # 检测窗口（毫秒）
    "retracement_threshold": 0.3,     # 回撤确认阈值（插针幅度的30%）
    "min_pin_amplitude": 0.002,       # 最小插针幅度（0.2%）
    "max_pin_amplitude": 0.05,        # 最大插针幅度（5%）
}


@dataclass
class TickEvent:
    """Single tick event from exchange."""
    symbol: str
    price: float
    quantity: float
    timestamp_ms: int
    is_buyer_maker: bool = False


@dataclass
class PinMetrics:
    """Metrics calculated within the detection window."""
    window_high: float          # 窗口内最高价
    window_low: float           # 窗口内最低价
    first_price: float          # 窗口起始价格
    last_price: float           # 窗口最新价格
    window_volume: float        # 窗口内成交量
    tick_count: int             # tick数量
    velocity: float             # 价格速度
    acceleration: float         # 价格加速度


@dataclass
class PinSignal:
    """Pin bar detection signal."""
    symbol: str
    pin_type: PinType           # UP_PIN or DOWN_PIN
    direction: PinDirection     # UP (做多机会) or DOWN (做空机会)

    # 价格信息
    peak_price: float           # 插针顶点（向上插针的最高点，向下插针的最低点）
    start_price: float          # 插针前价格
    current_price: float        # 当前价格
    amplitude: float            # 插针幅度（百分比）

    # 确认信息
    is_confirmed: bool          # 是否已回撤确认
    retracement: float          # 实际回撤幅度（百分比）
    retracement_ratio: float    # 回撤比例（相对于插针幅度）

    # 评分
    confidence: int             # 置信度 0-100
    velocity_score: float       # 速度得分
    volume_score: float         # 成交量得分
    morphology_score: float     # 形态得分

    # 时间信息
    peak_time_ms: int           # 顶点时间
    detection_time_ms: int      # 检测时间
    confirmation_time_ms: int   # 确认时间（0表示未确认）

    # 窗口信息
    detection_window_ms: int    # 检测窗口大小
    actual_duration_ms: int     # 实际持续时间


@dataclass
class DetectionWindow:
    """Sliding detection window for tick analysis."""
    ticks: Deque[TickEvent] = field(default_factory=lambda: deque(maxlen=1000))

    # 窗口统计
    window_high: float = 0.0
    window_low: float = float('inf')
    first_price: float = 0.0
    window_volume: float = 0.0
    window_start_ms: int = 0

    # 状态跟踪
    peak_price: float = 0.0
    peak_time_ms: int = 0
    detected_pin_type: Optional[PinType] = None
    pin_start_price: float = 0.0

    # 速度跟踪
    last_velocity: float = 0.0
    last_price: float = 0.0
    last_time_ms: int = 0


class PinDetector:
    """
    Pin bar detection engine.

    Implements the detection algorithm as specified in the design document:
    1. Calculate velocity from tick data
    2. Detect pin bar formation
    3. Verify pin bar morphology
    4. Generate pin bar signal with confidence score
    """

    def __init__(
        self,
        symbol: str,
        config: Optional[Dict] = None
    ):
        """Initialize pin detector for a symbol.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            config: Optional config dict (defaults to PIN_BAR_DEFINITION)
        """
        self.symbol = symbol
        self.config = config or PIN_BAR_DEFINITION.copy()

        # Detection window
        self.window = DetectionWindow()

        # State
        self.detection_cooldown = 0  # 检测冷却（避免重复检测）
        self.last_signal: Optional[PinSignal] = None

        # Statistics
        self.total_detections = 0
        self.confirmed_detections = 0

        self.logger = logger.bind(symbol=symbol)

    def on_tick(self, tick: TickEvent) -> Optional[PinSignal]:
        """Process incoming tick and check for pin bar.

        Args:
            tick: Tick event from exchange

        Returns:
            PinSignal if detected, None otherwise
        """
        # Update detection window
        self._update_window(tick)

        # Check cooldown
        if self.detection_cooldown > 0:
            self.detection_cooldown -= 1
            return None

        # Calculate metrics
        metrics = self._calculate_metrics()

        # Detect pin bar
        pin_signal = self._detect_pin(metrics, tick)

        if pin_signal:
            self.total_detections += 1
            self.detection_cooldown = 100  # 冷却期，避免重复检测

            self.logger.info(
                "Pin bar detected",
                type=pin_signal.pin_type.value,
                direction=pin_signal.direction.value,
                amplitude=f"{pin_signal.amplitude:.2%}",
                confidence=pin_signal.confidence,
                confirmed=pin_signal.is_confirmed
            )

            self.last_signal = pin_signal

        return pin_signal

    def _update_window(self, tick: TickEvent) -> None:
        """Update detection window with new tick."""
        window = self.window
        window_ms = self.config['detection_window_ms']
        current_time = tick.timestamp_ms

        # Initialize or prune old ticks
        if not window.ticks:
            window.window_start_ms = current_time
            window.first_price = tick.price
            window.window_high = tick.price
            window.window_low = tick.price
            window.peak_price = tick.price
            window.peak_time_ms = current_time
            window.pin_start_price = tick.price
        else:
            # Remove ticks outside the detection window
            cutoff_time = current_time - window_ms * 2  # Keep 2x window for confirmation
            while window.ticks and window.ticks[0].timestamp_ms < cutoff_time:
                old = window.ticks.popleft()
                # Update volume
                window.window_volume -= old.quantity

        # Add new tick
        window.ticks.append(tick)
        window.window_volume += tick.quantity

        # Update high/low
        if tick.price > window.window_high:
            window.window_high = tick.price
            window.peak_price = tick.price
            window.peak_time_ms = tick.timestamp_ms

        if tick.price < window.window_low:
            window.window_low = tick.price
            # 对于向下插针，最低点是"顶点"
            if window.window_low < window.pin_start_price * (1 - self.config['velocity_threshold']):
                window.peak_price = tick.price
                window.peak_time_ms = tick.timestamp_ms

        window.last_price = tick.price
        window.last_time_ms = current_time

    def _calculate_metrics(self) -> PinMetrics:
        """Calculate metrics for pin detection."""
        window = self.window

        if not window.ticks:
            return PinMetrics(0, 0, 0, 0, 0, 0, 0, 0)

        # 价格变化
        price_delta = window.window_high - window.window_low
        first_price = window.first_price if window.first_price > 0 else window.ticks[0].price

        # 速度 = 价格变化 / 起始价格
        velocity = price_delta / first_price if first_price > 0 else 0

        # 加速度 = 当前速度 - 上次速度
        acceleration = velocity - window.last_velocity
        window.last_velocity = velocity

        return PinMetrics(
            window_high=window.window_high,
            window_low=window.window_low,
            first_price=first_price,
            last_price=window.last_price,
            window_volume=window.window_volume,
            tick_count=len(window.ticks),
            velocity=velocity,
            acceleration=acceleration
        )

    def _detect_pin(
        self,
        metrics: PinMetrics,
        tick: TickEvent
    ) -> Optional[PinSignal]:
        """Detect pin bar based on metrics."""
        config = self.config
        velocity_threshold = config['velocity_threshold']
        retracement_threshold = config['retracement_threshold']

        current_price = tick.price
        current_time = tick.timestamp_ms

        # ============ 向上插针检测 ============
        # 价格快速上涨后回落
        if (metrics.velocity > velocity_threshold and
            current_price < metrics.window_high):

            # 计算插针幅度
            pin_amplitude = (metrics.window_high - metrics.window_low) / metrics.window_low

            # 检查幅度是否在合理范围内
            if (config['min_pin_amplitude'] <= pin_amplitude <= config['max_pin_amplitude']):

                # 计算回撤
                retracement = (metrics.window_high - current_price) / (metrics.window_high - metrics.window_low)
                is_confirmed = retracement >= retracement_threshold

                # 确认时间
                confirmation_time = current_time if is_confirmed else 0

                # 计算置信度
                confidence = self._calculate_confidence(metrics, PinType.UP_PIN, retracement, is_confirmed)

                # 更新确认计数
                if is_confirmed:
                    self.confirmed_detections += 1

                # 实际持续时间
                duration_ms = current_time - window_start_ms(self.window)

                return PinSignal(
                    symbol=self.symbol,
                    pin_type=PinType.UP_PIN,
                    direction=PinDirection.DOWN,  # 向上插针后做空
                    peak_price=metrics.window_high,
                    start_price=metrics.window_low,
                    current_price=current_price,
                    amplitude=pin_amplitude,
                    is_confirmed=is_confirmed,
                    retracement=retracement,
                    retracement_ratio=retracement,
                    confidence=confidence,
                    velocity_score=0,  # 简化
                    volume_score=0,   # 简化
                    morphology_score=0,  # 简化
                    peak_time_ms=self.window.peak_time_ms,
                    detection_time_ms=current_time,
                    confirmation_time_ms=confirmation_time,
                    detection_window_ms=config['detection_window_ms'],
                    actual_duration_ms=duration_ms
                )

        # ============ 向下插针检测 ============
        # 价格快速下跌后反弹
        if (metrics.velocity < -velocity_threshold and
            current_price > metrics.window_low):

            # 计算插针幅度
            pin_amplitude = (metrics.window_high - metrics.window_low) / metrics.window_low

            # 检查幅度是否在合理范围内
            if (config['min_pin_amplitude'] <= pin_amplitude <= config['max_pin_amplitude']):

                # 计算回撤（反弹）
                retracement = (current_price - metrics.window_low) / (metrics.window_high - metrics.window_low)
                is_confirmed = retracement >= retracement_threshold

                # 确认时间
                confirmation_time = current_time if is_confirmed else 0

                # 计算置信度
                confidence = self._calculate_confidence(metrics, PinType.DOWN_PIN, retracement, is_confirmed)

                # 更新确认计数
                if is_confirmed:
                    self.confirmed_detections += 1

                # 实际持续时间
                duration_ms = current_time - window_start_ms(self.window)

                return PinSignal(
                    symbol=self.symbol,
                    pin_type=PinType.DOWN_PIN,
                    direction=PinDirection.UP,  # 向下插针后做多
                    peak_price=metrics.window_low,
                    start_price=metrics.window_high,
                    current_price=current_price,
                    amplitude=pin_amplitude,
                    is_confirmed=is_confirmed,
                    retracement=retracement,
                    retracement_ratio=retracement,
                    confidence=confidence,
                    velocity_score=0,  # 简化
                    volume_score=0,   # 简化
                    morphology_score=0,  # 简化
                    peak_time_ms=self.window.peak_time_ms,
                    detection_time_ms=current_time,
                    confirmation_time_ms=confirmation_time,
                    detection_window_ms=config['detection_window_ms'],
                    actual_duration_ms=duration_ms
                )

        return None

    def _calculate_confidence(
        self,
        metrics: PinMetrics,
        pin_type: PinType,
        retracement: float,
        is_confirmed: bool
    ) -> int:
        """Calculate confidence score (0-100).

        按设计文档：
        - velocity_score: 最多30分
        - volume_score: 最多30分
        - morphology_score: 最多40分
        """
        velocity = abs(metrics.velocity)

        # ========== 速度得分 (0-30) ==========
        # 速度越快，得分越高
        velocity_score = min(30, velocity / 0.01 * 30)

        # ========== 成交量得分 (0-30) ==========
        # 检测窗口内成交量越大，说明插针越真实
        # 使用tick数量作为成交量代理指标
        expected_ticks = self.config['detection_window_ms'] / 10  # 假设平均10ms一个tick
        volume_ratio = metrics.tick_count / max(1, expected_ticks)
        volume_score = min(30, volume_ratio * 20)

        # ========== 形态得分 (0-40) ==========
        morphology_score = 0

        # 回撤确认奖励 (最多20分)
        if is_confirmed:
            morphology_score += 20
            # 回撤越深，形态越标准
            if retracement > 0.5:
                morphology_score += 10
            elif retracement > 0.4:
                morphology_score += 5
        else:
            # 未确认，形态不完整，扣分
            morphology_score -= 10

        # 插针幅度适中 (最多10分)
        amplitude = (metrics.window_high - metrics.window_low) / metrics.window_low
        if 0.003 <= amplitude <= 0.02:
            morphology_score += 10
        elif 0.002 <= amplitude <= 0.03:
            morphology_score += 5

        # 价格还在合理位置 (最多10分)
        # 如果价格还在顶点附近（回撤<10%），说明插针刚发生，形态清晰
        if pin_type == PinType.UP_PIN:
            price_ratio = (metrics.window_high - metrics.last_price) / (metrics.window_high - metrics.window_low)
        else:
            price_ratio = (metrics.last_price - metrics.window_low) / (metrics.window_high - metrics.window_low)

        if 0.1 < price_ratio < 0.5:  # 回撤10%-50%，形态清晰
            morphology_score += 10
        elif price_ratio <= 0.1:  # 刚发生
            morphology_score += 5

        morphology_score = max(0, min(40, morphology_score))

        # 总分
        total_confidence = int(velocity_score + volume_score + morphology_score)

        return total_confidence

    def check_confirmation(self, current_price: float, current_time_ms: int) -> Optional[PinSignal]:
        """Check if a detected pin has been confirmed by retracement.

        Args:
            current_price: Current market price
            current_time_ms: Current timestamp in milliseconds

        Returns:
            Updated PinSignal if confirmed, None otherwise
        """
        if not self.last_signal or self.last_signal.is_confirmed:
            return None

        signal = self.last_signal
        retracement_threshold = self.config['retracement_threshold']

        # 计算回撤
        if signal.pin_type == PinType.UP_PIN:
            # 向上插针：价格需要从高点回落
            retracement = (signal.peak_price - current_price) / signal.amplitude
            is_confirmed = retracement >= retracement_threshold and current_price < signal.peak_price
        else:
            # 向下插针：价格需要从低点反弹
            retracement = (current_price - signal.peak_price) / signal.amplitude
            is_confirmed = retracement >= retracement_threshold and current_price > signal.peak_price

        if is_confirmed:
            # 更新信号状态
            self.last_signal.is_confirmed = True
            self.last_signal.retracement = retracement
            self.last_signal.retracement_ratio = retracement
            self.last_signal.confirmation_time_ms = current_time_ms
            self.last_signal.current_price = current_price

            self.confirmed_detections += 1

            self.logger.info(
                "Pin bar confirmed",
                type=signal.pin_type.value,
                retracement=f"{retracement:.1%}",
                duration_ms=current_time_ms - signal.detection_time_ms
            )

            return self.last_signal

        return None

    def get_statistics(self) -> Dict:
        """Get detection statistics."""
        confirmation_rate = (
            self.confirmed_detections / self.total_detections * 100
            if self.total_detections > 0 else 0
        )

        return {
            "total_detections": self.total_detections,
            "confirmed_detections": self.confirmed_detections,
            "confirmation_rate": confirmation_rate,
            "last_signal": self.last_signal
        }

    def reset(self) -> None:
        """Reset detector state."""
        self.window = DetectionWindow()
        self.detection_cooldown = 0
        self.last_signal = None


def window_start_ms(window: DetectionWindow) -> int:
    """Get window start time."""
    if window.ticks:
        return window.ticks[0].timestamp_ms
    return 0


# ============== K线分析辅助函数 ==============

def analyze_kline_for_pin(
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    avg_volume: float = 0
) -> Optional[Dict]:
    """Analyze a single kline for pin bar pattern.

    这个函数用于从历史K线数据中检测插针，与实时tick检测互补。

    Returns:
        Dict with pin info if detected, None otherwise
    """
    # 计算K线组成部分
    body_top = max(open_price, close)
    body_bottom = min(open_price, close)
    body_size = body_top - body_bottom

    upper_wick = high - body_top
    lower_wick = body_bottom - low
    total_range = high - low

    if total_range == 0:
        return None

    # 影线比例
    upper_wick_ratio = upper_wick / total_range
    lower_wick_ratio = lower_wick / total_range
    body_ratio = body_size / total_range

    # 成交量倍数
    volume_spike = volume / avg_volume if avg_volume > 0 else 1.0

    pin_info = None

    # 向上插针（射击之星/长上影）
    if (upper_wick_ratio > 0.6 and
        body_ratio < 0.4 and
        upper_wick > lower_wick * 2 and
        upper_wick > body_size * 2):

        amplitude = (high - low) / close
        if PIN_BAR_DEFINITION['min_pin_amplitude'] <= amplitude <= PIN_BAR_DEFINITION['max_pin_amplitude']:
            pin_info = {
                'type': PinType.UP_PIN,
                'direction': PinDirection.DOWN,
                'peak_price': high,
                'start_price': low,
                'current_price': close,
                'amplitude': amplitude,
                'upper_wick_ratio': upper_wick_ratio,
                'lower_wick_ratio': lower_wick_ratio,
                'body_ratio': body_ratio,
                'volume_spike': volume_spike,
            }

    # 向下插针（锤子线/长下影）
    elif (lower_wick_ratio > 0.6 and
          body_ratio < 0.4 and
          lower_wick > upper_wick * 2 and
          lower_wick > body_size * 2):

        amplitude = (high - low) / close
        if PIN_BAR_DEFINITION['min_pin_amplitude'] <= amplitude <= PIN_BAR_DEFINITION['max_pin_amplitude']:
            pin_info = {
                'type': PinType.DOWN_PIN,
                'direction': PinDirection.UP,
                'peak_price': low,
                'start_price': high,
                'current_price': close,
                'amplitude': amplitude,
                'upper_wick_ratio': upper_wick_ratio,
                'lower_wick_ratio': lower_wick_ratio,
                'body_ratio': body_ratio,
                'volume_spike': volume_spike,
            }

    return pin_info
