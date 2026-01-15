"""
多时间框架插针检测模块

检测逻辑：
- 30s K线：连续5根阳线/阴线确认趋势
- 1m/5m K线：颜色反转确认回调
- 15m K线：在近16根高点/低点 + 回落0.15% 确认插针位置
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

import structlog

from .kline_tracker import KlineTracker, Timeframe

logger = structlog.get_logger(__name__)


class PinDirection(str, Enum):
    """插针方向"""
    UP = "UP"       # 上涨（做多机会）
    DOWN = "DOWN"   # 下跌（做空机会）


class PinType(str, Enum):
    """插针类型"""
    UP_PIN = "UP_PIN"       # 上涨插针（做空信号）
    DOWN_PIN = "DOWN_PIN"   # 下跌插针（做多信号）


@dataclass
class PinSignal:
    """插针信号"""
    symbol: str
    pin_type: PinType
    direction: PinDirection
    entry_price: float
    peak_price: float
    start_price: float
    confidence: int
    consecutive_bars: int
    is_at_extreme: bool
    pullback_percent: float
    detected_at: datetime

    def __repr__(self) -> str:
        return (f"PinSignal({self.symbol}, {self.pin_type.value}, "
                f"entry={self.entry_price:.6f}, "
                f"pullback={self.pullback_percent:.2%})")


class MTFPinDetectorConfig:
    """多时间框架检测器配置"""

    TREND_TIMEFRAME: Timeframe = Timeframe.SEC_30
    TREND_CONSECUTIVE_BARS: int = 5
    RETRACEMENT_TIMEFRAMES: list | None = None
    POSITION_TIMEFRAME: Timeframe = Timeframe.MIN_15
    POSITION_BARS_COUNT: int = 16
    POSITION_THRESHOLD: float = 0.0015
    MIN_PULLBACK: float = 0.0015

    def __init__(self):
        if self.RETRACEMENT_TIMEFRAMES is None:
            self.RETRACEMENT_TIMEFRAMES = [Timeframe.MIN_1, Timeframe.MIN_5]


class MTFPinDetector:
    """多时间框架插针检测器"""

    def __init__(
        self,
        symbol: str,
        config: MTFPinDetectorConfig | None = None
    ):
        """初始化检测器

        Args:
            symbol: 交易对符号
            config: 检测器配置
        """
        self.symbol = symbol
        self.config = config or MTFPinDetectorConfig()
        self.last_detection_time: Optional[datetime] = None
        self.detection_cooldown_seconds: int = 60
        self.logger = logger.bind(symbol=symbol)

    def detect(self, tracker: KlineTracker) -> Optional[PinSignal]:
        """检测插针信号"""
        if self.last_detection_time:
            elapsed = (datetime.now() - self.last_detection_time).total_seconds()
            if elapsed < self.detection_cooldown_seconds:
                return None

        if not tracker.is_ready(min_klines=self.config.POSITION_BARS_COUNT):
            return None

        signal = self._detect_up_pin(tracker)
        if signal:
            self.last_detection_time = datetime.now()
            return signal

        signal = self._detect_down_pin(tracker)
        if signal:
            self.last_detection_time = datetime.now()

        return signal

    def _detect_up_pin(self, tracker: KlineTracker) -> Optional[PinSignal]:
        """检测上涨插针（做空机会）"""
        cfg = self.config

        consecutive = tracker.count_consecutive_bullish(cfg.TREND_TIMEFRAME)
        if consecutive < cfg.TREND_CONSECUTIVE_BARS:
            return None

        if tracker.predicting_bullish(cfg.TREND_TIMEFRAME):
            return None
        if tracker.predicting_bullish(Timeframe.MIN_1):
            return None
        if tracker.predicting_bullish(Timeframe.MIN_5):
            return None

        high_15m = tracker.get_high(cfg.POSITION_TIMEFRAME, cfg.POSITION_BARS_COUNT)
        if high_15m == 0:
            return None

        if not tracker.is_at_high(
            cfg.POSITION_TIMEFRAME,
            cfg.POSITION_BARS_COUNT,
            cfg.POSITION_THRESHOLD
        ):
            return None

        pullback = tracker.pullback_from_high(cfg.POSITION_TIMEFRAME, cfg.POSITION_BARS_COUNT)
        if pullback < cfg.MIN_PULLBACK:
            return None

        current_price = tracker.current_price
        signal = PinSignal(
            symbol=self.symbol,
            pin_type=PinType.UP_PIN,
            direction=PinDirection.DOWN,
            entry_price=current_price,
            peak_price=high_15m,
            start_price=tracker.get_close(cfg.POSITION_TIMEFRAME, -cfg.POSITION_BARS_COUNT),
            confidence=self._calculate_confidence(tracker, is_up_pin=True),
            consecutive_bars=consecutive,
            is_at_extreme=True,
            pullback_percent=pullback,
            detected_at=datetime.now()
        )

        self.logger.info(
            "Up pin detected",
            symbol=self.symbol,
            entry_price=current_price,
            peak_price=high_15m,
            pullback=f"{pullback:.2%}",
            confidence=signal.confidence
        )

        return signal

    def _detect_down_pin(self, tracker: KlineTracker) -> Optional[PinSignal]:
        """检测下跌插针（做多机会）"""
        cfg = self.config

        consecutive = tracker.count_consecutive_bearish(cfg.TREND_TIMEFRAME)
        if consecutive < cfg.TREND_CONSECUTIVE_BARS:
            return None

        if tracker.predicting_bearish(cfg.TREND_TIMEFRAME):
            return None
        if tracker.predicting_bearish(Timeframe.MIN_1):
            return None
        if tracker.predicting_bearish(Timeframe.MIN_5):
            return None

        low_15m = tracker.get_low(cfg.POSITION_TIMEFRAME, cfg.POSITION_BARS_COUNT)
        if low_15m == 0:
            return None

        if not tracker.is_at_low(
            cfg.POSITION_TIMEFRAME,
            cfg.POSITION_BARS_COUNT,
            cfg.POSITION_THRESHOLD
        ):
            return None

        bounce = tracker.bounce_from_low(cfg.POSITION_TIMEFRAME, cfg.POSITION_BARS_COUNT)
        if bounce < cfg.MIN_PULLBACK:
            return None

        current_price = tracker.current_price
        signal = PinSignal(
            symbol=self.symbol,
            pin_type=PinType.DOWN_PIN,
            direction=PinDirection.UP,
            entry_price=current_price,
            peak_price=low_15m,
            start_price=tracker.get_close(cfg.POSITION_TIMEFRAME, -cfg.POSITION_BARS_COUNT),
            confidence=self._calculate_confidence(tracker, is_up_pin=False),
            consecutive_bars=consecutive,
            is_at_extreme=True,
            pullback_percent=bounce,
            detected_at=datetime.now()
        )

        self.logger.info(
            "Down pin detected",
            symbol=self.symbol,
            entry_price=current_price,
            peak_price=low_15m,
            bounce=f"{bounce:.2%}",
            confidence=signal.confidence
        )

        return signal

    def _calculate_confidence(self, tracker: KlineTracker, is_up_pin: bool) -> int:
        """计算信号置信度"""
        score = 50

        consecutive = (
            tracker.count_consecutive_bullish(self.config.TREND_TIMEFRAME)
            if is_up_pin
            else tracker.count_consecutive_bearish(self.config.TREND_TIMEFRAME)
        )
        score += min(20, (consecutive - self.config.TREND_CONSECUTIVE_BARS) * 5)

        if is_up_pin:
            pullback = tracker.pullback_from_high(
                self.config.POSITION_TIMEFRAME,
                self.config.POSITION_BARS_COUNT
            )
        else:
            pullback = tracker.bounce_from_low(
                self.config.POSITION_TIMEFRAME,
                self.config.POSITION_BARS_COUNT
            )
        score += min(30, int(pullback * 1000))

        return min(100, max(0, score))


class MTFPinDetectorManager:
    """多交易对检测器管理器"""

    def __init__(self):
        self.detectors: dict = {}
        self.logger = logger.bind(component="MTFPinDetectorManager")

    def get_detector(self, symbol: str) -> MTFPinDetector:
        """获取或创建交易对的检测器"""
        if symbol not in self.detectors:
            self.detectors[symbol] = MTFPinDetector(symbol=symbol)
            self.logger.info("Created MTF pin detector", symbol=symbol)
        return self.detectors[symbol]

    def detect(self, symbol: str, tracker: KlineTracker) -> Optional[PinSignal]:
        """检测交易对的插针信号"""
        detector = self.get_detector(symbol)
        return detector.detect(tracker)

    def remove_detector(self, symbol: str) -> None:
        """移除交易对的检测器"""
        if symbol in self.detectors:
            del self.detectors[symbol]
            self.logger.info("Removed MTF pin detector", symbol=symbol)
