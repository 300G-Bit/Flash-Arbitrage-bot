#!/usr/bin/env python3
"""
Flash Arbitrage Bot - 插针信号数据记录与分析系统

功能:
1. 实时检测插针信号
2. 记录信号前后的价格数据
3. 分析信号的盈利/亏损情况
4. 生成统计报告

作者: Flash Arbitrage Bot Team
"""

import os
import sys
import json
import time
import threading
import requests
import websocket
import csv
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Deque, Tuple
from dataclasses import dataclass, field, asdict
from collections import deque
from pathlib import Path

# ============== 代理配置 ==============
PROXY_HOST = "127.0.0.1"
PROXY_HTTP_PORT = 7897
USE_PROXY = True

HTTP_PROXY = {
    'http': f'http://{PROXY_HOST}:{PROXY_HTTP_PORT}',
    'https': f'http://{PROXY_HOST}:{PROXY_HTTP_PORT}',
} if USE_PROXY else {}

# ============== 时区配置 ==============
BEIJING_TZ = timezone(timedelta(hours=8))

def get_beijing_time() -> datetime:
    return datetime.now(BEIJING_TZ)

def format_time(dt: datetime = None) -> str:
    if dt is None:
        dt = get_beijing_time()
    return dt.strftime("%H:%M:%S.%f")[:-3]

def format_datetime(dt: datetime = None) -> str:
    if dt is None:
        dt = get_beijing_time()
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

# ============== 交易参数配置 ==============
TRADING_CONFIG = {
    "capital": 15.0,              # 本金 15 USDT
    "leverage": 20,               # 杠杆倍数
    "fee_rate": 0.0004,           # 手续费率 0.04% (taker)
    "slippage": 0.0001,           # 滑点估算 0.01%
    
    # 止盈止损参数 (测试不同档位)
    "take_profit_levels": [2.0, 3.0, 4.0, 5.0, 6.0, 8.0],  # 止盈百分比
    "stop_loss_levels": [1.0, 1.5, 2.0, 2.5, 3.0],        # 止损百分比
    
    # 默认止盈止损
    "default_tp": 3.0,            # 默认止盈 3.0%
    "default_sl": 1.5,            # 默认止损 1.5%
}

# ============== API配置 ==============
REST_ENDPOINTS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
]

WS_ENDPOINTS = [
    "wss://fstream.binance.com/ws",
    "wss://fstream1.binance.com/ws",
]

CURRENT_REST_ENDPOINT = REST_ENDPOINTS[0]

# 监控配置
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT","TRUMPUSDT",
                   "ZECUSDT","VVVUSDT","TAOUSDT","RIVERUSDT","POLUSDT",
                   "币安人生USDT","BREVUSDT","MIRAUSDT","COLLECTUSDT",
                   "4USDT","BUSDT","CCUSDT","GUNUSDT","AAVEUSDT"]
MONITOR_DURATION = 3600  # 1小时

# 数据记录配置
DATA_CONFIG = {
    "price_history_seconds": 60,   # 记录信号前60秒的价格
    "tracking_seconds": 90,        # 信号后跟踪90秒
    "tracking_interval_ms": 100,   # 跟踪间隔100ms
}

# 插针检测参数
REALTIME_CONFIG = {
    "price_window_ms": 1000,
    "min_spike_percent": 0.3,
    "max_spike_percent": 5.0,
    "retracement_percent": 30,
}

# 数据保存目录
DATA_DIR = Path("pin_data")
DATA_DIR.mkdir(exist_ok=True)


# ============== 数据类 ==============

@dataclass
class TickData:
    """Tick数据"""
    timestamp: datetime
    price: float
    
    def to_dict(self):
        return {
            "timestamp": format_datetime(self.timestamp),
            "timestamp_ms": int(self.timestamp.timestamp() * 1000),
            "price": self.price
        }


@dataclass
class PriceSpike:
    """插针信号"""
    id: str                       # 唯一ID
    detected_at: datetime         # 检测时间
    symbol: str
    direction: str                # UP / DOWN
    start_price: float            # 窗口起始价
    peak_price: float             # 峰值价格
    current_price: float          # 当前价格(检测时)
    amplitude_percent: float      # 幅度
    retracement_percent: float    # 回撤比例
    duration_ms: int
    confirmed: bool
    
    # 交易相关
    entry_price: float = 0.0      # 入场价格 (检测时价格)
    
    # 后续跟踪数据
    prices_before: List[TickData] = field(default_factory=list)   # 信号前价格
    prices_after: List[TickData] = field(default_factory=list)    # 信号后价格
    
    # 分析结果
    max_profit_percent: float = 0.0      # 最大浮盈
    max_loss_percent: float = 0.0        # 最大浮亏
    max_profit_time_ms: int = 0          # 达到最大浮盈的时间
    max_loss_time_ms: int = 0            # 达到最大浮亏的时间
    final_price: float = 0.0             # 跟踪结束价格
    final_pnl_percent: float = 0.0       # 最终盈亏
    
    # 不同止盈止损的结果
    tp_sl_results: Dict = field(default_factory=dict)
    
    def __str__(self):
        icon = "🔺" if self.direction == "UP" else "🔻"
        return (f"{format_time(self.detected_at)} {self.symbol:10s} {icon} "
                f"幅度:{self.amplitude_percent:5.2f}% 入场:{self.entry_price:.4f}")


