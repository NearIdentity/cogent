"""LLM-based join inference with LLM-as-Judge evaluation, next to the deterministic one (``kg_relmap``).

Pipeline for one document (all calls go to Ollama through ``kg_llm``):

1. **Suggest**   for the most promising table pairs the *suggester* LLM proposes join column pairs (``samples`` independent runs, three
                 prompt styles: balanced / names / values; run 0 at temperature 0, the others warmer). Suggestions that name columns
                 that do not exist are counted (hallucination rate) and dropped.
2. **Collect**   candidates = LLM suggestions ∪ the deterministic relationships. Code computes the same facts for every candidate
                 (name similarity, value overlap, key uniqueness, KG evidence), so the two methods can be compared fairly.
3. **Judge**     the *judge* LLM (same or another model) rules on every candidate, ``judge_runs`` times with the column order swapped on
                 alternate runs (position-bias control): verdict accept / reject / uncertain by majority vote, confidence, reasons, problems.
4. **Reconcile** each candidate gets a status (confirmed = deterministic ∧ LLM ∧ judge-accept; deterministic_only; llm_only; disputed =
                 deterministic but judge-reject; rejected; uncertain) and a final score. Ambiguous alternatives (same column joined to
                 several columns of one table) are flagged.
5. **Evaluate**  internal consistency (LLM self-agreement across runs, judge vote consistency) and cross-method agreement (LLM vs
                 deterministic, judge vs each) are reported; the judge is another LLM, **not ground truth**.

``select`` turns the stored candidates into a relationship list for an *approach* (deterministic | llm | hybrid | consensus) with thresholds:
that is what the MCP front-end applies per session / per task, without re-running any model.
"""
from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import kg_relmap as R
import kg_telemetry as telemetry
from kg_llm import LLM, LLMError
from kg_tables import Col

log = logging.getLogger("kg_llmjoin")
APPROACHES = ("deterministic", "llm", "hybrid", "consensus")
STYLES = ("balanced", "names", "values")
W_DET, W_LLM, W_JUDGE = 0.35, 0.25, 0.40

PROFILES: dict[str, dict[str, Any]] = {
    "fast": {"approach": "deterministic", "min_score": 0.55, "include_rejected": False, "retrieval": "semantic", "units": "strict", "min_similarity": 0.0,
             "description": "heuristic joins only (names + values + KG); no LLM evidence needed"},
    "balanced": {"approach": "hybrid", "min_score": 0.55, "include_rejected": False, "retrieval": "semantic", "units": "strict", "min_similarity": 0.0,
                 "description": "heuristic and LLM-suggested joins, minus those the judge rejects (default)"},
    "strict": {"approach": "consensus", "min_score": 0.60, "include_rejected": False, "retrieval": "semantic", "units": "strict", "min_similarity": 0.70,
               "description": "only joins found by both methods and accepted by the judge: fewest false joins"},
    "exploratory": {"approach": "hybrid", "min_score": 0.30, "include_rejected": True, "retrieval": "hybrid", "units": "broad", "min_similarity": 0.0,
                    "description": "everything plausible: rejected / disputed joins (flagged), inferred units with alternatives, hybrid retrieval: for review"},
    "llm": {"approach": "llm", "min_score": 0.50, "include_rejected": False, "retrieval": "semantic", "units": "broad", "min_similarity": 0.0,
            "description": "LLM-suggested joins that the judge did not reject (finds semantic joins the heuristics miss)"},
}


@dataclass
class Config:
    samples: int = 3                  # independent suggestion runs per table pair
    judge_runs: int = 3               # judge votes per candidate
    max_pairs: int = 40               # table pairs sent to the suggester (best first)
    min_confidence: float = 0.4       # suggestions below this self-reported confidence are ignored
    seed: int = 7
    max_seconds: Optional[float] = None
    judge_deterministic: bool = True  # also judge the deterministic relationships (needed for LLM-vs-deterministic comparison)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# -------------------------------------------------------------------------------------------------- prompts
