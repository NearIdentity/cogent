# Scheduling the daily run: daemon vs. cron vs. systemd

The agent (`kg_agent.py run`) is a batch job: minutes to an hour, many downloads, writes files and the two JSON
configurations, and it is **idempotent** (conditional requests, content fingerprints, atomic writes, its own run lock). That
makes every option below safe to use; they differ in *who notices that something went wrong or was missed*.

## What the three give you

| | **cron** | **systemd timer** (+ oneshot service) | **`kg_daemon.py`** (Python daemon) |
|---|---|---|---|
| Setup | one crontab line | two unit files, `daemon-reload`, `enable` | `start` / `serve`, or a unit/launchd/Docker around `serve` |
| Runs what was missed while the machine was off | **no** (needs anacron / an `@reboot` hack) | **yes**, `Persistent=true` | **yes**, `--catch-up` (default) |
| Overlap protection | none (`flock` by hand; the agent's own lock saves you) | built in (a running oneshot is not started twice) | built in (one daemon per home, one run at a time) |
| Environment | bare `PATH`, no venv, no locale | explicit (`Environment=`, `WorkingDirectory=`) | whatever started it (explicit with `serve` under a unit) |
| Retry of a failed run | none | `Restart=on-failure`, `RestartSec`, `StartLimit*` | `--retries`, exponential `--retry-delay`, never past the next slot |
| Kill a hung run | no | `TimeoutStartSec` | `--run-timeout` (SIGTERM, then SIGKILL after `--stop-grace`) |
| Logs | mail / a redirect you remember to add | journald (`journalctl -u`), per run, filterable | `logs/daemon.log` (rotating) or journald with `serve` |
| "When did it last run, and did it work?" | not answerable | `systemctl status`, `list-timers` (next, last) | `kg_daemon.py status` (next, last, exit, failures, open warnings) |
| Failure notification | `MAILTO` if mail works | `OnFailure=` unit | `--on-failure CMD`, `--on-warnings CMD` |
| Resource limits, sandboxing | `nice`/`ulimit` by hand | `MemoryMax`, `CPUQuota`, `Nice`, `ProtectSystem`, … | none of its own (inherits from the unit if run under systemd) |
| Time zone / DST | machine zone (`CRON_TZ` only on cronie); skipped/doubled runs around DST depend on the implementation | machine zone or `OnCalendar=… Europe/London`; DST-correct | `--tz`, DST-correct (tested) |
| Jitter (spread load) | `sleep $((RANDOM % 600))` by hand | `RandomizedDelaySec` | `--jitter` |
| Platforms | any Unix, macOS, WSL, most containers | Linux with systemd only | POSIX (Linux, macOS, WSL); Windows: no |
| Moving parts that can die | cron (rock solid) | pid 1 | **the daemon itself**: needs a supervisor to be reliable |

## Merits and demerits

**cron**
* \+ Everywhere, one line, nothing to learn, survives for decades.
* \− Silent about misses: a machine that is off, suspended or rebooting at 06:00 simply skips the day.
* \− No notion of success: you get mail (if configured), a log file (if you redirected it), nothing else.
* \− Thin environment is the classic cause of "works in my shell, not in cron" (venv, `PATH`, browsers for Playwright, locale).
* \− No retry, no timeout, no limits. Fine for tiny jobs, uncomfortable for a job that downloads 80 documents.

**systemd timer**
* \+ Fixes cron's weaknesses declaratively: catch-up (`Persistent`), jitter, ordering after the network, retry,
  timeout, memory/CPU limits, sandboxing, journald, `OnFailure=`; `systemctl list-timers` shows next/last in one command.
* \+ Nothing of your own to keep alive: the supervision is pid 1's job. This is the *simplest robust* choice on Linux.
* \− Linux-only; root (or `loginctl enable-linger` + `systemctl --user`) to install; more concepts and files; calendar
  syntax and the "timer → service" indirection take a moment to learn.
* \− `Persistent=true` replays a *missed trigger*, not a *failed run*: retries need `Restart=on-failure` on the service
  (works for `Type=oneshot` since systemd 244) or the daemon.

**Python daemon (`kg_daemon.py`)**
* \+ Portable and self-contained; app-aware: it knows what "success" means (exit code *and* the agent's summary), shows open
  warnings in `status`, triggers `--on-warnings`, supports `run-now`, and its behaviour is unit-tested (schedule maths,
  catch-up, backoff, signals, detach).
* \+ Runs the agent in a subprocess: a crash, memory growth or a hung browser cannot take the daemon down.
* \− A long-lived process is one more thing that can die, leak or be forgotten; on its own it is **less** reliable than cron.
  To be dependable it needs a supervisor (systemd `Restart=`/watchdog, launchd, Docker `restart: always`), and once you have
  systemd, the timer does the same job with less code.
* \− `start` (double fork) is the old-fashioned way to detach; under any modern supervisor use `serve` in the foreground.

## Recommendation

1. **Linux server or workstation with systemd: use the timer** (`deploy/cogent-run.{service,timer}`). It is the least code,
   has catch-up, retry, timeout, limits and logs, and nothing to babysit.
2. **macOS, WSL, minimal containers, or "cron is what we have"**: cron **plus** `flock` (provided line in
   `deploy/cogent.cron`) and a catch-up (anacron, or `@reboot … kg_daemon.py run-now`). Check `logs/report.md` and `status`
   instead of waiting for mail.
3. **No scheduler you trust, Docker, or you want retry/catch-up/status/hooks identical on every platform**: run
   `kg_daemon.py serve` under whatever supervises processes there (`deploy/cogent-daemon.service` for systemd; Docker
   `restart: unless-stopped`). Do not combine it with the timer or cron: pick one scheduler.

Whatever you choose, the overlap lock (`<home>/.lock`) and the idempotent run mean a double trigger costs time, not data.
