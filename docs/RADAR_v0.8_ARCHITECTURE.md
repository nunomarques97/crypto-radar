# LOCAL CRYPTO RADAR — v0.8 architecture proposal

> HISTORICAL PROPOSAL: retained for provenance. Current implementation and accepted design are in ../ARCHITECTURE.md; delivery status is in ../ROADMAP.md. The original implementation status, cloud direction and phase numbering below are not current authority.

Basis: a full reading of `radar_v0.7.py` (705 lines). Status: proposal, no code.
Kraken documentation consulted on 13 Sep 2026 to confirm field semantics (Spot Ticker, Futures Tickers, Historical Funding Rates).

---

## 0. Executive summary (read this if nothing else)

v0.7 has three defects that no threshold tuning fixes:

1. **The pre-filter picks the 35 candidates by the wrong criterion.** `prefilter_score` is dominated by |24h change| (up to 22 points) and by liquidity/spread (up to 22 points). A large, liquid, idle asset (ARB, HYPE) always makes the 35; an asset accelerating right now but flat over 24h may never reach the OHLC stage. The "weak" ranking starts here, not in `score_candidate`.
2. **Nothing is normalised by the asset's own volatility.** +1% in 5m on BTC and +1% in 5m on LSK (after +197%) are worth the same 7 points. Without z-scores/ATR, "an interesting move" and "normal noise for that asset" cannot be told apart.
3. **There is no state between runs.** Every run starts from scratch: it does not know what the market looked like 5 minutes ago, that it already alerted on PUMP in the previous cycle, or whether OI went up or down. Without short-term memory there is no "move now vs old move", no OI delta, no dedup, and no way to calibrate thresholds.

v0.8 fixes all three with one new piece: **a local snapshot store (SQLite) fed by the global Ticker on every cycle.** With it, 5m/15m/1h momentum and volume intensity become computable for ALL markets with zero extra requests (the Ticker returns `v[0]` = volume since 00:00 UTC; the difference between two snapshots is the exact volume of the interval). OHLC is then used only for the shortlist, and the order book only for finalists.

There are also concrete bugs (section 1.6), two of them serious: BTC and DOGE **never** match with Futures (XBT/XDG on Spot vs BTC/DOGE on Futures), and the "24h change" is actually "change since 00:00 UTC".

**Verdict: BUILD v0.8**, in phases, with phase 1 running in shadow alongside v0.7. Details in section 16.

---

## 1. Review of the actual code (v0.7)

### 1.1 Current architecture

A single, sequential script with no state, no cache and no output persistence (only `print`). Each run:

```
AssetPairs (1 req) + Ticker all (1 req)
  → is_crypto()            static quote/base/status filter
  → prefilter_score()      ranking by 24h/volume/spread/range        → top 35
  → spot_ohlc() ×35        sequential 5m OHLC (720 bars each)         ← ~60% of the time
  → futures_tickers (1 req) → index_perpetuals() → enrich_futures()
  → score_candidate()      additive 0–100 score
  → build_qwen_payload()   score ≥ 25, max 12
  → qwen_review()          1 Ollama call, format=json
  → print
```

### 1.2 Endpoints used

| Endpoint | Auth | Use | Cost/cycle |
|---|---|---|---|
| `GET /0/public/AssetPairs` | no | metadata | 1 (could be a daily cache) |
| `GET /0/public/Ticker` (no pair) | no | ~1000 pairs in a single payload | 1 |
| `GET /0/public/OHLC?pair=&interval=5` | no | 5m/15m/1h + 15m/1h volume | 35 sequential, ~50 KB each |
| `GET /derivatives/api/v3/tickers` | no | all PF_ | 1 |
| `POST localhost:11434/api/generate` | local | Qwen | 1 |

No private endpoint, no key read. Security confirmed (section 14).

### 1.3 Where the cost is

Of the ~15 s: ~1-2 s on the two tickers, **~8-10 s on the 35 sequential OHLC calls** (no `requests.Session`, no keep-alive, no `since`, 720 bars = 60 h of data per pair when only 12 are used), ~3-5 s on Qwen. OHLC is the only cost that scales with the number of candidates, and it is exactly what the wrong shortlist wastes.

### 1.4 Weaknesses and assumptions

