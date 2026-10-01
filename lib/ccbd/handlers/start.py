from __future__ import annotations

import time

from ccbd.startup_identity import resolve_startup_run_id
from runtime_observability import StartupReadinessRecorder


def build_start_handler(app):
    def handle(payload: dict) -> dict:
        rpc_accepted_ns = time.perf_counter_ns()
        requested = tuple(
            str(item).strip()
            for item in (payload.get('agent_names') or ())
            if str(item).strip()
        )
        terminal_size = _terminal_size_from_payload(payload)
        restore = _bool_payload(payload, 'restore', default=True)
        auto_permission = _bool_payload(payload, 'auto_permission', default=True)
        startup_run_id = resolve_startup_run_id(payload.get('startup_run_id'))
        daemon_started = _optional_bool_payload(payload, 'daemon_started')
        readiness_recorder = StartupReadinessRecorder.from_rpc_payload(
            payload.get('readiness_trace'),
            now_ns=rpc_accepted_ns,
            trusted_keeper_checkpoint=_trusted_keeper_startup_checkpoint(app),
        )
        if readiness_recorder is not None:
            readiness_recorder.mark(
                'rpc_accepted',
                source='ccbd_start_handler',
                now_ns=rpc_accepted_ns,
            )
        with app.start_maintenance_lock:
            # Persist before the start flow: if the start is interrupted or
            # fails, the requested authority still survives as the recovery
            # policy instead of leaving the previous policy (or none) in place.
            app.persist_start_policy(
                auto_permission=auto_permission,
                recovery_restore=restore,
                source='start_command',
            )
            summary = app.runtime_supervisor.start(
                agent_names=requested,
                restore=restore,
                auto_permission=auto_permission,
                terminal_size=terminal_size,
                startup_run_id=startup_run_id,
                daemon_started=daemon_started,
                readiness_recorder=readiness_recorder,
            )
        response = summary.to_record()
        response['startup_run_id'] = startup_run_id
        return response

    return handle


def _trusted_keeper_startup_checkpoint(app):
    checkpoint = getattr(app, 'keeper_startup_checkpoint', None)
    fence = getattr(app, 'expected_startup_fence', None)
    lease = getattr(app, 'lease', None)
    if checkpoint is None or fence is None or lease is None:
        return None
    try:
        if (
            str(checkpoint.startup_id) != str(fence.startup_id)
            or int(checkpoint.generation) != int(fence.generation)
            or int(lease.generation) != int(fence.generation)
            or int(lease.ccbd_pid) != int(app.pid)
            or str(lease.daemon_instance_id or '') != str(app.daemon_instance_id or '')
        ):
            return None
    except (AttributeError, TypeError, ValueError):
        return None
    return checkpoint


def _terminal_size_from_payload(payload: dict) -> tuple[int, int] | None:
    try:
        width = int(payload.get('terminal_width') or 0)
        height = int(payload.get('terminal_height') or 0)
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _bool_payload(payload: dict, key: str, *, default: bool) -> bool:
    if key not in payload:
        return bool(default)
    return bool(payload.get(key))


def _optional_bool_payload(payload: dict, key: str) -> bool | None:
    if key not in payload or payload.get(key) is None:
        return None
    return bool(payload.get(key))
