from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from agents.models import AgentRuntime, AgentState
from ccbd.app import CcbdApp
import ccbd.handlers.project_restart as project_restart
from ccbd.handlers.project_restart import RESTART_PANES_REASON, build_project_restart_panes_handler
from ccbd.lifecycle_report_store import CcbdStartupReportStore
from ccbd.services.lifecycle import build_lifecycle
from ccbd.startup_fence import StartupFenceError
from ccbd.start_flow import StartFlowSummary
from ccbd.start_flow_runtime.service_tmux import (
    bootstrap_cmd_pane_if_needed,
    project_socket_active_panes,
    tmux_namespace_runtime,
)
from ccbd.socket_client import CcbdClient, CcbdClientError
from cli.services.provider_binding import AgentBinding
from cli.services.runtime_launch import RuntimeLaunchResult
from cli.services.tmux_project_cleanup import ProjectTmuxCleanupSummary
from project.resolver import bootstrap_project
from provider_runtime.helper_manifest import load_helper_manifest
from runtime_observability import StartupReadinessRecorder
import pytest


def _wait_for(path: Path, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            if path.suffix != '.sock':
                return
            try:
                payload = CcbdClient(path, timeout_s=0.5).ping('ccbd')
                diagnostics = payload.get('diagnostics')
                stage = diagnostics.get('startup_stage') if isinstance(diagnostics, dict) else None
                if stage in {None, '', 'mounted'}:
                    return
            except Exception:
                pass
        time.sleep(0.02)
    raise AssertionError(f'timed out waiting for {path}')


class _TrackedGateLock:
    def __init__(self, contention_event: threading.Event) -> None:
        self._lock = threading.RLock()
        self._contention_event = contention_event

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self._contention_event.set()
            self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self._lock.release()


class _FakeNamespaceTmuxBackend:
    def __init__(self, *, socket_path: str | None = None):
        self.socket_path = socket_path

    def _tmux_run(self, args, *, capture=False, check=False, input_bytes=None, timeout=None):
        del capture, check, input_bytes, timeout
        if args[:3] == ['list-panes', '-t', 'ccb-repo-ccbd-start']:
            return SimpleNamespace(stdout='%0\n')
        if args[:2] == ['list-panes', '-t']:
            return SimpleNamespace(stdout='%0\n')
        raise AssertionError(f'unexpected tmux args: {args}')


def test_project_socket_active_panes_preserves_namespace_root_without_cmd() -> None:
    active_panes, cmd_pane_id = project_socket_active_panes(
        tmux_layout=SimpleNamespace(cmd_pane_id=None, agent_panes={}),
        tmux_socket_path='/tmp/ccb.sock',
        config=SimpleNamespace(cmd_enabled=False),
        root_pane_id='%0',
    )

    assert active_panes == ['%0']
    assert cmd_pane_id is None


def test_topology_start_uses_only_the_authoritative_cmd_pane() -> None:
    calls: list[list[str]] = []

    class Backend:
        def __init__(self, *, socket_path: str | None = None):
            self.socket_path = socket_path

        def _tmux_run(self, args, **kwargs):
            del kwargs
            calls.append(list(args))
            raise AssertionError('topology cmd selection must not rescan or infer the first pane')

    backend, cmd_pane = tmux_namespace_runtime(
        SimpleNamespace(tmux_backend_cls=Backend),
        tmux_socket_path='/tmp/ccb.sock',
        tmux_session_name='ccb-project',
        tmux_workspace_window_name='main',
        namespace_cmd_pane='%7',
        namespace_topology_managed=True,
        cmd_enabled=True,
    )

    assert backend.socket_path == '/tmp/ccb.sock'
    assert cmd_pane == '%7'
    assert calls == []


def test_topology_start_accepts_herdr_authoritative_cmd_pane_without_tmux_socket() -> None:
    class Backend:
        def __init__(self, *, socket_path: str | None = None):
            self.socket_path = socket_path

        def _tmux_run(self, args, **kwargs):
            raise AssertionError(f'Herdr topology must not invoke tmux: {args!r}, {kwargs!r}')

    backend, cmd_pane = tmux_namespace_runtime(
        SimpleNamespace(tmux_backend_cls=Backend),
        tmux_socket_path='',
        tmux_session_name='ccb-project',
        tmux_workspace_window_name='main',
        namespace_cmd_pane='herdr-pane-cmd',
        namespace_topology_managed=True,
        cmd_enabled=True,
    )

    assert backend is None
    assert cmd_pane == 'herdr-pane-cmd'


def test_topology_start_fails_closed_when_cmd_authority_is_missing() -> None:
    with pytest.raises(RuntimeError, match='authoritative topology cmd pane is missing'):
        tmux_namespace_runtime(
            SimpleNamespace(tmux_backend_cls=_FakeNamespaceTmuxBackend),
            tmux_socket_path='/tmp/ccb.sock',
            tmux_session_name='ccb-project',
            tmux_workspace_window_name='main',
            namespace_cmd_pane=None,
            namespace_topology_managed=True,
            cmd_enabled=True,
        )


def test_project_socket_active_panes_ignores_herdr_panes_without_tmux_socket() -> None:
    active_panes, cmd_pane_id = project_socket_active_panes(
        tmux_layout=SimpleNamespace(cmd_pane_id='herdr-pane-cmd', agent_panes={}),
        tmux_socket_path='',
        config=SimpleNamespace(cmd_enabled=True),
        root_pane_id='herdr-pane-root',
        namespace_active_panes=('herdr-pane-cmd', 'herdr-pane-agent'),
    )

    assert active_panes == []
    assert cmd_pane_id == 'herdr-pane-cmd'


def test_bootstrap_cmd_pane_skips_herdr_namespace_without_tmux_socket(tmp_path: Path) -> None:
    calls: list[object] = []

    bootstrap_cmd_pane_if_needed(
        SimpleNamespace(
            bootstrap_project_namespace_cmd_pane_impl=lambda **kwargs: calls.append(kwargs),
        ),
        fresh_namespace=True,
        cmd_pane_id='herdr-pane-cmd',
        project_root=tmp_path,
        project_id='proj-herdr',
        tmux_socket_path='',
        namespace_epoch=1,
        actions_taken=[],
    )

    assert calls == []


def test_tmux_layout_for_start_uses_namespace_agent_panes_when_provided() -> None:
    from ccbd.start_flow_runtime.service_tmux import tmux_layout_for_start

    calls: dict[str, object] = {}
    deps = SimpleNamespace(
        set_tmux_ui_active_fn=lambda active: calls.setdefault('ui_active', active),
        build_project_layout_plan_fn=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('namespace topology should provide panes')
        ),
        prepare_tmux_start_layout_fn=None,
    )
    prepared_agents = (
        SimpleNamespace(agent_name='agent1', binding=None),
        SimpleNamespace(agent_name='agent2', binding=None),
        SimpleNamespace(agent_name='agent3', binding=None),
    )

    layout = tmux_layout_for_start(
        deps,
        SimpleNamespace(),
        config=SimpleNamespace(windows_explicit=False),
        prepared_agents=prepared_agents,
        interactive_tmux_layout=True,
        tmux_backend=SimpleNamespace(),
        root_pane_id='%0',
        namespace_agent_panes={'agent1': '%1', 'agent2': '%2', 'agent3': '%3'},
        actions_taken=[],
    )

    assert calls['ui_active'] is True
    assert layout.cmd_pane_id is None
    assert layout.agent_panes == {'agent1': '%1', 'agent2': '%2', 'agent3': '%3'}


def test_tmux_layout_for_start_keeps_herdr_namespace_panes_for_bound_agents() -> None:
    from ccbd.start_flow_runtime.service_tmux import tmux_layout_for_start

    calls: list[str] = []
    deps = SimpleNamespace(set_tmux_ui_active_fn=lambda active: calls.append(f'ui:{active}'))
    prepared_agents = (
        SimpleNamespace(agent_name='agent1', binding=object()),
        SimpleNamespace(agent_name='agent2', binding=object()),
    )

    layout = tmux_layout_for_start(
        deps,
        SimpleNamespace(),
        config=SimpleNamespace(windows_explicit=False),
        prepared_agents=prepared_agents,
        interactive_tmux_layout=True,
        tmux_backend=SimpleNamespace(),
        root_pane_id='w1:p0',
        namespace_agent_panes={'agent1': 'w1:p1', 'agent2': 'w1:p2'},
        actions_taken=[],
        namespace_backend_impl='herdr',
    )

    assert calls == ['ui:True']
    assert layout.agent_panes == {'agent1': 'w1:p1', 'agent2': 'w1:p2'}


def test_tmux_layout_for_start_labels_legacy_main_window() -> None:
    from ccbd.start_flow_runtime.service_tmux import tmux_layout_for_start
    from cli.services.tmux_start_layout import TmuxStartLayout

    calls: dict[str, object] = {}

    def _prepare_start_layout_impl(*args, **kwargs):
        del args
        calls['window_name'] = kwargs.get('window_name')
        return TmuxStartLayout(cmd_pane_id=None, agent_panes={'main': '%1'})

    deps = SimpleNamespace(
        set_tmux_ui_active_fn=lambda active: calls.setdefault('ui_active', active),
        build_project_layout_plan_fn=lambda *args, **kwargs: SimpleNamespace(target_agent_names=('main',)),
        prepare_start_layout_impl=_prepare_start_layout_impl,
        inside_tmux_impl=lambda: True,
        prepare_tmux_start_layout_fn=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('prepare_start_layout_impl should be used')
        ),
    )
    prepared_agents = (SimpleNamespace(agent_name='main', binding=None),)

    layout = tmux_layout_for_start(
        deps,
        SimpleNamespace(),
        config=SimpleNamespace(
            windows_explicit=False,
            windows=(SimpleNamespace(name='main'),),
        ),
        prepared_agents=prepared_agents,
        interactive_tmux_layout=True,
        tmux_backend=SimpleNamespace(),
        root_pane_id='%0',
        namespace_agent_panes=None,
        actions_taken=[],
    )

    assert calls['ui_active'] is True
    assert calls['window_name'] == 'main'
    assert layout.agent_panes == {'main': '%1'}


def test_project_restart_panes_handler_schedules_in_place_pane_restart(monkeypatch) -> None:
    restarts: list[tuple[object, tuple[str, ...]]] = []

    app = SimpleNamespace(
        config=SimpleNamespace(agents={'agent1': object(), 'agent2': object()}),
        start_maintenance_lock=threading.Lock(),
    )
    monkeypatch.setattr(
        project_restart,
        'restart_project_agent_panes_in_place',
        lambda app_arg, *, agent_names: restarts.append((app_arg, agent_names)),
    )
    handler = build_project_restart_panes_handler(app)

    payload, after_response = handler({})

    assert payload == {
        'status': 'scheduled',
        'agent_names': ['agent1', 'agent2'],
        'restart_mode': 'in_place',
        'recreate_reason': RESTART_PANES_REASON,
    }

    after_response()

    assert restarts == [(app, ('agent1', 'agent2'))]


