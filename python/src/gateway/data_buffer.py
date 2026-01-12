"""
Data buffer management for Flash Arbitrage Bot.

This module provides in-memory buffering for market data with automatic
size management and time-based expiry.
"""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Deque, Dict, List, Optional

import structlog
import numpy as np
import pandas as pd

from .redis_consumer import AggTrade, Kline, DepthUpdate, BookTicker, MarketEvent

logger = structlog.get_logger(__name__)


@dataclass
class SymbolBuffer:
    """Buffer for a single trading pair."""

    symbol: str
    ticks: Deque[AggTrade] = field(default_factory=lambda: deque(maxlen=5000))
    klines: Dict[str, Deque[Kline]] = field(default_factory=dict)
    depth: Optional[DepthUpdate] = None
    ticker: Optional[BookTicker] = None

    # Last update timestamps
    last_tick_time: Optional[datetime] = None
    last_kline_time: Dict[str, datetime] = field(default_factory=dict)
    last_depth_time: Optional[datetime] = None
    last_ticker_time: Optional[datetime] = None

    def __post_init__(self):
        """Initialize kline buffers for all intervals."""
        intervals = ["1m", "5m", "15m", "30m", "1h", "4h"]
        for interval in intervals:
            self.klines[interval] = deque(maxlen=500)

    def add_tick(self, tick: AggTrade) -> None:
        """Add a tick to the buffer."""
        self.ticks.append(tick)
        self.last_tick_time = datetime.now(timezone.utc)

    def add_kline(self, kline: Kline) -> None:
        """Add a kline to the appropriate interval buffer."""
        interval = kline.interval
        if interval not in self.klines:
            self.klines[interval] = deque(maxlen=500)

        # Update existing kline or add new one
        buffer = self.klines[interval]
        if buffer and buffer[-1].open_time == kline.open_time:
            # Update current kline
            buffer[-1] = kline
        else:
            # Add new kline
            buffer.append(kline)

        self.last_kline_time[interval] = datetime.now(timezone.utc)

    def update_depth(self, depth: DepthUpdate) -> None:
        """Update depth snapshot."""
        self.depth = depth
        self.last_depth_time = datetime.now(timezone.utc)

    def update_ticker(self, ticker: BookTicker) -> None:
        """Update ticker snapshot."""
        self.ticker = ticker
        self.last_ticker_time = datetime.now(timezone.utc)

    def get_recent_ticks(self, count: int = 100) -> List[AggTrade]:
        """Get recent ticks."""
        return list(self.ticks)[-count:]

    def get_klines(self, interval: str, count: Optional[int] = None) -> List[Kline]:
        """Get klines for an interval."""
        if interval not in self.klines:
            return []

        klines = list(self.klines[interval])
        if count is not None:
            return klines[-count:]
        return klines

    def get_klines_df(self, interval: str) -> pd.DataFrame:
        """Get klines as a pandas DataFrame."""
        klines = self.get_klines(interval)
        if not klines:
            return pd.DataFrame()

        data = {
            "open_time": [k.open_time for k in klines],
            "close_time": [k.close_time for k in klines],
            "open": [k.open for k in klines],
            "high": [k.high for k in klines],
            "low": [k.low for k in klines],
            "close": [k.close for k in klines],
            "volume": [k.volume for k in klines],
            "is_closed": [k.is_closed for k in klines],
        }

        df = pd.DataFrame(data)
        df["datetime"] = pd.to_datetime(df["open_time"], unit="ms")
        return df

    def get_ticks_df(self) -> pd.DataFrame:
        """Get ticks as a pandas DataFrame."""
        ticks = list(self.ticks)
        if not ticks:
            return pd.DataFrame()

        data = {
            "timestamp": [t.timestamp for t in ticks],
            "price": [t.price for t in ticks],
            "quantity": [t.quantity for t in ticks],
            "is_buyer_maker": [t.is_buyer_maker for t in ticks],
            "trade_id": [t.trade_id for t in ticks],
        }

        df = pd.DataFrame(data)
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    def get_freshness(self, max_age_ms: int = 5000) -> Dict[str, bool]:
        """Check data freshness for each data type.

        Args:
            max_age_ms: Maximum age in milliseconds for data to be fresh.

        Returns:
            Dictionary with freshness status for each data type.
        """
        now = datetime.now(timezone.utc)
        max_age = timedelta(milliseconds=max_age_ms)

        result = {
            "tick": self._is_fresh(self.last_tick_time, now, max_age),
            "depth": self._is_fresh(self.last_depth_time, now, max_age),
            "ticker": self._is_fresh(self.last_ticker_time, now, max_age),
        }

        for interval, last_time in self.last_kline_time.items():
            result[f"kline_{interval}"] = self._is_fresh(last_time, now, max_age)

        return result

    @staticmethod
    def _is_fresh(
        last_time: Optional[datetime], now: datetime, max_age: timedelta
    ) -> bool:
        """Check if data is fresh."""
        if last_time is None:
            return False
        return (now - last_time) <= max_age


