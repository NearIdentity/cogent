#!/usr/bin/env python3
"""Generate grafana/dashboards/cogent-observability.json (the dashboard is committed; edit this script and re-run it to change it).

    python3 deploy/observability/grafana/build_dashboard.py > deploy/observability/grafana/dashboards/cogent-observability.json

Every panel queries the metrics described in docs/observability.md. Grafana 11 dashboard model, schemaVersion 39.
"""
import json

DS = {"type": "prometheus", "uid": "cogent-prometheus"}
panels = []
_id = [0]


def _base(kind, title, x, y, w, h, targets, description=""):
    _id[0] += 1
    return {"id": _id[0], "type": kind, "title": title, "description": description, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [{"refId": chr(65 + i), "datasource": DS, **t} for i, t in enumerate(targets)]}


def row(title, y):
    _id[0] += 1
    panels.append({"id": _id[0], "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})


def steps(*pairs):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in pairs]}


def stat(title, x, y, w, h, expr, unit="short", thresholds=None, mappings=None, legend="", decimals=None, description="", color_mode="background", targets=None):
    p = _base("stat", title, x, y, w, h, targets or [{"expr": expr, "instant": True, "legendFormat": legend}], description)
    p["fieldConfig"] = {"defaults": {"unit": unit, "thresholds": thresholds or steps(("green", None)), "mappings": mappings or [],
                                     **({"decimals": decimals} if decimals is not None else {}), "noValue": "no data"}, "overrides": []}
    p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "colorMode": color_mode, "graphMode": "none", "textMode": "auto",
                    "orientation": "auto", "justifyMode": "auto"}
    panels.append(p)


def timeseries(title, x, y, w, h, targets, unit="short", description="", stack=False, draw="line", fill=12, max_=None):
    p = _base("timeseries", title, x, y, w, h, targets, description)
    custom = {"drawStyle": draw, "lineWidth": 2, "fillOpacity": fill, "showPoints": "auto", "spanNulls": True, "lineInterpolation": "linear",
              "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    p["fieldConfig"] = {"defaults": {"unit": unit, "custom": custom, **({"max": max_} if max_ is not None else {}), "min": 0}, "overrides": []}
    p["options"] = {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}, "tooltip": {"mode": "multi", "sort": "desc"}}
    panels.append(p)


def bargauge(title, x, y, w, h, targets, unit="short", description="", max_=None, thresholds=None):
    p = _base("bargauge", title, x, y, w, h, targets, description)
    p["fieldConfig"] = {"defaults": {"unit": unit, "min": 0, **({"max": max_} if max_ is not None else {}), "thresholds": thresholds or steps(("green", None)),
                                     "noValue": "no data"}, "overrides": []}
    p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "orientation": "horizontal", "displayMode": "gradient",
                    "showUnfilled": True, "valueMode": "color", "namePlacement": "auto", "minVizHeight": 10, "minVizWidth": 0}
    panels.append(p)


def pie(title, x, y, w, h, expr, legend, description=""):
    p = _base("piechart", title, x, y, w, h, [{"expr": expr, "instant": True, "legendFormat": legend}], description)
    p["fieldConfig"] = {"defaults": {"unit": "short", "noValue": "no data"}, "overrides": []}
    p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "pieType": "donut", "displayLabels": ["value"],
                    "legend": {"displayMode": "table", "placement": "right", "showLegend": True, "values": ["value", "percent"]}, "tooltip": {"mode": "single"}}
    panels.append(p)


def gauge(title, x, y, w, h, expr, thresholds, description="", legend=""):
    p = _base("gauge", title, x, y, w, h, [{"expr": expr, "instant": True, "legendFormat": legend}], description)
    p["fieldConfig"] = {"defaults": {"unit": "percentunit", "min": 0, "max": 1, "thresholds": thresholds, "noValue": "no data"}, "overrides": []}
    p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "showThresholdLabels": False, "showThresholdMarkers": True}
    panels.append(p)


FAIL_RATE = ('(sum(increase(cogent_agent_operations_total{outcome="error"}[24h])) or vector(0))'
             ' / clamp_min(sum(increase(cogent_agent_operations_total[24h])), 1)')
HALLU = ('sum(cogent_llm_outputs_total{outcome="hallucinated"}) / clamp_min(sum(cogent_llm_outputs_total), 1)')

