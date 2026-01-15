#!/usr/bin/env python3
"""
Flash Arbitrage Bot - 币安测试网模拟交易系统

使用币安官方测试网进行真实环境的模拟交易
"""

import os
import sys
import json
import time
import hmac
import hashlib
import threading
import requests
import websocket
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field
from collections import deque
from pathlib import Path
from urllib.parse import urlencode

# ============== 配置 ==============

# 代理配置
PROXY_CONFIG = {
    "enabled": True,
    "host": "127.0.0.1",
    "http_port": 7897,
}

# 环境选择: "testnet" 或 "production"
ENVIRONMENT = "testnet"

# API配置
API_CONFIG = {
    "testnet": {
        "rest_url": "https://testnet.binancefuture.com",
        "ws_url": "wss://stream.binancefuture.com",
        "api_key": "",  # 测试网API Key
        "api_secret": "",  # 测试网API Secret
    },
    "production": {
        "rest_url": "https://fapi.binance.com",
        "ws_url": "wss://fstream.binance.com",
        "api_key": "",  # 生产环境API Key（谨慎使用）
        "api_secret": "",  # 生产环境API Secret
    }
}

# 交易配置
TRADING_CONFIG = {
    "capital": 100.0,        # 测试资金 (测试网可以有大量资金)
    "leverage": 20,          # 杠杆倍数
    "symbols": ["BTCUSDT", "ETHUSDT"],  # 监控交易对
    
    # 止盈止损 (账户盈亏百分比)
    "take_profit": 3.0,      # 止盈 3%
    "stop_loss": 2.0,        # 止损 2%
    
    # 插针检测参数
    "spike_threshold": 0.3,  # 插针幅度阈值 %
    "retracement_threshold": 30,  # 回撤阈值 %
}

# 时区
BEIJING_TZ = timezone(timedelta(hours=8))


# ============== 工具函数 ==============

def get_beijing_time() -> datetime:
    return datetime.now(BEIJING_TZ)

def format_time(dt: datetime = None) -> str:
    if dt is None:
        dt = get_beijing_time()
    return dt.strftime("%H:%M:%S.%f")[:-3]

def get_proxies():
    if PROXY_CONFIG["enabled"]:
        proxy = f"http://{PROXY_CONFIG['host']}:{PROXY_CONFIG['http_port']}"
        return {"http": proxy, "https": proxy}
    return None


# ============== 数据类 ==============

@dataclass
class OrderResult:
    """订单结果"""
    order_id: str
    client_order_id: str
    symbol: str
    side: str              # BUY / SELL
    position_side: str     # LONG / SHORT
    order_type: str        # MARKET / LIMIT
    quantity: float
    price: float           # 成交价格
    status: str            # NEW / FILLED / CANCELED
    executed_qty: float
    avg_price: float
    commission: float
    commission_asset: str
    timestamp: datetime
    raw_response: dict


@dataclass 
class Position:
    """持仓信息"""
    symbol: str
    side: str              # LONG / SHORT
    quantity: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: int
    margin_type: str


@dataclass
class TradeRecord:
    """交易记录"""
    id: str
    spike_id: str
    symbol: str
    direction: str         # UP / DOWN (插针方向)
    
    # 入场信息
    entry_time: datetime
    entry_order_id: str
    entry_price: float
    entry_quantity: float
    entry_commission: float
    
    # 出场信息
    exit_time: datetime = None
    exit_order_id: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""  # TP / SL / MANUAL
    exit_commission: float = 0.0
    
    # 盈亏
    pnl_usdt: float = 0.0
    pnl_percent: float = 0.0
    
    # 延迟统计
    signal_to_entry_ms: int = 0   # 信号到入场延迟
    entry_to_exit_ms: int = 0     # 持仓时间


# ============== 币安API客户端 ==============

