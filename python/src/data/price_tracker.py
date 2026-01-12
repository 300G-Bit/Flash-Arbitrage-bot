"""
Price Tracker Module.

追踪插针信号前后的价格变化，记录多个时间段的价格数据。
"""

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Callable, Deque, Tuple

import structlog

from .signal_recorder import PinSignalRecord

logger = structlog.get_logger(__name__)


# ============== 配置 ==============

TRACKER_CONFIG = {
    "track_duration_seconds": 180,     # 追踪时长（秒）
    "track_pre_seconds": 180,          # 信号前记录时长（秒）
    "track_interval_ms": 100,          # 采样间隔（毫秒）
    "hold_periods": [30, 60, 90, 180], # 测试的持仓时间段
}


# ============== 数据结构 ==============

@dataclass
class PriceSample:
    """价格采样点"""
    timestamp: datetime
    price: float
    volume: float = 0.0


@dataclass
class TrackedSignal:
    """被追踪的信号"""
    record: PinSignalRecord
    start_time: datetime
    target_end_time: datetime

    # 价格历史
    pre_prices: Deque[PriceSample]  # 信号前的价格
    post_prices: Deque[PriceSample]  # 信号后的价格

    # 各时间点的价格
    price_snapshots: Dict[int, float]  # {seconds_after: price}

    # 最佳入场点
    best_entry_price: float = None
    best_entry_time: datetime = None

    # 状态
    is_completed: bool = False


# ============== 价格追踪器 ==============

