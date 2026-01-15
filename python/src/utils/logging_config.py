"""
统一日志配置系统

功能:
- 所有模块通过 get_logger(__name__) 获取logger
- 自动添加模块名和correlation_id
- 支持多种handler（控制台、文件、JSON）
- 支持日志上下文管理
- 结构化事件日志

Usage:
    from src.utils.logging_config import get_logger, setup_logging

    # 初始化日志系统
    setup_logging()

    # 获取logger
    logger = get_logger(__name__)
    logger.info("普通日志")

    # 带correlation_id的日志
    logger.with_correlation_id("sig_123").info("信号处理中")

    # 结构化事件日志
    logger.event("order_placed", "订单已提交", symbol="BTCUSDT", qty=0.1)
"""

import json
import logging
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


class StructuredFormatter(logging.Formatter):
    """结构化日志格式化器 - 输出JSON格式"""

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录为JSON"""
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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
    """日志上下文适配器 - 自动添加correlation_id和额外数据

    扩展标准Logger以支持:
    - correlation_id关联日志
    - 结构化数据附加
    - 事件类型日志
    """

    def __init__(self, logger: logging.Logger, extra: Dict[str, Any] = None):
        super().__init__(logger, extra or {})
        self._extra = extra.copy() if extra else {}

    def process(self, msg: Any, kwargs: Dict[str, Any]) -> tuple:
        """处理日志消息和kwargs"""
        # 将extra数据传递给LogRecord
        if "extra" not in kwargs:
            kwargs["extra"] = {}

        # 合并当前上下文的extra数据
        for key, value in self._extra.items():
            if key not in kwargs["extra"]:
                kwargs["extra"][key] = value

        return msg, kwargs

    def with_correlation_id(self, correlation_id: str) -> "ContextAdapter":
        """创建新的带correlation_id的logger

        Args:
            correlation_id: 关联ID，用于追踪同一业务流程的日志

        Returns:
            新的ContextAdapter实例
        """
        new_extra = self._extra.copy()
        new_extra["correlation_id"] = correlation_id
        return ContextAdapter(self.logger, new_extra)

    def with_data(self, **data) -> "ContextAdapter":
        """添加结构化数据到日志

        Args:
            **data: 要附加的数据键值对

        Returns:
            新的ContextAdapter实例
        """
        new_extra = self._extra.copy()
        if "extra_data" not in new_extra:
            new_extra["extra_data"] = {}
        new_extra["extra_data"].update(data)
        return ContextAdapter(self.logger, new_extra)

    def event(self, event_name: str, msg: str, **data) -> None:
        """记录事件日志（带事件类型的结构化日志）

        Args:
            event_name: 事件名称 (如: signal_detected, order_placed)
            msg: 日志消息
            **data: 事件相关数据
        """
        adapter = self.with_data(event=event_name, **data)
        adapter.info(msg)

    def debug(self, msg: Any, *args, **kwargs):
        self._log(logging.DEBUG, msg, args, **kwargs)

    def info(self, msg: Any, *args, **kwargs):
        self._log(logging.INFO, msg, args, **kwargs)

    def warning(self, msg: Any, *args, **kwargs):
        self._log(logging.WARNING, msg, args, **kwargs)

    def error(self, msg: Any, *args, **kwargs):
        self._log(logging.ERROR, msg, args, **kwargs)

    def critical(self, msg: Any, *args, **kwargs):
        self._log(logging.CRITICAL, msg, args, **kwargs)

    def exception(self, msg: Any, *args, **kwargs):
        kwargs["exc_info"] = True
        self._log(logging.ERROR, msg, args, **kwargs)

    def _log(self, level: int, msg: Any, args: tuple, **kwargs):
        """内部日志方法"""
        if self.isEnabledFor(level):
            self.logger._log(level, msg, args, **self.process(msg, kwargs))


# 全局logger缓存
_loggers: Dict[str, ContextAdapter] = {}


def get_logger(name: str) -> ContextAdapter:
    """获取结构化logger

    Args:
        name: 通常是 __name__

    Returns:
        ContextAdapter实例

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


def generate_correlation_id() -> str:
    """生成新的correlation_id

    Returns:
        格式为 "sig_<timestamp>_<random>" 的ID
    """
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    random_part = uuid.uuid4().hex[:6]
    return f"sig_{timestamp}_{random_part}"


