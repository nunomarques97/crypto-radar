# Trend paper: operator guide

A short guide to the "Trend paper (research)" panel in the Game tab and the files behind it.

## What it is

* Paper books of the four registered trend rules: ENS, ENS_VT, btc_trend5 and btc_trend5_vt,
  each compared with buy-and-hold of the same coins.
* Every rule has a book in EUR and a book in USDT, at a 0.1% and a 0.4% fee per leg. Every book
  started with 7,000 (EUR or USDT) on 2026-10-04 (UTC). Earlier days are never booked.
* Prices come from Binance public daily candles. No account, no API key and no private data are
  used.
* One paper day is booked at each day's 00:00 UTC open. The decision uses closes up to the day
  before.
* Results are pre-tax: fees are charged on the traded amount, slippage is 0 and no tax is
  deducted.

## What it is not

* Paper only. No order is ever sent and nothing is bought or sold.
* NOT QUALIFIED. This is a research observation of rules that already exist, not a tested
  strategy and not a reason to trade.
* Not advice, and not a forecast.

## When it updates

* Each time the radar starts (START RADAR), it books the paper days missed since the last start.
  This runs in the background and never stops or slows the radar.
* "Catch up now" in the panel does the same at once. It only reads public market data and adds
  paper days; it places no order. It is off in TEST MODE.
* The PC does not have to be on every day: missed days are booked at the next start.

## How to read the panel

* The top line: how many paper days are booked, the first and last day, and when the last record
  was written.
* One table per currency, EUR first, then USDT. Each rule has two rows, one per fee (0.1% and
  0.4% per leg).
* Equity: what the book is worth after its last booked day.
* Return since 2026-10-04, and "vs buy-and-hold": the difference from holding the same coins, in
  percentage points. Green or red only follows the sign of the number. It says nothing about the
  future.
* Max drawdown: the largest fall from a previous high.
* Trades and fees: how often the book traded and what that cost.
* Exposure (target): how much of the book is in each coin after the last fill, and what the rule
  asked for.
* A dash ("—") means the value is not recorded. It never means zero.
* Skipped days: if a public candle is missing for good, a calm line under the top line lists each
  skipped day and why. Those days have no record and the books stay as they were.

## What each state means

* **Empty** ("No paper day yet ..."): nothing is booked yet. The line says when the first fill is,
  or, if days were skipped, how many.
* **Waiting for data** (after "Catch up now"): a paper day is due by the calendar but the public
  candles do not settle it yet, for example just after midnight UTC, or a candle is missing and
  not yet confirmed as missing for good. Nothing was written, not even the earlier days: a
  catch-up books every due day or none. Try again later; the next start also tries again. If it
  still says this after two days, ask for help: a coin pair may have stopped trading.
* **Market data failed** (after "Catch up now"): Binance could not be reached, answered too slowly,
  asked to slow down (rate limit), blocked the address for a while (ban), sent bad data, or the PC
  clock is behind. Nothing was written. Try again later; do not press it again and again. If it
  keeps failing, check that the PC clock and date are right.
* **Busy** or **Already running**: another catch-up is writing (for example the one at radar
  start). Nothing was written by this one. Wait a minute and press it again if needed.
* **"A catch-up is writing the trend paper ledger right now"**: the panel caught a write in
  progress. It shows again at the next refresh.
* **"Could not refresh"**: the panel kept the last reading it had. It tries again by itself.
* **Skipped days**: see above. A candle counts as missing for good only when a later candle of
  the same pair exists and a second, separate request agrees it is missing. A missing EUR pair
  price (BTCEUR or ETHEUR) then uses the USDT price divided by the EUR/USDT price instead (that
  day is booked, not skipped, and the source is recorded); if the EUR/USDT price is missing too,
  the day is skipped. A missing USDT candle is never filled in: the day is skipped. If an old BTCUSDT close is missing for good, btc_trend5 needs it every day, so every day
  after it is skipped too.
* **Ledger refused** (red pill): the ledger file is damaged or was changed (for example a cut-off
  last line or a line that no longer matches its check code). Nothing is shown from it, nothing is
  added to it, and it is never repaired or rewritten automatically. Follow the steps below.

## If it says "Ledger refused"

Do not edit the file. Do these steps one by one:

1. In the radar window, press STOP RADAR and wait until it shows the radar as stopped.
2. Open the folder `trend_paper` in the radar's state folder (the radar folder, the one that
   holds `radar.py`, unless `RADAR_STATE_DIR` is set).