@dataclass
class TradingResult:
    """交易结果"""
    tp_percent: float             # 止盈设置
    sl_percent: float             # 止损设置
    result: str                   # "TP" / "SL" / "TIMEOUT"
    exit_price: float             # 退出价格
    exit_time_ms: int             # 退出时间(相对信号)
    pnl_percent: float            # 盈亏百分比
    pnl_usdt: float               # 盈亏金额


@dataclass
class SymbolMonitor:
    """交易对监控"""
    symbol: str
    current_price: float = 0.0
    price_history: Deque[TickData] = field(default_factory=lambda: deque(maxlen=5000))
    window_high: float = 0.0
    window_low: float = float('inf')
    window_start_price: float = 0.0
    window_start_time: datetime = None
    spike_count: int = 0
    spikes: List[PriceSpike] = field(default_factory=list)
    last_update: datetime = None
    tick_count: int = 0
    
    # 正在跟踪的信号
    tracking_spikes: List[PriceSpike] = field(default_factory=list)


# ============== 交易计算 ==============

def calculate_position_size(capital: float, leverage: int, price: float) -> float:
    """计算仓位大小"""
    return (capital * leverage) / price


def calculate_pnl(entry_price: float, exit_price: float, direction: str,
                  capital: float, leverage: int, fee_rate: float) -> Tuple[float, float]:
    """
    计算盈亏
    
    Returns:
        (pnl_percent, pnl_usdt)
    """
    # 方向: UP插针后做空, DOWN插针后做多
    if direction == "UP":
        # 做空: 价格下跌盈利
        price_change_percent = (entry_price - exit_price) / entry_price * 100
    else:
        # 做多: 价格上涨盈利
        price_change_percent = (exit_price - entry_price) / entry_price * 100
    
    # 杠杆放大
    pnl_percent = price_change_percent * leverage
    
    # 手续费 (开仓+平仓)
    fee_percent = fee_rate * 2 * 100 * leverage
    
    # 净盈亏
    net_pnl_percent = pnl_percent - fee_percent
    net_pnl_usdt = capital * net_pnl_percent / 100
    
    return net_pnl_percent, net_pnl_usdt


def simulate_trade(spike: PriceSpike, tp_percent: float, sl_percent: float) -> TradingResult:
    """
    模拟交易，计算在指定止盈止损下的结果
    
    Args:
        spike: 插针信号
        tp_percent: 止盈百分比
        sl_percent: 止损百分比
    
    Returns:
        TradingResult
    """
    entry_price = spike.entry_price
    direction = spike.direction
    capital = TRADING_CONFIG["capital"]
    leverage = TRADING_CONFIG["leverage"]
    fee_rate = TRADING_CONFIG["fee_rate"]
    
    # 计算止盈止损价格
    if direction == "UP":
        # 做空: 止盈价 < 入场价, 止损价 > 入场价
        tp_price = entry_price * (1 - tp_percent / 100 / leverage)
        sl_price = entry_price * (1 + sl_percent / 100 / leverage)
    else:
        # 做多: 止盈价 > 入场价, 止损价 < 入场价
        tp_price = entry_price * (1 + tp_percent / 100 / leverage)
        sl_price = entry_price * (1 - sl_percent / 100 / leverage)
    
    # 遍历后续价格，检查是否触发止盈止损
    result = "TIMEOUT"
    exit_price = spike.final_price if spike.final_price > 0 else entry_price
    exit_time_ms = DATA_CONFIG["tracking_seconds"] * 1000
    
    start_time = spike.detected_at
    
    for tick in spike.prices_after:
        time_ms = int((tick.timestamp - start_time).total_seconds() * 1000)
        price = tick.price
        
        if direction == "UP":
            # 做空
            if price <= tp_price:
                result = "TP"
                exit_price = tp_price
                exit_time_ms = time_ms
                break
            elif price >= sl_price:
                result = "SL"
                exit_price = sl_price
                exit_time_ms = time_ms
                break
        else:
            # 做多
            if price >= tp_price:
                result = "TP"
                exit_price = tp_price
                exit_time_ms = time_ms
                break
            elif price <= sl_price:
                result = "SL"
                exit_price = sl_price
                exit_time_ms = time_ms
                break
    
    # 计算盈亏
    pnl_percent, pnl_usdt = calculate_pnl(
        entry_price, exit_price, direction, capital, leverage, fee_rate
    )
    
    return TradingResult(
        tp_percent=tp_percent,
        sl_percent=sl_percent,
        result=result,
        exit_price=exit_price,
        exit_time_ms=exit_time_ms,
        pnl_percent=pnl_percent,
        pnl_usdt=pnl_usdt
    )


