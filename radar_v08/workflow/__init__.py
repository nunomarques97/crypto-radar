"""Pure workflow policy for crypto-radar critical code.

This package holds deterministic policy and state reducers (T032a: OC-1 admission,
queue and deadlines). It performs no I/O, reads no wall clock and imports no adapter
or UI module; time and every other effect come in through injected ports
(docs/FAILURE_AND_QUALITY.md, Architectural quality contract).
"""
