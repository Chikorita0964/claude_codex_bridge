from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from time import monotonic

from agents.models import AgentState, RuntimeBindingSource, normalize_runtime_binding_source
from ccbd.api_models import JobStatus, TargetKind
from ccbd.models import CcbdShutdownReport, CcbdStartupReport, MountState, cleanup_summaries_from_objects
from ccbd.system import process_exists
from ccbd.reload_drain_auto_retry import tick_reload_drain_auto_retry
from ccbd.services.dispatcher_runtime.frontdesk_direct_handoff import recover_frontdesk_direct_handoffs
from ccbd.services.dispatcher_runtime.detailer_replan_handoff import recover_detailer_replan_handoffs
from ccbd.services.lifecycle import build_lifecycle, current_socket_inode
from ccbd.services.start_policy import recovery_start_options
from ccbd.startup_fence import StartupFenceError, validate_expected_startup_lifecycle
from ccbd.startup_policy import CONTROL_PLANE_RPC_TIMEOUT_S
from ccbd.stop_flow import build_shutdown_runtime_snapshots
from mailbox_kernel import MailboxKernelService
from mailbox_runtime.targets import normalize_mailbox_owner_name
from runtime_accelerator.lifecycle import maybe_start_runtime_accelerator, stop_runtime_accelerator
from runtime_observability import record_startup_operation, startup_operation_counts
from storage.path_helpers import socket_placement_payload

from .request_guard import lifecycle_is_stopping

DEFAULT_CCBD_POLL_INTERVAL_S = 1.0
DEFAULT_IDLE_FULL_HEARTBEAT_INTERVAL_S = 30.0


def start(app):
    claimed_starting = False
    try:
        with app.ownership_guard.startup_lock():
            expected_fence = getattr(app, 'expected_startup_fence', None)
            if expected_fence is None:
                lifecycle = _current_lifecycle(app)
                _validate_legacy_startup_transaction(lifecycle)
                verified_generation = app.ownership_guard.verify_or_takeover(
                    project_id=app.project_id,
                    pid=app.pid,
                    socket_path=app.paths.ccbd_socket_path,
                )
                _validate_starting_owner_available(
                    app,
                    lifecycle,
                    verified_generation=verified_generation,
                )
                generation = _legacy_startup_generation(
                    lifecycle,
                    verified_generation=verified_generation,
                )
            else:
                lifecycle = app.lifecycle_store.load()
                if lifecycle is not None:
                    owner_pid = int(getattr(lifecycle, 'owner_pid', 0) or 0)
                    if (
                        str(getattr(lifecycle, 'phase', '')) == 'mounted'
                        and owner_pid > 0
                        and not process_exists(owner_pid)
                    ):
                        # The previous ccbd crashed and left the lifecycle
                        # phase at "mounted".  Reset to "starting" so the
                        # expected-fence validation can proceed.
                        # CcbdLifecycle is a frozen dataclass; use replace()
                        # to create a new instance with the corrected phase.
                        lifecycle = replace(lifecycle, phase='starting')
                validate_expected_startup_lifecycle(
                    expected_fence,
                    lifecycle,
                    project_id=app.project_id,
                    config_signature=str(app.config_identity['config_signature']),
                    socket_path=app.paths.ccbd_socket_path,
                )
                _validate_starting_owner_available(app, lifecycle)
                app.ownership_guard.assert_expected_claim_allowed(
                    project_id=app.project_id,
                    pid=app.pid,
                    socket_path=app.paths.ccbd_socket_path,
                    daemon_instance_id=app.daemon_instance_id,
                    expected_generation=expected_fence.generation,
                )
                generation = expected_fence.generation
            app.socket_server.listen()
            _save_starting_owner_claim(app, lifecycle, generation=generation)
            app.startup_generation = generation
            claimed_starting = True
        _update_startup_progress(app, 'socket_listening')
    except Exception as exc:
        with app.ownership_guard.startup_lock():
            app.socket_server.request_shutdown()
        app.socket_server.shutdown()
        if claimed_starting:
            _mark_lifecycle_failed(app, failure_reason=str(exc))
        record_startup_report(
            app,
            trigger='daemon_boot',
            status='failed',
            actions_taken=('mount_backend', 'listen_socket_failed'),
            failure_reason=str(exc),
        )
        raise
    try:
        _update_startup_progress(app, 'publishing_mounted')
        app.socket_server.begin_runtime_bootstrap()
        with app.socket_server.bootstrap_readiness_probe(
            timeout_s=CONTROL_PLANE_RPC_TIMEOUT_S,
        ) as bootstrap_payload:
            _validate_bootstrap_readiness_payload(app, bootstrap_payload)
            _publish_mounted_after_bootstrap_probe(app)
    except Exception as exc:
        _mark_lifecycle_failed(app, failure_reason=str(exc))
        app.lease = release_backend_ownership(app, desired_state='running')
        record_startup_report(
            app,
            trigger='daemon_boot',
            status='failed',
            actions_taken=('mount_backend', 'listen_socket', 'bootstrap_probe_failed'),
            failure_reason=str(exc),
        )
        raise

    return app.lease


def _finish_runtime_bootstrap(app):
    authority_check = lambda: _validate_current_startup_authority(app)
    app.dispatcher._startup_authority_check = authority_check
    try:
        # Runtime restoration runs after the self-probe and interim lease
        # publication but before final lifecycle mounted.  It may update job and
        # runtime authority, so it must never run for a generation that failed
        # its own probe or startup-authority publication.
        authority_check()
        app.dispatcher.restore_running_jobs()
        authority_check()
        recovered_frontdesk_jobs = recover_frontdesk_direct_handoffs(
            app.dispatcher,
            authority_check=authority_check,
        )
        authority_check()
        recovered_detailer_replan_jobs = recover_detailer_replan_handoffs(
            app.dispatcher,
            authority_check=authority_check,
        )
        authority_check()
        adopted_agents = _adopt_existing_runtime_authority(
            app,
            authority_check=authority_check,
        )
        restore_report = app.dispatcher.last_restore_report(project_id=app.project_id)
        if restore_report is not None:
            app.restore_report_store.save(restore_report)
        _validate_current_startup_authority(app)
        app.runtime_accelerator = maybe_start_runtime_accelerator(app.project_root)
        _validate_current_startup_authority(app)
        startup_actions = ['mount_backend', 'listen_socket', 'restore_running_jobs']
        if recovered_frontdesk_jobs:
            startup_actions.append(f'recover_frontdesk_direct_handoff:{",".join(recovered_frontdesk_jobs)}')
        if recovered_detailer_replan_jobs:
            startup_actions.append(
                f'recover_detailer_replan_handoff:{",".join(recovered_detailer_replan_jobs)}'
            )
        startup_actions.extend(_runtime_accelerator_startup_actions(app))
        if adopted_agents:
            startup_actions.append(f'adopt_runtime_authority:{",".join(adopted_agents)}')
        _mark_runtime_bootstrap_complete(app)
        record_startup_report(
            app,
            trigger='daemon_boot',
            status='ok',
            actions_taken=tuple(startup_actions),
            restore_summary=restore_report.summary_fields() if restore_report is not None else {},
        )
    except Exception as exc:
        _mark_lifecycle_failed(app, failure_reason=str(exc))
        app.lease = release_backend_ownership(app, desired_state='running')
        record_startup_report(
            app,
            trigger='daemon_boot',
            status='failed',
            actions_taken=('mount_backend', 'listen_socket', 'restore_running_jobs_failed'),
            failure_reason=str(exc),
        )
        raise
    finally:
        if getattr(app.dispatcher, '_startup_authority_check', None) is authority_check:
            delattr(app.dispatcher, '_startup_authority_check')
    return app.lease


