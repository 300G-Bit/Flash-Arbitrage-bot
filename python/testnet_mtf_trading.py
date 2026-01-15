"""
测试网交易 - 基于ATR的动态插针检测版本

策略逻辑：
- 基于ATR(平均真实波幅)计算动态阈值
- 速度检测：价格在短时间内快速变化
- K线确认：影线、颜色反转、假突破
- 两阶段入场：检测入第一腿，回调入第二腿锁利
"""

import json
import signal
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

import websocket

# 添加src目录到路径
script_dir = Path(__file__).parent.resolve()
sys.path.insert(0, str(script_dir / "src"))
sys.path.insert(0, str(script_dir))

from config.testnet_config import load_config, TestnetConfig
from src.analysis.kline_tracker import KlineTrackerManager, Timeframe, Kline
from src.analysis.atr_detector import SpikeDetectorManager, SpikeDetectorConfig
from src.exchange.binance_futures import BinanceFuturesClient
from src.trading.simple_hedge import SimpleHedgeExecutor, SimpleHedgeConfig
from src.utils.logging_config import setup_logging, get_logger, EventLogger
from src.trading.trade_logger import TradeLogger, TradeRecord

# 使用统一日志系统
logger = get_logger(__name__)
events = EventLogger(logger)


# 配置
PROXY_HOST = "127.0.0.1"
PROXY_HTTP_PORT = 7897
USE_PROXY = True

BEIJING_TZ = timezone(timedelta(hours=8))
WS_ENDPOINT = "wss://stream.binancefuture.com/ws"

DEFAULT_SYMBOLS = [
    # 主流币
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    # 可能存在的币安新币
    "TRUMPUSDT", "ZECUSDT", "VVVUSDT", "TAOUSDT", "RIVERUSDT",
    "POLUSDT", "BREVUSDT", "MIRAUSDT", "COLLECTUSDT",
    "GUNUSDT", "AAVEUSDT", "DOGEUSDT", "PLAYUSDT", "DASHUSDT",
    "XMRUSDT", "XAGUSDT", "OPUSDT", "SAFEUSDT", "QNTUSDT",
    "COMPUSDT", "TRBUSDT", "LINKUSDT", "PROMUSDT", "ORDIUSDT",
    "NEOUSDT", "ICPUSDT", "DOTUSDT", "GASUSDT", "RPLUSDT",
    # 已移除测试网不存在的: 4USDT, BUSDT, CCUSDT, MYSUSDT,
    #                        ZKPUSDT, ICNUSDT, IPUUSDT, CLOUSDT,
    #                        APYUSDT, MYXUSDT
]


# WebSocket行情接收器

