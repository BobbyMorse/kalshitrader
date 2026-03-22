"""
Real-time price feed for digital option pricing.

Sources:
  Crypto (BTC, ETH, SOL): Coinbase Exchange WebSocket — real-time push, no auth, US-accessible.
  Equities / FX / Commodities: Yahoo Finance batch quote API — real-time during
    market hours, polled every 30 s (one HTTP request covers all symbols).
  Crypto fallback: CoinGecko REST API — polled every 60 s if WS is down.

Usage:
    feed = PriceFeed()
    await feed.start()          # launches Coinbase WS + first REST poll
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

# ── Coinbase Exchange WebSocket ───────────────────────────────────────────────
# Public ticker channel, no auth required, works from US servers.
_COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
_COINBASE_PRODUCTS = {
    "BTC-USD": "BTC",
    "ETH-USD": "ETH",
    "SOL-USD": "SOL",
}

# ── CoinGecko REST fallback (crypto, polled every 60 s) ──────────────────────
_COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
_COINGECKO_IDS = {
    "bitcoin":  "BTC",
    "ethereum": "ETH",
    "solana":   "SOL",
}
_COINGECKO_POLL_INTERVAL = 60.0
_COINGECKO_TIMEOUT       = 8.0

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

# ── Open-Meteo weather data (NOAA-sourced, no auth required) ─────────────────
# Maps Kalshi series_ticker → (latitude, longitude, metric)
# metric: "rain" = monthly precipitation in inches, "snow" = monthly snowfall in inches
_WEATHER_SERIES: Dict[str, tuple] = {
    "KXRAINNYCM":  (40.71, -74.01,   "rain"),   # NYC (JFK area)
    "KXRAINLAXM":  (33.93, -118.40,  "rain"),   # LA (LAX area)
    "KXRAINDENM":  (39.86, -104.67,  "rain"),   # Denver (DIA area)
    "KXRAINSFOM":  (37.62, -122.38,  "rain"),   # SF (SFO area)
    "KXRAINMIA":   (25.80, -80.29,   "rain"),   # Miami (MIA area)
    "KXRAINNO":    (29.99, -90.26,   "rain"),   # New Orleans (MSY area)
    "KXDETSNOWM":  (42.21, -83.35,   "snow"),   # Detroit (DTW area)
    "KXDENSNOWMB": (39.86, -104.67,  "snow"),   # Denver (DIA area)
    "KXDCSNOWM":   (38.85, -77.04,   "snow"),   # DC (DCA area)
    "KXHOUSNOWM":  (29.98, -95.34,   "snow"),   # Houston (IAH area)
    "KXAUSSNOWM":  (30.19, -97.67,   "snow"),   # Austin (AUS area)
    "KXLAXSNOWM":  (33.93, -118.40,  "snow"),   # LA snow (rare)
    "KXDENSNOWM":  (39.86, -104.67,  "snow"),   # Denver monthly snow
    "KXSNOWNYM":   (40.71, -74.01,   "snow"),   # NYC monthly snow
}
_WEATHER_POLL_INTERVAL = 3600.0   # 1 hour — weather data changes slowly


class PriceFeed:
    """
    Maintains a live price dict updated by:
      • Coinbase Exchange WebSocket (crypto — continuous push, US-friendly)
      • CoinGecko REST fallback (crypto — polled every 60 s when WS is down)
      • Yahoo Finance REST (equities / FX / commodities — polled every 30 s)
    """

    def __init__(self) -> None:
        self._prices:    Dict[str, float] = {}
        self._updated:   Dict[str, float] = {}   # asset → monotonic timestamp
        self._tasks:     List[asyncio.Task] = []
        self._ws_healthy: bool = False

    # ── Public API ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start background tasks. Call once on app startup."""
        await self._poll_yf()                              # warm up REST prices
        await self._poll_coingecko()                       # warm up crypto prices
        await self._poll_weather()                         # warm up weather data
        self._tasks.append(asyncio.create_task(self._coinbase_ws_loop()))
        self._tasks.append(asyncio.create_task(self._yf_poll_loop()))
        self._tasks.append(asyncio.create_task(self._coingecko_poll_loop()))
        self._tasks.append(asyncio.create_task(self._weather_poll_loop()))
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

    # ── Coinbase Exchange WebSocket ──────────────────────────────────────────

    async def _coinbase_ws_loop(self) -> None:
        """Subscribe to Coinbase ticker channel. Reconnects on error."""
        import websockets  # type: ignore
        backoff = 2.0
        while True:
            try:
                async with websockets.connect(
                    _COINBASE_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    # Subscribe to ticker channel for BTC, ETH, SOL
                    sub = {
                        "type": "subscribe",
                        "product_ids": list(_COINBASE_PRODUCTS.keys()),
                        "channels": ["ticker"],
                    }
                    await ws.send(json.dumps(sub))
                    backoff = 2.0
                    self._ws_healthy = True
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") == "ticker":
                            product = msg.get("product_id", "")
                            asset   = _COINBASE_PRODUCTS.get(product)
                            price_s = msg.get("price")
                            if asset and price_s:
                                self._prices[asset]  = float(price_s)
                                self._updated[asset] = time.monotonic()
            except Exception as exc:
                self._ws_healthy = False
                print(f"[PriceFeed] Coinbase WS error: {exc}. "
                      f"Reconnecting in {backoff:.0f}s …")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ── CoinGecko REST fallback ───────────────────────────────────────────────

    async def _coingecko_poll_loop(self) -> None:
        """Poll CoinGecko every 60 s as fallback when WS is unhealthy."""
        while True:
            await asyncio.sleep(_COINGECKO_POLL_INTERVAL)
            # Always poll — acts as backup even when WS is running
            await self._poll_coingecko()

    async def _poll_coingecko(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=_COINGECKO_TIMEOUT) as client:
                resp = await client.get(
                    _COINGECKO_URL,
                    params={
                        "ids": ",".join(_COINGECKO_IDS.keys()),
                        "vs_currencies": "usd",
                    },
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if resp.status_code != 200:
                    print(f"[PriceFeed] CoinGecko HTTP {resp.status_code}")
                    return
                data = resp.json()
                for cg_id, asset in _COINGECKO_IDS.items():
                    price = data.get(cg_id, {}).get("usd")
                    if price:
                        # Only update if WS hasn't updated in last 90 s (prefer WS)
                        if self.age(asset) > 90.0:
                            self._prices[asset]  = float(price)
                            self._updated[asset] = time.monotonic()
        except Exception as exc:
            print(f"[PriceFeed] CoinGecko poll error: {exc}")

    # ── Yahoo Finance via yfinance library ──────────────────────────────────

    async def _yf_poll_loop(self) -> None:
        """Poll Yahoo Finance every _YF_POLL_INTERVAL seconds."""
        while True:
            await asyncio.sleep(_YF_POLL_INTERVAL)
            await self._poll_yf()

    async def _poll_yf(self) -> None:
        """Fetch prices using yfinance library (handles Yahoo auth automatically)."""
        try:
            import yfinance as yf
            loop = asyncio.get_event_loop()

            def _fetch_sync() -> Dict[str, float]:
                results: Dict[str, float] = {}
                try:
                    tickers = yf.Tickers(" ".join(_YF_ASSETS.keys()))
                    for sym, asset in _YF_ASSETS.items():
                        try:
                            p = tickers.tickers[sym].fast_info.last_price
                            if p and float(p) > 0:
                                results[asset] = float(p)
                        except Exception:
                            pass
                except Exception:
                    pass
                return results

            prices = await loop.run_in_executor(None, _fetch_sync)
            for asset, price in prices.items():
                self._prices[asset]  = price
                self._updated[asset] = time.monotonic()
            if prices:
                print(f"[PriceFeed] YF updated: {list(prices.keys())}")
        except Exception as exc:
            print(f"[PriceFeed] Yahoo Finance poll error: {exc}")

    # ── Open-Meteo weather (NOAA-sourced monthly precipitation) ─────────────

    async def _weather_poll_loop(self) -> None:
        """Poll open-meteo once per hour for weather accumulations."""
        while True:
            await asyncio.sleep(_WEATHER_POLL_INTERVAL)
            await self._poll_weather()

    async def _poll_weather(self) -> None:
        """
        Fetch month-to-date accumulated precip/snow AND forecast for remaining days
        from open-meteo (free, no auth, NOAA-sourced data).

        Stored as:
          WXMTD:{series}  = actual month-to-date total in inches (archive data)
          WXFWD:{series}  = forecast inches for rest of month (NWS forecast)
        """
        from datetime import datetime, timezone, timedelta as _td
        now_utc = datetime.now(timezone.utc)
        month_start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # Archive lags by ~1 day; use up to 2 days ago to be safe
        archive_end = (now_utc - _td(days=1)).strftime("%Y-%m-%d")
        month_start_str = month_start.strftime("%Y-%m-%d")
        today_str = now_utc.strftime("%Y-%m-%d")
        # End of month = first day of next month minus 1 day
        if now_utc.month == 12:
            next_month = now_utc.replace(year=now_utc.year + 1, month=1, day=1)
        else:
            next_month = now_utc.replace(month=now_utc.month + 1, day=1)
        end_of_month_str = (next_month - _td(days=1)).strftime("%Y-%m-%d")

        updated = []
        async with httpx.AsyncClient(timeout=15.0) as client:
            for series, (lat, lon, metric) in _WEATHER_SERIES.items():
                try:
                    # ── Archive: actual month-to-date ───────────────────────
                    if archive_end >= month_start_str:
                        r = await client.get(
                            "https://archive-api.open-meteo.com/v1/archive",
                            params={
                                "latitude": lat, "longitude": lon,
                                "start_date": month_start_str,
                                "end_date": archive_end,
                                "daily": "precipitation_sum,snowfall_sum",
                                "precipitation_unit": "inch",
                                "timezone": "UTC",
                            },
                        )
                        mtd = 0.0
                        if r.status_code == 200:
                            daily = r.json().get("daily", {})
                            if metric == "rain":
                                mtd = sum(v or 0.0 for v in daily.get("precipitation_sum", []))
                            else:
                                # snowfall_sum is in cm; convert to inches
                                mtd = sum((v or 0.0) * 0.3937 for v in daily.get("snowfall_sum", []))
                        self._prices[f"WXMTD:{series}"] = round(mtd, 3)
                        self._updated[f"WXMTD:{series}"] = time.monotonic()

                    # ── Forecast: remaining days in month ───────────────────
                    r2 = await client.get(
                        "https://api.open-meteo.com/v1/forecast",
                        params={
                            "latitude": lat, "longitude": lon,
                            "daily": "precipitation_sum,snowfall_sum",
                            "precipitation_unit": "inch",
                            "forecast_days": 16,
                            "timezone": "UTC",
                        },
                    )
                    fwd = 0.0
                    if r2.status_code == 200:
                        daily2 = r2.json().get("daily", {})
                        dates = daily2.get("time", [])
                        precip = daily2.get("precipitation_sum", [])
                        snow   = daily2.get("snowfall_sum", [])
                        for i, d in enumerate(dates):
                            if today_str <= d <= end_of_month_str:
                                if metric == "rain":
                                    fwd += (precip[i] or 0.0) if i < len(precip) else 0.0
                                else:
                                    fwd += ((snow[i] or 0.0) * 0.3937) if i < len(snow) else 0.0
                    self._prices[f"WXFWD:{series}"] = round(fwd, 3)
                    self._updated[f"WXFWD:{series}"] = time.monotonic()

                    mtd_v = self._prices.get(f"WXMTD:{series}", 0.0)
                    fwd_v = self._prices.get(f"WXFWD:{series}", 0.0)
                    updated.append(f"{series}(mtd={mtd_v:.2f}\" fwd={fwd_v:.2f}\")")
                    await asyncio.sleep(0.3)   # be polite to open-meteo

                except Exception as exc:
                    print(f"[PriceFeed] Weather poll error {series}: {exc}")

        if updated:
            print(f"[PriceFeed] Weather: {', '.join(updated)}")
