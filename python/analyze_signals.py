#!/usr/bin/env python3
"""
插针信号数据分析脚本

用于分析已收集的插针信号数据，生成统计报告。
支持详细的每笔交易分析，包括趋势预测、入场/出场价格、波动率等指标。
"""

import os
import sys
import json
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from collections import defaultdict

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.signal_recorder import SignalRecorder
from src.analysis.signal_analytics import SignalAnalytics, ReportGenerator

# ============== 配置 ==============
DATA_DIR = Path("pin_data")

# 默认配置 (如果没有找到配置文件)
DEFAULT_CONFIG = {
    "capital": 15.0,
    "leverage": 20,
    "fee_rate": 0.0004,
    "take_profit_levels": [2.0, 3.0, 4.0, 5.0, 6.0, 8.0],
    "stop_loss_levels": [1.0, 1.5, 2.0, 2.5, 3.0],
    "default_tp": 3.0,
    "default_sl": 1.5,
    "tracking_seconds": 90,
    "tracking_interval_ms": 100,
}


def load_session_config() -> Dict:
    """加载会话配置"""
    config_file = DATA_DIR / "session_config.json"

    if not config_file.exists():
        print("未找到配置文件，使用默认配置")
        return {"current": {"trading": DEFAULT_CONFIG, "data": DEFAULT_CONFIG}}

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        current = data.get('current', {})
        history_count = data.get('history_count', 1)

        print(f"✓ 加载配置文件 (历史记录: {history_count}次)")

        # 合并配置
        trading = current.get('trading', {})
        data_cfg = current.get('data', {})

        return {
            'current': current,
            'trading': trading,
            'data': data_cfg,
            'capital': trading.get('capital', DEFAULT_CONFIG['capital']),
            'leverage': trading.get('leverage', DEFAULT_CONFIG['leverage']),
            'fee_rate': trading.get('fee_rate', DEFAULT_CONFIG['fee_rate']),
            'take_profit_levels': trading.get('take_profit_levels', DEFAULT_CONFIG['take_profit_levels']),
            'stop_loss_levels': trading.get('stop_loss_levels', DEFAULT_CONFIG['stop_loss_levels']),
            'default_tp': trading.get('default_tp', DEFAULT_CONFIG['default_tp']),
            'default_sl': trading.get('default_sl', DEFAULT_CONFIG['default_sl']),
            'tracking_seconds': data_cfg.get('tracking_seconds', DEFAULT_CONFIG['tracking_seconds']),
            'tracking_interval_ms': data_cfg.get('tracking_interval_ms', DEFAULT_CONFIG['tracking_interval_ms']),
        }
    except Exception as e:
        print(f"读取配置文件失败: {e}")
        return {"current": {"trading": DEFAULT_CONFIG, "data": DEFAULT_CONFIG}}


def print_config_header(config: Dict):
    """打印配置信息（兼容旧数据格式）"""
    print("\n" + "=" * 80)
    print("会话配置参数")
    print("=" * 80)

    current = config.get('current', {})
    trading = current.get('trading', {})
    data_cfg = current.get('data', {})
    detection = current.get('detection', {})
    symbols = current.get('symbols', [])

    print(f"\n【交易参数】")
    capital_val = trading.get('capital')
    leverage_val = trading.get('leverage')
    fee_val = trading.get('fee_rate')
    slippage_val = trading.get('slippage')

    print(f"  本金: {capital_val if capital_val is not None else 'N/A'} USDT")
    print(f"  杠杆: {leverage_val}x" if leverage_val is not None else "  杠杆: N/A")
    if fee_val is not None:
        print(f"  手续费率: {fee_val*100:.3f}%")
    else:
        print(f"  手续费率: N/A")
    if slippage_val is not None:
        print(f"  滑点: {slippage_val*100:.3f}%")
    else:
        print(f"  滑点: N/A")

    print(f"\n【止盈止损设置】")
    tp_levels = trading.get('take_profit_levels', [])
    sl_levels = trading.get('stop_loss_levels', [])
    print(f"  止盈档位: {tp_levels if tp_levels else 'N/A'}")
    print(f"  止损档位: {sl_levels if sl_levels else 'N/A'}")
    print(f"  默认止盈: {trading.get('default_tp', 'N/A')}%")
    print(f"  默认止损: {trading.get('default_sl', 'N/A')}%")

    print(f"\n【数据记录参数】")
    print(f"  信号前记录: {data_cfg.get('price_history_seconds', 'N/A')}秒")
    print(f"  信号后跟踪: {data_cfg.get('tracking_seconds', 'N/A')}秒")
    print(f"  采样间隔: {data_cfg.get('tracking_interval_ms', 'N/A')}ms")

    print(f"\n【插针检测参数】")
    print(f"  最小幅度: {detection.get('min_spike_percent', 'N/A')}%")
    print(f"  最大幅度: {detection.get('max_spike_percent', 'N/A')}%")
    print(f"  回撤阈值: {detection.get('retracement_percent', 'N/A')}%")
    print(f"  检测窗口: {detection.get('price_window_ms', 'N/A')}ms")

    print(f"\n【监控交易对】 ({len(symbols)}个)")
    for i, symbol in enumerate(symbols[:15]):
        print(f"  {symbol}", end="")
        if (i + 1) % 5 == 0:
            print()
    if len(symbols) > 15:
        print(f"\n  ... 还有 {len(symbols) - 15} 个")

    print(f"\n【会话时间】")
    print(f"  启动时间: {current.get('session_start', 'N/A')}")

    print("\n" + "=" * 80)


