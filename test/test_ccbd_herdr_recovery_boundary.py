from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agents.models import AgentRuntime, AgentState, RuntimeBindingSource
from ccbd.api_models import TargetKind
from ccbd.services.dispatcher_runtime.lifecycle_start_runtime.models import QueuedTargetSlot
from ccbd.services.dispatcher_runtime.lifecycle_start_runtime.tick import tick_jobs
from ccbd.services.runtime_recovery_policy import (
    HERDR_RECOVERY_CIRCUIT_THRESHOLD,
    HERDR_RECOVERY_OWNER,
    HERDR_RECOVERY_PROBATION_SECONDS,
    PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTH,
    herdr_recovery_policy,
    should_attempt_background_recovery,
)
from ccbd.services.dispatcher_runtime.lifecycle_start_runtime.recovery import (
    refresh_slot_runtime_for_start,
)
from ccbd.services.runtime import RuntimeService
from ccbd.services.runtime_runtime.refresh import refresh_provider_binding
from ccbd.supervision.recovery import recover_runtime
from ccbd.supervision.recovery_events import append_recovery_event
from ccbd.supervision.loop_runtime import runtime_requires_recovery
from provider_backends.pane_log_support.lifecycle_recovery import tmux_rebound_pane
from provider_core.contracts import ProviderSessionBinding
from storage.paths import PathLayout


def _herdr_runtime(*, auto_restore_mode: str | None = "disabled", health: str = "pane-dead"):
    return SimpleNamespace(
        agent_name="codex",
        state=AgentState.DEGRADED,
        health=health,
        binding_source=RuntimeBindingSource.PROVIDER_SESSION,
        terminal_backend="herdr",
        herdr_auto_restore_mode=auto_restore_mode,
    )


def test_herdr_recovery_policy_keeps_ccb_as_owner() -> None:
    policy = herdr_recovery_policy(_herdr_runtime())

    assert policy is not None
    assert policy["owner"] == HERDR_RECOVERY_OWNER == "ccb"
    assert policy["herdr_auto_restore_mode"] == "disabled"
    assert policy["probation_seconds"] == HERDR_RECOVERY_PROBATION_SECONDS == 90
    assert policy["circuit_threshold"] == HERDR_RECOVERY_CIRCUIT_THRESHOLD == 3
    assert policy["restore_token_required"] is True
    assert should_attempt_background_recovery(_herdr_runtime()) is True


@pytest.mark.parametrize("mode", ["observe-only", "unsupported", "unknown", "", None])
def test_herdr_auto_restore_must_be_disabled_for_recovery(mode: str | None) -> None:
    runtime = _herdr_runtime(auto_restore_mode=mode)

    policy = herdr_recovery_policy(runtime)

    assert policy is not None
    assert policy["herdr_auto_restore_mode"] == (mode or "unknown")
    assert should_attempt_background_recovery(runtime) is False


@pytest.mark.parametrize(
    "health",
    ["pane-dead", "pane-missing", "process-dead", "namespace-crashed", "daemon-unavailable"],
)
def test_herdr_recovery_admits_ccb_owned_runtime_healths(health: str) -> None:
    assert should_attempt_background_recovery(_herdr_runtime(health=health)) is True


def test_lifecycle_start_refresh_uses_herdr_recovery_gate() -> None:
    calls = []
    refreshed = SimpleNamespace(
        agent_name="codex",
        state=AgentState.IDLE,
        health="healthy",
        terminal_backend="herdr",
        herdr_auto_restore_mode="disabled",
    )
    dispatcher = SimpleNamespace(
        _execution_service=object(),
        _runtime_service=SimpleNamespace(
            refresh_provider_binding=lambda agent_name, recover: calls.append((agent_name, recover))
            or refreshed
        ),
        _registry=SimpleNamespace(spec_for=lambda agent_name: SimpleNamespace(provider="codex")),
        _provider_catalog=SimpleNamespace(get=lambda provider: SimpleNamespace(supports_resume=True)),
    )
    slot = QueuedTargetSlot(
        target_kind=TargetKind.AGENT,
        target_name="codex",
        runtime=_herdr_runtime(health="process-dead"),
    )

    updated = refresh_slot_runtime_for_start(dispatcher, slot)

    assert updated is not None
    assert updated.runtime is refreshed
    assert calls == [("codex", True)]


