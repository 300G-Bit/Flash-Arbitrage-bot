"""
持仓追踪器 - 跟踪测试网交易持仓状态

功能:
- 持仓信息实时跟踪
- 未实现盈亏计算
- 持仓风险监控
- 持仓历史记录
"""

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Callable

from ..exchange.binance_futures import BinanceFuturesClient, Position


class PositionState(Enum):
    """持仓状态"""
    OPENING = "opening"       # 开仓中
    OPEN = "open"             # 已开仓
    CLOSING = "closing"       # 平仓中
    CLOSED = "closed"         # 已平仓


@dataclass
class PositionRecord:
    """持仓记录"""
    symbol: str
    side: str                 # LONG/SHORT
    state: PositionState = PositionState.OPEN

    # 数量和价格
    quantity: float = 0
    entry_price: float = 0
    current_price: float = 0
    liquidation_price: float = 0
    mark_price: float = 0

    # 盈亏
    unrealized_pnl: float = 0
    realized_pnl: float = 0

    # 杠杆和保证金
    leverage: int = 1
    margin_type: str = ""     # ISOLATED/CROSSED
    isolated_margin: float = 0

    # 关联订单
    entry_order_id: str = ""
    stop_loss_order_id: str = ""
    take_profit_order_id: str = ""

    # 时间戳
    opened_at: float = field(default_factory=time.time)
    closed_at: float = 0
    updated_at: float = field(default_factory=time.time)

    # 统计
    max_profit: float = 0
    max_loss: float = 0
    max_favorable_pnl: float = 0
    max_adverse_pnl: float = 0

    def update_price(self, current_price: float, mark_price: float = None):
        """更新当前价格并计算盈亏

        Args:
            current_price: 当前价格
            mark_price: 标记价格
        """
        self.current_price = current_price
        self.mark_price = mark_price or current_price
        self.updated_at = time.time()

        if self.quantity > 0:
            if self.side == "LONG":
                self.unrealized_pnl = (self.current_price - self.entry_price) * self.quantity
            else:  # SHORT
                self.unrealized_pnl = (self.entry_price - self.current_price) * self.quantity

            # 更新最大盈亏
            if self.unrealized_pnl > self.max_profit:
                self.max_profit = self.unrealized_pnl
            if self.unrealized_pnl < self.max_loss:
                self.max_loss = self.unrealized_pnl

    def get_pnl_percent(self) -> float:
        """获取盈亏百分比"""
        if self.quantity <= 0 or self.entry_price <= 0:
            return 0
        notional = self.entry_price * self.quantity
        return (self.unrealized_pnl / notional * 100) if notional > 0 else 0

    def get_liquidation_distance(self) -> float:
        """获取清算距离百分比"""
        if self.side == "LONG":
            if self.current_price > self.liquidation_price and self.liquidation_price > 0:
                return ((self.current_price - self.liquidation_price) / self.current_price) * 100
        else:  # SHORT
            if self.liquidation_price > self.current_price and self.liquidation_price > 0:
                return ((self.liquidation_price - self.current_price) / self.current_price) * 100
        return 0

    @property
    def is_active(self) -> bool:
        """是否为活跃持仓"""
        return self.state in [PositionState.OPENING, PositionState.OPEN]

    @property
    def holding_duration(self) -> float:
        """持仓时长(秒)"""
        if self.closed_at > 0:
            return self.closed_at - self.opened_at
        return time.time() - self.opened_at


