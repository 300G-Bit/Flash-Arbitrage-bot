"""
测试网交易与插针检测器集成 - 双向对冲策略版本

策略逻辑:
- 上插针: 高位开空 → 回调后开多锁定利润 → 先平空后平多
- 下插针: 低位开多 → 反弹后开空锁定利润 → 先平空后平多

使用方法:
1. 设置环境变量或配置API密钥
2. 运行脚本: python testnet_with_recorder.py
"""

import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List

import websocket

# 添加src目录到路径
script_dir = Path(__file__).parent.resolve()
sys.path.insert(0, str(script_dir / "src"))
sys.path.insert(0, str(script_dir))

# 导入交易所客户端
from src.exchange.binance_futures import BinanceFuturesClient

# 导入日志系统
from src.utils.logger import setup_logging, BotLogger

# 导入对冲交易组件
from src.trading.hedge_manager import HedgeTradeManager
from src.trading.hedge_types import HedgeConfig, HedgePosition, PinSignal
from src.trading.hedge_logger import HedgeTradeLogger

# 导入配置
from config.testnet_config import TestnetConfig, load_config


# ============== 工具函数 ==============

BEIJING_TZ = timezone(timedelta(hours=8))


def format_time(dt: datetime | None = None) -> str:
    """格式化时间为 HH:MM:SS.mmm 格式"""
    if dt is None:
        dt = datetime.now(BEIJING_TZ)
    return dt.strftime("%H:%M:%S.%f")[:-3]


# ============== 配置 ==============

# 代理设置
PROXY_HOST = "127.0.0.1"
PROXY_HTTP_PORT = 7897
USE_PROXY = True

# WebSocket端点 - 使用测试网行情
WS_ENDPOINT = "wss://stream.binancefuture.com/ws"

# 监控交易对
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "TRUMPUSDT",
                   "ZECUSDT", "VVVUSDT", "TAOUSDT", "RIVERUSDT", "POLUSDT",
                   # "币安人生USDT",  # 移除：中文字符导致API签名失败
                   "BREVUSDT", "MIRAUSDT", "COLLECTUSDT",
                   "4USDT", "BUSDT", "CCUSDT", "GUNUSDT", "AAVEUSDT", "DOGEUSDT",
                   "PLAYUSDT", "DASHUSDT", "MYSUSDT", "XMRUSDT", "ZKPUSDT", "ICNUSDT",
                   "XAGUSDT", "IPUUSDT", "CLOUSDT", "OPUSDT", "SAFEUSDT", "QNTUSDT",
                   "COMPUSDT", "TRBUSDT", "LINKUSDT", "PROMUSDT", "ORDIUSDT", "NEOUSDT",
                   "ICPUSDT", "DOTUSDT", "GASUSDT", "RPLUSDT", "APYUSDT", "MYXUSDT"]

# 插针检测参数
SPIKE_CONFIG = {
    "price_window_ms": 30000,        # 检测窗口1秒
    "min_spike_percent": 0.65,       # 最小插针幅度0.5%
    "max_spike_percent": 4.0,       # 最大插针幅度5.0%
    "retracement_percent": 12,      # 回撤至少15%
}

# 对冲策略参数
HEDGE_CONFIG = {
    "enable_hedge": True,               # 启用对冲模式
    "hedge_retracement_percent": 0.8,   # 盈利0.5%时开对冲腿（原50.0改为0.5，含义从回撤改为盈利）
    "hedge_wait_timeout_seconds": 300,  # 等待对冲的超时时间(秒)，原60改为300（5分钟）
    "close_order": "SHORT_FIRST",       # 平仓顺序: 先平空
    "take_profit_after_hedge": 0.5,     # 对冲后止盈点(%)
    "stop_loss_after_hedge": 1.0,       # 对冲后止损点(%)
    "quick_tp_enabled": True,           # 启用第二腿快速止盈
    "quick_tp_percent": 0.3,            # 第二腿快速止盈点位(%) - 盈利0.3%立即平仓
}


# ============== 插针检测器 ==============