def heartbeat(app):
    started = monotonic()
    try:
        failures = ()
        if _begin_maintenance_tick(app):
            try:
                if full_heartbeat_due(app, started=started):
                    failures = _heartbeat_failures(app)
                    app._last_full_heartbeat_at = started
            finally:
                _end_maintenance_tick(app)
        with app.ownership_guard.startup_lock():
            generation = int(getattr(app, 'startup_generation', 0) or 0)
            if generation > 0:
                lifecycle = app.lifecycle_store.load()
                if lifecycle is not None and lifecycle.desired_state != 'running':
                    app.socket_server.request_shutdown()
                    return app.lease
                _validate_current_startup_authority_locked(
                    app,
                    lifecycle,
                    generation=generation,
                )
            app.lease = app.mount_manager.refresh_heartbeat(
                expected_pid=app.pid,
                expected_daemon_instance_id=app.daemon_instance_id,
            )
            _record_heartbeat_failures_locked(app, failures=failures)
        return app.lease
    finally:
        duration = max(0.0, monotonic() - started)
        app.control_plane_metrics.last_maintenance_duration_s = duration
        app.control_plane_metrics.last_heartbeat_duration_s = duration


def _begin_maintenance_tick(app) -> bool:
    lock = getattr(app, 'start_maintenance_lock', None)
    if lock is None:
        return True
    try:
        return bool(lock.acquire(blocking=False))
    except TypeError:
        return bool(lock.acquire(False))


def _end_maintenance_tick(app) -> None:
    lock = getattr(app, 'start_maintenance_lock', None)
    if lock is None:
        return
    try:
        lock.release()
    except RuntimeError:
        return


def full_heartbeat_due(app, *, started: float) -> bool:
    if hot_loop_work_pending(app):
        return True
    try:
        last = float(getattr(app, '_last_full_heartbeat_at', 0.0) or 0.0)
    except Exception:
        last = 0.0
    return started - last >= idle_full_heartbeat_interval_s()


def idle_full_heartbeat_interval_s() -> float:
    try:
        return max(0.0, float(os.environ.get('CCB_CCBD_IDLE_FULL_HEARTBEAT_INTERVAL_S', DEFAULT_IDLE_FULL_HEARTBEAT_INTERVAL_S)))
    except Exception:
        return DEFAULT_IDLE_FULL_HEARTBEAT_INTERVAL_S


def hot_loop_work_pending(app) -> bool:
    execution_active = getattr(getattr(app, 'execution_service', None), '_active', None)
    if isinstance(execution_active, dict) and execution_active:
        return True
    dispatcher_state = getattr(getattr(app, 'dispatcher', None), '_state', None)
    if dispatcher_state is None:
        return False
    try:
        if dispatcher_state.active_items():
            return True
    except Exception:
        return True
    queues = getattr(dispatcher_state, '_queues', {})
    try:
        if any(len(queue) > 0 for queue in queues.values()):
            return True
    except Exception:
        return True
    message_bureau = getattr(getattr(app, 'dispatcher', None), '_message_bureau', None)
    if message_bureau is not None:
        try:
            if message_bureau.pending_callback_edges():
                return True
        except Exception:
            return True
    return False


def serve_forever(app, *, poll_interval: float = DEFAULT_CCBD_POLL_INTERVAL_S) -> None:
    deferred_runtime_bootstrap = bool(
        getattr(app.socket_server, '_runtime_bootstrap_active', False)
    )
    deferred_failures: list[BaseException] = []
    if app.lease is None:
        start(app)
        deferred_runtime_bootstrap = True

    def finish_runtime_bootstrap() -> None:
        try:
            _finish_runtime_bootstrap(app)
        except BaseException as exc:
            deferred_failures.append(exc)
            raise

    try:
        app.socket_server.serve_forever(
            poll_interval=effective_poll_interval(poll_interval),
            on_tick=app.heartbeat,
            on_serving=(finish_runtime_bootstrap if deferred_runtime_bootstrap else None),
        )
    except Exception as exc:
        if (
            deferred_runtime_bootstrap
            and not deferred_failures
            and getattr(app.socket_server, '_runtime_bootstrap_active', False)
        ):
            _mark_lifecycle_failed(app, failure_reason=str(exc))
            app.lease = release_backend_ownership(app, desired_state='running')
            record_startup_report(
                app,
                trigger='daemon_boot',
                status='failed',
                actions_taken=('mount_backend', 'listen_socket', 'runtime_bootstrap_not_started'),
                failure_reason=str(exc),
            )
        raise
    finally:
        app.lease = release_backend_ownership(app, desired_state=_release_desired_state(app))


def request_shutdown(app) -> None:
    app.lease = release_backend_ownership(app, desired_state='stopped')


def shutdown(app) -> None:
    execute_project_stop(
        app,
        force=True,
        trigger='shutdown',
        reason='shutdown',
        clear_start_policy=True,
    )


def mark_current_daemon_unmounted(app):
    with app.ownership_guard.startup_lock():
        return _mark_current_daemon_unmounted_locked(app)


def _mark_current_daemon_unmounted_locked(app):
    try:
        return app.mount_manager.mark_unmounted(
            expected_pid=app.pid,
            expected_daemon_instance_id=app.daemon_instance_id,
        )
    except RuntimeError:
        return None


