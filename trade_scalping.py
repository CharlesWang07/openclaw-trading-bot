#!/usr/bin/env python3
"""
1分钟极速波段炒单策略 (Scalping)
严格按照用户提供的纪律执行

规则：
1. 止损：0.5% 固定硬止损
2. 止盈：1.0%平半仓保本 → 2.0%全平
3. 保护：0.6%回落到0.3%全平 / 1.5%后回落1.0%全平
4. 入场：回调至均线 ±0.1%~0.4%
5. 时间退出：15分钟强制平仓
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
USER_AGENT = "openclaw-scalping/1.0"
STATUS_FILE = BASE_DIR / "status.json"
TRADES_FILE = BASE_DIR / "trades.json"
THINKING_FILE = BASE_DIR / "thinking.json"


# ==================== 策略参数 ====================
# 止损
STOP_LOSS_PCT = 0.005  # 0.5%

# 止盈
TP1_PCT = 0.01        # 1.0%
TP1_CLOSE_PCT = 0.5   # 平50%
TP2_PCT = 0.02        # 2.0%

# 追踪止盈
TRAIL_PROFIT_HIGH = 0.006  # 0.6%
TRAIL_PROFIT_LOW = 0.003   # 0.3% 回撤触发全平
HALF_PROFIT_HIGH = 0.015    # 1.5% 半仓后
HALF_PROFIT_LOW = 0.010     # 1.0% 回撤触发全平

# 入场条件
MA_DISTANCE_MIN = 0.001  # 0.1%
MA_DISTANCE_MAX = 0.004  # 0.4%
MA_PERIODS = [5, 10, 30]

# 时间退出
TIME_EXIT_MINUTES = 15
TIME_EXIT_LIMIT_OFFSET = 0.0005  # ±0.05%
TIME_EXIT_LIMIT_TIMEOUT = 30  # 30秒

# 其他
COOLDOWN_SECONDS = 60
MAX_CONCURRENT_POSITIONS = 3
LEVERAGE = 10
POSITION_SIZE_PCT = 0.10  # 10%仓位


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
    raise RuntimeError("Missing Binance API credentials.")


@dataclass
class Config:
    version: str = "1.0-scalping"
    stop_loss_pct: float = STOP_LOSS_PCT
    tp1_pct: float = TP1_PCT
    tp1_close_pct: float = TP1_CLOSE_PCT
    tp2_pct: float = TP2_PCT
    trail_profit_high: float = TRAIL_PROFIT_HIGH
    trail_profit_low: float = TRAIL_PROFIT_LOW
    half_profit_high: float = HALF_PROFIT_HIGH
    half_profit_low: float = HALF_PROFIT_LOW
    ma_distance_min: float = MA_DISTANCE_MIN
    ma_distance_max: float = MA_DISTANCE_MAX
    time_exit_minutes: int = TIME_EXIT_MINUTES
    time_exit_limit_offset: float = TIME_EXIT_LIMIT_OFFSET
    cooldown_seconds: int = COOLDOWN_SECONDS
    max_positions: int = MAX_CONCURRENT_POSITIONS
    leverage: int = LEVERAGE
    position_size_pct: float = POSITION_SIZE_PCT


class BinanceClient:
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

    def get_klines(self, symbol: str, interval: str = "1m", limit: int = 60) -> list:
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
        timestamp = int(time.time() * 1000)
        params = {"timestamp": timestamp, "recvWindow": 5000}
        return self._request("/fapi/v2/account", params, signed=True) or {}

    def get_balance(self) -> float:
        account = self.get_account()
        for a in account.get("assets", []):
            if a.get("asset") == "USDT":
                return float(a.get("availableBalance", 0))
        return 0

    def get_positions(self) -> list:
        account = self.get_account()
        positions = []
        for item in account.get("positions", []):
            amt = float(item.get("positionAmt", 0))
            if abs(amt) > 0:
                positions.append({
                    "symbol": item["symbol"],
                    "amount": abs(amt),
                    "direction": "long" if amt > 0 else "short",
                    "entryPrice": float(item.get("entryPrice", 0)),
                    "markPrice": float(item.get("markPrice", 0)),
                    "unrealizedProfit": float(item.get("unrealizedProfit", 0)),
                    "leverage": int(item.get("leverage", 10)),
                })
        return positions

    def get_ticker_24h(self) -> list:
        return self._request("/fapi/v1/ticker/24hr") or []

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        timestamp = int(time.time() * 1000)
        params = {
            "symbol": symbol,
            "leverage": leverage,
            "timestamp": timestamp,
            "recvWindow": 5000,
        }
        query = urlencode(params)
        params["signature"] = self._sign(query)
        return self._request("/fapi/v1/leverage", params, signed=True) or {}

    def place_order(self, symbol: str, side: str, order_type: str,
                    quantity: float = None, price: float = None,
                    reduce_only: bool = False) -> dict:
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
    @staticmethod
    def sma(values: list, period: int) -> float:
        if len(values) < period:
            return values[-1] if values else 0
        return sum(values[-period:]) / period

    @staticmethod
    def ma_distance_pct(price: float, ma: float) -> float:
        if ma == 0:
            return 0
        return abs(price - ma) / ma


class ScalpingStrategy:
    def __init__(self, client: BinanceClient, config: Config):
        self.client = client
        self.config = config

    def get_top_coins(self, top_n: int = 10) -> list:
        """获取主流币种"""
        tickers = self.client.get_ticker_24h()
        stable_coins = ['USDCUSDT', 'USDTUSDT', 'FDUSDUSDT', 'USD1USDT', 'USDDUSDT', 'TUSDUSDT', 'BUSDUSDT']
        usdt_pairs = [t for t in tickers if t.get("symbol", "").endswith("USDT")
                      and t.get("symbol", "") not in stable_coins]
        sorted_tickers = sorted(usdt_pairs, key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
        return [t["symbol"] for t in sorted_tickers[:top_n]]

    def check_entry_condition(self, symbol: str) -> tuple[bool, str, float, float]:
        """
        检查入场条件：回调至均线0.1%-0.4%区间
        返回: (是否满足条件, 方向, 当前价格, 均线价格)
        """
        klines = self.client.get_klines(symbol, "1m", 60)
        if len(klines) < 35:
            return False, "K线不足", 0, 0

        closes = [k["close"] for k in klines]
        price = closes[-1]

        # 检查所有均线
        for period in MA_PERIODS:
            ma = TechnicalIndicators.sma(closes, period)
            dist_pct = TechnicalIndicators.ma_distance_pct(price, ma)

            if self.config.ma_distance_min <= dist_pct <= self.config.ma_distance_max:
                # 判断方向：价格在均线上方=回调做多，价格在均线下方=反弹做空
                if price > ma:
                    return True, "long", price, ma
                else:
                    return True, "short", price, ma

        return False, "不在均线回调区间", price, 0

    def calc_stop_loss(self, direction: str, entry: float) -> float:
        """计算止损价格：0.5%固定"""
        if direction == "long":
            return entry * (1 - self.config.stop_loss_pct)
        return entry * (1 + self.config.stop_loss_pct)

    def calc_tp1(self, direction: str, entry: float) -> float:
        """计算第一止盈目标：1.0%"""
        fees = 0.0002 + 0.0005  # 开仓+平仓手续费
        if direction == "long":
            return entry * (1 + self.config.tp1_pct - fees)
        return entry * (1 - self.config.tp1_pct + fees)

    def calc_tp2(self, direction: str, entry: float) -> float:
        """计算第二止盈目标：2.0%"""
        fees = 0.0002 + 0.0005
        if direction == "long":
            return entry * (1 + self.config.tp2_pct - fees)
        return entry * (1 - self.config.tp2_pct + fees)


class ScalpingBot:
    def __init__(self, api_key: str = "", secret_key: str = ""):
        self.client = BinanceClient(api_key, secret_key)
        self.config = Config()
        self.strategy = ScalpingStrategy(self.client, self.config)

        self.positions = {}  # symbol -> {direction, entry, qty, open_time, tp1_triggered, peak_profit}
        self.last_trade_time = 0
        self.highest_balance = 0
        self.universe = []

    def now_str(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def add_thought(self, message: str) -> None:
        thoughts = read_json(THINKING_FILE, [])
        thoughts.append({"time": self.now_str(), "thought": message})
        write_json(THINKING_FILE, thoughts[-200:])

    def append_trade(self, payload: dict) -> None:
        trades = read_json(TRADES_FILE, [])
        trades.append(payload)
        write_json(TRADES_FILE, trades[-500:])

    def get_balance(self) -> float:
        return self.client.get_balance()

    def update_status(self, events: list = None) -> None:
        status = read_json(STATUS_FILE, {})
        balance = self.get_balance()
        positions = self.client.get_positions()

        status.update({
            "last_run": self.now_str(),
            "balance": round(float(balance), 4),
            "equity": round(float(balance), 4),
            "unrealized_pnl": 0,
            "positions": len(positions),
            "open_positions": [
                {
                    "symbol": p["symbol"],
                    "direction": p["direction"],
                    "entryPrice": round(p["entryPrice"], 6),
                    "markPrice": round(p["markPrice"], 6),
                    "amount": round(p["amount"], 4),
                    "leverage": p["leverage"],
                }
                for p in positions
            ],
            "mode": "scalping-v1",
            "strategy": {
                "version": "1分钟极速波段",
                "stopLoss": f"固定{self.config.stop_loss_pct*100:.1f}%",
                "takeProfit": f"1.0%平半仓→2.0%全平",
                "trailProfit": f"0.6%→0.3%全平 / 1.5%后回落1.0%全平",
                "entryCondition": f"均线回调±{self.config.ma_distance_min*100:.1f}%~{self.config.ma_distance_max*100:.1f}%",
                "timeExit": f"{self.config.time_exit_minutes}分钟强制平仓",
            },
            "events": events or [],
        })
        write_json(STATUS_FILE, status)

    def update_universe(self) -> None:
        """更新交易币种列表"""
        self.universe = self.strategy.get_top_coins(10)
        self.add_thought(f"📡 交易池: {' / '.join(s.replace('USDT', '') for s in self.universe)}")

    def open_position(self, symbol: str, direction: str, price: float, ma_price: float) -> bool:
        """开仓"""
        now = time.time()

        # 冷却检查
        if now - self.last_trade_time < self.config.cooldown_seconds:
            return False

        # 持仓数检查
        if len(self.positions) >= self.config.max_positions:
            return False

        if symbol in self.positions:
            return False

        # 计算数量
        balance = self.get_balance()
        if balance < 10:
            return False

        # 设置杠杆
        self.client.set_leverage(symbol, self.config.leverage)

        # 计算数量 (10%仓位)
        qty = balance * self.config.position_size_pct * self.config.leverage / price
        qty = round(qty, 3)

        # 下单
        side = "BUY" if direction == "long" else "SELL"
        try:
            result = self.client.place_order(symbol, side, "MARKET", qty)
            if result.get("orderId"):
                self.positions[symbol] = {
                    "direction": direction,
                    "entry": price,
                    "qty": qty,
                    "open_time": now,
                    "tp1_triggered": False,
                    "peak_profit": 0,
                }
                self.last_trade_time = now
                self.add_thought(
                    f"🎯 {symbol} {'做多' if direction=='long' else '做空'} 开仓 {price:.4f} "
                    f"距均线{'+' if direction=='long' else '-'}{abs(price-ma_price)/ma_price*100:.2f}%"
                )
                self.append_trade({
                    "time": self.now_str(),
                    "type": side,
                    "symbol": symbol,
                    "amount": qty,
                    "price": price,
                    "pnl": 0,
                    "reason": f"极速波段{'做多' if direction=='long' else '做空'} | 距均线{abs(price-ma_price)/ma_price*100:.2f}%",
                    "balance": round(balance, 4),
                    "leverage": self.config.leverage,
                    "direction": direction,
                    "tradeAction": "OPEN",
                })
                return True
        except Exception as e:
            self.add_thought(f"❌ {symbol} 开仓失败: {e}")
        return False

    def close_position(self, symbol: str, reason: str) -> bool:
        """平仓"""
        if symbol not in self.positions:
            return False

        pos = self.positions[symbol]
        side = "SELL" if pos["direction"] == "long" else "BUY"

        # 市价平仓
        try:
            result = self.client.place_order(symbol, side, "MARKET", pos["qty"], reduce_only=True)
            if result.get("orderId"):
                balance = self.get_balance()
                self.add_thought(f"🧾 {symbol} 平仓: {reason}")

                # 计算实际盈亏
                klines = self.client.get_klines(symbol, "1m", 1)
                close_price = klines[-1]["close"] if klines else pos["entry"]
                if pos["direction"] == "long":
                    pnl = (close_price - pos["entry"]) * pos["qty"]
                else:
                    pnl = (pos["entry"] - close_price) * pos["qty"]

                self.append_trade({
                    "time": self.now_str(),
                    "type": side,
                    "symbol": symbol,
                    "amount": pos["qty"],
                    "price": close_price,
                    "pnl": round(pnl, 4),
                    "reason": f"极速波段平仓: {reason}",
                    "balance": round(balance, 4),
                    "leverage": pos.get("leverage", self.config.leverage),
                    "direction": pos["direction"],
                    "tradeAction": "CLOSE",
                })

                del self.positions[symbol]
                return True
        except Exception as e:
            self.add_thought(f"❌ {symbol} 平仓失败: {e}")
        return False

    def check_positions(self) -> None:
        """检查持仓并处理止盈止损"""
        now = time.time()

        for symbol, pos in list(self.positions.items()):
            # 获取当前价格
            klines = self.client.get_klines(symbol, "1m", 1)
            if not klines:
                continue
            price = klines[-1]["close"]
            entry = pos["entry"]
            direction = pos["direction"]

            # 计算盈亏百分比
            if direction == "long":
                profit_pct = (price - entry) / entry
            else:
                profit_pct = (entry - price) / entry

            # 更新峰值
            if profit_pct > pos["peak_profit"]:
                pos["peak_profit"] = profit_pct

            # ========== 止损检查 ==========
            sl_price = self.strategy.calc_stop_loss(direction, entry)
            if (direction == "long" and price <= sl_price) or (direction == "short" and price >= sl_price):
                self.close_position(symbol, "止损 0.5%")
                continue

            # ========== 止盈检查 ==========
            tp2_price = self.strategy.calc_tp2(direction, entry)

            # 2.0% 全部平仓
            if profit_pct >= self.config.tp2_pct:
                self.close_position(symbol, "止盈 2.0%")
                continue

            # 1.0% 平半仓
            if profit_pct >= self.config.tp1_pct and not pos["tp1_triggered"]:
                pos["tp1_triggered"] = True
                # 部分平仓
                qty_to_close = pos["qty"] * self.config.tp1_close_pct
                side = "SELL" if direction == "long" else "BUY"
                try:
                    self.client.place_order(symbol, side, "MARKET", qty_to_close, reduce_only=True)
                    pos["qty"] = pos["qty"] * (1 - self.config.tp1_close_pct)
                    # 止损移至成本
                    pos["entry"] = price
                    self.add_thought(f"📊 {symbol} 1.0%止盈50%，止损移至成本")
                except:
                    pass
                continue

            # ========== 追踪止盈检查 ==========
            peak = pos["peak_profit"]

            # 微利保护：0.6%→0.3%
            if peak >= self.config.trail_profit_high and profit_pct <= self.config.trail_profit_low:
                self.close_position(symbol, "微利保护 0.6%→0.3%")
                continue

            # 半仓保护：1.5%→1.0%
            if pos["tp1_triggered"] and peak >= self.config.half_profit_high and profit_pct <= self.config.half_profit_low:
                self.close_position(symbol, "半仓保护 1.5%→1.0%")
                continue

            # ========== 时间退出 ==========
            hold_seconds = now - pos["open_time"]
            hold_minutes = hold_seconds / 60

            if hold_minutes >= self.config.time_exit_minutes:
                # 15分钟强制平仓
                self.close_position(symbol, f"时间退出 {hold_minutes:.0f}分钟")
                continue

    def scan_and_trade(self) -> list:
        """扫描市场并交易"""
        events = [f"极速波段扫描 {self.now_str()}"]
        self.update_universe()

        # 先检查持仓
        self.check_positions()

        # 刷新持仓状态
        current_positions = self.client.get_positions()
        for cp in current_positions:
            symbol = cp["symbol"]
            if symbol not in self.positions:
                self.positions[symbol] = {
                    "direction": cp["direction"],
                    "entry": cp["entryPrice"],
                    "qty": cp["amount"],
                    "open_time": time.time(),
                    "tp1_triggered": False,
                    "peak_profit": 0,
                }

        # 如果持仓已满，不再开仓
        if len(self.positions) >= self.config.max_positions:
            events.append(f"持仓已满 ({len(self.positions)})")
            return events

        # 扫描候选币
        for symbol in self.universe:
            ok, direction, price, ma = self.strategy.check_entry_condition(symbol)
            if ok:
                events.append(f"✅ {symbol.replace('USDT','')} {'做多' if direction=='long' else '做空'} 信号")
                self.add_thought(f"📈 {symbol} {'做多' if direction=='long' else '做空'} 信号")
                self.open_position(symbol, direction, price, ma)
                break
            else:
                events.append(f"  {symbol.replace('USDT','')}: {direction}")

        if len(events) == 1:
            events.append("暂无信号")
            self.add_thought("😴 无入场信号")

        return events

    def tick(self) -> None:
        """一轮扫描"""
        self.add_thought(f"🔄 极速波段扫描 {self.now_str()}")
        events = self.scan_and_trade()
        self.update_status(events)

        balance = self.get_balance()
        if balance > self.highest_balance:
            self.highest_balance = balance

    def run(self) -> None:
        print("=" * 50)
        print("1分钟极速波段策略 启动")
        print("=" * 50)

        self.highest_balance = self.get_balance()

        while True:
            try:
                self.tick()
                time.sleep(60)  # 1分钟扫描一次
            except KeyboardInterrupt:
                print("\n停止")
                break
            except Exception as e:
                print(f"错误: {e}")
                time.sleep(60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["run", "run-once", "status"])
    args = parser.parse_args()

    ensure_credentials()
    api_key = os.getenv("BINANCE_API_KEY", "")
    secret_key = os.getenv("BINANCE_SECRET_KEY", "")

    bot = ScalpingBot(api_key, secret_key)

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