class DataBufferManager:
    """Manager for multiple symbol buffers."""

    def __init__(self):
        """Initialize buffer manager."""
        self.buffers: Dict[str, SymbolBuffer] = {}
        self._lock = asyncio.Lock()
        self.logger = structlog.get_logger(__name__)

    async def get_or_create_buffer(self, symbol: str) -> SymbolBuffer:
        """Get or create buffer for a symbol."""
        async with self._lock:
            if symbol not in self.buffers:
                self.buffers[symbol] = SymbolBuffer(symbol=symbol)
                self.logger.debug("Created buffer", symbol=symbol)
            return self.buffers[symbol]

    async def process_event(self, event: MarketEvent) -> None:
        """Process a market event and update buffers.

        Args:
            event: The market event to process.
        """
        symbol = event.symbol  # type: ignore
        buffer = await self.get_or_create_buffer(symbol)

        if isinstance(event, AggTrade):
            buffer.add_tick(event)
        elif isinstance(event, Kline):
            buffer.add_kline(event)
        elif isinstance(event, DepthUpdate):
            buffer.update_depth(event)
        elif isinstance(event, BookTicker):
            buffer.update_ticker(event)

    async def get_buffer(self, symbol: str) -> Optional[SymbolBuffer]:
        """Get buffer for a symbol.

        Args:
            symbol: The trading pair symbol.

        Returns:
            The symbol buffer or None if not found.
        """
        return self.buffers.get(symbol)

    async def get_all_symbols(self) -> List[str]:
        """Get all symbols with buffers."""
        return list(self.buffers.keys())

    async def check_all_freshness(
        self, max_age_ms: int = 5000
    ) -> Dict[str, Dict[str, bool]]:
        """Check freshness for all symbol buffers.

        Args:
            max_age_ms: Maximum age in milliseconds.

        Returns:
            Nested dict of freshness status.
        """
        result = {}
        for symbol, buffer in self.buffers.items():
            result[symbol] = buffer.get_freshness(max_age_ms)
        return result

    async def get_tradeable_symbols(
        self, max_age_ms: int = 5000, require_all: bool = True
    ) -> List[str]:
        """Get symbols with fresh data.

        Args:
            max_age_ms: Maximum age in milliseconds.
            require_all: If True, all data types must be fresh.

        Returns:
            List of tradeable symbols.
        """
        tradeable = []

        freshness = await self.check_all_freshness(max_age_ms)

        for symbol, status in freshness.items():
            if require_all:
                # Check all critical data types
                if status.get("tick") and status.get("ticker"):
                    tradeable.append(symbol)
            else:
                # At least one data type is fresh
                if any(status.values()):
                    tradeable.append(symbol)

        return tradeable

    async def cleanup_stale(self, max_age_ms: int = 60000) -> List[str]:
        """Remove buffers for symbols with stale data.

        Args:
            max_age_ms: Maximum age in milliseconds before cleanup.

        Returns:
            List of removed symbols.
        """
        removed = []
        now = datetime.now(timezone.utc)
        max_age = timedelta(milliseconds=max_age_ms)

        async with self._lock:
            to_remove = []
            for symbol, buffer in self.buffers.items():
                # Check most recent update
                most_recent = None
                for time_field in [
                    buffer.last_tick_time,
                    buffer.last_ticker_time,
                    buffer.last_depth_time,
                ] + list(buffer.last_kline_time.values()):
                    if time_field:
                        if most_recent is None or time_field > most_recent:
                            most_recent = time_field

                if most_recent and (now - most_recent) > max_age:
                    to_remove.append(symbol)

            for symbol in to_remove:
                del self.buffers[symbol]
                removed.append(symbol)
                self.logger.debug("Cleaned up stale buffer", symbol=symbol)

        return removed

    async def get_summary(self) -> Dict[str, any]:
        """Get summary of all buffers.

        Returns:
            Summary dictionary.
        """
        summary = {
            "total_symbols": len(self.buffers),
            "symbols": {},
        }

        for symbol, buffer in self.buffers.items():
            summary["symbols"][symbol] = {
                "tick_count": len(buffer.ticks),
                "kline_intervals": list(buffer.klines.keys()),
                "has_ticker": buffer.ticker is not None,
                "has_depth": buffer.depth is not None,
            }

        return summary