def test_project_restart_panes_handler_defers_herdr_without_scheduled_success(monkeypatch) -> None:
    restarts: list[tuple[object, tuple[str, ...]]] = []

    app = SimpleNamespace(
        config=SimpleNamespace(agents={'agent1': object(), 'agent2': object()}),
        start_maintenance_lock=threading.Lock(),
        project_namespace=SimpleNamespace(
            load=lambda: SimpleNamespace(
                namespace_backend_family='herdr-native',
                backend_impl='herdr',
                tmux_socket_path='',
            )
        ),
    )
    monkeypatch.setattr(
        project_restart,
        'restart_project_agent_panes_in_place',
        lambda app_arg, *, agent_names: restarts.append((app_arg, agent_names)),
    )
    handler = build_project_restart_panes_handler(app)

    payload, after_response = handler({})
    after_response()

    assert payload['status'] == 'unsupported'
    assert payload['restart_status'] == 'deferred'
    assert payload['reason'] == 'deferred_to_provider_runtime_on_herdr'
    assert payload['restart_mode'] == 'provider_runtime_required'
    assert payload['backend_impl'] == 'herdr'
    assert payload['restart_evidence'] == {
        'backend_impl': 'herdr',
        'namespace_backend_family': 'herdr-native',
        'restart_surface': 'provider_runtime_required',
        'restart_status': 'deferred',
        'reason': 'deferred_to_provider_runtime_on_herdr',
        'respawn_evidence': 'not_attempted',
        'session_binding_evidence': 'not_attempted',
        'result_count': 2,
    }
    assert payload['results'] == [
        {
            'agent': 'agent1',
            'status': 'deferred',
            'reason': 'deferred_to_provider_runtime_on_herdr',
            'backend_impl': 'herdr',
            'namespace_backend_family': 'herdr-native',
            'restart_mode': 'provider_runtime_required',
            'restart_evidence': {
                'backend_impl': 'herdr',
                'namespace_backend_family': 'herdr-native',
                'restart_surface': 'provider_runtime_required',
                'restart_status': 'deferred',
                'reason': 'deferred_to_provider_runtime_on_herdr',
                'respawn_evidence': 'not_attempted',
                'session_binding_evidence': 'not_attempted',
                'agent': 'agent1',
            },
        },
        {
            'agent': 'agent2',
            'status': 'deferred',
            'reason': 'deferred_to_provider_runtime_on_herdr',
            'backend_impl': 'herdr',
            'namespace_backend_family': 'herdr-native',
            'restart_mode': 'provider_runtime_required',
            'restart_evidence': {
                'backend_impl': 'herdr',
                'namespace_backend_family': 'herdr-native',
                'restart_surface': 'provider_runtime_required',
                'restart_status': 'deferred',
                'reason': 'deferred_to_provider_runtime_on_herdr',
                'respawn_evidence': 'not_attempted',
                'session_binding_evidence': 'not_attempted',
                'agent': 'agent2',
            },
        },
    ]
    assert restarts == []


def test_ccbd_start_flow_writes_runtime_authority_via_rpc(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-ccbd-start'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('cmd; demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda: SimpleNamespace(
            tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
            tmux_session_name=app.paths.ccbd_tmux_session_name,
            namespace_epoch=1,
        ),
    )
    monkeypatch.setattr('ccbd.start_flow.TmuxBackend', _FakeNamespaceTmuxBackend)

    monkeypatch.setattr('ccbd.start_flow.set_tmux_ui_active', lambda active: None)
    monkeypatch.setattr(
        'ccbd.start_flow.prepare_tmux_start_layout',
        lambda context, config, targets, **kwargs: SimpleNamespace(cmd_pane_id=None, agent_panes={}),
    )
    monkeypatch.setattr('ccbd.start_flow.cleanup_project_tmux_orphans_by_socket', lambda **kwargs: ())
    monkeypatch.setattr(
        'ccbd.start_flow.TmuxCleanupHistoryStore',
        lambda paths: SimpleNamespace(append=lambda event: None),
    )
    monkeypatch.setattr('ccbd.start_flow.resolve_agent_binding', lambda **kwargs: None)
    monkeypatch.setattr(
        'ccbd.start_flow.ensure_agent_runtime',
        lambda *args, **kwargs: RuntimeLaunchResult(
            launched=True,
            binding=AgentBinding(
                runtime_ref='tmux:%901',
                session_ref='session-901',
                provider='codex',
                runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
                runtime_pid=901,
                session_file=str(project_root / '.ccb' / 'demo.session.json'),
                session_id='session-901',
                tmux_socket_name='sock-a',
                tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
                terminal='tmux',
                pane_id='%901',
                active_pane_id='%901',
                pane_title_marker='CCB-demo',
                pane_state='alive',
            ),
        ),
    )

    thread = threading.Thread(target=app.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    _wait_for(app.paths.ccbd_socket_path)

    client = CcbdClient(app.paths.ccbd_socket_path)
    readiness_origin_ns = time.perf_counter_ns() - 5_000_000
    payload = client.start(
        agent_names=('demo',),
        restore=False,
        auto_permission=False,
        readiness_trace={
            'schema_version': 1,
            'trace_id': 'trace_' + 'a' * 32,
            'origin_monotonic_ns': readiness_origin_ns,
            'attach_mode': 'no_attach',
            'expected_daemon_generation': int(app.lease.generation),
            'keeper_startup_id': None,
            'T1_lifecycle_intent': {
                'status': 'not_required_already_mounted',
                'elapsed_ms': None,
                'source': 'test_existing_generation',
            },
            'T2_control_plane_ready': {
                'status': 'reached',
                'elapsed_ms': 1.0,
                'source': 'test_control_plane',
            },
        },
    )
    runtime = app.registry.get('demo')
    report = CcbdStartupReportStore(app.paths).load()

    assert payload['started'] == ['demo']
    assert payload['project_id'] == app.project_id
    assert str(payload['startup_run_id']).startswith('start_')
    assert report is not None
    assert report.startup_run_id == payload['startup_run_id']
    assert report.readiness_timeline is not None
    assert report.readiness_timeline['timeline_complete'] is True
    assert report.readiness_timeline['generation_correlation'] == 'matched'
    assert report.readiness_timeline['points']['T3_namespace_attachable']['status'] == 'reached'
    assert report.readiness_timeline['points']['T4_requested_agents_ready']['status'] == 'reached'
    assert report.readiness_timeline['points']['T5_foreground_attached']['status'] == 'not_applicable_no_attach'
    assert report.readiness_timeline['points']['T6_fully_warm']['status'] == 'reached'
    assert 'origin_monotonic_ns' not in json.dumps(report.readiness_timeline, sort_keys=True)
    assert runtime is not None
    assert runtime.runtime_ref == 'tmux:%901'
    assert runtime.session_ref == 'session-901'
    assert runtime.runtime_root == str(app.paths.agent_provider_runtime_dir('demo', 'codex'))
    assert runtime.runtime_pid == 901
    assert runtime.tmux_socket_name == 'sock-a'
    assert runtime.tmux_socket_path == str(app.paths.ccbd_tmux_socket_path)
    assert runtime.binding_source.value == 'provider-session'
    assert runtime.managed_by == 'ccbd'
    assert runtime.lifecycle_state == 'idle'

    client.shutdown()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_runtime_supervisor_start_can_skip_tmux_cleanup_and_layout_for_background_mount(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-ccbd-start-no-cleanup'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('cmd; demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda: SimpleNamespace(
            tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
            tmux_session_name=app.paths.ccbd_tmux_session_name,
            namespace_epoch=2,
        ),
    )
    monkeypatch.setattr('ccbd.start_flow.TmuxBackend', _FakeNamespaceTmuxBackend)

    monkeypatch.setattr('ccbd.start_flow.set_tmux_ui_active', lambda active: (_ for _ in ()).throw(AssertionError('tmux ui should be skipped')))
    monkeypatch.setattr(
        'ccbd.start_flow.prepare_tmux_start_layout',
        lambda context, config, targets, **kwargs: (_ for _ in ()).throw(AssertionError('interactive layout should be skipped')),
    )
    monkeypatch.setattr('ccbd.start_flow.cleanup_project_tmux_orphans_by_socket', lambda **kwargs: (_ for _ in ()).throw(AssertionError('cleanup should be skipped')))
    monkeypatch.setattr(
        'ccbd.start_flow.TmuxCleanupHistoryStore',
        lambda paths: SimpleNamespace(append=lambda event: (_ for _ in ()).throw(AssertionError('cleanup history should be skipped'))),
    )
    monkeypatch.setattr('ccbd.start_flow.resolve_agent_binding', lambda **kwargs: None)
    monkeypatch.setattr(
        'ccbd.start_flow.ensure_agent_runtime',
        lambda *args, **kwargs: RuntimeLaunchResult(
            launched=True,
            binding=AgentBinding(
                runtime_ref='tmux:%777',
                session_ref='session-777',
                provider='codex',
                runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
                runtime_pid=777,
                session_file=str(project_root / '.ccb' / 'demo.session.json'),
                session_id='session-777',
                tmux_socket_name='sock-a',
                tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
                terminal='tmux',
                pane_id='%777',
                active_pane_id='%777',
                pane_title_marker='CCB-demo',
                pane_state='alive',
            ),
        ),
    )

    summary = app.runtime_supervisor.start(
        agent_names=('demo',),
        restore=False,
        auto_permission=False,
        cleanup_tmux_orphans=False,
        interactive_tmux_layout=False,
    )

    assert summary.started == ('demo',)
    assert summary.cleanup_summaries == ()
    runtime = app.registry.get('demo')
    assert runtime is not None
    assert runtime.runtime_ref == 'tmux:%777'
    assert runtime.tmux_socket_path == str(app.paths.ccbd_tmux_socket_path)


def test_runtime_supervisor_start_persists_startup_report(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-ccbd-start-report'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('cmd; demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    app.lease = app.mount_manager.mark_mounted(
        project_id=app.project_id,
        pid=4321,
        socket_path=app.paths.ccbd_socket_path,
        generation=3,
        config_signature=str(app.config_identity['config_signature']),
    )
    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda: SimpleNamespace(
            tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
            tmux_session_name=app.paths.ccbd_tmux_session_name,
            namespace_epoch=7,
        ),
    )
    monkeypatch.setattr('ccbd.start_flow.TmuxBackend', _FakeNamespaceTmuxBackend)

    monkeypatch.setattr('ccbd.start_flow.set_tmux_ui_active', lambda active: None)
    monkeypatch.setattr(
        'ccbd.start_flow.prepare_tmux_start_layout',
        lambda context, config, targets, **kwargs: SimpleNamespace(cmd_pane_id=None, agent_panes={}),
    )
    monkeypatch.setattr('ccbd.start_flow.cleanup_project_tmux_orphans_by_socket', lambda **kwargs: ())
    monkeypatch.setattr(
        'ccbd.start_flow.TmuxCleanupHistoryStore',
        lambda paths: SimpleNamespace(append=lambda event: None),
    )
    monkeypatch.setattr('ccbd.start_flow.resolve_agent_binding', lambda **kwargs: None)
    monkeypatch.setattr(
        'ccbd.start_flow.ensure_agent_runtime',
        lambda *args, **kwargs: RuntimeLaunchResult(
            launched=True,
            binding=AgentBinding(
                runtime_ref='tmux:%880',
                session_ref='session-880',
                provider='codex',
                runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
                runtime_pid=880,
                session_file=str(project_root / '.ccb' / 'demo.session.json'),
                session_id='session-880',
                tmux_socket_name='sock-a',
                tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
                terminal='tmux',
                pane_id='%880',
                active_pane_id='%880',
                pane_title_marker='CCB-demo',
                pane_state='alive',
            ),
        ),
    )

    summary = app.runtime_supervisor.start(
        agent_names=('demo',),
        restore=False,
        auto_permission=False,
        cleanup_tmux_orphans=False,
        interactive_tmux_layout=False,
        startup_run_id='start_' + 'd' * 32,
        daemon_started=True,
    )
    report = CcbdStartupReportStore(app.paths).load()

    assert summary.started == ('demo',)
    assert report is not None
    assert report.trigger == 'start_command'
    assert report.status == 'ok'
    assert report.daemon_generation == 3
    assert report.requested_agents == ('demo',)
    assert report.daemon_started is True
    assert report.startup_run_id == 'start_' + 'd' * 32
    assert report.socket_placement is not None
    assert report.socket_placement['effective_socket_path'] == str(app.paths.ccbd_socket_path)
    assert report.socket_placement['socket_root_kind'] == app.paths.ccbd_socket_placement.root_kind
    assert report.socket_placement['tmux_effective_socket_path'] == str(app.paths.ccbd_tmux_socket_path)
    assert report.socket_placement['tmux_socket_root_kind'] == app.paths.ccbd_tmux_socket_placement.root_kind
    assert report.actions_taken == (
        f'ensure_namespace:epoch=7,session={app.paths.ccbd_tmux_session_name}',
        'launch_runtime:demo',
    )
    assert len(report.agent_results) == 1
    assert report.agent_results[0].agent_name == 'demo'
    assert report.agent_results[0].action == 'launched'
    assert report.agent_results[0].provider_prepare_count == 1
    assert report.agent_results[0].duration_ms is not None
    assert report.agent_results[0].timings_ms is not None
    assert set(report.agent_results[0].timings_ms) == {
        'prepare_launch_context',
        'build_start_cmd',
        'tmux_respawn',
        'pane_identity',
        'pane_agent_report',
        'session_write',
        'provider_post_launch',
        'binding_resolve',
        'pane_and_runtime_facts',
        'authority_commit',
        'restore_bookkeeping',
        'unattributed',
    }
    assert sum(report.agent_results[0].timings_ms.values()) == pytest.approx(
        report.agent_results[0].duration_ms
    )
    assert report.timings_ms is not None
    assert report.timings_ms['namespace_ensure'] >= 0
    assert report.timings_ms['agent_prepare_and_classify'] >= 0
    assert report.timings_ms['agent_runtime_duration_sum'] >= 0
    assert report.timings_ms['agent_runtime_prepare_launch_context'] >= 0
    assert report.timings_ms['agent_runtime_build_start_cmd'] >= 0
    assert report.timings_ms['agent_runtime_tmux_respawn'] >= 0
    assert report.timings_ms['agent_runtime_pane_identity'] >= 0
    assert report.timings_ms['agent_runtime_pane_agent_report'] >= 0
    assert report.timings_ms['agent_runtime_session_write'] >= 0
    assert report.timings_ms['agent_runtime_provider_post_launch'] >= 0
    assert report.timings_ms['agent_runtime_binding_resolve'] >= 0
    assert report.timings_ms['agent_runtime_pane_and_runtime_facts'] >= 0
    assert report.timings_ms['agent_runtime_authority_commit'] >= 0
    assert report.timings_ms['agent_runtime_restore_bookkeeping'] >= 0
    assert report.timings_ms['agent_runtime_unattributed'] >= 0
    assert report.timings_ms['agent_runtime_loop_overhead'] >= 0
    assert report.timings_ms['supervisor_total'] >= 0


def test_runtime_supervisor_start_defers_provider_runtime_for_herdr_namespace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo-ccbd-start-herdr-deferred'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('cmd; demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    app.lease = app.mount_manager.mark_mounted(
        project_id=app.project_id,
        pid=4321,
        socket_path=app.paths.ccbd_socket_path,
        generation=4,
        config_signature=str(app.config_identity['config_signature']),
    )
    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda **kwargs: SimpleNamespace(
            tmux_socket_path='',
            tmux_session_name='ccb-herdr-session',
            namespace_epoch=11,
            workspace_window_name='main',
            workspace_window_id='win-herdr-main',
            workspace_epoch=2,
            ui_attachable=True,
            namespace_backend_family='herdr-native',
            backend_impl='herdr',
            created_this_call=False,
            workspace_recreated_this_call=False,
        ),
    )
    monkeypatch.setattr('ccbd.start_flow.TmuxBackend', _FakeNamespaceTmuxBackend)
    monkeypatch.setattr('ccbd.start_flow.set_tmux_ui_active', lambda active: None)
    monkeypatch.setattr(
        'ccbd.start_flow.prepare_tmux_start_layout',
        lambda context, config, targets, **kwargs: SimpleNamespace(cmd_pane_id=None, agent_panes={}),
    )
    monkeypatch.setattr('ccbd.start_flow.cleanup_project_tmux_orphans_by_socket', lambda **kwargs: ())
    monkeypatch.setattr(
        'ccbd.start_flow.TmuxCleanupHistoryStore',
        lambda paths: SimpleNamespace(append=lambda event: None),
    )
    monkeypatch.setattr('ccbd.start_flow.resolve_agent_binding', lambda **kwargs: None)

    def fail_if_provider_runtime_starts(*args, **kwargs):
        del args, kwargs
        raise AssertionError('provider runtime should be deferred for Herdr namespace')

    monkeypatch.setattr('ccbd.start_flow.ensure_agent_runtime', fail_if_provider_runtime_starts)
    rpc_accepted_ns = time.perf_counter_ns()
    readiness_recorder = StartupReadinessRecorder.from_rpc_payload(
        {
            'schema_version': 1,
            'trace_id': 'trace_' + 'b' * 32,
            'origin_monotonic_ns': rpc_accepted_ns - 5_000_000,
            'attach_mode': 'no_attach',
            'expected_daemon_generation': int(app.lease.generation),
            'keeper_startup_id': None,
            'T1_lifecycle_intent': {
                'status': 'not_required_already_mounted',
                'elapsed_ms': None,
                'source': 'test_existing_generation',
            },
            'T2_control_plane_ready': {
                'status': 'reached',
                'elapsed_ms': 1.0,
                'source': 'test_control_plane',
            },
        },
        now_ns=rpc_accepted_ns,
    )
    assert readiness_recorder is not None
    readiness_recorder.mark(
        'rpc_accepted',
        source='test_start_handler',
        now_ns=rpc_accepted_ns,
    )

    summary = app.runtime_supervisor.start(
        agent_names=('demo',),
        restore=False,
        auto_permission=False,
        cleanup_tmux_orphans=False,
        interactive_tmux_layout=False,
        startup_run_id='start_' + 'h' * 32,
        daemon_started=True,
        readiness_recorder=readiness_recorder,
    )
    report = CcbdStartupReportStore(app.paths).load()

    assert summary.started == ('demo',)
    assert 'provider_runtime_deferred_on_herdr:demo' in summary.actions_taken
    assert len(summary.agent_results) == 1
    deferred = summary.agent_results[0]
    assert deferred.agent_name == 'demo'
    assert deferred.provider == 'codex'
    assert deferred.action == 'deferred'
    assert deferred.health == 'deferred'
    assert deferred.terminal_backend == 'herdr'
    assert deferred.failure_reason == 'deferred_to_provider_runtime_on_herdr'
    assert deferred.workspace_path == str(project_root)
    assert report is not None
    assert report.status == 'ok'
    assert report.readiness_timeline['timeline_complete'] is False
    assert report.readiness_timeline['points']['T4_requested_agents_ready']['status'] == 'not_reached_at_rpc_return'
    assert report.readiness_timeline['points']['T4_requested_agents_ready']['source'] == 'ccbd_start_flow_provider_runtime_deferred_on_herdr'
    assert report.readiness_timeline['points']['T6_fully_warm']['status'] == 'not_reached_at_rpc_return'
    assert report.readiness_timeline['points']['T6_fully_warm']['source'] == 'ccbd_start_flow_provider_runtime_deferred_on_herdr'
    assert report.agent_results[0].action == 'deferred'
    assert report.agent_results[0].terminal_backend == 'herdr'
    assert report.agent_results[0].failure_reason == 'deferred_to_provider_runtime_on_herdr'