def release_backend_ownership(app, *, desired_state: str | None = None):
    stop_runtime_accelerator(getattr(app, 'runtime_accelerator', None))
    app.runtime_accelerator = None
    lease = None
    try:
        with app.ownership_guard.startup_lock():
            # Closing the fd and unlinking its attempt-owned socket path must be
            # in the same transaction as lease/lifecycle release.  Otherwise an
            # old child can race a replacement bind between inode check and
            # pathname unlink.  Worker joins remain outside the lock below.
            app.socket_server.request_shutdown()
            lease = _mark_current_daemon_unmounted_locked(app)
            if _is_released_app_lease(app, lease):
                current_lease = app.mount_manager.load_state()
                if _is_released_app_lease(app, current_lease):
                    _mark_lifecycle_unmounted_locked(
                        app,
                        desired_state=desired_state,
                    )
            elif lease is None and _can_release_lifecycle_without_lease_locked(app):
                _mark_lifecycle_unmounted_locked(
                    app,
                    desired_state=desired_state,
                )
    finally:
        app.socket_server.shutdown()
    return lease


def _runtime_accelerator_startup_actions(app) -> list[str]:
    handle = getattr(app, 'runtime_accelerator', None)
    if handle is None or not getattr(handle, 'enabled', False):
        return []
    actions = []
    reclaimed_pids = tuple(getattr(handle, 'reclaimed_pids', ()) or ())
    if reclaimed_pids:
        actions.append(f'reclaim_runtime_accelerator:{",".join(str(pid) for pid in reclaimed_pids)}')
    if getattr(handle, 'process', None) is not None:
        return [*actions, 'start_runtime_accelerator']
    error = str(getattr(handle, 'error', '') or 'unavailable')
    return [*actions, f'runtime_accelerator_fallback:{error}']


def execute_project_stop(
    app,
    *,
    force: bool,
    trigger: str,
    reason: str,
    clear_start_policy: bool,
):
    execution, terminated_jobs = prepare_project_stop(
        app,
        force=force,
        trigger=trigger,
        reason=reason,
    )
    finalize_project_stop(
        app,
        execution=execution,
        terminated_jobs=terminated_jobs,
        trigger=trigger,
        forced=force,
        reason=reason,
        clear_start_policy=clear_start_policy,
    )
    return execution.summary


def prepare_project_stop(
    app,
    *,
    force: bool,
    trigger: str,
    reason: str,
):
    terminated_jobs = ()
    app.project_stop_requested = True
    _mark_lifecycle_stopping(app, shutdown_intent=reason)
    try:
        app.dispatcher.disable_auto_reply_delivery()
    except Exception:
        pass
    try:
        terminated_jobs = app.dispatcher.terminate_nonterminal_jobs(
            shutdown_reason=reason,
            forced=force,
        )
    except Exception:
        terminated_jobs = ()
    try:
        execution = app.runtime_supervisor.stop_all(force=force)
    except Exception as exc:
        record_shutdown_report(
            app,
            trigger=trigger,
            status='failed',
            forced=force,
            reason=reason,
            stopped_agents=(),
            actions_taken=(
                f'terminate_nonterminal_jobs:{len(terminated_jobs)}',
                'stop_all_failed',
            ),
            cleanup_summaries=(),
            failure_reason=str(exc),
        )
        raise
    return execution, terminated_jobs


def finalize_project_stop(
    app,
    *,
    execution,
    terminated_jobs,
    trigger: str,
    forced: bool,
    reason: str,
    clear_start_policy: bool,
) -> None:
    summary = execution.summary
    app.project_stop_requested = True
    app.lease = release_backend_ownership(app, desired_state='stopped')
    if clear_start_policy:
        try:
            app.start_policy_store.clear()
        except Exception:
            pass
    record_shutdown_report(
        app,
        trigger=trigger,
        status='ok',
        forced=forced,
        reason=reason,
        stopped_agents=tuple(summary.stopped_agents),
        actions_taken=(
            f'terminate_nonterminal_jobs:{len(terminated_jobs)}',
            'request_shutdown',
        ),
        cleanup_summaries=summary.cleanup_summaries,
        failure_reason=None,
    )
    for action in getattr(execution, 'deferred_actions', ()) or ():
        action()


def record_shutdown_report(
    app,
    *,
    trigger: str,
    status: str,
    forced: bool,
    reason: str,
    stopped_agents: tuple[str, ...],
    actions_taken: tuple[str, ...],
    cleanup_summaries,
    failure_reason: str | None,
) -> None:
    try:
        inspection = app.ownership_guard.inspect()
        runtime_snapshots = build_shutdown_runtime_snapshots(
            paths=app.paths,
            config=app.config,
            registry=app.registry,
        )
        report = CcbdShutdownReport(
            project_id=app.project_id,
            generated_at=app.clock(),
            trigger=trigger,
            status=status,
            forced=forced,
            stopped_agents=stopped_agents,
            daemon_generation=inspection.generation,
            reason=reason,
            inspection_after=inspection.to_record(),
            actions_taken=actions_taken,
            cleanup_summaries=cleanup_summaries_from_objects(cleanup_summaries),
            runtime_snapshots=runtime_snapshots,
            failure_reason=failure_reason,
        )
        app.shutdown_report_store.save(report)
    except Exception:
        return


def record_startup_report(
    app,
    *,
    trigger: str,
    status: str,
    actions_taken: tuple[str, ...],
    restore_summary: dict[str, object] | None = None,
    failure_reason: str | None = None,
) -> None:
    try:
        expected_fence = getattr(app, 'expected_startup_fence', None)
        with app.ownership_guard.startup_lock():
            if not _startup_report_write_allowed(
                app,
                expected_fence,
                status=status,
            ):
                return
            # The envelope's own atomic write is not recursively added to its
            # payload; retain an explicit request-scoped attempt instead.
            record_startup_operation('startup_report_write_attempt_count')
            inspection = app.ownership_guard.inspect(
                assume_mounted_socket_connectable=(str(status) == 'ok')
            )
            # Daemon boot has no start RPC payload; the persisted start policy
            # is the authority this boot recovers with, so the report states it
            # instead of a constant that hides a missing policy.
            start_policy = _load_start_policy(app)
            restore_requested, auto_permission = recovery_start_options(start_policy)
            report = CcbdStartupReport(
                project_id=app.project_id,
                generated_at=app.clock(),
                trigger=trigger,
                status=status,
                requested_agents=(),
                desired_agents=tuple(sorted(app.config.agents)),
                restore_requested=restore_requested,
                auto_permission=auto_permission,
                start_policy_present=start_policy is not None,
                start_policy_auto_permission=(
                    bool(start_policy.auto_permission) if start_policy is not None else None
                ),
                daemon_generation=app.lease.generation if app.lease is not None else inspection.generation,
                daemon_started=True,
                config_signature=str(app.config_identity.get('config_signature') or '').strip() or None,
                inspection=inspection.to_record(),
                socket_placement={
                    **app.paths.runtime_state_payload(),
                    **socket_placement_payload(app.paths.ccbd_socket_placement),
                    **socket_placement_payload(app.paths.ccbd_tmux_socket_placement, prefix='tmux'),
                },
                restore_summary=dict(restore_summary or {}),
                actions_taken=actions_taken,
                cleanup_summaries=(),
                agent_results=(),
                failure_reason=failure_reason,
                operation_counts=startup_operation_counts(),
            )
            app.startup_report_store.save(report)
    except Exception:
        return