class MarketDataReceiver:
    """WebSocket市场数据接收器"""

    def __init__(
        self,
        symbols: List[str],
        kline_manager: KlineTrackerManager,
        hedge_executor: SimpleHedgeExecutor,
        detector_manager: SpikeDetectorManager,
        config: TestnetConfig
    ):
        self.symbols_upper = [s.upper() for s in symbols]
        self.symbols_lower = [s.lower() for s in symbols]
        self.kline_manager = kline_manager
        self.hedge_executor = hedge_executor
        self.detector_manager = detector_manager
        self.config = config

        self.running = False
        self.ws_connected = False
        self.ws = None
        self.ws_thread = None
        self.message_count = 0
        self.signal_count = 0

        # 用于检测K线收盘
        self._last_kline_close: Dict[str, int] = {}  # symbol -> timestamp

    def start(self) -> None:
        self.running = True
        self._connect()

    def stop(self) -> None:
        self.running = False
        if self.ws:
            self.ws.close()

    def _connect(self) -> None:
        streams = [f"{s}@aggTrade" for s in self.symbols_lower]
        ws_url = f"{WS_ENDPOINT}/{'/'.join(streams)}"

        print(f"[{self._format_time()}] 连接WebSocket...")

        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open
        )

        def run_ws():
            if USE_PROXY:
                self.ws.run_forever(
                    http_proxy_host=PROXY_HOST,
                    http_proxy_port=PROXY_HTTP_PORT,
                    proxy_type="http"
                )
            else:
                self.ws.run_forever()

        self.ws_thread = threading.Thread(target=run_ws, daemon=True)
        self.ws_thread.start()

    def _on_open(self, ws) -> None:
        self.ws_connected = True
        print(f"[{self._format_time()}] WebSocket已连接")
        self._load_historical_klines()

    def _on_error(self, ws, error) -> None:
        if error:
            print(f"[{self._format_time()}] WebSocket错误: {str(error)[:80]}")

    def _on_close(self, ws, code, msg) -> None:
        self.ws_connected = False
        print(f"[{self._format_time()}] WebSocket断开")
        if self.running:
            print(f"[{self._format_time()}] 2秒后重连...")
            time.sleep(2)
            self._connect()

    def _on_message(self, ws, message) -> None:
        try:
            self.message_count += 1
            data = json.loads(message)

            symbol = data.get('s', '').upper()
            if symbol not in self.symbols_upper:
                return

            price = float(data['p'])
            timestamp_ms = data['T']

            self._process_price(symbol, price, timestamp_ms)
        except Exception:
            pass

    def _process_price(self, symbol: str, price: float, timestamp_ms: int) -> None:
        # 更新K线追踪器
        self.kline_manager.on_price(symbol, price, timestamp_ms)

        # 更新检测器的价格历史
        self.detector_manager.on_price(symbol, price, timestamp_ms)

        # 更新对冲执行器
        self.hedge_executor.on_price_update(symbol, price)

        tracker = self.kline_manager.get_tracker(symbol)

        # 检测1m K线收盘（用于更新ATR）
        atr_tf = Timeframe.MIN_1
        tf_data = tracker.data.get(atr_tf)
        if tf_data and tf_data.current_candle:
            candle_start = tf_data.current_candle.timestamp
            # 检测K线是否切换（新K线开始）
            last_close = self._last_kline_close.get(symbol, 0)
            if candle_start > last_close:
                # 上一根K线收盘，更新ATR
                if tf_data.klines:
                    closed_kline = tf_data.klines[-1]
                    self.detector_manager.on_kline_close(symbol, closed_kline)
                self._last_kline_close[symbol] = candle_start

        # 检测插针信号
        signal = self.detector_manager.detect(
            symbol,
            tracker,
            price,
            timestamp_ms
        )

        if signal:
            self.signal_count += 1
            self.hedge_executor.on_signal(signal)

    def _get_binance_interval(self, tf: Timeframe) -> str | None:
        """Timeframe转币安API interval格式

        测试网不支持30s K线，返回None跳过。
        """
        mapping = {
            Timeframe.MIN_1: "1m",
            Timeframe.MIN_5: "5m",
            Timeframe.MIN_15: "15m",
            Timeframe.SEC_30: None,  # 测试网不支持
        }
        return mapping.get(tf)

    def _load_historical_klines(self) -> None:
        print(f"[{self._format_time()}] 加载历史K线数据...")

        client = BinanceFuturesClient(
            api_key="",
            api_secret="",
            testnet=True,  # FIX: 使用测试网，与交易保持一致
            enable_proxy=USE_PROXY,
            proxy_url=f"http://{PROXY_HOST}:{PROXY_HTTP_PORT}" if USE_PROXY else None
        )

        success_count = 0
        skip_count = 0

        for symbol in self.symbols_upper:
            for tf in [Timeframe.MIN_1, Timeframe.MIN_5, Timeframe.MIN_15]:  # 跳过30s
                interval = self._get_binance_interval(tf)
                if not interval:
                    skip_count += 1
                    continue

                for attempt in range(3):  # 最多重试3次
                    try:
                        klines = client.get_klines(
                            symbol=symbol,
                            interval=interval,
                            limit=50
                        )
                        if klines:
                            tracker = self.kline_manager.get_tracker(symbol)
                            tracker.load_historical_klines(tf, klines)
                            success_count += 1
                            break
                        time.sleep(0.3)
                    except Exception as e:
                        if attempt == 2:
                            print(f"[{self._format_time()}] 加载失败 {symbol} {tf.value}: {e}")
                        time.sleep(0.5)
            time.sleep(0.1)

        print(f"[{self._format_time()}] 历史数据加载完成 (成功: {success_count}, 跳过30s: {skip_count})")

    @staticmethod
    def _format_time(dt: datetime | None = None) -> str:
        if dt is None:
            dt = datetime.now(BEIJING_TZ)
        return dt.strftime("%H:%M:%S")

    def is_connected(self) -> bool:
        return self.ws_connected


