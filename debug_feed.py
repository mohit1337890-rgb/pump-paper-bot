import asyncio, json, time, requests, websockets

BAD = ["TTWofwAge91oFhZs7kpQdyrVRkmevgM88xijGvQFbKo", "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"]
r = requests.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(BAD), timeout=10)
for p in r.json():
    print("PAIR", p.get("dexId"), "base", p["baseToken"]["address"][:8], p["baseToken"].get("symbol"),
          "quote", p["quoteToken"]["address"][:8], p["quoteToken"].get("symbol"),
          "priceUsd", p.get("priceUsd"), "mc", p.get("marketCap"), "fdv", p.get("fdv"),
          "liq", (p.get("liquidity") or {}).get("usd"), "labels", p.get("labels"))


async def main():
    n = 0; nonpump = []
    async with websockets.connect("wss://pumpportal.fun/api/data", max_size=2**22) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        t0 = time.time()
        while time.time() - t0 < 90:
            m = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if m.get("txType") != "create":
                continue
            n += 1
            if not m["mint"].endswith("pump"):
                nonpump.append(m)
    print("creates", n, "non-pump mints", len(nonpump))
    for m in nonpump[:5]:
        print("NONPUMP", json.dumps(m)[:500])

asyncio.run(main())