def test_lifecycle_start_blocks_herdr_auto_restore_co_owner() -> None:
    calls = []
    dispatcher = SimpleNamespace(
        _execution_service=object(),
        _runtime_service=SimpleNamespace(
            refresh_provider_binding=lambda agent_name, recover: calls.append((agent_name, recover))
        ),
        _registry=SimpleNamespace(spec_for=lambda agent_name: SimpleNamespace(provider="codex")),
        _provider_catalog=SimpleNamespace(get=lambda provider: SimpleNamespace(supports_resume=True)),
    )
    slot = QueuedTargetSlot(
        target_kind=TargetKind.AGENT,
        target_name="codex",
        runtime=_herdr_runtime(auto_restore_mode="observe-only", health="process-dead"),
    )

    assert refresh_slot_runtime_for_start(dispatcher, slot) is None
    assert calls == []


def test_lifecycle_start_blocked_herdr_auto_restore_writes_evidence() -> None:
    events = []
    runtime = _herdr_runtime(auto_restore_mode="observe-only", health="process-dead")
    runtime.daemon_generation = 7
    runtime.desired_state = "mounted"
    runtime.reconcile_state = "degraded"
    runtime.runtime_ref = "herdr:pane-1"
    runtime.session_ref = "session-1"
    runtime.namespace_restore_token_present = True
    calls = []
    dispatcher = SimpleNamespace(
        _project_id="project-1",
        _clock=lambda: "2026-08-03T00:00:00Z",
        _event_store=SimpleNamespace(append=events.append),
        _execution_service=object(),
        _runtime_service=SimpleNamespace(
            refresh_provider_binding=lambda agent_name, recover: calls.append((agent_name, recover))
        ),
        _registry=SimpleNamespace(
            spec_for=lambda agent_name: SimpleNamespace(provider="codex"),
            get=lambda agent_name: runtime,
            upsert_authority=lambda updated: updated,
        ),
        _provider_catalog=SimpleNamespace(get=lambda provider: SimpleNamespace(supports_resume=True)),
    )
    slot = QueuedTargetSlot(
        target_kind=TargetKind.AGENT,
        target_name="codex",
        runtime=runtime,
    )

    assert refresh_slot_runtime_for_start(dispatcher, slot) is None

    assert calls == []
    assert events[-1].event_kind == "recover_blocked"
    assert events[-1].details["recovery_evidence_ledger"]["action"] == "blocked"
    assert events[-1].details["recovery_evidence_ledger"]["herdr_auto_restore_mode"] == "observe-only"


def test_lifecycle_start_tick_records_blocked_herdr_recovery_evidence() -> None:
    events = []
    calls = []
    runtime = _herdr_runtime(auto_restore_mode="observe-only", health="process-dead")
    runtime.daemon_generation = 7
    runtime.desired_state = "mounted"
    runtime.reconcile_state = "degraded"
    runtime.runtime_ref = "herdr:pane-1"
    runtime.session_ref = "session-1"
    runtime.namespace_restore_token_present = True
    registry = _Registry(runtime)
    dispatcher = SimpleNamespace(
        _project_id="project-1",
        _clock=lambda: "2026-08-03T00:00:00Z",
        _config=SimpleNamespace(agents=["codex"]),
        _state=SimpleNamespace(
            active_job=lambda agent_name: None,
            queue_depth=lambda agent_name: 1,
        ),
        _event_store=SimpleNamespace(append=events.append),
        _execution_service=object(),
        _runtime_service=SimpleNamespace(
            refresh_provider_binding=lambda agent_name, recover: calls.append((agent_name, recover))
        ),
        _registry=registry,
        _provider_catalog=SimpleNamespace(get=lambda provider: SimpleNamespace(supports_resume=True)),
    )

    assert tick_jobs(dispatcher) == ()

    assert calls == []
    assert registry.current.health == PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTH
    assert events[-1].event_kind == "recover_blocked"
    assert events[-1].details["recovery_evidence_ledger"]["action"] == "blocked"
    assert events[-1].details["recovery_evidence_ledger"]["herdr_auto_restore_mode"] == "observe-only"

    assert tick_jobs(dispatcher) == ()
    assert calls == []
    assert len(events) == 1


