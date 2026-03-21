"""
Data models for the Kalshi arb bot.

Strategy 1 — Threshold monotonicity:
  P(X >= a) >= P(X >= b) for a < b.
  Violation: bid(b) > ask(a) → buy YES at a, buy NO at b.
  Gross edge = bid(b) - ask(a). Guaranteed $1 payout per pair.

Strategy 2 — Bucket sum arb:
  All bucket YES prices sum to 1.0 (exactly one bucket resolves YES).
  If sum(all bucket asks) < 1.0 - fee, buy all buckets for guaranteed profit.
  Gross edge = 1.0 - sum(asks).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class ThresholdMarket:
    ticker: str
    event_ticker: str       # ticker without -T<level> suffix
    series: str
    expiry_dt: datetime
    threshold: float
    yes_bid: float          # 0-1
    yes_ask: float          # 0-1
    open_interest: int = 0
    title: str = ""         # human-readable label (yes_sub_title from API)

    def mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2


@dataclass
class ViolationSignal:
    """A detected monotonicity violation ready to trade."""
    id: str                         # "{lower_ticker}|{higher_ticker}"
    series: str
    expiry_dt: datetime
    lower: ThresholdMarket
    higher: ThresholdMarket
    gross_edge: float               # bid(higher) - ask(lower); guaranteed min profit/contract
    net_edge: float                 # gross_edge - fee_rate (worst-case: one leg wins)
    entry_cost: float               # ask(lower) + (1 - bid(higher)); cost per contract pair
    avail_size: int
    detected_at: datetime
    # Middle-band pricing: P(lower <= X < higher) ≈ mid(lower) - mid(higher)
    # When both legs resolve YES, payout = $2 instead of $1.
    middle_prob: float = 0.0        # market-implied P(both legs win)
    expected_edge: float = 0.0      # net_edge + middle_prob * (1 - fee) — true EV per contract
    # True orderbook depth at the target prices (filled by main.py after detection).
    # 0 = not yet fetched.
    lower_depth: int = 0            # YES ask depth at lower.yes_ask
    higher_depth: int = 0           # NO ask depth at (1 - higher.yes_bid)

    def to_dict(self) -> dict:
        # Recompute from live prices (ThresholdMarket objects are updated by ticks)
        live_edge = round(self.higher.yes_bid - self.lower.yes_ask, 4)
        fee = self.gross_edge - self.net_edge  # preserve original fee rate
        live_net = round(live_edge - fee, 4)
        live_lower_mid = (self.lower.yes_bid + self.lower.yes_ask) / 2
        live_higher_mid = (self.higher.yes_bid + self.higher.yes_ask) / 2
        live_middle_prob = round(max(0.0, live_lower_mid - live_higher_mid), 4)
        live_expected_edge = round(live_net + live_middle_prob * (1.0 - fee), 4)
        return {
            "id": self.id,
            "series": self.series,
            "expiry": self.expiry_dt.isoformat(),
            "lower_ticker": self.lower.ticker,
            "higher_ticker": self.higher.ticker,
            "lower_threshold": self.lower.threshold,
            "higher_threshold": self.higher.threshold,
            "lower_ask": round(self.lower.yes_ask, 4),
            "higher_bid": round(self.higher.yes_bid, 4),
            "gross_edge": live_edge,
            "net_edge": live_net,
            "middle_prob": live_middle_prob,
            "expected_edge": live_expected_edge,
            "entry_cost": round(self.lower.yes_ask + (1.0 - self.higher.yes_bid), 4),
            "avail_size": self.avail_size,
            "lower_depth": self.lower_depth,
            "higher_depth": self.higher_depth,
            "detected_at": self.detected_at.isoformat(),
            "event_ticker": self.lower.event_ticker,
        }


@dataclass
class Position:
    """A paper position in a monotonicity arb pair."""
    id: str
    signal_id: str
    series: str
    expiry_dt: datetime
    lower_ticker: str
    higher_ticker: str
    lower_threshold: float
    higher_threshold: float
    size: int
    lower_entry: float      # ask paid for YES at lower threshold
    higher_entry: float     # (1 - bid) paid for NO at higher threshold
    entry_cost: float       # lower_entry + higher_entry
    entry_time: datetime
    gross_edge: float
    net_edge: float
    entry_avail_size: int = 0   # L2 depth available at entry (pre-fill)
    status: str = "open"    # "open" | "closed"
    strategy: str = "threshold_arb"  # "threshold_arb" | "structural_arb"
    # Middle-band probability at entry (market-implied P(both legs win))
    # EV per contract = net_edge + middle_prob × (1 - fee_rate)
    middle_prob: float = 0.0

    # Mark-to-market
    lower_mid: float = 0.0
    higher_no_mid: float = 0.0
    unrealized_pnl: float = 0.0

    # Realized (on close)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    exit_time: Optional[datetime] = None
    exit_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "signal_id": self.signal_id,
            "series": self.series,
            "expiry": self.expiry_dt.isoformat(),
            "lower_ticker": self.lower_ticker,
            "higher_ticker": self.higher_ticker,
            "lower_threshold": self.lower_threshold,
            "higher_threshold": self.higher_threshold,
            "size": self.size,
            "lower_entry": round(self.lower_entry, 4),
            "higher_entry": round(self.higher_entry, 4),
            "entry_cost": round(self.entry_cost, 4),
            "entry_time": self.entry_time.isoformat(),
            "gross_edge": round(self.gross_edge, 4),
            "net_edge": round(self.net_edge, 4),
            "middle_prob": round(self.middle_prob, 4),
            "entry_avail_size": self.entry_avail_size,
            "status": self.status,
            "lower_mid": round(self.lower_mid, 4),
            "higher_no_mid": round(self.higher_no_mid, 4),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "fees_paid": round(self.fees_paid, 4),
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_reason": self.exit_reason,
            "strategy": self.strategy,
        }


@dataclass
class TradeRecord:
    """Immutable record of an entry or exit event."""
    id: str
    position_id: str
    timestamp: datetime
    action: str             # "OPEN" | "CLOSE_EXPIRY" | "CLOSE_FLATTEN"
    series: str
    lower_ticker: str
    higher_ticker: str
    lower_threshold: float
    higher_threshold: float
    size: int
    lower_entry: float
    higher_entry: float
    gross_edge: float
    net_edge: float
    pnl: Optional[float]    # None when action=OPEN
    fees: float
    status: str             # "paper_filled" | "expired" | "flattened"
    strategy: str = "threshold_arb"  # "threshold_arb" | "structural_arb" | "bucket_arb"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "position_id": self.position_id,
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "series": self.series,
            "lower_ticker": self.lower_ticker,
            "higher_ticker": self.higher_ticker,
            "lower_threshold": self.lower_threshold,
            "higher_threshold": self.higher_threshold,
            "size": self.size,
            "lower_entry": round(self.lower_entry, 4),
            "higher_entry": round(self.higher_entry, 4),
            "gross_edge": round(self.gross_edge, 4),
            "net_edge": round(self.net_edge, 4),
            "pnl": round(self.pnl, 4) if self.pnl is not None else None,
            "fees": round(self.fees, 4),
            "status": self.status,
            "strategy": self.strategy,
        }


# ── Bucket sum arb models ──────────────────────────────────────────────────────


@dataclass
class StructuralAnomaly:
    """
    A non-adjacent monotonicity violation (index gap >= 2).
    The markets between lower and higher are 'odd' — their prices make a
    non-adjacent pair violate monotonicity even though adjacent pairs may not.
    Strategy: Buy YES at lower + Buy NO at higher for guaranteed $1 payout.
    """
    id: str                                     # "{lower_ticker}|{higher_ticker}"
    series: str
    expiry_dt: datetime
    lower: ThresholdMarket
    higher: ThresholdMarket
    middle_markets: List[ThresholdMarket]        # markets between lower and higher
    gross_edge: float                            # bid(higher) - ask(lower)
    net_edge: float
    entry_cost: float
    avail_size: int
    detected_at: datetime

    def to_dict(self) -> dict:
        # Live prices (lower/higher objects are updated by ticks)
        live_gross = round(self.higher.yes_bid - self.lower.yes_ask, 4)
        fee = round(self.gross_edge - self.net_edge, 4)  # preserve original fee rate
        live_net = round(live_gross - fee, 4)
        live_lower_mid = (self.lower.yes_bid + self.lower.yes_ask) / 2
        live_higher_mid = (self.higher.yes_bid + self.higher.yes_ask) / 2
        live_middle_prob = round(max(0.0, live_lower_mid - live_higher_mid), 4)
        live_expected_edge = round(live_net + live_middle_prob * (1.0 - fee), 4)
        return {
            "id": self.id,
            "series": self.series,
            "expiry": self.expiry_dt.isoformat(),
            "lower_ticker": self.lower.ticker,
            "higher_ticker": self.higher.ticker,
            "lower_threshold": self.lower.threshold,
            "higher_threshold": self.higher.threshold,
            "lower_ask": round(self.lower.yes_ask, 4),
            "higher_bid": round(self.higher.yes_bid, 4),
            "gross_edge": live_gross,
            "net_edge": live_net,
            "middle_prob": live_middle_prob,
            "expected_edge": live_expected_edge,
            "entry_cost": round(self.lower.yes_ask + (1.0 - self.higher.yes_bid), 4),
            "avail_size": self.avail_size,
            "detected_at": self.detected_at.isoformat(),
            "gap": len(self.middle_markets) + 1,
            "event_ticker": self.lower.event_ticker,
            "middle_markets": [
                {
                    "ticker": m.ticker,
                    "threshold": m.threshold,
                    "yes_bid": round(m.yes_bid, 4),
                    "yes_ask": round(m.yes_ask, 4),
                }
                for m in self.middle_markets
            ],
        }


@dataclass
class SingleLegSignal:
    """
    A ladder rung that is anomalously cheap relative to both its neighbors
    (mean-reversion strategy).

    For sorted ladder [..., T_{k-1}, T_k, T_{k+1}, ...]:
      interpolated_mid = (T_{k-1}.mid() + T_{k+1}.mid()) / 2
      anomaly = interpolated_mid - T_k.mid()  (positive → T_k is cheap)

    Both neighbors must agree the price is too low. When the anomaly closes,
    the position profits.

    Trade: buy YES on `market` at market.yes_ask.
    Exit:  when market.yes_bid >= target_bid (≈ interpolated fair value).
    """
    id: str                         # = market.ticker
    series: str
    expiry_dt: datetime
    market: ThresholdMarket         # the cheap middle rung
    adj_higher: Optional[ThresholdMarket] = None  # upper threshold neighbor (None for digital signals)
    inversion: float = 0.0          # anomaly: interpolated_mid - market.mid() (>0 = cheap)
    target_bid: float = 0.0         # auto-exit when market.yes_bid reaches this
    detected_at: datetime = None    # type: ignore[assignment]
    adj_lower: Optional[ThresholdMarket] = None   # lower threshold neighbor (higher price)
    avail_size: int = 0             # L2 depth at entry price; 0 = no depth / not yet fetched
    side: str = "yes"               # "yes" = buy YES (mean-rev); "no" = buy NO (sell expensive)
    strategy: str = "mean_rev"      # "mean_rev" | "sell_expensive" | "digital"

    def to_dict(self) -> dict:
        if self.adj_higher is not None:
            if self.adj_lower is not None:
                live_interp = round((self.adj_lower.mid() + self.adj_higher.mid()) / 2, 4)
            else:
                live_interp = round(self.adj_higher.mid(), 4)
            live_anomaly = round(live_interp - self.market.mid(), 4)
        else:
            # digital signal: no neighbor context; use stored inversion as-is
            live_interp = round(self.market.mid() + self.inversion, 4)
            live_anomaly = self.inversion
        d = {
            "id": self.id,
            "series": self.series,
            "expiry": self.expiry_dt.isoformat(),
            "ticker": self.market.ticker,
            "threshold": self.market.threshold,
            "title": self.market.title,
            "adj_ticker": self.adj_higher.ticker if self.adj_higher else "",
            "adj_threshold": self.adj_higher.threshold if self.adj_higher else 0,
            "adj_title": self.adj_higher.title if self.adj_higher else "",
            "ask": round(self.market.yes_ask, 4),
            "bid": round(self.market.yes_bid, 4),
            "adj_ask": round(self.adj_higher.yes_ask, 4) if self.adj_higher else 0,
            "adj_bid": round(self.adj_higher.yes_bid, 4) if self.adj_higher else 0,
            "inversion": live_anomaly,
            "interp_mid": live_interp,
            "target_bid": round(self.target_bid, 4),
            "detected_at": self.detected_at.isoformat() if self.detected_at else "",
            "event_ticker": self.market.event_ticker,
            "avail_size": self.avail_size,
            "side": self.side,
            "strategy": self.strategy,
        }
        if self.adj_lower is not None:
            d["adj_lower_ticker"] = self.adj_lower.ticker
            d["adj_lower_threshold"] = self.adj_lower.threshold
            d["adj_lower_title"] = self.adj_lower.title
            d["adj_lower_ask"] = round(self.adj_lower.yes_ask, 4)
            d["adj_lower_bid"] = round(self.adj_lower.yes_bid, 4)
        return d


@dataclass
class SingleLegPosition:
    """Paper position: long YES on a single mispriced market."""
    id: str
    signal_id: str          # = market ticker
    series: str
    expiry_dt: datetime
    ticker: str
    threshold: float
    adj_ticker: str         # adjacent reference market
    size: int
    entry_price: float      # yes_ask at entry (0-1)
    entry_bid: float        # yes_bid at entry — stop-loss basis (avoids spread noise)
    target_bid: float       # auto-exit when bid reaches this
    entry_time: datetime
    entry_avail_size: int = 0       # L2 depth available at entry (pre-fill)
    status: str = "open"    # "open" | "closed"
    strategy: str = "mispriced_leg"
    side: str = "yes"               # "yes" = long YES; "no" = long NO (sell expensive)
    current_bid: float = 0.0       # YES bid (YES pos) or YES ask (NO pos) — see update_single_leg_marks
    current_ask: float = 0.0       # YES ask (YES pos) or YES bid (NO pos) — opposite of current_bid
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    exit_reason: str = ""
    # Entry rationale — captured from the signal at execution time
    entry_inversion: float = 0.0        # how many ¢ below fair value at entry
    entry_interp_mid: float = 0.0       # interpolated fair-value mid at entry
    entry_adj_lower_bid: float = 0.0    # lower-threshold neighbor bid at entry
    entry_adj_higher_bid: float = 0.0   # upper-threshold neighbor bid at entry
    entry_adj_lower_threshold: float = 0.0
    entry_adj_higher_threshold: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "signal_id": self.signal_id,
            "series": self.series,
            "expiry": self.expiry_dt.isoformat(),
            "ticker": self.ticker,
            "threshold": self.threshold,
            "adj_ticker": self.adj_ticker,
            "size": self.size,
            "entry_price": round(self.entry_price, 4),   # yes_ask at entry
            "entry_bid": round(self.entry_bid, 4),         # yes_bid at entry
            "target_bid": round(self.target_bid, 4),
            "entry_time": self.entry_time.isoformat(),
            "entry_avail_size": self.entry_avail_size,
            "status": self.status,
            "strategy": self.strategy,
            "side": self.side,
            "current_bid": round(self.current_bid, 4),
            "current_ask": round(self.current_ask, 4),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "exit_price": round(self.exit_price, 4),
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_reason": self.exit_reason,
            "entry_inversion": round(self.entry_inversion, 4),
            "entry_interp_mid": round(self.entry_interp_mid, 4),
            "entry_adj_lower_bid": round(self.entry_adj_lower_bid, 4),
            "entry_adj_higher_bid": round(self.entry_adj_higher_bid, 4),
            "entry_adj_lower_threshold": self.entry_adj_lower_threshold,
            "entry_adj_higher_threshold": self.entry_adj_higher_threshold,
        }


@dataclass
class BucketMarket:
    """A single bucket YES market (e.g. KXBTCD closes in [82500, 83000))."""
    ticker: str
    event_ticker: str       # ticker without -B<floor> suffix
    series: str
    expiry_dt: datetime
    bucket_floor: float
    yes_bid: float          # 0-1
    yes_ask: float          # 0-1
    open_interest: int = 0

    def mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2


@dataclass
class BucketSumSignal:
    """sum(all bucket asks) < 1.0 → buy all buckets for guaranteed profit."""
    id: str                         # event_ticker
    series: str
    expiry_dt: datetime
    buckets: List[BucketMarket]
    sum_asks: float                 # total cost to buy one YES in each bucket
    gross_edge: float               # 1.0 - sum_asks
    net_edge: float                 # gross_edge - fee_rate
    avail_size: int
    detected_at: datetime

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": "bucket_sum",
            "series": self.series,
            "expiry": self.expiry_dt.isoformat(),
            "event_ticker": self.id,
            "bucket_count": len(self.buckets),
            "sum_asks": round(self.sum_asks, 4),
            "gross_edge": round(self.gross_edge, 4),
            "net_edge": round(self.net_edge, 4),
            "avail_size": self.avail_size,
            "detected_at": self.detected_at.isoformat(),
            "buckets": [
                {"ticker": b.ticker, "floor": b.bucket_floor, "ask": round(b.yes_ask, 4)}
                for b in sorted(self.buckets, key=lambda x: x.bucket_floor)
            ],
        }


@dataclass
class BucketPosition:
    """Paper position in a bucket sum arb (all N buckets bought simultaneously)."""
    id: str
    signal_id: str          # event_ticker
    series: str
    expiry_dt: datetime
    event_ticker: str
    bucket_tickers: List[str]
    bucket_entries: List[float]     # ask price paid per bucket (0-1)
    size: int
    entry_cost: float               # sum(bucket_entries)
    gross_edge: float               # 1.0 - entry_cost
    net_edge: float                 # gross_edge - fee_rate
    entry_time: datetime
    entry_avail_size: int = 0       # L2 depth available at entry
    status: str = "open"
    strategy: str = "bucket_arb"
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    exit_time: Optional[datetime] = None
    exit_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": "bucket_sum",
            "strategy": self.strategy,
            "signal_id": self.signal_id,
            "series": self.series,
            "expiry": self.expiry_dt.isoformat(),
            "event_ticker": self.event_ticker,
            "bucket_count": len(self.bucket_tickers),
            "size": self.size,
            "entry_cost": round(self.entry_cost, 4),
            "gross_edge": round(self.gross_edge, 4),
            "net_edge": round(self.net_edge, 4),
            "entry_avail_size": self.entry_avail_size,
            "status": self.status,
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "fees_paid": round(self.fees_paid, 4),
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_reason": self.exit_reason,
        }
