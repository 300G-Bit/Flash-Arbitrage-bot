"""
持仓监控器 - 监控对冲持仓状态、盈亏和动态止损

功能:
- 实时监控持仓盈亏
- 亏损超过阈值时自动止损
- 达到止盈后调整止损位到保本位
- 继续盈利时追踪止损位
"""

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable
from threading import Lock

from .hedge_types import HedgePosition, HedgeState
from ..exchange.binance_futures import BinanceFuturesClient


class PositionMonitor:
    """持仓监控器

    监控对冲持仓的盈亏和状态，提供动态止损功能。
    """

    def __init__(
        self,
        client: BinanceFuturesClient,
        max_loss_usdt: float = 999.0,  # 提高阈值，基本不触发（由止损订单处理）
        trailing_stop_percent: float = 0.3,
        check_interval: float = 1.0
    ):
        """初始化监控器

        Args:
            client: 交易所客户端
            max_loss_usdt: 最大亏损额度（USDT），超过则自动止损
            trailing_stop_percent: 追踪止损回调百分比
            check_interval: 检查间隔（秒）
        """
        self.client = client
        self.max_loss_usdt = max_loss_usdt
        self.trailing_stop_percent = trailing_stop_percent
        self.check_interval = check_interval

        self.lock = Lock()
        self.monitored_positions: Dict[str, HedgePosition] = {}
        self.running = False
        self._thread = None

        # 动态止损状态
        self.trailing_stop_prices: Dict[str, float] = {}  # symbol -> 追踪止损价
        self.breakeven_prices: Dict[str, float] = {}  # symbol -> 保本价

    def add_position(self, hedge: HedgePosition):
        """添加要监控的持仓

        Args:
            hedge: 对冲持仓对象
        """
        with self.lock:
            self.monitored_positions[hedge.symbol] = hedge
            # 初始化追踪止损价为当前的止损价
            if hedge.stop_loss_price > 0:
                self.trailing_stop_prices[hedge.symbol] = hedge.stop_loss_price
            # 计算保本价
            self._calculate_breakeven(hedge)

    def remove_position(self, symbol: str):
        """移除监控的持仓

        Args:
            symbol: 交易对
        """
        with self.lock:
            self.monitored_positions.pop(symbol, None)
            self.trailing_stop_prices.pop(symbol, None)
            self.breakeven_prices.pop(symbol, None)

    def _calculate_breakeven(self, hedge: HedgePosition):
        """计算保本价格

        保本价是让两腿总盈亏为0的价格位置

        Args:
            hedge: 对冲持仓对象
        """
        if not hedge.is_fully_hedged:
            return

        entry1 = hedge.first_leg_entry_price
        entry2 = hedge.second_leg_entry_price

        # 计算平均入场价（考虑手续费）
        # 简化计算：使用中间价作为保本基准
        mid_price = (entry1 + entry2) / 2
        self.breakeven_prices[hedge.symbol] = mid_price

    def check_positions(self, current_prices: Dict[str, float]) -> List[str]:
        """检查所有监控的持仓

        Args:
            current_prices: 当前价格字典 {symbol: price}

        Returns:
            需要平仓的交易对列表
        """
        close_signals = []

        with self.lock:
            for symbol, hedge in list(self.monitored_positions.items()):
                if symbol not in current_prices:
                    continue

                price = current_prices[symbol]

                # 计算当前未实现盈亏
                unrealized_pnl = self._calculate_unrealized_pnl(hedge, price)

                # 检查1: 亏损超过阈值
                if unrealized_pnl <= -self.max_loss_usdt:
                    print(f"\n🔴 [监控] {symbol} 亏损 {unrealized_pnl:+.4f} USDT，达到止损阈值 {-self.max_loss_usdt} USDT")
                    close_signals.append(symbol)
                    continue

                # 检查2: 已达到止盈，调整止损
                if hedge.is_fully_hedged and symbol in self.breakeven_prices:
                    self._update_trailing_stop(hedge, price)

        return close_signals

    def _calculate_unrealized_pnl(self, hedge: HedgePosition, current_price: float) -> float:
        """计算未实现盈亏

        Args:
            hedge: 对冲持仓对象
            current_price: 当前价格

        Returns:
            未实现盈亏（USDT）
        """
        # 简化计算，使用配置的仓位金额
        position_usdt = 15.0  # 从配置获取
        leverage = 20

        # 第一腿盈亏
        if hedge.first_leg_side == "SHORT":
            pnl1 = (hedge.first_leg_entry_price - current_price) / hedge.first_leg_entry_price
        else:
            pnl1 = (current_price - hedge.first_leg_entry_price) / hedge.first_leg_entry_price

        # 第二腿盈亏
        if hedge.is_second_leg_open:
            if hedge.second_leg_side == "SHORT":
                pnl2 = (hedge.second_leg_entry_price - current_price) / hedge.second_leg_entry_price
            else:
                pnl2 = (current_price - hedge.second_leg_entry_price) / hedge.second_leg_entry_price
        else:
            pnl2 = 0

        total_pnl_percent = pnl1 + pnl2
        return position_usdt * total_pnl_percent * leverage

    def _update_trailing_stop(self, hedge: HedgePosition, current_price: float):
        """更新追踪止损位

        当价格有利变动时，提高止损位（做多）或降低止损位（做空）

        Args:
            hedge: 对冲持仓对象
            current_price: 当前价格
        """
        symbol = hedge.symbol

        if not hedge.take_profit_price:
            return

        # 计算当前盈利百分比
        if hedge.first_leg_side == "SHORT":
            # 空单对冲：价格下跌时盈利
            entry = hedge.first_leg_entry_price
            profit_percent = (entry - current_price) / entry * 100
        else:
            # 多单对冲：价格上涨时盈利
            entry = hedge.first_leg_entry_price
            profit_percent = (current_price - entry) / entry * 100

        # 盈利超过 0.3% 时，将止损调整到保本位
        if profit_percent >= 0.3:
            breakeven = self.breakeven_prices.get(symbol)
            if breakeven:
                # 检查是否需要调整止损
                current_stop = self.trailing_stop_prices.get(symbol, 0)
                if hedge.first_leg_side == "SHORT":
                    # 空单：止损应该高于保本价
                    new_stop = breakeven * 1.001  # 略高于保本价
                    if new_stop < current_stop:  # 当前追踪止损更高，保持
                        return
                    self.trailing_stop_prices[symbol] = new_stop
                else:
                    # 多单：止损应该低于保本价
                    new_stop = breakeven * 0.999  # 略低于保本价
                    if new_stop > current_stop:  # 当前追踪止损更低，保持
                        return
                    self.trailing_stop_prices[symbol] = new_stop

    def start(self):
        """启动监控线程"""
        if self.running:
            return

        self.running = True
        import threading
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """停止监控线程"""
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _monitor_loop(self):
        """监控循环"""
        while self.running:
            try:
                # 获取所有监控持仓的当前价格
                prices = {}
                for symbol in list(self.monitored_positions.keys()):
                    try:
                        ticker = self.client.get_ticker_price(symbol)
                        if isinstance(ticker, dict) and "price" in ticker:
                            prices[symbol] = float(ticker["price"])
                    except Exception:
                        pass

                if prices:
                    close_signals = self.check_positions(prices)

                    # 触发平仓信号
                    for symbol in close_signals:
                        # 通过回调通知外部
                        if hasattr(self, '_on_stop_loss_signal'):
                            try:
                                self._on_stop_loss_signal(symbol)
                            except Exception:
                                pass

            except Exception as e:
                print(f"[监控] 检查错误: {e}")

            time.sleep(self.check_interval)

    def set_stop_loss_callback(self, callback: Callable[[str], None]):
        """设置止损回调函数

        Args:
            callback: 回调函数，接收交易对 symbol
        """
        self._on_stop_loss_signal = callback
