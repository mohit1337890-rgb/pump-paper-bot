"""
Pump.fun PAPER TEST  v2  (nakli paise - koi wallet / private key nahi)

Mohit ke rules:
  ENTRY : market cap $30K-60K  AND  pichhle 5 minute mein MC 30%+ badha
  EXIT 1: 2x pe 50% becho
  EXIT 2: baaki ko hold, sabse uncha MC (peak) se 30% gire to sab becho

v2 mein naya:
  - Connection toote to apne aap 5 sec mein dobara judta hai (band nahi hota)
  - Har minute sab kuch paper_state.json mein save -> dobara chalao to wahin se shuru
  - Har report report.txt mein bhi save
  - RUN_HOURS env set karo to utne ghante baad khud band (GitHub ke liye)

Chalane ke liye:
  pip install websockets requests
  python pump_paper_test.py
"""

import asyncio, json, time, csv, os
import requests, websockets

# ---------------- SETTINGS (yahin badlo) ----------------
ENTRY_MC_MIN = 30_000      # $
ENTRY_MC_MAX = 60_000      # $
RISE_PCT     = 30          # pichhle 5 min mein kitna % badha ho
RISE_WINDOW  = 5 * 60      # seconds
TAKE_HALF_AT = 2.0         # 2x pe aadha becho
TRAIL_DROP   = 30          # peak se kitna % gire to sab becho
PAPER_SIZE   = 10.0        # har trade $10 (nakli)
FEE_PCT      = 1.0         # pump.fun fee har buy/sell pe ~1%
WATCH_MIN    = 60          # naya coin kitne minute tak dekhna (entry ke liye)
SOL_PRICE_FALLBACK = 150.0
TRADES_CSV   = "paper_trades.csv"
STATE_FILE   = "paper_state.json"
REPORT_FILE  = "report.txt"
RUN_HOURS    = float(os.environ.get("RUN_HOURS", "0"))   # 0 = hamesha chalo
# --------------------------------------------------------

WS_URL = "wss://pumpportal.fun/api/data"
sol_price = SOL_PRICE_FALLBACK
coins = {}       # mint -> {name, born, hist:[(t,mc)], last_mc}
positions = {}   # mint -> position dict
closed = []
seen_total = 0   # ab tak kitne naye coins dekhe (sab runs milake)


def get_sol_price():
    global sol_price
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": "solana", "vs_currencies": "usd"}, timeout=10)
        sol_price = float(r.json()["solana"]["usd"])
    except Exception as e:
        print(f"[!] SOL price nahi mila, ${sol_price} use kar raha hoon ({e})", flush=True)


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"positions": positions, "closed": closed, "coins": coins,
                       "seen_total": seen_total, "sol_price": sol_price}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[!] state save fail: {e}", flush=True)


def load_state():
    global positions, closed, coins, seen_total, sol_price
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        positions = s.get("positions", {})
        closed = s.get("closed", [])
        coins = s.get("coins", {})
        seen_total = s.get("seen_total", 0)
        sol_price = s.get("sol_price", sol_price)
        print(f"Purana data mila: {len(positions)} khule, {len(closed)} band trades. "
              f"Wahin se aage...", flush=True)
    except Exception as e:
        print(f"[!] state load fail, naya shuru: {e}", flush=True)


def log_trade(row):
    new = not os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "mint", "name", "action", "mc_usd", "usd_in_or_out", "note"])
        w.writerow(row)


def ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def check_entry(mint, c, now, mc):
    if mint in positions or any(p["mint"] == mint for p in closed):
        return
    if not (ENTRY_MC_MIN <= mc <= ENTRY_MC_MAX):
        return
    old = [m for (t, m) in c["hist"] if t <= now - RISE_WINDOW]
    base = old[-1] if old else c["hist"][0][1]
    if base <= 0 or mc < base * (1 + RISE_PCT / 100):
        return
    share = PAPER_SIZE * (1 - FEE_PCT / 100) / mc
    positions[mint] = {"mint": mint, "name": c["name"], "entry_mc": mc, "share": share,
                       "cash_back": 0.0, "half_sold": False, "peak": mc, "t": now}
    print(f"{ts()} BUY  {c['name'][:20]:20} MC ${mc:,.0f}  (5min pehle ${base:,.0f})", flush=True)
    log_trade([ts(), mint, c["name"], "BUY", round(mc), PAPER_SIZE, f"base {round(base)}"])


def sell(p, frac, mc, why):
    amt = p["share"] * frac
    usd = amt * mc * (1 - FEE_PCT / 100)
    p["share"] -= amt
    p["cash_back"] += usd
    print(f"{ts()} SELL {p['name'][:20]:20} {int(frac*100)}% @ MC ${mc:,.0f} -> ${usd:.2f}  [{why}]",
          flush=True)
    log_trade([ts(), p["mint"], p["name"], f"SELL {int(frac*100)}%", round(mc), round(usd, 2), why])


