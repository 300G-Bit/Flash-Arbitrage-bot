//! Exchange abstraction trait for unified WebSocket handling
//!
//! This module defines the common interface that all exchange implementations must follow.

use serde::{Deserialize, Serialize};
use anyhow::Result;

/// Supported exchange types
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum ExchangeType {
    Binance,
    Okx,
}

impl std::fmt::Display for ExchangeType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ExchangeType::Binance => write!(f, "binance"),
            ExchangeType::Okx => write!(f, "okx"),
        }
    }
}

/// Market data types to subscribe
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum DataType {
    AggTrade,      // Aggregate trades
    Kline,         // K-line/candlestick data
    Depth,         // Order book depth
    BookTicker,    // Best bid/ask price
}

impl DataType {
    pub fn as_str(&self) -> &'static str {
        match self {
            DataType::AggTrade => "aggTrade",
            DataType::Kline => "kline",
            DataType::Depth => "depth",
            DataType::BookTicker => "bookTicker",
        }
    }
}

/// K-line time intervals
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum KlineInterval {
    OneMinute,
    FiveMinutes,
    FifteenMinutes,
    ThirtyMinutes,
    OneHour,
    FourHours,
    OneDay,
}

impl KlineInterval {
    pub fn as_str(&self) -> &'static str {
        match self {
            KlineInterval::OneMinute => "1m",
            KlineInterval::FiveMinutes => "5m",
            KlineInterval::FifteenMinutes => "15m",
            KlineInterval::ThirtyMinutes => "30m",
            KlineInterval::OneHour => "1h",
            KlineInterval::FourHours => "4h",
            KlineInterval::OneDay => "1d",
        }
    }
}

/// Aggregated trade data
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AggTrade {
    pub exchange: ExchangeType,
    pub symbol: String,
    pub price: f64,
    pub quantity: f64,
    pub timestamp: i64,
    pub is_buyer_maker: bool,
    pub trade_id: u64,
}

/// K-line/candlestick data
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Kline {
    pub exchange: ExchangeType,
    pub symbol: String,
    pub interval: String,
    pub open_time: i64,
    pub close_time: i64,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
    pub is_closed: bool,
}

/// Order book depth update
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DepthUpdate {
    pub exchange: ExchangeType,
    pub symbol: String,
    pub bids: Vec<(f64, f64)>,  // (price, quantity)
    pub asks: Vec<(f64, f64)>,
    pub timestamp: i64,
}

/// Best bid/ask ticker
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BookTicker {
    pub exchange: ExchangeType,
    pub symbol: String,
    pub bid_price: f64,
    pub bid_qty: f64,
    pub ask_price: f64,
    pub ask_qty: f64,
    pub timestamp: i64,
}

/// Unified market data event
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum MarketEvent {
    AggTrade(AggTrade),
    Kline(Kline),
    DepthUpdate(DepthUpdate),
    BookTicker(BookTicker),
}

impl MarketEvent {
    pub fn exchange(&self) -> ExchangeType {
        match self {
            MarketEvent::AggTrade(t) => t.exchange,
            MarketEvent::Kline(k) => k.exchange,
            MarketEvent::DepthUpdate(d) => d.exchange,
            MarketEvent::BookTicker(b) => b.exchange,
        }
    }

    pub fn symbol(&self) -> &str {
        match self {
            MarketEvent::AggTrade(t) => &t.symbol,
            MarketEvent::Kline(k) => &k.symbol,
            MarketEvent::DepthUpdate(d) => &d.symbol,
            MarketEvent::BookTicker(b) => &b.symbol,
        }
    }

    pub fn event_type(&self) -> DataType {
        match self {
            MarketEvent::AggTrade(_) => DataType::AggTrade,
            MarketEvent::Kline(_) => DataType::Kline,
            MarketEvent::DepthUpdate(_) => DataType::Depth,
            MarketEvent::BookTicker(_) => DataType::BookTicker,
        }
    }
}

/// Subscription request
#[derive(Debug, Clone)]
pub struct Subscription {
    pub symbol: String,
    pub data_type: DataType,
    pub interval: Option<KlineInterval>,
}

/// Exchange trait that all exchange implementations must follow
#[async_trait::async_trait]
pub trait Exchange: Send + Sync {
    /// Returns the exchange type
    fn exchange_type(&self) -> ExchangeType;

    /// Connect to the exchange WebSocket
    async fn connect(&mut self) -> Result<()>;

    /// Disconnect from the exchange
    async fn disconnect(&mut self) -> Result<()>;

    /// Subscribe to market data for given symbols
    async fn subscribe(&mut self, subscriptions: Vec<Subscription>) -> Result<()>;

    /// Unsubscribe from market data
    async fn unsubscribe(&mut self, subscriptions: Vec<Subscription>) -> Result<()>;

    /// Receive the next market event (blocking)
    async fn recv_event(&mut self) -> Result<Option<MarketEvent>>;

    /// Check if the connection is active
    fn is_connected(&self) -> bool;

    /// Get the WebSocket endpoint URL
    fn ws_endpoint(&self) -> &str;
}

/// Result of handling a market event
#[derive(Debug)]
pub enum EventResult {
    /// Event was successfully processed
    Processed,
    /// Event was skipped (filtering logic)
    Skipped,
    /// Event processing failed
    Failed(anyhow::Error),
}