# ============== 全局配置变量 ==============
SESSION_CONFIG = load_session_config()
CAPITAL = SESSION_CONFIG.get('capital', 15.0)
LEVERAGE = SESSION_CONFIG.get('leverage', 20)
FEE_RATE = SESSION_CONFIG.get('fee_rate', 0.0004)


def load_spike_files() -> List[Dict]:
    """加载所有spike JSON文件"""
    spikes = []

    if not DATA_DIR.exists():
        print(f"数据目录不存在: {DATA_DIR}")
        return spikes

    for json_file in DATA_DIR.glob("spike_*.json"):
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                spikes.append(data)
        except Exception as e:
            print(f"读取文件失败 {json_file}: {e}")

    print(f"加载了 {len(spikes)} 个信号文件")
    return spikes


def calculate_volatility(prices: List[float]) -> float:
    """计算波动率 (标准差 / 平均价格 * 100)"""
    if not prices or len(prices) < 2:
        return 0.0
    return np.std(prices) / np.mean(prices) * 100


def calculate_price_range(spike: Dict) -> float:
    """计算价格波动范围百分比"""
    prices_after = spike.get('prices_after', [])
    if not prices_after:
        return 0.0

    prices = [p.get('price', 0) for p in prices_after if p.get('price')]
    if not prices:
        return 0.0

    price_range = (max(prices) - min(prices)) / spike.get('entry_price', 1) * 100
    return price_range


def predict_trend(spike: Dict) -> Dict[str, str]:
    """
    预测交易趋势

    基于信号方向、价格历史和当前状态判断趋势
    """
    direction = spike.get('direction', '')
    entry_price = spike.get('entry_price', 0)
    peak_price = spike.get('peak_price', 0)
    amplitude = spike.get('amplitude_percent', 0)
    retracement = spike.get('retracement_percent', 0)

    analysis = spike.get('analysis', {})
    max_profit = analysis.get('max_profit_percent', 0)
    max_loss = analysis.get('max_loss_percent', 0)

    # 预测信号方向
    if direction == 'UP':
        signal_direction = "做多信号 (向下插针后做多)"
        expected_move = "预期反弹上涨"
    else:
        signal_direction = "做空信号 (向上插针后做空)"
        expected_move = "预期回调下跌"

    # 趋势强度
    if amplitude >= 1.0:
        strength = "强"
    elif amplitude >= 0.5:
        strength = "中"
    else:
        strength = "弱"

    # 回撤质量
    if retracement >= 50:
        quality = "优秀 (充分回撤)"
    elif retracement >= 30:
        quality = "良好 (标准回撤)"
    else:
        quality = "一般 (回撤不足)"

    # 预测结果
    if max_profit > 0.3:
        prediction = "高概率盈利"
    elif max_profit > 0.1:
        prediction = "可能盈利"
    elif max_profit > 0:
        prediction = "微利机会"
    else:
        prediction = "无盈利空间"

    return {
        'signal_direction': signal_direction,
        'expected_move': expected_move,
        'strength': strength,
        'retracement_quality': quality,
        'prediction': prediction,
        'amplitude_level': f"{amplitude:.2f}%",
        'retracement_level': f"{retracement:.1f}%"
    }