class PositionTracker:
    """持仓追踪器

    跟踪所有持仓的实时状态和盈亏。
    """

    def __init__(
        self,
        exchange_client: BinanceFuturesClient,
        risk_warning_threshold: float = 0.7,
        auto_sync_interval: float = 1.0
    ):
        """初始化持仓追踪器

        Args:
            exchange_client: 交易所客户端
            risk_warning_threshold: 风险警告阈值(清算距离百分比)
            auto_sync_interval: 自动同步间隔(秒)
        """
        self.client = exchange_client
        self.risk_warning_threshold = risk_warning_threshold
        self.auto_sync_interval = auto_sync_interval

        # 持仓存储
        self._positions: Dict[str, PositionRecord] = {}  # symbol -> PositionRecord
        self._position_history: List[PositionRecord] = []

        # 回调函数
        self._on_position_opened: Optional[Callable[[PositionRecord], None]] = None
        self._on_position_closed: Optional[Callable[[PositionRecord], None]] = None
        self._on_risk_warning: Optional[Callable[[PositionRecord], None]] = None
        self._on_pnl_update: Optional[Callable[[PositionRecord], None]] = None

        # 风险状态
        self._risk_warnings: Dict[str, bool] = {}

    # ==================== 回调设置 ====================

    def set_position_opened_callback(self, callback: Callable[[PositionRecord], None]):
        """设置开仓回调"""
        self._on_position_opened = callback

    def set_position_closed_callback(self, callback: Callable[[PositionRecord], None]):
        """设置平仓回调"""
        self._on_position_closed = callback

    def set_risk_warning_callback(self, callback: Callable[[PositionRecord], None]):
        """设置风险警告回调"""
        self._on_risk_warning = callback

    def set_pnl_update_callback(self, callback: Callable[[PositionRecord], None]):
        """设置盈亏更新回调"""
        self._on_pnl_update = callback

    # ==================== 持仓操作 ====================

    def open_position(
        self,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        leverage: int = 1,
        margin_type: str = "ISOLATED",
        entry_order_id: str = ""
    ) -> PositionRecord:
        """开仓

        Args:
            symbol: 交易对
            side: 方向 (LONG/SHORT)
            quantity: 数量
            entry_price: 入场价格
            leverage: 杠杆
            margin_type: 保证金模式
            entry_order_id: 入场订单ID

        Returns:
            PositionRecord对象
        """
        # 获取交易所持仓信息
        exchange_positions = self.client.get_position(symbol)
        liq_price = 0
        isolated_margin = 0

        for ep in exchange_positions:
            if ep.symbol == symbol:
                liq_price = ep.liquidation_price
                isolated_margin = ep.isolated_margin
                break

        position = PositionRecord(
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            current_price=entry_price,
            liquidation_price=liq_price,
            leverage=leverage,
            margin_type=margin_type,
            isolated_margin=isolated_margin,
            entry_order_id=entry_order_id,
            state=PositionState.OPEN
        )

        self._positions[symbol] = position
        self._risk_warnings[symbol] = False

        if self._on_position_opened:
            try:
                self._on_position_opened(position)
            except Exception as e:
                print(f"开仓回调错误: {e}")

        return position

    def close_position(
        self,
        symbol: str,
        close_price: float = None,
        realized_pnl: float = 0
    ) -> Optional[PositionRecord]:
        """平仓

        Args:
            symbol: 交易对
            close_price: 平仓价格
            realized_pnl: 已实现盈亏

        Returns:
            平仓的PositionRecord，不存在则返回None
        """
        position = self._positions.get(symbol)
        if not position:
            return None

        position.state = PositionState.CLOSED
        position.closed_at = time.time()
        position.realized_pnl = realized_pnl

        if close_price:
            position.current_price = close_price

        # 移入历史记录
        self._position_history.append(position)
        self._positions.pop(symbol, None)
        self._risk_warnings.pop(symbol, None)

        if self._on_position_closed:
            try:
                self._on_position_closed(position)
            except Exception as e:
                print(f"平仓回调错误: {e}")

        return position

    def update_position(self, symbol: str, current_price: float = None, mark_price: float = None):
        """更新持仓价格和盈亏

        Args:
            symbol: 交易对
            current_price: 当前价格
            mark_price: 标记价格
        """
        position = self._positions.get(symbol)
        if not position:
            return

        # 如果没提供价格，从交易所获取
        if current_price is None:
            ticker = self.client.get_ticker_price(symbol)
            current_price = float(ticker.get("price", 0)) if ticker else 0

        if mark_price is None:
            positions = self.client.get_position(symbol)
            for p in positions:
                if p.symbol == symbol:
                    mark_price = p.mark_price
                    position.liquidation_price = p.liquidation_price
                    break

        position.update_price(current_price, mark_price)

        # 检查风险
        self._check_risk(position)

        # 触发盈亏更新回调
        if self._on_pnl_update:
            try:
                self._on_pnl_update(position)
            except Exception as e:
                print(f"盈亏更新回调错误: {e}")

    def sync_from_exchange(self):
        """从交易所同步所有持仓"""
        try:
            exchange_positions = self.client.get_position()

            # 获取当前有持仓的交易对
            active_symbols = {p.symbol for p in exchange_positions}

            # 更新现有持仓
            for ep in exchange_positions:
                symbol = ep.symbol
                if symbol in self._positions:
                    position = self._positions[symbol]
                    position.current_price = ep.mark_price
                    position.mark_price = ep.mark_price
                    position.liquidation_price = ep.liquidation_price
                    position.quantity = abs(ep.position_amount)  # 确保为正
                    position.update_price(ep.mark_price, ep.mark_price)

                    # 检查风险
                    self._check_risk(position)

            # 移除已平仓的
            for symbol in list(self._positions.keys()):
                if symbol not in active_symbols:
                    # 从交易所获取历史盈亏
                    close_price = self._positions[symbol].current_price
                    self.close_position(symbol, close_price)

        except Exception as e:
            print(f"同步持仓失败: {e}")

    # ==================== 查询方法 ====================

    def get_position(self, symbol: str) -> Optional[PositionRecord]:
        """获取持仓

        Args:
            symbol: 交易对

        Returns:
            PositionRecord对象
        """
        return self._positions.get(symbol)

    def get_all_positions(self) -> List[PositionRecord]:
        """获取所有活跃持仓

        Returns:
            PositionRecord列表
        """
        return list(self._positions.values())

    def get_position_history(self, limit: int = 100) -> List[PositionRecord]:
        """获取持仓历史

        Args:
            limit: 返回数量限制

        Returns:
            PositionRecord列表
        """
        return self._position_history[-limit:]

    def has_position(self, symbol: str) -> bool:
        """是否有持仓

        Args:
            symbol: 交易对

        Returns:
            是否有持仓
        """
        return symbol in self._positions

    def get_total_pnl(self) -> Dict:
        """获取总盈亏统计

        Returns:
            盈亏统计字典
        """
        total_unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        total_realized = sum(p.realized_pnl for p in self._position_history)

        return {
            "unrealized_pnl": total_unrealized,
            "realized_pnl": total_realized,
            "total_pnl": total_unrealized + total_realized,
            "active_positions": len(self._positions),
            "closed_positions": len(self._position_history)
        }

    def get_position_summary(self, symbol: str) -> Optional[Dict]:
        """获取持仓摘要

        Args:
            symbol: 交易对

        Returns:
            持仓摘要字典
        """
        position = self._positions.get(symbol)
        if not position:
            return None

        return {
            "symbol": position.symbol,
            "side": position.side,
            "quantity": position.quantity,
            "entry_price": position.entry_price,
            "current_price": position.current_price,
            "leverage": position.leverage,
            "unrealized_pnl": position.unrealized_pnl,
            "pnl_percent": position.get_pnl_percent(),
            "holding_duration": position.holding_duration,
            "liquidation_distance": position.get_liquidation_distance(),
            "max_profit": position.max_profit,
            "max_loss": position.max_loss
        }

    # ==================== 风险检查 ====================

    def _check_risk(self, position: PositionRecord):
        """检查持仓风险

        Args:
            position: 持仓记录
        """
        liq_distance = position.get_liquidation_distance()
        symbol = position.symbol

        # 清算距离低于阈值，触发警告
        if liq_distance < self.risk_warning_threshold:
            if not self._risk_warnings.get(symbol, False):
                self._risk_warnings[symbol] = True
                if self._on_risk_warning:
                    try:
                        self._on_risk_warning(position)
                    except Exception as e:
                        print(f"风险警告回调错误: {e}")
        else:
            self._risk_warnings[symbol] = False

    def is_at_risk(self, symbol: str = None) -> bool:
        """检查是否有持仓处于风险中

        Args:
            symbol: 交易对，None则检查所有

        Returns:
            是否有风险
        """
        if symbol:
            return self._risk_warnings.get(symbol, False)
        return any(self._risk_warnings.values())

    def get_risk_positions(self) -> List[PositionRecord]:
        """获取有风险的持仓

        Returns:
            PositionRecord列表
        """
        return [
            p for s, p in self._positions.items()
            if self._risk_warnings.get(s, False)
        ]

    # ==================== 统计 ====================

    def get_stats(self) -> Dict:
        """获取统计信息

        Returns:
            统计字典
        """
        if not self._position_history:
            return {
                "total_trades": 0,
                "win_rate": 0,
                "total_pnl": 0,
                "avg_pnl": 0,
                "max_profit": 0,
                "max_loss": 0
            }

        total_trades = len(self._position_history)
        winning_trades = sum(1 for p in self._position_history if p.realized_pnl > 0)
        total_pnl = sum(p.realized_pnl for p in self._position_history)
        max_profit = max((p.realized_pnl for p in self._position_history), default=0)
        max_loss = min((p.realized_pnl for p in self._position_history), default=0)

        return {
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": total_trades - winning_trades,
            "win_rate": (winning_trades / total_trades * 100) if total_trades > 0 else 0,
            "total_pnl": total_pnl,
            "avg_pnl": total_pnl / total_trades if total_trades > 0 else 0,
            "max_profit": max_profit,
            "max_loss": max_loss
        }
