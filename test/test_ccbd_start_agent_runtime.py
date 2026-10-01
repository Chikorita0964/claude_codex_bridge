from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.models import (
    AgentRuntime,
    AgentSpec,
    AgentState,
    PermissionMode,
    QueuePolicy,
    RestoreMode,
    RuntimeBindingSource,
    RuntimeMode,
    WorkspaceMode,
)
from agents.store import AgentRuntimeStore
import ccbd.start_runtime.agent_runtime as agent_runtime_module
import ccbd.start_runtime.agent_runtime_binding as agent_runtime_binding_module
from ccbd.services.registry import AgentRegistry
from ccbd.services.runtime import RuntimeService
from ccbd.services.project_namespace_pane import ProjectNamespacePaneRecord
from ccbd.start_runtime.agent_runtime import start_agent_runtime
from cli.services.provider_binding import AgentBinding
from cli.services.runtime_launch import RuntimeLaunchResult
from project.ids import compute_project_id
from project.resolver import ProjectContext
from storage.paths import PathLayout
from terminal_runtime.tmux_identity import pane_visual


class _RuntimeService:
    def __init__(self) -> None:
        self.attach_calls: list[dict[str, object]] = []
        self.mount_attach_calls: list[dict[str, object]] = []
        self.restore_calls: list[str] = []
        self._registry = _Registry()

    def attach(self, **kwargs):
        self.attach_calls.append(kwargs)
        binding_source = SimpleNamespace(value=kwargs['binding_source'])
        return SimpleNamespace(
            agent_name=kwargs['agent_name'],
            runtime_ref=kwargs['runtime_ref'],
            session_ref=kwargs['session_ref'],
            lifecycle_state=kwargs['lifecycle_state'],
            desired_state=None,
            reconcile_state=None,
            binding_source=binding_source,
            provider='codex',
            terminal_backend=kwargs['terminal_backend'],
            tmux_socket_name=kwargs['tmux_socket_name'],
            tmux_socket_path=kwargs['tmux_socket_path'],
            tmux_window_name=kwargs['tmux_window_name'],
            tmux_window_id=kwargs['tmux_window_id'],
            pane_id=kwargs['pane_id'],
            active_pane_id=kwargs['active_pane_id'],
            pane_state=kwargs['pane_state'],
            runtime_pid=kwargs['runtime_pid'],
            runtime_root=kwargs['runtime_root'],
            runtime_generation=kwargs.get('binding_generation', 1),
            daemon_generation=7,
            started_at='2026-04-21T00:00:00Z',
            last_seen_at='2026-04-21T00:00:01Z',
        )

    def attach_mount_attempt_authority(self, **kwargs):
        self.mount_attach_calls.append(kwargs)
        return self.attach(**{key: value for key, value in kwargs.items() if key != 'attempt_id'}), True

    def restore(self, agent_name: str):
        self.restore_calls.append(agent_name)


class _Registry:
    def __init__(self, runtime: AgentRuntime | None = None) -> None:
        self._runtime = runtime

    def get(self, agent_name: str):
        assert agent_name == 'agent1'
        return self._runtime


def _binding(**overrides) -> AgentBinding:
    values = {
        'runtime_ref': 'tmux:%5',
        'session_ref': 'session-5',
        'provider': 'codex',
        'runtime_root': '/tmp/runtime',
        'runtime_pid': 55,
        'session_file': '/tmp/session.json',
        'session_id': 'session-5',
        'tmux_socket_name': 'sock-a',
        'tmux_socket_path': '/tmp/ccb.sock',
        'terminal': 'tmux',
        'pane_id': '%5',
        'active_pane_id': '%5',
        'pane_title_marker': 'agent1',
        'pane_state': 'alive',
    }
    values.update(overrides)
    return AgentBinding(**values)


