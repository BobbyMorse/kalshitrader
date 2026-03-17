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
import re
import time
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
from models import (BucketMarket, BucketSumSignal, SingleLegSignal,
                    StructuralAnomaly, ThresholdMarket, ViolationSignal)
from paper_trader import PaperTrader
from scanner import (find_bucket_violations, find_ladder_mean_reversion,
                     find_structural_anomalies, find_violations,
                     group_bucket_markets, group_integer_threshold_markets,
                     group_threshold_markets)

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

_STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(__file__), "trader_state.json"))
_CONFIG_FILE = _STATE_FILE.replace("trader_state.json", "trader_config.json")

_config: Dict[str, Any] = {
    "min_gross_edge": 0.07,   # minimum gross_edge for actual violations
    "max_size": 500,
    "fee_rate": 0.07,
    "refresh_interval": 300,   # seconds between full REST refreshes
    "auto_trade": True,
    "paper_trading": True,
    "auto_trade_inverted": False,
    "_v": 4,
}

def _load_config() -> None:
    """Load persisted config, overriding defaults."""
    if not os.path.exists(_CONFIG_FILE):
        return
    try:
        with open(_CONFIG_FILE) as f:
            saved = json.load(f)
        for k, v in saved.items():
            if k in _config:
                _config[k] = v
        # Migration v3: auto_trade_inverted caused a stop-loss feedback loop on illiquid markets.
        # Migration v4: mean-reversion strategy replaced inverted-leg; disable auto-trading until
        # new signals are validated (wide-spread markets caused target < entry = guaranteed losses).
        if saved.get("_v", 1) < 4:
            _config["auto_trade_inverted"] = False
            _config["_v"] = 4
        print(f"[Config] Loaded from {_CONFIG_FILE}: {saved}")
    except Exception as e:
        print(f"[Config] Failed to load {_CONFIG_FILE}: {e}")

def _save_config() -> None:
    """Atomically persist current config."""
    try:
        tmp = _CONFIG_FILE + ".tmp"
        parent = os.path.dirname(tmp)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(_config, f, indent=2)
        os.replace(tmp, _CONFIG_FILE)
    except Exception as e:
        print(f"[Config] Failed to save {_CONFIG_FILE}: {e}")

_load_config()
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

_last_group_scan: Dict[str, float] = {}   # event_ticker → last scan timestamp
_SCAN_THROTTLE_S = 1.0                     # max 1 scan per group per second

_signals: List[ViolationSignal] = []
_near_misses: List[ViolationSignal] = []          # positive edge, below trade threshold
_bucket_signals: List[BucketSumSignal] = []
_bucket_near_misses: List[BucketSumSignal] = []   # positive edge, below trade threshold
_structural_anomalies: List[StructuralAnomaly] = []        # non-adjacent violations (gross_edge > 0)
_inverted_leg_signals: List[SingleLegSignal] = []          # single-leg price inversions (buy the cheap leg)
_structural_near_misses: List[StructuralAnomaly] = []     # non-adjacent near-misses (closest to arb)
_market_cache: Dict[str, dict] = {}               # ticker → raw market dict
_threshold_map: Dict[str, ThresholdMarket] = {}      # ticker → ThresholdMarket
_threshold_groups: Dict[str, List[str]] = {}         # event_ticker → [tickers]
_int_threshold_map: Dict[str, ThresholdMarket] = {}  # sports integer-suffix tickers
_int_threshold_groups: Dict[str, List[str]] = {}     # event_ticker → [tickers]
_bucket_map: Dict[str, BucketMarket] = {}            # ticker → BucketMarket
_bucket_groups: Dict[str, List[str]] = {}            # event_ticker → [tickers]
_pnl_history: List[dict] = []
_PNL_FILE = _STATE_FILE.replace("trader_state.json", "pnl_history.json")


