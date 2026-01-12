//! Binance Futures WebSocket implementation
//!
//! This module handles WebSocket connections to Binance Futures
//! and parses incoming market data.

use crate::exchange::{
    Exchange, ExchangeType, MarketEvent, AggTrade, Kline, DepthUpdate, BookTicker,
    Subscription, DataType, KlineInterval,
};
use crate::redis_publisher::RedisPublisher;
use anyhow::{Result, anyhow};
use async_trait::async_trait;
use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use tokio_tungstenite::{connect_async, tungstenite::Message, WebSocketStream};
use tracing::{debug, error, info, warn};
use url::Url;

/// Binance WebSocket endpoints
pub const BINANCE_FUTURES_WS: &str = "wss://fstream.binance.com/ws";
pub const BINANCE_FUTURES_TESTNET_WS: &str = "wss://stream.binancefuture.com/ws";

/// Binance-specific WebSocket client
pub struct BinanceClient {
    exchange_type: ExchangeType,
    ws_url: String,
    ws: Option<WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>>,
    symbols: Vec<String>,
    redis_publisher: Option<RedisPublisher>,
    connected: bool,
}

impl BinanceClient {
    /// Create a new Binance client
    pub fn new(testnet: bool) -> Self {
        let exchange_type = if testnet {
            ExchangeType::Binance // Still Binance, just testnet
        } else {
            ExchangeType::Binance
        };

        let ws_url = if testnet {
            BINANCE_FUTURES_TESTNET_WS.to_string()
        } else {
            BINANCE_FUTURES_WS.to_string()
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

    /// Build combined stream URL for multiple subscriptions
    fn build_stream_url(&self, subscriptions: &[Subscription]) -> Result<String> {
        if subscriptions.is_empty() {
            return Ok(format!("{}/{}", self.ws_url, ""));
        }

        let mut streams = Vec::new();

        for sub in subscriptions {
            let symbol_lower = sub.symbol.to_lowercase();
            let stream = match sub.data_type {
                DataType::AggTrade => {
                    format!("{}@aggTrade", symbol_lower)
                }
                DataType::Kline => {
                    let interval = sub.interval.unwrap_or(KlineInterval::OneMinute).as_str();
                    format!("{}@kline_{}", symbol_lower, interval)
                }
                DataType::Depth => {
                    format!("{}@depth@100ms", symbol_lower)
                }
                DataType::BookTicker => {
                    format!("{}@bookTicker", symbol_lower)
                }
            };
            streams.push(stream);
        }

        // Combine streams: /stream1/stream2/stream3
        let combined = streams.join("/");
        Ok(format!("{}/{}", self.ws_url, combined))
    }

    /// Parse aggregated trade event from Binance WebSocket message
    fn parse_agg_trade(&self, data: &Value) -> Result<MarketEvent> {
        let price = data["p"].as_str().ok_or_else(|| anyhow!("Missing price"))?
            .parse::<f64>()?;
        let quantity = data["q"].as_str().ok_or_else(|| anyhow!("Missing quantity"))?
            .parse::<f64>()?;
        let symbol = data["s"].as_str().ok_or_else(|| anyhow!("Missing symbol"))?
            .to_string();
        let is_buyer_maker = data["m"].as_bool().ok_or_else(|| anyhow!("Missing is_buyer_maker"))?;
        let trade_id = data["a"].as_u64().ok_or_else(|| anyhow!("Missing trade ID"))?;
        let timestamp = data["T"].as_i64().ok_or_else(|| anyhow!("Missing trade time"))?
            .unwrap_or_else(|| data["E"].as_i64().unwrap_or(0));

        Ok(MarketEvent::AggTrade(AggTrade {
            exchange: self.exchange_type,
            symbol,
            price,
            quantity,
            timestamp,
            is_buyer_maker,
            trade_id,
        }))
    }

    /// Parse kline event from Binance WebSocket message
    fn parse_kline(&self, data: &Value) -> Result<MarketEvent> {
        let k = data.get("k").ok_or_else(|| anyhow!("Missing kline data"))?;

        let symbol = data["s"].as_str().ok_or_else(|| anyhow!("Missing symbol"))?
            .to_string();
        let interval = k["i"].as_str().ok_or_else(|| anyhow!("Missing interval"))?
            .to_string();
        let open_time = k["t"].as_i64().ok_or_else(|| anyhow!("Missing open time"))?;
        let close_time = k["T"].as_i64().ok_or_else(|| anyhow!("Missing close time"))?;
        let open = k["o"].as_str().ok_or_else(|| anyhow!("Missing open"))?
            .parse::<f64>()?;
        let high = k["h"].as_str().ok_or_else(|| anyhow!("Missing high"))?
            .parse::<f64>()?;
        let low = k["l"].as_str().ok_or_else(|| anyhow!("Missing low"))?
            .parse::<f64>()?;
        let close = k["c"].as_str().ok_or_else(|| anyhow!("Missing close"))?
            .parse::<f64>()?;
        let volume = k["v"].as_str().ok_or_else(|| anyhow!("Missing volume"))?
            .parse::<f64>()?;
        let is_closed = k["x"].as_bool().ok_or_else(|| anyhow!("Missing is_closed"))?;

        Ok(MarketEvent::Kline(Kline {
            exchange: self.exchange_type,
            symbol,
            interval,
            open_time,
            close_time,
            open,
            high,
            low,
            close,
            volume,
            is_closed,
        }))
    }

    /// Parse depth update event from Binance WebSocket message
    fn parse_depth_update(&self, data: &Value) -> Result<MarketEvent> {
        let symbol = data["s"].as_str().ok_or_else(|| anyhow!("Missing symbol"))?
            .to_string();
        let timestamp = data["E"].as_i64().ok_or_else(|| anyhow!("Missing event time"))?;

        let mut bids = Vec::new();
        if let Some(b) = data.get("b") {
            if let Some(bid_array) = b.as_array() {
                for bid in bid_array {
                    if let Some(arr) = bid.as_array() {
                        if arr.len() >= 2 {
                            let price = arr[0].as_str().and_then(|s| s.parse::<f64>().ok());
                            let qty = arr[1].as_str().and_then(|s| s.parse::<f64>().ok());
                            if let (Some(p), Some(q)) = (price, qty) {
                                bids.push((p, q));
                            }
                        }
                    }
                }
            }
        }

        let mut asks = Vec::new();
        if let Some(a) = data.get("a") {
            if let Some(ask_array) = a.as_array() {
                for ask in ask_array {
                    if let Some(arr) = ask.as_array() {
                        if arr.len() >= 2 {
                            let price = arr[0].as_str().and_then(|s| s.parse::<f64>().ok());
                            let qty = arr[1].as_str().and_then(|s| s.parse::<f64>().ok());
                            if let (Some(p), Some(q)) = (price, qty) {
                                asks.push((p, q));
                            }
                        }
                    }
                }
            }
        }

        Ok(MarketEvent::DepthUpdate(DepthUpdate {
            exchange: self.exchange_type,
            symbol,
            bids,
            asks,
            timestamp,
        }))
    }

    /// Parse book ticker event from Binance WebSocket message
    fn parse_book_ticker(&self, data: &Value) -> Result<MarketEvent> {
        let symbol = data["s"].as_str().ok_or_else(|| anyhow!("Missing symbol"))?
            .to_string();
        let bid_price = data["b"].as_str().ok_or_else(|| anyhow!("Missing bid price"))?
            .parse::<f64>()?;
        let bid_qty = data["B"].as_str().ok_or_else(|| anyhow!("Missing bid qty"))?
            .parse::<f64>()?;
        let ask_price = data["a"].as_str().ok_or_else(|| anyhow!("Missing ask price"))?
            .parse::<f64>()?;
        let ask_qty = data["A"].as_str().ok_or_else(|| anyhow!("Missing ask qty"))?
            .parse::<f64>()?;
        let timestamp = data.get("E")
            .and_then(|e| e.as_i64())
            .unwrap_or_else(|| chrono::Utc::now().timestamp_millis());

        Ok(MarketEvent::BookTicker(BookTicker {
            exchange: self.exchange_type,
            symbol,
            bid_price,
            bid_qty,
            ask_price,
            ask_qty,
            timestamp,
        }))
    }

    /// Parse incoming message into a MarketEvent
    fn parse_message(&self, msg: &str) -> Result<MarketEvent> {
        let data: Value = serde_json::from_str(msg)?;

        let event_type = data.get("e")
            .and_then(|e| e.as_str())
            .ok_or_else(|| anyhow!("Missing event type"))?;

        match event_type {
            "aggTrade" => self.parse_agg_trade(&data),
            "kline" => self.parse_kline(&data),
            "depthUpdate" => self.parse_depth_update(&data),
            "bookTicker" => self.parse_book_ticker(&data),
            _ => Err(anyhow!("Unknown event type: {}", event_type)),
        }
    }
}

#[async_trait]
impl Exchange for BinanceClient {
    fn exchange_type(&self) -> ExchangeType {
        self.exchange_type
    }

    async fn connect(&mut self) -> Result<()> {
        info!("Connecting to Binance Futures WebSocket at {}", self.ws_url);

        let url = Url::parse(&self.ws_url)?;
        let (ws_stream, _) = connect_async(url).await?;

        self.ws = Some(ws_stream);
        self.connected = true;

        info!("Connected to Binance Futures WebSocket");
        Ok(())
    }

    async fn disconnect(&mut self) -> Result<()> {
        if let Some(mut ws) = self.ws.take() {
            ws.close(None).await?;
        }
        self.connected = false;
        info!("Disconnected from Binance");
        Ok(())
    }

    async fn subscribe(&mut self, subscriptions: Vec<Subscription>) -> Result<()> {
        // For Binance, we need to reconnect with new stream URL
        // This is because Binance uses combined streams
        info!("Subscribing to {} data streams", subscriptions.len());

        if self.connected {
            self.disconnect().await?;
        }

        let stream_url = self.build_stream_url(&subscriptions)?;
        info!("Connecting to stream: {}", stream_url);

        let url = Url::parse(&stream_url)?;
        let (ws_stream, _) = connect_async(url).await?;

        self.ws = Some(ws_stream);
        self.connected = true;

        // Track symbols
        for sub in &subscriptions {
            if !self.symbols.contains(&sub.symbol) {
                self.symbols.push(sub.symbol.clone());
            }
        }

        info!("Successfully subscribed to {} streams", subscriptions.len());
        Ok(())
    }

    async fn unsubscribe(&mut self, _subscriptions: Vec<Subscription>) -> Result<()> {
        // For Binance, we'd need to reconnect with updated stream list
        // This is a simplified implementation
        warn!("Unsubscribe not fully implemented for Binance, requires reconnect");
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
                    Ok(event) => {
                        // Forward to Redis if configured
                        if let Some(ref mut publisher) = self.redis_publisher {
                            if let Err(e) = publisher.publish_event(&event).await {
                                error!("Failed to publish event to Redis: {}", e);
                            }
                        }
                        Ok(Some(event))
                    }
                    Err(e) => {
                        debug!("Failed to parse message: {}", e);
                        Ok(None)
                    }
                }
            }
            Some(Ok(Message::Ping(payload))) => {
                ws.send(Message::Pong(payload)).await?;
                self.recv_event().await // Recursive call to get next message
            }
            Some(Ok(Message::Pong(_))) => {
                self.recv_event().await
            }
            Some(Ok(Message::Close(_))) => {
                self.connected = false;
                Ok(None)
            }
            Some(Err(e)) => {
                error!("WebSocket error: {}", e);
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
    fn test_parse_agg_trade() {
        let client = BinanceClient::new(false);
        let json = r#"{"e":"aggTrade","E":123456789,"s":"BTCUSDT","a":12345,"p":"50000.5","q":"0.001","f":100,"l":200,"T":123456788,"m":true}"#;

        let result = client.parse_message(json);
        assert!(result.is_ok());

        if let Ok(MarketEvent::AggTrade(trade)) = result {
            assert_eq!(trade.symbol, "BTCUSDT");
            assert_eq!(trade.price, 50000.5);
            assert_eq!(trade.quantity, 0.001);
            assert!(trade.is_buyer_maker);
        } else {
            panic!("Expected AggTrade event");
        }
    }

    #[test]
    fn test_parse_book_ticker() {
        let client = BinanceClient::new(false);
        let json = r#"{"u":400900217,"s":"BTCUSDT","b":"25.35190000","B":"31.21000000","a":"25.36520000","A":"40.66000000","T":1234567891,"E":1234567892}"#;

        let result = client.parse_message(json);
        assert!(result.is_ok());

        if let Ok(MarketEvent::BookTicker(ticker)) = result {
            assert_eq!(ticker.symbol, "BTCUSDT");
            assert_eq!(ticker.bid_price, 25.3519);
            assert_eq!(ticker.ask_price, 25.3652);
        } else {
            panic!("Expected BookTicker event");
        }
    }
}
