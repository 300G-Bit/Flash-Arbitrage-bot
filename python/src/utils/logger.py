"""
交易机器人日志系统

提供统一的日志记录功能:
- 控制台彩色输出
- 文件日志记录
- API请求/响应追踪
- 交易记录专用日志
"""

import os
import sys
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from contextlib import contextmanager


class ColoredFormatter(logging.Formatter):
    """彩色控制台格式化器"""

    # ANSI颜色代码
    COLORS = {
        'DEBUG': '\033[36m',      # 青色
        'INFO': '\033[37m',       # 白色
        'WARNING': '\033[33m',    # 黄色
        'ERROR': '\033[31m',      # 红色
        'CRITICAL': '\033[35m',   # 紫色
        'RESET': '\033[0m',       # 重置
        'GREEN': '\033[32m',      # 绿色
        'CYAN': '\033[36m',       # 青色
        'YELLOW': '\033[33m',     # 黄色
        'GRAY': '\033[90m',       # 灰色
    }

    # 图标
    ICONS = {
        'DEBUG': '🔍',
        'INFO': 'ℹ️',
        'WARNING': '⚠️',
        'ERROR': '❌',
        'CRITICAL': '🚨',
    }

    def __init__(self, use_colors: bool = True, use_icons: bool = True):
        """初始化格式化器

        Args:
            use_colors: 是否使用颜色
            use_icons: 是否使用图标
        """
        super().__init__()
        self.use_colors = use_colors
        self.use_icons = use_icons

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录"""
        levelname = record.levelname
        message = record.getMessage()

        # 获取颜色
        if self.use_colors:
            color = self.COLORS.get(levelname, self.COLORS['RESET'])
            reset = self.COLORS['RESET']
        else:
            color = ''
            reset = ''

        # 获取图标
        icon = self.ICONS.get(levelname, '') if self.use_icons else ''

        # 格式化时间
        timestamp = datetime.fromtimestamp(record.created).strftime('%H:%M:%S.%f')[:-3]

        # 格式化位置
        if record.levelname in ['DEBUG', 'ERROR']:
            location = f" [{record.name}:{record.funcName}:{record.lineno}]"
        else:
            location = ""

        # 构建最终消息
        if icon:
            result = f"{color}[{timestamp}] {icon} {message}{location}{reset}"
        else:
            result = f"{color}[{timestamp}] {levelname:7} {message}{location}{reset}"

        return result


class FileFormatter(logging.Formatter):
    """文件日志格式化器（无颜色）"""

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录"""
        timestamp = datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        levelname = record.levelname
        message = record.getMessage()

        # 添加详细信息
        details = []
        if hasattr(record, 'extra'):
            for key, value in record.extra.items():
                details.append(f"{key}={value}")

        if details:
            detail_str = " | " + " | ".join(details)
        else:
            detail_str = ""

        return f"[{timestamp}] {levelname:7} {message}{detail_str}"