def test_runtime_supervisor_starts_provider_runtime_for_herdr_assigned_pane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo-ccbd-start-herdr-provider'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    app.lease = app.mount_manager.mark_mounted(
        project_id=app.project_id,
        pid=4321,
        socket_path=app.paths.ccbd_socket_path,
        generation=4,
        config_signature=str(app.config_identity['config_signature']),
    )
    app.project_namespace._last_materialized_agent_panes = {'demo': 'herdr-pane-1'}
    app.project_namespace._last_topology_pane_records = {
        'herdr-pane-1': SimpleNamespace(
            pane_id='herdr-pane-1',
            session_name='ccb-herdr-session',
            ccb_window='main',
            window_name='main',
        )
    }
    app.project_namespace._last_topology_active_panes = ('herdr-pane-1',)
    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda **kwargs: SimpleNamespace(
            tmux_socket_path='',
            tmux_session_name='ccb-herdr-session',
            namespace_session_name='ccb-herdr-session',
            namespace_id='workspace-1',
            namespace_ipc_kind='herdr_socket',
            namespace_ipc_ref='herdr://local',
            namespace_restore_token='ccb-herdr-session::workspace-1',
            namespace_epoch=11,
            workspace_window_name='main',
            workspace_window_id='win-herdr-main',
            workspace_epoch=2,
            ui_attachable=True,
            namespace_backend_family='herdr-native',
            backend_impl='herdr',
            created_this_call=False,
            workspace_recreated_this_call=False,
        ),
    )
    monkeypatch.setattr('ccbd.start_flow.TmuxBackend', _FakeNamespaceTmuxBackend)
    monkeypatch.setattr('ccbd.start_flow.set_tmux_ui_active', lambda active: None)
    monkeypatch.setattr('ccbd.start_flow.cleanup_project_tmux_orphans_by_socket', lambda **kwargs: ())
    monkeypatch.setattr(
        'ccbd.start_flow.TmuxCleanupHistoryStore',
        lambda paths: SimpleNamespace(append=lambda event: None),
    )
    monkeypatch.setattr('ccbd.start_flow.resolve_agent_binding', lambda **kwargs: None)
    observed: dict[str, object] = {}

    def launch_provider_runtime(*args, **kwargs):
        del args
        observed.update(kwargs)
        return RuntimeLaunchResult(
            launched=True,
            binding=AgentBinding(
                runtime_ref='herdr:herdr-pane-1',
                session_ref='session-herdr-1',
                provider='codex',
                runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
                runtime_pid=901,
                session_file=str(project_root / '.ccb' / 'demo.session.json'),
                session_id='session-herdr-1',
                terminal='herdr',
                pane_id='herdr-pane-1',
                active_pane_id='herdr-pane-1',
                pane_title_marker='CCB-demo',
                pane_state='alive',
            ),
        )

    monkeypatch.setattr('ccbd.start_flow.ensure_agent_runtime', launch_provider_runtime)

    summary = app.runtime_supervisor.start(
        agent_names=('demo',),
        restore=False,
        auto_permission=False,
        cleanup_tmux_orphans=False,
        interactive_tmux_layout=True,
        startup_run_id='start_' + 'p' * 32,
        daemon_started=True,
    )

    assert summary.started == ('demo',)
    assert not any(action.startswith('provider_runtime_deferred_on_herdr') for action in summary.actions_taken)
    assert observed['namespace_backend_impl'] == 'herdr'
    assert observed['assigned_pane_ref'] == {
        'backend_impl': 'herdr',
        'pane_id': 'herdr-pane-1',
        'session_name': 'ccb-herdr-session',
        'window_name': 'main',
        'agent_slug': 'demo',
    }
    assert observed['namespace_ref'] == {
        'backend_family': 'herdr-native',
        'backend_impl': 'herdr',
        'namespace_id': 'workspace-1',
        'session_name': 'ccb-herdr-session',
        'ipc_kind': 'herdr_socket',
        'ipc_ref': 'herdr://local',
        'restore_token': 'ccb-herdr-session::workspace-1',
    }
    assert summary.agent_results[0].terminal_backend == 'herdr'