def _load_start_policy(app):
    try:
        return app.start_policy_store.load()
    except Exception:
        return None


def _startup_report_write_allowed(app, expected_fence, *, status: str) -> bool:
    lifecycle = app.lifecycle_store.load()
    generation = int(getattr(app, 'startup_generation', 0) or 0)
    if lifecycle is None or generation <= 0:
        return False
    if str(getattr(lifecycle, 'project_id', '')) != str(app.project_id):
        return False
    if int(getattr(lifecycle, 'generation', 0) or 0) != generation:
        return False
    if (
        expected_fence is not None
        and not _matches_expected_startup_transaction(app, lifecycle, expected_fence)
    ):
        return False
    if str(status) == 'ok':
        return (
            lifecycle.desired_state == 'running'
            and lifecycle.phase == 'mounted'
            and lifecycle.startup_stage == 'mounted'
            and int(lifecycle.owner_pid or 0) == int(app.pid)
            and str(lifecycle.owner_daemon_instance_id or '') == str(app.daemon_instance_id or '')
            and _lease_is_app_generation(
                app,
                app.mount_manager.load_state(),
                generation=generation,
                mount_state=MountState.MOUNTED,
            )
        )
    return lifecycle.desired_state == 'running' and lifecycle.phase == 'failed'


def effective_poll_interval(poll_interval: float) -> float:
    try:
        requested = float(poll_interval)
    except Exception:
        requested = DEFAULT_CCBD_POLL_INTERVAL_S
    try:
        minimum = float(os.environ.get('CCB_CCBD_MIN_POLL_INTERVAL_S', '0'))
    except Exception:
        minimum = 0.0
    requested = max(0.0, requested)
    minimum = max(0.0, minimum)
    return max(requested, minimum)


def _adopt_existing_runtime_authority(app, *, authority_check=None) -> tuple[str, ...]:
    if app.lease is None:
        return ()
    generation = int(app.lease.generation)
    adopted: list[str] = []
    for runtime in app.registry.list_all():
        if callable(authority_check):
            authority_check()
        if normalize_runtime_binding_source(
            getattr(runtime, 'binding_source', RuntimeBindingSource.PROVIDER_SESSION)
        ) is RuntimeBindingSource.EXTERNAL_ATTACH:
            continue
        if runtime.state not in {AgentState.IDLE, AgentState.BUSY, AgentState.DEGRADED}:
            continue
        current_generation = getattr(runtime, 'daemon_generation', None)
        try:
            current_generation = int(current_generation) if current_generation is not None else None
        except Exception:
            current_generation = None
        if current_generation == generation and runtime.binding_generation == runtime.runtime_generation:
            continue
        app.runtime_service.adopt_runtime_authority(runtime, daemon_generation=generation)
        adopted.append(runtime.agent_name)
    return tuple(adopted)


def _current_lifecycle(app):
    lifecycle = app.lifecycle_store.load()
    if lifecycle is not None:
        return lifecycle
    return build_lifecycle(
        project_id=app.project_id,
        occurred_at=app.clock(),
        desired_state='running',
        phase='unmounted',
        generation=int(getattr(app.lease, 'generation', 0) or 0),
        keeper_pid=app.keeper_pid,
        config_signature=str(app.config_identity.get('config_signature') or '').strip() or None,
        socket_path=str(app.paths.ccbd_socket_path),
        control_plane_endpoint=_current_control_plane_endpoint(app),
    )


def _release_desired_state(app) -> str:
    if bool(getattr(app, 'project_stop_requested', False)):
        return 'stopped'
    try:
        lifecycle = app.lifecycle_store.load()
    except Exception:
        lifecycle = None
    if lifecycle_is_stopping(lifecycle):
        return 'stopped'
    return 'running'


def _mark_lifecycle_mounted(app) -> None:
    namespace_state = app.namespace_state_store.load() if getattr(app, 'namespace_state_store', None) is not None else None
    expected_fence = getattr(app, 'expected_startup_fence', None)
    if expected_fence is not None:
        with app.ownership_guard.startup_lock():
            lifecycle = app.lifecycle_store.load()
            validate_expected_startup_lifecycle(
                expected_fence,
                lifecycle,
                project_id=app.project_id,
                config_signature=str(app.config_identity['config_signature']),
                socket_path=app.paths.ccbd_socket_path,
            )
            _validate_expected_lease_holder(app, expected_fence)
            _save_lifecycle_mounted(app, lifecycle, namespace_state=namespace_state)
        return
    with app.ownership_guard.startup_lock():
        lifecycle = _current_lifecycle(app)
        _save_lifecycle_mounted(app, lifecycle, namespace_state=namespace_state)


def _save_lifecycle_mounted(
    app,
    lifecycle,
    *,
    namespace_state,
    startup_stage: str = 'mounted',
) -> None:
    app.lifecycle_store.save(
        lifecycle.with_phase(
            'mounted',
            occurred_at=app.clock(),
            desired_state='running',
            generation=int(getattr(app.lease, 'generation', 0) or lifecycle.generation),
            keeper_pid=app.keeper_pid,
            owner_pid=app.pid,
            owner_daemon_instance_id=app.daemon_instance_id,
            config_signature=str(app.config_identity.get('config_signature') or '').strip() or lifecycle.config_signature,
            socket_path=str(app.paths.ccbd_socket_path),
            socket_inode=current_socket_inode(app.paths.ccbd_socket_path),
            control_plane_endpoint=_current_control_plane_endpoint(app),
            namespace_epoch=getattr(namespace_state, 'namespace_epoch', None),
            startup_stage=startup_stage,
            last_progress_at=app.clock(),
            startup_deadline_at=None,
            last_failure_reason=None,
            shutdown_intent=None,
        )
    )


