from __future__ import annotations

from typing import Callable

from provider_core.tmux_ownership import (
    apply_session_tmux_identity,
    inspect_tmux_pane_ownership,
    ownership_error_text,
)
from provider_runtime.pane_recovery_outcome import LIVE_OWNED, publish_pane_recovery_outcome
from provider_runtime.session_payload import session_uses_tmux_compatible_pane
from terminal_runtime.mux_backend_contract import MuxCommandErrorV2

from .lifecycle_common import (
    attach_pane_log,
    backend_is_alive,
    lifecycle_backend_error_text,
    live_owned_pane,
    pane_lifecycle_target,
)
from .lifecycle_recovery import (
    can_reclaim_project_slot_pane,
    clear_recovery_block,
    tmux_rebound_pane,
)


def ensure_pane(
    session,
    *,
    now_str_fn: Callable[[], str],
    attach_pane_log_fn: Callable[[object, object, object], None] = attach_pane_log,
) -> tuple[bool, str]:
    # Local patch (ccb-team-kit): publish the action this call takes, so a recovery that finds the
    # pane alive and owned can be recognised as the false alarm it is. Cleared first: a session
    # reused across calls must never report the previous call's action.
    publish_pane_recovery_outcome(session, None)
    backend = session.backend()
    if not backend:
        return False, 'Terminal backend not available'

    pane_id = session.pane_id
    tmux_compatible = session_uses_tmux_compatible_pane(_session_data(session))
    try:
        live_pane = live_owned_pane(session, backend, pane_id)
    except MuxCommandErrorV2 as exc:
        return False, lifecycle_backend_error_text(exc)
    if live_pane is not None:
        clear_recovery_block(session)
        if tmux_compatible:
            apply_session_tmux_identity(session, backend, live_pane)
        attach_pane_log_fn(session, backend, live_pane)
        publish_pane_recovery_outcome(session, LIVE_OWNED)
        return True, live_pane

    if tmux_compatible and session.terminal == 'tmux':
        rebound = tmux_rebound_pane(
            session,
            backend,
            pane_id,
            now_str_fn=now_str_fn,
            attach_pane_log_fn=attach_pane_log_fn,
        )
        if rebound is not None:
            return rebound

    try:
        pane_alive = bool(pane_id and backend_is_alive(backend, pane_lifecycle_target(session, pane_id)))
    except MuxCommandErrorV2 as exc:
        return False, lifecycle_backend_error_text(exc)
    if pane_alive and tmux_compatible:
        ownership = inspect_tmux_pane_ownership(session, backend, str(pane_id))
        if can_reclaim_project_slot_pane(session, backend, str(pane_id)):
            return False, f'Pane not rebound: {pane_id}'
        return False, ownership_error_text(ownership, pane_id=str(pane_id))
    if pane_alive:
        return False, f'Pane is alive but not owned by CCB: {pane_id}'

    return False, f'Pane not alive: {pane_id}'


def _session_data(session) -> dict[str, object]:
    data = getattr(session, 'data', None)
    return data if isinstance(data, dict) else {}


__all__ = ['attach_pane_log', 'ensure_pane']
