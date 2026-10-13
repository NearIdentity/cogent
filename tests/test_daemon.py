"""The daemon: schedule maths, catch-up, retries, supervision, signals, start/stop (no network, no real agent)."""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import kg_daemon as D  # noqa: E402

UTC = ZoneInfo("UTC")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def opts(tmp_path, spec="06:00", tz="UTC", **kw):
    base = dict(home=tmp_path, schedule=D.Schedule.parse(spec, tz), agent_cmd=["true"], spec=spec,
                retries=2, retry_delay=600.0)
    base.update(kw)
    return D.Options(**base)


def make(tmp_path, results, start, **kw):
    """A daemon whose 'agent' returns the queued (exit, reason) results."""
    calls = []

    def runner(stop, tick):
        calls.append(1)
        return results.pop(0) if results else (0, "")

    clock = Clock(start)
    return D.Daemon(opts(tmp_path, **kw), runner=runner, clock=clock), clock, calls


def at(day, hh, mm=0, ss=0, tz=UTC):
    return datetime(2026, 10, day, hh, mm, ss, tzinfo=tz)


# ---------------------------------------------------------------- schedule


def test_schedule_next_and_last_with_dst():
    berlin = ZoneInfo("Europe/Berlin")
    s = D.Schedule.parse("18:30, 06:00", "Europe/Berlin")
    assert [t.strftime("%H:%M") for t in s.times] == ["06:00", "18:30"]
    now = at(24, 7, tz=berlin)
    assert s.last_due(now) == at(24, 6, tz=berlin) and s.next_after(now) == at(24, 18, 30, tz=berlin)
    # clocks go back on 2026-10-25: 18:30 CEST -> 06:00 CET is 12.5 real hours, not 11.5
    nxt = s.next_after(at(24, 18, 31, tz=berlin))
    assert nxt == at(25, 6, tz=berlin) and (nxt.astimezone(UTC) - at(24, 18, 31, tz=berlin).astimezone(UTC)) == timedelta(hours=12, minutes=29)
    with pytest.raises(ValueError):
        D.Schedule.parse("25:00")
    with pytest.raises(ValueError):
        D.Schedule.parse("6")


def test_jitter_is_stable_and_bounded():
    s = D.Schedule.parse("06:00", "UTC", jitter=300)
    slot = at(24, 6)
    f1, f2 = s.fire_time(slot), s.fire_time(slot)
    assert f1 == f2 and timedelta(0) <= f1 - slot <= timedelta(seconds=300)
    assert s.fire_time(at(25, 6)) - at(25, 6) != f1 - slot            # differs between slots


# ---------------------------------------------------------------- decisions


def test_fires_once_per_slot(tmp_path):
    d, clock, calls = make(tmp_path, [], at(24, 5))
    assert d.tick() is None and not calls                               # fresh install: waits for the slot
    clock.now = at(24, 6, 0, 5)
    assert d.tick() == "schedule" and len(calls) == 1
    clock.now = at(24, 6, 0, 30)
    assert d.tick() is None and len(calls) == 1                         # no double fire
    st = json.loads((tmp_path / "daemon/state.json").read_text())
    assert st["last_exit"] == 0 and st["last_success"] and st["next_run"].startswith("2026-10-25T06:00")
    clock.now = at(25, 6, 0, 1)
    assert d.tick() == "schedule" and len(calls) == 2


def test_catch_up_after_downtime(tmp_path):
    d, clock, calls = make(tmp_path, [], at(24, 5))
    d.tick()
    clock.now = at(24, 6, 0, 2)
    d.tick()                                                           # the 24th ran; daemon then "goes down"
    d2 = D.Daemon(opts(tmp_path), runner=lambda stop, tick: (calls.append(1) or (0, "")), clock=Clock(at(25, 9)))
    assert d2.tick() == "catch-up"                                      # woke up at 09:00: yesterday... today's 06:00 slot was missed
    assert len(calls) == 2


