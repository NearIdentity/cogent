"""A real daemon for the KG update agent: schedule, supervise, recover, report.

``kg_agent.py run`` does one update pass. This module keeps doing it, every day:

* **schedule**  one or more times of day in any time zone (``--at 06:00,18:00 --tz Europe/Berlin``), DST-correct,
                optional random jitter; the agent run itself is a *subprocess* (a crash, a leak or a hung download
                cannot take the daemon down; it is killed after ``--run-timeout``)
* **catch-up**  after downtime / a sleeping laptop the missed slot is run on start (``--catch-up``, default on)
* **retry**     a failed run is retried with exponential backoff (``--retries``, ``--retry-delay``), never past the next slot
* **control**   ``start | stop | restart | status | run-now | serve``; ``SIGTERM``/``SIGINT`` stop gracefully (the running
                agent is asked to stop, then killed), ``SIGUSR1`` runs now, ``SIGHUP`` reopens the log file
* **safety**    a locked PID file allows a single daemon per home folder (the agent has its own run lock as well)
* **ops**       rotating log file, ``state.json`` (``status`` reads it), systemd ``Type=notify`` + watchdog support,
                failure / warning hooks (``--on-failure CMD``, ``--on-warnings CMD``)

Typical use::

    python src/kg_daemon.py start  --at 06:00 --tz Europe/London -- --extractor hybrid     # detach, log to file
    python src/kg_daemon.py status
    python src/kg_daemon.py run-now
    python src/kg_daemon.py stop
    python src/kg_daemon.py serve --at 06:00        # foreground; what systemd / Docker should run

Everything after ``--`` is passed to ``kg_agent.py run``. POSIX only (fork, flock, signals); on Windows use Task Scheduler
with ``kg_agent.py run``. See ``docs/scheduling.md`` for daemon vs. cron vs. systemd timer.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import random
import shlex
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

try:                                    # POSIX
    import fcntl
except ImportError:                     # pragma: no cover - Windows
    fcntl = None                        # type: ignore[assignment]

log = logging.getLogger("kg_daemon")
HERE = Path(__file__).resolve().parent
POLL = 1.0                              # seconds between checks while waiting / supervising


# --------------------------------------------------------------------------
# Schedule (pure, testable)
# --------------------------------------------------------------------------


@dataclass
class Schedule:
    """Daily times of day in a time zone."""

    times: list[dtime]
    tz: tzinfo
    jitter: float = 0.0                 # seconds; a stable pseudo-random delay per slot

    @classmethod
    def parse(cls, spec: str, tz: Optional[str] = None, jitter: float = 0.0) -> "Schedule":
        times = []
        for part in spec.split(","):
            h, _, m = part.strip().partition(":")
            if not h.isdigit() or not (0 <= int(h) < 24) or not m.isdigit() or not (0 <= int(m) < 60):
                raise ValueError(f"bad time {part!r}: use HH:MM[,HH:MM...]")
            times.append(dtime(int(h), int(m)))
        zone = ZoneInfo(tz) if tz else datetime.now().astimezone().tzinfo
        return cls(sorted(set(times)), zone, jitter)               # type: ignore[arg-type]

    def _slots(self, around: datetime) -> list[datetime]:
        d = around.astimezone(self.tz).date()
        out = []
        for off in (-2, -1, 0, 1, 2):
            day = d + timedelta(days=off)
            for t in self.times:
                out.append(datetime.combine(day, t, tzinfo=self.tz))
        return sorted(out)

    def fire_time(self, slot: datetime) -> datetime:
        """The slot plus its stable jitter."""
        if not self.jitter:
            return slot
        return slot + timedelta(seconds=random.Random(slot.isoformat()).uniform(0, self.jitter))

    def next_after(self, now: datetime) -> datetime:
        """The next slot whose fire time is after ``now``."""
        return next(s for s in self._slots(now) if _utc(self.fire_time(s)) > _utc(now))

    def last_due(self, now: datetime) -> datetime:
        """The latest slot whose fire time is at or before ``now``."""
        return [s for s in self._slots(now) if _utc(self.fire_time(s)) <= _utc(now)][-1]


# --------------------------------------------------------------------------
# State file
# --------------------------------------------------------------------------


@dataclass
class DaemonState:
    pid: Optional[int] = None
    started: Optional[str] = None
    schedule: str = ""
    next_run: Optional[str] = None
    last_slot_done: Optional[str] = None      # latest slot that has been run (or deliberately skipped)
    last_trigger: Optional[str] = None        # schedule | catch-up | retry | signal | start
    last_start: Optional[str] = None
    last_end: Optional[str] = None
    last_exit: Optional[int] = None
    last_success: Optional[str] = None
    last_summary: Optional[dict[str, Any]] = None
    consecutive_failures: int = 0           # failed runs in a row (across slots)
    retries_used: int = 0                   # retries spent on the current slot
    retry_at: Optional[str] = None
    running: bool = False
    status: str = "idle"


def _utc(dt: datetime) -> datetime:
    """Aware datetimes in one zone compare by wall clock in Python; compare in UTC so DST changes are right."""
    return dt.astimezone(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


def _parse(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


# --------------------------------------------------------------------------
# systemd notification (no dependency)
# --------------------------------------------------------------------------


def sd_notify(message: str) -> bool:
    """Send a message to systemd's notify socket (READY=1, WATCHDOG=1, STATUS=…). No-op outside systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(message.encode())
        return True
    except OSError:
        return False


