"""币安期货API客户端 - 支持测试网和主网

提供完整的币安合约API封装，包括账户信息查询、订单管理、持仓查询、K线数据获取等。
"""

import hashlib
import hmac
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

import requests


_logger = None


def _get_logger():
    """获取日志器实例"""
    global _logger
    if _logger is None:
        try:
            from ..utils.logger import get_logger
            _logger = get_logger()
        except ImportError:
            _logger = None
    return _logger


class SideType(Enum):
    """订单方向"""
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(Enum):
    """持仓方向"""
    LONG = "LONG"
    SHORT = "SHORT"
    BOTH = "BOTH"


class OrderType(Enum):
    """订单类型"""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    STOP_LIMIT = "STOP_LIMIT"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    TAKE_PROFIT_LIMIT = "TAKE_PROFIT_LIMIT"


class TimeInForce(Enum):
    """订单时效"""
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    GTX = "GTX"


class WorkingType(Enum):
    """止损/止盈触发类型"""
    MARK_PRICE = "MARK_PRICE"
    CONTRACT_PRICE = "CONTRACT_PRICE"


@dataclass
class OrderResult:
    """订单结果"""
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    status: str
    quantity: float
    price: Optional[float] = None
    avg_price: Optional[float] = None
    execute_qty: float = 0
    commission: float = 0
    time_in_force: str = "GTC"
    raw: Dict = None

    @classmethod
    def from_response(cls, data: Dict) -> "OrderResult":
        avg_price_str = data.get("avgPrice", "0")
        avg_price = float(avg_price_str) if avg_price_str and avg_price_str != "0" else None

        return cls(
            order_id=str(data.get("orderId", "")),
            client_order_id=data.get("clientOrderId", ""),
            symbol=data.get("symbol", ""),
            side=data.get("side", ""),
            order_type=data.get("type", ""),
            status=data.get("status", ""),
            quantity=float(data.get("origQty", 0)),
            price=float(data.get("price", 0)) if data.get("price") else None,
            avg_price=avg_price,
            execute_qty=float(data.get("executedQty", 0)),
            commission=0,
            time_in_force=data.get("timeInForce", "GTC"),
            raw=data
        )

    def update_commission(self, commission: float):
        self.commission = commission


@dataclass
class UserTrade:
    """成交记录（包含实际手续费）"""
    trade_id: str
    order_id: str
    symbol: str
    side: str
    price: float
    qty: float
    quote_qty: float
    commission: float              # 实际手续费
    commission_asset: str          # 手续费币种
    time: int                      # 成交时间
    is_maker: bool                 # 是否是Maker
    raw: Dict = None

    @classmethod
    def from_response(cls, data: Dict) -> "UserTrade":
        return cls(
            trade_id=str(data.get("id", "")),
            order_id=str(data.get("orderId", "")),
            symbol=data.get("symbol", ""),
            side=data.get("side", ""),
            price=float(data.get("price", 0)),
            qty=float(data.get("qty", 0)),
            quote_qty=float(data.get("quoteQty", 0)),
            commission=float(data.get("commission", 0)),
            commission_asset=data.get("commissionAsset", ""),
            time=int(data.get("time", 0)),
            is_maker=data.get("isMaker", False),
            raw=data
        )


@dataclass
class Position:
    """持仓信息"""
    symbol: str
    position_amount: float
    entry_price: float
    mark_price: float
    un_realized_profit: float
    liquidation_price: float
    leverage: int
    max_notional_value: float
    margin_type: str
    isolated_margin: float
    is_auto_add_margin: str
    position_side: str
    notional: float
    isolated_wallet: float
    update_time: int

    @classmethod
    def from_response(cls, data: Dict) -> "Position":
        return cls(
            symbol=data.get("symbol", ""),
            position_amount=float(data.get("positionAmt", 0)),
            entry_price=float(data.get("entryPrice", 0)),
            mark_price=float(data.get("markPrice", 0)),
            un_realized_profit=float(data.get("unRealizedProfit", 0)),
            liquidation_price=float(data.get("liquidationPrice", 0)),
            leverage=int(data.get("leverage", 1)),
            max_notional_value=float(data.get("maxNotionalValue", 0)),
            margin_type=data.get("marginType", ""),
            isolated_margin=float(data.get("isolatedMargin", 0)),
            is_auto_add_margin=data.get("isAutoAddMargin", ""),
            position_side=data.get("positionSide", ""),
            notional=float(data.get("notional", 0)),
            isolated_wallet=float(data.get("isolatedWallet", 0)),
            update_time=int(data.get("updateTime", 0))
        )