def test_herdr_recovery_event_ledger_redacts_namespace_ref() -> None:
    raw_key = "restore" + "_token"
    raw_value = "secret-restore-token"
    events = []
    runtime = SimpleNamespace(
        daemon_generation=7,
        desired_state="mounted",
        reconcile_state="recovering",
        state=AgentState.DEGRADED,
        runtime_ref="herdr:pane-1",
        session_ref="session-1",
        terminal_backend="herdr",
        herdr_auto_restore_mode="disabled",
        herdr_agent_state_ref="herdr-agent-state://session-1/pane-1",
        provider_runtime_backend_ref={
            "backend_impl": "herdr",
            "namespace_ref": {
                "backend_impl": "herdr",
                "session_name": "ccb-demo",
                "namespace_id": "workspace-1",
                raw_key: raw_value,
            },
            "pane_ref": {
                "backend_impl": "herdr",
                "pane_id": "pane-1",
            },
        },
    )
    ctx = SimpleNamespace(
        project_id="project-1",
        agent_name="codex",
        event_store=SimpleNamespace(append=events.append),
    )

    append_recovery_event(
        ctx,
        event_kind="recover_started",
        occurred_at="2026-08-03T00:00:00Z",
        runtime=runtime,
        prior_health="namespace-crashed",
        result_health="namespace-crashed",
        details={"action": "namespace_recover", "reason": "namespace-crashed"},
    )

    event = events[0]
    ledger = event.details["recovery_evidence_ledger"]
    record = event.to_record()

    assert ledger["backend_impl"] == "herdr"
    assert ledger["owner"] == "ccb"
    assert ledger["herdr_auto_restore_mode"] == "disabled"
    assert ledger["action"] == "namespace_recover"
    assert ledger["reason"] == "namespace-crashed"
    assert ledger["restore_token_present"] is True
    assert ledger["namespace_ref"] == {
        "backend_impl": "herdr",
        "session_name": "ccb-demo",
        "namespace_id": "workspace-1",
    }
    assert ledger["pane_ref"] == {"backend_impl": "herdr", "pane_id": "pane-1"}
    assert ledger["herdr_agent_state_ref"] == "herdr-agent-state://session-1/pane-1"
    assert raw_key not in ledger["namespace_ref"]
    assert raw_value not in repr(record)


def test_herdr_recovery_event_details_redact_nested_restore_tokens() -> None:
    raw_key = "restore" + "_token"
    raw_value = "details-secret-restore-token"
    events = []
    namespace_ref = {
        "backend_impl": "herdr",
        "namespace_id": "workspace-1",
        raw_key: raw_value,
    }
    runtime = SimpleNamespace(
        daemon_generation=7,
        desired_state="mounted",
        reconcile_state="recovering",
        state=AgentState.DEGRADED,
        runtime_ref="herdr:pane-1",
        session_ref="session-1",
        terminal_backend="herdr",
        herdr_auto_restore_mode="disabled",
        namespace_ref=namespace_ref,
    )
    ctx = SimpleNamespace(
        project_id="project-1",
        agent_name="codex",
        event_store=SimpleNamespace(append=events.append),
    )

    append_recovery_event(
        ctx,
        event_kind="recover_failed",
        occurred_at="2026-08-03T00:00:00Z",
        runtime=runtime,
        prior_health="namespace-crashed",
        result_health="namespace-crashed",
        details={
            "namespace_ref": namespace_ref,
            "provider_runtime_backend_ref": {
                "backend_impl": "herdr",
                "namespace_ref": namespace_ref,
            },
            "action": "namespace_recover",
            "reason": "namespace-crashed",
        },
    )

    record = events[0].to_record()

    assert raw_value not in repr(record)
    assert raw_key not in events[0].details["namespace_ref"]
    assert raw_key not in events[0].details["provider_runtime_backend_ref"]["namespace_ref"]
    assert events[0].details["recovery_evidence_ledger"]["restore_token_present"] is True


