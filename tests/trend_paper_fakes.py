"""Offline market data for the trend paper CLI and hook tests (no network).

``live_market(now)`` is the vendored BTCUSDT/ETHUSDT history (closed candles up to 2026-10-02),
extended with deterministic synthetic candles from 2026-10-03 to the day before ``now`` and a
still-open candle (open only) on ``now``'s day. EUR pairs are the USDT prices / 1.17 and EURUSDT is
1.17. The synthetic days are test data, not market history.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from radar_v08.domain import trend_engine as E
from radar_v08.domain import trend_paper as P

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "trend"
LAST_REAL_CLOSE = date(2026, 10, 2)
FX = 1.17
DRIFT = (0.012, -0.018, 0.007, 0.021, -0.009, -0.025, 0.015, 0.004, -0.011, 0.019)


def at(day: date, hour: int = 8) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _real(symbol: str) -> list[E.Bar]:
    rows = json.loads((FIXTURES / f"spot1d_{symbol}.json").read_text(encoding="ascii"))
    return list(E.bars_from_rows([r for r in rows if E.utc_day(r[0]) <= LAST_REAL_CLOSE]))


def _extended(symbol: str, now: datetime) -> tuple[list[E.Bar], float]:
    bars = _real(symbol)
    price = bars[-1].close
    day = LAST_REAL_CLOSE + timedelta(days=1)
    k = 0
    while day < now.date():
        close = round(price * (1 + DRIFT[k % len(DRIFT)]), 2)
        bars.append(E.Bar(E.day_open_ms(day), price, max(price, close), min(price, close), close))
        price = close
        day += timedelta(days=1)
        k += 1
    return bars, price


def live_market(now: datetime) -> dict[str, P.DailySeries]:
    market: dict[str, P.DailySeries] = {}
    for asset, symbol in P.USDT_SYMBOL.items():
        bars, live_open = _extended(symbol, now)
        market[symbol] = P.DailySeries(symbol, tuple(bars), now.date(), live_open)
        eur = tuple(E.Bar(b.open_time_ms, b.open / FX, b.high / FX, b.low / FX, b.close / FX) for b in bars)
        market[P.EUR_SYMBOL[asset]] = P.DailySeries(P.EUR_SYMBOL[asset], eur, now.date(), live_open / FX)
    btc = market["BTCUSDT"].closed
    fx = tuple(E.Bar(b.open_time_ms, FX, FX, FX, FX) for b in btc)
    market[P.EURUSDT] = P.DailySeries(P.EURUSDT, fx, now.date(), FX)
    return market


def without(market: dict[str, P.DailySeries], symbol: str, *days: date) -> dict[str, P.DailySeries]:
    """``market`` with the candles of ``symbol`` on ``days`` removed (closed or still open)."""
    gone = {E.day_open_ms(d) for d in days}
    s = market[symbol]
    live = s.live_day is not None and E.day_open_ms(s.live_day) in gone
    bars = tuple(b for b in s.closed if b.open_time_ms not in gone)
    return {**market, symbol: P.DailySeries(symbol, bars, None if live else s.live_day, None if live else s.live_open)}


def ending(market: dict[str, P.DailySeries], symbol: str, last: date) -> dict[str, P.DailySeries]:
    """``market`` with ``symbol`` cut after ``last`` (no still-open candle)."""
    s = market[symbol]
    return {**market, symbol: P.DailySeries(symbol, tuple(b for b in s.closed if E.utc_day(b.open_time_ms) <= last))}


class FakeFetcher:
    """Serves ``market`` like the Binance adapter; can block on an event or fail. ``second`` (a
    market) answers every request after the first per symbol, e.g. a gap confirmation."""

    def __init__(self, market, *, block: threading.Event | None = None, error: Exception | None = None, second=None):
        self.market = market
        self.second = second
        self.block = block
        self.error = error
        self.calls: list[tuple[str, date, int]] = []
        self.closed = False

    def fetch_daily(self, symbol, since, now_ms):
        repeat = any(c[0] == symbol for c in self.calls)
        self.calls.append((symbol, since, now_ms))
        if self.block is not None:
            self.block.wait(30)
        if self.error is not None:
            raise self.error
        s = (self.second if repeat and self.second is not None else self.market)[symbol]
        bars = tuple(b for b in s.closed if b.open_time_ms >= E.day_open_ms(since))
        return P.DailySeries(symbol, bars, s.live_day, s.live_open)

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# A fake Binance for the real adapter (radar_v08.adapters.binance_public_klines)
# ---------------------------------------------------------------------------

#: The still-open candle's high and close as served: open x this factor, so a leak is visible.
LIVE_FACTOR = 7.77
#: In a fault queue: serve this request normally.
SERVE = "serve"


class FakeResponse:
    def __init__(self, status=200, body=b"[]", headers=None, history=(), cut_after=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.history = list(history)
        self.cut_after = cut_after  # bytes sent before the connection drops (ChunkedEncodingError)
        self.closed = False

    def iter_content(self, chunk_size=1):
        import requests

        body = self._body if self.cut_after is None else self._body[: self.cut_after]
        for k in range(0, len(body), chunk_size):
            yield body[k : k + chunk_size]
        if self.cut_after is not None:
            raise requests.exceptions.ChunkedEncodingError("connection broken: incomplete read")

    def close(self):
        self.closed = True


def kline_row(bar: E.Bar) -> list:
    t = bar.open_time_ms
    return [t, repr(bar.open), repr(bar.high), repr(bar.low), repr(bar.close), "1.0", t + E.MS_PER_DAY - 1, "0", 1, "0", "0", "0"]


class FakeExchange:
    """A ``requests.Session`` stand-in that answers ``GET /api/v3/klines`` from ``market`` the way
    Binance would at ``market``'s live day: closed candles from ``startTime`` (at most ``limit``),
    then the still-open candle with a visibly wrong high and close (:data:`LIVE_FACTOR`).

    ``faults`` maps a symbol (or ``"*"`` for any) to a queue consumed one request at a time: an
    exception is raised, a :class:`FakeResponse` is returned, :data:`SERVE` serves normally and a
    callable receives the normal rows and returns a response. Requests are recorded in ``calls``."""

    def __init__(self, market, faults=None):
        self.market = market
        self.faults = {k: list(v) for k, v in (faults or {}).items()}
        self.calls: list[tuple[str, str, dict]] = []
        self.trust_env = True
        self.auth = None

    def rows(self, symbol: str, start: int, limit: int) -> list:
        s = self.market[symbol]
        rows = [kline_row(b) for b in s.closed if b.open_time_ms >= start]
        if s.live_day is not None and E.day_open_ms(s.live_day) >= start:
            o = s.live_open
            rows.append(kline_row(E.Bar(E.day_open_ms(s.live_day), o, o * LIVE_FACTOR, o * 0.5, o * LIVE_FACTOR)))
        return rows[:limit]

    def request(self, method, url, params=None, **kwargs):
        params = dict(params or {})
        self.calls.append((method, url, params))
        symbol = params.get("symbol")
        queue = self.faults.get(symbol) or self.faults.get("*")
        rows = self.rows(symbol, params.get("startTime", 0), params.get("limit", 500))
        if queue:
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, FakeResponse):
                return item
            if callable(item):
                return item(rows)
        return FakeResponse(200, json.dumps(rows).encode())

    def symbol_calls(self, symbol: str) -> list[dict]:
        return [p for _, _, p in self.calls if p.get("symbol") == symbol]

    def close(self):
        pass


class FakeMonotonic:
    """A monotonic clock that the injected sleep advances (no real waiting)."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def adapter_factory(exchange: FakeExchange, made: list | None = None, **kwargs):
    """A fetcher factory for the real adapter over ``exchange`` with a fake clock and sleep."""
    from radar_v08.adapters.binance_public_klines import BinancePublicKlines

    def factory():
        clock = FakeMonotonic()
        client = BinancePublicKlines(session=exchange, sleep=clock.sleep, monotonic=clock, **kwargs)
        client.fake_clock = clock
        if made is not None:
            made.append(client)
        return client

    return factory


