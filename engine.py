"""Core trading logic — no I/O in this file, just pure functions over state.

Ported 1:1 from the Node/JS version (engine.js) in the companion Signal Desk
repo. Tokens/positions/trades are plain dicts throughout (not dataclasses) so
they serialize straight to JSON for the WebSocket state broadcast and the
on-disk persistence file, exactly like the JS version.
"""

import random
from datetime import datetime, timezone

HISTORY_LEN = 40
TUNE_EVERY_N_TRADES = 8

DEFAULT_CONFIG = {
    "positionSize": 40,
    "maxOpenPositions": 3,
    "momentumPriceThreshold": 9,
    "momentumVolThreshold": 45,
    "takeProfitPct": 18,
    "stopLossPct": 8,
    "trailingStopPct": 6,
    "dailyLossLimit": 150,
    "dailyProfitFloor": 250,
    "dailyProfitCap": 1000,
    "autoTune": False,
}

# bounds the self-tuner is allowed to move strategy params within — it never
# touches position size or the daily loss/profit rails you set
PARAM_BOUNDS = {
    "momentumPriceThreshold": (3, 25),
    "momentumVolThreshold": (15, 150),
    "takeProfitPct": (8, 40),
    "stopLossPct": (4, 20),
    "trailingStopPct": (3, 15),
}


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def pct(a: float, b: float) -> float:
    return 0 if b == 0 else ((a - b) / b) * 100


def money(n: float) -> str:
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):.2f}"


def make_sim_token(name: str) -> dict:
    base = 0.0005 + random.random() * 0.02
    return {
        "id": name,
        "name": name,
        "price": base,
        "priceHistory": [base] * HISTORY_LEN,
        "volume": 500 + random.random() * 2000,
        "volHistory": [500] * HISTORY_LEN,
        "live": False,
    }


def make_live_token(mint: str, name: str | None) -> dict:
    return {
        "id": mint,
        "name": name or mint[:6],
        "price": 0,
        "priceHistory": [0] * HISTORY_LEN,
        "volume": 0,
        "volHistory": [0] * HISTORY_LEN,
        "live": True,
        "migrated": False,
        "hasTradeData": False,
        "createdAt": datetime.now(timezone.utc).timestamp() * 1000,
    }


def step_sim_token(t: dict) -> dict:
    spike_roll = random.random()
    drift = (random.random() - 0.5) * 0.03
    vol_mult = 1 + (random.random() - 0.5) * 0.3
    if spike_roll > 0.965:
        drift = 0.12 + random.random() * 0.35
        vol_mult = 2 + random.random() * 4
    elif spike_roll < 0.03:
        drift = -(0.1 + random.random() * 0.3)
        vol_mult = 1.5 + random.random() * 2
    new_price = max(0.00001, t["price"] * (1 + drift))
    new_vol = max(50, t["volume"] * vol_mult * (0.85 + random.random() * 0.3))
    return {
        **t,
        "price": new_price,
        "priceHistory": t["priceHistory"][1:] + [new_price],
        "volume": new_vol,
        "volHistory": t["volHistory"][1:] + [new_vol],
    }


def apply_trade_to_token(t: dict, sol_amount: float, token_amount: float) -> dict:
    """Applies one real trade event onto a live token's rolling history."""
    price = (sol_amount / token_amount) if token_amount > 0 else t["price"]
    decayed_volume = t["volume"] * 0.9 + sol_amount * 10  # approximation of recent trade intensity
    return {
        **t,
        "price": price or t["price"],
        "priceHistory": t["priceHistory"][1:] + [price or t["price"]],
        "volume": decayed_volume,
        "volHistory": t["volHistory"][1:] + [decayed_volume],
        "hasTradeData": True,
    }


def momentum_signal(t: dict, cfg: dict) -> dict:
    idx = max(0, HISTORY_LEN - 8)
    price_ago = t["priceHistory"][idx]
    vol_ago = t["volHistory"][idx]
    price_change = pct(t["price"], price_ago)
    vol_change = pct(t["volume"], vol_ago)
    eligible = t.get("hasTradeData", False) if t.get("live") else True
    hit = eligible and price_change >= cfg["momentumPriceThreshold"] and vol_change >= cfg["momentumVolThreshold"]
    return {"priceChange": price_change, "volChange": vol_change, "hit": hit}