def check_exit(mint, mc):
    p = positions.get(mint)
    if not p:
        return
    p["peak"] = max(p["peak"], mc)
    if not p["half_sold"] and mc >= p["entry_mc"] * TAKE_HALF_AT:
        sell(p, 0.5, mc, "2x - aadha")
        p["half_sold"] = True
        p["peak"] = mc
    elif p["half_sold"] and mc <= p["peak"] * (1 - TRAIL_DROP / 100):
        sell(p, 1.0, mc, f"peak ${p['peak']:,.0f} se {TRAIL_DROP}% gira")
        closed.append(positions.pop(mint))


def report():
    lines = ["=" * 55, f"REPORT  {ts()}   coins dekhe (total): {seen_total}"]
    total_in = total_back = 0.0
    wins = 0
    for p in closed:
        total_in += PAPER_SIZE
        total_back += p["cash_back"]
        wins += (p["cash_back"] - PAPER_SIZE) > 0
    lines.append(f"Band trades: {len(closed)}   jeete: {wins}   haare: {len(closed)-wins}")
    open_val = 0.0
    for m, p in positions.items():
        last = coins.get(m, {}).get("last_mc", p["entry_mc"])
        val = p["cash_back"] + p["share"] * last
        open_val += val
        total_in += PAPER_SIZE
        total_back += val
    lines.append(f"Khule trades: {len(positions)}  (abhi ki value ${open_val:.2f})")
    lines.append(f"Total lagaya: ${total_in:.2f}   wapas/value: ${total_back:.2f}   "
                 f"P&L: ${total_back-total_in:+.2f}")
    lines.append("=" * 55)
    text = "\n".join(lines)
    print("\n" + text + "\n", flush=True)
    try:
        with open(REPORT_FILE, "w") as f:
            f.write(text + "\n")
    except Exception:
        pass


async def subscribe_existing(ws):
    keys = list(coins.keys())
    for i in range(0, len(keys), 100):
        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": keys[i:i+100]}))


async def run_once(start):
    """Ek connection. Return True = time poora, band karo."""
    global seen_total
    last_report = last_price = last_cleanup = last_save = time.time()
    async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        await subscribe_existing(ws)
        print(f"{ts()} Connected. Live coins sun raha hoon...", flush=True)
        while True:
            if RUN_HOURS and time.time() - start > RUN_HOURS * 3600:
                return True
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
            except asyncio.TimeoutError:
                raise ConnectionError("60 sec se koi data nahi")
            msg = json.loads(raw)
            now = time.time()
            mint = msg.get("mint")
            if mint and "marketCapSol" in msg:
                mc = float(msg["marketCapSol"]) * sol_price
                if msg.get("txType") == "create":
                    seen_total += 1
                    coins[mint] = {"name": msg.get("symbol") or msg.get("name") or mint[:6],
                                   "born": now, "hist": [(now, mc)], "last_mc": mc}
                    await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
                elif mint in coins:
                    c = coins[mint]
                    c["hist"].append((now, mc))
                    c["hist"] = [(t, m) for (t, m) in c["hist"] if t >= now - RISE_WINDOW - 60]
                    c["last_mc"] = mc
                    check_entry(mint, c, now, mc)
                    check_exit(mint, mc)

            if now - last_cleanup > 60:
                old = [m for m, c in coins.items()
                       if now - c["born"] > WATCH_MIN * 60 and m not in positions]
                if old:
                    for i in range(0, len(old), 100):
                        await ws.send(json.dumps({"method": "unsubscribeTokenTrade",
                                                  "keys": old[i:i+100]}))
                    for m in old:
                        coins.pop(m, None)
                last_cleanup = now
            if now - last_save > 60:
                save_state(); last_save = now
            if now - last_price > 600:
                get_sol_price(); last_price = now
            if now - last_report > 900:
                report(); last_report = now


async def main():
    load_state()
    get_sol_price()
    print(f"SOL = ${sol_price}" + (f"   (chalega {RUN_HOURS} ghante)" if RUN_HOURS else ""),
          flush=True)
    start = time.time()
    while True:
        try:
            if await run_once(start):
                print(f"{ts()} Time poora hua, band kar raha hoon.", flush=True)
                break
        except Exception as e:
            print(f"{ts()} [!] Connection toota ({type(e).__name__}: {e}). "
                  f"5 sec mein dobara...", flush=True)
            save_state()
            await asyncio.sleep(5)
    save_state()
    report()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        save_state()
        report()
