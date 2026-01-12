"""
Trade Simulator Module.

模拟插针信号的交易，计算不同持仓时间的盈亏。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import structlog

from ..data.signal_recorder import PinSignalRecord

logger = structlog.get_logger(__name__)


# ============== 配置 ==============

SIMULATOR_CONFIG = {
    "position_size_usd": 15,        # 本金 USDT
    "leverage": 20,                  # 杠杆倍数
    "stop_loss_percent": 20,         # 止损百分比（相对于本金）
    "hold_periods": [30, 60, 90, 180],  # 测试的持仓时间段（秒）
    "entry_retracement_min": 30,     # 最小回撤入场%
    "entry_retracement_max": 70,     # 最大回撤入场%
    "taker_fee_rate": 0.0004,        # Taker手续费率 (0.04%)
}


# ============== 数据结构 ==============

@dataclass
class TradeResult:
    """单个时间段的交易结果"""
    hold_period: int          # 持仓时间（秒）
    entry_price: float        # 入场价
    exit_price: float         # 出场价
    direction: str            # 方向 LONG/SHORT

    profit_usd: float         # 盈利 USD
    profit_percent: float     # 盈利百分比（相对于本金）
    fee_usd: float            # 手续费

    is_profitable: bool       # 是否盈利
    is_stopped: bool          # 是否触发止损


@dataclass
class SimulationResult:
    """模拟结果"""
    record_id: str
    symbol: str
    direction: str            # UP/DOWN（信号方向）

    # 各时间段结果
    results: Dict[int, TradeResult] = field(default_factory=dict)  # {hold_period: TradeResult}

    # 最佳结果
    best_period: int = None
    best_profit_usd: float = None
    best_profit_percent: float = None

    # 汇总
    is_tradeable: bool = False  # 是否有至少一个时间段可交易


# ============== 交易模拟器 ==============

class TradeSimulator:
    """
    交易模拟器

    功能：
    1. 根据信号记录模拟交易
    2. 测试多个持仓时间（30s/60s/90s/180s）
    3. 计算各时间段的盈亏
    4. 考虑手续费和止损
    """

    def __init__(self, config: Optional[Dict] = None):
        """初始化模拟器

        Args:
            config: 配置字典
        """
        self.config = {**SIMULATOR_CONFIG, **(config or {})}
        self.logger = logger.bind(component="TradeSimulator")

    def simulate(self, record: PinSignalRecord) -> SimulationResult:
        """模拟交易

        Args:
            record: 信号记录

        Returns:
            SimulationResult: 模拟结果
        """
        result = SimulationResult(
            record_id=record.id,
            symbol=record.symbol,
            direction=record.direction,
        )

        # 计算各时间段的盈亏
        for hold_period in self.config["hold_periods"]:
            trade_result = self._simulate_period(record, hold_period)
            result.results[hold_period] = trade_result

            # 更新最佳结果
            if trade_result.profit_usd > 0:
                result.is_tradeable = True
                if (result.best_profit_usd is None or
                    trade_result.profit_usd > result.best_profit_usd):
                    result.best_period = hold_period
                    result.best_profit_usd = trade_result.profit_usd
                    result.best_profit_percent = trade_result.profit_percent

        self.logger.debug(
            "Simulation completed",
            record_id=record.id[:8],
            symbol=record.symbol,
            direction=record.direction,
            is_tradeable=result.is_tradeable,
            best_period=result.best_period,
            best_profit=result.best_profit_usd
        )

        return result

    def _simulate_period(
        self,
        record: PinSignalRecord,
        hold_period: int
    ) -> TradeResult:
        """模拟单个持仓时间段的交易

        Args:
            record: 信号记录
            hold_period: 持仓时间（秒）

        Returns:
            TradeResult: 交易结果
        """
        # 获取入场价和出场价
        entry_price = self._get_entry_price(record)
        exit_price = self._get_exit_price(record, hold_period, entry_price)

        if entry_price is None or exit_price is None:
            # 数据不完整，返回空结果
            return TradeResult(
                hold_period=hold_period,
                entry_price=0,
                exit_price=0,
                direction="LONG",
                profit_usd=0,
                profit_percent=0,
                fee_usd=0,
                is_profitable=False,
                is_stopped=False
            )

        # 确定交易方向
        # UP信号：做多（向下插针后反弹）
        # DOWN信号：做空（向上插针后回落）
        if record.direction == "UP":
            direction = "LONG"
        else:
            direction = "SHORT"

        # 计算盈亏
        profit_usd, profit_percent, fee_usd = self._calculate_pnl(
            entry_price, exit_price, direction
        )

        # 检查是否止损
        is_stopped = self._check_stop_loss(record, profit_usd)

        return TradeResult(
            hold_period=hold_period,
            entry_price=entry_price,
            exit_price=exit_price,
            direction=direction,
            profit_usd=profit_usd,
            profit_percent=profit_percent,
            fee_usd=fee_usd,
            is_profitable=profit_usd > 0,
            is_stopped=is_stopped
        )

    def _get_entry_price(self, record: PinSignalRecord) -> Optional[float]:
        """获取入场价格

        优先使用best_entry_price（回撤最深的位置）
        否则使用current_price（检测时的价格）
        """
        if record.best_entry_price is not None:
            return record.best_entry_price
        return record.current_price

    def _get_exit_price(
        self,
        record: PinSignalRecord,
        hold_period: int,
        entry_price: float
    ) -> Optional[float]:
        """获取出场价格

        Args:
            record: 信号记录
            hold_period: 持仓时间
            entry_price: 入场价

        Returns:
            出场价，如果数据不完整返回None
        """
        price_attr = f"price_after_{hold_period}s"
        price = getattr(record, price_attr, None)

        if price is None:
            return None

        # 检查止损
        # 如果价格突破了插针顶点，触发止损
        if record.direction == "UP":
            # 做多：价格低于入场价一定比例止损
            stop_loss_price = entry_price * (1 - self.config["stop_loss_percent"] / 100)
            if price < stop_loss_price:
                return stop_loss_price
        else:
            # 做空：价格高于入场价一定比例止损
            stop_loss_price = entry_price * (1 + self.config["stop_loss_percent"] / 100)
            if price > stop_loss_price:
                return stop_loss_price

        return price

    def _calculate_pnl(
        self,
        entry_price: float,
        exit_price: float,
        direction: str
    ) -> Tuple[float, float, float]:
        """计算盈亏

        Args:
            entry_price: 入场价
            exit_price: 出场价
            direction: 方向 LONG/SHORT

        Returns:
            (profit_usd, profit_percent, fee_usd)
        """
        position_size = self.config["position_size_usd"]
        leverage = self.config["leverage"]

        # 计算价格变化率
        if direction == "LONG":
            price_change_pct = (exit_price - entry_price) / entry_price
        else:  # SHORT
            price_change_pct = (entry_price - exit_price) / entry_price

        # 杠杆后的盈亏百分比
        pnl_pct = price_change_pct * leverage

        # 盈亏金额
        profit_usd = position_size * pnl_pct

        # 手续费（开仓+平仓）
        notional_value = position_size * leverage
        fee_usd = notional_value * self.config["taker_fee_rate"] * 2

        # 扣除手续费后的净盈亏
        net_profit_usd = profit_usd - fee_usd
        net_profit_percent = (net_profit_usd / position_size) * 100

        return net_profit_usd, net_profit_percent, fee_usd

    def _check_stop_loss(self, record: PinSignalRecord, profit_usd: float) -> bool:
        """检查是否触发止损

        Args:
            record: 信号记录
            profit_usd: 盈亏金额

        Returns:
            是否触发止损
        """
        max_loss = self.config["position_size_usd"] * (
            self.config["stop_loss_percent"] / 100
        )
        return profit_usd < -max_loss


# ============== 批量模拟 ==============

class BatchSimulator:
    """批量模拟器"""

    def __init__(self, config: Optional[Dict] = None):
        self.simulator = TradeSimulator(config)
        self.logger = logger.bind(component="BatchSimulator")

    def simulate_all(
        self,
        records: List[PinSignalRecord]
    ) -> List[SimulationResult]:
        """批量模拟

        Args:
            records: 信号记录列表

        Returns:
            模拟结果列表
        """
        results = []

        for record in records:
            try:
                result = self.simulator.simulate(record)
                results.append(result)
            except Exception as e:
                self.logger.error(
                    "Simulation failed",
                    record_id=record.id[:8],
                    error=str(e)
                )

        return results

    def simulate_and_update(
        self,
        records: List[PinSignalRecord]
    ) -> List[PinSignalRecord]:
        """模拟并更新记录中的盈利数据

        Args:
            records: 信号记录列表

        Returns:
            更新后的记录列表
        """
        for record in records:
            result = self.simulator.simulate(record)

            # 更新记录中的盈利数据
            for hold_period, trade_result in result.results.items():
                setattr(record, f"profit_{hold_period}s_usd", trade_result.profit_usd)
                setattr(record, f"profit_{hold_period}s_percent", trade_result.profit_percent)

        return records
