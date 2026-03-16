"""
Paper trading engine for threshold monotonicity arbitrage.

Entry:  simultaneous paper-fill of both legs at current ask prices.
Exit:   at expiry (settlement) or manual flatten.
PnL:    mark-to-market on each scan; realized on close.

One-leg protection:
- Both legs are filled simultaneously, so no true one-leg exposure in paper mode.
- If a leg's current exit price drops to near zero, a warning is logged and
  the position is flagged so the UI can surface it.
"""
from __future__ import annotations

import dataclasses
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional


def _mkt_bid(m: dict) -> float:
    """Extract yes_bid as 0-1 float from a raw market cache dict (handles both formats)."""
    yb = m.get("yes_bid")
    if yb is not None:
        return yb / 100.0 if yb > 1 else float(yb)
    yb_d = m.get("yes_bid_dollars")
    return float(yb_d) if yb_d is not None else 0.0


def _mkt_ask(m: dict) -> float:
    """Extract yes_ask as 0-1 float from a raw market cache dict (handles both formats)."""
    ya = m.get("yes_ask")
    if ya is not None:
        return ya / 100.0 if ya > 1 else float(ya)
    ya_d = m.get("yes_ask_dollars")
    return float(ya_d) if ya_d is not None else 0.0

from models import (BucketPosition, BucketSumSignal, Position, SingleLegPosition,
                    SingleLegSignal, TradeRecord, ViolationSignal)


# ── Persistence helpers ───────────────────────────────────────────────────────

class _DTEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


def _dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


def _load_position(d: dict) -> Position:
    return Position(
        id=d["id"], signal_id=d["signal_id"], series=d["series"],
        expiry_dt=datetime.fromisoformat(d["expiry_dt"]),
        lower_ticker=d["lower_ticker"], higher_ticker=d["higher_ticker"],
        lower_threshold=d["lower_threshold"], higher_threshold=d["higher_threshold"],
        size=d["size"], lower_entry=d["lower_entry"], higher_entry=d["higher_entry"],
        entry_cost=d["entry_cost"], entry_time=datetime.fromisoformat(d["entry_time"]),
        gross_edge=d["gross_edge"], net_edge=d["net_edge"],
        status=d.get("status", "open"),
        strategy=d.get("strategy", "threshold_arb"),
        lower_mid=d.get("lower_mid", 0.0), higher_no_mid=d.get("higher_no_mid", 0.0),
        unrealized_pnl=d.get("unrealized_pnl", 0.0),
        realized_pnl=d.get("realized_pnl", 0.0), fees_paid=d.get("fees_paid", 0.0),
        exit_time=_dt(d.get("exit_time")), exit_reason=d.get("exit_reason", ""),
    )


def _load_bucket_position(d: dict) -> BucketPosition:
    return BucketPosition(
        id=d["id"], signal_id=d["signal_id"], series=d["series"],
        expiry_dt=datetime.fromisoformat(d["expiry_dt"]),
        event_ticker=d["event_ticker"],
        bucket_tickers=d["bucket_tickers"], bucket_entries=d["bucket_entries"],
        size=d["size"], entry_cost=d["entry_cost"],
        gross_edge=d["gross_edge"], net_edge=d["net_edge"],
        entry_time=datetime.fromisoformat(d["entry_time"]),
        status=d.get("status", "open"),
        unrealized_pnl=d.get("unrealized_pnl", 0.0),
        realized_pnl=d.get("realized_pnl", 0.0), fees_paid=d.get("fees_paid", 0.0),
        exit_time=_dt(d.get("exit_time")), exit_reason=d.get("exit_reason", ""),
    )


def _load_single_leg_position(d: dict) -> SingleLegPosition:
    return SingleLegPosition(
        id=d["id"], signal_id=d["signal_id"], series=d["series"],
        expiry_dt=datetime.fromisoformat(d["expiry_dt"]),
        ticker=d["ticker"], threshold=d["threshold"], adj_ticker=d["adj_ticker"],
        size=d["size"], entry_price=d["entry_price"], target_bid=d["target_bid"],
        entry_time=datetime.fromisoformat(d["entry_time"]),
        status=d.get("status", "open"), strategy=d.get("strategy", "mispriced_leg"),
        current_bid=d.get("current_bid", 0.0),
        unrealized_pnl=d.get("unrealized_pnl", 0.0),
        realized_pnl=d.get("realized_pnl", 0.0),
        exit_price=d.get("exit_price", 0.0),
        exit_time=_dt(d.get("exit_time")), exit_reason=d.get("exit_reason", ""),
    )


