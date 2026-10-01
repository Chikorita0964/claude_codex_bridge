from __future__ import annotations

import subprocess
from types import SimpleNamespace

from ccbd.services.health_assessment.tmux_runtime.namespace import pane_outside_project_namespace


def test_pane_outside_namespace_accepts_runtime_socket_fallback(monkeypatch) -> None:
    runtime = SimpleNamespace(project_id='proj-1', agent_name='agent1', tmux_socket_path='/tmp/ccb.sock')
    namespace_store = SimpleNamespace(
        load=lambda: SimpleNamespace(tmux_socket_path='/tmp/ccb.sock', tmux_session_name='sess-1')
    )

    monkeypatch.setattr(
        'ccbd.services.health_assessment.tmux_runtime.namespace.backend_socket_matches',
        lambda backend, tmux_socket_path: False,
    )
    monkeypatch.setattr(
        'ccbd.services.health_assessment.tmux_runtime.namespace.same_tmux_socket_path',
        lambda left, right: left == right,
    )

    assert pane_outside_project_namespace(
        runtime=runtime,
        namespace_state_store=namespace_store,
        backend=object(),
        pane_id='%3',
    ) is True


def test_pane_outside_namespace_is_unknown_when_both_describes_time_out() -> None:
    # Local patch (ccb-team-kit): a probe that did not answer is not "outside the namespace";
    # unknown leaves the stored health alone instead of starting a recovery for a live pane.
    runtime = SimpleNamespace(project_id='proj-1', agent_name='agent1', tmux_socket_path='/tmp/ccb.sock')
    namespace_store = SimpleNamespace(
        load=lambda: SimpleNamespace(tmux_socket_path='/tmp/ccb.sock', tmux_session_name='sess-1')
    )

    class TimedOutBackend:
        _socket_path = '/tmp/ccb.sock'

        def _tmux_run(self, *args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs.get('timeout'))

        def describe_pane(self, *args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs.get('timeout'))

    assert pane_outside_project_namespace(
        runtime=runtime,
        namespace_state_store=namespace_store,
        backend=TimedOutBackend(),
        pane_id='%3',
    ) is None


def test_pane_outside_namespace_checks_project_namespace_record(monkeypatch) -> None:
    runtime = SimpleNamespace(project_id='proj-1', agent_name='agent1', tmux_socket_path='/tmp/ccb.sock')
    namespace_store = SimpleNamespace(
        load=lambda: SimpleNamespace(tmux_socket_path='/tmp/ccb.sock', tmux_session_name='sess-1')
    )
    record = SimpleNamespace(
        matches=lambda **kwargs: kwargs == {
            'tmux_session_name': 'sess-1',
            'project_id': 'proj-1',
            'role': 'agent',
            'slot_key': 'agent1',
            'managed_by': 'ccbd',
        }
    )

    monkeypatch.setattr(
        'ccbd.services.health_assessment.tmux_runtime.namespace.backend_socket_matches',
        lambda backend, tmux_socket_path: True,
    )
    monkeypatch.setattr(
        'ccbd.services.health_assessment.tmux_runtime.namespace.inspect_project_namespace_pane',
        lambda backend, pane_id: record,
    )

    assert pane_outside_project_namespace(
        runtime=runtime,
        namespace_state_store=namespace_store,
        backend=object(),
        pane_id='%3',
    ) is False


def test_pane_outside_namespace_rejects_old_workspace_window(monkeypatch) -> None:
    runtime = SimpleNamespace(
        project_id='proj-1',
        agent_name='agent1',
        slot_key='agent1',
        tmux_socket_path='/tmp/ccb.sock',
    )
    namespace_store = SimpleNamespace(
        load=lambda: SimpleNamespace(
            tmux_socket_path='/tmp/ccb.sock',
            tmux_session_name='sess-1',
            workspace_window_id='@2',
        )
    )
    record = SimpleNamespace(
        window_id='@1',
        matches=lambda **kwargs: True,
    )

    monkeypatch.setattr(
        'ccbd.services.health_assessment.tmux_runtime.namespace.backend_socket_matches',
        lambda backend, tmux_socket_path: True,
    )
    monkeypatch.setattr(
        'ccbd.services.health_assessment.tmux_runtime.namespace.inspect_project_namespace_pane',
        lambda backend, pane_id: record,
    )

    assert pane_outside_project_namespace(
        runtime=runtime,
        namespace_state_store=namespace_store,
        backend=object(),
        pane_id='%3',
    ) is True


def test_pane_outside_namespace_accepts_declared_secondary_window(monkeypatch) -> None:
    runtime = SimpleNamespace(
        project_id='proj-1',
        agent_name='agent2',
        slot_key='agent2',
        tmux_socket_path='/tmp/ccb.sock',
        tmux_window_name='review',
    )
    namespace_store = SimpleNamespace(
        load=lambda: SimpleNamespace(
            tmux_socket_path='/tmp/ccb.sock',
            tmux_session_name='sess-1',
            workspace_window_id='@0',
        )
    )
    record = SimpleNamespace(
        window_id='@1',
        window_name='review',
        ccb_window='review',
        matches=lambda **kwargs: kwargs == {
            'tmux_session_name': 'sess-1',
            'project_id': 'proj-1',
            'role': 'agent',
            'slot_key': 'agent2',
            'window_name': 'review',
            'managed_by': 'ccbd',
        },
    )

    monkeypatch.setattr(
        'ccbd.services.health_assessment.tmux_runtime.namespace.backend_socket_matches',
        lambda backend, tmux_socket_path: True,
    )
    monkeypatch.setattr(
        'ccbd.services.health_assessment.tmux_runtime.namespace.inspect_project_namespace_pane',
        lambda backend, pane_id: record,
    )

    assert pane_outside_project_namespace(
        runtime=runtime,
        namespace_state_store=namespace_store,
        backend=object(),
        pane_id='%3',
    ) is False


def test_pane_outside_namespace_rejects_mismatched_declared_window(monkeypatch) -> None:
    runtime = SimpleNamespace(
        project_id='proj-1',
        agent_name='agent2',
        slot_key='agent2',
        tmux_socket_path='/tmp/ccb.sock',
        tmux_window_name='review',
    )
    namespace_store = SimpleNamespace(
        load=lambda: SimpleNamespace(
            tmux_socket_path='/tmp/ccb.sock',
            tmux_session_name='sess-1',
            workspace_window_id='@0',
        )
    )
    record = SimpleNamespace(
        window_id='@1',
        window_name='review',
        ccb_window='other',
        matches=lambda **kwargs: False,
    )

    monkeypatch.setattr(
        'ccbd.services.health_assessment.tmux_runtime.namespace.backend_socket_matches',
        lambda backend, tmux_socket_path: True,
    )
    monkeypatch.setattr(
        'ccbd.services.health_assessment.tmux_runtime.namespace.inspect_project_namespace_pane',
        lambda backend, pane_id: record,
    )

    assert pane_outside_project_namespace(
        runtime=runtime,
        namespace_state_store=namespace_store,
        backend=object(),
        pane_id='%3',
    ) is True
