# Flash Arbitrage Bot

超高频"插针回调"对冲套利交易系统

## 项目概述

Flash Arbitrage Bot 是一个专为加密货币期货市场设计的高频套利交易系统。系统通过捕捉市场中的"插针"现象，利用两阶段入场策略（第一腿反向吃回调，第二腿顺向吃反弹）实现双向盈利。

### 核心特性

- **高性能数据网关**: Rust 实现的 WebSocket 行情网关，支持 Binance 和 OKX
- **多时间框架分析**: 4H/1H/30m/15m/5m/1m 多时间框架趋势判断
- **智能插针检测**: 基于速度、成交量、形态的综合插针识别算法
- **两阶段入场策略**: 第一腿回调交易 + 第二腿反弹交易
- **完整风险控制**: 熔断器、止损、日度限额、黑名单机制
- **模拟交易模式**: 支持模拟交易验证策略有效性
- **数据持久化**: 完整的交易历史和数据分析

## 系统架构

```
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│  Rust 行情网关  │ ───▶ │  Redis 消息队列  │ ───▶ │  Python 策略引擎  │
│  - WebSocket    │      │  - Tick Stream  │      │  - 趋势分析      │
│  - Binance/OKX  │      │  - Kline Stream │      │  - 插针检测      │
│  - 高性能解析   │      │  - Depth Stream │      │  - 交易执行      │
└─────────────────┘      └─────────────────┘      │  - 风险控制      │
                                                  └─────────────────┘
```

## 快速开始

### 环境要求

- Rust 1.75+
- Python 3.11+
- Docker Desktop (用于 Redis 和 PostgreSQL)
- Windows/Linux/macOS

### 安装步骤

1. **克隆项目**
```bash
git clone <repository-url>
cd Flash_Arbitrage_bot
```

2. **启动依赖服务**
```bash
docker-compose up -d redis postgres
```

3. **安装 Rust 依赖**
```bash
cd rust/gateway
cargo build
```

4. **安装 Python 依赖**
```bash
cd ../python
python -m venv venv

# Windows
.\venv\Scripts\activate

# Linux/macOS
source venv/bin/activate

pip install -r requirements.txt
```

5. **配置环境变量**
```bash
cp .env.example .env
# 编辑 .env 文件，填入你的 API 密钥
```

### 运行

**启动 Rust 行情网关**
```bash
cd rust/gateway
cargo run -- --symbols BTCUSDT,ETHUSDT --exchanges binance
```

**启动 Python 策略引擎 (模拟模式)**
```bash
cd python
python src/main.py --mode simulation
```

## 配置说明

主配置文件位于 `config/config.yaml`，支持以下主要配置项：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `base.mode` | 运行模式 (simulation/live) | simulation |
| `base.account_balance` | 账户余额 (USDT) | 100 |
| `position.base_position_usdt` | 单腿仓位大小 | 15 |
| `position.default_leverage` | 默认杠杆倍数 | 20 |
| `trend_analysis.min_alignment_score` | 最小趋势对齐分数 | 60 |
| `pin_detection.velocity_threshold` | 插针速度阈值 | 0.003 (0.3%) |
| `risk_control.daily_loss_percent_limit` | 日亏损限额 | 0.10 (10%) |

## 项目结构

```
Flash_Arbitrage_bot/
├── rust/
│   └── gateway/           # Rust 行情网关
│       ├── src/
│       │   ├── main.rs
│       │   ├── exchange.rs       # 交易所抽象接口
│       │   ├── binance.rs        # Binance WebSocket 实现
│       │   ├── okx.rs            # OKX WebSocket 实现
│       │   └── redis_publisher.rs
│       └── Cargo.toml
├── python/
│   ├── src/
│   │   ├── main.py
│   │   ├── gateway/              # 数据网关 (Redis 消费)
│   │   ├── analysis/             # 分析引擎
│   │   ├── strategy/             # 交易策略
│   │   ├── execution/            # 订单执行
│   │   └── risk/                 # 风险控制
│   ├── requirements.txt
│   └── pyproject.toml
├── config/
│   └── config.yaml        # 主配置文件
├── docker-compose.yml
└── README.md
```

## 策略说明

### 插针检测原理

系统通过以下条件综合判断插针：
1. **速度特征**: 短时间内价格变化率超过阈值
2. **成交量特征**: 伴随异常放大的成交量
3. **形态特征**: 快速冲击后快速回撤
4. **趋势对齐**: 插针方向必须与大趋势一致

### 两阶段入场策略

**向上插针示例 (大趋势为上涨)**:
1. 检测向上插针到达顶点
2. 等待回调确认 (0.3% 回撤)
3. 第一腿: 开空单，吃回调利润
4. 回调到位后，第二腿: 开多单，吃反弹利润
5. 分别在最优位置平仓

### 风险控制

| 机制 | 触发条件 | 动作 |
|------|----------|------|
| 止损 | 亏损达 1% | 立即平仓 |
| 超时强平 | 持仓超 60 秒 | 强制平仓 |
| 熔断器 | 连续亏损 5 次 | 暂停交易 5 分钟 |
| 日度限额 | 日亏损达 10% | 停止当日交易 |

## 开发指南

### 运行测试

**Rust 测试**
```bash
cd rust/gateway
cargo test
```

**Python 测试**
```bash
cd python
pytest tests/
```

### 代码规范

- Rust: 使用 `cargo fmt` 格式化，`cargo clippy` 检查
- Python: 使用 `black` 格式化，`ruff` 检查

## 免责声明

本项目仅供学习和研究使用。加密货币交易具有高风险，可能导致全部资金损失。使用本系统进行实盘交易的风险由使用者自行承担。

## 许可证

MIT License

## 联系方式

- Issues: 在 GitHub 上提交 Issue
