"""Telemetry for the agent and the MCP server: OpenTelemetry traces and Prometheus metrics.

**Traces** use the OpenTelemetry API; nothing is required to run without it. Install ``opentelemetry-sdk`` (and
``opentelemetry-exporter-otlp-proto-http``) and point ``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``--otlp-endpoint`` at a collector, Jaeger, Tempo ... to see
them. Spans: ``agent.run`` > ``agent.step <name>`` > operations (``fetch``, ``sync_doc``, ``track_url`` ...) > ``llm.chat`` / ``embed``; in the MCP
server ``mcp.tool <name>`` (with the caller's ``traceparent`` as parent when the client sends one). Without the SDK every call here is a no-op.

**Metrics** are kept in a small registry that renders the Prometheus text format (no client library needed):

* the **MCP server** keeps its registry in memory and serves it on ``/metrics`` (``--metrics``), together with the agent's results below;
* the **agent** is a batch job, so a Prometheus scrape of the process would miss it. Each run collects its own metrics, then merges them into
  ``<home>/metrics/agent_metrics.json`` (counters and histograms accumulate across runs, gauges hold the last run), writes the same data as
  ``<home>/metrics/cogent_agent.prom`` (node_exporter textfile collector format), appends one line to ``<home>/logs/metrics.jsonl`` and, with
  ``--pushgateway URL``, pushes it to a Prometheus Pushgateway. The MCP server's ``/metrics`` merges the persisted agent metrics in, so one scrape
  target shows everything.

Telemetry must never break a run: every recording function swallows its own errors.
"""
from __future__ import annotations

import bisect
import contextlib
import json
import logging
import math
import os
import threading
import time
import urllib.request
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger("kg_telemetry")

# --------------------------------------------------------------------------------------------------------------------------------- schema
OP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)
RUN_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200)

# name -> (kind, help, buckets)
SCHEMA: dict[str, tuple[str, str, Optional[tuple]]] = {
    # --- agent runs
    "cogent_agent_runs_total": ("counter", "Agent runs by result (ok = finished, failed = raised or left errors).", None),
    "cogent_agent_run_duration_seconds": ("histogram", "Wall-clock duration of an agent run.", RUN_BUCKETS),
    "cogent_agent_step_duration_seconds": ("histogram", "Duration of a workflow step (init, track, apply, sync_docs, assemble, versions, combine, alerts, finalize).", OP_BUCKETS + (1200, 3600)),
    "cogent_agent_operation_duration_seconds": ("histogram", "Duration of one operation inside a run (fetch, track_url, sync_doc, tabledb_build ...).", OP_BUCKETS),
    "cogent_agent_operations_total": ("counter", "Operations by name and outcome (ok | error).", None),
    "cogent_agent_failures_total": ("counter", "Failed operations / stages (a run keeps going after most of them).", None),
    "cogent_agent_retries_total": ("counter", "Retries by component (fetch, llm, embed, daemon_run).", None),
    "cogent_agent_findings_total": ("counter", "Edition findings by outcome: auto_applied (configuration rewritten), needs_review (left for a human), human_resolved (a pending finding a human has since dealt with).", None),
    "cogent_agent_last_run_timestamp_seconds": ("gauge", "Unix time the last agent run finished.", None),
    "cogent_agent_last_run_duration_seconds": ("gauge", "Duration of the last agent run.", None),
    "cogent_agent_last_run_success": ("gauge", "1 if the last agent run finished without errors, else 0.", None),
    "cogent_agent_last_run_errors": ("gauge", "Number of errors in the last agent run.", None),
    "cogent_agent_last_run_tokens": ("gauge", "LLM tokens used by the last agent run, by direction (input | output).", None),
    "cogent_agent_last_run_cost_usd": ("gauge", "Cost of the LLM tokens of the last agent run (0 unless prices are configured, see COGENT_LLM_PRICES).", None),
    "cogent_agent_last_run_findings": ("gauge", "Edition findings of the last agent run by outcome.", None),
    "cogent_agent_operation_latency_seconds": ("gauge", "Exact latency quantile of an operation in the last agent run (quantile = 0.5 | 0.95).", None),
    # --- LLM / embeddings (agent and server)
    "cogent_llm_calls_total": ("counter", "LLM and embedding calls by service, model, kind and outcome.", None),
    "cogent_llm_tokens_total": ("counter", "Tokens by service, model and direction (input | output), as reported by the model server.", None),
    "cogent_llm_cost_usd_total": ("counter", "Cost of those tokens at the configured prices (0 for local models unless priced).", None),
    "cogent_llm_call_duration_seconds": ("histogram", "Duration of LLM / embedding calls.", OP_BUCKETS),
    "cogent_llm_outputs_total": ("counter", "Checked LLM outputs: kind = join_suggestion | triple, outcome = grounded | hallucinated.", None),
    "cogent_llm_hallucination_ratio": ("gauge", "Hallucinated share of the checked LLM outputs of the last run (see docs/observability.md for the definition).", None),
    # --- join inference judge
    "cogent_join_judge_candidates_total": ("counter", "Judged join candidates by agreement of the judge's votes (unanimous | split).", None),
    "cogent_join_judge_verdicts_total": ("counter", "Judge verdicts (accept | reject | uncertain).", None),
    "cogent_join_llm_vs_deterministic_total": ("counter", "Join candidates by who proposed them: both | llm_only | deterministic_only.", None),
    "cogent_join_judge_agreement_ratio": ("gauge", "Share of judged candidates with unanimous votes, last run.", None),
    # --- MCP server
    "cogent_mcp_tool_calls_total": ("counter", "MCP tool calls by tool and outcome (ok | not_found | error).", None),
    "cogent_mcp_tool_duration_seconds": ("histogram", "MCP tool latency.", OP_BUCKETS),
}


