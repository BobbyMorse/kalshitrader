"""
Kalshi threshold monotonicity arbitrage bot — paper trading only.

Real-time strategy:
  1. Initial REST fetch seeds the market cache and builds threshold groups.
  2. Kalshi WebSocket feed delivers live ticker updates for all threshold markets.
  3. On each price update, violation detection runs immediately on the affected group.
  4. Violations → paper trade both legs simultaneously.

Fallback REST refresh every N minutes catches newly listed markets.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import get_settings
from kalshi_client import KalshiClient
from kalshi_feed import KalshiFeed
from models import BucketMarket, BucketSumSignal, StructuralAnomaly, ThresholdMarket, ViolationSignal
from paper_trader import PaperTrader
from scanner import (find_bucket_violations, find_structural_anomalies,
                     find_violations, group_bucket_markets,
                     group_integer_threshold_markets, group_threshold_markets)

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Kalshi Threshold Arb Bot")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Singletons ────────────────────────────────────────────────────────────────

settings = get_settings()

_client = KalshiClient(
    host=settings.kalshi_host,
    api_prefix=settings.kalshi_api_prefix,
    api_key_id=settings.kalshi_api_key_id,
    private_key_path=settings.kalshi_private_key_path,
    private_key_content=settings.kalshi_private_key,
    email=settings.kalshi_email,
    password=settings.kalshi_password,
)

_trader = PaperTrader()
_trader.load()

# ── Module-level state ────────────────────────────────────────────────────────
# All mutable state lives in dicts/lists so mutations never need `global`.

_config: Dict[str, Any] = {
    "min_gross_edge": 0.08,   # must exceed fee_rate (0.07) to guarantee net profit
    "max_size": 500,
    "fee_rate": 0.07,
    "refresh_interval": 300,   # seconds between full REST refreshes
    "auto_trade": True,
    "paper_trading": True,
}
_trader.max_size = _config["max_size"]

_state: Dict[str, Any] = {
    "running": False,
    "scanning": False,
    "last_scan": None,
    "scan_count": 0,
    "auth_method": "none",
    "markets_fetched": 0,
    "groups_found": 0,
    "feed_connected": False,
    "ticks_received": 0,
    "scan_task": None,
    "feed_task": None,
    "feed": None,
    "ws_clients": set(),
}

_signals: List[ViolationSignal] = []
_near_misses: List[ViolationSignal] = []          # positive edge, below trade threshold
_bucket_signals: List[BucketSumSignal] = []
_bucket_near_misses: List[BucketSumSignal] = []   # positive edge, below trade threshold
_structural_anomalies: List[StructuralAnomaly] = []  # non-adjacent violations for manual review
_market_cache: Dict[str, dict] = {}               # ticker → raw market dict
_threshold_map: Dict[str, ThresholdMarket] = {}      # ticker → ThresholdMarket
_threshold_groups: Dict[str, List[str]] = {}         # event_ticker → [tickers]
_int_threshold_map: Dict[str, ThresholdMarket] = {}  # sports integer-suffix tickers
_int_threshold_groups: Dict[str, List[str]] = {}     # event_ticker → [tickers]
_bucket_map: Dict[str, BucketMarket] = {}            # ticker → BucketMarket
_bucket_groups: Dict[str, List[str]] = {}            # event_ticker → [tickers]
_pnl_history: List[dict] = []


# ── WebSocket helpers ─────────────────────────────────────────────────────────


async def _broadcast(payload: dict) -> None:
    text = json.dumps(payload, default=str)
    dead = set()
    for ws in list(_state["ws_clients"]):
        try:
            await ws.send_text(text)
        except Exception:
            dead.add(ws)
    _state["ws_clients"].difference_update(dead)


def _snapshot() -> dict:
    realized = _trader.realized_pnl
    unrealized = _trader.unrealized_pnl
    open_pos = _trader.open_positions
    closed_pos = _trader.closed_positions

    closed_with_pnl = [p for p in closed_pos if p.realized_pnl != 0]
    wins = sum(1 for p in closed_with_pnl if p.realized_pnl > 0)
    win_rate = wins / len(closed_with_pnl) if closed_with_pnl else 0.0

    bucket_open = _trader.bucket_open_positions
    bucket_closed = _trader.bucket_closed_positions
    total_open = len(open_pos) + len(bucket_open)
    total_closed = len(closed_pos) + len(bucket_closed)

    return {
        "type": "snapshot",
        "bot_state": {
            "running": _state["running"],
            "scanning": _state["scanning"],
            "last_scan": _state["last_scan"],
            "scan_count": _state["scan_count"],
            "auth_method": _state["auth_method"],
            "markets_fetched": _state["markets_fetched"],
            "groups_found": _state["groups_found"],
            "feed_connected": _state["feed_connected"],
            "ticks_received": _state["ticks_received"],
            "realized_pnl": round(realized, 4),
            "unrealized_pnl": round(unrealized, 4),
            "total_pnl": round(realized + unrealized, 4),
            "open_positions": total_open,
            "closed_positions": total_closed,
            "total_trades": len(_trader.all_trades),
            "win_rate": round(win_rate, 3),
            "paper_trading": True,
        },
        "config": _config,
        "signals": [s.to_dict() for s in _signals],
        "near_misses": [s.to_dict() for s in sorted(
            _near_misses, key=lambda v: v.higher.yes_bid - v.lower.yes_ask, reverse=True
        )[:20]],
        "bucket_signals": [s.to_dict() for s in _bucket_signals],
        "bucket_near_misses": [s.to_dict() for s in _bucket_near_misses[:10]],
        "structural_anomalies": [s.to_dict() for s in _structural_anomalies[:20]],
        "positions": (
            [p.to_dict() for p in open_pos] +
            [p.to_dict() for p in closed_pos[-30:]] +
            [p.to_dict() for p in bucket_open] +
            [p.to_dict() for p in bucket_closed[-10:]]
        ),
        "trades": [t.to_dict() for t in _trader.all_trades[-100:]],
        "pnl_history": _pnl_history[-200:],
    }


# ── Real-time tick handler ────────────────────────────────────────────────────


async def _on_tick(ticker: str, bid_cents: int, ask_cents: int) -> None:
    """Called by KalshiFeed on every live price update."""
    _state["ticks_received"] += 1
    # Broadcast tick count every 500 ticks (lightweight partial update)
    if _state["ticks_received"] % 500 == 0:
        asyncio.create_task(_broadcast({"bot_state": {"ticks_received": _state["ticks_received"]}}))

    # Update raw cache
    if ticker in _market_cache:
        _market_cache[ticker]["yes_bid"] = bid_cents
        _market_cache[ticker]["yes_ask"] = ask_cents

    broadcast_needed = False

    # ── Threshold arb check (financial + sports integer) ─────────────────────
    tm = _threshold_map.get(ticker) or _int_threshold_map.get(ticker)
    _groups_ref = _threshold_groups if ticker in _threshold_map else _int_threshold_groups
    _map_ref = _threshold_map if ticker in _threshold_map else _int_threshold_map
    if tm is not None:
        tm.yes_bid = bid_cents / 100.0
        tm.yes_ask = ask_cents / 100.0

        if not (tm.yes_ask <= 0 or tm.yes_bid <= 0 or
                tm.yes_bid >= 0.99 or tm.yes_ask >= 0.99 or
                tm.yes_ask < tm.yes_bid):

            event_ticker = tm.event_ticker
            group_tickers = _groups_ref.get(event_ticker, [])
            group_markets = [
                _map_ref[t] for t in group_tickers
                if t in _map_ref
                and 0.01 <= _map_ref[t].yes_bid < 0.99
                and 0.01 <= _map_ref[t].yes_ask < 0.99
                and _map_ref[t].yes_ask - _map_ref[t].yes_bid <= 0.35  # wide spread = stale (sports markets have 16-18¢ spreads)
            ]
            if len(group_markets) >= 2:
                violations = find_violations(
                    {event_ticker: group_markets},
                    min_gross_edge=_config["min_gross_edge"],
                    max_size=_config["max_size"],
                    fee_rate=_config["fee_rate"],
                    allow_negative_edge=True,  # paper: trade any genuine violation, fees tracked separately
                )
                if violations:
                    sig_index = {s.id: i for i, s in enumerate(_signals)}
                    for v in violations:
                        print(
                            f"[Feed] VIOLATION {v.id}: "
                            f"edge={v.gross_edge:.3f} net={v.net_edge:.3f}"
                        )
                        if v.id in sig_index:
                            _signals[sig_index[v.id]] = v
                        else:
                            _signals.append(v)
                    _signals.sort(key=lambda x: x.gross_edge, reverse=True)
                    if _config["auto_trade"] and _config["paper_trading"]:
                        for v in violations:
                            if not _trader.is_positioned(v.id):
                                _trader.execute(v, strategy="threshold_arb")
                    broadcast_needed = True
                elif len(group_markets) >= 2:
                    # No violation yet — check near-miss for logging only
                    _min_edge = _config["min_gross_edge"]
                    near = find_violations(
                        {event_ticker: group_markets},
                        min_gross_edge=_min_edge - 0.15,
                        max_size=_config["max_size"],
                        fee_rate=_config["fee_rate"],
                        allow_negative_edge=True,
                        adjacent_only=True,
                    )
                    near_below = [v for v in near if v.gross_edge < _min_edge]
                    if near_below:
                        top = near_below[0]
                        print(
                            f"[NEAR-TICK] {top.id}: edge={top.gross_edge:.3f} "
                            f"(gap={_min_edge - top.gross_edge:.3f})"
                        )

    # ── Bucket sum arb check ──────────────────────────────────────────────────
    bm = _bucket_map.get(ticker)
    if bm is not None:
        bm.yes_bid = bid_cents / 100.0
        bm.yes_ask = ask_cents / 100.0

        event_ticker = bm.event_ticker
        bucket_tickers = _bucket_groups.get(event_ticker, [])
        bucket_markets = [
            _bucket_map[t] for t in bucket_tickers
            if t in _bucket_map
            and _bucket_map[t].yes_ask > 0  # must have an ask to be buyable
        ]
        if len(bucket_markets) >= 3:
            b_violations = find_bucket_violations(
                {event_ticker: bucket_markets},
                min_gross_edge=_config["min_gross_edge"],
                max_size=_config["max_size"],
                fee_rate=_config["fee_rate"],
                allow_negative_edge=True,
            )
            if b_violations:
                for v in b_violations:
                    print(
                        f"[Feed] BUCKET ARB {v.id}: "
                        f"sum_asks={v.sum_asks:.3f} edge={v.gross_edge:.3f}"
                    )
                b_index = {s.id: i for i, s in enumerate(_bucket_signals)}
                for v in b_violations:
                    if v.id in b_index:
                        _bucket_signals[b_index[v.id]] = v
                    else:
                        _bucket_signals.append(v)
                _bucket_signals.sort(key=lambda x: x.gross_edge, reverse=True)
                if _config["auto_trade"] and _config["paper_trading"]:
                    for v in b_violations:
                        if not _trader.is_positioned(v.id):
                            _trader.execute_bucket(v)
                broadcast_needed = True

    if broadcast_needed:
        _trader.update_marks(_market_cache)
        _trader.update_marks_bucket(_market_cache)
        await _broadcast(_snapshot())


# ── Full REST refresh ─────────────────────────────────────────────────────────


async def _refresh_markets() -> None:
    """
    Full REST fetch: refreshes prices, discovers new threshold markets,
    subscribes the feed to new tickers, runs a full violation scan.
    """
    _state["scanning"] = True
    await _broadcast({"type": "scan_start"})

    try:
        markets = await _client.get_markets(status="open")
        _market_cache.clear()
        _market_cache.update({m["ticker"]: m for m in markets})
        _state["markets_fetched"] = len(markets)

        groups = group_threshold_markets(markets)
        int_groups = group_integer_threshold_markets(markets)
        bucket_groups = group_bucket_markets(markets)
        _state["groups_found"] = len(groups) + len(int_groups) + len(bucket_groups)

        # Rebuild threshold map + groups, subscribe feed to new tickers
        new_tickers: List[str] = []
        for event_ticker, tms in groups.items():
            _threshold_groups[event_ticker] = [tm.ticker for tm in tms]
            for tm in tms:
                if tm.ticker not in _threshold_map:
                    new_tickers.append(tm.ticker)
                _threshold_map[tm.ticker] = tm

        # Rebuild integer-threshold (sports) map + groups
        for event_ticker, tms in int_groups.items():
            _int_threshold_groups[event_ticker] = [tm.ticker for tm in tms]
            for tm in tms:
                if tm.ticker not in _int_threshold_map:
                    new_tickers.append(tm.ticker)
                _int_threshold_map[tm.ticker] = tm

        # Rebuild bucket map + groups, subscribe feed to new bucket tickers
        for event_ticker, bms in bucket_groups.items():
            _bucket_groups[event_ticker] = [bm.ticker for bm in bms]
            for bm in bms:
                if bm.ticker not in _bucket_map:
                    new_tickers.append(bm.ticker)
                _bucket_map[bm.ticker] = bm

        feed: Optional[KalshiFeed] = _state.get("feed")
        if feed is not None and new_tickers:
            await feed.subscribe(new_tickers)
            print(f"[Refresh] +{len(new_tickers)} new tickers subscribed")

        # ── Price warmup: fetch real bid/ask for all threshold/bucket tickers ──
        # The REST market list omits bid/ask; individual market endpoints have it.
        all_tickers = list(_threshold_map.keys()) + list(_int_threshold_map.keys()) + list(_bucket_map.keys())
        if all_tickers:
            print(f"[Refresh] Fetching prices for {len(all_tickers)} tickers...")
            price_data = await _client.get_market_prices_bulk(all_tickers, concurrency=40)
            updated = 0
            for ticker, m in price_data.items():
                # REST API returns yes_bid_dollars/yes_ask_dollars as string decimals (0-1)
                yb_str = m.get("yes_bid_dollars") or m.get("yes_bid")
                ya_str = m.get("yes_ask_dollars") or m.get("yes_ask")
                if yb_str is None or ya_str is None:
                    continue
                yb = float(yb_str) if isinstance(yb_str, str) else yb_str / 100.0
                ya = float(ya_str) if isinstance(ya_str, str) else ya_str / 100.0
                _market_cache[ticker] = {**_market_cache.get(ticker, {}), **m}
                tm = _threshold_map.get(ticker)
                if tm is not None:
                    tm.yes_bid = yb
                    tm.yes_ask = ya
                    updated += 1
                bm = _bucket_map.get(ticker)
                if bm is not None:
                    bm.yes_bid = yb
                    bm.yes_ask = ya
                    updated += 1
                itm = _int_threshold_map.get(ticker)
                if itm is not None:
                    itm.yes_bid = yb
                    itm.yes_ask = ya
                    updated += 1
            print(f"[Refresh] Prices updated for {updated}/{len(all_tickers)} tickers")

        # Full threshold violation scan (financial + sports integer markets)
        _fee = _config["fee_rate"]
        _min_edge = _config["min_gross_edge"]
        all_groups = {**groups, **int_groups}
        violations = find_violations(
            all_groups,
            min_gross_edge=_min_edge,
            max_size=_config["max_size"],
            fee_rate=_fee,
            allow_negative_edge=True,  # paper: trade any genuine violation
        )
        # Near-miss scan: adjacent pairs only to avoid duplicates
        _near_miss_floor = _min_edge - 0.15  # show pairs within 15 cents of threshold
        all_close = find_violations(
            all_groups,
            min_gross_edge=_near_miss_floor,
            max_size=_config["max_size"],
            fee_rate=_fee,
            allow_negative_edge=True,
            adjacent_only=True,
        )
        raw_near_misses = sorted(
            [v for v in all_close
             if v.gross_edge < _min_edge
             # Skip deep-ITM pairs locked at price ceiling (no chance of becoming a violation)
             and not (v.lower.yes_ask >= 0.99 and v.higher.yes_bid >= 0.98)],
            key=lambda x: x.gross_edge, reverse=True,
        )
        # Cap at 3 per series so one liquid series doesn't drown others
        from collections import defaultdict as _dd
        _series_cnt: dict = _dd(int)
        near_misses = []
        for v in raw_near_misses:
            if _series_cnt[v.series] < 3:
                near_misses.append(v)
                _series_cnt[v.series] += 1
            if len(near_misses) >= 30:
                break
        _signals.clear()
        _signals.extend(violations)
        _near_misses.clear()
        _near_misses.extend(near_misses[:30])

        # Full bucket sum arb scan
        b_violations = find_bucket_violations(
            bucket_groups,
            min_gross_edge=_min_edge,
            max_size=_config["max_size"],
            fee_rate=_fee,
            allow_negative_edge=True,  # paper: trade any genuine violation
        )
        all_b_close = find_bucket_violations(
            bucket_groups,
            min_gross_edge=_min_edge - 0.30,
            max_size=_config["max_size"],
            fee_rate=0,
            allow_negative_edge=True,
        )
        b_near_misses = sorted(
            [v for v in all_b_close if v.gross_edge < _min_edge],
            key=lambda x: x.gross_edge, reverse=True,
        )
        _bucket_signals.clear()
        _bucket_signals.extend(b_violations)
        _bucket_near_misses.clear()
        _bucket_near_misses.extend(b_near_misses[:10])

        # Structural anomaly scan: non-adjacent violations for manual review
        structural = find_structural_anomalies(
            all_groups,
            max_size=_config["max_size"],
            fee_rate=_fee,
        )
        _structural_anomalies.clear()
        _structural_anomalies.extend(structural)

        print(
            f"[Refresh] {len(markets)} markets | "
            f"{len(groups)} T-groups / {len(int_groups)} INT-groups / {len(bucket_groups)} B-groups | "
            f"{len(violations)} T-violations / {len(b_violations)} B-violations | "
            f"{len(near_misses)} T-near / {len(b_near_misses)} B-near | "
            f"{len(structural)} structural | "
            f"{len(_threshold_map)} T-tickers / {len(_int_threshold_map)} INT-tickers / {len(_bucket_map)} B-tickers"
        )
        if near_misses:
            for v in near_misses[:3]:
                gap = _min_edge - v.gross_edge
                print(f"  [NEAR] {v.id}: edge={v.gross_edge:.3f} (gap={gap:.3f})")
        if b_near_misses:
            for v in b_near_misses[:2]:
                print(f"  [NEAR-B] {v.id}: sum_asks={v.sum_asks:.3f} edge={v.gross_edge:.3f}")

        _trader.update_marks(_market_cache)
        _trader.update_marks_bucket(_market_cache)

        if _config["auto_trade"] and _config["paper_trading"]:
            new_count = 0
            for sig in violations:
                if not _trader.is_positioned(sig.id):
                    if _trader.execute(sig, strategy="threshold_arb"):
                        new_count += 1
            for sig in b_violations:
                if not _trader.is_positioned(sig.id):
                    if _trader.execute_bucket(sig):
                        new_count += 1
            if new_count:
                print(f"[Refresh] Opened {new_count} new paper positions")

        _pnl_history.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "realized": round(_trader.realized_pnl, 4),
            "unrealized": round(_trader.unrealized_pnl, 4),
            "total": round(_trader.realized_pnl + _trader.unrealized_pnl, 4),
            "open_positions": len(_trader.open_positions),
        })

        _state["last_scan"] = datetime.now(timezone.utc).isoformat()
        _state["scan_count"] += 1

    except Exception as exc:
        import traceback
        print(f"[Refresh] Error: {exc}")
        traceback.print_exc()
    finally:
        _state["scanning"] = False

    await _broadcast(_snapshot())


async def _refresh_loop() -> None:
    while _state["running"]:
        await _refresh_markets()
        interval = int(_config.get("refresh_interval", 300))
        await asyncio.sleep(interval)


# ── Feed lifecycle ────────────────────────────────────────────────────────────


async def _start_feed() -> None:
    """Create the KalshiFeed and start it as a background task."""
    feed = KalshiFeed(
        ws_host=_client.get_ws_host(),
        get_headers=lambda: _client.get_ws_headers(),
        on_tick=_on_tick,
    )
    _state["feed"] = feed

    # Queue all known threshold + bucket tickers immediately
    tickers = list(_threshold_map.keys()) + list(_int_threshold_map.keys()) + list(_bucket_map.keys())
    if tickers:
        await feed.subscribe(tickers)

    async def _run_feed():
        try:
            _state["feed_connected"] = True
            await feed.start()
        finally:
            _state["feed_connected"] = False

    _state["feed_task"] = asyncio.create_task(_run_feed())
    print(f"[main] Feed task started, {len(tickers)} tickers queued ({len(_threshold_map)} T + {len(_int_threshold_map)} INT + {len(_bucket_map)} B)")


# ── FastAPI lifecycle ─────────────────────────────────────────────────────────


@app.on_event("startup")
async def startup() -> None:
    ok, method = await _client.login()
    _state["auth_method"] = method
    print(f"[main] Kalshi auth: {method} (ok={ok})")

    if ok:
        print("[main] Seeding market cache…")
        await _refresh_markets()
        await _start_feed()
    else:
        print("[main] Auth failed — real-time feed not started.")


# ── WebSocket endpoint ────────────────────────────────────────────────────────


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _state["ws_clients"].add(ws)
    try:
        await ws.send_text(json.dumps(_snapshot(), default=str))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            await _handle_ws_msg(msg)
    except WebSocketDisconnect:
        pass
    finally:
        _state["ws_clients"].discard(ws)


async def _handle_ws_msg(msg: dict) -> None:
    t = msg.get("type")

    if t == "start":
        if not _state["running"]:
            _state["running"] = True
            _state["scan_task"] = asyncio.create_task(_refresh_loop())
            if _state.get("feed") is None:
                await _start_feed()
        await _broadcast({"type": "status", "running": True})

    elif t == "stop":
        _state["running"] = False
        task = _state.get("scan_task")
        if task and not task.done():
            task.cancel()
        await _broadcast({"type": "status", "running": False})

    elif t == "scan":
        asyncio.create_task(_refresh_markets())

    elif t == "config":
        for key, val in msg.get("config", {}).items():
            if key in _config:
                _config[key] = val
        await _broadcast({"type": "config_update", "config": _config})

    elif t == "ping":
        pass


# ── REST endpoints ────────────────────────────────────────────────────────────


_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_STATIC_INDEX = os.path.join(_STATIC_DIR, "index.html")


@app.get("/")
def root():
    if os.path.isfile(_STATIC_INDEX):
        return FileResponse(_STATIC_INDEX)
    return {"status": "ok", "service": "kalshi-arb-bot"}


@app.get("/status")
def get_status() -> dict:
    return _snapshot()


@app.post("/bot/start")
async def bot_start() -> dict:
    if not _state["running"]:
        _state["running"] = True
        _state["scan_task"] = asyncio.create_task(_refresh_loop())
        if _state.get("feed") is None:
            await _start_feed()
    return {"running": True}


@app.post("/bot/stop")
async def bot_stop() -> dict:
    _state["running"] = False
    task = _state.get("scan_task")
    if task and not task.done():
        task.cancel()
    return {"running": False}


@app.post("/bot/scan")
async def bot_scan() -> dict:
    asyncio.create_task(_refresh_markets())
    return {"ok": True}


@app.post("/bot/reset")
async def bot_reset() -> dict:
    _trader.reset()
    _pnl_history.clear()
    _signals.clear()
    await _broadcast(_snapshot())
    return {"ok": True}


@app.post("/structural/{signal_id}/trade")
async def trade_structural(signal_id: str) -> dict:
    """Manually execute a structural anomaly as a threshold arb trade."""
    anomaly = next((a for a in _structural_anomalies if a.id == signal_id), None)
    if anomaly is None:
        raise HTTPException(status_code=404, detail="Structural anomaly not found")
    # Convert to a ViolationSignal so the paper trader can execute it
    from datetime import datetime, timezone
    sig = ViolationSignal(
        id=anomaly.id,
        series=anomaly.series,
        expiry_dt=anomaly.expiry_dt,
        lower=anomaly.lower,
        higher=anomaly.higher,
        gross_edge=anomaly.gross_edge,
        net_edge=anomaly.net_edge,
        entry_cost=anomaly.entry_cost,
        avail_size=anomaly.avail_size,
        detected_at=anomaly.detected_at,
    )
    pos = _trader.execute(sig, strategy="structural_arb")
    if pos is None:
        raise HTTPException(status_code=409, detail="Already positioned or zero size")
    await _broadcast(_snapshot())
    return {"ok": True, "position_id": pos.id}


@app.post("/positions/{pos_id}/flatten")
async def flatten_position(pos_id: str) -> dict:
    pos = _trader.flatten(pos_id, _market_cache)
    if pos is None:
        raise HTTPException(status_code=404, detail="Position not found or already closed")
    await _broadcast(_snapshot())
    return {"ok": True, "pnl": pos.realized_pnl}


class ConfigUpdate(BaseModel):
    min_gross_edge: Optional[float] = None
    max_size: Optional[int] = None
    fee_rate: Optional[float] = None
    refresh_interval: Optional[int] = None
    auto_trade: Optional[bool] = None


@app.get("/debug/groups")
def debug_groups() -> dict:
    """Show all threshold groups and market counts — useful for coverage check."""
    groups = {}
    for event_ticker, tickers in _threshold_groups.items():
        markets = [_threshold_map[t] for t in tickers if t in _threshold_map]
        tradeable = [
            m for m in markets
            if 0.01 < m.yes_bid <= 0.92 and 0.04 <= m.yes_ask < 0.99
        ]
        groups[event_ticker] = {
            "total": len(tickers),
            "tradeable": len(tradeable),
            "thresholds": sorted([m.threshold for m in markets]),
            "series": markets[0].series if markets else "",
        }
    return {
        "group_count": len(groups),
        "ticker_count": len(_threshold_map),
        "groups": dict(sorted(groups.items(), key=lambda x: x[1]["series"])),
    }


@app.post("/config")
async def update_config(cfg: ConfigUpdate) -> dict:
    if cfg.min_gross_edge is not None:
        _config["min_gross_edge"] = cfg.min_gross_edge
    if cfg.max_size is not None:
        _config["max_size"] = cfg.max_size
        _trader.max_size = cfg.max_size
    if cfg.fee_rate is not None:
        _config["fee_rate"] = cfg.fee_rate
    if cfg.refresh_interval is not None:
        _config["refresh_interval"] = cfg.refresh_interval
    if cfg.auto_trade is not None:
        _config["auto_trade"] = cfg.auto_trade
    await _broadcast({"type": "config_update", "config": _config})
    return _config


# ── Static files (built React app) ────────────────────────────────────────────
# Must be mounted AFTER all API routes so API paths take priority.

if os.path.isdir(_STATIC_DIR):
    _assets_dir = os.path.join(_STATIC_DIR, "assets")
    if os.path.isdir(_assets_dir):
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        return FileResponse(_STATIC_INDEX)