def analyze_spike(spike: PriceSpike):
    """分析单个插针信号"""
    if not spike.prices_after:
        return
    
    entry_price = spike.entry_price
    direction = spike.direction
    start_time = spike.detected_at
    
    max_profit = 0.0
    max_loss = 0.0
    max_profit_time = 0
    max_loss_time = 0
    
    for tick in spike.prices_after:
        time_ms = int((tick.timestamp - start_time).total_seconds() * 1000)
        price = tick.price
        
        if direction == "UP":
            # 做空: 价格下跌是盈利
            change = (entry_price - price) / entry_price * 100
        else:
            # 做多: 价格上涨是盈利
            change = (price - entry_price) / entry_price * 100
        
        if change > max_profit:
            max_profit = change
            max_profit_time = time_ms
        if change < max_loss:
            max_loss = change
            max_loss_time = time_ms
    
    spike.max_profit_percent = max_profit
    spike.max_loss_percent = max_loss
    spike.max_profit_time_ms = max_profit_time
    spike.max_loss_time_ms = max_loss_time
    
    if spike.prices_after:
        spike.final_price = spike.prices_after[-1].price
        if direction == "UP":
            spike.final_pnl_percent = (entry_price - spike.final_price) / entry_price * 100
        else:
            spike.final_pnl_percent = (spike.final_price - entry_price) / entry_price * 100
    
    # 测试不同止盈止损组合
    spike.tp_sl_results = {}
    for tp in TRADING_CONFIG["take_profit_levels"]:
        for sl in TRADING_CONFIG["stop_loss_levels"]:
            key = f"TP{tp}_SL{sl}"
            result = simulate_trade(spike, tp, sl)
            spike.tp_sl_results[key] = {
                "result": result.result,
                "exit_time_ms": result.exit_time_ms,
                "pnl_percent": result.pnl_percent,
                "pnl_usdt": result.pnl_usdt
            }


# ============== 数据保存 ==============

def get_session_config():
    """获取当前会话的配置参数"""
    return {
        "trading": TRADING_CONFIG.copy(),
        "data": DATA_CONFIG.copy(),
        "detection": REALTIME_CONFIG.copy(),
        "symbols": DEFAULT_SYMBOLS.copy(),
        "session_start": format_datetime(get_beijing_time())
    }


def save_session_config():
    """保存会话配置到文件"""
    config = get_session_config()
    filename = DATA_DIR / "session_config.json"

    # 如果已有配置文件，先读取历史记录
    history = []
    if filename.exists():
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
                history = data.get('history', [])
        except:
            pass

    # 添加当前配置到历史
    history.append(config)

    # 保存
    save_data = {
        'current': config,
        'history': history[-10:],  # 只保留最近10次
        'history_count': len(history)
    }

    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(save_data, f, indent=2, ensure_ascii=False)

    return filename


def save_spike_data(spike: PriceSpike):
    """保存单个信号的详细数据"""
    filename = DATA_DIR / f"spike_{spike.id}.json"

    data = {
        # 会话配置参数
        "_config": {
            "capital": TRADING_CONFIG["capital"],
            "leverage": TRADING_CONFIG["leverage"],
            "fee_rate": TRADING_CONFIG["fee_rate"],
            "take_profit_levels": TRADING_CONFIG["take_profit_levels"],
            "stop_loss_levels": TRADING_CONFIG["stop_loss_levels"],
            "default_tp": TRADING_CONFIG["default_tp"],
            "default_sl": TRADING_CONFIG["default_sl"],
            "tracking_seconds": DATA_CONFIG["tracking_seconds"],
            "tracking_interval_ms": DATA_CONFIG["tracking_interval_ms"],
            "min_spike_percent": REALTIME_CONFIG["min_spike_percent"],
            "retracement_percent": REALTIME_CONFIG["retracement_percent"],
        },

        # 信号数据
        "id": spike.id,
        "detected_at": format_datetime(spike.detected_at),
        "symbol": spike.symbol,
        "direction": spike.direction,
        "start_price": spike.start_price,
        "peak_price": spike.peak_price,
        "entry_price": spike.entry_price,
        "amplitude_percent": spike.amplitude_percent,
        "retracement_percent": spike.retracement_percent,
        "confirmed": spike.confirmed,

        # 持续时间信息
        "duration_info": {
            "spike_duration_ms": spike.duration_ms,  # 插针形成时间
            "tracking_duration_seconds": DATA_CONFIG["tracking_seconds"],  # 跟踪时长
            "actual_tracking_seconds": len(spike.prices_after) * DATA_CONFIG["tracking_interval_ms"] / 1000,  # 实际跟踪秒数
        },

        "analysis": {
            "max_profit_percent": spike.max_profit_percent,
            "max_loss_percent": spike.max_loss_percent,
            "max_profit_time_ms": spike.max_profit_time_ms,
            "max_loss_time_ms": spike.max_loss_time_ms,
            "final_price": spike.final_price,
            "final_pnl_percent": spike.final_pnl_percent,
        },

        "tp_sl_results": spike.tp_sl_results,

        "prices_before": [t.to_dict() for t in spike.prices_before[-100:]],
        "prices_after": [t.to_dict() for t in spike.prices_after[-600:]],
    }

    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return filename


