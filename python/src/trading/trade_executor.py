"""
交易执行器 - 执行测试网交易信号

功能:
- 接收交易信号并执行
- 自动设置止损止盈
- 风控检查
- 交易结果记录
"""

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Callable

from .order_manager import OrderManager, OrderInfo, OrderType
from .position_tracker import PositionTracker, PositionRecord, PositionState
from ..exchange.binance_futures import BinanceFuturesClient


class TradeStatus(Enum):
    """交易状态"""
    PENDING = "pending"           # 待执行
    SUBMITTED = "submitted"       # 已提交
    OPENED = "opened"             # 已开仓
    FAILED = "failed"             # 开仓失败
    CLOSED = "closed"             # 已平仓
    PARTIAL = "partial"           # 部分平仓


@dataclass
class TradeSignal:
    """交易信号"""
    symbol: str                   # 交易对
    side: str                     # LONG/SHORT
    direction: str                # UP/DOWN (插针方向)
    entry_price: float            # 入场价格
    peak_price: float             # 峰值价格
    start_price: float            # 起始价格
    amplitude: float              # 振幅百分比
    retracement: float            # 回撤百分比

    # 止损止盈
    stop_loss_percent: float = 1.5    # 止损百分比
    take_profit_percent: float = 3.0  # 止盈百分比

    # 数量配置
    position_usdt: float = 15.0       # 仓位大小(USDT)
    leverage: int = 20                # 杠杆

    # 元数据
    signal_id: str = ""
    signal_time: float = field(default_factory=time.time)
    source: str = "pin_detector"       # 信号来源
    raw_data: Dict = field(default_factory=dict)

    def get_stop_loss_price(self) -> float:
        """获取止损价格"""
        if self.side == "LONG":
            return self.entry_price * (1 - self.stop_loss_percent / 100)
        else:
            return self.entry_price * (1 + self.stop_loss_percent / 100)

    def get_take_profit_price(self) -> float:
        """获取止盈价格"""
        if self.side == "LONG":
            return self.entry_price * (1 + self.take_profit_percent / 100)
        else:
            return self.entry_price * (1 - self.take_profit_percent / 100)

    def get_position_side(self) -> str:
        """获取持仓方向"""
        return "LONG" if self.side == "LONG" else "SHORT"


@dataclass
class TradeResult:
    """交易结果"""
    trade_id: str
    signal: TradeSignal

    status: TradeStatus = TradeStatus.PENDING

    # 订单
    entry_order: Optional[OrderInfo] = None
    stop_loss_order: Optional[OrderInfo] = None
    take_profit_order: Optional[OrderInfo] = None

    # 持仓
    position: Optional[PositionRecord] = None

    # 执行信息
    submitted_at: float = field(default_factory=time.time)
    opened_at: float = 0
    closed_at: float = 0

    # 结果
    entry_price: float = 0
    exit_price: float = 0
    quantity: float = 0
    realized_pnl: float = 0
    fee_paid: float = 0

    # 错误信息
    error_message: str = ""

    @property
    def duration(self) -> float:
        """持仓时长(秒)"""
        if self.closed_at > 0 and self.opened_at > 0:
            return self.closed_at - self.opened_at
        return 0

    @property
    def pnl_percent(self) -> float:
        """盈亏百分比"""
        if self.entry_price > 0 and self.quantity > 0:
            notional = self.entry_price * self.quantity
            return (self.realized_pnl / notional * 100) if notional > 0 else 0
        return 0

    def is_profitable(self) -> bool:
        """是否盈利"""
        return self.realized_pnl > 0


