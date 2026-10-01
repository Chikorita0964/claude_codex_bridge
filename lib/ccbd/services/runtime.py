from __future__ import annotations

from dataclasses import replace
from threading import RLock
from uuid import uuid4

from agents.models import AgentRuntime, AgentState, RuntimeBindingSource
from agents.store import AgentRestoreStore
from ccbd.system import utc_now
from provider_core.registry import build_default_session_binding_map
from storage.paths import PathLayout

from .registry import AgentRegistry
from .runtime_runtime.attach import attach_runtime as attach_runtime_impl
from .runtime_runtime.common import ACTIVE_RUNTIME_STATES
from .runtime_runtime.refresh import refresh_provider_binding as refresh_provider_binding_impl
from .runtime_runtime.restore import ensure_runtime_ready as ensure_runtime_ready_impl
from .runtime_runtime.restore import restore_runtime as restore_runtime_impl

_ACTIVE_STATES = ACTIVE_RUNTIME_STATES
_STATE_PATCH_FIELDS = frozenset(
    {
        'state',
        'health',
        'queue_depth',
        'active_pane_id',
        'pane_state',
        'desired_state',
        'reconcile_state',
        'restart_count',
        'recovery_failure_count',
        'last_seen_at',
        'last_reconcile_at',
        'last_failure_reason',
        'lifecycle_state',
        'pid',
        'mount_attempt_id',
    }
)
_AUTHORITY_ONLY_FIELDS = frozenset(
    {
        'started_at',
        'binding_generation',
        'runtime_generation',
        'daemon_generation',
        'runtime_ref',
        'session_ref',
        'runtime_root',
        'runtime_pid',
        'terminal_backend',
        'pane_id',
        'pane_title_marker',
        'tmux_socket_name',
        'tmux_socket_path',
        'session_file',
        'session_id',
        'slot_key',
        'window_id',
        'workspace_epoch',
        'managed_by',
        'binding_source',
        'workspace_path',
        'backend_type',
        'provider',
    }
)


