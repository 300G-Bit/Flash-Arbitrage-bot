"""
多时间框架K线数据管理模块

提供实时K线数据更新、查询和分析功能，不依赖K线收盘。
"""

from collections import deque
from dataclasses import dataclass
from datetime import timedelta, timezone
from enum import Enum
from typing import Deque, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)


class Timeframe(str, Enum):
    """K线时间周期"""
    SEC_30 = "30s"
    MIN_1 = "1m"
    MIN_5 = "5m"
    MIN_15 = "15m"


# Timeframe duration in milliseconds
TIMEFRAME_MS: dict[Timeframe, int] = {
    Timeframe.SEC_30: 30_000,
    Timeframe.MIN_1: 60_000,
    Timeframe.MIN_5: 300_000,
    Timeframe.MIN_15: 900_000,
}


@dataclass
class Kline:
    """K线数据结构"""
    open: float
    high: float
    low: float
    close: float
    timestamp: int
    volume: float = 0.0
    closed: bool = False

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        """上影线长度"""
        if self.is_bullish:
            return self.high - self.close
        return self.high - self.open

    @property
    def lower_wick(self) -> float:
        """下影线长度"""
        if self.is_bullish:
            return self.open - self.low
        return self.close - self.low

    @property
    def range(self) -> float:
        """K线振幅"""
        return self.high - self.low


@dataclass
class TimeframeData:
    """单个时间框架的K线数据容器"""
    timeframe: Timeframe
    klines: Deque[Kline]
    current_candle: Optional[Kline] = None

    @property
    def last_close(self) -> float:
        if not self.klines:
            return 0.0
        return self.klines[-1].close

    @property
    def last_high(self) -> float:
        if not self.klines:
            return 0.0
        return self.klines[-1].high

    @property
    def last_low(self) -> float:
        if not self.klines:
            return 0.0
        return self.klines[-1].low