SUGGEST_SCHEMA = {"type": "object", "properties": {"joins": {"type": "array", "items": {"type": "object", "properties": {
    "left_column": {"type": "string"}, "right_column": {"type": "string"},
    "relationship": {"type": "string", "enum": ["join", "lookup", "same_entity"]},
    "cardinality": {"type": "string", "enum": ["1:1", "1:N", "N:1", "N:M"]},
    "confidence": {"type": "number"}, "rationale": {"type": "string"}},
    "required": ["left_column", "right_column", "confidence", "rationale"]}}}, "required": ["joins"]}
JUDGE_SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["accept", "reject", "uncertain"]}, "confidence": {"type": "number"},
                                                 "reasons": {"type": "array", "items": {"type": "string"}}, "problems": {"type": "array", "items": {"type": "string"}}},
                "required": ["verdict", "confidence", "reasons"]}
SUGGEST_SYSTEM = ("You are a data engineer analysing tables extracted from engineering standards and regulatory documents. Find pairs of columns "
                  "(one from each table) that can be used to JOIN the two tables: both columns hold the same kind of key (a category, code, class, "
                  "component, identifier), possibly under different names or spellings. Do not propose measurements, free text, row numbers or "
                  "columns that only share a few coincidental numbers. Use the column headers exactly as written. If there is no genuine join, "
                  "return an empty list. Answer with JSON only.")
STYLE_HINT = {"balanced": "", "names": "Pay most attention to column names, abbreviations and synonyms (e.g. 'Category' and 'SDC').",
              "values": "Pay most attention to whether the sample values of two columns come from the same set of codes or entities."}
JUDGE_SYSTEM = ("You are a careful reviewer of proposed table joins. Decide whether joining the two tables on the proposed column pair is "
                "meaningful: matching values must refer to the same real-world entity or code (so rows can be combined), the columns must not "
                "merely share coincidental values, and the join should not explode into meaningless many-to-many matches. Trust the computed "
                "facts; weigh the suggestions of other methods as opinions, not evidence. Answer with JSON only.")


def _col_line(c: Col, rows: list[list[str]], j: int) -> str:
    vals = list(dict.fromkeys(r[j] for r in rows if r[j]))[:6]
    unit = f", unit {c.unit}" if c.unit else ""
    return f'  - "{c.header}" ({c.pg_type}{unit}; {c.n_distinct} distinct{"; unique" if c.key_candidate else ""}; e.g. {", ".join(repr(v) for v in vals)})'


def describe(t: R.T, label: str) -> str:
    c = t.ctx
    head = [f"TABLE {label}: {c.caption or t.table_id}"]
    if c.section_path:
        head.append("  section: " + " > ".join(c.section_path))
    if c.intro:
        head.append("  introduction: " + c.intro[:200])
    if c.notes:
        head.append("  notes: " + " ".join(c.notes)[:200])
    return "\n".join(head + ["  columns:"] + [_col_line(col, t.rows, j) for j, col in enumerate(t.cols)] + [f"  ({len(t.rows)} rows)"])


def suggest_prompt(a: R.T, b: R.T, style: str) -> str:
    return "\n\n".join([describe(a, "A"), describe(b, "B"), STYLE_HINT[style],
                        'Return JSON: {"joins": [{"left_column": <header in A>, "right_column": <header in B>, "relationship": "join"|"lookup"|"same_entity", '
                        '"cardinality": "1:1"|"1:N"|"N:1"|"N:M", "confidence": 0..1, "rationale": <one sentence>}]}'])


