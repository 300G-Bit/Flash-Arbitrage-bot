"""
订单管理器 - 管理测试网交易订单的生命周期

功能:
- 订单创建与跟踪
- 止损止盈订单管理
- 订单状态监控
- 自动撤单与重试
"""

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Callable
from collections import defaultdict

from ..exchange.binance_futures import BinanceFuturesClient, OrderResult


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "pending"           # 待发送
    SUBMITTED = "submitted"       # 已提交
    PARTIAL_FILLED = "partial"    # 部分成交
    FILLED = "filled"             # 已成交
    CANCELLED = "cancelled"       # 已取消
    REJECTED = "rejected"         # 被拒绝
    EXPIRED = "expired"           # 已过期
    FAILED = "failed"             # 失败


class OrderType(Enum):
    """订单类型"""
    ENTRY = "entry"               # 入场单
    STOP_LOSS = "stop_loss"       # 止损单
    TAKE_PROFIT = "take_profit"   # 止盈单
    CLOSE = "close"               # 平仓单


@dataclass
class OrderInfo:
    """订单信息"""
    order_id: str                 # 本地订单ID
    exchange_order_id: str = ""   # 交易所订单ID
    client_order_id: str = ""     # 客户端订单ID
    symbol: str = ""
    side: str = ""                # BUY/SELL
    order_type: OrderType = OrderType.ENTRY
    status: OrderStatus = OrderStatus.PENDING

    quantity: float = 0           # 委托数量
    executed_qty: float = 0       # 成交数量
    price: float = 0              # 委托价格
    avg_price: float = 0          # 成交均价

    stop_price: float = 0         # 止损/止盈触发价
    position_side: str = ""       # LONG/SHORT

    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    filled_at: float = 0

    parent_order_id: str = ""     # 父订单ID(用于止盈止损关联)
    child_orders: List[str] = field(default_factory=list)  # 子订单ID列表

    commission: float = 0         # 实际手续费(从交易所获取)

    error_message: str = ""
    raw_data: Dict = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        """是否为活跃订单"""
        return self.status in [
            OrderStatus.PENDING,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIAL_FILLED
        ]

    @property
    def is_filled(self) -> bool:
        """是否已成交"""
        return self.status == OrderStatus.FILLED

    @property
    def fill_ratio(self) -> float:
        """成交比例"""
        if self.quantity <= 0:
            return 0
        return self.executed_qty / self.quantity

    def update_from_exchange(self, exchange_order: OrderResult):
        """从交易所订单更新"""
        self.exchange_order_id = exchange_order.order_id
        self.client_order_id = exchange_order.client_order_id
        self.executed_qty = exchange_order.execute_qty
        self.avg_price = exchange_order.avg_price or 0

        # 映射状态
        status_map = {
            "NEW": OrderStatus.SUBMITTED,
            "PARTIALLY_FILLED": OrderStatus.PARTIAL_FILLED,
            "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.REJECTED,
            "EXPIRED": OrderStatus.EXPIRED,
        }
        self.status = status_map.get(exchange_order.status, OrderStatus.PENDING)
        self.updated_at = time.time()

        # 更新手续费（如果OrderResult中有）
        if exchange_order.commission > 0:
            self.commission = exchange_order.commission

        if self.is_filled:
            self.filled_at = self.updated_at


