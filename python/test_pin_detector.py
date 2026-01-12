#!/usr/bin/env python3
"""
Flash Arbitrage Bot - 实时插针检测脚本 (WebSocket版)

修复WebSocket代理连接问题
集成信号记录和验证功能
"""

import os
import sys
import json
import time
import threading
import requests
import websocket
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Deque
from dataclasses import dataclass, field
from collections import deque

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 导入记录和分析模块
from src.data import SignalRecorder, PinSignalRecord, MultiSymbolPriceTracker
from src.backtest import BatchSimulator
from src.analysis import SignalAnalytics, ReportGenerator

# ============== 代理配置 ==============
PROXY_HOST = "127.0.0.1"
PROXY_HTTP_PORT = 7897
USE_PROXY = True

# HTTP代理配置
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

# ============== API配置 ==============
REST_ENDPOINTS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
]

WS_ENDPOINTS = [
    "wss://fstream.binance.com/ws",
    "wss://fstream1.binance.com/ws",
    "wss://fstream2.binance.com/ws",
]

CURRENT_REST_ENDPOINT = REST_ENDPOINTS[0]

# 监控配置
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT","TRUMPUSDT",
                   "ZECUSDT","VVVUSDT","TAOUSDT","RIVERUSDT","POLUSDT",
                   "币安人生USDT","BREVUSDT","MIRAUSDT","COLLECTUSDT",
                   "4USDT","BUSDT","CCUSDT","GUNUSDT","AAVEUSDT"]
MONITOR_DURATION = 300

# 实时插针检测参数
REALTIME_CONFIG = {
    "price_window_ms": 1000,
    "min_spike_percent": 0.3,
    "max_spike_percent": 5.0,
    "retracement_percent": 30,
}

# 信号记录配置
ENABLE_RECORDING = True  # 是否启用信号记录
TRACK_DURATION_SECONDS = 180  # 价格追踪时长（秒）
HOLD_PERIODS = [30, 60, 90, 180]  # 测试的持仓时间段（秒）


# ============== 数据类 ==============

@dataclass
class TickData:
    timestamp: datetime
    price: float
    
@dataclass
class PriceSpike:
    detected_at: datetime
    symbol: str
    direction: str
    start_price: float
    peak_price: float
    current_price: float
    amplitude_percent: float
    retracement_percent: float
    duration_ms: int
    confirmed: bool
    
    def __str__(self):
        icon = "🔺" if self.direction == "UP" else "🔻"
        status = "✓确认" if self.confirmed else "⏳待确认"
        time_str = format_time(self.detected_at)
        return (f"{time_str} {self.symbol:10s} {icon} {self.direction:4s} "
                f"幅度:{self.amplitude_percent:5.2f}% 回撤:{self.retracement_percent:5.1f}% "
                f"峰值:{self.peak_price:.6f} {status}")


@dataclass
class SymbolMonitor:
    symbol: str
    current_price: float = 0.0
    price_history: Deque[TickData] = field(default_factory=lambda: deque(maxlen=1000))
    window_high: float = 0.0
    window_low: float = float('inf')
    window_start_price: float = 0.0
    window_start_time: datetime = None
    spike_count: int = 0
    up_spikes: int = 0
    down_spikes: int = 0
    spikes: List[PriceSpike] = field(default_factory=list)
    last_update: datetime = None
    connected: bool = False
    tick_count: int = 0


# ============== 网络函数 ==============

def diagnose_proxy():
    """诊断代理"""
    print(f"\n🔧 代理诊断:")
    print(f"   地址: {PROXY_HOST}:{PROXY_HTTP_PORT}")
    
    print(f"   测试HTTP代理...", end=" ")
    try:
        response = requests.get("https://httpbin.org/ip", proxies=HTTP_PROXY, timeout=10)
        if response.status_code == 200:
            ip = response.json().get('origin', 'unknown')
            print(f"✓ (出口IP: {ip})")
            return True
    except Exception as e:
        print(f"✗ ({type(e).__name__})")
    return False