class BinanceFuturesClient:
    """币安合约API客户端"""
    
    def __init__(self, environment: str = "testnet"):
        config = API_CONFIG[environment]
        self.base_url = config["rest_url"]
        self.ws_url = config["ws_url"]
        self.api_key = config["api_key"]
        self.api_secret = config["api_secret"]
        self.environment = environment
        
        self.session = requests.Session()
        self.session.headers.update({
            "X-MBX-APIKEY": self.api_key
        })
        
        if PROXY_CONFIG["enabled"]:
            self.session.proxies = get_proxies()
    
    def _sign(self, params: dict) -> dict:
        """生成签名"""
        params["timestamp"] = int(time.time() * 1000)
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params
    
    def _request(self, method: str, endpoint: str, params: dict = None, signed: bool = False) -> dict:
        """发送请求"""
        url = f"{self.base_url}{endpoint}"
        
        if params is None:
            params = {}
        
        if signed:
            params = self._sign(params)
        
        try:
            if method == "GET":
                response = self.session.get(url, params=params, timeout=10)
            elif method == "POST":
                response = self.session.post(url, params=params, timeout=10)
            elif method == "DELETE":
                response = self.session.delete(url, params=params, timeout=10)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.RequestException as e:
            print(f"[API错误] {method} {endpoint}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"   响应: {e.response.text}")
            raise
    
    # ===== 账户相关 =====
    
    def get_account_info(self) -> dict:
        """获取账户信息"""
        return self._request("GET", "/fapi/v2/account", signed=True)
    
    def get_balance(self) -> Dict[str, float]:
        """获取余额"""
        account = self.get_account_info()
        balances = {}
        for asset in account.get("assets", []):
            balance = float(asset.get("walletBalance", 0))
            if balance > 0:
                balances[asset["asset"]] = balance
        return balances
    
    def get_positions(self) -> List[Position]:
        """获取持仓"""
        account = self.get_account_info()
        positions = []
        
        for pos in account.get("positions", []):
            quantity = float(pos.get("positionAmt", 0))
            if quantity != 0:
                positions.append(Position(
                    symbol=pos["symbol"],
                    side="LONG" if quantity > 0 else "SHORT",
                    quantity=abs(quantity),
                    entry_price=float(pos.get("entryPrice", 0)),
                    mark_price=float(pos.get("markPrice", 0)),
                    unrealized_pnl=float(pos.get("unrealizedProfit", 0)),
                    leverage=int(pos.get("leverage", 1)),
                    margin_type=pos.get("marginType", "cross")
                ))
        
        return positions
    
    # ===== 交易相关 =====
    
    def set_leverage(self, symbol: str, leverage: int) -> dict:
        """设置杠杆"""
        return self._request("POST", "/fapi/v1/leverage", {
            "symbol": symbol,
            "leverage": leverage
        }, signed=True)
    
    def set_margin_type(self, symbol: str, margin_type: str = "CROSSED") -> dict:
        """设置保证金模式"""
        try:
            return self._request("POST", "/fapi/v1/marginType", {
                "symbol": symbol,
                "marginType": margin_type
            }, signed=True)
        except:
            pass  # 可能已经是目标模式
    
    def place_market_order(self, symbol: str, side: str, quantity: float, 
                           reduce_only: bool = False) -> OrderResult:
        """
        下市价单
        
        Args:
            symbol: 交易对
            side: BUY / SELL
            quantity: 数量
            reduce_only: 是否只减仓
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
        }
        
        if reduce_only:
            params["reduceOnly"] = "true"
        
        result = self._request("POST", "/fapi/v1/order", params, signed=True)
        
        return self._parse_order_result(result)
    
    def place_stop_market_order(self, symbol: str, side: str, quantity: float,
                                 stop_price: float, reduce_only: bool = True) -> OrderResult:
        """
        下止损市价单
        
        Args:
            symbol: 交易对
            side: BUY (做空止损) / SELL (做多止损)
            quantity: 数量
            stop_price: 触发价格
            reduce_only: 是否只减仓
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "stopPrice": stop_price,
            "quantity": quantity,
            "reduceOnly": "true" if reduce_only else "false",
        }
        
        result = self._request("POST", "/fapi/v1/order", params, signed=True)
        return self._parse_order_result(result)
    
    def place_take_profit_market_order(self, symbol: str, side: str, quantity: float,
                                        stop_price: float, reduce_only: bool = True) -> OrderResult:
        """
        下止盈市价单
        
        Args:
            symbol: 交易对
            side: BUY (做空止盈) / SELL (做多止盈)
            quantity: 数量
            stop_price: 触发价格
            reduce_only: 是否只减仓
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": stop_price,
            "quantity": quantity,
            "reduceOnly": "true" if reduce_only else "false",
        }
        
        result = self._request("POST", "/fapi/v1/order", params, signed=True)
        return self._parse_order_result(result)
    
    def cancel_order(self, symbol: str, order_id: str) -> dict:
        """取消订单"""
        return self._request("DELETE", "/fapi/v1/order", {
            "symbol": symbol,
            "orderId": order_id
        }, signed=True)
    
    def cancel_all_orders(self, symbol: str) -> dict:
        """取消所有订单"""
        return self._request("DELETE", "/fapi/v1/allOpenOrders", {
            "symbol": symbol
        }, signed=True)
    
    def get_order(self, symbol: str, order_id: str) -> dict:
        """查询订单"""
        return self._request("GET", "/fapi/v1/order", {
            "symbol": symbol,
            "orderId": order_id
        }, signed=True)
    
    def get_open_orders(self, symbol: str = None) -> List[dict]:
        """获取挂单"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/fapi/v1/openOrders", params, signed=True)
    
    # ===== 行情相关 =====
    
    def get_ticker_price(self, symbol: str = None) -> dict:
        """获取价格"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/fapi/v1/ticker/price", params)
    
    def get_exchange_info(self, symbol: str = None) -> dict:
        """获取交易规则"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/fapi/v1/exchangeInfo", params)
    
    def get_symbol_info(self, symbol: str) -> dict:
        """获取交易对信息"""
        info = self.get_exchange_info(symbol)
        for s in info.get("symbols", []):
            if s["symbol"] == symbol:
                return s
        return None
    
    def _parse_order_result(self, result: dict) -> OrderResult:
        """解析订单结果"""
        return OrderResult(
            order_id=str(result.get("orderId", "")),
            client_order_id=result.get("clientOrderId", ""),
            symbol=result.get("symbol", ""),
            side=result.get("side", ""),
            position_side=result.get("positionSide", "BOTH"),
            order_type=result.get("type", ""),
            quantity=float(result.get("origQty", 0)),
            price=float(result.get("price", 0)),
            status=result.get("status", ""),
            executed_qty=float(result.get("executedQty", 0)),
            avg_price=float(result.get("avgPrice", 0)),
            commission=0,  # 需要从交易记录获取
            commission_asset="",
            timestamp=datetime.fromtimestamp(result.get("updateTime", 0) / 1000, tz=BEIJING_TZ),
            raw_response=result
        )


