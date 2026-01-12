"""
Signal Recorder Module.

记录插针信号到文件，用于后续分析和验证。
"""

import json
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
from uuid import uuid4

import structlog

logger = structlog.get_logger(__name__)


# ============== 配置 ==============

RECORDER_CONFIG = {
    "data_dir": "data",
    "signal_file_prefix": "pin_signals_",
    "auto_save": True,
    "flush_interval": 5,  # 每5秒刷新一次
}


# ============== 数据结构 ==============

@dataclass
class PinSignalRecord:
    """插针信号记录，包含多时间段追踪数据"""

    # 基础信息
    id: str
    symbol: str
    direction: str  # UP/DOWN
    detected_at: datetime

    # 价格信息
    start_price: float
    peak_price: float
    peak_time: datetime
    current_price: float

    # 幅度信息
    amplitude_percent: float
    retracement_percent: float
    duration_ms: int

    # 多时间段价格追踪（信号前）
    price_before_30s: float = None
    price_before_60s: float = None
    price_before_90s: float = None
    price_before_180s: float = None

    # 多时间段价格追踪（信号后）
    price_after_30s: float = None
    price_after_60s: float = None
    price_after_90s: float = None
    price_after_180s: float = None

    # 最佳入场价（回调最深点）
    best_entry_price: float = None
    best_entry_time: datetime = None

    # 多时间段盈利结果（持仓30s/60s/90s/180s）
    profit_30s_usd: float = None
    profit_60s_usd: float = None
    profit_90s_usd: float = None
    profit_180s_usd: float = None

    # 多时间段盈亏%
    profit_30s_percent: float = None
    profit_60s_percent: float = None
    profit_90s_percent: float = None
    profit_180s_percent: float = None

    # 价格历史（用于详细分析）
    price_history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，处理datetime序列化"""
        d = asdict(self)
        # 转换datetime为ISO格式字符串
        for key, value in d.items():
            if isinstance(value, datetime):
                d[key] = value.isoformat()
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                # 处理price_history中的datetime
                for item in value:
                    for k, v in item.items():
                        if isinstance(v, datetime):
                            item[k] = v.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PinSignalRecord':
        """从字典恢复对象"""
        # 转换ISO格式字符串回datetime
        for key in ['detected_at', 'peak_time', 'best_entry_time']:
            if data.get(key) and isinstance(data[key], str):
                data[key] = datetime.fromisoformat(data[key])

        # 处理price_history中的datetime
        if data.get('price_history'):
            for item in data['price_history']:
                for key in ['time']:
                    if item.get(key) and isinstance(item[key], str):
                        item[key] = datetime.fromisoformat(item[key])

        return cls(**data)


@dataclass
class PricePoint:
    """价格采样点"""
    time: datetime
    price: float
    volume: float = 0.0


# ============== 信号记录器 ==============

class SignalRecorder:
    """
    信号记录器

    功能：
    1. 接收插针信号
    2. 记录信号到JSON文件
    3. 支持增量追加
    4. 自动刷新缓冲区
    """

    def __init__(self, config: Optional[Dict] = None):
        """初始化记录器

        Args:
            config: 配置字典，默认使用RECORDER_CONFIG
        """
        self.config = {**RECORDER_CONFIG, **(config or {})}

        # 确保数据目录存在
        self.data_dir = self.config["data_dir"]
        os.makedirs(self.data_dir, exist_ok=True)

        # 当前日期的文件路径
        self._current_file = None
        self._lock = threading.Lock()

        # 内存缓冲区
        self._pending_records: List[PinSignalRecord] = []

        self.logger = logger.bind(component="SignalRecorder")

    def _get_file_path(self) -> str:
        """获取当前日期的文件路径"""
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        filename = f"{self.config['signal_file_prefix']}{today}.json"
        return os.path.join(self.data_dir, filename)

    def record_spike(
        self,
        symbol: str,
        direction: str,
        start_price: float,
        peak_price: float,
        current_price: float,
        amplitude_percent: float,
        retracement_percent: float,
        duration_ms: int,
        detected_at: datetime = None,
        peak_time: datetime = None,
    ) -> PinSignalRecord:
        """记录插针信号

        Args:
            symbol: 交易对
            direction: 方向 UP/DOWN
            start_price: 起始价格
            peak_price: 顶点价格
            current_price: 当前价格
            amplitude_percent: 幅度百分比
            retracement_percent: 回撤百分比
            duration_ms: 持续时间毫秒
            detected_at: 检测时间
            peak_time: 顶点时间

        Returns:
            PinSignalRecord: 创建的信号记录
        """
        if detected_at is None:
            detected_at = datetime.now(timezone.utc)
        if peak_time is None:
            peak_time = detected_at

        record = PinSignalRecord(
            id=str(uuid4()),
            symbol=symbol,
            direction=direction,
            detected_at=detected_at,
            start_price=start_price,
            peak_price=peak_price,
            peak_time=peak_time,
            current_price=current_price,
            amplitude_percent=amplitude_percent,
            retracement_percent=retracement_percent,
            duration_ms=duration_ms,
        )

        with self._lock:
            self._pending_records.append(record)
            if self.config["auto_save"]:
                self._flush()

        self.logger.info(
            "Signal recorded",
            symbol=symbol,
            direction=direction,
            amplitude=f"{amplitude_percent:.2f}%",
            record_id=record.id[:8]
        )

        return record

    def update_price_after(
        self,
        record_id: str,
        period_seconds: int,
        price: float
    ) -> bool:
        """更新信号后的价格数据

        Args:
            record_id: 信号ID
            period_seconds: 时间段（30/60/90/180）
            price: 价格

        Returns:
            是否更新成功
        """
        with self._lock:
            for record in self._pending_records:
                if record.id == record_id:
                    attr_name = f"price_after_{period_seconds}s"
                    if hasattr(record, attr_name):
                        setattr(record, attr_name, price)
                        return True
            return False

    def update_profit(
        self,
        record_id: str,
        period_seconds: int,
        profit_usd: float,
        profit_percent: float
    ) -> bool:
        """更新盈利数据

        Args:
            record_id: 信号ID
            period_seconds: 时间段（30/60/90/180）
            profit_usd: 盈利USD
            profit_percent: 盈利百分比

        Returns:
            是否更新成功
        """
        with self._lock:
            for record in self._pending_records:
                if record.id == record_id:
                    setattr(record, f"profit_{period_seconds}s_usd", profit_usd)
                    setattr(record, f"profit_{period_seconds}s_percent", profit_percent)
                    return True
            return False

    def finalize_record(self, record_id: str) -> bool:
        """完成记录，写入文件

        Args:
            record_id: 信号ID

        Returns:
            是否成功
        """
        with self._lock:
            # 查找记录
            record = None
            for i, r in enumerate(self._pending_records):
                if r.id == record_id:
                    record = self._pending_records.pop(i)
                    break

            if record is None:
                return False

            # 写入文件
            return self._append_to_file(record)

    def _append_to_file(self, record: PinSignalRecord) -> bool:
        """追加记录到文件"""
        try:
            file_path = self._get_file_path()

            # 每行一个JSON对象
            with open(file_path, 'a', encoding='utf-8') as f:
                json.dump(record.to_dict(), f, ensure_ascii=False)
                f.write('\n')

            self.logger.debug(
                "Record saved to file",
                file_path=file_path,
                record_id=record.id[:8]
            )
            return True

        except Exception as e:
            self.logger.error(
                "Failed to save record",
                error=str(e),
                record_id=record.id[:8]
            )
            return False

    def _flush(self) -> None:
        """刷新所有待处理记录到文件"""
        if not self._pending_records:
            return

        file_path = self._get_file_path()

        try:
            with open(file_path, 'a', encoding='utf-8') as f:
                for record in self._pending_records:
                    json.dump(record.to_dict(), f, ensure_ascii=False)
                    f.write('\n')

            count = len(self._pending_records)
            self._pending_records.clear()

            self.logger.debug(f"Flushed {count} records to {file_path}")

        except Exception as e:
            self.logger.error("Failed to flush records", error=str(e))

    def load_records(self, date: str = None) -> List[PinSignalRecord]:
        """加载指定日期的记录

        Args:
            date: 日期字符串 YYYYMMDD，默认今天

        Returns:
            信号记录列表
        """
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y%m%d")

        file_path = os.path.join(
            self.data_dir,
            f"{self.config['signal_file_prefix']}{date}.json"
        )

        records = []

        if not os.path.exists(file_path):
            self.logger.warning(f"No data file for {date}")
            return records

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        data = json.loads(line)
                        records.append(PinSignalRecord.from_dict(data))

            self.logger.info(f"Loaded {len(records)} records from {date}")

        except Exception as e:
            self.logger.error("Failed to load records", error=str(e))

        return records

    def get_all_records(self) -> List[PinSignalRecord]:
        """获取所有可用日期的记录"""
        all_records = []

        if not os.path.exists(self.data_dir):
            return all_records

        for filename in os.listdir(self.data_dir):
            if filename.startswith(self.config['signal_file_prefix']) and filename.endswith('.json'):
                date = filename[len(self.config['signal_file_prefix']):][:8]
                all_records.extend(self.load_records(date))

        # 按检测时间排序
        all_records.sort(key=lambda r: r.detected_at)

        return all_records

    def cleanup_old_files(self, keep_days: int = 7) -> int:
        """清理旧数据文件

        Args:
            keep_days: 保留最近多少天的数据

        Returns:
            删除的文件数
        """
        if not os.path.exists(self.data_dir):
            return 0

        cutoff_date = datetime.now(timezone.utc) - timedelta(days=keep_days)
        deleted = 0

        for filename in os.listdir(self.data_dir):
            if filename.startswith(self.config['signal_file_prefix']) and filename.endswith('.json'):
                file_path = os.path.join(self.data_dir, filename)
                file_time = datetime.fromtimestamp(os.path.getmtime(file_path), tz=timezone.utc)

                if file_time < cutoff_date:
                    os.remove(file_path)
                    deleted += 1
                    self.logger.info(f"Deleted old file: {filename}")

        return deleted

    def close(self) -> None:
        """关闭记录器，刷新所有数据"""
        with self._lock:
            self._flush()


# ============== 便捷函数 ==============

def create_recorder(config: Optional[Dict] = None) -> SignalRecorder:
    """创建信号记录器"""
    return SignalRecorder(config)
