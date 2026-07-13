# -*- coding: utf-8 -*-
"""
VALOVI BOT v4 — tri portfelja: TREND, KONTRA i TOMO

TREND : original — ulazi i izlazi po signalima + stop/trailing (v3)
KONTRA: suprotna strana signala + stop/trailing (v3)
TOMO  : ulazi kao TREND, ali IZLAZI SU RUCNI — Tomo s ploce salje naloge
        (market = odmah po sljedecem krugu; limit = kad cijena dosegne cilj).
        Sigurnosna mreza ostaje: stop-loss -8 % i trailing zakljucavanje.
        Nakon rucnog zatvaranja bot taj token ne otvara ponovno dok se
        signal ne promijeni (da ruka ima smisla).

Nalozi s ploce stizu kroz commands.json:
  {"cmds":[{"sym":"BTC","type":"market"} , {"sym":"ETH","type":"limit","price":1900}]}
Bot ih obradi i isprazni datoteku. Limit nalozi cekaju u state-u.
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
STOP_LOSS = 0.08
LOCK_AT = 0.03
TRAIL = 0.04
STATE_FILE = "state.json"
CMD_FILE = "commands.json"
PORTS = ["trend", "kontra", "tomo"]


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
    if "port" not in s:
        s = {"port": {"trend": {"cash": s.get("cash", START_CASH),
                                "positions": s.get("positions", {}),
                                "trades": s.get("trades", []),
                                "eq": s.get("eq", [])}}}
    for p in PORTS:                      # dodaj portfelje koji nedostaju
        if p not in s["port"]:
            s["port"][p] = blank_port()
            print(f"Novi portfelj: {p.upper()} krece s {START_CASH:,.0f} $")
    return s


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def load_commands():
    if not os.path.exists(CMD_FILE):
        return []
    try:
        with open(CMD_FILE, encoding="utf-8") as f:
            return json.load(f).get("cmds", [])
    except Exception:
        return []


def clear_commands():
    with open(CMD_FILE, "w", encoding="utf-8") as f:
        json.dump({"cmds": []}, f)


def unrealized(pos, price):
    move = (price / pos["entry"] - 1) if pos["side"] == "long" else (1 - price / pos["entry"])
    return POS_SIZE * move


def now_str():
    return datetime.now(timezone.utc).strftime("%d.%m. %H:%M UTC")


def init_stop(pos):
    if "stop" not in pos:
        if pos["side"] == "long":
            pos["stop"] = pos["entry"] * (1 - STOP_LOSS)
        else:
            pos["stop"] = pos["entry"] * (1 + STOP_LOSS)


def update_trailing(pos, price):
    if pos["side"] == "long":
        if price >= pos["entry"] * (1 + LOCK_AT):
            pos["stop"] = max(pos["stop"], pos["entry"], price * (1 - TRAIL))
    else:
        if price <= pos["entry"] * (1 - LOCK_AT):
            pos["stop"] = min(pos["stop"], pos["entry"], price * (1 + TRAIL))


def stop_hit(pos, price):
    return price <= pos["stop"] if pos["side"] == "long" else price >= pos["stop"]


def stop_reason(pos):
    if pos["side"] == "long":
        return "trailing stop — dobit zakljucana" if pos["stop"] >= pos["entry"] else "stop-loss -8 %"
    return "trailing stop — dobit zakljucana" if pos["stop"] <= pos["entry"] else "stop-loss -8 %"


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
    return pos


def open_position(port, name, sym, side, price):
    pos = {"side": side, "entry": price, "opened": now_str()}
    init_stop(pos)
    port["positions"][sym] = pos
    print(f"  [{name}] OTVOREN {side.upper()} {sym} @ {price:.4f}  (stop {pos['stop']:.4f})")



def repair_bogus_limit_trades(state):
    """Ponisti trejdove nastale starim bugom (limit izvrsen po upisanom broju
    umjesto po trzistu). Prepoznaje ih po razlogu 'limit nalog izvrsen' i
    apsurdnom gubitku (<= -50 %) — s v4.1+ kodom takvi vise ne mogu nastati."""
    tomo = state["port"].get("tomo")
    if not tomo:
        return
    bad = [t for t in tomo.get("trades", [])
           if "limit nalog izvrsen" in t.get("why", "") and t.get("plPct", 0) <= -50]
    for t in bad:
        tomo["cash"] -= t["pl"]                       # vrati novac (pl je negativan)
        tomo["trades"].remove(t)
        pos = {"side": t["side"], "entry": t["entry"], "opened": t["time"]}
        init_stop(pos)
        tomo["positions"][t["sym"]] = pos             # vrati poziciju
        tomo.get("muted", {}).pop(t["sym"], None)
        print(f"  [tomo] POPRAVAK: ponisten laznii trejd {t['sym']} "
              f"({t['pl']} $), pozicija vracena @ {t['entry']}")


FLIP = {"long": "short", "short": "long", "wait": "wait"}


def process_commands(state):
    """Rucni nalozi s ploce za TOMO portfelj."""
    cmds = load_commands()
    if not cmds:
        return
    tomo = state["port"]["tomo"]
    tomo.setdefault("limits", {})
    for c in cmds:
        sym = c.get("sym")
        if c.get("type") == "limit" and sym in tomo["positions"]:
            try:
                tomo["limits"][sym] = float(c.get("price"))
                print(f"  [tomo] LIMIT nalog primljen: {sym} @ {tomo['limits'][sym]}")
            except (TypeError, ValueError):
                print(f"  [tomo] ! neispravan limit za {sym}, preskacem")
        elif c.get("type") == "market" and sym in tomo["positions"]:
            tomo.setdefault("_market", []).append(sym)
            print(f"  [tomo] MARKET nalog primljen: {sym}")
    clear_commands()


def main():
    state = load_state()
    repair_bogus_limit_trades(state)
    process_commands(state)
    tomo = state["port"]["tomo"]
    tomo.setdefault("limits", {})
    tomo.setdefault("muted", {})
    market_q = set(tomo.pop("_market", []))
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
            want = sig if name != "kontra" else FLIP[sig]
            pos = port["positions"].get(sym)

            if name == "tomo":
                # rucno vodjeni portfelj
                if pos:
                    init_stop(pos)
                    update_trailing(pos, price)
                    if sym in market_q:
                        close_position(port, name, sym, price, "rucno zatvoreno (TOMO, market)")
                        tomo["muted"][sym] = pos["side"]
                        tomo["limits"].pop(sym, None)
                        continue
                    lim = tomo["limits"].get(sym)
                    if lim is not None:
                        hit = price >= lim if pos["side"] == "long" else price <= lim
                        if hit:
                            # izvrsi po stvarnoj trenutnoj cijeni (satna provjera);
                            # stiti od krivo upisanog limita — ne moze biti gore od trzista
                            close_position(port, name, sym, price, "limit nalog izvrsen (TOMO)")
                            tomo["muted"][sym] = pos["side"]
                            tomo["limits"].pop(sym, None)
                            continue
                    if stop_hit(pos, price):
                        close_position(port, name, sym, price, stop_reason(pos))
                        tomo["limits"].pop(sym, None)
                        continue
                    # nema signalnih izlaza — Tomo odlucuje
                else:
                    if tomo["muted"].get(sym) and tomo["muted"][sym] != want:
                        tomo["muted"].pop(sym, None)
                    if want in ("long", "short") and not tomo["muted"].get(sym):
                        open_position(port, name, sym, want, price)
                continue

            # trend/kontra: v3 pravila
            if pos:
                init_stop(pos)
                update_trailing(pos, price)
                if stop_hit(pos, price):
                    close_position(port, name, sym, price, stop_reason(pos))
                    continue
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