def get_trade_exit_info(spike: Dict, tp: float = 0.5, sl: float = 0.3,
                        capital: float = None, leverage: int = None, fee_rate: float = None) -> Dict:
    """
    获取交易的出场信息

    Args:
        spike: 信号数据
        tp: 止盈百分比
        sl: 止损百分比
        capital: 本金 (从文件读取)
        leverage: 杠杆 (从文件读取)
        fee_rate: 手续费率 (从文件读取)
    """
    # 使用文件中的配置或全局配置
    cap = capital if capital is not None else CAPITAL
    lev = leverage if leverage is not None else LEVERAGE
    fee = fee_rate if fee_rate is not None else FEE_RATE

    entry_price = spike.get('entry_price', 0)
    direction = spike.get('direction', '')
    prices_after = spike.get('prices_after', [])

    if not entry_price or not prices_after:
        return {
            'exit_price': None,
            'exit_time_ms': None,
            'exit_reason': 'NO_DATA',
            'exit_price_pct': None
        }

    # 计算止盈止损价格 (使用文件中的杠杆)
    if direction == 'UP':
        # 做多
        tp_price = entry_price * (1 + tp / 100 / lev)
        sl_price = entry_price * (1 - sl / 100 / lev)
    else:
        # 做空
        tp_price = entry_price * (1 - tp / 100 / lev)
        sl_price = entry_price * (1 + sl / 100 / lev)

    # 模拟交易过程
    exit_price = None
    exit_time = None
    exit_reason = 'TIMEOUT'
    final_price = prices_after[-1].get('price', entry_price)

    for i, tick in enumerate(prices_after):
        price = tick.get('price', entry_price)
        time_str = tick.get('timestamp', '')
        try:
            tick_time = datetime.fromisoformat(time_str)
            detected_at = datetime.fromisoformat(spike.get('detected_at', ''))
            time_ms = int((tick_time - detected_at).total_seconds() * 1000)
        except:
            time_ms = i * 100  # 默认100ms间隔

        if direction == 'UP':
            # 做多: 价格上涨止盈，下跌止损
            if price >= tp_price:
                exit_price = tp_price
                exit_time = time_ms
                exit_reason = 'TP'
                break
            elif price <= sl_price:
                exit_price = sl_price
                exit_time = time_ms
                exit_reason = 'SL'
                break
        else:
            # 做空: 价格下跌止盈，上涨止损
            if price <= tp_price:
                exit_price = tp_price
                exit_time = time_ms
                exit_reason = 'TP'
                break
            elif price >= sl_price:
                exit_price = sl_price
                exit_time = time_ms
                exit_reason = 'SL'
                break

    if exit_price is None:
        exit_price = final_price
        exit_time = len(prices_after) * 100
        exit_reason = 'TIMEOUT'

    # 计算涨跌幅
    if direction == 'UP':
        price_pct = (exit_price - entry_price) / entry_price * 100
    else:
        price_pct = (entry_price - exit_price) / entry_price * 100

    return {
        'exit_price': exit_price,
        'exit_time_ms': exit_time,
        'exit_reason': exit_reason,
        'exit_price_pct': price_pct,
        'tp_price': tp_price,
        'sl_price': sl_price
    }


def calculate_pnl(spike: Dict, exit_info: Dict,
                   capital: float = None, leverage: int = None, fee_rate: float = None) -> Dict:
    """计算盈亏"""
    # 使用文件中的配置或全局配置
    cap = capital if capital is not None else CAPITAL
    lev = leverage if leverage is not None else LEVERAGE
    fee = fee_rate if fee_rate is not None else FEE_RATE

    entry_price = spike.get('entry_price', 0)
    direction = spike.get('direction', '')
    exit_price = exit_info.get('exit_price', entry_price)

    if not entry_price or not exit_price:
        return {'pnl_usd': 0, 'pnl_percent': 0, 'fee_usd': 0, 'net_pnl_usd': 0}

    # 计算价格变化率
    if direction == 'UP':
        price_change_pct = (exit_price - entry_price) / entry_price
    else:
        price_change_pct = (entry_price - exit_price) / entry_price

    # 杠杆后盈亏
    pnl_pct = price_change_pct * lev
    pnl_usd = cap * pnl_pct / 100

    # 手续费
    notional = cap * lev
    fee_usd = notional * fee * 2

    # 净盈亏
    net_pnl_usd = pnl_usd - fee_usd
    net_pnl_percent = net_pnl_usd / cap * 100

    return {
        'pnl_usd': pnl_usd,
        'pnl_percent': pnl_pct,
        'fee_usd': fee_usd,
        'net_pnl_usd': net_pnl_usd,
        'net_pnl_percent': net_pnl_percent
    }


