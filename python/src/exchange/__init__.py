"""
交易所集成模块 - Flash Arbitrage Bot

提供与各大交易所现货和期货市场交互的统一接口。
"""

from .binance_futures import BinanceFuturesClient

__all__ = ["BinanceFuturesClient"]