class PriceTracker:
    """
    价格追踪器

    功能：
    1. 追踪信号前180秒的价格历史
    2. 追踪信号后180秒的价格变化
    3. 在30s/60s/90s/180s时间点采样价格
    4. 计算最佳入场点
    """

    def __init__(self, config: Optional[Dict] = None):
        """初始化追踪器

        Args:
            config: 配置字典
        """
        self.config = {**TRACKER_CONFIG, **(config or {})}

        # 正在追踪的信号
        self._active_signals: Dict[str, TrackedSignal] = {}
        self._lock = threading.Lock()

        # 全局价格缓存（用于获取信号前的价格）
        self._price_cache: Dict[str, Deque[PriceSample]] = {}
        self._cache_lock = threading.Lock()

        # 回调函数
        self._on_track_complete: Optional[Callable[[PinSignalRecord], None]] = None

        self.logger = logger.bind(component="PriceTracker")

    def set_track_complete_callback(
        self,
        callback: Callable[[PinSignalRecord], None]
    ) -> None:
        """设置追踪完成回调"""
        self._on_track_complete = callback

    def update_price(self, symbol: str, price: float, volume: float = 0.0) -> None:
        """更新价格（从WebSocket调用）

        Args:
            symbol: 交易对
            price: 当前价格
            volume: 成交量
        """
        now = datetime.now(timezone.utc)
        sample = PriceSample(timestamp=now, price=price, volume=volume)

        with self._cache_lock:
            if symbol not in self._price_cache:
                max_len = (self.config["track_pre_seconds"] * 1000 //
                          self.config["track_interval_ms"]) + 1000
                self._price_cache[symbol] = deque(maxlen=max_len)

            self._price_cache[symbol].append(sample)

        # 更新所有活跃信号的追踪数据
        self._update_active_signals(symbol, price, now)

        # 清理完成的追踪
        self._cleanup_completed()

    def start_tracking(
        self,
        record: PinSignalRecord,
        pre_prices: List[PriceSample] = None
    ) -> str:
        """开始追踪一个信号

        Args:
            record: 信号记录
            pre_prices: 信号前的价格历史（可选）

        Returns:
            追踪ID（与record.id相同）
        """
        now = datetime.now(timezone.utc)
        duration = timedelta(seconds=self.config["track_duration_seconds"])
        end_time = record.detected_at + duration

        tracked = TrackedSignal(
            record=record,
            start_time=now,
            target_end_time=end_time,
            pre_prices=deque(maxlen=2000),
            post_prices=deque(maxlen=2000),
            price_snapshots={},
        )

        # 填充信号前的价格
        if pre_prices:
            tracked.pre_prices.extend(pre_prices)
        else:
            # 从缓存中获取
            with self._cache_lock:
                if record.symbol in self._price_cache:
                    cutoff_time = record.detected_at - timedelta(
                        seconds=self.config["track_pre_seconds"]
                    )
                    for sample in self._price_cache[record.symbol]:
                        if sample.timestamp >= cutoff_time:
                            tracked.pre_prices.append(sample)

        with self._lock:
            self._active_signals[record.id] = tracked

        self.logger.info(
            "Started tracking signal",
            signal_id=record.id[:8],
            symbol=record.symbol,
            direction=record.direction,
            pre_prices=len(tracked.pre_prices)
        )

        return record.id

    def _update_active_signals(
        self,
        symbol: str,
        price: float,
        timestamp: datetime
    ) -> None:
        """更新活跃信号的追踪数据"""
        with self._lock:
            for signal_id, tracked in list(self._active_signals.items()):
                if tracked.record.symbol != symbol:
                    continue

                if tracked.is_completed:
                    continue

                # 计算距离信号检测的时间差
                elapsed_ms = int(
                    (timestamp - tracked.record.detected_at).total_seconds() * 1000
                )

                # 只记录信号后的价格
                if elapsed_ms >= 0:
                    sample = PriceSample(timestamp=timestamp, price=price)
                    tracked.post_prices.append(sample)

                    # 检查是否到达采样点
                    for period in self.config["hold_periods"]:
                        period_ms = period * 1000
                        # 允许100ms误差
                        if abs(elapsed_ms - period_ms) <= 100:
                            if period not in tracked.price_snapshots:
                                tracked.price_snapshots[period] = price
                                self.logger.debug(
                                    "Price snapshot taken",
                                    signal_id=signal_id[:8],
                                    period=f"{period}s",
                                    price=price
                                )

                    # 更新最佳入场点
                    self._update_best_entry(tracked, price, timestamp, elapsed_ms)

                    # 检查是否完成追踪
                    target_ms = self.config["track_duration_seconds"] * 1000
                    if elapsed_ms >= target_ms:
                        self._complete_tracking(signal_id, tracked)

    def _update_best_entry(
        self,
        tracked: TrackedSignal,
        price: float,
        timestamp: datetime,
        elapsed_ms: int
    ) -> None:
        """更新最佳入场点

        对于向上插针（DOWN方向）：价格从高点回落，入场点是回撤最深的位置
        对于向下插针（UP方向）：价格从低点反弹，入场点是反弹最弱的位置（即最低点）
        """
        record = tracked.record

        if record.direction == "DOWN":
            # 向上插针后做空：等待价格从高点回落
            # 回撤越深，入场越有利
            if price < tracked.record.peak_price:
                if tracked.best_entry_price is None or price < tracked.best_entry_price:
                    tracked.best_entry_price = price
                    tracked.best_entry_time = timestamp

        elif record.direction == "UP":
            # 向下插针后做多：等待价格从低点反弹
            # 反弹前的低点是入场点
            if price > tracked.record.peak_price:
                if tracked.best_entry_price is None or price < tracked.best_entry_price:
                    tracked.best_entry_price = price
                    tracked.best_entry_time = timestamp

    def _complete_tracking(
        self,
        signal_id: str,
        tracked: TrackedSignal
    ) -> None:
        """完成追踪"""
        tracked.is_completed = True

        # 更新记录中的价格数据
        record = tracked.record

        # 信号前的价格
        self._set_pre_price(record, tracked.pre_prices, 30)
        self._set_pre_price(record, tracked.pre_prices, 60)
        self._set_pre_price(record, tracked.pre_prices, 90)
        self._set_pre_price(record, tracked.pre_prices, 180)

        # 信号后的价格
        for period, price in tracked.price_snapshots.items():
            setattr(record, f"price_after_{period}s", price)

        # 最佳入场点
        if tracked.best_entry_price is not None:
            record.best_entry_price = tracked.best_entry_price
            record.best_entry_time = tracked.best_entry_time

        # 价格历史
        record.price_history = [
            {
                "time": s.timestamp.isoformat(),
                "price": s.price,
                "volume": s.volume
            }
            for s in list(tracked.post_prices)
        ]

        self.logger.info(
            "Completed tracking signal",
            signal_id=signal_id[:8],
            snapshots=list(tracked.price_snapshots.keys()),
            best_entry=tracked.best_entry_price
        )

        # 触发回调
        if self._on_track_complete:
            try:
                self._on_track_complete(record)
            except Exception as e:
                self.logger.error(
                    "Callback error",
                    signal_id=signal_id[:8],
                    error=str(e)
                )

    def _set_pre_price(
        self,
        record: PinSignalRecord,
        pre_prices: Deque[PriceSample],
        seconds: int
    ) -> None:
        """设置信号前N秒的价格"""
        if not pre_prices:
            return

        target_time = record.detected_at - timedelta(seconds=seconds)

        # 找最接近的时间点
        closest = None
        min_diff = float('inf')

        for sample in pre_prices:
            diff = abs((sample.timestamp - target_time).total_seconds())
            if diff < min_diff:
                min_diff = diff
                closest = sample.price

        if closest is not None and min_diff <= 5:  # 允许5秒误差
            setattr(record, f"price_before_{seconds}s", closest)

    def _cleanup_completed(self) -> None:
        """清理已完成的追踪"""
        with self._lock:
            completed = [
                sid for sid, tracked in self._active_signals.items()
                if tracked.is_completed
            ]
            for sid in completed:
                del self._active_signals[sid]

    def get_active_count(self) -> int:
        """获取活跃追踪数"""
        with self._lock:
            return len(self._active_signals)

    def stop_tracking(self, signal_id: str) -> bool:
        """停止追踪指定信号

        Args:
            signal_id: 信号ID

        Returns:
            是否成功停止
        """
        with self._lock:
            if signal_id in self._active_signals:
                tracked = self._active_signals[signal_id]
                if not tracked.is_completed:
                    self._complete_tracking(signal_id, tracked)
                del self._active_signals[signal_id]
                return True
            return False

    def stop_all(self) -> int:
        """停止所有追踪

        Returns:
            停止的追踪数
        """
        with self._lock:
            count = len(self._active_signals)
            for signal_id in list(self._active_signals.keys()):
                tracked = self._active_signals[signal_id]
                if not tracked.is_completed:
                    self._complete_tracking(signal_id, tracked)
            self._active_signals.clear()
            return count


# ============== 多交易对价格追踪器 ==============

class MultiSymbolPriceTracker:
    """支持多交易对的价格追踪器"""

    def __init__(self, config: Optional[Dict] = None):
        self.tracker = PriceTracker(config)
        self._symbols: set = set()

    def add_symbol(self, symbol: str) -> None:
        """添加要追踪的交易对"""
        self._symbols.add(symbol)

    def remove_symbol(self, symbol: str) -> None:
        """移除交易对"""
        self._symbols.discard(symbol)

    def update_price(self, symbol: str, price: float, volume: float = 0.0) -> None:
        """更新价格（仅追踪已添加的交易对）"""
        if symbol in self._symbols:
            self.tracker.update_price(symbol, price, volume)

    def start_tracking(self, record: PinSignalRecord) -> str:
        """开始追踪"""
        return self.tracker.start_tracking(record)

    def set_callback(
        self,
        callback: Callable[[PinSignalRecord], None]
    ) -> None:
        """设置追踪完成回调"""
        self.tracker.set_track_complete_callback(callback)

    def stop_all(self) -> int:
        """停止所有追踪"""
        return self.tracker.stop_all()