def test_herdr_runtime_metadata_survives_runtime_record_roundtrip(tmp_path) -> None:
    from agents.store import AgentRuntimeStore

    layout = PathLayout(tmp_path / "repo")
    runtime = AgentRuntime(
        agent_name="codex",
        state=AgentState.DEGRADED,
        pid=101,
        started_at="2026-08-03T00:00:00Z",
        last_seen_at="2026-08-03T00:00:00Z",
        runtime_ref="herdr:pane-1",
        session_ref="session-1",
        workspace_path="/workspace/codex",
        project_id="project-1",
        backend_type="pane-backed",
        queue_depth=0,
        socket_path=None,
        health="namespace-crashed",
        terminal_backend="herdr",
        provider_runtime_backend_ref={
            "backend_impl": "herdr",
            "namespace_ref": {"backend_impl": "herdr", "namespace_id": "workspace-1"},
            "pane_ref": {"backend_impl": "herdr", "pane_id": "pane-1"},
        },
        namespace_ref={"backend_impl": "herdr", "namespace_id": "workspace-1"},
        pane_ref={"backend_impl": "herdr", "pane_id": "pane-1"},
        namespace_restore_token_present=True,
        herdr_auto_restore_mode="disabled",
        herdr_agent_state_ref="herdr-agent-state://session-1/pane-1",
        binding_source=RuntimeBindingSource.PROVIDER_SESSION,
    )

    AgentRuntimeStore(layout).save(runtime)
    loaded = AgentRuntimeStore(layout).load("codex")

    assert loaded is not None
    assert loaded.provider_runtime_backend_ref == runtime.provider_runtime_backend_ref
    assert loaded.namespace_ref == runtime.namespace_ref
    assert loaded.pane_ref == runtime.pane_ref
    assert loaded.namespace_restore_token_present is True
    assert loaded.herdr_auto_restore_mode == "disabled"
    assert loaded.herdr_agent_state_ref == runtime.herdr_agent_state_ref
    assert should_attempt_background_recovery(loaded) is True


def test_agent_runtime_to_record_redacts_direct_raw_restore_token() -> None:
    raw_key = "restore" + "_token"
    raw_value = "direct-secret-restore-token"
    namespace_ref = {
        "backend_impl": "herdr",
        "namespace_id": "workspace-1",
        raw_key: raw_value,
    }
    runtime = _agent_runtime(health="namespace-crashed")
    runtime.namespace_ref = namespace_ref
    runtime.provider_runtime_backend_ref = {
        "backend_impl": "herdr",
        "namespace_ref": namespace_ref,
        "pane_ref": {"backend_impl": "herdr", "pane_id": "pane-1"},
    }

    record = runtime.to_record()

    assert raw_value not in repr(record)
    assert raw_key not in record["namespace_ref"]
    assert raw_key not in record["provider_runtime_backend_ref"]["namespace_ref"]
    assert record["namespace_restore_token_present"] is True


def test_runtime_service_attach_redacts_raw_restore_token(tmp_path) -> None:
    raw_key = "restore" + "_token"
    raw_value = "attach-secret-restore-token"
    layout = PathLayout(tmp_path / "repo")
    runtime = _agent_runtime(health="namespace-crashed")
    registry = _Registry(runtime)
    namespace_ref = {
        "backend_impl": "herdr",
        "namespace_id": "workspace-1",
        raw_key: raw_value,
    }
    service = RuntimeService(
        layout,
        registry,
        project_id="project-1",
        session_bindings={},
        clock=lambda: "2026-08-03T00:00:00Z",
    )

    attached = service.attach(
        agent_name="codex",
        workspace_path=str(layout.workspace_path("codex")),
        backend_type="pane-backed",
        runtime_ref="herdr:pane-1",
        health="namespace-crashed",
        terminal_backend="herdr",
        provider_runtime_backend_ref={
            "backend_impl": "herdr",
            "namespace_ref": namespace_ref,
            "pane_ref": {"backend_impl": "herdr", "pane_id": "pane-1"},
        },
        namespace_ref=namespace_ref,
        pane_ref={"backend_impl": "herdr", "pane_id": "pane-1"},
        herdr_auto_restore_mode="disabled",
    )
    record = attached.to_record()

    assert raw_value not in repr(record)
    assert raw_key not in record["namespace_ref"]
    assert raw_key not in record["provider_runtime_backend_ref"]["namespace_ref"]
    assert record["namespace_restore_token_present"] is True