def test_rest_endpoint(endpoint: str) -> bool:
    """测试REST端点"""
    try:
        response = requests.get(
            f"{endpoint}/fapi/v1/time",
            timeout=10,
            proxies=HTTP_PROXY if USE_PROXY else None
        )
        return response.status_code == 200
    except:
        return False


def find_working_endpoint() -> Optional[str]:
    """找到可用的REST端点"""
    print("\n🔍 测试REST API:")
    for endpoint in REST_ENDPOINTS:
        print(f"   {endpoint}...", end=" ")
        if test_rest_endpoint(endpoint):
            print("✓")
            return endpoint
        print("✗")
    return None


def create_websocket_connection(url: str, on_message, on_error, on_close, on_open):
    """
    创建WebSocket连接，支持HTTP代理
    
    关键：使用正确的代理参数名称
    """
    ws = websocket.WebSocketApp(
        url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open
    )
    return ws


def run_websocket_with_proxy(ws):
    """运行WebSocket，支持代理"""
    if USE_PROXY:
        # websocket-client 的正确代理参数
        ws.run_forever(
            http_proxy_host=PROXY_HOST,
            http_proxy_port=PROXY_HTTP_PORT,
            proxy_type="http"
        )
    else:
        ws.run_forever()


def test_websocket() -> tuple:
    """测试WebSocket连接"""
    print("\n🔍 测试WebSocket:")
    
    for ws_endpoint in WS_ENDPOINTS:
        print(f"   {ws_endpoint}...", end=" ", flush=True)
        
        result = {"connected": False, "error": None, "done": False}
        
        def on_open(ws):
            result["connected"] = True
            result["done"] = True
            ws.close()
            
        def on_error(ws, error):
            result["error"] = str(error)[:50] if error else None
            
        def on_close(ws, code, msg):
            result["done"] = True
            
        def on_message(ws, msg):
            pass
            
        try:
            ws_url = f"{ws_endpoint}/btcusdt@trade"
            ws = create_websocket_connection(ws_url, on_message, on_error, on_close, on_open)
            
            # 在线程中运行
            def run():
                try:
                    run_websocket_with_proxy(ws)
                except Exception as e:
                    result["error"] = str(e)[:50]
                    result["done"] = True
            
            thread = threading.Thread(target=run)
            thread.daemon = True
            thread.start()
            
            # 等待结果
            for _ in range(50):  # 最多等5秒
                if result["done"]:
                    break
                time.sleep(0.1)
            
            if result["connected"]:
                print("✓")
                return True, ws_endpoint, None
            else:
                error_msg = result["error"] or "超时"
                print(f"✗ ({error_msg})")
                
        except Exception as e:
            print(f"✗ ({str(e)[:30]})")
    
    return False, None, "所有端点失败"


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

