import React, { useEffect, useMemo, useRef, useState } from "react";

const API = (import.meta.env.VITE_API_URL as string | undefined) || window.location.origin;
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  AreaChart,
  Area,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  ExternalLink,
  Play,
  RefreshCw,
  Square,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import { useWebSocket } from "@/hooks/useWebSocket";
import { BotConfig, BucketPosition, BucketSignal, InvertedLegSignal, Position, SingleLegPosition, StructuralAnomaly, TradeRecord, ViolationSignal } from "./types";

// ── Trade notifications ───────────────────────────────────────────────────────

function requestNotificationPermission() {
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission();
  }
}

function playTradeBeep() {
  try {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type = "sine";
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.setValueAtTime(1100, ctx.currentTime + 0.12);
    gain.gain.setValueAtTime(0.25, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + 0.4);
  } catch {
    // AudioContext not available
  }
}

function fireTradeNotification(title: string, body: string) {
  playTradeBeep();
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification(title, { body, icon: "/favicon.ico" });
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtCents(x: number) {
  return `${(x * 100).toFixed(1)}¢`;
}

function fmtPnl(x: number) {
  const sign = x >= 0 ? "+" : "";
  return `${sign}$${x.toFixed(2)}`;
}

function fmtPct(x: number) {
  return `${(x * 100).toFixed(1)}%`;
}

function fmtThreshold(v: number) {
  if (v >= 1000) return v.toLocaleString();
  return String(v);
}

function timeSince(iso: string) {
  const diff = Date.now() - new Date(iso).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h ago`;
}

function expiryIn(iso: string) {
  const diff = new Date(iso).getTime() - Date.now();
  if (diff <= 0) return "expired";
  const h = Math.floor(diff / 3600000);
  const m = Math.floor((diff % 3600000) / 60000);
  if (h > 24) return `${Math.floor(h / 24)}d`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function kalshiUrl(eventTicker: string): string {
  const series = eventTicker.split("-")[0].toLowerCase();
  return `https://kalshi.com/markets/${series}/events/${eventTicker.toLowerCase()}`;
}

function pnlColor(v: number) {
  if (v > 0) return "text-emerald-600";
  if (v < 0) return "text-rose-600";
  return "text-slate-500";
}

function statusBadgeVariant(s: string): "success" | "destructive" | "secondary" | "outline" {
  if (s === "open") return "secondary";
  if (s === "closed") return "outline";
  if (s === "one_leg_risk") return "destructive";
  return "outline";
}

// ── PnL Summary cards ─────────────────────────────────────────────────────────

function PnlCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-2xl border bg-white p-4 shadow-sm">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-slate-900">{value}</div>
      {sub && <div className="mt-0.5 text-[10px] text-slate-400">{sub}</div>}
    </div>
  );
}

// ── Signal row ────────────────────────────────────────────────────────────────

function SignalRow({
  sig,
  isTraded,
  onTrade,
}: {
  sig: ViolationSignal;
  isTraded: boolean;
  onTrade: (id: string) => void;
}) {
  const [trading, setTrading] = useState(false);

  async function handleTrade() {
    if (trading || isTraded) return;
    setTrading(true);
    try {
      const res = await fetch(`${API}/bot/scan`, { method: "POST" });
      if (res.ok) onTrade(sig.id);
    } finally {
      setTrading(false);
    }
  }

  const expEdge = sig.expected_edge ?? sig.net_edge;
  const edgeColor =
    expEdge >= 0.15
      ? "text-emerald-700 font-bold"
      : expEdge >= 0.05
      ? "text-emerald-600 font-semibold"
      : "text-amber-600";

  return (
    <div className="grid gap-2 rounded-2xl border bg-slate-50 p-4 text-sm md:grid-cols-[1.5fr_1fr_1fr_1fr_1fr_80px] md:items-center">
      {/* Series + thresholds */}
      <div>
        <div className="flex items-center gap-1.5">
          <span className="font-semibold text-slate-800">{sig.series}</span>
          <span className="text-[10px] font-medium bg-slate-200 text-slate-600 px-1.5 py-0.5 rounded-full">
            expires {expiryIn(sig.expiry)}
          </span>
          {sig.event_ticker && (
            <a href={kalshiUrl(sig.event_ticker)} target="_blank" rel="noopener noreferrer"
               className="text-[10px] text-sky-500 hover:text-sky-700 flex items-center gap-0.5">
              <ExternalLink className="h-3 w-3" />Kalshi
            </a>
          )}
        </div>
        <div className="mt-1 flex items-center gap-1 text-xs text-slate-500">
          <span className="font-mono">{fmtThreshold(sig.lower_threshold)}</span>
          <ArrowUp className="h-3 w-3 text-emerald-500" />
          <span className="text-slate-300">|</span>
          <span className="font-mono">{fmtThreshold(sig.higher_threshold)}</span>
          <ArrowUp className="h-3 w-3 text-rose-400" />
        </div>
      </div>

      {/* Lower leg */}
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Lower YES ask</div>
        <div className="font-mono font-medium text-slate-700">{fmtCents(sig.lower_ask)}</div>
        <div className="text-[10px] text-slate-400 truncate" title={sig.lower_ticker}>
          {sig.lower_ticker.split("-").slice(-1)[0]}
        </div>
      </div>

      {/* Higher leg */}
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Higher YES bid</div>
        <div className="font-mono font-medium text-slate-700">{fmtCents(sig.higher_bid)}</div>
        <div className="text-[10px] text-slate-400 truncate" title={sig.higher_ticker}>
          {sig.higher_ticker.split("-").slice(-1)[0]}
        </div>
      </div>

      {/* Edge */}
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Exp. edge</div>
        <div className={`font-mono text-base ${edgeColor}`}>{fmtCents(expEdge)}</div>
        <div className="text-[10px] text-slate-400">
          gross {fmtCents(sig.gross_edge)}
          {sig.middle_prob > 0.01 && (
            <span className="ml-1 text-sky-500">· {Math.round(sig.middle_prob * 100)}% both</span>
          )}
        </div>
      </div>

      {/* Entry cost + depth */}
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Entry cost</div>
        <div className="font-mono font-medium text-slate-700">{fmtCents(sig.entry_cost)}</div>
        {sig.lower_depth > 0 || sig.higher_depth > 0 ? (
          <div className="text-[10px] text-slate-400">
            <span className={sig.avail_size <= 5 ? "text-amber-500 font-semibold" : ""}>
              {sig.avail_size} cts
            </span>
            <span className="text-slate-400"> ({sig.lower_depth}↑/{sig.higher_depth}↓)</span>
          </div>
        ) : (
          <div className="text-[10px] text-slate-400">~{sig.avail_size} cts (OI est.)</div>
        )}
      </div>

      {/* Action */}
      <div className="flex justify-end">
        {isTraded ? (
          <Badge variant="success" className="rounded-full text-[10px]">
            Traded
          </Badge>
        ) : (
          <button
            onClick={handleTrade}
            disabled={trading}
            className="rounded-full bg-emerald-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-600 disabled:opacity-50 transition"
          >
            {trading ? "…" : "Trade"}
          </button>
        )}
      </div>
    </div>
  );
}

// ── Bucket signal row ─────────────────────────────────────────────────────────

function BucketSignalRow({
  sig,
  isTraded,
}: {
  sig: BucketSignal;
  isTraded: boolean;
}) {
  const edgeColor =
    sig.gross_edge >= 0.15
      ? "text-emerald-700 font-bold"
      : sig.gross_edge >= 0.10
      ? "text-emerald-600 font-semibold"
      : "text-amber-600";

  return (
    <div className="grid gap-2 rounded-2xl border border-violet-100 bg-violet-50 p-4 text-sm md:grid-cols-[1.5fr_1fr_1fr_1fr_80px] md:items-center">
      {/* Series + info */}
      <div>
        <div className="flex items-center gap-1.5">
          <span className="font-semibold text-slate-800">{sig.series}</span>
          <span className="text-[10px] font-medium bg-violet-200 text-violet-700 px-1.5 py-0.5 rounded-full">
            BUCKET SUM
          </span>
          <span className="text-[10px] font-medium bg-slate-200 text-slate-600 px-1.5 py-0.5 rounded-full">
            expires {expiryIn(sig.expiry)}
          </span>
        </div>
        <div className="mt-1 text-xs text-slate-500 font-mono truncate" title={sig.event_ticker}>
          {sig.event_ticker} · {sig.bucket_count} buckets
        </div>
      </div>

      {/* Sum asks */}
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Sum of asks</div>
        <div className="font-mono font-medium text-slate-700">{fmtCents(sig.sum_asks)}</div>
        <div className="text-[10px] text-slate-400">should be ≥ 100¢</div>
      </div>

      {/* Edge */}
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Gross edge</div>
        <div className={`font-mono text-base ${edgeColor}`}>{fmtCents(sig.gross_edge)}</div>
        <div className="text-[10px] text-slate-400">net {fmtCents(sig.net_edge)}</div>
      </div>

      {/* Size */}
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Avail size</div>
        <div className="font-mono font-medium text-slate-700">{sig.avail_size} cts</div>
        <div className="text-[10px] text-slate-400">buy all {sig.bucket_count} buckets</div>
      </div>

      {/* Badge */}
      <div className="flex justify-end">
        {isTraded ? (
          <Badge variant="success" className="rounded-full text-[10px]">Traded</Badge>
        ) : (
          <Badge variant="secondary" className="rounded-full text-[10px] bg-violet-100 text-violet-700">
            Auto
          </Badge>
        )}
      </div>
    </div>
  );
}

