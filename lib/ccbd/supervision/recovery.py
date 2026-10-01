from __future__ import annotations

from ccbd.services.runtime_recovery_policy import (
    RUNTIME_RECOVERY_PROBING_HEALTH,
    herdr_auto_restore_mode,
    normalized_runtime_health,
    recovery_circuit_threshold,
    should_record_recovery_capability_block,
)
from ccbd.system import parse_utc_timestamp
from provider_runtime.pane_recovery_outcome import LIVE_OWNED

from .loop_runtime import runtime_active_pane_id
from .recovery_context import RecoveryContext, build_recovery_context
from .recovery_transitions import (
    MAX_CONSECUTIVE_RECOVERY_ATTEMPTS,
    RECOVERY_STABILITY_WINDOW_S,
    SUCCESS_RUNTIME_HEALTHS,
    attempt_recovery_action,
    mark_recovery_blocked,
    mark_recovery_circuit_open,
    mark_recovery_failed,
    mark_recovery_missing,
    mark_recovery_probing,
    mark_recovery_succeeded,
    start_recovery,
)


def recover_runtime(
    *,
    project_id: str,
    agent_name: str,
    runtime,
    registry,
    runtime_service,
    remount_project_fn,
    clock,
    event_store,
    align_runtime_authority_fn,
    upsert_if_changed_fn,
    is_in_backoff_window_fn,
    should_reflow_project_namespace_fn,
) -> str:
    ctx = build_recovery_context(
        project_id=project_id,
        agent_name=agent_name,
        runtime=runtime,
        registry=registry,
        runtime_service=runtime_service,
        remount_project_fn=remount_project_fn,
        clock=clock,
        event_store=event_store,
        align_runtime_authority_fn=align_runtime_authority_fn,
        upsert_if_changed_fn=upsert_if_changed_fn,
        is_in_backoff_window_fn=is_in_backoff_window_fn,
        should_reflow_project_namespace_fn=should_reflow_project_namespace_fn,
    )
    attempted_at = ctx.clock()
    prior_health = normalized_runtime_health(ctx.runtime) or ctx.runtime.health
    if should_record_recovery_capability_block(ctx.runtime):
        mode = herdr_auto_restore_mode(ctx.runtime)
        return mark_recovery_blocked(
            ctx,
            runtime=ctx.runtime,
            occurred_at=attempted_at,
            prior_health=prior_health,
            reason=f'herdr-auto-restore-{mode}-not-recovery-capable',
        )
    if _recovery_probe_active(ctx.runtime):
        if _recovery_probe_stable(ctx.runtime, now=attempted_at):
            return mark_recovery_succeeded(
                ctx,
                refreshed=ctx.runtime,
                attempted_at=attempted_at,
                restart_count=ctx.runtime.restart_count,
                prior_health=prior_health,
                next_health='healthy',
            )
        if normalized_runtime_health(ctx.runtime) in {
            RUNTIME_RECOVERY_PROBING_HEALTH,
            *SUCCESS_RUNTIME_HEALTHS,
        }:
            return ctx.runtime.health
    recovery_failure_count = int(getattr(ctx.runtime, 'recovery_failure_count', 0) or 0)
    if recovery_failure_count >= recovery_circuit_threshold(
        ctx.runtime,
        default=MAX_CONSECUTIVE_RECOVERY_ATTEMPTS,
    ):
        return mark_recovery_circuit_open(
            ctx,
            runtime=ctx.runtime,
            occurred_at=attempted_at,
            restart_count=ctx.runtime.restart_count,
            recovery_failure_count=recovery_failure_count,
            prior_health=prior_health,
            reason=ctx.runtime.last_failure_reason or prior_health or 'recover-failed',
        )
    if ctx.is_in_backoff_window_fn(ctx.runtime, now=attempted_at):
        return ctx.runtime.health
    recovering = start_recovery(
        ctx,
        attempted_at=attempted_at,
        prior_health=prior_health,
    )
    restart_count = recovering.restart_count + 1
    recovery_failure_count = int(getattr(recovering, 'recovery_failure_count', 0) or 0) + 1

    try:
        refreshed, failure_reason, pane_outcome = attempt_recovery_action(ctx, recovering=recovering)
    except Exception as exc:
        refreshed = ctx.registry.get(ctx.agent_name) or recovering
        failure_reason = f'{type(exc).__name__}: {exc}'
        pane_outcome = None

    if refreshed is None:
        return mark_recovery_missing(
            ctx,
            recovering=recovering,
            attempted_at=attempted_at,
            restart_count=restart_count,
            recovery_failure_count=recovery_failure_count,
            prior_health=prior_health,
        )

    refreshed = ctx.align_runtime_authority_fn(refreshed)
    next_health = normalized_runtime_health(refreshed) or refreshed.health
    if next_health in SUCCESS_RUNTIME_HEALTHS:
        if _pane_already_live_owned(ctx, refreshed=refreshed, pane_outcome=pane_outcome):
            # Local patch (ccb-team-kit): the alarm was raised on a live pane that ensure_pane found
            # alive and owned - nothing was respawned or replaced, so there is nothing to stabilise.
            # End it here, keeping the restart count, instead of holding the agent's mailbox through
            # the 90 s window and counting the false alarm toward the circuit.
            return mark_recovery_succeeded(
                ctx,
                refreshed=refreshed,
                attempted_at=attempted_at,
                restart_count=ctx.runtime.restart_count,
                prior_health=prior_health,
                next_health='healthy',
                pane_outcome=pane_outcome,
            )
        return mark_recovery_probing(
            ctx,
            refreshed=refreshed,
            attempted_at=attempted_at,
            restart_count=restart_count,
            recovery_failure_count=recovery_failure_count,
            prior_health=prior_health,
            failure_reason=failure_reason,
        )

    return mark_recovery_failed(
        ctx,
        refreshed=refreshed,
        attempted_at=attempted_at,
        restart_count=restart_count,
        recovery_failure_count=recovery_failure_count,
        prior_health=prior_health,
        next_health=next_health,
        failure_reason=failure_reason,
    )