def _key(labels: dict[str, Any]) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(x: float) -> str:
    if x == math.inf:
        return "+Inf"
    return repr(float(x)) if not float(x).is_integer() else str(int(x)) if abs(x) < 1e15 else repr(float(x))


class Registry:
    """Counters, gauges and histograms with labels; renders the Prometheus text format; can be saved, loaded and merged."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.series: dict[str, dict[tuple, Any]] = {name: {} for name in SCHEMA}

    # ------------------------------------------------------------------ recording
    def inc(self, name: str, value: float = 1.0, **labels: Any) -> None:
        kind = SCHEMA[name][0]
        assert kind == "counter", name
        with self._lock:
            s = self.series[name]
            k = _key(labels)
            s[k] = s.get(k, 0.0) + value

    def set(self, name: str, value: float, **labels: Any) -> None:
        assert SCHEMA[name][0] == "gauge", name
        with self._lock:
            self.series[name][_key(labels)] = float(value)

    def observe(self, name: str, value: float, **labels: Any) -> None:
        kind, _, buckets = SCHEMA[name]
        assert kind == "histogram" and buckets, name
        with self._lock:
            s = self.series[name]
            k = _key(labels)
            h = s.get(k)
            if h is None:
                h = s[k] = {"b": [0] * (len(buckets) + 1), "sum": 0.0, "count": 0}
            h["b"][bisect.bisect_left(buckets, value)] += 1
            h["sum"] += value
            h["count"] += 1

    # ------------------------------------------------------------------ reading
    def value(self, name: str, **labels: Any) -> float:
        """Sum of the counter / gauge series whose labels include ``labels``."""
        want = set(_key(labels))
        with self._lock:
            return sum(v for k, v in self.series[name].items() if want <= set(k) and not isinstance(v, dict))

    def count(self, name: str, **labels: Any) -> int:
        """Observation count of the histogram series whose labels include ``labels``."""
        want = set(_key(labels))
        with self._lock:
            return sum(v["count"] for k, v in self.series[name].items() if want <= set(k))

    def by_label(self, name: str, label: str) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        with self._lock:
            for k, v in self.series[name].items():
                if not isinstance(v, dict):
                    out[dict(k).get(label, "")] += v
        return dict(out)

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {"version": 1, "series": {n: [[list(map(list, k)), v] for k, v in s.items()] for n, s in self.series.items() if s}}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Registry":
        r = cls()
        for name, rows in (data.get("series") or {}).items():
            if name not in SCHEMA:
                continue
            for k, v in rows:
                if isinstance(v, dict):
                    nb = len(SCHEMA[name][2] or ()) + 1
                    if len(v.get("b", [])) != nb:             # bucket layout changed: drop the old series rather than mix them
                        continue
                r.series[name][tuple(map(tuple, k))] = v
        return r

    def merge(self, other: "Registry") -> None:
        """Counters and histograms add, gauges take the other's value."""
        with self._lock, other._lock:
            for name, (kind, _, _) in SCHEMA.items():
                mine = self.series[name]
                for k, v in other.series[name].items():
                    if kind == "gauge":
                        mine[k] = v
                    elif kind == "counter":
                        mine[k] = mine.get(k, 0.0) + v
                    else:
                        h = mine.get(k)
                        if h is None:
                            mine[k] = {"b": list(v["b"]), "sum": v["sum"], "count": v["count"]}
                        else:
                            h["b"] = [a + b for a, b in zip(h["b"], v["b"])]
                            h["sum"] += v["sum"]
                            h["count"] += v["count"]

    # ------------------------------------------------------------------ exposition
    def render(self) -> str:
        out: list[str] = []
        with self._lock:
            for name, (kind, help_, buckets) in SCHEMA.items():
                s = self.series[name]
                if not s:
                    continue
                out += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
                for k in sorted(s):
                    lab = ",".join(f'{a}="{_esc(b)}"' for a, b in k)
                    v = s[k]
                    if kind != "histogram":
                        out.append(f"{name}{{{lab}}} {_fmt(v)}" if lab else f"{name} {_fmt(v)}")
                        continue
                    run = 0
                    for ub, c in zip(list(buckets or ()) + [math.inf], v["b"]):
                        run += c
                        le = f'le="{_fmt(ub)}"'
                        out.append(f'{name}_bucket{{{lab + "," if lab else ""}{le}}} {run}')
                    out.append(f"{name}_sum{{{lab}}} {_fmt(v['sum'])}" if lab else f"{name}_sum {_fmt(v['sum'])}")
                    out.append(f"{name}_count{{{lab}}} {v['count']}" if lab else f"{name}_count {v['count']}")
        return "\n".join(out) + "\n" if out else ""


