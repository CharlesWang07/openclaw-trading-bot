#!/usr/bin/env python3
"""
OpenClaw 趋势回调交易策略 v2.0
永续合约趋势回调交易策略

规则文档：24条策略规则
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_DIR = Path("/Users/sonic/.openclaw/workspace/trading-bot")
BASE_URL = "https://fapi.binance.com"
USER_AGENT = "openclaw-trend-reversal/2.0"
STATUS_FILE = BASE_DIR / "status.json"
TRADES_FILE = BASE_DIR / "trades.json"
THINKING_FILE = BASE_DIR / "thinking.json"
STRATEGY_V2_FILE = BASE_DIR / "strategy_v2.json"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


load_env_file(BASE_DIR / ".env")


def ensure_credentials() -> None:
    if os.getenv("BINANCE_API_KEY") and os.getenv("BINANCE_SECRET_KEY"):
        return
    raise RuntimeError("Missing Binance API credentials. Copy .env.example to .env and fill BINANCE_API_KEY / BINANCE_SECRET_KEY.")


@dataclass
class Config:
    """策略配置"""
    version: str = "2.0-trend-reversal"
    top_n: int = 5
    max_concurrent_positions: int = 3
    position_size_min: float = 0.10
    position_size_max: float = 0.15
    fixed_loss_pct: float = 0.015
    first_target_pct: float = 0.04
    first_target_close_pct: float = 0.5
    second_target_pct: float = 0.08
    leverage_low_vol: int = 10
    leverage_mid_vol: int = 7
    leverage_high_vol: int = 5
    low_vol_threshold: float = 0.015
    mid_vol_threshold: float = 0.03
    cooldown_general: int = 180
    cooldown_after_sl: int = 300
    limit_order_timeout: int = 10
    time_exit_minutes: int = 30
    fixed_coins: list[str] | None = None


class BinanceClient:
    """Binance API 客户端"""
    
    def __init__(self, api_key: str = "", secret_key: str = ""):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = BASE_URL
    
    def _sign(self, params: str) -> str:
        return hmac.new(
            self.secret_key.encode(), 
            params.encode(), 
            hashlib.sha256
        ).hexdigest()
    
    def _request(self, endpoint: str, params: dict = None, signed: bool = False) -> dict:
        url = f"{self.base_url}{endpoint}"
        if params:
            query = urlencode(params)
            if signed:
                query += f"&signature={self._sign(query)}"
            url = f"{url}?{query}"
        
        headers = {"User-Agent": USER_AGENT}
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key
        
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=10) as response:
                return json.loads(response.read().decode())
        except Exception as e:
            print(f"API Error: {e}")
            return {}
    
    def get_ticker_24h(self) -> list:
        """获取24小时ticker数据"""
        return self._request("/fapi/v1/ticker/24hr") or []
    
    def get_klines(self, symbol: str, interval: str = "1m", limit: int = 60) -> list:
        """获取K线数据"""
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        data = self._request("/fapi/v1/klines", params) or []
        return [
            {
                "time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
            for k in data
        ]
    
    def get_account(self) -> dict:
        """获取账户信息"""
        timestamp = int(time.time() * 1000)
        params = {"timestamp": timestamp, "recvWindow": 5000}
        return self._request("/fapi/v2/account", params, signed=True) or {}
    
    def place_order(self, symbol: str, side: str, order_type: str, 
                    quantity: float = None, price: float = None,
                    reduce_only: bool = False) -> dict:
        """下单"""
        timestamp = int(time.time() * 1000)
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "timestamp": timestamp,
            "recvWindow": 5000,
        }
        if quantity:
            params["quantity"] = round(quantity, 3)
        if price:
            params["price"] = round(price, 2)
        if reduce_only:
            params["reduceOnly"] = "true"
        
        query = urlencode(params)
        params["signature"] = self._sign(query)
        
        return self._request("/fapi/v1/order", params, signed=True) or {}


class TechnicalIndicators:
    """技术指标计算"""
    
    @staticmethod
    def sma(values: list, period: int) -> float:
        if len(values) < period:
            return values[-1] if values else 0
        return sum(values[-period:]) / period
    
    @staticmethod
    def atr(klines: list, period: int = 14) -> float:
        if len(klines) < period + 1:
            return 0
        trs = []
        for i in range(1, len(klines)):
            high = klines[i]["high"]
            low = klines[i]["low"]
            prev_close = klines[i-1]["close"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        return sum(trs[-period:]) / period if trs else 0
    
    @staticmethod
    def volatility(klines: list, period: int = 15) -> float:
        if not klines:
            return 0
        current_price = klines[-1]["close"]
        if current_price == 0:
            return 0
        atr = TechnicalIndicators.atr(klines, period)
        return (atr / current_price) * 100
    
    @staticmethod
    def ma_slope(ma_current: float, ma_past: float) -> float:
        if ma_past == 0:
            return 0
        return ((ma_current - ma_past) / ma_past) * 100
    
    @staticmethod
    def count_ma_crosses(klines: list, ma_p1: int = 5, ma_p2: int = 10) -> int:
        if len(klines) < max(ma_p1, ma_p2) + 1:
            return 0
        closes = [k["close"] for k in klines]
        crosses = 0
        prev_diff = None
        for i in range(max(ma_p1, ma_p2), len(closes)):
            ma1 = TechnicalIndicators.sma(closes[:i+1], ma_p1)
            ma2 = TechnicalIndicators.sma(closes[:i+1], ma_p2)
            diff = ma1 - ma2
            if prev_diff is not None:
                if (prev_diff > 0 and diff < 0) or (prev_diff < 0 and diff > 0):
                    crosses += 1
            prev_diff = diff
        return crosses
    
    @staticmethod
    def hh_hl(klines: list) -> tuple:
        if len(klines) < 30:
            return False, False
        highs = [k["high"] for k in klines[-30:]]
        lows = [k["low"] for k in klines[-30:]]
        hh = highs[-1] > highs[-2]
        hl = lows[-1] > lows[-2]
        lh = highs[-1] < highs[-2]
        ll = lows[-1] < lows[-2]
        return (hh and hl), (lh and ll)


class CoinScorer:
    """币种评分系统"""
    
    def __init__(self, client: BinanceClient):
        self.client = client
    
    def get_top_coins(self, top_n: int = 20) -> list:
        """获取现货成交额前N的币种"""
        import urllib.request
        import json
        
        # 稳定币列表（过滤掉）
        stable_coins = ['USDCUSDT', 'USDTUSDT', 'FDUSDUSDT', 'USD1USDT', 'USDDUSDT', 'TUSDUSDT', 'BUSDUSDT']
        
        url = "https://api.binance.com/api/v3/ticker/24hr"
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/5.0')
            with urllib.request.urlopen(req, timeout=10) as response:
                tickers = json.loads(response.read().decode())
        except Exception as e:
            print(f"获取现货数据失败: {e}")
            tickers = self.client.get_ticker_24h()
        
        # 过滤USDT交易对，排除稳定币
        usdt_pairs = [t for t in tickers if t.get("symbol", "").endswith("USDT") 
                      and t.get("symbol", "") not in stable_coins]
        # 按现货成交额排序
        sorted_tickers = sorted(usdt_pairs, key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
        return [t["symbol"] for t in sorted_tickers[:top_n]]
    
    def get_top_scored(self, universe: list, top_n: int = 10) -> list:
        """获取评分最高的币种（使用百分位排名）"""
        btc_klines = self.client.get_klines("BTCUSDT", "1m", 60)
        eth_klines = self.client.get_klines("ETHUSDT", "1m", 60)
        
        btc_chg = (btc_klines[-1]["close"] - btc_klines[-30]["close"]) / btc_klines[-30]["close"] * 100 if len(btc_klines) >= 30 else 0
        eth_chg = (eth_klines[-1]["close"] - eth_klines[-30]["close"]) / eth_klines[-30]["close"] * 100 if len(eth_klines) >= 30 else 0
        
        # 先获取所有币种的原始指标值
        coin_metrics = []
        for symbol in universe:
            metrics = self._calc_metrics(symbol, btc_chg, eth_chg)
            if metrics:
                coin_metrics.append((symbol, metrics))
        
        if not coin_metrics:
            return []
        
        # 提取各项指标
        price_changes = [m['price_change'] for _, m in coin_metrics]
        vol_growths = [m['vol_growth'] for _, m in coin_metrics]
        volatilities = [m['volatility'] for _, m in coin_metrics]
        rel_changes = [m['rel_change'] for _, m in coin_metrics]
        
        # 计算百分位排名
        def percentile_rank(value: float, values: list) -> float:
            if not values or len(values) < 2:
                return 50
            sorted_vals = sorted(values)
            try:
                rank = sorted_vals.index(value) / (len(sorted_vals) - 1) * 100
            except:
                rank = 50
            return rank
        
        # 计算每个币种的综合评分
        scored = []
        for symbol, metrics in coin_metrics:
            p_score = percentile_rank(metrics['price_change'], price_changes)
            v_score = percentile_rank(metrics['vol_growth'], vol_growths)
            vola_score = percentile_rank(metrics['volatility'], volatilities)
            r_score = percentile_rank(metrics['rel_change'], rel_changes)
            
            # 权重: 涨跌幅30%, 成交量增长25%, 波动率25%, 相对BTC/ETH 20%
            total = p_score * 0.30 + v_score * 0.25 + vola_score * 0.25 + r_score * 0.20
            scored.append((symbol, total))
        
        # 排序返回top_n
        scored.sort(key=lambda x: x[1], reverse=True)
        return [s[0] for s in scored[:top_n]]
    
    def _calc_metrics(self, symbol: str, btc_change: float, eth_change: float) -> dict:
        """计算单个币种的原始指标"""
        klines_30m = self.client.get_klines(symbol, "1m", 60)
        if not klines_30m or len(klines_30m) < 30:
            return None
        
        closes = [k["close"] for k in klines_30m]
        volumes = [k["volume"] for k in klines_30m]
        
        # 1. 30分钟涨跌幅（绝对值）
        change_30m = abs((closes[-1] - closes[-30]) / closes[-30] * 100)
        
        # 2. 成交量增长率
        vol_growth = 0
        if len(volumes) >= 60:
            vol_c = sum(volumes[-30:]) / 30
            vol_p = sum(volumes[-60:-30]) / 30
            vol_growth = (vol_c - vol_p) / vol_p * 100 if vol_p > 0 else 0
        
        # 3. 波动率
        volatility = TechnicalIndicators.volatility(klines_30m, 30)
        
        # 4. 相对BTC/ETH涨跌幅差值
        rel_change = (change_30m - btc_change + change_30m - eth_change) / 2
        
        return {
            'price_change': change_30m,
            'vol_growth': vol_growth,
            'volatility': volatility,
            'rel_change': rel_change
        }


class TrendStrategy:
    """趋势回调策略"""
    
    def __init__(self, client: BinanceClient, config: Config):
        self.client = client
        self.config = config
    
    def check_market_filter(self) -> tuple:
        for symbol in ["BTCUSDT", "ETHUSDT"]:
            klines = self.client.get_klines(symbol, "1m", 15)
            if len(klines) < 15:
                continue
            chg = (klines[-1]["close"] - klines[0]["close"]) / klines[0]["close"]
            if chg <= -0.03:
                return False, f"{symbol} 15m跌{chg*100:.1f}%"
        return True, "OK"
    
    def check_trend_filter(self, symbol: str) -> tuple:
        klines = self.client.get_klines(symbol, "1m", 60)
        if len(klines) < 35:  # 需要至少35根K线来计算当前MA30和5根K线前的MA30
            return False, "K线不足"
        
        closes = [k["close"] for k in klines]
        
        # MA30斜率 = (当前MA30 - 5根K线前的MA30) / 5根K线前的MA30 × 100%
        ma30_c = TechnicalIndicators.sma(closes, 30)
        # 5根K线前的MA30：用closes[:-5]计算
        ma30_5_ago = TechnicalIndicators.sma(closes[:-5], 30) if len(closes) > 5 else ma30_c
        ma30_slope = abs(TechnicalIndicators.ma_slope(ma30_c, ma30_5_ago))
        
        if ma30_slope < 0.15:
            return False, f"MA30斜率{ma30_slope:.3f}%<0.15%"
        
        vol_15m = TechnicalIndicators.volatility(klines, 15)
        if vol_15m < 1.0:
            return False, f"波动率{vol_15m:.2f}%<1%"
        
        crosses = TechnicalIndicators.count_ma_crosses(klines, 5, 10)
        if crosses >= 3:
            return False, f"MA交叉{crosses}次>=3"
        
        return True, "OK"
    
    def identify_trend(self, symbol: str) -> str:
        klines = self.client.get_klines(symbol, "1m", 60)
        if len(klines) < 60:
            return "unknown"
        
        closes = [k["close"] for k in klines]
        ma5, ma10, ma30, ma60 = [TechnicalIndicators.sma(closes, p) for p in [5, 10, 30, 60]]
        
        is_up = ma5 > ma10 > ma30 > ma60
        is_down = ma5 < ma10 < ma30 < ma60
        
        if not (is_up or is_down):
            return "unknown"
        
        hh_hl, lh_ll = TechnicalIndicators.hh_hl(klines)
        
        if is_up and hh_hl:
            return "uptrend"
        elif is_down and lh_ll:
            return "downtrend"
        
        return "unknown"
    
    def check_long_entry(self, symbol: str) -> tuple:
        klines = self.client.get_klines(symbol, "1m", 30)
        if len(klines) < 15:
            return False, []
        
        closes = [k["close"] for k in klines]
        volumes = [k["volume"] for k in klines]
        price = closes[-1]
        
        ma5, ma10, ma30 = [TechnicalIndicators.sma(closes, p) for p in [5, 10, 30]]
        
        cond = []
        
        # 条件1: 价格回调至MA附近
        for ma in [ma5, ma10, ma30]:
            d = abs(price - ma) / ma * 100
            if 0.6 <= d <= 1.2:
                cond.append("near_ma")
                break
        
        # 条件2: 成交量递减
        if len(volumes) >= 3 and volumes[-1] < volumes[-2] < volumes[-3]:
            cond.append("vol_down")
        
        # 条件3: 阳线站上MA5
        if klines[-1]["close"] > ma5 and klines[-1]["close"] > klines[-1]["open"]:
            cond.append("bullish")
        
        # 条件4: 未跌破15分钟低点
        lows = [k["low"] for k in klines[-15:]]
        if price > min(lows):
            cond.append("above_low")
        
        return len(cond) >= 3, cond
    
    def check_short_entry(self, symbol: str) -> tuple:
        klines = self.client.get_klines(symbol, "1m", 30)
        if len(klines) < 15:
            return False, []
        
        closes = [k["close"] for k in klines]
        volumes = [k["volume"] for k in klines]
        price = closes[-1]
        
        ma5, ma10, ma30 = [TechnicalIndicators.sma(closes, p) for p in [5, 10, 30]]
        
        cond = []
        
        for ma in [ma5, ma10, ma30]:
            d = abs(price - ma) / ma * 100
            if 0.6 <= d <= 1.2:
                cond.append("near_ma")
                break
        
        if len(volumes) >= 3 and volumes[-1] < volumes[-2] < volumes[-3]:
            cond.append("vol_down")
        
        if klines[-1]["close"] < ma5 and klines[-1]["close"] < klines[-1]["open"]:
            cond.append("bearish")
        
        highs = [k["high"] for k in klines[-15:]]
        if price < max(highs):
            cond.append("below_high")
        
        return len(cond) >= 3, cond
    
    def get_leverage(self, symbol: str) -> int:
        klines = self.client.get_klines(symbol, "1m", 60)
        if not klines:
            return 5
        vol = TechnicalIndicators.volatility(klines, 60)
        
        if vol < self.config.low_vol_threshold:
            return self.config.leverage_low_vol
        elif vol < self.config.mid_vol_threshold:
            return self.config.leverage_mid_vol
        return self.config.leverage_high_vol
    
    def calc_sl(self, direction: str, entry: float) -> float:
        fees = 0.0002 + 0.0005 + 0.001
        dist = self.config.fixed_loss_pct + fees
        if direction == "long":
            return entry * (1 - dist)
        return entry * (1 + dist)
    
    def calc_tp(self, direction: str, entry: float) -> tuple:
        fees = 0.0002 + 0.0002
        tp1 = entry * (1 + self.config.first_target_pct - fees)
        tp2 = entry * (1 + self.config.second_target_pct - fees)
        if direction == "short":
            tp1 = entry * (1 - self.config.first_target_pct + fees)
            tp2 = entry * (1 - self.config.second_target_pct + fees)
        return tp1, tp2
    
    def check_trend_break(self, symbol: str, direction: str) -> bool:
        klines = self.client.get_klines(symbol, "1m", 15)
        if len(klines) < 10:
            return False
        closes = [k["close"] for k in klines]
        ma5, ma10 = [TechnicalIndicators.sma(closes, p) for p in [5, 10]]
        if direction == "long" and ma5 < ma10:
            return True
        if direction == "short" and ma5 > ma10:
            return True
        return False


class TradingBot:
    """交易机器人"""
    
    def __init__(self, api_key: str = "", secret_key: str = ""):
        self.client = BinanceClient(api_key, secret_key)
        self.config = Config()
        self.load_strategy_config()
        self.scorer = CoinScorer(self.client)
        self.strategy = TrendStrategy(self.client, self.config)
        
        self.universe = []
        self.trading_coins = []
        self.positions = {}
        self.last_trade = {}
        self.consecutive_losses = 0
        self.pause_until = 0
        self.highest_balance = 0
        self.last_universe_update = 0
        self.last_events = []

    def load_strategy_config(self) -> None:
        strategy_v2 = read_json(STRATEGY_V2_FILE, {})
        if not strategy_v2:
            return

        self.config.version = strategy_v2.get("version", self.config.version)
        self.config.top_n = int(strategy_v2.get("topN", self.config.top_n))
        self.config.max_concurrent_positions = int(strategy_v2.get("position", {}).get("maxConcurrentPositions", self.config.max_concurrent_positions))
        self.config.position_size_min = float(strategy_v2.get("position", {}).get("sizeMinFraction", self.config.position_size_min))
        self.config.position_size_max = float(strategy_v2.get("position", {}).get("sizeMaxFraction", self.config.position_size_max))
        self.config.fixed_loss_pct = float(strategy_v2.get("stopLoss", {}).get("fixedLossPct", self.config.fixed_loss_pct))
        self.config.first_target_pct = float(strategy_v2.get("takeProfit", {}).get("firstTargetPct", self.config.first_target_pct))
        self.config.first_target_close_pct = float(strategy_v2.get("takeProfit", {}).get("firstTargetClosePct", self.config.first_target_close_pct))
        self.config.second_target_pct = float(strategy_v2.get("takeProfit", {}).get("secondTargetPct", self.config.second_target_pct))
        self.config.leverage_low_vol = int(strategy_v2.get("position", {}).get("leverageVolatilityLow", {}).get("leverage", self.config.leverage_low_vol))
        self.config.leverage_mid_vol = int(strategy_v2.get("position", {}).get("leverageVolatilityMid", {}).get("leverage", self.config.leverage_mid_vol))
        self.config.leverage_high_vol = int(strategy_v2.get("position", {}).get("leverageVolatilityHigh", {}).get("leverage", self.config.leverage_high_vol))
        self.config.low_vol_threshold = float(strategy_v2.get("position", {}).get("leverageVolatilityLow", {}).get("max", self.config.low_vol_threshold))
        self.config.mid_vol_threshold = float(strategy_v2.get("position", {}).get("leverageVolatilityMid", {}).get("max", self.config.mid_vol_threshold))
        self.config.cooldown_general = int(strategy_v2.get("cooldown", {}).get("generalMinutes", self.config.cooldown_general / 60) * 60)
        self.config.cooldown_after_sl = int(strategy_v2.get("cooldown", {}).get("symbolAfterSLMinutes", self.config.cooldown_after_sl / 60) * 60)
        self.config.limit_order_timeout = int(strategy_v2.get("orders", {}).get("limitOrderTimeoutSeconds", self.config.limit_order_timeout))
        self.config.time_exit_minutes = int(strategy_v2.get("timeExit", {}).get("maxHoldMinutes", self.config.time_exit_minutes))
        fixed = strategy_v2.get("coins", [])
        self.config.fixed_coins = [f"{str(coin).upper().replace('USDT', '')}USDT" for coin in fixed] if fixed else None

    def now_str(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def add_thought(self, message: str) -> None:
        thoughts = read_json(THINKING_FILE, [])
        thoughts.append({"time": self.now_str(), "thought": message})
        write_json(THINKING_FILE, thoughts[-200:])

    def append_trade_record(self, payload: dict[str, Any]) -> None:
        trades = read_json(TRADES_FILE, [])
        trades.append(payload)
        write_json(TRADES_FILE, trades[-500:])

    def build_open_positions(self) -> tuple[list[dict[str, Any]], float]:
        open_positions = []
        total_unrealized = 0.0
        for symbol, pos in self.positions.items():
            klines = self.client.get_klines(symbol, "1m", 1)
            mark_price = klines[-1]["close"] if klines else pos["entry"]
            qty = float(pos["qty"])
            if pos["direction"] == "long":
                unrealized = (mark_price - pos["entry"]) * qty
            else:
                unrealized = (pos["entry"] - mark_price) * qty
            total_unrealized += unrealized
            open_positions.append({
                "symbol": symbol,
                "direction": pos["direction"],
                "entryPrice": round(float(pos["entry"]), 6),
                "price": round(float(pos["entry"]), 6),
                "markPrice": round(float(mark_price), 6),
                "unrealizedProfit": round(float(unrealized), 4),
                "leverage": pos["leverage"],
                "amount": round(qty, 4),
            })
        return open_positions, total_unrealized

    def update_status(self, events: Optional[list[str]] = None, top_signal: Optional[dict[str, Any]] = None) -> None:
        status = read_json(STATUS_FILE, {})
        balance = self.get_balance()
        open_positions, total_unrealized = self.build_open_positions()

        strategy_v2 = read_json(STRATEGY_V2_FILE, {})
        take_profit = strategy_v2.get("takeProfit", {})
        stop_loss = strategy_v2.get("stopLoss", {})
        position = strategy_v2.get("position", {})

        status.update({
            "last_run": self.now_str(),
            "balance": round(float(balance), 4),
            "equity": round(float(balance + total_unrealized), 4),
            "unrealized_pnl": round(float(total_unrealized), 4),
            "positions": len(open_positions),
            "open_positions": open_positions,
            "mode": "strategy-v2",
            "watchlist": [symbol.replace("USDT", "") for symbol in self.trading_coins],
            "top_signal": top_signal or {"symbol": None, "direction": None, "score": None},
            "events": (events or self.last_events or ["v2 running"])[-8:],
            "strategy_v2": {
                "version": "趋势回调v2.0",
                "takeProfit": f"第一目标+{take_profit.get('firstTargetPct', 4) * 100:.1f}%卖50%，第二目标+{take_profit.get('secondTargetPct', 8) * 100:.1f}%全卖",
                "stopLoss": f"固定{stop_loss.get('fixedLossPct', 1.5) * 100:.1f}% + 结构保护",
                "leverage": f"低波动{position.get('leverageVolatilityLow', {}).get('leverage', 10)}x / 中波动{position.get('leverageVolatilityMid', {}).get('leverage', 7)}x / 高波动{position.get('leverageVolatilityHigh', {}).get('leverage', 5)}x",
                "positionSize": f"单笔{position.get('sizeMinFraction', 0.10) * 100:.0f}%-{position.get('sizeMaxFraction', 0.15) * 100:.0f}%仓位，最多{position.get('maxConcurrentPositions', 3)}持仓",
                "entryLogic": "MA趋势判断 + 4选3回调开仓",
                "coins": [symbol.replace("USDT", "") for symbol in self.trading_coins],
                "topN": strategy_v2.get("topN", self.config.top_n),
            }
        })
        write_json(STATUS_FILE, status)
    
    def update_universe(self):
        now = time.time()
        if not self.universe or now - self.last_universe_update > 600:
            print("更新币种列表...")
            if self.config.fixed_coins:
                self.universe = self.config.fixed_coins[:]
                self.trading_coins = self.config.fixed_coins[:self.config.top_n]
            else:
                self.universe = self.scorer.get_top_coins(30)
                self.trading_coins = self.scorer.get_top_scored(self.universe, self.config.top_n)
            self.last_universe_update = now
            print(f"交易币种: {self.trading_coins}")
            self.add_thought(f"📡 V2交易池: {' / '.join(symbol.replace('USDT', '') for symbol in self.trading_coins) if self.trading_coins else '暂无'}")
    
    def get_balance(self) -> float:
        account = self.client.get_account()
        for a in account.get("assets", []):
            if a.get("asset") == "USDT":
                return float(a.get("availableBalance", 0))
        return 0
    
    def is_paused(self) -> bool:
        now = time.time()
        if now < self.pause_until:
            print(f"暂停中，剩余{int(self.pause_until-now)}秒")
            return True
        
        if self.highest_balance > 0:
            bal = self.get_balance()
            dd = (self.highest_balance - bal) / self.highest_balance
            if dd >= 0.40:
                print("回撤40%，停机")
                self.pause_until = now + 86400
                return True
        return False
    
    def open_position(self, symbol: str, direction: str) -> bool:
        now = time.time()
        
        # 冷却
        if symbol in self.last_trade:
            cd = self.config.cooldown_after_sl if symbol in self.positions else self.config.cooldown_general
            if now - self.last_trade[symbol] < cd:
                return False
        
        if len(self.positions) >= self.config.max_concurrent_positions:
            return False
        if symbol in self.positions:
            return False
        
        bal = self.get_balance()
        if bal < 10:
            return False
        
        klines = self.client.get_klines(symbol, "1m", 1)
        if not klines:
            return False
        
        price = klines[-1]["close"]
        lev = self.strategy.get_leverage(symbol)
        size = bal * self.config.position_size_min * lev / price
        
        offset = 0.0005
        if direction == "long":
            order_price = price * (1 - offset)
            side = "BUY"
        else:
            order_price = price * (1 + offset)
            side = "SELL"
        
        try:
            result = self.client.place_order(symbol, side, "LIMIT", size, order_price)
            if result.get("orderId"):
                self.positions[symbol] = {
                    "direction": direction,
                    "entry": order_price,
                    "qty": size,
                    "leverage": lev,
                    "open_time": now,
                }
                self.last_trade[symbol] = now
                print(f"{symbol} 开{'多' if direction=='long' else '空'}成功")
                self.append_trade_record({
                    "time": self.now_str(),
                    "type": "BUY" if direction == "long" else "SELL",
                    "symbol": symbol,
                    "amount": round(float(size), 4),
                    "price": round(float(order_price), 6),
                    "pnl": 0.0,
                    "reason": "趋势回调v2开仓",
                    "balance": round(float(self.get_balance()), 4),
                    "leverage": lev,
                    "direction": direction,
                    "tradeAction": "OPEN",
                })
                self.add_thought(f"🎯 {symbol} {'做多' if direction == 'long' else '做空'} 开仓 {order_price:.4f}")
                return True
        except Exception as e:
            print(f"开仓失败: {e}")
        return False

    def close_position(self, symbol: str, reason: str):
        if symbol not in self.positions:
            return
        pos = self.positions[symbol]
        side = "SELL" if pos["direction"] == "long" else "BUY"
        
        try:
            klines = self.client.get_klines(symbol, "1m", 1)
            close_price = klines[-1]["close"] if klines else pos["entry"]
            self.client.place_order(symbol, side, "MARKET", pos["qty"], reduce_only=True)
            realized = (close_price - pos["entry"]) * pos["qty"] if pos["direction"] == "long" else (pos["entry"] - close_price) * pos["qty"]
            self.append_trade_record({
                "time": self.now_str(),
                "type": "SELL" if pos["direction"] == "long" else "BUY",
                "symbol": symbol,
                "amount": round(float(pos["qty"]), 4),
                "price": round(float(close_price), 6),
                "pnl": round(float(realized), 4),
                "reason": f"趋势回调v2平仓: {reason}",
                "balance": round(float(self.get_balance()), 4),
                "leverage": pos["leverage"],
                "direction": pos["direction"],
                "tradeAction": "CLOSE",
            })
            del self.positions[symbol]
            print(f"{symbol} 平仓: {reason}")
            self.add_thought(f"🧾 {symbol} 平仓 {reason}")
            if reason in ["stop_loss", "time_exit"]:
                self.consecutive_losses += 1
        except Exception as e:
            print(f"平仓失败: {e}")
    
    def check_positions(self):
        now = time.time()
        for symbol, pos in list(self.positions.items()):
            # 趋势破坏
            if self.strategy.check_trend_break(symbol, pos["direction"]):
                self.close_position(symbol, "trend_break")
                continue
            
            # 超时
            hold_mins = (now - pos["open_time"]) / 60
            if hold_mins >= self.config.time_exit_minutes:
                self.close_position(symbol, "time_exit")
                continue
            
            # 价格检查
            klines = self.client.get_klines(symbol, "1m", 1)
            if not klines:
                continue
            price = klines[-1]["close"]
            entry = pos["entry"]
            d = pos["direction"]
            
            pnl = (price - entry) / entry if d == "long" else (entry - price) / entry
            
            # 止损
            sl = self.strategy.calc_sl(d, entry)
            if (d == "long" and price <= sl) or (d == "short" and price >= sl):
                self.close_position(symbol, "stop_loss")
                continue
            
            # 止盈
            tp1, tp2 = self.strategy.calc_tp(d, entry)
            if pnl >= self.config.second_target_pct:
                self.close_position(symbol, "tp2")
            elif pnl >= self.config.first_target_pct and "tp1_triggered" not in pos:
                pos["tp1_triggered"] = True
                # 部分平仓
                qty = pos["qty"] * 0.5
                side = "SELL" if d == "long" else "BUY"
                try:
                    self.client.place_order(symbol, side, "MARKET", qty, reduce_only=True)
                    pos["qty"] *= 0.5
                    pos["entry"] = price  # 止损移至成本
                except:
                    pass
    
    def scan_and_trade(self) -> tuple[list[str], Optional[dict[str, Any]]]:
        events = []
        top_signal = None
        self.update_universe()
        
        ok, reason = self.strategy.check_market_filter()
        if not ok:
            events.append(reason)
            self.add_thought(f"⛔ 市场过滤: {reason}")
            return events, top_signal
        
        for symbol in self.trading_coins:
            ok, reason = self.strategy.check_trend_filter(symbol)
            if not ok:
                events.append(f"{symbol.replace('USDT', '')}: {reason}")
                continue
            
            trend = self.strategy.identify_trend(symbol)
            if top_signal is None:
                top_signal = {
                    "symbol": symbol,
                    "direction": "long" if trend == "uptrend" else "short" if trend == "downtrend" else None,
                    "score": None,
                }
            
            if trend == "uptrend":
                ok, cond = self.strategy.check_long_entry(symbol)
                if ok:
                    print(f"{symbol} 多单信号")
                    events.append(f"{symbol.replace('USDT', '')} 做多 {'/'.join(cond)}")
                    self.add_thought(f"📈 {symbol} 做多信号 {' / '.join(cond)}")
                    self.open_position(symbol, "long")
            
            elif trend == "downtrend":
                ok, cond = self.strategy.check_short_entry(symbol)
                if ok:
                    print(f"{symbol} 空单信号")
                    events.append(f"{symbol.replace('USDT', '')} 做空 {'/'.join(cond)}")
                    self.add_thought(f"📉 {symbol} 做空信号 {' / '.join(cond)}")
                    self.open_position(symbol, "short")
        return events, top_signal
    
    def update_strategy_display(self):
        self.update_status()

    def tick(self) -> None:
        events = [f"V2扫描 {datetime.now().strftime('%H:%M:%S')}"]
        self.add_thought(f"🔄 V2扫描 {datetime.now().strftime('%H:%M:%S')}")
        self.check_positions()
        scan_events, top_signal = self.scan_and_trade()
        events.extend(scan_events)
        if len(events) == 1:
            events.append("no candidate")
            self.add_thought("😴 V2没有可执行信号，等待下一轮")
        self.last_events = events[-8:]
        bal = self.get_balance()
        if bal > self.highest_balance:
            self.highest_balance = bal
        self.update_status(events=self.last_events, top_signal=top_signal)
    
    def run(self):
        print("=" * 50)
        print("趋势回调策略 v2.0 启动")
        print("=" * 50)
        
        self.highest_balance = self.get_balance()
        
        while True:
            try:
                if self.is_paused():
                    self.update_status(events=["paused"])
                    time.sleep(60)
                    continue

                self.tick()
                time.sleep(60)
                
            except KeyboardInterrupt:
                print("\n停止")
                break
            except Exception as e:
                print(f"错误: {e}")
                time.sleep(60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["run", "status", "run-once"])
    args = parser.parse_args()

    ensure_credentials()
    api_key = os.getenv("BINANCE_API_KEY", "")
    secret_key = os.getenv("BINANCE_SECRET_KEY", "")
    
    bot = TradingBot(api_key, secret_key)
    
    if args.cmd == "run":
        bot.run()
    elif args.cmd == "run-once":
        bot.highest_balance = bot.get_balance()
        bot.tick()
    elif args.cmd == "status":
        print(f"余额: {bot.get_balance()}")
        print(f"持仓: {bot.positions}")


if __name__ == "__main__":
    main()
