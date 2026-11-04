import asyncio, json, time, requests, websockets


async def main():
    creates = []
    async with websockets.connect("wss://pumpportal.fun/api/data", max_size=2**22) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        t0 = time.time(); other = 0; shown = 0
        while time.time() - t0 < 120:
            try:
                raw = await asyncio.wait_for(ws.recv(), 30)
            except asyncio.TimeoutError:
                print("timeout"); continue
            m = json.loads(raw)
            if m.get("txType") == "create":
                creates.append(m["mint"])
                if len(creates) <= 2:
                    print("CREATE", json.dumps(m)[:700])
                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [m["mint"]]}))
            else:
                other += 1
                if shown < 10:
                    print("OTHER", json.dumps(m)[:700]); shown += 1
        print(f"creates={len(creates)} other_msgs={other}")

    # alag connection: sirf trades ke liye
    async with websockets.connect("wss://pumpportal.fun/api/data", max_size=2**22) as ws2:
        await ws2.send(json.dumps({"method": "subscribeTokenTrade", "keys": creates[-40:]}))
        t1 = time.time(); n = 0
        while time.time() - t1 < 60:
            try:
                raw = await asyncio.wait_for(ws2.recv(), 30)
            except asyncio.TimeoutError:
                print("ws2 timeout"); break
            n += 1
            if n <= 5:
                print("WS2", raw[:700])
        print("separate-connection msgs:", n)

    r = requests.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(creates[:10]), timeout=10)
    print("dex status", r.status_code, r.text[:1500])


asyncio.run(main())
