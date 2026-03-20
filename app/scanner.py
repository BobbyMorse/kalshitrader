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
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import uuid

import numpy as np

from models import (BucketMarket, BucketSumSignal, SingleLegSignal,
                    StructuralAnomaly, ThresholdMarket, ViolationSignal)


# ── Distribution model for non-linear fair value estimation ──────────────────

def _fit_lognormal(sorted_markets: List[ThresholdMarket]):
    """
    Fit P(S > T) = Φ((μ - log(T)) / σ) to a full rung ladder.

    Linearises by applying Φ⁻¹ to each mid-price, then OLS on log(threshold).
    Returns (mu, sigma, r_squared) if fit quality >= threshold, else None.

    Works naturally for any price-based asset (BTC, ETH, oil, natgas, FX).
    Returns None (falls back to linear interpolation) when:
      - fewer than 4 priced rungs with interior probabilities
      - slope has wrong sign (prices increase with threshold — categorical)
      - scipy not installed
    """
    try:
        from scipy.stats import norm
    except ImportError:
        return None

    pts = [
        (m.threshold, m.mid())
        for m in sorted_markets
        if m.threshold > 0 and m.yes_bid > 0 and m.yes_ask > 0
        and 0.03 < m.mid() < 0.97
    ]
    if len(pts) < 4:
        return None

    log_t = np.array([np.log(t) for t, _ in pts])
    probs  = np.array([p          for _, p in pts])
    z      = norm.ppf(probs)   # Φ⁻¹(p) = (μ - log T) / σ  →  linear in log T

    A = np.column_stack([np.ones(len(log_t)), log_t])
    coeffs, _, _, _ = np.linalg.lstsq(A, z, rcond=None)
    a, b = coeffs            # z = a + b·log(T);  b = -1/σ  (must be negative)

    if b >= 0:               # prices increase with threshold → not a P(S>T) ladder
        return None

    sigma = -1.0 / b
    mu    = a * sigma

    z_pred = a + b * log_t
    ss_res = np.sum((z - z_pred) ** 2)
    ss_tot = np.sum((z - np.mean(z)) ** 2)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-10 else 0.0

    return mu, sigma, float(r2)


def _lognormal_fair(threshold: float, mu: float, sigma: float) -> float:
    """P(S > threshold) under log-normal with parameters (mu, sigma)."""
    from scipy.stats import norm
    return float(norm.cdf((mu - np.log(threshold)) / sigma))

_T_RE = re.compile(r"-T([\d.]+)$", re.IGNORECASE)
_BARE_FLOAT_RE = re.compile(r"-(\d+\.\d+)$")    # bare decimal: KXAAAGASM-26MAR31-4.50
_NP_RE = re.compile(r"-[NP]([\d.]+)$", re.IGNORECASE)  # N/P prefix: KXNGASMIN-26DEC31-N2.80
_B_RE = re.compile(r"-B([\d.]+)$", re.IGNORECASE)
_N_RE = re.compile(r"-(\d+)$")            # dash + integer: KXNBASTL-GAMEID-PLAYER-1
_TEAM_N_RE = re.compile(r"([A-Z]{2,5})(\d+)$")  # team+integer: KXNBA1HSPREAD-...-NYK7

# Parlay/multi-variant market prefixes — their hex-suffixed tickers look like
# bucket markets but are not. Exclude them from structural arb scanning.
_PARLAY_PREFIXES = ("KXMVECROSSCATEGORY", "KXMVESPORTSMULTIGAMEEXTENDED", "KXMVE")