class PinDetector:
    """插针检测器 - 检测市场价格快速波动（插针）"""

    # 信号冷却时间（毫秒）
    SIGNAL_COOLDOWN_MS = 5000

    def __init__(self, symbols: List[str]):
        self.symbols_upper = [s.upper() for s in symbols]
        self.symbols_lower = [s.lower() for s in symbols]
        self.running = False
        self.ws_connected = False
        self.ws = None
        self.ws_thread = None
        self.message_count = 0

        # 回调函数
        self.on_signal = None
        self.on_price_update = None

        # 每个交易对的监控数据
        self.monitors: Dict[str, Dict] = self._init_monitors()

    def _init_monitors(self) -> Dict[str, Dict]:
        """初始化所有交易对的监控数据"""
        return {
            s: {
                "current_price": 0.0,
                "window_start": 0,
                "window_start_price": 0.0,
                "window_high": 0.0,
                "window_low": float('inf'),
                "last_signal_time": 0,
            }
            for s in self.symbols_upper
        }

    def set_signal_callback(self, callback):
        """设置信号回调"""
        self.on_signal = callback

    def set_price_callback(self, callback):
        """设置价格更新回调"""
        self.on_price_update = callback

    def start(self):
        """启动检测器"""
        self.running = True
        self._connect()

    def stop(self):
        """停止检测器"""
        self.running = False
        if self.ws:
            self.ws.close()

    def _connect(self):
        """连接WebSocket"""
        streams = [f"{s}@aggTrade" for s in self.symbols_lower]
        ws_url = f"{WS_ENDPOINT}/{'/'.join(streams)}"

        print(f"[{format_time()}] 连接WebSocket: {ws_url[:80]}...")

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

    def _on_open(self, ws):
        self.ws_connected = True
        print(f"[{format_time()}] ✅ WebSocket已连接")

    def _on_error(self, ws, error):
        if error:
            print(f"[{format_time()}] WebSocket错误: {str(error)[:80]}")

    def _on_close(self, ws, code, msg):
        self.ws_connected = False
        print(f"[{format_time()}] WebSocket断开")
        if self.running:
            print(f"[{format_time()}] 2秒后重连...")
            time.sleep(2)
            self._connect()

    def _on_message(self, ws, message):
        """处理价格消息"""
        try:
            self.message_count += 1
            data = json.loads(message)

            symbol = data.get('s', '').upper()
            if symbol not in self.monitors:
                return

            price = float(data['p'])
            timestamp = datetime.fromtimestamp(data['T'] / 1000, tz=BEIJING_TZ)

            self._process_price(symbol, price, timestamp)
        except Exception:
            pass  # 静默忽略解析错误

    def _process_price(self, symbol: str, price: float, timestamp: datetime):
        """处理价格更新"""
        monitor = self.monitors[symbol]
        now_ms = timestamp.timestamp() * 1000

        monitor["current_price"] = price

        # 触发价格更新回调
        if self.on_price_update:
            self.on_price_update(symbol, price, timestamp)

        # 初始化窗口
        if monitor["window_start"] == 0:
            monitor["window_start"] = now_ms
            monitor["window_start_price"] = price
            monitor["window_high"] = price
            monitor["window_low"] = price
            return

        # 更新高低点
        monitor["window_high"] = max(monitor["window_high"], price)
        monitor["window_low"] = min(monitor["window_low"], price)

        # 检测插针（窗口期满）
        if now_ms - monitor["window_start"] >= SPIKE_CONFIG["price_window_ms"]:
            self._detect_spike(symbol, price, timestamp, monitor)
            self._reset_window(monitor, now_ms, price)

    def _reset_window(self, monitor: Dict, now_ms: int, price: float):
        """重置检测窗口"""
        monitor["window_start"] = now_ms
        monitor["window_start_price"] = price
        monitor["window_high"] = price
        monitor["window_low"] = price

    def _detect_spike(self, symbol: str, price: float, timestamp: datetime, monitor: Dict):
        """检测插针"""
        start = monitor["window_start_price"]
        high = monitor["window_high"]
        low = monitor["window_low"]

        if start == 0:
            return

        now_ms = timestamp.timestamp() * 1000
        if now_ms - monitor["last_signal_time"] < self.SIGNAL_COOLDOWN_MS:
            return  # 冷却中

        signal = self._try_detect_up_spike(symbol, start, high, low, price, timestamp)
        if signal is None:
            signal = self._try_detect_down_spike(symbol, start, high, low, price, timestamp)

        if signal:
            monitor["last_signal_time"] = now_ms
            print(f"\n🔔 [{format_time()}] 检测到插针: {signal}")
            if self.on_signal:
                self.on_signal(signal)

    def _try_detect_up_spike(self, symbol: str, start: float, high: float,
                            low: float, price: float, timestamp: datetime) -> PinSignal | None:
        """尝试检测上插针"""
        if high <= start:
            return None

        amplitude = (high - start) / start * 100
        min_amp = SPIKE_CONFIG["min_spike_percent"]
        max_amp = SPIKE_CONFIG["max_spike_percent"]

        if not (min_amp <= amplitude <= max_amp):
            return None

        retracement = (high - price) / (high - start) * 100
        if retracement >= SPIKE_CONFIG["retracement_percent"]:
            return PinSignal(
                symbol=symbol,
                direction="UP",
                start_price=start,
                peak_price=high,
                entry_price=price,
                amplitude=amplitude,
                retracement=retracement,
                detected_at=timestamp
            )
        return None

    def _try_detect_down_spike(self, symbol: str, start: float, high: float,
                              low: float, price: float, timestamp: datetime) -> PinSignal | None:
        """尝试检测下插针"""
        if start <= low:
            return None

        amplitude = (start - low) / start * 100
        min_amp = SPIKE_CONFIG["min_spike_percent"]
        max_amp = SPIKE_CONFIG["max_spike_percent"]

        if not (min_amp <= amplitude <= max_amp):
            return None

        retracement = (price - low) / (start - low) * 100
        if retracement >= SPIKE_CONFIG["retracement_percent"]:
            return PinSignal(
                symbol=symbol,
                direction="DOWN",
                start_price=start,
                peak_price=low,
                entry_price=price,
                amplitude=amplitude,
                retracement=retracement,
                detected_at=timestamp
            )
        return None

    def is_connected(self) -> bool:
        return self.ws_connected


