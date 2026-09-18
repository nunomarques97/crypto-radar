"""Public exchange adapters for crypto-radar critical code (T022b).

Wraps `radar_v08.kraken_spot` / `radar_v08.kraken_futures` fetch functions
with an injected receipt clock, the source time Kraken's payload actually
supplies (never fabricated), and the venue/instrument identity shaped for
`radar_v08.domain.integrity`'s inputs. Not wired to any consumer yet (T023b).
"""