def test_no_catch_up_skips_but_marks_slot(tmp_path):
    d, clock, calls = make(tmp_path, [], at(24, 5))
    d.tick(); clock.now = at(24, 6, 0, 2); d.tick()
    d2 = D.Daemon(opts(tmp_path, catch_up=False), runner=lambda stop, tick: (calls.append(1) or (0, "")), clock=Clock(at(25, 9)))
    assert d2.tick() is None and len(calls) == 1
    assert d2.tick() is None                                            # and it does not fire later for the same slot
    d2.clock.now = at(26, 6, 0, 1)
    assert d2.tick() == "schedule"


def test_run_on_start_and_signal_and_stop(tmp_path):
    d, clock, calls = make(tmp_path, [], at(24, 12), run_on_start=True)
    assert d.tick() == "start" and len(calls) == 1
    assert d.tick() is None
    d.run_now_requested = True
    assert d.tick() == "signal" and len(calls) == 2
    d.stop_requested = True
    d.run_now_requested = True
    assert d.tick() is None and len(calls) == 2


def test_retries_with_backoff_then_hook(tmp_path):
    marker = tmp_path / "hook.txt"
    hook = f"{sys.executable} -c \"import os;open(r'{marker}','a').write(os.environ['COGENT_EXIT']+os.environ['COGENT_FAILURES']+'|')\""
    d, clock, calls = make(tmp_path, [(1, ""), (1, ""), (1, ""), (0, "")], at(24, 5), on_failure=hook)
    d.tick()
    clock.now = at(24, 6, 0, 1)
    assert d.tick() == "schedule" and d.state.retry_at.startswith("2026-10-24T06:10")      # +600 s
    clock.now = at(24, 6, 5)
    assert d.tick() is None
    clock.now = at(24, 6, 10, 1)
    assert d.tick() == "retry" and d.state.retry_at.startswith("2026-10-24T06:30")         # +1200 s
    assert not marker.exists()                                                              # not given up yet
    clock.now = at(24, 6, 30, 1)
    assert d.tick() == "retry" and d.state.retry_at is None and d.state.consecutive_failures == 3
    assert marker.read_text() == "13|"                                                       # exit 1, failure 3: hook fired once
    clock.now = at(24, 12)
    assert d.tick() is None and len(calls) == 3                                             # nothing more that day
    clock.now = at(25, 6, 0, 1)
    assert d.tick() == "schedule" and d.state.consecutive_failures == 0 and d.state.last_success


def test_retry_never_crosses_the_next_slot(tmp_path):
    d, clock, calls = make(tmp_path, [(1, "")], at(24, 5), spec="06:00,06:20", retry_delay=1800)
    d.tick()
    clock.now = at(24, 6, 0, 1)
    d.tick()
    assert d.state.retry_at is None                                     # a retry at 06:30 would be after the 06:20 slot


def test_stopped_run_is_not_retried(tmp_path):
    d, clock, calls = make(tmp_path, [(-15, "stopped")], at(24, 5))
    d.tick(); clock.now = at(24, 6, 0, 1); d.tick()
    assert d.state.retry_at is None and d.state.status.startswith("last run failed")


def test_pidfile_is_exclusive(tmp_path):
    a, b = D.PidFile(tmp_path / "x.pid"), D.PidFile(tmp_path / "x.pid")
    assert a.acquire() and not b.acquire()
    assert D.PidFile.running_pid(tmp_path / "x.pid") == os.getpid()
    a.release()
    assert D.PidFile.running_pid(tmp_path / "x.pid") is None and b.acquire()
    b.release()


# ---------------------------------------------------------------- supervision