def analyze_single_trade(spike: Dict, tp: float = 0.5, sl: float = 0.3) -> Dict:
    """
    分析单笔交易的所有指标（兼容旧数据格式）
    """
    # 获取信号中的配置 (兼容旧数据格式)
    spike_config = spike.get('_config', {})

    # 安全获取配置值：优先使用文件中的配置，否则使用全局默认值
    def safe_get_config(key, default):
        if spike_config and key in spike_config:
            return spike_config[key]
        return default

    config_capital = safe_get_config('capital', CAPITAL)
    config_leverage = safe_get_config('leverage', LEVERAGE)
    config_fee_rate = safe_get_config('fee_rate', FEE_RATE)
    config_tracking_seconds = safe_get_config('tracking_seconds', 90)

    # 基本信息
    signal_id = spike.get('id', '')[:12]
    symbol = spike.get('symbol', '')
    direction = spike.get('direction', '')
    detected_at = spike.get('detected_at', '')

    # 价格信息
    start_price = spike.get('start_price', 0)
    peak_price = spike.get('peak_price', 0)
    entry_price = spike.get('entry_price', 0)
    amplitude = spike.get('amplitude_percent', 0)
    retracement = spike.get('retracement_percent', 0)
    duration_ms = spike.get('duration_ms', 0)

    # 持续时间信息 (兼容旧数据格式)
    duration_info = spike.get('duration_info', {})
    if duration_info:
        # 新数据格式：有 duration_info 字段
        spike_duration_ms = duration_info.get('spike_duration_ms', duration_ms)
        tracking_duration_seconds = duration_info.get('tracking_duration_seconds', config_tracking_seconds)
        actual_tracking_seconds = duration_info.get('actual_tracking_seconds', 0)
    else:
        # 旧数据格式：从现有字段推断
        spike_duration_ms = duration_ms
        tracking_duration_seconds = config_tracking_seconds
        prices_after = spike.get('prices_after', [])
        # 假设100ms采样间隔
        actual_tracking_seconds = len(prices_after) * 0.1 if prices_after else 0

    # 趋势预测
    trend = predict_trend(spike)

    # 出场信息 (使用信号中的配置)
    exit_info = get_trade_exit_info(spike, tp, sl, config_capital, config_leverage, config_fee_rate)

    # 盈亏计算 (使用信号中的配置)
    pnl_info = calculate_pnl(spike, exit_info, config_capital, config_leverage, config_fee_rate)

    # 波动率
    prices_after = spike.get('prices_after', [])
    prices_list = [p.get('price', 0) for p in prices_after if p.get('price')]
    volatility = calculate_volatility(prices_list)
    price_range = calculate_price_range(spike)

    # 最大浮盈浮亏
    analysis = spike.get('analysis', {})
    max_profit_pct = analysis.get('max_profit_percent', 0)
    max_loss_pct = analysis.get('max_loss_percent', 0)
    max_profit_time = analysis.get('max_profit_time_ms', 0)
    max_loss_time = analysis.get('max_loss_time_ms', 0)

    # 交易方向
    trade_direction = "LONG (做多)" if direction == 'UP' else "SHORT (做空)"

    # 信号质量评分
    quality_score = 0
    if amplitude >= 0.5: quality_score += 20
    if amplitude >= 1.0: quality_score += 10
    if retracement >= 30: quality_score += 20
    if retracement >= 50: quality_score += 10
    if max_profit_pct > 0.1: quality_score += 20
    if max_profit_pct > 0.3: quality_score += 20

    return {
        # 基本信息
        'id': signal_id,
        'symbol': symbol,
        'time': detected_at,
        'direction': direction,
        'trade_direction': trade_direction,

        # 价格信息
        'start_price': start_price,
        'peak_price': peak_price,
        'entry_price': entry_price,
        'exit_price': exit_info.get('exit_price'),
        'exit_reason': exit_info.get('exit_reason'),
        'hold_time_ms': exit_info.get('exit_time_ms'),
        'hold_time_sec': exit_info.get('exit_time_ms', 0) / 1000,

        # 持续时间信息
        'spike_duration_ms': spike_duration_ms,
        'spike_duration_sec': spike_duration_ms / 1000,
        'tracking_duration_seconds': tracking_duration_seconds,
        'actual_tracking_seconds': actual_tracking_seconds,

        # 波动指标
        'amplitude_pct': amplitude,
        'retracement_pct': retracement,
        'volatility_pct': volatility,
        'price_range_pct': price_range,
        'duration_ms': duration_ms,

        # 配置参数
        'config_capital': config_capital,
        'config_leverage': config_leverage,
        'config_fee_rate': config_fee_rate,

        # 盈亏指标
        'max_profit_pct': max_profit_pct,
        'max_loss_pct': max_loss_pct,
        'max_profit_time_ms': max_profit_time,
        'max_loss_time_ms': max_loss_time,
        'actual_pnl_usd': pnl_info['net_pnl_usd'],
        'actual_pnl_pct': pnl_info['net_pnl_percent'],
        'fee_usd': pnl_info['fee_usd'],

        # 趋势预测
        'signal_direction': trend['signal_direction'],
        'expected_move': trend['expected_move'],
        'trend_strength': trend['strength'],
        'retracement_quality': trend['retracement_quality'],
        'prediction': trend['prediction'],

        # 质量评分
        'quality_score': quality_score,

        # 止盈止损价格
        'tp_price': exit_info.get('tp_price'),
        'sl_price': exit_info.get('sl_price'),
    }


