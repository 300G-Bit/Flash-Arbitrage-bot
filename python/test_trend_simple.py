#!/usr/bin/env python3
"""
趋势分析测试脚本 - 使用币安API
"""

import os
import sys
import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import structlog
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="ISO"),
        structlog.dev.ConsoleRenderer()
    ]
)
logger = structlog.get_logger()


# ============== 配置代理 ==============
# 如果你在中国大陆，可能需要配置代理
PROXY_CONFIG = {
    # 取消下面的注释并修改为你的代理地址
    'http': 'http://127.0.0.1:7897',
    'https': 'http://127.0.0.1:7897',
}

# 备用API地址（如果主地址被墙）
API_ENDPOINTS = [
    "https://fapi.binance.com",      # 主地址
    "https://fapi1.binance.com",     # 备用1
    "https://fapi2.binance.com",     # 备用2
    "https://fapi3.binance.com",     # 备用3
    "https://fapi4.binance.com",     # 备用4
]


def fetch_klines_rest(symbol: str, interval: str = "1m", limit: int = 200) -> Optional[pd.DataFrame]:
    """从币安获取K线数据"""
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    for endpoint in API_ENDPOINTS:
        url = f"{endpoint}/fapi/v1/klines"

        try:
            logger.info(f"Trying endpoint", endpoint=endpoint, symbol=symbol)

            response = requests.get(
                url,
                params=params,
                timeout=15,
                proxies=PROXY_CONFIG if PROXY_CONFIG else None
            )

            if response.status_code == 200:
                data = response.json()

                if not data:
                    logger.warning(f"Empty data from {endpoint}")
                    continue

                # 转换为DataFrame
                df = pd.DataFrame(data, columns=[
                    'timestamp', 'open', 'high', 'low', 'close', 'volume',
                    'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                    'taker_buy_quote', 'ignore'
                ])

                # 转换数据类型
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = df[col].astype(float)

                df.set_index('timestamp', inplace=True)
                logger.info(f"Successfully fetched data", endpoint=endpoint, rows=len(df))
                return df

            else:
                logger.warning(f"HTTP error", endpoint=endpoint, status=response.status_code)

        except requests.exceptions.ConnectTimeout:
            logger.warning(f"Connection timeout", endpoint=endpoint)
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Connection error", endpoint=endpoint, error=str(e)[:100])
        except Exception as e:
            logger.warning(f"Request failed", endpoint=endpoint, error=str(e)[:100])

        # 尝试下一个端点前等待
        time.sleep(0.5)

    logger.error(f"All endpoints failed for {symbol}")
    return None


def fetch_klines_with_proxy_test() -> bool:
    """测试网络连接"""
    test_url = "https://fapi.binance.com/fapi/v1/ping"

    try:
        response = requests.get(test_url, timeout=10, proxies=PROXY_CONFIG if PROXY_CONFIG else None)
        return response.status_code == 200
    except:
        return False


# ============== 技术指标计算 ==============

def calculate_ema(data: pd.Series, period: int) -> pd.Series:
    """计算指数移动平均线"""
    return data.ewm(span=period, adjust=False).mean()


def calculate_sma(data: pd.Series, period: int) -> pd.Series:
    """计算简单移动平均线"""
    return data.rolling(window=period).mean()


def calculate_rsi(data: pd.Series, period: int = 14) -> pd.Series:
    """计算RSI指标"""
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()

    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """计算ATR（平均真实波幅）"""
    high = df['high']
    low = df['low']
    close = df['close']

    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr


def calculate_macd(data: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """计算MACD指标"""
    ema_fast = calculate_ema(data, fast)
    ema_slow = calculate_ema(data, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calculate_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_bollinger_bands(data: pd.Series, period: int = 20, std_dev: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """计算布林带"""
    middle = calculate_sma(data, period)
    std = data.rolling(window=period).std()
    upper = middle + (std * std_dev)
    lower = middle - (std * std_dev)
    return upper, middle, lower


# ============== 趋势分析 ==============

def analyze_trend(df: pd.DataFrame, symbol: str = "Unknown") -> Dict[str, Any]:
    """分析趋势"""
    if df is None or len(df) < 50:
        return {"error": "Insufficient data"}

    close = df['close']

    # 计算各种指标
    ema9 = calculate_ema(close, 9)
    ema21 = calculate_ema(close, 21)
    ema50 = calculate_ema(close, 50)

    rsi = calculate_rsi(close, 14)
    macd_line, signal_line, macd_hist = calculate_macd(close)
    bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(close)
    atr = calculate_atr(df)

    # 当前值
    current_price = close.iloc[-1]
    current_ema9 = ema9.iloc[-1]
    current_ema21 = ema21.iloc[-1]
    current_ema50 = ema50.iloc[-1]
    current_rsi = rsi.iloc[-1]
    current_macd = macd_line.iloc[-1]
    current_signal = signal_line.iloc[-1]
    current_macd_hist = macd_hist.iloc[-1]
    current_atr = atr.iloc[-1]

    # 趋势判断
    trend_signals = []
    trend_score = 0

    # 1. EMA排列
    if current_ema9 > current_ema21 > current_ema50:
        trend_signals.append("EMA多头排列 ↑")
        trend_score += 2
    elif current_ema9 < current_ema21 < current_ema50:
        trend_signals.append("EMA空头排列 ↓")
        trend_score -= 2
    else:
        trend_signals.append("EMA震荡 ↔")

    # 2. 价格与EMA关系
    if current_price > current_ema21:
        trend_signals.append("价格在EMA21上方")
        trend_score += 1
    else:
        trend_signals.append("价格在EMA21下方")
        trend_score -= 1

    # 3. RSI
    if current_rsi > 70:
        trend_signals.append(f"RSI超买 ({current_rsi:.1f})")
        trend_score -= 1
    elif current_rsi < 30:
        trend_signals.append(f"RSI超卖 ({current_rsi:.1f})")
        trend_score += 1
    elif current_rsi > 50:
        trend_signals.append(f"RSI偏多 ({current_rsi:.1f})")
        trend_score += 0.5
    else:
        trend_signals.append(f"RSI偏空 ({current_rsi:.1f})")
        trend_score -= 0.5

    # 4. MACD
    if current_macd > current_signal and current_macd_hist > 0:
        trend_signals.append("MACD金叉/多头")
        trend_score += 1
    elif current_macd < current_signal and current_macd_hist < 0:
        trend_signals.append("MACD死叉/空头")
        trend_score -= 1

    # 5. 布林带位置
    bb_position = (current_price - bb_lower.iloc[-1]) / (bb_upper.iloc[-1] - bb_lower.iloc[-1])
    if bb_position > 0.8:
        trend_signals.append(f"接近布林上轨 ({bb_position:.1%})")
    elif bb_position < 0.2:
        trend_signals.append(f"接近布林下轨 ({bb_position:.1%})")
    else:
        trend_signals.append(f"布林中轨附近 ({bb_position:.1%})")

    # 综合判断
    if trend_score >= 3:
        overall_trend = "强势上涨"
        trend_direction = "BULLISH"
    elif trend_score >= 1:
        overall_trend = "偏多震荡"
        trend_direction = "SLIGHTLY_BULLISH"
    elif trend_score <= -3:
        overall_trend = "强势下跌"
        trend_direction = "BEARISH"
    elif trend_score <= -1:
        overall_trend = "偏空震荡"
        trend_direction = "SLIGHTLY_BEARISH"
    else:
        overall_trend = "横盘整理"
        trend_direction = "NEUTRAL"

    return {
        "symbol": symbol,
        "current_price": current_price,
        "trend_direction": trend_direction,
        "overall_trend": overall_trend,
        "trend_score": trend_score,
        "signals": trend_signals,
        "indicators": {
            "EMA9": current_ema9,
            "EMA21": current_ema21,
            "EMA50": current_ema50,
            "RSI": current_rsi,
            "MACD": current_macd,
            "MACD_Signal": current_signal,
            "MACD_Hist": current_macd_hist,
            "ATR": current_atr,
            "ATR_Percent": (current_atr / current_price) * 100,
            "BB_Position": bb_position
        }
    }


def print_analysis(analysis: Dict[str, Any], symbol: str, timeframe: str):
    """打印分析结果"""
    if "error" in analysis:
        print(f"\n  X {timeframe}: {analysis['error']}")
        return

    direction_emoji = {
        "BULLISH": "GREEN",
        "SLIGHTLY_BULLISH": "YELLOW",
        "NEUTRAL": "WHITE",
        "SLIGHTLY_BEARISH": "ORANGE",
        "BEARISH": "RED"
    }

    emoji = direction_emoji.get(analysis['trend_direction'], "?")

    print(f"\n  [{emoji}] {timeframe} - {analysis['overall_trend']} (评分: {analysis['trend_score']:.1f})")
    print(f"     当前价格: {analysis['current_price']:.4f}")
    print(f"     信号:")
    for signal in analysis['signals']:
        print(f"       - {signal}")

    ind = analysis['indicators']
    print(f"     指标详情:")
    print(f"       EMA: 9={ind['EMA9']:.4f}, 21={ind['EMA21']:.4f}, 50={ind['EMA50']:.4f}")
    print(f"       RSI: {ind['RSI']:.2f}")
    print(f"       MACD: {ind['MACD']:.6f} (Signal: {ind['MACD_Signal']:.6f})")
    print(f"       ATR: {ind['ATR']:.4f} ({ind['ATR_Percent']:.2f}%)")


def test_symbol(symbol: str):
    """测试单个交易对"""
    print(f"\n{'='*60}")
    print(f"分析 {symbol}")
    print(f"{'='*60}")

    timeframes = ["1m", "5m", "15m"]

    for tf in timeframes:
        print(f"\n正在获取 {tf} 数据...")
        df = fetch_klines_rest(symbol, tf, limit=200)

        if df is not None:
            analysis = analyze_trend(df, symbol)
            print_analysis(analysis, symbol, tf)
        else:
            print(f"  X 无法获取 {tf} 数据")

        time.sleep(0.3)  # 避免请求过快


def main():
    """主函数"""
    print("=" * 60)
    print("Flash Arbitrage Bot - 趋势分析测试")
    print("=" * 60)

    print("\n正在测试与币安API的连接...")

    if not PROXY_CONFIG:
        print("! 未配置代理。如果你在中国大陆，可能需要配置代理。")
        print("  编辑脚本顶部的 PROXY_CONFIG 配置。")

    if fetch_klines_with_proxy_test():
        print("OK 网络连接正常")
    else:
        print("X 无法连接到币安API")
        print("\n可能的解决方案:")
        print("1. 检查网络连接")
        print("2. 配置代理 (编辑脚本顶部的 PROXY_CONFIG)")
        print("3. 使用VPN")
        print("4. 脚本会尝试备用API端点")

    user_input = input("\n输入要测试的交易对 (逗号分隔，或回车使用默认值): ").strip()

    if user_input:
        symbols = [s.strip().upper() for s in user_input.split(",")]
    else:
        symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]

    print(f"\n将测试: {', '.join(symbols)}")

    for symbol in symbols:
        try:
            test_symbol(symbol)
        except KeyboardInterrupt:
            print("\n\n用户中断")
            break
        except Exception as e:
            print(f"\nX 测试 {symbol} 时出错: {e}")

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