def watchdog_interval() -> Optional[float]:
    """Half of systemd's WatchdogSec (seconds) when a watchdog is configured."""
    usec = os.environ.get("WATCHDOG_USEC")
    return int(usec) / 2e6 if usec and usec.isdigit() else None


# --------------------------------------------------------------------------
# Single-instance PID file
# --------------------------------------------------------------------------


class PidFile:
    """A PID file held with an exclusive ``flock``: the lock dies with the process, so there are no stale locks."""

    def __init__(self, path: Path):
        self.path, self._fh = path, None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        if fcntl is not None:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh:
            try:
                self.path.unlink()
            except OSError:
                pass
            self._fh.close()
            self._fh = None

    @staticmethod
    def running_pid(path: Path) -> Optional[int]:
        """PID of the live daemon holding ``path``, else None."""
        try:
            pid = int(path.read_text().strip())
        except (OSError, ValueError):
            return None
        if fcntl is not None:
            with open(path, "a+") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fh, fcntl.LOCK_UN)
                    return None                       # nobody holds it: stale
                except OSError:
                    return pid
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            return None


# --------------------------------------------------------------------------
# Running the agent
# --------------------------------------------------------------------------


def run_command(cmd: list[str], *, timeout: float, stop: Callable[[], bool], grace: float = 30.0,
                out_path: Optional[Path] = None, tick: Callable[[], None] = lambda: None,
                env: Optional[dict[str, str]] = None) -> tuple[int, str]:
    """Run ``cmd`` in its own process group and supervise it. Returns (exit code, reason): reason is
    "" / "timeout" / "stopped". The group gets SIGTERM, then SIGKILL after ``grace`` seconds."""
    out = open(out_path, "ab") if out_path else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                                env={**os.environ, **(env or {})})
        t0, reason, termed_at = time.monotonic(), "", None
        while proc.poll() is None:
            if not reason and stop():
                reason = "stopped"
            if not reason and time.monotonic() - t0 > timeout:
                reason = "timeout"
            if reason and termed_at is None:
                log.warning("%s: terminating the agent (pid %d)", reason, proc.pid)
                _signal_group(proc.pid, signal.SIGTERM)
                termed_at = time.monotonic()
            if termed_at is not None and time.monotonic() - termed_at > grace:
                log.error("agent did not stop within %ds: killing it", grace)
                _signal_group(proc.pid, signal.SIGKILL)
                termed_at = time.monotonic() + 1e9                 # only once
            tick()
            time.sleep(0.2)
        return proc.returncode, reason
    finally:
        if out is not subprocess.DEVNULL:
            out.close()


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


# --------------------------------------------------------------------------
# The daemon
# --------------------------------------------------------------------------


