"""Signal Desk — FastAPI + WebSocket server, PumpPortal client, trading
engine loop. Python port of server.js; behavior and API shape are meant to
match it exactly so the existing public/ dashboard works unmodified against
either backend.
"""

import asyncio
import json
import os
import random
import string
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import engine
import state_store
from live_trader import TradeError, execute_trade
from wallet import WalletError, get_sol_balance, load_wallet, make_connection

PORT = int(os.environ.get("PORT", "3000"))
TICK_SEC = 1.4
LIVE_TICK_SEC = 4.0
PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
WATCHLIST_MAX = 12
FEED_MAX = 20
SIM_TOKEN_NAMES = ["WOJAK", "FROGE", "MOONK", "BOBER", "SNEK", "GIGA", "PEPI", "TURBO"]
PUBLIC_DIR = Path(__file__).parent / "public"


def _coalesce(*vals):
    for v in vals:
        if v is not None:
            return v
    return None


def _rand_suffix() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


# ---------- in-memory state ----------
_saved = state_store.load_persisted() or {}
_saved_live = _saved.get("liveTrading") or {}

state = {
    "config": {**engine.DEFAULT_CONFIG, **(_saved.get("config") or {})},
    "dataMode": "simulated",  # 'simulated' | 'live'
    "apiKey": _saved.get("apiKey", ""),
    "ntfyTopic": _saved.get("ntfyTopic", ""),
    "wsStatus": "idle",
    "running": False,
    "tokens": [engine.make_sim_token(n) for n in SIM_TOKEN_NAMES],
    "positions": [],
    "trades": _saved.get("trades", []),
    "dailyStats": _saved.get("dailyStats") or {"date": engine.today_str(), "pnl": 0, "wins": 0, "losses": 0, "halted": None},
    "reports": _saved.get("reports", []),
    "tuningLog": _saved.get("tuningLog", []),
    "equity": _saved.get("equity") or [{"t": 0, "v": 0}],
    "balance": _saved.get("balance") if isinstance(_saved.get("balance"), (int, float)) else 1000,
    "newTokenFeed": [],
    "migrationFeed": [],
    "debugFeed": [],
    # ---- live trading (real funds — off by default, never resumes automatically) ----
    "liveTrading": {
        "enabled": False,
        "keypairPath": _saved_live.get("keypairPath", "./wallet/keypair.json"),
        "rpcUrl": _saved_live.get("rpcUrl", "https://api.mainnet-beta.solana.com"),
        "maxSolPerTrade": _saved_live.get("maxSolPerTrade", 0.01),
        "maxConcurrentPositions": _saved_live.get("maxConcurrentPositions", 1),
        "dailyLossLimitSol": _saved_live.get("dailyLossLimitSol", 0.05),
        "slippagePct": _saved_live.get("slippagePct", 15),
        "priorityFeeSol": _saved_live.get("priorityFeeSol", 0.0005),
        "pool": _saved_live.get("pool", "auto"),
        "lastError": None,
    },
    "livePositions": [],
    "liveTrades": _saved.get("liveTrades", []),
    "liveDailyStats": _saved.get("liveDailyStats") or {"date": engine.today_str(), "pnlSol": 0, "wins": 0, "losses": 0, "halted": None},
}


class Runtime:
    """Mutable process-level handles that don't belong in the JSON state
    dict (live sockets, tasks, the wallet object, etc)."""

    def __init__(self):
        self.watchlist: list[str] = []
        self.last_tune_count = 0
        self.pump_ws_task: asyncio.Task | None = None
        self.pump_ws = None
        self.reconnect_task: asyncio.Task | None = None
        self.live_wallet = None
        self.live_connection = None
        self.live_busy = False


runtime = Runtime()
connected_clients: set[WebSocket] = set()


def persist():
    state_store.save_persisted({
        "config": state["config"],
        "apiKey": state["apiKey"],
        "ntfyTopic": state["ntfyTopic"],
        "trades": state["trades"],
        "dailyStats": state["dailyStats"],
        "reports": state["reports"],
        "tuningLog": state["tuningLog"],
        "equity": state["equity"],
        "balance": state["balance"],
        # liveTrading.enabled is intentionally NOT trusted back on restart — see enable_live_trading below.
        "liveTrading": state["liveTrading"],
        "liveTrades": state["liveTrades"],
        "liveDailyStats": state["liveDailyStats"],
    })


