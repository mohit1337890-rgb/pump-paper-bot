"""
Pump.fun PAPER TEST  v5  (nakli paise - koi wallet / private key nahi)

Exit rules (Mohit ke, dono strategy mein same):
  EXIT 1: 2x pe 50% becho
  EXIT 2: baaki ko hold, sabse uncha MC (peak) se 30% gire to sab becho

Entry (2 strategy saath-saath test hoti hain, alag-alag hisaab):
  A_mohit : MC $30K-60K  + pichhle 5 min mein 30%+ badha   (Mohit ka rule)
  B_jaldi : MC $15K-35K  + pichhle 5 min mein 30%+ badha   (same rule, graduation se pehle)

v5: data filter (galat uchhal ignore, price = priceUsd x 1B, sirf SOL/USDC pairs)
    + 6 strategy: entry A/B x exit base/sl40/t60
v4: naya coin PumpPortal se (free), price DexScreener se har ~10 sec (free).
    (PumpPortal ka trade feed ab paid hai - isliye v3 mein trade 0 aate the)
v3 mein naya:
  - Graduation ke baad bhi price milta hai (DexScreener se har 10 sec)
  - 2 strategy ek saath, report mein dono ka alag result
  - Har 30 min result GitHub pe save (AUTO_COMMIT=1)
  - Report mein data ki jaankari: kitne coins kis MC tak pahunche
"""

import asyncio, json, time, csv, os, subprocess
import requests, websockets

# ---------------- SETTINGS ----------------
# exit: "base" = sirf Mohit ke rules (2x pe aadha, peak se 30% gire to sab)
#       "sl40"  = base + 2x se pehle entry se 40% gire to sab becho
#       "t60"   = base + 60 min mein 2x nahi hua to sab becho
STRATEGIES = []
for _eid, _emin, _emax in [("A", 30_000, 60_000), ("B", 15_000, 35_000)]:
    for _x in ["base", "sl40", "t60"]:
        STRATEGIES.append({"id": f"{_eid}_{_x}", "mc_min": _emin, "mc_max": _emax,
                           "rise": 30, "exit": _x})
SL_PCT       = 40          # sl40: entry se kitna % gire
TIME_EXIT    = 60 * 60     # t60: kitne sec mein 2x na ho to becho
SPIKE_X      = 3.0         # data filter: 3x se bada achanak uchhal ...
SPIKE_CONFIRM = 2          # ... tabhi maano jab agle 2 reading bhi waisi hi hon
TOTAL_SUPPLY = 1_000_000_000   # pump.fun coin ki supply hamesha 1B
GOOD_QUOTES = {"So11111111111111111111111111111111111111112",      # SOL
               "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",     # USDC
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}     # USDT
RISE_WINDOW  = 5 * 60      # seconds
TAKE_HALF_AT = 2.0         # 2x pe aadha becho
TRAIL_DROP   = 30          # peak se kitna % gire to sab becho
PAPER_SIZE   = 10.0        # har trade $10 (nakli)
START_CASH   = 100.0       # har strategy ka total wallet $100 - isse zyada kabhi nahi lagega
FEE_PCT      = 1.0         # ~1% fee har buy/sell pe
WATCH_MIN    = 60          # normal coin kitne minute tak dekhna
DEX_WATCH_FROM = 12_000    # is MC ke upar gaye coins ko lamba dekho
DEX_WATCH_MIN  = 240       # aise coins kitne minute tak dekhna
DEAD_AFTER_MIN = 15        # 15 min mein $8K tak bhi nahi gaya to chhod do (sirf data bachane ke liye)
DEAD_MC        = 8_000
DEX_POLL_SEC   = 10
SOL_PRICE_FALLBACK = 150.0
TRADES_CSV   = "paper_trades.csv"
STATE_FILE   = "paper_state.json"
REPORT_FILE  = "report.txt"
RUN_HOURS    = float(os.environ.get("RUN_HOURS", "0"))   # 0 = hamesha
AUTO_COMMIT  = os.environ.get("AUTO_COMMIT") == "1"
COMMIT_EVERY = 30 * 60
# ------------------------------------------