def _save_lifecycle_runtime_bootstrap(
    app,
    lifecycle,
    *,
    namespace_state,
    generation: int,
) -> None:
    app.lifecycle_store.save(
        lifecycle.with_phase(
            'starting',
            occurred_at=app.clock(),
            desired_state='running',
            generation=int(generation),
            keeper_pid=app.keeper_pid,
            owner_pid=app.pid,
            owner_daemon_instance_id=app.daemon_instance_id,
            config_signature=str(app.config_identity.get('config_signature') or '').strip()
            or lifecycle.config_signature,
            socket_path=str(app.paths.ccbd_socket_path),
            socket_inode=current_socket_inode(app.paths.ccbd_socket_path),
            control_plane_endpoint=_current_control_plane_endpoint(app),
            namespace_epoch=getattr(namespace_state, 'namespace_epoch', None),
            startup_stage='runtime_bootstrap',
            last_progress_at=app.clock(),
            last_failure_reason=None,
            shutdown_intent=None,
        )
    )


def _save_starting_owner_claim(app, lifecycle, *, generation: int) -> None:
    expected_fence = getattr(app, 'expected_startup_fence', None)
    app.lifecycle_store.save(
        lifecycle.with_phase(
            'starting',
            occurred_at=app.clock(),
            desired_state='running',
            generation=int(generation),
            keeper_pid=app.keeper_pid,
            owner_pid=app.pid,
            owner_daemon_instance_id=app.daemon_instance_id,
            config_signature=str(app.config_identity.get('config_signature') or '').strip()
            or lifecycle.config_signature,
            socket_path=str(app.paths.ccbd_socket_path),
            socket_inode=current_socket_inode(app.paths.ccbd_socket_path),
            control_plane_endpoint=_current_control_plane_endpoint(app),
            startup_id=(expected_fence.startup_id if expected_fence is not None else None),
            startup_stage='socket_listening',
            last_progress_at=app.clock(),
            startup_deadline_at=(
                lifecycle.startup_deadline_at if expected_fence is not None else None
            ),
            last_failure_reason=None,
            shutdown_intent=None,
        )
    )


def _validate_legacy_startup_transaction(lifecycle) -> None:
    if lifecycle is None:
        return
    phase = str(getattr(lifecycle, 'phase', '') or '')
    startup_id = str(getattr(lifecycle, 'startup_id', '') or '')
    if phase == 'starting' and startup_id:
        raise StartupFenceError('keeper-owned startup requires an expected startup fence')
    if phase == 'stopping':
        raise StartupFenceError('cannot start ccbd while lifecycle is stopping')


def _legacy_startup_generation(lifecycle, *, verified_generation: int) -> int:
    verified = int(verified_generation)
    lifecycle_generation = int(getattr(lifecycle, 'generation', 0) or 0)
    phase = str(getattr(lifecycle, 'phase', '') or '')
    if phase == 'starting' and lifecycle_generation > 0:
        return max(verified, lifecycle_generation)
    if lifecycle_generation >= verified:
        return lifecycle_generation + 1
    return verified


def _validate_starting_owner_available(
    app,
    lifecycle,
    *,
    verified_generation: int | None = None,
) -> None:
    if lifecycle is None:
        return
    owner_pid = int(getattr(lifecycle, 'owner_pid', 0) or 0)
    owner_instance = str(getattr(lifecycle, 'owner_daemon_instance_id', '') or '')
    if not owner_pid and not owner_instance:
        return
    if owner_pid == int(app.pid) and owner_instance == str(app.daemon_instance_id or ''):
        return
    if (
        verified_generation is not None
        and int(verified_generation) > int(getattr(lifecycle, 'generation', 0) or 0)
    ):
        return
    raise StartupFenceError('startup lifecycle is already claimed by another child')


def _validate_starting_owner(app, lifecycle, *, generation: int) -> None:
    checks = (
        (lifecycle is not None, 'lifecycle is missing'),
        (str(getattr(lifecycle, 'desired_state', '')) == 'running', 'desired_state is not running'),
        (str(getattr(lifecycle, 'phase', '')) == 'starting', 'phase is not starting'),
        (int(getattr(lifecycle, 'generation', 0) or 0) == int(generation), 'generation mismatch'),
        (int(getattr(lifecycle, 'owner_pid', 0) or 0) == int(app.pid), 'owner_pid mismatch'),
        (
            str(getattr(lifecycle, 'owner_daemon_instance_id', '') or '')
            == str(app.daemon_instance_id or ''),
            'owner daemon_instance_id mismatch',
        ),
        (
            str(Path(getattr(lifecycle, 'socket_path', '') or ''))
            == str(Path(app.paths.ccbd_socket_path)),
            'socket_path mismatch',
        ),
    )
    for valid, reason in checks:
        if not valid:
            raise StartupFenceError(f'starting owner claim rejected: {reason}')


def _publish_mounted_after_bootstrap_probe(app) -> None:
    generation = int(getattr(app, 'startup_generation', 0) or 0)
    if generation <= 0:
        raise StartupFenceError('startup generation is missing before mounted publish')
    namespace_state = (
        app.namespace_state_store.load()
        if getattr(app, 'namespace_state_store', None) is not None
        else None
    )
    expected_fence = getattr(app, 'expected_startup_fence', None)
    with app.ownership_guard.startup_lock():
        lifecycle = app.lifecycle_store.load()
        if expected_fence is not None:
            validate_expected_startup_lifecycle(
                expected_fence,
                lifecycle,
                project_id=app.project_id,
                config_signature=str(app.config_identity['config_signature']),
                socket_path=app.paths.ccbd_socket_path,
            )
        # The generation was allocated and the starting owner was already
        # claimed under this same lock discipline.  Revalidate that exact claim
        # instead of recomputing a takeover generation: an embedded restart can
        # reuse the OS pid while changing daemon_instance_id, and the predecessor
        # unmounted lease must remain a valid generation+1 handoff.
        app.ownership_guard.assert_expected_claim_allowed(
            project_id=app.project_id,
            pid=app.pid,
            socket_path=app.paths.ccbd_socket_path,
            daemon_instance_id=app.daemon_instance_id,
            expected_generation=generation,
        )
        _validate_starting_owner(app, lifecycle, generation=generation)
        new_lease = app.mount_manager.mark_mounted(
            project_id=app.project_id,
            pid=app.pid,
            socket_path=app.paths.ccbd_socket_path,
            generation=generation,
            config_signature=str(app.config_identity['config_signature']),
            keeper_pid=app.keeper_pid,
            daemon_instance_id=app.daemon_instance_id,
            control_plane_endpoint=_current_control_plane_endpoint(app),
        )
        _save_lifecycle_runtime_bootstrap(
            app,
            lifecycle,
            namespace_state=namespace_state,
            generation=generation,
        )
        app.lease = new_lease


