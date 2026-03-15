"""
Kalshi real-time WebSocket feed.

Connects to wss://api.elections.kalshi.com/trade-api/ws/v2
Subscribes to the 'ticker' channel for the given market tickers.
Calls on_tick(ticker, yes_bid_cents, yes_ask_cents) on every price update.

Reconnects automatically on disconnect.
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable, Coroutine, Dict, List, Optional, Set


class KalshiFeed:
    WS_PATH = "/trade-api/ws/v2"

    def __init__(
        self,
        ws_host: str,                               # wss://api.elections.kalshi.com
        get_headers: Callable[[], Dict[str, str]],  # called fresh on each connect
        on_tick: Callable[[str, int, int], Coroutine],  # async (ticker, bid, ask)
    ):
        self._host = ws_host.rstrip("/")
        self._get_headers = get_headers
        self._on_tick = on_tick

        self._subscribed: Set[str] = set()
        self._pending: Set[str] = set()
        self._ws = None
        self._running = False
        self._cmd_id = 0

    # ── Public API ────────────────────────────────────────────────────────────

    async def subscribe(self, tickers: List[str]) -> None:
        """Add tickers. Sends command immediately if connected, else queues."""
        new = [t for t in tickers if t not in self._subscribed and t not in self._pending]
        if not new:
            return
        if self._ws is not None:
            await self._send_sub(new)
            self._subscribed.update(new)
        else:
            self._pending.update(new)

    async def start(self) -> None:
        """Run forever, reconnecting on errors. Call with asyncio.create_task()."""
        self._running = True
        while self._running:
            try:
                await self._run()
            except Exception as exc:
                if self._running:
                    print(f"[KalshiFeed] Disconnected ({exc}), reconnecting in 5s...")
                    await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False
        ws = self._ws
        if ws is not None:
            asyncio.create_task(ws.close())

    # ── Internals ─────────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._cmd_id += 1
        return self._cmd_id

    async def _send_sub(self, tickers: List[str]) -> None:
        if not tickers or self._ws is None:
            return
        for i in range(0, len(tickers), 500):
            chunk = tickers[i : i + 500]
            await self._ws.send(json.dumps({
                "id": self._next_id(),
                "cmd": "subscribe",
                "params": {"channels": ["ticker"], "market_tickers": chunk},
            }))
        print(f"[KalshiFeed] Subscribed to {len(tickers)} ticker(s)")

    async def _run(self) -> None:
        import websockets  # lazy import so startup doesn't fail if missing

        url = f"{self._host}{self.WS_PATH}"
        headers = self._get_headers()

        async with websockets.connect(
            url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=30,
        ) as ws:
            self._ws = ws
            print(f"[KalshiFeed] Connected -> {url}")

            # Flush any tickers that were queued before connect
            if self._pending:
                await self._send_sub(list(self._pending))
                self._subscribed.update(self._pending)
                self._pending.clear()

            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                mtype = msg.get("type")

                if mtype == "ticker":
                    data = msg.get("msg", {})
                    ticker = data.get("market_ticker", "")
                    # API now uses yes_bid_dollars/yes_ask_dollars (string, 0-1 range)
                    bid_d = data.get("yes_bid_dollars") or data.get("yes_bid")
                    ask_d = data.get("yes_ask_dollars") or data.get("yes_ask")
                    if ticker and bid_d is not None and ask_d is not None:
                        # Convert to integer cents
                        bid = round(float(bid_d) * 100) if isinstance(bid_d, str) else int(bid_d)
                        ask = round(float(ask_d) * 100) if isinstance(ask_d, str) else int(ask_d)
                        await self._on_tick(ticker, bid, ask)

                elif mtype == "subscribed":
                    print(f"[KalshiFeed] Subscription confirmed (raw): {str(msg)[:400]}")

                elif mtype == "error":
                    print(f"[KalshiFeed] Server error: {msg.get('msg')}")

                else:
                    if not hasattr(self, "_logged_unknown"):
                        self._logged_unknown = set()
                    if mtype not in self._logged_unknown:
                        self._logged_unknown.add(mtype)
                        print(f"[KalshiFeed] Unknown msg type: {mtype} | sample: {str(msg)[:200]}")

        self._ws = None
