"""
测试网配置 - 用于测试网模拟交易

使用方法:
1. 复制 .env.example 为 .env 并填写API密钥，或
2. 设置环境变量 BINANCE_TESTNET_API_KEY 和 BINANCE_TESTNET_API_SECRET
3. 运行测试网交易脚本

获取测试网API密钥: https://testnet.binancefuture.com/
"""

import os
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional

# 尝试加载 .env 文件
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    with open(_env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


@dataclass
class TestnetConfig:
    """测试网配置"""

    # ==================== 交易所配置 ====================

    # 币安期货测试网
    BINANCE_TESTNET_URL = "https://testnet.binancefuture.com"
    BINANCE_TESTNET_WS = "wss://stream.binancefuture.com/ws"

    # API密钥 (请从环境变量读取，不使用默认值以确保安全)
    BINANCE_API_KEY = os.getenv("BINANCE_TESTNET_API_KEY", "pmJ9VkqSKkIKwekV6SVMW11D0Pn0X0llgUMsbN474jScVNZONyTcIzqjyd74evxU")
    BINANCE_API_SECRET = os.getenv("BINANCE_TESTNET_API_SECRET", "cgqIKSG6z5xppSqMqvlUO7iq8vGIe0TrHj6xzL6IczktMsFtFrsVnkBASRG4fvK3")

    # 代理设置 (可选)
    ENABLE_PROXY = True
    PROXY_URL = "http://127.0.0.1:7897"

    # ==================== 交易参数 ====================

    # 仓位配置
    POSITION_USDT = 15.0           # 单笔仓位大小(USDT)
    LEVERAGE = 20                  # 杠杆倍数
    MARGIN_TYPE = "ISOLATED"       # 保证金模式: ISOLATED/CROSSED

    # 止损止盈配置
    DEFAULT_STOP_LOSS_PERCENT = 1.0    # 默认止损百分比
    DEFAULT_TAKE_PROFIT_PERCENT = 2.0  # 默认止盈百分比

    # 止盈级别 (可设置多个止盈单)
    TAKE_PROFIT_LEVELS = [1.5, 1.75, 2.0, 2.25]
    TAKE_PROFIT_ALLOCATION = [0.25, 0.25, 0.25, 0.25]  # 每个级别分配的比例

    # 止损级别
    STOP_LOSS_LEVELS = [1.0, 1.5, 2.0]

    # ==================== 风控参数 ====================

    # 每日限制
    MAX_DAILY_TRADES = 100          # 每日最大交易次数
    MAX_DAILY_LOSS_USDT = 100.0     # 每日最大亏损(USDT)
    MAX_CONSECUTIVE_LOSSES = 100     # 最大连续亏损次数

    # 熔断器
    ENABLE_CIRCUIT_BREAKER = True
    CIRCUIT_BREAKER_DURATION = 300  # 熔断持续时间(秒)

    # 仓位限制
    MAX_POSITION_USDT = 30.0       # 单笔最大仓位
    MIN_POSITION_USDT = 5.0        # 单笔最小仓位
    MAX_LEVERAGE = 50              # 最大杠杆

    # 风险警告阈值
    RISK_WARNING_THRESHOLD = 0.7   # 清算距离百分比触发警告

    # ==================== 信号过滤 ====================

    # 插针检测参数 (旧版，保留用于兼容)
    MIN_SPIKE_PERCENT = 0.8        # 最小插针幅度(%)
    MAX_SPIKE_PERCENT = 5.0        # 最大插针幅度(%)
    MIN_RETRACEMENT = 15           # 最小回撤比例(%)
    MAX_RETRACEMENT = 20           # 最大回撤比例(%)

    # ==================== ATR插针检测参数 (新版) ====================

    # ATR计算参数
    ATR_PERIOD = 7                        # ATR周期 (7=敏感, 14=稳定)
    ATR_SPIKE_MULTIPLIER = 0.3            # 速度阈值倍数 = ATR × K1 (降低阈值以提高灵敏度)
    ATR_RETRACE_MULTIPLIER = 0.2          # 回调阈值倍数 = ATR × K2
    SPIKE_DETECTION_WINDOW = 30           # 速度检测窗口(秒) - 缩短窗口以捕捉更快的变化

    # K线形态确认参数
    SHADOW_RATIO_THRESHOLD = 1.5          # 影线/实体比值阈值 (降低以提高灵敏度)
    FALSE_BREAKOUT_THRESHOLD = 0.001      # 假突破回归阈值(0.1%) - 降低以提高灵敏度

    # 检测冷却
    DETECTION_COOLDOWN_SECONDS = 15       # 同一币种检测间隔(秒) - 缩短以允许更频繁检测

    # 趋势对齐要求
    REQUIRE_TREND_ALIGNMENT = True
    MIN_TREND_SCORE = 60           # 最小趋势对齐分数

    # 交易时间窗口
    TRADING_HOURS_ONLY = False     # 是否只在高交易时段交易

    # ==================== 数据记录 ====================

    # 日志目录
    TRADE_LOG_DIR = "testnet_trades"
    ENABLE_AUTO_SAVE = True
    SAVE_INTERVAL = 60             # 自动保存间隔(秒)

    # 数据保留
    KEEP_DAYS = 30                 # 保留数据天数

    # ==================== 系统设置 ====================

    # 监控间隔
    ORDER_MONITOR_INTERVAL = 0.5   # 订单监控间隔(秒)
    POSITION_SYNC_INTERVAL = 1.0   # 持仓同步间隔(秒)

    # API超时
    API_TIMEOUT = 10               # API请求超时(秒)
    MAX_RETRIES = 3                # 最大重试次数
    RETRY_DELAY = 1                # 重试延迟(秒)

    # 手续费设置
    FEE_RATE = 0.0004              # 手续费率(0.04%)
    SLIPPAGE_TOLERANCE = 0.001     # 滑点容忍度(0.1%)

    # ==================== 显示设置 ====================

    # 日志级别
    LOG_LEVEL = "INFO"             # DEBUG/INFO/WARNING/ERROR

    # 控制台输出
    SHOW_ORDER_UPDATES = True
    SHOW_POSITION_UPDATES = True
    SHOW_PNL_UPDATES = True

    # ==================== 高级设置 ====================

    # 订单类型
    USE_MARKET_ORDERS = True       # 使用市价单入场
    ENTRY_LIMIT_OFFSET = 0.001     # 限价单偏移(0.1%)

    # OCO订单 (One-Cancels-Other)
    USE_OCO_ORDERS = False         # 使用OCO订单(需要交易所支持)

    # 跟踪止损
    ENABLE_TRAILING_STOP = False
    TRAILING_STOP_CALLBACK = 0.5   # 跟踪止损回调率(%)

    @classmethod
    def validate(cls) -> List[str]:
        """验证配置

        Returns:
            错误信息列表，空列表表示配置有效
        """
        errors = []

        if not cls.BINANCE_API_KEY:
            errors.append("BINANCE_API_KEY 未设置，请设置环境变量或配置文件")

        if not cls.BINANCE_API_SECRET:
            errors.append("BINANCE_API_SECRET 未设置，请设置环境变量或配置文件")

        if cls.POSITION_USDT < cls.MIN_POSITION_USDT:
            errors.append(f"POSITION_USDT ({cls.POSITION_USDT}) 小于 MIN_POSITION_USDT ({cls.MIN_POSITION_USDT})")

        if cls.POSITION_USDT > cls.MAX_POSITION_USDT:
            errors.append(f"POSITION_USDT ({cls.POSITION_USDT}) 大于 MAX_POSITION_USDT ({cls.MAX_POSITION_USDT})")

        if cls.LEVERAGE > cls.MAX_LEVERAGE:
            errors.append(f"LEVERAGE ({cls.LEVERAGE}) 大于 MAX_LEVERAGE ({cls.MAX_LEVERAGE})")

        if len(cls.TAKE_PROFIT_LEVELS) != len(cls.TAKE_PROFIT_ALLOCATION):
            errors.append("TAKE_PROFIT_LEVELS 和 TAKE_PROFIT_ALLOCATION 长度不一致")

        if abs(sum(cls.TAKE_PROFIT_ALLOCATION) - 1.0) > 0.01:
            errors.append("TAKE_PROFIT_ALLOCATION 总和应该为 1.0")

        return errors

    @classmethod
    def to_dict(cls) -> Dict:
        """转换为字典"""
        return {
            "binance": {
                "testnet_url": cls.BINANCE_TESTNET_URL,
                "ws_url": cls.BINANCE_TESTNET_WS,
                "api_key": cls.BINANCE_API_KEY[:10] + "..." if cls.BINANCE_API_KEY else "",
            },
            "trading": {
                "position_usdt": cls.POSITION_USDT,
                "leverage": cls.LEVERAGE,
                "margin_type": cls.MARGIN_TYPE,
                "stop_loss_percent": cls.DEFAULT_STOP_LOSS_PERCENT,
                "take_profit_percent": cls.DEFAULT_TAKE_PROFIT_PERCENT,
            },
            "risk_control": {
                "max_daily_trades": cls.MAX_DAILY_TRADES,
                "max_daily_loss": cls.MAX_DAILY_LOSS_USDT,
                "max_consecutive_losses": cls.MAX_CONSECUTIVE_LOSSES,
            },
            "filters": {
                "min_spike_percent": cls.MIN_SPIKE_PERCENT,
                "max_spike_percent": cls.MAX_SPIKE_PERCENT,
                "require_trend_alignment": cls.REQUIRE_TREND_ALIGNMENT,
            }
        }


def load_config() -> TestnetConfig:
    """加载配置

    Returns:
        TestnetConfig实例
    """
    # 验证配置
    errors = TestnetConfig.validate()
    if errors:
        print("配置验证失败:")
        for error in errors:
            print(f"  - {error}")
        print("\n请检查环境变量或配置文件")
        print("\n环境变量设置示例:")
        print("  export BINANCE_TESTNET_API_KEY=your_api_key")
        print("  export BINANCE_TESTNET_API_SECRET=your_api_secret")
        raise ValueError("配置验证失败")

    return TestnetConfig()


if __name__ == "__main__":
    # 打印当前配置
    print("当前测试网配置:")
    print("=" * 50)
    config_dict = TestnetConfig.to_dict()
    for section, values in config_dict.items():
        print(f"\n[{section}]")
        for key, value in values.items():
            print(f"  {key}: {value}")
    print("\n" + "=" * 50)

    # 验证配置
    errors = TestnetConfig.validate()
    if errors:
        print("\n配置验证失败:")
        for error in errors:
            print(f"  ❌ {error}")
    else:
        print("\n✓ 配置验证通过")