# ---------- live trading (real funds) ----------
def enable_live_trading() -> dict:
    try:
        runtime.live_wallet = load_wallet(state["liveTrading"]["keypairPath"])
        runtime.live_connection = make_connection(state["liveTrading"]["rpcUrl"])
        state["liveTrading"]["enabled"] = True
        state["liveTrading"]["lastError"] = None
        print(f"[live] ENABLED — wallet {runtime.live_wallet.pubkey()} — RPC {state['liveTrading']['rpcUrl']}")
        return {"ok": True, "publicKey": str(runtime.live_wallet.pubkey())}
    except (WalletError, Exception) as e:
        runtime.live_wallet = None
        runtime.live_connection = None
        state["liveTrading"]["enabled"] = False
        state["liveTrading"]["lastError"] = str(e)
        return {"ok": False, "error": str(e)}


def disable_live_trading(reason: str | None = None):
    state["liveTrading"]["enabled"] = False
    if reason:
        state["liveTrading"]["lastError"] = reason
    print(f"[live] DISABLED{' — ' + reason if reason else ''}")


def check_live_rollover():
    if state["liveDailyStats"]["date"] == engine.today_str():
        return
    state["liveDailyStats"] = {"date": engine.today_str(), "pnlSol": 0, "wins": 0, "losses": 0, "halted": None}


