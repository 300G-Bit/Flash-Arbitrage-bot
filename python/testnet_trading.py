"""
测试网交易运行器 - 在币安期货测试网执行模拟交易

功能:
- 监听插针信号并执行交易
- 自动设置止损止盈
- 实时跟踪持仓和盈亏
- 记录交易数据

使用方法:
1. 设置环境变量:
   export BINANCE_TESTNET_API_KEY=your_api_key
   export BINANCE_TESTNET_API_SECRET=your_api_secret

2. 运行脚本:
   python testnet_trading.py

3. 或作为模块导入到test_pin_recorder.py中
"""

import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

# 添加src目录到路径
script_dir = Path(__file__).parent.resolve()
sys.path.insert(0, str(script_dir / "src"))
sys.path.insert(0, str(script_dir))

from src.exchange.binance_futures import BinanceFuturesClient
from src.trading.order_manager import OrderManager
from src.trading.position_tracker import PositionTracker
from src.trading.trade_executor import TradeExecutor, TradeSignal
from src.trading.trade_logger import TradeLogger
from config.testnet_config import load_config, TestnetConfig


class TestnetTradingRunner:
    """测试网交易运行器

    集成所有交易组件，提供完整的测试网交易功能。
    """

    def __init__(self, config: TestnetConfig = None):
        """初始化运行器

        Args:
            config: 测试网配置
        """
        self.config = config or load_config()
        self.running = False

        # 初始化客户端
        self.client = BinanceFuturesClient(
            api_key=self.config.BINANCE_API_KEY,
            api_secret=self.config.BINANCE_API_SECRET,
            testnet=True,
            timeout=self.config.API_TIMEOUT,
            enable_proxy=self.config.ENABLE_PROXY,
            proxy_url=self.config.PROXY_URL if self.config.ENABLE_PROXY else None
        )

        # 初始化组件
        self.order_manager = OrderManager(
            exchange_client=self.client,
            enable_auto_monitor=True,
            monitor_interval=self.config.ORDER_MONITOR_INTERVAL
        )

        self.position_tracker = PositionTracker(
            exchange_client=self.client,
            risk_warning_threshold=self.config.RISK_WARNING_THRESHOLD,
            auto_sync_interval=self.config.POSITION_SYNC_INTERVAL
        )

        self.trade_executor = TradeExecutor(
            exchange_client=self.client,
            order_manager=self.order_manager,
            position_tracker=self.position_tracker,
            config={
                "max_position_usdt": self.config.MAX_POSITION_USDT,
                "min_position_usdt": self.config.MIN_POSITION_USDT,
                "max_leverage": self.config.MAX_LEVERAGE,
                "max_daily_trades": self.config.MAX_DAILY_TRADES,
                "max_consecutive_losses": self.config.MAX_CONSECUTIVE_LOSSES,
                "max_daily_loss_usdt": self.config.MAX_DAILY_LOSS_USDT,
                "fee_rate": self.config.FEE_RATE,
                "slippage_tolerance": self.config.SLIPPAGE_TOLERANCE,
                "enable_circuit_breaker": self.config.ENABLE_CIRCUIT_BREAKER,
                "circuit_breaker_duration": self.config.CIRCUIT_BREAKER_DURATION,
                "enable_stop_loss": True,
                "enable_take_profit": True,
            }
        )

        self.trade_logger = TradeLogger(
            log_dir=self.config.TRADE_LOG_DIR,
            auto_save=self.config.ENABLE_AUTO_SAVE,
            save_interval=self.config.SAVE_INTERVAL
        )

        # 设置回调
        self._setup_callbacks()

        # 统计
        self._start_time = 0
        self._signals_received = 0
        self._signals_executed = 0

    def _setup_callbacks(self):
        """设置回调函数"""
        # 持仓状态回调
        self.position_tracker.set_position_opened_callback(self._on_position_opened)
        self.position_tracker.set_position_closed_callback(self._on_position_closed)
        self.position_tracker.set_risk_warning_callback(self._on_risk_warning)
        self.position_tracker.set_pnl_update_callback(self._on_pnl_update)

    # ==================== 启动停止 ====================

    def start(self):
        """启动交易运行器"""
        if self.running:
            print("交易运行器已在运行中")
            return

        # 测试连接
        if not self._test_connection():
            print("连接测试失败，请检查配置和网络")
            return

        self.running = True
        self._start_time = time.time()

        print("\n" + "=" * 60)
        print("测试网交易运行器已启动")
        print("=" * 60)
        self._print_config()
        print("=" * 60)

        # 设置信号处理
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def stop(self):
        """停止交易运行器"""
        if not self.running:
            return

        print("\n正在停止交易运行器...")
        self.running = False

        # 平仓所有持仓
        active_count = len(self.trade_executor.get_active_trades())
        if active_count > 0:
            print(f"正在平仓 {active_count} 个活跃持仓...")
            closed = self.trade_executor.close_all_positions(reason="shutdown")
            print(f"已平仓 {closed} 个持仓")

        # 停止监控
        self.order_manager.stop_monitoring()

        # 导出数据
        self._export_data()

        print("交易运行器已停止")

    # ==================== 信号处理 ====================

    def on_pin_signal(self, signal_data: Dict) -> Optional[str]:
        """处理插针信号

        Args:
            signal_data: 信号数据，包含:
                - symbol: 交易对
                - direction: UP/DOWN
                - start_price: 起始价格
                - peak_price: 峰值价格
                - entry_price: 入场价格
                - amplitude: 振幅百分比
                - retracement: 回撤百分比

        Returns:
            交易ID，如果未执行则返回None
        """
        if not self.running:
            return None

        self._signals_received += 1

        # 信号验证
        if not self._validate_signal(signal_data):
            return None

        # 创建交易信号
        trade_signal = self._create_trade_signal(signal_data)

        # 执行交易
        result = self.trade_executor.execute_signal(trade_signal)

        if result.status.value in ["opened", "submitted"]:
            self._signals_executed += 1
            print(f"\n✓ 信号已执行: {signal_data['symbol']} {signal_data['direction']}")
            print(f"  入场价: {trade_signal.entry_price}")
            print(f"  止损: {trade_signal.get_stop_loss_price():.6f} ({trade_signal.stop_loss_percent}%)")
            print(f"  止盈: {trade_signal.get_take_profit_price():.6f} ({trade_signal.take_profit_percent}%)")
            return result.trade_id
        else:
            print(f"\n✗ 信号执行失败: {signal_data['symbol']} - {result.error_message}")
            return None

    # ==================== 内部方法 ====================

    def _test_connection(self) -> bool:
        """测试交易所连接

        Returns:
            是否连接成功
        """
        try:
            # 测试REST API
            if not self.client.test_connectivity():
                print("无法连接到币安测试网")
                return False

            # 获取账户信息
            account = self.client.get_account_info()
            if account:
                print(f"✓ 连接成功")
                print(f"  可用余额: {account.available_balance:.2f} USDT")
                print(f"  总余额: {account.total_wallet_balance:.2f} USDT")
                return True
            else:
                print("无法获取账户信息，请检查API密钥")
                return False

        except Exception as e:
            print(f"连接测试失败: {e}")
            return False

    def _validate_signal(self, signal_data: Dict) -> bool:
        """验证信号数据

        Args:
            signal_data: 信号数据

        Returns:
            是否有效
        """
        required_fields = ["symbol", "direction", "start_price", "peak_price", "entry_price", "amplitude", "retracement"]

        for field in required_fields:
            if field not in signal_data:
                print(f"信号缺少必要字段: {field}")
                return False

        # 检查振幅
        if signal_data["amplitude"] < self.config.MIN_SPIKE_PERCENT:
            return False

        if signal_data["amplitude"] > self.config.MAX_SPIKE_PERCENT:
            return False

        # 检查回撤
        if signal_data["retracement"] < self.config.MIN_RETRACEMENT:
            return False

        if signal_data["retracement"] > self.config.MAX_RETRACEMENT:
            return False

        return True

    def _create_trade_signal(self, signal_data: Dict) -> TradeSignal:
        """创建交易信号

        Args:
            signal_data: 信号数据

        Returns:
            TradeSignal对象
        """
        # 确定方向
        direction = signal_data["direction"]
        if direction == "UP":
            side = "LONG"
        elif direction == "DOWN":
            side = "SHORT"
        else:
            # 根据价格变化判断
            if signal_data["peak_price"] > signal_data["start_price"]:
                side = "LONG"
                direction = "UP"
            else:
                side = "SHORT"
                direction = "DOWN"

        return TradeSignal(
            symbol=signal_data["symbol"],
            side=side,
            direction=direction,
            start_price=signal_data["start_price"],
            peak_price=signal_data["peak_price"],
            entry_price=signal_data.get("entry_price", signal_data["peak_price"]),
            amplitude=signal_data["amplitude"],
            retracement=signal_data["retracement"],
            stop_loss_percent=self.config.DEFAULT_STOP_LOSS_PERCENT,
            take_profit_percent=self.config.DEFAULT_TAKE_PROFIT_PERCENT,
            position_usdt=self.config.POSITION_USDT,
            leverage=self.config.LEVERAGE,
            signal_id=signal_data.get("signal_id", ""),
            raw_data=signal_data
        )

    # ==================== 回调处理 ====================

    def _on_position_opened(self, position):
        """持仓开立回调"""
        if self.config.SHOW_POSITION_UPDATES:
            print(f"\n[持仓开立] {position.symbol} {position.side}")
            print(f"  数量: {position.quantity}")
            print(f"  入场价: {position.entry_price}")

    def _on_position_closed(self, position):
        """持仓平仓回调"""
        if self.config.SHOW_POSITION_UPDATES:
            pnl_color = "\033[92m" if position.realized_pnl > 0 else "\033[91m"
            reset = "\033[0m"
            print(f"\n[持仓平仓] {position.symbol}")
            print(f"  盈亏: {pnl_color}{position.realized_pnl:+.4f} USDT{reset} ({position.get_pnl_percent():+.2f}%)")
            print(f"  持仓时长: {position.holding_duration:.1f}秒")

        # 记录交易
        active_trades = self.trade_executor.get_active_trades()
        for trade in active_trades:
            if trade.signal.symbol == position.symbol:
                self.trade_logger.add_trade(trade, exit_reason="closed")
                break

    def _on_risk_warning(self, position):
        """风险警告回调"""
        liq_distance = position.get_liquidation_distance()
        print(f"\n⚠️  风险警告: {position.symbol}")
        print(f"   清算距离: {liq_distance:.2f}%")
        print(f"   未实现盈亏: {position.unrealized_pnl:.4f} USDT")

    def _on_pnl_update(self, position):
        """盈亏更新回调"""
        if self.config.SHOW_PNL_UPDATES and position.is_active:
            print(f"\r[{position.symbol}] 盈亏: {position.unrealized_pnl:+.4f} USDT ({position.get_pnl_percent():+.2f}%)", end="")

    # ==================== 导出和统计 ====================

    def _export_data(self):
        """导出交易数据"""
        print("\n正在导出交易数据...")

        # 导出为多种格式
        json_file = self.trade_logger.export_for_analysis()
        csv_file = self.trade_logger.export_to_csv()

        print(f"  JSON: {json_file}")
        print(f"  CSV: {csv_file}")

    def get_status(self) -> Dict:
        """获取当前状态

        Returns:
            状态字典
        """
        uptime = time.time() - self._start_time if self._start_time > 0 else 0

        return {
            "running": self.running,
            "uptime_seconds": uptime,
            "signals_received": self._signals_received,
            "signals_executed": self._signals_executed,
            "active_trades": len(self.trade_executor.get_active_trades()),
            "total_trades": self.trade_executor.stats["executed"],
            "stats": self.trade_executor.get_stats()
        }

    def print_status(self):
        """打印当前状态"""
        status = self.get_status()

        print("\n" + "-" * 50)
        print("交易运行器状态")
        print("-" * 50)
        print(f"运行时间: {status['uptime_seconds'] / 60:.1f} 分钟")
        print(f"接收信号: {status['signals_received']}")
        print(f"执行交易: {status['signals_executed']}")
        print(f"活跃持仓: {status['active_trades']}")
        print(f"总交易数: {status['total_trades']}")
        print("-" * 50)

        stats = status["stats"]
        if stats["total_pnl"] != 0 or stats["realized_pnl"] != 0:
            print(f"总盈亏: {stats['realized_pnl']:.4f} USDT")
            print(f"胜率: {stats['win_rate']:.1f}%")
        print("-" * 50)

    def _print_config(self):
        """打印配置信息"""
        print(f"交易所: 币安期货测试网")
        print(f"仓位大小: {self.config.POSITION_USDT} USDT")
        print(f"杠杆: {self.config.LEVERAGE}x")
        print(f"止损: {self.config.DEFAULT_STOP_LOSS_PERCENT}%")
        print(f"止盈: {self.config.DEFAULT_TAKE_PROFIT_PERCENT}%")
        print(f"最大每日交易: {self.config.MAX_DAILY_TRADES}")
        print(f"最大连续亏损: {self.config.MAX_CONSECUTIVE_LOSSES}")

    def _signal_handler(self, signum, frame):
        """信号处理器"""
        print(f"\n收到信号 {signum}")
        self.stop()
        sys.exit(0)


# ==================== 示例使用 ====================

def main():
    """主函数 - 示例使用"""
    import random

    # 加载配置
    try:
        config = load_config()
    except ValueError as e:
        print(f"配置错误: {e}")
        return

    # 创建运行器
    runner = TestnetTradingRunner(config)

    # 启动
    runner.start()

    # 模拟运行
    try:
        while runner.running:
            time.sleep(1)

            # 每10秒打印状态
            if int(time.time()) % 10 == 0:
                runner.print_status()

            # 模拟信号 (实际使用时应该从test_pin_recorder.py获取)
            # 示例:
            # signal = {
            #     "symbol": "BTCUSDT",
            #     "direction": "DOWN",
            #     "start_price": 95000,
            #     "peak_price": 94500,
            #     "entry_price": 94700,
            #     "amplitude": 0.5,
            #     "retracement": 30
            # }
            # runner.on_pin_signal(signal)

    except KeyboardInterrupt:
        runner.stop()


if __name__ == "__main__":
    main()
