# 成熟日志系统集成计划

## 当前问题分析

### 1. 日志系统混乱现状

| 模块 | 当前日志方式 | 问题 |
|------|-------------|------|
| `atr_detector.py` | `structlog.get_logger()` | 与主程序日志系统不兼容 |
| `simple_hedge.py` | 标准 `logging` + `_flush_log` 包装 | 自定义函数增加复杂度 |
| `binance_futures.py` | `_get_logger()` 动态导入 | 导入失败时无日志 |
| `hedge_manager.py` | `structlog` | 格式不一致 |
| `kline_tracker.py` | 无日志 | 关键数据变化不可见 |
| `testnet_mtf_trading.py` | `BotLogger` | 主日志系统 |

### 2. 日志内容缺失

```
缺失的关键日志点：
├── 信号检测流程
│   ├── ATR计算状态 ❌
│   ├── 速度检测详情 ❌
│   ├── K线形态判断 ❌
│   └── 信号生成原因 ⚠️ (不够详细)
│
├── 订单生命周期
│   ├── 订单提交前状态 ✅
│   ├── 订单API响应详情 ❌
│   ├── 订单成交确认轮询 ⚠️ (部分)
│   ├── 订单失败原因详情 ❌
│   └── 订单取消/修改 ❌
│
├── 持仓管理
│   ├── 持仓状态变化 ⚠️
│   ├── 浮动盈亏计算 ❌
│   ├── 止损止盈触发 ❌
│   └── 强平风险评估 ❌
│
├── 性能指标
│   ├── API延迟统计 ⚠️
│   ├── 信号处理延迟 ❌
│   ├── 内存使用 ❌
│   └── WebSocket连接状态 ⚠️
│
└── 错误追踪
    ├── 异常堆栈 ⚠️ (部分)
    ├── 错误上下文 ❌
    ├── 重试逻辑状态 ❌
    └── 降级策略触发 ❌
```

---

## 设计目标

### 1. 统一日志接口

所有模块使用统一的日志获取方式：

```python
from src.utils.logging_config import get_logger
logger = get_logger(__name__)
```

### 2. 结构化日志

每条日志包含标准字段：

```python
{
    "timestamp": "2026-01-15T10:30:45.123Z",
    "level": "INFO",
    "module": "simple_hedge",
    "event": "signal_received",
    "correlation_id": "sig_123456",
    "data": {
        "symbol": "BTCUSDT",
        "direction": "UP",
        "entry_price": 95000.0,
        "atr": 1500.0,
        "velocity": 0.008
    }
}
```

### 3. 分离日志文件

```
logs/
├── bot_YYYYMMDD.log          # 主日志（控制台+文件）
├── signals_YYYYMMDD.jsonl    # 信号事件（结构化）
├── orders_YYYYMMDD.jsonl     # 订单事件（结构化）
├── errors_YYYYMMDD.log       # 错误日志
├── performance_YYYYMMDD.log   # 性能指标
└── api_YYYYMMDD.jsonl        # API调用追踪
```

---

## 实施计划

### 第一阶段：核心日志基础设施

#### 1.1 创建统一日志配置模块

**文件**: `src/utils/logging_config.py`