def _runtime(**overrides) -> AgentRuntime:
    values = {
        'agent_name': 'agent1',
        'state': AgentState.STARTING,
        'pid': None,
        'started_at': '2026-04-21T00:00:00Z',
        'last_seen_at': '2026-04-21T00:00:01Z',
        'runtime_ref': 'tmux:%5',
        'session_ref': 'session-5',
        'workspace_path': '/tmp/ws',
        'project_id': 'proj-1',
        'backend_type': 'pane-backed',
        'queue_depth': 0,
        'socket_path': None,
        'health': 'starting',
        'provider': 'codex',
        'runtime_root': '/tmp/runtime',
        'runtime_pid': 55,
        'terminal_backend': 'tmux',
        'pane_id': '%5',
        'active_pane_id': '%5',
        'pane_title_marker': 'agent1',
        'pane_state': 'alive',
        'binding_generation': 2,
        'daemon_generation': 7,
        'runtime_generation': 2,
        'managed_by': 'ccbd',
        'binding_source': RuntimeBindingSource.PROVIDER_SESSION,
        'reconcile_state': 'starting',
        'mount_attempt_id': 'mount-123',
    }
    values.update(overrides)
    return AgentRuntime(**values)


def test_start_agent_runtime_degrades_unresolved_stale_binding() -> None:
    runtime_service = _RuntimeService()

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=True),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=None,
        raw_binding=None,
        stale_binding=True,
        assigned_pane_id='%9',
        style_index=0,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=2,
        ensure_agent_runtime_fn=lambda *args, **kwargs: RuntimeLaunchResult(launched=False, binding=None),
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: None,
        same_tmux_socket_path_fn=lambda left, right: left == right,
    )

    assert execution.agent_result.action == 'degraded'
    assert execution.agent_result.health == 'degraded'
    assert execution.agent_result.failure_reason == 'stale_binding_unresolved'
    assert execution.actions_taken == ('degraded_stale_binding:agent1',)
    assert runtime_service.restore_calls == []


def test_start_agent_runtime_records_namespace_pane_without_provider_binding() -> None:
    runtime_service = _RuntimeService()

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=True),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='fake-codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=None,
        raw_binding=None,
        stale_binding=False,
        assigned_pane_id='%9',
        style_index=0,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=2,
        ensure_agent_runtime_fn=lambda *args, **kwargs: RuntimeLaunchResult(launched=False, binding=None),
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: None,
        same_tmux_socket_path_fn=lambda left, right: left == right,
        window_name='main',
    )

    assert execution.agent_result.action == 'attached'
    assert execution.agent_result.runtime_ref == 'tmux:%9'
    assert execution.agent_result.session_ref is None
    assert execution.agent_result.terminal_backend == 'tmux'
    assert execution.agent_result.pane_id == '%9'
    assert execution.agent_result.active_pane_id == '%9'
    assert execution.agent_result.tmux_socket_path == '/tmp/ccb.sock'
    assert execution.agent_result.tmux_window_name == 'main'
    assert execution.runtime_pane_id == '%9'
    assert execution.project_socket_active_pane_id == '%9'
    assert execution.socket_name == '/tmp/ccb.sock'
    assert runtime_service.attach_calls[-1]['runtime_ref'] == 'tmux:%9'
    assert runtime_service.attach_calls[-1]['pane_id'] == '%9'
    assert runtime_service.attach_calls[-1]['tmux_window_name'] == 'main'
    assert runtime_service.restore_calls == ['agent1']
    assert 'restore_runtime:agent1' in execution.actions_taken


def test_start_agent_runtime_reuses_binding_without_restore_bookkeeping() -> None:
    runtime_service = _RuntimeService()
    binding = _binding()
    relabel_calls: list[dict[str, object]] = []

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=True),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=binding,
        raw_binding=binding,
        stale_binding=False,
        assigned_pane_id=None,
        style_index=1,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=3,
        ensure_agent_runtime_fn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should not relaunch')),
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: relabel_calls.append(kwargs) or '%5',
        same_tmux_socket_path_fn=lambda left, right: left == right,
        window_name='main',
    )

    assert execution.agent_result.action == 'attached'
    assert execution.actions_taken == (
        'relabel_runtime_pane:agent1:%5',
        'reuse_binding:agent1',
    )
    assert execution.runtime_pane_id == '%5'
    assert execution.project_socket_active_pane_id == '%5'
    assert runtime_service.restore_calls == []
    assert relabel_calls[-1]['window_name'] == 'main'
    assert runtime_service.attach_calls[-1]['tmux_window_name'] == 'main'
    assert execution.agent_result.tmux_window_name == 'main'
    assert execution.agent_result.provider_prepare_count == 0
    assert execution.agent_result.duration_ms is not None