@dataclass
class Options:
    home: Path
    schedule: Schedule
    agent_cmd: list[str]
    catch_up: bool = True
    run_on_start: bool = False
    retries: int = 2
    retry_delay: float = 600.0
    run_timeout: float = 4 * 3600.0
    stop_grace: float = 30.0
    on_failure: Optional[str] = None
    on_warnings: Optional[str] = None
    spec: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Daemon:
    """The scheduling loop. ``tick(now)`` decides and performs at most one run; ``serve()`` calls it forever."""

    def __init__(self, opts: Options, runner: Optional[Callable[..., tuple[int, str]]] = None,
                 clock: Optional[Callable[[], datetime]] = None, sleeper: Callable[[float], None] = time.sleep):
        self.o = opts
        self.dir = opts.home / "daemon"
        self.state_path = self.dir / "state.json"
        self.pid = PidFile(self.dir / "kg_agent.pid")
        self.runner = runner or self._run_agent
        self.clock = clock or (lambda: datetime.now(opts.schedule.tz))
        self.sleep = sleeper
        self.stop_requested = False
        self.run_now_requested = False
        self.reopen_logs = False
        self.state = self._load_state()

    # ---- state ---------------------------------------------------------------
    def _load_state(self) -> DaemonState:
        try:
            raw = json.loads(self.state_path.read_text())
            return DaemonState(**{k: v for k, v in raw.items() if k in DaemonState.__dataclass_fields__})
        except (OSError, ValueError):
            return DaemonState()

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), indent=2))
        os.replace(tmp, self.state_path)

    # ---- the decision -----------------------------------------------------------
    def due(self, now: datetime) -> Optional[tuple[str, datetime]]:
        """(trigger, slot) when a run is due now, else None. Also marks skipped slots as handled."""
        st, sch = self.state, self.o.schedule
        if self.run_now_requested:
            self.run_now_requested = False
            return "signal", sch.last_due(now)
        slot = sch.last_due(now)
        done = _parse(st.last_slot_done)
        if done is None or _utc(done) < _utc(slot):
            late = (_utc(now) - _utc(sch.fire_time(slot))).total_seconds()
            first_ever = done is None and st.last_start is None
            st.last_slot_done = _iso(slot)                    # handled either way: never fire a slot twice
            if first_ever:
                return None                                   # fresh install: wait for the next slot (see run_on_start)
            if late > 2 * POLL + 60 and not self.o.catch_up:
                log.warning("missed the %s slot (%.0f s late) and catch-up is off: skipping", _iso(slot), late)
                return None
            return ("catch-up" if late > 2 * POLL + 60 else "schedule"), slot
        retry = _parse(st.retry_at)
        if retry and _utc(retry) <= _utc(now):
            return "retry", slot
        return None

    def next_event(self, now: datetime) -> datetime:
        nxt = self.o.schedule.fire_time(self.o.schedule.next_after(now))
        retry = _parse(self.state.retry_at)
        return min(nxt, retry, key=_utc) if retry and _utc(retry) > _utc(now) else nxt

    # ---- one iteration ----------------------------------------------------------
    def tick(self, now: Optional[datetime] = None) -> Optional[str]:
        """Run the agent if something is due. Returns the trigger that fired (or None)."""
        now = now or self.clock()
        if self.stop_requested:
            return None
        if self.o.run_on_start and self.state.last_start is None and self.state.last_slot_done is None:
            trig: Optional[tuple[str, datetime]] = ("start", self.o.schedule.last_due(now))
            self.state.last_slot_done = _iso(trig[1])
        else:
            trig = self.due(now)
        if trig is None:
            self.state.next_run = _iso(self.next_event(now))
            self._save()
            return None
        self._execute(trig[0], now)
        return trig[0]

    def _execute(self, trigger: str, now: datetime) -> None:
        st = self.state
        st.running, st.status, st.last_trigger, st.last_start = True, f"running ({trigger})", trigger, _iso(now)
        st.retry_at = None
        if trigger != "retry":
            st.retries_used = 0
        self._save()
        sd_notify(f"STATUS=running the agent ({trigger})")
        log.info("run started (%s)", trigger)
        t0 = time.monotonic()
        code, reason = self.runner(stop=lambda: self.stop_requested, tick=self._heartbeat)
        end = self.clock()
        st.running, st.last_end, st.last_exit = False, _iso(end), code
        summary = self._last_run_summary()
        st.last_summary = summary
        ok = code == 0 and not reason
        if ok:
            st.last_success, st.consecutive_failures, st.status = _iso(end), 0, "idle"
            log.info("run finished OK in %.0f s %s", time.monotonic() - t0, summary or "")
        else:
            st.consecutive_failures += 1
            st.status = f"last run failed ({reason or 'exit ' + str(code)})"
            log.error("run FAILED (%s) after %.0f s; failure %d", reason or f"exit {code}", time.monotonic() - t0,
                      st.consecutive_failures)
            if reason != "stopped" and st.retries_used < self.o.retries:
                delay = self.o.retry_delay * 2 ** st.retries_used
                nxt = self.o.schedule.fire_time(self.o.schedule.next_after(end))
                retry = end + timedelta(seconds=delay)
                if _utc(retry) < _utc(nxt):                                  # never retry into the next slot
                    st.retries_used += 1
                    st.retry_at = _iso(retry)
                    log.info("retry %d of %d scheduled at %s", st.retries_used, self.o.retries, st.retry_at)
        st.next_run = _iso(self.next_event(end))
        self._save()
        sd_notify(f"STATUS={st.status}; next run {st.next_run}")
        self._hooks(ok, code, reason, summary)

    # ---- agent subprocess ---------------------------------------------------------
    def _run_agent(self, stop: Callable[[], bool], tick: Callable[[], None]) -> tuple[int, str]:
        out = self.o.home / "logs" / "agent-stdout.log"
        out.parent.mkdir(parents=True, exist_ok=True)
        return run_command(self.o.agent_cmd, timeout=self.o.run_timeout, stop=stop, grace=self.o.stop_grace,
                           out_path=out, tick=tick)

    _last_ping = 0.0

    def _heartbeat(self) -> None:
        """Called while an agent run is supervised: keep the systemd watchdog fed."""
        now = time.monotonic()
        if now - self._last_ping >= (watchdog_interval() or 10.0):
            self._last_ping = now
            sd_notify("WATCHDOG=1")

    def _last_run_summary(self) -> Optional[dict[str, Any]]:
        """Summary counters the agent wrote for its latest run (logs/runs.jsonl)."""
        try:
            last = (self.o.home / "logs" / "runs.jsonl").read_text().strip().splitlines()[-1]
            rec = json.loads(last)
            s = rec.get("summary", {})
            return {"run_id": rec.get("run_id"), **{k: s[k] for k in ("documents", "docs_updated", "docs_error",
                                                                      "warnings", "errors", "not_current") if k in s}}
        except (OSError, ValueError, IndexError):
            return None

    def _hooks(self, ok: bool, code: int, reason: str, summary: Optional[dict[str, Any]]) -> None:
        env = {"COGENT_EXIT": str(code), "COGENT_REASON": reason, "COGENT_HOME": str(self.o.home),
               "COGENT_REPORT": str(self.o.home / "logs" / "report.md"),
               "COGENT_WARNINGS": str((summary or {}).get("warnings", 0)),
               "COGENT_FAILURES": str(self.state.consecutive_failures)}
        gave_up = not ok and reason != "stopped" and self.state.retry_at is None
        for cmd, fire in ((self.o.on_failure, gave_up), (self.o.on_warnings, ok and (summary or {}).get("warnings", 0) > 0)):
            if cmd and fire:
                try:
                    subprocess.run(shlex.split(cmd), env={**os.environ, **env}, timeout=60, check=False)
                except Exception as exc:                       # a broken hook must not stop the daemon
                    log.warning("hook %r failed: %s", cmd, exc)

    # ---- serving ----------------------------------------------------------------------
    def install_signals(self) -> None:
        def stop(signum, _):
            log.info("signal %s: stopping", signal.Signals(signum).name)
            self.stop_requested = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGUSR1, lambda *_: setattr(self, "run_now_requested", True))
        signal.signal(signal.SIGHUP, lambda *_: setattr(self, "reopen_logs", True))

    def serve(self) -> int:
        if not self.pid.acquire():
            log.error("another daemon already runs for %s (pid file %s)", self.o.home, self.pid.path)
            return 1
        try:
            self.install_signals()
            st = self.state
            st.pid, st.started, st.schedule, st.running, st.status = os.getpid(), _iso(self.clock()), self.o.spec, False, "idle"
            self._save()
            log.info("daemon up (pid %d): schedule %s %s; catch-up %s; next run %s", os.getpid(), self.o.spec,
                     self.o.schedule.tz, "on" if self.o.catch_up else "off", _iso(self.next_event(self.clock())))
            sd_notify("READY=1")
            wd, last_wd = watchdog_interval(), 0.0
            while not self.stop_requested:
                self.tick()
                if self.reopen_logs:
                    self.reopen_logs = False
                    _reopen_file_handlers()
                until = (_utc(self.next_event(self.clock())) - _utc(self.clock())).total_seconds()
                end = time.monotonic() + max(0.0, min(until, 30.0))
                while not self.stop_requested and not self.run_now_requested and time.monotonic() < end:
                    self.sleep(POLL)
                    if wd and time.monotonic() - last_wd >= wd:
                        sd_notify("WATCHDOG=1")
                        last_wd = time.monotonic()
            return 0
        finally:
            sd_notify("STOPPING=1")
            self.state.pid, self.state.running, self.state.status = None, False, "stopped"
            self._save()
            self.pid.release()
            log.info("daemon stopped")