def setup_logging(
    log_dir: str = "logs",
    console_level: str = "INFO",
    file_level: str = "DEBUG",
    enable_json: bool = True
) -> None:
    """配置日志系统

    Args:
        log_dir: 日志目录
        console_level: 控制台日志级别 (DEBUG/INFO/WARNING/ERROR)
        file_level: 文件日志级别 (DEBUG/INFO/WARNING/ERROR)
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

    # 控制台handler (人类可读格式)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, console_level.upper(), logging.INFO))
    console_formatter = logging.Formatter(
        '[%(asctime)s.%(msecs)03d] [%(levelname)-8s] [%(name)s] %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # 主文件handler
    main_file = log_path / f"bot_{today}.log"
    file_handler = logging.FileHandler(main_file, encoding='utf-8')
    file_handler.setLevel(getattr(logging, file_level.upper(), logging.DEBUG))
    file_handler.setFormatter(console_formatter)
    root_logger.addHandler(file_handler)

    if enable_json:
        # JSON结构化日志 - 所有事件
        events_file = log_path / f"events_{today}.jsonl"
        events_handler = logging.FileHandler(events_file, encoding='utf-8')
        events_handler.setLevel(logging.INFO)
        events_handler.setFormatter(StructuredFormatter())
        root_logger.addHandler(events_handler)

        # JSON结构化日志 - 仅信号事件
        signals_file = log_path / f"signals_{today}.jsonl"
        signals_handler = logging.FileHandler(signals_file, encoding='utf-8')
        signals_handler.setLevel(logging.INFO)
        signals_handler.setFormatter(StructuredFormatter())

        def _signal_filter(record: logging.LogRecord) -> bool:
            event = getattr(record, "event", "")
            extra_data = getattr(record, "extra_data", {})
            return event.startswith("signal_") or extra_data.get("event", "").startswith("signal_")

        signals_handler.addFilter(_signal_filter)
        root_logger.addHandler(signals_handler)

        # JSON结构化日志 - 仅订单事件
        orders_file = log_path / f"orders_{today}.jsonl"
        orders_handler = logging.FileHandler(orders_file, encoding='utf-8')
        orders_handler.setLevel(logging.INFO)
        orders_handler.setFormatter(StructuredFormatter())

        def _order_filter(record: logging.LogRecord) -> bool:
            event = getattr(record, "event", "")
            extra_data = getattr(record, "extra_data", {})
            return event.startswith("order_") or extra_data.get("event", "").startswith("order_")
        orders_handler.addFilter(_order_filter)
        root_logger.addHandler(orders_handler)

    # 错误文件handler
    errors_file = log_path / f"errors_{today}.log"
    error_handler = logging.FileHandler(errors_file, encoding='utf-8')
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(console_formatter)
    root_logger.addHandler(error_handler)

    # 设置基本库的日志级别
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)


@contextmanager
def log_context(logger: ContextAdapter, **context):
    """日志上下文管理器 - 临时添加上下文数据

    Args:
        logger: ContextAdapter实例
        **context: 要添加的上下文数据

    Usage:
        with log_context(logger, request_id="123", user="admin"):
            logger.info("此日志包含request_id和user")
    """
    original_extra = logger._extra.copy()
    try:
        for key, value in context.items():
            logger._extra[key] = value
        yield logger
    finally:
        logger._extra.clear()
        logger._extra.update(original_extra)


class EventLogger:
    """事件日志记录器 - 提供类型安全的日志方法

    预定义所有事件类型和对应的日志方法
    """

    def __init__(self, module_logger: ContextAdapter):
        """初始化

        Args:
            module_logger: 模块的ContextAdapter
        """
        self.logger = module_logger

    # ==================== 信号事件 ====================

    def log_signal_detected(self, symbol: str, direction: str, price: float,
                           atr: float = 0, velocity: float = 0,
                           confidence: float = 0, **extra) -> None:
        """记录信号检测"""
        self.logger.event(
            "signal_detected",
            f"Signal detected: {symbol} {direction}",
            symbol=symbol,
            direction=direction,
            price=price,
            atr=atr,
            velocity=velocity,
            confidence=confidence,
            **extra
        )

    def log_signal_filtered(self, symbol: str, reason: str, **extra) -> None:
        """记录信号被过滤"""
        self.logger.event(
            "signal_filtered",
            f"Signal filtered: {symbol} - {reason}",
            symbol=symbol,
            reason=reason,
            **extra
        )

    # ==================== 订单事件 ====================

    def log_order_submitting(self, symbol: str, side: str, qty: float,
                             order_type: str = "MARKET", **extra) -> None:
        """记录订单提交前"""
        self.logger.event(
            "order_submitting",
            f"Submitting order: {symbol} {side} {qty}",
            symbol=symbol,
            side=side,
            quantity=qty,
            order_type=order_type,
            **extra
        )

    def log_order_submitted(self, symbol: str, order_id: str, side: str,
                           qty: float, **extra) -> None:
        """记录订单已提交"""
        self.logger.event(
            "order_submitted",
            f"Order submitted: {symbol} {order_id}",
            symbol=symbol,
            order_id=order_id,
            side=side,
            quantity=qty,
            **extra
        )

    def log_order_filled(self, symbol: str, order_id: str, avg_price: float,
                        filled_qty: float, **extra) -> None:
        """记录订单成交"""
        self.logger.event(
            "order_filled",
            f"Order filled: {symbol} {order_id} @ {avg_price}",
            symbol=symbol,
            order_id=order_id,
            avg_price=avg_price,
            filled_qty=filled_qty,
            **extra
        )

    def log_order_failed(self, symbol: str, reason: str, **extra) -> None:
        """记录订单失败"""
        self.logger.with_data(**extra).event(
            "order_failed",
            f"Order failed: {symbol} - {reason}",
            symbol=symbol,
            reason=reason
        )

    def log_order_rejected(self, symbol: str, reason: str, **extra) -> None:
        """记录订单被拒绝"""
        self.logger.with_data(**extra).event(
            "order_rejected",
            f"Order rejected: {symbol} - {reason}",
            symbol=symbol,
            reason=reason
        )

    # ==================== 持仓事件 ====================

    def log_position_opened(self, symbol: str, side: str, entry_price: float,
                            qty: float, correlation_id: str, **extra) -> None:
        """记录持仓开启"""
        self.logger.with_correlation_id(correlation_id).event(
            "position_opened",
            f"Position opened: {symbol} {side} @ {entry_price}",
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=qty,
            **extra
        )

    def log_position_closed(self, symbol: str, entry_price: float, exit_price: float,
                           pnl: float, reason: str, **extra) -> None:
        """记录持仓关闭"""
        self.logger.event(
            "position_closed",
            f"Position closed: {symbol} PnL={pnl:+.4f} ({reason})",
            symbol=symbol,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=pnl,
            reason=reason,
            **extra
        )

    def log_hedge_opened(self, symbol: str, first_side: str, second_side: str,
                        first_entry: float, second_entry: float, **extra) -> None:
        """记录对冲开启"""
        self.logger.event(
            "hedge_opened",
            f"Hedge opened: {symbol} {first_side}+{second_side}",
            symbol=symbol,
            first_side=first_side,
            second_side=second_side,
            first_entry=first_entry,
            second_entry=second_entry,
            **extra
        )

    def log_hedge_closed(self, symbol: str, total_pnl: float, **extra) -> None:
        """记录对冲关闭"""
        self.logger.event(
            "hedge_closed",
            f"Hedge closed: {symbol} PnL={total_pnl:+.4f}",
            symbol=symbol,
            total_pnl=total_pnl,
            **extra
        )

    # ==================== API事件 ====================

    def log_api_request(self, method: str, endpoint: str, **params) -> None:
        """记录API请求"""
        self.logger.event(
            "api_request",
            f"API {method} {endpoint}",
            method=method,
            endpoint=endpoint,
            **params
        )

    def log_api_response(self, method: str, endpoint: str, duration_ms: float,
                        status_code: int = None, **extra) -> None:
        """记录API响应"""
        self.logger.event(
            "api_response",
            f"API Response: {method} {endpoint} ({duration_ms:.0f}ms)",
            method=method,
            endpoint=endpoint,
            duration_ms=duration_ms,
            status_code=status_code,
            **extra
        )

    def log_api_error(self, method: str, endpoint: str, error: str, **extra) -> None:
        """记录API错误"""
        self.logger.with_data(**extra).event(
            "api_error",
            f"API Error: {method} {endpoint} - {error}",
            method=method,
            endpoint=endpoint,
            error=error
        )

    # ==================== 系统事件 ====================

    def log_websocket_connected(self, url: str, **extra) -> None:
        """记录WebSocket连接"""
        self.logger.event(
            "websocket_connected",
            f"WebSocket connected: {url}",
            url=url,
            **extra
        )

    def log_websocket_disconnected(self, reason: str = "", **extra) -> None:
        """记录WebSocket断开"""
        self.logger.with_data(**extra).event(
            "websocket_disconnected",
            f"WebSocket disconnected: {reason}" if reason else "WebSocket disconnected",
            reason=reason
        )

    # ==================== 错误事件 ====================

    def log_error(self, error_type: str, message: str, **context) -> None:
        """记录错误"""
        self.logger.with_data(**context).event(
            f"error_{error_type}",
            message,
            error_type=error_type
        )