# --------------------------------------------------------------------------------------------------------------------------------- current registry
_PROCESS = Registry()
_current: Registry = _PROCESS
_service = "cogent"


def metrics() -> Registry:
    """The registry that recordings go to: the process registry, or the registry of the agent run in progress."""
    return _current


def service() -> str:
    return _service


def safe(fn):
    """Decorator: a telemetry call never raises."""
    def inner(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as exc:                                  # noqa: BLE001
            log.debug("telemetry call %s failed: %s", fn.__name__, exc)
            return None
    inner.__name__ = fn.__name__
    inner.__doc__ = fn.__doc__
    return inner


# --------------------------------------------------------------------------------------------------------------------------------- pricing
def prices() -> dict[str, dict[str, float]]:
    """Per-model prices in USD per million tokens: ``COGENT_LLM_PRICES`` = inline JSON or a path to a JSON file,
    e.g. ``{"llama3.1:8b": {"in": 0.05, "out": 0.15}, "*": {"in": 0.0, "out": 0.0}}``. Local Ollama models are free by default."""
    raw = os.environ.get("COGENT_LLM_PRICES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw if raw.startswith("{") else Path(raw).read_text(encoding="utf-8"))
        return {k: {"in": float(v.get("in", 0)), "out": float(v.get("out", 0))} for k, v in data.items()}
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("COGENT_LLM_PRICES ignored: %s", exc)
        return {}


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    p = prices()
    row = p.get(model) or p.get("*") or {}
    return (tokens_in * row.get("in", 0.0) + tokens_out * row.get("out", 0.0)) / 1e6


# --------------------------------------------------------------------------------------------------------------------------------- tracing
class _NoSpan:
    def set_attribute(self, *a, **k): ...
    def set_attributes(self, *a, **k): ...
    def add_event(self, *a, **k): ...
    def record_exception(self, *a, **k): ...
    def set_status(self, *a, **k): ...
    def is_recording(self) -> bool:
        return False


_tracer: Any = None
_otel: Any = None
_init_key: Any = None


def _load_otel() -> Any:
    global _otel
    if _otel is None:
        try:
            from opentelemetry import trace                                    # noqa: F401
            _otel = True
        except ImportError:
            _otel = False
    return _otel


def init(service_name: str, otlp_endpoint: Optional[str] = None, console: bool = False, exporter: Any = None) -> bool:
    """Name this process and, when the OpenTelemetry SDK is installed, set up the tracer provider. Returns True when spans are exported somewhere.
    ``otlp_endpoint`` (or ``OTEL_EXPORTER_OTLP_ENDPOINT``) is an OTLP/HTTP base URL such as ``http://localhost:4318``; ``console`` prints spans;
    ``exporter`` takes any SpanExporter (tests pass an in-memory one)."""
    global _tracer, _service, _init_key
    _service = service_name.replace("cogent-", "") if service_name.startswith("cogent-") else service_name
    if not _load_otel():
        return False
    endpoint = otlp_endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    key = (service_name, endpoint, console)
    if exporter is None and key == _init_key and _tracer is not None:        # the daemon loop calls this before every run
        return True
    _init_key = key
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
    except ImportError:
        log.info("opentelemetry-sdk is not installed: tracing stays off (pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http)")
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)}))
    active = False
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        active = True
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            url = endpoint.rstrip("/") + ("" if endpoint.rstrip("/").endswith("/v1/traces") else "/v1/traces")
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=url)))
            active = True
        except ImportError:
            log.warning("OTLP export needs opentelemetry-exporter-otlp-proto-http")
    if console:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        active = True
    _tracer = provider.get_tracer("cogent")
    return active


