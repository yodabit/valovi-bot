# -*- coding: utf-8 -*-
"""
VALOVI BOT v2 — oblak verzija s DVA portfelja (A/B test strategija)

TREND portfelj: originalna logika — LONG kad val ide gore, SHORT kad ide dolje
KONTRA portfelj: identicni signali, SUPROTNA strana — gdje trend kupuje, kontra shorta

Oba igraju s vlastitih 10.000 virtualnih $, istim ulozima (1.000 $) i istim
naknadama, na istim podacima u istom trenutku — cist A/B eksperiment.
Postojeca povijest starog bota nastavlja zivjeti kao TREND portfelj.
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "DOT"]
START_CASH = 10000.0
POS_SIZE = 1000.0
FEE = 0.0009
STATE_FILE = "state.json"
PORTS = ["trend", "kontra"]


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
    candles = sorted(r.json(), key=lambda c: c[0])
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
    e9, e21 = ema(closes, 9), ema(closes, 21)
    r = rsi(closes)
    up, up_prev = e9[-1] > e21[-1], e9[-2] > e21[-2]
    if up and r < 72:
        return "long", ("svjezi presjek gore" if not up_prev else "uzlazni val traje")
    if not up and r > 28:
        return "short", ("svjezi presjek dolje" if up_prev else "silazni val traje")
    if up:
        return "wait", f"trend gore ali RSI {r:.0f} pregrijan"
    return "wait", f"trend dolje ali RSI {r:.0f} rasprodan"


# ---------------- stanje ----------------
def blank_port():
    return {"cash": START_CASH, "positions": {}, "trades": [], "eq": []}


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"port": {p: blank_port() for p in PORTS}}
    with open(STATE_FILE, encoding="utf-8") as f:
        s = json.load(f)
    if "port" not in s:   # migracija: stari bot postaje TREND portfelj
        s = {"port": {
            "trend": {"cash": s.get("cash", START_CASH),
                      "positions": s.get("positions", {}),
                      "trades": s.get("trades", []),
                      "eq": s.get("eq", [])},
            "kontra": blank_port(),
        }}
        print("Migracija: stara povijest nastavlja kao TREND portfelj, KONTRA krece svjeze")
    return s


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def unrealized(pos, price):
    move = (price / pos["entry"] - 1) if pos["side"] == "long" else (1 - price / pos["entry"])
    return POS_SIZE * move


def now_str():
    return datetime.now(timezone.utc).strftime("%d.%m. %H:%M UTC")


def close_position(port, name, sym, price, why):
    pos = port["positions"].pop(sym)
    pl = unrealized(pos, price) - POS_SIZE * FEE * 2
    port["cash"] += pl
    port["trades"].insert(0, {
        "time": now_str(), "sym": sym, "side": pos["side"],
        "entry": pos["entry"], "exit": price,
        "pl": round(pl, 2), "plPct": round(pl / POS_SIZE * 100, 2), "why": why,
    })
    del port["trades"][200:]
    print(f"  [{name}] ZATVOREN {pos['side'].upper()} {sym} @ {price:.4f}  P/L {pl:+.2f} $  ({why})")


def open_position(port, name, sym, side, price):
    port["positions"][sym] = {"side": side, "entry": price, "opened": now_str()}
    print(f"  [{name}] OTVOREN {side.upper()} {sym} @ {price:.4f}")


FLIP = {"long": "short", "short": "long", "wait": "wait"}


# ---------------- glavni ciklus ----------------
def main():
    state = load_state()
    prices = {}

    for sym in COINS:
        try:
            closes = fetch_closes(sym)
        except Exception as e:
            print(f"  ! preskacem {sym}: {e}")
            continue
        price = closes[-1]
        prices[sym] = price
        sig, why = signal(closes)

        for name in PORTS:
            port = state["port"][name]
            want = sig if name == "trend" else FLIP[sig]
            pos = port["positions"].get(sym)
            if pos:
                if want == "wait":
                    close_position(port, name, sym, price, "signal pao na CEKAJ")
                elif want != pos["side"]:
                    close_position(port, name, sym, price, "val se okrenuo")
                    open_position(port, name, sym, want, price)
            elif want in ("long", "short"):
                open_position(port, name, sym, want, price)

        time.sleep(0.5)

    for name in PORTS:
        port = state["port"][name]
        unreal = sum(unrealized(p, prices.get(s, p["entry"]))
                     for s, p in port["positions"].items())
        equity = port["cash"] + unreal
        port["eq"].append({"t": int(time.time() * 1000), "v": round(equity, 2)})
        del port["eq"][:-1000]
        wins = sum(1 for t in port["trades"] if t["pl"] > 0)
        total = len(port["trades"])
        wr = f"{100*wins/total:.0f} %" if total else "-"
        print(f"[{name.upper():6s}] kapital {equity:,.2f} $ | trejdova {total} | win rate {wr}")

    state["last_run"] = now_str()
    state["last_prices"] = {s: round(p, 6) for s, p in prices.items()}
    save_state(state)


if __name__ == "__main__":
    main()
