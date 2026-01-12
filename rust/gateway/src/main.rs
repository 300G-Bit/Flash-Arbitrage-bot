//! Flash Arbitrage Gateway
//!
//! High-performance market data gateway that connects to multiple exchanges
//! and publishes market events to Redis for consumption by the strategy engine.

mod exchange;
mod redis_publisher;

mod binance;
mod okx;

use anyhow::{Context, Result};
use clap::Parser;
use exchange::{Exchange, ExchangeType, Subscription, DataType, KlineInterval};
use redis_publisher::RedisPublisher;
use std::collections::HashMap;
use std::time::Duration;
use tokio::time;
use tracing::{error, info, warn};
use tracing_subscriber;

/// Gateway configuration
#[derive(Debug, Clone)]
struct GatewayConfig {
    /// Redis connection URL
    redis_url: String,
    /// Symbols to track
    symbols: Vec<String>,
    /// Exchanges to connect
    exchanges: Vec<ExchangeType>,
    /// Enable testnet/demo mode
    testnet: bool,
}

impl Default for GatewayConfig {
    fn default() -> Self {
        Self {
            redis_url: "redis://127.0.0.1:6379".to_string(),
            symbols: vec!["BTCUSDT".to_string(), "ETHUSDT".to_string()],
            exchanges: vec![ExchangeType::Binance],
            testnet: false,
        }
    }
}

/// Command line arguments
#[derive(Parser, Debug)]
#[command(author, version, about, long_about = None)]
struct Args {
    /// Redis connection URL
    #[arg(short, long, default_value = "redis://127.0.0.1:6379")]
    redis: String,

    /// Symbols to track (comma-separated)
    #[arg(short, long, value_delimiter = ',')]
    symbols: Vec<String>,

    /// Exchanges to connect (comma-separated: binance, okx)
    #[arg(short, long, value_delimiter = ',')]
    exchanges: Vec<String>,

    /// Use testnet/demo mode
    #[arg(short, long)]
    testnet: bool,

    /// Log level
    #[arg(short, long, default_value = "info")]
    log: String,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();

    // Initialize logging
    let log_level = args.log.to_lowercase();
    let env_filter = match log_level.as_str() {
        "trace" => "trace",
        "debug" => "debug",
        "info" => "info",
        "warn" => "warn",
        "error" => "error",
        _ => "info",
    };

    tracing_subscriber::fmt()
        .with_env_filter(env_filter)
        .with_target(false)
        .init();

    info!("Flash Arbitrage Gateway starting...");

    // Parse exchange types
    let exchanges = if args.exchanges.is_empty() {
        vec![ExchangeType::Binance]
    } else {
        args.exchanges
            .iter()
            .map(|e| match e.to_lowercase().as_str() {
                "binance" => Ok(ExchangeType::Binance),
                "okx" => Ok(ExchangeType::Okx),
                _ => anyhow::bail!("Unknown exchange: {}", e),
            })
            .collect::<Result<Vec<_>>>()?
    };

    // Default symbols if none provided
    let symbols = if args.symbols.is_empty() {
        vec!["BTCUSDT".to_string(), "ETHUSDT".to_string()]
    } else {
        args.symbols
    };

    let config = GatewayConfig {
        redis_url: args.redis,
        symbols,
        exchanges,
        testnet: args.testnet,
    };

    info!("Configuration: {:?}", config);

    // Create Redis publisher
    let redis_publisher = RedisPublisher::new(redis_publisher::RedisConfig {
        url: config.redis_url,
    })
    .await
    .context("Failed to connect to Redis")?;

    info!("Connected to Redis at {}", config.redis_url);

    // Verify Redis connection
    match redis_publisher.ping().await {
        Ok(_) => info!("Redis connection verified"),
        Err(e) => {
            error!("Redis ping failed: {}", e);
            return Err(e.context("Redis connection check failed"));
        }
    }

    // Run the gateway
    run_gateway(config, redis_publisher).await?;

    Ok(())
}