def _reopen_file_handlers() -> None:
    """After an external log rotation (SIGHUP): close the log files; logging reopens them on the next record."""
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.FileHandler):
            h.close()


# --------------------------------------------------------------------------
# Daemonizing, control commands
# --------------------------------------------------------------------------


def daemonize(log_path: Path) -> None:
    """Classic double fork: detach from the terminal, new session, stdio to /dev/null."""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)


def setup_logging(log_file: Optional[Path], stderr: bool, verbose: bool) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        h: logging.Handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5)
        h.setFormatter(fmt)
        root.addHandler(h)
    if stderr:
        h2 = logging.StreamHandler()
        h2.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))   # journald adds the timestamp
        root.addHandler(h2)


def build_options(a: argparse.Namespace, passthrough: list[str]) -> Options:
    home = Path(a.home).resolve()
    sched = Schedule.parse(a.at, a.tz, a.jitter)
    if a.agent_cmd:
        cmd = shlex.split(a.agent_cmd)
    else:
        cmd = [sys.executable, str(HERE / "kg_agent.py"), "run", "--home", str(home),
               "--standards", str(Path(a.standards).resolve()), "--reports", str(Path(a.reports).resolve()), *passthrough]
    return Options(home=home, schedule=sched, agent_cmd=cmd, catch_up=a.catch_up, run_on_start=a.run_on_start,
                   retries=a.retries, retry_delay=a.retry_delay, run_timeout=a.run_timeout, stop_grace=a.stop_grace,
                   on_failure=a.on_failure, on_warnings=a.on_warnings, spec=a.at)


