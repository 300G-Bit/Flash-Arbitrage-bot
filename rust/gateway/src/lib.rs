//! Flash Arbitrage Gateway Library
//!
//! High-performance market data gateway for cryptocurrency exchanges.

pub mod exchange;
pub mod redis_publisher;

pub mod binance;
pub mod okx;

// Re-export commonly used types
pub use exchange::{
    Exchange, ExchangeType, MarketEvent, DataType, KlineInterval,
    AggTrade, Kline, DepthUpdate, BookTicker, Subscription,
};

pub use redis_publisher::{RedisPublisher, RedisConfig};