def _load_pnl_history() -> None:
    if not os.path.exists(_PNL_FILE):
        return
    try:
        with open(_PNL_FILE) as f:
            data = json.load(f)
        _pnl_history.extend(data)
        print(f"[main] Loaded {len(data)} pnl_history points")
    except Exception as e:
        print(f"[main] Failed to load pnl_history: {e}")


def _save_pnl_history() -> None:
    try:
        tmp = _PNL_FILE + ".tmp"
        parent = os.path.dirname(tmp)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(_pnl_history[-500:], f)
        os.replace(tmp, _PNL_FILE)
    except Exception as e:
        print(f"[main] Failed to save pnl_history: {e}")


_load_pnl_history()


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
            "locked_pnl": round(_trader.locked_pnl, 4),
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
        "structural_near_misses": [s.to_dict() for s in _structural_near_misses[:20]],
        "inverted_legs": [s.to_dict() for s in _inverted_leg_signals],
        "positions": (
            [p.to_dict() for p in open_pos] +
            [p.to_dict() for p in closed_pos[-100:]] +
            [p.to_dict() for p in bucket_open] +
            [p.to_dict() for p in bucket_closed[-30:]] +
            [p.to_dict() for p in _trader.single_leg_open_positions] +
            [p.to_dict() for p in _trader.single_leg_closed_positions[-100:]]
        ),
        "trades": [t.to_dict() for t in _trader.all_trades[-500:]],
        "pnl_history": _pnl_history[-200:],
    }


# ── Orderbook depth enrichment ────────────────────────────────────────────────

async def _enrich_depths(violations: list, max_size: int) -> None:
    """Fetch real L2 orderbook depth for both legs of each violation and update:
      - lower_depth: contracts available at lower.yes_ask (consuming NO bids)
      - higher_depth: contracts available at higher.yes_bid (consuming YES bids)
      - avail_size: min(lower_depth, higher_depth) — true executable size

    Kalshi orderbook format:
      'no'  key = NO bids (highest first)  → these ARE the YES sellers (YES ask side)
      'yes' key = YES bids (highest first) → these ARE the NO sellers (NO ask side)
    """
    import asyncio as _asyncio

    async def _fetch_pair(v: ViolationSignal) -> None:
        lower_ob, higher_ob = await _asyncio.gather(
            _client.get_orderbook(v.lower.ticker),
            _client.get_orderbook(v.higher.ticker),
            return_exceptions=True,
        )
        if isinstance(lower_ob, Exception) or isinstance(higher_ob, Exception):
            return

        # Lower leg: we BUY YES at yes_ask.
        # YES ask at price P = NO bid at (100 - P). Depth = qty of NO bids at that price.
        lower_ask_c = round(v.lower.yes_ask * 100)
        no_target = 100 - lower_ask_c
        v.lower_depth = sum(
            int(qty) for price, qty in lower_ob.get("no", [])
            if int(price) == no_target
        )

        # Higher leg: we BUY NO at (1 - yes_bid).
        # NO ask at price Q = YES bid at (100 - Q). Depth = qty of YES bids at yes_bid.
        higher_bid_c = round(v.higher.yes_bid * 100)
        v.higher_depth = sum(
            int(qty) for price, qty in higher_ob.get("yes", [])
            if int(price) == higher_bid_c
        )

        if v.lower_depth > 0 and v.higher_depth > 0:
            v.avail_size = min(v.lower_depth, v.higher_depth, max_size)
        elif v.lower_depth > 0 or v.higher_depth > 0:
            # One side returned data but not the other; still cap conservatively
            v.avail_size = min(v.lower_depth or v.higher_depth, max_size)
        # else: orderbook returned empty (no resting orders at exact price); keep OI-based estimate

    await _asyncio.gather(*[_fetch_pair(v) for v in violations])


# ── Real-time tick handler ────────────────────────────────────────────────────


