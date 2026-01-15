# Flash Arbitrage Bot - Claude 项目文档

本文档为 Claude Code 提供项目上下文，帮助理解项目结构和开发规范。

---

## 项目概述

Flash Arbitrage Bot 是一个加密货币期货高频套利交易系统，采用 Rust + Python 混合架构：
- **Rust**: 高性能 WebSocket 行情网关
- **Python**: 策略引擎、交易执行、数据分析

### 核心功能

1. **插针检测**: 实时检测市场中的快速价格波动（插针）
2. **趋势分析**: 多时间框架趋势判断
3. **测试网交易**: 币安期货测试网完整交易功能
4. **风险控制**: 熔断器、止损止盈、日度限额
5. **统一日志**: 结构化日志系统，支持 correlation_id 追踪

---

## 项目结构

```
Flash_Arbitrage_bot/
├── rust/
│   └── gateway/              # Rust 行情网关
│       ├── src/
│       │   ├── main.rs       # 网关入口
│       │   ├── exchange.rs   # 交易所抽象
│       │   ├── binance.rs    # Binance WebSocket
│       │   └── redis_publisher.rs
│       └── Cargo.toml
│
├── python/
│   ├── src/
│   │   ├── exchange/               # 交易所客户端
│   │   │   └── binance_futures.py  # 币安期货API
│   │   ├── trading/                # 交易模块
│   │   │   ├── simple_hedge.py     # 对冲执行器
│   │   │   ├── order_manager.py    # 订单管理器
│   │   │   ├── position_tracker.py # 持仓追踪器
│   │   │   ├── trade_executor.py   # 交易执行器
│   │   │   └── trade_logger.py     # 交易日志
│   │   ├── analysis/               # 趋势分析、插针检测
│   │   │   ├── atr_detector.py     # ATR插针检测器
│   │   │   ├── atr_types.py        # ATR类型定义
│   │   │   └── kline_tracker.py    # K线追踪器
│   │   ├── utils/                  # 工具模块
│   │   │   ├── logging_config.py   # 统一日志系统
│   │   │   └── logger.py           # 旧版日志(兼容)
│   │   └── strategy/               # 交易策略
│   │
│   ├── config/
│   │   └── testnet_config.py       # 测试网配置
│   │
│   ├── docs/                       # 文档目录
│   │   └── logging_system_plan.md  # 日志系统计划
│   ├── testnet_mtf_trading.py      # 主运行器
│   ├── testnet_trading.py          # 测试网交易
│   ├── test_pin_recorder.py        # 插针记录
│   ├── analyze_signals.py          # 信号分析
│   └── CODE_QUALITY_REPORT.md      # 代码质量报告
│
├── config/
│   └── config.yaml
│
├── README.md
└── CLAUDE.md
```

---

## 重要文件说明

### 统一日志系统 (`python/src/utils/logging_config.py`)

新增的统一日志配置模块，提供：

| 组件 | 功能 |
|------|------|
| `ContextAdapter` | 日志上下文适配器，支持 correlation_id |
| `StructuredFormatter` | JSON 格式化器，输出结构化日志 |
| `EventLogger` | 事件日志记录器，预定义事件类型方法 |
| `setup_logging()` | 初始化日志系统，创建多个 handler |

**日志输出文件**：
- `logs/bot_YYYYMMDD.log` - 主日志（人类可读）
- `logs/events_YYYYMMDD.jsonl` - 所有事件（JSON）
- `logs/signals_YYYYMMDD.jsonl` - 仅信号事件（JSON）
- `logs/orders_YYYYMMDD.jsonl` - 仅订单事件（JSON）
- `logs/errors_YYYYMMDD.log` - 仅错误日志

**使用示例**：
```python
from src.utils.logging_config import get_logger, EventLogger, generate_correlation_id

# 初始化日志系统
setup_logging(log_dir="logs", console_level="INFO", file_level="DEBUG")

# 获取logger
logger = get_logger(__name__)
events = EventLogger(logger)

# 普通日志
logger.info("启动交易系统")

# 带correlation_id的日志（追踪完整交易流程）
correlation_id = generate_correlation_id()  # 生成唯一ID
logger.with_correlation_id(correlation_id).info("信号处理中")

# 结构化事件日志
events.log_signal_detected(symbol="BTCUSDT", direction="UP", price=50000)
events.log_order_filled(symbol="BTCUSDT", order_id="123", avg_price=50000, filled_qty=0.1)
```

### 测试网配置 (`python/config/testnet_config.py`)

测试网交易的核心参数配置：

```python
# 交易参数
POSITION_USDT = 15.0              # 单笔仓位
LEVERAGE = 20                     # 杠杆
DEFAULT_STOP_LOSS_PERCENT = 1.5   # 止损
DEFAULT_TAKE_PROFIT_PERCENT = 3.0 # 止盈

# 风控参数
MAX_DAILY_TRADES = 100             # 每日最大交易
MAX_DAILY_LOSS_USDT = 100.0        # 每日最大亏损
MAX_CONSECUTIVE_LOSSES = 100        # 最大连亏

# ATR检测参数
ATR_PERIOD = 7                     # ATR周期
ATR_SPIKE_MULTIPLIER = 0.5         # 速度阈值倍数
ATR_RETRACE_MULTIPLIER = 0.3       # 回调阈值倍数
```