/// Main gateway loop
async fn run_gateway(config: GatewayConfig, redis_publisher: RedisPublisher) -> Result<()> {
    let mut exchange_map: HashMap<ExchangeType, Box<dyn Exchange>> = HashMap::new();

    // Initialize exchanges
    for exchange_type in &config.exchanges {
        let exchange: Box<dyn Exchange> = match exchange_type {
            ExchangeType::Binance => {
                info!("Initializing Binance client (testnet={})", config.testnet);
                Box::new(binance::BinanceClient::new(config.testnet)
                    .with_redis_publisher(redis_publisher.clone()))
            }
            ExchangeType::Okx => {
                info!("Initializing OKX client (demo={})", config.testnet);
                Box::new(okx::OkxClient::new(config.testnet)
                    .with_redis_publisher(redis_publisher.clone()))
            }
        };

        exchange_map.insert(*exchange_type, exchange);
    }

    // Connect to all exchanges
    for (exchange_type, exchange) in exchange_map.iter_mut() {
        info!("Connecting to {}...", exchange_type);
        let mut ex = Box::new(exchange.as_ref());

        // We need to use the actual mutable reference
        let actual_exchange = exchange_map.get_mut(exchange_type).unwrap();
        actual_exchange.connect().await
            .context(format!("Failed to connect to {}", exchange_type))?;
    }

    // Subscribe to market data
    let subscriptions = create_subscriptions(&config.symbols);
    info!("Subscribing to {} data streams per exchange", subscriptions.len());

    for (exchange_type, exchange) in exchange_map.iter_mut() {
        if let Err(e) = exchange.subscribe(subscriptions.clone()).await {
            warn!("Failed to subscribe to {}: {}", exchange_type, e);
        }
    }

    info!("Gateway running, streaming market data...");

    // Main event loop - receive events from all exchanges
    let mut ping_interval = time::interval(Duration::from_secs(30));
    let mut reconnect_interval = time::interval(Duration::from_secs(5));

    loop {
        tokio::select! {
            // Periodic ping to keep connections alive
            _ = ping_interval.tick() => {
                debug!("Sending keepalive ping to exchanges");
                for (exchange_type, exchange) in exchange_map.iter_mut() {
                    if !exchange.is_connected() {
                        warn!("{} is not connected, will attempt reconnect", exchange_type);
                    }
                }
            }

            // Check for reconnections
            _ = reconnect_interval.tick() => {
                for (exchange_type, exchange) in exchange_map.iter_mut() {
                    if !exchange.is_connected() {
                        info!("Attempting to reconnect to {}...", exchange_type);
                        if let Err(e) = exchange.connect().await {
                            error!("Failed to reconnect to {}: {}", exchange_type, e);
                        } else {
                            info!("Successfully reconnected to {}", exchange_type);
                            let _ = exchange.subscribe(subscriptions.clone()).await;
                        }
                    }
                }
            }

            // Process events (with timeout)
            result = async {
                for (exchange_type, exchange) in exchange_map.iter_mut() {
                    if let Ok(Some(event)) = exchange.recv_event().await {
                        let symbol = event.symbol();
                        let event_type = event.event_type();
                        info!("[{}] {}: {} - {}", exchange_type, symbol, event_type.as_str(),
                            match event {
                                exchange::MarketEvent::AggTrade(t) => format!("price={}", t.price),
                                exchange::MarketEvent::Kline(k) => format!("close={}", k.close),
                                exchange::MarketEvent::BookTicker(b) => format!("bid={}/ask={}", b.bid_price, b.ask_price),
                                exchange::MarketEvent::DepthUpdate(_) => "update".to_string(),
                            }
                        );
                    }
                }
                Ok::<(), anyhow::Error>(())
            } => {
                if let Err(e) = result {
                    error!("Error processing events: {}", e);
                }
            }
        }
    }
}

/// Create subscriptions for all symbols
fn create_subscriptions(symbols: &[String]) -> Vec<Subscription> {
    let mut subscriptions = Vec::new();

    let intervals = [
        KlineInterval::OneMinute,
        KlineInterval::FiveMinutes,
        KlineInterval::FifteenMinutes,
        KlineInterval::ThirtyMinutes,
        KlineInterval::OneHour,
        KlineInterval::FourHours,
    ];

    for symbol in symbols {
        // Aggregate trades
        subscriptions.push(Subscription {
            symbol: symbol.clone(),
            data_type: DataType::AggTrade,
            interval: None,
        });

        // Klines for each interval
        for interval in &intervals {
            subscriptions.push(Subscription {
                symbol: symbol.clone(),
                data_type: DataType::Kline,
                interval: Some(*interval),
            });
        }

        // Book ticker
        subscriptions.push(Subscription {
            symbol: symbol.clone(),
            data_type: DataType::BookTicker,
            interval: None,
        });

        // Depth
        subscriptions.push(Subscription {
            symbol: symbol.clone(),
            data_type: DataType::Depth,
            interval: None,
        });
    }

    subscriptions
}