def print_detailed_trade_table(spikes: List[Dict], tp: float = 0.5, sl: float = 0.3, limit: int = 20):
    """打印详细的交易表格"""
    print("\n" + "=" * 155)
    print(f"详细交易分析表 (TP={tp}%, SL={sl}%)")
    print("=" * 155)

    # 表头 - 添加持续时间列
    header = f"{'时间':>16} {'交易对':>10} {'方向':>6} {'入场价':>11} {'出场价':>11} {'出场':>6} "
    header += f"{'持仓':>7} {'插针时长':>10} {'跟踪时长':>10} {'盈亏U':>8} {'盈亏%':>7} {'幅度%':>7} "
    header += f"{'波动%':>7} {'趋势':>4} {'预测':>12} {'质量':>4}"
    print(header)
    print("-" * 155)

    trades = []
    for spike in spikes:
        trade = analyze_single_trade(spike, tp, sl)
        trades.append(trade)

    # 按盈亏排序
    trades.sort(key=lambda x: x['actual_pnl_usd'], reverse=True)

    for i, trade in enumerate(trades[:limit]):
        time_str = trade['time'][:19] if trade['time'] else 'N/A'
        direction_icon = "做多" if trade['direction'] == 'UP' else "做空"

        pnl_color = "+" if trade['actual_pnl_usd'] >= 0 else ""
        exit_icon = {"TP": "止盈", "SL": "止损", "TIMEOUT": "超时"}.get(trade['exit_reason'], "?")

        print(f"{time_str:>16} {trade['symbol']:>10} {direction_icon:>4} "
              f"{trade['entry_price']:>11.6f} {trade['exit_price']:>11.6f} {exit_icon:>4} "
              f"{trade['hold_time_sec']:>5.1f}s "
              f"{trade['spike_duration_sec']:>5.2f}s "
              f"{trade['actual_tracking_seconds']:>5.0f}s "
              f"{pnl_color}{trade['actual_pnl_usd']:>7.2f} "
              f"{pnl_color}{trade['actual_pnl_pct']:>5.2f}% "
              f"{trade['amplitude_pct']:>6.2f}% {trade['volatility_pct']:>6.3f}% "
              f"{trade['trend_strength']:>4} {trade['prediction']:>12} {trade['quality_score']:>3}")

    print("-" * 155)

    # 汇总统计
    total_pnl = sum(t['actual_pnl_usd'] for t in trades)
    wins = sum(1 for t in trades if t['actual_pnl_usd'] > 0)
    losses = sum(1 for t in trades if t['actual_pnl_usd'] < 0)
    avg_pnl = total_pnl / len(trades) if trades else 0
    win_rate = wins / len(trades) * 100 if trades else 0

    print(f"\n汇总统计 (共{len(trades)}笔):")
    print(f"  总盈亏: {total_pnl:+.2f} USDT  |  胜率: {win_rate:.1f}%  |  平均盈亏: {avg_pnl:+.2f} USDT")
    print(f"  盈利: {wins}笔  |  亏损: {losses}笔  |  保本: {len(trades)-wins-losses}笔")