async def live_tick():
    """Runs on its own slower interval, decoupled from the paper tick,
    because real trades involve network round-trips (build tx, simulate,
    submit, confirm) that must never block the simulator loop. live_busy
    prevents two ticks from overlapping if a trade is still in flight."""
    lt = state["liveTrading"]
    if not lt["enabled"] or runtime.live_busy or runtime.live_wallet is None or runtime.live_connection is None:
        return
    if state["dataMode"] != "live":  # real trades require real market data
        return
    runtime.live_busy = True
    try:
        check_live_rollover()
        cfg = lt

        still_open = []
        for pos in state["livePositions"]:
            tok = next((t for t in state["tokens"] if t["id"] == pos["tokenId"]), None)
            if not tok or not tok["price"]:
                still_open.append(pos)
                continue
            change_pct = engine.pct(tok["price"], pos["entryPrice"])
            high_water = max(pos["highWater"], tok["price"])
            from_high_pct = engine.pct(tok["price"], high_water)
            close_reason = None
            if change_pct <= -state["config"]["stopLossPct"]:
                close_reason = "stop-loss"
            elif change_pct >= state["config"]["takeProfitPct"]:
                close_reason = "take-profit"
            elif from_high_pct <= -state["config"]["trailingStopPct"] and change_pct > 0:
                close_reason = "trailing-stop"

            if close_reason:
                try:
                    result = await execute_trade(
                        connection=runtime.live_connection, wallet=runtime.live_wallet, mint=pos["tokenId"],
                        action="sell", amount="100%", denominated_in_sol=False,
                        slippage_pct=cfg["slippagePct"], priority_fee_sol=cfg["priorityFeeSol"], pool=cfg["pool"],
                    )
                    pnl_sol = round(pos["solSpent"] * (change_pct / 100), 6)
                    state["liveDailyStats"]["pnlSol"] = round(state["liveDailyStats"]["pnlSol"] + pnl_sol, 6)
                    if pnl_sol > 0:
                        state["liveDailyStats"]["wins"] += 1
                    else:
                        state["liveDailyStats"]["losses"] += 1
                    state["liveTrades"] = ([{
                        "id": f"{pos['tokenId']}-{int(datetime.now().timestamp() * 1000)}",
                        "token": tok.get("name") or pos["tokenId"], "action": "sell",
                        "reason": close_reason, "solSpent": pos["solSpent"], "pnlSol": pnl_sol,
                        "signature": result["signature"], "explorerUrl": result["explorerUrl"],
                        "date": state["liveDailyStats"]["date"], "closedAt": datetime.now(timezone.utc).isoformat(), "status": "confirmed",
                    }] + state["liveTrades"])[:200]
                except TradeError as e:
                    print(f"[live] sell failed, will retry next tick: {e}")
                    state["liveTrading"]["lastError"] = str(e)
                    still_open.append(pos)
                    continue
            else:
                still_open.append({**pos, "highWater": high_water})
        state["livePositions"] = still_open

        if not state["liveDailyStats"]["halted"] and state["liveDailyStats"]["pnlSol"] <= -cfg["dailyLossLimitSol"]:
            state["liveDailyStats"]["halted"] = "daily SOL loss limit reached"

        can_open = not state["liveDailyStats"]["halted"] and len(state["livePositions"]) < cfg["maxConcurrentPositions"]
        if can_open:
            held_ids = {p["tokenId"] for p in state["livePositions"]}
            for tok in state["tokens"]:
                if len(state["livePositions"]) >= cfg["maxConcurrentPositions"]:
                    break
                if not tok.get("live") or not tok.get("hasTradeData") or tok["id"] in held_ids or tok["price"] <= 0:
                    continue
                sig = engine.momentum_signal(tok, state["config"])
                if not sig["hit"]:
                    continue
                try:
                    bal_sol = await get_sol_balance(runtime.live_connection, runtime.live_wallet.pubkey())
                    fee_buffer = 0.01  # headroom for network + priority fees
                    if bal_sol < cfg["maxSolPerTrade"] + fee_buffer:
                        state["liveTrading"]["lastError"] = (
                            f"Skipped entry on {tok['name']}: wallet balance {bal_sol:.4f} SOL is too low "
                            f"for a {cfg['maxSolPerTrade']} SOL position plus fees."
                        )
                        break
                    result = await execute_trade(
                        connection=runtime.live_connection, wallet=runtime.live_wallet, mint=tok["id"],
                        action="buy", amount=cfg["maxSolPerTrade"], denominated_in_sol=True,
                        slippage_pct=cfg["slippagePct"], priority_fee_sol=cfg["priorityFeeSol"], pool=cfg["pool"],
                    )
                    state["livePositions"].append({
                        "tokenId": tok["id"], "entryPrice": tok["price"], "solSpent": cfg["maxSolPerTrade"],
                        "highWater": tok["price"], "openedAt": datetime.now(timezone.utc).isoformat(),
                        "signature": result["signature"],
                    })
                    held_ids.add(tok["id"])
                    state["liveTrades"] = ([{
                        "id": f"{tok['id']}-{int(datetime.now().timestamp() * 1000)}",
                        "token": tok.get("name") or tok["id"], "action": "buy",
                        "reason": "momentum entry", "solSpent": cfg["maxSolPerTrade"], "pnlSol": None,
                        "signature": result["signature"], "explorerUrl": result["explorerUrl"],
                        "date": state["liveDailyStats"]["date"], "closedAt": datetime.now(timezone.utc).isoformat(), "status": "confirmed",
                    }] + state["liveTrades"])[:200]
                except TradeError as e:
                    print(f"[live] buy failed: {e}")
                    state["liveTrading"]["lastError"] = str(e)
        persist()
    except Exception as e:
        # Any unexpected failure in the live loop disables live trading
        # rather than risking it continuing in a half-understood state.
        print(f"[live] tick error — disabling live trading as a precaution: {e}")
        disable_live_trading(str(e))
    finally:
        runtime.live_busy = False