def _current_control_plane_endpoint(app) -> dict | None:
    server = getattr(app, 'socket_server', None)
    if server is None:
        return None
    endpoint = getattr(server, 'control_plane_endpoint', None)
    if callable(endpoint):
        try:
            return endpoint()
        except Exception:
            return None
    return dict(endpoint) if isinstance(endpoint, dict) else None


def _validate_bootstrap_readiness_payload(app, payload) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError('ccbd bootstrap self-ping payload is invalid')
    expected_fence = getattr(app, 'expected_startup_fence', None)
    diagnostics = payload.get('diagnostics')
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    generation = int(getattr(app, 'startup_generation', 0) or 0)
    checks = (
        (str(payload.get('project_id') or '') == str(app.project_id), 'project_id mismatch'),
        (str(payload.get('desired_state') or '') == 'running', 'desired_state is not running'),
        (str(payload.get('mount_state') or '') == 'starting', 'lifecycle is not starting'),
        (int(payload.get('generation') or 0) == generation, 'generation mismatch'),
        (int(payload.get('serving_pid') or 0) == int(app.pid), 'serving_pid mismatch'),
        (
            str(payload.get('serving_daemon_instance_id') or '')
            == str(app.daemon_instance_id or ''),
            'daemon_instance_id mismatch',
        ),
        (
            payload.get('serving_lease_generation') is None,
            'mounted lease published before self-ping',
        ),
        (
            int(payload.get('serving_startup_generation') or 0) == generation,
            'serving startup generation mismatch',
        ),
        (
            str(diagnostics.get('startup_stage') or '') == 'publishing_mounted',
            'startup stage mismatch',
        ),
    )
    for valid, reason in checks:
        if not valid:
            raise RuntimeError(f'ccbd bootstrap self-ping rejected: {reason}')
    if expected_fence is None:
        return
    fenced_checks = (
        (
            str(payload.get('accepted_startup_id') or '') == expected_fence.startup_id,
            'accepted startup_id mismatch',
        ),
        (
            str(diagnostics.get('startup_id') or '') == expected_fence.startup_id,
            'lifecycle startup_id mismatch',
        ),
    )
    for valid, reason in fenced_checks:
        if not valid:
            raise RuntimeError(f'ccbd bootstrap self-ping rejected: {reason}')


def _validate_current_startup_authority(app) -> None:
    generation = int(getattr(app, 'startup_generation', 0) or 0)
    if generation <= 0:
        raise StartupFenceError('startup generation is missing after mounted publish')
    with app.ownership_guard.startup_lock():
        lifecycle = app.lifecycle_store.load()
        _validate_current_startup_authority_locked(
            app,
            lifecycle,
            generation=generation,
        )


def _validate_current_startup_authority_locked(app, lifecycle, *, generation: int) -> None:
    expected_fence = getattr(app, 'expected_startup_fence', None)
    if (
        expected_fence is not None
        and str(getattr(lifecycle, 'startup_id', '') or '') != expected_fence.startup_id
    ):
        raise StartupFenceError('mounted startup authority rejected: startup_id mismatch')
    phase = str(getattr(lifecycle, 'phase', '') or '')
    stage = str(getattr(lifecycle, 'startup_stage', '') or '')
    phase_is_current = (
        (phase == 'starting' and stage == 'runtime_bootstrap')
        or (phase == 'mounted' and stage == 'mounted')
    )
    checks = (
        (lifecycle is not None, 'lifecycle is missing'),
        (str(getattr(lifecycle, 'project_id', '')) == str(app.project_id), 'project_id mismatch'),
        (str(getattr(lifecycle, 'desired_state', '')) == 'running', 'desired_state is not running'),
        (phase_is_current, 'phase/stage is not current startup authority'),
        (int(getattr(lifecycle, 'generation', 0) or 0) == int(generation), 'generation mismatch'),
        (int(getattr(lifecycle, 'owner_pid', 0) or 0) == int(app.pid), 'owner_pid mismatch'),
        (
            str(getattr(lifecycle, 'owner_daemon_instance_id', '') or '')
            == str(app.daemon_instance_id or ''),
            'owner daemon_instance_id mismatch',
        ),
        (
            str(Path(getattr(lifecycle, 'socket_path', '') or ''))
            == str(Path(app.paths.ccbd_socket_path)),
            'socket_path mismatch',
        ),
    )
    for valid, reason in checks:
        if not valid:
            raise StartupFenceError(f'current startup authority rejected: {reason}')
    _validate_lease_holder(app, generation=generation)


def _mark_runtime_bootstrap_complete(app) -> None:
    generation = int(getattr(app, 'startup_generation', 0) or 0)
    if generation <= 0:
        raise StartupFenceError('startup generation is missing before runtime bootstrap completion')

    def publish_ready() -> None:
        with app.ownership_guard.startup_lock():
            lifecycle = app.lifecycle_store.load()
            _validate_current_startup_authority_locked(
                app,
                lifecycle,
                generation=generation,
            )
            namespace_state = (
                app.namespace_state_store.load()
                if getattr(app, 'namespace_state_store', None) is not None
                else None
            )
            _save_lifecycle_mounted(app, lifecycle, namespace_state=namespace_state)

    app.socket_server.finish_runtime_bootstrap(publish_ready)


def _mark_lifecycle_stopping(app, *, shutdown_intent: str) -> None:
    with app.ownership_guard.startup_lock():
        lifecycle = _current_lifecycle(app)
        app.lifecycle_store.save(
            lifecycle.with_phase(
                'stopping',
                occurred_at=app.clock(),
                desired_state='stopped',
                startup_stage=None,
                last_progress_at=app.clock(),
                startup_deadline_at=None,
                shutdown_intent=shutdown_intent,
                last_failure_reason=None,
            )
        )


def _mark_lifecycle_unmounted(app, *, desired_state: str | None = None) -> None:
    with app.ownership_guard.startup_lock():
        _mark_lifecycle_unmounted_locked(app, desired_state=desired_state)


def _mark_lifecycle_unmounted_locked(app, *, desired_state: str | None = None) -> None:
    expected_fence = getattr(app, 'expected_startup_fence', None)
    if expected_fence is not None:
        lifecycle = app.lifecycle_store.load()
        if not _matches_expected_startup_transaction(app, lifecycle, expected_fence):
            return
    else:
        lifecycle = _current_lifecycle(app)
    if lifecycle.phase == 'failed':
        return
    _save_lifecycle_unmounted(app, lifecycle, desired_state=desired_state)