def test_runtime_supervisor_failure_report_preserves_start_correlation(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-ccbd-start-failed-report'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    app.runtime_supervisor._project_namespace = None

    def fail_start(**kwargs):
        del kwargs
        raise RuntimeError('planned start failure')

    monkeypatch.setattr('ccbd.supervisor.run_start_flow', fail_start)

    with pytest.raises(RuntimeError, match='planned start failure'):
        app.runtime_supervisor.start(
            agent_names=('demo',),
            restore=False,
            auto_permission=False,
            cleanup_tmux_orphans=False,
            interactive_tmux_layout=False,
            startup_run_id='start_' + 'e' * 32,
            daemon_started=False,
        )

    report = CcbdStartupReportStore(app.paths).load()
    assert report is not None
    assert report.status == 'failed'
    assert report.failure_reason == 'planned start failure'
    assert report.startup_run_id == 'start_' + 'e' * 32
    assert report.daemon_started is False


def test_runtime_supervisor_failure_report_preserves_structured_agent_timing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo-ccbd-agent-launch-failed-report'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('cmd; demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda: SimpleNamespace(
            tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
            tmux_session_name=app.paths.ccbd_tmux_session_name,
            namespace_epoch=7,
        ),
    )
    monkeypatch.setattr('ccbd.start_flow.TmuxBackend', _FakeNamespaceTmuxBackend)
    monkeypatch.setattr('ccbd.start_flow.set_tmux_ui_active', lambda active: None)
    monkeypatch.setattr(
        'ccbd.start_flow.prepare_tmux_start_layout',
        lambda context, config, targets, **kwargs: SimpleNamespace(cmd_pane_id=None, agent_panes={}),
    )
    monkeypatch.setattr('ccbd.start_flow.cleanup_project_tmux_orphans_by_socket', lambda **kwargs: ())
    monkeypatch.setattr(
        'ccbd.start_flow.TmuxCleanupHistoryStore',
        lambda paths: SimpleNamespace(append=lambda event: None),
    )
    monkeypatch.setattr('ccbd.start_flow.resolve_agent_binding', lambda **kwargs: None)
    original_error = RuntimeError('planned agent launch failure')

    def fail_launch(*args, **kwargs):
        del args, kwargs
        time.sleep(0.01)
        setattr(original_error, 'ccb_startup_timings_ms', {'build_start_cmd': 5.0})
        raise original_error

    monkeypatch.setattr('ccbd.start_flow.ensure_agent_runtime', fail_launch)

    with pytest.raises(RuntimeError, match='planned agent launch failure') as captured:
        app.runtime_supervisor.start(
            agent_names=('demo',),
            restore=False,
            auto_permission=False,
            cleanup_tmux_orphans=False,
            interactive_tmux_layout=False,
            startup_run_id='start_' + 'f' * 32,
            daemon_started=False,
        )

    assert captured.value is original_error
    report = CcbdStartupReportStore(app.paths).load()
    assert report is not None
    assert report.status == 'failed'
    assert report.failure_reason == 'planned agent launch failure'
    assert report.startup_run_id == 'start_' + 'f' * 32
    assert len(report.agent_results) == 1
    failed = report.agent_results[0]
    assert failed.agent_name == 'demo'
    assert failed.provider == 'codex'
    assert failed.action == 'failed'
    assert failed.health == 'failed'
    assert failed.failure_reason == 'planned agent launch failure'
    assert failed.duration_ms is not None
    assert failed.timings_ms is not None
    assert failed.timings_ms['build_start_cmd'] == 5.0
    assert sum(failed.timings_ms.values()) <= failed.duration_ms


def test_runtime_supervisor_start_passes_visible_layout_signature_to_namespace(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-layout-signature-pass'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('cmd, agent1:codex; agent2:codex, agent3:claude\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    seen: dict[str, object] = {}

    class FakeProjectNamespace:
        def ensure(self, *, layout_signature=None, force_recreate=False, recreate_reason=None):
            seen['layout_signature'] = layout_signature
            seen['force_recreate'] = force_recreate
            seen['recreate_reason'] = recreate_reason
            return SimpleNamespace(
                tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
                tmux_session_name=app.paths.ccbd_tmux_session_name,
                namespace_epoch=9,
                created_this_call=False,
            )

    monkeypatch.setattr(app.runtime_supervisor, '_project_namespace', FakeProjectNamespace())
    monkeypatch.setattr(
        'ccbd.supervisor.run_start_flow',
        lambda **kwargs: StartFlowSummary(
            project_root=str(project_root),
            project_id=app.project_id,
            started=('agent1', 'agent2', 'agent3'),
            socket_path=str(app.paths.ccbd_socket_path),
        ),
    )

    app.runtime_supervisor.start(
        agent_names=('agent1', 'agent2', 'agent3'),
        restore=False,
        auto_permission=False,
        interactive_tmux_layout=True,
    )

    assert seen['layout_signature'] == app.runtime_supervisor._config.topology_signature
    assert seen['force_recreate'] is False
    assert seen['recreate_reason'] is None


def test_runtime_supervisor_start_uses_namespace_topology_for_herdr_compact_layout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo-herdr-compact-topology'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('cmd; agent_2:codex, agent_1:claude\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    seen: list[dict[str, object]] = []
    start_calls: list[dict[str, object]] = []
    monkeypatch.delenv('CCB_HERDR_CAPABILITY_REPORT', raising=False)
    monkeypatch.delenv('CCB_HERDR_SOCKET_REF', raising=False)

    class FakeProjectNamespace:
        _last_materialized_agent_panes = {'agent_2': 'w1:p3', 'agent_1': 'w1:p4'}
        _last_materialized_cmd_pane = 'w1:p2'
        _last_topology_pane_records = {}
        _last_topology_active_panes = ('w1:p2', 'w1:p3', 'w1:p4')

        def ensure(self, *, layout_signature=None, topology_plan=None, force_recreate=False, recreate_reason=None):
            seen.append(
                {
                    'layout_signature': layout_signature,
                    'topology_plan': topology_plan,
                    'force_recreate': force_recreate,
                    'recreate_reason': recreate_reason,
                }
            )
            return SimpleNamespace(
                tmux_socket_path='',
                tmux_session_name='ccb-herdr-session',
                namespace_session_name='ccb-herdr-session',
                namespace_epoch=12,
                namespace_backend_family='herdr-native',
                backend_impl='herdr',
                namespace_id='w1',
                namespace_ipc_kind='herdr_socket',
                namespace_ipc_ref='herdr://local',
                namespace_restore_token='ccb-herdr-session::w1',
                workspace_window_name='main',
                workspace_window_id='w1',
                workspace_epoch=1,
                created_this_call=False,
                workspace_recreated_this_call=False,
            )

    monkeypatch.setattr(app.runtime_supervisor, '_project_namespace', FakeProjectNamespace())
    monkeypatch.setattr(
        'ccbd.supervisor.run_start_flow',
        lambda **kwargs: start_calls.append(dict(kwargs)) or StartFlowSummary(
            project_root=str(project_root),
            project_id=app.project_id,
            started=('agent_2', 'agent_1'),
            socket_path=str(app.paths.ccbd_socket_path),
        ),
    )

    app.runtime_supervisor.start(
        agent_names=('agent_2', 'agent_1'),
        restore=True,
        auto_permission=True,
        interactive_tmux_layout=True,
        cleanup_tmux_orphans=False,
    )

    assert len(seen) == 1
    assert seen[0]['topology_plan'] is not None
    assert seen[0]['layout_signature'] == seen[0]['topology_plan'].signature
    assert seen[0]['force_recreate'] is False
    assert seen[0]['recreate_reason'] is None
    assert start_calls[0]['namespace_agent_panes'] == {'agent_2': 'w1:p3', 'agent_1': 'w1:p4'}
    assert start_calls[0]['namespace_cmd_pane'] == 'w1:p2'
    assert start_calls[0]['namespace_topology_managed'] is True
    assert start_calls[0]['namespace_backend_impl'] == 'herdr'


def test_runtime_supervisor_start_syncs_namespace_epoch_into_lifecycle_authority(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-lifecycle-namespace-sync'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('cmd; demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    app.mount_manager.mark_mounted(
        project_id=app.project_id,
        pid=4321,
        socket_path=app.paths.ccbd_socket_path,
        generation=3,
        config_signature=str(app.config_identity['config_signature']),
    )
    app.lifecycle_store.save(
        build_lifecycle(
            project_id=app.project_id,
            occurred_at='2026-04-22T00:00:00Z',
            desired_state='running',
            phase='mounted',
            generation=3,
            keeper_pid=111,
            owner_pid=4321,
            config_signature=str(app.config_identity['config_signature']),
            socket_path=app.paths.ccbd_socket_path,
            namespace_epoch=None,
        )
    )

    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda **kwargs: SimpleNamespace(
            tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
            tmux_session_name=app.paths.ccbd_tmux_session_name,
            namespace_epoch=9,
            created_this_call=False,
            workspace_recreated_this_call=False,
        ),
    )
    monkeypatch.setattr(
        'ccbd.supervisor.run_start_flow',
        lambda **kwargs: StartFlowSummary(
            project_root=str(project_root),
            project_id=app.project_id,
            started=('demo',),
            socket_path=str(app.paths.ccbd_socket_path),
        ),
    )

    app.runtime_supervisor.start(
        agent_names=('demo',),
        restore=True,
        auto_permission=True,
        interactive_tmux_layout=False,
        cleanup_tmux_orphans=False,
    )

    lifecycle = app.lifecycle_store.load()

    assert lifecycle is not None
    assert lifecycle.generation == 3
    assert lifecycle.namespace_epoch == 9


def test_runtime_supervisor_background_mount_does_not_redefine_namespace_layout_signature(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-layout-signature-background'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('cmd, agent1:codex; agent2:codex, agent3:claude\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    seen: dict[str, object] = {}

    class FakeProjectNamespace:
        def ensure(self, *, layout_signature=None, force_recreate=False, recreate_reason=None):
            seen['layout_signature'] = layout_signature
            seen['force_recreate'] = force_recreate
            seen['recreate_reason'] = recreate_reason
            return SimpleNamespace(
                tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
                tmux_session_name=app.paths.ccbd_tmux_session_name,
                namespace_epoch=10,
                created_this_call=False,
            )

    monkeypatch.setattr(app.runtime_supervisor, '_project_namespace', FakeProjectNamespace())
    monkeypatch.setattr(
        'ccbd.supervisor.run_start_flow',
        lambda **kwargs: StartFlowSummary(
            project_root=str(project_root),
            project_id=app.project_id,
            started=('agent2',),
            socket_path=str(app.paths.ccbd_socket_path),
        ),
    )

    app.runtime_supervisor.start(
        agent_names=('agent2',),
        restore=True,
        auto_permission=True,
        interactive_tmux_layout=False,
        cleanup_tmux_orphans=False,
    )

    assert seen == {
        'layout_signature': None,
        'force_recreate': False,
        'recreate_reason': None,
    }


def test_runtime_supervisor_relabels_reused_project_namespace_pane_by_agent_name(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-ccbd-relabel'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('cmd; demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda: SimpleNamespace(
            tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
            tmux_session_name=app.paths.ccbd_tmux_session_name,
            namespace_epoch=3,
        ),
    )

    relabel_calls: list[dict[str, object]] = []

    class FakeTmuxBackend:
        def __init__(self, *, socket_path: str | None = None):
            self.socket_path = socket_path

        def _tmux_run(self, args, *, capture=False, check=False, input_bytes=None, timeout=None):
            del capture, check, input_bytes, timeout
            if args[:3] == ['display-message', '-p', '-t']:
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"{args[3]}\t{app.paths.ccbd_tmux_session_name}\t0\tagent\tdemo\t{app.project_id}\tccbd\n",
                )
            raise AssertionError(f'unexpected tmux args: {args}')

        def set_pane_title(self, pane_id: str, title: str) -> None:
            del pane_id, title

        def set_pane_user_option(self, pane_id: str, name: str, value: str) -> None:
            del pane_id, name, value

    monkeypatch.setattr('ccbd.start_flow.TmuxBackend', FakeTmuxBackend)
    monkeypatch.setattr(
        'ccbd.start_flow.apply_ccb_pane_identity',
        lambda backend, pane_id, **kwargs: relabel_calls.append(
            {
                'socket_path': getattr(backend, 'socket_path', None),
                'pane_id': pane_id,
                **kwargs,
            }
        ),
    )
    monkeypatch.setattr('ccbd.start_flow.resolve_agent_binding', lambda **kwargs: AgentBinding(
        runtime_ref='tmux:%77',
        session_ref='session-77',
        provider='codex',
        runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
        runtime_pid=77,
        session_file=str(project_root / '.ccb' / '.codex-demo-session'),
        session_id='session-77',
        tmux_socket_name='sock-a',
        tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
        terminal='tmux',
        pane_id='%77',
        active_pane_id='%77',
        pane_title_marker='CCB-demo',
        pane_state='alive',
    ))
    monkeypatch.setattr(
        'ccbd.start_flow.ensure_agent_runtime',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should reuse existing binding')),
    )

    summary = app.runtime_supervisor.start(
        agent_names=('demo',),
        restore=False,
        auto_permission=False,
        cleanup_tmux_orphans=False,
        interactive_tmux_layout=False,
    )

    assert summary.started == ('demo',)
    assert relabel_calls == [
        {
            'socket_path': str(app.paths.ccbd_tmux_socket_path),
            'pane_id': '%77',
            'title': 'demo',
            'agent_label': 'demo',
            'project_id': app.project_id,
                'order_index': 0,
                'slot_key': 'demo',
                'window_name': 'main',
                'namespace_epoch': 3,
                'managed_by': 'ccbd',
            }
    ]
    assert 'relabel_runtime_pane:demo:%77' in summary.actions_taken


def test_runtime_supervisor_relaunches_same_socket_binding_outside_namespace_session(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-ccbd-foreign-session'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('cmd; demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda: SimpleNamespace(
            tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
            tmux_session_name=app.paths.ccbd_tmux_session_name,
            namespace_epoch=5,
        ),
    )

    class FakeTmuxBackend:
        def __init__(self, *, socket_path: str | None = None):
            self.socket_path = socket_path

        def _tmux_run(self, args, *, capture=False, check=False, input_bytes=None, timeout=None):
            del capture, check, input_bytes, timeout
            if args[:3] == ['list-panes', '-t', app.paths.ccbd_tmux_session_name]:
                return SimpleNamespace(stdout='%0\n')
            if args[:3] == ['display-message', '-p', '-t']:
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"{args[3]}\tdetached-demo\t0\tagent\tdemo\t{app.project_id}\tccbd\n",
                )
            raise AssertionError(f'unexpected tmux args: {args}')

    monkeypatch.setattr('ccbd.start_flow.TmuxBackend', FakeTmuxBackend)
    monkeypatch.setattr('ccbd.start_flow.set_tmux_ui_active', lambda active: None)
    monkeypatch.setattr('ccbd.start_flow.cleanup_project_tmux_orphans_by_socket', lambda **kwargs: ())
    monkeypatch.setattr(
        'ccbd.start_flow.TmuxCleanupHistoryStore',
        lambda paths: SimpleNamespace(append=lambda event: None),
    )
    monkeypatch.setattr(
        'ccbd.start_flow.prepare_tmux_start_layout',
        lambda context, config, targets, **kwargs: SimpleNamespace(cmd_pane_id='%0', agent_panes={'demo': '%55'}),
    )
    monkeypatch.setattr('ccbd.start_flow.resolve_agent_binding', lambda **kwargs: AgentBinding(
        runtime_ref='tmux:%77',
        session_ref='session-77',
        provider='codex',
        runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
        runtime_pid=77,
        session_file=str(project_root / '.ccb' / '.codex-demo-session'),
        session_id='session-77',
        tmux_socket_name='sock-a',
        tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
        terminal='tmux',
        pane_id='%77',
        active_pane_id='%77',
        pane_title_marker='CCB-demo',
        pane_state='alive',
    ))
    launch_hints: list[object | None] = []
    monkeypatch.setattr(
        'ccbd.start_flow.ensure_agent_runtime',
        lambda *args, **kwargs: launch_hints.append(args[4]) or RuntimeLaunchResult(
            launched=True,
            binding=AgentBinding(
                runtime_ref='tmux:%55',
                session_ref='session-55',
                provider='codex',
                runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
                runtime_pid=55,
                session_file=str(project_root / '.ccb' / '.codex-demo-session'),
                session_id='session-55',
                tmux_socket_name='sock-a',
                tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
                terminal='tmux',
                pane_id='%55',
                active_pane_id='%55',
                pane_title_marker='CCB-demo',
                pane_state='alive',
            ),
        ),
    )

    summary = app.runtime_supervisor.start(
        agent_names=('demo',),
        restore=False,
        auto_permission=False,
        cleanup_tmux_orphans=False,
        interactive_tmux_layout=True,
    )

    assert summary.started == ('demo',)
    assert launch_hints == [None]
    assert 'prepare_tmux_layout:demo' in summary.actions_taken
    assert 'relaunch_runtime:demo' in summary.actions_taken
    assert 'reuse_binding:demo' not in summary.actions_taken