WS_URL = "wss://pumpportal.fun/api/data"
DEX_URL = "https://api.dexscreener.com/tokens/v1/solana/"
sol_price = SOL_PRICE_FALLBACK
coins = {}                                   # mint -> {name, born, hist, last_mc, max_mc}
dex_watch = set()
strat = {s["id"]: {"positions": {}, "closed": []} for s in STRATEGIES}
stats = {"seen": 0, "trade_msgs": 0, "dex_updates": 0, "dex_errors": 0,
         "reached": {"10K": 0, "20K": 0, "30K": 0, "60K": 0, "100K": 0}}
LEVELS = [("10K", 10_000), ("20K", 20_000), ("30K", 30_000), ("60K", 60_000), ("100K", 100_000)]


def ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def say(*a):
    print(*a, flush=True)


def get_sol_price():
    global sol_price
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": "solana", "vs_currencies": "usd"}, timeout=10)
        sol_price = float(r.json()["solana"]["usd"])
    except Exception as e:
        say(f"[!] SOL price nahi mila, ${sol_price} use kar raha hoon ({e})")


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"strat": strat, "coins": coins, "dex_watch": list(dex_watch),
                       "stats": stats, "sol_price": sol_price}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        say(f"[!] state save fail: {e}")


def load_state():
    global strat, coins, dex_watch, stats, sol_price
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        if "strat" not in s:          # purana v2 file - chhod do
            return
        for sid in strat:
            if sid in s["strat"]:
                strat[sid] = s["strat"][sid]
                st = strat[sid]
                if "cash" not in st:      # purane data se wallet ka hisaab banao
                    allp = list(st["positions"].values()) + st["closed"]
                    st["cash"] = START_CASH - PAPER_SIZE * len(allp) + sum(p["cash_back"] for p in allp)
        coins = s.get("coins", {})
        dex_watch = set(s.get("dex_watch", []))
        old = s.get("stats", {})
        for k in stats:
            if k in old:
                stats[k] = old[k]
        sol_price = s.get("sol_price", sol_price)
        say("Purana data mila, wahin se aage: " +
            ", ".join(f"{k} {len(v['closed'])} band/{len(v['positions'])} khule"
                      for k, v in strat.items()))
    except Exception as e:
        say(f"[!] state load fail, naya shuru: {e}")


def log_trade(row):
    new = not os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "strategy", "mint", "name", "action", "mc_usd", "usd", "note"])
        w.writerow(row)


def in_any_position(mint):
    return any(mint in v["positions"] for v in strat.values())


def check_entry(s, mint, c, now, mc):
    st = strat[s["id"]]
    if mint in st["positions"] or any(p["mint"] == mint for p in st["closed"]):
        return
    if not (s["mc_min"] <= mc <= s["mc_max"]):
        return
    old = [m for (t, m) in c["hist"] if t <= now - RISE_WINDOW]
    base = old[-1] if old else c["hist"][0][1]
    if base <= 0 or mc < base * (1 + s["rise"] / 100):
        return
    if st.get("cash", START_CASH) < PAPER_SIZE:      # wallet khaali - trade skip
        st["skipped"] = st.get("skipped", 0) + 1
        return
    st["cash"] = st.get("cash", START_CASH) - PAPER_SIZE
    share = PAPER_SIZE * (1 - FEE_PCT / 100) / mc
    st["positions"][mint] = {"mint": mint, "name": c["name"], "entry_mc": mc, "share": share,
                             "cash_back": 0.0, "half_sold": False, "peak": mc, "t": now}
    say(f"{ts()} [{s['id']}] BUY  {c['name'][:18]:18} MC ${mc:,.0f}  (5min pehle ${base:,.0f})")
    log_trade([ts(), s["id"], mint, c["name"], "BUY", round(mc), PAPER_SIZE, f"base {round(base)}"])


def sell(sid, p, frac, mc, why):
    amt = p["share"] * frac
    usd = amt * mc * (1 - FEE_PCT / 100)
    p["share"] -= amt
    p["cash_back"] += usd
    st = strat[sid]
    st["cash"] = st.get("cash", START_CASH) + usd
    say(f"{ts()} [{sid}] SELL {p['name'][:18]:18} {int(frac*100)}% @ MC ${mc:,.0f} -> ${usd:.2f} [{why}]")
    log_trade([ts(), sid, p["mint"], p["name"], f"SELL {int(frac*100)}%", round(mc), round(usd, 2), why])


