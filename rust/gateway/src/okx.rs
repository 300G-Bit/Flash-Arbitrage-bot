//! OKX Futures WebSocket implementation
//!
//! This module handles WebSocket connections to OKX Futures
//! and parses incoming market data.

use crate::exchange::{
    Exchange, ExchangeType, MarketEvent, AggTrade, Kline, DepthUpdate, BookTicker,
    Subscription, DataType, KlineInterval,
};
use crate::redis_publisher::RedisPublisher;
use anyhow::{Result, anyhow};
use async_trait::async_trait;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio_tungstenite::{connect_async, tungstenite::Message, WebSocketStream};
use tracing::{debug, error, info, warn};
use url::Url;

/// OKX WebSocket endpoints
pub const OKX_WS_PUBLIC: &str = "wss://ws.okx.com:8443/ws/v5/public";
pub const OKX_WS_DEMO: &str = "wss://wspap.okx.com:8443/ws/v5/public"; // Demo trading

/// OKX-specific WebSocket client
pub struct OkxClient {
    exchange_type: ExchangeType,
    ws_url: String,
    ws: Option<WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>>,
    symbols: Vec<String>,
    redis_publisher: Option<RedisPublisher>,
    connected: bool,
}

impl OkxClient {
    /// Create a new OKX client
    pub fn new(demo_trading: bool) -> Self {
        let exchange_type = ExchangeType::Okx;
        let ws_url = if demo_trading {
            OKX_WS_DEMO.to_string()
        } else {
            OKX_WS_PUBLIC.to_string()
        };

        Self {
            exchange_type,
            ws_url,
            ws: None,
            symbols: Vec::new(),
            redis_publisher: None,
            connected: false,
        }
    }

    /// Set the Redis publisher for forwarding events
    pub fn with_redis_publisher(mut self, publisher: RedisPublisher) -> Self {
        self.redis_publisher = Some(publisher);
        self
    }

    /// Build subscription message for OKX
    fn build_subscription_msg(&self, subscriptions: &[Subscription]) -> Value {
        let mut ops = Vec::new();

        for sub in subscriptions {
            let channel = match sub.data_type {
                DataType::AggTrade => {
                    format!("public-trade:{}", Self::okx_symbol(&sub.symbol))
                }
                DataType::Kline => {
                    let interval = sub.interval.unwrap_or(KlineInterval::OneMinute).as_str();
                    format!("public-candle{}:{}", interval, Self::okx_symbol(&sub.symbol))
                }
                DataType::Depth => {
                    format!("public-books:{}", Self::okx_symbol(&sub.symbol))
                }
                DataType::BookTicker => {
                    format!("public-tickers:{}", Self::okx_symbol(&sub.symbol))
                }
            };

            ops.push(json!({
                "channel": channel,
                "op": "subscribe"
            }));
        }

        json!({ "op": "subscribe", "args": ops })
    }

    /// Convert trading pair to OKX format (e.g., BTCUSDT -> BTC-USDT)
    fn okx_symbol(symbol: &str) -> String {
        // Insert hyphen before USDT
        symbol.replace("USDT", "-USDT")
            .replace("USD", "-USD") // For other USD pairs
    }

    /// Convert OKX symbol back to standard format
    fn standard_symbol(okx_symbol: &str) -> String {
        okx_symbol.replace("-", "")
    }

    /// Parse aggregated trade event from OKX WebSocket message
    fn parse_trade(&self, data: &Value, symbol: &str) -> Result<MarketEvent> {
        let arr = data.get("data").and_then(|d| d.as_array())
            .ok_or_else(|| anyhow!("Missing data array"))?;

        if arr.is_empty() {
            return Err(anyhow!("Empty trade data"));
        }

        let trade = &arr[0];
        let price = trade["px"].as_str().ok_or_else(|| anyhow!("Missing price"))?
            .parse::<f64>()?;
        let quantity = trade["sz"].as_str().ok_or_else(|| anyhow!("Missing quantity"))?
            .parse::<f64>()?;
        let timestamp = trade["ts"].as_i64().ok_or_else(|| anyhow!("Missing timestamp"))?;
        let side = trade["side"].as_str().ok_or_else(|| anyhow!("Missing side"))?;
        // OKX: buy=true means taker was buyer (not buyer maker)
        let is_buyer_maker = side == "sell";

        Ok(MarketEvent::AggTrade(AggTrade {
            exchange: self.exchange_type,
            symbol: Self::standard_symbol(symbol),
            price,
            quantity,
            timestamp,
            is_buyer_maker,
            trade_id: timestamp as u64, // OKX uses timestamp as trade ID
        }))
    }