class RealtimePinDetector:
    """实时插针检测器"""

    def __init__(
        self,
        symbols: List[str],
        ws_endpoint: str,
        on_spike_callback=None,
        recorder: SignalRecorder = None,
        price_tracker: MultiSymbolPriceTracker = None
    ):
        self.symbols = [s.lower() for s in symbols]
        self.ws_endpoint = ws_endpoint
        self.monitors: Dict[str, SymbolMonitor] = {
            s.upper(): SymbolMonitor(symbol=s.upper()) for s in symbols
        }
        self.on_spike_callback = on_spike_callback
        self.ws = None
        self.running = False
        self.start_time = get_beijing_time()
        self.ws_connected = False
        self.reconnect_count = 0
        self.message_count = 0
        self.ws_thread = None

        # 信号记录器
        self.recorder = recorder
        self.price_tracker = price_tracker

        # 如果启用了记录，添加所有交易对到价格追踪器
        if self.price_tracker:
            for symbol in symbols:
                self.price_tracker.add_symbol(symbol.upper())
        
    def start(self):
        """启动"""
        self.running = True
        self._connect()
        
    def _connect(self):
        """连接WebSocket"""
        if not self.running:
            return
            
        streams = [f"{s}@aggTrade" for s in self.symbols]
        stream_str = "/".join(streams)
        ws_url = f"{self.ws_endpoint}/{stream_str}"
        
        print(f"[{format_time()}] 连接: {ws_url[:60]}...")
        
        self.ws = create_websocket_connection(
            ws_url,
            self._on_message,
            self._on_error,
            self._on_close,
            self._on_open
        )
        
        self.ws_thread = threading.Thread(target=self._run_ws)
        self.ws_thread.daemon = True
        self.ws_thread.start()
        
    def _run_ws(self):
        """运行WebSocket"""
        try:
            run_websocket_with_proxy(self.ws)
        except Exception as e:
            print(f"[{format_time()}] WebSocket运行错误: {e}")
        
    def stop(self):
        """停止"""
        self.running = False
        if self.ws:
            self.ws.close()
            
    def _on_open(self, ws):
        self.ws_connected = True
        self.reconnect_count = 0
        print(f"[{format_time()}] ✅ WebSocket已连接")
        for symbol in self.monitors:
            self.monitors[symbol].connected = True
            
    def _on_error(self, ws, error):
        if error:
            print(f"[{format_time()}] WebSocket错误: {str(error)[:80]}")
        
    def _on_close(self, ws, code, msg):
        self.ws_connected = False
        print(f"[{format_time()}] WebSocket断开")
        for symbol in self.monitors:
            self.monitors[symbol].connected = False
            
        # 重连
        if self.running and self.reconnect_count < 3:
            self.reconnect_count += 1
            print(f"[{format_time()}] 重连 ({self.reconnect_count}/3)...")
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

        monitor.price_history.append(TickData(timestamp=timestamp, price=price))

        # 更新价格追踪器
        if self.price_tracker:
            self.price_tracker.update_price(symbol, price)

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
                spike = PriceSpike(
                    detected_at=timestamp, symbol=monitor.symbol, direction="UP",
                    start_price=start, peak_price=high, current_price=price,
                    amplitude_percent=up_amp, retracement_percent=ret,
                    duration_ms=int(window_ms), confirmed=ret >= 50
                )
                
        # 下插针
        if spike is None and min_amp <= down_amp <= max_amp and start > low:
            ret = (price - low) / (start - low) * 100
            if ret >= ret_threshold:
                spike = PriceSpike(
                    detected_at=timestamp, symbol=monitor.symbol, direction="DOWN",
                    start_price=start, peak_price=low, current_price=price,
                    amplitude_percent=down_amp, retracement_percent=ret,
                    duration_ms=int(window_ms), confirmed=ret >= 50
                )
                
        if spike:
            # 去重
            if monitor.spikes:
                last = monitor.spikes[-1]
                if (spike.detected_at - last.detected_at).total_seconds() < 2:
                    if spike.direction == last.direction:
                        if spike.amplitude_percent > last.amplitude_percent:
                            monitor.spikes[-1] = spike
                        return

            monitor.spike_count += 1
            if spike.direction == "UP":
                monitor.up_spikes += 1
            else:
                monitor.down_spikes += 1
            monitor.spikes.append(spike)

            if len(monitor.spikes) > 20:
                monitor.spikes = monitor.spikes[-20:]

            # 记录信号
            if self.recorder:
                record = self.recorder.record_spike(
                    symbol=spike.symbol,
                    direction=spike.direction,
                    start_price=spike.start_price,
                    peak_price=spike.peak_price,
                    current_price=spike.current_price,
                    amplitude_percent=spike.amplitude_percent,
                    retracement_percent=spike.retracement_percent,
                    duration_ms=spike.duration_ms,
                    detected_at=spike.detected_at,
                    peak_time=spike.detected_at,
                )

                # 启动价格追踪
                if self.price_tracker and record:
                    self.price_tracker.start_tracking(record)

            if self.on_spike_callback:
                self.on_spike_callback(spike)
                
    def get_stats(self) -> Dict:
        return {
            symbol: {
                "price": m.current_price,
                "up_spikes": m.up_spikes,
                "down_spikes": m.down_spikes,
                "total": m.spike_count,
                "last_update": m.last_update,
                "tick_count": m.tick_count,
            }
            for symbol, m in self.monitors.items()
        }
        
    def get_all_spikes(self) -> List[PriceSpike]:
        all_spikes = []
        for m in self.monitors.values():
            all_spikes.extend(m.spikes)
        return sorted(all_spikes, key=lambda x: x.detected_at)
    
    def is_connected(self) -> bool:
        return self.ws_connected


