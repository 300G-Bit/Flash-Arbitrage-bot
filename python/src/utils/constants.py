"""交易常量定义模块

集中管理交易相关的常量，消除代码中的魔法数字。
"""


class PricePercent:
    """价格百分比常量（用于计算止盈止损）"""

    QUICK_TP = 0.3           # 快速止盈百分比
    BREAKEVEN_SL = 0.3       # 保本止损百分比
    DEFAULT_HEDGE = 0.8      # 默认对冲价格偏移百分比
    DEFAULT_TP = 0.5         # 默认止盈百分比
    DEFAULT_SL = 1.0         # 默认止损百分比

    # 追踪止损回撤比例
    TRAILING_PULLBACK_30 = 0.3
    TRAILING_PULLBACK_50 = 0.5

    # 回撤比例
    RETRACEMENT_40 = 0.4
    RETRACEMENT_60 = 0.6


class TimeMs:
    """时间常量（毫秒）"""

    SPIKE_WINDOW = 30000        # 插针检测窗口（30秒）
    HEDGE_TIMEOUT = 300000      # 对冲超时时间（5分钟）
    ORDER_QUERY_DELAY = 50      # 订单查询延迟
    ORDER_CONFIRM_DELAY = 150   # 订单确认延迟
    WEBSOCKET_TIMEOUT = 60000   # WebSocket 超时时间
    RECONNECT_DELAY = 5000      # 重连延迟


class Direction:
    """交易方向常量"""

    UP = "UP"       # 向上
    DOWN = "DOWN"   # 向下


class Side:
    """订单侧常量"""

    BUY = "BUY"     # 买入
    SELL = "SELL"   # 卖出


class PositionSide:
    """持仓侧常量"""

    LONG = "LONG"       # 多头
    SHORT = "SHORT"     # 空头
    BOTH = "BOTH"       # 双向


class OrderType:
    """订单类型常量"""

    MARKET = "MARKET"                       # 市价单
    LIMIT = "LIMIT"                         # 限价单
    STOP = "STOP"                           # 止损单
    STOP_MARKET = "STOP_MARKET"             # 止损市价单
    TAKE_PROFIT = "TAKE_PROFIT"             # 止盈单
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"  # 止盈市价单
    TRAILING_STOP_MARKET = "TRAILING_STOP_MARKET"  # 追踪止损市价单


class OrderStatus:
    """订单状态常量"""

    NEW = "NEW"                     # 新建
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # 部分成交
    FILLED = "FILLED"               # 已成交
    CANCELED = "CANCELED"           # 已取消
    REJECTED = "REJECTED"           # 已拒绝
    EXPIRED = "EXPIRED"             # 已过期


class WorkingStatus:
    """工作状态常量"""

    OPENED = "opened"       # 已开仓
    CLOSED = "closed"       # 已平仓
    FAILED = "failed"       # 失败
    TIMEOUT = "timeout"     # 超时
    PENDING = "pending"     # 处理中
    HEDGING = "hedging"     # 对冲中


class SignalType:
    """信号类型常量"""

    SPIKE_UP = "SPIKE_UP"           # 向上插针
    SPIKE_DOWN = "SPIKE_DOWN"       # 向下插针
    TREND_FOLLOW = "TREND_FOLLOW"   # 趋势跟随
    MEAN_REVERT = "MEAN_REVERT"     # 均值回归


class RiskLimit:
    """风险限制常量"""

    MAX_POSITION_RATIO = 0.1        # 最大仓位比例
    MAX_DAILY_LOSS_RATIO = 0.05     # 最大日亏损比例
    MAX_CONSECUTIVE_LOSSES = 5      # 最大连续亏损次数
    EMERGENCY_STOP_LOSS_RATIO = 0.15  # 紧急止损比例