def test_start_agent_runtime_preserves_restored_health_on_reuse() -> None:
    runtime_service = _RuntimeService()
    runtime_service._registry = _Registry(
        _runtime(
            state=AgentState.IDLE,
            health='restored',
            reconcile_state='steady',
            mount_attempt_id=None,
        )
    )
    binding = _binding()

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=True),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=binding,
        raw_binding=binding,
        stale_binding=False,
        assigned_pane_id=None,
        style_index=1,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=3,
        ensure_agent_runtime_fn=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('should not relaunch')
        ),
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: '%5',
        same_tmux_socket_path_fn=lambda left, right: left == right,
        window_name='main',
    )

    assert execution.agent_result.action == 'attached'
    assert runtime_service.attach_calls[-1]['health'] is None
    assert runtime_service.restore_calls == []


def test_start_agent_runtime_reports_final_restored_authority() -> None:
    class RestoringRuntimeService(_RuntimeService):
        def attach(self, **kwargs):
            attached = super().attach(**kwargs)
            attached.health = kwargs['health'] or 'healthy'
            self._registry._runtime = attached
            return attached

        def restore(self, agent_name: str):
            super().restore(agent_name)
            current = self._registry.get(agent_name)
            values = vars(current).copy()
            values['health'] = 'restored'
            self._registry._runtime = SimpleNamespace(**values)

    runtime_service = RestoringRuntimeService()
    launched_binding = _binding()

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=True),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=None,
        raw_binding=None,
        stale_binding=False,
        assigned_pane_id='%5',
        style_index=0,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=3,
        ensure_agent_runtime_fn=lambda *args, **kwargs: RuntimeLaunchResult(
            launched=True,
            binding=launched_binding,
        ),
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: '%5',
        same_tmux_socket_path_fn=lambda left, right: left == right,
        window_name='main',
    )

    assert execution.agent_result.action == 'launched'
    assert execution.agent_result.health == 'restored'
    assert runtime_service._registry.get('agent1').health == 'restored'


def test_real_runtime_service_warm_reuse_preserves_restored_without_store_write(
    tmp_path: Path,
) -> None:
    layout = PathLayout(tmp_path)
    spec = AgentSpec(
        name='agent1',
        provider='codex',
        target='.',
        workspace_mode=WorkspaceMode.INPLACE,
        workspace_root=None,
        runtime_mode=RuntimeMode.PANE_BACKED,
        restore_default=RestoreMode.AUTO,
        permission_default=PermissionMode.AUTO,
        queue_policy=QueuePolicy.SERIAL_PER_AGENT,
    )
    config = SimpleNamespace(agents={'agent1': spec})
    runtime_store = AgentRuntimeStore(layout)
    existing = _runtime(
        state=AgentState.IDLE,
        pid=55,
        health='restored',
        desired_state='mounted',
        reconcile_state='steady',
        lifecycle_state='idle',
        mount_attempt_id=None,
        tmux_socket_name='sock-a',
        tmux_socket_path='/tmp/ccb.sock',
        session_file='/tmp/session.json',
        session_id='session-5',
        slot_key='agent1',
    )
    runtime_store.save(existing)
    registry = AgentRegistry(layout, config, runtime_store=runtime_store)
    service = RuntimeService(
        layout,
        registry,
        'proj-1',
        clock=lambda: existing.last_seen_at,
    )
    writes_before = runtime_store.save_count
    binding = _binding()

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=True),
        runtime_service=service,
        agent_name='agent1',
        spec=spec,
        plan=SimpleNamespace(workspace_path=Path('/tmp/ws')),
        binding=binding,
        raw_binding=binding,
        stale_binding=False,
        assigned_pane_id=None,
        style_index=0,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=3,
        ensure_agent_runtime_fn=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('should not relaunch')
        ),
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: None,
        same_tmux_socket_path_fn=lambda left, right: left == right,
    )

    persisted = registry.get('agent1')
    assert execution.agent_result.action == 'attached'
    assert execution.agent_result.health == 'restored'
    assert persisted is not None and persisted.health == 'restored'
    assert runtime_store.save_count == writes_before
    assert 'restore_runtime:agent1' not in execution.actions_taken


