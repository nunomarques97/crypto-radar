<div align="center">

# Crypto Radar

**A local-first crypto market radar that watches, reasons and warns.**

It holds no credentials and places no orders today. Execution is on the roadmap — behind seven approval gates, and never driven by a model.

[![tests](https://img.shields.io/badge/tests-1492_passing-2ea44f)](#tests)
[![python](https://img.shields.io/badge/python-3.12-3776ab)](#requirements)
[![platform](https://img.shields.io/badge/platform-Windows_11-0078d4)](#requirements)
[![inference](https://img.shields.io/badge/inference-100%25_local-6f42c1)](#how-it-works)
[![mode](https://img.shields.io/badge/mode-ANALYSIS__ONLY-critical)](#boundaries)
[![licence](https://img.shields.io/badge/licence-AGPL--3.0-f39c12)](#licence)

</div>

---

Crypto Radar watches public Kraken spot and perpetual markets, detects unusual activity with deterministic rules, asks a local model for a second opinion, and alerts a human.

Everything runs on your own machine: public market data in, SQLite on disk, inference through a local Ollama model. **No paid APIs and no cloud inference, ever.** Today it holds no exchange credentials at all, and reaches nothing beyond public market endpoints — see [Boundaries](#boundaries).

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

**What it cannot do today.** There is no order code in the repository. It holds no exchange credentials and asks for none, sends nothing off the machine beyond requests to public market endpoints, and shows no number the stored evidence cannot prove. Having credentials present would enable nothing: the mode is a declared state, not an inference from what happens to be on the machine.

**What stays true even when it can trade.** No language model may ever place, size or authorise an order — models advise, deterministic code decides. Execution lives in a separate local service that a model, the UI and the notification worker have no port to reach. Every rung of the ladder needs a fresh human approval bound to account, strategy, code and limits. The first live product is cash-funded spot LONG only: no margin, no borrowing, no leverage, no autonomous transfers, one position at a time. Derivatives and short selling are a separate programme after that, not an automatic unlock.

See [docs/EXECUTION_ARCHITECTURE.md](docs/EXECUTION_ARCHITECTURE.md) for the design and [docs/FUTURE_TRADING_ROADMAP.md](docs/FUTURE_TRADING_ROADMAP.md) for the gates.

## Status

Early, but real and measured.

| | |
|---|---|
| Tests | **1493** — 1492 passing, 1 skipped, all offline: no network, no model calls |
| Test files | 63 |
| Lines of production code | ~29 600 |
| Lines of test code | ~25 900 |
| Frozen benchmark cases | 302 |
| Models promoted to production | **0** — none has passed the gates yet |

The analysis pipeline, the integrity layer, versioned evidence, cost scenarios and forward labels are implemented and covered. The local-model benchmark is built and has been run once; no model passed the promotion gates, so the current profile stays pinned.

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

## Tests

```powershell
python scripts/run_tests.py     # full suite, offline, disposable state directory
python scripts/run_quality.py   # ruff, mypy, import boundaries
```

The suite runs against a throwaway state directory, with credential environment variables stripped from the child process, so it can never touch real state or reach a real service.

<details>
<summary><b>What the tests cover</b></summary>

- **Market layers** — universe, anomaly, structure, L2 features and OHLC, L3 microstructure, setups, tradeability, normalisation
- **Integrity and evidence** — Kraken timestamps, integrity wiring, versioned evidence, experiment ledger, store migrations
- **The local model** — prompt building, context building, model profiles, profile equivalence and runtime, router, benchmark harness and corpus
- **Money-adjacent maths** — cost lower bounds, domain costs, calibration, forward returns, outcome labels, Wilson intervals
- **Containment** — HTTP boundary, cloud-bridge containment, security, budget refusals, quality gates
- **Delivery** — alerts, events, notifications, outbox, cooldown, scheduler, worker, desktop control room

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