def test_herdr_refresh_redacts_raw_restore_token_from_runtime_record(tmp_path) -> None:
    raw_key = "restore" + "_token"
    raw_value = "session-secret-restore-token"
    layout = PathLayout(tmp_path / "repo")
    runtime = _agent_runtime(health="namespace-crashed")
    runtime.workspace_path = str(layout.workspace_path("codex"))
    registry = _Registry(runtime)
    namespace_ref = {
        "backend_impl": "herdr",
        "namespace_id": "workspace-1",
        raw_key: raw_value,
    }
    pane_ref = {"backend_impl": "herdr", "pane_id": "pane-1"}
    session = SimpleNamespace(
        pane_id="pane-1",
        data={
            "terminal": "mux",
            "backend_impl": "herdr",
            "pane_id": "pane-1",
            "provider_runtime_backend_ref": {
                "backend_impl": "herdr",
                "namespace_ref": namespace_ref,
                "pane_ref": pane_ref,
            },
            "namespace_ref": namespace_ref,
            "pane_ref": pane_ref,
            "herdr_auto_restore_mode": "disabled",
        },
        ensure_pane=lambda: (True, "pane-1"),
    )
    binding = ProviderSessionBinding(
        provider="codex",
        load_session=lambda workspace_path, instance: session,
        session_id_attr="missing_session_id",
        session_path_attr="missing_session_path",
    )

    refreshed = refresh_provider_binding(
        layout=layout,
        registry=registry,
        session_bindings={"codex": binding},
        attach_runtime_fn=lambda **kwargs: AgentRuntime(
            state=AgentState.IDLE,
            started_at="2026-08-03T00:00:00Z",
            last_seen_at="2026-08-03T00:00:01Z",
            project_id="project-1",
            queue_depth=0,
            socket_path=None,
            **kwargs,
        ),
        agent_name="codex",
        recover=True,
    ).runtime

    assert refreshed is not None
    record = refreshed.to_record()
    backend_ref = record["provider_runtime_backend_ref"]

    assert raw_value not in repr(record)
    assert raw_key not in record["namespace_ref"]
    assert raw_key not in backend_ref["namespace_ref"]
    assert record["namespace_restore_token_present"] is True


def test_herdr_refresh_carries_session_runtime_metadata_to_supervision(tmp_path) -> None:
    layout = PathLayout(tmp_path / "repo")
    runtime = _agent_runtime(health="namespace-crashed")
    runtime.workspace_path = str(layout.workspace_path("codex"))
    runtime.herdr_auto_restore_mode = "unknown"
    registry = _Registry(runtime)
    namespace_ref = {"backend_impl": "herdr", "namespace_id": "workspace-1"}
    pane_ref = {"backend_impl": "herdr", "pane_id": "pane-1"}
    session = SimpleNamespace(
        pane_id="pane-1",
        data={
            "terminal": "mux",
            "backend_impl": "herdr",
            "pane_id": "pane-1",
            "provider_runtime_backend_ref": {
                "backend_impl": "herdr",
                "namespace_ref": namespace_ref,
                "pane_ref": pane_ref,
                "herdr_agent_state_ref": "herdr-agent-state://session-1/pane-1",
            },
            "namespace_ref": namespace_ref,
            "pane_ref": pane_ref,
            "namespace_restore_token_present": True,
            "herdr_auto_restore_mode": "disabled",
        },
        ensure_pane=lambda: (True, "pane-1"),
    )
    binding = ProviderSessionBinding(
        provider="codex",
        load_session=lambda workspace_path, instance: session,
        session_id_attr="missing_session_id",
        session_path_attr="missing_session_path",
    )

    refreshed = refresh_provider_binding(
        layout=layout,
        registry=registry,
        session_bindings={"codex": binding},
        attach_runtime_fn=lambda **kwargs: AgentRuntime(
            state=AgentState.IDLE,
            started_at="2026-08-03T00:00:00Z",
            last_seen_at="2026-08-03T00:00:01Z",
            project_id="project-1",
            queue_depth=0,
            socket_path=None,
            **kwargs,
        ),
        agent_name="codex",
        recover=True,
    ).runtime

    assert refreshed is not None
    assert refreshed.provider_runtime_backend_ref == session.data["provider_runtime_backend_ref"]
    assert refreshed.namespace_ref == namespace_ref
    assert refreshed.pane_ref == pane_ref
    assert refreshed.namespace_restore_token_present is True
    assert refreshed.herdr_auto_restore_mode == "disabled"
    assert refreshed.herdr_agent_state_ref == "herdr-agent-state://session-1/pane-1"
    assert should_attempt_background_recovery(replace(refreshed, state=AgentState.DEGRADED, health="namespace-crashed")) is True


