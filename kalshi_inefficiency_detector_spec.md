# Kalshi Structural Inefficiency Detector and Execution System

## Goal
Build an automated system that detects and trades structural pricing inefficiencies across related Kalshi markets, with a primary focus on:

1. **Mutually exclusive bucket bundles** that should sum to 100 cents.
2. **Binary complements** where YES + NO should equal 100 cents after fees/slippage.
3. **Cross-market consistency constraints** across related contracts.
4. **Passive market making around mispriced structures**, not just pure taker arbitrage.

The system should:
- Continuously ingest Kalshi market/order book data.
- Normalize markets into a common mathematical representation.
- Detect arbitrage and quasi-arbitrage opportunities after fees and slippage.
- Decide whether to trade as a maker, taker, or hybrid.
- Manage inventory and risk.
- Log every decision for later analysis.

---

## Non-Goals
This system is **not** intended to:
- Predict event outcomes using deep fundamental models.
- Trade news latency or informational edge.
- Compete on ultra-low-latency infrastructure.
- Run unconstrained directional risk.

This is a **structure-first** system, not a pure forecasting bot.

---

## Core Thesis
Kalshi and similar prediction markets can exhibit persistent inefficiencies because:
- Liquidity is fragmented.
- Related contracts are priced independently.
- Many participants do not enforce probability consistency.
- Order books are thin and stale.

That creates opportunities where:
- A bundle of outcomes can be bought for **less than guaranteed payoff**.
- A bundle of outcomes can be sold for **more than guaranteed liability**.
- Related markets violate monotonicity or inclusion constraints.
- Wide spreads allow passive quoting around mathematically anchored fair values.

The system should treat prediction markets like a constrained pricing graph and continuously search for violated identities and inequalities.

---

## Trading Modes
The bot should support three modes.

### 1. Pure Arbitrage
Take immediate trades when a guaranteed-profit bundle exists after fees/slippage.

Examples:
- Buy complete mutually exclusive set for < 100.
- Sell complete mutually exclusive set for > 100.
- Buy YES and NO if combined ask is too low.
- Sell YES and NO if combined bid is too high.

### 2. Passive Structural Market Making
When markets are near, but not at, an arbitrage boundary:
- Post bids/offers around implied fair values.
- Let others cross into your quotes.
- Profit from spread plus expected reversion to consistency.

### 3. Inventory Rebalancing / Unwind
Use the structure of related markets to flatten exposure at better prices than crossing the spread blindly.

---

## Universe of Opportunities

### A. Mutually Exclusive Bucket Markets
Examples:
- CPI release in ranges
- Temperature in ranges
- Unemployment in ranges

If outcomes are exhaustive and mutually exclusive, then:

`sum(p_i) = 100`

For executable trading, use book prices rather than mid prices.

#### Buy-side bundle arbitrage
If buying one share of every bucket costs < 100 after fees:
- Buy all buckets.
- One and only one settles at 100.
- Guaranteed profit = 100 - total_cost - fees.

#### Sell-side bundle arbitrage
If selling one share of every bucket yields > 100 after fees:
- Sell all buckets.
- Maximum liability = 100.
- Guaranteed profit = total_credit - 100 - fees.

### B. Binary Complement Markets
For a single binary event:

`YES + NO = 100`

At the order book level:
- Best YES ask and best NO ask can imply a buy-bundle arbitrage.
- Best YES bid and best NO bid can imply a sell-bundle arbitrage.

### C. Cross-Market Inclusion Constraints
Examples:
- `P(A and B) <= P(A)`
- `P(A and B) <= P(B)`
- `P(A) <= P(A or B)`
- `P(X by 2026) <= P(X by 2027)`

These are not always guaranteed arbitrage in one step, but they create:
- Mispricing signals
- Spread quoting anchors
- Relative value trades

### D. Time Monotonicity Markets
Examples:
- Recession by 2026
- Recession by 2027
- Recession by 2028

These must satisfy monotonicity:

`P(by T1) <= P(by T2)` for `T1 < T2`

Violations are strong trading candidates.