# ---------- pump.fun live feed ----------
async def _handle_pump_message(raw, ws):
    try:
        data = json.loads(raw)
    except Exception:
        return
    state["debugFeed"] = ([{"at": int(datetime.now().timestamp() * 1000), "data": data}] + state["debugFeed"])[:15]

    mint = _coalesce(data.get("mint"), data.get("mintAddress"))
    tx_type = _coalesce(data.get("txType"), data.get("method"))

    if tx_type == "create" and mint:
        if not any(t["id"] == mint for t in state["tokens"]):
            tok = engine.make_live_token(mint, _coalesce(data.get("symbol"), data.get("name")))
            runtime.watchlist = (runtime.watchlist + [mint])[-WATCHLIST_MAX:]
            if state["apiKey"]:
                try:
                    await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
                except Exception:
                    pass
            state["tokens"] = ([t for t in state["tokens"] if not t.get("live") or t["id"] in runtime.watchlist] + [tok])[-40:]
        state["newTokenFeed"] = ([{
            "mint": mint, "name": data.get("name"), "symbol": data.get("symbol"), "at": int(datetime.now().timestamp() * 1000),
        }] + state["newTokenFeed"])[:FEED_MAX]
        return

    if (tx_type == "migrate" or data.get("pool") == "raydium" or tx_type == "complete") and mint:
        state["tokens"] = [{**t, "migrated": True} if t["id"] == mint else t for t in state["tokens"]]
        state["migrationFeed"] = ([{"mint": mint, "at": int(datetime.now().timestamp() * 1000)}] + state["migrationFeed"])[:FEED_MAX]
        return

    if tx_type in ("buy", "sell") and mint:
        sol_amount = float(_coalesce(data.get("solAmount"), data.get("sol_amount"), 0))
        token_amount = float(_coalesce(data.get("tokenAmount"), data.get("token_amount"), 0))
        state["tokens"] = [
            engine.apply_trade_to_token(t, sol_amount, token_amount) if t["id"] == mint else t
            for t in state["tokens"]
        ]


async def _pump_ws_run():
    try:
        async with websockets.connect(PUMPPORTAL_WS) as ws:
            runtime.pump_ws = ws
            state["wsStatus"] = "connected"
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            await ws.send(json.dumps({"method": "subscribeMigration"}))
            print("[pumpportal] connected")
            async for raw in ws:
                await _handle_pump_message(raw, ws)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[pumpportal] error: {e}")
    finally:
        runtime.pump_ws = None
        print("[pumpportal] disconnected")
        if state["dataMode"] == "live":
            state["wsStatus"] = "error"
            if runtime.reconnect_task and not runtime.reconnect_task.done():
                runtime.reconnect_task.cancel()
            runtime.reconnect_task = asyncio.create_task(_reconnect_after_delay())
        else:
            state["wsStatus"] = "idle"


async def _reconnect_after_delay():
    try:
        await asyncio.sleep(5)
        if state["dataMode"] == "live":
            connect_live()
    except asyncio.CancelledError:
        pass


def connect_live():
    if runtime.pump_ws_task and not runtime.pump_ws_task.done():
        runtime.pump_ws_task.cancel()
    state["wsStatus"] = "connecting"
    runtime.pump_ws_task = asyncio.create_task(_pump_ws_run())


def disconnect_live():
    if runtime.reconnect_task and not runtime.reconnect_task.done():
        runtime.reconnect_task.cancel()
    if runtime.pump_ws_task and not runtime.pump_ws_task.done():
        runtime.pump_ws_task.cancel()
    state["wsStatus"] = "idle"


def set_data_mode(mode: str):
    state["dataMode"] = mode
    if mode == "live":
        state["tokens"] = [t for t in state["tokens"] if t.get("live")]
        connect_live()
    else:
        disconnect_live()
        state["tokens"] = [engine.make_sim_token(n) for n in SIM_TOKEN_NAMES]


# ---------- daily rollover ----------
def check_rollover():
    if state["dailyStats"]["date"] == engine.today_str():
        return
    closed_today = [t for t in state["trades"] if t["date"] == state["dailyStats"]["date"]]
    report = {
        "date": state["dailyStats"]["date"],
        "pnl": state["dailyStats"]["pnl"],
        "wins": state["dailyStats"]["wins"],
        "losses": state["dailyStats"]["losses"],
        "tradeCount": len(closed_today),
        "haltedReason": state["dailyStats"]["halted"],
        "feedback": engine.gen_feedback(closed_today, state["config"]),
    }
    state["reports"] = ([report] + state["reports"])[:60]
    state["dailyStats"] = {"date": engine.today_str(), "pnl": 0, "wins": 0, "losses": 0, "halted": None}
    state["equity"] = [{"t": 0, "v": 0}]
    runtime.last_tune_count = 0
    halted_note = f" · halted: {report['haltedReason']}" if report["haltedReason"] else ""
    asyncio.create_task(notify_ntfy(
        f"Signal Desk — {report['date']} closed",
        f"P&L {'+' if report['pnl'] >= 0 else ''}${report['pnl']:.2f} · {report['wins']}W/{report['losses']}L · {report['tradeCount']} trades{halted_note}",
    ))