class KlineTracker:
    """多时间框架K线追踪器"""

    PRICE_HISTORY_SECONDS = 300  # 保留5分钟价格历史

    def __init__(
        self,
        symbol: str,
        timeframes: List[Timeframe] | None = None,
        max_klines: int = 50
    ):
        """初始化K线追踪器

        Args:
            symbol: 交易对符号
            timeframes: 要追踪的时间框架列表
            max_klines: 每个时间框架保存的最大K线数量
        """
        self.symbol = symbol
        self.timeframes = timeframes or [
            Timeframe.SEC_30,
            Timeframe.MIN_1,
            Timeframe.MIN_5,
            Timeframe.MIN_15,
        ]
        self.max_klines = max_klines

        self.data: Dict[Timeframe, TimeframeData] = {
            tf: TimeframeData(
                timeframe=tf,
                klines=deque(maxlen=max_klines),
                current_candle=None
            )
            for tf in self.timeframes
        }

        self.current_price: float = 0.0
        self.last_update_time: int = 0

        # 价格历史（用于速度计算）
        self.price_history: Dict[int, float] = {}

        self.logger = logger.bind(symbol=symbol)

    def on_price(self, price: float, timestamp: int) -> None:
        """处理实时价格更新"""
        self.current_price = price
        self.last_update_time = timestamp

        # 更新价格历史（清理旧数据）
        cutoff_time = timestamp - (self.PRICE_HISTORY_SECONDS * 1000)
        self.price_history = {
            ts: p for ts, p in self.price_history.items()
            if ts > cutoff_time
        }
        self.price_history[timestamp] = price

        for tf in self.timeframes:
            self._update_timeframe(tf, price, timestamp)

    def _update_timeframe(
        self,
        timeframe: Timeframe,
        price: float,
        timestamp: int
    ) -> None:
        """更新单个时间框架的K线数据"""
        tf_data = self.data[timeframe]
        tf_ms = TIMEFRAME_MS[timeframe]
        candle_start = (timestamp // tf_ms) * tf_ms

        if tf_data.current_candle is None:
            tf_data.current_candle = Kline(
                open=price,
                high=price,
                low=price,
                close=price,
                timestamp=candle_start,
                closed=False
            )
        elif tf_data.current_candle.timestamp != candle_start:
            old_candle = tf_data.current_candle
            old_candle.closed = True
            tf_data.klines.append(old_candle)

            tf_data.current_candle = Kline(
                open=old_candle.close,
                high=max(old_candle.close, price),
                low=min(old_candle.close, price),
                close=price,
                timestamp=candle_start,
                closed=False
            )
        else:
            candle = tf_data.current_candle
            candle.high = max(candle.high, price)
            candle.low = min(candle.low, price)
            candle.close = price

    def get_klines(
        self,
        timeframe: Timeframe,
        count: int = 10,
        include_current: bool = False
    ) -> List[Kline]:
        """获取K线数据

        Args:
            timeframe: 时间框架
            count: 获取数量
            include_current: 是否包含当前正在形成的K线

        Returns:
            K线列表，从旧到新排序
        """
        tf_data = self.data[timeframe]
        result = list(tf_data.klines)

        if include_current and tf_data.current_candle is not None:
            result.append(tf_data.current_candle)

        return result[-count:] if len(result) > count else result

    def get_high(self, timeframe: Timeframe, count: int = 10) -> float:
        """获取最近N根K线的最高价"""
        klines = self.get_klines(timeframe, count, include_current=True)
        if not klines:
            return 0.0
        return max(k.high for k in klines)

    def get_low(self, timeframe: Timeframe, count: int = 10) -> float:
        """获取最近N根K线的最低价"""
        klines = self.get_klines(timeframe, count, include_current=True)
        if not klines:
            return 0.0
        return min(k.low for k in klines)

    def get_close(self, timeframe: Timeframe, index: int = -1) -> float:
        """获取指定索引的收盘价

        Args:
            timeframe: 时间框架
            index: 索引，-1表示最新（包含当前K线）

        Returns:
            收盘价，如果没有数据返回0
        """
        klines = self.get_klines(timeframe, count=abs(index) + 1, include_current=True)
        if not klines:
            return 0.0
        return klines[index].close if index < 0 else klines[index].close

    def predicting_bullish(self, timeframe: Timeframe) -> bool:
        """判断当前K线是否预判为阳线（当前价格 > 上一根收盘价）"""
        tf_data = self.data[timeframe]

        if not tf_data.klines:
            return self.current_price > (tf_data.current_candle.open if tf_data.current_candle else 0)

        return self.current_price > tf_data.klines[-1].close

    def predicting_bearish(self, timeframe: Timeframe) -> bool:
        """判断当前K线是否预判为阴线（当前价格 < 上一根收盘价）"""
        tf_data = self.data[timeframe]

        if not tf_data.klines:
            return self.current_price < (tf_data.current_candle.open if tf_data.current_candle else 0)

        return self.current_price < tf_data.klines[-1].close

    def count_consecutive_bullish(self, timeframe: Timeframe) -> int:
        """统计连续阳线数量"""
        klines = self.get_klines(timeframe, count=self.max_klines, include_current=False)
        if not klines:
            return 0

        count = 0
        for kline in reversed(klines):
            if kline.is_bullish:
                count += 1
            else:
                break
        return count

    def count_consecutive_bearish(self, timeframe: Timeframe) -> int:
        """统计连续阴线数量"""
        klines = self.get_klines(timeframe, count=self.max_klines, include_current=False)
        if not klines:
            return 0

        count = 0
        for kline in reversed(klines):
            if kline.is_bearish:
                count += 1
            else:
                break
        return count

    def is_at_high(self, timeframe: Timeframe, count: int, threshold: float = 0.0015) -> bool:
        """判断当前价格是否在近期高点附近"""
        high = self.get_high(timeframe, count)
        if high == 0:
            return False
        return self.current_price >= high * (1 - threshold)

    def is_at_low(self, timeframe: Timeframe, count: int, threshold: float = 0.0015) -> bool:
        """判断当前价格是否在近期低点附近"""
        low = self.get_low(timeframe, count)
        if low == 0:
            return False
        return self.current_price <= low * (1 + threshold)

    def pullback_from_high(self, timeframe: Timeframe, count: int) -> float:
        """计算从近期高点的回撤百分比"""
        high = self.get_high(timeframe, count)
        if high == 0:
            return 0.0
        return (high - self.current_price) / high

    def bounce_from_low(self, timeframe: Timeframe, count: int) -> float:
        """计算从近期低点的反弹百分比"""
        low = self.get_low(timeframe, count)
        if low == 0:
            return 0.0
        return (self.current_price - low) / low

    def load_historical_klines(
        self,
        timeframe: Timeframe,
        klines: List[List]
    ) -> None:
        """加载历史K线数据

        Binance K线数据格式：
        [0] 开盘时间, [1] 开盘价, [2] 最高价, [3] 最低价, [4] 收盘价,
        [5] 成交量, [6] 成交时间, [7] 成交额, [8] 成交笔数...
        """
        tf_data = self.data[timeframe]
        tf_data.klines.clear()

        for k in klines:
            kline = Kline(
                open=float(k[1]),
                high=float(k[2]),
                low=float(k[3]),
                close=float(k[4]),
                timestamp=int(k[0]),
                volume=float(k[5]),
                closed=True
            )
            tf_data.klines.append(kline)

        self.logger.info(
            "Loaded historical klines",
            timeframe=timeframe.value,
            count=len(klines)
        )

    def is_ready(self, min_klines: int = 16) -> bool:
        """检查数据是否准备好（有足够的K线）

        注意：跳过没有数据的时间框架（如30s在测试网不可用）
        """
        for tf in self.timeframes:
            klines = self.get_klines(tf, include_current=False)
            # 跳过没有数据的时间框架
            if not klines:
                continue
            if len(klines) < min_klines:
                return False
        return True

    def get_price_velocity(
        self,
        window_seconds: int = 60,
        current_timestamp: int | None = None
    ) -> float:
        """计算价格变化速度

        Args:
            window_seconds: 时间窗口（秒）
            current_timestamp: 当前时间戳（毫秒），默认使用last_update_time

        Returns:
            价格变化百分比（正=上涨，负=下跌）
        """
        if current_timestamp is None:
            current_timestamp = self.last_update_time

        cutoff_time = current_timestamp - (window_seconds * 1000)

        # 获取窗口内的价格
        window_prices = [
            p for ts, p in self.price_history.items()
            if ts > cutoff_time
        ]

        if len(window_prices) < 2:
            return 0.0

        start_price = window_prices[0]
        end_price = self.current_price

        return (end_price - start_price) / start_price if start_price > 0 else 0.0

    def get_price_range(
        self,
        window_seconds: int = 60,
        current_timestamp: int | None = None
    ) -> tuple[float, float, float]:
        """获取时间窗口内的价格范围

        Args:
            window_seconds: 时间窗口（秒）
            current_timestamp: 当前时间戳（毫秒）

        Returns:
            (最低价, 最高价, 当前价)
        """
        if current_timestamp is None:
            current_timestamp = self.last_update_time

        cutoff_time = current_timestamp - (window_seconds * 1000)

        window_prices = [
            p for ts, p in self.price_history.items()
            if ts > cutoff_time
        ]

        if not window_prices:
            return self.current_price, self.current_price, self.current_price

        return (
            min(window_prices),
            max(window_prices),
            self.current_price
        )

    def has_long_shadow(
        self,
        timeframe: Timeframe,
        ratio_threshold: float = 2.0
    ) -> bool:
        """检查当前K线是否有长影线

        Args:
            timeframe: 时间框架
            ratio_threshold: 影线/实体比值阈值

        Returns:
            True如果有长影线
        """
        tf_data = self.data.get(timeframe)
        if not tf_data:
            return False

        kline = tf_data.current_candle
        if not kline:
            klines = self.get_klines(timeframe, count=1, include_current=False)
            kline = klines[-1] if klines else None

        if not kline or kline.body == 0:
            return False

        return (
            kline.upper_wick > kline.body * ratio_threshold or
            kline.lower_wick > kline.body * ratio_threshold
        )

    def get_atr_timeframe(self) -> Timeframe:
        """获取用于ATR计算的时间框架（优先1m）"""
        # 优先使用1m，如果不可用则返回其他有数据的时间框架
        for tf in [Timeframe.MIN_1, Timeframe.MIN_5, Timeframe.MIN_15]:
            if self.get_klines(tf, include_current=False):
                return tf
        return Timeframe.MIN_1

    def get_status(self) -> dict:
        """获取追踪器状态"""
        return {
            "symbol": self.symbol,
            "current_price": self.current_price,
            "timeframes": {
                tf.value: {
                    "klines_count": len(tf_data.klines),
                    "last_close": tf_data.last_close,
                    "predicting_bullish": self.predicting_bullish(tf),
                }
                for tf, tf_data in self.data.items()
            }
        }


class KlineTrackerManager:
    """多交易对K线追踪器管理器"""

    def __init__(self, max_klines: int = 50):
        """初始化管理器

        Args:
            max_klines: 每个时间框架保存的最大K线数量
        """
        self.trackers: Dict[str, KlineTracker] = {}
        self.max_klines = max_klines
        self.logger = logger.bind(component="KlineTrackerManager")

    def get_tracker(self, symbol: str) -> KlineTracker:
        """获取或创建交易对的追踪器"""
        if symbol not in self.trackers:
            self.trackers[symbol] = KlineTracker(
                symbol=symbol,
                max_klines=self.max_klines
            )
            self.logger.info("Created kline tracker", symbol=symbol)
        return self.trackers[symbol]

    def on_price(self, symbol: str, price: float, timestamp: int) -> None:
        """处理价格更新"""
        tracker = self.get_tracker(symbol)
        tracker.on_price(price, timestamp)

    def remove_tracker(self, symbol: str) -> None:
        """移除交易对的追踪器"""
        if symbol in self.trackers:
            del self.trackers[symbol]
            self.logger.info("Removed kline tracker", symbol=symbol)
