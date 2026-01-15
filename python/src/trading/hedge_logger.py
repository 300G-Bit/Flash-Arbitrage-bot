"""
对冲交易日志记录器 - 记录对冲策略的完整数据

功能:
- 记录对冲交易的两腿数据（第一腿、第二腿）
- 记录每笔订单的盈亏和资产变化
- 导出为JSON/CSV格式
- 兼容现有分析脚本 (trade_logger.py) 的数据格式
- 记录运行时配置参数
"""

import csv
import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .hedge_types import HedgePosition, HedgeState, PinSignal


@dataclass
class HedgeTradeRecord:
    """对冲交易记录 - 兼容现有分析脚本格式"""

    # 基本信息（兼容 TradeRecord）
    trade_id: str
    signal_id: str
    symbol: str
    direction: str                    # UP/DOWN

    # 插针信号信息
    start_price: float
    peak_price: float
    entry_price: float                # 检测时的入场价
    amplitude: float
    retracement: float

    # 第一腿（反向开仓）
    first_leg_side: str               # SHORT/LONG
    first_leg_entry_price: float
    first_leg_quantity: float
    first_leg_order_id: str
    first_leg_time: Optional[datetime] = None
    first_leg_exit_price: float = 0.0
    first_leg_exit_time: Optional[datetime] = None
    first_leg_pnl: float = 0.0
    first_leg_commission: float = 0.0

    # 第二腿（对冲）
    second_leg_side: str = ""
    second_leg_entry_price: float = 0.0
    second_leg_quantity: float = 0.0
    second_leg_order_id: str = ""
    second_leg_time: Optional[datetime] = None
    second_leg_exit_price: float = 0.0
    second_leg_exit_time: Optional[datetime] = None
    second_leg_pnl: float = 0.0
    second_leg_commission: float = 0.0

    # 目标价格
    hedge_target_price: float = 0.0
    take_profit_price: float = 0.0
    stop_loss_price: float = 0.0

    # 资产变化
    balance_before: float = 0.0       # 开仓前余额
    balance_after: float = 0.0        # 平仓后余额
    total_pnl: float = 0.0            # 总盈亏
    total_fees: float = 0.0           # 总手续费

    # 运行时配置
    runtime_config: Dict = field(default_factory=dict)

    # 时间戳
    signal_time: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None
    close_reason: str = ""

    # 持仓时长
    duration_seconds: float = 0.0

    # 状态
    status: str = "opened"             # opened/closed/failed/timeout

    @classmethod
    def from_hedge_position(
        cls,
        hedge: HedgePosition,
        balance_before: float = 0.0,
        balance_after: float = 0.0,
        runtime_config: Dict = None
    ) -> "HedgeTradeRecord":
        """从 HedgePosition 创建记录

        Args:
            hedge: 对冲持仓对象
            balance_before: 开仓前余额
            balance_after: 平仓后余额
            runtime_config: 运行时配置

        Returns:
            HedgeTradeRecord对象
        """
        signal = hedge.signal

        return cls(
            trade_id=f"hedge_{hedge.symbol}_{int(hedge.created_at.timestamp())}",
            signal_id=signal.signal_id,
            symbol=hedge.symbol,
            direction=signal.direction,
            start_price=signal.start_price,
            peak_price=signal.peak_price,
            entry_price=signal.entry_price,
            amplitude=signal.amplitude,
            retracement=signal.retracement,
            # 第一腿
            first_leg_side=hedge.first_leg_side,
            first_leg_entry_price=hedge.first_leg_entry_price,
            first_leg_quantity=hedge.first_leg_quantity,
            first_leg_order_id=hedge.first_leg_order_id,
            first_leg_time=hedge.first_leg_time,
            first_leg_exit_price=hedge.first_leg_exit_price or 0.0,
            first_leg_exit_time=hedge.closed_at if hedge.close_reason else None,
            first_leg_pnl=hedge.first_leg_pnl,
            # 第二腿
            second_leg_side=hedge.second_leg_side,
            second_leg_entry_price=hedge.second_leg_entry_price,
            second_leg_quantity=hedge.second_leg_quantity,
            second_leg_order_id=hedge.second_leg_order_id,
            second_leg_time=hedge.second_leg_time,
            second_leg_exit_price=hedge.second_leg_exit_price or 0.0,
            second_leg_exit_time=hedge.closed_at if hedge.close_reason else None,
            second_leg_pnl=hedge.second_leg_pnl,
            # 目标价格
            hedge_target_price=hedge.hedge_target_price,
            take_profit_price=hedge.take_profit_price,
            stop_loss_price=hedge.stop_loss_price,
            # 资产
            balance_before=balance_before,
            balance_after=balance_after,
            total_pnl=hedge.total_pnl,
            total_fees=abs(hedge.first_leg_pnl) * 0.0004 * 2 + abs(hedge.second_leg_pnl) * 0.0004 * 2,
            # 配置
            runtime_config=runtime_config or {},
            # 时间
            signal_time=signal.detected_at,
            created_at=hedge.created_at,
            closed_at=hedge.closed_at,
            close_reason=hedge.close_reason,
            duration_seconds=hedge.first_leg_duration,
            # 状态
            status="closed" if hedge.close_reason else "opened"
        )

    @staticmethod
    def _ensure_aware(dt: datetime) -> Optional[datetime]:
        """确保 datetime 是 timezone-aware

        Args:
            dt: datetime 对象

        Returns:
            timezone-aware datetime 对象，如果输入为 None 则返回 None
        """
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def to_dict(self) -> Dict:
        """转换为字典"""
        d = asdict(self)
        # 转换 datetime 为字符串
        for key, value in d.items():
            if isinstance(value, datetime):
                d[key] = value.isoformat()
            elif value is None:
                d[key] = None
        return d

    def to_spike_format(self) -> Dict:
        """转换为与现有分析脚本兼容的格式

        兼容 TradeRecord.to_spike_format() 的结构。
        """
        # 格式化时间戳
        detected_str = ""
        if self.signal_time:
            detected_str = self.signal_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        opened_str = ""
        if self.first_leg_time:
            opened_str = self.first_leg_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        closed_str = ""
        if self.closed_at:
            closed_str = self.closed_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        # 计算止损止盈结果
        sl_hit = self.close_reason == "stop_loss"
        tp_hit = self.close_reason == "take_profit"

        return {
            # 兼容字段（与 TradeRecord 对齐）
            "id": self.signal_id,
            "trade_id": self.trade_id,
            "detected_at": detected_str,
            "symbol": self.symbol,
            "direction": self.direction,
            "side": self.first_leg_side,  # 主腿方向
            "start_price": self.start_price,
            "peak_price": self.peak_price,
            "entry_price": self.first_leg_entry_price,
            "exit_price": self.first_leg_exit_price or self.first_leg_entry_price,
            "amplitude_percent": self.amplitude,
            "retracement_percent": self.retracement,

            # 止损止盈
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "stop_loss_percent": 1.0,  # 默认值
            "take_profit_percent": 0.5,  # 默认值

            # 数量
            "quantity": self.first_leg_quantity,
            "position_usdt": self.first_leg_quantity * self.first_leg_entry_price,
            "leverage": self.runtime_config.get("leverage", 20),

            # 时间
            "opened_at": opened_str,
            "closed_at": closed_str,
            "duration_seconds": self.duration_seconds,

            # 结果
            "status": self.status,
            "exit_reason": self.close_reason,
            "realized_pnl": self.total_pnl,
            "pnl_percent": (self.total_pnl / (self.first_leg_quantity * self.first_leg_entry_price) * 100)
                          if self.first_leg_quantity and self.first_leg_entry_price else 0.0,
            "fee_paid": self.total_fees,
            "net_pnl": self.total_pnl - self.total_fees,
            "is_profit": self.total_pnl > 0,

            # 扩展字段（对冲特有）
            "hedge": {
                "second_leg_entry": self.second_leg_entry_price,
                "second_leg_exit": self.second_leg_exit_price,
                "second_leg_quantity": self.second_leg_quantity,
                "first_leg_pnl": self.first_leg_pnl,
                "second_leg_pnl": self.second_leg_pnl,
                "hedge_target_price": self.hedge_target_price,
                "is_hedged": self.second_leg_order_id != "",
            },

            # 兼容 tp_sl_results
            "tp_sl_results": {
                "stop_loss_hit": sl_hit,
                "take_profit_hit": tp_hit,
                "stop_loss_price": self.stop_loss_price,
                "take_profit_price": self.take_profit_price,
                "exit_price": self.first_leg_exit_price or self.first_leg_entry_price,
                "exit_reason": self.close_reason
            },

            # 配置信息
            "_config": {
                "capital": self.runtime_config.get("position_usdt", 15.0),
                "leverage": self.runtime_config.get("leverage", 20),
                "fee_rate": 0.0004,
                "hedge_retracement_percent": self.runtime_config.get("hedge_retracement_percent", 50.0),
                "hedge_wait_timeout": self.runtime_config.get("hedge_wait_timeout_seconds", 60),
            },

            # 持续时间信息
            "duration_info": {
                "signal_to_entry": (self._ensure_aware(self.first_leg_time) - self._ensure_aware(self.signal_time)).total_seconds()
                                   if self.first_leg_time and self.signal_time else 0,
                "holding_duration": self.duration_seconds,
                "first_to_second_leg": (self._ensure_aware(self.second_leg_time) - self._ensure_aware(self.first_leg_time)).total_seconds()
                                       if self.second_leg_time and self.first_leg_time else 0,
                "total_duration": (self._ensure_aware(self.closed_at) - self._ensure_aware(self.signal_time)).total_seconds()
                                  if self.closed_at and self.signal_time else 0
            },

            # 资产变化
            "balance_info": {
                "balance_before": self.balance_before,
                "balance_after": self.balance_after,
                "balance_change": self.balance_after - self.balance_before if self.balance_after > 0 else 0.0,
            },

            # 来源标记
            "_source": "hedge_trading",
            "_record_type": "hedge"  # 标识为对冲交易记录
        }