def check_exit(s, mint, mc, now):
    sid = s["id"]
    st = strat[sid]
    p = st["positions"].get(mint)
    if not p:
        return
    p["peak"] = max(p["peak"], mc)
    if not p["half_sold"] and s["exit"] == "sl40" and mc <= p["entry_mc"] * (1 - SL_PCT / 100):
        sell(sid, p, 1.0, mc, f"2x se pehle entry se {SL_PCT}% gira")
        st["closed"].append(st["positions"].pop(mint))
        return
    if not p["half_sold"] and s["exit"] == "t60" and now - p["t"] >= TIME_EXIT \
            and mc < p["entry_mc"] * TAKE_HALF_AT:
        sell(sid, p, 1.0, mc, f"{TIME_EXIT//60} min mein 2x nahi hua")
        st["closed"].append(st["positions"].pop(mint))
        return
    if not p["half_sold"] and mc >= p["entry_mc"] * TAKE_HALF_AT:
        sell(sid, p, 0.5, mc, "2x - aadha")
        p["half_sold"] = True
        p["peak"] = mc
    elif p["half_sold"] and mc <= p["peak"] * (1 - TRAIL_DROP / 100):
        sell(sid, p, 1.0, mc, f"peak ${p['peak']:,.0f} se {TRAIL_DROP}% gira")
        st["closed"].append(st["positions"].pop(mint))


def update(mint, mc, now):
    c = coins.get(mint)
    if not c or mc <= 0:
        return
    # data filter: achanak bada uchhal (galat price) - confirm hone tak ignore
    last = c.get("last_mc") or mc
    if mc > last * SPIKE_X:
        pend = c.setdefault("spike", [])
        pend.append(mc)
        if len(pend) <= SPIKE_CONFIRM:          # lagatar 3 reading uchhal dikhaye tabhi maano
            stats["spikes_ignored"] = stats.get("spikes_ignored", 0) + 1
            return
    c["spike"] = []
    c["hist"].append((now, mc))
    c["hist"] = [(t, m) for (t, m) in c["hist"] if t >= now - RISE_WINDOW - 60]
    c["last_mc"] = mc
    prev_max = c.get("max_mc", 0)
    if mc > prev_max:
        for name, lvl in LEVELS:
            if prev_max < lvl <= mc:
                stats["reached"][name] += 1
        c["max_mc"] = mc
    if mc >= DEX_WATCH_FROM:
        dex_watch.add(mint)
    for s in STRATEGIES:
        check_entry(s, mint, c, now, mc)
        check_exit(s, mint, mc, now)


def fetch_dex(chunk):
    r = requests.get(DEX_URL + ",".join(chunk), timeout=10)
    r.raise_for_status()
    return r.json()


async def dex_poller():
    while True:
        await asyncio.sleep(DEX_POLL_SEC)
        mints = list(coins.keys())
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            await asyncio.sleep(0.25)
            try:
                data = await asyncio.to_thread(fetch_dex, chunk)
            except Exception:
                stats["dex_errors"] += 1
                await asyncio.sleep(5)
                continue
            best = {}
            for p in data if isinstance(data, list) else []:
                a = (p.get("baseToken") or {}).get("address")
                q = (p.get("quoteToken") or {}).get("address")
                if q not in GOOD_QUOTES:          # ajeeb pair (jaise ZCASHERS wala) skip
                    continue
                try:
                    mc = float(p.get("priceUsd") or 0) * TOTAL_SUPPLY   # har pair pe ek jaisa hisaab
                except ValueError:
                    continue
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if a and mc > 0 and (a not in best or liq > best[a][1]):
                    best[a] = (mc, liq)
            now = time.time()
            for a, (mc, _) in best.items():
                stats["dex_updates"] += 1
                update(a, mc, now)