- `except Exception: continue` in the OHLC stage silently drops the candidate. A Kraken 429 halfway through makes half the shortlist disappear without warning.
- `quote_to_usd` uses a hardcoded EUR×1.16. The same Ticker payload contains EUR/USD; the conversion could be live.
- `futures_quote_fresh = bool(lastTime)` is always `True` if the field exists. It does not measure age. A perpetual with no fills for 3 h counts as fresh.
- `is_crypto` accepts `post_only` as tradeable. A post-only market does not accept market orders; for Fable that changes tradeability.
- There is no asset-class filter: tokenised stocks/ETFs listed on Kraken (xStocks) pass as crypto if they have a USD quote and $1M of volume. The developer should check what `AssetPairs` exposes (`aclass_base`) to exclude them without a hardcoded list.
- Qwen receives `local_radar_score` and returns exactly that number (26.1). It is not scoring, it is echoing. The prompt asks for a "score" but does not define a scale; without a schema, `format: "json"` only guarantees valid JSON, not its shape.
- Qwen can return symbols that were not in the input; nothing validates them.
- There is no retry, backoff, differentiated timeout, or writing of results to a file. It cannot usefully be scheduled in a loop.
- No dedup/cooldown: the same alert would fire on every cycle.

### 1.5 Where the scoring produces false positives/negatives

- **Pre-filter (false negatives):** an asset with +2% in 15m, flat 24h, $3M volume, 30 bps spread → 0 + 3 + 0 + 0 = 3 points. Idle BTC → 0 + 14 + 8 + 0 = 22. The asset that matters never reaches OHLC.
- **No normalisation (false positives and negatives):** 5m×7, 15m×4, 1h×2 on raw percentages. Saturates at 2.86% in 5m. A memecoin with a 1.5% 5m ATR gets 10 points just for breathing.
- **Acceleration:** `|15m| - |1h|/4` assumes linearity and ignores sign. A reversal (+2% in 15m after -2% in 45m) and a breakout get the same bonus.
- **Volume:** a 1h baseline = volume24h/24 ignores intraday seasonality (the US vs Asia session alone gives 1.5-2×). The 15m window includes the current partial bar, so the 15m ratio is systematically underestimated at the start of the bar.
- **Spread weighs as much as momentum:** -15 for spread > 60 bps is what pushed LSK out of the gate while PUMP got in with zero movement. Liquidity should be a tradeability gate, not an additive term competing with the signal.
- **A threshold of 25 on a meaningless scale:** a sum of arbitrary constants has no interpretation; the gate passes or not by accident.

### 1.6 Concrete bugs

1. **BTC and DOGE never match with Futures.** The Spot `wsname` gives `XBT/USD` and `XDG/USD`; `normalize_future_base` only normalises the Futures side (`XBT→BTC`). `by_base.get("XBT")` fails. The Spot side must be normalised with the same map (a map of Kraken legacy codes, not a list of coins).
2. **"24h change" is "since 00:00 UTC".** Confirmed in the documentation: Ticker `o` = "Today's opening price" (a scalar), while `v/h/l/p/t` have `[today, last24h]`. At 01:00 UTC the "24h change" is a 1-hour change; at 23:00 it is a 23-hour one. Inconsistent with the 24h range and 24h volume used in the same formula.
3. **Basis across different quotes.** If the chosen Spot market is EUR (an asset with no USD pair), `futures_basis_pct = futures_last/spot_last - 1` gives ~16% of "basis". It should use `markPrice/indexPrice - 1` from the Futures ticker itself, which is the correct definition and does not depend on Spot.
4. **"5m change" has a variable window.** It compares the close of the current partial bar with the close of the previous bar: it measures anywhere between 0 and 5 minutes depending on when the cycle runs.
5. **15m/1h volume includes the partial bar** (`rows[-3:]`, `rows[-12:]`).
6. **Trade count, 24h VWAP and size at best bid/ask are ignored**, although they come free in the Ticker (`t[1]`, `p[1]`, `a[2]`, `b[2]`). They are the three best cheap detectors of fake volume and thin liquidity.

---

## 2. Universe discovery (dynamic, no coin lists)

Rule: **an allowed list of quotes and a map of the venue's legacy codes are acceptable configuration; a list of coins is not.**