def _get_tracer() -> Any:
    if _tracer is not None:
        return _tracer
    if _load_otel():
        from opentelemetry import trace
        return trace.get_tracer("cogent")
    return None


@contextmanager
def span(name: str, parent: Any = None, **attrs: Any) -> Iterator[Any]:
    """A span named ``name`` (no-op without OpenTelemetry); exceptions are recorded on it and re-raised. ``parent`` is an extracted context."""
    tr = _get_tracer()
    if tr is None:
        yield _NoSpan()
        return
    from opentelemetry.trace import Status, StatusCode
    clean = {k: (v if isinstance(v, (bool, int, float, str)) else str(v)) for k, v in attrs.items() if v is not None}
    with tr.start_as_current_span(name, context=parent, attributes=clean) as sp:
        try:
            yield sp
        except BaseException as exc:
            sp.record_exception(exc)
            sp.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            raise


def extract_context(headers: dict[str, str]) -> Any:
    """The parent context of a W3C ``traceparent`` / ``tracestate`` header pair (None without OpenTelemetry or without a header)."""
    if not _load_otel() or "traceparent" not in headers:
        return None
    try:
        from opentelemetry.propagate import extract
        return extract({k: v for k, v in headers.items() if k in ("traceparent", "tracestate")})
    except Exception:                                                          # noqa: BLE001
        return None


def inject_env(env: dict[str, str]) -> dict[str, str]:
    """Put the current span into ``env['COGENT_TRACEPARENT']`` so a child process (the agent started by the daemon) can continue the trace.
    A private variable name, because other tools set ``TRACEPARENT`` in their own environments and an unrelated, unsampled parent would silence our spans."""
    if _load_otel():
        try:
            from opentelemetry.propagate import inject
            carrier: dict[str, str] = {}
            inject(carrier)
            if "traceparent" in carrier:
                env["COGENT_TRACEPARENT"] = carrier["traceparent"]
        except Exception:                                                      # noqa: BLE001
            pass
    return env


def env_context() -> Any:
    tp = os.environ.get("COGENT_TRACEPARENT")
    return extract_context({"traceparent": tp}) if tp else None


# --------------------------------------------------------------------------------------------------------------------------------- operations
class Op:
    """Handle of a running operation: ``op.error(msg)`` marks it failed without raising."""

    def __init__(self, name: str, sp: Any):
        self.name, self.span, self.failed, self.detail = name, sp, False, ""

    def error(self, detail: str = "") -> None:
        self.failed, self.detail = True, str(detail)[:200]
        with contextlib.suppress(Exception):
            from opentelemetry.trace import Status, StatusCode
            self.span.set_status(Status(StatusCode.ERROR, self.detail))