async def _on_tick(ticker: str, bid_cents: int, ask_cents: int) -> None:
    """Called by KalshiFeed on every live price update."""
    _state["ticks_received"] += 1
    # Broadcast tick count every 100 ticks (lightweight partial update)
    if _state["ticks_received"] % 100 == 0:
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
            # Throttle: skip scan if this group was scanned within the last second
            _now = time.monotonic()
            if _now - _last_group_scan.get(event_ticker, 0.0) >= _SCAN_THROTTLE_S:
                _last_group_scan[event_ticker] = _now
            else:
                tm = None  # skip scan but keep price update above

        if tm is not None and not (tm.yes_ask <= 0 or tm.yes_bid <= 0):
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
                    # Fetch real L2 depth at the target prices before trading/broadcasting.
                    await _enrich_depths(violations, _config["max_size"])
                    sig_index = {s.id: i for i, s in enumerate(_signals)}
                    for v in violations:
                        print(
                            f"[Feed] VIOLATION {v.id}: "
                            f"edge={v.gross_edge:.3f} net={v.net_edge:.3f} "
                            f"exp={v.expected_edge:.3f} mid_p={v.middle_prob:.2f} "
                            f"depth={v.lower_depth}/{v.higher_depth}"
                        )
                        if v.id in sig_index:
                            _signals[sig_index[v.id]] = v
                        else:
                            _signals.append(v)
                    _signals.sort(key=lambda x: x.expected_edge, reverse=True)
                    if _config["auto_trade"] and _config["paper_trading"]:
                        for v in violations:
                            if not _trader.is_positioned(v.id):
                                _trader.execute(v, strategy="threshold_arb")
                    broadcast_needed = True

                # ── Near-miss scan (real-time, same group, same throttle) ───────────
                _min_edge = _config["min_gross_edge"]
                all_close = find_violations(
                    {event_ticker: group_markets},
                    min_gross_edge=_min_edge - 0.05,
                    max_size=_config["max_size"],
                    fee_rate=_config["fee_rate"],
                    allow_negative_edge=True,
                    adjacent_only=True,
                    require_liquidity=False,
                )
                tick_near = [
                    v for v in all_close
                    if v.gross_edge < _min_edge
                    and not (v.lower.yes_ask >= 0.99 and v.higher.yes_bid >= 0.98)
                ]
                group_ticker_set = set(group_tickers)
                _near_misses[:] = [
                    v for v in _near_misses
                    if v.lower.ticker not in group_ticker_set
                    and v.higher.ticker not in group_ticker_set
                ]
                _near_misses.extend(tick_near)
                _near_misses.sort(key=lambda x: x.gross_edge, reverse=True)
                del _near_misses[30:]
                if tick_near:
                    broadcast_needed = True

                # ── Ladder mean-reversion check on every tick (detect only, NEVER auto-trade) ──
                # Mean-reversion is directional and must be reviewed manually.
                # Auto-trading on ticks causes dozens of positions per minute — never do this.
                inverted = find_ladder_mean_reversion(
                    {event_ticker: group_markets},
                    min_anomaly=0.05,
                    top_n=5,
                )
                if inverted:
                    inv_index = {s.id: i for i, s in enumerate(_inverted_leg_signals)}
                    for sig in inverted:
                        if sig.id in inv_index:
                            _inverted_leg_signals[inv_index[sig.id]] = sig
                        else:
                            _inverted_leg_signals.append(sig)
                            print(
                                f"[Feed] MEAN-REV {sig.id}: "
                                f"mid={sig.market.mid():.2f} interp={((sig.adj_lower.mid() + sig.adj_higher.mid()) / 2 if sig.adj_lower else sig.adj_higher.mid()):.2f} "
                                f"anomaly={sig.inversion:.2f}"
                            )
                    broadcast_needed = True

    # ── Bucket sum arb check ──────────────────────────────────────────────────
    bm = _bucket_map.get(ticker)
    if bm is not None:
        bm.yes_bid = bid_cents / 100.0
        bm.yes_ask = ask_cents / 100.0

        event_ticker = bm.event_ticker
        _now2 = time.monotonic()
        if _now2 - _last_group_scan.get("B:" + event_ticker, 0.0) < _SCAN_THROTTLE_S:
            bm = None  # throttled

    if bm is not None:
        event_ticker = bm.event_ticker
        _last_group_scan["B:" + event_ticker] = time.monotonic()
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

    # Update single-leg marks on every tick for the relevant ticker
    single_closed = _trader.update_single_leg_marks(_threshold_map, _int_threshold_map)
    if single_closed:
        broadcast_needed = True

    # Always update P&L when a tick arrives for a ticker in an open position.
    # Previously P&L only refreshed when a violation was detected — this made
    # P&L appear frozen between violations (up to 5 min lag).
    if not broadcast_needed and ticker in _trader.open_position_tickers:
        broadcast_needed = True

    if broadcast_needed:
        _trader.update_marks(_market_cache)
        _trader.update_marks_bucket(_market_cache)
        await _broadcast(_snapshot())

    # Yield to event loop so health checks and other coroutines can run
    await asyncio.sleep(0)


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
        print(f"[Markets] API returned {len(markets)} non-parlay open markets")

        groups = group_threshold_markets(markets)
        int_groups = group_integer_threshold_markets(markets)
        bucket_groups = group_bucket_markets(markets)
        _state["groups_found"] = len(groups) + len(int_groups) + len(bucket_groups)

        # markets_fetched = threshold + bucket market count (more meaningful than raw API count)
        _t_tickers = {tm.ticker for tms in groups.values() for tm in tms}
        _it_tickers = {tm.ticker for tms in int_groups.values() for tm in tms}
        _b_tickers = {bm.ticker for bms in bucket_groups.values() for bm in bms}
        _state["markets_fetched"] = len(_t_tickers | _it_tickers | _b_tickers)

        # Diagnostic: show which series are covered and which big series are missed
        covered = sorted({ev.split("-")[0] for ev in groups})
        print(f"[Markets] T-groups={len(groups)} / INT={len(int_groups)} / B={len(bucket_groups)} | "
              f"{_state['markets_fetched']} threshold/bucket markets | series: {covered}")
        # Show sample tickers from fetched markets that have NO threshold pattern (might be missing ladders)
        _T_PAT = re.compile(r"-T[\d.]+$", re.IGNORECASE)
        non_threshold_series = {}
        for m in markets:
            t = m.get("ticker", "")
            if not _T_PAT.search(t):
                s = t.split("-")[0].upper()
                non_threshold_series[s] = non_threshold_series.get(s, 0) + 1
        # Print series with many markets (potential missed ladders)
        big_missed = sorted(
            [(cnt, s) for s, cnt in non_threshold_series.items() if cnt >= 5],
            reverse=True
        )[:20]
        if big_missed:
            print(f"[Markets] Non-threshold series with 5+ markets (potential missed ladders): {big_missed}")

        # Only cache the ~700 threshold/bucket markets; drop the other 59k to save memory
        relevant = (
            {tm.ticker for tms in groups.values() for tm in tms}
            | {tm.ticker for tms in int_groups.values() for tm in tms}
            | {bm.ticker for bms in bucket_groups.values() for bm in bms}
        )
        _market_cache.clear()
        _market_cache.update({m["ticker"]: m for m in markets if m["ticker"] in relevant})
        del markets  # free 60k-item list before the rest of the refresh

        # Rebuild threshold map + groups, subscribe feed to new tickers.
        # Preserve live WS prices: new objects start at 0 from REST list;
        # carry over bid/ask from the old in-memory object so the scan uses
        # real prices rather than waiting for REST warmup.
        new_tickers: List[str] = []
        for event_ticker, tms in groups.items():
            _threshold_groups[event_ticker] = [tm.ticker for tm in tms]
            for tm in tms:
                old = _threshold_map.get(tm.ticker)
                if old is not None and old.yes_bid > 0:
                    tm.yes_bid = old.yes_bid
                    tm.yes_ask = old.yes_ask
                if tm.ticker not in _threshold_map:
                    new_tickers.append(tm.ticker)
                _threshold_map[tm.ticker] = tm

        # Rebuild integer-threshold (sports) map + groups
        for event_ticker, tms in int_groups.items():
            _int_threshold_groups[event_ticker] = [tm.ticker for tm in tms]
            for tm in tms:
                old = _int_threshold_map.get(tm.ticker)
                if old is not None and old.yes_bid > 0:
                    tm.yes_bid = old.yes_bid
                    tm.yes_ask = old.yes_ask
                if tm.ticker not in _int_threshold_map:
                    new_tickers.append(tm.ticker)
                _int_threshold_map[tm.ticker] = tm

        # Rebuild bucket map + groups, subscribe feed to new bucket tickers
        for event_ticker, bms in bucket_groups.items():
            _bucket_groups[event_ticker] = [bm.ticker for bm in bms]
            for bm in bms:
                old_bm = _bucket_map.get(bm.ticker)
                if old_bm is not None and old_bm.yes_bid > 0:
                    bm.yes_bid = old_bm.yes_bid
                    bm.yes_ask = old_bm.yes_ask
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
                oi = m.get("open_interest") or 0
                tm = _threshold_map.get(ticker)
                if tm is not None:
                    tm.yes_bid = yb
                    tm.yes_ask = ya
                    if oi:
                        tm.open_interest = int(oi)
                    updated += 1
                bm = _bucket_map.get(ticker)
                if bm is not None:
                    bm.yes_bid = yb
                    bm.yes_ask = ya
                    if oi:
                        bm.open_interest = int(oi)
                    updated += 1
                itm = _int_threshold_map.get(ticker)
                if itm is not None:
                    itm.yes_bid = yb
                    itm.yes_ask = ya
                    if oi:
                        itm.open_interest = int(oi)
                    updated += 1
            print(f"[Refresh] Prices updated for {updated}/{len(all_tickers)} tickers")

        # Full threshold violation scan (financial + sports integer markets)
        _fee = _config["fee_rate"]
        _min_edge = _config["min_gross_edge"]
        all_groups = {**groups, **int_groups}

        # Diagnostic: count groups with ≥2 priced markets
        _priced_groups = sum(
            1 for mlist in all_groups.values()
            if sum(1 for m in mlist if m.yes_ask > 0 and m.yes_bid > 0) >= 2
        )
        print(f"[Refresh] {len(all_groups)} T+INT groups, {_priced_groups} have ≥2 priced markets")
        violations = find_violations(
            all_groups,
            min_gross_edge=_min_edge,
            max_size=_config["max_size"],
            fee_rate=_fee,
            allow_negative_edge=True,  # paper: trade any genuine violation
        )
        # Near-miss scan: adjacent pairs within 5¢ of the violation threshold.
        # require_liquidity=False: display-only; OI=0 from bulk API would block everything.
        all_close = find_violations(
            all_groups,
            min_gross_edge=_min_edge - 0.05,
            max_size=_config["max_size"],
            fee_rate=_fee,
            allow_negative_edge=True,
            adjacent_only=True,
            require_liquidity=False,
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
        # Enrich true violations with real orderbook depth before trading/display.
        if violations:
            await _enrich_depths(violations, _config["max_size"])
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
            min_gross_edge=_min_edge - 0.05,
            max_size=_config["max_size"],
            fee_rate=_fee,
            allow_negative_edge=True,
            require_liquidity=False,
        )
        b_near_misses = sorted(
            [v for v in all_b_close if v.gross_edge < _min_edge],
            key=lambda x: x.gross_edge, reverse=True,
        )
        _bucket_signals.clear()
        _bucket_signals.extend(b_violations)
        _bucket_near_misses.clear()
        _bucket_near_misses.extend(b_near_misses[:10])

        # Structural anomaly scan: non-adjacent violations + near-misses for manual review
        structural = find_structural_anomalies(
            all_groups,
            max_size=_config["max_size"],
            fee_rate=_fee,
            min_gross_edge=0.001,  # genuine violations only (exclude exact 0¢ edge)
        )
        structural_near = find_structural_anomalies(
            all_groups,
            max_size=_config["max_size"],
            fee_rate=_fee,
            min_gross_edge=-0.05,  # within 5¢ of being a true non-adjacent arb
            top_n=30,
        )
        _structural_anomalies.clear()
        _structural_anomalies.extend(structural)
        _structural_near_misses.clear()
        _structural_near_misses.extend(
            [s for s in structural_near if s.gross_edge < 0.0][:20]
        )

        # Ladder mean-reversion scan: find rungs cheap relative to both neighbors
        inverted = find_ladder_mean_reversion(all_groups, min_anomaly=0.05, top_n=20)
        _inverted_leg_signals.clear()
        _inverted_leg_signals.extend(inverted)

        if inverted:
            print(f"[Refresh] {len(inverted)} mean-reversion signal(s) detected")
            for sig in inverted[:3]:
                interp = ((sig.adj_lower.mid() + sig.adj_higher.mid()) / 2
                          if sig.adj_lower else sig.adj_higher.mid())
                print(f"  [MEAN-REV] {sig.id}: mid={sig.market.mid():.2f} "
                      f"interp={interp:.2f} anomaly={sig.inversion:.2f}")

        if _config["auto_trade_inverted"] and _config["auto_trade"] and _config["paper_trading"]:
            inv_new = 0
            for sig in inverted:
                if not _trader.is_positioned(sig.id):
                    if _trader.execute_single_leg(sig):
                        inv_new += 1
            if inv_new:
                print(f"[Refresh] Opened {inv_new} mean-reversion positions")
            # Remove auto-traded signals from display list
            _inverted_leg_signals[:] = [s for s in _inverted_leg_signals if not _trader.is_positioned(s.id)]

        best_near = f" best={near_misses[0].gross_edge:.3f}" if near_misses else ""
        print(
            f"[Refresh] {_state['markets_fetched']} markets | "
            f"{len(groups)} T-groups / {len(int_groups)} INT-groups / {len(bucket_groups)} B-groups | "
            f"{len(violations)} T-viol / {len(b_violations)} B-viol | "
            f"{len(near_misses)} T-near{best_near} / {len(b_near_misses)} B-near | "
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
            for sig in structural:
                # Only auto-trade structural anomalies that meet the configured edge threshold.
                # The structural scan uses min_gross_edge=0.001 to display all genuine anomalies,
                # but we must not trade sub-threshold signals (e.g. 2¢ gross edge with 7¢ fees = -5¢ net).
                if sig.gross_edge < _min_edge:
                    continue
                if not _trader.is_positioned(sig.id):
                    if _trader.execute(sig, strategy="structural_arb"):
                        new_count += 1
            for sig in b_violations:
                if not _trader.is_positioned(sig.id):
                    if _trader.execute_bucket(sig):
                        new_count += 1
            if new_count:
                print(f"[Refresh] Opened {new_count} new paper positions")

        _strat = _trader.realized_pnl_by_strategy
        _pnl_history.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "realized": round(_trader.realized_pnl, 4),
            "unrealized": round(_trader.unrealized_pnl, 4),
            "total": round(_trader.realized_pnl + _trader.unrealized_pnl, 4),
            "open_positions": len(_trader.open_positions),
            "threshold": round(_strat.get("threshold_arb", 0.0), 4),
            "structural": round(_strat.get("structural_arb", 0.0), 4),
            "bucket": round(_strat.get("bucket_arb", 0.0), 4),
            "meanrev": round(_strat.get("mispriced_leg", 0.0), 4),
        })
        _save_pnl_history()

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
    # First refresh already ran at startup; sleep before the next one
    while _state["running"]:
        interval = int(_config.get("refresh_interval", 300))
        await asyncio.sleep(interval)
        if _state["running"]:
            await _refresh_markets()


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
        _state["running"] = True
        await _refresh_markets()
        await _start_feed()
        _state["scan_task"] = asyncio.create_task(_refresh_loop())
        # Delayed rescan: WS delivers initial price snapshots within ~30s;
        # re-run full scan once those prices are populated so near misses
        # and structural anomalies appear immediately on cold start.
        async def _warm_rescan():
            await asyncio.sleep(45)
            if _state["running"]:
                print("[main] Warm rescan: re-scanning with WS-populated prices…")
                await _refresh_markets()
        asyncio.create_task(_warm_rescan())
        print("[main] Bot auto-started.")
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
        _save_config()
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
    """Manually execute a structural anomaly or near-miss as a threshold arb trade."""
    # Search both actual violations and near-misses (user may trade either)
    anomaly = next(
        (a for a in _structural_anomalies + _structural_near_misses if a.id == signal_id),
        None,
    )
    if anomaly is None:
        raise HTTPException(status_code=404, detail=f"Structural signal not found: {signal_id!r}")
    try:
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
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Execution error: {exc}")
    if pos is None:
        raise HTTPException(status_code=409, detail="Already positioned or zero size")
    await _broadcast(_snapshot())
    return {"ok": True, "position_id": pos.id}