```python
"""
统一日志配置系统

功能:
- 所有模块通过 get_logger(__name__) 获取logger
- 自动添加模块名和correlation_id
- 支持多种handler（控制台、文件、JSON）
- 支持日志上下文管理
"""

import logging
import sys
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional
import json
import uuid
from contextlib import contextmanager

class StructuredFormatter(logging.Formatter):
    """结构化日志格式化器 - 输出JSON"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "module": record.name,
            "event": getattr(record, "event", "generic"),
            "correlation_id": getattr(record, "correlation_id", ""),
            "message": record.getMessage(),
        }

        # 添加额外字段
        if hasattr(record, "extra_data"):
            log_entry["data"] = record.extra_data

        return json.dumps(log_entry, ensure_ascii=False)

class ContextAdapter(logging.LoggerAdapter):
    """日志上下文适配器 - 自动添加correlation_id和额外数据"""

    def __init__(self, logger: logging.Logger, extra: Dict[str, Any] = None):
        super().__init__(logger, extra or {})
        self.correlation_id = extra.get("correlation_id") if extra else None

    def process(self, msg, kwargs):
        # 将extra_data传递给LogRecord
        if "extra" not in kwargs:
            kwargs["extra"] = {}
        kwargs["extra"].update(self.extra)
        return msg, kwargs

    def with_correlation_id(self, correlation_id: str) -> "ContextAdapter":
        """创建新的带correlation_id的logger"""
        new_extra = self.extra.copy()
        new_extra["correlation_id"] = correlation_id
        return ContextAdapter(self.logger, new_extra)

    def with_data(self, **data) -> "ContextAdapter":
        """添加结构化数据"""
        new_extra = self.extra.copy()
        if "extra_data" not in new_extra:
            new_extra["extra_data"] = {}
        new_extra["extra_data"].update(data)
        return ContextAdapter(self.logger, new_extra)

    def event(self, event_name: str, msg: str, **data):
        """记录事件日志"""
        return self.with_data(event=event_name, **data).info(msg)

# 全局logger缓存
_loggers: Dict[str, ContextAdapter] = {}

def get_logger(name: str) -> ContextAdapter:
    """获取结构化logger

    Usage:
        logger = get_logger(__name__)
        logger.info("Message")
        logger.with_correlation_id("abc123").info("Correlated message")
        logger.event("order_placed", "Order submitted", symbol="BTCUSDT", qty=0.1)
    """
    if name not in _loggers:
        base_logger = logging.getLogger(name)
        _loggers[name] = ContextAdapter(base_logger)
    return _loggers[name]

def setup_logging(
    log_dir: str = "logs",
    console_level: str = "INFO",
    file_level: str = "DEBUG",
    enable_json: bool = True
) -> None:
    """配置日志系统

    Args:
        log_dir: 日志目录
        console_level: 控制台日志级别
        file_level: 文件日志级别
        enable_json: 是否启用JSON结构化日志
    """
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)

    today = datetime.now().strftime("%Y%m%d")

    # 根logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # 清除已有handlers
    root_logger.handlers.clear()

    # 控制台handler (人类可读)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, console_level))
    console_formatter = logging.Formatter(
        '[%(asctime)s.%(msecs)03d] [%(levelname)-8s] [%(name)s] %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # 主文件handler
    main_file = log_path / f"bot_{today}.log"
    file_handler = logging.FileHandler(main_file, encoding='utf-8')
    file_handler.setLevel(getattr(logging, file_level))
    file_handler.setFormatter(console_formatter)
    root_logger.addHandler(file_handler)

    if enable_json:
        # JSON结构化日志 - 信号事件
        signals_file = log_path / f"signals_{today}.jsonl"
        signals_handler = logging.FileHandler(signals_file, encoding='utf-8')
        signals_handler.setLevel(logging.INFO)
        signals_handler.setFormatter(StructuredFormatter())
        signals_handler.addFilter(lambda r: hasattr(r, 'extra_data') and r.extra_data.get('event', '').startswith('signal_'))
        root_logger.addHandler(signals_handler)

        # JSON结构化日志 - 订单事件
        orders_file = log_path / f"orders_{today}.jsonl"
        orders_handler = logging.FileHandler(orders_file, encoding='utf-8')
        orders_handler.setLevel(logging.INFO)
        orders_handler.setFormatter(StructuredFormatter())
        orders_handler.addFilter(lambda r: hasattr(r, 'extra_data') and r.extra_data.get('event', '').startswith('order_'))
        root_logger.addHandler(orders_handler)

        # JSON结构化日志 - 所有事件
        events_file = log_path / f"events_{today}.jsonl"
        events_handler = logging.FileHandler(events_file, encoding='utf-8')
        events_handler.setLevel(logging.DEBUG)
        events_handler.setFormatter(StructuredFormatter())
        root_logger.addHandler(events_handler)

    # 错误文件handler
    errors_file = log_path / f"errors_{today}.log"
    error_handler = logging.FileHandler(errors_file, encoding='utf-8')
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(console_formatter)
    root_logger.addHandler(error_handler)

@contextmanager
def log_context(logger: ContextAdapter, **context):
    """日志上下文管理器 - 临时添加上下文数据"""
    original_extra = logger.extra.copy()
    try:
        for key, value in context.items():
            logger.extra[key] = value
        yield logger
    finally:
        logger.extra.clear()
        logger.extra.update(original_extra)
```

