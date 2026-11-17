"""Research-only futures paper trader. Public market data only; never places live orders."""
import asyncio
import logging
import os
from datetime import datetime, timezone

import aiohttp

log = logging.getLogger("telegram-ai-operator.trader")
BASE_URL = os.getenv("TRADING_MARKET_DATA_URL", "https://fapi.binance.com")
SYMBOL = "BTCUSDT"
INTERVAL = "15m"
START_BALANCE = 10_000.0
RISK_FRACTION = 0.005
FEE_RATE = 0.0005  # conservative illustrative taker fee per side; adjust to actual venue later


def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = value * alpha + result * (1 - alpha)
    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None
    gains, losses = [], []
    for a, b in zip(values[-period-1:-1], values[-period:]):
        change = b - a
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles, period=14):
    if len(candles) <= period:
        return None
    trs = []
    for prev, cur in zip(candles[-period-1:-1], candles[-period:]):
        high, low, prev_close = cur[2], cur[3], prev[4]
        trs.append(max(high-low, abs(high-prev_close), abs(low-prev_close)))
    return sum(trs) / period


class PaperFuturesTrader:
    def __init__(self, db, bot, admin_id):
        self.db = db
        self.bot = bot
        self.admin_id = admin_id
        self._task = None
        self._session = None
        self._last_candle = None
        self._lock = asyncio.Lock()

    async def _db(self, fn):
        return await asyncio.to_thread(fn)

    async def setting(self, key, default=None):
        def op():
            rows = self.db.table("operator_settings").select("value").eq("key", key).limit(1).execute().data or []
            return rows[0]["value"] if rows else default
        return await self._db(op)

    async def set_setting(self, key, value):
        await self._db(lambda: self.db.table("operator_settings").upsert({"key": key, "value": str(value)}).execute())

    async def _get(self, path, params=None):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        async with self._session.get(BASE_URL + path, params=params) as response:
            response.raise_for_status()
            return await response.json()

    async def market_snapshot(self):
        raw = await self._get("/fapi/v1/klines", {"symbol": SYMBOL, "interval": INTERVAL, "limit": 100})
        candles = [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), int(x[6])] for x in raw]
        if len(candles) < 30:
            raise RuntimeError("Недостаточно свечей для анализа")
        # Last kline may still be forming: strategy only uses fully closed candles.
        closed = candles[:-1]
        closes = [c[4] for c in closed]
        funding = await self._get("/fapi/v1/premiumIndex", {"symbol": SYMBOL})
        oi = await self._get("/fapi/v1/openInterest", {"symbol": SYMBOL})
        fast_now, slow_now = ema(closes, 9), ema(closes, 21)
        fast_prev, slow_prev = ema(closes[:-1], 9), ema(closes[:-1], 21)
        current_rsi, current_atr = rsi(closes), atr(closed)
        if None in (fast_now, slow_now, fast_prev, slow_prev, current_rsi, current_atr):
            raise RuntimeError("Индикаторы пока не готовы")
        signal = "WAIT"
        if fast_prev <= slow_prev and fast_now > slow_now and 50 <= current_rsi <= 70:
            signal = "LONG"
        elif fast_prev >= slow_prev and fast_now < slow_now and 30 <= current_rsi <= 50:
            signal = "SHORT"
        return {
            "symbol": SYMBOL, "interval": INTERVAL, "price": closes[-1],
            "ema9": fast_now, "ema21": slow_now, "rsi": current_rsi,
            "atr": current_atr, "funding": float(funding.get("lastFundingRate", 0)),
            "open_interest": float(oi.get("openInterest", 0)),
            "signal": signal, "candle_time": closed[-1][0], "candle": closed[-1],
        }

    async def _open_trade(self, snap):
        balance = float(await self.setting("trader_paper_balance", START_BALANCE))
        entry = snap["price"]
        distance = snap["atr"] * 1.5
        if distance <= 0 or entry <= 0:
            return
        risk_usd = balance * RISK_FRACTION
        qty = min(risk_usd / distance, balance / entry)  # cap notional at 1x virtual equity
        side = snap["signal"]
        stop = entry - distance if side == "LONG" else entry + distance
        take = entry + distance * 2 if side == "LONG" else entry - distance * 2
        trade = {
            "symbol": SYMBOL, "side": side, "entry_price": entry, "quantity": qty,
            "stop_price": stop, "take_price": take, "risk_usd": risk_usd,
            "status": "open", "entry_time": datetime.now(timezone.utc).isoformat(),
            "strategy": "EMA9/21 crossover + RSI filter; ATR stop; paper only",
            "entry_fee": entry * qty * FEE_RATE,
        }
        await self._db(lambda: self.db.table("operator_trader_trades").insert(trade).execute())
        await self.bot.send_message(self.admin_id,
            f"🧪 Демо-сделка {side} {SYMBOL}\nВход: {entry:.2f}\nСтоп: {stop:.2f}\nЦель: {take:.2f}\nРазмер: {qty:.6f} BTC\nРиск до комиссий: {risk_usd:.2f} USDT\nПлечо: 1x (расчётный paper-режим)")

    async def _manage_open(self, snap, candle):
        rows = await self._db(lambda: self.db.table("operator_trader_trades").select("*").eq("status", "open").order("id", desc=True).limit(1).execute().data or [])
        if not rows:
            return
        trade = rows[0]
        side = trade["side"]
        stop_hit = candle[3] <= trade["stop_price"] if side == "LONG" else candle[2] >= trade["stop_price"]
        take_hit = candle[2] >= trade["take_price"] if side == "LONG" else candle[3] <= trade["take_price"]
        # If both touched within one candle, assume stop first (conservative).
        if stop_hit:
            # Model adverse gaps conservatively: stop fills at candle open if price jumped through it.
            opened = candle[1]
            if side == "LONG" and opened < float(trade["stop_price"]):
                exit_price = opened
            elif side == "SHORT" and opened > float(trade["stop_price"]):
                exit_price = opened
            else:
                exit_price = float(trade["stop_price"])
        elif take_hit:
            exit_price = float(trade["take_price"])
        else:
            exit_price = None
        if exit_price is None:
            return
        qty = float(trade["quantity"])
        entry = float(trade["entry_price"])
        gross = (exit_price-entry)*qty if side == "LONG" else (entry-exit_price)*qty
        fees = float(trade.get("entry_fee") or entry*qty*FEE_RATE) + exit_price*qty*FEE_RATE
        net = gross - fees
        balance = float(await self.setting("trader_paper_balance", START_BALANCE)) + net
        await self._db(lambda: self.db.table("operator_trader_trades").update({
            "status": "closed", "exit_price": exit_price, "exit_time": datetime.now(timezone.utc).isoformat(),
            "gross_pnl": gross, "fees": fees, "net_pnl": net,
            "close_reason": "stop_loss" if stop_hit else "take_profit",
        }).eq("id", trade["id"]).execute())
        await self.set_setting("trader_paper_balance", f"{balance:.8f}")
        await self.bot.send_message(self.admin_id,
            f"{'🛑 Стоп' if stop_hit else '🎯 Цель'}: {SYMBOL}\nРезультат после условных комиссий: {net:+.2f} USDT\nДемо-баланс: {balance:.2f} USDT")

    async def analyze_once(self):
        async with self._lock:
            snap = await self.market_snapshot()
            candle = snap["candle"]
            if self._last_candle == snap["candle_time"]:
                return snap, False
            self._last_candle = snap["candle_time"]
            await self._manage_open(snap, candle)
            if snap["signal"] in ("LONG", "SHORT"):
                opened = await self._db(lambda: self.db.table("operator_trader_trades").select("id").eq("status", "open").limit(1).execute().data or [])
                if not opened and (await self.setting("trader_demo_enabled", "false")) == "true":
                    await self._open_trade(snap)
            return snap, True

    async def run(self):
        while True:
            try:
                if (await self.setting("trader_demo_enabled", "false")) == "true":
                    await self.analyze_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Paper trader cycle failed")
            await asyncio.sleep(60)

    async def start(self):
        if self._task and not self._task.done():
            return
        await self.set_setting("trader_demo_enabled", "true")
        if await self.setting("trader_paper_balance") is None:
            await self.set_setting("trader_paper_balance", str(START_BALANCE))
        self._task = asyncio.create_task(self.run(), name="paper-futures-trader")

    async def stop(self):
        await self.set_setting("trader_demo_enabled", "false")

    async def status_text(self):
        enabled = (await self.setting("trader_demo_enabled", "false")) == "true"
        balance = float(await self.setting("trader_paper_balance", START_BALANCE))
        open_rows = await self._db(lambda: self.db.table("operator_trader_trades").select("side,entry_price,stop_price,take_price").eq("status", "open").limit(1).execute().data or [])
        closed = await self._db(lambda: self.db.table("operator_trader_trades").select("net_pnl").eq("status", "closed").execute().data or [])
        pnl = sum(float(x.get("net_pnl") or 0) for x in closed)
        return (f"🧪 <b>ИИ-трейдер — PAPER MODE</b>\nСтатус: {'РАБОТАЕТ' if enabled else 'ОСТАНОВЛЕН'}\n"
                f"Источник данных: публичный Binance USD-M Futures API (без торгового аккаунта)\nИнструмент: {SYMBOL}, {INTERVAL}\n"
                f"Виртуальный баланс: {balance:.2f} USDT\nЗакрытых сделок: {len(closed)}\nСуммарный результат: {pnl:+.2f} USDT\n"
                f"Открытая позиция: {open_rows[0]['side'] if open_rows else 'нет'}\n\n"
                "Реальные ордера не отправляются. Это исследовательский прототип, не доказательство прибыльности.")

    async def recent_trades_text(self):
        rows = await self._db(lambda: self.db.table("operator_trader_trades").select("id,side,entry_price,exit_price,net_pnl,status,close_reason").order("id", desc=True).limit(10).execute().data or [])
        if not rows:
            return "Сделок пока нет."
        lines = []
        for r in rows:
            pnl = f" · {float(r.get('net_pnl') or 0):+.2f} USDT" if r.get("status") == "closed" else " · открыта"
            lines.append(f"#{r['id']} {r['side']} {float(r['entry_price']):.2f}" + (f" → {float(r['exit_price']):.2f}" if r.get("exit_price") else "") + pnl)
        return "🧾 Последние демо-сделки\n" + "\n".join(lines)

    async def close(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