def print_trend_analysis(spikes: List[Dict]):
    """打印趋势分析"""
    print("\n" + "=" * 80)
    print("趋势预测分析")
    print("=" * 80)

    # 按预测分组
    predictions = defaultdict(lambda: {'count': 0, 'total_pnl': 0, 'wins': 0})
    strengths = defaultdict(lambda: {'count': 0, 'avg_profit': 0})

    for spike in spikes:
        trade = analyze_single_trade(spike)

        pred = trade['prediction']
        strength = trade['trend_strength']

        predictions[pred]['count'] += 1
        predictions[pred]['total_pnl'] += trade['actual_pnl_usd']
        if trade['actual_pnl_usd'] > 0:
            predictions[pred]['wins'] += 1

        strengths[strength]['count'] += 1
        strengths[strength]['avg_profit'] += trade['max_profit_pct']

    print("\n预测准确度统计:")
    print(f"{'预测':>15} {'数量':>6} {'胜率':>8} {'平均盈亏':>12}")
    print("-" * 50)

    for pred, stats in sorted(predictions.items(), key=lambda x: x[1]['total_pnl'], reverse=True):
        win_rate = stats['wins'] / stats['count'] * 100 if stats['count'] > 0 else 0
        avg_pnl = stats['total_pnl'] / stats['count'] if stats['count'] > 0 else 0
        print(f"{pred:>15} {stats['count']:>6} {win_rate:>7.1f}% {avg_pnl:>+11.2f}U")

    print("\n趋势强度与盈利空间:")
    print(f"{'强度':>10} {'数量':>6} {'平均最大盈利%':>15}")
    print("-" * 35)

    for strength, stats in strengths.items():
        avg_profit = stats['avg_profit'] / stats['count'] if stats['count'] > 0 else 0
        print(f"{strength:>10} {stats['count']:>6} {avg_profit:>14.3f}%")

    # 方向分析
    up_trades = [analyze_single_trade(s) for s in spikes if s.get('direction') == 'UP']
    down_trades = [analyze_single_trade(s) for s in spikes if s.get('direction') == 'DOWN']

    print("\n按方向对比:")
    print(f"{'方向':>10} {'数量':>6} {'平均盈亏U':>12} {'胜率':>8} {'平均幅度%':>12}")
    print("-" * 55)

    for direction_name, trades in [("做多 (UP)", up_trades), ("做空 (DOWN)", down_trades)]:
        if trades:
            total_pnl = sum(t['actual_pnl_usd'] for t in trades)
            wins = sum(1 for t in trades if t['actual_pnl_usd'] > 0)
            avg_pnl = total_pnl / len(trades)
            win_rate = wins / len(trades) * 100
            avg_amp = sum(t['amplitude_pct'] for t in trades) / len(trades)
            print(f"{direction_name:>10} {len(trades):>6} {avg_pnl:>+11.2f} {win_rate:>7.1f}% {avg_amp:>11.2f}%")