#### 1.2 事件类型定义

**文件**: `src/utils/logging_events.py`

```python
"""
日志事件类型定义

统一管理所有日志事件类型和字段
"""

from enum import Enum
from dataclasses import dataclass
from typing import Any, Dict, Optional
from datetime import datetime

class EventType(str, Enum):
    """日志事件类型"""

    # 信号检测事件
    SIGNAL_DETECTED = "signal_detected"
    SIGNAL_FILTERED = "signal_filtered"
    SIGNAL_COOLDOWN = "signal_cooldown"

    # 交易决策事件
    POSITION_SIZE_CALCULATED = "position_size_calculated"
    LEVERAGE_SET = "leverage_set"
    HEDGE_TARGET_SET = "hedge_target_set"

    # 订单事件
    ORDER_SUBMITTING = "order_submitting"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_REJECTED = "order_reJECTED"
    ORDER_FILLING = "order_filling"
    ORDER_FILLED = "order_filled"
    ORDER_FAILED = "order_failed"
    ORDER_TIMEOUT = "order_timeout"
    ORDER_CANCELLED = "order_cancelled"

    # 持仓事件
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    POSITION_MODIFIED = "position_modified"
    HEDGE_OPENED = "hedge_opened"
    HEDGE_CLOSED = "hedge_closed"

    # API事件
    API_REQUEST = "api_request"
    API_RESPONSE = "api_response"
    API_ERROR = "api_error"
    API_TIMEOUT = "api_timeout"

    # 系统事件
    WEBSOCKET_CONNECTED = "websocket_connected"
    WEBSOCKET_DISCONNECTED = "websocket_disconnected"
    WEBSOCKET_ERROR = "websocket_error"
    SHUTDOWN_INITIATED = "shutdown_initiated"

    # 性能事件
    LATENCY_HIGH = "latency_high"
    MEMORY_WARNING = "memory_warning"
    RATE_LIMIT_HIT = "rate_limit_hit"

class EventLogger:
    """事件日志记录器 - 提供类型安全的日志方法"""

    def __init__(self, module_logger):
        self.logger = module_logger

    def log_signal_detected(self, symbol: str, direction: str, price: float,
                           atr: float, velocity: float, confidence: float):
        """记录信号检测"""
        self.logger.event(
            EventType.SIGNAL_DETECTED,
            f"Signal detected: {symbol} {direction}",
            symbol=symbol,
            direction=direction,
            price=price,
            atr=atr,
            velocity=velocity,
            confidence=confidence
        )

    def log_order_submitting(self, symbol: str, side: str, qty: float, order_type: str):
        """记录订单提交前"""
        self.logger.event(
            EventType.ORDER_SUBMITTING,
            f"Submitting order: {symbol} {side} {qty}",
            symbol=symbol,
            side=side,
            quantity=qty,
            order_type=order_type
        )

    def log_order_submitted(self, symbol: str, order_id: str, side: str, qty: float):
        """记录订单已提交"""
        self.logger.event(
            EventType.ORDER_SUBMITTED,
            f"Order submitted: {symbol} {order_id}",
            symbol=symbol,
            order_id=order_id,
            side=side,
            quantity=qty
        )

    def log_order_filled(self, symbol: str, order_id: str, avg_price: float, filled_qty: float):
        """记录订单成交"""
        self.logger.event(
            EventType.ORDER_FILLED,
            f"Order filled: {symbol} {order_id} @ {avg_price}",
            symbol=symbol,
            order_id=order_id,
            avg_price=avg_price,
            filled_qty=filled_qty
        )

    def log_position_opened(self, symbol: str, side: str, entry_price: float,
                           qty: float, correlation_id: str):
        """记录持仓开启"""
        self.logger.with_correlation_id(correlation_id).event(
            EventType.POSITION_OPENED,
            f"Position opened: {symbol} {side} @ {entry_price}",
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=qty
        )
```