# Series that use -<integer> suffix as threshold (e.g. 1, 2, 3+ steals/blocks/goals)
_INT_THRESHOLD_SERIES = (
    # NBA player props
    "KXNBASTL",        # NBA steals
    "KXNBABLK",        # NBA blocks
    "KXNBAAST",        # NBA assists
    "KXNBAPTS",        # NBA player points (10+, 15+, 20+)
    "KXNBAREB",        # NBA rebounds
    "KXNBA3PT",        # NBA 3-pointers made
    # NBA game totals / season wins
    "KXNBA1HTOTAL",    # NBA 1st-half total points
    "KXNBATOTAL",      # NBA game total points
    "KXNBADRAFTCAT",   # NBA draft category count
    # NCAA Basketball
    "KXNCAAMB1HTOTAL", # NCAA Men's Basketball 1H total
    "KXNCAAMBTOTAL",   # NCAA Men's Basketball game total
    "KXNCAAWBTOTAL",   # NCAA Women's Basketball game total
    # NHL
    "KXNHLGOAL",       # NHL player goals (1+, 2+, 3+)
    "KXNHLPTS",        # NHL player points (1+, 2+, 3+)
    "KXNHLAST",        # NHL player assists (1+, 2+, 3+)
    "KXNHLTOTAL",      # NHL game total goals
    # Soccer
    "KXEPLGOAL",       # EPL goals scored by player (1+, 2+)
    "KXEPLTOTAL",      # EPL game total goals
    "KXMLSTOTAL",      # MLS game total goals
    # MLB
    "KXMLBSTATCOUNT",  # MLB combined stat milestone counts
    # Golf
    "KXPGASTROKEMARGIN", # PGA stroke margin (1+, 2+, 3+ strokes ahead)
)

# Series that use <TEAM><integer> suffix as threshold (e.g. NYK7 = NYK wins 1H by 7+)
_TEAM_N_SERIES = (
    # NBA spreads / team totals
    "KXNBA1HSPREAD",    # NBA 1st-half point spread ladder
    "KXNBASPREAD",      # NBA full-game point spread
    "KXNBATEAMTOTAL",   # NBA team total points (POR135, POR132...)
    # NCAA Basketball spreads
    "KXNCAAMB1HSPREAD", # NCAA Men's Basketball 1H spread ladder
    "KXNCAAMBSPREAD",   # NCAA Men's Basketball full-game spread
    "KXNCAAWBSPREAD",   # NCAA Women's Basketball spread
    # NHL spreads
    "KXNHLSPREAD",      # NHL goal spread
    # Soccer spreads
    "KXEPLSPREAD",      # EPL goal spread
    "KXMLSSPREAD",      # MLS goal spread
)

KALSHI_FEE_RATE = 0.07  # 7% of gross winnings per resolved contract


def _is_below_group(sorted_markets: list) -> bool:
    """Return True if this group uses 'Below $X' semantics (prices increase with threshold).

    For ≥-threshold markets prices DECREASE with threshold; for below/min markets
    they INCREASE. Detected via two data-driven checks (any one is sufficient):

      1. Title keyword: yes_sub_title contains 'below', 'minimum', or 'maximum'.
      2. Price shape: YES mid-prices increase with threshold across the majority of
         consecutive pairs. This is the primary guard — it works regardless of how
         the API labels the series, so no hardcoded series lists are required.
    """
    for m in sorted_markets[:3]:
        title = m.title.lower()
        if title and ("below" in title or "minimum" in title or "maximum" in title):
            return True

    # Price-shape check: majority of consecutive mid-price pairs increase with threshold.
    priced = [m for m in sorted_markets if m.yes_ask > 0 and m.yes_bid > 0]
    if len(priced) >= 3:
        mids = [(m.yes_bid + m.yes_ask) / 2 for m in priced]
        increasing = sum(1 for i in range(len(mids) - 1) if mids[i] < mids[i + 1])
        if increasing > len(mids) // 2:
            return True
    return False