def print_volatility_analysis(spikes: List[Dict]):
    """打印波动率分析"""
    print("\n" + "=" * 80)
    print("波动率分析")
    print("=" * 80)

    trades = [analyze_single_trade(s) for s in spikes]

    # 按波动率分组
    volatility_groups = {
        '低波动 (<0.05%)': [],
        '中波动 (0.05-0.1%)': [],
        '高波动 (0.1-0.2%)': [],
        '极高波动 (>0.2%)': []
    }

    for trade in trades:
        vol = trade['volatility_pct']
        if vol < 0.05:
            volatility_groups['低波动 (<0.05%)'].append(trade)
        elif vol < 0.1:
            volatility_groups['中波动 (0.05-0.1%)'].append(trade)
        elif vol < 0.2:
            volatility_groups['高波动 (0.1-0.2%)'].append(trade)
        else:
            volatility_groups['极高波动 (>0.2%)'].append(trade)

    print(f"{'波动率分组':>20} {'数量':>6} {'平均盈亏U':>12} {'胜率':>8} {'平均幅度%':>12}")
    print("-" * 65)

    for group_name, group_trades in volatility_groups.items():
        if group_trades:
            total_pnl = sum(t['actual_pnl_usd'] for t in group_trades)
            wins = sum(1 for t in group_trades if t['actual_pnl_usd'] > 0)
            avg_pnl = total_pnl / len(group_trades)
            win_rate = wins / len(group_trades) * 100
            avg_amp = sum(t['amplitude_pct'] for t in group_trades) / len(group_trades)
            print(f"{group_name:>20} {len(group_trades):>6} {avg_pnl:>+11.2f} {win_rate:>7.1f}% {avg_amp:>11.2f}%")


def print_time_analysis(spikes: List[Dict]):
    """打印时间分析"""
    print("\n" + "=" * 80)
    print("时间分析")
    print("=" * 80)

    trades = [analyze_single_trade(s) for s in spikes]

    # 持仓时间分析
    hold_times = [t['hold_time_sec'] for t in trades]
    avg_hold = np.mean(hold_times) if hold_times else 0
    max_hold = np.max(hold_times) if hold_times else 0
    min_hold = np.min(hold_times) if hold_times else 0

    print(f"\n持仓时间统计:")
    print(f"  平均: {avg_hold:.2f}秒")
    print(f"  最长: {max_hold:.2f}秒")
    print(f"  最短: {min_hold:.2f}秒")

    # 达到最大盈利时间
    profit_times = [t['max_profit_time_ms'] / 1000 for t in trades if t['max_profit_time_ms'] > 0]
    if profit_times:
        print(f"\n达到最大盈利时间:")
        print(f"  平均: {np.mean(profit_times):.2f}秒")
        print(f"  中位数: {np.median(profit_times):.2f}秒")

    # 按时间段分组
    time_groups = {
        '快速 (<10s)': [],
        '正常 (10-30s)': [],
        '较慢 (30-60s)': [],
        '超慢 (>60s)': []
    }

    for trade in trades:
        ht = trade['hold_time_sec']
        if ht < 10:
            time_groups['快速 (<10s)'].append(trade)
        elif ht < 30:
            time_groups['正常 (10-30s)'].append(trade)
        elif ht < 60:
            time_groups['较慢 (30-60s)'].append(trade)
        else:
            time_groups['超慢 (>60s)'].append(trade)

    print(f"\n按持仓时间分组:")
    print(f"{'分组':>15} {'数量':>6} {'平均盈亏U':>12} {'胜率':>8}")
    print("-" * 50)

    for group_name, group_trades in time_groups.items():
        if group_trades:
            total_pnl = sum(t['actual_pnl_usd'] for t in group_trades)
            wins = sum(1 for t in group_trades if t['actual_pnl_usd'] > 0)
            avg_pnl = total_pnl / len(group_trades)
            win_rate = wins / len(group_trades) * 100
            print(f"{group_name:>15} {len(group_trades):>6} {avg_pnl:>+11.2f} {win_rate:>7.1f}%")


