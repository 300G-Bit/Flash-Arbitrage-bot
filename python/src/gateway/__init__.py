"""Gateway module for Flash Arbitrage Bot.

This module handles data ingestion from Redis and provides buffering
for market data.
"""

from .redis_consumer import (
    RedisConsumer,
    ExchangeType,
    DataType,
    AggTrade,
    Kline,
    DepthUpdate,
    BookTicker,
    MarketEvent,
    parse_market_event,
)

from .data_buffer import (
    DataBufferManager,
    SymbolBuffer,
)

__all__ = [
    "RedisConsumer",
    "ExchangeType",
    "DataType",
    "AggTrade",
    "Kline",
    "DepthUpdate",
    "BookTicker",
    "MarketEvent",
    "parse_market_event",
    "DataBufferManager",
    "SymbolBuffer",
]