---

### 第二阶段：模块日志迁移

#### 2.1 迁移 `atr_detector.py`

**改动点**:
```python
# 旧代码
import structlog
logger = structlog.get_logger(__name__)

# 新代码
from src.utils.logging_config import get_logger, EventLogger
logger = get_logger(__name__)
events = EventLogger(logger)

# 使用
events.log_signal_detected(
    symbol=symbol,
    direction=direction.value,
    price=current_price,
    atr=atr,
    velocity=velocity,
    confidence=confidence
)
```

**新增日志点**:
1. ATR计算完成 → `atr_calculated`
2. 速度检测详情 → `velocity_checked`
3. K线形态判断 → `pattern_checked`
4. 信号过滤原因 → `signal_filtered`

#### 2.2 迁移 `simple_hedge.py`

**改动点**:
```python
from src.utils.logging_config import get_logger, EventLogger

class SimpleHedgeExecutor:
    def __init__(self, ..., external_logger=None):
        self.logger = external_logger or get_logger(__name__)
        self.events = EventLogger(self.logger)
```

**新增日志点**:
1. 信号接收 → `signal_received` (带correlation_id)
2. 仓位计算 → `position_size_calculated`
3. 杠杆设置 → `leverage_set`
4. 订单提交前 → `order_submitting`
5. 订单提交后 → `order_submitted`
6. 订单成交确认轮询 → `order_polling`
7. 订单成交 → `order_filled`
8. 持仓开启 → `position_opened`

#### 2.3 增强 `binance_futures.py`

**改动点**:
```python
from src.utils.logging_config import get_logger

def _get_logger():
    global _logger
    if _logger is None:
        _logger = get_logger(__name__)
    return _logger

class BinanceFuturesClient:
    def _make_request(self, ...):
        logger = get_logger(__name__)
        correlation_id = str(uuid.uuid4())

        logger.with_correlation_id(correlation_id).event(
            EventType.API_REQUEST,
            f"API {method} {endpoint}",
            method=method,
            endpoint=endpoint,
            params=params
        )

        start = time.time()
        response = self._session.request(...)

        logger.with_correlation_id(correlation_id).event(
            EventType.API_RESPONSE,
            f"API Response: {endpoint}",
            endpoint=endpoint,
            duration_ms=(time.time() - start) * 1000,
            status_code=response.status_code
        )
```

---

### 第三阶段：实时日志监控

#### 3.1 日志仪表板

创建 `src/utils/log_monitor.py`:

```python
"""
实时日志监控

提供:
1. 实时日志流式显示
2. 关键事件高亮
3. 错误统计
4. 性能指标展示
"""

import time
import threading
from pathlib import Path
from typing import Callable, Optional
import json

class LogMonitor:
    """日志监控器"""

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.running = False
        self.callbacks: Dict[str, Callable] = {}

    def on_event(self, event_type: str, callback: Callable):
        """注册事件回调"""
        self.callbacks[event_type] = callback

    def start(self):
        """开始监控日志文件"""
        self.running = True
        # 监控events_YYYYMMDD.jsonl文件
        # 解析JSON并调用回调

    def print_realtime_summary(self):
        """打印实时统计摘要"""
        while self.running:
            # 统计最近的事件
            # 打印信号数、订单数、错误数
            time.sleep(5)
```

---

### 第四阶段：日志分析工具

