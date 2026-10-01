"""Local patch (ccb-team-kit): a pane probe that times out is retried with more time."""
import subprocess
from types import SimpleNamespace

from terminal_runtime.tmux_panes_runtime.queries_runtime import service as queries


def _service(answers):
    calls = []

    def tmux_run(args, capture=False, timeout=None):
        calls.append(timeout)
        answer = answers[len(calls) - 1]
        if answer == "timeout":
            raise subprocess.TimeoutExpired(args, timeout)
        return SimpleNamespace(returncode=answer[0], stdout=answer[1])

    svc = SimpleNamespace(looks_like_pane_id_fn=lambda p: p.startswith("%"), tmux_run_fn=tmux_run,
                          pane_exists_output_fn=lambda out: out.strip().startswith("%"))
    return svc, calls


def test_a_probe_that_times_out_once_is_asked_again_with_more_time():
    svc, calls = _service(["timeout", (0, "%5\n")])
    assert queries.pane_exists(svc, "%5") is True
    assert calls == [0.5, 3.0]


def test_a_quick_answer_is_not_asked_twice():
    svc, calls = _service([(0, "%5\n")])
    assert queries.pane_exists(svc, "%5") is True
    assert calls == [0.5]


def test_a_pane_tmux_does_not_know_is_absent_without_a_retry():
    svc, calls = _service([(1, "")])
    assert queries.pane_exists(svc, "%9") is False
    assert calls == [0.5]


def test_two_timeouts_still_count_as_absent():
    svc, calls = _service(["timeout", "timeout"])
    assert queries.pane_exists(svc, "%5") is False
    assert calls == [0.5, 3.0]