class OrderManager:
    """订单管理器

    管理所有订单的生命周期，包括入场单、止损单、止盈单。
    支持订单状态监控和自动止损止盈触发。
    """

    def __init__(
        self,
        exchange_client: BinanceFuturesClient,
        enable_auto_monitor: bool = True,
        monitor_interval: float = 0.5
    ):
        """初始化订单管理器

        Args:
            exchange_client: 交易所客户端
            enable_auto_monitor: 是否启用自动监控
            monitor_interval: 监控间隔(秒)
        """
        self.client = exchange_client
        self.enable_auto_monitor = enable_auto_monitor
        self.monitor_interval = monitor_interval

        # 线程锁 - 保护共享字典
        self._lock = threading.Lock()

        # 订单存储
        self._orders: Dict[str, OrderInfo] = {}
        self._orders_by_symbol: Dict[str, Dict[str, OrderInfo]] = defaultdict(dict)
        self._orders_by_exchange_id: Dict[str, str] = {}  # exchange_id -> local_id

        # 回调函数
        self._on_order_filled: Optional[Callable[[OrderInfo], None]] = None
        self._on_order_cancelled: Optional[Callable[[OrderInfo], None]] = None
        self._on_order_failed: Optional[Callable[[OrderInfo], None]] = None
        self._on_stop_triggered: Optional[Callable[[OrderInfo], None]] = None
        self._on_profit_triggered: Optional[Callable[[OrderInfo], None]] = None

        # 监控线程
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        if enable_auto_monitor:
            self.start_monitoring()

    def set_order_filled_callback(self, callback: Callable[[OrderInfo], None]):
        """设置订单成交回调"""
        self._on_order_filled = callback

    def set_order_cancelled_callback(self, callback: Callable[[OrderInfo], None]):
        """设置订单取消回调"""
        self._on_order_cancelled = callback

    def set_order_failed_callback(self, callback: Callable[[OrderInfo], None]):
        """设置订单失败回调"""
        self._on_order_failed = callback

    def set_stop_triggered_callback(self, callback: Callable[[OrderInfo], None]):
        """设置止损触发回调"""
        self._on_stop_triggered = callback

    def set_profit_triggered_callback(self, callback: Callable[[OrderInfo], None]):
        """设置止盈触发回调"""
        self._on_profit_triggered = callback

    # ==================== 订单管理 ====================

    def create_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: OrderType = OrderType.ENTRY,
        price: float = None,
        stop_price: float = None,
        position_side: str = None,
        parent_order_id: str = None
    ) -> OrderInfo:
        """创建订单

        Args:
            symbol: 交易对
            side: 方向 (BUY/SELL)
            quantity: 数量
            order_type: 订单类型
            price: 价格(限价单)
            stop_price: 止损/止盈价
            position_side: 持仓方向
            parent_order_id: 父订单ID

        Returns:
            OrderInfo对象
        """
        with self._lock:
            order_id = self._generate_order_id()

            order = OrderInfo(
                order_id=order_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price or 0,
                stop_price=stop_price or 0,
                position_side=position_side or "",
                parent_order_id=parent_order_id or ""
            )

            self._orders[order_id] = order
            self._orders_by_symbol[symbol][order_id] = order

            if parent_order_id and parent_order_id in self._orders:
                self._orders[parent_order_id].child_orders.append(order_id)

            return order

    def submit_order(self, order: OrderInfo) -> bool:
        """提交订单到交易所

        Args:
            order: 订单对象

        Returns:
            是否提交成功
        """
        order.status = OrderStatus.SUBMITTED
        order.updated_at = time.time()

        try:
            result = None

            if order.order_type == OrderType.ENTRY:
                if order.price > 0:
                    result = self.client.place_limit_order(
                        symbol=order.symbol,
                        side=order.side,
                        quantity=order.quantity,
                        price=order.price,
                        position_side=order.position_side
                    )
                else:
                    result = self.client.place_market_order(
                        symbol=order.symbol,
                        side=order.side,
                        quantity=order.quantity,
                        position_side=order.position_side
                    )

            elif order.order_type == OrderType.STOP_LOSS:
                result = self.client.place_stop_market_order(
                    symbol=order.symbol,
                    side=order.side,
                    stop_price=order.stop_price,
                    position_side=order.position_side,
                    close_position=True
                )

            elif order.order_type == OrderType.TAKE_PROFIT:
                result = self.client.place_take_profit_order(
                    symbol=order.symbol,
                    side=order.side,
                    stop_price=order.stop_price,
                    position_side=order.position_side,
                    close_position=True
                )

            elif order.order_type == OrderType.CLOSE:
                result = self.client.place_market_order(
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    position_side=order.position_side,
                    reduce_only=True
                )

            if result:
                # 先检查订单是否被拒绝（在update_from_exchange之前）
                if result.status == "REJECTED":
                    order.status = OrderStatus.REJECTED
                    # 从raw数据获取错误信息
                    if result.raw and isinstance(result.raw, dict):
                        code = result.raw.get("code")
                        msg = result.raw.get("msg")
                        if code is not None and msg:
                            order.error_message = f"错误{code}: {msg}"
                        else:
                            order.error_message = "订单被交易所拒绝"
                    else:
                        order.error_message = "订单被交易所拒绝"
                    self._trigger_failed_callback(order)
                    return False

                order.update_from_exchange(result)

                # 对于市价单，立即查询获取实际成交价格
                is_market_order = (
                    order.order_type == OrderType.ENTRY and order.price == 0
                ) or order.order_type in [OrderType.STOP_LOSS, OrderType.TAKE_PROFIT, OrderType.CLOSE]

                if is_market_order and result.order_id and order.status != OrderStatus.FILLED:
                    # 等待一小段时间后查询订单状态
                    import time as _time
                    _time.sleep(0.1)
                    updated = self.client.get_order(
                        order.symbol,
                        order_id=result.order_id
                    )
                    if updated:
                        order.update_from_exchange(updated)

                self._orders_by_exchange_id[result.order_id] = order.order_id

                if order.is_filled:
                    self._trigger_filled_callback(order)
                return True
            else:
                order.status = OrderStatus.FAILED
                order.error_message = "提交失败，未收到有效响应"
                self._trigger_failed_callback(order)
                return False

        except Exception as e:
            order.status = OrderStatus.FAILED
            order.error_message = str(e)
            self._trigger_failed_callback(order)
            return False

    def cancel_order(self, order: OrderInfo) -> bool:
        """取消订单

        Args:
            order: 订单对象

        Returns:
            是否取消成功
        """
        if not order.is_active:
            return False

        try:
            if order.exchange_order_id:
                success = self.client.cancel_order(
                    symbol=order.symbol,
                    order_id=order.exchange_order_id
                )
            elif order.client_order_id:
                success = self.client.cancel_order(
                    symbol=order.symbol,
                    client_order_id=order.client_order_id
                )
            else:
                return False

            if success:
                order.status = OrderStatus.CANCELLED
                order.updated_at = time.time()
                self._trigger_cancelled_callback(order)
                return True

        except Exception:
            pass

        return False

    def cancel_all_orders(self, symbol: str = None) -> int:
        """取消所有订单

        Args:
            symbol: 交易对，None则取消所有

        Returns:
            取消成功的订单数量
        """
        if symbol:
            cancelled = self.client.cancel_all_orders(symbol)
            # 更新本地状态
            for order in list(self._orders_by_symbol.get(symbol, {}).values()):
                if order.is_active:
                    order.status = OrderStatus.CANCELLED
                    self._trigger_cancelled_callback(order)
            return len(self._orders_by_symbol.get(symbol, {}))
        else:
            count = 0
            for sym in list(self._orders_by_symbol.keys()):
                count += self.cancel_all_orders(sym)
            return count

    # ==================== 止损止盈管理 ====================

    def set_stop_loss(
        self,
        entry_order: OrderInfo,
        stop_price: float,
        quantity: float = None
    ) -> OrderInfo:
        """设置止损单

        Args:
            entry_order: 入场订单
            stop_price: 止损价格
            quantity: 止损数量，None则全平

        Returns:
            止损订单对象
        """
        # 确定平仓方向
        close_side = "SELL" if entry_order.side == "BUY" else "BUY"
        qty = quantity or entry_order.quantity

        stop_order = self.create_order(
            symbol=entry_order.symbol,
            side=close_side,
            quantity=qty,
            order_type=OrderType.STOP_LOSS,
            stop_price=stop_price,
            position_side=entry_order.position_side,
            parent_order_id=entry_order.order_id
        )

        return stop_order

    def set_take_profit(
        self,
        entry_order: OrderInfo,
        profit_price: float,
        quantity: float = None
    ) -> OrderInfo:
        """设置止盈单

        Args:
            entry_order: 入场订单
            profit_price: 止盈价格
            quantity: 止盈数量，None则全平

        Returns:
            止盈订单对象
        """
        close_side = "SELL" if entry_order.side == "BUY" else "BUY"
        qty = quantity or entry_order.quantity

        profit_order = self.create_order(
            symbol=entry_order.symbol,
            side=close_side,
            quantity=qty,
            order_type=OrderType.TAKE_PROFIT,
            stop_price=profit_price,
            position_side=entry_order.position_side,
            parent_order_id=entry_order.order_id
        )

        return profit_order

    def set_bracket_order(
        self,
        entry_order: OrderInfo,
        stop_loss_price: float,
        take_profit_price: float,
        stop_loss_qty: float = None,
        take_profit_qty: float = None
    ) -> Dict[str, OrderInfo]:
        """设置 bracket 订单(入场+止损+止盈)

        Args:
            entry_order: 入场订单
            stop_loss_price: 止损价格
            take_profit_price: 止盈价格
            stop_loss_qty: 止损数量
            take_profit_qty: 止盈数量

        Returns:
            包含 entry, stop_loss, take_profit 的字典
        """
        stop_order = self.set_stop_loss(entry_order, stop_loss_price, stop_loss_qty)
        profit_order = self.set_take_profit(entry_order, take_profit_price, take_profit_qty)

        return {
            "entry": entry_order,
            "stop_loss": stop_order,
            "take_profit": profit_order
        }

    # ==================== 查询方法 ====================

    def get_order(self, order_id: str) -> Optional[OrderInfo]:
        """获取订单

        Args:
            order_id: 本地订单ID

        Returns:
            OrderInfo对象
        """
        return self._orders.get(order_id)

    def get_order_by_exchange_id(self, exchange_order_id: str) -> Optional[OrderInfo]:
        """通过交易所订单ID获取订单

        Args:
            exchange_order_id: 交易所订单ID

        Returns:
            OrderInfo对象
        """
        local_id = self._orders_by_exchange_id.get(exchange_order_id)
        return self._orders.get(local_id) if local_id else None

    def get_orders_by_symbol(self, symbol: str, active_only: bool = False) -> List[OrderInfo]:
        """获取交易对的订单

        Args:
            symbol: 交易对
            active_only: 是否只返回活跃订单

        Returns:
            OrderInfo列表
        """
        orders = list(self._orders_by_symbol.get(symbol, {}).values())
        if active_only:
            orders = [o for o in orders if o.is_active]
        return orders

    def get_active_orders(self) -> List[OrderInfo]:
        """获取所有活跃订单

        Returns:
            OrderInfo列表
        """
        return [o for o in self._orders.values() if o.is_active]

    def get_child_orders(self, parent_order_id: str) -> List[OrderInfo]:
        """获取子订单

        Args:
            parent_order_id: 父订单ID

        Returns:
            子订单列表
        """
        parent = self._orders.get(parent_order_id)
        if not parent:
            return []
        return [self._orders.get(oid) for oid in parent.child_orders if oid in self._orders]

    def sync_from_exchange(self, symbol: str = None):
        """从交易所同步订单状态

        Args:
            symbol: 交易对，None则同步所有
        """
        try:
            open_orders = self.client.get_open_orders(symbol)
            current_exchange_ids = {o.order_id for o in open_orders}

            # 更新本地订单状态
            for order in self._orders.values():
                if symbol and order.symbol != symbol:
                    continue

                if order.exchange_order_id in current_exchange_ids:
                    # 在交易所仍然存在，更新信息
                    for ex_order in open_orders:
                        if ex_order.order_id == order.exchange_order_id:
                            order.update_from_exchange(ex_order)
                            if order.is_filled:
                                self._trigger_filled_callback(order)
                            break
                elif order.is_active and order.exchange_order_id:
                    # 在交易所不存在了，可能是成交或被取消
                    # 从交易所查询最终状态
                    remote_order = self.client.get_order(
                        order.symbol,
                        order_id=order.exchange_order_id,
                        client_order_id=order.client_order_id
                    )
                    if remote_order:
                        order.update_from_exchange(remote_order)
                        if order.is_filled:
                            self._trigger_filled_callback(order)
                        elif order.status == OrderStatus.CANCELLED:
                            self._trigger_cancelled_callback(order)

        except Exception as e:
            print(f"同步订单状态失败: {e}")

    def sync_order_commission(self, order_id: str) -> float:
        """同步订单的实际手续费

        从交易所获取订单的成交记录，汇总实际手续费。

        Args:
            order_id: 本地订单ID

        Returns:
            总手续费金额
        """
        order = self._orders.get(order_id)
        if not order or not order.exchange_order_id or not order.symbol:
            return 0

        try:
            total_commission = self.client.get_order_commission(
                symbol=order.symbol,
                order_id=order.exchange_order_id
            )
            order.commission = total_commission
            return total_commission
        except Exception as e:
            print(f"获取订单手续费失败 {order_id}: {e}")
            return 0

    # ==================== 监控 ====================

    def start_monitoring(self):
        """启动监控线程"""
        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._monitor_thread.start()

    def stop_monitoring(self):
        """停止监控线程"""
        self._stop_event.set()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)

    def _monitor_loop(self):
        """监控循环"""
        while not self._stop_event.is_set():
            try:
                # 定期同步活跃订单状态
                active_symbols = set(o.symbol for o in self.get_active_orders())
                for symbol in active_symbols:
                    self.sync_from_exchange(symbol)

                time.sleep(self.monitor_interval)

            except Exception as e:
                print(f"监控线程错误: {e}")
                time.sleep(1)

    # ==================== 回调触发 ====================

    def _trigger_filled_callback(self, order: OrderInfo):
        """触发成交回调"""
        if self._on_order_filled:
            try:
                self._on_order_filled(order)
            except Exception as e:
                print(f"成交回调错误: {e}")

        # 触发特定类型回调
        if order.order_type == OrderType.STOP_LOSS and self._on_stop_triggered:
            try:
                self._on_stop_triggered(order)
            except Exception as e:
                print(f"止损触发回调错误: {e}")
        elif order.order_type == OrderType.TAKE_PROFIT and self._on_profit_triggered:
            try:
                self._on_profit_triggered(order)
            except Exception as e:
                print(f"止盈触发回调错误: {e}")

    def _trigger_cancelled_callback(self, order: OrderInfo):
        """触发取消回调"""
        if self._on_order_cancelled:
            try:
                self._on_order_cancelled(order)
            except Exception as e:
                print(f"取消回调错误: {e}")

    def _trigger_failed_callback(self, order: OrderInfo):
        """触发失败回调"""
        if self._on_order_failed:
            try:
                self._on_order_failed(order)
            except Exception as e:
                print(f"失败回调错误: {e}")

    # ==================== 工具方法 ====================

    def _generate_order_id(self) -> str:
        """生成唯一订单ID"""
        return f"ord_{int(time.time() * 1000000)}"

    def get_stats(self) -> Dict:
        """获取统计信息

        Returns:
            统计数据字典
        """
        total = len(self._orders)
        by_status = defaultdict(int)
        by_type = defaultdict(int)

        for order in self._orders.values():
            by_status[order.status.value] += 1
            by_type[order.order_type.value] += 1

        return {
            "total_orders": total,
            "active_orders": len(self.get_active_orders()),
            "by_status": dict(by_status),
            "by_type": dict(by_type)
        }

    def cleanup_old_orders(self, max_age_seconds: int = 86400):
        """清理旧订单

        Args:
            max_age_seconds: 最大保留时间(秒)
        """
        now = time.time()
        to_remove = []

        for order_id, order in self._orders.items():
            if not order.is_active and (now - order.updated_at) > max_age_seconds:
                to_remove.append(order_id)

        for order_id in to_remove:
            self._remove_order(order_id)

    def _remove_order(self, order_id: str):
        """移除订单"""
        if order_id not in self._orders:
            return

        order = self._orders[order_id]

        # 从symbol索引移除
        if order.symbol in self._orders_by_symbol:
            self._orders_by_symbol[order.symbol].pop(order_id, None)
            if not self._orders_by_symbol[order.symbol]:
                self._orders_by_symbol.pop(order.symbol)

        # 从exchange_id索引移除
        if order.exchange_order_id:
            self._orders_by_exchange_id.pop(order.exchange_order_id, None)

        # 从主存储移除
        del self._orders[order_id]