# ============== 交易管理器 ==============

class TradingManager:
    """交易管理器 - 管理订单和持仓"""
    
    def __init__(self, client: BinanceFuturesClient):
        self.client = client
        self.trades: List[TradeRecord] = []
        self.active_trades: Dict[str, TradeRecord] = {}  # symbol -> trade
        self.lock = threading.Lock()
        
        # 交易对精度信息
        self.symbol_info: Dict[str, dict] = {}
        
    def initialize(self, symbols: List[str]):
        """初始化"""
        print(f"\n[{format_time()}] 初始化交易管理器...")
        
        # 获取账户信息
        balance = self.client.get_balance()
        print(f"   账户余额: {balance}")
        
        # 设置杠杆和保证金模式
        for symbol in symbols:
            try:
                # 获取交易对信息
                info = self.client.get_symbol_info(symbol)
                if info:
                    self.symbol_info[symbol] = info
                    print(f"   {symbol}: 精度={self._get_quantity_precision(symbol)}")
                
                # 设置杠杆 (参数顺序: leverage, symbol)
                self.client.set_leverage(TRADING_CONFIG["leverage"], symbol)
                print(f"   {symbol}: 杠杆={TRADING_CONFIG['leverage']}x")
                
                # 设置逐仓/全仓
                self.client.set_margin_type(symbol, "CROSSED")
                
            except Exception as e:
                print(f"   {symbol}: 初始化失败 - {e}")
        
        # 检查现有持仓
        positions = self.client.get_positions()
        if positions:
            print(f"\n   ⚠️ 发现现有持仓:")
            for pos in positions:
                print(f"      {pos.symbol} {pos.side} {pos.quantity} @ {pos.entry_price}")
    
    def _get_quantity_precision(self, symbol: str) -> int:
        """获取数量精度"""
        info = self.symbol_info.get(symbol, {})
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step = float(f["stepSize"])
                if step >= 1:
                    return 0
                return len(str(step).split(".")[-1].rstrip("0"))
        return 3
    
    def _format_quantity(self, symbol: str, quantity: float) -> float:
        """格式化数量"""
        precision = self._get_quantity_precision(symbol)
        return round(quantity, precision)
    
    def open_position(self, symbol: str, direction: str, spike_id: str) -> Optional[TradeRecord]:
        """
        开仓
        
        Args:
            symbol: 交易对
            direction: UP (做空) / DOWN (做多)
            spike_id: 信号ID
        
        Returns:
            TradeRecord if successful
        """
        with self.lock:
            if symbol in self.active_trades:
                print(f"   ⚠️ {symbol} 已有持仓，跳过")
                return None
        
        try:
            # 获取当前价格
            ticker = self.client.get_ticker_price(symbol)
            current_price = float(ticker["price"])
            
            # 计算仓位大小
            capital = TRADING_CONFIG["capital"]
            leverage = TRADING_CONFIG["leverage"]
            position_value = capital * leverage
            quantity = position_value / current_price
            quantity = self._format_quantity(symbol, quantity)
            
            # 确定方向
            side = "SELL" if direction == "UP" else "BUY"
            
            # 下单
            signal_time = get_beijing_time()
            print(f"\n[{format_time()}] 🚀 开仓: {symbol} {side} {quantity}")
            
            order = self.client.place_market_order(symbol, side, quantity)
            entry_time = get_beijing_time()
            
            print(f"   订单ID: {order.order_id}")
            print(f"   状态: {order.status}")
            print(f"   成交价: {order.avg_price}")
            
            if order.status != "FILLED":
                print(f"   ❌ 订单未完全成交")
                return None
            
            # 创建交易记录
            trade_id = f"{symbol}_{entry_time.strftime('%Y%m%d%H%M%S%f')}"
            trade = TradeRecord(
                id=trade_id,
                spike_id=spike_id,
                symbol=symbol,
                direction=direction,
                entry_time=entry_time,
                entry_order_id=order.order_id,
                entry_price=order.avg_price,
                entry_quantity=order.executed_qty,
                entry_commission=0,  # TODO: 获取实际手续费
                signal_to_entry_ms=int((entry_time - signal_time).total_seconds() * 1000)
            )
            
            # 设置止盈止损
            self._set_tp_sl(trade)
            
            with self.lock:
                self.active_trades[symbol] = trade
                self.trades.append(trade)
            
            print(f"   ✅ 开仓成功，延迟: {trade.signal_to_entry_ms}ms")
            return trade
            
        except Exception as e:
            print(f"   ❌ 开仓失败: {e}")
            return None
    
    def _set_tp_sl(self, trade: TradeRecord):
        """设置止盈止损订单"""
        symbol = trade.symbol
        entry_price = trade.entry_price
        quantity = trade.entry_quantity
        leverage = TRADING_CONFIG["leverage"]
        tp_percent = TRADING_CONFIG["take_profit"]
        sl_percent = TRADING_CONFIG["stop_loss"]
        
        # 计算价格
        if trade.direction == "UP":
            # 做空: 止盈价 < 入场价, 止损价 > 入场价
            tp_price = entry_price * (1 - tp_percent / 100 / leverage)
            sl_price = entry_price * (1 + sl_percent / 100 / leverage)
            close_side = "BUY"
        else:
            # 做多: 止盈价 > 入场价, 止损价 < 入场价
            tp_price = entry_price * (1 + tp_percent / 100 / leverage)
            sl_price = entry_price * (1 - sl_percent / 100 / leverage)
            close_side = "SELL"
        
        # 格式化价格
        tp_price = round(tp_price, 2)
        sl_price = round(sl_price, 2)
        
        print(f"   设置止盈: {tp_price} ({tp_percent}%)")
        print(f"   设置止损: {sl_price} ({sl_percent}%)")
        
        try:
            # 止盈单
            self.client.place_take_profit_market_order(
                symbol, close_side, quantity, tp_price
            )
            
            # 止损单
            self.client.place_stop_market_order(
                symbol, close_side, quantity, sl_price
            )
            
        except Exception as e:
            print(f"   ⚠️ 设置止盈止损失败: {e}")
    
    def close_position(self, symbol: str, reason: str = "MANUAL") -> Optional[TradeRecord]:
        """平仓"""
        with self.lock:
            if symbol not in self.active_trades:
                return None
            trade = self.active_trades[symbol]
        
        try:
            # 取消所有挂单
            self.client.cancel_all_orders(symbol)
            
            # 获取当前持仓
            positions = self.client.get_positions()
            position = None
            for pos in positions:
                if pos.symbol == symbol:
                    position = pos
                    break
            
            if not position:
                print(f"   ⚠️ 未找到持仓")
                with self.lock:
                    del self.active_trades[symbol]
                return trade
            
            # 平仓
            close_side = "BUY" if position.side == "SHORT" else "SELL"
            
            print(f"\n[{format_time()}] 🔒 平仓: {symbol} {close_side} {position.quantity}")
            
            order = self.client.place_market_order(
                symbol, close_side, position.quantity, reduce_only=True
            )
            
            exit_time = get_beijing_time()
            
            # 更新交易记录
            trade.exit_time = exit_time
            trade.exit_order_id = order.order_id
            trade.exit_price = order.avg_price
            trade.exit_reason = reason
            trade.entry_to_exit_ms = int((exit_time - trade.entry_time).total_seconds() * 1000)
            
            # 计算盈亏
            if trade.direction == "UP":
                price_change = (trade.entry_price - trade.exit_price) / trade.entry_price
            else:
                price_change = (trade.exit_price - trade.entry_price) / trade.entry_price
            
            trade.pnl_percent = price_change * TRADING_CONFIG["leverage"] * 100
            trade.pnl_usdt = TRADING_CONFIG["capital"] * trade.pnl_percent / 100
            
            with self.lock:
                del self.active_trades[symbol]
            
            pnl_icon = "📈" if trade.pnl_usdt > 0 else "📉"
            print(f"   {pnl_icon} 盈亏: {trade.pnl_usdt:+.2f} USDT ({trade.pnl_percent:+.2f}%)")
            print(f"   持仓时间: {trade.entry_to_exit_ms}ms")
            
            return trade
            
        except Exception as e:
            print(f"   ❌ 平仓失败: {e}")
            return None
    
    def get_statistics(self) -> dict:
        """获取统计数据"""
        completed_trades = [t for t in self.trades if t.exit_time is not None]
        
        if not completed_trades:
            return {
                "total_trades": 0,
                "win_rate": 0,
                "total_pnl": 0,
                "avg_pnl": 0,
            }
        
        wins = [t for t in completed_trades if t.pnl_usdt > 0]
        
        return {
            "total_trades": len(completed_trades),
            "wins": len(wins),
            "losses": len(completed_trades) - len(wins),
            "win_rate": len(wins) / len(completed_trades) * 100,
            "total_pnl": sum(t.pnl_usdt for t in completed_trades),
            "avg_pnl": sum(t.pnl_usdt for t in completed_trades) / len(completed_trades),
            "avg_entry_delay_ms": sum(t.signal_to_entry_ms for t in completed_trades) / len(completed_trades),
            "avg_hold_time_ms": sum(t.entry_to_exit_ms for t in completed_trades) / len(completed_trades),
        }


