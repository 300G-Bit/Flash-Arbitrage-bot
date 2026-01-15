"""时间格式化工具模块

提供统一的时间格式化函数，消除代码中的重复实现。
"""
from datetime import datetime, timezone, timedelta

# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))


def format_time_ms(dt: datetime = None, tz: timezone = BEIJING_TZ) -> str:
    """格式化时间为 HH:MM:SS.mmm 格式

    Args:
        dt: 要格式化的时间，None 则使用当前时间
        tz: 时区，默认为北京时区

    Returns:
        格式化后的时间字符串，如 "14:30:25.123"

    Examples:
        >>> format_time_ms()
        '14:30:25.123'
        >>> format_time_ms(datetime.now(timezone.utc), tz=timezone.utc)
        '06:30:25.123'
    """
    if dt is None:
        dt = datetime.now(tz)
    return dt.strftime("%H:%M:%S.%f")[:-3]


def format_time_iso(dt: datetime = None) -> str:
    """格式化时间为 ISO 8601 格式

    Args:
        dt: 要格式化的时间，None 则使用当前 UTC 时间

    Returns:
        ISO 格式的时间字符串

    Examples:
        >>> format_time_iso()
        '2025-01-14T06:30:25.123456+00:00'
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.isoformat()


def format_time_readable(dt: datetime = None, tz: timezone = BEIJING_TZ) -> str:
    """格式化时间为可读格式

    Args:
        dt: 要格式化的时间，None 则使用当前时间
        tz: 时区，默认为北京时区

    Returns:
        可读格式的时间字符串，如 "2025-01-14 14:30:25"

    Examples:
        >>> format_time_readable()
        '2025-01-14 14:30:25'
    """
    if dt is None:
        dt = datetime.now(tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_time_str(time_str: str) -> datetime:
    """解析时间字符串为 datetime 对象

    Args:
        time_str: ISO 格式的时间字符串

    Returns:
        datetime 对象

    Examples:
        >>> parse_time_str("2025-01-14T14:30:25+08:00")
        datetime.datetime(2025, 1, 14, 14, 30, 25, tzinfo=datetime.timezone(datetime.timedelta(seconds=28800), '+08:00'))
    """
    if time_str.endswith('Z'):
        time_str = time_str[:-1] + '+00:00'
    return datetime.fromisoformat(time_str)


def get_timestamp_ms() -> int:
    """获取当前时间戳（毫秒）

    Returns:
        当前时间戳（毫秒）
    """
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def timestamp_to_datetime(ts_ms: int, tz: timezone = BEIJING_TZ) -> datetime:
    """将时间戳转换为 datetime 对象

    Args:
        ts_ms: 时间戳（毫秒）
        tz: 目标时区

    Returns:
        datetime 对象
    """
    return datetime.fromtimestamp(ts_ms / 1000, tz=tz)
