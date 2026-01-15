"""
ATR插针检测器数据结构

定义基于ATR的插针检测信号类型
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class SpikeDirection(str, Enum):
    """插针方向"""
    UP = "UP"       # 上涨插针（快速拉升后回落）- 做空机会
    DOWN = "DOWN"   # 下跌插针（快速下跌后反弹）- 做多机会


class SpikeType(str, Enum):
    """插针类型"""
    UP_PIN = "UP_PIN"       # 上涨插针（高位做空）
    DOWN_PIN = "DOWN_PIN"   # 下跌插针（低位做多）


@dataclass
class SpikeSignal:
    """插针信号

    基于ATR的动态插针检测信号
    """
    symbol: str
    spike_type: SpikeType
    direction: SpikeDirection
    entry_price: float
    extreme_price: float           # 插针极端价格（最高点或最低点）
    start_price: float             # 插针起始价格
    confidence: int                # 置信度 (0-100)

    # ATR相关
    atr_value: float               # 当前ATR值
    spike_threshold: float         # 触发速度阈值
    retrace_threshold: float       # 回调/反弹阈值

    # 检测详情
    velocity_percent: float        # 价格变化速度（百分比）
    shadow_ratio: float = 0.0      # 影线/实体比值
    has_color_reversal: bool = False  # 是否有颜色反转
    has_false_breakout: bool = False  # 是否有假突破

    # 时间戳
    detected_at: datetime = None

    # 第二腿入场目标价（由simple_hedge使用）
    @property
    def second_leg_target(self) -> float:
        """计算第二腿入场目标价"""
        if self.direction == SpikeDirection.UP:
            # 上涨插针后回调，做多目标价
            return self.entry_price * (1 - self.retrace_threshold)
        else:
            # 下跌插针后反弹，做空目标价
            return self.entry_price * (1 + self.retrace_threshold)

    # 第一腿止盈目标价
    @property
    def first_leg_target(self) -> float:
        """第一腿止盈目标价"""
        if self.direction == SpikeDirection.UP:
            # 做空，价格下跌盈利
            return self.entry_price * (1 - self.retrace_threshold * 1.5)
        else:
            # 做多，价格上涨盈利
            return self.entry_price * (1 + self.retrace_threshold * 1.5)

    def __repr__(self) -> str:
        return (
            f"SpikeSignal({self.symbol}, {self.spike_type.value}, "
            f"entry={self.entry_price:.6f}, "
            f"velocity={self.velocity_percent:.2%}, "
            f"ATR={self.atr_value:.6f})"
        )


@dataclass
class ATRMetrics:
    """ATR指标"""
    period: int                 # ATR周期
    current_value: float        # 当前ATR值
    spike_threshold: float      # 速度阈值 = ATR × K1
    retrace_threshold: float    # 回调阈值 = ATR × K2

    # 用于计算的历史数据
    atr_history: list = None    # ATR历史值

    def __post_init__(self):
        if self.atr_history is None:
            self.atr_history = []

    def update(self, new_atr: float) -> None:
        """更新ATR值"""
        self.current_value = new_atr
        self.atr_history.append(new_atr)
        if len(self.atr_history) > self.period:
            self.atr_history.pop(0)

    def __repr__(self) -> str:
        return (
            f"ATRMetrics(period={self.period}, "
            f"ATR={self.current_value:.6f}, "
            f"spike_thr={self.spike_threshold:.6f}, "
            f"retrace_thr={self.retrace_threshold:.6f})"
        )