def _pane_already_live_owned(ctx: RecoveryContext, *, refreshed, pane_outcome: str | None) -> bool:
    """True when this recovery found the very pane the runtime already had, alive and owned.

    Gated on what ``ensure_pane`` reported, never on the pane id alone: an in-place respawn keeps
    the pane id too, and a runtime whose session reported nothing keeps the probing path."""
    if pane_outcome != LIVE_OWNED:
        return False
    previous_pane_id = runtime_active_pane_id(ctx.runtime)
    current_pane_id = runtime_active_pane_id(refreshed)
    if previous_pane_id is None or current_pane_id is None:
        return False
    return previous_pane_id == current_pane_id


def _recovery_probe_active(runtime) -> bool:
    return str(getattr(runtime, 'reconcile_state', '') or '').strip() == 'probing'


def _recovery_probe_stable(runtime, *, now: str) -> bool:
    if not _recovery_probe_active(runtime):
        return False
    if normalized_runtime_health(runtime) not in {
        RUNTIME_RECOVERY_PROBING_HEALTH,
        *SUCCESS_RUNTIME_HEALTHS,
    }:
        return False
    attempted_at = str(getattr(runtime, 'last_reconcile_at', '') or '').strip()
    observed_at = str(getattr(runtime, 'last_seen_at', '') or '').strip()
    if not attempted_at or not observed_at:
        return False
    try:
        attempted = parse_utc_timestamp(attempted_at)
        observed = parse_utc_timestamp(observed_at)
        checked = parse_utc_timestamp(now)
    except Exception:
        return False
    return (
        observed > attempted
        and checked >= attempted
        and (checked - attempted).total_seconds() >= RECOVERY_STABILITY_WINDOW_S
    )
