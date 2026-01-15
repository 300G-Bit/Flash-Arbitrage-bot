"""
交易日志记录器 - 记录测试网交易的所有数据

功能:
- 记录每笔交易的完整信息
- 导出为JSON/CSV格式
- 生成交易统计报告
- 兼容现有spike数据格式
"""

import json
import csv
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .trade_executor import TradeResult, TradeSignal


@dataclass
class TradeRecord:
    """交易记录"""
    # 基本信息
    trade_id: str
    signal_id: str
    symbol: str
    side: str                     # LONG/SHORT
    direction: str                # UP/DOWN (插针方向)

    # 价格信息
    start_price: float            # 起始价格
    peak_price: float             # 峰值价格
    entry_price: float            # 入场价格
    exit_price: float             # 退出价格
    amplitude: float              # 振幅百分比
    retracement: float            # 回撤百分比

    # 止损止盈
    stop_loss_price: float
    take_profit_price: float
    stop_loss_percent: float
    take_profit_percent: float

    # 数量和杠杆
    quantity: float
    position_usdt: float
    leverage: int

    # 时间
    signal_time: float
    submitted_at: float
    opened_at: float
    closed_at: float
    duration: float               # 持仓时长(秒)

    # 结果
    status: str                   # opened/closed/failed
    exit_reason: str              # stop_loss/take_profit/manual
    realized_pnl: float
    pnl_percent: float
    fee_paid: float
    net_pnl: float                # 扣除手续费后的净盈亏

    # 元数据
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    source: str = "testnet_trading"

    @classmethod
    def from_trade_result(cls, result: TradeResult, exit_reason: str = "") -> "TradeRecord":
        """从TradeResult创建TradeRecord

        Args:
            result: 交易结果
            exit_reason: 退出原因

        Returns:
            TradeRecord对象
        """
        signal = result.signal

        return cls(
            trade_id=result.trade_id,
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.side,
            direction=signal.direction,
            start_price=signal.start_price,
            peak_price=signal.peak_price,
            entry_price=result.entry_price,
            exit_price=result.exit_price,
            amplitude=signal.amplitude,
            retracement=signal.retracement,
            stop_loss_price=signal.get_stop_loss_price(),
            take_profit_price=signal.get_take_profit_price(),
            stop_loss_percent=signal.stop_loss_percent,
            take_profit_percent=signal.take_profit_percent,
            quantity=result.quantity,
            position_usdt=signal.position_usdt,
            leverage=signal.leverage,
            signal_time=signal.signal_time,
            submitted_at=result.submitted_at,
            opened_at=result.opened_at,
            closed_at=result.closed_at,
            duration=result.duration,
            status=result.status.value,
            exit_reason=exit_reason,
            realized_pnl=result.realized_pnl,
            pnl_percent=result.pnl_percent,
            fee_paid=result.fee_paid,
            net_pnl=result.realized_pnl - result.fee_paid
        )

    def to_dict(self) -> Dict:
        """转换为字典"""
        return asdict(self)

    def to_spike_format(self) -> Dict:
        """转换为与spike数据兼容的格式

        用于与现有分析脚本兼容。
        """
        # 计算止损止盈结果
        sl_hit = self.exit_reason == "stop_loss"
        tp_hit = self.exit_reason == "take_profit"

        # 格式化时间戳(精确到毫秒)
        detected_at = datetime.fromtimestamp(self.signal_time)
        detected_str = detected_at.strftime("%Y-%m-%d %H:%M:%S.%f")
        detected_ms = detected_str[:23] + detected_str[26:]

        opened_at_str = ""
        if self.opened_at > 0:
            opened_dt = datetime.fromtimestamp(self.opened_at)
            opened_str = opened_dt.strftime("%Y-%m-%d %H:%M:%S.%f")
            opened_at_str = opened_str[:23] + opened_str[26:]

        closed_at_str = ""
        if self.closed_at > 0:
            closed_dt = datetime.fromtimestamp(self.closed_at)
            closed_str = closed_dt.strftime("%Y-%m-%d %H:%M:%S.%f")
            closed_at_str = closed_str[:23] + closed_str[26:]

        return {
            "id": self.signal_id,
            "trade_id": self.trade_id,
            "detected_at": detected_ms,
            "symbol": self.symbol,
            "direction": self.direction,
            "side": self.side,
            "start_price": self.start_price,
            "peak_price": self.peak_price,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "amplitude_percent": self.amplitude,
            "retracement_percent": self.retracement,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "stop_loss_percent": self.stop_loss_percent,
            "take_profit_percent": self.take_profit_percent,
            "quantity": self.quantity,
            "position_usdt": self.position_usdt,
            "leverage": self.leverage,
            "opened_at": opened_at_str,
            "closed_at": closed_at_str,
            "duration_seconds": self.duration,
            "status": self.status,
            "exit_reason": self.exit_reason,
            "realized_pnl": self.realized_pnl,
            "pnl_percent": self.pnl_percent,
            "fee_paid": self.fee_paid,
            "net_pnl": self.net_pnl,
            "is_profit": self.net_pnl > 0,
            # 兼容tp_sl_results
            "tp_sl_results": {
                "stop_loss_hit": sl_hit,
                "take_profit_hit": tp_hit,
                "stop_loss_price": self.stop_loss_price,
                "take_profit_price": self.take_profit_price,
                "exit_price": self.exit_price,
                "exit_reason": self.exit_reason
            },
            # 配置信息
            "_config": {
                "capital": self.position_usdt,
                "leverage": self.leverage,
                "fee_rate": 0.0004,
                "stop_loss_percent": self.stop_loss_percent,
                "take_profit_percent": self.take_profit_percent,
            },
            # 持续时间信息
            "duration_info": {
                "signal_to_entry": self.opened_at - self.submitted_at if self.opened_at > 0 else 0,
                "holding_duration": self.duration,
                "total_duration": self.closed_at - self.signal_time if self.closed_at > 0 else 0
            },
            # 来源标记
            "_source": "testnet_trading"
        }