#### 4.1 日志查询工具

创建 `src/utils/log_analyzer.py`:

```python
"""
日志分析工具

功能:
1. 查询特定时间范围的日志
2. 按事件类型过滤
3. 按correlation_id追踪完整流程
4. 统计分析
"""

class LogAnalyzer:
    """日志分析器"""

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)

    def query_by_correlation_id(self, correlation_id: str) -> List[Dict]:
        """按correlation_id查询完整事件链"""
        pass

    def query_signal_to_trade(self, signal_id: str) -> Dict:
        """追踪信号到交易的完整流程"""
        pass

    def get_error_summary(self, date: str = None) -> Dict:
        """获取错误统计"""
        pass

    def get_performance_metrics(self, date: str = None) -> Dict:
        """获取性能指标"""
        pass
```

---

## 实施顺序

| 阶段 | 任务 | 文件 | 预估工作量 |
|------|------|------|-----------|
| 1.1 | 创建 `logging_config.py` | 新建 | 2小时 |
| 1.2 | 创建 `logging_events.py` | 新建 | 1小时 |
| 1.3 | 创建 `log_monitor.py` | 新建 | 1小时 |
| 2.1 | 迁移 `atr_detector.py` | 修改 | 1小时 |
| 2.2 | 迁移 `simple_hedge.py` | 修改 | 1.5小时 |
| 2.3 | 增强 `binance_futures.py` | 修改 | 1.5小时 |
| 2.4 | 迁移 `kline_tracker.py` | 修改 | 0.5小时 |
| 2.5 | 迁移 `hedge_manager.py` | 修改 | 1小时 |
| 2.6 | 更新 `testnet_mtf_trading.py` | 修改 | 0.5小时 |
| 3.1 | 创建日志监控CLI | 新建 | 1小时 |
| 4.1 | 创建日志分析工具 | 新建 | 2小时 |
| 4.2 | 编写日志分析脚本 | 新建 | 1小时 |

**总计**: 约16小时

---

## 日志输出示例

### 1. 信号检测日志

```json
{"timestamp": "2026-01-15T10:30:45.123Z", "level": "INFO", "module": "atr_detector", "event": "signal_detected", "correlation_id": "sig_abc123", "data": {"symbol": "BTCUSDT", "direction": "UP", "price": 95000.0, "atr": 1500.0, "velocity": 0.008, "confidence": 0.85}}
```

### 2. 订单提交日志

```json
{"timestamp": "2026-01-15T10:30:45.234Z", "level": "INFO", "module": "simple_hedge", "event": "order_submitting", "correlation_id": "sig_abc123", "data": {"symbol": "BTCUSDT", "side": "BUY", "quantity": 0.01, "order_type": "MARKET"}}
```

### 3. 订单成交日志

```json
{"timestamp": "2026-01-15T10:30:45.567Z", "level": "INFO", "module": "simple_hedge", "event": "order_filled", "correlation_id": "sig_abc123", "data": {"symbol": "BTCUSDT", "order_id": "123456789", "avg_price": 95001.5, "filled_qty": 0.01}}
```

### 4. 持仓开启日志

```json
{"timestamp": "2026-01-15T10:30:45.678Z", "level": "INFO", "module": "simple_hedge", "event": "position_opened", "correlation_id": "sig_abc123", "data": {"symbol": "BTCUSDT", "side": "LONG", "entry_price": 95001.5, "quantity": 0.01}}
```

---

## 验证检查清单

- [ ] 所有模块使用 `get_logger(__name__)`
- [ ] 所有关键事件有对应的 `EventType`
- [ ] 所有日志包含 `correlation_id`
- [ ] JSON日志文件正确生成
- [ ] 控制台日志可读
- [ ] 错误日志包含完整堆栈
- [ ] 性能日志包含延迟数据
- [ ] 可以按 `correlation_id` 追踪完整流程
- [ ] 日志文件按日期自动滚动
- [ ] 旧日志文件自动清理