# ---------------------------------------------------------------------------------------------------------------- row 1: the last run
row("Daily agent: last run", 0)
stat("Last run", 0, 1, 3, 4, "cogent_agent_last_run_success", mappings=[{"type": "value", "options": {"1": {"text": "OK", "color": "green"}, "0": {"text": "FAILED", "color": "red"}}}],
     thresholds=steps(("red", None), ("green", 1)), description="1 when the last agent run finished without errors.")
stat("Duration", 3, 1, 3, 4, "cogent_agent_last_run_duration_seconds", unit="s", decimals=1, color_mode="value")
stat("Since last run", 6, 1, 3, 4, "time() - cogent_agent_last_run_timestamp_seconds", unit="s", thresholds=steps(("green", None), ("orange", 93600), ("red", 180000)),
     description="Turns orange after 26 h and red after 50 h without a run.")
stat("Failure rate, 24 h", 9, 1, 3, 4, FAIL_RATE, unit="percentunit", decimals=1, thresholds=steps(("green", None), ("orange", 0.1), ("red", 0.25)),
     description="Failed operations / all operations over 24 hours (fetches, tracker pages, documents, entries, table loads). The alert fires above 25 %.")
stat("Alert", 12, 1, 3, 4, 'max(ALERTS{alertname="CogentAgentFailureRateHigh", alertstate="firing"}) * 2 or max(ALERTS{alertname="CogentAgentFailureRateHigh", alertstate="pending"}) or vector(0)',
     mappings=[{"type": "value", "options": {"0": {"text": "OK", "color": "green"}, "1": {"text": "PENDING", "color": "orange"}, "2": {"text": "FIRING", "color": "red"}}}], thresholds=steps(("green", None), ("orange", 1), ("red", 2)),
     description="State of the Prometheus alert CogentAgentFailureRateHigh (deploy/observability/prometheus/alerts.yml).")
stat("Tokens, last run", 15, 1, 5, 4, "", unit="short", legend="", color_mode="value",
     targets=[{"expr": 'cogent_agent_last_run_tokens{direction="input"}', "instant": True, "legendFormat": "input"},
              {"expr": 'cogent_agent_last_run_tokens{direction="output"}', "instant": True, "legendFormat": "output"}],
     description="Tokens reported by the model server for the last run (chat and embeddings).")
stat("Cost, last run", 20, 1, 4, 4, "cogent_agent_last_run_cost_usd", unit="currencyUSD", decimals=4, color_mode="value",
     description="Tokens x the prices in COGENT_LLM_PRICES. Local Ollama models cost 0 unless you set a price.")

# ---------------------------------------------------------------------------------------------------------------- row 2: latency
row("Latency, p50 and p95", 5)
bargauge("Agent operations, last run (exact p50 / p95)", 0, 6, 8, 9,
         [{"expr": 'cogent_agent_operation_latency_seconds{quantile="0.5"}', "instant": True, "legendFormat": "{{op}} p50"},
          {"expr": 'cogent_agent_operation_latency_seconds{quantile="0.95"}', "instant": True, "legendFormat": "{{op}} p95"}],
         unit="s", description="Quantiles of the raw samples of the last run (fetch, track_url, sync_doc, tabledb_load_table, join_inference ...).")
timeseries("Agent operation latency over time (p50 / p95)", 8, 6, 8, 9,
           [{"expr": 'histogram_quantile(0.5, sum by (le, op) (increase(cogent_agent_operation_duration_seconds_bucket[15m])))', "legendFormat": "{{op}} p50"},
            {"expr": 'histogram_quantile(0.95, sum by (le, op) (increase(cogent_agent_operation_duration_seconds_bucket[15m])))', "legendFormat": "{{op}} p95"}],
           unit="s", description="From the cumulative histograms, over a 15 minute window of runs.")
timeseries("MCP tool latency (p50 / p95)", 16, 6, 8, 9,
           [{"expr": 'histogram_quantile(0.5, sum by (le, tool) (rate(cogent_mcp_tool_duration_seconds_bucket[1m])))', "legendFormat": "{{tool}} p50"},
            {"expr": 'histogram_quantile(0.95, sum by (le, tool) (rate(cogent_mcp_tool_duration_seconds_bucket[1m])))', "legendFormat": "{{tool}} p95"}],
           unit="s", description="Per tool, from cogent_mcp_tool_duration_seconds.")

# ---------------------------------------------------------------------------------------------------------------- row 3: tokens, findings, quality
row("Tokens, cost and findings", 15)
timeseries("Tokens (cumulative)", 0, 16, 8, 8,
           [{"expr": "sum by (model, direction) (cogent_llm_tokens_total)", "legendFormat": "{{model}} {{direction}}"}], unit="short",
           description="Chat and embedding tokens from agent and server (Ollama's prompt_eval_count / eval_count).")
