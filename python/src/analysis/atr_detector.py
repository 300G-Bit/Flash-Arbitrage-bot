"""
基于ATR的插针检测模块

检测逻辑：
1. 计算ATR(平均真实波幅)作为动态阈值
2. 检测价格在短时间内快速变化（速度检测）
3. K线形态确认（影线、颜色反转、假突破）
4. 生成插针信号
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from .atr_types import ATRMetrics, SpikeDirection, SpikeSignal, SpikeType
from .kline_tracker import Kline, KlineTracker, Timeframe

# 使用统一日志系统
from ..utils.logging_config import get_logger, EventLogger, generate_correlation_id

logger = get_logger(__name__)
events = EventLogger(logger)


class ATRCalculator:
    """ATR(平均真实波幅)计算器"""

    def __init__(self, period: int = 7):
        """初始化ATR计算器

        Args:
            period: ATR周期，默认7（短线敏感）
        """
        self.period = period
        self.tr_values: deque = deque(maxlen=period)
        self.prev_close: float = 0.0
        self.current_atr: float = 0.0
        self.atr_ready: bool = False

    def update(self, kline: Kline) -> float:
        """更新ATR值

        Args:
            kline: K线数据

        Returns:
            当前ATR值
        """
        if kline.closed:
            # 计算真实波幅(TR)
            high_low = kline.high - kline.low
            high_close = abs(kline.high - self.prev_close) if self.prev_close > 0 else 0
            low_close = abs(kline.low - self.prev_close) if self.prev_close > 0 else 0
            tr = max(high_low, high_close, low_close)

            self.tr_values.append(tr)
            self.prev_close = kline.close

            if len(self.tr_values) >= self.period:
                # 使用EMA计算ATR（更平滑）
                if not self.atr_ready:
                    # 首次计算：使用SMA
                    self.current_atr = sum(self.tr_values) / len(self.tr_values)
                    self.atr_ready = True
                else:
                    # 后续使用EMA
                    alpha = 1 / self.period
                    self.current_atr = self.current_atr * (1 - alpha) + tr * alpha

        return self.current_atr

    def get_atr(self) -> float:
        """获取当前ATR值"""
        return self.current_atr

    def is_ready(self) -> bool:
        """ATR是否已准备好（有足够数据）"""
        return self.atr_ready

    def reset(self) -> None:
        """重置计算器"""
        self.tr_values.clear()
        self.prev_close = 0.0
        self.current_atr = 0.0
        self.atr_ready = False


@dataclass
class SpikeDetectorConfig:
    """插针检测器配置"""

    # ATR参数
    atr_period: int = 7                      # ATR周期
    atr_spike_multiplier: float = 0.5        # 速度阈值倍数
    atr_retrace_multiplier: float = 0.3      # 回调阈值倍数

    # 速度检测参数
    detection_window_seconds: int = 60       # 检测窗口（秒）

    # K线确认参数
    shadow_ratio_threshold: float = 2.0      # 影线/实体比值阈值
    false_breakout_threshold: float = 0.002  # 假突破阈值（0.2%）

    # 冷却时间
    detection_cooldown_seconds: int = 30     # 同一币种检测冷却时间

    # 使用的K线周期
    detection_timeframe: Timeframe = Timeframe.MIN_1


class SpikeDetector:
    """基于ATR的插针检测器"""

    def __init__(
        self,
        symbol: str,
        config: SpikeDetectorConfig | None = None
    ):
        """初始化检测器

        Args:
            symbol: 交易对符号
            config: 检测器配置
        """
        self.symbol = symbol
        self.config = config or SpikeDetectorConfig()
        self.atr_calc = ATRCalculator(period=self.config.atr_period)

        self.last_detection_time: Optional[datetime] = None

        # 价格历史（用于速度计算）
        self.price_history: Dict[int, float] = {}

        self.logger = logger.with_data(symbol=symbol)

    def on_price(self, price: float, timestamp: int) -> None:
        """处理价格更新

        Args:
            price: 当前价格
            timestamp: 时间戳（毫秒）
        """
        # 记录价格历史（保留最近5分钟）
        cutoff_time = timestamp - 300000  # 5分钟
        self.price_history = {
            ts: p for ts, p in self.price_history.items()
            if ts > cutoff_time
        }
        self.price_history[timestamp] = price

    def on_kline_close(self, kline: Kline) -> ATRMetrics | None:
        """处理K线收盘，更新ATR

        Args:
            kline: 已收盘的K线

        Returns:
            更新后的ATR指标
        """
        atr_value = self.atr_calc.update(kline)

        if self.atr_calc.is_ready():
            return ATRMetrics(
                period=self.config.atr_period,
                current_value=atr_value,
                spike_threshold=atr_value * self.config.atr_spike_multiplier,
                retrace_threshold=atr_value * self.config.atr_retrace_multiplier
            )
        return None

    def detect(
        self,
        tracker: KlineTracker,
        current_price: float,
        timestamp: int
    ) -> Optional[SpikeSignal]:
        """检测插针信号

        Args:
            tracker: K线追踪器
            current_price: 当前价格
            timestamp: 当前时间戳（毫秒）

        Returns:
            检测到的信号，如果没有则返回None
        """
        # 检查冷却时间
        if self.last_detection_time:
            elapsed = (datetime.now(timezone.utc) - self.last_detection_time).total_seconds()
            if elapsed < self.config.detection_cooldown_seconds:
                return None

        # 检查ATR是否就绪
        if not self.atr_calc.is_ready():
            return None

        # 检查是否有足够的K线数据
        klines = tracker.get_klines(
            self.config.detection_timeframe,
            count=self.config.atr_period,
            include_current=True
        )
        if len(klines) < self.config.atr_period:
            return None

        # 获取ATR指标
        atr = self.atr_calc.get_atr()
        spike_threshold_pct = self.config.atr_spike_multiplier * atr / current_price
        retrace_threshold_pct = self.config.atr_retrace_multiplier * atr / current_price

        # 速度检测
        velocity = self._calculate_velocity(
            current_price,
            timestamp,
            self.config.detection_window_seconds * 1000
        )

        # 调试日志：每30秒输出一次检测状态
        if timestamp % 30000 < 1000:  # 大约每30秒输出一次
            self.logger.debug(
                "Detection status",
                symbol=self.symbol,
                atr=f"{atr:.6f}",
                velocity=f"{velocity:.2%}",
                spike_threshold=f"{spike_threshold_pct:.2%}",
                velocity_pct_of_threshold=f"{abs(velocity)/spike_threshold_pct*100:.1f}%" if spike_threshold_pct > 0 else "N/A"
            )

        if abs(velocity) < spike_threshold_pct:
            return None

        # 检查K线形态
        current_kline = self._get_current_kline(tracker)
        if not current_kline:
            return None

        # 检测上涨插针
        if velocity > spike_threshold_pct:
            signal = self._detect_up_pin(
                tracker, current_price, velocity,
                atr, spike_threshold_pct, retrace_threshold_pct
            )
            if signal:
                self.last_detection_time = datetime.now(timezone.utc)
                return signal

        # 检测下跌插针
        elif velocity < -spike_threshold_pct:
            signal = self._detect_down_pin(
                tracker, current_price, velocity,
                atr, spike_threshold_pct, retrace_threshold_pct
            )
            if signal:
                self.last_detection_time = datetime.now(timezone.utc)
                return signal

        return None

    def _calculate_velocity(
        self,
        current_price: float,
        current_timestamp: int,
        window_ms: int
    ) -> float:
        """计算价格变化速度

        Args:
            current_price: 当前价格
            current_timestamp: 当前时间戳（毫秒）
            window_ms: 时间窗口（毫秒）

        Returns:
            价格变化百分比（正=上涨，负=下跌）
        """
        cutoff_time = current_timestamp - window_ms

        # 找到窗口内的价格范围
        window_prices = [
            p for ts, p in self.price_history.items()
            if ts > cutoff_time
        ]

        if len(window_prices) < 2:
            return 0.0

        # 使用窗口起始价格作为基准
        start_price = window_prices[0]

        # 简单计算：当前价格相对于窗口起始价格的百分比变化
        return (current_price - start_price) / current_price

    def _get_current_kline(self, tracker: KlineTracker) -> Optional[Kline]:
        """获取当前正在形成的K线"""
        tf_data = tracker.data.get(self.config.detection_timeframe)
        if tf_data and tf_data.current_candle:
            return tf_data.current_candle

        # 如果没有当前K线，返回最近的一根
        klines = tracker.get_klines(
            self.config.detection_timeframe,
            count=1,
            include_current=False
        )
        return klines[-1] if klines else None

    def _detect_up_pin(
        self,
        tracker: KlineTracker,
        current_price: float,
        velocity: float,
        atr: float,
        spike_threshold: float,
        retrace_threshold: float
    ) -> Optional[SpikeSignal]:
        """检测上涨插针（做空机会）

        条件：
        1. 价格快速上涨（velocity > spike_threshold）
        2. K线形态确认（满足其一）
        """
        current_kline = self._get_current_kline(tracker)
        if not current_kline:
            return None

        # K线形态确认
        has_long_shadow = current_kline.upper_wick > current_kline.body * self.config.shadow_ratio_threshold
        has_color_reversal = tracker.predicting_bearish(self.config.detection_timeframe)
        has_false_breakout = self._check_false_breakout_up(tracker, current_price)

        if not (has_long_shadow or has_color_reversal or has_false_breakout):
            return None

        # 获取近期高低点
        klines = tracker.get_klines(
            self.config.detection_timeframe,
            count=self.config.atr_period,
            include_current=True
        )
        high_price = max(k.high for k in klines) if klines else current_price
        low_price = min(k.low for k in klines) if klines else current_price
        start_price = klines[0].open if klines else current_price

        # 计算置信度
        confidence = 50
        if has_long_shadow:
            confidence += 15
        if has_color_reversal:
            confidence += 10
        if has_false_breakout:
            confidence += 15
        confidence += min(10, int(velocity / spike_threshold * 10))

        signal = SpikeSignal(
            symbol=self.symbol,
            spike_type=SpikeType.UP_PIN,
            direction=SpikeDirection.DOWN,  # 做空
            entry_price=current_price,
            extreme_price=high_price,
            start_price=start_price,
            confidence=min(100, confidence),
            atr_value=atr,
            spike_threshold=spike_threshold,
            retrace_threshold=retrace_threshold,
            velocity_percent=velocity,
            shadow_ratio=current_kline.upper_wick / max(current_kline.body, 0.0001),
            has_color_reversal=has_color_reversal,
            has_false_breakout=has_false_breakout,
            detected_at=datetime.now(timezone.utc)
        )

        # 记录信号检测事件
        events.log_signal_detected(
            symbol=self.symbol,
            direction="DOWN",  # 做空
            price=current_price,
            atr=atr,
            velocity=velocity,
            confidence=confidence,
            high_price=high_price,
            low_price=low_price,
            start_price=start_price,
            spike_threshold=spike_threshold,
            retrace_threshold=retrace_threshold,
            has_long_shadow=has_long_shadow,
            has_color_reversal=has_color_reversal,
            has_false_breakout=has_false_breakout,
            shadow_ratio=current_kline.upper_wick / max(current_kline.body, 0.0001)
        )

        self.logger.info(
            f"Up pin detected: {self.symbol} entry={current_price:.6f} high={high_price:.6f} "
            f"velocity={velocity:.2%} atr={atr:.6f} conf={confidence}"
        )

        return signal

    def _detect_down_pin(
        self,
        tracker: KlineTracker,
        current_price: float,
        velocity: float,
        atr: float,
        spike_threshold: float,
        retrace_threshold: float
    ) -> Optional[SpikeSignal]:
        """检测下跌插针（做多机会）

        条件：
        1. 价格快速下跌（abs(velocity) > spike_threshold）
        2. K线形态确认（满足其一）
        """
        current_kline = self._get_current_kline(tracker)
        if not current_kline:
            return None

        # K线形态确认
        has_long_shadow = current_kline.lower_wick > current_kline.body * self.config.shadow_ratio_threshold
        has_color_reversal = tracker.predicting_bullish(self.config.detection_timeframe)
        has_false_breakout = self._check_false_breakout_down(tracker, current_price)

        if not (has_long_shadow or has_color_reversal or has_false_breakout):
            return None

        # 获取近期高低点
        klines = tracker.get_klines(
            self.config.detection_timeframe,
            count=self.config.atr_period,
            include_current=True
        )
        high_price = max(k.high for k in klines) if klines else current_price
        low_price = min(k.low for k in klines) if klines else current_price
        start_price = klines[0].open if klines else current_price

        # 计算置信度
        confidence = 50
        if has_long_shadow:
            confidence += 15
        if has_color_reversal:
            confidence += 10
        if has_false_breakout:
            confidence += 15
        confidence += min(10, int(abs(velocity) / spike_threshold * 10))

        signal = SpikeSignal(
            symbol=self.symbol,
            spike_type=SpikeType.DOWN_PIN,
            direction=SpikeDirection.UP,  # 做多
            entry_price=current_price,
            extreme_price=low_price,
            start_price=start_price,
            confidence=min(100, confidence),
            atr_value=atr,
            spike_threshold=spike_threshold,
            retrace_threshold=retrace_threshold,
            velocity_percent=velocity,
            shadow_ratio=current_kline.lower_wick / max(current_kline.body, 0.0001),
            has_color_reversal=has_color_reversal,
            has_false_breakout=has_false_breakout,
            detected_at=datetime.now(timezone.utc)
        )

        # 记录信号检测事件
        events.log_signal_detected(
            symbol=self.symbol,
            direction="UP",  # 做多
            price=current_price,
            atr=atr,
            velocity=velocity,
            confidence=confidence,
            high_price=high_price,
            low_price=low_price,
            start_price=start_price,
            spike_threshold=spike_threshold,
            retrace_threshold=retrace_threshold,
            has_long_shadow=has_long_shadow,
            has_color_reversal=has_color_reversal,
            has_false_breakout=has_false_breakout,
            shadow_ratio=current_kline.lower_wick / max(current_kline.body, 0.0001)
        )

        self.logger.info(
            f"Down pin detected: {self.symbol} entry={current_price:.6f} low={low_price:.6f} "
            f"velocity={velocity:.2%} atr={atr:.6f} conf={confidence}"
        )

        return signal

    def _check_false_breakout_up(self, tracker: KlineTracker, current_price: float) -> bool:
        """检查上涨假突破（突破后快速回落）"""
        klines = tracker.get_klines(
            self.config.detection_timeframe,
            count=3,
            include_current=True
        )
        if len(klines) < 2:
            return False

        # 检查是否突破近期高点后回落
        recent_high = max(k.high for k in klines[:-1])
        return (
            klines[-1].high > recent_high * (1 + self.config.false_breakout_threshold) and
            current_price < klines[-1].high * (1 - self.config.false_breakout_threshold)
        )

    def _check_false_breakout_down(self, tracker: KlineTracker, current_price: float) -> bool:
        """检查下跌假突破（跌破后快速反弹）"""
        klines = tracker.get_klines(
            self.config.detection_timeframe,
            count=3,
            include_current=True
        )
        if len(klines) < 2:
            return False

        # 检查是否跌破近期低点后反弹
        recent_low = min(k.low for k in klines[:-1])
        return (
            klines[-1].low < recent_low * (1 - self.config.false_breakout_threshold) and
            current_price > klines[-1].low * (1 + self.config.false_breakout_threshold)
        )


class SpikeDetectorManager:
    """多交易对插针检测器管理器"""

    def __init__(self, config: SpikeDetectorConfig | None = None):
        """初始化管理器

        Args:
            config: 检测器配置
        """
        self.config = config or SpikeDetectorConfig()
        self.detectors: Dict[str, SpikeDetector] = {}
        self.logger = logger.with_data(component="SpikeDetectorManager")

    def get_detector(self, symbol: str) -> SpikeDetector:
        """获取或创建交易对的检测器"""
        if symbol not in self.detectors:
            self.detectors[symbol] = SpikeDetector(
                symbol=symbol,
                config=self.config
            )
            self.logger.info("Created spike detector", symbol=symbol)
        return self.detectors[symbol]

    def on_price(self, symbol: str, price: float, timestamp: int) -> None:
        """处理价格更新"""
        detector = self.get_detector(symbol)
        detector.on_price(price, timestamp)

    def on_kline_close(self, symbol: str, kline: Kline) -> None:
        """处理K线收盘，更新ATR"""
        detector = self.get_detector(symbol)
        detector.on_kline_close(kline)

    def detect(
        self,
        symbol: str,
        tracker: KlineTracker,
        current_price: float,
        timestamp: int
    ) -> Optional[SpikeSignal]:
        """检测交易对的插针信号"""
        detector = self.get_detector(symbol)
        return detector.detect(tracker, current_price, timestamp)

    def remove_detector(self, symbol: str) -> None:
        """移除交易对的检测器"""
        if symbol in self.detectors:
            del self.detectors[symbol]
            self.logger.info("Removed spike detector", symbol=symbol)