def save_summary_csv(spikes: List[PriceSpike]):
    """保存汇总CSV"""
    if not spikes:
        return
    
    filename = DATA_DIR / f"summary_{get_beijing_time().strftime('%Y%m%d_%H%M%S')}.csv"
    
    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        
        # 表头
        header = [
            "ID", "时间", "交易对", "方向", "幅度%", "回撤%", "入场价",
            "最大盈利%", "最大亏损%", "最终盈亏%",
            "盈利时间ms", "亏损时间ms",
        ]
        # 添加不同止盈止损的结果列
        for tp in TRADING_CONFIG["take_profit_levels"][:3]:
            for sl in TRADING_CONFIG["stop_loss_levels"][:3]:
                header.append(f"TP{tp}_SL{sl}_结果")
                header.append(f"TP{tp}_SL{sl}_盈亏")
        
        writer.writerow(header)
        
        # 数据行
        for spike in spikes:
            row = [
                spike.id,
                format_datetime(spike.detected_at),
                spike.symbol,
                spike.direction,
                f"{spike.amplitude_percent:.2f}",
                f"{spike.retracement_percent:.1f}",
                f"{spike.entry_price:.4f}",
                f"{spike.max_profit_percent:.3f}",
                f"{spike.max_loss_percent:.3f}",
                f"{spike.final_pnl_percent:.3f}",
                spike.max_profit_time_ms,
                spike.max_loss_time_ms,
            ]
            
            for tp in TRADING_CONFIG["take_profit_levels"][:3]:
                for sl in TRADING_CONFIG["stop_loss_levels"][:3]:
                    key = f"TP{tp}_SL{sl}"
                    if key in spike.tp_sl_results:
                        r = spike.tp_sl_results[key]
                        row.append(r["result"])
                        row.append(f"{r['pnl_usdt']:.2f}")
                    else:
                        row.extend(["", ""])
            
            writer.writerow(row)
    
    return filename


# ============== 网络函数 ==============

def test_connection() -> bool:
    """测试连接"""
    try:
        response = requests.get(
            f"{REST_ENDPOINTS[0]}/fapi/v1/time",
            timeout=10,
            proxies=HTTP_PROXY if USE_PROXY else None
        )
        return response.status_code == 200
    except:
        return False


def run_websocket_with_proxy(ws):
    """运行WebSocket"""
    if USE_PROXY:
        ws.run_forever(
            http_proxy_host=PROXY_HOST,
            http_proxy_port=PROXY_HTTP_PORT,
            proxy_type="http"
        )
    else:
        ws.run_forever()


def fetch_prices(symbols: List[str]) -> Dict[str, float]:
    """获取价格"""
    prices = {}
    try:
        response = requests.get(
            f"{CURRENT_REST_ENDPOINT}/fapi/v1/ticker/price",
            timeout=10,
            proxies=HTTP_PROXY if USE_PROXY else None
        )
        if response.status_code == 200:
            for item in response.json():
                if item['symbol'] in symbols:
                    prices[item['symbol']] = float(item['price'])
    except:
        pass
    return prices


# ============== 实时检测器 ==============