# ---------- engine tick ----------
async def tick():
    check_rollover()

    if state["dataMode"] == "simulated":
        state["tokens"] = [engine.step_sim_token(t) for t in state["tokens"]]

    if not state["running"]:
        return

    local_trades = []
    pnl_delta, wins_delta, losses_delta = 0.0, 0, 0
    still_open = []

    for pos in state["positions"]:
        tok = next((t for t in state["tokens"] if t["id"] == pos["tokenId"]), None)
        if not tok:
            still_open.append(pos)
            continue
        change_pct = engine.pct(tok["price"], pos["entryPrice"])
        high_water = max(pos["highWater"], tok["price"])
        from_high_pct = engine.pct(tok["price"], high_water)
        close_reason = None
        if change_pct <= -state["config"]["stopLossPct"]:
            close_reason = "stop-loss"
        elif change_pct >= state["config"]["takeProfitPct"]:
            close_reason = "take-profit"
        elif from_high_pct <= -state["config"]["trailingStopPct"] and change_pct > 0:
            close_reason = "trailing-stop"

        if close_reason:
            pnl = pos["size"] * (change_pct / 100)
            pnl_delta += pnl
            if pnl > 0:
                wins_delta += 1
            else:
                losses_delta += 1
            state["balance"] += pos["size"] + pnl
            local_trades.append({
                "id": f"{pos['tokenId']}-{int(datetime.now().timestamp() * 1000)}-{_rand_suffix()}",
                "token": tok.get("name") or pos["tokenId"],
                "entry": pos["entryPrice"], "exit": tok["price"], "pnl": pnl,
                "reason": close_reason, "date": state["dailyStats"]["date"],
                "closedAt": datetime.now(timezone.utc).isoformat(),
            })
        else:
            still_open.append({**pos, "highWater": high_water})

    projected_pnl = state["dailyStats"]["pnl"] + pnl_delta
    can_open_new = (
        not state["dailyStats"]["halted"]
        and projected_pnl > -state["config"]["dailyLossLimit"]
        and projected_pnl < state["config"]["dailyProfitCap"]
        and len(still_open) < state["config"]["maxOpenPositions"]
    )
    final_open = still_open
    if can_open_new:
        held_ids = {p["tokenId"] for p in still_open}
        for tok in state["tokens"]:
            if len(final_open) >= state["config"]["maxOpenPositions"]:
                break
            if state["balance"] < state["config"]["positionSize"]:
                break  # out of paper funds
            if tok["id"] in held_ids or tok["price"] <= 0:
                continue
            sig = engine.momentum_signal(tok, state["config"])
            if sig["hit"]:
                final_open.append({
                    "tokenId": tok["id"], "entryPrice": tok["price"], "size": state["config"]["positionSize"],
                    "highWater": tok["price"], "openedAt": datetime.now(timezone.utc).isoformat(),
                })
                held_ids.add(tok["id"])
                state["balance"] -= state["config"]["positionSize"]
    state["positions"] = final_open

    if local_trades:
        state["trades"] = (state["trades"] + local_trades)[-1000:]
        running_total = state["equity"][-1]["v"] if state["equity"] else 0
        base_len = len(state["equity"])
        additions = []
        for t in local_trades:
            running_total += t["pnl"]
            additions.append({"t": base_len + 1, "v": round(running_total, 2)})
        state["equity"] = (state["equity"] + additions)[-300:]

    new_pnl = state["dailyStats"]["pnl"] + pnl_delta
    halted = state["dailyStats"]["halted"]
    if not halted and new_pnl <= -state["config"]["dailyLossLimit"]:
        halted = "daily loss limit reached"
    if not halted and new_pnl >= state["config"]["dailyProfitCap"]:
        halted = "daily profit cap reached — locking in gains"
    state["dailyStats"] = {
        **state["dailyStats"], "pnl": round(new_pnl, 2),
        "wins": state["dailyStats"]["wins"] + wins_delta,
        "losses": state["dailyStats"]["losses"] + losses_delta,
        "halted": halted,
    }

    # self-tuning
    if state["config"]["autoTune"]:
        todays = [t for t in state["trades"] if t["date"] == state["dailyStats"]["date"]]
        if len(todays) >= runtime.last_tune_count + engine.TUNE_EVERY_N_TRADES:
            runtime.last_tune_count = len(todays) - (len(todays) % engine.TUNE_EVERY_N_TRADES)
            recent = todays[-15:]
            result = engine.compute_auto_tune(recent, state["config"])
            if result["changes"]:
                state["config"] = result["newConfig"]
                state["tuningLog"] = ([{"at": datetime.now(timezone.utc).isoformat(), "changes": result["changes"]}] + state["tuningLog"])[:80]

    persist()