class TradeLogger:
    """交易记录专用日志器

    记录所有交易相关的详细数据到JSON文件
    """

    def __init__(self, log_dir: str = "logs"):
        """初始化交易日志器

        Args:
            log_dir: 日志目录
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)

        # 创建子目录
        self.trades_dir = self.log_dir / "trades"
        self.trades_dir.mkdir(exist_ok=True)

        self.api_dir = self.log_dir / "api"
        self.api_dir.mkdir(exist_ok=True)

        # 当前会话ID
        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        # 交易记录缓存
        self._trades_buffer = []
        self._buffer_lock = threading.Lock()

    def log_trade(self, trade_type: str, data: Dict[str, Any]):
        """记录交易事件

        Args:
            trade_type: 交易类型 (signal_opened, hedge_opened, position_closed, etc.)
            data: 交易数据
        """
        with self._buffer_lock:
            record = {
                "session_id": self.session_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": trade_type,
                "data": data
            }
            self._trades_buffer.append(record)

            # 每累积10条记录就写入文件
            if len(self._trades_buffer) >= 10:
                self._flush_trades()

    def _flush_trades(self):
        """将缓存的交易记录写入文件"""
        if not self._trades_buffer:
            return

        filename = self.trades_dir / f"trades_{self.session_id}.jsonl"

        # 追加模式写入
        with open(filename, 'a', encoding='utf-8') as f:
            for record in self._trades_buffer:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')

        self._trades_buffer.clear()

    def flush(self):
        """刷新所有缓存的记录"""
        with self._buffer_lock:
            self._flush_trades()

    def log_api_request(self, method: str, endpoint: str, params: Dict = None,
                        response: Any = None, error: Any = None, duration_ms: float = 0):
        """记录API请求

        Args:
            method: HTTP方法
            endpoint: API端点
            params: 请求参数
            response: 响应数据
            error: 错误信息
            duration_ms: 请求耗时(毫秒)
        """
        # 过滤敏感参数
        safe_params = {}
        if params:
            for k, v in params.items():
                # 隐藏签名和密钥
                if 'signature' in k.lower() or 'secret' in k.lower():
                    safe_params[k] = "***REDACTED***"
                elif k == 'timestamp' and v:
                    # 只显示时间戳的部分，便于追踪
                    safe_params[k] = str(v)[-6:]
                else:
                    safe_params[k] = v

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "method": method,
            "endpoint": endpoint,
            "params": safe_params,
            "duration_ms": round(duration_ms, 2),
        }

        if response is not None:
            # 只记录关键响应信息
            if isinstance(response, dict):
                if response.get("error"):
                    record["response"] = {"error": response.get("msg", str(response))}
                elif "code" in response and response["code"] < 0:
                    record["response"] = {"code": response.get("code"), "msg": response.get("msg")}
                else:
                    # 成功响应，只记录关键字段
                    safe_response = {}
                    for key in ["orderId", "symbol", "side", "type", "status", "executedQty"]:
                        if key in response:
                            safe_response[key] = response[key]
                    record["response"] = safe_response
            else:
                record["response"] = str(response)[:200]

        if error is not None:
            record["error"] = str(error)[:500]

        # 写入API日志
        filename = self.api_dir / f"api_{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')


class BotLogger:
    """交易机器人主日志器

    提供统一的日志接口，同时输出到控制台和文件
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        name: str = "FlashArbitrageBot",
        log_dir: str = "logs",
        console_level: int = logging.INFO,
        file_level: int = logging.DEBUG
    ):
        """初始化日志器

        Args:
            name: 日志器名称
            log_dir: 日志目录
            console_level: 控制台日志级别
            file_level: 文件日志级别
        """
        # 避免重复初始化
        if hasattr(self, '_initialized'):
            return

        self.name = name
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)

        # 创建日志器
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers.clear()

        # 控制台处理器
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(console_level)
        console_handler.setFormatter(ColoredFormatter(use_colors=True, use_icons=True))
        self.logger.addHandler(console_handler)

        # 文件处理器（主日志）
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        main_file = self.log_dir / f"bot_{today}.log"
        file_handler = logging.FileHandler(main_file, encoding='utf-8')
        file_handler.setLevel(file_level)
        file_handler.setFormatter(FileFormatter())
        self.logger.addHandler(file_handler)

        # 错误日志单独文件
        error_file = self.log_dir / f"errors_{today}.log"
        error_handler = logging.FileHandler(error_file, encoding='utf-8')
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(FileFormatter())
        self.logger.addHandler(error_handler)

        # 交易日志器
        self.trade_logger = TradeLogger(log_dir)

        # API请求计时
        self._api_timings = {}

        self._initialized = True

    # ==================== 基础日志方法 ====================

    def debug(self, msg: str, **kwargs):
        """调试日志"""
        self._log_with_extra(logging.DEBUG, msg, **kwargs)

    def info(self, msg: str, **kwargs):
        """信息日志"""
        self._log_with_extra(logging.INFO, msg, **kwargs)

    def warning(self, msg: str, **kwargs):
        """警告日志"""
        self._log_with_extra(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, **kwargs):
        """错误日志"""
        self._log_with_extra(logging.ERROR, msg, **kwargs)

    def critical(self, msg: str, **kwargs):
        """严重错误日志"""
        self._log_with_extra(logging.CRITICAL, msg, **kwargs)

    def _log_with_extra(self, level: int, msg: str, **kwargs):
        """带额外信息的日志"""
        if kwargs:
            # 创建LogRecord时会添加extra字段
            old_factory = logging.getLogRecordFactory()

            def record_factory(*args, **factory_kwargs):
                record = old_factory(*args, **factory_kwargs)
                record.extra = kwargs
                return record

            logging.setLogRecordFactory(record_factory)
            self.logger.log(level, msg)
            logging.setLogRecordFactory(old_factory)
        else:
            self.logger.log(level, msg)

    # ==================== 专用日志方法 ====================

    def api_request(self, method: str, endpoint: str, params: Dict = None):
        """记录API请求开始"""
        self._api_timings[endpoint] = datetime.now(timezone.utc)
        self.debug(f"API请求: {method} {endpoint}", params=str(params)[:200] if params else "")

    def api_response(self, method: str, endpoint: str, response: Any = None, error: Any = None):
        """记录API响应"""
        duration = 0
        if endpoint in self._api_timings:
            duration = (datetime.now(timezone.utc) - self._api_timings[endpoint]).total_seconds() * 1000
            del self._api_timings[endpoint]

        if error:
            self.error(f"API错误: {method} {endpoint}", error=str(error)[:200], duration_ms=f"{duration:.1f}ms")
        elif response and isinstance(response, dict):
            if response.get("error") or response.get("code", 0) < 0:
                self.warning(f"API业务错误: {method} {endpoint}",
                           code=response.get("code"), msg=response.get("msg"),
                           duration_ms=f"{duration:.1f}ms")
            else:
                self.debug(f"API响应: {method} {endpoint}", duration_ms=f"{duration:.1f}ms")

        # 记录到交易日志
        self.trade_logger.log_api_request(method, endpoint, None, response, error, duration)

    def trade_signal(self, signal: Dict):
        """记录交易信号"""
        self.info(f"🔔 交易信号: {signal.get('symbol')} {signal.get('direction')}",
                 amplitude=signal.get('amplitude'), retracement=signal.get('retracement'))
        self.trade_logger.log_trade("signal", signal)

    def position_opened(self, symbol: str, side: str, price: float, quantity: float, order_id: str):
        """记录开仓"""
        self.info(f"📈 开仓: {symbol} {side} {quantity:.6f} @ {price:.6f}",
                 symbol=symbol, side=side, order_id=order_id)
        self.trade_logger.log_trade("position_opened", {
            "symbol": symbol,
            "side": side,
            "price": price,
            "quantity": quantity,
            "order_id": order_id
        })

    def position_closed(self, symbol: str, pnl: float, reason: str):
        """记录平仓"""
        icon = "📉" if pnl >= 0 else "💔"
        self.info(f"{icon} 平仓: {symbol} PnL: {pnl:+.4f} USDT ({reason})",
                 symbol=symbol, pnl=pnl, reason=reason)
        self.trade_logger.log_trade("position_closed", {
            "symbol": symbol,
            "pnl": pnl,
            "reason": reason
        })

    def hedge_completed(self, symbol: str, first_side: str, second_side: str,
                        first_entry: float, second_entry: float):
        """记录对冲完成"""
        self.info(f"🔒 对冲完成: {symbol}",
                 symbol=symbol, first_side=first_side, second_side=second_side,
                 first_entry=first_entry, second_entry=second_entry)
        self.trade_logger.log_trade("hedge_completed", {
            "symbol": symbol,
            "first_side": first_side,
            "second_side": second_side,
            "first_entry": first_entry,
            "second_entry": second_entry
        })

    def stop_loss_set(self, symbol: str, side: str, stop_price: float, order_id: str = None):
        """记录止损设置"""
        self.debug(f"🛡️ 止损已设: {symbol} {side} @ {stop_price:.6f}",
                  symbol=symbol, side=side, stop_price=stop_price, order_id=order_id)

    def take_profit_set(self, symbol: str, side: str, tp_price: float, order_id: str = None):
        """记录止盈设置"""
        self.debug(f"🎯 止盈已设: {symbol} {side} @ {tp_price:.6f}",
                  symbol=symbol, side=side, tp_price=tp_price, order_id=order_id)

    def order_verified(self, symbol: str, order_type: str, verified: bool):
        """记录订单验证结果"""
        if verified:
            self.debug(f"✅ 订单验证通过: {symbol} {order_type}")
        else:
            self.warning(f"⚠️ 订单验证失败: {symbol} {order_type}")

    # ==================== 会话管理 ====================

    def session_start(self, config: Dict = None):
        """记录会话开始"""
        self.info("="*70)
        self.info(f"🚀 {self.name} 会话开始")
        self.info(f"会话ID: {self.trade_logger.session_id}")
        if config:
            self.info(f"配置: {json.dumps(config, ensure_ascii=False)}")
        self.info("="*70)

        self.trade_logger.log_trade("session_start", {
            "session_id": self.trade_logger.session_id,
            "config": config
        })

    def session_end(self, stats: Dict = None):
        """记录会话结束"""
        self.trade_logger.flush()

        self.info("="*70)
        self.info(f"🏁 {self.name} 会话结束")
        if stats:
            self.info(f"统计: {json.dumps(stats, ensure_ascii=False)}")
        self.info("="*70)

        self.trade_logger.log_trade("session_end", {
            "session_id": self.trade_logger.session_id,
            "stats": stats
        })


# ==================== 便捷函数 ====================

_logger_instance = None

def get_logger(name: str = "FlashArbitrageBot", log_dir: str = "logs") -> BotLogger:
    """获取日志器实例

    Args:
        name: 日志器名称
        log_dir: 日志目录

    Returns:
        BotLogger实例
    """
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = BotLogger(name=name, log_dir=log_dir)
    return _logger_instance


def setup_logging(log_dir: str = "logs", console_level: str = "INFO") -> BotLogger:
    """设置日志系统

    Args:
        log_dir: 日志目录
        console_level: 控制台日志级别 (DEBUG/INFO/WARNING/ERROR)

    Returns:
        BotLogger实例
    """
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR
    }
    level = level_map.get(console_level.upper(), logging.INFO)

    return get_logger(log_dir=log_dir)
    # 可以在这里设置console_level
