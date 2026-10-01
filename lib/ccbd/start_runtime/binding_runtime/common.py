from __future__ import annotations

from terminal_runtime.tmux_panes_runtime.queries_runtime.service import is_tmux_query_unknown

_PANE_RUNTIME_BACKENDS = frozenset({'tmux', 'mux', 'rmux', 'psmux', 'herdr'})


def runtime_ref_backend(runtime_ref: str | None) -> str | None:
    text = str(runtime_ref or '').strip()
    if ':' not in text:
        return None
    backend, _sep, _pane_id = text.partition(':')
    backend = backend.strip().lower()
    return backend or None


def runtime_ref_pane_id(runtime_ref: str | None) -> str | None:
    text = str(runtime_ref or '').strip()
    if ':' not in text:
        return None
    backend, _sep, pane_id = text.partition(':')
    backend = backend.strip().lower()
    pane_id = pane_id.strip()
    if pane_id.startswith('title:'):
        return None
    if backend == 'tmux' and not pane_id.startswith('%'):
        return None
    return pane_id or None


def is_pane_runtime_ref(runtime_ref: str | None) -> bool:
    return runtime_ref_backend(runtime_ref) in _PANE_RUNTIME_BACKENDS


def binding_pane_id(binding) -> str | None:
    pane_id = str(getattr(binding, 'active_pane_id', None) or getattr(binding, 'pane_id', None) or '').strip()
    if pane_id:
        return pane_id
    return runtime_ref_pane_id(getattr(binding, 'runtime_ref', None))


def tmux_backend_for_factory(tmux_backend_factory, *, socket_path: str):
    try:
        return tmux_backend_factory(socket_path=socket_path)
    except TypeError:
        return tmux_backend_factory()


def matching_project_namespace_record(
    *,
    binding,
    tmux_socket_path: str,
    tmux_session_name: str | None,
    workspace_window_id: str | None,
    agent_name: str,
    project_id: str,
    window_name: str | None,
    namespace_epoch: int | None,
    tmux_backend_factory,
    inspect_project_namespace_pane_fn,
    namespace_pane_records: dict[str, object] | None = None,
):
    pane_id = binding_pane_id(binding)
    if pane_id is None:
        return None
    session_name = str(tmux_session_name or '').strip()
    if not session_name:
        return None
    if window_name is not None and namespace_epoch is None:
        return None
    if namespace_pane_records is not None:
        record = namespace_pane_records.get(pane_id)
    else:
        backend = tmux_backend_for_factory(tmux_backend_factory, socket_path=tmux_socket_path)
        record = inspect_project_namespace_pane_fn(backend, pane_id)
    if record is None or is_tmux_query_unknown(record):
        # A probe that timed out proves nothing about the binding; it is not a record.
        return None
    if not record.matches(
        tmux_session_name=session_name,
        project_id=project_id,
        role='agent',
        slot_key=agent_name,
        window_name=window_name,
        managed_by='ccbd',
        window_id=None if window_name is not None else workspace_window_id,
        namespace_epoch=namespace_epoch if window_name is not None else None,
    ):
        return None
    return record


__all__ = [
    'binding_pane_id',
    'is_pane_runtime_ref',
    'matching_project_namespace_record',
    'runtime_ref_backend',
    'runtime_ref_pane_id',
    'tmux_backend_for_factory',
]