timeseries("Cost (cumulative, USD)", 8, 16, 5, 8, [{"expr": "sum by (model) (cogent_llm_cost_usd_total)", "legendFormat": "{{model}}"}], unit="currencyUSD")
pie("Findings: auto-applied vs human-reviewed", 13, 16, 11, 8, "sum by (outcome) (cogent_agent_findings_total)", "{{outcome}}",
    description="auto_applied: the configuration was rewritten by the agent. needs_review: left for a person. human_resolved: a pending finding a person has since dealt with.")

row("LLM quality: judge agreement and hallucinations", 24)
gauge("Judge agreement (last run)", 0, 25, 5, 6, "cogent_join_judge_agreement_ratio", steps(("red", None), ("orange", 0.6), ("green", 0.8)),
      description="Share of judged join candidates on which all judge runs agreed.")
timeseries("Judge verdicts", 5, 25, 6, 6, [{"expr": "sum by (verdict) (cogent_join_judge_verdicts_total)", "legendFormat": "{{verdict}}"}], draw="bars", stack=True, fill=70)
gauge("Hallucination rate (all time)", 11, 25, 5, 6, HALLU, steps(("green", None), ("orange", 0.1), ("red", 0.25)),
      description="LLM outputs that the data does not support: join suggestions naming a table or column that does not exist, triples whose entities are not in the source text.")
timeseries("Hallucination rate by kind", 16, 25, 8, 6,
           [{"expr": 'sum by (kind) (cogent_llm_outputs_total{outcome="hallucinated"}) / clamp_min(sum by (kind) (cogent_llm_outputs_total), 1)', "legendFormat": "{{kind}}"}],
           unit="percentunit", max_=1)

# ---------------------------------------------------------------------------------------------------------------- row 4: failures and retries
row("Failures and retries", 31)
bargauge("Failures by stage (24 h)", 0, 32, 8, 7, [{"expr": "round(sum by (stage) (increase(cogent_agent_failures_total[24h])))", "instant": True, "legendFormat": "{{stage}}"}],
         thresholds=steps(("green", None), ("orange", 1), ("red", 3)), description="Operations that failed and were handled or raised, by stage.")
bargauge("Retries by component (24 h)", 8, 32, 8, 7, [{"expr": "round(sum by (component) (increase(cogent_agent_retries_total[24h])))", "instant": True, "legendFormat": "{{component}}"}],
         thresholds=steps(("green", None), ("orange", 3), ("red", 10)), description="fetch, llm, embed, daemon_run.")
timeseries("Operations by outcome", 16, 32, 8, 7, [{"expr": "sum by (outcome) (increase(cogent_agent_operations_total[15m]))", "legendFormat": "{{outcome}}"}],
           draw="bars", stack=True, fill=70)

# ---------------------------------------------------------------------------------------------------------------- row 5: MCP server
row("MCP server", 39)
timeseries("HTTP requests per second, by status", 0, 40, 8, 7, [{"expr": "sum by (status) (rate(cogent_http_requests_total[1m]))", "legendFormat": "{{status}}"}], unit="reqps")
timeseries("Tool calls per second, by outcome", 8, 40, 8, 7, [{"expr": "sum by (tool, outcome) (rate(cogent_mcp_tool_calls_total[1m]))", "legendFormat": "{{tool}} {{outcome}}"}], unit="reqps")
timeseries("Rate-limited requests, graph cache, embedder", 16, 40, 8, 7,
           [{"expr": "sum(rate(cogent_rate_limited_total[1m]))", "legendFormat": "429 per s"},
            {"expr": 'sum by (event) (rate(cogent_graph_cache_events_total[1m]))', "legendFormat": "graph cache {{event}}"},
            {"expr": 'sum by (event) (rate(cogent_embed_events_total[1m]))', "legendFormat": "embedder {{event}}"}], unit="ops")

dash = {"uid": "cogent-observability", "title": "cogent: agent and MCP server", "tags": ["cogent", "mcp", "agent"], "timezone": "browser", "editable": True,
        "graphTooltip": 1, "schemaVersion": 39, "version": 1, "refresh": "10s", "time": {"from": "now-30m", "to": "now"},
        "templating": {"list": []}, "annotations": {"list": []}, "links": [], "panels": panels,
        "description": "Per-run and live metrics of the cogent update agent and MCP server. Source: docs/observability.md."}

if __name__ == "__main__":
    print(json.dumps(dash, indent=2))
