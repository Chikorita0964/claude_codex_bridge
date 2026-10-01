from __future__ import annotations

from ccbd.supervisor_runtime.namespace import ensure_project_namespace


def test_ensure_project_namespace_calls_plain_ensure_when_no_namespace_kwargs_needed() -> None:
    calls: list[dict[str, object]] = []

    class _Namespace:
        def ensure(self):
            calls.append({})
            return 'ok'

    result = ensure_project_namespace(
        _Namespace(),
        layout_signature=None,
        recreate_namespace=False,
        reflow_workspace=False,
        recreate_reason=None,
    )

    assert result == 'ok'
    assert calls == [{}]


def test_ensure_project_namespace_passes_kwargs_when_supported() -> None:
    calls: list[dict[str, object]] = []

    class _Namespace:
        def ensure(self, *, layout_signature=None, force_recreate=False, recreate_reason=None):
            calls.append(
                {
                    'layout_signature': layout_signature,
                    'force_recreate': force_recreate,
                    'recreate_reason': recreate_reason,
                }
            )
            return 'ok'

    result = ensure_project_namespace(
        _Namespace(),
        layout_signature='cmd;agent1',
        recreate_namespace=True,
        reflow_workspace=False,
        recreate_reason='layout_changed',
    )

    assert result == 'ok'
    assert calls == [
        {
            'layout_signature': 'cmd;agent1',
            'force_recreate': True,
            'recreate_reason': 'layout_changed',
        }
    ]


def test_ensure_project_namespace_passes_fast_probe_timeout_for_background_maintenance() -> None:
    calls: list[dict[str, object]] = []

    class _Namespace:
        def ensure(
            self,
            *,
            layout_signature=None,
            force_recreate=False,
            recreate_reason=None,
            session_probe_timeout_s=None,
            terminal_size=None,
        ):
            calls.append(
                {
                    'layout_signature': layout_signature,
                    'force_recreate': force_recreate,
                    'recreate_reason': recreate_reason,
                    'session_probe_timeout_s': session_probe_timeout_s,
                    'terminal_size': terminal_size,
                }
            )
            return 'ok'

    result = ensure_project_namespace(
        _Namespace(),
        layout_signature='cmd;agent1',
        recreate_namespace=False,
        reflow_workspace=False,
        recreate_reason='mount_recovery:agent1',
        background_maintenance=True,
    )

    assert result == 'ok'
    assert calls == [
        {
            'layout_signature': 'cmd;agent1',
            'force_recreate': False,
            'recreate_reason': 'mount_recovery:agent1',
            'session_probe_timeout_s': 0.0,
            'terminal_size': None,
        }
    ]


def test_ensure_project_namespace_falls_back_when_signature_rejects_kwargs() -> None:
    calls: list[str] = []

    class _Namespace:
        def ensure(self):
            calls.append('plain')
            return 'ok'

    result = ensure_project_namespace(
        _Namespace(),
        layout_signature='cmd;agent1',
        recreate_namespace=False,
        reflow_workspace=False,
        recreate_reason='layout_changed',
    )

    assert result == 'ok'
    assert calls == ['plain']


def test_ensure_project_namespace_prefers_workspace_reflow_when_requested() -> None:
    calls: list[dict[str, object]] = []

    class _Namespace:
        def reflow_workspace(self, *, layout_signature=None, reason=None):
            calls.append({'layout_signature': layout_signature, 'reason': reason})
            return 'ok'

    result = ensure_project_namespace(
        _Namespace(),
        layout_signature='cmd;agent1',
        recreate_namespace=False,
        reflow_workspace=True,
        recreate_reason='pane_recovery:agent1',
    )

    assert result == 'ok'
    assert calls == [{'layout_signature': 'cmd;agent1', 'reason': 'pane_recovery:agent1'}]


def test_ensure_project_namespace_passes_fast_probe_timeout_to_reflow_for_background_maintenance() -> None:
    calls: list[dict[str, object]] = []

    class _Namespace:
        def reflow_workspace(self, *, layout_signature=None, reason=None, session_probe_timeout_s=None):
            calls.append(
                {
                    'layout_signature': layout_signature,
                    'reason': reason,
                    'session_probe_timeout_s': session_probe_timeout_s,
                }
            )
            return 'ok'

    result = ensure_project_namespace(
        _Namespace(),
        layout_signature='cmd;agent1',
        recreate_namespace=False,
        reflow_workspace=True,
        recreate_reason='pane_recovery:agent1',
        background_maintenance=True,
    )

    assert result == 'ok'
    assert calls == [
        {
            'layout_signature': 'cmd;agent1',
            'reason': 'pane_recovery:agent1',
            'session_probe_timeout_s': 0.0,
        }
    ]


def test_ensure_project_namespace_reflows_instead_of_recreating_with_topology_plan() -> None:
    ensure_calls: list[dict[str, object]] = []
    reflow_calls: list[dict[str, object]] = []

    class _Namespace:
        def ensure(self, **kwargs):
            ensure_calls.append(kwargs)
            return 'ensure'

        def reflow_workspace(self, *, layout_signature=None, reason=None):
            reflow_calls.append({'layout_signature': layout_signature, 'reason': reason})
            return 'reflow'

    result = ensure_project_namespace(
        _Namespace(),
        layout_signature='explicit-windows',
        topology_plan=object(),
        recreate_namespace=False,
        reflow_workspace=True,
        recreate_reason='pane_recovery:radviser1',
    )

    assert result == 'reflow'
    assert reflow_calls == [
        {'layout_signature': 'explicit-windows', 'reason': 'pane_recovery:radviser1'}
    ]
    assert ensure_calls == []
