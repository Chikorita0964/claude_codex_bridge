from __future__ import annotations

import subprocess

from terminal_runtime.tmux_panes_runtime.queries_runtime.service import (
    TMUX_QUERY_UNKNOWN,
    TmuxQueryUnknown,
    is_tmux_query_unknown,
)

from .ownership import inspect_tmux_pane_ownership


def tmux_pane_state(session, backend, pane_id: str) -> str:
    pane_text = normalized_pane_id(pane_id)
    if backend is None or not pane_text:
        return 'missing'
    existence = pane_existence_state(backend, pane_text)
    if existence is not None:
        return existence
    ownership = inspect_tmux_pane_ownership(session, backend, pane_text)
    if getattr(ownership, 'state', None) == 'unknown':
        # Local patch (ccb-team-kit): a timeout is not proof of foreign ownership.
        return 'unknown'
    if not ownership.is_owned:
        return 'foreign'
    alive_state = pane_alive_state(backend, pane_text)
    if alive_state is not None:
        return alive_state
    return 'missing'


def normalized_pane_id(pane_id: str) -> str:
    return str(pane_id or '').strip()


def pane_existence_state(backend, pane_id: str) -> str | None:
    pane_exists = getattr(backend, 'pane_exists', None)
    if not callable(pane_exists):
        return None
    try:
        exists = pane_exists(pane_id)
    except subprocess.TimeoutExpired:
        return 'unknown'
    except Exception:
        return 'missing'
    if is_tmux_query_unknown(exists):
        return 'unknown'
    return None if exists else 'missing'


def pane_alive_state(backend, pane_id: str) -> str | None:
    unknown = False
    for method_name in ('is_tmux_pane_alive', 'is_alive'):
        alive = bool_backend_call(backend, method_name, pane_id)
        if is_tmux_query_unknown(alive):
            unknown = True
            continue
        if alive is not None:
            return 'alive' if alive else 'dead'
    return 'unknown' if unknown else None


def bool_backend_call(backend, method_name: str, pane_id: str) -> bool | TmuxQueryUnknown | None:
    method = getattr(backend, method_name, None)
    if not callable(method):
        return None
    try:
        return bool(method(pane_id))
    except subprocess.TimeoutExpired:
        return TMUX_QUERY_UNKNOWN
    except Exception:
        return None


__all__ = ['tmux_pane_state']