def strip_ts(data: bytes) -> list[dict]:
    """The ledger lines without ``ts`` and the chain fields derived from it, for comparing runs."""
    return [
        {k: v for k, v in json.loads(line).items() if k not in ("ts", "sha256", "prev")}
        for line in data.splitlines()
    ]


def _body_cut(rows):
    data = json.dumps(rows).encode()
    return FakeResponse(200, data[: len(data) // 2])


def _drop_last(n):
    return lambda rows: FakeResponse(200, json.dumps(rows[:-n]).encode())


def fault_scenarios():
    """Network and data faults of one catch-up, each with the outcome it must give: ``market``
    (a market-data failure) or ``waiting`` (WAITING_FOR_DATA). Fresh objects on every call."""
    import requests

    def times(make, n=3):
        return [make() for _ in range(n)]

    return {
        "timeout": ({"BTCUSDT": times(lambda: requests.exceptions.ReadTimeout("read timed out"))}, "market"),
        "connection error": ({"ETHUSDT": times(lambda: requests.exceptions.ConnectionError("refused"))}, "market"),
        "http 5xx": ({"*": times(lambda: FakeResponse(503))}, "market"),
        "http 429 without Retry-After": ({"BTCUSDT": times(lambda: FakeResponse(429))}, "market"),
        "http 429 with a Retry-After past the deadline": (
            {"ETHEUR": [FakeResponse(429, headers={"Retry-After": "3600"})]}, "market",
        ),
        "http 418": ({"*": [FakeResponse(418)]}, "market"),
        "body cut off in transit": (
            {"EURUSDT": times(lambda: FakeResponse(200, json.dumps([[0]] * 50).encode(), cut_after=64))}, "market",
        ),
        "truncated body": ({"BTCEUR": [_body_cut]}, "market"),
        "invalid json": ({"ETHUSDT": [FakeResponse(200, b"<html>busy</html>")]}, "market"),
        "empty page": ({"ETHUSDT": [FakeResponse(200, b"[]")]}, "waiting"),
        "page that ends before the requested range": ({"ETHUSDT": [_drop_last(3)]}, "waiting"),
        "last page of a long history cut short": ({"BTCUSDT": [SERVE, SERVE, SERVE, _drop_last(2)]}, "waiting"),
        # An EUR pair that ends early must not switch the due days to the EURUSDT fallback.
        "empty BTCEUR page": ({"BTCEUR": [FakeResponse(200, b"[]")]}, "waiting"),
        "ETHEUR page that ends before the requested range": ({"ETHEUR": [_drop_last(2)]}, "waiting"),
    }


# ---------------------------------------------------------------------------
# Kraken EUR books: Kraken XBTEUR/ETHEUR daily candles
# ---------------------------------------------------------------------------

#: Kraken's synthetic price relative to the Binance EUR one (USDT / FX), so the difference shows.
KRAKEN_PREMIUM = {"XBTEUR": 1.0004, "ETHEUR": 0.9993}
KRAKEN_FIRST = date(2026, 9, 1)


def kraken_market(now: datetime, first: date = KRAKEN_FIRST) -> dict[str, P.DailySeries]:
    """Kraken XBTEUR/ETHEUR from ``first``: the synthetic USDT candles / FX x :data:`KRAKEN_PREMIUM`,
    with the still-open candle (open only) on ``now``'s day. Test data, not market history."""
    from radar_v08.domain.trend_paper_kraken import KRAKEN_PAIR

    market: dict[str, P.DailySeries] = {}
    for asset, pair in KRAKEN_PAIR.items():
        bars, live_open = _extended(P.USDT_SYMBOL[asset], now)
        k = KRAKEN_PREMIUM[pair] / FX
        kept = tuple(
            E.Bar(b.open_time_ms, round(b.open * k, 1), round(b.high * k, 1), round(b.low * k, 1), round(b.close * k, 1))
            for b in bars
            if E.utc_day(b.open_time_ms) >= first
        )
        market[pair] = P.DailySeries(pair, kept, now.date(), round(live_open * k, 1))
    return market


class FakeKrakenFetcher(FakeFetcher):
    """Serves Kraken pairs like the Kraken adapter: a :class:`KrakenDailySeries` whose
    ``history_start`` is set when the served history (``market``, cut at ``history_from``) starts
    after the requested ``since``."""

    def __init__(self, market, *, history_from: date | None = None, **kwargs):
        super().__init__(market, **kwargs)
        self.history_from = history_from

    def fetch_daily(self, symbol, since, now_ms):
        from radar_v08.adapters.kraken_public_ohlc import KrakenDailySeries

        s = super().fetch_daily(symbol, since, now_ms)
        bars = s.closed
        if self.history_from is not None:
            bars = tuple(b for b in bars if E.utc_day(b.open_time_ms) >= self.history_from)
        days = [E.utc_day(b.open_time_ms) for b in bars] + ([s.live_day] if s.live_day is not None else [])
        first = min(days) if days else None
        history_start = first if first is not None and first > since else None
        return KrakenDailySeries(symbol, bars, s.live_day, s.live_open, history_start=history_start)


def kraken_row(bar: E.Bar) -> list:
    return [bar.open_time_ms // 1000, repr(bar.open), repr(bar.high), repr(bar.low), repr(bar.close), repr(bar.close), "3.5", 120]


class FakeKrakenExchange:
    """A ``requests.Session`` stand-in that answers ``GET /0/public/OHLC`` from ``market`` the way
    Kraken would at ``market``'s live day: the latest 720 candles after ``since`` (Kraken's cap), the
    still-open candle last with a visibly wrong high and close (:data:`LIVE_FACTOR`). ``faults`` work
    as in :class:`FakeExchange`, keyed by pair."""

    RESULT_KEY = {"XBTEUR": "XXBTZEUR", "ETHEUR": "XETHZEUR"}

    def __init__(self, market, faults=None):
        self.market = market
        self.faults = {k: list(v) for k, v in (faults or {}).items()}
        self.calls: list[tuple[str, str, dict]] = []
        self.trust_env = True
        self.auth = None

    def rows(self, pair: str, since: int) -> list:
        s = self.market[pair]
        rows = [kraken_row(b) for b in s.closed if b.open_time_ms // 1000 > since]
        if s.live_day is not None and E.day_open_ms(s.live_day) // 1000 > since:
            o = s.live_open
            rows.append(kraken_row(E.Bar(E.day_open_ms(s.live_day), o, o * LIVE_FACTOR, o * 0.5, o * LIVE_FACTOR)))
        return rows[-720:]

    def envelope(self, pair: str, rows: list) -> bytes:
        last = rows[-1][0] if rows else 0
        return json.dumps({"error": [], "result": {self.RESULT_KEY[pair]: rows, "last": last}}).encode()

    def request(self, method, url, params=None, **kwargs):
        params = dict(params or {})
        self.calls.append((method, url, params))
        pair = params.get("pair")
        queue = self.faults.get(pair) or self.faults.get("*")
        rows = self.rows(pair, params.get("since", 0))
        if queue:
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, KrakenResponse):
                return item
            if callable(item):
                return item(rows)
        return KrakenResponse(200, self.envelope(pair, rows))

    def pair_calls(self, pair: str) -> list[dict]:
        return [p for _, _, p in self.calls if p.get("pair") == pair]

    def close(self):
        pass


class KrakenResponse:
    """A response as the Kraken adapter reads it (``content``, ``raise_for_status``)."""

    def __init__(self, status=200, content=b"{}", headers=None, history=()):
        self.status_code = status
        self.content = content
        self.headers = dict(headers or {})
        self.history = list(history)
        self.closed = False

    def raise_for_status(self):
        import requests

        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


def kraken_adapter_factory(exchange: FakeKrakenExchange, made: list | None = None, **kwargs):
    """A factory for the real Kraken adapter over ``exchange`` with a fake clock and sleep."""
    from radar_v08.adapters.kraken_public_ohlc import KrakenPublicOhlc

    def factory():
        clock = FakeMonotonic()
        client = KrakenPublicOhlc(http_session=exchange, sleep=clock.sleep, timer=clock, **kwargs)
        if made is not None:
            made.append(client)
        return client

    return factory


# ---------------------------------------------------------------------------
# End-to-end scenario: 60 paper days of radar starts
# ---------------------------------------------------------------------------
#
# One shared definition for tests/test_trend_paper_e2e.py and scripts/replay_trend_paper.py: the
# vendored candles of tests/fixtures/trend/e2e/ (generated by e2e_candle_files; test data, not market
# history), the fixed list of radar starts (E2E_STARTS) and the driver that runs each start through
# the real radar start path (run_e2e_scenario).

E2E_DIR = FIXTURES / "e2e"
E2E_FIRST_DAY = P.PAPER_START
E2E_LAST_DAY = date(2026, 12, 2)  # 60 paper days, 2026-10-04 .. 2026-12-02
E2E_KRAKEN_FROM = date(2026, 9, 27)  # KRAKEN_LEAD_DAYS before the first paper day (the first Kraken request)
E2E_MISSING_EUR = ("BTCEUR", date(2026, 10, 9))  # absent for good: the EUR books use the EURUSDT fallback
E2E_MISSING_KRAKEN = ("XBTEUR", date(2026, 10, 25))  # absent for good: NO_KRAKEN_OPEN skip, no substitute
#: Close-to-close returns of the synthetic USDT days from 2026-10-03, as (days, return) runs: a drift up,
#: a crash that takes every rule flat, a quiet base, a rally that takes them back in, then a drift.
E2E_RETURNS: dict[str, tuple[tuple[int, float], ...]] = {
    "BTCUSDT": ((8, 0.004), (14, -0.035), (8, 0.0), (20, 0.03), (11, 0.003)),
    "ETHUSDT": ((10, 0.003), (12, -0.045), (8, 0.002), (18, 0.04), (13, -0.002)),
}
#: EURUSDT opens of the paper days, cycled (each day's close is the next day's open).
E2E_FX = (1.17, 1.1712, 1.1695, 1.1688, 1.1721, 1.1704, 1.1693)
#: Binance EUR pairs: USDT price / EURUSDT x this factor, so the EURUSDT fallback price differs from them.
E2E_EUR_PREMIUM = {"BTCEUR": 1.0006, "ETHEUR": 0.9995}
#: Kraken pairs: (USDT symbol, factor on the USDT price / FX, decimals), so a Kraken fill differs from both.
E2E_KRAKEN_PRICE = {"XBTEUR": ("BTCUSDT", 1.0004, 1), "ETHEUR": ("ETHUSDT", 0.9993, 2)}
E2E_BINANCE_FILES = {
    "BTCUSDT": "binance_BTCUSDT_extension.json",
    "ETHUSDT": "binance_ETHUSDT_extension.json",
    "BTCEUR": "binance_BTCEUR.json",
    "ETHEUR": "binance_ETHEUR.json",
    "EURUSDT": "binance_EURUSDT.json",
}
E2E_KRAKEN_FILES = {"XBTEUR": "kraken_XBTEUR.json", "ETHEUR": "kraken_ETHEUR.json"}
#: Produced file -> its golden. The ledgers and the dedupe file are compared byte for byte.
E2E_STATE_GOLDENS = {
    "ledger.jsonl": "golden_ledger.jsonl",
    "kraken_ledger.jsonl": "golden_kraken_ledger.jsonl",
    "alerts.jsonl": "golden_alerts.jsonl",
}
E2E_TOASTS_GOLDEN = "golden_toasts.json"
E2E_READER_GOLDEN = "golden_reader.json"
E2E_STARTS_GOLDEN = "golden_starts.json"
E2E_MANIFEST = "MANIFEST.json"


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=k) for k in range((last - first).days + 1)]


def _e2e_usdt(symbol: str) -> list[E.Bar]:
    """The synthetic USDT days 2026-10-03 .. 2026-12-02 after the vendored history (open = previous close)."""
    price = _real(symbol)[-1].close
    days = iter(_days(LAST_REAL_CLOSE + timedelta(days=1), E2E_LAST_DAY))
    bars: list[E.Bar] = []
    for count, ret in E2E_RETURNS[symbol]:
        for _ in range(count):
            close = round(price * (1 + ret), 2)
            bars.append(E.Bar(E.day_open_ms(next(days)), price, max(price, close), min(price, close), close))
            price = close
    if next(days, None) is not None:
        raise AssertionError(f"{symbol}: E2E_RETURNS must cover every day up to {E2E_LAST_DAY}")
    return bars


def _bar(day: date, o: float, c: float) -> E.Bar:
    return E.Bar(E.day_open_ms(day), o, max(o, c), min(o, c), c)


def _rows_bytes(bars: list[E.Bar]) -> bytes:
    rows = [json.dumps([b.open_time_ms, b.open, b.high, b.low, b.close], separators=(",", ":")) for b in bars]
    return ("[\n" + ",\n".join(rows) + "\n]\n").encode("ascii")


def e2e_candle_files() -> dict[str, bytes]:
    """The vendored candle files of the scenario, generated deterministically (file name -> bytes)."""
    usdt = {s: _e2e_usdt(s) for s in E2E_RETURNS}
    paper = _days(E2E_FIRST_DAY, E2E_LAST_DAY)
    fx = [(E2E_FX[k % len(E2E_FX)], E2E_FX[(k + 1) % len(E2E_FX)]) for k in range(len(paper))]
    by_day = {s: {E.utc_day(b.open_time_ms): b for b in bars} for s, bars in usdt.items()}
    files = {E2E_BINANCE_FILES[s]: _rows_bytes(bars) for s, bars in usdt.items()}
    files[E2E_BINANCE_FILES[P.EURUSDT]] = _rows_bytes([_bar(d, o, c) for d, (o, c) in zip(paper, fx, strict=True)])
    for asset, symbol in P.EUR_SYMBOL.items():
        bars = []
        for d, (fo, fc) in zip(paper, fx, strict=True):
            if (symbol, d) == E2E_MISSING_EUR:
                continue
            u = by_day[P.USDT_SYMBOL[asset]][d]
            k = E2E_EUR_PREMIUM[symbol]
            bars.append(_bar(d, round(u.open / fo * k, 2), round(u.close / fc * k, 2)))
        files[E2E_BINANCE_FILES[symbol]] = _rows_bytes(bars)
    for pair, (symbol, k, decimals) in E2E_KRAKEN_PRICE.items():
        history = {E.utc_day(b.open_time_ms): b for b in _real(symbol)} | by_day[symbol]
        bars = [
            _bar(d, round(history[d].open / FX * k, decimals), round(history[d].close / FX * k, decimals))
            for d in _days(E2E_KRAKEN_FROM, E2E_LAST_DAY)
            if (pair, d) != E2E_MISSING_KRAKEN
        ]
        files[E2E_KRAKEN_FILES[pair]] = _rows_bytes(bars)
    return files


def _load_rows(directory: Path, name: str) -> tuple[E.Bar, ...]:
    return E.bars_from_rows(json.loads((directory / name).read_text(encoding="ascii")))


def load_e2e_candles(directory: Path = E2E_DIR) -> dict[str, dict[str, tuple[E.Bar, ...]]]:
    """``{"binance": {symbol: bars}, "kraken": {pair: bars}}`` from the vendored files; the USDT series
    are the vendored history up to 2026-10-02 followed by the scenario's extension days."""
    binance = {s: _load_rows(directory, name) for s, name in E2E_BINANCE_FILES.items()}
    for symbol in P.USDT_SYMBOL.values():
        binance[symbol] = tuple(_real(symbol)) + binance[symbol]
    kraken = {pair: _load_rows(directory, name) for pair, name in E2E_KRAKEN_FILES.items()}
    return {"binance": binance, "kraken": kraken}


def _as_of(symbol: str, bars: tuple[E.Bar, ...], now: datetime) -> P.DailySeries:
    """What the exchange shows at ``now``: closed candles before its UTC day, that day's candle still open."""
    today_ms = E.day_open_ms(now.date())
    live = next((b for b in bars if b.open_time_ms == today_ms), None)
    closed = tuple(b for b in bars if b.open_time_ms < today_ms)
    if live is None:
        return P.DailySeries(symbol, closed)
    return P.DailySeries(symbol, closed, now.date(), live.open)


def e2e_markets(candles, now: datetime) -> tuple[dict[str, P.DailySeries], dict[str, P.DailySeries]]:
    """The Binance and Kraken markets as served at ``now``."""
    return (
        {s: _as_of(s, bars, now) for s, bars in candles["binance"].items()},
        {p: _as_of(p, bars, now) for p, bars in candles["kraken"].items()},
    )


@dataclass(frozen=True)
class E2EStart:
    """One radar start at a fixed fake-clock UTC moment. ``binance_down``: every Binance request of
    that start fails (a connection error, then HTTP 503 and 502, through the adapter's retries)."""

    moment: datetime
    note: str = ""
    binance_down: bool = False


def _start(month: int, day: int, hour: int = 8, minute: int = 0, note: str = "", binance_down: bool = False) -> E2EStart:
    return E2EStart(datetime(2026, month, day, hour, minute, tzinfo=UTC), note, binance_down)


E2E_STARTS: tuple[E2EStart, ...] = (
    _start(10, 4, 7, 30, "first paper day"),
    _start(10, 5),
    _start(10, 7),
    _start(10, 10, 9, 15, "after BTCEUR 2026-10-09 went missing for good"),
    _start(10, 12),
    _start(10, 13),
    _start(10, 14),
    _start(10, 15),
    _start(10, 16),
    _start(10, 17, note="Binance unreachable", binance_down=True),
    _start(10, 18, note="books the day the failed start missed"),
    _start(10, 18, 19, 45, "duplicate start on the same UTC day"),
    _start(10, 19),
    _start(10, 20),
    _start(10, 23),
    _start(10, 26, note="after Kraken XBTEUR 2026-10-25 went missing for good"),
    _start(10, 30),
    _start(11, 3),
    _start(11, 6),
    _start(11, 8, 22, 10, "last start before the PC is off for 5 days"),
    _start(11, 14, 6, 5, "PC back on: backfills the missed days"),
    _start(11, 17),
    _start(11, 20),
    _start(11, 22),
    _start(11, 25),
    _start(11, 30),
    _start(12, 2, note="final start"),
)
#: The fixed moment of the UI reader payload after the last start.
E2E_READER_NOW = datetime(2026, 12, 2, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class E2EStartResult:
    index: int
    start: E2EStart
    binance_status: str  # from the hook's log line: BOOKED, UP_TO_DATE, FAILED, WAITING_FOR_DATA, ...
    kraken_status: str
    booked: tuple[date, ...]
    skipped: tuple[date, ...]
    kraken_booked: tuple[date, ...]
    kraken_skipped: tuple[date, ...]
    toasts: tuple[tuple[str, str], ...]
    alert_lines: int  # lines appended to alerts.jsonl
    requests: dict[str, int]  # requests that reached each fake exchange
    state_before: dict[str, str]  # sha256 of ledger.jsonl, kraken_ledger.jsonl and alerts.jsonl before the start
    state_after: dict[str, str]  # ... and after it

    def summary(self) -> dict:
        def days(values: tuple[date, ...]) -> list[str]:
            return [d.isoformat() for d in values]

        return {
            "start": self.index,
            "moment": self.start.moment.isoformat(),
            "note": self.start.note,
            "binance": {"status": self.binance_status, "booked": days(self.booked), "skipped": days(self.skipped)},
            "kraken": {
                "status": self.kraken_status, "booked": days(self.kraken_booked), "skipped": days(self.kraken_skipped),
            },
            "toasts": len(self.toasts),
            "alert_lines": self.alert_lines,
            "requests": dict(self.requests),
        }

    def line(self) -> str:
        def span(values: tuple[date, ...]) -> str:
            if not values:
                return "-"
            return values[0].isoformat() if len(values) == 1 else f"{values[0]}..{values[-1]} ({len(values)})"

        return (
            f"#{self.index:02d} {self.start.moment.isoformat()}  Binance {self.binance_status:<10} "
            f"booked {span(self.booked)} skipped {span(self.skipped)} | Kraken {self.kraken_status:<10} "
            f"booked {span(self.kraken_booked)} skipped {span(self.kraken_skipped)} | toasts {len(self.toasts)}"
            + (f"  [{self.start.note}]" if self.start.note else "")
        )


class _Messages(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _status(messages: list[str], prefix: str) -> str:
    """The outcome of one step from the hook's own log lines (``prefix``: ``Trend paper`` or
    ``Kraken EUR paper``)."""
    for m in messages:
        if m.startswith(f"{prefix} catch-up: "):
            return m[len(prefix) + len(" catch-up: "):].split(",")[0]
        if m.startswith(f"{prefix} catch-up waiting"):
            return "WAITING_FOR_DATA"
        if m.startswith(f"{prefix} ledger refused"):
            return "LEDGER_REFUSED"
        if m.startswith((f"{prefix} catch-up failed", f"{prefix} catch-up not run")):
            return "FAILED"
    return "NOT_RUN"


def _down_faults() -> dict[str, list]:
    import requests

    failing = [requests.exceptions.ConnectionError("connection refused (scenario)"), FakeResponse(503), FakeResponse(502)]
    return {"*": failing * 4}


def _bytes(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def _digests(files: dict[str, Path]) -> dict[str, str]:
    import hashlib

    return {name: hashlib.sha256(_bytes(path)).hexdigest() for name, path in files.items()}


def run_e2e_scenario(state_dir: Path, candles=None, starts: tuple[E2EStart, ...] = E2E_STARTS) -> list[E2EStartResult]:
    """Run every start of the scenario through ``cli._start_trend_paper_catch_up`` with
    ``config.STATE_DIR`` = ``state_dir`` and ``RADAR_TREND_PAPER_ENABLED`` on, joined on the
    catch-up thread. Only the seams are replaced: ``trend_paper_hook.utc_now`` (the start's moment),
    ``default_fetcher`` and ``default_kraken_fetchers`` (the real adapters over fake HTTP sessions with
    a fake sleep and monotonic clock) and ``notifications.send_windows_notification`` (a recorder)."""
    import contextlib
    from unittest import mock

    from radar_v08 import cli, config, notifications, trend_paper_hook
    from radar_v08.adapters import trend_paper_store as store
    from radar_v08.adapters.trend_alert_store import alerts_path
    from radar_v08.domain.trend_paper_kraken import extend_kraken_ledger, kraken_skipped

    candles = candles if candles is not None else load_e2e_candles()
    state_dir = Path(state_dir)
    ledger_file = store.ledger_path(state_dir)
    kraken_file = trend_paper_hook.kraken_ledger_path(state_dir)
    alerts_file = alerts_path(state_dir)
    state_files = {"ledger.jsonl": ledger_file, "kraken_ledger.jsonl": kraken_file, "alerts.jsonl": alerts_file}
    logger = logging.getLogger("radar_v08.trend_paper")
    results: list[E2EStartResult] = []
    for index, start in enumerate(starts, 1):
        binance_market, kraken_market = e2e_markets(candles, start.moment)
        main = FakeExchange(binance_market, _down_faults() if start.binance_down else None)
        signals = FakeExchange(binance_market, _down_faults() if start.binance_down else None)
        kraken = FakeKrakenExchange(kraken_market)
        toasts: list[tuple[str, str]] = []

        def toast(title, message, *args, _toasts=toasts, **kwargs):
            _toasts.append((title, message))
            return True

        def kraken_fetchers(s=signals, k=kraken):
            return adapter_factory(s)(), kraken_adapter_factory(k)()

        before = store.read_ledger(ledger_file)
        kraken_before = store.read_ledger(kraken_file, extend=extend_kraken_ledger)
        alerts_before = _bytes(alerts_file)
        state_before = _digests(state_files)
        handler = _Messages()
        level, propagate = logger.level, logger.propagate
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(config, "STATE_DIR", str(state_dir)))
            stack.enter_context(mock.patch.object(config, "RADAR_TREND_PAPER_ENABLED", True))
            stack.enter_context(mock.patch.object(trend_paper_hook, "utc_now", lambda m=start.moment: m))
            stack.enter_context(mock.patch.object(trend_paper_hook, "default_fetcher", adapter_factory(main)))
            stack.enter_context(mock.patch.object(trend_paper_hook, "default_kraken_fetchers", kraken_fetchers))
            stack.enter_context(mock.patch.object(notifications, "send_windows_notification", toast))
            logger.setLevel(logging.INFO)
            logger.propagate = False  # the run's log lines reach no handler outside this run
            logger.addHandler(handler)
            try:
                running = set(threading.enumerate())
                cli._start_trend_paper_catch_up()
                for thread in threading.enumerate():
                    if thread.name == trend_paper_hook.THREAD_NAME and thread not in running:
                        thread.join(120)
                        if thread.is_alive():
                            raise AssertionError(f"start {index}: the catch-up thread did not finish")
            finally:
                logger.removeHandler(handler)
                logger.setLevel(level)
                logger.propagate = propagate
        after = store.read_ledger(ledger_file)
        kraken_after = store.read_ledger(kraken_file, extend=extend_kraken_ledger)
        alerts_after = _bytes(alerts_file)
        if not alerts_after.startswith(alerts_before):
            raise AssertionError(f"start {index}: alerts.jsonl was rewritten")
        results.append(
            E2EStartResult(
                index=index,
                start=start,
                binance_status=_status(handler.messages, "Trend paper"),
                kraken_status=_status(handler.messages, "Kraken EUR paper"),
                booked=after.days[len(before.days):],
                skipped=tuple(s.day for s in after.skipped[len(before.skipped):]),
                kraken_booked=kraken_after.days[len(kraken_before.days):],
                kraken_skipped=tuple(s.day for s in kraken_skipped(kraken_after)[len(kraken_skipped(kraken_before)):]),
                toasts=tuple(toasts),
                alert_lines=alerts_after.count(b"\n") - alerts_before.count(b"\n"),
                requests={"binance": len(main.calls), "binance_signals": len(signals.calls), "kraken": len(kraken.calls)},
                state_before=state_before,
                state_after=_digests(state_files),
            )
        )
    return results


def e2e_json(obj) -> bytes:
    """The golden JSON encoding: sorted keys, ASCII only, LF line ends."""
    return (json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def e2e_reader_payload(state_dir: Path) -> dict:
    from ui.trend_reader import TrendReader

    return TrendReader(state_dir).read(E2E_READER_NOW)


def e2e_outputs(state_dir: Path, results: list[E2EStartResult]) -> dict[str, bytes]:
    """Golden file name -> the bytes this run produced, in comparison order."""
    from radar_v08.adapters.trend_paper_store import LEDGER_DIR_NAME

    out = {golden: _bytes(Path(state_dir) / LEDGER_DIR_NAME / name) for name, golden in E2E_STATE_GOLDENS.items()}
    out[E2E_TOASTS_GOLDEN] = e2e_json([{"start": r.index, "title": t, "body": b} for r in results for t, b in r.toasts])
    out[E2E_READER_GOLDEN] = e2e_json(e2e_reader_payload(state_dir))
    out[E2E_STARTS_GOLDEN] = e2e_json([r.summary() for r in results])
    return out


def e2e_manifest(files: dict[str, bytes]) -> bytes:
    """MANIFEST.json for the scenario directory: every file's bytes and sha256 (itself excluded)."""
    import hashlib

    return e2e_json(
        {
            "format_version": 1,
            "purpose": (
                "Offline end-to-end scenario of the trend paper stack (PRODUCT-TREND-PAPER-E2E): "
                "tests/test_trend_paper_e2e.py and scripts/replay_trend_paper.py run the radar starts of "
                "tests/trend_paper_fakes.E2E_STARTS over these candles and compare the results with the goldens."
            ),
            "synthetic": (
                "Every candle in this directory is synthetic test data, not market history, generated by "
                "tests/trend_paper_fakes.e2e_candle_files. The Binance BTCUSDT/ETHUSDT extension days "
                f"{LAST_REAL_CLOSE + timedelta(days=1)}..{E2E_LAST_DAY} follow the vendored real history "
                f"../spot1d_*.json (used up to {LAST_REAL_CLOSE}); BTCEUR, ETHEUR and EURUSDT cover the paper "
                f"window {E2E_FIRST_DAY}..{E2E_LAST_DAY}; Kraken XBTEUR and ETHEUR start {E2E_KRAKEN_FROM}, "
                "the Kraken request lead days before the first paper day."
            ),
            "missing_on_purpose": {
                f"Binance {E2E_MISSING_EUR[0]}": E2E_MISSING_EUR[1].isoformat(),
                f"Kraken {E2E_MISSING_KRAKEN[0]}": E2E_MISSING_KRAKEN[1].isoformat(),
            },
            "rows": "[openTimeMs, open, high, low, close]",
            "goldens": (
                "golden_ledger.jsonl, golden_kraken_ledger.jsonl and golden_alerts.jsonl are the produced "
                "ledger.jsonl, kraken_ledger.jsonl and alerts.jsonl; golden_toasts.json the recorded toasts; "
                "golden_reader.json the TrendReader payload at the fixed reader moment; golden_starts.json "
                "the per-start summary. Regenerate with python -B scripts/replay_trend_paper.py --write-golden "
                "(the test never rewrites them)."
            ),
            "files": [
                {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in sorted(files.items())
            ],
        }
    )


def e2e_first_difference(directory: Path, produced: dict[str, bytes]) -> str | None:
    """The first produced file (in ``produced`` order) that differs from the file of that name in
    ``directory``, or ``None`` when all match."""
    for name, data in produced.items():
        path = Path(directory) / name
        if not path.is_file() or path.read_bytes() != data:
            return name
    return None