def test_herdr_recovery_opens_circuit_after_policy_threshold() -> None:
    runtime = _agent_runtime()
    registry = _Registry(runtime)
    runtime_service = _RecoveringRuntimeService(registry)
    events = []

    for _attempt in range(HERDR_RECOVERY_CIRCUIT_THRESHOLD):
        assert _recover(registry, runtime_service, events) == "recovering"
        current = registry.get("codex")
        registry.current = replace(
            current,
            state=AgentState.DEGRADED,
            health="pane-dead",
            pane_state="dead",
        )
        registry.current.herdr_auto_restore_mode = "disabled"

    assert _recover(registry, runtime_service, events) == "recovery-circuit-open"
    assert runtime_service.refresh_calls == HERDR_RECOVERY_CIRCUIT_THRESHOLD
    assert registry.current.reconcile_state == "blocked"
    assert registry.current.recovery_failure_count == HERDR_RECOVERY_CIRCUIT_THRESHOLD
    assert events[-1].event_kind == "recover_blocked"
    assert events[-1].details["recovery_evidence_ledger"]["action"] == "circuit_open"


def test_herdr_namespace_recovery_event_uses_namespace_action() -> None:
    runtime = _agent_runtime(health="namespace-crashed")
    registry = _Registry(runtime)
    runtime_service = _RecoveringRuntimeService(registry)
    events = []

    assert _recover(registry, runtime_service, events) == "recovering"

    assert runtime_service.refresh_calls == 1
    assert events[0].event_kind == "recover_started"
    assert events[0].details["recovery_evidence_ledger"]["action"] == "namespace_recover"
    assert events[0].details["recovery_evidence_ledger"]["reason"] == "namespace-crashed"


@pytest.mark.parametrize("mode", ["observe-only", "unsupported", "unknown"])
def test_herdr_auto_restore_not_disabled_records_blocked_recovery(mode: str) -> None:
    runtime = _agent_runtime(health="namespace-crashed")
    runtime.herdr_auto_restore_mode = mode
    registry = _Registry(runtime)
    runtime_service = _RecoveringRuntimeService(registry)
    events = []

    assert _runtime_requires_recovery(runtime) is True
    assert _recover(registry, runtime_service, events) == PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTH

    assert runtime_service.refresh_calls == 0
    assert registry.current.reconcile_state == "blocked"
    assert registry.current.health == PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTH
    assert events[-1].event_kind == "recover_blocked"
    assert events[-1].details["recovery_evidence_ledger"]["action"] == "blocked"
    assert events[-1].details["recovery_evidence_ledger"]["herdr_auto_restore_mode"] == mode
    assert mode in events[-1].details["recovery_evidence_ledger"]["reason"]


def test_herdr_pane_rebound_uses_backend_neutral_pane_ref() -> None:
    pane_ref = {"backend_impl": "herdr", "pane_id": "pane-1", "window_id": "win-1"}
    session = SimpleNamespace(
        start_cmd="codex --continue",
        work_dir="D:/repo",
        runtime_dir=SimpleNamespace(mkdir=lambda **_kwargs: None),
        data={
            "terminal": "mux",
            "pane_id": "pane-1",
            "provider_runtime_backend_ref": {
                "backend_impl": "herdr",
                "pane_ref": pane_ref,
            },
        },
        _write_back=lambda: None,
    )
    backend = _HerdrPaneRecoveryBackend(pane_ref)
    attached = []

    assert tmux_rebound_pane(
        session,
        backend,
        "pane-1",
        now_str_fn=lambda: "2026-08-03T00:00:00Z",
        attach_pane_log_fn=lambda sess, bkd, pane_id: attached.append((sess, bkd, pane_id)),
    ) == (True, "pane-1")

    assert backend.respawn_calls == [
        {
            "target": pane_ref,
            "cmd": "codex --continue",
            "cwd": "D:/repo",
            "remain_on_exit": True,
        }
    ]
    assert attached == [(session, backend, "pane-1")]
    assert session.data["pane_id"] == "pane-1"