@dataclass
class AccountInfo:
    """账户信息"""
    fee_tier: int
    can_trade: bool
    can_deposit: bool
    can_withdraw: bool
    update_time: int
    total_initial_margin: float
    total_maintenance_margin: float
    total_wallet_balance: float
    total_unrealized_profit: float
    total_margin_balance: float
    total_position_initial_margin: float
    total_open_order_initial_margin: float
    total_cross_wallet_balance: float
    total_cross_unrealized_profit: float
    available_balance: float
    max_withdraw_amount: float

    @classmethod
    def from_response(cls, data: Dict) -> "AccountInfo":
        return cls(
            fee_tier=int(data.get("feeTier", 0)),
            can_trade=data.get("canTrade", False),
            can_deposit=data.get("canDeposit", False),
            can_withdraw=data.get("canWithdraw", False),
            update_time=int(data.get("updateTime", 0)),
            total_initial_margin=float(data.get("totalInitialMargin", 0)),
            total_maintenance_margin=float(data.get("totalMaintMargin", 0)),
            total_wallet_balance=float(data.get("totalWalletBalance", 0)),
            total_unrealized_profit=float(data.get("totalUnrealizedProfit", 0)),
            total_margin_balance=float(data.get("totalMarginBalance", 0)),
            total_position_initial_margin=float(data.get("totalPositionInitialMargin", 0)),
            total_open_order_initial_margin=float(data.get("totalOpenOrderInitialMargin", 0)),
            total_cross_wallet_balance=float(data.get("totalCrossWalletBalance", 0)),
            total_cross_unrealized_profit=float(data.get("totalCrossUnrealizedProfit", 0)),
            available_balance=float(data.get("availableBalance", 0)),
            max_withdraw_amount=float(data.get("maxWithdrawAmount", 0))
        )


