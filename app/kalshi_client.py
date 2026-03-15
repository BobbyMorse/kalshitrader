"""
Kalshi REST API v2 client.

Auth priority (first that works wins):
  1. RSA key pair  – KALSHI_API_KEY_ID + private_key.pem
  2. Email/password – KALSHI_EMAIL + KALSHI_PASSWORD  →  session token
  3. API key as bearer token  (fallback: tries the key_id directly as a token)
  4. Mock mode  – no credentials at all

The session token from method 2 is also used to authenticate the WebSocket feed.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False


# ── Mock data used when no credentials are configured ────────────────────────

_MOCK_MARKETS: List[Dict] = [
    {
        "ticker": "FED-25JUN-CUT",
        "title": "Fed cuts rates by June 2025",
        "yes_bid": 42, "yes_ask": 45, "no_bid": 54, "no_ask": 57,
        "volume": 8200, "open_interest": 3100, "status": "open",
        "close_time": "2025-06-18T18:00:00Z",
    },
    {
        "ticker": "CPI-APR-ABOVE32",
        "title": "Next CPI print above 3.2%",
        "yes_bid": 34, "yes_ask": 37, "no_bid": 62, "no_ask": 65,
        "volume": 14500, "open_interest": 6700, "status": "open",
        "close_time": "2025-05-13T12:30:00Z",
    },
    {
        "ticker": "BTC-100K-Q2",
        "title": "BTC above $100k this quarter",
        "yes_bid": 56, "yes_ask": 60, "no_bid": 39, "no_ask": 43,
        "volume": 22000, "open_interest": 9800, "status": "open",
        "close_time": "2025-06-30T21:00:00Z",
    },
    {
        "ticker": "SPX-5500-MAY",
        "title": "S&P 500 above 5,500 in May",
        "yes_bid": 61, "yes_ask": 64, "no_bid": 35, "no_ask": 38,
        "volume": 31000, "open_interest": 12400, "status": "open",
        "close_time": "2025-05-30T21:00:00Z",
    },
    {
        "ticker": "UNEMP-APR-BELOW4",
        "title": "April unemployment rate below 4%",
        "yes_bid": 68, "yes_ask": 71, "no_bid": 28, "no_ask": 31,
        "volume": 5400, "open_interest": 2100, "status": "open",
        "close_time": "2025-05-02T12:30:00Z",
    },
]


class KalshiClient:
    def __init__(
        self,
        host: str,
        api_prefix: str = "/trade-api/v2",
        api_key_id: Optional[str] = None,
        private_key_path: Optional[str] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
        private_key_content: Optional[str] = None,  # PEM string (for Fly.io / env-var deployments)
    ):
        self.host = host.rstrip("/")
        self.api_prefix = api_prefix
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.email = email
        self.password = password
        self._private_key_content = private_key_content

        self._token: Optional[str] = None
        self._private_key = None
        self._auth_method: str = "none"

        self.mock_mode = not (api_key_id or (email and password))

        if api_key_id and _CRYPTO_AVAILABLE:
            self._load_private_key()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _load_private_key(self) -> None:
        # Priority: inline PEM content (env var) > file path
        pem_bytes: Optional[bytes] = None
        if self._private_key_content:
            # Fly.io / shell env vars sometimes store literal \n instead of real newlines
            content = self._private_key_content.replace("\\n", "\n").replace("\r\n", "\n")
            pem_bytes = content.encode()
            print(f"[KalshiClient] PEM env var present, length={len(content)}, "
                  f"starts_with_header={content.strip().startswith('-----BEGIN')}")
        elif self.private_key_path:
            path = os.path.expanduser(self.private_key_path)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    pem_bytes = f.read()
            else:
                print(f"[KalshiClient] private_key.pem not found at {path} – will try other auth")
                return
        else:
            return
        try:
            self._private_key = serialization.load_pem_private_key(pem_bytes, password=None)
            self._auth_method = "rsa"
            print("[KalshiClient] RSA private key loaded")
        except Exception as exc:
            print(f"[KalshiClient] Could not load private key: {exc}")

    def _rsa_sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        # Kalshi uses RSA-PSS with SHA256 (NOT PKCS1v15). Sign path WITHOUT query params.
        sign_path = path.split("?")[0]
        msg = (timestamp + method.upper() + sign_path).encode()
        sig = self._private_key.sign(
            msg,
            asym_padding.PSS(
                mgf=asym_padding.MGF1(hashes.SHA256()),
                salt_length=asym_padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def _auth_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self._private_key and self.api_key_id:
            ts = str(int(time.time() * 1000))
            headers["KALSHI-ACCESS-KEY"] = self.api_key_id
            headers["KALSHI-ACCESS-SIGNATURE"] = self._rsa_sign(ts, method, path, body)
            headers["KALSHI-ACCESS-TIMESTAMP"] = ts
        elif self._token:
            headers["Authorization"] = self._token  # Kalshi session token (no Bearer prefix)
        elif self.api_key_id:
            headers["Authorization"] = f"Bearer {self.api_key_id}"
        return headers

    def _url(self, endpoint: str) -> str:
        return f"{self.host}{self.api_prefix}{endpoint}"

    def _path(self, endpoint: str) -> str:
        return f"{self.api_prefix}{endpoint}"

    # ── Auth ─────────────────────────────────────────────────────────────────

    async def login(self) -> Tuple[bool, str]:
        """Try all auth methods in priority order. Returns (success, method)."""
        if self.mock_mode:
            return False, "mock"

        # 1. RSA key already loaded
        if self._private_key and self.api_key_id:
            try:
                await self.get_balance()
                print("[KalshiClient] RSA auth verified")
                self._auth_method = "rsa"
                return True, "rsa"
            except Exception as exc:
                print(f"[KalshiClient] RSA auth test failed: {exc}")

        # 2. Email / password → session token
        if self.email and self.password:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        self._url("/login"),
                        json={"email": self.email, "password": self.password},
                    )
                    if resp.status_code == 200:
                        self._token = resp.json().get("token", "")
                        if self._token:
                            print("[KalshiClient] Email/password login OK")
                            self._auth_method = "token"
                            return True, "token"
            except Exception as exc:
                print(f"[KalshiClient] Email/password login failed: {exc}")

        # 3. Try api_key_id as bearer token
        if self.api_key_id:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        self._url("/portfolio/balance"),
                        headers={"Authorization": f"Bearer {self.api_key_id}",
                                 "Content-Type": "application/json"},
                    )
                    if resp.status_code == 200:
                        print("[KalshiClient] API key bearer auth OK")
                        self._auth_method = "bearer"
                        return True, "bearer"
                    else:
                        print(
                            f"[KalshiClient] Auth failed (HTTP {resp.status_code}). "
                            "To fix: add KALSHI_EMAIL + KALSHI_PASSWORD to .env, "
                            "OR download private_key.pem from kalshi.com/account/api-keys."
                        )
            except Exception as exc:
                print(f"[KalshiClient] Bearer test failed: {exc}")

        return False, "none"

    def get_ws_token(self) -> Optional[str]:
        """Return token for WebSocket auth header (email/password auth only)."""
        if self._token:
            return self._token
        if self._auth_method == "bearer" and self.api_key_id:
            return self.api_key_id
        return None

    def get_ws_headers(self, ws_path: str = "/trade-api/ws/v2") -> Dict[str, str]:
        """Return fresh RSA-signed headers for a WebSocket connection."""
        if self._private_key and self.api_key_id:
            ts = str(int(time.time() * 1000))
            return {
                "KALSHI-ACCESS-KEY": self.api_key_id,
                "KALSHI-ACCESS-SIGNATURE": self._rsa_sign(ts, "GET", ws_path),
                "KALSHI-ACCESS-TIMESTAMP": ts,
            }
        token = self.get_ws_token()
        if token:
            return {"Authorization": token}
        return {}

    def get_ws_host(self) -> str:
        return self.host.replace("https://", "wss://").replace("http://", "ws://")

    # ── Markets ───────────────────────────────────────────────────────────────

    # Series that produce simple, liquid binary markets worth trading
    BINARY_SERIES: List[str] = [
        # ── US Equity indices ────────────────────────────────────────────────
        "KXINX",        # S&P 500 intraday (bucket + threshold)
        "KXSPX",        # S&P 500 daily close (threshold)
        "KXNDAQ",       # Nasdaq 100
        "KXDOW",        # Dow Jones Industrial Average
        "KXRUS2K",      # Russell 2000
        "KXVIX",        # VIX fear index (threshold levels)
        # ── Crypto ──────────────────────────────────────────────────────────
        "KXBTCD",       # BTC daily close (bucket + threshold)
        "KXBTCH",       # BTC hourly
        "KXETHD",       # ETH daily (bucket + threshold)
        "KXSOLANA",     # Solana daily
        # ── Commodities ─────────────────────────────────────────────────────
        "KXGOLD",       # Gold price (threshold)
        "KXOIL",        # Crude oil (WTI, threshold)
        "KXWTI",        # WTI crude (alternative series name)
        "KXNGAS",       # Natural gas
        "KXSILVER",     # Silver
        # ── FX / Rates ──────────────────────────────────────────────────────
        "KXDXY",        # Dollar index (threshold)
        "KXUS10Y",      # 10-year Treasury yield (threshold)
        "KXUS2Y",       # 2-year Treasury yield
        "KXEURUSD",     # EUR/USD exchange rate
        "KXGBPUSD",     # GBP/USD
        "KXJPYUSD",     # JPY/USD
        # ── Macro / Economic data ────────────────────────────────────────────
        "KXFED",        # Fed rate decisions
        "KXCPI",        # CPI prints (threshold buckets)
        "KXJOB",        # Jobs / unemployment
        "KXGDP",        # GDP growth
        "KXPCE",        # PCE inflation
        "KXRETAIL",     # Retail sales
        "KXHOUSING",    # Housing starts / permits
        # ── Sports ──────────────────────────────────────────────────────────
        "KXNBAGM",      # NBA game winners
        "KXNBAPLAYER",  # NBA player props
        "KXNHLGM",      # NHL game winners
        "KXMLBGM",      # MLB game winners
        "KXNCAABGM",    # NCAA basketball
        "KXNFLGM",      # NFL game winners
        "KXNFL",        # NFL props
        "KXMMA",        # MMA
        "KXSOCCER",     # Soccer
        "KXPGA",        # PGA Tour
        "KXTENNIS",     # Tennis
        # ── Politics / Policy ────────────────────────────────────────────────
        "KXPRES",       # Presidential approval
        "KXCONG",       # Congress
        "KXECON",       # Economic indicators
        "KXGOVTSHUTLENGTH",  # Government shutdown duration
        "KXDEBTCEILING",     # Debt ceiling
        "KXTRUMP",           # Trump approval / policy
        "KXTARIFF",          # Tariffs
        "KXIMMIGRATION",     # Immigration policy
        "KXHEALTHCARE",      # Healthcare
        "KXCLIMATE",         # Climate policy
    ]

    async def get_markets(self, status: str = "open") -> List[Dict]:
        if self.mock_mode:
            return _MOCK_MARKETS
        endpoint = "/markets"
        path = self._path(endpoint)
        all_markets: List[Dict] = []
        seen: set = set()

        async def fetch_page(client: httpx.AsyncClient, params: Dict[str, Any]) -> Tuple[List[Dict], Optional[str]]:
            try:
                headers = self._auth_headers("GET", path)
                resp = await client.get(self._url(endpoint), params=params, headers=headers)
                if resp.status_code not in (200,):
                    return [], None
                data = resp.json()
                return data.get("markets", []), data.get("cursor")
            except Exception:
                return [], None

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # 1. Paginate through ALL open markets (gets sports, politics, news, etc.)
                # Skip KXMVE parlays — don't count them toward the page limit.
                _SKIP_PREFIXES = ("KXMVECROSSCATEGORY", "KXMVESPORTSMULTIGAMEEXTENDED")
                cursor: Optional[str] = None
                general_count = 0
                total_pages = 0
                while total_pages < 300:  # no general cap — fetch everything (up to 60k)
                    params: Dict[str, Any] = {"status": status, "limit": 200}
                    if cursor:
                        params["cursor"] = cursor
                    page, cursor = await fetch_page(client, params)
                    total_pages += 1
                    for m in page:
                        t = m.get("ticker", "")
                        if t and t not in seen:
                            seen.add(t)
                            all_markets.append(m)
                            # Only count non-parlay markets toward the cap
                            if not any(t.startswith(p) for p in _SKIP_PREFIXES):
                                general_count += 1
                    if not cursor or len(page) < 200:
                        break

                # 2. Also do full coverage of structural-analysis series (complete strike chains)
                import asyncio as _asyncio

                async def fetch_series_all(series: str) -> List[Dict]:
                    results: List[Dict] = []
                    cur: Optional[str] = None
                    while True:
                        p: Dict[str, Any] = {"status": status, "limit": 1000, "series_ticker": series}
                        if cur:
                            p["cursor"] = cur
                        pg, cur = await fetch_page(client, p)
                        results.extend(pg)
                        if not cur or len(pg) < 1000:
                            break
                    return results

                series_results = await _asyncio.gather(
                    *[fetch_series_all(s) for s in self.BINARY_SERIES]
                )
                for page in series_results:
                    for m in page:
                        t = m.get("ticker", "")
                        if t and t not in seen:
                            seen.add(t)
                            all_markets.append(m)

            print(f"[KalshiClient] Fetched {len(all_markets)} total markets")
            return all_markets
        except Exception as exc:
            print(f"[KalshiClient] get_markets error: {exc}")
            return _MOCK_MARKETS

    async def get_market(self, ticker: str) -> Dict:
        if self.mock_mode:
            return next((m for m in _MOCK_MARKETS if m["ticker"] == ticker), {})
        endpoint = f"/markets/{ticker}"
        headers = self._auth_headers("GET", self._path(endpoint))
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(self._url(endpoint), headers=headers)
            resp.raise_for_status()
            return resp.json().get("market", {})

    async def get_market_prices_bulk(self, tickers: List[str], concurrency: int = 40) -> Dict[str, Dict]:
        """Fetch current bid/ask for a list of tickers in parallel batches.
        Returns dict of ticker -> {yes_bid, yes_ask, no_bid, no_ask}."""
        if self.mock_mode or not tickers:
            return {}
        import asyncio as _asyncio
        results: Dict[str, Dict] = {}
        sem = _asyncio.Semaphore(concurrency)

        async def fetch_one(client: httpx.AsyncClient, ticker: str) -> None:
            async with sem:
                try:
                    endpoint = f"/markets/{ticker}"
                    headers = self._auth_headers("GET", self._path(endpoint))
                    resp = await client.get(self._url(endpoint), headers=headers, timeout=8)
                    if resp.status_code == 200:
                        m = resp.json().get("market", {})
                        if m.get("yes_bid_dollars") is not None or m.get("yes_bid") is not None:
                            results[ticker] = m
                except Exception:
                    pass

        async with httpx.AsyncClient(timeout=10) as client:
            await _asyncio.gather(*[fetch_one(client, t) for t in tickers])

        return results

    # ── Portfolio ─────────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        if self.mock_mode:
            return 0.0
        endpoint = "/portfolio/balance"
        headers = self._auth_headers("GET", self._path(endpoint))
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(self._url(endpoint), headers=headers)
            resp.raise_for_status()
            return resp.json().get("balance", 0) / 100

    async def get_positions(self) -> List[Dict]:
        if self.mock_mode:
            return []
        endpoint = "/portfolio/positions"
        headers = self._auth_headers("GET", self._path(endpoint))
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(self._url(endpoint), headers=headers)
                resp.raise_for_status()
                return resp.json().get("market_positions", [])
        except Exception:
            return []

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price_cents: int,
    ) -> Dict[str, Any]:
        if self.mock_mode:
            return {"order_id": str(uuid.uuid4()), "status": "resting"}
        endpoint = "/portfolio/orders"
        path = self._path(endpoint)
        body_dict = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "type": "limit",
            "action": action,
            "side": side,
            "count": count,
            "yes_price": price_cents if side == "yes" else 100 - price_cents,
            "no_price": price_cents if side == "no" else 100 - price_cents,
        }
        body = json.dumps(body_dict)
        headers = self._auth_headers("POST", path, body)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(self._url(endpoint), content=body, headers=headers)
            resp.raise_for_status()
            return resp.json().get("order", {})

    async def cancel_order(self, order_id: str) -> bool:
        if self.mock_mode:
            return True
        endpoint = f"/portfolio/orders/{order_id}"
        headers = self._auth_headers("DELETE", self._path(endpoint))
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(self._url(endpoint), headers=headers)
            return resp.status_code == 200

    async def get_orders(self, status: str = "resting") -> List[Dict]:
        if self.mock_mode:
            return []
        endpoint = "/portfolio/orders"
        headers = self._auth_headers("GET", self._path(endpoint))
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    self._url(endpoint), params={"status": status}, headers=headers
                )
                resp.raise_for_status()
                return resp.json().get("orders", [])
        except Exception:
            return []