# ============== 用户数据流（实时订单更新） ==============

class UserDataStream:
    """用户数据流 - 监听订单和持仓更新"""
    
    def __init__(self, client: BinanceFuturesClient, trading_manager: TradingManager):
        self.client = client
        self.trading_manager = trading_manager
        self.listen_key = None
        self.ws = None
        self.running = False
        
    def start(self):
        """启动"""
        self.running = True
        
        # 获取listenKey
        result = self.client._request("POST", "/fapi/v1/listenKey", signed=True)
        self.listen_key = result.get("listenKey")
        
        if not self.listen_key:
            print("❌ 获取listenKey失败")
            return
        
        print(f"[{format_time()}] 用户数据流已启动")
        
        # 启动WebSocket
        ws_url = f"{self.client.ws_url}/ws/{self.listen_key}"
        
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open
        )
        
        self.ws_thread = threading.Thread(target=self._run_ws)
        self.ws_thread.daemon = True
        self.ws_thread.start()
        
        # 定期延长listenKey
        self.keepalive_thread = threading.Thread(target=self._keepalive_loop)
        self.keepalive_thread.daemon = True
        self.keepalive_thread.start()
    
    def stop(self):
        """停止"""
        self.running = False
        if self.ws:
            self.ws.close()
    
    def _run_ws(self):
        if PROXY_CONFIG["enabled"]:
            self.ws.run_forever(
                http_proxy_host=PROXY_CONFIG["host"],
                http_proxy_port=PROXY_CONFIG["http_port"],
                proxy_type="http"
            )
        else:
            self.ws.run_forever()
    
    def _on_open(self, ws):
        print(f"[{format_time()}] ✅ 用户数据流已连接")
    
    def _on_error(self, ws, error):
        if error:
            print(f"[{format_time()}] 用户数据流错误: {error}")
    
    def _on_close(self, ws, code, msg):
        print(f"[{format_time()}] 用户数据流断开: {code} {msg}")
        if self.running:
            time.sleep(5)
            self.start()
    
    def _on_message(self, ws, message):
        """处理用户数据消息"""
        try:
            data = json.loads(message)
            event_type = data.get("e")
            
            if event_type == "ORDER_TRADE_UPDATE":
                self._handle_order_update(data)
            elif event_type == "ACCOUNT_UPDATE":
                self._handle_account_update(data)
                
        except Exception as e:
            print(f"处理用户数据消息错误: {e}")
    
    def _handle_order_update(self, data):
        """处理订单更新"""
        order = data.get("o", {})
        symbol = order.get("s")
        order_id = order.get("i")
        status = order.get("X")  # NEW, PARTIALLY_FILLED, FILLED, CANCELED, EXPIRED
        order_type = order.get("o")  # LIMIT, MARKET, STOP_MARKET, TAKE_PROFIT_MARKET
        side = order.get("S")
        
        print(f"[{format_time()}] 📋 订单更新: {symbol} {order_type} {side} -> {status}")
        
        # 如果是止盈止损订单成交，更新交易记录
        if status == "FILLED" and order_type in ["STOP_MARKET", "TAKE_PROFIT_MARKET"]:
            with self.trading_manager.lock:
                if symbol in self.trading_manager.active_trades:
                    trade = self.trading_manager.active_trades[symbol]
                    trade.exit_time = get_beijing_time()
                    trade.exit_order_id = str(order_id)
                    trade.exit_price = float(order.get("ap", 0))  # 平均成交价
                    trade.exit_reason = "TP" if order_type == "TAKE_PROFIT_MARKET" else "SL"
                    trade.entry_to_exit_ms = int((trade.exit_time - trade.entry_time).total_seconds() * 1000)
                    
                    # 计算盈亏
                    if trade.direction == "UP":
                        price_change = (trade.entry_price - trade.exit_price) / trade.entry_price
                    else:
                        price_change = (trade.exit_price - trade.entry_price) / trade.entry_price
                    
                    trade.pnl_percent = price_change * TRADING_CONFIG["leverage"] * 100
                    trade.pnl_usdt = TRADING_CONFIG["capital"] * trade.pnl_percent / 100
                    
                    del self.trading_manager.active_trades[symbol]
                    
                    icon = "✅" if trade.pnl_usdt > 0 else "❌"
                    print(f"   {icon} {trade.exit_reason} 触发: {trade.pnl_usdt:+.2f} USDT")
    
    def _handle_account_update(self, data):
        """处理账户更新"""
        # 可以记录余额变化等
        pass
    
    def _keepalive_loop(self):
        """保持listenKey活跃"""
        while self.running:
            time.sleep(30 * 60)  # 30分钟
            try:
                self.client._request("PUT", "/fapi/v1/listenKey", signed=True)
                print(f"[{format_time()}] listenKey已延长")
            except Exception as e:
                print(f"延长listenKey失败: {e}")