def judge_prompt(a: R.T, b: R.T, cand: dict[str, Any], swap: bool) -> str:
    f = cand["features"]
    ta, ha, tb, hb = (b, cand["right_header"], a, cand["left_header"]) if swap else (a, cand["left_header"], b, cand["right_header"])
    nl, nr = (f["n_right"], f["n_left"]) if swap else (f["n_left"], f["n_right"])
    ul, ur = (f["right_unique"], f["left_unique"]) if swap else (f["left_unique"], f["right_unique"])
    opinions = []
    d, l = cand["deterministic"], cand.get("llm")
    opinions.append(f"deterministic rules: {'proposed it (score %.2f)' % d['score'] if d['proposed'] else 'did not propose it'}")
    if l is not None:
        opinions.append(f"LLM suggester: proposed in {l['proposed_by']} of {l['runs']} runs" + (f"; rationale: {l['rationales'][0]}" if l["rationales"] else ""))
    facts = [f'proposed join: "{ha}" in table A  =  "{hb}" in table B',
             f"distinct values: {nl} in A.{ha}, {nr} in B.{hb}; {f['n_common']} in common (containment {f['containment']}, Jaccard {f['jaccard']})",
             f"shared values: {', '.join(repr(v) for v in f['shared_values']) or 'none'}",
             f"unique key: A.{ha}: {'yes' if ul else 'no'}; B.{hb}: {'yes' if ur else 'no'}; column-name similarity {f['name_similarity']}; "
             f"types {f['left_type'] if not swap else f['right_type']} / {f['right_type'] if not swap else f['left_type']}"]
    if f["kg_evidence"]:
        facts.append("document structure: " + "; ".join(f["kg_evidence"]))
    return "\n\n".join([describe(ta, "A"), describe(tb, "B"), "COMPUTED FACTS:\n" + "\n".join("- " + x for x in facts),
                        "OTHER METHODS:\n" + "\n".join("- " + x for x in opinions),
                        'Return JSON: {"verdict": "accept"|"reject"|"uncertain", "confidence": 0..1, "reasons": [..], "problems": [..]}'])


# ------------------------------------------------------------------------------------------------ candidates
def cand_id(lt: str, lc: str, rt: str, rc: str) -> str:
    """Order-independent identifier of a column pair."""
    x, y = sorted([(lt, lc), (rt, rc)])
    return f"{x[0]}|{x[1]}|{y[0]}|{y[1]}"


def _new_candidate(a: R.T, ca: Col, b: R.T, cb: Col, boost: tuple[float, list[str]]) -> dict[str, Any]:
    card = R.cardinality(ca, cb)
    left, right, lc, rc = (a, b, ca, cb) if card != "N:1" else (b, a, cb, ca)
    feats = R.pair_features(left, lc, right, rc, boost)
    return {"id": cand_id(left.table_id, lc.name, right.table_id, rc.name), "left_table": left.table_id, "left_column": lc.name, "left_header": lc.header,
            "right_table": right.table_id, "right_column": rc.name, "right_header": rc.header, "cardinality": "1:N" if card == "N:1" else card,
            "features": feats, "deterministic": {"proposed": False, "score": None, "evidence": []}, "llm": None, "judge": None}


def select_pairs(tables: list[R.T], det_rels: list[dict[str, Any]], max_pairs: int) -> list[tuple[R.T, R.T]]:
    """Table pairs for the suggester, most promising first: deterministic hits, KG evidence, header overlap, key-like text columns."""
    det = {frozenset([r["left_table"], r["right_table"]]) for r in det_rels}
    scored = []
    for a, b in itertools.combinations(tables, 2):
        boost, _ = R._kg_boost(a, b)
        ha, hb = {R.norm_name(c.header) for c in a.cols}, {R.norm_name(c.header) for c in b.cols}
        overlap = len(ha & hb) / max(1, len(ha | hb))
        keyish = any(c.role == "key" or c.key_candidate for c in a.cols) and any(c.role == "key" or c.key_candidate for c in b.cols)
        scored.append((3.0 * (frozenset([a.table_id, b.table_id]) in det) + 2.0 * boost + overlap + 0.5 * keyish, a.table_id, b.table_id, a, b))
    scored.sort(key=lambda x: (-x[0], x[1], x[2]))
    return [(a, b) for _, _, _, a, b in scored[:max_pairs]]


def _find_col(t: R.T, header: str) -> Optional[Col]:
    h = (header or "").strip().lower()
    return next((c for c in t.cols if c.header.strip().lower() == h or c.name == h), None)


def classify(c: dict[str, Any]) -> str:
    det, llm = c["deterministic"]["proposed"], bool(c["llm"] and c["llm"]["proposed_by"] > 0)
    j = c["judge"]["verdict"] if c["judge"] else None
    if j == "reject":
        return "disputed" if det else "rejected"
    if j == "uncertain":
        return "uncertain"
    if det and llm:
        return "confirmed"
    return "deterministic_only" if det else "llm_only"