class HedgeTradeLogger:
    """对冲交易日志记录器

    记录对冲策略的所有交易数据，支持多种导出格式。
    """

    def __init__(
        self,
        log_dir: str = "hedge_trades",
        auto_save: bool = True
    ):
        """初始化日志记录器

        Args:
            log_dir: 日志目录
            auto_save: 是否自动保存
        """
        self.log_dir = Path(log_dir)
        self.auto_save = auto_save

        # 确保目录存在
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # 记录存储
        self._records: List[HedgeTradeRecord] = []
        self._records_by_id: Dict[str, HedgeTradeRecord] = {}

        # 运行时配置
        self._runtime_config: Dict = {}

        # 初始余额
        self._initial_balance: float = 0.0

        # 统计
        self._stats = {
            "total_trades": 0,
            "hedged_trades": 0,
            "timeout_trades": 0,
            "winning": 0,
            "losing": 0,
            "total_pnl": 0,
            "total_fees": 0
        }

    def set_runtime_config(self, config: Dict):
        """设置运行时配置

        Args:
            config: 配置字典
        """
        self._runtime_config = config
        # 保存配置到文件
        self._save_config()

    def set_initial_balance(self, balance: float):
        """设置初始余额

        Args:
            balance: 初始余额
        """
        self._initial_balance = balance

    def record_hedge_opened(self, hedge: HedgePosition):
        """记录对冲开仓（第一腿）

        Args:
            hedge: 对冲持仓对象
        """
        if hedge.symbol in self._records_by_id:
            return  # 已记录

        record = HedgeTradeRecord.from_hedge_position(
            hedge,
            balance_before=self._initial_balance,
            runtime_config=self._runtime_config
        )
        record.status = "opened"

        self._records.append(record)
        self._records_by_id[hedge.symbol] = record
        self._stats["total_trades"] += 1

        if self.auto_save:
            self._save_record(record)

        return record

    def record_hedge_closed(
        self,
        hedge: HedgePosition,
        balance_after: float = 0.0
    ) -> HedgeTradeRecord:
        """记录对冲平仓

        Args:
            hedge: 对冲持仓对象
            balance_after: 平仓后余额

        Returns:
            HedgeTradeRecord对象
        """
        # 获取或创建记录
        record = self._records_by_id.get(hedge.symbol)
        if not record:
            record = self.record_hedge_opened(hedge)

        # 更新记录
        updated = HedgeTradeRecord.from_hedge_position(
            hedge,
            balance_before=record.balance_before or self._initial_balance,
            balance_after=balance_after,
            runtime_config=self._runtime_config
        )

        # 更新统计
        if updated.total_pnl > 0:
            self._stats["winning"] += 1
        else:
            self._stats["losing"] += 1

        self._stats["total_pnl"] += updated.total_pnl
        self._stats["total_fees"] += updated.total_fees

        if updated.second_leg_order_id:
            self._stats["hedged_trades"] += 1
        else:
            self._stats["timeout_trades"] += 1

        # 更新存储
        idx = next(i for i, r in enumerate(self._records) if r.symbol == hedge.symbol)
        self._records[idx] = updated
        self._records_by_id[hedge.symbol] = updated

        if self.auto_save:
            self._save_record(updated)

        return updated

    def _save_config(self):
        """保存运行时配置"""
        config_path = self.log_dir / "runtime_config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "config": self._runtime_config
            }, f, indent=2, ensure_ascii=False)

    def _save_record(self, record: HedgeTradeRecord):
        """保存单条记录为JSON文件

        Args:
            record: 对冲交易记录
        """
        timestamp = int(record.created_at.timestamp())
        filename = f"hedge_{record.symbol}_{timestamp}.json"
        filepath = self.log_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(record.to_spike_format(), f, indent=2, ensure_ascii=False)

    # ==================== 导出功能 ====================

    def export_to_json(self, filepath: str = None) -> str:
        """导出为JSON文件

        Args:
            filepath: 导出文件路径

        Returns:
            文件路径
        """
        if filepath is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filepath = self.log_dir / f"hedge_export_{timestamp}.json"

        export_data = {
            "export_time": datetime.now(timezone.utc).isoformat(),
            "runtime_config": self._runtime_config,
            "stats": self.get_stats(),
            "trades": [r.to_spike_format() for r in self._records]
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)

        return str(filepath)

    def export_to_csv(self, filepath: str = None) -> str:
        """导出为CSV文件

        Args:
            filepath: 导出文件路径

        Returns:
            文件路径
        """
        if filepath is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filepath = self.log_dir / f"hedge_export_{timestamp}.csv"

        if not self._records:
            return str(filepath)

        # 扁平化数据结构
        fieldnames = [
            "trade_id", "symbol", "direction", "first_leg_side",
            "start_price", "peak_price", "first_leg_entry", "first_leg_exit",
            "second_leg_entry", "second_leg_exit",
            "total_pnl", "total_fees", "net_pnl",
            "status", "close_reason", "duration_seconds",
            "is_hedged", "is_profit"
        ]

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for record in self._records:
                row = {
                    "trade_id": record.trade_id,
                    "symbol": record.symbol,
                    "direction": record.direction,
                    "first_leg_side": record.first_leg_side,
                    "start_price": record.start_price,
                    "peak_price": record.peak_price,
                    "first_leg_entry": record.first_leg_entry_price,
                    "first_leg_exit": record.first_leg_exit_price or record.first_leg_entry_price,
                    "second_leg_entry": record.second_leg_entry_price,
                    "second_leg_exit": record.second_leg_exit_price or record.second_leg_entry_price,
                    "total_pnl": record.total_pnl,
                    "total_fees": record.total_fees,
                    "net_pnl": record.total_pnl - record.total_fees,
                    "status": record.status,
                    "close_reason": record.close_reason,
                    "duration_seconds": record.duration_seconds,
                    "is_hedged": record.second_leg_order_id != "",
                    "is_profit": record.total_pnl > 0
                }
                writer.writerow(row)

        return str(filepath)

    def export_for_analysis(self, filepath: str = None) -> str:
        """导出为分析脚本兼容的格式

        Args:
            filepath: 导出文件路径

        Returns:
            文件路径
        """
        return self.export_to_json(filepath)

    # ==================== 查询方法 ====================

    def get_records(self, limit: int = None) -> List[HedgeTradeRecord]:
        """获取记录

        Args:
            limit: 数量限制

        Returns:
            HedgeTradeRecord列表
        """
        if limit:
            return self._records[-limit:]
        return self._records.copy()

    def get_winning_trades(self) -> List[HedgeTradeRecord]:
        """获取盈利交易"""
        return [r for r in self._records if r.total_pnl > 0]

    def get_losing_trades(self) -> List[HedgeTradeRecord]:
        """获取亏损交易"""
        return [r for r in self._records if r.total_pnl <= 0]

    # ==================== 统计方法 ====================

    def get_stats(self) -> Dict:
        """获取统计信息

        Returns:
            统计字典
        """
        total = self._stats["total_trades"]
        winning = self._stats["winning"]
        hedged = self._stats["hedged_trades"]

        return {
            "total_trades": total,
            "hedged_trades": hedged,
            "timeout_trades": self._stats["timeout_trades"],
            "hedge_rate": (hedged / total * 100) if total > 0 else 0,
            "winning": winning,
            "losing": self._stats["losing"],
            "win_rate": (winning / total * 100) if total > 0 else 0,
            "total_pnl": self._stats["total_pnl"],
            "total_fees": self._stats["total_fees"],
            "net_pnl": self._stats["total_pnl"] - self._stats["total_fees"],
            "avg_pnl": (self._stats["total_pnl"] / total) if total > 0 else 0,
            "max_win": max((r.total_pnl for r in self._records), default=0),
            "max_loss": min((r.total_pnl for r in self._records), default=0),
        }

    def print_summary(self):
        """打印统计摘要"""
        stats = self.get_stats()

        print("\n" + "=" * 60)
        print("对冲交易统计摘要")
        print("=" * 60)
        print(f"总交易次数:   {stats['total_trades']}")
        print(f"已对冲:       {stats['hedged_trades']} ({stats['hedge_rate']:.1f}%)")
        print(f"超时未对冲:   {stats['timeout_trades']}")
        print(f"盈利次数:     {stats['winning']}")
        print(f"亏损次数:     {stats['losing']}")
        print(f"胜率:         {stats['win_rate']:.2f}%")
        print("-" * 60)
        print(f"总盈亏:       {stats['total_pnl']:.4f} USDT")
        print(f"总手续费:     {stats['total_fees']:.4f} USDT")
        print(f"净盈亏:       {stats['net_pnl']:.4f} USDT")
        print(f"平均盈亏:     {stats['avg_pnl']:.4f} USDT")
        print(f"最大盈利:     {stats['max_win']:.4f} USDT")
        print(f"最大亏损:     {stats['max_loss']:.4f} USDT")
        print("=" * 60 + "\n")

    # ==================== 清理方法 ====================

    def clear_all(self):
        """清空所有记录"""
        self._records.clear()
        self._records_by_id.clear()
        self._stats = {
            "total_trades": 0,
            "hedged_trades": 0,
            "timeout_trades": 0,
            "winning": 0,
            "losing": 0,
            "total_pnl": 0,
            "total_fees": 0
        }
