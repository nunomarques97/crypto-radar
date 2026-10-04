<div align="center">

# Crypto Radar

**A local-first crypto market radar that watches, reasons and warns.**

It places no orders, and the radar itself holds no exchange credentials. Execution is on the roadmap — behind seven approval gates, and never driven by a model.

[![tests](https://img.shields.io/badge/tests-2693_passing-2ea44f)](#tests)
[![python](https://img.shields.io/badge/python-3.12-3776ab)](#requirements)
[![platform](https://img.shields.io/badge/platform-Windows_11-0078d4)](#requirements)
[![inference](https://img.shields.io/badge/inference-100%25_local-6f42c1)](#how-it-works)
[![mode](https://img.shields.io/badge/mode-ANALYSIS__ONLY-critical)](#boundaries)
[![licence](https://img.shields.io/badge/licence-AGPL--3.0-f39c12)](#licence)

</div>

---

Crypto Radar watches public Kraken spot and perpetual markets, detects unusual activity with deterministic rules, asks a local model for a second opinion, and alerts a human.

Everything runs on your own machine: public market data in, SQLite on disk, inference through a local Ollama model. **No paid APIs and no cloud inference, ever.** The radar holds no exchange credentials and reaches nothing beyond public market endpoints. The one exception is a separate, optional command that reads a Kraken account with a read-only key you create yourself — see [Boundaries](#boundaries).

## How it works

Four deterministic layers narrow the whole market down to a handful of names, and only then is a local model asked what it thinks.

| Layer | What it does | In → out |
|---|---|---|
| **L0 · Universe** | Public pairs and tickers, validated and snapshotted | all markets → snapshot |
| **L1 · Anomaly** | Z-scores of return, volume, trade count and open interest | snapshot → **40** |
| **L2 · Structure** | OHLC, ATR-normalised features, setup detection | 40 → **10** |
| **L3 · Micro** | Depth, trade tape, perpetual order book | 10 → **8** |
| **Screener** | Local model, structured JSON only | 8 → advisory verdict |

The model is **advisory**. It can flag or abstain, but it cannot size a position, set a stop, or authorise anything. Any claim it makes that the evidence does not support is rejected before it reaches a notification.

Integrity comes first: prices, timestamps and order books are validated before they are consumed. What cannot be verified is marked `UNKNOWN`, never silently treated as good.

## Boundaries

The system runs in an explicit, declared mode. Today that mode is the first rung of a ladder that only a human can climb, one approval at a time:

| | Mode | What it allows |
|---|---|---|
| **▸** | **`ANALYSIS_ONLY`** | Public data in, alerts out. No orders, no credentials. **You are here.** |
| | `RETROSPECTIVE` | Replaying recorded history to measure whether an edge is real |
| | `PAPER` | Simulated orders against a synthetic ledger, no private side effect |
| | `SHADOW_LIVE` | Authorised account *reads* only; the process has no submission port |
| | `MICRO_LIVE` | First real money, deliberately tiny, as a canary |
| | `CONSTRAINED_LIVE` | Real money inside a narrow, approved envelope |
| | `APPROVED_ENVELOPE` | Steady state, still bounded by the approval it was given |

**What it cannot do today.** There is no order code in the repository. The radar holds no exchange credentials and asks for none, sends nothing off the machine beyond requests to public market endpoints and the optional ntfy push notifications you configure, and shows no number the stored evidence cannot prove. Having credentials present would enable nothing: the mode is a declared state, not an inference from what happens to be on the machine.

**What exists beyond alerts, still without any order path.** Three pieces go further than alerts, and none of them can place an order:

- **The paper game and the pilot shadow wallet** run as **`PAPER`**: pretend money, no exchange account. The paper game opens and closes simulated plays with a stop, a target and a 24-hour limit, checked by a paper position monitor. The pilot shadow wallet is a separate pretend 240 EUR account sized by a deterministic risk engine with loss locks and a kill switch. It reads no real balance; it is not `SHADOW_LIVE`.
- **A read-only Kraken account check** (`scripts/kraken_account_check.py`) is a separate command, never imported by the radar, the loop, the paper game, the pilot or the UI. It runs only when you pass `--mode SHADOW_LIVE` yourself, reads balances, trade balance, 30-day volume and fee, and open orders with a key you create with query permissions only, and refuses every write, trade, cancel or withdraw endpoint before anything is signed. See [docs/KRAKEN_READ_ONLY_API_KEY.md](docs/KRAKEN_READ_ONLY_API_KEY.md).
- **The trend paper books** are research only; see [Trend paper (research)](#trend-paper-research).

No strategy has qualified, and every boundary above still holds.

**What stays true even when it can trade.** No language model may ever place, size or authorise an order — models advise, deterministic code decides. Execution lives in a separate local service that a model, the UI and the notification worker have no port to reach. Every rung of the ladder needs a fresh human approval bound to account, strategy, code and limits. The first live product is cash-funded spot LONG only: no margin, no borrowing, no leverage, no autonomous transfers, one position at a time. Derivatives and short selling are a separate programme after that, not an automatic unlock.

See [docs/EXECUTION_ARCHITECTURE.md](docs/EXECUTION_ARCHITECTURE.md) for the design and [docs/FUTURE_TRADING_ROADMAP.md](docs/FUTURE_TRADING_ROADMAP.md) for the gates.

## Status

Early, but real and measured.

| | |
|---|---|
| Tests | **2693** — all passing, none skipped, all offline: no network, no model calls |
| Test modules | 122 |
| Lines of production code | ~57 500 (Python ~52 000, control-room JavaScript/HTML/CSS ~5 400) |
| Lines of test code | ~54 000 (Python ~50 100, JavaScript ~3 900) |
| Frozen benchmark cases | 300 |
| Models promoted to production | **0** — none has passed the gates yet |

The analysis pipeline, the integrity layer, versioned evidence, cost scenarios and forward labels are implemented and covered. The local-model benchmark is built and has been run once; no model passed the promotion gates, so the current profile stays pinned.

The paper game, the pilot shadow wallet (`PAPER`) and the read-only account check are implemented, still with no order code. Each paper wallet reports free cash, the cost of open positions, a conservative value if closed now, and realized and open results separately, and `python scripts/audit_paper.py --db <file>` prints a read-only audit of the paper records. The research-only [trend paper](#trend-paper-research) books are the newest piece.

Trading is designed but **not implemented** — not a line of order code exists. See [Boundaries](#boundaries).

## Requirements

- Windows 11, Python 3.12
- [Ollama](https://ollama.com) running locally, with a model such as `qwen3:14b`
- A GPU helps: roughly 16 GB of VRAM for the default profile

## Run it

```powershell
pip install -r requirements.txt

python radar.py --mode heartbeat   # one cycle: public data, L1/L2, persistence
python radar.py --mode full        # one full cycle: L0-L3, local model, events
python radar.py --mode loop        # keep watching
python -m ui                       # desktop control room
```

The first run creates the SQLite store and applies additive migrations. Nothing existing is dropped or rewritten.

## Trend paper (research)

A research-only module that keeps paper books for four registered trend rules (ENS, ENS_VT, btc_trend5 and btc_trend5_vt) and their buy-and-hold comparators. It is paper only: it places no order, uses no account, API key or private endpoint, and reads only public daily candles (Binance for the signals and the main books, Kraken for a second set of EUR books). Every figure is pre-tax, and every rule is **NOT QUALIFIED**: this is a forward observation for research, not a tested strategy and not a reason to trade.

- **Books.** 24 books from 2026-10-04 (UTC): EUR and USDT, at a 0.1% and a 0.4% fee per leg, 7,000 of the quote currency each, filled at the Binance daily open. 12 Kraken EUR books use the same signals and fill at the Kraken XBTEUR/ETHEUR daily open, at 0.4% and 0.8% per leg.
- **When it runs.** Each time the radar loop starts (`python radar.py --mode loop`, or START RADAR in the control room), one background thread books every paper day missed since the last start, first the Binance books, then the Kraken books. It never blocks or stops the radar, and a failure is only logged. `RADAR_TREND_PAPER_ENABLED=0` turns it off. Nothing runs while the PC is off; missed days are booked at the next start.
- **By hand.** `python scripts/run_trend_paper.py run` books missed days and prints the report; `catch-up` only books, `report --offline` only reads. `python scripts/run_trend_paper_kraken.py run`, `catch-up` and `report` do the same for the Kraken books.
- **Where to see it.** The Game tab panel "Trend paper (research)" shows the Binance books and has a "Catch up now" button. The Kraken books are in the text report only. When the start-up catch-up books a day on which a rule changes exposure, one local Windows toast says so and ends with "paper only — no order placed".
- **Files.** `trend_paper/ledger.jsonl`, `kraken_ledger.jsonl` and `alerts.jsonl` in the state folder (the repository folder unless `RADAR_STATE_DIR` is set), all gitignored. The two ledgers are append-only and hash-chained; a damaged ledger is refused and never repaired automatically.

The operator guide explains every panel state and what to do when a ledger is refused: [docs/guides/TREND-PAPER.md](docs/guides/TREND-PAPER.md).

## Tests

```powershell
python scripts/run_tests.py     # full suite, offline, disposable state directory
python scripts/run_quality.py   # ruff, mypy, import boundaries
```

The suite runs against a throwaway state directory, with credential environment variables stripped from the child process, so it can never touch real state or reach a real service.

The runner is parallel by default (`--jobs N` worker processes, default one per CPU); `--serial` runs a single `unittest discover` process and `--list` prints the discovered test ids. See [TESTING.md](TESTING.md).

<details>
<summary><b>What the tests cover</b></summary>

- **Market layers** — universe, anomaly, structure, L2 features and OHLC, L3 microstructure, setups, tradeability, normalisation
- **Integrity and evidence** — Kraken timestamps, integrity wiring, versioned evidence, experiment ledger, store migrations
- **The local model** — prompt building, context building, model profiles, profile equivalence and runtime, router, benchmark harness and corpus
- **Money-adjacent maths** — cost lower bounds, domain costs, calibration, forward returns, outcome labels, Wilson intervals
- **Containment** — HTTP boundary, cloud-bridge containment, security, budget refusals, quality gates
- **Delivery** — alerts, events, notifications, outbox, cooldown, scheduler, worker, desktop control room
- **Paper and risk** — paper game and exits, paper position monitor, risk engine, pilot shadow wallet and its locks, paper records audit, declared trading modes, read-only account adapter and its refusal of write endpoints
- **Trend paper** — engine parity with the research records, registry, paper ledgers, Binance and Kraken public adapters, start hook, alerts, CLIs, panel, wording scan and an offline end-to-end replay

</details>

## Documentation

| Document | What it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The system as verified, and the accepted target |
| [ROADMAP.md](ROADMAP.md) | Phases, acceptance criteria, rollback |
| [RISK.md](RISK.md) | What the system is forbidden from doing |
| [docs/OPERATING_CONTRACTS.md](docs/OPERATING_CONTRACTS.md) | Evidence, scheduling, context and model evaluation rules |
| [docs/EXECUTION_ARCHITECTURE.md](docs/EXECUTION_ARCHITECTURE.md) | Future execution design: deterministic, never model-driven |
| [docs/FAILURE_AND_QUALITY.md](docs/FAILURE_AND_QUALITY.md) | Failure modes and quality gates |
| [TESTING.md](TESTING.md) | Test baseline and isolation |
| [docs/guides/TREND-PAPER.md](docs/guides/TREND-PAPER.md) | Trend paper panel, reports and ledger recovery |
| [docs/KRAKEN_READ_ONLY_API_KEY.md](docs/KRAKEN_READ_ONLY_API_KEY.md) | Optional read-only Kraken key for the account check |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Environment and setup |
| [DESIGN.md](DESIGN.md) | Visual system for the control room |

## Design principles

1. **Deterministic first.** Rules decide; models advise.
2. **Honest state.** The interface never shows what the data cannot prove.
3. **Fail closed.** Missing or stale evidence blocks the path instead of being guessed.
4. **Local only.** No cloud inference and no third-party services. Everything, including any future execution service, runs on your own machine.
5. **Evidence over opinion.** Every claim is tied to a versioned, hash-bound record.

## Disclaimer

This is research software. It is **not financial advice** and it makes no claim of profitability. Crypto markets can lose you everything you put in them.

## Licence

[AGPL-3.0](LICENSE). Copyright © 2026 Nuno Marques.