def test_runtime_supervisor_bootstraps_fresh_cmd_pane_after_layout(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-ccbd-fresh-cmd'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('cmd; demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda: SimpleNamespace(
            tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
            tmux_session_name=app.paths.ccbd_tmux_session_name,
            namespace_epoch=8,
            created_this_call=True,
        ),
    )

    respawn_calls: list[dict[str, object]] = []

    class FakeTmuxBackend:
        def __init__(self, *, socket_path: str | None = None):
            self.socket_path = socket_path

        def _tmux_run(self, args, *, capture=False, check=False, input_bytes=None, timeout=None):
            del capture, check, input_bytes, timeout
            if args[:3] == ['list-panes', '-t', app.paths.ccbd_tmux_session_name]:
                return SimpleNamespace(stdout='%0\n')
            if args[:3] == ['display-message', '-p', '-t']:
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"{args[3]}\t{app.paths.ccbd_tmux_session_name}\t0\tagent\tdemo\t{app.project_id}\tccbd\n",
                )
            raise AssertionError(f'unexpected tmux args: {args}')

        def respawn_pane(self, pane_id: str, *, cmd: str, cwd: str | None = None, stderr_log_path=None, remain_on_exit: bool = True) -> None:
            del stderr_log_path
            respawn_calls.append(
                {
                    'pane_id': pane_id,
                    'cmd': cmd,
                    'cwd': cwd,
                    'remain_on_exit': remain_on_exit,
                    'socket_path': self.socket_path,
                }
            )

        def set_pane_title(self, pane_id: str, title: str) -> None:
            del pane_id, title

        def set_pane_user_option(self, pane_id: str, name: str, value: str) -> None:
            del pane_id, name, value

    monkeypatch.setattr('ccbd.start_flow.TmuxBackend', FakeTmuxBackend)
    monkeypatch.setattr('ccbd.start_flow.set_tmux_ui_active', lambda active: None)
    monkeypatch.setattr(
        'ccbd.start_flow.prepare_tmux_start_layout',
        lambda context, config, targets, **kwargs: SimpleNamespace(cmd_pane_id='%0', agent_panes={'demo': '%55'}),
    )
    monkeypatch.setattr('ccbd.start_flow.cleanup_project_tmux_orphans_by_socket', lambda **kwargs: ())
    monkeypatch.setattr(
        'ccbd.start_flow.TmuxCleanupHistoryStore',
        lambda paths: SimpleNamespace(append=lambda event: None),
    )
    monkeypatch.setenv('SHELL', 'zsh')
    monkeypatch.setattr('ccbd.start_runtime.layout.shutil.which', lambda name: '/mock/bin/zsh' if name == 'zsh' else None)
    monkeypatch.setattr('ccbd.start_flow.resolve_agent_binding', lambda **kwargs: None)
    monkeypatch.setattr(
        'ccbd.start_flow.ensure_agent_runtime',
        lambda *args, **kwargs: RuntimeLaunchResult(
            launched=True,
            binding=AgentBinding(
                runtime_ref='tmux:%55',
                session_ref='session-55',
                provider='codex',
                runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
                runtime_pid=55,
                session_file=str(project_root / '.ccb' / '.codex-demo-session'),
                session_id='session-55',
                tmux_socket_name=None,
                tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
                terminal='tmux',
                pane_id='%55',
                active_pane_id='%55',
                pane_title_marker='CCB-demo',
                pane_state='alive',
            ),
        ),
    )

    summary = app.runtime_supervisor.start(
        agent_names=('demo',),
        restore=False,
        auto_permission=False,
        cleanup_tmux_orphans=False,
        interactive_tmux_layout=True,
    )

    assert summary.started == ('demo',)
    assert respawn_calls == [
        {
            'pane_id': '%0',
            'cmd': 'exec /mock/bin/zsh -l',
            'cwd': str(project_root),
            'remain_on_exit': False,
            'socket_path': str(app.paths.ccbd_tmux_socket_path),
        }
    ]
    assert 'bootstrap_cmd_pane:%0' in summary.actions_taken


def test_runtime_supervisor_project_namespace_cleanup_uses_authoritative_active_panes(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-ccbd-project-cleanup'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('cmd; demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda: SimpleNamespace(
            tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
            tmux_session_name=app.paths.ccbd_tmux_session_name,
            namespace_epoch=9,
            created_this_call=False,
        ),
    )

    class FakeTmuxBackend:
        def __init__(self, *, socket_path: str | None = None):
            self.socket_path = socket_path

        def _tmux_run(self, args, *, capture=False, check=False, input_bytes=None, timeout=None):
            del capture, check, input_bytes, timeout
            if args[:3] == ['list-panes', '-t', app.paths.ccbd_tmux_session_name]:
                return SimpleNamespace(stdout='%0\n')
            if args[:3] == ['display-message', '-p', '-t']:
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"{args[3]}\t{app.paths.ccbd_tmux_session_name}\t0\tagent\tdemo\t{app.project_id}\tccbd\n",
                )
            raise AssertionError(f'unexpected tmux args: {args}')

        def set_pane_title(self, pane_id: str, title: str) -> None:
            del pane_id, title

        def set_pane_user_option(self, pane_id: str, name: str, value: str) -> None:
            del pane_id, name, value

    cleanup_calls: list[dict[str, object]] = []
    history_events: list[object] = []

    monkeypatch.setattr('ccbd.start_flow.TmuxBackend', FakeTmuxBackend)
    monkeypatch.setattr('ccbd.start_flow.set_tmux_ui_active', lambda active: None)
    monkeypatch.setattr(
        'ccbd.start_flow.prepare_tmux_start_layout',
        lambda context, config, targets, **kwargs: SimpleNamespace(cmd_pane_id='%0', agent_panes={'demo': '%55'}),
    )
    monkeypatch.setattr(
        'ccbd.start_flow.cleanup_project_tmux_orphans_by_socket',
        lambda **kwargs: cleanup_calls.append(kwargs) or (
            ProjectTmuxCleanupSummary(
                socket_name=str(app.paths.ccbd_tmux_socket_path),
                owned_panes=('%0', '%55', '%77'),
                active_panes=tuple(kwargs['active_panes_by_socket'][str(app.paths.ccbd_tmux_socket_path)]),
                orphaned_panes=('%77',),
                killed_panes=('%77',),
            ),
        ),
    )
    monkeypatch.setattr(
        'ccbd.start_flow.TmuxCleanupHistoryStore',
        lambda paths: SimpleNamespace(append=lambda event: history_events.append(event)),
    )
    monkeypatch.setattr('ccbd.start_flow.resolve_agent_binding', lambda **kwargs: None)
    monkeypatch.setattr(
        'ccbd.start_flow.ensure_agent_runtime',
        lambda *args, **kwargs: RuntimeLaunchResult(
            launched=True,
            binding=AgentBinding(
                runtime_ref='tmux:%55',
                session_ref='session-55',
                provider='codex',
                runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
                runtime_pid=55,
                session_file=str(project_root / '.ccb' / '.codex-demo-session'),
                session_id='session-55',
                tmux_socket_name=None,
                tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
                terminal='tmux',
                pane_id='%55',
                active_pane_id='%55',
                pane_title_marker='CCB-demo',
                pane_state='alive',
            ),
        ),
    )

    summary = app.runtime_supervisor.start(
        agent_names=('demo',),
        restore=False,
        auto_permission=False,
        cleanup_tmux_orphans=True,
        interactive_tmux_layout=True,
    )

    assert summary.started == ('demo',)
    assert cleanup_calls == [
        {
            'project_id': app.project_id,
            'active_panes_by_socket': {str(app.paths.ccbd_tmux_socket_path): ('%0', '%55')},
        }
    ]
    assert len(history_events) == 1
    assert 'cleanup_tmux_orphans:killed=1' in summary.actions_taken
    assert 'cleanup_tmux_orphans:skipped_project_namespace' not in summary.actions_taken