async def tick_loop():
    while True:
        await asyncio.sleep(TICK_SEC)
        await tick()


async def live_tick_loop():
    while True:
        await asyncio.sleep(LIVE_TICK_SEC)
        await live_tick()


# ---------- noon push notification (optional, via ntfy.sh) ----------
async def notify_ntfy(title: str, message: str):
    if not state["ntfyTopic"]:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"https://ntfy.sh/{state['ntfyTopic']}", headers={"Title": title}, content=message)
    except Exception as e:
        print(f"[ntfy] failed to send notification: {e}")


async def noon_notifier_loop():
    last_fired_date = None
    while True:
        now = datetime.now()
        if now.hour == 12 and now.minute == 0 and last_fired_date != now.date():
            last_fired_date = now.date()
            s = state["dailyStats"]
            halted_note = f" · halted: {s['halted']}" if s["halted"] else ""
            await notify_ntfy(
                "Signal Desk — midday check-in",
                f"Today so far: {'+' if s['pnl'] >= 0 else ''}${s['pnl']:.2f} · {s['wins']}W/{s['losses']}L{halted_note}",
            )
        await asyncio.sleep(30)


async def broadcast_loop():
    while True:
        await asyncio.sleep(TICK_SEC)
        if connected_clients:
            payload = json.dumps(public_state())
            dead = []
            for client in list(connected_clients):
                try:
                    await client.send_text(payload)
                except Exception:
                    dead.append(client)
            for d in dead:
                connected_clients.discard(d)


async def _open_browser_delayed(url: str):
    await asyncio.sleep(1)
    try:
        webbrowser.open(url)
    except Exception:
        pass