// ── Structural anomaly row ────────────────────────────────────────────────────

// API is defined at module level above

function StructuralAnomalyRow({ sig }: { sig: StructuralAnomaly }) {
  const [trading, setTrading] = useState(false);
  const [traded, setTraded] = useState(false);

  async function handleTrade() {
    if (trading || traded) return;
    if (!confirm(`Execute structural arb: Buy YES@${fmtCents(sig.lower_ask)} + NO@${fmtCents(1 - sig.higher_bid)} (${sig.avail_size} contracts)?`)) return;
    setTrading(true);
    try {
      const res = await fetch(`${API}/structural/${encodeURIComponent(sig.id)}/trade`, { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(`Trade failed (${res.status}): ${err.detail || err.message || res.statusText || "unknown error"}`);
      } else {
        setTraded(true);
      }
    } catch (e) {
      alert(`Network error: ${e}`);
    } finally {
      setTrading(false);
    }
  }

  return (
    <div className="rounded-xl border border-purple-100 bg-purple-50 p-3 text-xs">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          {/* Header */}
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="font-semibold text-slate-700">{sig.series}</span>
            <span className="text-[10px] bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded-full font-medium">STRUCTURAL</span>
            <span className="text-[10px] bg-slate-100 text-slate-500 px-1.5 py-0.5 rounded-full">gap {sig.gap}</span>
            <span className="text-[10px] text-slate-400">expires {expiryIn(sig.expiry)}</span>
            {sig.event_ticker && (
              <a href={kalshiUrl(sig.event_ticker)} target="_blank" rel="noopener noreferrer"
                 className="text-[10px] text-sky-500 hover:text-sky-700 flex items-center gap-0.5">
                <ExternalLink className="h-3 w-3" />Kalshi
              </a>
            )}
          </div>

          {/* Bookend pair */}
          <div className="mt-1 flex items-center gap-1 text-[10px] font-mono text-slate-600">
            <span className="bg-white border border-slate-200 rounded px-1">{fmtThreshold(sig.lower_threshold)}</span>
            <span className="text-slate-300">←</span>
            {sig.middle_markets.map((m) => (
              <span key={m.ticker} className="bg-amber-100 border border-amber-300 rounded px-1 text-amber-700" title={`bid ${fmtCents(m.yes_bid)} ask ${fmtCents(m.yes_ask)}`}>
                {fmtThreshold(m.threshold)} ⚠
              </span>
            ))}
            <span className="text-slate-300">←</span>
            <span className="bg-white border border-slate-200 rounded px-1">{fmtThreshold(sig.higher_threshold)}</span>
          </div>

          {/* Strategy + costs */}
          <div className="mt-1 text-[10px] text-slate-400 font-mono">
            Buy YES@{fmtCents(sig.lower_ask)} + NO@{fmtCents(1 - sig.higher_bid)}
            <span className="text-slate-300 mx-1">·</span>
            cost {fmtCents(sig.entry_cost)}
            <span className="text-slate-300 mx-1">·</span>
            <span className={(sig.expected_edge ?? sig.net_edge) >= 0 ? "text-green-600 font-semibold" : "text-red-500 font-semibold"}>
              EV {fmtCents(sig.expected_edge ?? sig.net_edge)}/contract
            </span>
            {(sig.middle_prob ?? 0) > 0.01 && (
              <span className="ml-1 text-sky-500">· {Math.round((sig.middle_prob ?? 0) * 100)}% both</span>
            )}
          </div>
        </div>

        {/* Trade button */}
        <button
          onClick={handleTrade}
          disabled={trading || traded}
          className={`shrink-0 rounded-xl px-3 py-1.5 text-xs font-semibold transition-colors ${
            traded
              ? "bg-green-100 text-green-600 cursor-default"
              : trading
              ? "bg-slate-100 text-slate-400 cursor-wait"
              : "bg-purple-600 text-white hover:bg-purple-700 active:bg-purple-800"
          }`}
        >
          {traded ? "Traded ✓" : trading ? "…" : "Trade"}
        </button>
      </div>
    </div>
  );
}

// ── Inverted-leg row ──────────────────────────────────────────────────────────

function InvertedLegRow({ sig }: { sig: InvertedLegSignal }) {
  const [trading, setTrading] = useState(false);
  const [traded, setTraded] = useState(false);

  async function handleTrade() {
    if (trading || traded) return;
    if (!confirm(`Buy YES on ${sig.ticker} @ ${fmtCents(sig.ask)}, target exit ≥ ${fmtCents(sig.target_bid)}?`)) return;
    setTrading(true);
    try {
      const res = await fetch(`${API}/inverted/${encodeURIComponent(sig.ticker)}/trade`, { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(`Trade failed (${res.status}): ${err.detail || err.message || res.statusText || "unknown error"}`);
      } else {
        setTraded(true);
      }
    } catch (e) {
      alert(`Network error: ${e}`);
    } finally {
      setTrading(false);
    }
  }

  return (
    <div className="rounded-xl border border-orange-200 bg-white p-3 text-xs">
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="font-semibold text-slate-700">{sig.series}</span>
            <span className="text-[10px] bg-orange-100 text-orange-700 px-1.5 py-0.5 rounded-full font-medium">MEAN-REV</span>
            <span className="text-[10px] text-slate-400">expires {expiryIn(sig.expiry)}</span>
            {sig.event_ticker && (
              <a href={kalshiUrl(sig.event_ticker)} target="_blank" rel="noopener noreferrer"
                 className="text-[10px] text-sky-500 hover:text-sky-700 flex items-center gap-0.5">
                <ExternalLink className="h-3 w-3" />Kalshi
              </a>
            )}
          </div>
          {/* Three-rung ladder: lower neighbor → cheap middle → upper neighbor */}
          <div className="mt-1 flex items-center gap-1 font-mono text-[10px] flex-wrap">
            {sig.adj_lower_ticker && (
              <>
                <span className="bg-slate-50 border border-slate-200 text-slate-500 rounded px-1.5 py-0.5">
                  {sig.adj_lower_title || fmtThreshold(sig.adj_lower_threshold ?? 0)} {fmtCents(sig.adj_lower_bid ?? 0)}/{fmtCents(sig.adj_lower_ask ?? 0)}
                </span>
                <span className="text-slate-300">›</span>
              </>
            )}
            <span className="bg-red-50 border border-red-200 text-red-700 rounded px-1.5 py-0.5 font-semibold">
              {sig.title || fmtThreshold(sig.threshold)} {fmtCents(sig.bid ?? 0)}/{fmtCents(sig.ask)} ↓cheap
            </span>
            <span className="text-slate-300">›</span>
            <span className="bg-slate-50 border border-slate-200 text-slate-500 rounded px-1.5 py-0.5">
              {sig.adj_title || fmtThreshold(sig.adj_threshold)} {fmtCents(sig.adj_bid ?? 0)}/{fmtCents(sig.adj_ask)}
            </span>
          </div>
          <div className="mt-0.5 text-[10px] text-orange-600 font-semibold">
            Anomaly: {fmtCents(sig.inversion)} below fair ({fmtCents(sig.interp_mid)}) · target: {fmtCents(sig.target_bid)}
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-[10px]">
            <span className="text-slate-400">
              Buy {sig.ticker} YES @ {fmtCents(sig.ask)}, exit ≥ {fmtCents(sig.target_bid)}
            </span>
            <span className={sig.avail_size >= 10 ? "text-emerald-600 font-semibold" : "text-orange-500 font-semibold"}>
              {sig.avail_size} cts at price{sig.avail_size < 10 ? " (min 10 to trade)" : ""}
            </span>
          </div>
        </div>
        <button
          onClick={handleTrade}
          disabled={trading || traded}
          className={`shrink-0 rounded-xl px-3 py-1.5 text-xs font-semibold transition-colors ${
            traded
              ? "bg-green-100 text-green-600 cursor-default"
              : trading
              ? "bg-slate-100 text-slate-400 cursor-wait"
              : "bg-orange-500 text-white hover:bg-orange-600 active:bg-orange-700"
          }`}
        >
          {traded ? "Traded ✓" : trading ? "…" : "Trade"}
        </button>
      </div>
    </div>
  );
}

// ── Near-miss row ─────────────────────────────────────────────────────────────

function NearMissRow({ sig, threshold }: { sig: ViolationSignal; threshold: number }) {
  const gap = threshold - sig.gross_edge;
  // Window is 30 cents below threshold to threshold. Map to 0-100%.
  const windowSize = 0.30;
  const pct = Math.max(0, (sig.gross_edge - (threshold - windowSize)) / windowSize);
  const barWidth = Math.min(100, Math.round(pct * 100));

  return (
    <div className="grid gap-2 rounded-xl border border-amber-100 bg-amber-50 p-3 text-xs md:grid-cols-[1.5fr_1fr_1fr_1fr] md:items-center">
      <div>
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="font-semibold text-slate-700">{sig.series}</span>
          <span className="text-[10px] bg-sky-100 text-sky-600 px-1.5 py-0.5 rounded-full">MONO</span>
          <span className="text-[10px] text-slate-400">expires {expiryIn(sig.expiry)}</span>
          {sig.event_ticker && (
            <a href={kalshiUrl(sig.event_ticker)} target="_blank" rel="noopener noreferrer"
               className="text-[10px] text-sky-500 hover:text-sky-700 flex items-center gap-0.5">
              <ExternalLink className="h-3 w-3" />Kalshi
            </a>
          )}
          <span className="text-[10px] text-slate-300">·</span>
          <span className="text-[10px] text-slate-400 font-mono">{new Date(sig.detected_at).toLocaleTimeString()}</span>
        </div>
        <div className="mt-0.5 flex items-center gap-1 text-[10px] text-slate-500 font-mono">
          <span>{fmtThreshold(sig.lower_threshold)}</span>
          <span className="text-slate-300">→</span>
          <span>{fmtThreshold(sig.higher_threshold)}</span>
        </div>
        <div className="mt-1 text-[10px] text-slate-400 font-mono">
          Buy YES@{fmtCents(sig.lower_ask)} + NO@{fmtCents(1 - sig.higher_bid)}
          <span className="text-slate-300 mx-1">·</span>
          cost {fmtCents(sig.entry_cost)}
          <span className="text-slate-300 mx-1">·</span>
          <span className={(sig.expected_edge ?? sig.net_edge) >= 0 ? "text-green-600 font-semibold" : "text-red-500 font-semibold"}>
            EV {fmtCents(sig.expected_edge ?? sig.net_edge)}/contract
          </span>
          {sig.middle_prob > 0.01 && (
            <span className="ml-1 text-sky-500">· {Math.round(sig.middle_prob * 100)}% both</span>
          )}
        </div>
      </div>
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Gross edge</div>
        <div className="font-mono font-medium text-amber-700">{fmtCents(sig.gross_edge)}</div>
      </div>
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Gap to fire</div>
        <div className="font-mono font-medium text-slate-600">{fmtCents(gap)}</div>
      </div>
      <div>
        <div className="text-[10px] text-slate-400 mb-1 uppercase tracking-wide">
          {barWidth}% to threshold
        </div>
        <div className="h-1.5 w-full rounded-full bg-amber-200">
          <div
            className="h-1.5 rounded-full bg-amber-500 transition-all"
            style={{ width: `${barWidth}%` }}
          />
        </div>
      </div>
    </div>
  );
}

// ── Position row ──────────────────────────────────────────────────────────────

function PositionRow({ pos }: { pos: Position }) {
  const [flattening, setFlattening] = useState(false);

  async function handleFlatten() {
    if (flattening || pos.status === "closed") return;
    if (!confirm(`Flatten position ${pos.id}?`)) return;
    setFlattening(true);
    try {
      await fetch(`${API}/positions/${pos.id}/flatten`, { method: "POST" });
    } finally {
      setFlattening(false);
    }
  }

  const isOpen = pos.status !== "closed";
  const pnl = isOpen ? pos.unrealized_pnl : pos.realized_pnl;
  const currentValue = pos.lower_mid + pos.higher_no_mid;

  return (
    <div
      className={`grid gap-2 rounded-2xl border p-4 text-sm md:grid-cols-[1.5fr_1fr_1fr_1fr_1fr_80px] md:items-center ${
        pos.status === "one_leg_risk"
          ? "border-amber-300 bg-amber-50"
          : isOpen
          ? "bg-white"
          : "bg-slate-50 opacity-75"
      }`}
    >
      {/* ID + series + thresholds */}
      <div>
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="font-mono text-xs text-slate-400">{pos.id}</span>
          <span className="font-semibold text-slate-800">{pos.series}</span>
          {pos.strategy === "structural_arb" ? (
            <span className="text-[10px] bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded-full font-medium">STRUCTURAL</span>
          ) : (
            <span className="text-[10px] bg-sky-100 text-sky-700 px-1.5 py-0.5 rounded-full font-medium">THRESHOLD</span>
          )}
          {pos.status === "one_leg_risk" && (
            <AlertTriangle className="h-3.5 w-3.5 text-amber-500" />
          )}
          <a href={kalshiUrl(pos.lower_ticker.replace(/-T[\d.]+$/i, "").replace(/-\d+$/, ""))}
             target="_blank" rel="noopener noreferrer"
             className="text-[10px] text-sky-500 hover:text-sky-700 flex items-center gap-0.5">
            <ExternalLink className="h-3 w-3" />Kalshi
          </a>
        </div>
        <div className="mt-1 flex items-center gap-1 text-xs text-slate-500">
          <span className="font-mono">{fmtThreshold(pos.lower_threshold)}</span>
          <span className="text-slate-300">—</span>
          <span className="font-mono">{fmtThreshold(pos.higher_threshold)}</span>
        </div>
        <div className="mt-0.5 text-[10px] font-mono space-y-0.5">
          <div className="flex items-center gap-1 text-slate-500">
            <span className="bg-emerald-50 border border-emerald-200 text-emerald-700 rounded px-1">YES ≥{fmtThreshold(pos.lower_threshold)}</span>
            <span className="text-slate-300">entry {fmtCents(pos.lower_entry)}</span>
            <span className="text-slate-300">→</span>
            <span className={pos.lower_mid >= pos.lower_entry ? "text-emerald-600" : "text-rose-500"}>
              bid {fmtCents(pos.lower_mid)}
            </span>
          </div>
          <div className="flex items-center gap-1 text-slate-500">
            <span className="bg-rose-50 border border-rose-200 text-rose-700 rounded px-1">NO ≥{fmtThreshold(pos.higher_threshold)}</span>
            <span className="text-slate-300">entry {fmtCents(pos.higher_entry)}</span>
            <span className="text-slate-300">→</span>
            <span className={pos.higher_no_mid >= pos.higher_entry ? "text-emerald-600" : "text-rose-500"}>
              bid {fmtCents(pos.higher_no_mid)}
            </span>
          </div>
        </div>
        <div className="text-[10px] text-slate-400">
          expires {expiryIn(pos.expiry)} · entered {timeSince(pos.entry_time)}
        </div>
      </div>

      {/* Size + entry */}
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Size</div>
        <div className="font-medium">{pos.size} cts</div>
        <div className="text-[10px] text-slate-400">cost {fmtCents(pos.entry_cost)}/pair</div>
        {pos.entry_avail_size !== undefined && pos.entry_avail_size > 0 && (
          <div className="text-[10px] text-slate-400">{pos.entry_avail_size} avail at entry</div>
        )}
      </div>

      {/* Gross edge */}
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Entry edge</div>
        <div className="font-mono text-emerald-600 font-semibold">{fmtCents(pos.gross_edge)}</div>
        <div className="text-[10px] text-slate-400">worst case {fmtCents(pos.net_edge)}/ct</div>
      </div>

      {/* Worst-case P&L at expiry (one leg resolves, no middle-band bonus) */}
      {isOpen ? (
        <div>
          <div className="text-[10px] text-slate-400 uppercase tracking-wide">Worst case</div>
          <div className="font-mono font-semibold text-slate-400">
            {fmtPnl(pos.net_edge * pos.size)}
          </div>
          <div className="text-[10px] text-slate-400">single-leg · fees incl.</div>
        </div>
      ) : (
        <div>
          <div className="text-[10px] text-slate-400 uppercase tracking-wide">Exit</div>
          <div className="text-[10px] text-slate-500">{pos.exit_reason}</div>
          {pos.exit_time && (
            <div className="text-[10px] text-slate-400">{timeSince(pos.exit_time)}</div>
          )}
        </div>
      )}

      {/* PnL */}
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">
          {isOpen ? "Exit P&L" : "Realized"}
        </div>
        <div className={`font-mono text-sm font-semibold ${pnlColor(pnl)}`}>
          {fmtPnl(pnl)}
        </div>
        {isOpen && <div className="text-[10px] text-slate-400">if flattened now</div>}
        {pos.fees_paid > 0 && (
          <div className="text-[10px] text-slate-400">fees ${pos.fees_paid.toFixed(2)}</div>
        )}
      </div>

      {/* Action */}
      <div className="flex justify-end">
        {isOpen ? (
          <button
            onClick={handleFlatten}
            disabled={flattening}
            className="rounded-full border border-rose-200 px-3 py-1.5 text-xs text-rose-600 hover:bg-rose-50 disabled:opacity-50 transition"
          >
            {flattening ? "…" : "Flatten"}
          </button>
        ) : (
          <Badge variant="outline" className="rounded-full text-[10px]">
            {pos.status}
          </Badge>
        )}
      </div>
    </div>
  );
}

// ── Bucket position row ───────────────────────────────────────────────────────

function BucketPositionRow({ pos }: { pos: BucketPosition }) {
  const isOpen = pos.status !== "closed";
  const pnl = isOpen ? pos.unrealized_pnl : pos.realized_pnl;

  return (
    <div className={`grid gap-2 rounded-2xl border border-violet-100 p-4 text-sm md:grid-cols-[1.5fr_1fr_1fr_1fr_1fr_80px] md:items-center ${isOpen ? "bg-violet-50" : "bg-slate-50 opacity-75"}`}>
      <div>
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-xs text-slate-400">{pos.id}</span>
          <span className="font-semibold text-slate-800">{pos.series}</span>
          <span className="text-[10px] bg-violet-200 text-violet-700 px-1.5 py-0.5 rounded-full">BUCKET</span>
        </div>
        <div className="mt-1 text-xs text-slate-500 font-mono truncate">{pos.event_ticker}</div>
        <div className="text-[10px] text-slate-400">
          {pos.bucket_count} buckets · expires {expiryIn(pos.expiry)} · entered {timeSince(pos.entry_time)}
        </div>
      </div>
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Size</div>
        <div className="font-medium">{pos.size} cts</div>
        <div className="text-[10px] text-slate-400">cost {fmtCents(pos.entry_cost)}/set</div>
        {pos.entry_avail_size !== undefined && pos.entry_avail_size > 0 && (
          <div className="text-[10px] text-slate-400">{pos.entry_avail_size} avail at entry</div>
        )}
      </div>
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Entry edge</div>
        <div className="font-mono text-emerald-600 font-semibold">{fmtCents(pos.gross_edge)}</div>
        <div className="text-[10px] text-slate-400">net {fmtCents(pos.net_edge)}</div>
      </div>
      {isOpen ? (
        <div>
          <div className="text-[10px] text-slate-400 uppercase tracking-wide">MTM</div>
          <div className="text-[10px] text-slate-400">sum of bucket bids</div>
        </div>
      ) : (
        <div>
          <div className="text-[10px] text-slate-400 uppercase tracking-wide">Exit</div>
          <div className="text-[10px] text-slate-500">{pos.exit_reason}</div>
          {pos.exit_time && <div className="text-[10px] text-slate-400">{timeSince(pos.exit_time)}</div>}
        </div>
      )}
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">{isOpen ? "Unrealized" : "Realized"}</div>
        <div className={`font-mono text-base font-semibold ${pnlColor(pnl)}`}>{fmtPnl(pnl)}</div>
        {pos.fees_paid > 0 && <div className="text-[10px] text-slate-400">fees ${pos.fees_paid.toFixed(2)}</div>}
      </div>
      <div className="flex justify-end">
        <Badge variant="outline" className="rounded-full text-[10px]">{pos.status}</Badge>
      </div>
    </div>
  );
}

// ── Single-leg position row ───────────────────────────────────────────────────

function SingleLegPositionRow({ pos }: { pos: SingleLegPosition }) {
  const [flattening, setFlattening] = useState(false);
  const isOpen = pos.status !== "closed";
  const pnl = isOpen ? pos.unrealized_pnl : pos.realized_pnl;
  const progress = pos.current_bid > 0
    ? Math.min(100, Math.round(((pos.current_bid - pos.entry_price) / (pos.target_bid - pos.entry_price)) * 100))
    : 0;

  async function handleFlatten() {
    if (flattening || !isOpen) return;
    if (!confirm(`Flatten single-leg ${pos.id}?`)) return;
    setFlattening(true);
    try {
      await fetch(`${API}/positions/${pos.id}/flatten`, { method: "POST" });
    } finally {
      setFlattening(false);
    }
  }

  return (
    <div className={`rounded-2xl border border-orange-200 p-4 text-sm ${isOpen ? "bg-orange-50/50" : "bg-slate-50 opacity-75"}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="font-mono text-xs text-slate-400">{pos.id}</span>
            <span className="font-semibold text-slate-800">{pos.series}</span>
            <span className="text-[10px] bg-orange-100 text-orange-700 px-1.5 py-0.5 rounded-full font-medium">MEAN-REV</span>
            <a href={kalshiUrl(pos.ticker.replace(/-T[\d.]+$/i, "").replace(/-\d+$/, ""))}
               target="_blank" rel="noopener noreferrer"
               className="text-[10px] text-sky-500 hover:text-sky-700 flex items-center gap-0.5">
              <ExternalLink className="h-3 w-3" />Kalshi
            </a>
          </div>
          <div className="mt-1 text-xs font-mono text-slate-600">
            Long YES {pos.threshold} · {pos.size} cts
            {pos.entry_avail_size !== undefined && pos.entry_avail_size > 0 && (
              <span className="text-slate-400"> ({pos.entry_avail_size} avail)</span>
            )}
            {" "}· entry {fmtCents(pos.entry_price)} → target {fmtCents(pos.target_bid)}
          </div>
          <div className="text-[10px] text-slate-400">
            expires {expiryIn(pos.expiry)} · entered {timeSince(pos.entry_time)}
            {!isOpen && pos.exit_reason && ` · ${pos.exit_reason}`}
          </div>
          {pos.entry_inversion > 0 && (
            <div className="text-[10px] text-orange-500/80 mt-0.5 font-mono">
              triggered: {fmtCents(pos.entry_inversion)} below fair ({fmtCents(pos.entry_interp_mid)})
              {pos.entry_adj_lower_threshold > 0 && pos.entry_adj_higher_threshold > 0 && (
                <> · neighbors: {fmtCents(pos.entry_adj_lower_bid)} / {fmtCents(pos.entry_adj_higher_bid)}</>
              )}
            </div>
          )}
          {isOpen && (() => {
            const isNo = pos.side === "no";
            // For YES: entry_price=ask paid, current_ask=current ask (thesis indicator)
            // For NO:  entry_price=NO paid (1-bid), current_ask=current yes_bid
            const entryAsk = pos.entry_price;
            const curAsk = pos.current_ask ?? entryAsk;
            const askMoved = curAsk - entryAsk;
            const askColor = askMoved > 0.001
              ? "text-green-600"
              : askMoved < -0.001 ? "text-red-500" : "text-slate-400";
            const askArrow = askMoved > 0.001 ? "↑" : askMoved < -0.001 ? "↓" : "→";
            return (
              <div className="mt-1.5">
                <div className="flex justify-between text-[10px] mb-0.5">
                  <span className="text-slate-400 font-mono">
                    {isNo ? "no" : "ask"} {fmtCents(entryAsk)}
                    <span className={`ml-1 font-semibold ${askColor}`}>
                      {askArrow} {fmtCents(curAsk)}
                    </span>
                    <span className="text-slate-400 ml-2">
                      bid {fmtCents(pos.current_bid)}
                    </span>
                  </span>
                  <span className="text-slate-400">{progress}% to target</span>
                </div>
                <div className="h-1.5 w-full rounded-full bg-orange-200">
                  <div className="h-1.5 rounded-full bg-orange-500 transition-all" style={{ width: `${Math.max(0, progress)}%` }} />
                </div>
              </div>
            );
          })()}
        </div>
        <div className="flex flex-col items-end gap-2 shrink-0">
          <div className={`font-mono text-lg font-semibold ${pnlColor(pnl)}`}>{fmtPnl(pnl)}</div>
          {isOpen && (
            <button
              onClick={handleFlatten}
              disabled={flattening}
              className="rounded-xl px-3 py-1.5 text-xs font-semibold bg-orange-100 text-orange-700 hover:bg-orange-200 transition-colors"
            >
              {flattening ? "…" : "Flatten"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Trade row ─────────────────────────────────────────────────────────────────

function TradeRow({ trade }: { trade: TradeRecord }) {
  const isOpen = trade.action === "OPEN";
  const pnl = trade.pnl;

  return (
    <div className="grid gap-2 rounded-2xl border bg-slate-50 p-3 text-xs md:grid-cols-[90px_80px_1.5fr_1fr_1fr_1fr_80px] md:items-center">
      <div className="font-mono text-slate-500">
        {new Date(trade.timestamp).toLocaleTimeString()}
      </div>
      <div>
        <span
          className={`rounded-full px-2 py-0.5 font-semibold ${
            isOpen
              ? "bg-blue-100 text-blue-700"
              : "bg-slate-100 text-slate-600"
          }`}
        >
          {trade.action.replace("CLOSE_", "")}
        </span>
      </div>
      <div>
        <div className="flex items-center gap-1.5">
          <span className="font-medium text-slate-700">{trade.series}</span>
          {trade.strategy === "structural_arb" ? (
            <span className="text-[10px] bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded-full font-medium">STRUCT</span>
          ) : trade.strategy === "bucket_arb" ? (
            <span className="text-[10px] bg-violet-100 text-violet-700 px-1.5 py-0.5 rounded-full font-medium">BUCKET</span>
          ) : (
            <span className="text-[10px] bg-sky-100 text-sky-700 px-1.5 py-0.5 rounded-full font-medium">MONO</span>
          )}
        </div>
        <div className="text-slate-400 font-mono">
          {fmtThreshold(trade.lower_threshold)} → {fmtThreshold(trade.higher_threshold)}
        </div>
      </div>
      <div>
        <span className="text-slate-500">×{trade.size}</span>
        <div className="text-slate-400">cost {fmtCents(trade.lower_entry + trade.higher_entry)}</div>
      </div>
      <div>
        <span className="text-emerald-600 font-semibold">{fmtCents(trade.gross_edge)}</span>
        <div className="text-slate-400">edge</div>
      </div>
      <div>
        {pnl != null ? (
          <span className={`font-semibold font-mono ${pnlColor(pnl)}`}>{fmtPnl(pnl)}</span>
        ) : (
          <span className="text-slate-300">—</span>
        )}
        {trade.fees > 0 && <div className="text-slate-400">fee ${trade.fees.toFixed(2)}</div>}
      </div>
      <div className="flex justify-end">
        <Badge
          variant={
            trade.status === "paper_filled"
              ? "success"
              : trade.status === "expired"
              ? "secondary"
              : "outline"
          }
          className="rounded-full text-[10px]"
        >
          {trade.status}
        </Badge>
      </div>
    </div>
  );
}

// ── Config panel ──────────────────────────────────────────────────────────────

function ConfigPanel({
  config,
  onUpdate,
}: {
  config: BotConfig | null;
  onUpdate: (patch: Partial<BotConfig>) => void;
}) {
  const [minEdge, setMinEdge] = useState(String((config?.min_gross_edge ?? 0.07) * 100));
  const [maxSize, setMaxSize] = useState(String(config?.max_size ?? 500));
  const [refreshInterval, setRefreshInterval] = useState(String(config?.refresh_interval ?? 300));

  React.useEffect(() => {
    if (!config) return;
    setMinEdge(String(Math.round(config.min_gross_edge * 100)));
    setMaxSize(String(config.max_size));
    setRefreshInterval(String(config.refresh_interval));
  }, [config]);

  function save() {
    const e = parseFloat(minEdge) / 100;
    const s = parseInt(maxSize);
    const i = parseInt(refreshInterval);
    if (!isNaN(e) && !isNaN(s) && !isNaN(i)) {
      // Hard floor: must strictly exceed 7c fee so net_edge > 0
      onUpdate({ min_gross_edge: Math.max(e, 0.071), max_size: s, refresh_interval: i });
    }
  }

  return (
    <div className="grid gap-4 sm:grid-cols-3">
      <div>
        <label className="block text-xs text-slate-500 mb-1">Min gross edge (cents)</label>
        <input
          type="number"
          value={minEdge}
          onChange={(e) => setMinEdge(e.target.value)}
          className="w-full rounded-xl border px-3 py-2 text-sm"
          min="1"
          max="50"
        />
        <p className="mt-1 text-[10px] text-slate-400">
          Minimum bid(higher) − ask(lower) to trigger trade
        </p>
      </div>
      <div>
        <label className="block text-xs text-slate-500 mb-1">Max size (contracts)</label>
        <input
          type="number"
          value={maxSize}
          onChange={(e) => setMaxSize(e.target.value)}
          className="w-full rounded-xl border px-3 py-2 text-sm"
          min="1"
          max="10000"
        />
      </div>
      <div>
        <label className="block text-xs text-slate-500 mb-1">REST refresh interval (seconds)</label>
        <input
          type="number"
          value={refreshInterval}
          onChange={(e) => setRefreshInterval(e.target.value)}
          className="w-full rounded-xl border px-3 py-2 text-sm"
          min="60"
          max="3600"
        />
        <p className="mt-1 text-[10px] text-slate-400">
          Full market refresh cadence. Real-time feed handles sub-second detection.
        </p>
      </div>
      <div className="sm:col-span-3 flex flex-wrap items-center gap-4">
        <Button size="sm" className="rounded-2xl" onClick={save}>
          Save Config
        </Button>
        <label className="flex items-center gap-2 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={config?.auto_trade_inverted ?? false}
            onChange={(e) => onUpdate({ auto_trade_inverted: e.target.checked })}
            className="rounded"
          />
          <span className="text-xs text-slate-600 font-medium">Auto-trade mean reversion</span>
          <span className="text-[10px] text-amber-500 font-semibold">⚠ directional risk</span>
        </label>
        <div className="text-xs text-slate-400">
          Fee rate: {fmtPct(config?.fee_rate ?? 0.07)} · Auto-trade:{" "}
          {config?.auto_trade ? "on" : "off"} · Paper mode always on
        </div>
      </div>
    </div>
  );
}

// ── Main Dashboard ────────────────────────────────────────────────────────────

export default function Dashboard() {
  const {
    botState,
    config,
    signals,
    nearMisses,
    bucketSignals,
    bucketNearMisses,
    structuralAnomalies,
    structuralNearMisses,
    invertedLegs,
    positions,
    trades,
    pnlHistory,
    connected,
    error,
    startBot,
    stopBot,
    triggerScan,
    updateConfig,
  } = useWebSocket();

  const [tradedSignalIds, setTradedSignalIds] = useState<Set<string>>(new Set());
  const [activeTab, setActiveTab] = useState("signals");
  const [unseenTrades, setUnseenTrades] = useState(0);
  const [stratFilter, setStratFilter] = useState<"all" | "threshold" | "structural" | "bucket" | "meanrev">("all");
  const seenTradeIds = useRef<Set<string>>(new Set());

  // Request browser notification permission on first render
  useEffect(() => { requestNotificationPermission(); }, []);

  // Fire notification on every new OPEN trade
  useEffect(() => {
    // First snapshot: seed all existing trades as seen without notifying
    if (seenTradeIds.current.size === 0) {
      trades.forEach((t) => seenTradeIds.current.add(t.id));
      return;
    }
    const newOpens = trades.filter(
      (t) => t.action === "OPEN" && !seenTradeIds.current.has(t.id)
    );
    for (const t of newOpens) {
      seenTradeIds.current.add(t.id);
      const body = `${t.series} ${fmtThreshold(t.lower_threshold)}→${fmtThreshold(t.higher_threshold)} · ×${t.size} · edge ${fmtCents(t.gross_edge)}`;
      fireTradeNotification("New Trade Opened", body);
      if (activeTab !== "trades") {
        setUnseenTrades((n) => n + 1);
      }
    }
  }, [trades]);

  // Clear unseen badge when user clicks Trades tab
  useEffect(() => {
    if (activeTab === "trades") setUnseenTrades(0);
  }, [activeTab]);

  // Separate threshold vs bucket vs single-leg positions
  const SINGLE_LEG_STRATEGIES = new Set(["mispriced_leg", "mean_rev", "sell_expensive", "limit_ladder"]);
  const thresholdPositions = positions.filter((p) => (p as any).type !== "bucket_sum" && !SINGLE_LEG_STRATEGIES.has((p as any).strategy)) as Position[];
  const bucketPositions = positions.filter((p) => (p as any).type === "bucket_sum") as BucketPosition[];
  const singleLegPositions = positions.filter((p) => SINGLE_LEG_STRATEGIES.has((p as any).strategy)) as SingleLegPosition[];

  const openPos = thresholdPositions.filter((p) => p.status !== "closed");
  const closedPos = thresholdPositions.filter((p) => p.status === "closed");
  const openBucketPos = bucketPositions.filter((p) => p.status !== "closed");
  const closedBucketPos = bucketPositions.filter((p) => p.status === "closed");
  const openSinglePos = singleLegPositions.filter((p) => p.status !== "closed");
  const closedSinglePos = singleLegPositions.filter((p) => p.status === "closed");

  const positionedIds = new Set([
    ...openPos.map((p) => p.signal_id),
    ...openBucketPos.map((p) => p.signal_id),
    ...openSinglePos.map((p) => p.signal_id),
  ]);

  const activeSignals = signals.filter((s) => !positionedIds.has(s.id));
  const activeBucketSignals = bucketSignals.filter((s) => !positionedIds.has(s.id));
  const totalActiveSignals = activeSignals.length + activeBucketSignals.length;

  const STARTING_CAPITAL = 1000;

  // Per-strategy stats derived from closed positions (covers all types including single-leg)
  type StratStat = {
    pnl: number; count: number; wins: number; losses: number;
    grossWins: number; grossLosses: number;
    exitReasons: Record<string, number>;
  };
  const stratStats = useMemo(() => {
    const make = (): StratStat => ({
      pnl: 0, count: 0, wins: 0, losses: 0,
      grossWins: 0, grossLosses: 0, exitReasons: {},
    });
    const s: Record<string, StratStat> = {
      threshold_arb: make(), structural_arb: make(),
      bucket_arb: make(), mispriced_leg: make(),
    };
    const all = [
      ...closedPos.map(p => ({ pnl: p.realized_pnl, strategy: p.strategy, exit: p.exit_reason })),
      ...closedBucketPos.map(p => ({ pnl: p.realized_pnl, strategy: p.strategy, exit: p.exit_reason })),
      ...closedSinglePos.map(p => ({ pnl: p.realized_pnl, strategy: p.strategy, exit: p.exit_reason })),
    ];
    all.forEach(({ pnl, strategy, exit }) => {
      const key = strategy || "threshold_arb";
      if (!s[key]) s[key] = make();
      s[key].pnl += pnl;
      s[key].count += 1;
      if (pnl > 0) { s[key].wins += 1; s[key].grossWins += pnl; }
      else if (pnl < 0) { s[key].losses += 1; s[key].grossLosses += Math.abs(pnl); }
      if (exit) s[key].exitReasons[exit] = (s[key].exitReasons[exit] ?? 0) + 1;
    });
    return s;
  }, [closedPos, closedBucketPos, closedSinglePos]);

  // Overall profit factor
  const overallProfitFactor = useMemo(() => {
    const totalWins = Object.values(stratStats).reduce((a, s) => a + s.grossWins, 0);
    const totalLoss = Object.values(stratStats).reduce((a, s) => a + s.grossLosses, 0);
    return totalLoss === 0 ? (totalWins > 0 ? Infinity : null) : totalWins / totalLoss;
  }, [stratStats]);

  // Sharpe ratio from daily P&L snapshots (need ≥ 3 days)
  const sharpe = useMemo(() => {
    if (pnlHistory.length < 2) return null;
    const days: Record<string, number> = {};
    pnlHistory.forEach(p => { const d = p.time.slice(0, 10); days[d] = p.total; });
    const dayKeys = Object.keys(days).sort();
    if (dayKeys.length < 3) return null;
    const ret = dayKeys.slice(1).map((d, i) => days[d] - days[dayKeys[i]]);
    const mean = ret.reduce((a, b) => a + b, 0) / ret.length;
    const std = Math.sqrt(ret.reduce((a, b) => a + (b - mean) ** 2, 0) / ret.length);
    return std === 0 ? null : (mean / std) * Math.sqrt(252);
  }, [pnlHistory]);

  // PnL chart
  const chartData = useMemo(
    () =>
      pnlHistory.map((p, i) => ({
        i,
        total: p.total,
        realized: p.realized,
        unrealized: p.unrealized,
        threshold: p.threshold ?? 0,
        structural: p.structural ?? 0,
        bucket: p.bucket ?? 0,
        meanrev: p.meanrev ?? 0,
      })),
    [pnlHistory]
  );

  // Totals
  const realizedPnl = botState?.realized_pnl ?? 0;
  const unrealizedPnl = botState?.unrealized_pnl ?? 0;  // mark-to-market: exit now value
  const lockedPnl = botState?.locked_pnl ?? 0;           // guaranteed profit at expiry
  const totalPnl = botState?.total_pnl ?? 0;

  return (
    <div className="min-h-screen bg-slate-50 p-4 md:p-6">
      <div className="mx-auto max-w-6xl space-y-5">

        {/* ── Header ──────────────────────────────────────────────── */}
        <div className="rounded-3xl bg-white p-5 shadow-sm">
          <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
            <div>
              <div className="flex items-center gap-2 text-sm text-slate-500 mb-2">
                <Activity className="h-4 w-4" />
                <span>Kalshi Ladder Arb</span>
                <span
                  className={`flex items-center gap-1 text-xs font-medium ml-2 ${
                    connected ? "text-emerald-600" : "text-slate-400"
                  }`}
                >
                  {connected ? <Wifi className="h-3 w-3" /> : <WifiOff className="h-3 w-3" />}
                  {connected ? "Live" : "Offline"}
                </span>
                <Badge variant="secondary" className="rounded-full text-[10px] ml-1">
                  PAPER
                </Badge>
                {botState && (
                  <span className="text-[10px] text-slate-400">
                    {botState.auth_method} · {botState.markets_fetched} mkts ·{" "}
                    {botState.groups_found} groups
                  </span>
                )}
                {botState && (
                  <span
                    className={`flex items-center gap-1 text-[10px] font-medium ${
                      botState.feed_connected ? "text-blue-600" : "text-slate-400"
                    }`}
                  >
                    {botState.feed_connected ? <Wifi className="h-3 w-3" /> : <WifiOff className="h-3 w-3" />}
                    {botState.feed_connected
                      ? `feed live · ${botState.ticks_received.toLocaleString()} ticks`
                      : "feed offline"}
                  </span>
                )}
              </div>
              <h1 className="text-2xl font-semibold text-slate-900">
                Kalshi Ladder Arb Scanner
              </h1>
              <p className="mt-1 text-sm text-slate-500 max-w-xl">
                Finds mispricings across threshold markets where buying both sides costs less than
                $1 — yet one side always wins. Profit = spread between what you pay and $1 payout,
                minus fees.
              </p>

              <div className="mt-3 flex flex-wrap gap-2">
                {!botState?.running ? (
                  <Button size="sm" className="rounded-2xl gap-1" onClick={startBot} disabled={!connected}>
                    <Play className="h-3.5 w-3.5" /> Start
                  </Button>
                ) : (
                  <Button size="sm" variant="destructive" className="rounded-2xl gap-1" onClick={stopBot}>
                    <Square className="h-3.5 w-3.5" /> Stop
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  className="rounded-2xl gap-1"
                  onClick={triggerScan}
                  disabled={!connected}
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                  Scan now
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="rounded-2xl gap-1 text-rose-600 border-rose-200 hover:bg-rose-50"
                  onClick={async () => {
                    if (!confirm("Reset all paper P&L?")) return;
                    await fetch(`${API}/bot/reset`, { method: "POST" });
                  }}
                  disabled={!connected}
                >
                  <X className="h-3.5 w-3.5" /> Reset P&L
                </Button>
                {botState?.scanning && (
                  <span className="flex items-center gap-1 text-xs text-slate-400 animate-pulse">
                    <RefreshCw className="h-3 w-3" /> Scanning…
                  </span>
                )}
                {error && (
                  <span className="flex items-center gap-1 text-xs text-red-500">
                    <AlertTriangle className="h-3 w-3" /> {error}
                  </span>
                )}
              </div>
            </div>

            {/* PnL cards */}
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <PnlCard
                label="Total P&L"
                value={fmtPnl(totalPnl)}
                sub="realized + unrealized"
              />
              <PnlCard
                label="Realized"
                value={fmtPnl(realizedPnl)}
                sub={`${botState?.closed_positions ?? 0} closed`}
              />
              <PnlCard
                label="Unrealized (MTM)"
                value={fmtPnl(unrealizedPnl)}
                sub={lockedPnl !== 0 ? `locked: ${fmtPnl(lockedPnl)} at expiry` : `${botState?.open_positions ?? 0} open`}
              />
              <PnlCard
                label="Win rate"
                value={botState ? fmtPct(botState.win_rate) : "—"}
                sub={`${botState?.closed_positions ?? 0} closed`}
              />
            </div>
          </div>
        </div>

        {/* ── Tabs ────────────────────────────────────────────────── */}
        <Tabs value={activeTab} onValueChange={setActiveTab} className="space-y-5">
          <TabsList className="grid w-full grid-cols-4 rounded-2xl bg-white p-1 shadow-sm">
            <TabsTrigger value="signals">
              Signals {totalActiveSignals > 0 && `(${totalActiveSignals})`}
            </TabsTrigger>
            <TabsTrigger value="positions">
              Positions {(openPos.length + openBucketPos.length + openSinglePos.length) > 0 && `(${openPos.length + openBucketPos.length + openSinglePos.length})`}
            </TabsTrigger>
            <TabsTrigger value="trades" className="relative">
              Trades
              {unseenTrades > 0 && (
                <span className="ml-1.5 inline-flex items-center justify-center rounded-full bg-green-500 text-white text-[10px] font-bold w-4 h-4 leading-none">
                  {unseenTrades}
                </span>
              )}
            </TabsTrigger>
            <TabsTrigger value="config">Config</TabsTrigger>
          </TabsList>

          {/* Signals */}
          <TabsContent value="signals">
            <div className="space-y-3">
              {/* Threshold monotonicity violations */}
              <Card className="rounded-3xl shadow-sm">
                <CardHeader className="pb-2">
                  <CardTitle className="flex items-center gap-2">
                    Threshold Violations
                    {activeSignals.length > 0 && (
                      <Badge className="rounded-full bg-emerald-100 text-emerald-700 text-xs">
                        {activeSignals.length} found
                      </Badge>
                    )}
                  </CardTitle>
                  <p className="text-xs text-slate-400">
                    bid(higher) − ask(lower) ≥ {fmtCents(config?.min_gross_edge ?? 0.07)}.
                    Buy YES at lower + NO at higher. Detected real-time on every tick.
                  </p>
                </CardHeader>
                <CardContent className="space-y-2">
                  {activeSignals.length === 0 ? (
                    <div className="py-8 text-center text-sm text-slate-400">
                      {connected
                        ? "No threshold violations above edge. Scanning…"
                        : "Connecting to backend…"}
                    </div>
                  ) : (
                    activeSignals.map((sig) => (
                      <SignalRow
                        key={sig.id}
                        sig={sig}
                        isTraded={tradedSignalIds.has(sig.id)}
                        onTrade={(id) =>
                          setTradedSignalIds((prev) => new Set([...prev, id]))
                        }
                      />
                    ))
                  )}
                </CardContent>
              </Card>

              {/* Bucket sum arb */}
              <Card className="rounded-3xl shadow-sm border-violet-100">
                <CardHeader className="pb-2">
                  <CardTitle className="flex items-center gap-2">
                    Bucket Sum Arb
                    {activeBucketSignals.length > 0 && (
                      <Badge className="rounded-full bg-violet-100 text-violet-700 text-xs">
                        {activeBucketSignals.length} found
                      </Badge>
                    )}
                  </CardTitle>
                  <p className="text-xs text-slate-400">
                    sum(all bucket asks) &lt; 100¢ → buy all buckets for guaranteed $1.
                    Gross edge = 100¢ − sum(asks). Auto-traded when detected.
                  </p>
                </CardHeader>
                <CardContent className="space-y-2">
                  {activeBucketSignals.length === 0 ? (
                    <div className="py-8 text-center text-sm text-slate-400">
                      No bucket sum violations. Market makers pricing buckets efficiently.
                    </div>
                  ) : (
                    activeBucketSignals.map((sig) => (
                      <BucketSignalRow
                        key={sig.id}
                        sig={sig}
                        isTraded={tradedSignalIds.has(sig.id)}
                      />
                    ))
                  )}
                </CardContent>
              </Card>

              {/* Near misses */}
              {(nearMisses.length > 0 || bucketNearMisses.length > 0) && (
                <Card className="rounded-3xl shadow-sm border-amber-100">
                  <CardHeader className="pb-2">
                    <CardTitle className="flex items-center gap-2 text-amber-700">
                      Near Misses
                      <Badge className="rounded-full bg-amber-100 text-amber-700 text-xs">
                        {nearMisses.length + bucketNearMisses.length}
                      </Badge>
                    </CardTitle>
                    <p className="text-xs text-slate-400">
                      Closest pairs to the trading threshold ({fmtCents(config?.min_gross_edge ?? 0.07)}).
                      Would auto-trade if edge reaches threshold.
                    </p>
                  </CardHeader>
                  <CardContent className="space-y-1.5">
                    {nearMisses.map((sig) => (
                      <NearMissRow
                        key={sig.id}
                        sig={sig}
                        threshold={config?.min_gross_edge ?? 0.07}
                      />
                    ))}
                    {bucketNearMisses.map((sig) => (
                      <div
                        key={sig.id}
                        className="grid gap-2 rounded-xl border border-amber-100 bg-amber-50 p-3 text-xs md:grid-cols-[1.5fr_1fr_1fr_1fr] md:items-center"
                      >
                        <div>
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <span className="font-semibold text-slate-700">{sig.series}</span>
                            <span className="text-[10px] bg-violet-100 text-violet-600 px-1.5 py-0.5 rounded-full">BUCKET</span>
                            <span className="text-[10px] text-slate-300">·</span>
                            <span className="text-[10px] text-slate-400 font-mono">{new Date(sig.detected_at).toLocaleTimeString()}</span>
                          </div>
                          <div className="mt-0.5 text-[10px] text-slate-500 font-mono truncate">{sig.event_ticker}</div>
                          <div className="mt-1 text-[10px] text-slate-400 font-mono">
                            Buy YES ×{sig.bucket_count} · cost {fmtCents(sig.sum_asks)}
                            <span className="text-slate-300 mx-1">·</span>
                            <span className={sig.net_edge >= 0 ? "text-green-600 font-semibold" : "text-red-500 font-semibold"}>
                              net {fmtCents(sig.net_edge)}/contract
                            </span>
                          </div>
                        </div>
                        <div>
                          <div className="text-[10px] text-slate-400 uppercase tracking-wide">Sum asks</div>
                          <div className="font-mono font-medium text-slate-700">{fmtCents(sig.sum_asks)}</div>
                        </div>
                        <div>
                          <div className="text-[10px] text-slate-400 uppercase tracking-wide">Gross edge</div>
                          <div className="font-mono font-medium text-amber-700">{fmtCents(sig.gross_edge)}</div>
                        </div>
                        <div>
                          {(() => {
                            const thr = config?.min_gross_edge ?? 0.07;
                            const bPct = Math.max(0, Math.min(100, Math.round(((sig.gross_edge - (thr - 0.30)) / 0.30) * 100)));
                            return (
                              <>
                                <div className="text-[10px] text-slate-400 mb-1 uppercase tracking-wide">
                                  {bPct}% to threshold
                                </div>
                                <div className="h-1.5 w-full rounded-full bg-amber-200">
                                  <div className="h-1.5 rounded-full bg-amber-500" style={{ width: `${bPct}%` }} />
                                </div>
                              </>
                            );
                          })()}
                        </div>
                      </div>
                    ))}
                  </CardContent>
                </Card>
              )}

              {/* Structural Anomalies (violations) */}
              {structuralAnomalies.length > 0 && (
                <Card className="rounded-3xl shadow-sm border-purple-100">
                  <CardHeader className="pb-2">
                    <CardTitle className="flex items-center gap-2 text-purple-700">
                      Structural Anomalies
                      <Badge className="rounded-full bg-purple-100 text-purple-700 text-xs">
                        {structuralAnomalies.length}
                      </Badge>
                    </CardTitle>
                    <p className="text-xs text-slate-400">
                      Non-adjacent monotonicity violations — one of the middle bands is oddly priced.
                      Review before trading. Payout: $1/contract if arb holds at expiry.
                    </p>
                  </CardHeader>
                  <CardContent className="space-y-1.5">
                    {structuralAnomalies.map((sig) => (
                      <StructuralAnomalyRow key={sig.id} sig={sig} />
                    ))}
                  </CardContent>
                </Card>
              )}

              {/* Structural Near-Misses (manual setups) */}
              {structuralNearMisses.length > 0 && (
                <Card className="rounded-3xl shadow-sm border-slate-100">
                  <CardHeader className="pb-2">
                    <CardTitle className="flex items-center gap-2 text-slate-600">
                      Structural Near-Misses
                      <Badge className="rounded-full bg-slate-100 text-slate-600 text-xs">
                        {structuralNearMisses.length}
                      </Badge>
                    </CardTitle>
                    <p className="text-xs text-slate-400">
                      Non-adjacent pairs closest to arb — not yet profitable but worth watching.
                      Edge gap shows how far from break-even. Manual entry only.
                    </p>
                  </CardHeader>
                  <CardContent className="space-y-1.5">
                    {structuralNearMisses.map((sig) => (
                      <StructuralAnomalyRow key={sig.id} sig={sig} />
                    ))}
                  </CardContent>
                </Card>
              )}

              {/* Mean Reversion — ladder rungs cheap vs both neighbors */}
              {invertedLegs.length > 0 && (
                <Card className="rounded-3xl shadow-sm border-orange-100 bg-orange-50/30">
                  <CardHeader className="pb-2">
                    <CardTitle className="flex items-center gap-2 text-orange-700">
                      🎯 Mean Reversion
                      <Badge className="rounded-full bg-orange-100 text-orange-700 text-xs">
                        {invertedLegs.length}
                      </Badge>
                      <span className="text-[10px] font-normal bg-slate-200 text-slate-500 px-1.5 py-0.5 rounded-full">
                        display only
                      </span>
                    </CardTitle>
                    <p className="text-xs text-orange-600/70">
                      Ladder rungs priced below interpolated fair value from <em>both</em> neighbors.
                      Directional bet on price normalization — enable auto-trading in Config (risky, not true arb).
                    </p>
                  </CardHeader>
                  <CardContent className="space-y-1.5">
                    {invertedLegs.map((sig) => (
                      <InvertedLegRow key={sig.id} sig={sig} />
                    ))}
                  </CardContent>
                </Card>
              )}
            </div>
          </TabsContent>

          {/* Positions */}
          <TabsContent value="positions" className="space-y-5">
            <div className="grid gap-5 lg:grid-cols-[2fr_1fr]">
              {/* P&L chart */}
              <Card className="rounded-3xl shadow-sm">
                <CardHeader className="pb-2">
                  <CardTitle>P&L History</CardTitle>
                  <div className="flex flex-wrap gap-1 pt-1">
                    {(["all", "threshold", "structural", "bucket", "meanrev"] as const).map((s) => (
                      <button
                        key={s}
                        onClick={() => setStratFilter(s)}
                        className={`rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors ${
                          stratFilter === s
                            ? "bg-blue-600 text-white"
                            : "bg-slate-100 text-slate-500 hover:bg-slate-200"
                        }`}
                      >
                        {s === "all" ? "All" : s === "threshold" ? "Threshold" : s === "structural" ? "Structural" : s === "bucket" ? "Bucket" : "Mean-Rev"}
                      </button>
                    ))}
                  </div>
                </CardHeader>
                <CardContent className="h-56 pt-2">
                  {chartData.length === 0 ? (
                    <div className="flex h-full items-center justify-center text-sm text-slate-400">
                      Run a scan to start tracking P&L
                    </div>
                  ) : (
                    <ResponsiveContainer width="100%" height="100%">
                      <AreaChart data={chartData}>
                        <CartesianGrid strokeDasharray="3 3" />
                        <XAxis dataKey="i" hide />
                        <YAxis tickFormatter={(v) => `$${v.toFixed(2)}`} />
                        <Tooltip
                          formatter={(v: number) => [`$${v.toFixed(2)}`]}
                          labelFormatter={() => ""}
                        />
                        {stratFilter === "all" && <>
                          <Area type="monotone" dataKey="total" name="Total P&L" stroke="#2563eb" fill="#dbeafe" strokeWidth={2} />
                          <Area type="monotone" dataKey="realized" name="Realized" stroke="#16a34a" fill="#dcfce7" strokeWidth={1.5} fillOpacity={0.3} />
                        </>}
                        {stratFilter === "threshold" && <Area type="monotone" dataKey="threshold" name="Threshold Arb" stroke="#2563eb" fill="#dbeafe" strokeWidth={2} />}
                        {stratFilter === "structural" && <Area type="monotone" dataKey="structural" name="Structural Arb" stroke="#9333ea" fill="#f3e8ff" strokeWidth={2} />}
                        {stratFilter === "bucket" && <Area type="monotone" dataKey="bucket" name="Bucket Arb" stroke="#ea580c" fill="#ffedd5" strokeWidth={2} />}
                        {stratFilter === "meanrev" && <Area type="monotone" dataKey="meanrev" name="Mean-Rev" stroke="#0891b2" fill="#cffafe" strokeWidth={2} />}
                      </AreaChart>
                    </ResponsiveContainer>
                  )}
                </CardContent>
              </Card>

              {/* Stats */}
              <Card className="rounded-3xl shadow-sm">
                <CardHeader>
                  <CardTitle>Stats</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3 text-sm">
                  {/* Capital row */}
                  {(() => {
                    const current = STARTING_CAPITAL + totalPnl;
                    const pct = (totalPnl / STARTING_CAPITAL) * 100;
                    return (
                      <div className="flex justify-between rounded-xl bg-slate-50 px-3 py-2">
                        <span className="text-slate-500">Capital</span>
                        <span className="font-medium text-right">
                          <span className="text-slate-400">${STARTING_CAPITAL.toLocaleString()} → </span>
                          <span className={totalPnl >= 0 ? "text-emerald-600" : "text-rose-600"}>
                            ${current.toFixed(2)} ({pct >= 0 ? "+" : ""}{pct.toFixed(2)}%)
                          </span>
                        </span>
                      </div>
                    );
                  })()}
                  {[
                    ["Open positions", String(openPos.length + openBucketPos.length + openSinglePos.length)],
                    ["Closed positions", String(closedPos.length + closedBucketPos.length + closedSinglePos.length)],
                    ["Last refresh", botState?.last_scan ? timeSince(botState.last_scan) : "never"],
                  ].map(([label, value]) => (
                    <div key={label} className="flex justify-between rounded-xl bg-slate-50 px-3 py-2">
                      <span className="text-slate-500">{label}</span>
                      <span className="font-medium">{value}</span>
                    </div>
                  ))}
                  {/* Win rate */}
                  <div className="flex justify-between rounded-xl bg-slate-50 px-3 py-2">
                    <span className="text-slate-500">Win rate <span className="text-slate-400">({closedPos.length + closedBucketPos.length + closedSinglePos.length} closed)</span></span>
                    <span className="font-medium">{botState ? fmtPct(botState.win_rate) : "—"}</span>
                  </div>
                  {/* Profit factor */}
                  <div className="flex justify-between rounded-xl bg-slate-50 px-3 py-2">
                    <span className="text-slate-500">Profit factor</span>
                    <span className={`font-medium ${overallProfitFactor === null ? "text-slate-400" : overallProfitFactor >= 1.5 ? "text-emerald-600" : overallProfitFactor >= 1 ? "text-amber-600" : "text-rose-600"}`}>
                      {overallProfitFactor === null ? "—" : overallProfitFactor === Infinity ? "∞" : overallProfitFactor.toFixed(2)}
                    </span>
                  </div>
                  {/* Sharpe */}
                  {sharpe !== null && (
                    <div className="flex justify-between rounded-xl bg-slate-50 px-3 py-2">
                      <span className="text-slate-500">Sharpe (ann.)</span>
                      <span className={`font-medium ${sharpe >= 2 ? "text-emerald-600" : sharpe >= 1 ? "text-amber-600" : "text-rose-600"}`}>
                        {sharpe.toFixed(2)}
                      </span>
                    </div>
                  )}
                  <div className="pt-1 text-xs font-semibold text-slate-400 uppercase tracking-wide">By Strategy</div>
                  {([
                    ["Threshold", "threshold_arb"],
                    ["Structural", "structural_arb"],
                    ["Bucket", "bucket_arb"],
                    ["Mean-Rev", "mispriced_leg"],
                  ] as const).map(([label, key]) => {
                    const st = stratStats[key];
                    if (!st || st.count === 0) return null;
                    const winPct = st.count > 0 ? Math.round(st.wins / st.count * 100) : 0;
                    const avgPnl = st.pnl / st.count;
                    const pf = st.grossLosses === 0 ? (st.grossWins > 0 ? Infinity : null) : st.grossWins / st.grossLosses;
                    return (
                      <div key={key} className="rounded-xl bg-slate-50 px-3 py-2 space-y-1">
                        <div className="flex justify-between">
                          <span className="text-slate-500 font-medium">{label}</span>
                          <span className={`font-mono font-semibold ${st.pnl >= 0 ? "text-emerald-600" : "text-rose-600"}`}>
                            {st.pnl >= 0 ? "+" : ""}{st.pnl.toFixed(2)}
                          </span>
                        </div>
                        <div className="flex justify-between text-xs text-slate-400">
                          <span>{st.count} trades · {winPct}% win · avg {avgPnl >= 0 ? "+" : ""}{avgPnl.toFixed(2)}</span>
                          <span>PF: {pf === null ? "—" : pf === Infinity ? "∞" : pf.toFixed(2)}</span>
                        </div>
                        {Object.keys(st.exitReasons).length > 0 && (
                          <div className="text-xs text-slate-400">
                            {Object.entries(st.exitReasons).map(([r, n]) => `${r}: ${n}`).join(" · ")}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </CardContent>
              </Card>
            </div>

            {/* Open positions */}
            {openPos.length > 0 && (
              <Card className="rounded-3xl shadow-sm">
                <CardHeader>
                  <CardTitle>Open Positions ({openPos.length})</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2">
                  {openPos.map((p) => (
                    <PositionRow key={p.id} pos={p} />
                  ))}
                </CardContent>
              </Card>
            )}

            {/* Closed positions */}
            {closedPos.length > 0 && (
              <Card className="rounded-3xl shadow-sm">
                <CardHeader>
                  <CardTitle>Closed Positions ({closedPos.length})</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2">
                  {[...closedPos].reverse().map((p) => (
                    <PositionRow key={p.id} pos={p} />
                  ))}
                </CardContent>
              </Card>
            )}

            {/* Open bucket positions */}
            {openBucketPos.length > 0 && (
              <Card className="rounded-3xl shadow-sm border-violet-100">
                <CardHeader>
                  <CardTitle>Open Bucket Positions ({openBucketPos.length})</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2">
                  {openBucketPos.map((p) => (
                    <BucketPositionRow key={p.id} pos={p} />
                  ))}
                </CardContent>
              </Card>
            )}

            {/* Closed bucket positions */}
            {closedBucketPos.length > 0 && (
              <Card className="rounded-3xl shadow-sm border-violet-100">
                <CardHeader>
                  <CardTitle>Closed Bucket Positions ({closedBucketPos.length})</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2">
                  {[...closedBucketPos].reverse().map((p) => (
                    <BucketPositionRow key={p.id} pos={p} />
                  ))}
                </CardContent>
              </Card>
            )}

            {/* Open single-leg positions */}
            {openSinglePos.length > 0 && (
              <Card className="rounded-3xl shadow-sm border-orange-100">
                <CardHeader className="flex flex-row items-center justify-between">
                  <CardTitle className="text-orange-700">Open Mean-Reversion Positions ({openSinglePos.length})</CardTitle>
                  <button
                    className="text-xs bg-red-500 text-white rounded-xl px-3 py-1.5 font-semibold hover:bg-red-600"
                    onClick={async () => {
                      if (!confirm(`Flatten all ${openSinglePos.length} open mean-reversion positions at current market prices?`)) return;
                      const res = await fetch(`${API}/inverted/flatten-all`, { method: "POST" });
                      const d = await res.json().catch(() => ({}));
                      alert(res.ok ? `Closed ${d.closed} positions, P&L: ${d.total_pnl?.toFixed(2)}` : "Failed: " + (d.detail || res.statusText));
                    }}
                  >
                    Flatten All
                  </button>
                </CardHeader>
                <CardContent className="space-y-2">
                  {openSinglePos.map((p) => (
                    <SingleLegPositionRow key={p.id} pos={p} />
                  ))}
                </CardContent>
              </Card>
            )}

            {/* Closed single-leg positions */}
            {closedSinglePos.length > 0 && (
              <Card className="rounded-3xl shadow-sm border-orange-100">
                <CardHeader>
                  <CardTitle className="text-orange-700">Closed Mean-Reversion Positions ({closedSinglePos.length})</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2">
                  {[...closedSinglePos].reverse().map((p) => (
                    <SingleLegPositionRow key={p.id} pos={p} />
                  ))}
                </CardContent>
              </Card>
            )}

            {openPos.length === 0 && closedPos.length === 0 && openBucketPos.length === 0 && closedBucketPos.length === 0 && openSinglePos.length === 0 && (
              <div className="py-12 text-center text-sm text-slate-400">
                No positions yet — start the bot to begin trading.
              </div>
            )}
          </TabsContent>

          {/* Trades */}
          <TabsContent value="trades">
            <Card className="rounded-3xl shadow-sm">
              <CardHeader>
                <CardTitle>Trade Log ({trades.length})</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                {trades.length === 0 ? (
                  <div className="py-12 text-center text-sm text-slate-400">
                    No trades yet.
                  </div>
                ) : (
                  [...trades].reverse().map((t) => <TradeRow key={t.id} trade={t} />)
                )}
              </CardContent>
            </Card>
          </TabsContent>

          {/* Config */}
          <TabsContent value="config">
            <Card className="rounded-3xl shadow-sm">
              <CardHeader>
                <CardTitle>Strategy Config</CardTitle>
                <p className="text-xs text-slate-400">
                  Changes take effect on the next scan.
                </p>
              </CardHeader>
              <CardContent>
                <ConfigPanel config={config} onUpdate={updateConfig} />
              </CardContent>
            </Card>

            {/* Strategy explanation */}
            <Card className="rounded-3xl shadow-sm mt-4">
              <CardHeader>
                <CardTitle>How It Works</CardTitle>
              </CardHeader>
              <CardContent className="text-sm text-slate-600 space-y-2">
                <p>
                  <strong>The setup:</strong> On a price ladder (e.g. BTC closes above $90k /
                  $95k / $100k), the lower threshold must be priced higher — it's easier to clear.
                  When that relationship breaks, there's a free lunch.
                </p>
                <p>
                  <strong>The trade:</strong> Buy YES at the lower threshold + buy NO at the
                  higher threshold. Total cost = ask(lower) + (1 − bid(higher)). Since one of
                  the two legs must win (X is either above the higher bar, between the bars, or
                  below the lower bar), you always collect at least $1 per contract.
                </p>
                <p>
                  <strong>Profit per contract:</strong> $1 − entry cost = gross edge. If X lands
                  between the two levels, both legs win and you collect $2 instead (middle-band
                  bonus shown as "X% both").
                </p>
                <p>
                  <strong>Fees:</strong> Kalshi charges{" "}
                  {fmtPct(config?.fee_rate ?? 0.07)} of winnings per leg. Worst case (one leg
                  wins): net edge = gross edge − {fmtCents(config?.fee_rate ?? 0.07)}.
                </p>
                <p>
                  <strong>Sizing:</strong> Capped at {config?.max_size ?? 10} contracts.
                  Depth shown as actual orderbook qty at the target bid/ask prices.
                </p>
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