def test_start_agent_runtime_skips_current_reused_pane_identity() -> None:
    runtime_service = _RuntimeService()
    binding = _binding()
    visual = pane_visual(
        project_id='proj-1',
        slot_key='agent1',
        order_index=1,
        is_cmd=False,
        role='agent',
    )
    record = ProjectNamespacePaneRecord(
        pane_id='%5',
        session_name='ccb-demo',
        window_id='@1',
        window_name='main',
        pane_title='agent1',
        role='agent',
        slot_key='agent1',
        ccb_window='main',
        agent_label='agent1',
        label_style=visual.label_style,
        border_style=visual.border_style,
        active_border_style=visual.active_border_style,
        project_id='proj-1',
        managed_by='ccbd',
        namespace_epoch=3,
        alive=True,
    )

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=False),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=binding,
        raw_binding=binding,
        stale_binding=False,
        assigned_pane_id=None,
        style_index=1,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=3,
        namespace_pane_records={'%5': record},
        ensure_agent_runtime_fn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should not relaunch')),
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError('current pane identity must not be rewritten')
        ),
        same_tmux_socket_path_fn=lambda left, right: left == right,
        window_name='main',
    )

    assert execution.agent_result.action == 'attached'
    assert execution.actions_taken == ('reuse_binding:agent1',)


def test_start_agent_runtime_relaunches_and_tracks_project_socket_pane() -> None:
    runtime_service = _RuntimeService()
    launched_binding = _binding(runtime_ref='tmux:%7', session_ref='session-7', pane_id='%7', active_pane_id='%7')

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=True),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=None,
        raw_binding=_binding(runtime_ref='tmux:%3'),
        stale_binding=True,
        assigned_pane_id='%7',
        style_index=2,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=4,
        ensure_agent_runtime_fn=lambda *args, **kwargs: RuntimeLaunchResult(launched=True, binding=launched_binding),
        launch_binding_hint_fn=lambda **kwargs: 'hint',
        relabel_project_namespace_pane_fn=lambda **kwargs: '%7',
        same_tmux_socket_path_fn=lambda left, right: left == right,
        provider_prepared=True,
        provider_prepare_ms=12.5,
        binding_reject_reason='namespace_epoch_mismatch',
    )

    assert execution.agent_result.action == 'relaunched'
    assert execution.actions_taken == (
        'relabel_runtime_pane:agent1:%7',
        'relaunch_runtime:agent1',
        'restore_runtime:agent1',
    )
    assert execution.runtime_pane_id == '%7'
    assert execution.project_socket_active_pane_id == '%7'
    assert runtime_service.attach_calls[-1]['runtime_ref'] == 'tmux:%7'
    assert runtime_service.restore_calls == ['agent1']
    assert execution.agent_result.provider_prepare_count == 1
    assert execution.agent_result.provider_prepare_ms == 12.5
    assert execution.agent_result.binding_reject_reason == 'namespace_epoch_mismatch'


def test_start_agent_runtime_tells_the_launcher_which_pane_identity_to_verify() -> None:
    runtime_service = _RuntimeService()
    launched_binding = _binding(runtime_ref='tmux:%7', session_ref='session-7', pane_id='%7', active_pane_id='%7')
    ensure_calls: list[dict[str, object]] = []

    def ensure_runtime(*args, **kwargs):
        del args
        ensure_calls.append(dict(kwargs))
        return RuntimeLaunchResult(launched=True, binding=launched_binding)

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=False),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(name='agent1', provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=None,
        raw_binding=_binding(runtime_ref='tmux:%3'),
        stale_binding=True,
        assigned_pane_id='%7',
        style_index=2,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=7,
        window_name='pair1',
        ensure_agent_runtime_fn=ensure_runtime,
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: '%7',
        same_tmux_socket_path_fn=lambda left, right: left == right,
    )

    assert execution.agent_result.action == 'relaunched'
    assert ensure_calls[-1]['assigned_pane_id'] == '%7'
    assert ensure_calls[-1]['expected_pane_identity'] == {
        'slot': 'agent1',
        'window': 'pair1',
        'epoch': 7,
    }


