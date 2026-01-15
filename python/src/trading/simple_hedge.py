"""
简化的对冲执行器

策略逻辑：
- 上涨插针：开空单 -> 回调开多单锁利 -> 空单盈利平仓 -> 等多单回到入场价平仓
- 下跌插针：开多单 -> 反弹开空单锁利 -> 多单盈利平仓 -> 等空单回到入场价平仓

支持基于ATR的动态阈值
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Callable, Union, Any

from ..exchange.binance_futures import BinanceFuturesClient
from ..utils.logging_config import get_logger, EventLogger, generate_correlation_id

# 兼容旧版和新版信号类型
try:
    from ..analysis.atr_types import SpikeSignal, SpikeDirection
    NEW_SIGNAL_TYPE = True
except ImportError:
    NEW_SIGNAL_TYPE = False

    # 旧版类型
    try:
        from ..analysis.mtf_detector import PinSignal, PinDirection
        SpikeSignal = PinSignal
        SpikeDirection = PinDirection
    except ImportError:
        # 定义基本类型
        from enum import Enum

        class SpikeDirection(str, Enum):
            UP = "UP"
            DOWN = "DOWN"

# 使用统一日志系统
logger = get_logger(__name__)
events = EventLogger(logger)


class SimpleHedgeConfig:
    """简化对冲策略配置"""

    HEDGE_ENTRY_PERCENT: float = 0.006
    FIRST_LEG_TARGET_PERCENT: float = 0.008
    SECOND_LEG_WAIT_SECONDS: int = 300
    MAX_POSITION_USDT: float = 15.0
    LEVERAGE: int = 20


@dataclass
class SimpleHedgePosition:
    """简化的对冲持仓状态"""
    symbol: str
    direction: SpikeDirection
    entry_price: float
    signal_time: datetime
    correlation_id: str = ""  # 关联ID，用于追踪完整交易流程

    first_side: str = ""
    first_entry: float = 0.0
    first_quantity: float = 0.0
    first_order_id: str = ""
    first_filled: bool = False

    second_side: str = ""
    second_entry: float = 0.0
    second_quantity: float = 0.0
    second_order_id: str = ""
    second_filled: bool = False

    hedge_target: float = 0.0
    first_tp_price: float = 0.0

    first_closed: bool = False
    second_closed: bool = False

    first_pnl: float = 0.0
    second_pnl: float = 0.0
    total_pnl: float = 0.0

    second_open_time: Optional[datetime] = None
    close_time: Optional[datetime] = None
    close_reason: str = ""

    @property
    def is_first_open(self) -> bool:
        return self.first_filled and self.first_order_id

    @property
    def is_second_open(self) -> bool:
        return self.second_filled and self.second_order_id

    @property
    def is_hedged(self) -> bool:
        return self.is_first_open and self.is_second_open

    @property
    def is_closed(self) -> bool:
        return self.first_closed and self.second_closed

    @property
    def second_wait_seconds(self) -> float:
        if self.second_open_time and not self.second_closed:
            return (datetime.now(timezone.utc) - self.second_open_time).total_seconds()
        return 0


class SimpleHedgeExecutor:
    """简化的对冲执行器"""

    def __init__(
        self,
        client: BinanceFuturesClient | None = None,
        config: SimpleHedgeConfig | None = None,
        position_usdt: float = 15.0,
        leverage: int = 20,
        fee_rate: float = 0.0004,
        external_logger: Any = None
    ):
        """初始化执行器

        Args:
            client: Binance期货客户端
            config: 对冲配置
            position_usdt: 仓位大小(USDT)
            leverage: 杠杆倍数
            fee_rate: 手续费率
            external_logger: 外部传入的logger(用于集成主程序日志系统)
        """
        self.client = client
        self.config = config or SimpleHedgeConfig()
        self.position_usdt = position_usdt
        self.leverage = leverage
        self.fee_rate = fee_rate

        self.positions: Dict[str, SimpleHedgePosition] = {}
        self._leverage_set: set = set()

        self._on_signal: Optional[Callable] = None
        self._on_hedge_opened: Optional[Callable] = None
        self._on_hedge_closed: Optional[Callable] = None

        # 使用外部logger或默认logger
        self.logger = external_logger if external_logger is not None else logger

    def set_signal_callback(self, callback: Callable) -> None:
        self._on_signal = callback

    def set_hedge_opened_callback(self, callback: Callable) -> None:
        self._on_hedge_opened = callback

    def set_hedge_closed_callback(self, callback: Callable) -> None:
        self._on_hedge_closed = callback

    def on_signal(self, signal: SpikeSignal) -> bool:
        """处理插针信号

        支持动态ATR阈值和固定阈值两种模式
        """
        symbol = signal.symbol

        # 生成关联ID，用于追踪完整交易流程
        correlation_id = generate_correlation_id()

        # 诊断日志：确认信号到达
        self.logger.with_correlation_id(correlation_id).debug(
            f"[信号] {symbol} direction={signal.direction.value} entry={signal.entry_price:.6f} "
            f"client_ready={self.client is not None}"
        )

        if symbol in self.positions:
            pos = self.positions[symbol]
            if not pos.is_closed:
                self.logger.debug(f"[跳过] {symbol} 已有持仓")
                events.log_signal_filtered(symbol, "已有活跃持仓")
                return False

        # 确定方向：UP做多，DOWN做空（第一腿顺势）
        first_side = "LONG" if signal.direction == SpikeDirection.UP else "SHORT"
        second_side = "SHORT" if signal.direction == SpikeDirection.UP else "LONG"

        self.logger.with_correlation_id(correlation_id).debug(
            f"[DEBUG-1] {symbol} 创建持仓对象, first_side={first_side}, second_side={second_side}"
        )

        position = SimpleHedgePosition(
            symbol=symbol,
            direction=signal.direction,
            entry_price=signal.entry_price,
            signal_time=signal.detected_at,
            correlation_id=correlation_id,
            first_side=first_side,
            first_entry=signal.entry_price,
        )
        position.second_side = second_side

        # 计算仓位数量
        self.logger.debug(f"[DEBUG-2] {symbol} 开始计算数量, position_usdt={self.position_usdt}, leverage={self.leverage}")

        if self.client:
            try:
                quantity = self.client.calculate_quantity(
                    symbol, self.position_usdt, signal.entry_price, self.leverage
                )
                position.first_quantity = quantity
                position.second_quantity = quantity
                self.logger.debug(f"[DEBUG-3] {symbol} 计算数量结果: quantity={quantity:.6f}")

                if quantity <= 0:
                    self.logger.error(f"[ERROR] {symbol} 计算数量为0或负数! quantity={quantity}")
                    return False
            except Exception as e:
                self.logger.error(f"[ERROR] {symbol} 计算数量异常: {e}")
                return False
        else:
            position.first_quantity = 0.0
            position.second_quantity = 0.0
            self.logger.error(f"[ERROR] {symbol} 客户端未初始化，无法计算数量")
            return False

        # 对冲目标价格计算：
        # - UP信号（下跌插针，做多）：等待价格上涨时开空单对冲
        # - DOWN信号（上涨插针，做空）：等待价格下跌时开多单对冲
        if hasattr(signal, 'second_leg_target') and signal.second_leg_target > 0:
            # 使用信号中预计算的第二腿目标价
            position.hedge_target = signal.second_leg_target
        else:
            # 使用动态ATR阈值或固定阈值
            if hasattr(signal, 'retrace_threshold') and signal.retrace_threshold > 0:
                retrace_percent = signal.retrace_threshold
            else:
                retrace_percent = self.config.HEDGE_ENTRY_PERCENT

            # 动态计算
            if signal.direction == SpikeDirection.UP:
                # 做多后，等待价格上涨再开空单锁利
                position.hedge_target = signal.entry_price * (1 + retrace_percent)
            else:
                # 做空后，等待价格下跌再开多单锁利
                position.hedge_target = signal.entry_price * (1 - retrace_percent)

        # 第一腿止盈目标
        if hasattr(signal, 'retrace_threshold') and signal.retrace_threshold > 0:
            retrace_percent = signal.retrace_threshold
        else:
            retrace_percent = self.config.HEDGE_ENTRY_PERCENT

        if signal.direction == SpikeDirection.UP:
            position.first_tp_price = signal.entry_price * (1 + retrace_percent * 1.5)
        else:
            position.first_tp_price = signal.entry_price * (1 - retrace_percent * 1.5)

        self.logger.debug(f"[DEBUG-4] {symbol} 准备开第一腿, quantity={position.first_quantity:.6f}")

        try:
            success = self._open_first_leg(position)
            self.logger.debug(f"[DEBUG-5] {symbol} 第一腿开仓结果: success={success}")
        except Exception as e:
            self.logger.error(f"[ERROR] {symbol} 第一腿开仓异常: {e}")
            import traceback
            traceback.print_exc()
            return False

        if success:
            self.positions[symbol] = position

            # 记录订单成交事件
            events.log_order_filled(
                symbol=symbol,
                order_id=position.first_order_id,
                avg_price=position.first_entry,
                filled_qty=position.first_quantity,
                correlation_id=correlation_id,
                side=first_side,
                leg="first"
            )

            self.logger.info(
                f"✓ 第一腿已开: {symbol} {first_side} @ {position.first_entry:.6f} x {position.first_quantity:.6f}\n"
                f"   对冲目标: {position.hedge_target:.6f} | 止盈: {position.first_tp_price:.6f}"
            )

            if self._on_signal:
                self._on_signal(signal)
        else:
            # 记录订单失败事件
            events.log_order_failed(
                symbol=symbol,
                reason="第一腿开仓失败",
                correlation_id=correlation_id,
                side=first_side,
                quantity=position.first_quantity
            )

            self.logger.error(
                f"✗ 第一腿开仓失败: {symbol} {first_side} @ {signal.entry_price:.6f}\n"
                f"   数量: {position.first_quantity} | 客户端已初始化: {self.client is not None}"
            )

        return success

    def on_price_update(self, symbol: str, price: float) -> None:
        """处理价格更新"""
        if symbol not in self.positions:
            return

        pos = self.positions[symbol]

        if pos.is_closed:
            del self.positions[symbol]
            return

        # 使用关联ID记录价格更新日志
        if pos.correlation_id:
            log = self.logger.with_correlation_id(pos.correlation_id)
        else:
            log = self.logger

        if pos.is_first_open and not pos.is_second_open:
            self._check_hedge_entry(pos, price)

        if pos.is_hedged and not pos.first_closed:
            self._check_first_leg_tp(pos, price)

        if pos.is_hedged and pos.first_closed and not pos.second_closed:
            self._check_second_leg_exit(pos, price)

    def _wait_for_order_fill(
        self, symbol: str, order_id: str, current_price: float, leg: str
    ) -> Optional[float]:
        """等待订单成交确认

        Args:
            symbol: 交易对
            order_id: 订单ID
            current_price: 当前价格（作为回退值）
            leg: 腿标识 ("first", "second")

        Returns:
            成交均价，或None表示失败
        """
        if not order_id:
            self.logger.warning(f"{symbol} {leg}腿无订单ID，使用当前价格: {current_price:.6f}")
            self.logger.warning(f"[WARN] {symbol} {leg}腿无订单ID")
            return current_price

        # 等待订单处理
        self.logger.debug(f"[_wait-1] {symbol} {leg}腿 等待订单处理, order_id={order_id}")
        time.sleep(0.15)

        # 轮询确认订单已FILLED
        for attempt in range(5):
            try:
                self.logger.debug(f"[_wait-2] {symbol} {leg}腿 查询订单状态 (尝试 {attempt+1}/5)")

                updated = self.client.get_order(symbol, order_id=order_id)

                self.logger.debug(f"[_wait-3] {symbol} {leg}腿 订单查询结果: updated={updated}, status={getattr(updated, 'status', 'N/A') if updated else 'None'}")

                if updated and updated.status == "FILLED":
                    self.logger.info(f"[SUCCESS] {symbol} {leg}腿 订单已成交, avg_price={updated.avg_price}")
                    return updated.avg_price or current_price
                if updated and updated.status in ["EXPIRED", "CANCELED", "REJECTED"]:
                    self.logger.warning(f"{symbol} {leg}腿订单状态: {updated.status}")
                    self.logger.warning(f"[WARN] {symbol} {leg}腿 订单异常状态: {updated.status}")
                    return None
            except Exception as e:
                self.logger.error(f"[ERROR] {symbol} {leg}腿 查询异常 (尝试 {attempt+1}/5): {e}")
                if attempt == 4:  # 最后一次尝试
                    self.logger.error(f"{symbol} {leg}腿订单查询失败: {e}")
            time.sleep(0.1)

        self.logger.warning(f"{symbol} {leg}腿订单未确认，使用当前价格")
        self.logger.warning(f"[WARN] {symbol} {leg}腿 订单未确认，使用当前价格 {current_price:.6f}")
        return current_price

    def _open_first_leg(self, pos: SimpleHedgePosition) -> bool:
        """开第一腿"""
        self.logger.debug(f"[_open_first_leg-ENTRY] {pos.symbol} 开始开第一腿")

        if not self.client:
            self.logger.error(f"第一腿开仓失败: {pos.symbol} - 客户端未初始化")
            self.logger.error(f"[ERROR] {pos.symbol} 客户端未初始化")
            return False

        try:
            # 设置杠杆（使用缓存避免重复设置）
            self.logger.debug(f"[_open_first_leg-1] {pos.symbol} 设置杠杆, leverage={self.leverage}, in_cache={pos.symbol in self._leverage_set}")

            if pos.symbol not in self._leverage_set:
                self.client.set_leverage(self.leverage, pos.symbol)
                self._leverage_set.add(pos.symbol)
                self.logger.debug(f"[_open_first_leg-2] {pos.symbol} 杠杆设置完成")

            side = "BUY" if pos.first_side == "LONG" else "SELL"
            self.logger.debug(f"[_open_first_leg-3] {pos.symbol} 下市价单, side={side}, quantity={pos.first_quantity:.6f}, position_side={pos.first_side}")

            order = self.client.place_market_order(
                symbol=pos.symbol,
                side=side,
                quantity=pos.first_quantity,
                position_side=pos.first_side
            )

            self.logger.debug(f"[_open_first_leg-4] {pos.symbol} 订单返回, order={order}, type={type(order)}")

            if not order:
                self.logger.warning(f"第一腿开仓失败: {pos.symbol} - 订单返回None")
                self.logger.error(f"[ERROR] {pos.symbol} 订单返回None")
                return False

            if hasattr(order, 'status'):
                self.logger.debug(f"[_open_first_leg-5] {pos.symbol} 订单状态: {order.status}")

                if order.status == "REJECTED":
                    self.logger.warning(f"第一腿开仓被拒绝: {pos.symbol}")
                    self.logger.error(f"[ERROR] {pos.symbol} 订单被拒绝")
                    return False

            if hasattr(order, 'order_id'):
                pos.first_order_id = order.order_id
                self.logger.debug(f"[_open_first_leg-6] {pos.symbol} 订单ID: {order.order_id}")
            else:
                self.logger.error(f"[ERROR] {pos.symbol} 订单对象没有order_id属性, order={order}")
                return False

            # 确认成交
            self.logger.debug(f"[_open_first_leg-7] {pos.symbol} 等待订单成交确认")

            filled_price = self._wait_for_order_fill(
                pos.symbol, order.order_id, pos.first_entry, "第一腿"
            )

            self.logger.debug(f"[_open_first_leg-8] {pos.symbol} 成交确认结果: filled_price={filled_price}")

            if filled_price is not None:
                pos.first_entry = filled_price
                pos.first_filled = True
                self.logger.info(f"第一腿开仓成功: {pos.symbol} {pos.first_side} @ {filled_price:.6f}")
                self.logger.info(f"[SUCCESS] {pos.symbol} 第一腿开仓成功 @ {filled_price:.6f}")
                return True
            else:
                self.logger.error(f"第一腿未确认成交: {pos.symbol}")
                self.logger.error(f"[ERROR] {pos.symbol} 第一腿未确认成交")
                return False

        except Exception as e:
            self.logger.error(f"第一腿开仓错误: {e}")
            self.logger.error(f"[ERROR] {pos.symbol} 第一腿开仓异常: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _open_second_leg(self, pos: SimpleHedgePosition) -> bool:
        """开第二腿（对冲单）"""
        if not self.client:
            self.logger.error(f"对冲腿开仓失败: {pos.symbol} - 客户端未初始化")
            return False

        try:
            side = "BUY" if pos.second_side == "LONG" else "SELL"

            order = self.client.place_market_order(
                symbol=pos.symbol,
                side=side,
                quantity=pos.second_quantity,
                position_side=pos.second_side
            )

            if not order:
                self.logger.warning(f"对冲腿开仓失败: {pos.symbol} - 订单返回None")
                return False

            if order.status == "REJECTED":
                self.logger.warning(f"对冲腿开仓被拒绝: {pos.symbol}")
                return False

            pos.second_order_id = order.order_id

            # 确认成交
            filled_price = self._wait_for_order_fill(
                pos.symbol, order.order_id, pos.second_entry, "第二腿"
            )

            if filled_price is not None:
                pos.second_entry = filled_price
                pos.second_filled = True
                pos.second_open_time = datetime.now(timezone.utc)

                self.logger.info(
                    f"对冲完成: {pos.symbol}\n"
                    f"   第一腿: {pos.first_side} @ {pos.first_entry:.6f}\n"
                    f"   第二腿: {pos.second_side} @ {pos.second_entry:.6f}"
                )

                if self._on_hedge_opened:
                    self._on_hedge_opened(pos)

                return True
            else:
                self.logger.error(f"对冲腿未确认成交: {pos.symbol}")
                return False

        except Exception as e:
            self.logger.error(f"对冲开仓错误: {e}")
            return False

    def _check_hedge_entry(self, pos: SimpleHedgePosition, price: float) -> None:
        """检查是否开对冲腿"""
        should_hedge = False

        if pos.direction == SpikeDirection.UP:
            if price >= pos.hedge_target:
                should_hedge = True
        else:
            if price <= pos.hedge_target:
                should_hedge = True

        if should_hedge:
            self.logger.info(f"达到对冲目标: {pos.symbol} @ {price:.6f}")
            self._open_second_leg(pos)

    def _check_first_leg_tp(self, pos: SimpleHedgePosition, price: float) -> None:
        """检查第一腿止盈"""
        should_close = False

        if pos.first_side == "LONG":
            if price >= pos.first_tp_price:
                should_close = True
        else:
            if price <= pos.first_tp_price:
                should_close = True

        if should_close:
            self.logger.info(
                f"{pos.symbol} 第一腿触发止盈\n"
                f"   当前: {price:.6f} | 目标: {pos.first_tp_price:.6f}"
            )
            self._close_first_leg(pos, price)

    def _check_second_leg_exit(self, pos: SimpleHedgePosition, price: float) -> None:
        """检查第二腿退出条件（保本或超时）"""
        if pos.second_wait_seconds >= self.config.SECOND_LEG_WAIT_SECONDS:
            self.logger.info(f"{pos.symbol} 第二腿超时，强制平仓")
            self._close_second_leg(pos, price, "timeout")
            return

        BREAKEVEN_THRESHOLD = 0.003
        should_close = False

        if pos.second_side == "LONG":
            if pos.second_entry * (1 - BREAKEVEN_THRESHOLD) <= price <= pos.second_entry * (1 + BREAKEVEN_THRESHOLD):
                should_close = True
        else:
            if pos.second_entry * (1 - BREAKEVEN_THRESHOLD) <= price <= pos.second_entry * (1 + BREAKEVEN_THRESHOLD):
                should_close = True

        if should_close:
            self.logger.info(
                f"{pos.symbol} 第二腿保本平仓\n"
                f"   当前: {price:.6f} | 入场: {pos.second_entry:.6f}"
            )
            self._close_second_leg(pos, price, "breakeven")

    def _close_first_leg(self, pos: SimpleHedgePosition, price: float) -> bool:
        """平第一腿（带重试机制）"""
        if not self.client:
            return False

        for attempt in range(3):
            try:
                close_side = "SELL" if pos.first_side == "LONG" else "BUY"

                order = self.client.place_market_order(
                    symbol=pos.symbol,
                    side=close_side,
                    quantity=pos.first_quantity,
                    position_side=pos.first_side
                )

                if not order:
                    self.logger.error(f"{pos.symbol} 第一腿平仓返回None，重试 {attempt+1}/3")
                    time.sleep(0.1)
                    continue

                if order.status == "REJECTED":
                    self.logger.error(f"{pos.symbol} 第一腿平仓被拒绝")
                    return False

                # 确认成交
                filled_price = self._wait_for_order_fill(
                    pos.symbol, order.order_id, price, "第一腿平仓"
                )

                if filled_price is not None:
                    # 使用实际成交价计算盈亏
                    if pos.first_side == "SHORT":
                        pnl_pct = (pos.first_entry - filled_price) / pos.first_entry
                    else:
                        pnl_pct = (filled_price - pos.first_entry) / pos.first_entry

                    fee = self.position_usdt * self.fee_rate * 2
                    pos.first_pnl = self.position_usdt * pnl_pct * self.leverage - fee
                    pos.first_closed = True

                    self.logger.info(
                        f"{pos.symbol} 第一腿平仓: {pos.first_pnl:+.4f} USDT ({pnl_pct*100:+.2f}%) @ {filled_price:.6f}"
                    )
                    return True
                else:
                    self.logger.error(f"{pos.symbol} 第一腿平仓未确认，重试 {attempt+1}/3")
                    time.sleep(0.1)

            except Exception as e:
                self.logger.error(f"{pos.symbol} 第一腿平仓异常 (尝试 {attempt+1}/3): {e}")
                time.sleep(0.1)

        self.logger.error(f"{pos.symbol} 第一腿平仓失败，已尝试3次")
        return False

    def _close_second_leg(self, pos: SimpleHedgePosition, price: float, reason: str) -> bool:
        """平第二腿（带重试机制）"""
        if not self.client:
            return False

        for attempt in range(3):
            try:
                close_side = "SELL" if pos.second_side == "LONG" else "BUY"

                order = self.client.place_market_order(
                    symbol=pos.symbol,
                    side=close_side,
                    quantity=pos.second_quantity,
                    position_side=pos.second_side
                )

                if not order:
                    self.logger.error(f"{pos.symbol} 第二腿平仓返回None，重试 {attempt+1}/3")
                    time.sleep(0.1)
                    continue

                if order.status == "REJECTED":
                    self.logger.error(f"{pos.symbol} 第二腿平仓被拒绝")
                    return False

                # 确认成交
                filled_price = self._wait_for_order_fill(
                    pos.symbol, order.order_id, price, "第二腿平仓"
                )

                if filled_price is not None:
                    # 使用实际成交价计算盈亏
                    if pos.second_side == "SHORT":
                        pnl_pct = (pos.second_entry - filled_price) / pos.second_entry
                    else:
                        pnl_pct = (filled_price - pos.second_entry) / pos.second_entry

                    fee = self.position_usdt * self.fee_rate * 2
                    pos.second_pnl = self.position_usdt * pnl_pct * self.leverage - fee
                    pos.total_pnl = pos.first_pnl + pos.second_pnl
                    pos.second_closed = True
                    pos.close_time = datetime.now(timezone.utc)
                    pos.close_reason = reason

                    self.logger.info(
                        f"{pos.symbol} 对冲完成\n"
                        f"   第一腿: {pos.first_pnl:+.4f} USDT\n"
                        f"   第二腿: {pos.second_pnl:+.4f} USDT\n"
                        f"   总盈亏: {pos.total_pnl:+.4f} USDT"
                    )

                    if self._on_hedge_closed:
                        self._on_hedge_closed(pos)

                    return True
                else:
                    self.logger.error(f"{pos.symbol} 第二腿平仓未确认，重试 {attempt+1}/3")
                    time.sleep(0.1)

            except Exception as e:
                self.logger.error(f"{pos.symbol} 第二腿平仓异常 (尝试 {attempt+1}/3): {e}")
                time.sleep(0.1)

        self.logger.error(f"{pos.symbol} 第二腿平仓失败，已尝试3次")
        return False

    def close_all(self, reason: str = "manual") -> None:
        """平掉所有持仓"""
        for symbol, pos in list(self.positions.items()):
            if pos.is_closed:
                continue

            try:
                ticker = self.client.get_ticker_price(symbol) if self.client else None
                if ticker and isinstance(ticker, dict) and "price" in ticker:
                    price = float(ticker["price"])
                else:
                    continue
            except Exception:
                continue

            if pos.is_first_open and not pos.first_closed:
                self._close_first_leg(pos, price)

            if pos.is_second_open and not pos.second_closed:
                self._close_second_leg(pos, price, reason)

    def get_stats(self) -> dict:
        """获取统计信息"""
        total = len(self.positions)
        closed = sum(1 for p in self.positions.values() if p.is_closed)
        active = total - closed

        total_pnl = sum(p.total_pnl for p in self.positions.values() if p.is_closed)
        winning = sum(1 for p in self.positions.values() if p.is_closed and p.total_pnl > 0)

        win_rate = (winning / closed * 100) if closed > 0 else 0

        return {
            "total_trades": closed,
            "active_positions": active,
            "total_pnl": total_pnl,
            "win_rate": win_rate,
        }
