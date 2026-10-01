from __future__ import annotations

from terminal_runtime.tmux_panes_runtime.queries_runtime.service import is_tmux_query_unknown

from ccbd.services.project_namespace_pane import backend_socket_matches, inspect_project_namespace_pane, same_tmux_socket_path


def pane_outside_project_namespace(*, runtime, namespace_state_store, backend, pane_id: str) -> bool | None:
    """True outside, False inside, None when the namespace probe did not answer."""
    pane_text = _normalized_tmux_pane_id(pane_id)
    if pane_text is None or backend is None or namespace_state_store is None:
        return False
    namespace_state = _load_namespace_state(namespace_state_store)
    if namespace_state is None:
        return False
    if not _backend_matches_namespace_socket(backend, namespace_state.tmux_socket_path):
        return _runtime_socket_matches_namespace(runtime, namespace_state.tmux_socket_path)
    record = inspect_project_namespace_pane(backend, pane_text)
    return _record_outside_namespace(runtime, namespace_state, record)


def _normalized_tmux_pane_id(pane_id: str) -> str | None:
    pane_text = str(pane_id or '').strip()
    return pane_text if pane_text.startswith('%') else None


def _load_namespace_state(namespace_state_store):
    try:
        return namespace_state_store.load()
    except Exception:
        return None


def _backend_matches_namespace_socket(backend, tmux_socket_path: str | None) -> bool:
    return backend_socket_matches(backend, tmux_socket_path)


def _runtime_socket_matches_namespace(runtime, tmux_socket_path: str | None) -> bool:
    runtime_socket = str(getattr(runtime, 'tmux_socket_path', None) or '').strip()
    return bool(runtime_socket) and same_tmux_socket_path(runtime_socket, tmux_socket_path)


def _record_outside_namespace(runtime, namespace_state, record) -> bool | None:
    if is_tmux_query_unknown(record):
        # Local patch (ccb-team-kit): a probe that did not answer is not "outside the namespace";
        # unknown must not change the stored health (2026-09-27..10-01: live panes marked foreign).
        return None
    if record is None:
        return True
    slot_key = str(getattr(runtime, 'slot_key', None) or getattr(runtime, 'agent_name', None) or '').strip() or None
    match_kwargs = {
        'tmux_session_name': namespace_state.tmux_session_name,
        'project_id': runtime.project_id,
        'role': 'agent',
        'slot_key': slot_key,
        'managed_by': 'ccbd',
    }
    window_name = _runtime_window_name(runtime)
    if window_name is not None:
        match_kwargs['window_name'] = window_name
        namespace_epoch = getattr(namespace_state, 'namespace_epoch', None)
        if namespace_epoch is not None:
            match_kwargs['namespace_epoch'] = int(namespace_epoch)
    if not record.matches(**match_kwargs):
        return True
    if _record_matches_runtime_window(runtime, record):
        return False
    workspace_window_id = str(getattr(namespace_state, 'workspace_window_id', None) or '').strip()
    if workspace_window_id and str(getattr(record, 'window_id', None) or '').strip():
        return str(record.window_id).strip() != workspace_window_id
    return False


def _runtime_window_name(runtime) -> str | None:
    window_name = str(getattr(runtime, 'tmux_window_name', None) or '').strip()
    return window_name or None


def _record_matches_runtime_window(runtime, record) -> bool:
    window_name = _runtime_window_name(runtime)
    if window_name is None:
        return False
    for field_name in ('ccb_window', 'window_name'):
        value = str(getattr(record, field_name, None) or '').strip()
        if value == window_name:
            return True
    return False


__all__ = ['pane_outside_project_namespace']