class BinanceFuturesClient:
    """币安期货API客户端 - 支持测试网和主网"""

    TESTNET_URL = "https://testnet.binancefuture.com"
    MAINNET_URL = "https://fapi.binance.com"
    TESTNET_WS_URL = "wss://stream.binancefuture.com/ws"
    MAINNET_WS_URL = "wss://fstream.binance.com/ws"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        timeout: int = 10,
        enable_proxy: bool = False,
        proxy_url: str = None
    ):
        """初始化客户端"""
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.timeout = timeout
        self._recv_window = 5000

        self.base_url = self.TESTNET_URL if testnet else self.MAINNET_URL
        self.ws_url = self.TESTNET_WS_URL if testnet else self.MAINNET_WS_URL

        self.session = requests.Session()
        self.session.headers.update({
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/json"
        })

        if enable_proxy and proxy_url:
            self.session.proxies = {
                "http": proxy_url,
                "https": proxy_url
            }

    def _generate_signature(self, params: Dict) -> str:
        """生成请求签名"""
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _request(
        self,
        method: str,
        endpoint: str,
        signed: bool = False,
        **kwargs
    ) -> Dict:
        """发送HTTP请求"""
        logger = _get_logger()

        if logger and signed:
            logger.api_request(method, endpoint, kwargs.get("params"))

        url = f"{self.base_url}{endpoint}"

        if signed:
            params = kwargs.get("params", {})
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = self._recv_window
            signature = self._generate_signature(params)
            query_string = self._prepare_query_string(params)
            url = f"{url}?{query_string}&signature={signature}"
            kwargs.pop("params", None)

        try:
            response = self.session.request(
                method=method,
                url=url,
                timeout=self.timeout,
                **kwargs
            )
            response.raise_for_status()
            response_data = response.json()

            if logger and signed:
                logger.api_response(method, endpoint, response_data)

            return response_data

        except requests.exceptions.HTTPError as e:
            error_data = self._build_error_data(e)
            if logger:
                logger.api_response(method, endpoint, error=error_data)
            return error_data

        except requests.exceptions.RequestException as e:
            error_data = {"error": True, "message": str(e), "response": {}}
            if logger:
                logger.api_response(method, endpoint, error=error_data)
            return error_data

    def _build_error_data(self, error: requests.exceptions.HTTPError) -> Dict:
        """构建错误响应数据"""
        error_data = {"error": True, "message": str(error), "response": {}}
        if hasattr(error, "response") and error.response is not None:
            try:
                json_data = error.response.json()
                error_data["response"] = json_data
                if "code" in json_data:
                    error_data["code"] = json_data["code"]
                    error_data["msg"] = json_data["msg"]
            except Exception:
                pass
        return error_data

    def _prepare_query_string(self, params: Dict) -> str:
        """准备查询字符串"""
        return "&".join([f"{k}={v}" for k, v in params.items()])

    def get_account_info(self) -> Optional[AccountInfo]:
        """获取账户信息"""
        response = self._request("GET", "/fapi/v2/account", signed=True)
        if not response.get("error"):
            return AccountInfo.from_response(response)
        return None

    def get_balance(self) -> List[Dict]:
        """获取账户余额"""
        response = self._request("GET", "/fapi/v2/balance", signed=True)
        return response if not response.get("error") else []

    def get_position(self, symbol: str = None) -> List[Position]:
        """获取持仓信息"""
        params = {"symbol": symbol} if symbol else {}
        response = self._request("GET", "/fapi/v2/positionRisk", signed=True, params=params)
        if not response.get("error"):
            return [Position.from_response(p) for p in response if float(p.get("positionAmt", 0)) != 0]
        return []

    def set_leverage(self, leverage: int, symbol: str) -> bool:
        """设置杠杆倍数"""
        response = self._request(
            "POST", "/fapi/v1/leverage",
            signed=True,
            data={"leverage": leverage, "symbol": symbol}
        )
        return not response.get("error") and response.get("leverage") == leverage

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> bool:
        """设置保证金模式"""
        response = self._request(
            "POST", "/fapi/v1/marginType",
            signed=True,
            data={"symbol": symbol, "marginType": margin_type}
        )
        return not response.get("error")

    def set_position_mode(self, dual_side: bool = True) -> bool:
        """设置持仓模式"""
        response = self._request(
            "POST", "/fapi/v1/positionSide/dual",
            signed=True,
            data={"dualSidePosition": "true" if dual_side else "false"}
        )
        return not response.get("error")

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: float = None,
        stop_price: float = None,
        position_side: str = None,
        time_in_force: str = "GTC",
        working_type: str = "MARK_PRICE",
        reduce_only: bool = False,
        close_position: bool = False,
        new_client_order_id: str = None,
        callback_rate: float = None,
        price_protect: bool = False
    ) -> Optional[OrderResult]:
        """下单

        Args:
            symbol: 交易对
            side: 订单方向 (BUY/SELL)
            order_type: 订单类型 (MARKET/LIMIT/STOP_MARKET等)
            quantity: 数量
            price: 价格(限价单必填)
            stop_price: 止损/止盈触发价
            position_side: 持仓方向 (LONG/SHORT) - Hedge Mode下必填
            time_in_force: 订单时效
            working_type: 触发价格类型
            reduce_only: 是否只减仓 (Hedge Mode下无效)
            close_position: 是否平仓
            new_client_order_id: 客户端订单ID
            callback_rate: 跟踪止损回调率
            price_protect: 是否开启条件单触发保护
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": self._format_quantity(symbol, quantity),
        }

        if position_side:
            params["positionSide"] = position_side

        # 限价单需要价格
        limit_types = ("LIMIT", "STOP_LIMIT", "TAKE_PROFIT_LIMIT")
        if order_type in limit_types:
            if price is None:
                raise ValueError("限价单必须指定价格")
            params["price"] = self._format_price(symbol, price)
            params["timeInForce"] = time_in_force

        # 止损/止盈单设置
        if stop_price and ("STOP" in order_type or "TAKE_PROFIT" in order_type):
            params["stopPrice"] = self._format_price(symbol, stop_price)
            params["workingType"] = working_type
            if price_protect:
                params["priceProtect"] = "TRUE"

        # Hedge Mode 下不接受 reduceOnly 参数
        if reduce_only and not position_side:
            params["reduceOnly"] = "true"

        if close_position:
            params["closePosition"] = "true"

        if new_client_order_id:
            params["newClientOrderId"] = new_client_order_id

        if callback_rate and "TRAILING" in order_type:
            params["callbackRate"] = callback_rate

        response = self._request("POST", "/fapi/v1/order", signed=True, params=params)

        # 检查错误响应
        if isinstance(response, dict):
            code = response.get("code")
            if (code is not None and code < 0) or response.get("error"):
                return self._create_error_order(symbol, side, order_type, quantity, response)

        return OrderResult.from_response(response)

    def _create_error_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        error_response: Dict
    ) -> OrderResult:
        """创建包含错误信息的订单结果"""
        return OrderResult(
            order_id="",
            client_order_id="",
            symbol=symbol,
            side=side,
            order_type=order_type,
            status="REJECTED",
            quantity=quantity,
            raw=error_response
        )

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        position_side: str = None,
        reduce_only: bool = False,
        close_position: bool = False
    ) -> Optional[OrderResult]:
        """下市价单"""
        return self.place_order(
            symbol=symbol,
            side=side,
            order_type="MARKET",
            quantity=quantity,
            position_side=position_side,
            reduce_only=reduce_only,
            close_position=close_position
        )

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        position_side: str = None,
        time_in_force: str = "GTC",
        reduce_only: bool = False
    ) -> Optional[OrderResult]:
        """下限价单"""
        return self.place_order(
            symbol=symbol,
            side=side,
            order_type="LIMIT",
            quantity=quantity,
            price=price,
            position_side=position_side,
            time_in_force=time_in_force,
            reduce_only=reduce_only
        )

    def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        quantity: float = None,
        position_side: str = None,
        close_position: bool = False,
        reduce_only: bool = False,
        working_type: str = "MARK_PRICE",
        price_protect: bool = True
    ) -> Optional[OrderResult]:
        """下止损市价单"""
        if not close_position and (quantity is None or quantity <= 0):
            raise ValueError(f"止损单必须指定有效的 quantity，收到: {quantity}")

        return self.place_order(
            symbol=symbol,
            side=side,
            order_type="STOP_MARKET",
            quantity=quantity if quantity is not None else 0,
            stop_price=stop_price,
            position_side=position_side,
            close_position=close_position,
            reduce_only=reduce_only,
            working_type=working_type,
            price_protect=price_protect
        )

    def place_take_profit_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        quantity: float = None,
        position_side: str = None,
        close_position: bool = False,
        reduce_only: bool = False,
        working_type: str = "MARK_PRICE",
        price_protect: bool = True
    ) -> Optional[OrderResult]:
        """下止盈市价单"""
        if not close_position and (quantity is None or quantity <= 0):
            raise ValueError(f"止盈单必须指定有效的 quantity，收到: {quantity}")

        return self.place_order(
            symbol=symbol,
            side=side,
            order_type="TAKE_PROFIT_MARKET",
            quantity=quantity if quantity is not None else 0,
            stop_price=stop_price,
            position_side=position_side,
            close_position=close_position,
            reduce_only=reduce_only,
            working_type=working_type,
            price_protect=price_protect
        )

    def cancel_order(
        self,
        symbol: str,
        order_id: str = None,
        client_order_id: str = None
    ) -> bool:
        """取消订单"""
        params = {"symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["origClientOrderId"] = client_order_id

        response = self._request("DELETE", "/fapi/v1/order", signed=True, params=params)
        return not response.get("error")

    def cancel_all_orders(self, symbol: str) -> bool:
        """取消某交易对所有挂单"""
        response = self._request(
            "DELETE", "/fapi/v1/allOpenOrders",
            signed=True,
            params={"symbol": symbol}
        )
        return not response.get("error")

    def get_open_orders(self, symbol: str = None) -> List[OrderResult]:
        """获取当前挂单"""
        params = {"symbol": symbol} if symbol else {}
        response = self._request("GET", "/fapi/v1/openOrders", signed=True, params=params)

        if isinstance(response, list):
            return [OrderResult.from_response(o) for o in response]
        return []

    def get_order(
        self,
        symbol: str,
        order_id: str = None,
        client_order_id: str = None
    ) -> Optional[OrderResult]:
        """查询订单"""
        params = {"symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["origClientOrderId"] = client_order_id

        response = self._request("GET", "/fapi/v1/order", signed=True, params=params)
        if not response.get("error"):
            return OrderResult.from_response(response)
        return None

    def get_user_trades(
        self,
        symbol: str,
        order_id: str = None,
        start_time: int = None,
        end_time: int = None,
        limit: int = 500
    ) -> List[UserTrade]:
        """获取成交历史（包含实际手续费）"""
        params = {"symbol": symbol, "limit": min(limit, 1000)}
        if order_id:
            params["orderId"] = order_id
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        response = self._request("GET", "/fapi/v1/userTrades", signed=True, params=params)
        if isinstance(response, list):
            return [UserTrade.from_response(t) for t in response]
        return []

    def get_order_commission(self, symbol: str, order_id: str) -> float:
        """获取订单的总手续费"""
        trades = self.get_user_trades(symbol, order_id=order_id)
        return sum(t.commission for t in trades if t.commission_asset == "USDT")

    def get_ticker_price(self, symbol: str = None) -> Dict:
        """获取最新价格"""
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/ticker/price", params=params)

    def get_ticker_24h(self, symbol: str = None) -> Dict:
        """获取24小时价格变动"""
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/ticker/24hr", params=params)

    def get_depth(self, symbol: str, limit: int = 20) -> Dict:
        """获取深度信息"""
        return self._request(
            "GET", "/fapi/v1/depth",
            params={"symbol": symbol, "limit": limit}
        )

    def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 500,
        start_time: int = None,
        end_time: int = None
    ) -> List[List]:
        """获取K线数据"""
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        response = self._request("GET", "/fapi/v1/klines", params=params)
        return response if isinstance(response, list) else []

    def get_exchange_info(self) -> Dict:
        """获取交易规则和交易对信息"""
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def get_symbol_info(self, symbol: str) -> Optional[Dict]:
        """获取指定交易对信息"""
        info = self.get_exchange_info()
        for s in info.get("symbols", []):
            if s["symbol"] == symbol:
                return s
        return None

    def _format_quantity(self, symbol: str, quantity: float) -> str:
        """格式化数量（按交易对精度并去除尾随零）"""
        rounded = self.round_quantity(symbol, quantity)

        if rounded <= 0:
            return self._get_default_quantity(symbol)

        result = f"{rounded:.8f}".rstrip("0").rstrip(".")
        if not result or result in (".", "-"):
            return self._get_default_quantity(symbol)

        return result

    def _get_default_quantity(self, symbol: str) -> str:
        """获取交易对的默认数量值"""
        step = self.get_quantity_step(symbol)
        return str(int(1 / step)) if step < 1 else "1"

    def _format_price(self, symbol: str, price: float) -> str:
        """格式化价格（按交易对精度并去除尾随零）"""
        rounded = self.round_price(symbol, price)
        return f"{rounded:.8f}".rstrip("0").rstrip(".")

    def get_quantity_step(self, symbol: str) -> float:
        """获取交易对的数量精度"""
        if not hasattr(self, '_quantity_step_cache'):
            self._quantity_step_cache = {}

        if symbol in self._quantity_step_cache:
            return self._quantity_step_cache[symbol]

        # 从API获取
        info = self.get_symbol_info(symbol)
        if info:
            for f in info.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step = float(f["stepSize"])
                    self._quantity_step_cache[symbol] = step
                    return step

        # 使用已知的默认值
        step = self._KNOWN_STEPS.get(symbol, 1.0)
        if step == 1.0:
            print(f"[Warning] 无法获取 {symbol} 精度信息，使用默认值 1.0")
        self._quantity_step_cache[symbol] = step
        return step

    _KNOWN_STEPS = {
        # 主流币
        "BTCUSDT": 0.001, "ETHUSDT": 0.001, "BNBUSDT": 0.001, "SOLUSDT": 0.001,
        "ADAUSDT": 0.1, "AVAXUSDT": 0.01, "DOTUSDT": 0.01, "MATICUSDT": 0.1,
        "LINKUSDT": 0.01, "ATOMUSDT": 0.01, "UNIUSDT": 0.01, "AAVEUSDT": 0.01,
        "XRPUSDT": 0.1, "ZECUSDT": 0.01,
        # 新币/Meme币
        "TRUMPUSDT": 1.0, "BUSDT": 1.0, "4USDT": 1.0, "币安人生USDT": 1.0,
        "BREVUSDT": 1.0, "MIRAUSDT": 1.0, "COLLECTUSDT": 1.0, "GUNUSDT": 1.0,
        "RIVERUSDT": 1.0, "VVVUSDT": 1.0, "TAOUSDT": 1.0, "CCUSDT": 1.0,
    }

    def get_price_step(self, symbol: str) -> float:
        """获取交易对的价格精度"""
        info = self.get_symbol_info(symbol)
        if info:
            for f in info.get("filters", []):
                if f["filterType"] == "PRICE_FILTER":
                    return float(f["tickSize"])
        return 0.01

    def round_quantity(self, symbol: str, quantity: float) -> float:
        """按交易对精度四舍五入数量"""
        step = self.get_quantity_step(symbol)
        return round(quantity // step * step, 8)

    def round_price(self, symbol: str, price: float) -> float:
        """按交易对精度四舍五入价格"""
        step = self.get_price_step(symbol)
        return round(price // step * step, 8)

    def calculate_quantity(
        self,
        symbol: str,
        usdt_amount: float,
        price: float,
        leverage: int = 1
    ) -> float:
        """根据USDT金额计算下单数量"""
        notional_value = usdt_amount * leverage
        quantity = notional_value / price
        return self.round_quantity(symbol, quantity)

    def get_all_positions(self) -> List[Position]:
        """获取所有持仓信息（包括空仓）"""
        response = self._request("GET", "/fapi/v2/positionRisk", signed=True, params={})
        if not response.get("error"):
            return [Position.from_response(p) for p in response]
        return []

    def get_position_by_side(self, symbol: str, position_side: str) -> Optional[Position]:
        """获取指定方向的持仓"""
        positions = self.get_all_positions()
        for pos in positions:
            if pos.symbol == symbol and pos.position_side == position_side:
                if pos.position_amount != 0:
                    return pos
        return None

    def get_active_positions(self) -> Dict[str, Dict[str, Position]]:
        """获取所有活跃持仓（按交易对分组）"""
        positions = self.get_all_positions()
        result = {}
        for pos in positions:
            if pos.position_amount == 0:
                continue
            if pos.symbol not in result:
                result[pos.symbol] = {}
            result[pos.symbol][pos.position_side] = pos
        return result

    def get_account_summary(self) -> Dict:
        """获取账户摘要信息"""
        account = self.get_account_info()
        if account:
            return {
                "available_balance": account.available_balance,
                "wallet_balance": account.total_wallet_balance,
                "unrealized_pnl": account.total_unrealized_profit,
                "margin_balance": account.total_margin_balance,
                "position_initial_margin": account.total_position_initial_margin,
                "update_time": account.update_time
            }
        return {}

    def get_open_stop_orders(self, symbol: str = None) -> List[Dict]:
        """获取所有未成交的止损止盈订单"""
        all_orders = self.get_open_orders(symbol)
        stop_orders = []
        for order in all_orders:
            if "STOP" in order.order_type or "TAKE_PROFIT" in order.order_type:
                stop_orders.append({
                    "order_id": order.order_id,
                    "symbol": order.symbol,
                    "side": order.side,
                    "type": order.order_type,
                    "quantity": order.quantity,
                    "status": order.status,
                    "raw": order.raw
                })
        return stop_orders

    def test_connectivity(self) -> bool:
        """测试连接"""
        try:
            response = self.get_ticker_price()
            if isinstance(response, dict):
                return not response.get("error")
            if isinstance(response, list):
                return len(response) > 0
            return False
        except Exception:
            return False


def create_testnet_client(
    api_key: str,
    api_secret: str,
    enable_proxy: bool = False,
    proxy_url: str = None
) -> BinanceFuturesClient:
    """创建测试网客户端的便捷函数"""
    return BinanceFuturesClient(
        api_key=api_key,
        api_secret=api_secret,
        testnet=True,
        enable_proxy=enable_proxy,
        proxy_url=proxy_url
    )
