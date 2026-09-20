<div align="center">

# Crypto Radar

**A local-first crypto market radar that watches, reasons and warns — and never places an order.**

[![tests](https://img.shields.io/badge/tests-1492_passing-2ea44f)](#tests)
[![python](https://img.shields.io/badge/python-3.12-3776ab)](#requirements)
[![platform](https://img.shields.io/badge/platform-Windows_11-0078d4)](#requirements)
[![inference](https://img.shields.io/badge/inference-100%25_local-6f42c1)](#how-it-works)
[![orders](https://img.shields.io/badge/places_orders-never-critical)](#what-it-will-not-do)
[![licence](https://img.shields.io/badge/licence-AGPL--3.0-f39c12)](#licence)

</div>

---

Crypto Radar watches public Kraken spot and perpetual markets, detects unusual activity with deterministic rules, asks a local model for a second opinion, and alerts a human.

Everything runs on your own machine: public market data in, SQLite on disk, inference through a local Ollama model. **No paid APIs, no cloud inference, no exchange credentials, no outbound calls beyond public market endpoints.**

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

## What it will not do

- Place, cancel or modify an order
- Hold exchange credentials, or ask for any
- Send your data anywhere
- Let a language model decide anything with money attached
- Show a number the stored evidence cannot prove

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

Trading is designed but **not implemented**. That programme is gated phase by phase behind explicit human approval, with a paper stage before any real money. See [docs/EXECUTION_ARCHITECTURE.md](docs/EXECUTION_ARCHITECTURE.md) and [docs/FUTURE_TRADING_ROADMAP.md](docs/FUTURE_TRADING_ROADMAP.md).

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
4. **Local only.** No cloud inference, no credentials, no outbound calls beyond public market endpoints.
5. **Evidence over opinion.** Every claim is tied to a versioned, hash-bound record.

## Disclaimer

This is research software. It is **not financial advice** and it makes no claim of profitability. Crypto markets can lose you everything you put in them.

## Licence

[AGPL-3.0](LICENSE). Copyright © 2026 Nuno Marques.