def final_score(c: dict[str, Any]) -> float:
    """Weighted blend of the deterministic score, the LLM suggestion agreement and the judge's verdict (components that did not run are left out)."""
    parts = []
    parts.append((W_DET, (c["deterministic"]["score"] or 0.0) if c["deterministic"]["proposed"] else 0.0))
    if c["llm"] is not None:
        parts.append((W_LLM, c["llm"]["agreement"] * max(0.5, c["llm"]["confidence"] or 0.5)))
    if c["judge"] is not None:
        v, conf = c["judge"]["verdict"], c["judge"]["confidence"]
        parts.append((W_JUDGE, conf if v == "accept" else 0.5 * conf if v == "uncertain" else 0.0))
    w = sum(p for p, _ in parts)
    score = round(sum(p * s for p, s in parts) / w, 3) if w else 0.0
    return min(score, 0.30) if c["judge"] is not None and c["judge"]["verdict"] == "reject" else score      # a judge's rejection caps the score


# ------------------------------------------------------------------------------------------------ evaluation
def _stage_notes(tables: list[R.T], det_rels: list[dict[str, Any]], llm: Optional[LLM], cfg: Config, status: dict[str, Any]) -> list[str]:
    """Why a stage did not run or found nothing, in words (an empty evaluation must not look like a failure)."""
    n, notes = len(tables), []
    joins = [r for r in det_rels if r["kind"] == "join"]
    if n < 2:
        notes.append(f"The document has {n} table(s): there is nothing to join, so no join inference was attempted.")
        return notes
    pairs = n * (n - 1) // 2
    if not joins:
        notes.append(f"The deterministic rules compared {pairs} table pair(s) of {n} tables and found no join: no column pair shares a similar name together with "
                     f"overlapping values. The closest near-misses are listed under 'near_misses'.")
    else:
        notes.append(f"The deterministic rules found {len(joins)} join(s) among {n} tables ({pairs} table pairs).")
    if llm is None:
        notes.append("LLM join inference is off (start the table agent with --llm-joins, or ask for it with the evaluate_joins tool), so only the deterministic joins are listed and nothing was judged.")
    elif cfg.samples <= 0:
        notes.append("LLM join inference is configured with 0 samples.")
    elif status["llm"] == "unavailable":
        notes.append("The LLM was unreachable; see 'errors'.")
    return notes


def evaluate(tables: list[R.T], det_rels: list[dict[str, Any]], llm: Optional[LLM], judge_llm: Optional[LLM] = None, cfg: Optional[Config] = None) -> dict[str, Any]:
    """Run suggest -> collect -> judge -> reconcile for one document. ``llm=None`` returns the deterministic relationships as candidates."""
    with telemetry.span("joins.evaluate", **{"cogent.tables": len(tables), "cogent.llm": llm is not None}) as sp:
        ev = _evaluate(tables, det_rels, llm, judge_llm, cfg)
        sp.set_attribute("cogent.candidates", len(ev["candidates"]))
        sp.set_attribute("cogent.hallucination_rate", ev["metrics"].get("hallucination_rate") or 0.0)
        return ev