# ============== 对冲策略运行器 ==============

class HedgeStrategyRunner:
    """双向对冲策略运行器"""

    # 状态打印间隔（秒）
    STATUS_INTERVAL_SECONDS = 30
    # 平仓等待时间（秒）
    CLOSE_WAIT_SECONDS = 2

    def __init__(self, config: TestnetConfig | None = None):
        self.config = config or load_config()
        self.running = False
        self._start_time: float | None = None
        self._signals_count = 0

        # 初始化日志系统
        self.bot_logger = setup_logging(log_dir="logs", console_level="INFO")

        # 初始化交易客户端
        self.client = BinanceFuturesClient(
            api_key=self.config.BINANCE_API_KEY,
            api_secret=self.config.BINANCE_API_SECRET,
            testnet=True,
            timeout=self.config.API_TIMEOUT,
            enable_proxy=self.config.ENABLE_PROXY,
            proxy_url=self.config.PROXY_URL if self.config.ENABLE_PROXY else None
        )

        # 初始化交易日志记录器
        self.logger = HedgeTradeLogger(log_dir="hedge_trades", auto_save=True)

        # 创建对冲配置
        hedge_cfg = HedgeConfig(**HEDGE_CONFIG)

        # 初始化对冲交易管理器
        self.hedge_manager = HedgeTradeManager(
            client=self.client,
            config=self.config,
            hedge_config=hedge_cfg,
            logger=self.logger
        )

        # 设置回调
        self.hedge_manager.set_hedge_opened_callback(self._on_hedge_opened)
        self.hedge_manager.set_hedge_closed_callback(self._on_hedge_closed)

    def start(self, symbols: List[str] | None = None):
        """启动运行器"""
        symbols = symbols or DEFAULT_SYMBOLS
        logger = self.bot_logger

        # 记录会话开始
        self._log_session_start(symbols, logger)

        # 测试连接
        if not self._test_connection(logger):
            return

        # 设置双向持仓模式
        self._set_dual_position_mode(logger)

        # 启动插针检测器和持仓监控
        self.detector = PinDetector(symbols)
        self.detector.set_signal_callback(self._on_pin_signal)
        self.detector.set_price_callback(self._on_price_update)
        self.detector.start()
        self.hedge_manager.start_monitoring()

        self.running = True
        self._start_time = time.time()

        # 打印启动信息
        self._print_startup_info(symbols, logger)

        # 注册信号处理
        signal.signal(signal.SIGINT, self._signal_handler)

        # 主循环
        self._main_loop()

    def _log_session_start(self, symbols: List[str], logger: BotLogger):
        """记录会话开始"""
        logger.session_start({
            "symbols": symbols,
            "spike_config": SPIKE_CONFIG,
            "hedge_config": HEDGE_CONFIG,
            "trading_config": {
                "position_usdt": self.config.POSITION_USDT,
                "leverage": self.config.LEVERAGE,
                "fee_rate": self.config.FEE_RATE,
            }
        })

    def _test_connection(self, logger: BotLogger) -> bool:
        """测试交易所连接"""
        logger.info(f"{'='*60}")
        logger.info(f"Flash Arbitrage Bot - 对冲策略（测试网）")
        logger.info(f"{'='*60}")
        logger.info("测试交易所连接...")

        if not self.client.test_connectivity():
            logger.error("无法连接到币安测试网")
            return False

        account = self.client.get_account_info()
        if not account:
            logger.error("无法获取账户信息，请检查API密钥")
            return False

        logger.info(f"连接成功 | 可用余额: {account.available_balance:.2f} USDT")
        self._save_runtime_config(DEFAULT_SYMBOLS)
        self.logger.set_initial_balance(account.available_balance)
        return True

    def _set_dual_position_mode(self, logger: BotLogger):
        """设置双向持仓模式"""
        logger.info("设置双向持仓模式...")
        try:
            result = self.client.set_position_mode(dual_side=True)
            if result:
                logger.info("双向持仓模式已启用")
            else:
                logger.warning("双向持仓模式设置失败（可能已启用）")
        except Exception as e:
            logger.warning(f"持仓模式设置: {e}")

    def _print_startup_info(self, symbols: List[str], logger: BotLogger):
        """打印启动信息"""
        symbols_display = ', '.join(symbols[:5]) + ('...' if len(symbols) > 5 else '')
        logger.info(f"{'='*60}")
        logger.info("对冲策略运行器已启动")
        logger.info(f"监控: {symbols_display}")
        logger.info(f"配置: {self.config.POSITION_USDT} USDT × {self.config.LEVERAGE}x")
        logger.debug(f"对冲回撤: {HEDGE_CONFIG['hedge_retracement_percent']}% | "
                    f"止盈: {HEDGE_CONFIG['take_profit_after_hedge']}% | "
                    f"止损: {HEDGE_CONFIG['stop_loss_after_hedge']}%")
        logger.info(f"{'='*60}")
        logger.info("等待信号...")

    def _main_loop(self):
        """主循环"""
        try:
            last_status_time = 0
            while self.running:
                time.sleep(0.1)

                now = time.time()
                if now - last_status_time >= self.STATUS_INTERVAL_SECONDS:
                    self._print_status()
                    last_status_time = now
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _on_pin_signal(self, signal: PinSignal):
        """处理插针信号"""
        self._signals_count += 1

        # 记录信号到日志
        self.bot_logger.trade_signal({
            "symbol": signal.symbol,
            "direction": signal.direction,
            "amplitude": signal.amplitude,
            "retracement": signal.retracement,
            "entry_price": signal.entry_price
        })

        # 执行对冲策略
        self.hedge_manager.on_pin_signal(signal)

    def _on_price_update(self, symbol: str, price: float, timestamp: datetime):
        """处理价格更新"""
        self.hedge_manager.on_price_update(symbol, price, timestamp)

    def _on_hedge_opened(self, hedge: HedgePosition):
        """对冲完成回调（已在hedge_manager中记录日志）"""
        pass

    def _on_hedge_closed(self, hedge: HedgePosition):
        """对冲平仓回调（已在hedge_manager中记录日志）"""
        pass

    def _print_status(self):
        """打印运行状态"""
        if not self.running:
            return

        logger = self.bot_logger
        elapsed = time.time() - self._start_time if self._start_time else 0
        stats = self.hedge_manager.get_stats()

        logger.info(f"📊 运行 {elapsed/60:.1f}min | "
                   f"信号: {self._signals_count} | "
                   f"等待: {stats['waiting_hedges']} | "
                   f"已对冲: {stats['active_hedges']} | "
                   f"完成: {stats['total_trades']}")

        if stats['total_trades'] > 0:
            pnl = stats['total_pnl']
            pnl_emoji = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
            logger.info(f"   {pnl_emoji} 总盈亏: {pnl:+.4f} USDT | 胜率: {stats['win_rate']:.1f}%")

        ws_status = "🟢" if self.detector.is_connected() else "🔴"
        logger.debug(f"   WebSocket: {ws_status}")

    def stop(self):
        """停止运行器"""
        if not self.running:
            return

        logger = self.bot_logger
        logger.warning("\n正在停止...")
        self.running = False

        # 停止持仓监控器和检测器
        if hasattr(self, 'hedge_manager'):
            self.hedge_manager.stop_monitoring()
        if hasattr(self, 'detector'):
            self.detector.stop()

        # 平掉所有持仓
        self._close_all_positions(logger)

        # 打印最终统计并导出日志
        self._print_final_stats()
        self._export_trade_logs(logger)
        self.logger.print_summary()

        # 记录会话结束
        final_stats = self.hedge_manager.get_stats()
        logger.session_end(final_stats)

    def _close_all_positions(self, logger: BotLogger):
        """平掉所有持仓"""
        stats = self.hedge_manager.get_stats()
        active = stats['waiting_hedges'] + stats['active_hedges']
        if active > 0:
            logger.info(f"平仓 {active} 个持仓...")
            try:
                self.hedge_manager.close_all_positions(reason="shutdown")
                time.sleep(self.CLOSE_WAIT_SECONDS)

                stats_after = self.hedge_manager.get_stats()
                remaining = stats_after['waiting_hedges'] + stats_after['active_hedges']
                if remaining > 0:
                    logger.warning(f"仍有 {remaining} 个持仓未平仓")
            except Exception as e:
                logger.error(f"平仓失败: {e}")

    def _export_trade_logs(self, logger: BotLogger):
        """导出交易日志"""
        logger.info("导出交易数据...")
        try:
            json_path = self.logger.export_to_json()
            logger.info(f"   JSON: {json_path}")
        except Exception as e:
            logger.warning(f"   JSON导出失败: {e}")

        try:
            csv_path = self.logger.export_to_csv()
            logger.info(f"   CSV: {csv_path}")
        except Exception as e:
            logger.warning(f"   CSV导出失败: {e}")

        logger.trade_logger.flush()

    def _print_final_stats(self):
        """打印最终统计"""
        logger = self.bot_logger
        stats = self.hedge_manager.get_stats()
        elapsed = time.time() - self._start_time if self._start_time else 0

        logger.info(f"{'='*60}")
        logger.info("最终统计")
        logger.info(f"   运行时长: {elapsed/60:.1f}分钟")
        logger.info(f"   信号数: {self._signals_count}")
        logger.info(f"   完成交易: {stats['total_trades']}")

        if stats['total_trades'] > 0:
            pnl = stats['total_pnl']
            pnl_emoji = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
            logger.info(f"   {pnl_emoji} 胜率: {stats['win_rate']:.1f}% | 总盈亏: {pnl:+.4f} USDT")

        logger.info(f"{'='*60}")

    def _signal_handler(self, signum, frame):
        """信号处理器"""
        self.bot_logger.warning("收到停止信号，正在安全停止...")
        self.stop()

    def _save_runtime_config(self, symbols: List[str]):
        """保存运行时配置到记录器"""
        config = {
            "script_version": "1.0",
            "start_time": datetime.now(BEIJING_TZ).isoformat(),
            "symbols": symbols,
            "spike_config": SPIKE_CONFIG,
            "hedge_config": HEDGE_CONFIG,
            "trading_config": {
                "position_usdt": self.config.POSITION_USDT,
                "leverage": self.config.LEVERAGE,
                "fee_rate": self.config.FEE_RATE,
                "margin_type": self.config.MARGIN_TYPE,
            },
            "proxy_config": {
                "enable_proxy": self.config.ENABLE_PROXY,
                "proxy_url": self.config.PROXY_URL if self.config.ENABLE_PROXY else None,
            }
        }
        self.logger.set_runtime_config(config)

def main():
    """主函数"""

    # 加载配置
    try:
        config = load_config()
    except ValueError as e:
        print(f"\n❌ 配置错误: {e}")
        print("\n请设置环境变量:")
        print("  export BINANCE_TESTNET_API_KEY=your_api_key")
        print("  export BINANCE_TESTNET_API_SECRET=your_api_secret")
        print("\n或访问 https://testnet.binancefuture.com/ 获取测试网API密钥")
        return

    # 创建运行器
    runner = HedgeStrategyRunner(config)

    # 启动
    runner.start(DEFAULT_SYMBOLS)


if __name__ == "__main__":
    main()
