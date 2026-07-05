# -*- coding: utf-8 -*-
"""
VALOVI BOT — oblak verzija (GitHub Actions)
Ista strategija kao na dashboardu: EMA 9/21 presjek + RSI(14) filter,
dnevne svijece, 10 glavnih tokena, virtualni novac (10.000 $ start).

Bot se pokrece svaki sat, procita svoje prethodno stanje iz state.json,
provjeri signale, otvori/zatvori virtualne pozicije i spremi novo stanje.
Nista ovdje nije pravi novac i nema API kljuceva.
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

# ---------------- postavke ----------------
COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "DOT"]
START_CASH = 10000.0
POS_SIZE = 1000.0      # $ po poziciji (virtualno)
FEE = 0.0009           # 0,09 % po strani (kao Revolut X taker)
STATE_FILE = "state.json"


# ---------------- podaci ----------------
COINGECKO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "BNB": "binancecoin", "DOGE": "dogecoin", "ADA": "cardano",
    "AVAX": "avalanche-2", "LINK": "chainlink", "DOT": "polkadot",
}
HEADERS = {"User-Agent": "valovi-bot"}


def _closes_coinbase(sym):
    r = requests.get(f"https://api.exchange.coinbase.com/products/{sym}-USD/candles",
                     params={"granularity": 86400}, timeout=20, headers=HEADERS)
    r.raise_for_status()
    candles = sorted(r.json(), key=lambda c: c[0])  # najstarije prvo
    return [float(c[4]) for c in candles]


def _closes_kraken(sym):
    r = requests.get("https://api.kraken.com/0/public/OHLC",
                     params={"pair": f"{sym}USD", "interval": 1440},
                     timeout=20, headers=HEADERS)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(",".join(j["error"]))
    key = next(k for k in j["result"] if k != "last")
    return [float(c[4]) for c in j["result"][key]]


def _closes_coingecko(sym):
    r = requests.get(
        f"https://api.coingecko.com/api/v3/coins/{COINGECKO_IDS[sym]}/market_chart",
        params={"vs_currency": "usd", "days": 120, "interval": "daily"},
        timeout=20, headers=HEADERS)
    r.raise_for_status()
    return [float(p[1]) for p in r.json()["prices"]]


def fetch_closes(symbol, limit=120):
    """Dnevne zavrsne cijene — proba izvore redom dok jedan ne uspije."""
    last_err = None
    for source in (_closes_coinbase, _closes_kraken, _closes_coingecko):
        try:
            closes = [c for c in source(symbol) if c and c > 0][-limit:]
            if len(closes) >= 40:
                return closes
            last_err = RuntimeError("premalo podataka")
        except Exception as e:
            last_err = e
    raise RuntimeError(f"nijedan izvor nije uspio ({last_err})")


# ---------------- indikatori ----------------
def ema(values, period):
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    out = [None] * (period - 1) + [e]
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, period=14):
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def signal(closes):
    """Vraca ('long'|'short'|'wait', objasnjenje)."""
    e9, e21 = ema(closes, 9), ema(closes, 21)
    r = rsi(closes)
    up, up_prev = e9[-1] > e21[-1], e9[-2] > e21[-2]
    if up and r < 72:
        why = ("svjezi presjek gore" if not up_prev else "uzlazni val traje")
        return "long", why
    if not up and r > 28:
        why = ("svjezi presjek dolje" if up_prev else "silazni val traje")
        return "short", why
    if up:
        return "wait", f"trend gore ali RSI {r:.0f} pregrijan"
    return "wait", f"trend dolje ali RSI {r:.0f} rasprodan"


# ---------------- stanje ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"cash": START_CASH, "positions": {}, "trades": [], "eq": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def unrealized(pos, price):
    move = (price / pos["entry"] - 1) if pos["side"] == "long" else (1 - price / pos["entry"])
    return POS_SIZE * move


def now_str():
    return datetime.now(timezone.utc).strftime("%d.%m. %H:%M UTC")


def close_position(state, sym, price, why):
    pos = state["positions"].pop(sym)
    pl = unrealized(pos, price) - POS_SIZE * FEE * 2
    state["cash"] += pl
    state["trades"].insert(0, {
        "time": now_str(), "sym": sym, "side": pos["side"],
        "entry": pos["entry"], "exit": price,
        "pl": round(pl, 2), "plPct": round(pl / POS_SIZE * 100, 2), "why": why,
    })
    del state["trades"][200:]
    print(f"  ZATVOREN {pos['side'].upper()} {sym} @ {price:.4f}  P/L {pl:+.2f} $  ({why})")


def open_position(state, sym, side, price):
    state["positions"][sym] = {"side": side, "entry": price, "opened": now_str()}
    print(f"  OTVOREN {side.upper()} {sym} @ {price:.4f}")


# ---------------- glavni ciklus ----------------
def main():
    state = load_state()
    prices = {}

    for sym in COINS:
        try:
            closes = fetch_closes(sym)
        except Exception as e:  # jedan token ne smije srusiti cijeli ciklus
            print(f"  ! preskacem {sym}: {e}")
            continue
        price = closes[-1]
        prices[sym] = price
        sig, why = signal(closes)
        pos = state["positions"].get(sym)

        if pos:
            if sig == "wait":
                close_position(state, sym, price, "signal pao na CEKAJ")
            elif sig != pos["side"]:
                close_position(state, sym, price, "val se okrenuo")
                open_position(state, sym, sig, price)
        elif sig in ("long", "short"):
            open_position(state, sym, sig, price)

        time.sleep(0.5)  # pristojan razmak izmedu poziva

    # tocka na krivulji kapitala
    unreal = sum(unrealized(p, prices.get(s, p["entry"]))
                 for s, p in state["positions"].items())
    equity = state["cash"] + unreal
    state["eq"].append({"t": int(time.time() * 1000), "v": round(equity, 2)})
    del state["eq"][:-1000]
    state["last_run"] = now_str()
    state["last_prices"] = {s: round(p, 6) for s, p in prices.items()}

    save_state(state)

    wins = sum(1 for t in state["trades"] if t["pl"] > 0)
    total = len(state["trades"])
    print(f"\nKapital: {equity:,.2f} $ | realizirano: {state['cash']-START_CASH:+,.2f} $ | "
          f"otvoreno: {len(state['positions'])} | trejdova: {total} | "
          f"win rate: {100*wins/total:.0f} %" if total else
          f"\nKapital: {equity:,.2f} $ | jos nema zatvorenih trejdova")


if __name__ == "__main__":
    main()
