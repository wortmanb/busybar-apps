"""Activity gating regression tests; no device or Paperclip server required."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("paperclip_agents", Path(__file__).parents[1] / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def snapshot(running=True, fetched_at=10000):
    snap = app.demo_snapshot(0)
    snap["agents"] = [{"name": "test", "status": "running" if running else "idle"}]
    snap["fetched_at"] = fetched_at
    return snap


def test_gate_startup_idle_and_exact_grace_boundary():
    gate = app.ActivityGate()
    gate.observe(snapshot(False), 0)
    assert not gate.visible(0)
    gate.observe(snapshot(), 10)
    assert gate.visible(10)
    gate.observe(snapshot(False), 20)
    assert gate.visible(619.999)
    assert not gate.visible(620)


def test_gate_resumed_activity_restarts_grace():
    gate = app.ActivityGate()
    gate.observe(snapshot(), 0)
    gate.observe(snapshot(False), 10)
    gate.observe(snapshot(), 500)
    gate.observe(snapshot(False), 510)
    assert gate.visible(1109.999)
    assert not gate.visible(1110)


@pytest.mark.parametrize("poll_result,age", [(None, 0), (snapshot(), 121)])
def test_gate_failed_or_stale_polls_do_not_renew(poll_result, age):
    gate = app.ActivityGate()
    gate.observe(snapshot(), 0)
    gate.observe(poll_result, 10, age)
    gate.observe(poll_result, 500, age)
    assert not gate.visible(610)
    startup = app.ActivityGate()
    startup.observe(poll_result, 0, age)
    assert not startup.visible(0)


def test_cached_running_expires_without_polls():
    gate = app.ActivityGate()
    gate.observe(snapshot(), 0)
    assert gate.visible(120)
    assert gate.visible(719.999)
    assert not gate.visible(720)


def run_loop(monkeypatch, results, end=1000, once=False, draw_results=None, clear_results=None, wait=False, snapshot_age=0):
    class Clock:
        now = 0
        wall = 10000

        def sleep(self, delay):
            self.now += delay
            # Wall-clock jumps must not change inactivity or poll scheduling.
            self.wall -= 1000
            if self.now >= end:
                raise KeyboardInterrupt

    clock = Clock()
    polls, draws, clears = [], [], []
    draw_results = iter(draw_results or [])
    clear_results = iter(clear_results or [])

    def poll(self, **kwargs):
        polls.append(clock.now)
        value = results(clock.now)
        if isinstance(value, Exception):
            raise value
        value = snapshot(value)
        value["fetched_at"] = clock.wall - snapshot_age
        return value

    def send(events, responses):
        events.append(clock.now)
        result = next(responses, (200, "ok"))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(app.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(app.time, "time", lambda: clock.wall)
    monkeypatch.setattr(app.time, "sleep", clock.sleep)
    monkeypatch.setattr(app, "FRAME_INTERVAL", 100)
    monkeypatch.setattr(app.Paperclip, "snapshot", poll)
    monkeypatch.setattr(app.BusyBar, "draw", lambda self, frame: send(draws, draw_results))
    monkeypatch.setattr(app.BusyBar, "clear", lambda self: send(clears, clear_results))
    monkeypatch.setattr(app.signal, "signal", lambda *args: None)
    args = ["--poll", "100"] + (["--once"] if once else []) + (["--wait-for-bar"] if wait else [])
    result = app.main(args)
    return result, polls, draws, clears


def test_idle_keeps_polling_without_device_calls(monkeypatch):
    result, polls, draws, clears = run_loop(monkeypatch, lambda now: False)
    assert result == 0
    assert polls == list(range(0, 1000, 100))
    assert draws == clears == []


def test_stale_running_snapshots_never_take_display(monkeypatch):
    _, polls, draws, clears = run_loop(monkeypatch, lambda now: True, snapshot_age=121)
    assert len(polls) == 10
    assert draws == clears == []


def test_startup_errors_keep_polling_without_device_calls(monkeypatch):
    _, polls, draws, clears = run_loop(monkeypatch, lambda now: app.PaperclipError("offline"))
    assert len(polls) == 10
    assert draws == clears == []


@pytest.mark.parametrize("status", ["paused", "error", "pending_approval"])
def test_nonrunning_statuses_do_not_acquire(status):
    gate = app.ActivityGate()
    gate.observe({"agents": [{"status": status}]}, 0)
    assert not gate.visible(0)


def test_empty_company_does_not_acquire():
    gate = app.ActivityGate()
    gate.observe({"agents": []}, 0)
    assert not gate.visible(0)


def test_release_once_and_reacquire_on_resumed_activity(monkeypatch):
    _, polls, draws, clears = run_loop(monkeypatch, lambda now: now in (100, 1100), end=1300)
    assert draws == list(range(100, 800, 100)) + [1100, 1200]
    assert clears == [800, 1300]  # release, then shutdown after resumed activity
    assert polls == list(range(0, 1300, 100))


def test_poll_errors_release_cached_running(monkeypatch):
    _, polls, draws, clears = run_loop(
        monkeypatch, lambda now: True if now == 0 else app.PaperclipError("offline"))
    assert draws == list(range(0, 700, 100))
    assert clears == [700]
    assert len(polls) == 10


@pytest.mark.parametrize("running", [False, True])
def test_once_obeys_activity_gate(monkeypatch, running):
    result, polls, draws, clears = run_loop(monkeypatch, lambda now: running, once=True)
    assert result == 0
    assert polls == [0]
    assert draws == ([0] if running else [])
    assert clears == []


@pytest.mark.parametrize("status", [401, 403])
def test_device_auth_errors_remain_fatal(monkeypatch, status):
    with pytest.raises(SystemExit, match="rejected the token"):
        run_loop(monkeypatch, lambda now: True, draw_results=[(status, "denied")])


def test_once_priority_conflict_remains_fatal(monkeypatch):
    with pytest.raises(SystemExit, match="display busy"):
        run_loop(monkeypatch, lambda now: True, once=True, draw_results=[(409, "busy")])


def test_unreachable_bar_retries(monkeypatch):
    _, polls, draws, _ = run_loop(monkeypatch, lambda now: True, end=110,
                                 draw_results=[app.DeviceError("offline")], wait=True)
    assert draws[:2] == [0, 2]


def test_failed_clear_retries_then_stops(monkeypatch):
    _, _, _, clears = run_loop(monkeypatch, lambda now: now == 0, end=1000,
                              clear_results=[app.DeviceError("offline")], wait=True)
    assert clears == [700, 702]


def test_clear_is_scoped_delete_and_keeps_auth(monkeypatch):
    requests = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b"ok"
    def urlopen(req, **kwargs):
        requests.append(req)
        return Response()
    monkeypatch.setattr(app.urllib.request, "urlopen", urlopen)
    bar = app.BusyBar("localhost", "test-token", 20)
    bar.clear()
    req = requests[0]
    assert req.method == "DELETE"
    assert req.full_url.endswith("/api/display/draw?application_name=paperclip-agents")
    assert req.get_header("X-api-token") == "test-token"
    assert req.data is None