def _save_lifecycle_unmounted(app, lifecycle, *, desired_state: str | None) -> None:
    requested_state = str(desired_state or '').strip()
    next_desired_state = (
        'stopped'
        if lifecycle.desired_state == 'stopped' or requested_state == 'stopped'
        else 'running'
    )
    app.lifecycle_store.save(
        lifecycle.with_phase(
            'unmounted',
            occurred_at=app.clock(),
            desired_state=next_desired_state,
            owner_pid=None,
            owner_daemon_instance_id=None,
            socket_inode=None,
            socket_path=str(app.paths.ccbd_socket_path),
            namespace_epoch=None,
            startup_stage=None,
            last_progress_at=app.clock(),
            startup_deadline_at=None,
            last_failure_reason=None,
        )
    )


def _mark_lifecycle_failed(app, *, failure_reason: str) -> None:
    expected_fence = getattr(app, 'expected_startup_fence', None)
    if expected_fence is not None:
        with app.ownership_guard.startup_lock():
            lifecycle = app.lifecycle_store.load()
            if not _matches_expected_startup_transaction(app, lifecycle, expected_fence):
                return
            if lifecycle.desired_state != 'running':
                return
            if lifecycle.phase not in {'starting', 'mounted', 'unmounted'}:
                return
            if lifecycle.phase in {'starting', 'mounted'} and not (
                int(lifecycle.owner_pid or 0) == int(app.pid)
                and str(lifecycle.owner_daemon_instance_id or '')
                == str(app.daemon_instance_id or '')
            ):
                return
            if lifecycle.phase == 'unmounted' and not _lease_is_app_generation(
                app,
                app.mount_manager.load_state(),
                generation=expected_fence.generation,
                mount_state=MountState.UNMOUNTED,
            ):
                return
            _save_lifecycle_failed(app, lifecycle, failure_reason=failure_reason)
        return
    with app.ownership_guard.startup_lock():
        lifecycle = app.lifecycle_store.load()
        if not _legacy_failure_write_allowed(app, lifecycle):
            return
        _save_lifecycle_failed(app, lifecycle, failure_reason=failure_reason)


def _save_lifecycle_failed(app, lifecycle, *, failure_reason: str) -> None:
    app.lifecycle_store.save(
        lifecycle.with_phase(
            'failed',
            occurred_at=app.clock(),
            owner_pid=None,
            owner_daemon_instance_id=None,
            socket_inode=None,
            socket_path=str(app.paths.ccbd_socket_path),
            namespace_epoch=None,
            startup_stage='failed',
            last_progress_at=app.clock(),
            startup_deadline_at=None,
            last_failure_reason=failure_reason,
        )
    )


def _update_startup_progress(app, stage: str) -> None:
    expected_fence = getattr(app, 'expected_startup_fence', None)
    if expected_fence is not None:
        with app.ownership_guard.startup_lock():
            lifecycle = app.lifecycle_store.load()
            validate_expected_startup_lifecycle(
                expected_fence,
                lifecycle,
                project_id=app.project_id,
                config_signature=str(app.config_identity['config_signature']),
                socket_path=app.paths.ccbd_socket_path,
            )
            app.lifecycle_store.save(
                lifecycle.with_updates(
                    startup_stage=str(stage).strip() or None,
                    last_progress_at=app.clock(),
                )
            )
        return
    with app.ownership_guard.startup_lock():
        lifecycle = app.lifecycle_store.load()
        generation = int(getattr(app, 'startup_generation', 0) or 0)
        if generation <= 0:
            raise StartupFenceError('legacy startup generation is missing during progress update')
        _validate_starting_owner(app, lifecycle, generation=generation)
        app.lifecycle_store.save(
            lifecycle.with_updates(
                startup_stage=str(stage).strip() or None,
                last_progress_at=app.clock(),
            )
        )


def _legacy_failure_write_allowed(app, lifecycle) -> bool:
    if lifecycle is None or str(lifecycle.desired_state) != 'running':
        return False
    generation = int(getattr(app, 'startup_generation', 0) or 0)
    if generation <= 0 or int(getattr(lifecycle, 'generation', 0) or 0) != generation:
        return False
    phase = str(getattr(lifecycle, 'phase', '') or '')
    if phase in {'starting', 'mounted'}:
        return (
            int(getattr(lifecycle, 'owner_pid', 0) or 0) == int(app.pid)
            and str(getattr(lifecycle, 'owner_daemon_instance_id', '') or '')
            == str(app.daemon_instance_id or '')
        )
    if phase != 'unmounted':
        return False
    return _lease_is_app_generation(
        app,
        app.mount_manager.load_state(),
        generation=generation,
        mount_state=MountState.UNMOUNTED,
    )