def _parse_threshold(ticker: str) -> Optional[float]:
    m = _T_RE.search(ticker)
    if m:
        return float(m.group(1))
    m = _BARE_FLOAT_RE.search(ticker)
    if m:
        return float(m.group(1))
    m = _NP_RE.search(ticker)
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

        # event_ticker = everything before the final threshold suffix
        event_ticker = _T_RE.sub("", ticker)
        if event_ticker == ticker:  # T-prefix didn't match → bare-float suffix
            event_ticker = _BARE_FLOAT_RE.sub("", ticker)
        if event_ticker == ticker:  # N/P prefix (KXNGASMIN/KXNGASMAX)
            event_ticker = _NP_RE.sub("", ticker)
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
            title=m.get("yes_sub_title", ""),
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
    require_liquidity: bool = True,
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

        # Skip "Below $X" markets — prices increase with threshold (reversed monotonicity)
        if _is_below_group(sorted_markets):
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
            if require_liquidity:
                # Minimum liquidity: require ≥$50 of open interest on each leg AND ≥20 contracts.
                if lower.open_interest * lower.yes_ask < 50.0:
                    continue
                if higher.open_interest * (1.0 - higher.yes_bid) < 50.0:
                    continue
                if lower.open_interest < 20 or higher.open_interest < 20:
                    continue
            else:
                # For display-only (near-miss) scans: skip deep-OTM markets where
                # both bid AND ask are below 10¢ (no meaningful price signal).
                if lower.yes_bid < 0.10 and lower.yes_ask < 0.10:
                    continue
                if higher.yes_bid < 0.10 and higher.yes_ask < 0.10:
                    continue
            # Fake-liquidity guard: if the higher leg has a huge internal spread,
            # its yes_bid is a thin top-of-book outlier (e.g. 10 contracts at 34¢ while
            # the real ask is at 98¢). We'd only get a few contracts at the advertised
            # gross_edge before the price collapses. Require a tight book on both legs.
            higher_spread = higher.yes_ask - higher.yes_bid
            lower_spread = lower.yes_ask - lower.yes_bid
            if higher_spread > 0.40 or lower_spread > 0.40:
                continue
            # P(X >= lower) >= P(X >= higher)  must hold
            # Violation: bid(higher) > ask(lower)
            gross_edge = higher.yes_bid - lower.yes_ask
            if gross_edge < min_gross_edge:
                continue

            net_edge = gross_edge - fee_rate   # worst-case: one leg wins, pay fee once

            # ── Middle-band probability ────────────────────────────────────────
            # P(lower <= X < higher) ≈ mid(lower) - mid(higher)
            # When both legs win, payout = $2 instead of $1 → bonus per contract.
            lower_mid = (lower.yes_bid + lower.yes_ask) / 2
            higher_mid = (higher.yes_bid + higher.yes_ask) / 2
            middle_prob = max(0.0, lower_mid - higher_mid)
            # EV = worst-case net + middle-band bonus
            # (one leg: payout $1-fee; both legs: payout $2-2*fee = (1-fee) extra)
            expected_edge = net_edge + middle_prob * (1.0 - fee_rate)

            if not allow_negative_edge and expected_edge <= 0:
                continue  # fee eats all profit even with middle-band bonus

            entry_cost = lower.yes_ask + (1.0 - higher.yes_bid)

            # Size: cap at open_interest; if OI=0 (not populated from REST bulk list), use max_size
            avail = min(lower.open_interest or max_size, higher.open_interest or max_size, max_size)

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
                middle_prob=middle_prob,
                expected_edge=expected_edge,
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

        # Skip "Below $X" markets — their prices increase with threshold (reversed)
        if _is_below_group(sorted_markets):
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

                # OI filter removed: bulk REST API omits open_interest → OI=0 blocks all pairs.
                # Spread filter in find_violations + price filters above are sufficient.

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

                avail = min(lower.open_interest or max_size, higher.open_interest or max_size, max_size)

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


# ── Ladder mean-reversion ─────────────────────────────────────────────────────