# ============== 插针检测器（带真实交易） ==============

class LiveTradingDetector:
    """带真实交易功能的插针检测器"""
    
    def __init__(self, client: BinanceFuturesClient, trading_manager: TradingManager):
        self.client = client
        self.trading_manager = trading_manager
        self.symbols = [s.lower() for s in TRADING_CONFIG["symbols"]]
        
        # 价格监控
        self.prices: Dict[str, float] = {}
        self.price_windows: Dict[str, deque] = {
            s.upper(): deque(maxlen=100) for s in self.symbols
        }
        
        self.ws = None
        self.running = False
        self.spike_counter = 0
        self.detected_spikes = []
        
    def start(self):
        """启动"""
        self.running = True
        self._connect()
    
    def stop(self):
        """停止"""
        self.running = False
        if self.ws:
            self.ws.close()
    
    def _connect(self):
        """连接行情WebSocket"""
        streams = [f"{s}@aggTrade" for s in self.symbols]
        ws_url = f"{self.client.ws_url}/ws/{'/'.join(streams)}"
        
        print(f"[{format_time()}] 连接行情WebSocket...")
        
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open
        )
        
        self.ws_thread = threading.Thread(target=self._run_ws)
        self.ws_thread.daemon = True
        self.ws_thread.start()
    
    def _run_ws(self):
        if PROXY_CONFIG["enabled"]:
            self.ws.run_forever(
                http_proxy_host=PROXY_CONFIG["host"],
                http_proxy_port=PROXY_CONFIG["http_port"],
                proxy_type="http"
            )
        else:
            self.ws.run_forever()
    
    def _on_open(self, ws):
        print(f"[{format_time()}] ✅ 行情WebSocket已连接")
    
    def _on_error(self, ws, error):
        if error:
            print(f"[{format_time()}] 行情WebSocket错误: {error}")
    
    def _on_close(self, ws, code, msg):
        print(f"[{format_time()}] 行情WebSocket断开")
        if self.running:
            time.sleep(2)
            self._connect()
    
    def _on_message(self, ws, message):
        """处理行情消息"""
        try:
            data = json.loads(message)
            symbol = data.get("s", "").upper()
            price = float(data["p"])
            timestamp = data["T"]
            
            self.prices[symbol] = price
            self.price_windows[symbol].append({
                "price": price,
                "timestamp": timestamp
            })
            
            # 检测插针
            self._detect_spike(symbol, price, timestamp)
            
        except Exception as e:
            pass
    
    def _detect_spike(self, symbol: str, price: float, timestamp: int):
        """检测插针信号"""
        window = self.price_windows[symbol]
        if len(window) < 10:
            return
        
        # 获取最近1秒的数据
        now_ms = timestamp
        recent_prices = [
            p["price"] for p in window 
            if now_ms - p["timestamp"] <= 1000
        ]
        
        if len(recent_prices) < 5:
            return
        
        start_price = recent_prices[0]
        high_price = max(recent_prices)
        low_price = min(recent_prices)
        
        if start_price == 0:
            return
        
        # 计算幅度
        up_amplitude = (high_price - start_price) / start_price * 100
        down_amplitude = (start_price - low_price) / start_price * 100
        
        threshold = TRADING_CONFIG["spike_threshold"]
        retracement_threshold = TRADING_CONFIG["retracement_threshold"]
        
        direction = None
        amplitude = 0
        peak_price = 0
        
        # 上插针检测
        if up_amplitude >= threshold and high_price > start_price:
            retracement = (high_price - price) / (high_price - start_price) * 100
            if retracement >= retracement_threshold:
                direction = "UP"
                amplitude = up_amplitude
                peak_price = high_price
        
        # 下插针检测
        if direction is None and down_amplitude >= threshold and start_price > low_price:
            retracement = (price - low_price) / (start_price - low_price) * 100
            if retracement >= retracement_threshold:
                direction = "DOWN"
                amplitude = down_amplitude
                peak_price = low_price
        
        if direction:
            # 防止重复信号
            if self.detected_spikes:
                last_spike = self.detected_spikes[-1]
                if (last_spike["symbol"] == symbol and 
                    last_spike["direction"] == direction and
                    timestamp - last_spike["timestamp"] < 5000):
                    return
            
            self.spike_counter += 1
            spike_id = f"{symbol}_{timestamp}_{self.spike_counter}"
            
            spike_info = {
                "id": spike_id,
                "symbol": symbol,
                "direction": direction,
                "amplitude": amplitude,
                "peak_price": peak_price,
                "current_price": price,
                "timestamp": timestamp,
                "time": format_time()
            }
            
            self.detected_spikes.append(spike_info)
            
            icon = "🔺" if direction == "UP" else "🔻"
            print(f"\n[{format_time()}] {icon} 检测到插针: {symbol} {direction} 幅度:{amplitude:.2f}%")
            
            # 执行交易
            self._execute_trade(spike_info)
    
    def _execute_trade(self, spike_info: dict):
        """执行交易"""
        symbol = spike_info["symbol"]
        direction = spike_info["direction"]
        spike_id = spike_info["id"]
        
        # 检查是否已有持仓
        if symbol in self.trading_manager.active_trades:
            print(f"   ⚠️ {symbol} 已有持仓，跳过")
            return
        
        # 开仓
        trade = self.trading_manager.open_position(symbol, direction, spike_id)
        
        if trade:
            spike_info["trade_id"] = trade.id
            spike_info["entry_price"] = trade.entry_price
            spike_info["entry_delay_ms"] = trade.signal_to_entry_ms


