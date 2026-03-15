"""
Scan Kalshi markets for structural arbitrage opportunities.

Strategy 1 — Threshold monotonicity:
  For thresholds a < b on the same underlying + expiry:
    P(X >= a) >= P(X >= b) must hold.
  Violation: bid(b) > ask(a) -> buy YES at a + buy NO at b.
  Gross edge = bid(b) - ask(a). Guaranteed $1 payout per pair.

Strategy 2 — Bucket sum arb:
  Bucket markets are mutually exclusive and exhaustive: exactly one resolves YES.
  If sum(all bucket asks) < 1.0, buy all buckets for guaranteed $1 profit.
  Gross edge = 1.0 - sum(asks).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import uuid

from models import (BucketMarket, BucketSumSignal, SingleLegSignal,
                    StructuralAnomaly, ThresholdMarket, ViolationSignal)

_T_RE = re.compile(r"-T([\d.]+)$", re.IGNORECASE)
_B_RE = re.compile(r"-B([\d.]+)$", re.IGNORECASE)
_N_RE = re.compile(r"-(\d+)$")            # dash + integer: KXNBASTL-GAMEID-PLAYER-1
_TEAM_N_RE = re.compile(r"([A-Z]{2,5})(\d+)$")  # team+integer: KXNBA1HSPREAD-...-NYK7

# Parlay/multi-variant market prefixes — their hex-suffixed tickers look like
# bucket markets but are not. Exclude them from structural arb scanning.
_PARLAY_PREFIXES = ("KXMVECROSSCATEGORY", "KXMVESPORTSMULTIGAMEEXTENDED", "KXMVE")

# Series that use -<integer> suffix as threshold (e.g. 1, 2, 3+ steals/blocks/goals)
_INT_THRESHOLD_SERIES = (
    "KXNBASTL",        # NBA steals
    "KXNBA1HTOTAL",    # NBA 1st-half total points
    "KXNCAAMB1HTOTAL", # NCAA Men's Basketball 1H total
    "KXNBABLK",        # NBA blocks
    "KXNBAAST",        # NBA assists
    "KXEPLGOAL",       # EPL goals scored by player (1+, 2+)
    "KXNHLGOAL",       # NHL player goals (1+, 2+, 3+)
    "KXNHLPTS",        # NHL player points (1+, 2+, 3+)
    "KXNHLAST",        # NHL player assists (1+, 2+, 3+)
)

# Series that use <TEAM><integer> suffix as threshold (e.g. NYK7 = NYK wins 1H by 7+)
_TEAM_N_SERIES = (
    "KXNBA1HSPREAD",    # NBA 1st-half point spread ladder
    "KXNCAAMB1HSPREAD", # NCAA Men's Basketball 1H spread ladder
)

KALSHI_FEE_RATE = 0.07  # 7% of gross winnings per resolved contract


def _parse_threshold(ticker: str) -> Optional[float]:
    m = _T_RE.search(ticker)
    return float(m.group(1)) if m else None


def _parse_expiry(market: dict) -> Optional[datetime]:
    for key in ("close_time", "expiration_time", "settlement_time"):
        val = market.get(key)
        if val:
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except Exception:
                pass
    return None


def group_integer_threshold_markets(markets: List[dict]) -> Dict[str, List[ThresholdMarket]]:
    """
    Filter and group sports threshold markets with two suffix formats:
      1. Dash+integer: KXNBASTL-GAMEID-PLAYER-1, -2, -3  (series in _INT_THRESHOLD_SERIES)
      2. Team+integer: KXNBA1HSPREAD-GAMEID-NYK7, NYK4    (series in _TEAM_N_SERIES)

    P(X >= lower) >= P(X >= higher) monotonicity must hold — same arb structure as
    financial threshold markets.
    """
    groups: Dict[str, List[ThresholdMarket]] = {}
    now = datetime.now(timezone.utc)

    for m in markets:
        ticker = m.get("ticker", "")
        series = ticker.split("-")[0].upper()

        # Pattern 1: dash+integer suffix
        if series in _INT_THRESHOLD_SERIES:
            match = _N_RE.search(ticker)
            if match is None:
                continue
            threshold = float(match.group(1))
            event_ticker = ticker[: match.start()]

        # Pattern 2: TEAM+integer suffix (e.g. NYK7 → team=NYK, threshold=7)
        elif series in _TEAM_N_SERIES:
            match = _TEAM_N_RE.search(ticker)
            if match is None:
                continue
            threshold = float(match.group(2))
            # event_ticker keeps the team prefix, strips only the numeric part
            event_ticker = ticker[: len(ticker) - len(match.group(2))]

        else:
            continue

        expiry_dt = _parse_expiry(m)
        if expiry_dt is None or expiry_dt <= now:
            continue

        yes_bid = float(m.get("yes_bid_dollars") or m.get("yes_bid") or 0)
        yes_ask = float(m.get("yes_ask_dollars") or m.get("yes_ask") or 0)

        tm = ThresholdMarket(
            ticker=ticker,
            event_ticker=event_ticker,
            series=series,
            expiry_dt=expiry_dt,
            threshold=threshold,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            open_interest=m.get("open_interest", 0),
        )
        groups.setdefault(event_ticker, []).append(tm)

    return groups


def group_threshold_markets(markets: List[dict]) -> Dict[str, List[ThresholdMarket]]:
    """
    Filter and group raw Kalshi market dicts into ThresholdMarket objects,
    keyed by event_ticker (same underlying + same expiry = same group).
    """
    groups: Dict[str, List[ThresholdMarket]] = {}
    now = datetime.now(timezone.utc)

    for m in markets:
        ticker = m.get("ticker", "")
        if any(ticker.startswith(p) for p in _PARLAY_PREFIXES):
            continue
        threshold = _parse_threshold(ticker)
        if threshold is None:
            continue

        expiry_dt = _parse_expiry(m)
        if expiry_dt is None or expiry_dt <= now:
            continue

        # event_ticker = everything before the final -T<number>
        event_ticker = _T_RE.sub("", ticker)
        series = event_ticker.split("-")[0].upper()

        # REST API returns yes_bid_dollars/yes_ask_dollars as string decimals (0-1 range).
        # Default to 0.0 so market is included in group but skipped by detectors.
        yes_bid = float(m.get("yes_bid_dollars") or m.get("yes_bid") or 0)
        yes_ask = float(m.get("yes_ask_dollars") or m.get("yes_ask") or 0)

        tm = ThresholdMarket(
            ticker=ticker,
            event_ticker=event_ticker,
            series=series,
            expiry_dt=expiry_dt,
            threshold=threshold,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            open_interest=m.get("open_interest", 0),
        )
        groups.setdefault(event_ticker, []).append(tm)

    return groups


def find_violations(
    groups: Dict[str, List[ThresholdMarket]],
    min_gross_edge: float,
    max_size: int,
    fee_rate: float = KALSHI_FEE_RATE,
    allow_negative_edge: bool = False,
    adjacent_only: bool = False,
) -> List[ViolationSignal]:
    """
    For each group with >= 2 threshold markets, check pairs for monotonicity violations.
    adjacent_only=True: only check consecutive (i, i+1) pairs (for near-miss display).
    adjacent_only=False: check all (i, j) pairs (for violation detection).
    Results sorted by gross_edge descending.
    """
    violations: List[ViolationSignal] = []
    now = datetime.now(timezone.utc)
    min_ttl = timedelta(minutes=15)  # skip markets expiring in <15 min

    for event_ticker, markets in groups.items():
        if len(markets) < 2:
            continue

        sorted_markets = sorted(markets, key=lambda x: x.threshold)

        # Skip groups expiring too soon (deep-ITM boundary markets add noise)
        if sorted_markets[0].expiry_dt - now < min_ttl:
            continue

        pairs = (
            [(sorted_markets[i], sorted_markets[i + 1]) for i in range(len(sorted_markets) - 1)]
            if adjacent_only
            else [(sorted_markets[i], sorted_markets[j])
                  for i in range(len(sorted_markets))
                  for j in range(i + 1, len(sorted_markets))]
        )

        for lower, higher in pairs:
            # Skip markets whose prices haven't been updated yet
            if lower.yes_ask == 0.0 or higher.yes_bid == 0.0:
                continue
            # P(X >= lower) >= P(X >= higher)  must hold
            # Violation: bid(higher) > ask(lower)
            gross_edge = higher.yes_bid - lower.yes_ask
            if gross_edge < min_gross_edge:
                continue

            net_edge = gross_edge - fee_rate   # worst case: one leg wins
            if not allow_negative_edge and net_edge <= 0:
                continue  # fee would eat all profit — hard reject

            entry_cost = lower.yes_ask + (1.0 - higher.yes_bid)

            # Size proxy: use open_interest if available, else max_size
            avail = min(
                lower.open_interest if lower.open_interest > 0 else max_size,
                higher.open_interest if higher.open_interest > 0 else max_size,
                max_size,
            )
            avail = max(avail, 1)

            violations.append(ViolationSignal(
                id=f"{lower.ticker}|{higher.ticker}",
                series=lower.series,
                expiry_dt=lower.expiry_dt,
                lower=lower,
                higher=higher,
                gross_edge=gross_edge,
                net_edge=net_edge,
                entry_cost=entry_cost,
                avail_size=avail,
                detected_at=now,
            ))

    violations.sort(key=lambda v: v.gross_edge, reverse=True)
    return violations


# ── Structural anomaly detection ──────────────────────────────────────────────


def find_structural_anomalies(
    groups: Dict[str, List[ThresholdMarket]],
    max_size: int,
    fee_rate: float = KALSHI_FEE_RATE,
    min_gross_edge: float = 0.0,
    top_n: int = 30,
) -> List[StructuralAnomaly]:
    """
    Find non-adjacent pairs where bid(higher) > ask(lower) - min_gross_edge.

    When min_gross_edge=0 (default): genuine violations only.
    When min_gross_edge<0: near-miss pairs within abs(min_gross_edge) of being
    a true arb — useful for showing manual trade candidates.

    Non-adjacent means there is at least one market between the pair on the
    threshold ladder, so a genuine violation implies the middle market(s) are
    structurally inconsistent with the outer two.
    """
    anomalies: List[StructuralAnomaly] = []
    seen_middle: set = set()      # avoid surfacing the same odd market multiple times
    per_group_count: Dict[str, int] = {}  # cap entries per event_ticker for diversity
    now = datetime.now(timezone.utc)
    min_ttl = timedelta(minutes=30)  # skip groups expiring soon (reduces noise)

    for event_ticker, markets in groups.items():
        if len(markets) < 3:
            continue

        sorted_markets = sorted(markets, key=lambda x: x.threshold)

        if sorted_markets[0].expiry_dt - now < min_ttl:
            continue

        for i in range(len(sorted_markets)):
            for j in range(i + 2, len(sorted_markets)):
                lower = sorted_markets[i]
                higher = sorted_markets[j]

                if lower.yes_ask == 0.0 or higher.yes_bid == 0.0:
                    continue

                # Skip deep-ITM pairs (BTC/ETH near-certain markets are noise)
                if lower.yes_ask >= 0.97:
                    continue

                gross_edge = higher.yes_bid - lower.yes_ask
                if gross_edge < min_gross_edge:
                    continue

                # Cap entries per group to prevent one series flooding the list
                if per_group_count.get(event_ticker, 0) >= 2:
                    continue

                middle = sorted_markets[i + 1 : j]
                middle_key = tuple(m.ticker for m in middle)
                if middle_key in seen_middle:
                    continue
                seen_middle.add(middle_key)
                per_group_count[event_ticker] = per_group_count.get(event_ticker, 0) + 1

                net_edge = gross_edge - fee_rate
                entry_cost = lower.yes_ask + (1.0 - higher.yes_bid)

                avail = min(
                    lower.open_interest if lower.open_interest > 0 else max_size,
                    higher.open_interest if higher.open_interest > 0 else max_size,
                    max_size,
                )
                avail = max(avail, 1)

                anomalies.append(StructuralAnomaly(
                    id=f"{lower.ticker}|{higher.ticker}",
                    series=lower.series,
                    expiry_dt=lower.expiry_dt,
                    lower=lower,
                    higher=higher,
                    middle_markets=middle,
                    gross_edge=gross_edge,
                    net_edge=net_edge,
                    entry_cost=entry_cost,
                    avail_size=avail,
                    detected_at=now,
                ))

    anomalies.sort(key=lambda a: a.gross_edge, reverse=True)
    return anomalies[:top_n]


# ── Inverted-leg mispricing ───────────────────────────────────────────────────


def find_inverted_legs(
    groups: Dict[str, List[ThresholdMarket]],
    min_inversion: float = 0.15,
    top_n: int = 20,
) -> List[SingleLegSignal]:
    """
    Detect markets where ask(lower_threshold) is significantly cheaper than
    ask(adjacent_higher_threshold) — the ladder is price-inverted.

    Normally ask(lower) >= ask(higher) because lower threshold = more likely.
    When ask(lower) << ask(higher), the lower market is mispriced cheap.
    The correct trade: buy YES on the cheap market (single leg, directional).
    Auto-exit when the bid reaches near the adjacent reference price (target_bid).

    min_inversion: how many dollars cheaper the lower must be (default 15¢).
    """
    signals: List[SingleLegSignal] = []
    seen: set = set()
    now = datetime.now(timezone.utc)
    min_ttl = timedelta(minutes=30)

    for event_ticker, markets in groups.items():
        if len(markets) < 2:
            continue
        sorted_markets = sorted(markets, key=lambda x: x.threshold)
        if sorted_markets[0].expiry_dt - now < min_ttl:
            continue

        for i in range(len(sorted_markets) - 1):
            lower = sorted_markets[i]
            higher = sorted_markets[i + 1]

            if lower.yes_ask <= 0 or higher.yes_ask <= 0:
                continue
            if lower.ticker in seen:
                continue

            # Liquidity filter: bid ≥ 10¢ and spread (ask - bid) ≤ 40¢.
            # Ratio-based checks are too aggressive — KXFED 3.25% at 50¢ ask with 20¢ bid
            # (spread=30¢) is a real mispricing. KXCPI at 72¢ ask / 4¢ bid (spread=68¢) is not.
            spread = lower.yes_ask - lower.yes_bid
            if lower.yes_bid < 0.10 or spread > 0.40:
                continue

            # Inversion: ask(lower) should be >= ask(higher); when inverted, lower is mispriced cheap
            inversion = higher.yes_ask - lower.yes_ask  # positive = lower is cheaper = inverted
            if inversion < min_inversion:
                continue

            seen.add(lower.ticker)
            # Target: exit when lower's bid climbs to near the adjacent higher's current ask
            target_bid = round(higher.yes_ask - 0.05, 4)

            signals.append(SingleLegSignal(
                id=lower.ticker,
                series=lower.series,
                expiry_dt=lower.expiry_dt,
                market=lower,
                adj_higher=higher,
                inversion=inversion,
                target_bid=target_bid,
                detected_at=now,
            ))

    signals.sort(key=lambda s: s.inversion, reverse=True)
    return signals[:top_n]


# ── Bucket sum arb ────────────────────────────────────────────────────────────


def _parse_bucket_floor(ticker: str) -> Optional[float]:
    m = _B_RE.search(ticker)
    return float(m.group(1)) if m else None


def group_bucket_markets(markets: List[dict]) -> Dict[str, List[BucketMarket]]:
    """
    Filter and group raw Kalshi market dicts into BucketMarket objects,
    keyed by event_ticker (same underlying + same expiry = same group).
    Only includes tradeable (non-settled, non-crossed, liquid) markets.
    """
    groups: Dict[str, List[BucketMarket]] = {}
    now = datetime.now(timezone.utc)

    for m in markets:
        ticker = m.get("ticker", "")
        if any(ticker.startswith(p) for p in _PARLAY_PREFIXES):
            continue
        bucket_floor = _parse_bucket_floor(ticker)
        if bucket_floor is None:
            continue

        expiry_dt = _parse_expiry(m)
        if expiry_dt is None or expiry_dt <= now:
            continue

        event_ticker = _B_RE.sub("", ticker)
        series = event_ticker.split("-")[0].upper()

        # REST API returns yes_bid_dollars/yes_ask_dollars as string decimals (0-1 range).
        yes_bid = float(m.get("yes_bid_dollars") or m.get("yes_bid") or 0)
        yes_ask = float(m.get("yes_ask_dollars") or m.get("yes_ask") or 0)

        bm = BucketMarket(
            ticker=ticker,
            event_ticker=event_ticker,
            series=series,
            expiry_dt=expiry_dt,
            bucket_floor=bucket_floor,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            open_interest=m.get("open_interest", 0),
        )
        groups.setdefault(event_ticker, []).append(bm)

    return groups


def find_bucket_violations(
    groups: Dict[str, List[BucketMarket]],
    min_gross_edge: float,
    max_size: int,
    fee_rate: float = KALSHI_FEE_RATE,
    allow_negative_edge: bool = False,
) -> List[BucketSumSignal]:
    """
    For each bucket group, check if sum(all asks) < 1.0.
    Note: in a fair market sum(asks) > 1.0 (ask > mid for each bucket).
    A violation means the market is collectively underpricing all buckets.
    Gross edge = 1.0 - sum(asks).  Net edge = gross_edge - fee_rate.
    Requires at least 3 buckets to be a meaningful exhaustive partition.

    allow_negative_edge=True: skip the net_edge > 0 hard-reject (for near-miss scanning).
    """
    violations: List[BucketSumSignal] = []
    now = datetime.now(timezone.utc)

    for event_ticker, buckets in groups.items():
        if len(buckets) < 3:
            continue

        # Skip entire group if any bucket has uninitialized price (0.0)
        if any(b.yes_ask == 0.0 for b in buckets):
            continue

        sum_asks = sum(b.yes_ask for b in buckets)
        gross_edge = 1.0 - sum_asks

        if gross_edge < min_gross_edge:
            continue

        net_edge = gross_edge - fee_rate
        if not allow_negative_edge and net_edge <= 0:
            continue

        avail = min(
            min(b.open_interest if b.open_interest > 0 else max_size for b in buckets),
            max_size,
        )
        avail = max(avail, 1)

        violations.append(BucketSumSignal(
            id=event_ticker,
            series=buckets[0].series,
            expiry_dt=buckets[0].expiry_dt,
            buckets=list(buckets),
            sum_asks=sum_asks,
            gross_edge=gross_edge,
            net_edge=net_edge,
            avail_size=avail,
            detected_at=now,
        ))

    violations.sort(key=lambda v: v.gross_edge, reverse=True)
    return violations