def _agent_runtime(*, health: str = "pane-dead") -> AgentRuntime:
    pane_state = "dead" if health in {"pane-dead", "namespace-crashed"} else health
    runtime = AgentRuntime(
        agent_name="codex",
        state=AgentState.DEGRADED,
        pid=101,
        started_at="2026-08-03T00:00:00Z",
        last_seen_at="2026-08-03T00:00:00Z",
        runtime_ref="herdr:pane-1",
        session_ref="session-1",
        workspace_path="/workspace/codex",
        project_id="project-1",
        backend_type="pane-backed",
        queue_depth=0,
        socket_path=None,
        health=health,
        terminal_backend="herdr",
        pane_id="pane-1",
        active_pane_id="pane-1",
        pane_state=pane_state,
        binding_source=RuntimeBindingSource.PROVIDER_SESSION,
    )
    runtime.herdr_auto_restore_mode = "disabled"
    return runtime


class _Registry:
    def __init__(self, runtime: AgentRuntime) -> None:
        self.current = runtime

    def spec_for(self, agent_name: str):
        assert agent_name == "codex"
        return SimpleNamespace(name="codex", provider="codex")

    def get(self, agent_name: str):
        assert agent_name == "codex"
        return self.current

    def upsert_authority(self, runtime: AgentRuntime):
        self.current = runtime
        return runtime


class _RecoveringRuntimeService:
    def __init__(self, registry: _Registry) -> None:
        self._registry = registry
        self.refresh_calls = 0

    def refresh_provider_binding(self, agent_name: str, *, recover: bool = False):
        assert agent_name == "codex"
        assert recover is True
        self.refresh_calls += 1
        refreshed = replace(
            self._registry.current,
            state=AgentState.IDLE,
            health="healthy",
            pane_state="alive",
            last_seen_at="2026-08-03T00:00:01Z",
        )
        refreshed.herdr_auto_restore_mode = "disabled"
        self._registry.current = refreshed
        return refreshed


class _HerdrPaneRecoveryBackend:
    def __init__(self, pane_ref: dict[str, object]) -> None:
        self._pane_ref = pane_ref
        self.respawn_calls: list[dict[str, object]] = []

    def pane_exists(self, target) -> bool:
        return target == self._pane_ref

    def respawn_pane(self, target, *, cmd: str, cwd: str, remain_on_exit: bool = True) -> None:
        self.respawn_calls.append(
            {
                "target": target,
                "cmd": cmd,
                "cwd": cwd,
                "remain_on_exit": remain_on_exit,
            }
        )

    def is_alive(self, target) -> bool:
        return target == self._pane_ref

    def set_pane_title(self, *_args, **_kwargs) -> None:
        raise AssertionError("Herdr pane recovery must not use tmux identity")

    def set_pane_user_option(self, *_args, **_kwargs) -> None:
        raise AssertionError("Herdr pane recovery must not use tmux identity")


def _recover(registry: _Registry, runtime_service: _RecoveringRuntimeService, events: list) -> str:
    return recover_runtime(
        project_id="project-1",
        agent_name="codex",
        runtime=registry.get("codex"),
        registry=registry,
        runtime_service=runtime_service,
        remount_project_fn=lambda reason: None,
        clock=lambda: "2026-08-03T00:00:30Z",
        event_store=SimpleNamespace(append=events.append),
        align_runtime_authority_fn=lambda runtime: runtime,
        upsert_if_changed_fn=lambda runtime, **updates: registry.upsert_authority(
            replace(runtime, **updates)
        ),
        is_in_backoff_window_fn=lambda runtime, *, now: False,
        should_reflow_project_namespace_fn=lambda *args, **kwargs: False,
    )


def _runtime_requires_recovery(runtime) -> bool:
    ctx = SimpleNamespace(
        config=SimpleNamespace(windows_explicit=False, cmd_enabled=False, agents=["codex"]),
        layout=SimpleNamespace(ccbd_tmux_socket_path=""),
        registry=SimpleNamespace(
            get=lambda agent_name: runtime,
            spec_for=lambda agent_name: SimpleNamespace(runtime_mode=None),
        ),
        remount_project_fn=None,
    )
    return runtime_requires_recovery(ctx, runtime)