| Case | v0.8 handling |
|---|---|
| USD quote | primary |
| USDT / USDC quote | only when the asset has no USD pair; priority USD > USDT > USDC |
| EUR quote | only when there is no USD/USDT/USDC pair; live conversion via `EUR/USD` from the same Ticker payload; marked `quote_fallback=true` in the output |
| XBT/BTC, XDG/DOGE | map `LEGACY_CODES = {"XBT":"BTC","XDG":"DOGE"}` applied on both sides (Spot and Futures) before matching. The developer checks `Assets` for other X/Z-prefixed codes |
| Stablecoins | a known list of quote assets (venue configuration) **plus** a dynamic rule: price in [0.97, 1.03] against USD and 24h range < 1% → `stable_like`, excluded. Catches new stablecoins without their code |
| Fiat as base | excluded (the fiat list is venue configuration) |
| Tokenised stocks/ETFs | excluded by the venue's `aclass_base`/naming, to be checked by the developer in `AssetPairs`; if no reliable field exists, a dynamic rule: no fills between Friday 21:00 and Sunday 21:00 UTC in the store → `non_crypto_like` |
| Duplicates | group by normalised base; choose the market by quote priority, then volume; **the asset's liquidity is the sum of the volumes of all quotes** (for the gate), while the analysis uses the primary market |
| Artificially low volume / dead markets | `t[1]` (24h trades) < 200 → `dead`; 24h volume < $1M → out of the tradeable universe but **kept in the snapshot store** (to detect take-offs) |
| Post-only, cancel_only, limit_only, reduce_only | out of the tradeable universe, recorded in the output with the status |
| Extreme spread | top-of-book spread > 150 bps → `untradeable`; 50-150 → flag, penalises tradeability, does not exclude |

A new memecoin on Kraken appears in `AssetPairs` (24 h cache, with a forced refresh if a Ticker symbol is not in the cache) and in the Ticker in the same cycle. It enters the store immediately and becomes a candidate once it meets liquidity and has ≥ 1 h of snapshots (or backfilled OHLC).

---

## 3. Market data architecture (what is collected at each layer)

Principle: **everything the global Ticker and the Futures Tickers provide is CHEAP GLOBAL and is collected for all markets in 2 requests. Everything that needs one request per asset is CANDIDATE or FINALIST.**

### CHEAP GLOBAL (2 requests/cycle, ~1000 markets)

Spot Ticker: last, bid, ask, **bidSize/askSize** (`a[2]`, `b[2]`), volume today/24h, **VWAP today/24h**, **trades today/24h**, high/low today/24h, open today.
Futures Tickers: markPrice, **indexPrice**, last, lastTime, bid/ask/sizes, vol24h, volumeQuote, openInterest, fundingRate (raw), fundingRatePrediction (raw), open24h, suspended, postOnly, tag.

Derived with no extra requests:
- spread in bps, USD at best bid/ask (a level-1 depth proxy);
- distance to the 24h VWAP (`last/p[1] - 1`): a better "position" than range position;
- 24h average trade size (`volume/trades`) and trades per hour: a wash/fake volume detector;
- **with the snapshot store:** 5m/15m/1h/4h return (price vs the snapshot from N minutes ago), exact interval volume (`Δ v[0]`, with the midnight reset handled), interval trades (`Δ t[0]`), **15m/1h/4h ΔOI**, Δfunding, basis `mark/index - 1`, age of the last Futures fill;
- return relative to the market (`r_asset - r_BTC`) and aggregate market activity (sum of USD Δvolume across the whole universe): separates idiosyncratic moves from beta.

### CANDIDATE ONLY (shortlist ≤ 40, 1 request per asset, incremental)

5m OHLC with `since` = the last cached timestamp (after warm-up, each request brings 1-2 bars). 15m/1h/4h/24h are derived from it by aggregation. Computed:
- ATR(14) on 5m bars and on 1h bars; 24h realised volatility;
- returns in ATR units (the normalisation v0.7 lacks);
- volume baseline: median of the last 96 15m windows and a time-of-day factor (from the previous 2 days);
- structure: N-bar high/low (4h, 24h), distance to breakout in ATR, higher highs/lower lows over the last 12 bars, range compression (1h ATR / 24h ATR as a percentile);
- intraday VWAP from OHLC (per-bar approximation).

**No 1m data.** With 5m bars and minute snapshots, the gain from 1m is noise and costs one extra request per asset. 4h as a separate request is rejected too: it is aggregated from 5m (60 h of history is enough for 4h and 24h).

### FINALIST ONLY (≤ 8, 2 requests per asset)

