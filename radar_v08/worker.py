"""Local inference worker entry point: ``python -m radar_v08.worker``.

Nothing starts automatically. This module is not imported by the radar loop, the CLI
or the UI, and running it starts no worker, opens no database and makes no network
request:

* with no arguments it prints what it is and exits 0;
* ``--check`` validates a profile and a loopback Ollama endpoint offline and prints
  the resolved configuration (exit 0) or the refusal code (exit 2).

There is no run mode yet: the worker's durable admission source (the collector's
transitions/outbox in SQLite) is not wired yet. Until then the worker exists as tested
code (``radar_v08.workflow.worker``) and this wiring: ``SqliteInvocationLedger`` adapts the
invocation store functions to the worker's ``InvocationLedger`` port.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence

from .adapters import invocation_store
from .adapters.local_inference import LocalEndpointRefused, loopback_base_url
from .domain.invocation import (
    Claimed,
    Duplicate,
    InvocationIdentity,
    InvocationRequest,
    Lease,
    ModelBudget,
    Refused,
    ReleaseReason,
    TransitionStatus,
)
from .workflow.scheduler import Role, SchedulerPolicy
from .workflow.worker import InferenceProfile, WorkerClock, WorkerConfigError

RECOVERY_LEASE_SECONDS = 60
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


class SqliteInvocationLedger:
    """``InvocationLedger`` over the invocation store: every call is one short transaction."""

    def __init__(self, conn: sqlite3.Connection, budget: ModelBudget, clock: WorkerClock, owner: str) -> None:
        self._conn = conn
        self._budget = budget
        self._clock = clock
        self._owner = owner

    def claim(self, identity: InvocationIdentity, model: str, lease_seconds: int) -> Claimed | Duplicate | Refused:
        return invocation_store.claim_invocation(
            self._conn,
            InvocationRequest(identity, model),
            self._budget,
            owner=self._owner,
            now=self._clock.current(),
            lease_seconds=lease_seconds,
        )

    def record_attempt(self, lease: Lease) -> TransitionStatus:
        return invocation_store.record_attempt(self._conn, lease, self._budget, now=self._clock.current()).status

    def complete(self, lease: Lease) -> TransitionStatus:
        return invocation_store.complete_invocation(self._conn, lease, now=self._clock.current()).status

    def release(self, lease: Lease, reason: ReleaseReason) -> TransitionStatus:
        return invocation_store.release_invocation(self._conn, lease, reason, now=self._clock.current()).status

    def recover_expired(self) -> tuple[Lease, ...]:
        return invocation_store.recover_expired(
            self._conn, owner=self._owner, now=self._clock.current(), lease_seconds=RECOVERY_LEASE_SECONDS
        )


_ABOUT = (
    "radar_v08.worker: local inference worker. Nothing was started: no worker, "
    "no database, no network. There is no run mode until a durable "
    "admission source is wired. Use --check to validate a profile and a loopback endpoint offline."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m radar_v08.worker", description=_ABOUT)
    parser.add_argument("--check", action="store_true", help="validate the profile and endpoint offline, then exit")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="loopback Ollama URL with explicit port")
    parser.add_argument("--profile-id", default=None, help="versioned profile identifier")
    parser.add_argument("--model", default=None, help="model name of the profile (required with --check)")
    parser.add_argument("--role", default=Role.SCREENER.value, choices=[role.value for role in Role])
    parser.add_argument("--hard-timeout", type=int, default=30, help="hard call limit in seconds")
    parser.add_argument("--context-tokens", type=int, default=4096)
    parser.add_argument("--output-cap", type=int, default=768)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.check:
        print(_ABOUT)
        return 0
    try:
        endpoint = loopback_base_url(args.ollama_url)
    except LocalEndpointRefused as refusal:
        print(f"refused: endpoint {refusal.code.value}", file=sys.stderr)
        return 2
    try:
        profile = InferenceProfile(
            profile_id=args.profile_id,
            model=args.model,
            role=Role(args.role),
            hard_timeout_seconds=args.hard_timeout,
            context_tokens=args.context_tokens,
            output_cap_tokens=args.output_cap,
        )
    except WorkerConfigError:
        print("refused: profile invalid or disabled in OC-1 production", file=sys.stderr)
        return 2
    limits = SchedulerPolicy().role_profiles[profile.role]
    if profile.hard_timeout_seconds > limits.hard_timeout_seconds or profile.output_cap_tokens > limits.output_cap_tokens:
        print("refused: profile exceeds the OC-1 role limits", file=sys.stderr)
        return 2
    print(
        f"ok: endpoint={endpoint} profile={profile.profile_id} model={profile.model} role={profile.role.value} "
        f"hard_timeout={profile.hard_timeout_seconds}s context={profile.context_tokens} output_cap={profile.output_cap_tokens} "
        "(offline check; nothing started)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