def test_start_agent_runtime_without_namespace_epoch_launches_without_identity_check() -> None:
    runtime_service = _RuntimeService()
    launched_binding = _binding(runtime_ref='tmux:%7', session_ref='session-7', pane_id='%7', active_pane_id='%7')
    ensure_calls: list[dict[str, object]] = []

    def ensure_runtime(*args, **kwargs):
        del args
        ensure_calls.append(dict(kwargs))
        return RuntimeLaunchResult(launched=True, binding=launched_binding)

    start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=False),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(name='agent1', provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=None,
        raw_binding=_binding(runtime_ref='tmux:%3'),
        stale_binding=True,
        assigned_pane_id='%7',
        style_index=2,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=None,
        window_name='pair1',
        ensure_agent_runtime_fn=ensure_runtime,
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: '%7',
        same_tmux_socket_path_fn=lambda left, right: left == right,
    )

    assert 'expected_pane_identity' not in ensure_calls[-1]


def test_start_agent_runtime_launches_herdr_assigned_pane_even_with_existing_binding() -> None:
    runtime_service = _RuntimeService()
    existing_binding = _binding(
        runtime_ref='mux:w1:p3',
        session_ref='old-session',
        terminal='mux',
        tmux_socket_name=None,
        tmux_socket_path=None,
        pane_id='w1:p3',
        active_pane_id='w1:p3',
        pane_state='unknown',
        runtime_pid=None,
    )
    launched_binding = _binding(
        runtime_ref='herdr:w1:p3',
        session_ref='session-herdr-1',
        terminal='herdr',
        tmux_socket_name=None,
        tmux_socket_path=None,
        pane_id='w1:p3',
        active_pane_id='w1:p3',
        pane_state='alive',
        runtime_pid=901,
    )
    ensure_calls: list[dict[str, object]] = []

    def ensure_runtime(*args, **kwargs):
        del args
        ensure_calls.append(dict(kwargs))
        return RuntimeLaunchResult(launched=True, binding=launched_binding)

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=False),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=existing_binding,
        raw_binding=existing_binding,
        stale_binding=False,
        assigned_pane_id='w1:p3',
        assigned_pane_ref={
            'backend_impl': 'herdr',
            'pane_id': 'w1:p3',
            'session_name': 'ccb-herdr-session',
            'window_name': 'main',
            'agent_slug': 'agent1',
        },
        namespace_ref={
            'backend_family': 'herdr-native',
            'backend_impl': 'herdr',
            'namespace_id': 'w1',
            'session_name': 'ccb-herdr-session',
            'ipc_kind': 'herdr_socket',
            'ipc_ref': 'herdr://local',
            'restore_token': None,
        },
        style_index=0,
        project_id='proj-1',
        tmux_socket_path=None,
        namespace_backend_impl='herdr',
        namespace_epoch=1,
        ensure_agent_runtime_fn=ensure_runtime,
        launch_binding_hint_fn=lambda **kwargs: existing_binding,
        relabel_project_namespace_pane_fn=lambda **kwargs: 'w1:p3',
        same_tmux_socket_path_fn=lambda left, right: left == right,
        window_name='main',
    )

    assert len(ensure_calls) == 1
    assert ensure_calls[0]['assigned_pane_ref']['backend_impl'] == 'herdr'
    assert execution.agent_result.action == 'launched'
    assert execution.actions_taken == (
        'relabel_runtime_pane:agent1:w1:p3',
        'launch_runtime:agent1',
    )
    assert runtime_service.attach_calls[-1]['terminal_backend'] == 'herdr'
    assert runtime_service.attach_calls[-1]['pane_id'] == 'w1:p3'


