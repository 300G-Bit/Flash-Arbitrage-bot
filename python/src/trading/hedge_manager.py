"""对冲交易管理器 - 双向对冲插针策略

策略说明:
- 上插针: 高位开空 -> 回调后开多锁定利润 -> 先平空后平多
- 下插针: 低位开多 -> 反弹后开空锁定利润 -> 先平空后平多
"""

import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable

from ..exchange.binance_futures import BinanceFuturesClient
from .hedge_types import (
    HedgePosition, HedgeState, PinSignal, HedgeConfig
)
from .hedge_logger import HedgeTradeLogger
from .order_monitor import PositionMonitor

# 日志器（延迟初始化）
_logger = None


def _get_logger():
    """获取日志器实例"""
    global _logger
    if _logger is None:
        try:
            from ..utils.logger import get_logger
            _logger = get_logger()
        except ImportError:
            _logger = None
    return _logger


class HedgeTradeManager:
    """双向对冲交易管理器

    处理流程:
    1. 检测插针 -> 开第一腿（与插针方向相反）
    2. 等待价格回调/反弹 -> 开第二腿（对冲）
    3. 达到止盈/止损 -> 平仓（先平空，后平多）
    """

    # 平仓顺序配置
    CLOSE_ORDER = {
        "SHORT": ["first", "second"],   # 上插针做空: 先平空(第一腿)，后平多(第二腿)
        "LONG": ["second", "first"],    # 下插针做多: 先平空(第二腿)，后平多(第一腿)
    }

    # 价格回调比例
    BREAKEVEN_SLIPPAGE = 0.003  # 0.3%

    def __init__(
        self,
        client: BinanceFuturesClient,
        config,
        hedge_config: HedgeConfig = None,
        logger: HedgeTradeLogger = None
    ):
        """初始化对冲交易管理器

        Args:
            client: 交易所客户端
            config: 交易配置（来自testnet_config.py）
            hedge_config: 对冲策略配置
            logger: 交易日志记录器
        """
        self.client = client
        self.config = config
        self.hedge_config = hedge_config or HedgeConfig()
        self.logger = logger or HedgeTradeLogger()

        # 持仓管理
        self.active_hedges: Dict[str, HedgePosition] = {}    # 已对冲持仓
        self.waiting_hedges: Dict[str, HedgePosition] = {}   # 等待对冲持仓
        self.completed_hedges: List[HedgePosition] = []      # 已完成对冲

        # 统计
        self.total_trades = 0
        self.winning_trades = 0
        self.total_pnl = 0.0

        # 线程安全
        self.lock = threading.Lock()

        # 杠杆缓存（避免重复设置导致400错误）
        self._leverage_cache = set()

        # 持仓监控器
        self.position_monitor = PositionMonitor(
            client=self.client,
            max_loss_usdt=1.5,
            trailing_stop_percent=0.3,
            check_interval=1.0
        )
        self.position_monitor.set_stop_loss_callback(self._on_monitor_stop_loss)

        # 回调函数
        self._on_hedge_opened: Optional[Callable] = None
        self._on_hedge_closed: Optional[Callable] = None

    def set_hedge_opened_callback(self, callback: Callable):
        """设置对冲完成回调"""
        self._on_hedge_opened = callback

    def set_hedge_closed_callback(self, callback: Callable):
        """设置对冲平仓回调"""
        self._on_hedge_closed = callback

    # ==================== 信号处理 ====================

    def on_pin_signal(self, signal: PinSignal) -> bool:
        """处理插针信号，开第一腿

        Args:
            signal: 插针信号

        Returns:
            是否成功开仓
        """
        logger = _get_logger()
        with self.lock:
            # 检查是否已有该交易对的持仓
            if signal.symbol in self.active_hedges or signal.symbol in self.waiting_hedges:
                logger.debug(f"[跳过] {signal.symbol} 已有持仓")
                return False

            # 创建对冲持仓
            hedge = HedgePosition(
                symbol=signal.symbol,
                signal=signal,
                state=HedgeState.NONE
            )

            # 开第一腿
            if not self._open_first_leg(hedge, signal.entry_price):
                logger.warning(f"第一腿开仓失败: {signal.symbol} - {hedge.error_message}")
                return False

            # 计算对冲目标价格
            self._calculate_hedge_targets(hedge, signal)

            # 移入等待列表
            self.waiting_hedges[signal.symbol] = hedge
            self.position_monitor.add_position(hedge)

            logger.info(
                f"第一腿已开: {signal.symbol} {hedge.first_leg_side} "
                f"@ {hedge.first_leg_entry_price:.6f} x {hedge.first_leg_quantity:.6f}"
            )
            logger.debug(f"对冲目标价: {hedge.hedge_target_price:.6f}")

            if logger:
                logger.position_opened(
                    symbol=signal.symbol,
                    side=hedge.first_leg_side,
                    price=hedge.first_leg_entry_price,
                    quantity=hedge.first_leg_quantity,
                    order_id=hedge.first_leg_order_id
                )

            return True

    def on_price_update(self, symbol: str, price: float, timestamp: datetime = None):
        """处理价格更新

        检查是否需要:
        1. 开对冲腿（第一腿已开，等待价格到达目标）
        2. 平仓（已对冲，检查止盈止损）

        Args:
            symbol: 交易对
            price: 当前价格
            timestamp: 时间戳
        """
        with self.lock:
            # 检查等待对冲的持仓
            if symbol in self.waiting_hedges:
                self._check_hedge_entry(self.waiting_hedges[symbol], price, timestamp)

            # 检查已对冲的持仓
            if symbol in self.active_hedges:
                self._check_hedge_exit(self.active_hedges[symbol], price, timestamp)

    # ==================== 开仓逻辑 ====================

    def _open_first_leg(self, hedge: HedgePosition, price: float) -> bool:
        """开第一腿（与插针方向相反）"""
        logger = _get_logger()
        try:
            # 计算数量
            position_usdt = self.config.POSITION_USDT
            leverage = self.config.LEVERAGE
            quantity = self.client.calculate_quantity(
                hedge.symbol, position_usdt, price, leverage
            )

            # 设置杠杆（使用缓存避免重复设置）
            if hedge.symbol not in self._leverage_cache:
                self.client.set_leverage(leverage, hedge.symbol)
                self._leverage_cache.add(hedge.symbol)

            # 市价单开仓
            side = "SELL" if hedge.first_leg_side == "SHORT" else "BUY"
            order = self.client.place_market_order(
                symbol=hedge.symbol,
                side=side,
                quantity=quantity,
                position_side=hedge.first_leg_side
            )

            if not order or order.status == "REJECTED":
                self._set_order_error(hedge, order)
                return False

            # 查询实际成交价
            if order.order_id:
                time.sleep(0.05)
                updated = self.client.get_order(hedge.symbol, order_id=order.order_id)
                if updated:
                    order = updated

            # 更新持仓信息
            hedge.first_leg_order_id = order.order_id
            hedge.first_leg_quantity = order.quantity if order.quantity > 0 else quantity
            hedge.first_leg_entry_price = order.avg_price or price
            hedge.first_leg_filled = order.execute_qty > 0
            hedge.first_leg_time = datetime.now(timezone.utc)
            hedge.state = HedgeState.FIRST_LEG

            # 设置第一腿的止损止盈
            self._calculate_first_leg_exit_targets(hedge)
            self._set_first_leg_stop_orders(hedge)

            logger.debug(f"第一腿开仓成功: {hedge.symbol} {hedge.first_leg_side} "
                        f"@ {hedge.first_leg_entry_price:.6f}")

            return True

        except Exception as e:
            hedge.error_message = str(e)
            if logger:
                logger.error(f"第一腿开仓错误: {e}")
            return False

    def _open_second_leg(self, hedge: HedgePosition, price: float) -> bool:
        """开第二腿（对冲腿）"""
        try:
            quantity = hedge.first_leg_quantity
            side = "BUY" if hedge.second_leg_side == "LONG" else "SELL"

            order = self.client.place_market_order(
                symbol=hedge.symbol,
                side=side,
                quantity=quantity,
                position_side=hedge.second_leg_side
            )

            if not order or order.status == "REJECTED":
                print(f"[Hedge] 对冲腿开仓失败: {hedge.symbol}")
                return False

            # 查询实际成交价
            if order.order_id:
                time.sleep(0.05)
                updated = self.client.get_order(hedge.symbol, order_id=order.order_id)
                if updated:
                    order = updated

            hedge.second_leg_order_id = order.order_id
            hedge.second_leg_quantity = order.quantity if order.quantity > 0 else quantity
            hedge.second_leg_entry_price = order.avg_price or price
            hedge.second_leg_filled = order.execute_qty > 0
            hedge.second_leg_time = datetime.now(timezone.utc)
            hedge.state = HedgeState.HEDGED

            # 计算止盈止损目标
            self._calculate_exit_targets(hedge)
            self._set_second_leg_stop_orders(hedge)

            return True

        except Exception as e:
            print(f"[Hedge] 对冲开仓错误: {e}")
            return False

    # ==================== 价格检查 ====================

    def _check_hedge_entry(self, hedge: HedgePosition, price: float, timestamp: datetime):
        """检查是否应该开对冲腿"""
        if hedge.state != HedgeState.FIRST_LEG:
            return

        logger = _get_logger()

        # 检查超时
        if hedge.age_seconds > self.hedge_config.hedge_wait_timeout_seconds:
            if logger:
                logger.warning(f"对冲等待超时: {hedge.symbol}")
            self._close_first_leg_only(hedge, price, "timeout")
            return

        # 检查是否达到对冲目标
        should_hedge = self._should_hedge(hedge, price)

        if should_hedge:
            if logger:
                logger.info(f"达到对冲目标: {hedge.symbol} @ {price:.6f}")

            if not self._open_second_leg(hedge, price):
                return

            # 移动到活跃对冲
            del self.waiting_hedges[hedge.symbol]
            self.active_hedges[hedge.symbol] = hedge
            self.position_monitor.add_position(hedge)

            if logger:
                logger.info(
                    f"对冲完成: {hedge.symbol}\n"
                    f"  第一腿: {hedge.first_leg_side} @ {hedge.first_leg_entry_price:.6f}\n"
                    f"  第二腿: {hedge.second_leg_side} @ {hedge.second_leg_entry_price:.6f}\n"
                    f"  第一腿TP: {hedge.first_leg_take_profit:.6f} | SL: {hedge.first_leg_stop_loss:.6f}\n"
                    f"  第二腿SL: {hedge.second_leg_stop_loss:.6f} (动态追踪)"
                )
                logger.hedge_completed(
                    symbol=hedge.symbol,
                    first_side=hedge.first_leg_side,
                    second_side=hedge.second_leg_side,
                    first_entry=hedge.first_leg_entry_price,
                    second_entry=hedge.second_leg_entry_price
                )

            # 触发回调
            self._try_callback(self._on_hedge_opened, hedge, "对冲回调错误")

    def _should_hedge(self, hedge: HedgePosition, price: float) -> bool:
        """判断是否应该对冲"""
        if hedge.signal.direction == "UP":
            return price <= hedge.hedge_target_price
        else:
            return price >= hedge.hedge_target_price

    def _check_hedge_exit(self, hedge: HedgePosition, price: float, timestamp: datetime):
        """检查平仓条件

        策略:
        - 第一腿：检查是否达到止盈或保本止损
        - 第二腿（在第一腿平仓后）：使用动态追踪止损
        """
        logger = _get_logger()

        if hedge.first_leg_closed and hedge.second_leg_closed:
            return

        # 检查第一腿平仓条件
        if not hedge.first_leg_closed:
            close_result = self._check_first_leg_exit(hedge, price)
            if close_result.should_close:
                if logger:
                    logger.info(
                        f"{hedge.symbol} 第一腿触发平仓 ({close_result.reason})\n"
                        f"  当前价格: {price:.6f}\n"
                        f"  止盈目标: {hedge.first_leg_take_profit:.6f}\n"
                        f"  止损位置: {hedge.first_leg_stop_loss:.6f}"
                    )
                self._close_single_leg(hedge, "first", price, close_result.reason)
                self._enable_max_profit_mode(hedge)
                return

        # 检查第二腿动态止损（仅在第一腿已平仓后）
        if hedge.first_leg_closed and not hedge.second_leg_closed:
            self._update_second_leg_trailing_stop(hedge, price)

    def _check_first_leg_exit(self, hedge: HedgePosition, price: float):
        """检查第一腿是否应该平仓"""
        class CloseResult:
            def __init__(self, should_close: bool, reason: str):
                self.should_close = should_close
                self.reason = reason

        if hedge.first_leg_side == "SHORT":
            if price <= hedge.first_leg_take_profit:
                return CloseResult(True, "first_leg_tp")
            if price >= hedge.first_leg_stop_loss:
                return CloseResult(True, "first_leg_sl_breakeven")
        else:  # LONG
            if price >= hedge.first_leg_take_profit:
                return CloseResult(True, "first_leg_tp")
            if price <= hedge.first_leg_stop_loss:
                return CloseResult(True, "first_leg_sl_breakeven")

        return CloseResult(False, "")

    # ==================== 平仓逻辑 ====================

    def _close_first_leg_only(self, hedge: HedgePosition, price: float, reason: str):
        """只平第一腿（未对冲的情况）"""
        logger = _get_logger()

        try:
            exit_price = self._execute_close_order(hedge, "first", price)
            if exit_price is None:
                return

            hedge.first_leg_exit_price = exit_price

            # 计算盈亏
            hedge.first_leg_pnl = self._calculate_leg_pnl(
                hedge.first_leg_side,
                hedge.first_leg_entry_price,
                exit_price
            )
            hedge.total_pnl = hedge.first_leg_pnl
            hedge.close_reason = reason
            hedge.closed_at = datetime.now(timezone.utc)
            hedge.state = HedgeState.NONE

            # 清理持仓
            if hedge.symbol in self.waiting_hedges:
                del self.waiting_hedges[hedge.symbol]
            self.position_monitor.remove_position(hedge.symbol)
            self.completed_hedges.append(hedge)
            self._update_stats(hedge)

            print(f"   平仓盈亏: {hedge.total_pnl:+.4f} USDT")

            try:
                self.logger.record_hedge_closed(hedge)
            except Exception as e:
                print(f"[Hedge] 记录日志错误: {e}")

        except Exception as e:
            print(f"[Hedge] 平仓错误: {e}")
            if logger:
                logger.error(f"{hedge.symbol} 第一腿平仓异常: {e}")
            hedge.error_message = f"平仓异常: {e}"

    def _close_hedge(self, hedge: HedgePosition, price: float, reason: str):
        """平掉对冲持仓（顺序：先平空单，再平多单）"""
        logger = _get_logger()
        hedge.state = HedgeState.CLOSING

        try:
            # 取消所有止损止盈订单
            self._cancel_all_orders_safe(hedge.symbol)

            # 按顺序平仓
            close_order = self.hedge_config.get_close_order_list(hedge.first_leg_side)
            exit_prices = {}

            for leg in close_order:
                if leg == "first":
                    exit_price = self._close_leg(hedge, "first", price)
                    hedge.first_leg_exit_price = exit_price
                    exit_prices["first"] = exit_price
                else:
                    exit_price = self._close_leg(hedge, "second", price)
                    hedge.second_leg_exit_price = exit_price
                    exit_prices["second"] = exit_price

            # 计算盈亏
            position_usdt = self.config.POSITION_USDT
            leverage = self.config.LEVERAGE
            fee_rate = self.config.FEE_RATE

            hedge.calculate_pnl(
                exit_prices.get("first", price),
                exit_prices.get("second", price),
                position_usdt, leverage, fee_rate
            )

            hedge.close_reason = reason
            hedge.closed_at = datetime.now(timezone.utc)
            hedge.state = HedgeState.NONE

            # 清理持仓
            if hedge.symbol in self.active_hedges:
                del self.active_hedges[hedge.symbol]
            self.position_monitor.remove_position(hedge.symbol)
            self.completed_hedges.append(hedge)
            self._update_stats(hedge)

            # 输出结果
            if logger:
                logger.info(
                    f"{'='*50}\n"
                    f"对冲平仓: {hedge.symbol} ({reason})\n"
                    f"  第一腿: {hedge.first_leg_pnl:+.4f} USDT\n"
                    f"  第二腿: {hedge.second_leg_pnl:+.4f} USDT\n"
                    f"  总盈亏: {hedge.total_pnl:+.4f} USDT\n"
                    f"{'='*50}"
                )
                logger.position_closed(hedge.symbol, hedge.total_pnl, reason)

            # 触发回调
            self._try_callback(self._on_hedge_closed, hedge, "平仓回调错误")

            # 记录到日志
            try:
                self.logger.record_hedge_closed(hedge)
            except Exception as e:
                if logger:
                    logger.error(f"记录日志错误: {e}")

        except Exception as e:
            if logger:
                logger.error(f"对冲平仓错误: {e}")
            else:
                print(f"[Hedge] 对冲平仓错误: {e}")

    def _close_leg(self, hedge: HedgePosition, leg: str, current_price: float) -> Optional[float]:
        """平掉一条腿，使用精确数量平仓"""
        if leg == "first":
            side = hedge.first_leg_side
            quantity = hedge.first_leg_quantity
            position_side = hedge.first_leg_side
        else:
            side = hedge.second_leg_side
            quantity = hedge.second_leg_quantity
            position_side = hedge.second_leg_side

        close_side = "BUY" if side == "SHORT" else "SELL"
        logger = _get_logger()

        for attempt in range(3):
            try:
                order = self.client.place_market_order(
                    symbol=hedge.symbol,
                    side=close_side,
                    quantity=quantity,
                    position_side=position_side
                )

                if not order:
                    logger.error(f"{hedge.symbol} {leg}腿平仓返回None，重试 {attempt+1}/3")
                    time.sleep(0.1)
                    continue

                if order.status == "REJECTED":
                    error_msg = self._extract_order_error(order)
                    logger.error(f"{hedge.symbol} {leg}腿平仓被拒绝: {error_msg}")
                    return None

                # 确认成交
                filled_price = self._wait_for_order_fill(hedge.symbol, order.order_id, current_price, leg)
                if filled_price is not None:
                    if logger:
                        logger.info(f"{hedge.symbol} {leg}腿平仓确认成交 @ {filled_price:.6f}")
                    return filled_price

            except Exception as e:
                if logger:
                    logger.error(f"{hedge.symbol} {leg}腿平仓异常 (尝试 {attempt+1}/3): {e}")
                time.sleep(0.1)

        if logger:
            logger.error(f"{hedge.symbol} {leg}腿平仓失败，已尝试3次")
        return None

    def _close_single_leg(self, hedge: HedgePosition, leg: str, price: float, reason: str):
        """平掉单条腿，另一腿继续持有"""
        logger = _get_logger()

        if leg == "first":
            exit_price = self._close_leg(hedge, "first", price)
            if exit_price is None:
                return

            hedge.first_leg_exit_price = exit_price
            hedge.first_leg_closed = True

            # 计算第一腿盈亏
            hedge.first_leg_pnl = self._calculate_leg_pnl(
                hedge.first_leg_side,
                hedge.first_leg_entry_price,
                exit_price
            )

            if logger:
                pnl_pct = self._calculate_pnl_percent(
                    hedge.first_leg_side,
                    hedge.first_leg_entry_price,
                    exit_price
                )
                logger.info(
                    f"{hedge.symbol} 第一腿平仓: {hedge.first_leg_side}\n"
                    f"  入场: {hedge.first_leg_entry_price:.6f}\n"
                    f"  出场: {exit_price:.6f}\n"
                    f"  盈亏: {hedge.first_leg_pnl:+.4f} USDT ({pnl_pct*100:+.2f}%)\n"
                    f"  原因: {reason}"
                )

            # 取消第一腿的止损止盈订单
            self._cancel_all_orders_safe(hedge.symbol)

        else:  # second leg
            exit_price = self._close_leg(hedge, "second", price)
            if exit_price is None:
                return

            hedge.second_leg_exit_price = exit_price
            hedge.second_leg_closed = True

            # 计算第二腿盈亏
            hedge.second_leg_pnl = self._calculate_leg_pnl(
                hedge.second_leg_side,
                hedge.second_leg_entry_price,
                exit_price
            )
            hedge.total_pnl = hedge.first_leg_pnl + hedge.second_leg_pnl

            if logger:
                pnl_pct = self._calculate_pnl_percent(
                    hedge.second_leg_side,
                    hedge.second_leg_entry_price,
                    exit_price
                )
                logger.info(
                    f"{'='*50}\n"
                    f"{hedge.symbol} 第二腿平仓完成\n"
                    f"  第一腿: {hedge.first_leg_pnl:+.4f} USDT\n"
                    f"  第二腿: {hedge.second_leg_pnl:+.4f} USDT ({pnl_pct*100:+.2f}%)\n"
                    f"  总盈亏: {hedge.total_pnl:+.4f} USDT\n"
                    f"{'='*50}"
                )

            # 清理
            self._cancel_all_orders_safe(hedge.symbol)
            self.position_monitor.remove_position(hedge.symbol)

            if hedge.symbol in self.active_hedges:
                del self.active_hedges[hedge.symbol]

            hedge.close_reason = reason
            hedge.closed_at = datetime.now(timezone.utc)
            hedge.state = HedgeState.NONE
            self.completed_hedges.append(hedge)
            self._update_stats(hedge)

            # 触发回调
            self._try_callback(self._on_hedge_closed, hedge, "平仓回调错误")

            # 记录到日志
            try:
                self.logger.record_hedge_closed(hedge)
            except Exception as e:
                if logger:
                    logger.error(f"记录日志错误: {e}")

    # ==================== 计算方法 ====================

    def _calculate_hedge_targets(self, hedge: HedgePosition, signal: PinSignal):
        """计算对冲目标价格"""
        entry = hedge.first_leg_entry_price if hedge.first_leg_entry_price > 0 else signal.entry_price
        hedge_percent = self.hedge_config.hedge_retracement_percent / 100

        if signal.direction == "UP":
            # 上插针做空：等价格下跌时开多单锁利
            hedge.hedge_target_price = entry * (1 - hedge_percent)
        else:
            # 下插针做多：等价格上涨时开空单锁利
            hedge.hedge_target_price = entry * (1 + hedge_percent)

    def _calculate_exit_targets(self, hedge: HedgePosition):
        """计算独立的止盈止损目标"""
        first_entry = hedge.first_leg_entry_price
        second_entry = hedge.second_leg_entry_price
        signal = hedge.signal

        # 第一腿：止损设在入场价（保本）
        hedge.first_leg_stop_loss = first_entry

        # 第一腿：止盈基于插针回撤幅度（吃60%的插针幅度）
        spike_size = abs(signal.peak_price - signal.start_price)
        if hedge.first_leg_side == "SHORT":
            hedge.first_leg_take_profit = first_entry - spike_size * 0.6
        else:
            hedge.first_leg_take_profit = first_entry + spike_size * 0.6

        # 第二腿：初始止损在入场价，无固定止盈（使用动态追踪）
        hedge.second_leg_stop_loss = second_entry
        hedge.second_leg_take_profit = 0.0

        logger = _get_logger()
        if logger:
            logger.debug(
                f"{hedge.symbol} 止盈止损目标:\n"
                f"  第一腿({hedge.first_leg_side}): TP={hedge.first_leg_take_profit:.6f}, SL={hedge.first_leg_stop_loss:.6f}\n"
                f"  第二腿({hedge.second_leg_side}): SL={hedge.second_leg_stop_loss:.6f} (动态追踪)"
            )

    def _calculate_first_leg_exit_targets(self, hedge: HedgePosition):
        """计算第一腿的止损止盈价格（开仓后立即设置）"""
        entry = hedge.first_leg_entry_price
        signal = hedge.signal

        # 止损：入场价（保本）
        hedge.first_leg_stop_loss = entry

        # 止盈：基于插针回调幅度（吃到回调幅度的40%）
        if hedge.first_leg_side == "SHORT":
            retrace_size = signal.peak_price - entry
            hedge.first_leg_take_profit = entry - retrace_size * 0.4
        else:
            retrace_size = entry - signal.peak_price
            hedge.first_leg_take_profit = entry + retrace_size * 0.4

    def _enable_max_profit_mode(self, hedge: HedgePosition):
        """第一腿平仓后，第二腿进入最大化利润模式"""
        logger = _get_logger()
        entry = hedge.second_leg_entry_price

        # 将第二腿止损调整为保本价（入场价 +/- 0.3%）
        if hedge.second_leg_side == "LONG":
            hedge.second_leg_stop_loss = entry * (1 - self.BREAKEVEN_SLIPPAGE)
        else:
            hedge.second_leg_stop_loss = entry * (1 + self.BREAKEVEN_SLIPPAGE)

        if logger:
            logger.info(
                f"{hedge.symbol} 第二腿进入最大化利润模式\n"
                f"  止损调整至: {hedge.second_leg_stop_loss:.6f} (保本 +/-0.3%)"
            )

    def _update_second_leg_trailing_stop(self, hedge: HedgePosition, price: float):
        """更新第二腿追踪止损（最大化利润模式）

        平仓条件优先级：
        1. 快速止盈：盈利 >= quick_tp_percent -> 立即平仓
        2. 保本止损：价格跌破入场价 +/-0.3% -> 平仓保本
        3. 追踪止损：盈利 >= 0.5% 后，允许30%回撤
        """
        logger = _get_logger()
        second_side = hedge.second_leg_side
        entry = hedge.second_leg_entry_price

        # 计算当前浮盈百分比
        current_profit_percent = self._calculate_profit_percent(second_side, entry, price)

        # 更新最高浮盈记录
        if current_profit_percent > hedge.second_leg_max_profit:
            hedge.second_leg_max_profit = current_profit_percent

        max_profit = hedge.second_leg_max_profit

        # 优先级1：快速止盈检查
        if self.hedge_config.quick_tp_enabled:
            quick_tp_threshold = self.hedge_config.quick_tp_percent
            if current_profit_percent >= quick_tp_threshold:
                if logger:
                    logger.info(
                        f"{hedge.symbol} 第二腿快速止盈触发\n"
                        f"  当前浮盈: {current_profit_percent:.2f}%\n"
                        f"  当前价格: {price:.6f}"
                    )
                self._close_single_leg(hedge, "second", price, "second_leg_quick_tp")
                return

        # 优先级2：保本止损检查
        if self._should_hit_breakeven(second_side, price, entry):
            if logger:
                logger.info(
                    f"{hedge.symbol} 第二腿保本止损触发\n"
                    f"  入场价格: {entry:.6f}\n"
                    f"  当前价格: {price:.6f}\n"
                    f"  当前浮盈: {current_profit_percent:+.2f}%"
                )
            self._close_single_leg(hedge, "second", price, "breakeven_sl")
            return

        # 优先级3：追踪止损（仅在最高浮盈 >= 0.5% 时启用）
        if max_profit >= 0.5:
            self._apply_trailing_stop(hedge, price, max_profit, second_side, logger)

    def _calculate_profit_percent(self, side: str, entry: float, price: float) -> float:
        """计算盈利百分比"""
        if side == "LONG":
            return (price - entry) / entry * 100
        else:
            return (entry - price) / entry * 100

    def _should_hit_breakeven(self, side: str, price: float, entry: float) -> bool:
        """检查是否触发保本止损"""
        if side == "LONG":
            return price <= entry * (1 - self.BREAKEVEN_SLIPPAGE)
        else:
            return price >= entry * (1 + self.BREAKEVEN_SLIPPAGE)

    def _apply_trailing_stop(self, hedge: HedgePosition, price: float, max_profit: float, side: str, logger):
        """应用追踪止损"""
        entry = hedge.second_leg_entry_price

        # 根据盈利水平计算新的止损价
        if max_profit < 1.0:
            # 盈利0.5%-1%，保护一半利润
            if side == "LONG":
                new_sl = entry + (price - entry) * 0.5
            else:
                new_sl = entry - (entry - price) * 0.5
        else:
            # 盈利超过1%，使用追踪止损（允许回撤30%）
            profit_pullback = max_profit * 0.3
            if side == "LONG":
                new_sl = price * (1 - profit_pullback / 100)
            else:
                new_sl = price * (1 + profit_pullback / 100)

        # 更新止损价
        hedge.second_leg_stop_loss = new_sl

        # 检查是否触发追踪止损
        should_close = False
        if side == "LONG" and price <= new_sl:
            should_close = True
        elif side == "SHORT" and price >= new_sl:
            should_close = True

        if should_close:
            if logger:
                logger.info(
                    f"{hedge.symbol} 第二腿追踪止损触发\n"
                    f"  最高浮盈: {max_profit:.2f}%\n"
                    f"  当前价格: {price:.6f}\n"
                    f"  止损价格: {new_sl:.6f}"
                )
            self._close_single_leg(hedge, "second", price, "second_leg_trailing_sl")

    # ==================== 止损止盈订单 ====================

    def _set_stop_loss_orders(self, hedge: HedgePosition):
        """在交易所设置实际的止损止盈订单（已禁用 - 使用本地监控）"""
        try:
            self._place_leg_stop_orders(hedge, "first")
            self._place_leg_stop_orders(hedge, "second")

            print(f"   第一腿: TP={hedge.first_leg_take_profit:.6f}, SL={hedge.first_leg_stop_loss:.6f}")
            print(f"   第二腿: SL={hedge.second_leg_stop_loss:.6f} (动态追踪)")

        except Exception as e:
            print(f"   止损止盈设置失败: {e}")

    def _set_first_leg_stop_orders(self, hedge: HedgePosition) -> bool:
        """为第一腿设置止损止盈订单（已禁用 - 使用本地监控）"""
        logger = _get_logger()

        if logger:
            logger.debug(f"{hedge.symbol} 第一腿使用本地监控止损，跳过条件单设置")
            logger.debug(f"本地止损价: {hedge.first_leg_stop_loss:.6f}")
            logger.debug(f"本地止盈价: {hedge.first_leg_take_profit:.6f}")

        print(f"   第一腿使用本地监控止损: SL={hedge.first_leg_stop_loss:.6f}, TP={hedge.first_leg_take_profit:.6f}")

        return True

    def _set_second_leg_stop_orders(self, hedge: HedgePosition) -> bool:
        """为第二腿设置止损止盈订单（已禁用 - 使用本地监控）"""
        logger = _get_logger()

        if logger:
            logger.debug(f"{hedge.symbol} 第二腿使用本地动态追踪止损，跳过条件单设置")
            logger.debug(f"本地止损价: {hedge.second_leg_stop_loss:.6f}")

        print(f"   第二腿使用本地动态追踪止损: SL={hedge.second_leg_stop_loss:.6f}")

        return True

    def _verify_stop_orders_set(self, symbol: str) -> bool:
        """验证止损止盈订单是否在交易所生效"""
        logger = _get_logger()
        try:
            time.sleep(0.1)
            open_orders = self.client.get_open_orders(symbol)

            for order in open_orders:
                if isinstance(order, dict):
                    order_type = order.get("type", "")
                    if "STOP" in order_type or "TAKE_PROFIT" in order_type:
                        return True
        except Exception as e:
            if logger:
                logger.debug(f"验证订单时出错: {e}")

        return False

    def _place_leg_stop_orders_with_result(
        self, hedge: HedgePosition, leg: str, order_type: str
    ) -> bool:
        """为单条腿设置单个止损或止盈订单（已禁用 - 使用本地监控）"""
        logger = _get_logger()

        if leg == "first":
            sl_price = hedge.first_leg_stop_loss
            tp_price = hedge.first_leg_take_profit
        else:
            sl_price = hedge.second_leg_stop_loss
            tp_price = hedge.second_leg_take_profit

        if logger:
            if order_type == "stop":
                logger.debug(f"{hedge.symbol} {leg}腿本地止损记录: {sl_price:.6f}")
            else:
                logger.debug(f"{hedge.symbol} {leg}腿本地止盈记录: {tp_price:.6f}")

        # 第二腿顺势单使用动态追踪，不设置固定止盈
        if leg == "second" and order_type == "take_profit":
            return True

        return True

    def _place_leg_stop_orders(self, hedge: HedgePosition, leg: str):
        """为单条腿设置止损止盈订单（已禁用 - 使用本地监控）"""
        pass

    # ==================== 公共方法 ====================

    def close_all_positions(self, reason: str = "manual"):
        """平掉所有持仓"""
        with self.lock:
            # 平掉等待对冲的持仓
            for symbol, hedge in list(self.waiting_hedges.items()):
                price = self._get_current_price(symbol)
                if price > 0:
                    self._close_first_leg_only(hedge, price, reason)

            # 平掉已对冲的持仓
            for symbol, hedge in list(self.active_hedges.items()):
                price = self._get_current_price(symbol)
                if price > 0:
                    self._close_hedge(hedge, price, reason)

    def _get_current_price(self, symbol: str) -> float:
        """获取当前价格"""
        try:
            ticker = self.client.get_ticker_price(symbol)
            if isinstance(ticker, dict) and "price" in ticker:
                return float(ticker["price"])
            elif isinstance(ticker, list) and len(ticker) > 0:
                return float(ticker[0].get("price", 0))
        except Exception:
            pass
        return 0.0

    def get_stats(self) -> Dict:
        """获取统计信息"""
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "win_rate": win_rate,
            "total_pnl": self.total_pnl,
            "active_hedges": len(self.active_hedges),
            "waiting_hedges": len(self.waiting_hedges),
        }

    # ==================== 监控相关 ====================

    def start_monitoring(self):
        """启动持仓监控"""
        self.position_monitor.start()

    def stop_monitoring(self):
        """停止持仓监控"""
        self.position_monitor.stop()

    def _on_monitor_stop_loss(self, symbol: str):
        """监控器触发止损时的回调"""
        with self.lock:
            if symbol in self.active_hedges:
                hedge = self.active_hedges[symbol]
                price = self._get_current_price(symbol)
                if price > 0:
                    print(f"\n[监控] 触发自动止损: {symbol} @ {price:.6f}")
                    self._close_hedge(hedge, price, "auto_stop_loss")
            elif symbol in self.waiting_hedges:
                hedge = self.waiting_hedges[symbol]
                price = self._get_current_price(symbol)
                if price > 0:
                    print(f"\n[监控] 触发自动止损: {symbol} @ {price:.6f}")
                    self._close_first_leg_only(hedge, price, "auto_stop_loss")

    def sync_positions_to_monitor(self):
        """同步持仓到监控器"""
        for hedge in self.active_hedges.values():
            self.position_monitor.add_position(hedge)
        for hedge in self.waiting_hedges.values():
            self.position_monitor.add_position(hedge)

    # ==================== 辅助方法 ====================

    def _update_stats(self, hedge: HedgePosition):
        """更新统计"""
        self.total_trades += 1
        self.total_pnl += hedge.total_pnl
        if hedge.total_pnl > 0:
            self.winning_trades += 1

    def _set_order_error(self, hedge: HedgePosition, order):
        """设置订单错误信息"""
        if order and order.raw:
            code = order.raw.get("code")
            msg = order.raw.get("msg")
            if code and msg:
                hedge.error_message = f"错误{code}: {msg}"
                return
        hedge.error_message = "订单被拒绝" if order else "下单失败"

    def _extract_order_error(self, order) -> str:
        """提取订单错误信息"""
        if order.raw:
            code = order.raw.get("code")
            msg = order.raw.get("msg")
            if code is not None:
                return f"错误{code}: {msg}" if msg else f"错误码: {code}"
            if msg:
                return msg
        return "未知错误"

    def _wait_for_order_fill(self, symbol: str, order_id: str, current_price: float, leg: str) -> Optional[float]:
        """等待订单成交确认"""
        logger = _get_logger()

        if not order_id:
            if logger:
                logger.warning(f"{symbol} {leg}腿平仓订单无ID，使用当前价格: {current_price:.6f}")
            return current_price

        # 等待订单处理
        time.sleep(0.15)

        # 轮询确认订单已 FILLED
        for _ in range(5):
            updated = self.client.get_order(symbol, order_id=order_id)
            if updated and updated.status == "FILLED":
                return updated.avg_price or current_price
            if updated and updated.status in ["EXPIRED", "CANCELED", "REJECTED"]:
                if logger:
                    logger.warning(f"{symbol} {leg}腿平仓订单状态: {updated.status}")
                break
            time.sleep(0.1)

        # 使用返回的价格
        return current_price

    def _execute_close_order(self, hedge: HedgePosition, leg: str, current_price: float) -> Optional[float]:
        """执行平仓订单（用于_close_first_leg_only）"""
        logger = _get_logger()

        if leg == "first":
            close_side = "BUY" if hedge.first_leg_side == "SHORT" else "SELL"
            quantity = hedge.first_leg_quantity
            position_side = hedge.first_leg_side
        else:
            close_side = "BUY" if hedge.second_leg_side == "SHORT" else "SELL"
            quantity = hedge.second_leg_quantity
            position_side = hedge.second_leg_side

        try:
            order = self.client.place_market_order(
                symbol=hedge.symbol,
                side=close_side,
                quantity=quantity,
                position_side=position_side
            )

            if not order:
                logger.error(f"{hedge.symbol} 第一腿平仓返回None")
                hedge.error_message = "平仓订单返回None"
                return None

            if order.status == "REJECTED":
                error_msg = self._extract_order_error(order)
                logger.error(f"{hedge.symbol} 第一腿平仓被拒绝: {error_msg}")
                hedge.error_message = "平仓订单被拒绝"
                return None

            # 确认成交
            exit_price = self._wait_for_order_fill(hedge.symbol, order.order_id, current_price, "第一腿")
            if exit_price is not None:
                if logger:
                    logger.info(f"{hedge.symbol} 第一腿平仓确认成交 @ {exit_price:.6f}")
                return exit_price

            hedge.error_message = "平仓订单未确认成交"
            return None

        except Exception as e:
            logger.error(f"{hedge.symbol} 第一腿平仓异常: {e}")
            hedge.error_message = f"平仓异常: {e}"
            return None

    def _cancel_all_orders_safe(self, symbol: str):
        """安全地取消所有订单"""
        try:
            self.client.cancel_all_orders(symbol)
        except Exception as e:
            logger = _get_logger()
            if logger:
                logger.debug(f"取消订单警告: {e}")

    def _try_callback(self, callback: Callable, hedge: HedgePosition, error_msg: str):
        """尝试执行回调函数"""
        if callback:
            try:
                callback(hedge)
            except Exception as e:
                logger = _get_logger()
                if logger:
                    logger.error(f"{error_msg}: {e}")

    def _calculate_leg_pnl(self, side: str, entry_price: float, exit_price: float) -> float:
        """计算单腿盈亏"""
        position_usdt = self.config.POSITION_USDT
        leverage = self.config.LEVERAGE
        fee_rate = self.config.FEE_RATE

        pnl_percent = self._calculate_pnl_percent(side, entry_price, exit_price)
        fee = position_usdt * fee_rate * 2
        return position_usdt * pnl_percent * leverage - fee

    def _calculate_pnl_percent(self, side: str, entry_price: float, exit_price: float) -> float:
        """计算盈亏百分比"""
        if side == "SHORT":
            return (entry_price - exit_price) / entry_price
        else:
            return (exit_price - entry_price) / entry_price