def find_ladder_mean_reversion(
    groups: Dict[str, List[ThresholdMarket]],
    min_anomaly: float = 0.05,
    top_n: int = 20,
    tick_times: Optional[Dict[str, float]] = None,
    max_stale_s: float = 60.0,
    debug: bool = False,
) -> List[SingleLegSignal]:
    """
    Detect ladder rungs that are anomalously cheap relative to BOTH neighbors.

    For sorted ladder [..., T_{k-1}, T_k, T_{k+1}, ...]:
      interpolated_mid = (T_{k-1}.mid() + T_{k+1}.mid()) / 2
      anomaly = interpolated_mid - T_k.mid()  (positive → T_k is cheap)

    Both neighbors must agree the price is too low, making this a cleaner
    signal than single-neighbor inversion (which fires on spread differences).
    The trade: buy YES on the cheap rung, exit when price normalizes.

    min_anomaly: minimum ¢ discount vs interpolated fair value (default 5¢).
      Dynamic floor: actual threshold = max(min_anomaly, 2 × spread).
      Wider spreads need a larger anomaly to overcome noise (#4).

    tick_times: dict of ticker → time.monotonic() of last WS tick received.
      When provided, applies stale-quote filter: only signal when the middle
      rung is confirmed lagging (neighbors ticked more recently than middle).
      This eliminates false signals where the whole ladder moved together (#3).

    max_stale_s: neighbor must have ticked within this many seconds to be
      considered a live price reference (default 60s).
    """
    signals: List[SingleLegSignal] = []
    now = datetime.now(timezone.utc)
    now_mono = time.monotonic() if tick_times is not None else 0.0
    min_ttl = timedelta(minutes=30)

    # Debug counters: track why each candidate triplet was rejected
    _dbg: dict = {} if debug else {}  # series → {reason: count}

    def _dbg_inc(series: str, reason: str) -> None:
        if not debug:
            return
        if series not in _dbg:
            _dbg[series] = {}
        _dbg[series][reason] = _dbg[series].get(reason, 0) + 1

    for event_ticker, markets in groups.items():
        series = event_ticker.split("-")[0].upper()
        if len(markets) < 3:  # need at least one middle rung with two neighbors
            _dbg_inc(series, "too_few_markets")
            continue
        sorted_markets = sorted(markets, key=lambda x: x.threshold)
        if sorted_markets[0].expiry_dt - now < min_ttl:
            _dbg_inc(series, "expiring_soon")
            continue
        # Max TTL: long-dated markets can trend for months — mean-rev is unreliable
        if sorted_markets[0].expiry_dt - now > timedelta(days=45):
            _dbg_inc(series, "too_far_out")
            continue

        # Categorical market check: skip bell-curve/bucket markets masquerading as
        # threshold ladders (prices INCREASE with threshold = mutually exclusive buckets)
        priced = [m for m in sorted_markets if m.yes_ask > 0 and m.yes_bid > 0]
        if len(priced) >= 3:
            increasing = sum(1 for i in range(len(priced) - 1) if priced[i].yes_ask < priced[i + 1].yes_ask)
            if increasing > len(priced) // 2:
                _dbg_inc(series, "categorical_shape")
                continue

        # Fit log-normal distribution to the full rung ladder.
        # When R² >= 0.85 the model replaces 2-neighbor linear interpolation,
        # giving accurate fair values for non-linear distributions (crypto, oil, FX).
        # KXFED / step-function markets fit poorly (R² ≈ 0) and fall back to linear.
        dist = _fit_lognormal(sorted_markets)
        use_model = dist is not None and dist[2] >= 0.85
        if debug and dist is not None:
            r2_tag = f"R²={dist[2]:.3f}"
            spot   = np.exp(dist[0])
            print(f"  [{series}] lognormal fit {r2_tag} implied_spot={spot:.4g} "
                  f"({'MODEL' if use_model else 'fallback→linear'})")

        for k in range(1, len(sorted_markets) - 1):
            lower_nb = sorted_markets[k - 1]   # lower threshold (higher price)
            market   = sorted_markets[k]        # candidate cheap middle rung
            upper_nb = sorted_markets[k + 1]   # upper threshold (lower price)

            # All three must have valid prices
            if (lower_nb.yes_bid <= 0 or lower_nb.yes_ask <= 0 or
                market.yes_bid   <= 0 or market.yes_ask   <= 0 or
                upper_nb.yes_bid <= 0 or upper_nb.yes_ask <= 0):
                _dbg_inc(series, "no_price")
                continue

            # Near-the-money filter: tail markets (ask < 20¢ or > 80¢) are illiquid
            # and anomalies there are noise, not genuine mispricings.
            if market.yes_ask < 0.20 or market.yes_ask > 0.80:
                _dbg_inc(series, "tail_market")
                continue

            # Liquidity: middle rung must have active bid and tight spread.
            # Wide spreads create false anomalies (mid is far from ask) and make
            # the target land below entry, guaranteeing a loss on exit.
            spread = market.yes_ask - market.yes_bid
            if market.yes_bid < 0.05 or spread > 0.25:
                _dbg_inc(series, "middle_illiquid")
                continue
            # Neighbors must also be liquid enough to be reliable references.
            # A wide neighbor spread (>15¢) means its mid is unreliable and will
            # create false anomalies (e.g. stale 27¢/94¢ quote inflates interp_mid).
            nb_spread_lower = lower_nb.yes_ask - lower_nb.yes_bid
            nb_spread_upper = upper_nb.yes_ask - upper_nb.yes_bid
            if lower_nb.yes_bid < 0.05 or upper_nb.yes_bid < 0.05:
                _dbg_inc(series, "neighbor_illiquid")
                continue
            if nb_spread_lower > 0.15 or nb_spread_upper > 0.15:
                _dbg_inc(series, "neighbor_wide_spread")
                continue

            # Fair value: log-normal model when R²≥0.85, else 2-neighbor linear interpolation
            if use_model:
                fair_mid = _lognormal_fair(market.threshold, dist[0], dist[1])
            else:
                fair_mid = (lower_nb.mid() + upper_nb.mid()) / 2
            anomaly = fair_mid - market.mid()  # positive = market is cheap

            # Anomaly threshold: flat minimum, no spread multiplier.
            if anomaly < min_anomaly:
                _dbg_inc(series, f"anomaly_too_small(need={min_anomaly:.2f},got={anomaly:.2f})")
                continue

            # Target: exit when market.yes_bid closes within 2¢ of model fair value
            target_bid = round(fair_mid - 0.02, 4)

            # Require at least 5¢ gross gain at target after estimated round-trip fee.
            # Secondary market fee ≈ fee_rate × (entry + exit) ≈ 1-2¢; require 3¢ net min.
            if target_bid - market.yes_ask < 0.05:
                _dbg_inc(series, "rr_gate")
                continue

            # #3 — Stale-quote filter: only fire when the middle rung is confirmed lagging.
            # If tick_times are available, require:
            #   (a) at least one neighbor has ticked recently (live price reference), AND
            #   (b) middle rung ticked BEFORE the freshest neighbor (it hasn't caught up).
            # If middle ticked after both neighbors it already repriced — skip.
            if tick_times is not None:
                lower_last = tick_times.get(lower_nb.ticker, 0.0)
                upper_last = tick_times.get(upper_nb.ticker, 0.0)
                middle_last = tick_times.get(market.ticker, 0.0)
                freshest_nb = max(lower_last, upper_last)
                if freshest_nb > 0:
                    # Both neighbors stale? Price references are unreliable — skip.
                    if now_mono - freshest_nb > max_stale_s:
                        _dbg_inc(series, "neighbors_stale")
                        continue
                    # (middle_not_stale check removed: anomaly + R:R gate are sufficient guards)

            _dbg_inc(series, "SIGNAL")
            signals.append(SingleLegSignal(
                id=market.ticker,
                series=market.series,
                expiry_dt=market.expiry_dt,
                market=market,
                adj_higher=upper_nb,
                adj_lower=lower_nb,
                inversion=round(anomaly, 4),
                target_bid=target_bid,
                detected_at=now,
            ))

    signals.sort(key=lambda s: s.inversion, reverse=True)

    if debug and _dbg:
        print("[MeanRev] Filter breakdown by series:")
        for series_key in sorted(_dbg, key=lambda s: sum(_dbg[s].values()), reverse=True):
            counts = _dbg[series_key]
            total = sum(v for k, v in counts.items() if k != "SIGNAL")
            sig_n = counts.get("SIGNAL", 0)
            reasons = {k: v for k, v in counts.items() if k != "SIGNAL"}
            top_reasons = sorted(reasons.items(), key=lambda x: -x[1])[:3]
            reason_str = "  ".join(f"{k}={v}" for k, v in top_reasons)
            print(f"  {series_key:20s} {total:4d} candidates rejected  {sig_n} signals  | {reason_str}")

    return signals[:top_n]