def export_detailed_csv(spikes: List[Dict], tp: float = 0.5, sl: float = 0.3):
    """导出详细数据到CSV"""
    trades = []
    for spike in spikes:
        trade = analyze_single_trade(spike, tp, sl)
        trades.append(trade)

    df = pd.DataFrame(trades)

    # 选择重要列 - 添加持续时间和配置列
    columns = [
        'time', 'symbol', 'direction', 'trade_direction',
        'start_price', 'peak_price', 'entry_price', 'exit_price',
        'exit_reason', 'hold_time_sec', 'spike_duration_sec', 'tracking_duration_seconds', 'actual_tracking_seconds',
        'actual_pnl_usd', 'actual_pnl_pct', 'fee_usd',
        'max_profit_pct', 'max_loss_pct', 'amplitude_pct', 'retracement_pct',
        'volatility_pct', 'price_range_pct',
        'signal_direction', 'expected_move', 'trend_strength',
        'retracement_quality', 'prediction', 'quality_score',
        'config_capital', 'config_leverage', 'config_fee_rate'
    ]

    # 重命名列
    column_names = {
        'time': '时间',
        'symbol': '交易对',
        'direction': '信号方向',
        'trade_direction': '交易方向',
        'start_price': '起始价',
        'peak_price': '峰值价',
        'entry_price': '入场价',
        'exit_price': '出场价',
        'exit_reason': '出场原因',
        'hold_time_sec': '持仓秒数',
        'spike_duration_sec': '插针持续秒数',
        'tracking_duration_seconds': '跟踪时长配置秒数',
        'actual_tracking_seconds': '实际跟踪秒数',
        'actual_pnl_usd': '盈亏USDT',
        'actual_pnl_pct': '盈亏%',
        'fee_usd': '手续费',
        'max_profit_pct': '最大盈利%',
        'max_loss_pct': '最大亏损%',
        'amplitude_pct': '插针幅度%',
        'retracement_pct': '回撤%',
        'volatility_pct': '波动率%',
        'price_range_pct': '价格范围%',
        'signal_direction': '信号方向描述',
        'expected_move': '预期走势',
        'trend_strength': '趋势强度',
        'retracement_quality': '回撤质量',
        'prediction': '预测结果',
        'quality_score': '质量评分',
        'config_capital': '配置本金',
        'config_leverage': '配置杠杆',
        'config_fee_rate': '配置手续费率'
    }

    df_export = df[columns].copy()
    df_export.rename(columns=column_names, inplace=True)

    # 保存
    output_file = DATA_DIR / f"detailed_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    df_export.to_csv(output_file, index=False, encoding='utf-8-sig')
    print(f"\n详细数据已导出: {output_file}")


def detailed_analysis():
    """详细分析模式"""
    spikes = load_spike_files()

    if not spikes:
        print("没有数据可分析")
        return

    print(f"\n共加载 {len(spikes)} 个信号")

    # 打印配置信息
    print_config_header(SESSION_CONFIG)

    # 选择止盈止损参数
    print("\n默认止盈止损: TP=0.5%, SL=0.3%")
    custom = input("使用自定义参数? (y/n, 默认n): ").strip().lower()

    tp, sl = 0.5, 0.3
    if custom == 'y':
        try:
            tp = float(input("止盈百分比 (如0.5): ").strip() or "0.5")
            sl = float(input("止损百分比 (如0.3): ").strip() or "0.3")
        except:
            print("输入无效，使用默认值")

    # 打印详细交易表
    print_detailed_trade_table(spikes, tp, sl, limit=50)

    # 趋势分析
    print_trend_analysis(spikes)

    # 波动率分析
    print_volatility_analysis(spikes)

    # 时间分析
    print_time_analysis(spikes)

    # 导出CSV
    export = input("\n是否导出详细CSV? (y/n, 默认y): ").strip().lower() or 'y'
    if export == 'y':
        export_detailed_csv(spikes, tp, sl)


def main():
    print("=" * 80)
    print("插针信号数据分析 - 增强版")
    print("=" * 80)

    # 检查数据目录
    if not DATA_DIR.exists():
        print(f"\n数据目录不存在: {DATA_DIR}")
        print("请先运行 test_pin_recorder.py 收集数据")
        return

    # 统计文件
    json_count = len(list(DATA_DIR.glob("spike_*.json")))
    csv_count = len(list(DATA_DIR.glob("summary_*.csv")))

    print(f"\n数据文件:")
    print(f"  JSON文件: {json_count} 个")
    print(f"  CSV文件: {csv_count} 个")

    if json_count == 0 and csv_count == 0:
        print("\n没有数据可分析")
        return

    # 选择分析方式
    print("\n分析方式:")
    print("  1. 详细分析 (包含趋势预测、入场出场价、波动率等)")
    print("  2. CSV快速分析")
    print("  3. 交互式分析")

    choice = input("\n选择 (1-3, 默认1): ").strip() or "1"

    if choice == "1":
        detailed_analysis()
    elif choice == "2":
        from analyze_signals import analyze_csv
        analyze_csv()
    elif choice == "3":
        from analyze_signals import interactive_analysis
        interactive_analysis()
    else:
        detailed_analysis()

    print("\n" + "=" * 80)
    print("分析完成")
    print("=" * 80)


if __name__ == "__main__":
    main()
