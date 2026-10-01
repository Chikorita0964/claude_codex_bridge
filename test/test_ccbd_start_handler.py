from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import threading
from types import SimpleNamespace

import pytest


def _build_start_handler():
    path = Path(__file__).resolve().parents[1] / 'lib' / 'ccbd' / 'handlers' / 'start.py'
    spec = importlib.util.spec_from_file_location('ccbd_start_handler_for_test', path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.build_start_handler


def test_start_handler_defaults_missing_auto_permission_to_plain_ccb_policy() -> None:
    start_calls: list[dict[str, object]] = []
    policies: list[dict[str, object]] = []

    app = SimpleNamespace(
        start_maintenance_lock=threading.Lock(),
        runtime_supervisor=SimpleNamespace(
            start=lambda **kwargs: start_calls.append(kwargs)
            or SimpleNamespace(to_record=lambda: {'started': []})
        ),
        persist_start_policy=lambda **kwargs: policies.append(kwargs),
    )

    payload = _build_start_handler()(app)({'agent_names': []})

    assert payload['started'] == []
    assert re.fullmatch(r'start_[0-9a-f]{32}', payload['startup_run_id'])
    assert start_calls[0]['restore'] is True
    assert start_calls[0]['auto_permission'] is True
    assert start_calls[0]['startup_run_id'] == payload['startup_run_id']
    assert start_calls[0]['daemon_started'] is None
    assert policies == [
        {
            'auto_permission': True,
            'recovery_restore': True,
            'source': 'start_command',
        }
    ]


def test_start_handler_preserves_client_correlation_and_daemon_started_observation() -> None:
    start_calls: list[dict[str, object]] = []
    app = SimpleNamespace(
        start_maintenance_lock=threading.Lock(),
        runtime_supervisor=SimpleNamespace(
            start=lambda **kwargs: start_calls.append(kwargs)
            or SimpleNamespace(to_record=lambda: {'started': []})
        ),
        persist_start_policy=lambda **kwargs: None,
    )
    startup_run_id = 'start_' + 'a' * 32

    payload = _build_start_handler()(app)(
        {
            'agent_names': [],
            'startup_run_id': startup_run_id,
            'daemon_started': True,
        }
    )

    assert payload['startup_run_id'] == startup_run_id
    assert start_calls[0]['startup_run_id'] == startup_run_id
    assert start_calls[0]['daemon_started'] is True


def test_start_handler_rejects_invalid_correlation_before_start() -> None:
    start_calls: list[dict[str, object]] = []
    app = SimpleNamespace(
        start_maintenance_lock=threading.Lock(),
        runtime_supervisor=SimpleNamespace(start=lambda **kwargs: start_calls.append(kwargs)),
        persist_start_policy=lambda **kwargs: None,
    )

    with pytest.raises(ValueError, match='startup_run_id'):
        _build_start_handler()(app)({'agent_names': [], 'startup_run_id': 'not-a-run-id'})

    assert start_calls == []


def test_start_handler_generates_unique_correlation_ids_for_concurrent_legacy_calls() -> None:
    start_calls: list[dict[str, object]] = []
    app = SimpleNamespace(
        start_maintenance_lock=threading.Lock(),
        runtime_supervisor=SimpleNamespace(
            start=lambda **kwargs: start_calls.append(kwargs)
            or SimpleNamespace(to_record=lambda: {'started': []})
        ),
        persist_start_policy=lambda **kwargs: None,
    )
    handler = _build_start_handler()(app)
    results: list[dict[str, object]] = []

    threads = [threading.Thread(target=lambda: results.append(handler({'agent_names': []}))) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    response_ids = {str(item['startup_run_id']) for item in results}
    request_ids = {str(item['startup_run_id']) for item in start_calls}
    assert len(results) == 8
    assert len(response_ids) == 8
    assert request_ids == response_ids


def test_start_handler_persists_policy_before_interrupted_start_flow() -> None:
    order: list[str] = []
    policies: list[dict[str, object]] = []

    def failing_start(**kwargs):
        order.append('start')
        raise RuntimeError('start flow interrupted')

    def persist_policy(**kwargs):
        order.append('persist')
        policies.append(kwargs)

    app = SimpleNamespace(
        start_maintenance_lock=threading.Lock(),
        runtime_supervisor=SimpleNamespace(start=failing_start),
        persist_start_policy=persist_policy,
    )

    with pytest.raises(RuntimeError, match='start flow interrupted'):
        _build_start_handler()(app)({'agent_names': ['demo'], 'restore': False, 'auto_permission': True})

    assert order == ['persist', 'start']
    assert policies == [
        {
            'auto_permission': True,
            'recovery_restore': False,
            'source': 'start_command',
        }
    ]


def test_start_handler_preserves_explicit_safe_auto_permission_false() -> None:
    start_calls: list[dict[str, object]] = []
    policies: list[dict[str, object]] = []

    app = SimpleNamespace(
        start_maintenance_lock=threading.Lock(),
        runtime_supervisor=SimpleNamespace(
            start=lambda **kwargs: start_calls.append(kwargs)
            or SimpleNamespace(to_record=lambda: {'started': []})
        ),
        persist_start_policy=lambda **kwargs: policies.append(kwargs),
    )

    _build_start_handler()(app)({'agent_names': [], 'restore': False, 'auto_permission': False})

    assert start_calls[0]['restore'] is False
    assert start_calls[0]['auto_permission'] is False
    assert policies == [
        {
            'auto_permission': False,
            'recovery_restore': False,
            'source': 'start_command',
        }
    ]