def cmd_status(home: Path) -> int:
    pidfile = home / "daemon" / "kg_agent.pid"
    pid = PidFile.running_pid(pidfile)
    try:
        st = DaemonState(**json.loads((home / "daemon" / "state.json").read_text()))
    except (OSError, ValueError, TypeError):
        st = DaemonState()
    print(f"daemon:        {'running (pid %d)' % pid if pid else 'not running'}")
    if st.schedule:
        print(f"schedule:      {st.schedule}")
    print(f"state:         {st.status if pid else 'stopped'}")
    print(f"next run:      {st.next_run or '-'}")
    print(f"last run:      {st.last_start or '-'} -> {st.last_end or '-'}  exit {st.last_exit}  ({st.last_trigger or '-'})")
    print(f"last success:  {st.last_success or '-'}   consecutive failures: {st.consecutive_failures}"
          + (f"   retry at {st.retry_at}" if st.retry_at else ""))
    if st.last_summary:
        print(f"last summary:  {st.last_summary}")
    alerts = home / "state" / "alerts.json"
    try:
        n = sum(1 for a in json.loads(alerts.read_text()).values() if a.get("severity") == "warning")
        print(f"open warnings: {n}  (python src/kg_agent.py status --home {home})")
    except (OSError, ValueError):
        pass
    return 0 if pid else 3