- Spot `Depth?pair=&count=25`: USD available within ±0.5% and ±1% of mid, bid/ask imbalance, estimated slippage for a configurable order size;
- Spot `Trades?pair=` (last 1000 fills): taker buy/sell ratio, average size, time covered (real aggression, not just volume);
- Futures `orderbook?symbol=PF_…`: the same depth metric;
- Futures `historical-funding-rates?symbol=`: **once a day for 2-3 reference symbols**, not per finalist, for the semantics check (section 9).

**Liquidations:** the public Kraken Futures API does not expose a liquidation feed. Nothing is invented. The only proxy is a sharp ΔOI with a price move against funding, and it is labelled `proxy`, never liquidation data.

---

## 4. Multi-stage radar

Six layers and **two cadences**: a light cycle every minute and a full cycle every 5 minutes or whenever the light cycle triggers.

```
L0  UNIVERSE      2 req    AssetPairs (24h cache) + Ticker + Futures Tickers → snapshot store
L1  ANOMALY       0 req    return/volume/trades/OI z-scores for ALL            → shortlist ≤ 40
L2  STRUCTURE    ≤40 req   incremental OHLC → ATR features, rule-based setup, opportunity score → ≤ 10
L3  MICRO/DERIV  ≤16 req   depth + trades (+ futures book) → tradeability, risk flags → finalists ≤ 8
L4  QWEN         0-1 call  classification + veto + call_fable (only if finalists clear the pre-gate)
L5  FABLE GATE    0 req    deterministic rules ∧ Qwen ∧ cooldown ∧ budget → list for Fable
```

Why this order and not "momentum → structure → derivatives": derivatives are already in L0 for free (the Futures ticker is global), so OI/funding/basis enter L1 features rather than a late layer. What is expensive (the order book) comes last.

**Cadence:** a `heartbeat` every 60 s runs only L0+L1 (~1-2 s, 2 requests, no Qwen). If any asset has anomaly ≥ the trigger threshold, L2-L5 run immediately (event-driven). Independently, L2-L5 run every 5 minutes. Result: minute-level coverage, OHLC cost every 5 minutes, Qwen only when there is something.

**Cold start:** for the first 60 minutes the store has no history. L1 degrades to the signals the Ticker gives on its own (distance to the 24h VWAP, range position, spread, trades/h) and L2 backfills OHLC (without `since`) for the top 40. The output marks `warmup=true`.

**Squeeze:** a squeeze is compression, so it is not an activity anomaly and L1 does not catch it, which is correct: it is only actionable on release, and the release IS an anomaly (range expansion with volume). L2 recognises that the expansion comes from compression because it has the ATR percentile in the cache.

---

## 5. Scoring

Four separate numbers, because they answer different questions and mixing them was v0.7's mistake:

