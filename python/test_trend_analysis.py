"""
Test script for trend analysis with Binance data.

This script:
1. Fetches historical kline data from Binance
2. Runs the trend analysis engine
3. Validates trend prediction accuracy
"""

import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import structlog
from binance.client import Client
from binance.helpers import round_step_size

from src.analysis import TrendAnalyzer, TrendDirection
from src.config import Settings, RedisConfig

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


class BinanceDataFetcher:
    """Fetch historical kline data from Binance."""

    def __init__(self, api_key: str = None, api_secret: str = None):
        """Initialize Binance client.

        Args:
            api_key: Binance API key (optional, public endpoints don't need it)
            api_secret: Binance API secret (optional)
        """
        self.client = Client(api_key, api_secret)
        self.logger = logger

    def fetch_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: datetime = None,
    ) -> list[dict]:
        """Fetch kline data from Binance.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            interval: Kline interval (e.g., "1m", "5m", "1h", "4h")
            limit: Number of klines to fetch (max 1000)
            start_time: Start time for data (optional)

        Returns:
            List of kline dictionaries.
        """
        self.logger.info(
            "Fetching klines",
            symbol=symbol,
            interval=interval,
            limit=limit,
        )

        try:
            klines = self.client.get_klines(
                symbol=symbol,
                interval=interval,
                limit=limit,
                startTime=int(start_time.timestamp() * 1000) if start_time else None,
            )

            result = []
            for k in klines:
                result.append({
                    "symbol": symbol,
                    "interval": interval,
                    "open_time": k[0],
                    "close_time": k[6],
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "is_closed": True,
                    "quote_volume": float(k[7]),
                    "trades": k[8],
                })

            self.logger.info(
                "Fetched klines successfully",
                symbol=symbol,
                interval=interval,
                count=len(result),
            )

            return result

        except Exception as e:
            self.logger.error("Failed to fetch klines", error=str(e))
            return []


def load_historical_data(
    fetcher: BinanceDataFetcher,
    symbol: str,
    timeframes: list[str],
) -> dict[str, list[dict]]:
    """Load historical data for multiple timeframes.

    Args:
        fetcher: BinanceDataFetcher instance
        symbol: Trading pair symbol
        timeframes: List of timeframe strings

    Returns:
        Dictionary mapping timeframe to list of klines.
    """
    data = {}

    # Calculate start time (fetch enough data for analysis)
    # Need at least 500 candles for longest timeframe
    start_time = datetime.now(timezone.utc) - timedelta(days=7)

    for tf in timeframes:
        klines = fetcher.fetch_klines(
            symbol=symbol,
            interval=tf,
            limit=500,
            start_time=start_time,
        )
        data[tf] = klines
        logger.info(f"Loaded {len(klines)} {tf} klines for {symbol}")

    return data


def validate_trend_predictions(
    analyzer: TrendAnalyzer,
    symbol: str,
) -> dict:
    """Validate trend prediction accuracy.

    This backtests the trend analyzer by comparing predicted trends
    with actual price movements.

    Args:
        analyzer: TrendAnalyzer instance with loaded data
        symbol: Trading pair symbol

    Returns:
        Dictionary with validation results.
    """
    results = {
        "symbol": symbol,
        "timeframes": {},
        "overall_accuracy": {},
    }

    timeframes_to_test = ["5m", "15m", "30m", "1h", "4h"]

    for tf in timeframes_to_test:
        klines = list(analyzer.kline_buffers.get(tf, []))

        if len(klines) < 50:
            results["timeframes"][tf] = {"error": "Insufficient data"}
            continue

        # Get trend analysis at different points in history
        correct_predictions = 0
        total_predictions = 0
        predictions = []

        # Test on last 30 klines
        test_klines = klines[-30:]

        for i, kline in enumerate(test_klines):
            # Create analyzer with data up to this point
            test_analyzer = TrendAnalyzer(symbol)
            for k in klines[:klines.index(kline) + 1]:
                test_analyzer.update_kline(k)

            try:
                trend_result = test_analyzer.analyze_timeframe(tf)

                # Validate prediction: check if next 3 klines moved in predicted direction
                if i < len(test_klines) - 3:
                    next_closes = [
                        test_klines[i + j]["close"]
                        for j in range(1, 4)
                    ]
                    current_close = kline["close"]

                    # Determine actual direction
                    if all(c > current_close for c in next_closes):
                        actual_direction = TrendDirection.UP
                    elif all(c < current_close for c in next_closes):
                        actual_direction = TrendDirection.DOWN
                    else:
                        actual_direction = TrendDirection.SIDEWAYS

                    # Check if prediction was correct
                    predicted = trend_result.direction
                    is_correct = predicted == actual_direction

                    if predicted in (TrendDirection.UP, TrendDirection.DOWN):
                        total_predictions += 1
                        if is_correct:
                            correct_predictions += 1

                    predictions.append({
                        "index": i,
                        "predicted": predicted.value,
                        "actual": actual_direction.value,
                        "correct": is_correct,
                        "strength": trend_result.strength,
                    })

            except Exception as e:
                logger.warning(f"Error analyzing {tf} at index {i}: {e}")
                continue

        if total_predictions > 0:
            accuracy = correct_predictions / total_predictions
            results["timeframes"][tf] = {
                "accuracy": accuracy,
                "correct": correct_predictions,
                "total": total_predictions,
                "predictions": predictions[-10:],  # Last 10 predictions
            }
            results["overall_accuracy"][tf] = accuracy

    return results