def _load_trade(d: dict) -> TradeRecord:
    return TradeRecord(
        id=d["id"], position_id=d["position_id"],
        timestamp=datetime.fromisoformat(d["timestamp"]),
        action=d["action"], series=d["series"],
        lower_ticker=d["lower_ticker"], higher_ticker=d["higher_ticker"],
        lower_threshold=d["lower_threshold"], higher_threshold=d["higher_threshold"],
        size=d["size"], lower_entry=d["lower_entry"], higher_entry=d["higher_entry"],
        gross_edge=d["gross_edge"], net_edge=d["net_edge"],
        pnl=d.get("pnl"), fees=d.get("fees", 0.0), status=d["status"],
        strategy=d.get("strategy", "threshold_arb"),
    )

KALSHI_FEE_RATE = 0.07
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "trader_state.json"))


class PaperTrader:
    def __init__(self, max_size: int = 10, fee_rate: float = KALSHI_FEE_RATE):
        self.max_size = max_size
        self.fee_rate = fee_rate

        self._open: Dict[str, Position] = {}            # pos_id → Position
        self._closed: List[Position] = []
        self._trades: List[TradeRecord] = []
        self._positioned: Dict[str, str] = {}           # signal_id → pos_id

        # Bucket sum arb positions
        self._bucket_open: Dict[str, BucketPosition] = {}
        self._bucket_closed: List[BucketPosition] = []
        self._bucket_positioned: Dict[str, str] = {}   # signal_id → pos_id

        # Single-leg mispriced market positions
        self._single_open: Dict[str, SingleLegPosition] = {}   # pos_id → pos
        self._single_closed: List[SingleLegPosition] = []
        self._single_positioned: Dict[str, str] = {}           # signal_id (ticker) → pos_id

    # ── Read-only properties ──────────────────────────────────────────────────

    @property
    def open_positions(self) -> List[Position]:
        return list(self._open.values())

    @property
    def open_position_tickers(self) -> set:
        """Set of all tickers that are legs of open positions (threshold + bucket + single-leg).
        Used to trigger real-time P&L updates on every tick for those markets."""
        tickers: set = set()
        for p in self._open.values():
            tickers.add(p.lower_ticker)
            tickers.add(p.higher_ticker)
        for p in self._bucket_open.values():
            tickers.update(p.bucket_tickers)
        for p in self._single_open.values():
            tickers.add(p.ticker)
        return tickers

    @property
    def closed_positions(self) -> List[Position]:
        return self._closed

    @property
    def all_trades(self) -> List[TradeRecord]:
        return self._trades

    @property
    def realized_pnl(self) -> float:
        return (sum(p.realized_pnl for p in self._closed) +
                sum(p.realized_pnl for p in self._bucket_closed) +
                sum(p.realized_pnl for p in self._single_closed))

    @property
    def unrealized_pnl(self) -> float:
        # For threshold/structural arb: locked expiry P&L = net_edge × size (fees included, worst-case)
        # For bucket arb: mid-based is meaningful (exhaustive set, one pays $1)
        # For single-leg: mark-to-bid (directional)
        threshold_locked = sum(p.net_edge * p.size for p in self._open.values())
        bucket_mid = sum(p.unrealized_pnl for p in self._bucket_open.values())
        single_mid = sum(p.unrealized_pnl for p in self._single_open.values())
        return threshold_locked + bucket_mid + single_mid

    @property
    def single_leg_open_positions(self) -> List[SingleLegPosition]:
        return list(self._single_open.values())

    @property
    def single_leg_closed_positions(self) -> List[SingleLegPosition]:
        return self._single_closed

    @property
    def bucket_open_positions(self) -> List[BucketPosition]:
        return list(self._bucket_open.values())

    @property
    def bucket_closed_positions(self) -> List[BucketPosition]:
        return self._bucket_closed

    def is_positioned(self, signal_id: str) -> bool:
        return (signal_id in self._positioned or
                signal_id in self._bucket_positioned or
                signal_id in self._single_positioned)

    # ── Entry ─────────────────────────────────────────────────────────────────

    def execute(self, signal: ViolationSignal, strategy: str = "threshold_arb") -> Optional[Position]:
        """Paper-fill both legs at current ask prices. Returns the new Position."""
        if self.is_positioned(signal.id):
            return None

        size = min(signal.avail_size, self.max_size)
        if size <= 0:
            return None

        pos_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc)

        pos = Position(
            id=pos_id,
            signal_id=signal.id,
            series=signal.series,
            expiry_dt=signal.expiry_dt,
            lower_ticker=signal.lower.ticker,
            higher_ticker=signal.higher.ticker,
            lower_threshold=signal.lower.threshold,
            higher_threshold=signal.higher.threshold,
            size=size,
            lower_entry=signal.lower.yes_ask,           # pay YES ask at lower
            higher_entry=1.0 - signal.higher.yes_bid,   # pay NO = 1 - bid at higher
            entry_cost=signal.entry_cost,
            entry_time=now,
            gross_edge=signal.gross_edge,
            net_edge=signal.net_edge,
            status="open",
            strategy=strategy,
            lower_mid=signal.lower.yes_bid,               # sell YES at bid
            higher_no_mid=1.0 - signal.higher.yes_ask,    # exit NO = buy YES at ask
        )
        # Initial unrealized mark (exit P&L at entry prices)
        pos.unrealized_pnl = (pos.lower_mid + pos.higher_no_mid - pos.entry_cost) * size

        self._open[pos_id] = pos
        self._positioned[signal.id] = pos_id

        self._trades.append(TradeRecord(
            id=str(uuid.uuid4())[:8],
            position_id=pos_id,
            timestamp=now,
            action="OPEN",
            series=signal.series,
            lower_ticker=signal.lower.ticker,
            higher_ticker=signal.higher.ticker,
            lower_threshold=signal.lower.threshold,
            higher_threshold=signal.higher.threshold,
            size=size,
            lower_entry=signal.lower.yes_ask,
            higher_entry=1.0 - signal.higher.yes_bid,
            gross_edge=signal.gross_edge,
            net_edge=signal.net_edge,
            pnl=None,
            fees=0.0,
            status="paper_filled",
            strategy=strategy,
        ))

        print(
            f"[PaperTrader] OPEN {pos_id} [{strategy}]: "
            f"{signal.lower.ticker} YES@{signal.lower.yes_ask:.2f} + "
            f"{signal.higher.ticker} NO@{1-signal.higher.yes_bid:.2f} | "
            f"edge={signal.gross_edge:.3f} size={size}"
        )
        self.save()
        return pos

    # ── Mark-to-market ────────────────────────────────────────────────────────

    def update_marks(self, market_map: Dict[str, dict]) -> None:
        """
        Refresh unrealized PnL for all open positions.
        Settle any that have passed their expiry.
        market_map: ticker → raw Kalshi market dict.
        """
        now = datetime.now(timezone.utc)
        to_settle = []
        to_auto_flatten = []  # structural arb positions where mid P&L turned positive

        for pos in list(self._open.values()):
            if pos.expiry_dt <= now:
                to_settle.append(pos.id)
                continue

            lower_m = market_map.get(pos.lower_ticker)
            higher_m = market_map.get(pos.higher_ticker)
            if lower_m is None or higher_m is None:
                continue

            lower_bid = _mkt_bid(lower_m)
            lower_ask = _mkt_ask(lower_m)
            higher_bid = _mkt_bid(higher_m)
            higher_ask = _mkt_ask(higher_m)

            # Exit P&L: use actual liquidation prices, not mids.
            # Sell YES at lower leg's bid; exit NO at lower leg by buying YES at ask
            # → NO worth = 1 - yes_ask(higher). This is what a flatten would actually net.
            pos.lower_mid = lower_bid              # sell YES at bid
            pos.higher_no_mid = 1.0 - higher_ask  # exit NO = buy YES at ask
            pos.unrealized_pnl = (pos.lower_mid + pos.higher_no_mid - pos.entry_cost) * pos.size

            # One-leg exposure warnings
            if lower_bid <= 0.02:
                print(f"[PaperTrader] WARNING {pos.id}: lower leg near zero (bid={lower_bid:.2f})")
                pos.status = "one_leg_risk"
            elif higher_bid >= 0.98:
                print(f"[PaperTrader] WARNING {pos.id}: higher leg near settled (bid={higher_bid:.2f})")
                pos.status = "one_leg_risk"
            else:
                pos.status = "open"

            # Auto-exit structural arb when mid P&L goes positive:
            # the mispricing has corrected — capture profit now rather than hold to expiry
            if pos.strategy == "structural_arb" and pos.unrealized_pnl > 0:
                to_auto_flatten.append(pos.id)
                print(f"[PaperTrader] AUTO-FLATTEN structural {pos.id}: "
                      f"mid P&L=${pos.unrealized_pnl:.2f} > 0, locking profit")

        for pos_id in to_settle:
            self._settle_expired(pos_id)
        for pos_id in to_auto_flatten:
            self.flatten(pos_id, market_map)

    # ── Settlement ────────────────────────────────────────────────────────────

    def _settle_expired(self, pos_id: str) -> None:
        """
        Settle a position at expiry.

        In paper trading we use the worst-case guaranteed outcome:
          - At least one leg pays $1 (guaranteed by the arb structure).
          - Worst case: exactly one leg wins → payout = $1 per contract pair.
          - Fee: 7% of $1 = $0.07 per contract pair.

        Realized PnL per contract = $1 - entry_cost - $0.07 = gross_edge - $0.07 = net_edge.
        """
        pos = self._open.pop(pos_id, None)
        if pos is None:
            return

        fee_per_contract = self.fee_rate * 1.0   # 7% on the $1 that wins
        realized = pos.net_edge * pos.size        # (gross_edge - fee_rate) * size

        pos.realized_pnl = realized
        pos.fees_paid = fee_per_contract * pos.size
        pos.unrealized_pnl = 0.0
        pos.status = "closed"
        pos.exit_time = datetime.now(timezone.utc)
        pos.exit_reason = "expired"

        self._closed.append(pos)
        self._positioned.pop(pos.signal_id, None)

        self._trades.append(TradeRecord(
            id=str(uuid.uuid4())[:8],
            position_id=pos.id,
            timestamp=pos.exit_time,
            action="CLOSE_EXPIRY",
            series=pos.series,
            lower_ticker=pos.lower_ticker,
            higher_ticker=pos.higher_ticker,
            lower_threshold=pos.lower_threshold,
            higher_threshold=pos.higher_threshold,
            size=pos.size,
            lower_entry=pos.lower_entry,
            higher_entry=pos.higher_entry,
            gross_edge=pos.gross_edge,
            net_edge=pos.net_edge,
            pnl=realized,
            fees=fee_per_contract * pos.size,
            status="expired",
            strategy=pos.strategy,
        ))

        print(f"[PaperTrader] SETTLE {pos.id} (expired): PnL=${realized:.2f}")
        self.save()

    # ── Manual flatten ────────────────────────────────────────────────────────

    def flatten(self, pos_id: str, market_map: Dict[str, dict]) -> Optional[Position]:
        """
        Close both legs at current market prices (paper slippage = bid/ask spread).
          lower YES: sell at yes_bid
          higher NO: sell at (1 - yes_ask) of higher
        """
        pos = self._open.get(pos_id)
        if pos is None:
            return None

        lower_m = market_map.get(pos.lower_ticker, {})
        higher_m = market_map.get(pos.higher_ticker, {})

        lower_exit = _mkt_bid(lower_m) if lower_m else 0.0
        higher_no_exit = 1.0 - (_mkt_ask(higher_m) if higher_m else pos.higher_entry)

        gross_payout = lower_exit + higher_no_exit
        gain = max(0.0, gross_payout - pos.entry_cost)
        fee = self.fee_rate * gain
        realized = (gross_payout - pos.entry_cost - fee) * pos.size

        pos.realized_pnl = realized
        pos.fees_paid = fee * pos.size
        pos.unrealized_pnl = 0.0
        pos.status = "closed"
        pos.exit_time = datetime.now(timezone.utc)
        pos.exit_reason = "flattened"

        self._open.pop(pos_id)
        self._closed.append(pos)
        self._positioned.pop(pos.signal_id, None)

        self._trades.append(TradeRecord(
            id=str(uuid.uuid4())[:8],
            position_id=pos.id,
            timestamp=pos.exit_time,
            action="CLOSE_FLATTEN",
            series=pos.series,
            lower_ticker=pos.lower_ticker,
            higher_ticker=pos.higher_ticker,
            lower_threshold=pos.lower_threshold,
            higher_threshold=pos.higher_threshold,
            size=pos.size,
            lower_entry=pos.lower_entry,
            higher_entry=pos.higher_entry,
            gross_edge=pos.gross_edge,
            net_edge=pos.net_edge,
            pnl=realized,
            fees=fee * pos.size,
            status="flattened",
            strategy=pos.strategy,
        ))

        print(f"[PaperTrader] FLATTEN {pos.id}: PnL=${realized:.2f}")
        self.save()
        return pos

    # ── Bucket entry ──────────────────────────────────────────────────────────

    def execute_bucket(self, signal: BucketSumSignal) -> Optional[BucketPosition]:
        """Paper-fill all bucket YES legs simultaneously at current ask prices."""
        if signal.id in self._bucket_positioned:
            return None

        size = min(signal.avail_size, self.max_size)
        if size <= 0:
            return None

        pos_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc)

        bucket_entries = [b.yes_ask for b in sorted(signal.buckets, key=lambda x: x.bucket_floor)]
        entry_cost = sum(bucket_entries)

        pos = BucketPosition(
            id=pos_id,
            signal_id=signal.id,
            series=signal.series,
            expiry_dt=signal.expiry_dt,
            event_ticker=signal.id,
            bucket_tickers=[b.ticker for b in sorted(signal.buckets, key=lambda x: x.bucket_floor)],
            bucket_entries=bucket_entries,
            size=size,
            entry_cost=entry_cost,
            gross_edge=signal.gross_edge,
            net_edge=signal.net_edge,
            entry_time=now,
        )
        # Initial unrealized: mark at current bids
        sum_bids = sum(b.yes_bid for b in signal.buckets)
        pos.unrealized_pnl = (sum_bids - entry_cost) * size

        self._bucket_open[pos_id] = pos
        self._bucket_positioned[signal.id] = pos_id

        self._trades.append(TradeRecord(
            id=str(uuid.uuid4())[:8],
            position_id=pos_id,
            timestamp=now,
            action="OPEN",
            series=signal.series,
            lower_ticker=signal.id,
            higher_ticker=f"bucket_sum_{len(signal.buckets)}",
            lower_threshold=0.0,
            higher_threshold=0.0,
            size=size,
            lower_entry=entry_cost,
            higher_entry=0.0,
            gross_edge=signal.gross_edge,
            net_edge=signal.net_edge,
            pnl=None,
            fees=0.0,
            status="paper_filled",
            strategy="bucket_arb",
        ))

        print(
            f"[PaperTrader] OPEN BUCKET {pos_id}: "
            f"{signal.id} {len(signal.buckets)} buckets "
            f"cost={entry_cost:.3f} edge={signal.gross_edge:.3f} size={size}"
        )
        self.save()
        return pos

    # ── Single-leg mispriced market ───────────────────────────────────────────

    def execute_single_leg(self, sig: SingleLegSignal) -> Optional[SingleLegPosition]:
        """Buy YES on the single mispriced market at current ask."""
        if sig.id in self._single_positioned:
            return None
        size = min(sig.market.open_interest if sig.market.open_interest > 0 else self.max_size,
                   self.max_size)
        if size <= 0:
            return None

        pos_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc)

        pos = SingleLegPosition(
            id=pos_id,
            signal_id=sig.id,
            series=sig.series,
            expiry_dt=sig.expiry_dt,
            ticker=sig.market.ticker,
            threshold=sig.market.threshold,
            adj_ticker=sig.adj_higher.ticker,
            size=size,
            entry_price=sig.market.yes_ask,
            target_bid=sig.target_bid,
            entry_time=now,
            current_bid=sig.market.yes_bid,
        )
        pos.unrealized_pnl = (pos.current_bid - pos.entry_price) * size

        self._single_open[pos_id] = pos
        self._single_positioned[sig.id] = pos_id

        print(
            f"[PaperTrader] OPEN SINGLE-LEG {pos_id}: "
            f"{sig.market.ticker} YES@{sig.market.yes_ask:.2f} "
            f"inversion={sig.inversion:.2f} target={sig.target_bid:.2f} size={size}"
        )
        self.save()
        return pos

    def update_single_leg_marks(self, threshold_map: Dict, int_threshold_map: Dict) -> List[str]:
        """
        Refresh P&L for open single-leg positions; auto-close when target hit or expired.
        Returns list of auto-closed position IDs.
        """
        auto_closed = []
        now = datetime.now(timezone.utc)

        for pos in list(self._single_open.values()):
            tm = threshold_map.get(pos.ticker) or int_threshold_map.get(pos.ticker)
            if tm is not None:
                pos.current_bid = tm.yes_bid
            pos.unrealized_pnl = round((pos.current_bid - pos.entry_price) * pos.size, 4)

            # Auto-exit: price normalized to near fair value
            if pos.current_bid >= pos.target_bid:
                gain = pos.current_bid - pos.entry_price
                fee = self.fee_rate * gain if gain > 0 else 0.0
                pos.realized_pnl = round((gain - fee) * pos.size, 4)
                pos.exit_price = pos.current_bid
                pos.exit_time = now
                pos.exit_reason = "target_hit"
                pos.status = "closed"
                pos.unrealized_pnl = 0.0
                self._single_open.pop(pos.id)
                self._single_closed.append(pos)
                self._single_positioned.pop(pos.signal_id, None)
                auto_closed.append(pos.id)
                print(f"[PaperTrader] AUTO-CLOSE SINGLE-LEG {pos.id}: "
                      f"bid={pos.current_bid:.2f} PnL=${pos.realized_pnl:.2f}")
                continue

            # Expire
            if pos.expiry_dt <= now:
                gain = pos.current_bid - pos.entry_price
                fee = self.fee_rate * gain if gain > 0 else 0.0
                pos.realized_pnl = round((gain - fee) * pos.size, 4)
                pos.exit_price = pos.current_bid
                pos.exit_time = now
                pos.exit_reason = "expired"
                pos.status = "closed"
                pos.unrealized_pnl = 0.0
                self._single_open.pop(pos.id)
                self._single_closed.append(pos)
                self._single_positioned.pop(pos.signal_id, None)
                auto_closed.append(pos.id)
                print(f"[PaperTrader] EXPIRE SINGLE-LEG {pos.id}: PnL=${pos.realized_pnl:.2f}")

        if auto_closed:
            self.save()
        return auto_closed

    def flatten_single_leg(self, pos_id: str) -> Optional[SingleLegPosition]:
        """Manually close a single-leg position at current bid."""
        pos = self._single_open.get(pos_id)
        if pos is None:
            return None
        gain = pos.current_bid - pos.entry_price
        fee = self.fee_rate * gain if gain > 0 else 0.0
        pos.realized_pnl = round((gain - fee) * pos.size, 4)
        pos.exit_price = pos.current_bid
        pos.exit_time = datetime.now(timezone.utc)
        pos.exit_reason = "flattened"
        pos.status = "closed"
        pos.unrealized_pnl = 0.0
        self._single_open.pop(pos_id)
        self._single_closed.append(pos)
        self._single_positioned.pop(pos.signal_id, None)
        print(f"[PaperTrader] FLATTEN SINGLE-LEG {pos.id}: PnL=${pos.realized_pnl:.2f}")
        self.save()
        return pos

    # ── Bucket MTM + settlement ────────────────────────────────────────────────

    def update_marks_bucket(self, market_map: Dict[str, dict]) -> None:
        """Refresh unrealized PnL for bucket positions; settle expired ones."""
        now = datetime.now(timezone.utc)
        to_settle = []

        for pos in list(self._bucket_open.values()):
            if pos.expiry_dt <= now:
                to_settle.append(pos.id)
                continue

            # MTM = sum(current bid for each bucket) - entry_cost
            sum_bids = 0.0
            priced = 0
            for t in pos.bucket_tickers:
                m = market_map.get(t)
                if m:
                    b = _mkt_bid(m)
                    if b > 0:
                        priced += 1
                        sum_bids += b
            if priced == len(pos.bucket_tickers):
                pos.unrealized_pnl = (sum_bids - pos.entry_cost) * pos.size

        for pos_id in to_settle:
            self._settle_bucket(pos_id)

    def _settle_bucket(self, pos_id: str) -> None:
        """Settle at expiry using guaranteed net_edge."""
        pos = self._bucket_open.pop(pos_id, None)
        if pos is None:
            return

        fee_per_contract = self.fee_rate * 1.0
        realized = pos.net_edge * pos.size

        pos.realized_pnl = realized
        pos.fees_paid = fee_per_contract * pos.size
        pos.unrealized_pnl = 0.0
        pos.status = "closed"
        pos.exit_time = datetime.now(timezone.utc)
        pos.exit_reason = "expired"

        self._bucket_closed.append(pos)
        self._bucket_positioned.pop(pos.signal_id, None)

        self._trades.append(TradeRecord(
            id=str(uuid.uuid4())[:8],
            position_id=pos.id,
            timestamp=pos.exit_time,
            action="CLOSE_EXPIRY",
            series=pos.series,
            lower_ticker=pos.event_ticker,
            higher_ticker=f"bucket_sum_{len(pos.bucket_tickers)}",
            lower_threshold=0.0,
            higher_threshold=0.0,
            size=pos.size,
            lower_entry=pos.entry_cost,
            higher_entry=0.0,
            gross_edge=pos.gross_edge,
            net_edge=pos.net_edge,
            pnl=realized,
            fees=pos.fees_paid,
            status="expired",
            strategy="bucket_arb",
        ))

        print(f"[PaperTrader] SETTLE BUCKET {pos.id} (expired): PnL=${realized:.2f}")
        self.save()

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, path: str = STATE_FILE) -> None:
        """Atomically persist all state to a JSON file."""
        state = {
            "open": {k: dataclasses.asdict(v) for k, v in self._open.items()},
            "closed": [dataclasses.asdict(v) for v in self._closed],
            "trades": [dataclasses.asdict(v) for v in self._trades],
            "positioned": dict(self._positioned),
            "bucket_open": {k: dataclasses.asdict(v) for k, v in self._bucket_open.items()},
            "bucket_closed": [dataclasses.asdict(v) for v in self._bucket_closed],
            "bucket_positioned": dict(self._bucket_positioned),
            "single_open": {k: dataclasses.asdict(v) for k, v in self._single_open.items()},
            "single_closed": [dataclasses.asdict(v) for v in self._single_closed],
            "single_positioned": dict(self._single_positioned),
        }
        tmp = path + ".tmp"
        parent = os.path.dirname(tmp)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(state, f, cls=_DTEncoder, indent=2)
        os.replace(tmp, path)

    def load(self, path: str = STATE_FILE) -> None:
        """Load persisted state from JSON file (called once at startup)."""
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                state = json.load(f)
            for k, v in state.get("open", {}).items():
                self._open[k] = _load_position(v)
            for v in state.get("closed", []):
                self._closed.append(_load_position(v))
            for v in state.get("trades", []):
                self._trades.append(_load_trade(v))
            self._positioned.update(state.get("positioned", {}))
            for k, v in state.get("bucket_open", {}).items():
                self._bucket_open[k] = _load_bucket_position(v)
            for v in state.get("bucket_closed", []):
                self._bucket_closed.append(_load_bucket_position(v))
            self._bucket_positioned.update(state.get("bucket_positioned", {}))
            for k, v in state.get("single_open", {}).items():
                self._single_open[k] = _load_single_leg_position(v)
            for v in state.get("single_closed", []):
                self._single_closed.append(_load_single_leg_position(v))
            self._single_positioned.update(state.get("single_positioned", {}))
            print(
                f"[PaperTrader] Loaded state: {len(self._open)} open, "
                f"{len(self._closed)} closed, {len(self._trades)} trades, "
                f"{len(self._bucket_open)} bucket_open, {len(self._single_open)} single_open"
            )
        except Exception as e:
            print(f"[PaperTrader] Failed to load state from {path}: {e}")

    # ── Reset ─────────────────────────────────────────────────────────────────

    def reset(self) -> None:
        self._open.clear()
        self._closed.clear()
        self._trades.clear()
        self._positioned.clear()
        self._bucket_open.clear()
        self._bucket_closed.clear()
        self._single_open.clear()
        self._single_closed.clear()
        self._single_positioned.clear()
        self._bucket_positioned.clear()
        self.save()