# ---------- HTTP + WS API for the dashboard ----------
def public_state() -> dict:
    def _tok_field(token_id, field):
        tok = next((t for t in state["tokens"] if t["id"] == token_id), None)
        return tok[field] if tok else None

    return {
        "config": state["config"],
        "dataMode": state["dataMode"],
        "wsStatus": state["wsStatus"],
        "running": state["running"],
        "hasApiKey": bool(state["apiKey"]),
        "ntfyTopic": state["ntfyTopic"],
        "balance": round(state["balance"], 2),
        "lockedInPositions": round(sum(p["size"] for p in state["positions"]), 2),
        "tokens": [
            {
                "id": t["id"], "name": t["name"], "price": t["price"], "live": t.get("live", False),
                "migrated": bool(t.get("migrated")), "hasTradeData": bool(t.get("hasTradeData")),
                "signal": engine.momentum_signal(t, state["config"]),
            }
            for t in state["tokens"][-20:]
        ],
        "positions": [
            {**p, "tokenName": _tok_field(p["tokenId"], "name") or p["tokenId"], "currentPrice": _tok_field(p["tokenId"], "price")}
            for p in state["positions"]
        ],
        "trades": list(reversed(state["trades"][-30:])),
        "dailyStats": state["dailyStats"],
        "reports": state["reports"],
        "tuningLog": state["tuningLog"][:30],
        "equity": state["equity"],
        "newTokenFeed": state["newTokenFeed"],
        "migrationFeed": state["migrationFeed"],
        "debugFeed": state["debugFeed"],
        "feedback": engine.gen_feedback([t for t in state["trades"] if t["date"] == state["dailyStats"]["date"]], state["config"]),
        "liveTrading": state["liveTrading"],
        "livePositions": [
            {**p, "tokenName": _tok_field(p["tokenId"], "name") or p["tokenId"], "currentPrice": _tok_field(p["tokenId"], "price")}
            for p in state["livePositions"]
        ],
        "liveTrades": state["liveTrades"][:30],
        "liveDailyStats": state["liveDailyStats"],
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(tick_loop())
    asyncio.create_task(live_tick_loop())
    asyncio.create_task(broadcast_loop())
    asyncio.create_task(noon_notifier_loop())
    url = f"http://localhost:{PORT}"
    print(f"Signal Desk running at {url}")
    if os.environ.get("NO_AUTO_OPEN") != "1":
        asyncio.create_task(_open_browser_delayed(url))
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/api/state")
async def api_get_state():
    return public_state()


@app.post("/api/config")
async def api_post_config(req: Request):
    body = await req.json()
    state["config"] = {**state["config"], **body}
    persist()
    return {"ok": True}


@app.post("/api/control")
async def api_post_control(req: Request):
    body = await req.json()
    if isinstance(body.get("running"), bool):
        state["running"] = body["running"]
    return {"ok": True}


@app.post("/api/datamode")
async def api_post_datamode(req: Request):
    body = await req.json()
    if body.get("mode") in ("live", "simulated"):
        set_data_mode(body["mode"])
    return {"ok": True}


@app.post("/api/apikey")
async def api_post_apikey(req: Request):
    body = await req.json()
    state["apiKey"] = body.get("apiKey", "")
    persist()
    return {"ok": True}


@app.post("/api/balance")
async def api_post_balance(req: Request):
    body = await req.json()
    action = body.get("action")
    amt = float(body.get("amount") or 0)
    if action == "deposit":
        state["balance"] += max(0, amt)
    elif action == "withdraw":
        state["balance"] = max(0, state["balance"] - max(0, amt))
    elif action == "set":
        state["balance"] = max(0, amt)
    persist()
    return {"ok": True, "balance": state["balance"]}


@app.post("/api/ntfy")
async def api_post_ntfy(req: Request):
    body = await req.json()
    state["ntfyTopic"] = body.get("topic", "")
    persist()
    return {"ok": True}


# ---------- live trading API (real funds) ----------
@app.get("/api/live/status")
async def api_live_status():
    sol_balance, wallet_public_key, rpc_ok = None, None, False
    if runtime.live_wallet:
        wallet_public_key = str(runtime.live_wallet.pubkey())
    if runtime.live_wallet and runtime.live_connection:
        try:
            sol_balance = await get_sol_balance(runtime.live_connection, runtime.live_wallet.pubkey())
            rpc_ok = True
        except Exception:
            rpc_ok = False
    return {
        "enabled": state["liveTrading"]["enabled"], "walletPublicKey": wallet_public_key,
        "solBalance": sol_balance, "rpcOk": rpc_ok, "lastError": state["liveTrading"]["lastError"],
    }


@app.post("/api/live/config")
async def api_live_config(req: Request):
    body = await req.json()
    allowed = ["keypairPath", "rpcUrl", "maxSolPerTrade", "maxConcurrentPositions", "dailyLossLimitSol", "slippagePct", "priorityFeeSol", "pool"]
    for k in allowed:
        if k in body:
            state["liveTrading"][k] = body[k]
    persist()
    return {"ok": True}


@app.post("/api/live/enable")
async def api_live_enable(req: Request):
    body = await req.json()
    if body.get("confirmPhrase") != "ENABLE LIVE TRADING":
        return JSONResponse(status_code=400, content={"ok": False, "error": "Confirmation phrase did not match — type it exactly to enable."})
    return enable_live_trading()


@app.post("/api/live/disable")
async def api_live_disable():
    disable_live_trading()
    return {"ok": True}


@app.websocket("/")
async def dashboard_ws(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    try:
        await websocket.send_text(json.dumps(public_state()))
        while True:
            await websocket.receive_text()  # the dashboard never sends app data; this just detects disconnects
    except WebSocketDisconnect:
        pass
    finally:
        connected_clients.discard(websocket)


# static dashboard files — mounted last so it doesn't shadow the API/WS routes above
app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
