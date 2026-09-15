"""Runtime dispatch for a validated `python radar.py --mode MODE` invocation."""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone

from . import alerts, claude_bridge, config, mock_alert, notifications, ntfy
from .heartbeat import run_and_write
from .shadow import run_shadow
from .store import SnapshotStore
from .terminal import render_terminal, safe_print

logger = logging.getLogger("radar_v08.cli")

_STARTED_AT = time.time()

V08_SUPPORTED_MODES = (
    "heartbeat",
    "full",
    "bridge",
    "shadow",
    "loop",
    "notify-test",
    "mock-alert",
    "alerts",
    "prompt",
)


def _run_bridge_and_render(output: dict, store: SnapshotStore) -> None:
    """Shared by `--mode full` and `--mode loop`: drains actionable events,
    fires Windows + ntfy mobile notifications for anything that completes,
    retries any previously FAILED mobile push, then prints the terminal panel
    with real Claude Bridge health and queue counts.
    """
    result = claude_bridge.run_bridge_cycle(
        store, notify_fn=lambda event: notifications.notify_for_event(event, store=store)
    )
    # A contained bridge cycle must not turn a harmless queue drain into a
    # legacy delivery side effect.  Historical notification retry behavior is
    # otherwise untouched.
    if result.skipped_reason != "LOCAL_ONLY_POLICY":
        notifications.retry_pending_ntfy(store)
    counts = store.event_status_counts()
    last_event = store.latest_event()
    safe_print(render_terminal(
        output,
        uptime_seconds=time.time() - _STARTED_AT,
        bridge_health=result.health,
        event_queue_counts=counts,
        last_event=dict(last_event) if last_event is not None else None,
        mobile_notifications_enabled=notifications.mobile_notifications_enabled(),
    ))
    for entry in result.processed:
        logger.info(
            "claude_bridge event=%s asset=%s model=%s outcome=%s status=%s",
            entry.get("event_id"), entry.get("asset"), entry.get("model"),
            entry.get("outcome"), entry.get("status"),
        )