def test_start_agent_runtime_records_exact_boundary_timings(monkeypatch) -> None:
    now_ns = 0

    def monotonic_ns() -> int:
        return now_ns

    def advance_ms(value: int) -> None:
        nonlocal now_ns
        now_ns += value * 1_000_000

    monkeypatch.setattr(agent_runtime_module, 'monotonic_ns', monotonic_ns)
    monkeypatch.setattr(agent_runtime_binding_module, 'monotonic_ns', monotonic_ns)

    runtime_service = _RuntimeService()
    original_attach = runtime_service.attach
    original_restore = runtime_service.restore

    def attach(**kwargs):
        advance_ms(5)
        return original_attach(**kwargs)

    def restore(agent_name: str):
        advance_ms(7)
        return original_restore(agent_name)

    runtime_service.attach = attach
    runtime_service.restore = restore
    launched_binding = _binding(runtime_ref='tmux:%7', pane_id='%7', active_pane_id='%7')

    def ensure_agent_runtime(*args, **kwargs):
        advance_ms(11)
        return RuntimeLaunchResult(
            launched=True,
            binding=launched_binding,
            timings_ms={
                'build_start_cmd': 4.0,
                'binding_resolve': 3.0,
                'unattributed': 4.0,
            },
        )

    def relabel_project_namespace_pane(**kwargs):
        advance_ms(2)
        return '%7'

    def same_tmux_socket_path(left, right):
        advance_ms(3)
        return left == right

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=True),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=None,
        raw_binding=None,
        stale_binding=False,
        assigned_pane_id='%7',
        style_index=0,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=1,
        ensure_agent_runtime_fn=ensure_agent_runtime,
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=relabel_project_namespace_pane,
        same_tmux_socket_path_fn=same_tmux_socket_path,
    )

    assert execution.agent_result.duration_ms == 28.0
    assert execution.agent_result.timings_ms == {
        'prepare_launch_context': 0.0,
        'pane_and_runtime_facts': 5.0,
        'build_start_cmd': 4.0,
        'tmux_respawn': 0.0,
        'pane_identity': 0.0,
        'pane_agent_report': 0.0,
        'session_write': 0.0,
        'provider_post_launch': 0.0,
        'binding_resolve': 3.0,
        'unattributed': 4.0,
        'authority_commit': 5.0,
        'restore_bookkeeping': 7.0,
    }
    record = execution.agent_result.to_record()
    assert type(execution.agent_result).from_record(record).timings_ms == execution.agent_result.timings_ms
    record.pop('timings_ms')
    assert type(execution.agent_result).from_record(record).timings_ms == {}


def test_start_agent_runtime_attaches_structured_failure_without_wrapping(monkeypatch) -> None:
    now_ns = 0

    def monotonic_ns() -> int:
        return now_ns

    def advance_ms(value: int) -> None:
        nonlocal now_ns
        now_ns += value * 1_000_000

    monkeypatch.setattr(agent_runtime_module, 'monotonic_ns', monotonic_ns)
    monkeypatch.setattr(agent_runtime_binding_module, 'monotonic_ns', monotonic_ns)
    original_error = RuntimeError('provider launch failed')

    def ensure_agent_runtime(*args, **kwargs):
        del args, kwargs
        advance_ms(11)
        setattr(
            original_error,
            'ccb_startup_timings_ms',
            {'build_start_cmd': 4.0, 'unattributed': 7.0},
        )
        raise original_error

    with pytest.raises(RuntimeError, match='provider launch failed') as captured:
        start_agent_runtime(
            context=object(),
            command=SimpleNamespace(restore=False),
            runtime_service=_RuntimeService(),
            agent_name='agent1',
            spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
            plan=SimpleNamespace(workspace_path='/tmp/ws'),
            binding=None,
            raw_binding=None,
            stale_binding=False,
            assigned_pane_id='%7',
            style_index=0,
            project_id='proj-1',
            tmux_socket_path='/tmp/ccb.sock',
            namespace_epoch=1,
            ensure_agent_runtime_fn=ensure_agent_runtime,
            launch_binding_hint_fn=lambda **kwargs: None,
            relabel_project_namespace_pane_fn=lambda **kwargs: None,
            same_tmux_socket_path_fn=lambda left, right: left == right,
            provider_prepared=True,
            provider_prepare_ms=2.0,
        )

    assert captured.value is original_error
    failed = getattr(original_error, 'ccb_startup_agent_result')
    assert failed.agent_name == 'agent1'
    assert failed.provider == 'codex'
    assert failed.action == 'failed'
    assert failed.health == 'failed'
    assert failed.failure_reason == 'provider launch failed'
    assert failed.provider_prepare_count == 1
    assert failed.provider_prepare_ms == 2.0
    assert failed.duration_ms == 11.0
    assert failed.timings_ms == {
        'prepare_launch_context': 0.0,
        'pane_and_runtime_facts': 0.0,
        'build_start_cmd': 4.0,
        'tmux_respawn': 0.0,
        'pane_identity': 0.0,
        'pane_agent_report': 0.0,
        'session_write': 0.0,
        'provider_post_launch': 0.0,
        'binding_resolve': 0.0,
        'authority_commit': 0.0,
        'restore_bookkeeping': 0.0,
        'unattributed': 7.0,
    }
    assert sum(failed.timings_ms.values()) <= failed.duration_ms