def gen_feedback(trades: list[dict], cfg: dict) -> list[str]:
    recent = trades[-25:]
    if len(recent) < 5:
        return ["Not enough closed trades yet to generate tuning feedback — needs at least 5."]
    wins = [t for t in recent if t["pnl"] > 0]
    losses = [t for t in recent if t["pnl"] <= 0]
    win_rate = (len(wins) / len(recent)) * 100
    avg_win = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0
    avg_loss = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
    stop_outs = sum(1 for t in recent if t["reason"] == "stop-loss")
    trail_outs = sum(1 for t in recent if t["reason"] == "trailing-stop")
    tp_outs = sum(1 for t in recent if t["reason"] == "take-profit")

    notes = []
    notes.append(f"Last {len(recent)} trades: {win_rate:.0f}% win rate, avg win {money(avg_win)}, avg loss {money(avg_loss)}.")
    if stop_outs / len(recent) > 0.4:
        notes.append(f"{stop_outs} of {len(recent)} exits hit stop-loss. Stop-loss ({cfg['stopLossPct']}%) may be too tight for this volatility — consider widening it or lowering position size instead.")
    if tp_outs / len(recent) > 0.5 and avg_win < abs(avg_loss) * 1.5:
        notes.append(f"Take-profit ({cfg['takeProfitPct']}%) triggers often but wins aren't outsizing losses. Consider raising take-profit or tightening the trailing stop to let winners run.")
    if trail_outs / len(recent) > 0.3:
        notes.append(f"Trailing stop ({cfg['trailingStopPct']}%) is catching a lot of exits after partial pumps — this is often healthy, but if it's cutting winners short, widen it slightly.")
    if win_rate < 35:
        notes.append(f"Win rate is low. Momentum thresholds (price {cfg['momentumPriceThreshold']}% / volume {cfg['momentumVolThreshold']}%) may be too loose, entering on noise rather than real momentum.")
    if win_rate > 65 and avg_win < 5:
        notes.append("High win rate but small average wins — thresholds may be too strict, only catching moves that are nearly over already.")
    return notes


def _clamp(v, bounds):
    lo, hi = bounds
    return min(hi, max(lo, v))


def _step_val(current, direction, bounds_key):
    step = max(0.5, abs(current) * 0.12)
    return round(_clamp(current + direction * step, PARAM_BOUNDS[bounds_key]), 1)


def compute_auto_tune(recent: list[dict], cfg: dict):
    """Looks at the most recent closed trades and nudges strategy params —
    bounded, one small step per param per cycle, every change is logged with why."""
    if len(recent) < 5:
        return {"changes": [], "newConfig": cfg}

    wins = [t for t in recent if t["pnl"] > 0]
    losses = [t for t in recent if t["pnl"] <= 0]
    win_rate = (len(wins) / len(recent)) * 100
    avg_win = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0
    avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
    stop_out_rate = sum(1 for t in recent if t["reason"] == "stop-loss") / len(recent)
    trail_out_rate = sum(1 for t in recent if t["reason"] == "trailing-stop") / len(recent)
    tp_out_rate = sum(1 for t in recent if t["reason"] == "take-profit") / len(recent)

    next_cfg = dict(cfg)
    changes = []

    if stop_out_rate > 0.4:
        frm = next_cfg["stopLossPct"]
        next_cfg["stopLossPct"] = _step_val(frm, 1, "stopLossPct")
        if next_cfg["stopLossPct"] != frm:
            changes.append({
                "param": "Stop-loss %", "from": frm, "to": next_cfg["stopLossPct"],
                "reason": f"{round(stop_out_rate * 100)}% of recent exits were stop-outs — widening so normal volatility doesn't shake the position out early",
            })

    if win_rate < 35:
        from_p, from_v = next_cfg["momentumPriceThreshold"], next_cfg["momentumVolThreshold"]
        next_cfg["momentumPriceThreshold"] = _step_val(from_p, 1, "momentumPriceThreshold")
        next_cfg["momentumVolThreshold"] = _step_val(from_v, 1, "momentumVolThreshold")
        if next_cfg["momentumPriceThreshold"] != from_p or next_cfg["momentumVolThreshold"] != from_v:
            changes.append({
                "param": "Momentum thresholds",
                "from": f"{from_p}% / {from_v}%",
                "to": f"{next_cfg['momentumPriceThreshold']}% / {next_cfg['momentumVolThreshold']}%",
                "reason": f"Win rate is {win_rate:.0f}%, too low — tightening entry criteria to filter out noise",
            })
    elif win_rate > 65 and avg_win < avg_loss * 1.3 and avg_loss > 0:
        frm = next_cfg["takeProfitPct"]
        next_cfg["takeProfitPct"] = _step_val(frm, 1, "takeProfitPct")
        if next_cfg["takeProfitPct"] != frm:
            changes.append({
                "param": "Take-profit %", "from": frm, "to": next_cfg["takeProfitPct"],
                "reason": f"Win rate {win_rate:.0f}% is strong but average wins are barely beating average losses — giving winners more room",
            })

    if trail_out_rate > 0.35 and avg_win > 0:
        frm = next_cfg["trailingStopPct"]
        next_cfg["trailingStopPct"] = _step_val(frm, 1, "trailingStopPct")
        if next_cfg["trailingStopPct"] != frm:
            changes.append({
                "param": "Trailing stop %", "from": frm, "to": next_cfg["trailingStopPct"],
                "reason": f"{round(trail_out_rate * 100)}% of exits were trailing-stops — widening slightly to capture more of each move",
            })

    if win_rate > 70 and stop_out_rate < 0.15 and tp_out_rate > 0.5:
        frm = next_cfg["momentumPriceThreshold"]
        next_cfg["momentumPriceThreshold"] = _step_val(frm, -1, "momentumPriceThreshold")
        if next_cfg["momentumPriceThreshold"] != frm:
            changes.append({
                "param": "Momentum price %", "from": frm, "to": next_cfg["momentumPriceThreshold"],
                "reason": "Strategy is winning consistently and rarely stopped out — loosening entry slightly to catch more setups",
            })

    return {"changes": changes, "newConfig": next_cfg}
