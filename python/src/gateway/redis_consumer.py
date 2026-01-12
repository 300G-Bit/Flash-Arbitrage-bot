"""
Redis data consumer for Flash Arbitrage Bot.

This module subscribes to Redis channels that receive market data
from the Rust gateway and makes it available to the strategy engine.
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Callable, Any, Dict

import orjson
import structlog
from redis.asyncio import Redis, ConnectionPool

from ..config import get_settings

logger = structlog.get_logger(__name__)


class ExchangeType(str, Enum):
    """Supported exchange types."""

    BINANCE = "binance"
    OKX = "okx"


class DataType(str, Enum):
    """Market data types."""

    AGG_TRADE = "aggTrade"
    KLINE = "kline"
    DEPTH_UPDATE = "depthUpdate"
    BOOK_TICKER = "bookTicker"


@dataclass
class AggTrade:
    """Aggregated trade data."""

    exchange: ExchangeType
    symbol: str
    price: float
    quantity: float
    timestamp: int
    is_buyer_maker: bool
    trade_id: int


@dataclass
class Kline:
    """K-line/candlestick data."""

    exchange: ExchangeType
    symbol: str
    interval: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool


@dataclass
class DepthUpdate:
    """Order book depth update."""

    exchange: ExchangeType
    symbol: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    timestamp: int


@dataclass
class BookTicker:
    """Best bid/ask ticker."""

    exchange: ExchangeType
    symbol: str
    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float
    timestamp: int


MarketEvent = AggTrade | Kline | DepthUpdate | BookTicker


def parse_market_event(data: bytes | str) -> Optional[MarketEvent]:
    """Parse market event from Redis message.

    Args:
        data: Raw JSON data from Redis.

    Returns:
        Parsed market event or None if parsing failed.
    """
    try:
        if isinstance(data, bytes):
            obj = orjson.loads(data)
        else:
            obj = json.loads(data)

        # Determine event type by presence of specific fields
        if "open" in obj and "close" in obj and "high" in obj:
            return Kline(
                exchange=ExchangeType(obj.get("exchange", "binance")),
                symbol=obj["symbol"],
                interval=obj["interval"],
                open_time=obj["open_time"],
                close_time=obj["close_time"],
                open=obj["open"],
                high=obj["high"],
                low=obj["low"],
                close=obj["close"],
                volume=obj["volume"],
                is_closed=obj["is_closed"],
            )
        elif "bids" in obj or "asks" in obj:
            return DepthUpdate(
                exchange=ExchangeType(obj.get("exchange", "binance")),
                symbol=obj["symbol"],
                bids=[tuple(b) for b in obj.get("bids", [])],
                asks=[tuple(a) for a in obj.get("asks", [])],
                timestamp=obj["timestamp"],
            )
        elif "bid_price" in obj:
            return BookTicker(
                exchange=ExchangeType(obj.get("exchange", "binance")),
                symbol=obj["symbol"],
                bid_price=obj["bid_price"],
                bid_qty=obj["bid_qty"],
                ask_price=obj["ask_price"],
                ask_qty=obj["ask_qty"],
                timestamp=obj["timestamp"],
            )
        else:  # Default to agg trade
            return AggTrade(
                exchange=ExchangeType(obj.get("exchange", "binance")),
                symbol=obj["symbol"],
                price=obj["price"],
                quantity=obj["quantity"],
                timestamp=obj["timestamp"],
                is_buyer_maker=obj["is_buyer_maker"],
                trade_id=obj["trade_id"],
            )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("Failed to parse market event", error=str(e))
        return None


class RedisConsumer:
    """Consumer for market data from Redis.

    This class subscribes to Redis channels and delivers market events
    to registered handlers.
    """

    def __init__(
        self,
        redis_url: Optional[str] = None,
        channels: Optional[Dict[str, str]] = None,
    ):
        """Initialize Redis consumer.

        Args:
            redis_url: Redis connection URL. If None, loads from settings.
            channels: Channel name mapping. Keys are data types, values are channel names.
        """
        settings = get_settings()

        self.redis_url = redis_url or settings.redis.url

        self.channels = channels or {
            DataType.AGG_TRADE: settings.redis.channels_tick,
            DataType.KLINE: settings.redis.channels_kline,
            DataType.DEPTH_UPDATE: settings.redis.channels_depth,
            DataType.BOOK_TICKER: settings.redis.channels_ticker,
        }

        self._redis: Optional[Redis] = None
        self._pool: Optional[ConnectionPool] = None
        self._running = False
        self._handlers: Dict[type, list[Callable]] = {}

        # Event queues for each data type
        self._queues: Dict[DataType, asyncio.Queue] = {
            dtype: asyncio.Queue(maxsize=10000) for dtype in DataType
        }

    async def connect(self) -> None:
        """Connect to Redis."""
        logger.info("Connecting to Redis", url=self.redis_url)

        self._pool = ConnectionPool.from_url(
            self.redis_url,
            encoding="utf-8",
            decode_responses=False,
        )

        self._redis = Redis(connection_pool=self._pool)

        # Test connection
        await self._redis.ping()
        logger.info("Connected to Redis")

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        if self._redis:
            await self._redis.close()
        if self._pool:
            await self._pool.disconnect()
        logger.info("Disconnected from Redis")

    def register_handler(self, event_type: type, handler: Callable) -> None:
        """Register a handler for a specific event type.

        Args:
            event_type: The event class (e.g., AggTrade, Kline).
            handler: Async function to call with the event.
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        logger.debug("Registered handler", event_type=event_type.__name__)

    async def subscribe_all(self) -> None:
        """Subscribe to all configured channels."""
        if not self._redis:
            await self.connect()

        channels = [self.channels[dtype] for dtype in DataType]
        pubsub = self._redis.pubsub()

        await pubsub.subscribe(*channels)
        logger.info("Subscribed to channels", channels=channels)

        self._running = True

        # Start listener task
        asyncio.create_task(self._listen(pubsub))

    async def _listen(self, pubsub) -> None:
        """Listen for messages on subscribed channels.

        Args:
            pubsub: Redis pubsub instance.
        """
        logger.info("Listening for market data...")

        async for message in pubsub.listen():
            if not self._running:
                break

            if message["type"] != "message":
                continue

            channel = message["channel"]
            data = message["data"]

            # Determine data type from channel
            dtype = None
            for dt, ch in self.channels.items():
                if ch == channel:
                    dtype = dt
                    break

            if dtype is None:
                continue

            # Parse and queue event
            event = parse_market_event(data)
            if event:
                try:
                    queue = self._queues[dtype]
                    if queue.full():
                        # Remove oldest item
                        queue.get_nowait()
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning("Queue full for data type", dtype=dtype)

    async def get_event(self, dtype: DataType, timeout: float = 1.0) -> Optional[MarketEvent]:
        """Get the next event of a specific type.

        Args:
            dtype: The data type to get.
            timeout: Maximum time to wait in seconds.

        Returns:
            The next event or None if timeout.
        """
        try:
            return await asyncio.wait_for(self._queues[dtype].get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def process_events(self) -> None:
        """Process queued events and deliver to handlers.

        This runs continuously while the consumer is active.
        """
        while self._running:
            for dtype in DataType:
                event = await self.get_event(dtype, timeout=0.1)
                if event:
                    await self._dispatch_event(event)

            await asyncio.sleep(0.01)

    async def _dispatch_event(self, event: MarketEvent) -> None:
        """Dispatch event to registered handlers.

        Args:
            event: The market event to dispatch.
        """
        event_type = type(event)

        for handler in self._handlers.get(event_type, []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                logger.error(
                    "Handler error",
                    handler=handler.__name__,
                    event_type=event_type.__name__,
                    error=str(e),
                )

    def stop(self) -> None:
        """Stop the consumer."""
        self._running = False
        logger.info("Redis consumer stopped")

    @property
    def is_running(self) -> bool:
        """Check if consumer is running."""
        return self._running


async def demo_consumer():
    """Demo function to test Redis consumer."""

    async def handle_agg_trade(event: AggTrade) -> None:
        logger.info(
            "AggTrade",
            symbol=event.symbol,
            price=event.price,
            quantity=event.quantity,
            timestamp=datetime.fromtimestamp(event.timestamp / 1000, tz=timezone.utc),
        )

    async def handle_kline(event: Kline) -> None:
        if event.is_closed:
            logger.info(
                "Kline closed",
                symbol=event.symbol,
                interval=event.interval,
                close=event.close,
                volume=event.volume,
            )

    consumer = RedisConsumer()
    await consumer.connect()
    consumer.register_handler(AggTrade, handle_agg_trade)
    consumer.register_handler(Kline, handle_kline)

    await consumer.subscribe_all()

    # Process events for 30 seconds
    task = asyncio.create_task(consumer.process_events())
    await asyncio.sleep(30)

    consumer.stop()
    task.cancel()
    await consumer.disconnect()


if __name__ == "__main__":
    asyncio.run(demo_consumer())