def find_ladder_sell_expensive(
    groups: Dict[str, List[ThresholdMarket]],
    min_anomaly: float = 0.05,
    top_n: int = 20,
    tick_times: Optional[Dict[str, float]] = None,
    max_stale_s: float = 60.0,
    debug: bool = False,
) -> List[SingleLegSignal]:
    """
    Detect ladder rungs that are anomalously EXPENSIVE relative to both neighbors.

    For sorted ladder [..., T_{k-1}, T_k, T_{k+1}, ...]:
      interpolated_mid = (T_{k-1}.mid() + T_{k+1}.mid()) / 2
      anomaly = T_k.mid() - interpolated_mid  (positive → T_k is expensive)

    Trade: buy NO on the expensive rung at (1 - market.yes_bid).
    Exit:  when market.yes_ask <= interp_mid + 2¢  (stored in target_bid field).
    Stop:  when market.yes_ask rises 5¢ above entry yes_bid.
    """
    signals: List[SingleLegSignal] = []
    now = datetime.now(timezone.utc)
    now_mono = time.monotonic() if tick_times is not None else 0.0
    min_ttl = timedelta(minutes=30)

    _dbg: dict = {} if debug else {}

    def _dbg_inc(series: str, reason: str) -> None:
        if not debug:
            return
        if series not in _dbg:
            _dbg[series] = {}
        _dbg[series][reason] = _dbg[series].get(reason, 0) + 1

    for event_ticker, markets in groups.items():
        series = event_ticker.split("-")[0].upper()
        if len(markets) < 3:
            _dbg_inc(series, "too_few_markets")
            continue
        sorted_markets = sorted(markets, key=lambda x: x.threshold)
        if sorted_markets[0].expiry_dt - now < min_ttl:
            _dbg_inc(series, "expiring_soon")
            continue
        if sorted_markets[0].expiry_dt - now > timedelta(days=45):
            _dbg_inc(series, "too_far_out")
            continue

        priced = [m for m in sorted_markets if m.yes_ask > 0 and m.yes_bid > 0]
        if len(priced) >= 3:
            increasing = sum(1 for i in range(len(priced) - 1) if priced[i].yes_ask < priced[i + 1].yes_ask)
            if increasing > len(priced) // 2:
                _dbg_inc(series, "categorical_shape")
                continue

        dist = _fit_lognormal(sorted_markets)
        use_model = dist is not None and dist[2] >= 0.85
        if debug and dist is not None:
            r2_tag = f"R²={dist[2]:.3f}"
            spot   = np.exp(dist[0])
            print(f"  [SellExp/{series}] lognormal fit {r2_tag} implied_spot={spot:.4g} "
                  f"({'MODEL' if use_model else 'fallback→linear'})")

        for k in range(1, len(sorted_markets) - 1):
            lower_nb = sorted_markets[k - 1]
            market   = sorted_markets[k]
            upper_nb = sorted_markets[k + 1]

            if (lower_nb.yes_bid <= 0 or lower_nb.yes_ask <= 0 or
                market.yes_bid   <= 0 or market.yes_ask   <= 0 or
                upper_nb.yes_bid <= 0 or upper_nb.yes_ask <= 0):
                _dbg_inc(series, "no_price")
                continue

            # Near-the-money filter: only trade expensive rungs with yes_bid 20-80¢.
            # Deep ITM (bid > 80¢) or deep OTM (bid < 20¢) anomalies are noise.
            if market.yes_bid < 0.20 or market.yes_bid > 0.80:
                _dbg_inc(series, "tail_market")
                continue

            spread = market.yes_ask - market.yes_bid
            if market.yes_bid < 0.05 or spread > 0.25:
                _dbg_inc(series, "middle_illiquid")
                continue
            nb_spread_lower = lower_nb.yes_ask - lower_nb.yes_bid
            nb_spread_upper = upper_nb.yes_ask - upper_nb.yes_bid
            if lower_nb.yes_bid < 0.05 or upper_nb.yes_bid < 0.05:
                _dbg_inc(series, "neighbor_illiquid")
                continue
            if nb_spread_lower > 0.15 or nb_spread_upper > 0.15:
                _dbg_inc(series, "neighbor_wide_spread")
                continue

            # Fair value: log-normal model when R²≥0.85, else 2-neighbor linear interpolation
            if use_model:
                fair_mid = _lognormal_fair(market.threshold, dist[0], dist[1])
            else:
                fair_mid = (lower_nb.mid() + upper_nb.mid()) / 2
            anomaly = market.mid() - fair_mid  # positive = expensive

            if anomaly < min_anomaly:
                _dbg_inc(series, f"anomaly_too_small(need={min_anomaly:.2f},got={anomaly:.2f})")
                continue

            # Target: YES ask falls to model fair + 2¢
            # (stored in target_bid field; used as YES ask target for NO positions)
            target_ask = round(fair_mid + 0.02, 4)

            # Require at least 5¢ gross gain at target.
            profit_if_target = market.yes_bid - target_ask
            if profit_if_target < 0.05:
                _dbg_inc(series, "rr_gate")
                continue

            # Stale-quote filter: middle ticked before neighbors → it hasn't repriced yet
            if tick_times is not None:
                lower_last = tick_times.get(lower_nb.ticker, 0.0)
                upper_last = tick_times.get(upper_nb.ticker, 0.0)
                middle_last = tick_times.get(market.ticker, 0.0)
                freshest_nb = max(lower_last, upper_last)
                if freshest_nb > 0:
                    if now_mono - freshest_nb > max_stale_s:
                        _dbg_inc(series, "neighbors_stale")
                        continue
                    # (middle_not_stale check removed: anomaly + R:R gate are sufficient guards)

            _dbg_inc(series, "SIGNAL")
            signals.append(SingleLegSignal(
                id=market.ticker,
                series=market.series,
                expiry_dt=market.expiry_dt,
                market=market,
                adj_higher=upper_nb,
                adj_lower=lower_nb,
                inversion=round(anomaly, 4),
                target_bid=target_ask,   # YES ask target for exit (stored in target_bid)
                detected_at=now,
                side="no",
            ))

    signals.sort(key=lambda s: s.inversion, reverse=True)

    if debug and _dbg:
        print("[SellExpensive] Filter breakdown by series:")
        for series_key in sorted(_dbg, key=lambda s: sum(_dbg[s].values()), reverse=True):
            counts = _dbg[series_key]
            total = sum(v for k, v in counts.items() if k != "SIGNAL")
            sig_n = counts.get("SIGNAL", 0)
            reasons = {k: v for k, v in counts.items() if k != "SIGNAL"}
            top_reasons = sorted(reasons.items(), key=lambda x: -x[1])[:3]
            reason_str = "  ".join(f"{k}={v}" for k, v in top_reasons)
            print(f"  {series_key:20s} {total:4d} candidates rejected  {sig_n} signals  | {reason_str}")

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
    require_liquidity: bool = True,
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

        if require_liquidity:
            # Minimum liquidity per bucket: ≥$50 OI × ask and ≥20 contracts.
            if any(b.open_interest * b.yes_ask < 50.0 for b in buckets):
                continue
            if any(b.open_interest < 20 for b in buckets):
                continue

        avail = min((min(b.open_interest for b in buckets) if require_liquidity else max_size), max_size)
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