def test_runtime_supervisor_reuses_agent_only_binding_without_cmd_namespace_match(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-ccbd-agent-only-binding'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda: SimpleNamespace(
            tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
            tmux_session_name=app.paths.ccbd_tmux_session_name,
            namespace_epoch=6,
        ),
    )
    monkeypatch.setattr('ccbd.start_flow.TmuxBackend', _FakeNamespaceTmuxBackend)
    monkeypatch.setattr('ccbd.start_flow.set_tmux_ui_active', lambda active: None)
    layout_targets: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        'ccbd.start_flow.prepare_tmux_start_layout',
        lambda context, config, targets, **kwargs: layout_targets.append(tuple(targets)) or SimpleNamespace(cmd_pane_id=None, agent_panes={}),
    )
    monkeypatch.setattr('ccbd.start_flow.cleanup_project_tmux_orphans_by_socket', lambda **kwargs: ())
    monkeypatch.setattr(
        'ccbd.start_flow.TmuxCleanupHistoryStore',
        lambda paths: SimpleNamespace(append=lambda event: None),
    )
    monkeypatch.setattr(
        'ccbd.start_flow.resolve_agent_binding',
        lambda **kwargs: AgentBinding(
            runtime_ref='tmux:%9',
            session_ref='demo-session-id',
            provider='codex',
            runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
            runtime_pid=9,
            session_file=str(project_root / '.ccb' / '.codex-demo-session'),
            session_id='demo-session-id',
            tmux_socket_name=None,
            tmux_socket_path=None,
            terminal='tmux',
            pane_id='%9',
            active_pane_id='%9',
            pane_title_marker='CCB-demo',
            pane_state='unknown',
        ),
    )
    monkeypatch.setattr(
        'ccbd.start_flow.ensure_agent_runtime',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('agent-only startup should reuse existing binding')),
    )

    summary = app.runtime_supervisor.start(
        agent_names=('demo',),
        restore=False,
        auto_permission=False,
        cleanup_tmux_orphans=False,
        interactive_tmux_layout=True,
    )

    runtime = app.registry.get('demo')
    assert summary.started == ('demo',)
    assert layout_targets == [()]
    assert 'reuse_binding:demo' in summary.actions_taken
    assert runtime is not None
    assert runtime.runtime_ref == 'tmux:%9'
    assert runtime.session_ref == 'demo-session-id'


def test_runtime_supervisor_reuses_agent_only_missing_binding_when_session_file_declares_no_socket(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-ccbd-agent-only-missing-binding'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda: SimpleNamespace(
            tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
            tmux_session_name=app.paths.ccbd_tmux_session_name,
            namespace_epoch=6,
        ),
    )
    monkeypatch.setattr('ccbd.start_flow.TmuxBackend', _FakeNamespaceTmuxBackend)
    monkeypatch.setattr('ccbd.start_flow.set_tmux_ui_active', lambda active: None)
    layout_targets: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        'ccbd.start_flow.prepare_tmux_start_layout',
        lambda context, config, targets, **kwargs: layout_targets.append(tuple(targets)) or SimpleNamespace(cmd_pane_id=None, agent_panes={}),
    )
    monkeypatch.setattr('ccbd.start_flow.cleanup_project_tmux_orphans_by_socket', lambda **kwargs: ())
    monkeypatch.setattr(
        'ccbd.start_flow.TmuxCleanupHistoryStore',
        lambda paths: SimpleNamespace(append=lambda event: None),
    )
    monkeypatch.setattr(
        'ccbd.start_flow.resolve_agent_binding',
        lambda **kwargs: AgentBinding(
            runtime_ref='tmux:%9',
            session_ref='demo-session-id',
            provider='codex',
            runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
            runtime_pid=9,
            session_file=str(project_root / '.ccb' / '.codex-demo-session'),
            session_id='demo-session-id',
            tmux_socket_name=None,
            tmux_socket_path=None,
            terminal='tmux',
            pane_id='%9',
            active_pane_id=None,
            pane_title_marker='CCB-demo',
            pane_state='missing',
            provider_identity_state='unknown',
            provider_identity_reason='pane_pid_unavailable',
        ),
    )
    monkeypatch.setattr(
        'ccbd.start_flow.ensure_agent_runtime',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('agent-only missing binding should remain reusable legacy evidence')),
    )

    summary = app.runtime_supervisor.start(
        agent_names=('demo',),
        restore=False,
        auto_permission=False,
        cleanup_tmux_orphans=False,
        interactive_tmux_layout=True,
    )

    runtime = app.registry.get('demo')
    assert summary.started == ('demo',)
    assert layout_targets == [()]
    assert 'reuse_binding:demo' in summary.actions_taken
    assert runtime is not None
    assert runtime.runtime_ref == 'tmux:%9'
    assert runtime.session_ref == 'demo-session-id'


def test_runtime_supervisor_rejects_agent_only_binding_with_provider_identity_mismatch(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-ccbd-agent-only-mismatch-binding'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda: SimpleNamespace(
            tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
            tmux_session_name=app.paths.ccbd_tmux_session_name,
            namespace_epoch=6,
        ),
    )
    monkeypatch.setattr('ccbd.start_flow.TmuxBackend', _FakeNamespaceTmuxBackend)
    monkeypatch.setattr('ccbd.start_flow.set_tmux_ui_active', lambda active: None)
    monkeypatch.setattr(
        'ccbd.start_flow.prepare_tmux_start_layout',
        lambda context, config, targets, **kwargs: SimpleNamespace(cmd_pane_id=None, agent_panes={'demo': '%10'}),
    )
    monkeypatch.setattr('ccbd.start_flow.cleanup_project_tmux_orphans_by_socket', lambda **kwargs: ())
    monkeypatch.setattr(
        'ccbd.start_flow.TmuxCleanupHistoryStore',
        lambda paths: SimpleNamespace(append=lambda event: None),
    )
    monkeypatch.setattr(
        'ccbd.start_flow.resolve_agent_binding',
        lambda **kwargs: AgentBinding(
            runtime_ref='tmux:%9',
            session_ref='demo-session-id',
            provider='codex',
            runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
            runtime_pid=9,
            session_file=str(project_root / '.ccb' / '.codex-demo-session'),
            session_id='demo-session-id',
            tmux_socket_name=None,
            tmux_socket_path=None,
            terminal='tmux',
            pane_id='%9',
            active_pane_id='%9',
            pane_title_marker='CCB-demo',
            pane_state='alive',
            provider_identity_state='mismatch',
            provider_identity_reason='live_codex_process_not_running_bound_resume_session',
        ),
    )
    launch_hints: list[object | None] = []
    monkeypatch.setattr(
        'ccbd.start_flow.ensure_agent_runtime',
        lambda *args, **kwargs: launch_hints.append(args[4]) or RuntimeLaunchResult(
            launched=True,
            binding=AgentBinding(
                runtime_ref='tmux:%10',
                session_ref='demo-session-id',
                provider='codex',
                runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
                runtime_pid=10,
                session_file=str(project_root / '.ccb' / '.codex-demo-session'),
                session_id='demo-session-id',
                tmux_socket_name=None,
                tmux_socket_path=None,
                terminal='tmux',
                pane_id='%10',
                active_pane_id='%10',
                pane_title_marker='CCB-demo',
                pane_state='alive',
                provider_identity_state='match',
            ),
        ),
    )

    summary = app.runtime_supervisor.start(
        agent_names=('demo',),
        restore=False,
        auto_permission=False,
        cleanup_tmux_orphans=False,
        interactive_tmux_layout=True,
    )

    runtime = app.registry.get('demo')
    assert len(launch_hints) == 1
    assert getattr(launch_hints[0], 'runtime_ref', None) == 'tmux:%9'
    assert getattr(launch_hints[0], 'provider_identity_state', None) == 'mismatch'
    assert 'relaunch_runtime:demo' in summary.actions_taken
    assert runtime is not None
    assert runtime.runtime_ref == 'tmux:%10'


def test_runtime_supervisor_project_namespace_start_does_not_preheal_dead_binding_before_layout(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-ccbd-layout-launch'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('cmd; demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    monkeypatch.setattr(
        app.project_namespace,
        'ensure',
        lambda: SimpleNamespace(
            tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
            tmux_session_name=app.paths.ccbd_tmux_session_name,
            namespace_epoch=4,
        ),
    )
    monkeypatch.setattr('ccbd.start_flow.TmuxBackend', _FakeNamespaceTmuxBackend)
    monkeypatch.setattr('ccbd.start_flow.set_tmux_ui_active', lambda active: None)
    monkeypatch.setattr('ccbd.start_flow.cleanup_project_tmux_orphans_by_socket', lambda **kwargs: ())
    monkeypatch.setattr(
        'ccbd.start_flow.TmuxCleanupHistoryStore',
        lambda paths: SimpleNamespace(append=lambda event: None),
    )

    layout_targets: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        'ccbd.start_flow.prepare_tmux_start_layout',
        lambda context, config, targets, **kwargs: layout_targets.append(tuple(targets)) or SimpleNamespace(cmd_pane_id='%0', agent_panes={'demo': '%2'}),
    )

    def _resolve_agent_binding(**kwargs):
        if kwargs.get('ensure_usable') is not False:
            raise AssertionError('project namespace startup should not call ensure_usable=True before layout assignment')
        return AgentBinding(
            runtime_ref='tmux:%41',
            session_ref='session-41',
            provider='codex',
            runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
            runtime_pid=41,
            session_file=str(project_root / '.ccb' / '.codex-demo-session'),
            session_id='session-41',
            tmux_socket_name='sock-a',
            tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
            terminal='tmux',
            pane_id='%41',
            active_pane_id=None,
            pane_title_marker='CCB-demo',
            pane_state='dead',
        )

    monkeypatch.setattr('ccbd.start_flow.resolve_agent_binding', _resolve_agent_binding)
    launch_binding_hints: list[object | None] = []
    monkeypatch.setattr(
        'ccbd.start_flow.ensure_agent_runtime',
        lambda *args, **kwargs: launch_binding_hints.append(args[4]) or RuntimeLaunchResult(
            launched=True,
            binding=AgentBinding(
                runtime_ref='tmux:%55',
                session_ref='session-55',
                provider='codex',
                runtime_root=str(app.paths.agent_provider_runtime_dir('demo', 'codex')),
                runtime_pid=55,
                session_file=str(project_root / '.ccb' / '.codex-demo-session'),
                session_id='session-55',
                tmux_socket_name='sock-a',
                tmux_socket_path=str(app.paths.ccbd_tmux_socket_path),
                terminal='tmux',
                pane_id='%55',
                active_pane_id='%55',
                pane_title_marker='CCB-demo',
                pane_state='alive',
            ),
        ),
    )

    summary = app.runtime_supervisor.start(
        agent_names=('demo',),
        restore=False,
        auto_permission=False,
        cleanup_tmux_orphans=False,
        interactive_tmux_layout=True,
    )

    assert summary.started == ('demo',)
    assert layout_targets == [('demo',)]
    assert launch_binding_hints == [None]
    assert 'prepare_tmux_layout:demo' in summary.actions_taken