_samples: dict[str, list[float]] = defaultdict(list)       # op -> seconds, of the run in progress (exact quantiles at commit)
_samples_lock = threading.Lock()
SAMPLE_CAP = 20000


@contextmanager
def op(name: str, **attrs: Any) -> Iterator[Op]:
    """Trace and measure one operation of an agent run: a span, a duration observation, ``operations_total{op,outcome}`` and, when it fails
    (an exception, or ``handle.error()``), ``failures_total{stage=op}``."""
    t0 = time.perf_counter()
    with span(f"agent.op {name}", **{"cogent.op": name, **attrs}) as sp:
        handle = Op(name, sp)
        try:
            yield handle
        except BaseException:
            handle.failed = True
            raise
        finally:
            dt = time.perf_counter() - t0
            _record_op(name, dt, handle.failed)


@safe
def _record_op(name: str, seconds: float, failed: bool) -> None:
    m = metrics()
    m.observe("cogent_agent_operation_duration_seconds", seconds, op=name)
    m.inc("cogent_agent_operations_total", op=name, outcome="error" if failed else "ok")
    if failed:
        m.inc("cogent_agent_failures_total", stage=name)
    with _samples_lock:
        if len(_samples[name]) < SAMPLE_CAP:
            _samples[name].append(seconds)


@contextmanager
def step(name: str) -> Iterator[Any]:
    """A workflow step of the agent: span ``agent.step <name>`` and ``step_duration_seconds{step}``; an exception also counts as a failure of the step."""
    t0 = time.perf_counter()
    with span(f"agent.step {name}", **{"cogent.step": name}) as sp:
        try:
            yield sp
        except BaseException:
            _count("cogent_agent_failures_total", stage=f"step:{name}")
            raise
        finally:
            _observe("cogent_agent_step_duration_seconds", time.perf_counter() - t0, step=name)


@safe
def _count(name: str, value: float = 1.0, **labels: Any) -> None:
    metrics().inc(name, value, **labels)


@safe
def _observe(name: str, value: float, **labels: Any) -> None:
    metrics().observe(name, value, **labels)


@safe
def record_failure(stage: str, n: int = 1) -> None:
    """A failure that was handled (not raised): counted under ``failures_total{stage}``."""
    metrics().inc("cogent_agent_failures_total", float(n), stage=stage)


@safe
def record_retry(component: str, n: int = 1) -> None:
    metrics().inc("cogent_agent_retries_total", float(n), component=component)


@safe
def record_finding(outcome: str, n: int = 1) -> None:
    """outcome: auto_applied | needs_review | human_resolved."""
    metrics().inc("cogent_agent_findings_total", float(n), outcome=outcome)


@safe
def record_llm(model: str, kind: str, tokens_in: int = 0, tokens_out: int = 0, seconds: float = 0.0, outcome: str = "ok", service_name: Optional[str] = None) -> None:
    """One LLM / embedding call: calls, tokens, cost and duration. ``kind`` = chat | embed (| suggest, judge, ...)."""
    m, svc = metrics(), service_name or _service
    m.inc("cogent_llm_calls_total", service=svc, model=model, kind=kind, outcome=outcome)
    if tokens_in:
        m.inc("cogent_llm_tokens_total", float(tokens_in), service=svc, model=model, direction="input")
    if tokens_out:
        m.inc("cogent_llm_tokens_total", float(tokens_out), service=svc, model=model, direction="output")
    c = cost_usd(model, int(tokens_in), int(tokens_out))
    if c:
        m.inc("cogent_llm_cost_usd_total", c, service=svc, model=model)
    if seconds:
        m.observe("cogent_llm_call_duration_seconds", seconds, service=svc, kind=kind)


@safe
def record_output(kind: str, hallucinated: bool, n: int = 1) -> None:
    """A checked LLM output: ``kind`` = join_suggestion (names a table / column that does not exist) | triple (an entity that is not in the source text)."""
    metrics().inc("cogent_llm_outputs_total", float(n), kind=kind, outcome="hallucinated" if hallucinated else "grounded")