def _evaluate(tables: list[R.T], det_rels: list[dict[str, Any]], llm: Optional[LLM], judge_llm: Optional[LLM] = None, cfg: Optional[Config] = None) -> dict[str, Any]:
    cfg = cfg or Config()
    judge_llm = judge_llm or llm
    t0 = time.time()
    by_id = {t.table_id: t for t in tables}
    cands: dict[str, dict[str, Any]] = {}
    status: dict[str, Any] = {"llm": "not run", "judge": "not run", "errors": [], "suggest_calls": 0, "judge_calls": 0, "invalid_suggestions": 0, "suggestions": 0}

    for r in det_rels:                                                        # deterministic candidates
        if r["kind"] != "join":
            continue
        a, b = by_id[r["left_table"]], by_id[r["right_table"]]
        ca, cb = next(c for c in a.cols if c.name == r["left_column"]), next(c for c in b.cols if c.name == r["right_column"])
        c = _new_candidate(a, ca, b, cb, R._kg_boost(a, b))
        c["deterministic"] = {"proposed": True, "score": r["score"], "evidence": r["evidence"]}
        cands[c["id"]] = c

    def out_of_time() -> bool:
        return cfg.max_seconds is not None and time.time() - t0 > cfg.max_seconds

    run_sets: list[set[str]] = [set() for _ in range(cfg.samples)]
    if llm is not None and cfg.samples > 0 and len(tables) >= 2:                  # a document with one table has nothing to join
        status["llm"] = "ok"
        pairs = select_pairs(tables, det_rels, cfg.max_pairs)
        status["pairs"] = len(pairs)
        per_pair: dict[str, dict[str, Any]] = {}
        asked_pairs: set[frozenset] = set()
        stop = False
        for a, b in pairs:
            if stop:
                break
            boost = R._kg_boost(a, b)
            for run in range(cfg.samples):
                if out_of_time():
                    status["errors"].append("time budget exhausted during suggestion; remaining pairs skipped")
                    status["llm"] = "partial"
                    stop = True
                    break
                try:
                    ans = llm.chat_json(SUGGEST_SYSTEM, suggest_prompt(a, b, STYLES[run % len(STYLES)]), SUGGEST_SCHEMA,
                                        temperature=0.0 if run == 0 else 0.7, seed=cfg.seed + run, task="suggest")
                    status["suggest_calls"] += 1
                    asked_pairs.add(frozenset([a.table_id, b.table_id]))
                except LLMError as exc:
                    status["errors"].append(f"suggester: {exc}")
                    status["llm"] = "unavailable" if status["suggest_calls"] == 0 else "partial"
                    stop = True
                    break
                for s in (ans.get("joins") or []) if isinstance(ans, dict) else []:
                    status["suggestions"] += 1
                    ca, cb = _find_col(a, s.get("left_column")), _find_col(b, s.get("right_column"))
                    if ca is None or cb is None:                                # the model may have swapped A and B
                        ca, cb = _find_col(a, s.get("right_column")), _find_col(b, s.get("left_column"))
                    try:
                        conf = float(s.get("confidence", 0.5))
                    except (TypeError, ValueError):
                        conf = 0.5
                    if ca is None or cb is None:
                        status["invalid_suggestions"] += 1
                        telemetry.record_output("join_suggestion", True)          # names a column that does not exist: a hallucination
                        continue
                    telemetry.record_output("join_suggestion", False)
                    if conf < cfg.min_confidence:
                        continue
                    c = _new_candidate(a, ca, b, cb, boost)
                    c = cands.setdefault(c["id"], c)
                    run_sets[run].add(c["id"])
                    rec = per_pair.setdefault(c["id"], {"runs": set(), "conf": [], "why": [], "rel": []})
                    rec["runs"].add(run)
                    rec["conf"].append(max(0.0, min(1.0, conf)))
                    if s.get("rationale") and s["rationale"] not in rec["why"]:
                        rec["why"].append(str(s["rationale"])[:240])
                    rec["rel"].append(s.get("relationship") or "join")
        for cid, c in cands.items():                                            # every candidate whose pair was asked gets an LLM record
            if frozenset([c["left_table"], c["right_table"]]) in asked_pairs:
                rec = per_pair.get(cid)
                c["llm"] = {"runs": cfg.samples, "proposed_by": len(rec["runs"]) if rec else 0, "agreement": round(len(rec["runs"]) / cfg.samples, 3) if rec else 0.0,
                            "confidence": round(sum(rec["conf"]) / len(rec["conf"]), 3) if rec else 0.0, "rationales": rec["why"][:3] if rec else [],
                            "relationships": sorted(set(rec["rel"])) if rec else []}

    if judge_llm is not None and cfg.judge_runs > 0 and cands and len(tables) >= 2 and not (status["llm"] == "unavailable" and judge_llm is llm):
        status["judge"] = "ok"
        for cid, c in sorted(cands.items()):
            if not cfg.judge_deterministic and not (c["llm"] and c["llm"]["proposed_by"]):
                continue
            if out_of_time():
                status["errors"].append("time budget exhausted during judging; remaining candidates unjudged")
                status["judge"] = "partial"
                break
            a, b = by_id[c["left_table"]], by_id[c["right_table"]]
            votes, confs, reasons, problems = [], [], [], []
            for run in range(cfg.judge_runs):
                try:
                    ans = judge_llm.chat_json(JUDGE_SYSTEM, judge_prompt(a, b, c, swap=bool(run % 2)), JUDGE_SCHEMA,
                                              temperature=0.0 if run == 0 else 0.3, seed=cfg.seed + 100 + run, task="judge")
                    status["judge_calls"] += 1
                except LLMError as exc:
                    status["errors"].append(f"judge: {exc}")
                    status["judge"] = "unavailable" if status["judge_calls"] == 0 else "partial"
                    break
                v = str(ans.get("verdict", "uncertain")).lower() if isinstance(ans, dict) else "uncertain"
                votes.append(v if v in ("accept", "reject", "uncertain") else "uncertain")
                try:
                    confs.append(max(0.0, min(1.0, float(ans.get("confidence", 0.5)))))
                except (TypeError, ValueError, AttributeError):
                    confs.append(0.5)
                reasons += [str(x)[:240] for x in (ans.get("reasons") or [])][:2] if isinstance(ans, dict) else []
                problems += [str(x)[:240] for x in (ans.get("problems") or [])][:2] if isinstance(ans, dict) else []
            if status["judge"] == "unavailable":
                break
            if votes:
                tally = {v: votes.count(v) for v in ("accept", "reject", "uncertain")}
                top = max(tally.values())
                winners = [v for v, n in tally.items() if n == top]
                verdict = winners[0] if len(winners) == 1 else "uncertain"
                conf = [x for v, x in zip(votes, confs) if v == verdict] or confs
                c["judge"] = {"verdict": verdict, "confidence": round(sum(conf) / len(conf), 3), "votes": tally, "runs": len(votes),
                              "consistency": round(top / len(votes), 3), "reasons": list(dict.fromkeys(reasons))[:4], "problems": list(dict.fromkeys(problems))[:4]}
                if len(votes) >= 2:                                                # agreement between judge runs only means something with two or more
                    is_det, is_llm = c["deterministic"]["proposed"], bool(c["llm"] and c["llm"]["proposed_by"])
                    telemetry.record_judge(top == len(votes), verdict, "both" if is_det and is_llm else "llm_only" if is_llm else "deterministic_only")

    for c in cands.values():
        c["status"], c["final_score"] = classify(c), final_score(c)
        c["methods"] = [m for m, on in (("deterministic", c["deterministic"]["proposed"]), ("llm", bool(c["llm"] and c["llm"]["proposed_by"]))) if on]
    _flag_ambiguity(cands)
    status["notes"] = _stage_notes(tables, det_rels, llm, cfg, status)
    if not any(c["deterministic"]["proposed"] for c in cands.values()) and len(tables) >= 2:
        status["near_misses"] = R.near_misses(tables)
    return {"candidates": sorted(cands.values(), key=lambda c: (-c["final_score"], c["id"])), "metrics": metrics(list(cands.values()), run_sets, status),
            "config": cfg.as_dict(), "models": {"suggester": getattr(llm, "model", None), "judge": getattr(judge_llm, "model", None)}, "status": status,
            "judge_note": "The judge is a language model, not ground truth: use its verdicts and the agreement statistics as evidence, not as proof."}