### E. Partition / Decomposition Constraints
Examples:
- `P(A) = P(A and B) + P(A and not B)`
- Candidate vote-share bucket markets that should aggregate to a broader event.

These are more complex but valuable once the first system version works.

---

## High-Level Architecture

### Services
1. **Market Discovery Service**
2. **Data Ingestion Service**
3. **Normalization / Contract Graph Builder**
4. **Opportunity Engine**
5. **Execution Engine**
6. **Risk Engine**
7. **Portfolio / Position Service**
8. **Persistence + Analytics**
9. **Backtest / Replay Engine**
10. **Operator Dashboard / Alerts**

A pragmatic first version can run as one process with clear modules, but the code should be structured so it can later be split into services.

---

## Recommended Tech Stack
Use Python first unless there is a proven performance problem.

### Language
- Python 3.11+

### Useful libraries
- `httpx` or `aiohttp` for async HTTP
- `websockets` if streaming is available
- `pydantic` for typed models
- `sqlalchemy` for persistence
- `pandas` for research/reporting
- `numpy` for calculations
- `networkx` optional for graph constraints
- `pytest` for tests
- `tenacity` for retries
- `apscheduler` or internal async scheduling loop

### Storage
- PostgreSQL for durable state
- Redis optional for fast book snapshots / locks / queues

### Deployment
- Docker
- One VPS or cloud instance to start
- systemd / Docker Compose for stability

---

## Suggested Repository Structure

```text
kalshi-structure-mm/
  app/
    config.py
    main.py
    logging.py
    models/
      market.py
      book.py
      positions.py
      opportunities.py
      orders.py
    connectors/
      kalshi_rest.py
      kalshi_ws.py
    discovery/
      market_catalog.py
      relationship_builder.py
    normalization/
      canonical_market.py
      constraint_parser.py
    opportunity/
      bundle_arbitrage.py
      complement_arbitrage.py
      monotonicity.py
      inclusion_constraints.py
      fair_value.py
    execution/
      order_manager.py
      quote_manager.py
      execution_planner.py
    risk/
      limits.py
      inventory.py
      kill_switch.py
    portfolio/
      position_service.py
      pnl.py
    persistence/
      db.py
      repositories.py
    backtest/
      replay.py
      simulator.py
    dashboard/
      api.py
  scripts/
  tests/
  docs/
  pyproject.toml
  README.md
```

---

## Data Model

### Market
Represents a Kalshi contract and metadata.

Fields:
- `market_id`
- `event_id`
- `ticker`
- `title`
- `subtitle`
- `category`
- `status`
- `settlement_rules`
- `expiration_ts`
- `outcome_type` (binary, bucket, range, time-series, etc.)
- `group_key` (used to cluster related markets)
- `raw_metadata`

### Order Book Snapshot
Fields:
- `market_id`
- `ts`
- `yes_bid`
- `yes_ask`
- `no_bid`
- `no_ask`
- depth ladders if available
- `last_trade`
- `volume`
- `open_interest`

### Canonical Contract Node
Normalized representation used by the opportunity engine.

Fields:
- `node_id`
- `market_id`
- `proposition`
- `domain`
- `start_time`
- `end_time`
- `bucket_low`
- `bucket_high`
- `direction`
- `is_exhaustive_member`
- `parent_group`

### Constraint Edge
Represents a mathematical relationship.

Types:
- `SUM_TO_100`
- `COMPLEMENT`
- `LEQ`
- `GEQ`
- `PARTITION`
- `EQUALITY`

Fields:
- `constraint_id`
- `constraint_type`
- `members`
- `expression`
- `notes`

### Opportunity
Fields:
- `opportunity_id`
- `type`
- `markets_involved`
- `direction`
- `gross_edge_cents`
- `estimated_fees_cents`
- `estimated_slippage_cents`
- `net_edge_cents`
- `max_size`
- `confidence`
- `created_ts`
- `expires_ts`
- `status`

---

## Normalization Layer
This is one of the most important parts of the system.

The bot should not reason about raw market strings alone. It should map markets into canonical propositions.

Examples:
- "Will CPI YoY for April 2026 be below 2.5%?"
- "Will CPI YoY for April 2026 be between 2.5% and 3.0%?"
- "Will CPI YoY for April 2026 be above 3.5%?"