    /// Parse kline event from OKX WebSocket message
    fn parse_kline(&self, data: &Value, symbol: &str, channel: &str) -> Result<MarketEvent> {
        let arr = data.get("data").and_then(|d| d.as_array())
            .ok_or_else(|| anyhow!("Missing data array"))?;

        if arr.is_empty() {
            return Err(anyhow!("Empty kline data"));
        }

        let candle = &arr[0];

        // Extract interval from channel (e.g., "public-candle1m:BTC-USDT")
        let interval = channel
            .split(':')
            .next()
            .and_then(|c| c.strip_prefix("public-candle"))
            .unwrap_or("1m");

        let open = candle[0].as_str().ok_or_else(|| anyhow!("Missing open"))?
            .parse::<f64>()?;
        let high = candle[1].as_str().ok_or_else(|| anyhow!("Missing high"))?
            .parse::<f64>()?;
        let low = candle[2].as_str().ok_or_else(|| anyhow!("Missing low"))?
            .parse::<f64>()?;
        let close = candle[3].as_str().ok_or_else(|| anyhow!("Missing close"))?
            .parse::<f64>()?;
        let volume = candle[5].as_str().ok_or_else(|| anyhow!("Missing volume"))?
            .parse::<f64>()?;
        let timestamp = candle[0].as_str().ok_or_else(|| anyhow!("Missing timestamp"))?
            .parse::<i64>()?;

        // OKX confirms candle closing
        let confirm = candle.get("confirm").and_then(|c| c.as_bool()).unwrap_or(false);

        Ok(MarketEvent::Kline(Kline {
            exchange: self.exchange_type,
            symbol: Self::standard_symbol(symbol),
            interval: interval.to_string(),
            open_time: timestamp,
            close_time: timestamp + self.interval_ms(interval) - 1,
            open,
            high,
            low,
            close,
            volume,
            is_closed: confirm,
        }))
    }

    /// Get interval duration in milliseconds
    fn interval_ms(&self, interval: &str) -> i64 {
        let num = interval.trim_end_matches('m')
            .trim_end_matches('h')
            .trim_end_matches('d')
            .parse::<u64>()
            .unwrap_or(1);

        let base = if interval.ends_with('m') {
            60_000
        } else if interval.ends_with('h') {
            3_600_000
        } else if interval.ends_with('d') {
            86_400_000
        } else {
            60_000
        };

        num as i64 * base
    }

    /// Parse book ticker event from OKX WebSocket message
    fn parse_ticker(&self, data: &Value, symbol: &str) -> Result<MarketEvent> {
        let arr = data.get("data").and_then(|d| d.as_array())
            .ok_or_else(|| anyhow!("Missing data array"))?;

        if arr.is_empty() {
            return Err(anyhow!("Empty ticker data"));
        }

        let ticker = &arr[0];
        let bid_price = ticker["bidPx"].as_str().ok_or_else(|| anyhow!("Missing bid price"))?
            .parse::<f64>().unwrap_or(0.0);
        let bid_qty = ticker["bidSz"].as_str().ok_or_else(|| anyhow!("Missing bid qty"))?
            .parse::<f64>().unwrap_or(0.0);
        let ask_price = ticker["askPx"].as_str().ok_or_else(|| anyhow!("Missing ask price"))?
            .parse::<f64>().unwrap_or(0.0);
        let ask_qty = ticker["askSz"].as_str().ok_or_else(|| anyhow!("Missing ask qty"))?
            .parse::<f64>().unwrap_or(0.0);
        let timestamp = ticker["ts"].as_i64().ok_or_else(|| anyhow!("Missing timestamp"))?;

        Ok(MarketEvent::BookTicker(BookTicker {
            exchange: self.exchange_type,
            symbol: Self::standard_symbol(symbol),
            bid_price,
            bid_qty,
            ask_price,
            ask_qty,
            timestamp,
        }))
    }