def _flag_ambiguity(cands: dict[str, dict[str, Any]]) -> None:
    """A column joined to several columns of the same table: keep the best-scored, flag the others as alternatives."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for c in cands.values():
        if c["status"] in ("rejected", "disputed"):
            continue
        groups.setdefault((c["left_table"], c["left_column"], c["right_table"]), []).append(c)
        groups.setdefault((c["right_table"], c["right_column"], c["left_table"]), []).append(c)
    for g in groups.values():
        if len(g) > 1:
            best = max(g, key=lambda c: (c["final_score"], c["id"]))
            for c in g:
                c["ambiguous_alternative"] = c is not best


def metrics(cands: list[dict[str, Any]], run_sets: list[set[str]], status: dict[str, Any]) -> dict[str, Any]:
    det = [c for c in cands if c["deterministic"]["proposed"]]
    llm = [c for c in cands if c["llm"] and c["llm"]["proposed_by"] > 0]
    both = [c for c in det if c in llm]
    judged = [c for c in cands if c["judge"]]
    acc = lambda c: bool(c["judge"] and c["judge"]["verdict"] == "accept")
    rate = lambda xs, f: round(sum(1 for x in xs if f(x)) / len(xs), 3) if xs else None
    jac = []
    for x, y in itertools.combinations([s for s in run_sets], 2):
        if x or y:
            jac.append(len(x & y) / len(x | y))
    m: dict[str, Any] = {
        "candidates": len(cands), "deterministic": len(det), "llm": len(llm), "both": len(both), "deterministic_only": len(det) - len(both), "llm_only": len(llm) - len(both),
        "llm_self_consistency": round(sum(jac) / len(jac), 3) if jac else None,           # mean pairwise Jaccard of the suggestion sets of the runs
        "llm_mean_agreement": round(sum(c["llm"]["agreement"] for c in llm) / len(llm), 3) if llm else None,
        "hallucination_rate": round(status["invalid_suggestions"] / status["suggestions"], 3) if status.get("suggestions") else None,
        "llm_confirmed_by_deterministic": round(len(both) / len(llm), 3) if llm else None,
        "deterministic_confirmed_by_llm": round(len(both) / len(det), 3) if det else None,
        "judged": len(judged), "judge_consistency": round(sum(c["judge"]["consistency"] for c in judged) / len(judged), 3) if judged else None,
        "judge_accept_rate_deterministic": rate([c for c in det if c["judge"]], acc), "judge_accept_rate_llm": rate([c for c in llm if c["judge"]], acc),
        "judge_accept_rate_both": rate([c for c in both if c["judge"]], acc),
        "judge_accept_rate_deterministic_only": rate([c for c in det if c not in llm and c["judge"]], acc),
        "judge_accept_rate_llm_only": rate([c for c in llm if c not in det and c["judge"]], acc),
        "statuses": {s: sum(1 for c in cands if c["status"] == s) for s in sorted({c["status"] for c in cands})}}
    if judged:                                           # the judge as an independent labeller: precision / recall of each method against 'accept'
        positives = [c for c in judged if acc(c)]
        for name, group in (("deterministic", det), ("llm", llm)):
            tp = [c for c in group if c["judge"] and acc(c)]
            m[f"{name}_precision_vs_judge"] = round(len(tp) / len([c for c in group if c["judge"]]), 3) if [c for c in group if c["judge"]] else None
            m[f"{name}_recall_vs_judge"] = round(len(tp) / len(positives), 3) if positives else None
    return m


# ---------------------------------------------------------------------------------------------------- select
def select(cands: list[dict[str, Any]], approach: str = "hybrid", min_score: Optional[float] = None, include_rejected: bool = False,
           min_agreement: float = 0.0) -> list[dict[str, Any]]:
    """Relationships for an approach, from the stored candidates (no model is called):

    * ``deterministic`` proposed by the heuristics (score = deterministic score);
    * ``llm``           suggested by the LLM in >= ``min_agreement`` of the runs and not rejected by the judge;
    * ``hybrid``        any candidate the judge did not reject (deterministic, LLM, or both), flagged with its status;
    * ``consensus``     proposed by both methods *and* accepted by the judge.
    ``include_rejected`` also returns rejected / disputed candidates, marked ``rejected: true``. Without any LLM evidence ``hybrid`` is the
    deterministic result and ``llm`` / ``consensus`` are empty."""
    if approach not in APPROACHES:
        raise ValueError(f"approach must be one of {APPROACHES}")
    floor = 0.55 if min_score is None else float(min_score)
    out = []
    for c in cands:
        det, llm = c["deterministic"]["proposed"], bool(c.get("llm") and c["llm"]["proposed_by"] > 0)
        j = c["judge"]["verdict"] if c.get("judge") else None
        score = c["deterministic"]["score"] if approach == "deterministic" else c["final_score"]
        if approach == "deterministic":
            keep = det
        elif approach == "llm":
            keep = llm and c["llm"]["agreement"] >= min_agreement
        elif approach == "consensus":
            keep = det and llm and j == "accept"
        else:
            keep = det or llm
        rejected = j == "reject"
        if rejected and approach != "deterministic":
            keep = keep and include_rejected
        if keep and ((score or 0.0) >= floor or (rejected and include_rejected)):
            out.append({**c, "selected_score": score, "approach": approach, "rejected": rejected})
    return sorted(out, key=lambda c: (-(c["selected_score"] or 0), c["id"]))


def candidates_to_relations(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The relationship dicts the dictionary / Mermaid writers expect (kind 'join'), from selected candidates."""
    return [{"kind": "join", "left_table": c["left_table"], "left_column": c["left_column"], "left_header": c["left_header"],
             "right_table": c["right_table"], "right_column": c["right_column"], "right_header": c["right_header"], "cardinality": c["cardinality"],
             "score": c["selected_score"], "status": c["status"], "methods": c["methods"], "rejected": c["rejected"],
             "name_similarity": c["features"]["name_similarity"], "containment": c["features"]["containment"], "jaccard": c["features"]["jaccard"],
             "shared_values": c["features"]["shared_values"],
             "evidence": (c["deterministic"]["evidence"] or []) + [f"LLM: {w}" for w in (c["llm"]["rationales"] if c.get("llm") else [])]
                         + [f"judge ({c['judge']['verdict']}, {c['judge']['confidence']}): {r}" for r in ((c["judge"] or {}).get("reasons", [])[:2])]}
            for c in cands]