def cmd_stop(home: Path, timeout: float) -> int:
    pidfile = home / "daemon" / "kg_agent.pid"
    pid = PidFile.running_pid(pidfile)
    if not pid:
        print("daemon is not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not PidFile.running_pid(pidfile):
            print("stopped")
            return 0
        time.sleep(0.3)
    print(f"daemon (pid {pid}) is still stopping after {timeout:.0f} s (an agent run is being wound down)", file=sys.stderr)
    return 1


def parse_args(argv: Optional[list[str]] = None) -> tuple[argparse.Namespace, list[str]]:
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description="Daemon for the KG update agent (arguments after -- go to `kg_agent.py run`).")
    ap.add_argument("command", choices=["serve", "start", "stop", "restart", "status", "run-now"])
    ap.add_argument("--home", default="kg_agent_home")
    ap.add_argument("--standards", default="config/standards.json")
    ap.add_argument("--reports", default="config/reports.json")
    ap.add_argument("--at", default="06:00", help="daily time(s) HH:MM[,HH:MM] (default 06:00)")
    ap.add_argument("--tz", help="IANA time zone of --at, e.g. Europe/London (default: the machine's)")
    ap.add_argument("--jitter", type=float, default=0.0, help="spread each run by up to N seconds (stable per slot)")
    ap.add_argument("--catch-up", action=argparse.BooleanOptionalAction, default=True,
                    help="run a slot that was missed while the daemon was down (default on)")
    ap.add_argument("--run-on-start", action="store_true", help="also run once when a fresh daemon starts")
    ap.add_argument("--retries", type=int, default=2, help="retries after a failed run (default 2)")
    ap.add_argument("--retry-delay", type=float, default=600.0, help="first retry delay in seconds; doubles each time")
    ap.add_argument("--run-timeout", type=float, default=4 * 3600, help="kill an agent run after N seconds")
    ap.add_argument("--stop-grace", type=float, default=30.0, help="seconds between SIGTERM and SIGKILL of the agent")
    ap.add_argument("--on-failure", help="command run when a run fails for good (env: COGENT_EXIT, COGENT_REASON, …)")
    ap.add_argument("--on-warnings", help="command run after a run that left open warnings (env: COGENT_WARNINGS, …)")
    ap.add_argument("--log-file", help="log file (default <home>/logs/daemon.log for `start`; `serve` logs to stderr)")
    ap.add_argument("--agent-cmd", help=argparse.SUPPRESS)               # tests: replace the agent command
    ap.add_argument("--stop-timeout", type=float, default=60.0, help="how long `stop` waits")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv), passthrough


def main(argv: Optional[list[str]] = None) -> int:
    a, passthrough = parse_args(argv)
    home = Path(a.home).resolve()
    if a.command == "status":
        return cmd_status(home)
    if a.command == "stop":
        return cmd_stop(home, a.stop_timeout)
    if a.command == "restart":
        cmd_stop(home, a.stop_timeout)
        a.command = "start"
    if a.command == "run-now":
        pid = PidFile.running_pid(home / "daemon" / "kg_agent.pid")
        if pid:
            os.kill(pid, signal.SIGUSR1)
            print(f"asked the daemon (pid {pid}) to run now; follow with: python src/kg_daemon.py status")
            return 0
        setup_logging(None, True, a.verbose)
        d = Daemon(build_options(a, passthrough))
        d._execute("signal", d.clock())                                   # no daemon: just run once, here
        return d.state.last_exit or 0

    opts = build_options(a, passthrough)
    log_file = Path(a.log_file) if a.log_file else (home / "logs" / "daemon.log" if a.command == "start" else None)
    if a.command == "start":
        if PidFile.running_pid(home / "daemon" / "kg_agent.pid"):
            print("daemon is already running", file=sys.stderr)
            return 1
        if fcntl is None:
            print("`start` needs a POSIX system; use `serve` under a supervisor", file=sys.stderr)
            return 2
        setup_logging(log_file, True, a.verbose)                          # stderr is shown until we detach
        print(f"starting the daemon; log: {log_file}")
        daemonize(log_file)
        setup_logging(log_file, False, a.verbose)
    else:
        setup_logging(log_file, True, a.verbose)
    return Daemon(opts).serve()


if __name__ == "__main__":
    sys.exit(main())