def test_ccbd_start_listens_and_self_probes_before_mounted_publish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo-ccbd-start-order'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    observed: dict[str, object] = {'events': []}
    original_listen = app.socket_server.listen
    original_probe = app.socket_server.bootstrap_readiness_probe

    def fake_listen() -> None:
        observed['events'].append('listen')
        observed['lease_during_listen'] = app.mount_manager.load_state()
        lifecycle = app.lifecycle_store.load()
        observed['phase_during_listen'] = lifecycle.phase if lifecycle is not None else None
        original_listen()

    @contextmanager
    def tracked_probe(*, timeout_s: float):
        observed['events'].append('probe_enter')
        observed['lease_before_probe'] = app.mount_manager.load_state()
        lifecycle = app.lifecycle_store.load()
        observed['phase_before_probe'] = lifecycle.phase if lifecycle is not None else None
        with original_probe(timeout_s=timeout_s) as payload:
            observed['events'].append('probe_verified')
            observed['lease_after_probe'] = app.mount_manager.load_state()
            lifecycle = app.lifecycle_store.load()
            observed['phase_after_probe'] = lifecycle.phase if lifecycle is not None else None
            yield payload
            observed['events'].append('mounted_publish')
            observed['lease_after_publish'] = app.mount_manager.load_state()
            lifecycle = app.lifecycle_store.load()
            observed['phase_after_publish'] = lifecycle.phase if lifecycle is not None else None
            observed['stage_after_publish'] = lifecycle.startup_stage if lifecycle is not None else None

    monkeypatch.setattr(app.socket_server, 'listen', fake_listen)
    monkeypatch.setattr(app.socket_server, 'bootstrap_readiness_probe', tracked_probe)
    monkeypatch.setattr(app.dispatcher, 'restore_running_jobs', lambda: None)
    monkeypatch.setattr(app.dispatcher, 'last_restore_report', lambda **kwargs: None)
    monkeypatch.setattr(app.restore_report_store, 'save', lambda report: None)

    lease = app.start()
    mounted = observed.get('lease_after_publish')
    thread_errors: list[BaseException] = []

    assert observed['events'] == ['listen', 'probe_enter', 'probe_verified', 'mounted_publish']
    assert observed['lease_during_listen'] is None
    assert observed['phase_during_listen'] is None
    assert observed['lease_before_probe'] is None
    assert observed['phase_before_probe'] == 'starting'
    assert observed['lease_after_probe'] is None
    assert observed['phase_after_probe'] == 'starting'
    assert mounted is not None
    assert mounted.mount_state.value == 'mounted'
    assert mounted.generation == lease.generation
    assert observed['phase_after_publish'] == 'starting'
    assert observed['stage_after_publish'] == 'runtime_bootstrap'
    assert app.mount_manager.load_state().mount_state.value == 'mounted'
    lifecycle = app.lifecycle_store.load()
    assert lifecycle is not None
    assert lifecycle.phase == 'starting'
    assert lifecycle.startup_stage == 'runtime_bootstrap'
    with pytest.raises(CcbdClientError, match='timed out'):
        CcbdClient(app.paths.ccbd_socket_path, timeout_s=0.1).ping('ccbd')

    def run_server() -> None:
        try:
            app.serve_forever(poll_interval=0.01)
        except BaseException as exc:
            thread_errors.append(exc)

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    try:
        _wait_for(app.paths.ccbd_socket_path)
        lifecycle = app.lifecycle_store.load()
        assert lifecycle is not None
        assert lifecycle.phase == 'mounted'
        assert lifecycle.startup_stage == 'mounted'
    finally:
        app.request_shutdown()
        server_thread.join(timeout=3.0)

    assert server_thread.is_alive() is False
    assert thread_errors == []


def test_ccbd_release_unlinks_owned_socket_inside_startup_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo-ccbd-release-socket-lock'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    app.start()
    original_lock = app.ownership_guard.startup_lock
    original_request_shutdown = app.socket_server.request_shutdown
    lock_active = False
    calls: list[tuple[bool, tuple[int, int] | None]] = []

    @contextmanager
    def tracked_lock():
        nonlocal lock_active
        with original_lock():
            lock_active = True
            try:
                yield
            finally:
                lock_active = False

    def tracked_request_shutdown() -> None:
        calls.append((lock_active, app.socket_server._bound_socket_stat))
        original_request_shutdown()

    monkeypatch.setattr(app.ownership_guard, 'startup_lock', tracked_lock)
    monkeypatch.setattr(app.socket_server, 'request_shutdown', tracked_request_shutdown)

    app.request_shutdown()

    assert calls
    assert all(active for active, identity in calls if identity is not None)
    assert not app.paths.ccbd_socket_path.exists()


