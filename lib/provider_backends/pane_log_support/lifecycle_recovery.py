from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from provider_core.tmux_ownership import (
    inspect_tmux_pane_ownership,
    ownership_error_text,
    session_slot_user_option_lookup,
)

from .lifecycle_common import (
    activate_rebound_pane,
    bind_session_to_pane,
    pane_exists,
    pane_lifecycle_target,
    persist_crash_log,
)
from provider_runtime.pane_recovery_outcome import (
    REPLACEMENT,
    RESPAWN,
    publish_pane_recovery_outcome,
)
from provider_runtime.session_payload import session_uses_tmux_compatible_pane


def tmux_rebound_pane(
    session,
    backend: object,
    pane_id: str,
    *,
    now_str_fn: Callable[[], str],
    attach_pane_log_fn: Callable[[object, object, str], None],
) -> tuple[bool, str] | None:
    start_cmd = session.start_cmd
    respawn = getattr(backend, 'respawn_pane', None)
    create_pane = getattr(backend, 'create_pane', None)
    if not start_cmd or (not callable(respawn) and not callable(create_pane)):
        return None
    if not session_uses_tmux_compatible_pane(_session_data(session)):
        return _backend_neutral_rebound_pane(
            session,
            backend,
            pane_id,
            start_cmd=start_cmd,
            respawn=respawn,
            now_str_fn=now_str_fn,
            attach_pane_log_fn=attach_pane_log_fn,
        )

    outcome = _respawn_existing_pane(
        session,
        backend,
        pane_id,
        start_cmd=start_cmd,
        respawn=respawn,
        now_str_fn=now_str_fn,
        attach_pane_log_fn=attach_pane_log_fn,
    )
    if outcome.error is None:
        return True, str(pane_id)
    if not outcome.allow_replacement:
        return False, outcome.error

    created = create_replacement_pane(
        session,
        backend,
        start_cmd=start_cmd,
        create_pane=create_pane,
        now_str_fn=now_str_fn,
        attach_pane_log_fn=attach_pane_log_fn,
    )
    if created is not None:
        return True, created
    return False, f'Pane not alive and respawn failed: {outcome.error}'


def _backend_neutral_rebound_pane(
    session,
    backend: object,
    pane_id: str,
    *,
    start_cmd: str,
    respawn,
    now_str_fn: Callable[[], str],
    attach_pane_log_fn: Callable[[object, object, str], None],
) -> tuple[bool, str] | None:
    if not callable(respawn) or not pane_id:
        return _RespawnOutcome('respawn unavailable').as_tuple()
    target = pane_lifecycle_target(session, pane_id)
    if not target or not pane_exists(backend, target):
        return _RespawnOutcome('pane target no longer exists').as_tuple()
    blocked_detail = _stored_recovery_block(session, str(pane_id))
    if blocked_detail is not None:
        return _RespawnOutcome(blocked_detail, allow_replacement=False).as_tuple()
    try:
        reason = persist_crash_log(session, backend, target)
        recovery = _prepare_crash_recovery(session, reason)
        if recovery is not None and not recovery[0]:
            _record_recovery_block(
                session,
                pane_id=str(pane_id),
                reason=str(reason or 'provider_recovery_blocked'),
                detail=recovery[1],
                blocked_at=now_str_fn(),
            )
            return False, recovery[1]
        prepared_start_cmd = str(getattr(session, 'start_cmd', '') or '').strip()
        if prepared_start_cmd:
            start_cmd = prepared_start_cmd
        respawn(target, cmd=start_cmd, cwd=session.work_dir, remain_on_exit=True)
        if not backend.is_alive(target):
            return False, 'respawn did not revive pane'
        bind_session_to_pane(session, str(pane_id), now_str_fn=now_str_fn)
        attach_pane_log_fn(session, backend, str(pane_id))
        clear_recovery_block(session)
        publish_pane_recovery_outcome(session, RESPAWN)
        return True, str(pane_id)
    except Exception as exc:
        return False, f'{exc}'


def respawn_existing_pane(
    session,
    backend: object,
    pane_id: str,
    *,
    start_cmd: str,
    respawn,
    now_str_fn: Callable[[], str],
    attach_pane_log_fn: Callable[[object, object, str], None],
) -> str | None:
    return _respawn_existing_pane(
        session,
        backend,
        pane_id,
        start_cmd=start_cmd,
        respawn=respawn,
        now_str_fn=now_str_fn,
        attach_pane_log_fn=attach_pane_log_fn,
    ).error