class DataRecordingDetector:
    """带数据记录功能的插针检测器"""
    
    def __init__(self, symbols: List[str], ws_endpoint: str):
        self.symbols = [s.lower() for s in symbols]
        self.ws_endpoint = ws_endpoint
        self.monitors: Dict[str, SymbolMonitor] = {
            s.upper(): SymbolMonitor(symbol=s.upper()) for s in symbols
        }
        self.ws = None
        self.running = False
        self.start_time = get_beijing_time()
        self.ws_connected = False
        self.message_count = 0
        self.spike_counter = 0
        
        # 所有完成分析的信号
        self.completed_spikes: List[PriceSpike] = []
        self.lock = threading.Lock()
        
    def start(self):
        """启动"""
        self.running = True
        self._connect()
        
        # 启动跟踪线程
        self.tracking_thread = threading.Thread(target=self._tracking_loop)
        self.tracking_thread.daemon = True
        self.tracking_thread.start()
        
    def _connect(self):
        """连接WebSocket"""
        if not self.running:
            return
            
        streams = [f"{s}@aggTrade" for s in self.symbols]
        ws_url = f"{self.ws_endpoint}/{'/'.join(streams)}"
        
        print(f"[{format_time()}] 连接WebSocket...")
        
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open
        )
        
        self.ws_thread = threading.Thread(target=lambda: run_websocket_with_proxy(self.ws))
        self.ws_thread.daemon = True
        self.ws_thread.start()
        
    def stop(self):
        """停止"""
        self.running = False
        if self.ws:
            self.ws.close()
            
    def _on_open(self, ws):
        self.ws_connected = True
        print(f"[{format_time()}] ✅ WebSocket已连接")
            
    def _on_error(self, ws, error):
        if error:
            print(f"[{format_time()}] WebSocket错误: {str(error)[:50]}")
        
    def _on_close(self, ws, code, msg):
        self.ws_connected = False
        if self.running:
            print(f"[{format_time()}] WebSocket断开，重连...")
            time.sleep(2)
            self._connect()
            
    def _on_message(self, ws, message):
        """处理消息"""
        try:
            self.message_count += 1
            data = json.loads(message)
            
            symbol = data.get('s', '').upper()
            if symbol not in self.monitors:
                return
                
            price = float(data['p'])
            timestamp = datetime.fromtimestamp(data['T'] / 1000, tz=BEIJING_TZ)
            
            self._update_price(symbol, price, timestamp)
        except:
            pass
            
    def _update_price(self, symbol: str, price: float, timestamp: datetime):
        """更新价格"""
        monitor = self.monitors[symbol]
        monitor.current_price = price
        monitor.last_update = timestamp
        monitor.tick_count += 1
        
        tick = TickData(timestamp=timestamp, price=price)
        monitor.price_history.append(tick)
        
        # 更新正在跟踪的信号
        for spike in monitor.tracking_spikes:
            spike.prices_after.append(tick)
        
        # 初始化窗口
        if monitor.window_start_time is None:
            monitor.window_start_time = timestamp
            monitor.window_start_price = price
            monitor.window_high = price
            monitor.window_low = price
            return
            
        # 更新高低点
        if price > monitor.window_high:
            monitor.window_high = price
        if price < monitor.window_low:
            monitor.window_low = price
            
        window_ms = (timestamp - monitor.window_start_time).total_seconds() * 1000
        
        # 检测插针
        self._detect_spike(monitor, price, timestamp, window_ms)
        
        # 重置窗口
        if window_ms >= REALTIME_CONFIG["price_window_ms"]:
            monitor.window_start_time = timestamp
            monitor.window_start_price = price
            monitor.window_high = price
            monitor.window_low = price
            
    def _detect_spike(self, monitor: SymbolMonitor, price: float, timestamp: datetime, window_ms: float):
        """检测插针"""
        start = monitor.window_start_price
        high = monitor.window_high
        low = monitor.window_low
        
        if start == 0:
            return
            
        up_amp = (high - start) / start * 100
        down_amp = (start - low) / start * 100
        
        min_amp = REALTIME_CONFIG["min_spike_percent"]
        max_amp = REALTIME_CONFIG["max_spike_percent"]
        ret_threshold = REALTIME_CONFIG["retracement_percent"]
        
        spike = None
        
        # 上插针
        if min_amp <= up_amp <= max_amp and high > start:
            ret = (high - price) / (high - start) * 100
            if ret >= ret_threshold:
                spike = self._create_spike(
                    monitor, timestamp, "UP", start, high, price,
                    up_amp, ret, window_ms, ret >= 50
                )
                
        # 下插针
        if spike is None and min_amp <= down_amp <= max_amp and start > low:
            ret = (price - low) / (start - low) * 100
            if ret >= ret_threshold:
                spike = self._create_spike(
                    monitor, timestamp, "DOWN", start, low, price,
                    down_amp, ret, window_ms, ret >= 50
                )
                
        if spike:
            # 去重
            if monitor.spikes:
                last = monitor.spikes[-1]
                if (spike.detected_at - last.detected_at).total_seconds() < 2:
                    if spike.direction == last.direction:
                        return
            
            # 记录信号前的价格
            history_start = timestamp - timedelta(seconds=DATA_CONFIG["price_history_seconds"])
            spike.prices_before = [
                t for t in monitor.price_history 
                if t.timestamp >= history_start and t.timestamp < timestamp
            ]
            
            monitor.spikes.append(spike)
            monitor.spike_count += 1
            monitor.tracking_spikes.append(spike)
            
            print(f"\n🔔 新信号: {spike}")
            print(f"   开始跟踪 {DATA_CONFIG['tracking_seconds']}秒...")
            
    def _create_spike(self, monitor, timestamp, direction, start, peak, price,
                      amplitude, retracement, duration, confirmed) -> PriceSpike:
        """创建信号"""
        self.spike_counter += 1
        spike_id = f"{monitor.symbol}_{timestamp.strftime('%Y%m%d%H%M%S')}_{self.spike_counter}"
        
        return PriceSpike(
            id=spike_id,
            detected_at=timestamp,
            symbol=monitor.symbol,
            direction=direction,
            start_price=start,
            peak_price=peak,
            current_price=price,
            amplitude_percent=amplitude,
            retracement_percent=retracement,
            duration_ms=int(duration),
            confirmed=confirmed,
            entry_price=price,  # 入场价格 = 检测时价格
        )
    
    def _tracking_loop(self):
        """跟踪循环，检查并完成信号分析"""
        while self.running:
            try:
                now = get_beijing_time()
                
                for monitor in self.monitors.values():
                    completed = []
                    
                    for spike in monitor.tracking_spikes:
                        elapsed = (now - spike.detected_at).total_seconds()
                        
                        if elapsed >= DATA_CONFIG["tracking_seconds"]:
                            # 跟踪完成，分析数据
                            analyze_spike(spike)
                            
                            # 保存数据
                            filename = save_spike_data(spike)
                            
                            with self.lock:
                                self.completed_spikes.append(spike)
                            
                            completed.append(spike)
                            
                            print(f"\n✅ 信号分析完成: {spike.id}")
                            print(f"   最大盈利: {spike.max_profit_percent:.3f}% @ {spike.max_profit_time_ms}ms")
                            print(f"   最大亏损: {spike.max_loss_percent:.3f}% @ {spike.max_loss_time_ms}ms")
                            print(f"   最终盈亏: {spike.final_pnl_percent:.3f}%")
                            print(f"   数据保存: {filename}")
                    
                    # 移除已完成的
                    for spike in completed:
                        monitor.tracking_spikes.remove(spike)
                        
            except Exception as e:
                print(f"跟踪错误: {e}")
            
            time.sleep(1)
    
    def get_stats(self) -> Dict:
        """获取统计"""
        return {
            symbol: {
                "price": m.current_price,
                "spikes": m.spike_count,
                "tracking": len(m.tracking_spikes),
                "tick_count": m.tick_count,
            }
            for symbol, m in self.monitors.items()
        }
    
    def is_connected(self) -> bool:
        return self.ws_connected
    
    def get_completed_spikes(self) -> List[PriceSpike]:
        with self.lock:
            return list(self.completed_spikes)