class TradeExecutor:
    """交易执行器

    接收交易信号，执行入场订单，设置止损止盈，跟踪持仓状态。
    """

    def __init__(
        self,
        exchange_client: BinanceFuturesClient,
        order_manager: OrderManager = None,
        position_tracker: PositionTracker = None,
        config: Dict = None
    ):
        """初始化交易执行器

        Args:
            exchange_client: 交易所客户端
            order_manager: 订单管理器
            position_tracker: 持仓追踪器
            config: 配置参数
        """
        self.client = exchange_client
        self.config = config or self._default_config()

        # 初始化管理器
        self.order_manager = order_manager or OrderManager(exchange_client)
        self.position_tracker = position_tracker or PositionTracker(exchange_client)

        # 交易记录
        self._trades: Dict[str, TradeResult] = {}
        self._trade_counter = 0

        # 风控状态
        self._circuit_breaker_active = False
        self._consecutive_losses = 0
        self._daily_loss = 0
        self._last_reset_time = time.time()

        # 设置回调
        self._setup_callbacks()

        # 统计
        self.stats = {
            "total_signals": 0,
            "executed": 0,
            "failed": 0,
            "winning": 0,
            "losing": 0
        }

    def _default_config(self) -> Dict:
        """默认配置"""
        return {
            "max_position_usdt": 30.0,
            "min_position_usdt": 5.0,
            "max_leverage": 50,
            "max_daily_trades": 50,
            "max_consecutive_losses": 5,
            "max_daily_loss_usdt": 10.0,
            "fee_rate": 0.0004,
            "slippage_tolerance": 0.001,  # 0.1%
            "enable_circuit_breaker": True,
            "circuit_breaker_duration": 300,  # 5分钟
            "enable_stop_loss": True,
            "enable_take_profit": True,
        }

    def _setup_callbacks(self):
        """设置回调函数"""
        self.order_manager.set_order_filled_callback(self._on_order_filled)
        self.order_manager.set_order_failed_callback(self._on_order_failed)
        self.order_manager.set_stop_triggered_callback(self._on_stop_loss_triggered)
        self.order_manager.set_profit_triggered_callback(self._on_take_profit_triggered)

        self.position_tracker.set_risk_warning_callback(self._on_risk_warning)

    # ==================== 交易执行 ====================

    def execute_signal(self, signal: TradeSignal) -> TradeResult:
        """执行交易信号

        Args:
            signal: 交易信号

        Returns:
            TradeResult对象
        """
        self._trade_counter += 1
        trade_id = f"trade_{int(time.time() * 1000)}_{self._trade_counter}"
        signal.signal_id = signal.signal_id or trade_id

        result = TradeResult(trade_id=trade_id, signal=signal)
        self._trades[trade_id] = result
        self.stats["total_signals"] += 1

        # 风控检查
        if not self._check_risk_control(signal):
            result.status = TradeStatus.FAILED
            result.error_message = "风控拒绝"
            self.stats["failed"] += 1
            return result

        # 检查是否有重复持仓
        if self.position_tracker.has_position(signal.symbol):
            result.status = TradeStatus.FAILED
            result.error_message = "已有持仓"
            self.stats["failed"] += 1
            return result

        try:
            # 设置杠杆
            self.client.set_leverage(signal.leverage, signal.symbol)

            # 计算数量
            quantity = self._calculate_quantity(signal)

            # 确定订单方向
            side = "BUY" if signal.side == "LONG" else "SELL"
            position_side = signal.get_position_side()

            # 创建入场订单
            entry_order = self.order_manager.create_order(
                symbol=signal.symbol,
                side=side,
                quantity=quantity,
                order_type=OrderType.ENTRY,
                price=signal.entry_price if signal.entry_price > 0 else None,
                position_side=position_side
            )

            result.entry_order = entry_order

            # 提交入场订单
            if self.order_manager.submit_order(entry_order):
                result.status = TradeStatus.SUBMITTED

                # 如果市价单，通常立即成交
                if entry_order.is_filled:
                    self._on_position_opened(result, entry_order)

                    # 只有成交后才设置止损止盈
                    if self.config["enable_stop_loss"]:
                        stop_price = signal.get_stop_loss_price()
                        result.stop_loss_order = self.order_manager.set_stop_loss(
                            entry_order, stop_price, quantity
                        )
                        self.order_manager.submit_order(result.stop_loss_order)

                    if self.config["enable_take_profit"]:
                        profit_price = signal.get_take_profit_price()
                        result.take_profit_order = self.order_manager.set_take_profit(
                            entry_order, profit_price, quantity
                        )
                        self.order_manager.submit_order(result.take_profit_order)

                self.stats["executed"] += 1
            else:
                result.status = TradeStatus.FAILED
                result.error_message = entry_order.error_message
                self.stats["failed"] += 1

        except Exception as e:
            result.status = TradeStatus.FAILED
            result.error_message = str(e)
            self.stats["failed"] += 1

        return result

    def close_position(self, trade_id: str, reason: str = "manual") -> bool:
        """手动平仓

        Args:
            trade_id: 交易ID
            reason: 平仓原因

        Returns:
            是否成功
        """
        result = self._trades.get(trade_id)
        if not result or not result.position:
            return False

        try:
            # 取消所有挂单
            self.order_manager.cancel_all_orders(result.signal.symbol)

            # 创建平仓订单
            close_side = "SELL" if result.signal.side == "LONG" else "BUY"
            close_order = self.order_manager.create_order(
                symbol=result.signal.symbol,
                side=close_side,
                quantity=result.position.quantity,
                order_type=OrderType.CLOSE,
                position_side=result.position.side
            )

            if self.order_manager.submit_order(close_order):
                return True

        except Exception as e:
            print(f"平仓失败: {e}")

        return False

    def close_all_positions(self, reason: str = "manual") -> int:
        """平仓所有持仓

        Args:
            reason: 平仓原因

        Returns:
            平仓数量
        """
        closed = 0
        for trade_id, result in list(self._trades.items()):
            if result.position and result.position.is_active:
                if self.close_position(trade_id, reason):
                    closed += 1
        return closed

    # ==================== 风控检查 ====================

    def _check_risk_control(self, signal: TradeSignal) -> bool:
        """风控检查

        Args:
            signal: 交易信号

        Returns:
            是否通过
        """
        # 检查熔断器
        if self._circuit_breaker_active:
            if time.time() - self._last_reset_time > self.config["circuit_breaker_duration"]:
                self._circuit_breaker_active = False
                self._consecutive_losses = 0
            else:
                return False

        # 检查连续亏损
        if self._consecutive_losses >= self.config["max_consecutive_losses"]:
            self._activate_circuit_breaker()
            return False

        # 检查每日亏损
        if abs(self._daily_loss) >= self.config["max_daily_loss_usdt"]:
            self._activate_circuit_breaker()
            return False

        # 检查仓位大小
        if signal.position_usdt > self.config["max_position_usdt"]:
            return False

        if signal.position_usdt < self.config["min_position_usdt"]:
            return False

        # 检查杠杆
        if signal.leverage > self.config["max_leverage"]:
            return False

        return True

    def _activate_circuit_breaker(self):
        """激活熔断器"""
        self._circuit_breaker_active = True
        self._last_reset_time = time.time()
        print(f"熔断器已激活，暂停交易 {self.config['circuit_breaker_duration']} 秒")

    def _calculate_quantity(self, signal: TradeSignal) -> float:
        """计算下单数量

        Args:
            signal: 交易信号

        Returns:
            数量
        """
        return self.client.calculate_quantity(
            symbol=signal.symbol,
            usdt_amount=signal.position_usdt,
            price=signal.entry_price,
            leverage=signal.leverage
        )

    # ==================== 订单回调 ====================

    def _on_order_filled(self, order: OrderInfo):
        """订单成交回调"""
        # 查找关联的交易
        for result in self._trades.values():
            if result.entry_order and result.entry_order.order_id == order.order_id:
                self._on_position_opened(result, order)
                break

    def _on_order_failed(self, order: OrderInfo):
        """订单失败回调"""
        print(f"订单失败: {order.symbol} {order.error_message}")

    def _on_position_opened(self, result: TradeResult, order: OrderInfo):
        """持仓开立处理"""
        result.status = TradeStatus.OPENED
        result.opened_at = time.time()
        result.entry_price = order.avg_price or order.price
        result.quantity = order.executed_qty

        # 创建持仓记录
        result.position = self.position_tracker.open_position(
            symbol=result.signal.symbol,
            side=result.signal.side,
            quantity=result.quantity,
            entry_price=result.entry_price,
            leverage=result.signal.leverage,
            entry_order_id=order.order_id
        )

    def _on_stop_loss_triggered(self, order: OrderInfo):
        """止损触发回调"""
        print(f"止损触发: {order.symbol} @ {order.avg_price}")

        # 查找关联交易
        for result in self._trades.values():
            if result.stop_loss_order and result.stop_loss_order.order_id == order.order_id:
                self._finalize_trade(result, order, "stop_loss")
                break

    def _on_take_profit_triggered(self, order: OrderInfo):
        """止盈触发回调"""
        print(f"止盈触发: {order.symbol} @ {order.avg_price}")

        for result in self._trades.values():
            if result.take_profit_order and result.take_profit_order.order_id == order.order_id:
                self._finalize_trade(result, order, "take_profit")
                break

    def _on_risk_warning(self, position: PositionRecord):
        """风险警告回调"""
        liq_distance = position.get_liquidation_distance()
        print(f"风险警告: {position.symbol} 清算距离 {liq_distance:.2f}%")

    def _finalize_trade(self, result: TradeResult, close_order: OrderInfo, exit_reason: str):
        """完成交易

        Args:
            result: 交易结果
            close_order: 平仓订单
            exit_reason: 退出原因
        """
        result.status = TradeStatus.CLOSED
        result.closed_at = time.time()
        result.exit_price = close_order.avg_price
        # 安全地获取realizedPnl - raw_data可能是list或dict
        if close_order.raw_data and isinstance(close_order.raw_data, dict):
            result.realized_pnl = close_order.raw_data.get("realizedPnl", 0)
        else:
            result.realized_pnl = 0

        # 更新持仓
        if result.position:
            self.position_tracker.close_position(
                symbol=result.signal.symbol,
                close_price=result.exit_price,
                realized_pnl=result.realized_pnl
            )

        # 获取实际手续费（从交易所）
        result.fee_paid = self._get_actual_fees(result)

        # 更新统计
        if result.realized_pnl > 0:
            self.stats["winning"] += 1
            self._consecutive_losses = 0
        else:
            self.stats["losing"] += 1
            self._consecutive_losses += 1

        self._daily_loss += result.realized_pnl

        print(f"交易完成: {result.signal.symbol} "
              f"盈亏: {result.realized_pnl:.4f} USDT "
              f"({result.pnl_percent:.2f}%) 原因: {exit_reason}")

    def _get_actual_fees(self, result: TradeResult) -> float:
        """获取实际手续费

        从交易所订单获取实际手续费，而不是本地计算。

        Args:
            result: 交易结果

        Returns:
            实际手续费总额
        """
        total_fee = 0.0

        # 入场单手续费
        if result.entry_order:
            entry_fee = self.order_manager.sync_order_commission(result.entry_order.order_id)
            total_fee += entry_fee

        # 止损单手续费（如果已成交）
        if result.stop_loss_order and result.stop_loss_order.is_filled:
            stop_fee = self.order_manager.sync_order_commission(result.stop_loss_order.order_id)
            total_fee += stop_fee

        # 止盈单手续费（如果已成交）
        if result.take_profit_order and result.take_profit_order.is_filled:
            profit_fee = self.order_manager.sync_order_commission(result.take_profit_order.order_id)
            total_fee += profit_fee

        # 平仓单手续费（如果有）
        # 通过symbol和最近时间查询成交记录
        if total_fee == 0 and result.entry_order:
            # 如果没有获取到手续费，回退到本地计算
            notional = result.entry_price * result.quantity
            total_fee = notional * self.config["fee_rate"] * 2  # 开仓+平仓

        return total_fee

    # ==================== 查询方法 ====================

    def get_trade(self, trade_id: str) -> Optional[TradeResult]:
        """获取交易

        Args:
            trade_id: 交易ID

        Returns:
            TradeResult对象
        """
        return self._trades.get(trade_id)

    def get_active_trades(self) -> List[TradeResult]:
        """获取活跃交易

        Returns:
            TradeResult列表
        """
        return [t for t in self._trades.values() if t.position and t.position.is_active]

    def get_trade_history(self, limit: int = 100) -> List[TradeResult]:
        """获取交易历史

        Args:
            limit: 返回数量限制

        Returns:
            TradeResult列表
        """
        closed_trades = [t for t in self._trades.values() if t.status == TradeStatus.CLOSED]
        return closed_trades[-limit:]

    def get_stats(self) -> Dict:
        """获取统计信息

        Returns:
            统计字典
        """
        position_stats = self.position_tracker.get_stats()

        return {
            "signals": self.stats["total_signals"],
            "executed": self.stats["executed"],
            "failed": self.stats["failed"],
            "active_trades": len(self.get_active_trades()),
            "winning": self.stats["winning"],
            "losing": self.stats["losing"],
            "win_rate": (self.stats["winning"] / max(self.stats["winning"] + self.stats["losing"], 1)) * 100,
            "total_pnl": position_stats.get("total_pnl", 0),
            "realized_pnl": position_stats.get("realized_pnl", 0),
            "consecutive_losses": self._consecutive_losses,
            "circuit_breaker_active": self._circuit_breaker_active
        }

    def cleanup_old_records(self, max_age_seconds: int = 86400):
        """清理旧记录

        Args:
            max_age_seconds: 最大保留时间
        """
        now = time.time()
        to_remove = []

        for trade_id, result in self._trades.items():
            if result.status == TradeStatus.CLOSED:
                if (now - result.closed_at) > max_age_seconds:
                    to_remove.append(trade_id)

        for trade_id in to_remove:
            self._trades.pop(trade_id, None)

        self.order_manager.cleanup_old_orders(max_age_seconds)