@app.post("/inverted/{ticker}/trade")
async def trade_inverted(ticker: str) -> dict:
    """Manually execute a single-leg inverted trade."""
    sig = next((s for s in _inverted_leg_signals if s.id == ticker), None)
    if sig is None:
        raise HTTPException(status_code=404, detail=f"Inverted signal not found: {ticker!r}")
    if _trader.is_positioned(ticker):
        raise HTTPException(status_code=409, detail="Already positioned")
    try:
        pos = _trader.execute_single_leg(sig)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Execution error: {exc}")
    if pos is None:
        raise HTTPException(status_code=409, detail="Already positioned or zero size")
    await _broadcast(_snapshot())
    return {"ok": True, "position_id": pos.id}


@app.post("/positions/{pos_id}/flatten")
async def flatten_position(pos_id: str) -> dict:
    # Try threshold/structural arb first, then single-leg
    pos = _trader.flatten(pos_id, _market_cache)
    if pos is None:
        pos = _trader.flatten_single_leg(pos_id)
    if pos is None:
        raise HTTPException(status_code=404, detail="Position not found or already closed")
    await _broadcast(_snapshot())
    return {"ok": True, "pnl": pos.realized_pnl}


@app.post("/inverted/flatten-all")
async def flatten_all_inverted() -> dict:
    """Flatten all open single-leg (mean-reversion/inverted) positions at once."""
    open_ids = [p.id for p in _trader.single_leg_open_positions]
    closed = 0
    total_pnl = 0.0
    for pos_id in open_ids:
        pos = _trader.flatten_single_leg(pos_id)
        if pos is not None:
            closed += 1
            total_pnl += pos.realized_pnl
    await _broadcast(_snapshot())
    return {"ok": True, "closed": closed, "total_pnl": round(total_pnl, 4)}


class ConfigUpdate(BaseModel):
    min_gross_edge: Optional[float] = None
    max_size: Optional[int] = None
    fee_rate: Optional[float] = None
    refresh_interval: Optional[int] = None
    auto_trade: Optional[bool] = None
    auto_trade_inverted: Optional[bool] = None


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
    if cfg.auto_trade_inverted is not None:
        _config["auto_trade_inverted"] = cfg.auto_trade_inverted
    _save_config()
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