3. Rename `ledger.jsonl` to a dated backup in the same folder, for example
   `ledger.refused-2026-10-20.jsonl` (use today's date). Do not open, change or delete it.
4. Press START RADAR, or press "Catch up now" in the Trend paper panel. The ledger is rebuilt from
   2026-10-04 using the public candles.
5. Check that the panel shows books again, with the days up to today.
6. Keep the backup file. It is the evidence of what went wrong; share it when asking for help.

Leave the other files in that folder (`ledger.jsonl.lock`, `alerts.jsonl`) where they are.
Because `alerts.jsonl` stays, the rebuilt days do not show their alerts again.

## Where the files are

* Ledger: `trend_paper/ledger.jsonl` in the radar's state folder (the radar folder unless
  `RADAR_STATE_DIR` is set). One line per book and day, plus one line per skipped day, each
  chained to the one before it.
* Alerts already shown: `trend_paper/alerts.jsonl` in the same folder.
* A text report: `python scripts/run_trend_paper.py report --offline` (reads only, no network).

## Kraken EUR books (text report only)

A second set of paper books that fill at Kraken's EUR prices instead of Binance's. They are not
in the panel; read them with the text report below.

* 12 books: the same six rules (ENS, ENS_VT, BH_5050, BTC_TREND5, BTC_TREND5_VT, BH_BTC) at two
  fees per leg: 0.4% (maker assumption) and 0.8% (taker sensitivity). Each started with 7,000 EUR
  on 2026-10-04 (UTC). The comparator of each book is buy-and-hold at Kraken with the same fee.
* Same signals as the Binance books: the decision still uses the Binance USDT closes up to the day
  before, unchanged. Only the fill price differs.
* Fills: the Kraken public XBTEUR and ETHEUR daily open at 00:00 UTC. There is no substitute
  price: if a Kraken open is missing, that day is not booked with a Binance or EUR/USDT price.
* The fees are the Kraken tier reported for the account (0.4% maker, 0.8% taker), and that Kraken
  is the MiCA-licensed EUR venue is also as reported. Neither was checked here.
* Prices come from Kraken's public daily candles (no account, no API key, no private data). Paper
  only, no order is ever sent, pre-tax and NOT QUALIFIED, like the Binance books.
* The report also lists, per day and coin, the Kraken open, the Binance EUR book's fill price from
  `ledger.jsonl` (with its source, for example the BTCEUR open), and the difference (Kraken minus
  Binance) in EUR and in basis points. A cell shows n/a when either side is missing, skipped, not
  booked yet, or when `ledger.jsonl` cannot be read. That file is only read, never changed.

### When they update

* At each radar start, after the Binance books and their alerts, in the same background thread.
  It never stops the radar, never shows an alert, and any failure is only written to the log.
  It is off when `RADAR_TREND_PAPER_ENABLED` is `0`, like the Binance books.
* By hand: `python scripts/run_trend_paper_kraken.py catch-up` books the missed days;
  `run` books them and then prints the report; `report` only prints the report (no network).
  `--state-dir DIR` uses another state folder.
* Exit codes: 0 done; 2 wrong command; 3 busy (another catch-up is writing the Kraken ledger;
  nothing was written); 4 market data failed or waiting for data (nothing was written; the next
  start retries); 5 Kraken ledger refused (nothing was written).

### What each outcome means

* **Waiting for data** (exit 4): a day is due but the public candles do not settle it yet, or a
  candle is missing and not yet confirmed as missing for good. Nothing was written, not even the
  earlier days. Kraken returns at most its latest 720 daily candles; a day older than the oldest
  candle Kraken returns is unknown, so the catch-up waits rather than skip it.
* **Market data failed** (exit 4): Binance or Kraken could not be reached, asked to slow down,
  sent bad data, or the PC clock is behind. Nothing was written. Try again later.
* **Skipped day**: a Kraken open of the day (`NO_KRAKEN_OPEN`) or a USDT close the signal needs
  (`NO_SIGNAL_CLOSE`) is missing for good: a later candle of the same pair exists and a second,
  separate request agrees. The day gets one skip line in the ledger, no book changes, and the
  report lists it with the reason.
* **Ledger refused** (exit 5): `kraken_ledger.jsonl` is damaged or was changed. Nothing is read
  from it or added to it, and it is never repaired or rewritten. Follow the steps in "If it says
  Ledger refused" above with `kraken_ledger.jsonl` instead of `ledger.jsonl`, for example renamed
  to `kraken_ledger.refused-2026-10-20.jsonl`; then run
  `python scripts/run_trend_paper_kraken.py run` (or start the radar) to rebuild it from
  2026-10-04. The Binance `ledger.jsonl` is not affected.

### Where the files are

* Kraken ledger: `trend_paper/kraken_ledger.jsonl` in the radar's state folder, next to
  `ledger.jsonl`, with its own lock file `kraken_ledger.jsonl.lock`. One line per book and day,
  plus one line per skipped day, each chained to the one before it, from 2026-10-04.
* A text report: `python scripts/run_trend_paper_kraken.py report` (reads only, no network).