def _respawn_existing_pane(
    session,
    backend: object,
    pane_id: str,
    *,
    start_cmd: str,
    respawn,
    now_str_fn: Callable[[], str],
    attach_pane_log_fn: Callable[[object, object, str], None],
) -> '_RespawnOutcome':
    if not callable(respawn) or not pane_id or not str(pane_id).startswith('%'):
        return _RespawnOutcome('respawn unavailable')
    if not pane_exists(backend, str(pane_id)):
        return _RespawnOutcome('pane target no longer exists')
    ownership = inspect_tmux_pane_ownership(session, backend, str(pane_id))
    if not ownership.is_owned and not can_reclaim_project_slot_pane(session, backend, str(pane_id)):
        return _RespawnOutcome(ownership_error_text(ownership, pane_id=str(pane_id)))
    blocked_detail = _stored_recovery_block(session, str(pane_id))
    if blocked_detail is not None:
        return _RespawnOutcome(blocked_detail, allow_replacement=False)
    try:
        reason = persist_crash_log(session, backend, str(pane_id))
        recovery = _prepare_crash_recovery(session, reason)
        if recovery is not None and not recovery[0]:
            _record_recovery_block(
                session,
                pane_id=str(pane_id),
                reason=str(reason or 'provider_recovery_blocked'),
                detail=recovery[1],
                blocked_at=now_str_fn(),
            )
            return _RespawnOutcome(recovery[1], allow_replacement=False)
        prepared_start_cmd = str(getattr(session, 'start_cmd', '') or '').strip()
        if prepared_start_cmd:
            start_cmd = prepared_start_cmd
        respawn(str(pane_id), cmd=start_cmd, cwd=session.work_dir, remain_on_exit=True)
        if not backend.is_alive(str(pane_id)):
            return _RespawnOutcome('respawn did not revive pane')
        activate_rebound_pane(
            session,
            backend,
            str(pane_id),
            now_str_fn=now_str_fn,
            attach_pane_log_fn=attach_pane_log_fn,
        )
        clear_recovery_block(session)
        publish_pane_recovery_outcome(session, RESPAWN)
        return _RespawnOutcome(None)
    except Exception as exc:
        return _RespawnOutcome(f'{exc}')


def _prepare_crash_recovery(session, reason: str | None) -> tuple[bool, str] | None:
    if reason is None:
        return None
    handler = getattr(session, 'prepare_crash_recovery', None)
    if not callable(handler):
        return None
    try:
        result = handler(reason)
    except Exception as exc:
        return False, f'Pane recovery blocked after {reason}: {exc}'
    if result is None:
        return None
    recovered, detail = result
    return bool(recovered), str(detail or f'Pane recovery blocked: {reason}')


@dataclass(frozen=True)
class _RespawnOutcome:
    error: str | None
    allow_replacement: bool = True

    def as_tuple(self) -> tuple[bool, str]:
        return False, str(self.error or 'respawn unavailable')


def _session_data(session) -> dict[str, object]:
    data = getattr(session, 'data', None)
    return data if isinstance(data, dict) else {}


def _stored_recovery_block(session, pane_id: str) -> str | None:
    data = getattr(session, 'data', None)
    if not isinstance(data, dict):
        return None
    block = data.get('pane_recovery_block')
    if not isinstance(block, dict) or str(block.get('pane_id') or '') != str(pane_id):
        return None
    return str(block.get('detail') or 'Pane recovery is blocked; remount after repairing provider authentication')


def _record_recovery_block(
    session,
    *,
    pane_id: str,
    reason: str,
    detail: str,
    blocked_at: str,
) -> None:
    data = getattr(session, 'data', None)
    if not isinstance(data, dict):
        return
    data['pane_recovery_block'] = {
        'reason': reason,
        'detail': detail,
        'pane_id': pane_id,
        'blocked_at': blocked_at,
    }
    _write_session_best_effort(session)


def clear_recovery_block(session) -> None:
    data = getattr(session, 'data', None)
    if not isinstance(data, dict) or data.pop('pane_recovery_block', None) is None:
        return
    _write_session_best_effort(session)


def _write_session_best_effort(session) -> None:
    writer = getattr(session, '_write_back', None)
    if not callable(writer):
        return
    try:
        writer()
    except Exception:
        return


def can_reclaim_project_slot_pane(session, backend: object, pane_id: str) -> bool:
    expected = session_slot_user_option_lookup(session)
    if not expected:
        return False
    ownership = inspect_tmux_pane_ownership(_SlotOwnershipSession(expected), backend, pane_id)
    return ownership.is_owned


def create_replacement_pane(
    session,
    backend: object,
    *,
    start_cmd: str,
    create_pane,
    now_str_fn: Callable[[], str],
    attach_pane_log_fn: Callable[[object, object, str], None],
) -> str | None:
    if not callable(create_pane):
        return None
    data = getattr(session, 'data', None)
    parent_pane = None
    if isinstance(data, dict):
        parent_pane_text = str(data.get('ccb_replacement_parent_pane') or '').strip()
        if parent_pane_text.startswith('%'):
            parent_pane = parent_pane_text
    try:
        if parent_pane is not None:
            new_pane = create_pane(start_cmd, session.work_dir, parent_pane=parent_pane)
        else:
            new_pane = create_pane(start_cmd, session.work_dir)
    except Exception:
        return None
    if not new_pane or not backend.is_alive(str(new_pane)):
        return None
    activate_rebound_pane(
        session,
        backend,
        str(new_pane),
        now_str_fn=now_str_fn,
        attach_pane_log_fn=attach_pane_log_fn,
    )
    publish_pane_recovery_outcome(session, REPLACEMENT)
    return str(new_pane)


__all__ = [
    'clear_recovery_block',
    'create_replacement_pane',
    'respawn_existing_pane',
    'tmux_rebound_pane',
]


class _SlotOwnershipSession:
    def __init__(self, expected: dict[str, str]) -> None:
        self._expected = expected

    def user_option_lookup(self) -> dict[str, str]:
        return dict(self._expected)