def test_run_command_timeout_and_stop(tmp_path):
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    t0 = time.monotonic()
    code, reason = D.run_command(sleeper, timeout=1, stop=lambda: False, grace=2)
    assert reason == "timeout" and code != 0 and time.monotonic() - t0 < 10
    flag = {"n": 0}

    def stop():
        flag["n"] += 1
        return flag["n"] > 3

    code, reason = D.run_command(sleeper, timeout=60, stop=stop, grace=2)
    assert reason == "stopped"
    code, reason = D.run_command([sys.executable, "-c", "raise SystemExit(7)"], timeout=30, stop=lambda: False)
    assert (code, reason) == (7, "")


# ---------------------------------------------------------------- real processes

FAKE_AGENT = """
import json, os, sys, time
home = os.environ["COGENT_TEST_HOME"]
os.makedirs(home + "/logs", exist_ok=True)
with open(home + "/logs/runs.jsonl", "a") as fh:
    fh.write(json.dumps({"run_id": time.time(), "summary": {"documents": 3, "warnings": 2}}) + "\\n")
"""


def _env(tmp_path):
    (tmp_path / "fake_agent.py").write_text(FAKE_AGENT)
    return {**os.environ, "COGENT_TEST_HOME": str(tmp_path)}


def _wait(cond, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def _state(tmp_path):
    try:
        return json.loads((tmp_path / "daemon/state.json").read_text())
    except (OSError, ValueError):
        return {}


def test_serve_runs_signals_and_stops_gracefully(tmp_path):
    env = _env(tmp_path)
    cmd = [sys.executable, str(ROOT / "src/kg_daemon.py"), "serve", "--home", str(tmp_path), "--at", "06:00", "--tz", "UTC",
           "--run-on-start", "--agent-cmd", f"{sys.executable} {tmp_path / 'fake_agent.py'}"]
    p = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE, text=True)
    try:
        assert _wait(lambda: _state(tmp_path).get("last_exit") == 0), "first run (run-on-start)"
        assert _state(tmp_path)["last_trigger"] == "start" and _state(tmp_path)["last_summary"]["warnings"] == 2
        assert D.main(["status", "--home", str(tmp_path)]) == 0
        # a second daemon on the same home refuses to start
        again = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
        assert again.returncode == 1 and "already runs" in again.stderr
        # SIGUSR1: run now
        p.send_signal(signal.SIGUSR1)
        assert _wait(lambda: len((tmp_path / "logs/runs.jsonl").read_text().splitlines()) == 2), "run-now via SIGUSR1"
        assert _wait(lambda: _state(tmp_path).get("last_trigger") == "signal")
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=20) == 0
    finally:
        if p.poll() is None:
            p.kill()
    assert not (tmp_path / "daemon/kg_agent.pid").exists() and _state(tmp_path)["status"] == "stopped"
    assert D.main(["status", "--home", str(tmp_path)]) == 3              # not running


def test_start_detaches_and_stop_works(tmp_path):
    env = _env(tmp_path)
    base = [sys.executable, str(ROOT / "src/kg_daemon.py")]
    common = ["--home", str(tmp_path), "--at", "06:00", "--tz", "UTC", "--run-on-start",
              "--agent-cmd", f"{sys.executable} {tmp_path / 'fake_agent.py'}"]
    r = subprocess.run(base + ["start"] + common, env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "starting the daemon" in r.stdout
    try:
        assert _wait(lambda: D.PidFile.running_pid(tmp_path / "daemon/kg_agent.pid") is not None), "detached daemon is up"
        assert _wait(lambda: _state(tmp_path).get("last_exit") == 0)
        pid = D.PidFile.running_pid(tmp_path / "daemon/kg_agent.pid")
        assert os.getpgid(pid) != os.getpgid(os.getpid())                 # own session
        assert (tmp_path / "logs/daemon.log").exists() and "daemon up" in (tmp_path / "logs/daemon.log").read_text()
    finally:
        s = subprocess.run(base + ["stop", "--home", str(tmp_path)], env=env, capture_output=True, text=True, timeout=60)
    assert s.returncode == 0 and D.PidFile.running_pid(tmp_path / "daemon/kg_agent.pid") is None