These should all normalize to a shared event family like:
- `domain = CPI_YOY`
- `reference_period = 2026-04`
- bucket bounds
- same `parent_group`

### Requirements
- Parse titles/subtitles/rules into normalized semantics where possible.
- Allow manual overrides in a config file for messy markets.
- Maintain a relationship registry so new markets can be grouped correctly.

### Practical advice
Start with **manual rule-based normalization** for a few categories:
- temperature ranges
- CPI buckets
- unemployment buckets
- recession-by-date markets

Do not start with a general NLP parser. It will slow implementation and add fragility.

---

## Opportunity Detection Logic

### 1. Bundle Arbitrage Detector
Input:
- set of exhaustive mutually exclusive contracts
- current executable prices
- fee model

#### Buy bundle check
Use asks if buying.

`bundle_buy_cost = sum(best_ask_i)`

If:

`bundle_buy_cost + fees + slippage_buffer < 100`

then emit `BUY_BUNDLE` opportunity.

#### Sell bundle check
Use bids if selling.

`bundle_sell_credit = sum(best_bid_i)`

If:

`bundle_sell_credit - fees - slippage_buffer > 100`

then emit `SELL_BUNDLE` opportunity.

### 2. Complement Arbitrage Detector
For YES/NO complements of the same event:

Buy both side bundle:
`yes_ask + no_ask < 100 - cost_buffer`

Sell both side bundle:
`yes_bid + no_bid > 100 + cost_buffer`

### 3. Monotonicity Detector
For ordered events such as `P(X by 2026)` and `P(X by 2027)`:

Expected:
`price_2026 <= price_2027`

Violation score:
`violation = price_2026 - price_2027`

If executable violation exceeds threshold, emit opportunity.

Execution may be:
- sell near-term contract / buy long-term contract
- or passive quoting to collect convergence

### 4. Inclusion Constraint Detector
For any relationship where `P(A and B) <= P(A)`:

If market for intersection > market for superset, emit relative value opportunity.

### 5. Synthetic Fair Value Engine
When there is no immediate arbitrage, derive fair value from related contracts.

Examples:
- Implied bucket price from neighboring buckets.
- Implied broader event probability from narrower event family.
- Implied complement from opposite side.

Use this for passive market making.

---

## Execution Philosophy
The system should not always cross the spread just because a theoretical edge exists.

For each opportunity, decide whether the best action is:
- **Take immediately**
- **Post and wait**
- **Leg partially and then quote remainder**
- **Skip because inventory/risk costs dominate**

### Decision criteria
- Net edge after fees
- Available size
- Fill probability
- Information risk
- Current inventory
- Time to expiry
- Whether one leg is much more liquid than others

---

## Execution Engine Requirements

### Order manager must support
- Place order
- Amend order if supported; otherwise cancel/replace
- Cancel order
- Track acknowledgements
- Track partial fills
- Reconcile open orders periodically
- Handle network/API failures safely

### Execution planner
Given an opportunity, determine:
- Order sequence
- Size
- Aggressiveness
- Whether to leg or work passively
- Maximum acceptable residual exposure

### Important rule
Never assume all legs fill.
Every multi-leg trade must be designed with partial-fill logic.

---

## Maker vs Taker Logic

### Taker mode
Use when:
- Guaranteed edge clearly exceeds fees and slippage.
- Available displayed size is enough.
- Residual inventory risk is acceptable.
- Opportunity is likely to disappear quickly.

### Maker mode
Use when:
- Immediate arbitrage is not available.
- Related markets imply a strong fair value anchor.
- Order book is wide and stale.
- You can sit inside the spread with low pickoff risk.

### Hybrid mode
Common pattern:
- Cross the most liquid or most mispriced leg.
- Post passively on the hedging leg(s).
- Cancel if hedge fails to fill within timeout or if fair value moves.

---

## Fee and Slippage Model
Claude should make the fee layer configurable, not hard-coded.

Need a module that computes all-in expected cost by:
- market
n- side
- order type
- quantity
- whether maker/taker pricing differs