# 主运行器

class MTFTradingRunner:
    """基于ATR的插针交易运行器"""

    def __init__(self, config: TestnetConfig | None = None):
        self.config = config or load_config()
        self.running = False
        self._start_time = 0.0

        # 配置统一日志系统
        setup_logging(log_dir="logs", console_level="INFO", file_level="DEBUG")

        # 交易记录
        self.trade_logger = TradeLogger(log_dir="testnet_trades", auto_save=True)
        self._trade_records: List[dict] = []

        # K线追踪器
        self.kline_manager = KlineTrackerManager(max_klines=50)

        # ATR插针检测器
        detector_config = SpikeDetectorConfig(
            atr_period=self.config.ATR_PERIOD,
            atr_spike_multiplier=self.config.ATR_SPIKE_MULTIPLIER,
            atr_retrace_multiplier=self.config.ATR_RETRACE_MULTIPLIER,
            detection_window_seconds=self.config.SPIKE_DETECTION_WINDOW,
            shadow_ratio_threshold=self.config.SHADOW_RATIO_THRESHOLD,
            false_breakout_threshold=self.config.FALSE_BREAKOUT_THRESHOLD,
            detection_cooldown_seconds=self.config.DETECTION_COOLDOWN_SECONDS
        )
        self.detector_manager = SpikeDetectorManager(config=detector_config)

        # 对冲执行器 - 传入logger以便日志集成
        self.hedge_executor = SimpleHedgeExecutor(
            client=None,
            config=SimpleHedgeConfig(),
            position_usdt=self.config.POSITION_USDT,
            leverage=self.config.LEVERAGE,
            fee_rate=self.config.FEE_RATE,
            external_logger=logger  # 传入主程序logger
        )
        self.hedge_executor.set_hedge_closed_callback(self._on_hedge_closed)

    def start(self, symbols: List[str] | None = None) -> None:
        symbols = symbols or DEFAULT_SYMBOLS

        self.client = BinanceFuturesClient(
            api_key=self.config.BINANCE_API_KEY,
            api_secret=self.config.BINANCE_API_SECRET,
            testnet=True,
            timeout=self.config.API_TIMEOUT,
            enable_proxy=self.config.ENABLE_PROXY,
            proxy_url=self.config.PROXY_URL if self.config.ENABLE_PROXY else None
        )
        self.hedge_executor.client = self.client

        logger.info(f"{'='*60}")
        logger.info("Flash Arbitrage Bot - 多时间框架策略（测试网）")
        logger.info(f"{'='*60}")
        logger.info("测试交易所连接...")

        if not self.client.test_connectivity():
            logger.error("无法连接到币安测试网")
            return

        account = self.client.get_account_info()
        if not account:
            logger.error("无法获取账户信息")
            return

        logger.info(f"连接成功 | 可用余额: {account.available_balance:.2f} USDT")

        self.receiver = MarketDataReceiver(
            symbols=symbols,
            kline_manager=self.kline_manager,
            hedge_executor=self.hedge_executor,
            detector_manager=self.detector_manager,
            config=self.config
        )
        self.receiver.start()

        self.running = True
        self._start_time = time.time()

        hedge_cfg = self.hedge_executor.config
        logger.info(f"{'='*60}")
        logger.info("多时间框架策略已启动")
        logger.info(f"监控: {', '.join(symbols[:5])}{'...' if len(symbols) > 5 else ''}")
        logger.info(f"配置: {self.config.POSITION_USDT} USDT × {self.config.LEVERAGE}x")
        logger.info(
            f"参数: 对冲{hedge_cfg.HEDGE_ENTRY_PERCENT:.1%} | "
            f"目标{hedge_cfg.FIRST_LEG_TARGET_PERCENT:.1%} | "
            f"等待{hedge_cfg.SECOND_LEG_WAIT_SECONDS}s"
        )
        logger.info(f"{'='*60}")
        logger.info("等待信号...")

        signal.signal(signal.SIGINT, self._signal_handler)

        try:
            last_status_time = 0.0
            while self.running:
                time.sleep(0.1)

                now = time.time()
                if now - last_status_time >= 30:
                    self._print_status()
                    last_status_time = now
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _on_hedge_closed(self, position) -> None:
        """对冲交易完成回调，保存交易记录"""
        record = {
            "symbol": position.symbol,
            "direction": position.direction.value,
            "entry_price": position.entry_price,
            "first_side": position.first_side,
            "first_entry": position.first_entry,
            "first_quantity": position.first_quantity,
            "first_pnl": position.first_pnl,
            "second_side": position.second_side,
            "second_entry": position.second_entry,
            "second_quantity": position.second_quantity,
            "second_pnl": position.second_pnl,
            "total_pnl": position.total_pnl,
            "signal_time": position.signal_time.isoformat() if position.signal_time else None,
            "close_time": position.close_time.isoformat() if position.close_time else None,
            "close_reason": position.close_reason,
            "created_at": datetime.now().isoformat(),
        }
        self._trade_records.append(record)

        logger.info(
            f"交易记录: {position.symbol} "
            f"第一腿:{position.first_pnl:+.4f} "
            f"第二腿:{position.second_pnl:+.4f} "
            f"总计:{position.total_pnl:+.4f} USDT"
        )

    def _print_status(self) -> None:
        if not self.running:
            return

        elapsed = time.time() - self._start_time if self._start_time else 0
        stats = self.hedge_executor.get_stats()

        logger.info(
            f"运行 {elapsed/60:.1f}min | "
            f"信号: {self.receiver.signal_count} | "
            f"活跃: {stats['active_positions']} | "
            f"完成: {stats['total_trades']}"
        )

        if stats['total_trades'] > 0:
            pnl = stats['total_pnl']
            pnl_mark = "+" if pnl > 0 else "" if pnl == 0 else ""
            logger.info(
                f"   总盈亏: {pnl_mark}{pnl:.4f} USDT | "
                f"胜率: {stats['win_rate']:.1f}%"
            )

    def stop(self) -> None:
        if not self.running:
            return

        logger.warning("\n正在停止...")
        self.running = False

        if hasattr(self, 'receiver'):
            self.receiver.stop()

        logger.info("平仓所有持仓...")
        self.hedge_executor.close_all(reason="shutdown")
        time.sleep(2)

        # 导出交易记录
        self._export_trade_records()

        self._print_final_stats()

    def _export_trade_records(self) -> None:
        """导出交易记录到JSON和CSV"""
        if not self._trade_records:
            return

        import json
        from pathlib import Path

        log_dir = Path("testnet_trades")
        log_dir.mkdir(exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 导出JSON
        json_path = log_dir / f"hedge_trades_{timestamp}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "export_time": datetime.now().isoformat(),
                "total_trades": len(self._trade_records),
                "trades": self._trade_records
            }, f, indent=2, ensure_ascii=False)

        # 导出CSV
        csv_path = log_dir / f"hedge_trades_{timestamp}.csv"
        if self._trade_records:
            import csv
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                fieldnames = list(self._trade_records[0].keys())
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(self._trade_records)

        logger.info(f"交易记录已导出: {json_path.name}, {csv_path.name}")

    def _print_final_stats(self) -> None:
        stats = self.hedge_executor.get_stats()
        elapsed = time.time() - self._start_time if self._start_time else 0

        logger.info(f"{'='*60}")
        logger.info("最终统计")
        logger.info(f"   运行时长: {elapsed/60:.1f}分钟")
        logger.info(f"   完成交易: {stats['total_trades']}")

        if stats['total_trades'] > 0:
            pnl = stats['total_pnl']
            pnl_mark = "+" if pnl > 0 else "" if pnl == 0 else ""
            logger.info(
                f"   胜率: {stats['win_rate']:.1f}% | "
                f"总盈亏: {pnl_mark}{pnl:.4f} USDT"
            )

        logger.info(f"{'='*60}")

    def _signal_handler(self, signum, frame) -> None:
        logger.warning("收到停止信号，正在安全停止...")
        self.stop()


def main() -> None:
    try:
        config = load_config()
    except ValueError as e:
        print(f"\n配置错误: {e}")
        print("\n请设置环境变量:")
        print("  export BINANCE_TESTNET_API_KEY=your_api_key")
        print("  export BINANCE_TESTNET_API_SECRET=your_api_secret")
        print("\n或访问 https://testnet.binancefuture.com/ 获取测试网API密钥")
        return

    runner = MTFTradingRunner(config)
    runner.start(DEFAULT_SYMBOLS)


if __name__ == "__main__":
    main()
