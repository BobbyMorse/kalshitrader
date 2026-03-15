// Threshold monotonicity arb bot — frontend types

export interface BotState {
  running: boolean;
  scanning: boolean;
  last_scan: string | null;
  scan_count: number;
  auth_method: string;
  markets_fetched: number;
  groups_found: number;
  feed_connected: boolean;
  ticks_received: number;
  realized_pnl: number;
  unrealized_pnl: number;
  total_pnl: number;
  open_positions: number;
  closed_positions: number;
  total_trades: number;
  win_rate: number;
  paper_trading: boolean;
}

export interface BotConfig {
  min_gross_edge: number;   // e.g. 0.10 = 10 cents
  max_size: number;
  fee_rate: number;
  refresh_interval: number;  // seconds between full REST refreshes
  auto_trade: boolean;
  paper_trading: boolean;
}

export interface ViolationSignal {
  id: string;
  series: string;
  expiry: string;
  lower_ticker: string;
  higher_ticker: string;
  lower_threshold: number;
  higher_threshold: number;
  lower_ask: number;        // 0-1
  higher_bid: number;       // 0-1
  gross_edge: number;       // bid(higher) - ask(lower)
  net_edge: number;         // gross_edge - fee_rate
  entry_cost: number;       // ask(lower) + (1 - bid(higher))
  avail_size: number;
  detected_at: string;
  event_ticker?: string;
}

export interface Position {
  id: string;
  signal_id: string;
  series: string;
  expiry: string;
  lower_ticker: string;
  higher_ticker: string;
  lower_threshold: number;
  higher_threshold: number;
  size: number;
  lower_entry: number;      // ask paid for YES at lower
  higher_entry: number;     // (1-bid) paid for NO at higher
  entry_cost: number;
  entry_time: string;
  gross_edge: number;
  net_edge: number;
  status: string;           // "open" | "closed" | "one_leg_risk"
  strategy: string;         // "threshold_arb" | "structural_arb"
  lower_mid: number;
  higher_no_mid: number;
  unrealized_pnl: number;
  realized_pnl: number;
  fees_paid: number;
  exit_time: string | null;
  exit_reason: string;
}

export interface TradeRecord {
  id: string;
  position_id: string;
  timestamp: string;
  action: string;           // "OPEN" | "CLOSE_EXPIRY" | "CLOSE_FLATTEN"
  series: string;
  lower_ticker: string;
  higher_ticker: string;
  lower_threshold: number;
  higher_threshold: number;
  size: number;
  lower_entry: number;
  higher_entry: number;
  gross_edge: number;
  net_edge: number;
  pnl: number | null;
  fees: number;
  status: string;           // "paper_filled" | "expired" | "flattened"
  strategy: string;         // "threshold_arb" | "structural_arb" | "bucket_arb"
}

export interface PnlPoint {
  time: string;
  realized: number;
  unrealized: number;
  total: number;
  open_positions: number;
}

export interface BucketEntry {
  ticker: string;
  floor: number;
  ask: number;
}

export interface BucketSignal {
  id: string;
  type: 'bucket_sum';
  series: string;
  expiry: string;
  event_ticker: string;
  bucket_count: number;
  sum_asks: number;
  gross_edge: number;
  net_edge: number;
  avail_size: number;
  detected_at: string;
  buckets: BucketEntry[];
}

export interface BucketPosition {
  id: string;
  type: 'bucket_sum';
  strategy: string;         // "bucket_arb"
  signal_id: string;
  series: string;
  expiry: string;
  event_ticker: string;
  bucket_count: number;
  size: number;
  entry_cost: number;
  gross_edge: number;
  net_edge: number;
  status: string;
  unrealized_pnl: number;
  realized_pnl: number;
  fees_paid: number;
  entry_time: string;
  exit_time: string | null;
  exit_reason: string;
}

export interface StructuralMiddleMarket {
  ticker: string;
  threshold: number;
  yes_bid: number;
  yes_ask: number;
}

export interface StructuralAnomaly {
  id: string;
  series: string;
  expiry: string;
  lower_ticker: string;
  higher_ticker: string;
  lower_threshold: number;
  higher_threshold: number;
  lower_ask: number;
  higher_bid: number;
  gross_edge: number;
  net_edge: number;
  entry_cost: number;
  avail_size: number;
  detected_at: string;
  gap: number;                            // index distance between lower and higher
  event_ticker?: string;
  middle_markets: StructuralMiddleMarket[];
}

export interface WsMessage {
  type: string;
  bot_state?: BotState;
  config?: BotConfig;
  signals?: ViolationSignal[];
  near_misses?: ViolationSignal[];
  bucket_signals?: BucketSignal[];
  bucket_near_misses?: BucketSignal[];
  structural_anomalies?: StructuralAnomaly[];
  positions?: (Position | BucketPosition)[];
  trades?: TradeRecord[];
  pnl_history?: PnlPoint[];
  running?: boolean;
}