class RuntimeService:
    def __init__(
        self,
        layout: PathLayout,
        registry: AgentRegistry,
        project_id: str,
        restore_store: AgentRestoreStore | None = None,
        session_bindings=None,
        daemon_generation_getter=None,
        *,
        clock=utc_now,
    ) -> None:
        self._layout = layout
        self._registry = registry
        self._project_id = project_id
        self._restore_store = restore_store or AgentRestoreStore(layout)
        self._session_bindings = session_bindings or build_default_session_binding_map(include_optional=True)
        self._daemon_generation_getter = daemon_generation_getter
        self._clock = clock
        self._attach_lock = RLock()
        self._pane_recovery_outcomes: dict[str, str] = {}

    def attach(
        self,
        *,
        agent_name: str,
        workspace_path: str,
        backend_type: str,
        pid: int | None = None,
        runtime_ref: str | None = None,
        session_ref: str | None = None,
        health: str | None = None,
        provider: str | None = None,
        runtime_root: str | None = None,
        runtime_pid: int | None = None,
        terminal_backend: str | None = None,
        provider_runtime_backend_ref: dict[str, object] | None = None,
        namespace_ref: dict[str, object] | None = None,
        pane_ref: dict[str, object] | None = None,
        namespace_restore_token_present: bool | None = None,
        herdr_auto_restore_mode: str | None = None,
        herdr_agent_state_ref: str | None = None,
        pane_id: str | None = None,
        active_pane_id: str | None = None,
        pane_title_marker: str | None = None,
        pane_state: str | None = None,
        tmux_socket_name: str | None = None,
        tmux_socket_path: str | None = None,
        tmux_window_name: str | None = None,
        tmux_window_id: str | None = None,
        session_file: str | None = None,
        session_id: str | None = None,
        slot_key: str | None = None,
        window_id: str | None = None,
        workspace_epoch: int | None = None,
        lifecycle_state: str | None = None,
        daemon_generation: int | None = None,
        managed_by: str | None = None,
        binding_source: str | RuntimeBindingSource | None = None,
    ) -> AgentRuntime:
        with self._attach_lock:
            resolved_daemon_generation = daemon_generation
            if resolved_daemon_generation is None and self._daemon_generation_getter is not None:
                try:
                    value = self._daemon_generation_getter()
                except Exception:
                    value = None
                if value is not None:
                    resolved_daemon_generation = int(value)
            runtime = attach_runtime_impl(
                registry=self._registry,
                project_id=self._project_id,
                clock=self._clock,
                agent_name=agent_name,
                workspace_path=workspace_path,
                backend_type=backend_type,
                pid=pid,
                runtime_ref=runtime_ref,
                session_ref=session_ref,
                health=health,
                provider=provider,
                runtime_root=runtime_root,
                runtime_pid=runtime_pid,
                terminal_backend=terminal_backend,
                provider_runtime_backend_ref=provider_runtime_backend_ref,
                namespace_ref=namespace_ref,
                pane_ref=pane_ref,
                namespace_restore_token_present=namespace_restore_token_present,
                herdr_auto_restore_mode=herdr_auto_restore_mode,
                herdr_agent_state_ref=herdr_agent_state_ref,
                pane_id=pane_id,
                active_pane_id=active_pane_id,
                pane_title_marker=pane_title_marker,
                pane_state=pane_state,
                tmux_socket_name=tmux_socket_name,
                tmux_socket_path=tmux_socket_path,
                tmux_window_name=tmux_window_name,
                tmux_window_id=tmux_window_id,
                session_file=session_file,
                session_id=session_id,
                slot_key=slot_key,
                window_id=window_id,
                workspace_epoch=workspace_epoch,
                lifecycle_state=lifecycle_state,
                daemon_generation=resolved_daemon_generation,
                managed_by=managed_by,
                binding_source=binding_source,
            )
            return runtime

    def attach_mount_attempt_authority(
        self,
        *,
        agent_name: str,
        attempt_id: str,
        workspace_path: str,
        backend_type: str,
        pid: int | None = None,
        runtime_ref: str | None = None,
        session_ref: str | None = None,
        health: str | None = None,
        provider: str | None = None,
        runtime_root: str | None = None,
        runtime_pid: int | None = None,
        terminal_backend: str | None = None,
        pane_id: str | None = None,
        active_pane_id: str | None = None,
        pane_title_marker: str | None = None,
        pane_state: str | None = None,
        tmux_socket_name: str | None = None,
        tmux_socket_path: str | None = None,
        tmux_window_name: str | None = None,
        tmux_window_id: str | None = None,
        session_file: str | None = None,
        session_id: str | None = None,
        slot_key: str | None = None,
        window_id: str | None = None,
        workspace_epoch: int | None = None,
        lifecycle_state: str | None = None,
        daemon_generation: int | None = None,
        managed_by: str | None = None,
        binding_source: str | RuntimeBindingSource | None = None,
    ) -> tuple[AgentRuntime | None, bool]:
        with self._attach_lock:
            current = self._registry.get(agent_name)
            if current is None or current.mount_attempt_id != attempt_id:
                return current, False
            runtime = self.attach(
                agent_name=agent_name,
                workspace_path=workspace_path,
                backend_type=backend_type,
                pid=pid,
                runtime_ref=runtime_ref,
                session_ref=session_ref,
                health=health,
                provider=provider,
                runtime_root=runtime_root,
                runtime_pid=runtime_pid,
                terminal_backend=terminal_backend,
                pane_id=pane_id,
                active_pane_id=active_pane_id,
                pane_title_marker=pane_title_marker,
                pane_state=pane_state,
                tmux_socket_name=tmux_socket_name,
                tmux_socket_path=tmux_socket_path,
                tmux_window_name=tmux_window_name,
                tmux_window_id=tmux_window_id,
                session_file=session_file,
                session_id=session_id,
                slot_key=slot_key,
                window_id=window_id,
                workspace_epoch=workspace_epoch,
                lifecycle_state=lifecycle_state,
                daemon_generation=daemon_generation,
                managed_by=managed_by,
                binding_source=binding_source,
            )
            refreshed = self._registry.get(agent_name)
            if refreshed is None or refreshed.mount_attempt_id != attempt_id:
                return refreshed, False
            return runtime, True

    def adopt_runtime_authority(
        self,
        runtime: AgentRuntime,
        *,
        daemon_generation: int | None = None,
    ) -> AgentRuntime:
        return self.mutate_runtime_authority(
            runtime,
            daemon_generation=daemon_generation,
        )

    def mutate_runtime_authority(
        self,
        runtime: AgentRuntime,
        **overrides,
    ) -> AgentRuntime:
        recognized = {
            'workspace_path',
            'backend_type',
            'pid',
            'runtime_ref',
            'session_ref',
            'health',
            'provider',
            'runtime_root',
            'runtime_pid',
            'terminal_backend',
            'pane_id',
            'active_pane_id',
            'pane_title_marker',
            'pane_state',
            'tmux_socket_name',
            'tmux_socket_path',
            'tmux_window_name',
            'tmux_window_id',
            'session_file',
            'session_id',
            'slot_key',
            'window_id',
            'workspace_epoch',
            'lifecycle_state',
            'daemon_generation',
            'managed_by',
            'binding_source',
            'mount_attempt_id',
        }
        unknown = set(overrides) - recognized
        if unknown:
            raise ValueError(f'invalid runtime authority fields: {", ".join(sorted(unknown))}')
        workspace_path = str(overrides.pop('workspace_path', runtime.workspace_path) or '').strip()
        if not workspace_path:
            workspace_path = str(self._layout.workspace_path(runtime.agent_name))
        updated = self.attach(
            agent_name=runtime.agent_name,
            workspace_path=workspace_path,
            backend_type=overrides.pop('backend_type', runtime.backend_type),
            pid=overrides.pop('pid', runtime.pid),
            runtime_ref=overrides.pop('runtime_ref', runtime.runtime_ref),
            session_ref=overrides.pop('session_ref', runtime.session_ref),
            health=overrides.pop('health', runtime.health),
            provider=overrides.pop('provider', runtime.provider),
            runtime_root=overrides.pop('runtime_root', runtime.runtime_root),
            runtime_pid=overrides.pop('runtime_pid', runtime.runtime_pid),
            terminal_backend=overrides.pop('terminal_backend', runtime.terminal_backend),
            pane_id=overrides.pop('pane_id', runtime.pane_id),
            active_pane_id=overrides.pop('active_pane_id', runtime.active_pane_id),
            pane_title_marker=overrides.pop('pane_title_marker', runtime.pane_title_marker),
            pane_state=overrides.pop('pane_state', runtime.pane_state),
            tmux_socket_name=overrides.pop('tmux_socket_name', runtime.tmux_socket_name),
            tmux_socket_path=overrides.pop('tmux_socket_path', runtime.tmux_socket_path),
            tmux_window_name=overrides.pop('tmux_window_name', runtime.tmux_window_name),
            tmux_window_id=overrides.pop('tmux_window_id', runtime.tmux_window_id),
            session_file=overrides.pop('session_file', runtime.session_file),
            session_id=overrides.pop('session_id', runtime.session_id),
            slot_key=overrides.pop('slot_key', runtime.slot_key),
            window_id=overrides.pop('window_id', runtime.window_id),
            workspace_epoch=overrides.pop('workspace_epoch', runtime.workspace_epoch),
            lifecycle_state=overrides.pop('lifecycle_state', runtime.lifecycle_state),
            daemon_generation=overrides.pop('daemon_generation', runtime.daemon_generation),
            managed_by=overrides.pop('managed_by', runtime.managed_by),
            binding_source=overrides.pop('binding_source', runtime.binding_source),
        )
        mount_attempt_id = overrides.pop('mount_attempt_id', runtime.mount_attempt_id)
        if updated.mount_attempt_id == mount_attempt_id:
            return updated
        return self._upsert_authority(replace(updated, mount_attempt_id=mount_attempt_id))

    def patch_runtime_state(self, runtime: AgentRuntime, **updates) -> AgentRuntime:
        unknown = set(updates) - _STATE_PATCH_FIELDS
        if unknown:
            raise ValueError(f'invalid runtime state patch fields: {", ".join(sorted(unknown))}')
        forbidden = set(updates) & _AUTHORITY_ONLY_FIELDS
        if forbidden:
            raise ValueError(f'authority fields cannot be patched: {", ".join(sorted(forbidden))}')
        current = self._registry.get(runtime.agent_name) or runtime
        candidate = replace(current, **updates)
        if candidate == current:
            return current
        return self._registry.upsert(candidate)

    def begin_mount_attempt(
        self,
        runtime: AgentRuntime,
        *,
        attempted_at: str,
    ) -> tuple[AgentRuntime, str]:
        attempt_id = f'mount-{uuid4().hex}'
        started = self._upsert_authority(
            replace(
                runtime,
                mount_attempt_id=attempt_id,
                recovery_failure_count=0,
                last_reconcile_at=attempted_at,
            )
        )
        return started, attempt_id

    def finalize_mount_attempt_success(
        self,
        agent_name: str,
        *,
        attempt_id: str,
        attempted_at: str,
        restart_count: int,
    ) -> tuple[AgentRuntime | None, bool]:
        current = self._registry.get(agent_name)
        if current is None or current.mount_attempt_id != attempt_id:
            return current, False
        state = AgentState.IDLE if current.state is AgentState.STARTING else current.state
        lifecycle_state = 'idle' if current.state is AgentState.STARTING else current.lifecycle_state
        updated = self._upsert_authority(
            replace(
                current,
                state=state,
                reconcile_state='steady',
                restart_count=restart_count,
                recovery_failure_count=0,
                last_reconcile_at=attempted_at,
                last_failure_reason=None,
                lifecycle_state=lifecycle_state,
                mount_attempt_id=None,
            )
        )
        return updated, True

    def finalize_mount_attempt_failure(
        self,
        agent_name: str,
        *,
        attempt_id: str,
        attempted_at: str,
        state: AgentState,
        health: str,
        reconcile_state: str,
        restart_count: int,
        reason: str,
        lifecycle_state: str | None,
    ) -> tuple[AgentRuntime | None, bool]:
        current = self._registry.get(agent_name)
        if current is None or current.mount_attempt_id != attempt_id:
            return current, False
        updated = self._upsert_authority(
            replace(
                current,
                state=state,
                health=health,
                reconcile_state=reconcile_state,
                restart_count=restart_count,
                last_reconcile_at=attempted_at,
                last_failure_reason=reason,
                lifecycle_state=lifecycle_state,
                mount_attempt_id=None,
            )
        )
        return updated, True

    def restore(self, agent_name: str):
        return restore_runtime_impl(
            layout=self._layout,
            registry=self._registry,
            restore_store=self._restore_store,
            attach_runtime_fn=self.attach,
            clock=self._clock,
            agent_name=agent_name,
        )

    def ensure_ready(self, agent_name: str) -> AgentRuntime:
        return ensure_runtime_ready_impl(
            layout=self._layout,
            registry=self._registry,
            restore_store=self._restore_store,
            attach_runtime_fn=self.attach,
            restore_runtime_fn=self.restore,
            clock=self._clock,
            agent_name=agent_name,
        )

    def refresh_provider_binding(self, agent_name: str, *, recover: bool = False) -> AgentRuntime | None:
        refresh = refresh_provider_binding_impl(
            layout=self._layout,
            registry=self._registry,
            session_bindings=self._session_bindings,
            attach_runtime_fn=self.attach,
            patch_runtime_state_fn=self.patch_runtime_state,
            agent_name=agent_name,
            recover=recover,
        )
        self._record_pane_recovery_outcome(agent_name, refresh.pane_outcome)
        return refresh.runtime

    def _record_pane_recovery_outcome(self, agent_name: str, pane_outcome: str | None) -> None:
        if pane_outcome is None:
            self._pane_recovery_outcomes.pop(agent_name, None)
            return
        self._pane_recovery_outcomes[agent_name] = pane_outcome

    def pane_recovery_outcome(self, agent_name: str) -> str | None:
        """The pane outcome the most recent ``refresh_provider_binding`` reported for this agent
        (local patch, ccb-team-kit): the recovery loop reads it to tell a pane that was live and
        owned all along from one this recovery respawned, which the pane id alone cannot say."""
        return self._pane_recovery_outcomes.get(agent_name)

    def _upsert_authority(self, runtime: AgentRuntime) -> AgentRuntime:
        upsert_authority = getattr(self._registry, 'upsert_authority', None)
        if callable(upsert_authority):
            return upsert_authority(runtime)
        return self._registry.upsert(runtime)


__all__ = ['RuntimeService']