    /// Parse incoming message into a MarketEvent
    fn parse_message(&mut self, msg: &str) -> Result<(MarketEvent, String)> {
        let data: Value = serde_json::from_str(msg)?;

        // Check if this is a subscription confirmation
        if let Some(event) = data.get("event").and_then(|e| e.as_str()) {
            if event == "subscribe" {
                debug!("Subscription confirmed: {:?}", data);
                return Err(anyhow!("Subscription confirmation"));
            }
        }

        let arg = data.get("arg").ok_or_else(|| anyhow!("Missing arg"))?;
        let channel = arg.get("channel")
            .and_then(|c| c.as_str())
            .ok_or_else(|| anyhow!("Missing channel"))?;
        let symbol = arg.get("instId")
            .and_then(|s| s.as_str())
            .ok_or_else(|| anyhow!("Missing instId"))?;

        // Parse based on channel type
        if channel.contains("trade") {
            let event = self.parse_trade(&data, symbol)?;
            Ok((event, symbol.to_string()))
        } else if channel.contains("candle") {
            let event = self.parse_kline(&data, symbol, channel)?;
            Ok((event, symbol.to_string()))
        } else if channel.contains("tickers") {
            let event = self.parse_ticker(&data, symbol)?;
            Ok((event, symbol.to_string()))
        } else if channel.contains("books") {
            // For depth updates, return a simplified version
            let timestamp = data.get("data")
                .and_then(|d| d.get("ts"))
                .and_then(|ts| ts.as_i64())
                .unwrap_or_else(|| chrono::Utc::now().timestamp_millis());

            Ok((MarketEvent::DepthUpdate(DepthUpdate {
                exchange: self.exchange_type,
                symbol: Self::standard_symbol(symbol),
                bids: Vec::new(),
                asks: Vec::new(),
                timestamp,
            }), symbol.to_string()))
        } else {
            Err(anyhow!("Unknown channel: {}", channel))
        }
    }
}

#[async_trait]
impl Exchange for OkxClient {
    fn exchange_type(&self) -> ExchangeType {
        self.exchange_type
    }

    async fn connect(&mut self) -> Result<()> {
        info!("Connecting to OKX WebSocket at {}", self.ws_url);

        let url = Url::parse(&self.ws_url)?;
        let (ws_stream, _) = connect_async(url).await?;

        self.ws = Some(ws_stream);
        self.connected = true;

        info!("Connected to OKX WebSocket");
        Ok(())
    }

    async fn disconnect(&mut self) -> Result<()> {
        if let Some(mut ws) = self.ws.take() {
            ws.close(None).await?;
        }
        self.connected = false;
        info!("Disconnected from OKX");
        Ok(())
    }

    async fn subscribe(&mut self, subscriptions: Vec<Subscription>) -> Result<()> {
        info!("Subscribing to {} OKX data streams", subscriptions.len());

        if !self.connected {
            self.connect().await?;
        }

        let sub_msg = self.build_subscription_msg(&subscriptions);
        let msg_str = serde_json::to_string(&sub_msg)?;

        if let Some(ref mut ws) = self.ws {
            ws.send(Message::Text(msg_str)).await?;
        }

        // Track symbols
        for sub in &subscriptions {
            if !self.symbols.contains(&sub.symbol) {
                self.symbols.push(sub.symbol.clone());
            }
        }

        info!("OKX subscription request sent");
        Ok(())
    }

    async fn unsubscribe(&mut self, _subscriptions: Vec<Subscription>) -> Result<()> {
        warn!("Unsubscribe not fully implemented for OKX");
        Ok(())
    }

    async fn recv_event(&mut self) -> Result<Option<MarketEvent>> {
        if !self.connected || self.ws.is_none() {
            return Ok(None);
        }

        let ws = self.ws.as_mut().unwrap();

        match ws.next().await {
            Some(Ok(Message::Text(text))) => {
                match self.parse_message(&text) {
                    Ok((event, _symbol)) => {
                        // Forward to Redis if configured
                        if let Some(ref mut publisher) = self.redis_publisher {
                            if let Err(e) = publisher.publish_event(&event).await {
                                error!("Failed to publish event to Redis: {}", e);
                            }
                        }
                        Ok(Some(event))
                    }
                    Err(e) => {
                        debug!("Failed to parse OKX message: {}", e);
                        Ok(None)
                    }
                }
            }
            Some(Ok(Message::Ping(payload))) => {
                ws.send(Message::Pong(payload)).await?;
                self.recv_event().await
            }
            Some(Ok(Message::Pong(_))) => {
                self.recv_event().await
            }
            Some(Ok(Message::Close(_))) => {
                self.connected = false;
                Ok(None)
            }
            Some(Err(e)) => {
                error!("OKX WebSocket error: {}", e);
                self.connected = false;
                Err(e.into())
            }
            None => {
                self.connected = false;
                Ok(None)
            }
            _ => Ok(None),
        }
    }

    fn is_connected(&self) -> bool {
        self.connected
    }

    fn ws_endpoint(&self) -> &str {
        &self.ws_url
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_okx_symbol_conversion() {
        assert_eq!(OkxClient::okx_symbol("BTCUSDT"), "BTC-USDT");
        assert_eq!(OkxClient::okx_symbol("ETHUSDT"), "ETH-USDT");
        assert_eq!(OkxClient::standard_symbol("BTC-USDT"), "BTCUSDT");
    }
}