def test_ccbd_same_process_restart_advances_unmounted_predecessor_generation(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / 'repo-ccbd-same-process-restart'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    errors: list[BaseException] = []

    def serve(app: CcbdApp) -> threading.Thread:
        def run() -> None:
            try:
                app.serve_forever(poll_interval=0.01)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        _wait_for(app.paths.ccbd_socket_path)
        return thread

    first = CcbdApp(project_root)
    first_thread = serve(first)
    first_lease = first.mount_manager.load_state()
    assert first_lease is not None
    first.request_shutdown()
    first_thread.join(timeout=3.0)

    second = CcbdApp(project_root)
    assert second.pid == first.pid
    assert second.daemon_instance_id != first.daemon_instance_id
    second_thread = serve(second)
    try:
        second_lease = second.mount_manager.load_state()
        lifecycle = second.lifecycle_store.load()
        assert second_lease is not None
        assert second_lease.generation == first_lease.generation + 1
        assert lifecycle is not None
        assert lifecycle.phase == 'mounted'
        assert lifecycle.startup_stage == 'mounted'
        assert lifecycle.generation == second_lease.generation
    finally:
        second.request_shutdown()
        second_thread.join(timeout=3.0)

    assert first_thread.is_alive() is False
    assert second_thread.is_alive() is False
    assert errors == []


def test_ccbd_serve_forever_accepts_ping_while_runtime_bootstrap_is_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo-ccbd-continuous-accept'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    restore_entered = threading.Event()
    release_restore = threading.Event()
    thread_errors: list[BaseException] = []

    def blocked_restore() -> None:
        restore_entered.set()
        if not release_restore.wait(timeout=3.0):
            raise TimeoutError('test did not release runtime bootstrap')

    def run_server() -> None:
        try:
            app.serve_forever(poll_interval=0.05)
        except BaseException as exc:
            thread_errors.append(exc)

    monkeypatch.setattr(app.dispatcher, 'restore_running_jobs', blocked_restore)
    monkeypatch.setattr('ccbd.app_runtime.lifecycle.maybe_start_runtime_accelerator', lambda root: None)
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    try:
        assert restore_entered.wait(timeout=2.0)
        lifecycle = app.lifecycle_store.load()
        lease = app.mount_manager.load_state()
        assert lifecycle is not None
        assert lifecycle.phase == 'starting'
        assert lifecycle.startup_stage == 'runtime_bootstrap'
        assert lease is not None
        assert lease.mount_state.value == 'mounted'

        payload = CcbdClient(app.paths.ccbd_socket_path, timeout_s=1.0).ping('ccbd')
        assert payload['mount_state'] == 'starting'
        assert payload['diagnostics']['startup_stage'] == 'runtime_bootstrap'

        release_restore.set()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            lifecycle = app.lifecycle_store.load()
            if lifecycle is not None and lifecycle.startup_stage == 'mounted':
                break
            time.sleep(0.01)
        else:
            raise AssertionError('runtime bootstrap did not publish final mounted stage')

        payload = CcbdClient(app.paths.ccbd_socket_path, timeout_s=1.0).ping('ccbd')
        assert app.socket_server._runtime_bootstrap_active is False
        assert payload['diagnostics']['startup_stage'] == 'mounted'
    finally:
        release_restore.set()
        app.request_shutdown()
        server_thread.join(timeout=3.0)

    assert server_thread.is_alive() is False
    assert thread_errors == []


def test_ccbd_final_mounted_publish_opens_request_gate_atomically(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo-ccbd-atomic-mounted-gate'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    mounted_persisted = threading.Event()
    allow_gate_open = threading.Event()
    request_waiting_on_gate = threading.Event()
    thread_errors: list[BaseException] = []
    request_results: list[dict] = []
    original_save = app.lifecycle_store.save

    def block_after_mounted_persist(lifecycle) -> None:
        original_save(lifecycle)
        if lifecycle.phase == 'mounted' and lifecycle.startup_stage == 'mounted':
            mounted_persisted.set()
            if not allow_gate_open.wait(timeout=3.0):
                raise TimeoutError('test did not release final readiness publication')

    def run_server() -> None:
        try:
            app.serve_forever(poll_interval=0.01)
        except BaseException as exc:
            thread_errors.append(exc)

    def request_ready() -> None:
        try:
            request_results.append(
                CcbdClient(app.paths.ccbd_socket_path, timeout_s=2.0).request('test-ready')
            )
        except BaseException as exc:
            thread_errors.append(exc)

    app.socket_server._bootstrap_gate_lock = _TrackedGateLock(request_waiting_on_gate)
    app.socket_server.register_handler('test-ready', lambda payload: {'ready': True})
    monkeypatch.setattr(app.lifecycle_store, 'save', block_after_mounted_persist)
    monkeypatch.setattr('ccbd.app_runtime.lifecycle.maybe_start_runtime_accelerator', lambda root: None)

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    request_thread = None
    try:
        assert mounted_persisted.wait(timeout=2.0)
        lifecycle = app.lifecycle_store.load()
        assert lifecycle is not None
        assert lifecycle.phase == 'mounted'
        assert lifecycle.startup_stage == 'mounted'
        assert app.socket_server._runtime_bootstrap_active is True

        request_thread = threading.Thread(target=request_ready, daemon=True)
        request_thread.start()
        assert request_waiting_on_gate.wait(timeout=2.0)
        assert request_thread.is_alive() is True
        assert request_results == []

        allow_gate_open.set()
        request_thread.join(timeout=2.0)
        assert request_thread.is_alive() is False
        assert request_results == [{'ready': True}]
        assert app.socket_server._runtime_bootstrap_active is False
    finally:
        allow_gate_open.set()
        if request_thread is not None:
            request_thread.join(timeout=2.0)
        app.request_shutdown()
        server_thread.join(timeout=3.0)

    assert server_thread.is_alive() is False
    assert thread_errors == []


def test_ccbd_failed_final_publish_never_opens_request_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo-ccbd-failed-mounted-gate'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    mounted_replaced = threading.Event()
    allow_publish_failure = threading.Event()
    request_waiting_on_gate = threading.Event()
    handler_calls: list[dict] = []
    server_errors: list[BaseException] = []
    request_errors: list[BaseException] = []
    original_save = app.lifecycle_store.save

    def fail_after_mounted_replace(lifecycle) -> None:
        original_save(lifecycle)
        if lifecycle.phase == 'mounted' and lifecycle.startup_stage == 'mounted':
            mounted_replaced.set()
            if not allow_publish_failure.wait(timeout=3.0):
                raise TimeoutError('test did not release failed readiness publication')
            raise OSError('planned directory fsync failure after mounted replace')

    def run_server() -> None:
        try:
            app.serve_forever(poll_interval=0.01)
        except BaseException as exc:
            server_errors.append(exc)

    def request_ready() -> None:
        try:
            CcbdClient(app.paths.ccbd_socket_path, timeout_s=3.0).request('test-ready')
        except BaseException as exc:
            request_errors.append(exc)

    def handle_ready(payload: dict) -> dict:
        handler_calls.append(payload)
        return {'ready': True}

    app.socket_server._bootstrap_gate_lock = _TrackedGateLock(request_waiting_on_gate)
    app.socket_server.register_handler('test-ready', handle_ready)
    monkeypatch.setattr(app.lifecycle_store, 'save', fail_after_mounted_replace)
    monkeypatch.setattr('ccbd.app_runtime.lifecycle.maybe_start_runtime_accelerator', lambda root: None)

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    request_thread = None
    try:
        assert mounted_replaced.wait(timeout=2.0)
        lifecycle = app.lifecycle_store.load()
        assert lifecycle is not None
        assert lifecycle.phase == 'mounted'
        assert lifecycle.startup_stage == 'mounted'

        request_thread = threading.Thread(target=request_ready, daemon=True)
        request_thread.start()
        assert request_waiting_on_gate.wait(timeout=2.0)
        assert request_thread.is_alive() is True
        assert handler_calls == []

        allow_publish_failure.set()
        request_thread.join(timeout=3.0)
        server_thread.join(timeout=3.0)
    finally:
        allow_publish_failure.set()
        if request_thread is not None:
            request_thread.join(timeout=3.0)
        if server_thread.is_alive():
            app.request_shutdown()
            server_thread.join(timeout=3.0)

    assert request_thread is not None
    assert request_thread.is_alive() is False
    assert server_thread.is_alive() is False
    assert handler_calls == []
    assert len(request_errors) == 1
    assert isinstance(request_errors[0], CcbdClientError)
    assert 'stopping' in str(request_errors[0])
    assert any(
        'planned directory fsync failure after mounted replace' in str(error)
        for error in server_errors
    )
    lifecycle = app.lifecycle_store.load()
    assert lifecycle is not None
    assert lifecycle.phase == 'failed'
    assert app.socket_server._stop_event.is_set()
    assert app.socket_server._runtime_bootstrap_active is False


def test_second_starting_child_cannot_unlink_first_child_socket(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo-ccbd-starting-owner-race'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    first = CcbdApp(project_root)
    second = CcbdApp(project_root)
    probe_verified = threading.Event()
    allow_publish = threading.Event()
    thread_errors: list[BaseException] = []
    original_probe = first.socket_server.bootstrap_readiness_probe

    @contextmanager
    def delayed_probe(*, timeout_s: float):
        with original_probe(timeout_s=timeout_s) as payload:
            probe_verified.set()
            if not allow_publish.wait(timeout=3.0):
                raise TimeoutError('test did not release mounted publication')
            yield payload

    def run_first() -> None:
        try:
            first.start()
        except BaseException as exc:
            thread_errors.append(exc)

    monkeypatch.setattr(first.socket_server, 'bootstrap_readiness_probe', delayed_probe)
    monkeypatch.setattr(first.dispatcher, 'restore_running_jobs', lambda: None)
    monkeypatch.setattr(first.dispatcher, 'last_restore_report', lambda **kwargs: None)
    monkeypatch.setattr('ccbd.app_runtime.lifecycle.maybe_start_runtime_accelerator', lambda root: None)
    first_thread = threading.Thread(target=run_first, daemon=True)
    first_thread.start()
    try:
        assert probe_verified.wait(timeout=2.0)
        before = first.paths.ccbd_socket_path.stat()
        before_identity = (int(before.st_dev), int(before.st_ino))

        with pytest.raises(StartupFenceError, match='already claimed by another child'):
            second.start()

        after = first.paths.ccbd_socket_path.stat()
        assert (int(after.st_dev), int(after.st_ino)) == before_identity
        lifecycle = first.lifecycle_store.load()
        assert lifecycle is not None
        assert lifecycle.phase == 'starting'
        assert lifecycle.owner_daemon_instance_id == first.daemon_instance_id
    finally:
        allow_publish.set()
        first_thread.join(timeout=3.0)
        if first.lease is not None:
            first.request_shutdown()

    assert first_thread.is_alive() is False
    assert thread_errors == []


def test_ccbd_start_rolls_back_mount_when_restore_fails(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-ccbd-start-rollback'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    monkeypatch.setattr(app.dispatcher, 'restore_running_jobs', lambda: (_ for _ in ()).throw(RuntimeError('boom')))

    with pytest.raises(RuntimeError, match='boom'):
        app.serve_forever(poll_interval=0.01)

    lease = app.mount_manager.load_state()
    lifecycle = app.lifecycle_store.load()
    assert lease is not None
    assert lease.mount_state.value == 'unmounted'
    assert lifecycle is not None
    assert lifecycle.phase == 'failed'
    assert lifecycle.last_failure_reason == 'boom'
    assert not app.paths.ccbd_socket_path.exists()

    startup_report = app.startup_report_store.load()
    assert startup_report is not None
    assert startup_report.status == 'failed'
    assert startup_report.auto_permission is False
    assert startup_report.start_policy_present is False
    assert startup_report.start_policy_auto_permission is None


def test_ccbd_start_daemon_boot_adopts_existing_runtime_authority(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo-ccbd-daemon-boot-adopt'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    app.persist_start_policy(auto_permission=True, recovery_restore=True)
    runtime_root = app.paths.agent_provider_runtime_dir('demo', 'codex')
    runtime_root.mkdir(parents=True, exist_ok=True)
    (runtime_root / 'bridge.pid').write_text('5511\n', encoding='utf-8')
    old_started_at = '2026-04-20T00:00:00Z'
    app.registry.upsert(
        AgentRuntime(
            agent_name='demo',
            state=AgentState.IDLE,
            pid=123,
            started_at=old_started_at,
            last_seen_at=old_started_at,
            runtime_ref='tmux:%1',
            session_ref='session-1',
            workspace_path=str(app.paths.workspace_path('demo')),
            project_id=app.project_id,
            backend_type='pane-backed',
            queue_depth=0,
            socket_path=None,
            health='healthy',
            provider='codex',
            runtime_root=str(runtime_root),
            runtime_pid=123,
            terminal_backend='tmux',
            pane_id='%1',
            active_pane_id='%1',
            pane_state='alive',
            binding_generation=5,
            runtime_generation=4,
            daemon_generation=5,
        )
    )
    monkeypatch.setattr(app.ownership_guard, 'verify_or_takeover', lambda **kwargs: 6)
    monkeypatch.setattr(app.dispatcher, 'restore_running_jobs', lambda: None)
    monkeypatch.setattr(app.dispatcher, 'last_restore_report', lambda **kwargs: None)
    monkeypatch.setattr(app.restore_report_store, 'save', lambda report: None)

    lease = app.start()
    thread_errors: list[BaseException] = []

    def run_server() -> None:
        try:
            app.serve_forever(poll_interval=0.01)
        except BaseException as exc:
            thread_errors.append(exc)

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    try:
        _wait_for(app.paths.ccbd_socket_path)
        updated = app.registry.get('demo')
        helper = load_helper_manifest(app.paths.agent_helper_path('demo'))

        assert lease.generation == 6
        assert updated is not None
        assert updated.binding_generation == 6
        assert updated.runtime_generation == 6
        assert updated.daemon_generation == 6
        assert updated.started_at != old_started_at
        assert helper is not None
        assert helper.runtime_generation == 6
        assert helper.owner_daemon_generation == 6
        assert helper.started_at == updated.started_at
    finally:
        app.request_shutdown()
        server_thread.join(timeout=3.0)

    assert server_thread.is_alive() is False
    assert thread_errors == []

    startup_report = app.startup_report_store.load()
    assert startup_report is not None
    assert startup_report.trigger == 'daemon_boot'
    assert startup_report.status == 'ok'
    assert startup_report.restore_requested is True
    assert startup_report.auto_permission is True
    assert startup_report.start_policy_present is True
    assert startup_report.start_policy_auto_permission is True


def test_ccbd_probe_failure_does_not_adopt_unmounted_runtime_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo-ccbd-probe-failure-no-adopt'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    runtime_root = app.paths.agent_provider_runtime_dir('demo', 'codex')
    old_started_at = '2026-04-20T00:00:00Z'
    app.registry.upsert(
        AgentRuntime(
            agent_name='demo',
            state=AgentState.IDLE,
            pid=123,
            started_at=old_started_at,
            last_seen_at=old_started_at,
            runtime_ref='tmux:%1',
            session_ref='session-1',
            workspace_path=str(app.paths.workspace_path('demo')),
            project_id=app.project_id,
            backend_type='pane-backed',
            queue_depth=0,
            socket_path=None,
            health='healthy',
            provider='codex',
            runtime_root=str(runtime_root),
            runtime_pid=123,
            terminal_backend='tmux',
            pane_id='%1',
            active_pane_id='%1',
            pane_state='alive',
            binding_generation=5,
            runtime_generation=5,
            daemon_generation=5,
        )
    )
    restore_calls = []

    @contextmanager
    def invalid_probe(*, timeout_s: float):
        del timeout_s
        yield {'project_id': 'wrong-project'}

    monkeypatch.setattr(app.ownership_guard, 'verify_or_takeover', lambda **kwargs: 6)
    monkeypatch.setattr(app.socket_server, 'bootstrap_readiness_probe', invalid_probe)
    monkeypatch.setattr(app.dispatcher, 'restore_running_jobs', lambda: restore_calls.append(True))

    with pytest.raises(RuntimeError, match='project_id mismatch'):
        app.start()

    updated = app.registry.get('demo')
    lifecycle = app.lifecycle_store.load()
    assert updated is not None
    assert updated.binding_generation == 5
    assert updated.runtime_generation == 5
    assert updated.daemon_generation == 5
    assert updated.started_at == old_started_at
    assert load_helper_manifest(app.paths.agent_helper_path('demo')) is None
    assert restore_calls == []
    assert app.mount_manager.load_state() is None
    assert lifecycle is not None
    assert lifecycle.phase == 'failed'
    assert not app.paths.ccbd_socket_path.exists()


def test_ccbd_mounted_lifecycle_write_failure_unmounts_new_lease(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / 'repo-ccbd-mounted-write-failure'
    (project_root / '.ccb').mkdir(parents=True, exist_ok=True)
    (project_root / '.ccb' / 'ccb.config').write_text('demo:codex\n', encoding='utf-8')
    bootstrap_project(project_root)
    app = CcbdApp(project_root)
    original_save = app.lifecycle_store.save

    def fail_mounted_save(lifecycle) -> None:
        if lifecycle.phase == 'mounted':
            raise OSError('planned mounted lifecycle write failure')
        original_save(lifecycle)

    monkeypatch.setattr(app.lifecycle_store, 'save', fail_mounted_save)

    with pytest.raises(OSError, match='planned mounted lifecycle write failure'):
        app.serve_forever(poll_interval=0.01)

    lease = app.mount_manager.load_state()
    lifecycle = app.lifecycle_store.load()
    assert lease is not None
    assert lease.mount_state.value == 'unmounted'
    assert lease.ccbd_pid == app.pid
    assert lease.daemon_instance_id == app.daemon_instance_id
    assert lifecycle is not None
    assert lifecycle.phase == 'failed'
    assert lifecycle.owner_pid is None
    assert lifecycle.owner_daemon_instance_id is None
    assert app.socket_server._worker_thread is None
    assert app.socket_server._runtime_bootstrap_active is False
    assert not app.paths.ccbd_socket_path.exists()