# ============== 显示函数 ==============

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def print_header():
    now = get_beijing_time()
    print("=" * 80)
    print("              Flash Arbitrage Bot - 实时插针检测")
    print(f"                   {now.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
    print("=" * 80)


def print_stats(detector: RealtimePinDetector):
    """打印统计"""
    elapsed = (get_beijing_time() - detector.start_time).total_seconds()
    minutes, seconds = int(elapsed // 60), int(elapsed % 60)
    
    status = "🟢 已连接" if detector.is_connected() else "🔴 断开"
    msg_rate = detector.message_count / max(1, elapsed)
    
    print(f"\n📊 运行: {minutes}分{seconds}秒 | {status} | 消息: {detector.message_count} ({msg_rate:.1f}/s)")
    print("-" * 80)
    print(f"{'交易对':<12} {'价格':>14} {'Ticks':>8} {'上插':>6} {'下插':>6} {'总计':>6} {'更新时间':>12}")
    print("-" * 80)
    
    stats = detector.get_stats()
    for symbol, data in stats.items():
        price = f"{data['price']:.6f}" if data['price'] > 0 else "等待..."
        update = format_time(data['last_update'])[:8] if data['last_update'] else "N/A"
        print(f"{symbol:<12} {price:>14} {data['tick_count']:>8} "
              f"{data['up_spikes']:>6} {data['down_spikes']:>6} {data['total']:>6} {update:>12}")
    
    print("-" * 80)


def print_spikes(detector: RealtimePinDetector, count: int = 10):
    """打印插针信号"""
    print(f"\n🔔 最近 {count} 个插针:")
    print("-" * 80)
    
    spikes = detector.get_all_spikes()[-count:]
    if not spikes:
        print("   暂无信号")
    else:
        for spike in reversed(spikes):
            print(f"   {spike}")
    print("-" * 80)


# ============== 主函数 ==============

def main():
    global DEFAULT_SYMBOLS, CURRENT_REST_ENDPOINT
    
    clear_screen()
    print_header()
    
    print(f"\n📋 配置: USE_PROXY={USE_PROXY}, {PROXY_HOST}:{PROXY_HTTP_PORT}")
    
    # 诊断
    if USE_PROXY and not diagnose_proxy():
        print("\n⚠️ 代理测试失败")
        if input("继续? (y/n): ").lower() != 'y':
            return
    
    # REST API
    endpoint = find_working_endpoint()
    if not endpoint:
        print("\n❌ REST API连接失败")
        return
    CURRENT_REST_ENDPOINT = endpoint
    
    # WebSocket
    ws_ok, ws_endpoint, _ = test_websocket()
    if not ws_ok:
        print("\n⚠️ WebSocket测试失败，但仍可尝试启动")
        if input("继续? (y/n): ").lower() != 'y':
            return
        ws_endpoint = WS_ENDPOINTS[0]
    
    # 交易对
    print(f"\n默认: {', '.join(DEFAULT_SYMBOLS)}")
    user_input = input("输入交易对 (回车默认): ").strip()
    if user_input:
        DEFAULT_SYMBOLS = [s.strip().upper() for s in user_input.split(",")]
    
    # 验证
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
    
    # 启动
    print(f"\n🚀 启动监控...")

    # 初始化记录器和追踪器
    recorder = None
    price_tracker = None

    if ENABLE_RECORDING:
        print(f"📝 信号记录已启用")
        print(f"   追踪时长: {TRACK_DURATION_SECONDS}秒")
        print(f"   测试时间段: {HOLD_PERIODS}秒")

        # 创建配置
        tracker_config = {
            "track_duration_seconds": TRACK_DURATION_SECONDS,
            "track_pre_seconds": 180,
            "hold_periods": HOLD_PERIODS,
        }

        recorder = SignalRecorder()
        price_tracker = MultiSymbolPriceTracker(tracker_config)

        # 设置追踪完成回调（自动保存记录）
        def on_track_complete(record):
            recorder.finalize_record(record)

        price_tracker.set_callback(on_track_complete)

    new_spikes = []
    lock = threading.Lock()

    detector = RealtimePinDetector(
        DEFAULT_SYMBOLS,
        ws_endpoint,
        on_spike_callback=lambda s: (lock.acquire(), new_spikes.append(s), lock.release()),
        recorder=recorder,
        price_tracker=price_tracker
    )
    detector.start()
    
    # 等待连接
    for i in range(10):
        if detector.is_connected():
            break
        time.sleep(1)
        print(f"   等待连接... ({i+1}/10)")
    
    last_display = 0
    
    try:
        while True:
            now = time.time()
            elapsed = (get_beijing_time() - detector.start_time).total_seconds()
            
            if elapsed >= MONITOR_DURATION:
                print("\n⏰ 时间到")
                break
            
            if now - last_display >= 2:
                clear_screen()
                print_header()
                print_stats(detector)
                print_spikes(detector, 8)
                
                with lock:
                    if new_spikes:
                        print(f"\n⚡ 新信号:")
                        for s in new_spikes[-3:]:
                            print(f"   🆕 {s}")
                        new_spikes.clear()
                
                print(f"\n💡 剩余: {int(MONITOR_DURATION - elapsed)}秒 | Ctrl+C 停止")
                last_display = now
            
            time.sleep(0.1)
            
    except KeyboardInterrupt:
        print("\n\n👋 停止")
    finally:
        detector.stop()

        # 等待追踪完成
        if price_tracker:
            print(f"\n⏳ 等待价格追踪完成... (剩余 {price_tracker.get_active_count()} 个)")
            time.sleep(2)  # 给一点时间让追踪完成
            price_tracker.stop_all()

        # 关闭记录器
        if recorder:
            recorder.close()

    # 最终统计
    print("\n" + "=" * 80)
    print_stats(detector)
    print_spikes(detector, 15)
    print(f"\n✅ 共检测 {len(detector.get_all_spikes())} 个插针")

    # 生成分析报告
    if recorder and ENABLE_RECORDING:
        print("\n" + "=" * 80)
        print("📊 正在生成分析报告...")

        # 加载所有记录
        records = recorder.get_all_records()

        if records:
            # 模拟盈亏
            simulator_config = {
                "position_size_usd": 15,
                "leverage": 20,
                "hold_periods": HOLD_PERIODS,
            }

            from src.backtest import BatchSimulator
            simulator = BatchSimulator(simulator_config)
            records = simulator.simulate_and_update(records)

            # 生成报告
            analytics_config = {
                "position_size_usd": 15,
                "leverage": 20,
                "hold_periods": HOLD_PERIODS,
            }

            from src.analysis import SignalAnalytics, ReportGenerator
            analytics = SignalAnalytics(analytics_config)
            report = analytics.analyze(records)
            generator = ReportGenerator()
            generator.print_report(report)
        else:
            print("📭 暂无记录数据")


if __name__ == "__main__":
    main()
