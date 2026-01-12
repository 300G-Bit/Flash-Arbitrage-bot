//! Redis publisher for distributing market data
//!
//! This module handles publishing market events to Redis channels
//! for consumption by the Python strategy engine.

use crate::exchange::{MarketEvent, DataType, ExchangeType};
use anyhow::Result;
use redis::{AsyncCommands, Client, ConnectionManager};
use serde_json::to_string;
use tracing::{debug, error, info};

/// Redis channel names
pub const CHANNEL_TICK: &str = "flash_arb:tick";
pub const CHANNEL_KLINE: &str = "flash_arb:kline";
pub const CHANNEL_DEPTH: &str = "flash_arb:depth";
pub const CHANNEL_TICKER: &str = "flash_arb:ticker";

/// Configuration for Redis connection
#[derive(Debug, Clone)]
pub struct RedisConfig {
    pub url: String,
}

impl Default for RedisConfig {
    fn default() -> Self {
        Self {
            url: "redis://127.0.0.1:6379".to_string(),
        }
    }
}

/// Redis publisher for market data
pub struct RedisPublisher {
    client: Client,
    conn: ConnectionManager,
}

impl RedisPublisher {
    /// Create a new Redis publisher
    pub async fn new(config: RedisConfig) -> Result<Self> {
        info!("Connecting to Redis at {}", config.url);

        let client = Client::open(config.url)?;
        let conn = ConnectionManager::new(client.clone()).await?;

        info!("Connected to Redis successfully");

        Ok(Self { client, conn })
    }

    /// Publish a market event to the appropriate channel
    pub async fn publish_event(&mut self, event: &MarketEvent) -> Result<()> {
        let (channel, payload) = self.prepare_event(event)?;

        debug!("Publishing to {}: {}", channel, payload);

        self.conn
            .publish(channel, payload)
            .await?;

        Ok(())
    }

    /// Prepare an event for publishing (returns channel and JSON payload)
    fn prepare_event(&self, event: &MarketEvent) -> Result<(String, String)> {
        let json = to_string(event)?;

        let channel = match event {
            MarketEvent::AggTrade(_) => CHANNEL_TICK,
            MarketEvent::Kline(_) => CHANNEL_KLINE,
            MarketEvent::DepthUpdate(_) => CHANNEL_DEPTH,
            MarketEvent::BookTicker(_) => CHANNEL_TICKER,
        };

        Ok((channel.to_string(), json))
    }

    /// Publish to a custom channel
    pub async fn publish_to_channel(&mut self, channel: &str, data: &str) -> Result<()> {
        self.conn
            .publish(channel, data)
            .await?;
        Ok(())
    }

    /// Ping Redis to check connection
    pub async fn ping(&mut self) -> Result<String> {
        let response: String = self.conn.ping().await?;
        Ok(response)
    }

    /// Get the Redis client
    pub fn client(&self) -> &Client {
        &self.client
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    #[ignore]  // Requires Redis to be running
    async fn test_redis_connection() {
        let config = RedisConfig::default();
        let publisher = RedisPublisher::new(config).await;
        assert!(publisher.is_ok());

        let mut publisher = publisher.unwrap();
        let pong = publisher.ping().await.unwrap();
        assert_eq!(pong, "PONG");
    }
}
