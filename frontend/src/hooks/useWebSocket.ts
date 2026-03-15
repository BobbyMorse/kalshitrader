import { useCallback, useEffect, useRef, useState } from "react";
import {
  BotConfig,
  BotState,
  BucketPosition,
  BucketSignal,
  PnlPoint,
  Position,
  StructuralAnomaly,
  TradeRecord,
  ViolationSignal,
  WsMessage,
} from "../types";

const WS_URL =
  (import.meta.env.VITE_WS_URL as string | undefined) ||
  `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws`;

export interface BotData {
  botState: BotState | null;
  config: BotConfig | null;
  signals: ViolationSignal[];
  nearMisses: ViolationSignal[];
  bucketSignals: BucketSignal[];
  bucketNearMisses: BucketSignal[];
  structuralAnomalies: StructuralAnomaly[];
  positions: (Position | BucketPosition)[];
  trades: TradeRecord[];
  pnlHistory: PnlPoint[];
  connected: boolean;
  error: string | null;
}

export interface BotControls {
  startBot: () => void;
  stopBot: () => void;
  triggerScan: () => void;
  updateConfig: (patch: Partial<BotConfig>) => void;
}

export function useWebSocket(): BotData & BotControls {
  const ws = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout>>();
  const pingRef = useRef<ReturnType<typeof setInterval>>();

  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [botState, setBotState] = useState<BotState | null>(null);
  const [config, setConfig] = useState<BotConfig | null>(null);
  const [signals, setSignals] = useState<ViolationSignal[]>([]);
  const [nearMisses, setNearMisses] = useState<ViolationSignal[]>([]);
  const [bucketSignals, setBucketSignals] = useState<BucketSignal[]>([]);
  const [bucketNearMisses, setBucketNearMisses] = useState<BucketSignal[]>([]);
  const [structuralAnomalies, setStructuralAnomalies] = useState<StructuralAnomaly[]>([]);
  const [positions, setPositions] = useState<(Position | BucketPosition)[]>([]);
  const [trades, setTrades] = useState<TradeRecord[]>([]);
  const [pnlHistory, setPnlHistory] = useState<PnlPoint[]>([]);

  const send = useCallback((msg: object) => {
    if (ws.current?.readyState === WebSocket.OPEN) {
      ws.current.send(JSON.stringify(msg));
    }
  }, []);

  const connect = useCallback(() => {
    if (ws.current?.readyState === WebSocket.OPEN) return;

    const socket = new WebSocket(WS_URL);
    ws.current = socket;

    socket.onopen = () => {
      setConnected(true);
      setError(null);
      pingRef.current = setInterval(() => send({ type: "ping" }), 25_000);
    };

    socket.onmessage = (ev) => {
      try {
        const msg: WsMessage = JSON.parse(ev.data as string);

        if (msg.bot_state) setBotState(prev => prev ? { ...prev, ...msg.bot_state } : (msg.bot_state ?? null));
        if (msg.config) setConfig(msg.config);
        if (msg.signals) setSignals(msg.signals);
        if (msg.near_misses) setNearMisses(msg.near_misses);
        if (msg.bucket_signals) setBucketSignals(msg.bucket_signals);
        if (msg.bucket_near_misses) setBucketNearMisses(msg.bucket_near_misses);
        if (msg.structural_anomalies) setStructuralAnomalies(msg.structural_anomalies);
        if (msg.positions) setPositions(msg.positions);
        if (msg.trades) setTrades(msg.trades);
        if (msg.pnl_history) setPnlHistory(msg.pnl_history);

        if (msg.type === "status" && msg.running !== undefined) {
          setBotState((prev) => (prev ? { ...prev, running: msg.running! } : null));
        }
        if (msg.type === "config_update" && msg.config) {
          setConfig(msg.config);
        }
      } catch {
        // ignore malformed frames
      }
    };

    socket.onerror = () => setError("WebSocket error — retrying…");

    socket.onclose = () => {
      setConnected(false);
      clearInterval(pingRef.current);
      reconnectRef.current = setTimeout(connect, 5_000);
    };
  }, [send]);

  useEffect(() => {
    connect();
    return () => {
      ws.current?.close();
      clearTimeout(reconnectRef.current);
      clearInterval(pingRef.current);
    };
  }, [connect]);

  const startBot = useCallback(() => send({ type: "start" }), [send]);
  const stopBot = useCallback(() => send({ type: "stop" }), [send]);
  const triggerScan = useCallback(() => send({ type: "scan" }), [send]);
  const updateConfig = useCallback(
    (patch: Partial<BotConfig>) => send({ type: "config", config: patch }),
    [send]
  );

  return {
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
  };
}