# ============== 数据保存 ==============

def save_trades_to_file(trades: List[TradeRecord], filename: str = None):
    """保存交易记录到文件"""
    if not trades:
        return
    
    if filename is None:
        filename = f"trades_{get_beijing_time().strftime('%Y%m%d_%H%M%S')}.json"
    
    data = []
    for trade in trades:
        data.append({
            "id": trade.id,
            "spike_id": trade.spike_id,
            "symbol": trade.symbol,
            "direction": trade.direction,
            "entry_time": format_time(trade.entry_time) if trade.entry_time else None,
            "entry_price": trade.entry_price,
            "entry_quantity": trade.entry_quantity,
            "exit_time": format_time(trade.exit_time) if trade.exit_time else None,
            "exit_price": trade.exit_price,
            "exit_reason": trade.exit_reason,
            "pnl_usdt": trade.pnl_usdt,
            "pnl_percent": trade.pnl_percent,
            "signal_to_entry_ms": trade.signal_to_entry_ms,
            "entry_to_exit_ms": trade.entry_to_exit_ms,
        })
    
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"交易记录已保存到: {filename}")


def print_statistics(trading_manager: TradingManager):
    """打印统计信息"""
    stats = trading_manager.get_statistics()
    
    print("\n" + "=" * 60)
    print("                    交易统计")
    print("=" * 60)
    print(f"总交易次数: {stats['total_trades']}")
    print(f"盈利次数:   {stats.get('wins', 0)}")
    print(f"亏损次数:   {stats.get('losses', 0)}")
    print(f"胜率:       {stats['win_rate']:.1f}%")
    print(f"总盈亏:     {stats['total_pnl']:+.2f} USDT")
    print(f"平均盈亏:   {stats['avg_pnl']:+.2f} USDT")
    
    if stats['total_trades'] > 0:
        print(f"平均入场延迟: {stats['avg_entry_delay_ms']:.0f}ms")
        print(f"平均持仓时间: {stats['avg_hold_time_ms']:.0f}ms")
    
    print("=" * 60)