def run_mode(mode: str, argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    if mode == "heartbeat":
        output = run_and_write(mode="HEARTBEAT")
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return 0

    if mode == "full":
        output = run_and_write(mode="FULL", full=True)
        print(json.dumps(output, indent=2, ensure_ascii=False))
        print()
        store = SnapshotStore(config.SQLITE_PATH)
        try:
            _run_bridge_and_render(output, store)
        finally:
            store.close()
        return 0

    if mode == "bridge":
        # Drains the existing event queue without running a radar cycle -
        # useful for recovery/testing, and for a tight bridge-only poll
        # interleaved between full cycles in `--mode loop`.
        store = SnapshotStore(config.SQLITE_PATH)
        try:
            result = claude_bridge.run_bridge_cycle(
                store, notify_fn=lambda event: notifications.notify_for_event(event, store=store)
            )
            if result.skipped_reason != "LOCAL_ONLY_POLICY":
                notifications.retry_pending_ntfy(store)
            print(json.dumps({"health": result.health, "processed": result.processed, "recovered_stale": result.recovered_stale}, indent=2, ensure_ascii=False, default=str))
        finally:
            store.close()
        return 0

    if mode == "shadow":
        result = run_shadow()
        print(json.dumps(result["comparison"], indent=2, ensure_ascii=False))
        return 0

    if mode == "loop":
        return _run_loop()

    if mode == "notify-test":
        return _run_notify_test()

    if mode == "mock-alert":
        return _run_mock_alert()

    if mode == "alerts":
        return _run_alerts()

    if mode == "prompt":
        return _run_prompt_recovery(argv)

    print(
        f"Unknown --mode '{mode}'. Expected one of: {', '.join(V08_SUPPORTED_MODES)}.",
        file=sys.stderr,
    )
    return 2


def _run_notify_test() -> int:
    """`python radar.py --mode notify-test`: verifies ntfy configuration and
    sends ONE synthetic push, entirely outside the event pipeline - no Kraken
    call, no Qwen call, no Claude call, no SQLite event, no trade. The
    message is clearly marked so it can never be confused with a real alert.
    """
    enabled = notifications.mobile_notifications_enabled()
    print(f"Mobile notifications: {'ENABLED' if enabled else 'DISABLED'}")
    if not enabled:
        print("CRYPTO_RADAR_NTFY_TOPIC is not set - nothing to send.")
        return 0

    title = "CRYPTO RADAR TEST"
    message = "Synthetic test notification - ntfy check only. No Kraken, no Qwen, no Claude, no trade."
    result = ntfy.send_ntfy_notification(title, message, priority="default")
    print(f"ntfy test -> {result}")
    return 0 if result == ntfy.RESULT_SUCCESS else 1


def _run_mock_alert() -> int:
    """`python radar.py --mode mock-alert`: a fully synthetic, end-to-end dry
    run of MOCK EVENT -> EVENT QUEUE -> WINDOWS NOTIFICATION -> NTFY -> PROMPT
    BUILDER -> CLIPBOARD -> POPUP. Never touches Kraken, Qwen, or the Claude
    Bridge - see mock_alert.py. Exit code reflects the two checks fully under
    this machine's control (Windows toast + clipboard copy); ntfy is exit-
    code-neutral here since RESULT_DISABLED (no CRYPTO_RADAR_NTFY_TOPIC) is a
    valid, non-error configuration, exactly like `--mode notify-test`.
    """
    store = SnapshotStore(config.SQLITE_PATH)
    try:
        outcome = mock_alert.run_mock_alert(store)
    finally:
        store.close()

    notify_result = outcome["notify_result"]
    return 0 if notify_result["windows_sent"] and notify_result["prompt_copied"] else 1


def _parse_event_arg(argv: list[str]) -> str | None:
    if "--event" in argv:
        idx = argv.index("--event")
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return None


def _run_alerts() -> int:
    """`python radar.py --mode alerts` (task 'HISTÓRICO DE ALERTAS'): lists
    recoverable alerts, most recent first, then - only when actually
    interactive (never in a script/test) - offers to copy one alert's prompt
    by number, reusing `alerts.recover_prompt` (task section 5: no separate
    prompt/clipboard/popup path for the interactive picker either).
    """
    store = SnapshotStore(config.SQLITE_PATH)
    try:
        rows = alerts.list_alerts(store)
        safe_print(alerts.format_alert_history(rows))
        if rows and sys.stdin.isatty():
            safe_print("")
            safe_print("Introduzir o numero do alerta (Enter para sair):")
            try:
                choice = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                choice = ""
            if choice:
                try:
                    index = int(choice)
                except ValueError:
                    index = -1
                if 1 <= index <= len(rows):
                    alerts.recover_prompt(store, rows[index - 1]["event_id"])
    finally:
        store.close()
    return 0


def _run_prompt_recovery(argv: list[str]) -> int:
    """`python radar.py --mode prompt --event <EVENT_ID>` (task 'RECUPERAR
    PROMPT'): rebuilds and re-copies an already-persisted alert's prompt.
    """
    event_id = _parse_event_arg(argv)
    if not event_id:
        print("Usage: python radar.py --mode prompt --event <EVENT_ID>", file=sys.stderr)
        return 2

    store = SnapshotStore(config.SQLITE_PATH)
    try:
        found = alerts.recover_prompt(store, event_id)
    finally:
        store.close()
    return 0 if found else 1


def _run_loop() -> int:
    """Continuous operation (task section 16): a light heartbeat cadence, a
    full cycle (which creates events) on a slower cadence, and the Claude
    Bridge drained every cycle so the queue never sits idle for long. Every
    interval is config-driven and none of them is aggressive polling.
    """
    logger.info(
        "Starting loop mode: heartbeat every %.0fs, full cycle every %.0fs",
        config.LOOP_HEARTBEAT_INTERVAL_SECONDS, config.LOOP_FULL_INTERVAL_SECONDS,
    )
    last_full = 0.0
    try:
        while True:
            now_monotonic = time.monotonic()
            due_for_full = (now_monotonic - last_full) >= config.LOOP_FULL_INTERVAL_SECONDS

            if due_for_full:
                output = run_and_write(mode="FULL", full=True)
                last_full = now_monotonic
            else:
                output = run_and_write(mode="HEARTBEAT", full=False)

            store = SnapshotStore(config.SQLITE_PATH)
            try:
                _run_bridge_and_render(output, store)
            finally:
                store.close()

            time.sleep(config.LOOP_HEARTBEAT_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Loop mode stopped by user")
        return 0
