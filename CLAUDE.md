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
│   │   │   ├── order_manager.py    # 订单管理器
│   │   │   ├── position_tracker.py # 持仓追踪器
│   │   │   ├── trade_executor.py   # 交易执行器
│   │   │   └── trade_logger.py     # 交易日志
│   │   ├── gateway/                # Redis 消费
│   │   ├── analysis/               # 趋势分析、插针检测
│   │   └── strategy/               # 交易策略
│   │
│   ├── config/
│   │   └── testnet_config.py       # 测试网配置
│   │
│   ├── testnet_with_recorder.py    # 主运行器
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

# 信号检测参数
MIN_SPIKE_PERCENT = 0.3           # 最小插针幅度
MAX_SPIKE_PERCENT = 5.0           # 最大插针幅度
MIN_RETRACEMENT = 15              # 最小回撤
```

### 交易模块

| 模块 | 文件 | 职责 |
|------|------|------|
| BinanceFuturesClient | `src/exchange/binance_futures.py` | 币安API封装 |
| OrderManager | `src/trading/order_manager.py` | 订单生命周期管理 |
| PositionTracker | `src/trading/position_tracker.py` | 持仓追踪、盈亏计算 |
| TradeExecutor | `src/trading/trade_executor.py` | 信号执行、风控 |
| TradeLogger | `src/trading/trade_logger.py` | 交易记录、数据导出 |

---

## 运行脚本

### 测试网交易集成运行器

```bash
cd python
python testnet_with_recorder.py
```

功能：
- WebSocket 实时行情订阅
- 插针信号检测
- 自动执行交易
- 订单状态监控
- 数据记录与导出

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

```python
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
        # 实现
        pass
    except Exception as e:
        logger.error(f"操作失败: {e}")
        return None
```

### Rust

- 使用 `cargo fmt` 格式化
- 使用 `cargo clippy` 检查
- 错误处理使用 `Result<T, E>`

---

## 常见任务

### 添加新的交易对

编辑 `testnet_with_recorder.py` 中的 `DEFAULT_SYMBOLS`：

```python
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
```

### 调整交易参数

修改 `config/testnet_config.py` 或设置环境变量：

```bash
export POSITION_USDT=20.0
export LEVERAGE=25
```

### 查看交易记录

交易记录保存在 `testnet_trades/` 目录：

```bash
ls testnet_trades/*.json
```

### 分析交易结果

```bash
python analyze_signals.py testnet_trades/recording_YYYYMMDD.json
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

---

## 开发建议

1. **优先使用测试网**: 在测试网验证策略后再考虑实盘
2. **小仓位测试**: 即使在测试网也使用小仓位
3. **关注日志**: 所有交易操作都有日志输出
4. **定期检查**: 使用 `analyze_signals.py` 分析交易结果

---

## 免责声明

本项目仅供学习和研究。加密货币交易具有高风险，请谨慎使用。