# ============== 显示函数 ==============

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def print_header():
    now = get_beijing_time()
    print("=" * 85)
    print("              Flash Arbitrage Bot - 插针信号数据记录与分析")
    print(f"                      {now.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
    print("=" * 85)


def print_stats(detector: DataRecordingDetector):
    """打印统计"""
    elapsed = (get_beijing_time() - detector.start_time).total_seconds()
    minutes, seconds = int(elapsed // 60), int(elapsed % 60)
    
    status = "🟢 已连接" if detector.is_connected() else "🔴 断开"
    
    print(f"\n📊 运行: {minutes}分{seconds}秒 | {status} | 消息: {detector.message_count}")
    print("-" * 85)
    print(f"{'交易对':<12} {'价格':>14} {'Ticks':>10} {'检测':>6} {'跟踪中':>8} {'已完成':>8}")
    print("-" * 85)
    
    stats = detector.get_stats()
    completed = detector.get_completed_spikes()
    
    for symbol, data in stats.items():
        price = f"{data['price']:.6f}" if data['price'] > 0 else "等待..."
        completed_count = len([s for s in completed if s.symbol == symbol])
        print(f"{symbol:<12} {price:>14} {data['tick_count']:>10} "
              f"{data['spikes']:>6} {data['tracking']:>8} {completed_count:>8}")
    
    print("-" * 85)


def print_completed_analysis(detector: DataRecordingDetector):
    """打印已完成的分析"""
    completed = detector.get_completed_spikes()
    
    if not completed:
        print("\n📋 暂无已完成的信号分析")
        return
    
    print(f"\n📋 已完成分析的信号 ({len(completed)}个):")
    print("-" * 85)
    
    # 统计汇总
    total_profit = 0
    win_count = 0
    
    default_tp = TRADING_CONFIG["default_tp"]
    default_sl = TRADING_CONFIG["default_sl"]
    key = f"TP{default_tp}_SL{default_sl}"
    
    for spike in completed[-10:]:  # 显示最近10个
        result = spike.tp_sl_results.get(key, {})
        result_str = result.get("result", "N/A")
        pnl = result.get("pnl_usdt", 0)
        
        icon = "✅" if pnl > 0 else "❌" if pnl < 0 else "➖"
        dir_icon = "🔺" if spike.direction == "UP" else "🔻"
        
        print(f"   {icon} {format_time(spike.detected_at)} {spike.symbol:10s} {dir_icon} "
              f"幅度:{spike.amplitude_percent:5.2f}% "
              f"最大盈:{spike.max_profit_percent:6.3f}% "
              f"结果:{result_str:7s} 盈亏:{pnl:+.2f}U")
        
        if pnl > 0:
            win_count += 1
        total_profit += pnl
    
    print("-" * 85)
    
    # 总计
    if completed:
        win_rate = win_count / len(completed) * 100
        print(f"\n📈 汇总 (TP={default_tp}%, SL={default_sl}%):")
        print(f"   信号数: {len(completed)} | 胜率: {win_rate:.1f}% | 总盈亏: {total_profit:+.2f} USDT")


def print_tracking_status(detector: DataRecordingDetector):
    """打印正在跟踪的信号"""
    tracking = []
    for m in detector.monitors.values():
        tracking.extend(m.tracking_spikes)
    
    if not tracking:
        return
    
    print(f"\n⏳ 正在跟踪的信号 ({len(tracking)}个):")
    print("-" * 85)
    
    now = get_beijing_time()
    for spike in tracking:
        elapsed = (now - spike.detected_at).total_seconds()
        remaining = DATA_CONFIG["tracking_seconds"] - elapsed
        
        # 计算当前浮动盈亏
        current_price = detector.monitors[spike.symbol].current_price
        if spike.direction == "UP":
            current_pnl = (spike.entry_price - current_price) / spike.entry_price * 100
        else:
            current_pnl = (current_price - spike.entry_price) / spike.entry_price * 100
        
        dir_icon = "🔺" if spike.direction == "UP" else "🔻"
        pnl_icon = "📈" if current_pnl > 0 else "📉"
        
        print(f"   {dir_icon} {spike.symbol:10s} 入场:{spike.entry_price:.4f} "
              f"现价:{current_price:.4f} {pnl_icon} {current_pnl:+.3f}% "
              f"剩余:{remaining:.0f}秒")
    
    print("-" * 85)


def generate_final_report(detector: DataRecordingDetector):
    """生成最终报告"""
    completed = detector.get_completed_spikes()
    
    if not completed:
        print("\n❌ 没有收集到信号数据")
        return
    
    print("\n" + "=" * 85)
    print("                              最终分析报告")
    print("=" * 85)
    
    # 保存CSV
    csv_file = save_summary_csv(completed)
    print(f"\n📁 数据已保存:")
    print(f"   CSV汇总: {csv_file}")
    print(f"   详细数据: {DATA_DIR}/spike_*.json")
    
    # 按交易对统计
    print(f"\n📊 按交易对统计:")
    print("-" * 85)
    
    symbols = set(s.symbol for s in completed)
    for symbol in symbols:
        symbol_spikes = [s for s in completed if s.symbol == symbol]
        up_count = len([s for s in symbol_spikes if s.direction == "UP"])
        down_count = len([s for s in symbol_spikes if s.direction == "DOWN"])
        avg_amplitude = sum(s.amplitude_percent for s in symbol_spikes) / len(symbol_spikes)
        avg_max_profit = sum(s.max_profit_percent for s in symbol_spikes) / len(symbol_spikes)
        
        print(f"   {symbol}: {len(symbol_spikes)}个信号 "
              f"(上插:{up_count} 下插:{down_count}) "
              f"平均幅度:{avg_amplitude:.2f}% "
              f"平均最大盈利:{avg_max_profit:.3f}%")
    
    # 不同止盈止损组合的表现
    print(f"\n📈 止盈止损组合表现 (本金:{TRADING_CONFIG['capital']}U, 杠杆:{TRADING_CONFIG['leverage']}x):")
    print("-" * 85)
    print(f"{'TP%':>6} {'SL%':>6} {'胜率':>8} {'盈利次':>8} {'亏损次':>8} {'超时':>6} {'总盈亏':>12} {'平均盈亏':>10}")
    print("-" * 85)
    
    best_combo = None
    best_profit = float('-inf')
    
    for tp in TRADING_CONFIG["take_profit_levels"]:
        for sl in TRADING_CONFIG["stop_loss_levels"]:
            key = f"TP{tp}_SL{sl}"
            
            wins = 0
            losses = 0
            timeouts = 0
            total_pnl = 0
            
            for spike in completed:
                if key in spike.tp_sl_results:
                    r = spike.tp_sl_results[key]
                    if r["result"] == "TP":
                        wins += 1
                    elif r["result"] == "SL":
                        losses += 1
                    else:
                        timeouts += 1
                    total_pnl += r["pnl_usdt"]
            
            total = wins + losses + timeouts
            if total > 0:
                win_rate = wins / total * 100
                avg_pnl = total_pnl / total
                
                print(f"{tp:>6.1f} {sl:>6.1f} {win_rate:>7.1f}% {wins:>8} {losses:>8} "
                      f"{timeouts:>6} {total_pnl:>+11.2f}U {avg_pnl:>+9.2f}U")
                
                if total_pnl > best_profit:
                    best_profit = total_pnl
                    best_combo = (tp, sl, win_rate, total_pnl)
    
    print("-" * 85)
    
    if best_combo:
        print(f"\n🏆 最佳组合: TP={best_combo[0]}% SL={best_combo[1]}% "
              f"胜率:{best_combo[2]:.1f}% 总盈亏:{best_combo[3]:+.2f}U")
    
    # 时间分析
    print(f"\n⏱️ 时间分析:")
    print("-" * 85)
    
    avg_profit_time = sum(s.max_profit_time_ms for s in completed) / len(completed)
    avg_loss_time = sum(s.max_loss_time_ms for s in completed) / len(completed)
    
    print(f"   平均达到最大盈利时间: {avg_profit_time:.0f}ms ({avg_profit_time/1000:.1f}秒)")
    print(f"   平均达到最大亏损时间: {avg_loss_time:.0f}ms ({avg_loss_time/1000:.1f}秒)")
    
    # 信号质量分析
    print(f"\n🎯 信号质量分析:")
    print("-" * 85)
    
    profitable_signals = [s for s in completed if s.max_profit_percent > 0.1]
    high_quality = [s for s in completed if s.max_profit_percent > 0.3]
    
    print(f"   有盈利空间的信号 (>0.1%): {len(profitable_signals)}/{len(completed)} "
          f"({len(profitable_signals)/len(completed)*100:.1f}%)")
    print(f"   高质量信号 (>0.3%): {len(high_quality)}/{len(completed)} "
          f"({len(high_quality)/len(completed)*100:.1f}%)")
    
    # 方向分析
    up_spikes = [s for s in completed if s.direction == "UP"]
    down_spikes = [s for s in completed if s.direction == "DOWN"]
    
    if up_spikes:
        up_avg_profit = sum(s.max_profit_percent for s in up_spikes) / len(up_spikes)
        print(f"   上插针平均最大盈利: {up_avg_profit:.3f}%")
    if down_spikes:
        down_avg_profit = sum(s.max_profit_percent for s in down_spikes) / len(down_spikes)
        print(f"   下插针平均最大盈利: {down_avg_profit:.3f}%")


# ============== 主函数 ==============

def main():
    global DEFAULT_SYMBOLS, CURRENT_REST_ENDPOINT
    
    clear_screen()
    print_header()
    
    print(f"\n📋 交易参数:")
    print(f"   本金: {TRADING_CONFIG['capital']} USDT")
    print(f"   杠杆: {TRADING_CONFIG['leverage']}x")
    print(f"   手续费: {TRADING_CONFIG['fee_rate']*100:.2f}%")
    print(f"   跟踪时长: {DATA_CONFIG['tracking_seconds']}秒")
    
    print(f"\n📋 代理: {PROXY_HOST}:{PROXY_HTTP_PORT}" if USE_PROXY else "\n📋 代理: 未使用")
    
    # 测试连接
    print("\n🔗 测试连接...")
    if not test_connection():
        print("❌ 连接失败")
        return
    print("✅ 连接成功")
    
    # 测试WebSocket
    print("\n🔗 测试WebSocket...")
    ws_endpoint = WS_ENDPOINTS[0]
    
    # 简单测试
    test_result = {"ok": False}
    def on_open(ws):
        test_result["ok"] = True
        ws.close()
    
    test_ws = websocket.WebSocketApp(
        f"{ws_endpoint}/btcusdt@trade",
        on_open=on_open
    )
    
    def run_test():
        if USE_PROXY:
            test_ws.run_forever(http_proxy_host=PROXY_HOST, http_proxy_port=PROXY_HTTP_PORT, proxy_type="http")
        else:
            test_ws.run_forever()
    
    t = threading.Thread(target=run_test)
    t.daemon = True
    t.start()
    t.join(timeout=5)
    
    if test_result["ok"]:
        print("✅ WebSocket连接成功")
    else:
        print("⚠️ WebSocket测试超时，仍将尝试启动")
    
    # 用户输入
    print(f"\n默认监控: {', '.join(DEFAULT_SYMBOLS)}")
    user_input = input("输入交易对 (回车默认): ").strip()
    if user_input:
        DEFAULT_SYMBOLS = [s.strip().upper() for s in user_input.split(",")]
    
    # 监控时长
    print(f"\n默认监控时长: {MONITOR_DURATION//60}分钟")
    duration_input = input("输入监控时长(分钟，回车默认): ").strip()
    monitor_duration = MONITOR_DURATION
    if duration_input:
        try:
            monitor_duration = int(duration_input) * 60
        except:
            pass
    
    # 验证交易对
    print("\n🔍 验证交易对...")
    prices = fetch_prices(DEFAULT_SYMBOLS)
    valid = [s for s in DEFAULT_SYMBOLS if s in prices]
    for s in DEFAULT_SYMBOLS:
        status = f"✓ {prices[s]:.4f}" if s in prices else "✗"
        print(f"   {s}: {status}")
    
    if not valid:
        print("❌ 无有效交易对")
        return
    DEFAULT_SYMBOLS = valid
    
    # 创建数据目录
    print(f"\n📁 数据保存目录: {DATA_DIR.absolute()}")

    # 保存会话配置
    config_file = save_session_config()
    print(f"📋 配置文件: {config_file.name}")
    print(f"   本金: {TRADING_CONFIG['capital']} U, 杠杆: {TRADING_CONFIG['leverage']}x")
    print(f"   止盈: {TRADING_CONFIG['take_profit_levels']}%, 止损: {TRADING_CONFIG['stop_loss_levels']}%")
    print(f"   跟踪: {DATA_CONFIG['tracking_seconds']}秒, 间隔: {DATA_CONFIG['tracking_interval_ms']}ms")

    # 启动检测器
    print(f"\n🚀 启动数据记录 ({len(DEFAULT_SYMBOLS)}个交易对, {monitor_duration//60}分钟)...")
    
    detector = DataRecordingDetector(DEFAULT_SYMBOLS, ws_endpoint)
    detector.start()
    
    # 等待连接
    for i in range(10):
        if detector.is_connected():
            break
        time.sleep(1)
        print(f"   等待连接... ({i+1}/10)")
    
    last_display = 0
    start_time = time.time()
    
    try:
        while True:
            now = time.time()
            elapsed = now - start_time
            
            if elapsed >= monitor_duration:
                print("\n⏰ 监控时间结束")
                break
            
            if now - last_display >= 3:  # 3秒刷新
                clear_screen()
                print_header()
                print_stats(detector)
                print_tracking_status(detector)
                print_completed_analysis(detector)
                
                remaining = int(monitor_duration - elapsed)
                print(f"\n💡 剩余: {remaining//60}分{remaining%60}秒 | Ctrl+C 停止并生成报告")
                
                last_display = now
            
            time.sleep(0.1)
            
    except KeyboardInterrupt:
        print("\n\n👋 用户中断，正在生成报告...")
    finally:
        detector.stop()
        
        # 等待正在跟踪的信号完成
        tracking_count = sum(len(m.tracking_spikes) for m in detector.monitors.values())
        if tracking_count > 0:
            print(f"\n⏳ 等待 {tracking_count} 个信号完成跟踪...")
            time.sleep(min(tracking_count * 2, 30))  # 最多等30秒
    
    # 生成最终报告
    generate_final_report(detector)
    
    print("\n" + "=" * 85)
    print("✅ 数据记录完成")
    print("=" * 85)


if __name__ == "__main__":
    main()