@pytest.mark.parametrize(
    ('failure_kind', 'timing_field', 'expected_duration'),
    (
        ('authority_commit', 'authority_commit', 5.0),
        ('mount_attempt_authority', 'authority_commit', 5.0),
        ('restore_bookkeeping', 'restore_bookkeeping', 7.0),
    ),
)
def test_start_agent_runtime_keeps_structured_failure_after_launch(
    monkeypatch,
    failure_kind: str,
    timing_field: str,
    expected_duration: float,
) -> None:
    now_ns = 0

    def monotonic_ns() -> int:
        return now_ns

    def advance_ms(value: int) -> None:
        nonlocal now_ns
        now_ns += value * 1_000_000

    monkeypatch.setattr(agent_runtime_module, 'monotonic_ns', monotonic_ns)
    monkeypatch.setattr(agent_runtime_binding_module, 'monotonic_ns', monotonic_ns)
    runtime_service = _RuntimeService()
    original_error = RuntimeError(f'{failure_kind} failed')
    if failure_kind == 'authority_commit':
        def attach(**_kwargs):
            advance_ms(5)
            raise original_error

        runtime_service.attach = attach
        restore = False
    elif failure_kind == 'mount_attempt_authority':
        runtime_service._registry = _Registry(_runtime())

        def attach_mount_attempt_authority(**_kwargs):
            advance_ms(5)
            raise original_error

        runtime_service.attach_mount_attempt_authority = attach_mount_attempt_authority
        restore = False
    else:
        def restore_agent(_agent_name: str):
            advance_ms(7)
            raise original_error

        runtime_service.restore = restore_agent
        restore = True

    launched_binding = _binding(
        runtime_ref='tmux:%7',
        session_ref='session-7',
        pane_id='%7',
        active_pane_id='%7',
    )
    with pytest.raises(RuntimeError, match=failure_kind) as captured:
        start_agent_runtime(
            context=object(),
            command=SimpleNamespace(restore=restore),
            runtime_service=runtime_service,
            agent_name='agent1',
            spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
            plan=SimpleNamespace(workspace_path='/tmp/ws'),
            binding=None,
            raw_binding=None,
            stale_binding=False,
            assigned_pane_id='%7',
            style_index=0,
            project_id='proj-1',
            tmux_socket_path='/tmp/ccb.sock',
            namespace_epoch=1,
            ensure_agent_runtime_fn=lambda *args, **kwargs: RuntimeLaunchResult(
                launched=True,
                binding=launched_binding,
            ),
            launch_binding_hint_fn=lambda **kwargs: None,
            relabel_project_namespace_pane_fn=lambda **kwargs: '%7',
            same_tmux_socket_path_fn=lambda left, right: left == right,
        )

    assert captured.value is original_error
    failed = getattr(original_error, 'ccb_startup_agent_result')
    assert failed.action == 'failed'
    assert failed.health == 'failed'
    assert failed.failure_reason == f'{failure_kind} failed'
    assert failed.duration_ms == expected_duration
    assert failed.timings_ms[timing_field] == expected_duration
    assert sum(failed.timings_ms.values()) <= failed.duration_ms


def test_start_agent_runtime_uses_runtime_service_for_helper_ownership(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo'
    config_dir = project_root / '.ccb'
    config_dir.mkdir(parents=True)
    context = SimpleNamespace(
        paths=PathLayout(project_root),
        project=ProjectContext(
            cwd=project_root,
            project_root=project_root,
            config_dir=config_dir,
            project_id=compute_project_id(project_root),
            source='test',
        ),
    )
    runtime_service = _RuntimeService()
    launched_binding = _binding(
        runtime_ref='tmux:%7',
        session_ref='session-7',
        pane_id='%7',
        active_pane_id='%7',
        runtime_root=str(tmp_path / 'runtime'),
    )
    runtime_dir = Path(launched_binding.runtime_root)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / 'bridge.pid').write_text('5511\n', encoding='utf-8')

    start_agent_runtime(
        context=context,
        command=SimpleNamespace(restore=False),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=None,
        raw_binding=None,
        stale_binding=False,
        assigned_pane_id='%7',
        style_index=0,
        project_id=context.project.project_id,
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=1,
        ensure_agent_runtime_fn=lambda *args, **kwargs: RuntimeLaunchResult(launched=True, binding=launched_binding),
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: '%7',
        same_tmux_socket_path_fn=lambda left, right: left == right,
    )

    assert runtime_service.attach_calls