### Net edge formula
For any trade candidate:

`net_edge = guaranteed_value - entry_cost - fees - slippage_buffer - inventory_penalty`

No trade unless `net_edge >= min_required_edge`.

### Suggested defaults
Make these config-driven:
- `min_required_edge_cents = 2.0`
- `slippage_buffer_cents = 0.5 to 2.0`
- `stale_quote_timeout_ms`
- `max_legging_exposure_dollars`

---

## Risk Engine
This is mandatory. Do not let Claude skip it.

### Hard limits
- Max notional per market
- Max notional per event family
- Max contracts per order
- Max daily gross traded notional
- Max portfolio worst-case loss
- Max number of open orders

### Soft limits
- Edge threshold rises as inventory grows
- Quote width widens as inventory grows
- Disable one side if imbalanced

### Kill switches
- Lost connectivity
- Position reconciliation failure
- Fee model unavailable
- Repeated reject/cancel errors
- PnL drawdown exceeds threshold
- Clock skew or stale data

### Exposure accounting
For each position, compute:
- current mark-to-market
- max loss
- guaranteed payout under bundle completion
- correlation with related positions

For bucket families, compute outcome-by-outcome scenario PnL.

---

## Portfolio Logic
The bot should maintain a scenario-based portfolio view.

For each market family, model final PnL under each possible settlement state.

Example for 4-bucket market:
- If bucket 1 settles YES, what is family PnL?
- If bucket 2 settles YES, what is family PnL?
- etc.

This allows the system to recognize:
- Already partially completed bundles
- Free hedge opportunities
- Cheap flattening trades

---

## Research / Backtesting Requirements
Before live trading, the system should support replaying historical snapshots if possible.

### Backtest goals
- Estimate frequency of structural violations.
- Measure how often opportunities were truly executable.
- Measure edge persistence and decay.
- Simulate fill assumptions.
- Quantify partial-fill risk.

### Research metrics
- Opportunity count per day
- Average gross edge
- Average net edge
- Fill rate
- Realized PnL by strategy type
- PnL by market category
- Time-to-fill
- Cancellation rate
- Pickoff losses
- Inventory carry time

### Important
Backtests should distinguish:
- Mid-price opportunities
- Top-of-book executable opportunities
- Fully executable opportunities for target size

Only the last one matters for realistic deployment.

---

## Suggested First Version Scope
To keep this build tractable, version 1 should focus on one narrow slice.

### V1 market scope
- One market category only, preferably **weather range contracts** or **economic range contracts**.
- Detect only:
  - complete bundle arbitrage
  - complement arbitrage
- Trade only small size.
- Prefer taker or very conservative maker quotes.

### V1 features
- Market discovery for chosen category
- Live book polling/streaming
- Constraint grouping
- Opportunity detector
- Simple execution with strict position limits
- SQLite or Postgres logging
- Basic PnL dashboard

### Explicitly defer to V2
- NLP-heavy relationship inference
- cross-category constraints
- advanced quote optimization
- portfolio optimizer
- machine-learning fill prediction

---

## Suggested V2 Roadmap
Once V1 is stable:

### V2
- Monotonicity markets
- Inclusion constraints
- Synthetic fair values
- Passive market making inside wide spreads
- Better inventory-aware quoting

### V3
- Multi-family portfolio optimizer
- Fill probability model
- Regime detection by market type
- Automated dynamic sizing
- More categories and auto-discovery

---

## Detailed Implementation Notes for Claude

### 1. Build strongly typed domain models
Use `pydantic` models for:
- markets
- books
- constraints
- opportunities
- orders
- positions

### 2. Keep exchange integration isolated
All Kalshi-specific API code should live behind a connector interface. The strategy engine should not care about raw REST payloads.

### 3. Make every detector pure and testable
Each opportunity detector should accept:
- normalized books
- constraints
- config

and return opportunities. No side effects.

### 4. Make the execution planner separate from detectors
Detection answers: "Is this good?"
Execution answers: "How do we trade it?"

### 5. Add a dry-run mode first
The system should support:
- paper trading
- alert-only mode
- live trading mode