def evaluation_markdown(label: str, ev: dict[str, Any]) -> str:
    """Human-readable join evaluation: models, statistics, and every candidate with its verdicts."""
    m, st = ev["metrics"], ev["status"]
    L_ = [f"# Join evaluation: {label}", "", f"Suggester `{ev['models']['suggester'] or 'off'}` · judge `{ev['models']['judge'] or 'off'}` · LLM stage: {st['llm']} · judge stage: {st['judge']} · "
          f"{st['suggest_calls']} suggestion call(s), {st['judge_calls']} judge call(s)", "", f"> {ev['judge_note']}", ""]
    if st.get("notes"):
        L_ += ["## Why", ""] + [f"- {x}" for x in st["notes"]] + [""]
    if st.get("near_misses"):
        L_ += ["Closest column pairs the rules rejected:", "", "| left | right | name similarity | shared values | missing |", "|---|---|---|---|---|"]
        L_ += [f"| {x['left_table'].split('/')[-1]}.{x['left_column']} | {x['right_table'].split('/')[-1]}.{x['right_column']} | {x['name_similarity']} | {x['containment']} | {x['missing']} |"
               for x in st["near_misses"]] + [""]
    L_ += ["## Statistics", ""]
    L_ += [f"- {k.replace('_', ' ')}: {v}" for k, v in m.items() if v is not None and k != "statuses"]
    L_ += [f"- statuses: {m['statuses']}", "", "## Candidates", "",
           "| status | join | cardinality | final | deterministic | LLM agreement | judge | reasons |", "|---|---|---|---|---|---|---|---|"]
    for c in ev["candidates"]:
        j, l = c.get("judge"), c.get("llm")
        left, right = c["left_table"].split("/")[-1], c["right_table"].split("/")[-1]
        L_.append(f"| {c['status']}{' (alt.)' if c.get('ambiguous_alternative') else ''} | {left}.{c['left_column']} = {right}.{c['right_column']} | {c['cardinality']} | {c['final_score']} | "
                  f"{c['deterministic']['score'] if c['deterministic']['proposed'] else '-'} | {(str(l['proposed_by']) + '/' + str(l['runs'])) if l else '-'} | "
                  f"{(j['verdict'] + ' ' + str(j['confidence']) + ' (' + str(j['votes']) + ')') if j else '-'} | "
                  f"{'; '.join((j or {}).get('reasons', [])[:2] + (j or {}).get('problems', [])[:1]).replace('|', '/')[:200]} |")
    return "\n".join(L_) + "\n"