async def test_trend_analysis(
    symbols: list[str],
    timeframes: list[str] = None,
) -> None:
    """Main test function.

    Args:
        symbols: List of trading pairs to test
        timeframes: List of timeframes to test (default: common ones)
    """
    logger.info("Starting trend analysis test", symbols=symbols)

    if timeframes is None:
        timeframes = ["1m", "5m", "15m", "30m", "1h", "4h"]

    # Initialize Binance fetcher (no API keys needed for public data)
    fetcher = BinanceDataFetcher()

    for symbol in symbols:
        logger.info(f"Analyzing {symbol}...")

        # Fetch historical data
        data = load_historical_data(fetcher, symbol, timeframes)

        if not any(data.values()):
            logger.error(f"No data fetched for {symbol}")
            continue

        # Initialize analyzer
        analyzer = TrendAnalyzer(symbol)

        # Load all klines into analyzer
        for tf, klines in data.items():
            for kline in klines:
                analyzer.update_kline(kline)

        # Run multi-timeframe analysis
        mtf_result = analyzer.analyze_multi_timeframe()

        logger.info(
            f"Multi-timeframe analysis for {symbol}",
            direction=mtf_result.overall_direction.value,
            strength=mtf_result.overall_strength,
            alignment=mtf_result.alignment_score,
            tradeable=mtf_result.is_tradeable,
            recommendation=mtf_result.recommendation,
        )

        # Print per-timeframe results
        for tf_name, result in mtf_result.timeframes.items():
            logger.info(
                f"  {tf_name}: {result.direction.value} "
                f"(strength={result.strength}, {result.confidence})"
            )

        # Validate predictions
        validation = validate_trend_predictions(analyzer, symbol)

        logger.info(f"Validation results for {symbol}:", **results)

        for tf, tf_result in validation["timeframes"].items():
            if "accuracy" in tf_result:
                logger.info(
                    f"  {tf} accuracy: {tf_result['accuracy']:.1%} "
                    f"({tf_result['correct']}/{tf_result['total']})"
                )

                # Show recent predictions
                if tf_result.get("predictions"):
                    logger.info(f"  Recent {tf} predictions:")
                    for pred in tf_result["predictions"][-5:]:
                        status = "✓" if pred["correct"] else "✗"
                        logger.info(
                            f"    {status} {pred['predicted']} → {pred['actual']} "
                            f"(strength={pred['strength']})"
                        )

        logger.info("-" * 50)


async def interactive_test():
    """Interactive test mode - user selects symbols to test."""
    print("\n" + "=" * 60)
    print("Flash Arbitrage Bot - Trend Analysis Test")
    print("=" * 60)

    # Common trading pairs
    common_symbols = [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
        "XRPUSDT",
        "ADAUSDT",
        "DOGEUSDT",
        "AVAXUSDT",
        "DOTUSDT",
        "LINKUSDT",
        "MATICUSDT",
    ]

    print("\nCommon trading pairs:")
    for i, s in enumerate(common_symbols, 1):
        print(f"  {i}. {s}")

    print("\nOption 0: Enter custom symbols (comma-separated)")
    print("Option 99: Exit")

    while True:
        try:
            choice = input("\nSelect symbols (numbers or 0 for custom): ").strip()

            if choice == "99":
                print("Exiting...")
                break

            if choice == "0":
                custom = input("Enter symbols (comma-separated, e.g., BTCUSDT,ETHUSDT): ")
                symbols = [s.strip().upper() for s in custom.split(",") if s.strip()]
            else:
                indices = [int(x.strip()) for x in choice.split(",")]
                symbols = [common_symbols[i-1] for i in indices if 1 <= i <= len(common_symbols)]

            if symbols:
                logger.info(f"Testing symbols: {symbols}")
                await test_trend_analysis(symbols)
            else:
                print("No valid symbols selected.")

        except (ValueError, KeyboardInterrupt):
            print("\nInvalid input or cancelled.")


def main():
    """Entry point."""
    import structlog

    # Configure simple logging for console
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.JSONRenderer() if False else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

    # Run interactive test
    asyncio.run(interactive_test())


if __name__ == "__main__":
    main()