def test_start_agent_runtime_uses_mount_attempt_scoped_attach_during_supervision_starting() -> None:
    runtime_service = _RuntimeService()
    runtime_service._registry = _Registry(_runtime())
    binding = _binding(runtime_ref='tmux:%7', session_ref='session-7', pane_id='%7', active_pane_id='%7')

    execution = start_agent_runtime(
        context=object(),
        command=SimpleNamespace(restore=False),
        runtime_service=runtime_service,
        agent_name='agent1',
        spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
        plan=SimpleNamespace(workspace_path='/tmp/ws'),
        binding=binding,
        raw_binding=binding,
        stale_binding=False,
        assigned_pane_id=None,
        style_index=1,
        project_id='proj-1',
        tmux_socket_path='/tmp/ccb.sock',
        namespace_epoch=3,
        ensure_agent_runtime_fn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should not relaunch')),
        launch_binding_hint_fn=lambda **kwargs: None,
        relabel_project_namespace_pane_fn=lambda **kwargs: '%7',
        same_tmux_socket_path_fn=lambda left, right: left == right,
    )

    assert execution.agent_result.action == 'attached'
    assert len(runtime_service.mount_attach_calls) == 1
    assert runtime_service.mount_attach_calls[0]['attempt_id'] == 'mount-123'
    assert runtime_service.attach_calls == [
        {
            'agent_name': 'agent1',
            'workspace_path': '/tmp/ws',
            'backend_type': 'pane-backed',
            'runtime_ref': 'tmux:%7',
            'session_ref': 'session-7',
            'health': 'healthy',
            'provider': 'codex',
            'runtime_root': '/tmp/runtime',
            'runtime_pid': 55,
            'terminal_backend': 'tmux',
            'pane_id': '%7',
            'active_pane_id': '%7',
            'pane_title_marker': 'agent1',
            'pane_state': 'alive',
            'tmux_socket_name': 'sock-a',
            'tmux_socket_path': '/tmp/ccb.sock',
            'tmux_window_name': None,
            'tmux_window_id': None,
            'session_file': '/tmp/session.json',
            'session_id': 'session-5',
            'slot_key': 'agent1',
            'window_id': None,
            'workspace_epoch': None,
            'lifecycle_state': execution.agent_result.lifecycle_state,
            'managed_by': 'ccbd',
            'binding_source': 'provider-session',
        }
    ]


def test_start_agent_runtime_fails_closed_when_mount_attempt_is_superseded() -> None:
    runtime_service = _RuntimeService()
    current = _runtime()
    runtime_service._registry = _Registry(current)

    def superseded(**_kwargs):
        return current, False

    runtime_service.attach_mount_attempt_authority = superseded
    launched_binding = _binding(
        runtime_ref='tmux:%7',
        session_ref='session-7',
        pane_id='%7',
        active_pane_id='%7',
    )

    with pytest.raises(RuntimeError, match='mount attempt authority was superseded') as captured:
        start_agent_runtime(
            context=object(),
            command=SimpleNamespace(restore=True),
            runtime_service=runtime_service,
            agent_name='agent1',
            spec=SimpleNamespace(provider='codex', runtime_mode=SimpleNamespace(value='pane-backed')),
            plan=SimpleNamespace(workspace_path='/tmp/ws'),
            binding=None,
            raw_binding=None,
            stale_binding=False,
            assigned_pane_id='%7',
            style_index=0,
            project_id='proj-1',
            tmux_socket_path='/tmp/ccb.sock',
            namespace_epoch=1,
            ensure_agent_runtime_fn=lambda *args, **kwargs: RuntimeLaunchResult(
                launched=True,
                binding=launched_binding,
            ),
            launch_binding_hint_fn=lambda **kwargs: None,
            relabel_project_namespace_pane_fn=lambda **kwargs: '%7',
            same_tmux_socket_path_fn=lambda left, right: left == right,
        )

    assert runtime_service.restore_calls == []
    failed = getattr(captured.value, 'ccb_startup_agent_result')
    assert failed.action == 'failed'
    assert failed.failure_reason == 'startup mount attempt authority was superseded for agent1'
