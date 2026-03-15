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
import { BotConfig, BucketPosition, BucketSignal, Position, StructuralAnomaly, TradeRecord, ViolationSignal } from "./types";

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

  const edgeColor =
    sig.gross_edge >= 0.15
      ? "text-emerald-700 font-bold"
      : sig.gross_edge >= 0.10
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
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Gross edge</div>
        <div className={`font-mono text-base ${edgeColor}`}>{fmtCents(sig.gross_edge)}</div>
        <div className="text-[10px] text-slate-400">net {fmtCents(sig.net_edge)}</div>
      </div>

      {/* Entry cost + size */}
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Entry cost</div>
        <div className="font-mono font-medium text-slate-700">{fmtCents(sig.entry_cost)}</div>
        <div className="text-[10px] text-slate-400">avail {sig.avail_size} cts</div>
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
        alert(`Trade failed: ${err.detail ?? res.statusText}`);
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
            <span className={sig.gross_edge >= 0 ? "text-green-600 font-semibold" : "text-red-500 font-semibold"}>
              net {fmtCents(sig.gross_edge)}/contract
            </span>
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
          <span className={sig.gross_edge >= 0 ? "text-green-600 font-semibold" : "text-red-500 font-semibold"}>
            net {fmtCents(sig.gross_edge)}/contract
          </span>
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
        <div className="text-[10px] text-slate-400">
          expires {expiryIn(pos.expiry)} · entered {timeSince(pos.entry_time)}
        </div>
      </div>

      {/* Size + entry */}
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Size</div>
        <div className="font-medium">{pos.size} cts</div>
        <div className="text-[10px] text-slate-400">cost {fmtCents(pos.entry_cost)}/pair</div>
      </div>

      {/* Gross edge */}
      <div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">Entry edge</div>
        <div className="font-mono text-emerald-600 font-semibold">{fmtCents(pos.gross_edge)}</div>
        <div className="text-[10px] text-slate-400">net {fmtCents(pos.net_edge)}</div>
      </div>

      {/* Locked P&L at expiry */}
      {isOpen ? (
        <div>
          <div className="text-[10px] text-slate-400 uppercase tracking-wide">Locked at expiry</div>
          <div className={`font-mono font-semibold ${pnlColor(pos.net_edge * pos.size)}`}>
            {fmtPnl(pos.net_edge * pos.size)}
          </div>
          <div className="text-[10px] text-slate-400">net {fmtCents(pos.net_edge)}/ct · fees incl.</div>
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
          {isOpen ? "Mid P&L (est.)" : "Realized"}
        </div>
        <div className={`font-mono text-sm ${pnlColor(pnl)} opacity-50`}>
          {fmtPnl(pnl)}
        </div>
        {isOpen && (
          <div className="text-[10px] text-slate-300 italic">spread-distorted</div>
        )}
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
      <div className="sm:col-span-3 flex items-center gap-3">
        <Button size="sm" className="rounded-2xl" onClick={save}>
          Save Config
        </Button>
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
  const seenTradeIds = useRef<Set<string>>(new Set());

  // Request browser notification permission on first render
  useEffect(() => { requestNotificationPermission(); }, []);

  // Fire notification on every new OPEN trade
  useEffect(() => {
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
    // Also mark any pre-existing trades as seen on first load
    if (newOpens.length === 0) {
      trades.forEach((t) => seenTradeIds.current.add(t.id));
    }
  }, [trades]);

  // Clear unseen badge when user clicks Trades tab
  useEffect(() => {
    if (activeTab === "trades") setUnseenTrades(0);
  }, [activeTab]);

  // Separate threshold vs bucket positions
  const thresholdPositions = positions.filter((p) => (p as any).type !== "bucket_sum") as Position[];
  const bucketPositions = positions.filter((p) => (p as any).type === "bucket_sum") as BucketPosition[];

  const openPos = thresholdPositions.filter((p) => p.status !== "closed");
  const closedPos = thresholdPositions.filter((p) => p.status === "closed");
  const openBucketPos = bucketPositions.filter((p) => p.status !== "closed");
  const closedBucketPos = bucketPositions.filter((p) => p.status === "closed");

  const positionedIds = new Set([
    ...openPos.map((p) => p.signal_id),
    ...openBucketPos.map((p) => p.signal_id),
  ]);

  const activeSignals = signals.filter((s) => !positionedIds.has(s.id));
  const activeBucketSignals = bucketSignals.filter((s) => !positionedIds.has(s.id));
  const totalActiveSignals = activeSignals.length + activeBucketSignals.length;

  // PnL chart
  const chartData = useMemo(
    () =>
      pnlHistory.map((p, i) => ({
        i,
        total: p.total,
        realized: p.realized,
        unrealized: p.unrealized,
      })),
    [pnlHistory]
  );

  // Totals
  const realizedPnl = botState?.realized_pnl ?? 0;
  const unrealizedPnl = botState?.unrealized_pnl ?? 0;
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
                <span>Threshold Monotonicity Arb</span>
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
                Kalshi Threshold Ladder Scanner
              </h1>
              <p className="mt-1 text-sm text-slate-500 max-w-xl">
                Detects violations of P(X≥a) ≥ P(X≥b) for a &lt; b. Trades: buy YES at lower +
                buy NO at higher for guaranteed $1 minimum payout per pair.
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
                label="Unrealized"
                value={fmtPnl(unrealizedPnl)}
                sub={`${botState?.open_positions ?? 0} open`}
              />
              <PnlCard
                label="Win rate"
                value={botState ? fmtPct(botState.win_rate) : "—"}
                sub={`${botState?.total_trades ?? 0} trades`}
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
              Positions {openPos.length > 0 && `(${openPos.length})`}
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
                            <span className={sig.gross_edge >= 0 ? "text-green-600 font-semibold" : "text-red-500 font-semibold"}>
                              net {fmtCents(sig.gross_edge)}/contract
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

              {/* Structural Anomalies */}
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
            </div>
          </TabsContent>

          {/* Positions */}
          <TabsContent value="positions" className="space-y-5">
            <div className="grid gap-5 lg:grid-cols-[2fr_1fr]">
              {/* P&L chart */}
              <Card className="rounded-3xl shadow-sm">
                <CardHeader>
                  <CardTitle>P&L History</CardTitle>
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
                        <Area
                          type="monotone"
                          dataKey="total"
                          name="Total P&L"
                          stroke="#2563eb"
                          fill="#dbeafe"
                          strokeWidth={2}
                        />
                        <Area
                          type="monotone"
                          dataKey="realized"
                          name="Realized"
                          stroke="#16a34a"
                          fill="#dcfce7"
                          strokeWidth={1.5}
                          fillOpacity={0.3}
                        />
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
                  {[
                    ["Open positions", String(openPos.length)],
                    ["Closed positions", String(closedPos.length)],
                    ["Win rate", botState ? fmtPct(botState.win_rate) : "—"],
                    ["Total trades", String(botState?.total_trades ?? 0)],
                    ["Refresh count", String(botState?.scan_count ?? 0)],
                    ["Ticks received", (botState?.ticks_received ?? 0).toLocaleString()],
                    [
                      "Last refresh",
                      botState?.last_scan ? timeSince(botState.last_scan) : "never",
                    ],
                  ].map(([label, value]) => (
                    <div key={label} className="flex justify-between rounded-xl bg-slate-50 px-3 py-2">
                      <span className="text-slate-500">{label}</span>
                      <span className="font-medium">{value}</span>
                    </div>
                  ))}
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

            {openPos.length === 0 && closedPos.length === 0 && openBucketPos.length === 0 && closedBucketPos.length === 0 && (
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
                  <strong>Monotonicity constraint:</strong> For two threshold levels a &lt; b on
                  the same underlying and expiry, the market price of P(X≥a) must be ≥ P(X≥b),
                  since exceeding a lower bar is strictly more likely.
                </p>
                <p>
                  <strong>Violation signal:</strong>{" "}
                  <code className="bg-slate-100 px-1 rounded">bid(b) − ask(a) &gt; threshold</code>.
                  This means you can simultaneously buy YES at level a and buy NO at level b for
                  a total cost less than $1, while the combined position pays at least $1 in all
                  outcomes.
                </p>
                <p>
                  <strong>Outcomes:</strong> If X≥b: YES_a pays $1, NO_b pays $0. If a≤X&lt;b:
                  both pay $1 ($2 total). If X&lt;a: YES_a pays $0, NO_b pays $1. Minimum is
                  always $1 per pair.
                </p>
                <p>
                  <strong>Fees:</strong> Kalshi charges{" "}
                  {fmtPct(config?.fee_rate ?? 0.07)} of gross winnings. Worst case: one leg
                  wins $1, fee = {fmtCents(config?.fee_rate ?? 0.07)}.
                  Net edge = gross_edge − {fmtCents(config?.fee_rate ?? 0.07)}.
                </p>
                <p>
                  <strong>Sizing:</strong> Capped at {config?.max_size ?? 10} contracts.
                  Available liquidity is estimated from open interest.
                </p>
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