- **anomaly_score (L1, all):** "how unusual is current activity for this asset?" Direction-agnostic. A weighted average of z-scores (15m return vs the standard deviation of the asset's own 15m returns over the last 48 h; 15m volume vs the median of matching windows; 15m trades likewise; |1h ΔOI| vs history), adjusted for aggregate market activity (if the whole universe has z=2, the relative z falls). It only orders the shortlist. A liquid, idle asset has z≈0 and does not get in, BTC or not.
- **opportunity_score (L2, shortlist):** "is there a setup?" 0-100 built from features bounded to [-1, 1]:
  - momentum: 15m and 1h return in ATR units, with **sign coherence** across 5m/15m/1h (incoherence penalises);
  - acceleration: 15m return vs (1h return − 15m return), signed;
  - volume expansion **confirmed by price**: 15m volume/baseline only counts if the bar's range also expanded (volume without range = absorption, not expansion);
  - breakout distance: close vs the 4h and 24h high/low in ATR; positive near/above, with volume;
  - freshness: the share of the 24h move that happened in the last hour. If |r_24h| ≫ |r_1h| and |r_1h| < 0.5 ATR → old move, negative feature;
  - exhaustion: |r_1h| > 3 ATR ∧ volume of the last 3 bars falling ∧ close far from the bar high → negative;
  - reversal: 15m against 4h, with volume at the extreme and rejection (wick) → its own setup, not a penalty;
  - squeeze release: low ATR compression percentile over the previous 24 h ∧ expansion now;
  - relative strength: r_asset − r_BTC over the window (idiosyncratic > beta);
  - derivatives coherence: ΔOI with the sign of the move (OI rising with price rising = new positions; OI falling = closing), basis within its normal band, funding only if VERIFIED.
  Each setup_type (BREAKOUT, CONTINUATION, REVERSAL, SQUEEZE_RELEASE, EXHAUSTION, NONE) is assigned by deterministic rules over these features, with per-type weights in config. Direction comes from the sign of the momentum features, not from the LLM.
- **tradeability_score (L3, finalists):** spread, USD within ±0.5%, estimated slippage, trades/h, status, existence of a perpetual, Futures spread/depth, age of the last fill. **It is a gate, not an additive term:** below the minimum the asset does not go to Qwen, however good the setup looks. Above it, it goes into the output as a number for Fable.
- **confidence:** data_quality (enough snapshots? OHLC up to date? Futures fresh? funding verified?) × the number of independent confirmations (momentum, volume, breakout, derivatives, microstructure). Bucketed LOW/MEDIUM/HIGH.

On costs, a point v0.7 ignores completely: **on Kraken Spot a taker round trip is ~160 bps** (a tier verified in the Sextant project; the developer confirms it in config). A +2% alert in 15m on Spot is economically irrelevant; the same move on perpetuals (2/5 bps) is not. The opportunity score must require a minimum expected amplitude depending on the executable venue: `market=FUTURES` has a lower bar than `market=SPOT`. Without this the radar produces true but useless alerts.

**Calibration instead of guessing:** for every asset that reached L2, the store records the features and the following 15m/1h/4h return (forward return). After a few days there is data to choose weights and thresholds from evidence, and to answer the question that matters: "were the alerts we generated followed by a move?" Without this, v0.8 would be another round of invented constants.

---

## 6. The role of Qwen3:14b

**It does not score.** 14B LLMs are bad at arithmetic and echo the numbers they are given; the v0.7 test proved it (26.1 → 26.1).

It does four things, on ≤ 8 finalists that already have a rule-based setup and features:

1. **Veto false positives:** is the rule-based setup consistent with all the features presented? (For example, "BREAKOUT" but negative volume expansion and a widening spread → veto with a reason.)
2. **Classification:** confirms or corrects `setup_type` and `direction` within closed enums.
3. **A `call_fable` recommendation + `confidence` bucket + a one-sentence reason** referring to the supplied data.
4. **Data quality flags** it notices (raw funding, quote fallback, warmup).

It is called **only when finalists clear the deterministic pre-gate** (opportunity ≥ 50 and tradeability ok). Many cycles will make zero Qwen calls. `think=false`, temperature 0, **structured output with a JSON schema** in Ollama's `format` field (not the string `"json"`), symbols validated against the input, one retry if the JSON fails.

An honest note: with well-built features, the marginal value of a 14B model is modest. It stays because it is cheap and reduces Fable calls, but **the log always records the deterministic decision and Qwen's side by side**. If after two weeks Qwen agrees with the rules in > 95% of cases, it is removed and the latency saved.

---

## 7. Fable gate

Calling Fable is the expensive action. Deterministic rules, evaluated after Qwen:

**Mandatory (all):**
- tradeability ≥ minimum and status online;
- data_quality with no critical flag (no warmup, OHLC up to date, Futures fresh if `market≠SPOT`);
- opportunity ≥ 60;
- not in cooldown: the same asset does not go back to Fable for the next N hours (config, e.g. 4 h) unless `setup_type` changed or opportunity rose ≥ 15 points;
- Qwen did not veto with HIGH confidence (or Qwen is unavailable, in which case one extra confirmation is required).

**At least two independent confirmations from:**
- momentum ≥ 2 ATR in 1h with 5m/15m/1h sign coherence;
- volume expansion ≥ 3× baseline with range confirmation;
- a breakout of the 24h high/low with volume;
- squeeze release;
- coherent derivatives (1h ΔOI with the sign of price; normal basis; futures volume rising);
- aggression on the tape (taker imbalance ≥ 65/35 in recent trades);
- Qwen `call_fable=true` with HIGH.

**Vetoes:** the `exhaustion` flag, the `illiquid_pump` flag, the `late_pump` flag (negative freshness), extreme funding if VERIFIED and against the direction.

**Budget:** at most K Fable calls per hour and per day (config, initial suggestion 2/h, 6/day). If there are more candidates than budget, they are ordered by opportunity × confidence and the rest stay in the output as `deferred`.

---

## 8. Memecoins / high-beta

There is no special treatment and no list. Eligibility comes automatically from: the dynamic universe, liquidity as a gate (not a score) and ATR normalisation (the memecoin is compared with itself). What protects against the traps:

| Trap | Detection |
|---|---|
| Late pump | freshness (share of the 24h move in the last hour) + exhaustion |
| Illiquid pump | USD within ±0.5% on the book (finalist), bid/ask size in the Ticker (global), trades/h |
| Fake volume | high volume with few trades (anomalous average trade size), volume without range, Spot volume with no echo on Futures when a perpetual exists |
| Wide spread | tradeability gate, with the estimated cost explicit in the output |
| Exhaustion | its own feature + a veto in the Fable gate |

An asset with a 2% 5m ATR needs more absolute amplitude to reach the same z; that is the correct behaviour, not a penalty for being a meme.

---

## 9. Futures

Everything comes from the global `tickers` endpoint; what changes in v0.8 is the use of `indexPrice` and of the store:

- **basis** = `markPrice/indexPrice − 1` (fixes bug 3);
- **ΔOI** 15m/1h/4h from the snapshots, in % and in USD (`OI × mark`); the sign of OI vs the sign of price classifies: new positions / closing / likely squeeze;
- **Futures volume** vs Spot (ratio and change);
- **level-1 spread and depth** from the ticker; order book only for finalists;
- **freshness** = `now − lastTime` in seconds; > 300 s → `stale`;
- `suspended` or `postOnly` → Futures not executable.

**Funding: RAW vs VERIFIED.** Confirmed in the documentation: the ticker gives `fundingRate` = "current **absolute** funding rate" and `fundingRatePrediction` = "estimated next absolute funding rate". There is a public history endpoint with `fundingRate` (absolute) and `relativeFundingRate` per period. The documentation defines **neither** the period nor the mathematical relation between the two. Therefore:

1. A `verify_funding_semantics()` routine once a day (24 h cache), with 2-3 reference symbols (BTC, ETH, one alt): it pulls the history, measures the timestamp spacing (empirical period), tests the hypothesis `relative ≈ absolute / mark`, and compares the last historical `fundingRate` with the ticker's.
2. If all three checks pass within tolerance, the output marks `funding_semantics: "VERIFIED"` with `period_hours` and a derived `relative_rate`. Otherwise it stays `RAW_UNVERIFIED` and funding **does not enter any directional feature**, only the output as context.
3. Even when VERIFIED, funding enters with a low weight. The Sextant project measured that funding is the price of basis risk, not a free signal; here it is used only to detect extremes (crowded positioning) and potential squeezes.

The developer confirms the exact path of the history endpoint (the documentation lists `historical-funding-rates`; in current use `/derivatives/api/v4/historicalfundingrates` also appears). Test both and record which one responds.

---

## 10. Order book

Finalists only (≤ 8), full cycle only, and only once the candidate has passed the opportunity pre-gate. Metrics: USD within ±0.5% and ±1%, imbalance, estimated slippage for the configured order size. If the shortlist is empty, zero book requests. In a normal regime this is 0-16 requests per 5-minute cycle. There is no book in the heartbeat.

`Trades` (latest fills) is added at the same tier: it is one request, and it tells whether volume is buying or selling aggression, which OHLC does not know.

---

## 11. Performance

| | v0.7 | v0.8 heartbeat (60 s) | v0.8 full (5 min) |
|---|---|---|---|
| Kraken requests | 38 | 2 | 2 + ≤40 incremental OHLC + ≤16 finalists |
| OHLC payload | ~1.7 MB | 0 | ~50 KB (with `since`) |
| Qwen | always | never | only with finalists |
| Estimated time | 15 s | 1-2 s | 4-8 s |

Means: `requests.Session` with keep-alive, a `ThreadPoolExecutor` with 3-4 workers for OHLC, exponential backoff on 429/5xx, an incremental OHLC cache, AssetPairs in a daily cache. SQLite store with 7-day retention (~10 MB/day with a snapshot every minute for ~300 assets).

---

## 12. Memory

The radar has its own technical state (`radar_state.sqlite`: snapshots, OHLC cache, emitted alerts, cooldowns, forward returns) and writes `radar_latest.json` + `alerts.jsonl`. **It never writes to trading state or trading history files.** The radar only produces events that Fable (and a human) consume.

---

## 13. JSON output (proposal)

```json
{
  "schema_version": "0.8",
  "run_id": "2026-09-13T14:05:00Z#3812",
  "timestamp": "2026-09-13T14:05:03Z",
  "mode": "FULL | HEARTBEAT",
  "warmup": false,
  "universe": {
    "pairs_seen": 1043, "assets_eligible": 287, "assets_tradeable": 241,
    "futures_perpetuals": 275, "excluded": {"stable_like": 14, "dead": 22, "status": 9, "non_crypto": 31}
  },
  "funnel": {"L1_shortlist": 40, "L2_candidates": 10, "L3_finalists": 5, "qwen_called": true, "fable_recommended": 1},
  "data_quality": {
    "spot_ticker": "OK", "futures_ticker": "OK | STALE | UNAVAILABLE",
    "funding_semantics": "VERIFIED | RAW_UNVERIFIED", "funding_period_hours": 1,
    "ohlc_failures": 0, "qwen": "OK | INVALID_JSON | TIMEOUT | UNAVAILABLE",
    "credentials_used": false
  },
  "candidates": [
    {
      "asset": "LSK", "spot_pair": "LSK/USD", "quote_fallback": false,
      "futures_symbol": "PF_LSKUSD",
      "scores": {"anomaly": 3.4, "opportunity": 71, "tradeability": 58, "confidence": "MEDIUM"},
      "setup": {"type": "CONTINUATION", "direction": "LONG", "market": "FUTURES", "freshness": 0.62},
      "features": {
        "ret_5m_atr": 1.3, "ret_15m_atr": 2.1, "ret_1h_atr": 1.9, "ret_24h_pct": 196.9, "ret_rel_btc_1h_pct": 0.5,
        "vol_15m_x": 3.8, "vol_1h_x": 2.6, "trades_1h_x": 2.9, "range_expansion": true,
        "breakout_dist_24h_atr": -0.3, "squeeze_pctile": 0.71, "exhaustion": false,
        "spread_bps": 78.2, "depth_usd_0_5pct": 18400, "taker_buy_ratio": 0.68,
        "oi_delta_1h_pct": 4.1, "basis_pct": 0.12, "funding_raw": -0.00195, "funding_relative": null
      },
      "flags": ["WIDE_SPREAD", "EXTENDED_24H"],
      "qwen": {"setup_type": "CONTINUATION", "direction": "LONG", "call_fable": true, "confidence": "MEDIUM",
               "veto": false, "reason": "..."},
      "fable_gate": {"decision": "CALL | DEFER | SKIP", "confirmations": ["momentum", "volume", "oi"],
                     "vetoes": [], "cooldown_until": null}
    }
  ],
  "alerts_for_fable": ["LSK"]
}
```

---

## 14. Security (confirmed in the v0.7 code and kept in v0.8)

- Only `GET` on `/0/public/*` and `/derivatives/api/v3/*` (tickers, orderbook, historical funding). No private, account or order endpoint.
- No environment variable holding a key is read; v0.8 adds a **startup guard** that aborts if `KRAKEN_API_KEY`/`KRAKEN_SECRET` are set in the process environment, and a test that fails if any URL contains `/private/` or the method is not GET.
- Ollama on localhost; Qwen has no tools.

---

## 15. Implementation plan

**A. Architecture:** a `radar/` package with `config.py`, `kraken_spot.py`, `kraken_futures.py`, `store.py` (SQLite), `universe.py`, `features.py`, `setups.py`, `scoring.py`, `micro.py`, `qwen_gate.py`, `fable_gate.py`, `output.py`, `radar.py` (orchestrator with `--mode heartbeat|full|loop`). Dataclasses/pydantic for `Snapshot`, `Candidate`, `Features`, `Decision`.

**B. Scoring:** section 5; weights and thresholds in `config.py` with initial values marked `UNCALIBRATED`, to be reviewed with forward returns after 7 days.

**C. Qwen prompt (English, structured output):**

System: "You are the final gatekeeper of a read-only crypto radar. You receive up to 8 finalists with numeric features already computed and a rule-based setup label. You do NOT compute scores. Your job: (1) veto candidates whose rule-based setup is contradicted by the supplied features; (2) confirm or correct setup_type and direction using only the enums; (3) recommend whether a deeper analysis by a senior analyst is worth its cost; (4) note data quality issues. Use only supplied data. Funding fields labeled RAW_UNVERIFIED must not influence direction. Memecoins and high-beta assets are valid. Output must match the schema exactly and reference only symbols from the input."

User: the finalists as compact JSON + "Return JSON matching the schema."

Schema (Ollama's `format` field): `{"reviews":[{"asset":str, "setup_type": enum[BREAKOUT,CONTINUATION,REVERSAL,SQUEEZE_RELEASE,EXHAUSTION,NONE], "direction": enum[LONG,SHORT,NONE], "market": enum[SPOT,FUTURES,BOTH,NONE], "veto": bool, "call_fable": bool, "confidence": enum[LOW,MEDIUM,HIGH], "reason": str(max 200), "data_quality_notes": [str]}]}`. Post-validation: `asset` ∈ input, one review per finalist, a single retry.

**D. Fable gate:** section 7, implemented in `fable_gate.py` as a pure function over `Candidate` + `QwenReview` + cooldown/budget state, testable without a network.

**E. Endpoints:** `AssetPairs`, `Ticker`, `OHLC?since`, `Depth?count=25`, `Trades`, Futures `tickers`, `orderbook`, historical funding. Nothing else.

**F. Main classes/functions:** `SnapshotStore.write/window(asset, minutes)`, `Universe.build(pairs, ticker, fut) → list[Asset]`, `compute_anomaly(store, asset)`, `OhlcCache.update(pair)`, `compute_features(asset, ohlc, snapshots)`, `classify_setup(features) → (type, direction)`, `opportunity(features, type)`, `tradeability(asset, depth, trades)`, `qwen_review(finalists)`, `fable_gate(candidates, reviews, state)`, `write_output(...)`, `label_forward_returns(store)`.

**G. Caching:** AssetPairs 24 h; EUR/USD per cycle; incremental OHLC per pair (`since`); snapshots 7 days; funding verification 24 h; Qwen result per `(run_id)`.

**H. Logging:** a human-readable `radar.log` (one line per layer with counts and timings) + a structured `runs.jsonl` (features of every L2 asset, the deterministic and Qwen decisions side by side) + `alerts.jsonl`. Without this there is no calibration and no audit of Qwen.

**I. Failures:** Futures unavailable → continue without derivatives, `data_quality.futures_ticker=UNAVAILABLE`, Spot-only tradeability. OHLC fails for an asset → it stays with L1 features and an `OHLC_MISSING` flag, never disappearing silently. 429 → backoff and fewer workers. Qwen timeout/invalid JSON → one retry, then the deterministic gate with `qwen=UNAVAILABLE` and an extra confirmation required. Corrupted store → recreate it and mark warmup. A cycle exceeding 45 s aborts that cycle's Qwen call.

**J. Migration v0.7 → v0.8:**
1. `store.py` + L0/L1 + heartbeat in a loop. Run 3-5 days in shadow alongside v0.7, recording both shortlists. Pass criterion: the L1 shortlist contains the assets a human identifies by eye as "moving" in at least 9 of 10 manual checks, while v0.7 misses several.
2. L2 (incremental OHLC, ATR features, setups, opportunity) + forward return labelling.
3. L3 (depth, trades, tradeability) + fixes for inherited bugs 1-6.
4. Qwen with a schema + comparative log.
5. Fable gate + cooldown + budget + final output.
6. Retire v0.7. First weight calibration with 7+ days of `runs.jsonl`.

Each step is a separate change, delivered one at a time.

---

## 16. Final decision

**ARCHITECTURE VERDICT: BUILD v0.8**

Why: the observed problems (weak ranking, a single candidate at the gate, inability to tell a new move from an old one) are a direct consequence of three structural decisions in v0.7 (pre-filtering by 24h/liquidity, no normalisation, no state), not of thresholds. Tuning constants in v0.7 would change which assets fail, not the fact that they fail. v0.8 changes the right piece (snapshot store + anomaly ranking + liquidity as a gate) and does it while **reducing** requests and latency, not increasing them.

Conditions attached to BUILD:
- Phase 1 runs in shadow before replacing what exists; v0.7 is not switched off on a promise.
- Initial weights are declared `UNCALIBRATED` and only get "definitive" values from measured forward returns. Without `runs.jsonl` and labelling, v0.8 is v0.7 with more invented constants.
- Qwen stays under evaluation, with the comparative log as judge, and is removed if it adds nothing.
- The radar is not proof of an edge. Detecting a move is not predicting one; what it buys is Fable's time spent where there is something to analyse. The question "does this make money?" is answered by measured trade records, not by the radar.