# ============== 主函数 ==============

def main():
    print("=" * 60)
    print("    Flash Arbitrage Bot - 币安测试网模拟交易")
    print("=" * 60)
    
    # 检查API配置
    config = API_CONFIG[ENVIRONMENT]
    if not config["api_key"] or not config["api_secret"]:
        print("\n❌ 请先配置API Key和Secret!")
        print("\n获取测试网API Key的步骤:")
        print("1. 访问 https://testnet.binancefuture.com/")
        print("2. 使用GitHub账号登录")
        print("3. 在API Management中创建API Key")
        print("4. 将API Key和Secret填入脚本的API_CONFIG中")
        return
    
    print(f"\n环境: {ENVIRONMENT}")
    print(f"代理: {'启用' if PROXY_CONFIG['enabled'] else '禁用'}")
    print(f"交易对: {TRADING_CONFIG['symbols']}")
    print(f"杠杆: {TRADING_CONFIG['leverage']}x")
    print(f"止盈: {TRADING_CONFIG['take_profit']}%")
    print(f"止损: {TRADING_CONFIG['stop_loss']}%")
    
    # 创建客户端
    client = BinanceFuturesClient(ENVIRONMENT)
    
    # 测试连接
    print("\n🔗 测试API连接...")
    try:
        server_time = client._request("GET", "/fapi/v1/time")
        print(f"   服务器时间: {server_time}")
        
        balance = client.get_balance()
        print(f"   账户余额: {balance}")
    except Exception as e:
        print(f"❌ 连接失败: {e}")
        return
    
    print("✅ 连接成功")
    
    # 创建交易管理器
    trading_manager = TradingManager(client)
    trading_manager.initialize(TRADING_CONFIG["symbols"])
    
    # 启动用户数据流
    user_stream = UserDataStream(client, trading_manager)
    user_stream.start()
    
    # 启动检测器
    detector = LiveTradingDetector(client, trading_manager)
    detector.start()
    
    print(f"\n🚀 系统已启动，按 Ctrl+C 停止...")
    
    try:
        while True:
            time.sleep(10)
            
            # 定期打印状态
            active_count = len(trading_manager.active_trades)
            completed_count = len([t for t in trading_manager.trades if t.exit_time])
            
            print(f"\r[{format_time()}] 活跃持仓: {active_count} | 已完成: {completed_count} | "
                  f"检测信号: {len(detector.detected_spikes)}", end="")
            
    except KeyboardInterrupt:
        print("\n\n👋 正在停止...")
    
    finally:
        detector.stop()
        user_stream.stop()
        
        # 平掉所有持仓
        if trading_manager.active_trades:
            print("\n关闭所有持仓...")
            for symbol in list(trading_manager.active_trades.keys()):
                trading_manager.close_position(symbol, "SHUTDOWN")
        
        # 打印统计
        print_statistics(trading_manager)
        
        # 保存交易记录
        if trading_manager.trades:
            save_trades_to_file(trading_manager.trades)


if __name__ == "__main__":
    main()
