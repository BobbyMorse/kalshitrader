"""
Real-time price feed for digital option pricing.

Sources:
  Crypto (BTC, ETH, SOL): Binance WebSocket — sub-second push, no auth needed.
  Equities / FX / Commodities: Yahoo Finance batch quote API — real-time during
    market hours, polled every 30 s (one HTTP request covers all symbols).

Usage:
    feed = PriceFeed()
    await feed.start()          # launches Binance WS + first REST poll
    price = feed.get("BTC")     # returns latest price or None
    vol   = feed.vol("BTC")     # returns annual vol estimate
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Dict, List, Optional

import httpx

# ── Annual volatility estimates (used in Black-Scholes pricer) ───────────────
# Conservative realized-vol proxies; can be made adaptive later.
_DEFAULT_VOL: Dict[str, float] = {
    "BTC":    0.65,    # ~4.1 % / day
    "ETH":    0.80,    # ~5.0 % / day
    "SOL":    1.00,    # ~6.3 % / day
    "WTI":    0.35,    # ~2.2 % / day  (crude oil)
    "GOLD":   0.15,    # ~0.95% / day
    "SILVER": 0.25,    # ~1.6 % / day
    "SPX":    0.18,    # ~1.1 % / day  (S&P 500)
    "NDX":    0.22,    # ~1.4 % / day  (Nasdaq 100)
    "EURUSD": 0.08,    # ~0.5 % / day
    "GBPUSD": 0.08,
    "DXY":    0.08,    # Dollar index
    "TNX":    0.20,    # 10-year yield
    "NGAS":   0.55,    # Natural gas — very volatile
}

# ── Binance WebSocket streams ────────────────────────────────────────────────
_BINANCE_STREAMS = {
    "btcusdt@miniTicker": "BTC",
    "ethusdt@miniTicker": "ETH",
    "solusdt@miniTicker": "SOL",
}
_BINANCE_WS_URL = (
    "wss://stream.binance.com:9443/stream?streams="
    + "/".join(_BINANCE_STREAMS.keys())
)

# ── Yahoo Finance symbols (batch-fetched) ────────────────────────────────────
_YF_ASSETS: Dict[str, str] = {
    "^GSPC":    "SPX",
    "^NDX":     "NDX",
    "CL=F":     "WTI",
    "GC=F":     "GOLD",
    "SI=F":     "SILVER",
    "DX-Y.NYB": "DXY",
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "^TNX":     "TNX",
    "NG=F":     "NGAS",
}
_YF_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
_YF_SYMBOLS = ",".join(_YF_ASSETS.keys())
_YF_POLL_INTERVAL = 30.0   # seconds between REST polls
_YF_TIMEOUT       = 8.0    # HTTP timeout


class PriceFeed:
    """
    Maintains a live price dict updated by:
      • Binance WebSocket (crypto — continuous push)
      • Yahoo Finance REST (equities / FX / commodities — polled every 30 s)
    """

    def __init__(self) -> None:
        self._prices:    Dict[str, float] = {}
        self._updated:   Dict[str, float] = {}   # asset → monotonic timestamp
        self._tasks:     List[asyncio.Task] = []

    # ── Public API ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start background tasks. Call once on app startup."""
        await self._poll_yf()                              # warm up REST prices
        self._tasks.append(asyncio.create_task(self._binance_ws_loop()))
        self._tasks.append(asyncio.create_task(self._yf_poll_loop()))
        print(f"[PriceFeed] Started. Initial prices: "
              f"BTC={self._prices.get('BTC')} ETH={self._prices.get('ETH')} "
              f"SPX={self._prices.get('SPX')} WTI={self._prices.get('WTI')}")

    def get(self, asset: str) -> Optional[float]:
        """Return latest price for asset, or None if not yet received."""
        return self._prices.get(asset)

    def age(self, asset: str) -> float:
        """Seconds since last update for asset (large = stale)."""
        ts = self._updated.get(asset)
        return time.monotonic() - ts if ts else 9999.0

    def vol(self, asset: str) -> float:
        """Annual volatility estimate for asset."""
        return _DEFAULT_VOL.get(asset, 0.20)

    def snapshot(self) -> Dict[str, float]:
        """Return copy of current price dict (for logging / status)."""
        return dict(self._prices)

    # ── Binance WebSocket ────────────────────────────────────────────────────

    async def _binance_ws_loop(self) -> None:
        """Subscribe to combined mini-ticker stream. Reconnects on error."""
        import websockets  # type: ignore
        backoff = 2.0
        while True:
            try:
                async with websockets.connect(
                    _BINANCE_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    backoff = 2.0
                    async for raw in ws:
                        msg = json.loads(raw)
                        stream = msg.get("stream", "")
                        data   = msg.get("data", {})
                        asset  = _BINANCE_STREAMS.get(stream)
                        if asset and data.get("c"):
                            price = float(data["c"])
                            self._prices[asset]  = price
                            self._updated[asset] = time.monotonic()
            except Exception as exc:
                print(f"[PriceFeed] Binance WS error: {exc}. "
                      f"Reconnecting in {backoff:.0f}s …")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ── Yahoo Finance REST ───────────────────────────────────────────────────

    async def _yf_poll_loop(self) -> None:
        """Poll Yahoo Finance every _YF_POLL_INTERVAL seconds."""
        while True:
            await asyncio.sleep(_YF_POLL_INTERVAL)
            await self._poll_yf()

    async def _poll_yf(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=_YF_TIMEOUT) as client:
                resp = await client.get(
                    _YF_URL,
                    params={"symbols": _YF_SYMBOLS},
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if resp.status_code != 200:
                    return
                quotes = resp.json().get("quoteResponse", {}).get("result", [])
                for q in quotes:
                    sym   = q.get("symbol", "")
                    price = q.get("regularMarketPrice")
                    asset = _YF_ASSETS.get(sym)
                    if asset and price:
                        self._prices[asset]  = float(price)
                        self._updated[asset] = time.monotonic()
        except Exception as exc:
            print(f"[PriceFeed] Yahoo Finance poll error: {exc}")