@safe
def record_judge(unanimous: bool, verdict: str, proposed_by: str) -> None:
    """One judged join candidate: were all judge votes the same, the final verdict, and who proposed it (both | llm_only | deterministic_only)."""
    m = metrics()
    m.inc("cogent_join_judge_candidates_total", agreement="unanimous" if unanimous else "split")
    m.inc("cogent_join_judge_verdicts_total", verdict=verdict)
    m.inc("cogent_join_llm_vs_deterministic_total", result=proposed_by)


@safe
def record_tool(tool: str, outcome: str, seconds: float) -> None:
    m = metrics()
    m.inc("cogent_mcp_tool_calls_total", tool=tool, outcome=outcome)
    m.observe("cogent_mcp_tool_duration_seconds", seconds, tool=tool)


# --------------------------------------------------------------------------------------------------------------------------------- persistence
def _quantile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated quantile of a sorted list."""
    if not sorted_vals:
        return 0.0
    pos = (len(sorted_vals) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def metrics_dir(home: Path) -> Path:
    return Path(home) / "metrics"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path.with_suffix(".lock"), "w")
    try:
        try:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
        except ImportError:                                                    # Windows: no lock, one writer is the normal case
            pass
        yield
    finally:
        fh.close()


def load_persisted(home: Path) -> Registry:
    try:
        return Registry.from_dict(json.loads((metrics_dir(home) / "agent_metrics.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return Registry()


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def persist(home: Path, run: Registry) -> Registry:
    """Merge ``run`` into the cumulative agent metrics of ``home`` (atomic, locked) and refresh the ``.prom`` textfile. Returns the cumulative registry."""
    d = metrics_dir(home)
    with _locked(d / "agent_metrics.json"):
        total = load_persisted(home)
        total.merge(run)
        _atomic(d / "agent_metrics.json", json.dumps(total.to_dict()))
        _atomic(d / "cogent_agent.prom", total.render())
    return total


def push(url: str, registry: Registry, job: str = "cogent_agent", timeout: float = 10.0) -> bool:
    """PUT the registry to a Prometheus Pushgateway (``/metrics/job/<job>``). Returns False (and logs) on failure."""
    req = urllib.request.Request(url.rstrip("/") + f"/metrics/job/{job}", data=registry.render().encode(), method="PUT",
                                 headers={"Content-Type": "text/plain; version=0.0.4"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception as exc:                                                   # noqa: BLE001
        log.warning("pushgateway %s not updated: %s", url, exc)
        return False


class Run:
    """The metrics of one agent run. ``with Run(home) as run:`` makes a fresh registry current; ``run.commit(...)`` folds it into the persisted totals."""

    def __init__(self, home: Path, run_id: str = "", pushgateway: Optional[str] = None):
        self.home, self.run_id, self.pushgateway = Path(home), run_id, pushgateway
        self.registry = Registry()
        self.t0 = time.time()
        self._prev: Optional[Registry] = None
        self.record: dict[str, Any] = {}

    def __enter__(self) -> "Run":
        global _current
        self._prev, _current = _current, self.registry
        with _samples_lock:
            _samples.clear()
        return self

    def __exit__(self, *exc: Any) -> None:
        global _current
        _current = self._prev or _PROCESS

    def summary(self) -> dict[str, Any]:
        """Per-run numbers read from this run's registry (the content of a ``metrics.jsonl`` line)."""
        r = self.registry
        with _samples_lock:
            ops = {name: {"count": len(v), "p50": round(_quantile(sorted(v), 0.5), 4), "p95": round(_quantile(sorted(v), 0.95), 4), "max": round(max(v), 4)}
                   for name, v in _samples.items() if v}
        steps = {}
        for k, v in r.series["cogent_agent_step_duration_seconds"].items():
            steps[dict(k).get("step", "")] = round(v["sum"], 4)
        tok = r.by_label("cogent_llm_tokens_total", "direction")
        halluc = r.by_label("cogent_llm_outputs_total", "outcome")
        total_out = sum(halluc.values())
        judged = r.by_label("cogent_join_judge_candidates_total", "agreement")
        return {
            "operations": ops, "steps": steps,
            "tokens": {"input": int(tok.get("input", 0)), "output": int(tok.get("output", 0))},
            "cost_usd": round(r.value("cogent_llm_cost_usd_total"), 6),
            "findings": {k: int(v) for k, v in r.by_label("cogent_agent_findings_total", "outcome").items()},
            "failures": {k: int(v) for k, v in r.by_label("cogent_agent_failures_total", "stage").items()},
            "retries": {k: int(v) for k, v in r.by_label("cogent_agent_retries_total", "component").items()},
            "judge": {"unanimous": int(judged.get("unanimous", 0)), "split": int(judged.get("split", 0)),
                      "verdicts": {k: int(v) for k, v in r.by_label("cogent_join_judge_verdicts_total", "verdict").items()},
                      "proposed_by": {k: int(v) for k, v in r.by_label("cogent_join_llm_vs_deterministic_total", "result").items()}},
            "llm_outputs": {"grounded": int(halluc.get("grounded", 0)), "hallucinated": int(halluc.get("hallucinated", 0)),
                            "hallucination_ratio": round(halluc.get("hallucinated", 0) / total_out, 4) if total_out else None},
        }

    def commit(self, success: bool, errors: int = 0, status: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Set the last-run gauges, persist, append to ``logs/metrics.jsonl`` and push; returns the per-run record."""
        r = self.registry
        now = time.time()
        dur = now - self.t0
        r.inc("cogent_agent_runs_total", result="ok" if success else "failed")
        r.observe("cogent_agent_run_duration_seconds", dur)
        r.set("cogent_agent_last_run_timestamp_seconds", now)
        r.set("cogent_agent_last_run_duration_seconds", dur)
        r.set("cogent_agent_last_run_success", 1.0 if success else 0.0)
        r.set("cogent_agent_last_run_errors", float(errors))
        s = self.summary()
        for d in ("input", "output"):
            r.set("cogent_agent_last_run_tokens", s["tokens"][d], direction=d)
        r.set("cogent_agent_last_run_cost_usd", s["cost_usd"])
        for outcome in ("auto_applied", "needs_review", "human_resolved"):
            r.set("cogent_agent_last_run_findings", s["findings"].get(outcome, 0), outcome=outcome)
        for name, q in s["operations"].items():
            r.set("cogent_agent_operation_latency_seconds", q["p50"], op=name, quantile="0.5")
            r.set("cogent_agent_operation_latency_seconds", q["p95"], op=name, quantile="0.95")
        if s["llm_outputs"]["hallucination_ratio"] is not None:
            r.set("cogent_llm_hallucination_ratio", s["llm_outputs"]["hallucination_ratio"])
        judged = s["judge"]["unanimous"] + s["judge"]["split"]
        if judged:
            r.set("cogent_join_judge_agreement_ratio", s["judge"]["unanimous"] / judged)
        rec = {"run_id": self.run_id, "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "duration_s": round(dur, 3), "success": success,
               "errors": errors, **s, **({"status": status} if status else {})}
        self.record = rec
        try:
            total = persist(self.home, r)
            logs = self.home / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            with (logs / "metrics.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
            if self.pushgateway:
                push(self.pushgateway, total)
        except OSError as exc:
            log.warning("metrics not saved: %s", exc)
        return rec


@safe
def record_daemon_retry(home: Path, failed_run: bool = True) -> None:
    """The daemon (a separate process) schedules a retry of a failed run: add it to the persisted totals."""
    r = Registry()
    r.inc("cogent_agent_retries_total", component="daemon_run")
    if failed_run:
        r.inc("cogent_agent_failures_total", stage="daemon_run")
    persist(home, r)


# --------------------------------------------------------------------------------------------------------------------------------- server exposition
class Exposition:
    """What ``/metrics`` serves: the live registry of this process plus the agent's persisted totals (cached by mtime)."""

    def __init__(self, home: Optional[Path] = None):
        self.home, self._mtime, self._persisted = Path(home) if home else None, -1.0, Registry()

    def _agent(self) -> Registry:
        if self.home is None:
            return Registry()
        try:
            m = (metrics_dir(self.home) / "agent_metrics.json").stat().st_mtime
        except OSError:
            return Registry()
        if m != self._mtime:
            self._persisted, self._mtime = load_persisted(self.home), m
        return self._persisted

    def render(self) -> str:
        merged = Registry()
        merged.merge(self._agent())
        merged.merge(_PROCESS)
        return merged.render()