def report():
    L = ["=" * 60, f"REPORT  {ts()} UTC", ""]
    for s in STRATEGIES:
        st = strat[s["id"]]
        tin = tback = 0.0
        wins = 0
        for p in st["closed"]:
            tin += PAPER_SIZE
            tback += p["cash_back"]
            wins += (p["cash_back"] - PAPER_SIZE) > 0
        oval = 0.0
        for m, p in st["positions"].items():
            last = coins.get(m, {}).get("last_mc", p["entry_mc"])
            v = p["cash_back"] + p["share"] * last
            oval += v
            tin += PAPER_SIZE
            tback += v
        xname = {"base": "sirf Mohit ke rules", "sl40": f"+ 2x se pehle -{SL_PCT}% pe becho",
                 "t60": f"+ {TIME_EXIT//60} min mein 2x nahi to becho"}[s["exit"]]
        L.append(f"[{s['id']}]  entry ${s['mc_min']//1000}K-{s['mc_max']//1000}K | exit: {xname}")
        L.append(f"  Band trades: {len(st['closed'])}  (jeete {wins}, haare {len(st['closed'])-wins})")
        L.append(f"  Khule trades: {len(st['positions'])}  (abhi ki value ${oval:.2f})")
        cash = st.get("cash", START_CASH)
        L.append(f"  Wallet: cash ${cash:.2f} + khule trades ${oval - sum(p['cash_back'] for p in st['positions'].values()):.2f}"
                 f" = ${cash + oval - sum(p['cash_back'] for p in st['positions'].values()):.2f}  (shuru ${START_CASH:.0f})"
                 f" | paise khatam hone se skip: {st.get('skipped', 0)}")
        L.append("")
    r = stats["reached"]
    L.append(f"Data: naye coins {stats['seen']} | inme se kitne pahunche -> "
             f"10K:{r['10K']} 20K:{r['20K']} 30K:{r['30K']} 60K:{r['60K']} 100K:{r['100K']}")
    L.append(f"      abhi dekh rahe coins {len(coins)} | DexScreener updates {stats['dex_updates']} "
             f"(errors {stats['dex_errors']}) | galat uchhal ignore {stats.get('spikes_ignored', 0)} "
             f"| SOL ${sol_price:.2f}")
    L.append("=" * 60)
    text = "\n".join(L)
    say("\n" + text + "\n")
    try:
        with open(REPORT_FILE, "w") as f:
            f.write(text + "\n")
    except Exception:
        pass


def git_push():
    subprocess.run("git add -A && (git commit -qm 'paper bot update' || true) && "
                   "git pull -q --rebase && git push -q", shell=True, timeout=120)


async def run_once(start, timers):
    async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20,
                                  max_size=2**22) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        say(f"{ts()} Connected. Live coins sun raha hoon...")
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
                    stats["seen"] += 1
                    coins[mint] = {"name": msg.get("symbol") or msg.get("name") or mint[:6],
                                   "born": now, "hist": [(now, mc)], "last_mc": mc, "max_mc": mc}
                elif mint in coins:
                    stats["trade_msgs"] += 1
                    update(mint, mc, now)

            if now - timers["cleanup"] > 60:
                drop = []
                for m, c in coins.items():
                    if in_any_position(m):
                        continue
                    age = now - c["born"]
                    if (m in dex_watch and age > DEX_WATCH_MIN * 60) or \
                       (m not in dex_watch and age > WATCH_MIN * 60) or \
                       (age > DEAD_AFTER_MIN * 60 and c.get("max_mc", 0) < DEAD_MC):
                        drop.append(m)
                for m in drop:
                    coins.pop(m, None)
                    dex_watch.discard(m)
                timers["cleanup"] = now
            if now - timers["save"] > 60:
                save_state(); timers["save"] = now
            if now - timers["price"] > 600:
                await asyncio.to_thread(get_sol_price); timers["price"] = now
            if now - timers["report"] > timers["report_gap"]:
                report(); timers["report"] = now; timers["report_gap"] = 900
            if AUTO_COMMIT and now - timers["commit"] > COMMIT_EVERY:
                save_state(); report()
                await asyncio.to_thread(git_push); timers["commit"] = now


async def main():
    load_state()
    get_sol_price()
    say(f"SOL = ${sol_price}" + (f"   (chalega {RUN_HOURS} ghante)" if RUN_HOURS else ""))
    start = time.time()
    t = time.time()
    timers = {"cleanup": t, "save": t, "price": t, "report": t, "report_gap": 180,
              "commit": t - COMMIT_EVERY + 300}      # pehla save 5 min pe
    poller = asyncio.create_task(dex_poller())
    while True:
        try:
            if await run_once(start, timers):
                say(f"{ts()} Time poora hua, band kar raha hoon.")
                break
        except Exception as e:
            say(f"{ts()} [!] Connection toota ({type(e).__name__}: {e}). 5 sec mein dobara...")
            save_state()
            await asyncio.sleep(5)
    poller.cancel()
    save_state()
    report()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        save_state()
        report()