class TradeLogger:
    """交易日志记录器

    记录所有测试网交易数据，支持多种导出格式。
    """

    def __init__(
        self,
        log_dir: str = "testnet_trades",
        auto_save: bool = True,
        save_interval: int = 60
    ):
        """初始化日志记录器

        Args:
            log_dir: 日志目录
            auto_save: 是否自动保存
            save_interval: 自动保存间隔(秒)
        """
        self.log_dir = Path(log_dir)
        self.auto_save = auto_save
        self.save_interval = save_interval

        # 确保目录存在
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # 记录存储
        self._records: List[TradeRecord] = []
        self._records_by_id: Dict[str, TradeRecord] = {}

        # 统计
        self._stats = {
            "total_trades": 0,
            "winning": 0,
            "losing": 0,
            "total_pnl": 0,
            "total_fees": 0
        }

    def add_trade(
        self,
        result: TradeResult,
        exit_reason: str = ""
    ) -> TradeRecord:
        """添加交易记录

        Args:
            result: 交易结果
            exit_reason: 退出原因

        Returns:
            TradeRecord对象
        """
        record = TradeRecord.from_trade_result(result, exit_reason)

        self._records.append(record)
        self._records_by_id[record.trade_id] = record

        # 更新统计
        self._stats["total_trades"] += 1
        if record.net_pnl > 0:
            self._stats["winning"] += 1
        else:
            self._stats["losing"] += 1
        self._stats["total_pnl"] += record.net_pnl
        self._stats["total_fees"] += record.fee_paid

        # 自动保存
        if self.auto_save:
            self.save_trade(record)

        return record

    def save_trade(self, record: TradeRecord):
        """保存单笔交易为JSON文件

        Args:
            record: 交易记录
        """
        filename = f"trade_{record.symbol}_{int(record.signal_time)}.json"
        filepath = self.log_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(record.to_spike_format(), f, indent=2, ensure_ascii=False)

    def save_all(self):
        """保存所有交易记录"""
        for record in self._records:
            self.save_trade(record)

    # ==================== 导出功能 ====================

    def export_to_json(self, filepath: str = None) -> str:
        """导出为JSON文件

        Args:
            filepath: 导出文件路径

        Returns:
            文件路径
        """
        if filepath is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = self.log_dir / f"trades_export_{timestamp}.json"

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump({
                "export_time": datetime.now().isoformat(),
                "stats": self.get_stats(),
                "trades": [r.to_spike_format() for r in self._records]
            }, f, indent=2, ensure_ascii=False)

        return str(filepath)

    def export_to_csv(self, filepath: str = None) -> str:
        """导出为CSV文件

        Args:
            filepath: 导出文件路径

        Returns:
            文件路径
        """
        if filepath is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = self.log_dir / f"trades_export_{timestamp}.csv"

        if not self._records:
            return str(filepath)

        # 获取所有字段
        fieldnames = list(self._records[0].to_dict().keys())

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for record in self._records:
                writer.writerow(record.to_dict())

        return str(filepath)

    def export_for_analysis(self, filepath: str = None) -> str:
        """导出为分析脚本兼容的格式

        Args:
            filepath: 导出文件路径

        Returns:
            文件路径
        """
        if filepath is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = self.log_dir / f"trades_analysis_{timestamp}.json"

        export_data = {
            "export_time": datetime.now().isoformat(),
            "stats": self.get_stats(),
            "trades": [r.to_spike_format() for r in self._records]
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)

        return str(filepath)

    # ==================== 查询方法 ====================

    def get_records(self, limit: int = None) -> List[TradeRecord]:
        """获取记录

        Args:
            limit: 数量限制

        Returns:
            TradeRecord列表
        """
        if limit:
            return self._records[-limit:]
        return self._records.copy()

    def get_record(self, trade_id: str) -> Optional[TradeRecord]:
        """获取单条记录

        Args:
            trade_id: 交易ID

        Returns:
            TradeRecord对象
        """
        return self._records_by_id.get(trade_id)

    def get_records_by_symbol(self, symbol: str) -> List[TradeRecord]:
        """获取交易对的记录

        Args:
            symbol: 交易对

        Returns:
            TradeRecord列表
        """
        return [r for r in self._records if r.symbol == symbol]

    def get_winning_trades(self) -> List[TradeRecord]:
        """获取盈利交易

        Returns:
            TradeRecord列表
        """
        return [r for r in self._records if r.net_pnl > 0]

    def get_losing_trades(self) -> List[TradeRecord]:
        """获取亏损交易

        Returns:
            TradeRecord列表
        """
        return [r for r in self._records if r.net_pnl <= 0]

    # ==================== 统计方法 ====================

    def get_stats(self) -> Dict:
        """获取统计信息

        Returns:
            统计字典
        """
        total = self._stats["total_trades"]
        winning = self._stats["winning"]

        return {
            "total_trades": total,
            "winning": winning,
            "losing": self._stats["losing"],
            "win_rate": (winning / total * 100) if total > 0 else 0,
            "total_pnl": self._stats["total_pnl"],
            "total_fees": self._stats["total_fees"],
            "net_pnl": self._stats["total_pnl"],
            "avg_pnl": (self._stats["total_pnl"] / total) if total > 0 else 0,
            "profit_factor": self._calculate_profit_factor(),
            "max_win": max((r.net_pnl for r in self._records), default=0),
            "max_loss": min((r.net_pnl for r in self._records), default=0),
            "avg_win": sum(r.net_pnl for r in self.get_winning_trades()) / winning if winning > 0 else 0,
            "avg_loss": sum(r.net_pnl for r in self.get_losing_trades()) / self._stats["losing"] if self._stats["losing"] > 0 else 0,
        }

    def _calculate_profit_factor(self) -> float:
        """计算盈亏比"""
        total_profit = sum(r.net_pnl for r in self.get_winning_trades())
        total_loss = abs(sum(r.net_pnl for r in self.get_losing_trades()))

        if total_loss == 0:
            return float("inf") if total_profit > 0 else 0
        return total_profit / total_loss

    def get_daily_stats(self) -> Dict:
        """按日期统计

        Returns:
            日期统计字典
        """
        daily = {}

        for record in self._records:
            date = datetime.fromtimestamp(record.signal_time).strftime("%Y-%m-%d")
            if date not in daily:
                daily[date] = {
                    "trades": 0,
                    "winning": 0,
                    "losing": 0,
                    "pnl": 0
                }

            daily[date]["trades"] += 1
            if record.net_pnl > 0:
                daily[date]["winning"] += 1
            else:
                daily[date]["losing"] += 1
            daily[date]["pnl"] += record.net_pnl

        return daily

    def get_symbol_stats(self) -> Dict:
        """按交易对统计

        Returns:
            交易对统计字典
        """
        by_symbol = {}

        for record in self._records:
            sym = record.symbol
            if sym not in by_symbol:
                by_symbol[sym] = {
                    "trades": 0,
                    "winning": 0,
                    "losing": 0,
                    "pnl": 0
                }

            by_symbol[sym]["trades"] += 1
            if record.net_pnl > 0:
                by_symbol[sym]["winning"] += 1
            else:
                by_symbol[sym]["losing"] += 1
            by_symbol[sym]["pnl"] += record.net_pnl

        return by_symbol

    def print_summary(self):
        """打印统计摘要"""
        stats = self.get_stats()

        print("\n" + "=" * 50)
        print("测试网交易统计摘要")
        print("=" * 50)
        print(f"总交易次数:   {stats['total_trades']}")
        print(f"盈利次数:     {stats['winning']}")
        print(f"亏损次数:     {stats['losing']}")
        print(f"胜率:         {stats['win_rate']:.2f}%")
        print("-" * 50)
        print(f"总盈亏:       {stats['net_pnl']:.4f} USDT")
        print(f"总手续费:     {stats['total_fees']:.4f} USDT")
        print(f"平均盈亏:     {stats['avg_pnl']:.4f} USDT")
        print(f"最大盈利:     {stats['max_win']:.4f} USDT")
        print(f"最大亏损:     {stats['max_loss']:.4f} USDT")
        print(f"平均盈利:     {stats['avg_win']:.4f} USDT")
        print(f"平均亏损:     {stats['avg_loss']:.4f} USDT")
        print(f"盈亏比:       {stats['profit_factor']:.2f}")
        print("=" * 50 + "\n")

    # ==================== 清理方法 ====================

    def clear_old_records(self, keep_days: int = 30):
        """清理旧记录

        Args:
            keep_days: 保留天数
        """
        cutoff_time = time.time() - (keep_days * 86400)
        self._records = [r for r in self._records if r.signal_time > cutoff_time]
        self._records_by_id = {r.trade_id: r for r in self._records}

    def clear_all(self):
        """清空所有记录"""
        self._records.clear()
        self._records_by_id.clear()
        self._stats = {
            "total_trades": 0,
            "winning": 0,
            "losing": 0,
            "total_pnl": 0,
            "total_fees": 0
        }

    # ==================== 批量导入 ====================

    @classmethod
    def load_from_directory(cls, log_dir: str) -> "TradeLogger":
        """从目录加载交易记录

        Args:
            log_dir: 日志目录

        Returns:
            TradeLogger实例
        """
        logger = cls(log_dir=log_dir, auto_save=False)

        log_path = Path(log_dir)
        for json_file in log_path.glob("trade_*.json"):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # 这里可以转换为TradeRecord，但需要根据实际格式调整
                    logger._stats["total_trades"] += 1
            except Exception as e:
                print(f"加载文件失败 {json_file.name}: {e}")

        return logger