def _matches_expected_startup_transaction(app, lifecycle, expected_fence) -> bool:
    if lifecycle is None:
        return False
    try:
        return (
            str(lifecycle.project_id) == str(app.project_id)
            and str(lifecycle.startup_id or '') == expected_fence.startup_id
            and int(lifecycle.generation) == expected_fence.generation
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _validate_expected_lease_holder(app, expected_fence) -> None:
    _validate_lease_holder(app, generation=expected_fence.generation)


def _validate_lease_holder(app, *, generation: int) -> None:
    lease = app.mount_manager.load_state()
    if lease is None:
        raise StartupFenceError('expected startup lease is missing')
    checks = (
        (str(lease.project_id) == str(app.project_id), 'project_id mismatch'),
        (int(lease.generation) == int(generation), 'generation mismatch'),
        (int(lease.ccbd_pid) == int(app.pid), 'pid mismatch'),
        (
            str(lease.daemon_instance_id or '') == str(app.daemon_instance_id or ''),
            'daemon_instance_id mismatch',
        ),
        (
            str(Path(lease.socket_path)) == str(Path(app.paths.ccbd_socket_path)),
            'socket_path mismatch',
        ),
        (lease.mount_state is MountState.MOUNTED, 'mount_state is not mounted'),
    )
    for valid, reason in checks:
        if not valid:
            raise StartupFenceError(f'expected startup lease rejected: {reason}')


def _is_released_app_lease(app, lease) -> bool:
    if lease is None:
        return False
    generation = int(getattr(app, 'startup_generation', 0) or 0)
    if generation <= 0:
        generation = int(getattr(lease, 'generation', 0) or 0)
    return _lease_is_app_generation(
        app,
        lease,
        generation=generation,
        mount_state=MountState.UNMOUNTED,
    )


def _lease_is_app_generation(app, lease, *, generation: int, mount_state: MountState) -> bool:
    if lease is None or lease.mount_state is not mount_state:
        return False
    try:
        return (
            str(lease.project_id) == str(app.project_id)
            and int(lease.generation) == int(generation)
            and int(lease.ccbd_pid) == int(app.pid)
            and str(lease.daemon_instance_id or '') == str(app.daemon_instance_id or '')
            and str(Path(lease.socket_path)) == str(Path(app.paths.ccbd_socket_path))
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _can_release_lifecycle_without_lease_locked(app) -> bool:
    try:
        current_lease = app.mount_manager.load_state()
    except Exception:
        return False
    # `mark_unmounted()` returns None both when no lease exists and, through the
    # guarded wrapper above, when a replacement holder rejected the old daemon.
    # Only the genuinely missing-lease case permits lifecycle-only cleanup.
    if current_lease is not None:
        return False
    lifecycle = app.lifecycle_store.load()
    if lifecycle is None:
        return False
    generation = int(getattr(app, 'startup_generation', 0) or 0)
    if generation <= 0:
        return False
    try:
        if int(lifecycle.generation) != generation:
            return False
    except (AttributeError, TypeError, ValueError):
        return False
    if str(getattr(lifecycle, 'phase', '') or '') not in {'starting', 'mounted', 'stopping'}:
        return False
    if str(Path(getattr(lifecycle, 'socket_path', '') or '')) != str(
        Path(app.paths.ccbd_socket_path)
    ):
        return False
    expected_fence = getattr(app, 'expected_startup_fence', None)
    if expected_fence is not None:
        if not _matches_expected_startup_transaction(app, lifecycle, expected_fence):
            return False
        if lifecycle.phase == 'stopping' and lifecycle.desired_state == 'stopped':
            return True
    return (
        int(getattr(lifecycle, 'owner_pid', 0) or 0) == int(app.pid)
        and str(getattr(lifecycle, 'owner_daemon_instance_id', '') or '')
        == str(app.daemon_instance_id or '')
    )


def _heartbeat_failures(app) -> tuple[str, ...]:
    failures: list[str] = []
    step_timings: dict[str, float] = {}
    agents_inspected = 0
    runtime_store_writes = 0
    for step_name, action in (
        ('health_monitor', app.health_monitor.check_all),
        ('runtime_supervision', app.runtime_supervision.reconcile_once),
        ('dispatcher_runtime_views', app.dispatcher.reconcile_runtime_views),
        ('dispatcher_tick', app.dispatcher.tick),
        ('dispatcher_poll_completions', app.dispatcher.poll_completions),
        ('reload_drain_auto_retry', lambda: tick_reload_drain_auto_retry(app)),
        ('job_heartbeat', lambda: app.job_heartbeat.tick(app.dispatcher)),
        ('lease_expiry_sweep', lambda: _sweep_expired_delivery_leases(app)),
    ):
        if _lifecycle_stopping(app):
            break
        step_started = monotonic()
        save_count_before = _runtime_store_save_count(app)
        try:
            result = action()
            if step_name == 'runtime_supervision' and isinstance(result, dict):
                agents_inspected = max(agents_inspected, len(result))
        except Exception as exc:
            failures.append(f'heartbeat:{step_name}: {type(exc).__name__}: {exc}')
        finally:
            step_timings[step_name] = max(0.0, monotonic() - step_started)
            save_count_after = _runtime_store_save_count(app)
            if save_count_before is not None and save_count_after is not None:
                runtime_store_writes += max(0, save_count_after - save_count_before)
    metrics = getattr(app, 'control_plane_metrics', None)
    if metrics is not None:
        metrics.last_heartbeat_duration_s = sum(step_timings.values())
        metrics.heartbeat_step_duration_s = step_timings
        metrics.last_heartbeat_agents_inspected = agents_inspected
        metrics.last_heartbeat_runtime_store_writes = runtime_store_writes
    return tuple(failures)


def _sweep_expired_delivery_leases(app) -> None:
    ttl = getattr(app, 'mailbox_lease_ttl_s', None)
    try:
        ttl = float(ttl) if ttl is not None else None
    except (TypeError, ValueError):
        return
    if ttl is None or ttl <= 0:
        return
    kernel = MailboxKernelService(app.paths, clock=app.clock, lease_ttl_seconds=ttl)
    candidates = kernel.expired_delivery_leases()
    if not candidates:
        return
    busy = _agents_with_running_jobs(app)
    for lease in candidates:
        if lease.agent_name in busy:
            continue
        kernel.expire_lease(lease.agent_name)


def _agents_with_running_jobs(app) -> set[str]:
    names: set[str] = set()
    dispatcher = getattr(app, 'dispatcher', None)
    state = getattr(dispatcher, '_state', None)
    if state is None:
        return names
    for _target_kind, _target_name, job_id in state.active_items():
        job = dispatcher.get(job_id)
        if job is None or job.status is not JobStatus.RUNNING or job.target_kind is not TargetKind.AGENT:
            continue
        try:
            names.add(normalize_mailbox_owner_name(job.target_name))
        except Exception:
            continue
    return names


def _runtime_store_save_count(app) -> int | None:
    store = getattr(getattr(app, 'registry', None), '_runtime_store', None)
    value = getattr(store, 'save_count', None)
    return value if isinstance(value, int) else None


def _lifecycle_stopping(app) -> bool:
    try:
        lifecycle = app.lifecycle_store.load()
    except Exception:
        return False
    return lifecycle_is_stopping(lifecycle)


def _record_heartbeat_failures(app, *, failures: tuple[str, ...]) -> None:
    with app.ownership_guard.startup_lock():
        _record_heartbeat_failures_locked(app, failures=failures)


def _record_heartbeat_failures_locked(app, *, failures: tuple[str, ...]) -> None:
    lifecycle = app.lifecycle_store.load()
    if lifecycle is None:
        return
    if lifecycle.phase not in {'starting', 'mounted'}:
        return
    next_reason = ' | '.join(failures) if failures else None
    if lifecycle.last_failure_reason == next_reason:
        return
    try:
        app.lifecycle_store.save(
            lifecycle.with_updates(last_failure_reason=next_reason)
        )
    except Exception:
        return


__all__ = [
    'execute_project_stop',
    'heartbeat',
    'mark_current_daemon_unmounted',
    'record_shutdown_report',
    'record_startup_report',
    'release_backend_ownership',
    'request_shutdown',
    'serve_forever',
    'shutdown',
    'start',
]
