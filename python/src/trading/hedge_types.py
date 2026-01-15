"""
对冲交易数据类型

定义双向对冲策略使用的数据结构。
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class HedgeState(Enum):
    """对冲状态"""
    NONE = "none"              # 无持仓
    FIRST_LEG = "first_leg"    # 第一腿已开（等待对冲）
    HEDGED = "hedged"          # 已对冲（双向持仓）
    CLOSING = "closing"        # 正在平仓


@dataclass
class PinSignal:
    """插针信号"""
    symbol: str
    direction: str  # UP / DOWN
    start_price: float
    peak_price: float
    entry_price: float  # 第一腿入场价
    amplitude: float  # 幅度百分比
    retracement: float  # 回撤百分比
    detected_at: datetime = None
    signal_id: str = ""

    def __post_init__(self):
        if self.detected_at is None:
            # 使用 UTC 时间（带时区信息）
            self.detected_at = datetime.now(timezone.utc)
        # 如果传入的 datetime 没有时区信息，添加 UTC 时区
        elif self.detected_at.tzinfo is None:
            self.detected_at = self.detected_at.replace(tzinfo=timezone.utc)

        if not self.signal_id:
            self.signal_id = f"{self.symbol}_{int(self.detected_at.timestamp())}"

    def __str__(self):
        icon = "🔺" if self.direction == "UP" else "🔻"
        return f"{self.symbol} {icon} 幅度:{self.amplitude:.2f}% 回撤:{self.retracement:.1f}%"

    def get_first_leg_side(self) -> str:
        """获取第一腿方向（与插针方向相反）"""
        return "SHORT" if self.direction == "UP" else "LONG"

    def get_second_leg_side(self) -> str:
        """获取第二腿方向（与第一腿相反，即对冲方向）"""
        return "LONG" if self.direction == "UP" else "SHORT"


@dataclass
class HedgePosition:
    """对冲持仓记录"""
    symbol: str
    signal: PinSignal
    state: HedgeState = HedgeState.NONE

    # 第一腿（插针反向）
    first_leg_side: str = ""  # SHORT（上插针）或 LONG（下插针）
    first_leg_entry_price: float = 0.0
    first_leg_quantity: float = 0.0
    first_leg_order_id: str = ""
    first_leg_filled: bool = False
    first_leg_time: Optional[datetime] = None
    first_leg_exit_price: float = 0.0  # 第一腿平仓价格

    # 第二腿（对冲腿）
    second_leg_side: str = ""  # LONG（上插针）或 SHORT（下插针）
    second_leg_entry_price: float = 0.0
    second_leg_quantity: float = 0.0
    second_leg_order_id: str = ""
    second_leg_filled: bool = False
    second_leg_time: Optional[datetime] = None
    second_leg_exit_price: float = 0.0  # 第二腿平仓价格

    # 目标价格
    hedge_target_price: float = 0.0  # 开对冲腿的目标价格
    take_profit_price: float = 0.0  # 止盈价格（已弃用，使用独立字段）
    stop_loss_price: float = 0.0  # 止损价格（已弃用，使用独立字段）

    # 独立止盈止损价格（新增 - 支持两腿独立平仓）
    first_leg_take_profit: float = 0.0   # 第一腿止盈价
    first_leg_stop_loss: float = 0.0     # 第一腿止损价（设为入场价保本）
    second_leg_take_profit: float = 0.0  # 第二腿止盈价（顺势单不固定）
    second_leg_stop_loss: float = 0.0    # 第二腿动态止损价
    second_leg_max_profit: float = 0.0   # 第二腿最高浮盈%（用于追踪止损）

    # 单腿平仓状态（新增 - 支持两腿分别平仓）
    first_leg_closed: bool = False       # 第一腿是否已平仓
    second_leg_closed: bool = False      # 第二腿是否已平仓

    # 盈亏
    first_leg_pnl: float = 0.0
    second_leg_pnl: float = 0.0
    total_pnl: float = 0.0

    # 状态
    created_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    close_reason: str = ""

    # 错误信息
    error_message: str = ""

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
        # 如果未设置第一腿方向，从信号推断
        if not self.first_leg_side and self.signal:
            self.first_leg_side = self.signal.get_first_leg_side()
        if not self.second_leg_side and self.signal:
            self.second_leg_side = self.signal.get_second_leg_side()

    @property
    def is_first_leg_open(self) -> bool:
        """第一腿是否已开仓"""
        return self.first_leg_filled and self.first_leg_order_id

    @property
    def is_second_leg_open(self) -> bool:
        """第二腿是否已开仓"""
        return self.second_leg_filled and self.second_leg_order_id

    @property
    def is_fully_hedged(self) -> bool:
        """是否完全对冲（两腿都开了）"""
        return self.is_first_leg_open and self.is_second_leg_open

    @property
    def is_partially_closed(self) -> bool:
        """是否部分平仓（只平了一腿）"""
        return self.first_leg_closed != self.second_leg_closed

    @property
    def age_seconds(self) -> float:
        """持仓年龄（秒）"""
        if self.created_at:
            return (datetime.now(timezone.utc) - self.created_at).total_seconds()
        return 0

    @property
    def first_leg_duration(self) -> float:
        """第一腿持续时间（秒）"""
        if self.first_leg_time:
            if self.closed_at:
                return (self.closed_at - self.first_leg_time).total_seconds()
            return (datetime.now(timezone.utc) - self.first_leg_time).total_seconds()
        return 0

    def get_close_order(self) -> list:
        """获取平仓顺序（先平哪个腿）

        Returns:
            列表，元素为 "first" 或 "second"
        """
        # 默认先平空单，再平多单
        if self.first_leg_side == "SHORT":
            return ["first", "second"]  # 先平第一腿（空），再平第二腿（多）
        else:
            return ["second", "first"]  # 先平第二腿（空），再平第一腿（多）

    def calculate_pnl(self, exit_price_1: float, exit_price_2: float,
                      position_usdt: float, leverage: int, fee_rate: float) -> tuple:
        """计算盈亏

        Args:
            exit_price_1: 第一腿平仓价
            exit_price_2: 第二腿平仓价
            position_usdt: 仓位USDT金额
            leverage: 杠杆倍数
            fee_rate: 手续费率

        Returns:
            (first_leg_pnl, second_leg_pnl, total_pnl)
        """
        # 第一腿盈亏
        if self.first_leg_side == "SHORT":
            pnl_percent_1 = (self.first_leg_entry_price - exit_price_1) / self.first_leg_entry_price
        else:
            pnl_percent_1 = (exit_price_1 - self.first_leg_entry_price) / self.first_leg_entry_price

        # 第二腿盈亏
        if self.second_leg_side == "SHORT":
            pnl_percent_2 = (self.second_leg_entry_price - exit_price_2) / self.second_leg_entry_price
        else:
            pnl_percent_2 = (exit_price_2 - self.second_leg_entry_price) / self.second_leg_entry_price

        # 计算金额（考虑杠杆和手续费）
        fee_per_leg = position_usdt * fee_rate * 2  # 开仓+平仓

        self.first_leg_pnl = position_usdt * pnl_percent_1 * leverage - fee_per_leg
        self.second_leg_pnl = position_usdt * pnl_percent_2 * leverage - fee_per_leg
        self.total_pnl = self.first_leg_pnl + self.second_leg_pnl

        return self.first_leg_pnl, self.second_leg_pnl, self.total_pnl


@dataclass
class HedgeConfig:
    """对冲策略配置"""
    enable_hedge: bool = True  # 启用对冲模式
    hedge_retracement_percent: float = 50.0  # 回撤50%时开对冲腿
    hedge_wait_timeout_seconds: int = 60  # 等待对冲的超时时间（秒）
    close_order: str = "SHORT_FIRST"  # 平仓顺序：先平空
    take_profit_after_hedge: float = 0.5  # 对冲后止盈点(%)
    stop_loss_after_hedge: float = 1.0  # 对冲后止损点(%)
    quick_tp_enabled: bool = True  # 启用第二腿快速止盈
    quick_tp_percent: float = 0.3  # 第二腿快速止盈点位(%)

    def get_close_order_list(self, first_leg_side: str) -> list:
        """获取平仓顺序列表"""
        if self.close_order == "SHORT_FIRST":
            # 先平空单
            if first_leg_side == "SHORT":
                return ["first", "second"]
            else:
                return ["second", "first"]
        else:
            # 先平第一腿
            return ["first", "second"]