### 6. Log everything
For every decision, log:
- input prices
- fees assumed
- constraint violated
- opportunity score
- order decisions
- fills/cancels
- realized result

### 7. Build replay tooling early
The fastest way to improve this system will be replaying detected opportunities and seeing which assumptions were wrong.

---

## Example Opportunity Logic

### Example 1: Exhaustive 4-bucket market
Prices to buy all buckets at ask:
- 18
- 24
- 31
- 25

Total ask = 98
Fees = 1
Slippage buffer = 0.5

Net edge = 100 - 98 - 1 - 0.5 = 0.5

If threshold is 2, skip.
If threshold is 0.25, trade.

### Example 2: Sell bundle
Best bids:
- 20
- 27
- 30
- 26

Total bid = 103
Fees = 1.2
Slippage buffer = 0.5

Net edge = 103 - 100 - 1.2 - 0.5 = 1.3

Trade only if above threshold.

### Example 3: Monotonicity violation
- Recession by Dec 2026 = 47 bid / 49 ask
- Recession by Dec 2027 = 44 bid / 46 ask

This is inconsistent because later horizon should be at least as high.

Potential action:
- Sell 2026 near 47–49
- Buy 2027 near 44–46
- Or post passive quotes around corrected fair values

---

## Quote Placement Logic for Passive MM
For a contract with synthetic fair value `f`:

### Inputs
- top-of-book bid/ask
- fair value `f`
- current inventory
- volatility estimate
- time to expiry

### Example heuristic
- If current market spread is wide and `f` lies inside spread:
  - post bid = max(existing_bid + 1 tick, f - half_width)
  - post ask = min(existing_ask - 1 tick, f + half_width)
- widen if inventory is too long or too short.
- cancel if reference fair value shifts beyond tolerance.

Need configurable logic, not hard-coded constants spread everywhere.

---

## Monitoring / Dashboard Requirements
At minimum display:
- Open positions
- Open orders
- Realized PnL
- Unrealized/scenario PnL
- Current detected opportunities
- Rejected/skipped opportunities and why
- Per-family exposure
- System health

Alerts for:
- connectivity loss
- repeated order rejects
- exceeded limits
- stale books
- drawdown breach

---

## Testing Requirements
Claude should include tests for:

### Unit tests
- fee calculations
- bundle arithmetic
- complement logic
- monotonicity detection
- constraint parser
- scenario PnL

### Simulation tests
- partial fills
- cancel/replace loops
- stale data handling
- execution planner under missing liquidity

### Regression tests
Use canned market snapshots where known opportunities should be found.

---

## Minimal Acceptance Criteria
A usable V1 should be able to:

1. Pull live market and order book data for one chosen category.
2. Correctly group related contracts into exhaustive families.
3. Detect executable buy-bundle and sell-bundle opportunities.
4. Compute net edge after fees and buffers.
5. Place/cancel orders in paper mode.
6. Persist all opportunities and simulated trades.
7. Show scenario PnL for every open family.
8. Enforce hard risk limits and kill switches.

---

## Prompt to Claude Code
You can give Claude Code this summary:

Build a Python trading system for Kalshi focused on structural pricing inefficiencies across related contracts. Start with one category, preferably weather or economic range markets. Normalize related contracts into exhaustive families and detect bundle arbitrage where the executable buy cost of all buckets is below 100 or the executable sell credit of all buckets is above 100, after configurable fees and slippage buffers. Also detect binary complement arbitrage. Separate the code into modules for exchange connectivity, market discovery, normalization, opportunity detection, execution, risk, portfolio, persistence, and backtesting. Use strongly typed models, pure opportunity detectors, a dry-run mode, and strict risk limits. Log every opportunity and decision. Build unit tests and a simple dashboard. Optimize for correctness, observability, and safe execution over speed.

---

## Final Strategic Note
The edge here is not primarily "predicting the world better."
It is:
- enforcing math
- exploiting fragmented liquidity
- collecting spread near violated constraints
- managing execution and inventory better than other participants

The first version should be narrow, boring, testable, and safe. That is much more important than trying to build a giant all-market bot immediately.

