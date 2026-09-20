# Crypto Radar

A local-first crypto market radar for Windows. It watches public Kraken spot and perpetual markets, detects unusual activity with deterministic rules, asks a local model for a second opinion, and alerts a human. **It never places orders.**

Everything runs on your own machine: public market data in, SQLite on disk, inference through a local Ollama model. No paid APIs, no cloud inference, no exchange credentials.

## What it does

```
L0 Universe     public pairs and tickers               →  snapshot
L1 Anomaly      z-scores of return, volume, trades, OI →  shortlist of 40
L2 Structure    OHLC, ATR-normalised features, setups  →  up to 10 candidates
L3 Micro        depth, trades, perpetual book          →  up to 8 finalists
Screener        local model, structured JSON only      →  advisory verdict
```

Every layer is deterministic and testable. The model is advisory: it can flag or abstain, but it cannot size a position, set a stop, or authorise anything. Facts it cannot support are rejected before they reach a notification.

Integrity comes first: prices, timestamps and order books are validated before they are consumed. What cannot be verified is marked `UNKNOWN`, never silently treated as good.

## Status

Early but real. The analysis pipeline, the integrity layer, versioned evidence, cost scenarios and forward labels are implemented and covered by **1359 tests** that run offline. The local-model benchmark is built and has been executed once; no model has passed the promotion gates yet, so the current profile stays in place.

Trading is designed but not implemented. That programme is gated phase by phase behind explicit human approval, with a paper-trading stage before any real money. See [`docs/EXECUTION_ARCHITECTURE.md`](docs/EXECUTION_ARCHITECTURE.md) and [`docs/FUTURE_TRADING_ROADMAP.md`](docs/FUTURE_TRADING_ROADMAP.md).

## Requirements

- Windows 11, Python 3.12
- Ollama running locally with a model such as `qwen3:14b`
- A GPU helps; roughly 16 GB of VRAM for the default profile

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
python scripts/run_tests.py     # full suite, offline
python scripts/run_quality.py   # ruff, mypy, import boundaries
```

## Documentation

| Document | What it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The system as verified, and the accepted target |
| [ROADMAP.md](ROADMAP.md) | Phases, acceptance criteria, rollback |
| [RISK.md](RISK.md) | What the system is forbidden from doing |
| [docs/OPERATING_CONTRACTS.md](docs/OPERATING_CONTRACTS.md) | Evidence, scheduling, context and model evaluation rules |
| [docs/EXECUTION_ARCHITECTURE.md](docs/EXECUTION_ARCHITECTURE.md) | Future execution design: deterministic, never model-driven |
| [TESTING.md](TESTING.md) | Test baseline and isolation |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Environment and setup |

## Design principles

1. **Deterministic first.** Rules decide; models advise.
2. **Honest state.** The interface never shows what the data cannot prove.
3. **Fail closed.** Missing or stale evidence blocks the path instead of being guessed.
4. **Local only.** No cloud inference, no credentials, no outbound calls beyond public market endpoints.
5. **Evidence over opinion.** Every claim is tied to a versioned, hash-bound record.

## Disclaimer

This is research software. It is not financial advice, and it makes no claim of profitability. Crypto markets can lose you everything you put in them.

## Licence

AGPL-3.0. Copyright (c) 2026 Nuno Marques. See [LICENSE](LICENSE).