### 交易模块

| 模块 | 文件 | 职责 |
|------|------|------|
| BinanceFuturesClient | `src/exchange/binance_futures.py` | 币安API封装 |
| SimpleHedgeExecutor | `src/trading/simple_hedge.py` | 对冲策略执行器 |
| SpikeDetector | `src/analysis/atr_detector.py` | ATR插针检测器 |
| KlineTracker | `src/analysis/kline_tracker.py` | K线数据管理 |

### 日志系统事件类型

| 事件类型 | 方法 | 说明 |
|----------|------|------|
| 信号事件 | `log_signal_detected()` | 记录检测到的信号 |
| 信号事件 | `log_signal_filtered()` | 记录被过滤的信号 |
| 订单事件 | `log_order_submitting()` | 订单提交前 |
| 订单事件 | `log_order_submitted()` | 订单已提交 |
| 订单事件 | `log_order_filled()` | 订单成交 |
| 订单事件 | `log_order_failed()` | 订单失败 |
| 持仓事件 | `log_position_opened()` | 持仓开启 |
| 持仓事件 | `log_hedge_opened()` | 对冲开启 |
| 持仓事件 | `log_hedge_closed()` | 对冲关闭 |
| API事件 | `log_api_request()` | API请求 |
| API事件 | `log_api_response()` | API响应 |
| API事件 | `log_api_error()` | API错误 |

---

## 运行脚本

### 测试网交易（主运行器）

```bash
cd python
python testnet_mtf_trading.py
```

功能：
- WebSocket 实时行情订阅
- ATR动态阈值插针检测
- 对冲策略自动执行
- 完整日志记录与追踪
- 订单状态监控

### 插针检测与记录

```bash
python test_pin_recorder.py
```

功能：
- 实时检测插针
- 信号数据记录
- 支持后续回测分析

### 信号分析

```bash
python analyze_signals.py
```

功能：
- 分析录制的信号数据
- 统计胜率、盈亏比
- 生成可视化报告

---

## 代码规范

### Python

- 类型注解：所有公共函数必须添加类型注解
- 文档字符串：使用 Google 风格的 docstring
- 导入顺序：标准库 → 第三方库 → 本地模块
- 错误处理：API 调用必须处理异常
- 日志记录：使用统一日志系统

```python
from src.utils.logging_config import get_logger, EventLogger

logger = get_logger(__name__)
events = EventLogger(logger)

def example_function(
    symbol: str,
    quantity: float,
    price: float = None
) -> Optional[OrderInfo]:
    """函数功能说明

    Args:
        symbol: 交易对
        quantity: 数量
        price: 价格，None 表示市价单

    Returns:
        OrderInfo 对象，失败返回 None
    """
    try:
        events.log_order_submitting(symbol, "BUY", quantity)
        # 实现
        events.log_order_filled(symbol, order_id, price, quantity)
        return result
    except Exception as e:
        events.log_error("order_error", str(e), symbol=symbol)
        logger.exception(f"操作失败: {e}")
        return None
```

### Rust

- 使用 `cargo fmt` 格式化
- 使用 `cargo clippy` 检查
- 错误处理使用 `Result<T, E>`

---

## 常见任务

### 添加新的交易对

编辑 `testnet_mtf_trading.py` 中的 `DEFAULT_SYMBOLS`：

```python
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
```

### 调整交易参数

修改 `config/testnet_config.py` 或设置环境变量：

```bash
export POSITION_USDT=20.0
export LEVERAGE=25
```

### 查看日志

日志保存在 `logs/` 目录：

```bash
# 查看主日志
tail -f logs/bot_$(date +%Y%m%d).log

# 查看信号事件
cat logs/signals_$(date +%Y%m%d).jsonl | jq

# 查看订单事件
cat logs/orders_$(date +%Y%m%d).jsonl | jq
```

### 追踪完整交易流程

使用 correlation_id 在日志中搜索：

```bash
grep "sig_20250115123456_abc123" logs/events_*.jsonl
```

---

## API 密钥配置

### 币安测试网

1. 访问 https://testnet.binancefuture.com/
2. 注册并获取 API 密钥
3. 设置环境变量：

```bash
export BINANCE_TESTNET_API_KEY=your_api_key
export BINANCE_TESTNET_API_SECRET=your_api_secret
```

---

## 已知问题与修复

详见 `python/CODE_QUALITY_REPORT.md`：

- ✅ P0-1: HTTP 请求方法错误 (已修复)
- ✅ P0-3: 订单 ID 类型问题 (已修复)
- ✅ P1-5: 线程安全问题 (已修复)
- ✅ P1-6: 止损止盈时机问题 (已修复)
- ✅ P2-1: 统一日志系统 (已完成)

---

## 开发建议

1. **优先使用测试网**: 在测试网验证策略后再考虑实盘
2. **小仓位测试**: 即使在测试网也使用小仓位
3. **关注日志**: 使用统一日志系统追踪所有交易事件
4. **定期检查**: 使用 `analyze_signals.py` 分析交易结果
5. **Correlation ID**: 每个交易流程生成唯一 correlation_id 便于追踪

---

## 免责声明

本项目仅供学习和研究。加密货币交易具有高风险，请谨慎使用。
