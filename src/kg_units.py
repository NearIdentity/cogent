"""Deduce the units of numeric table columns from the table's own metadata and from the KG around it.

Evidence, from most to least direct (``source``, base ``confidence``):

* ``header``            unit in brackets in the column header: ``Damping (%)``, ``Stress [ksi]``                         0.97
* ``cells``             the same unit suffix on >= 90 % of the numeric cells: ``5 psi``, ``12 mm``                      0.90
* ``header_phrase``     a unit phrase in the header: ``Annual probability per year``, ``Pressure, psi``, ``in kPa``       0.85
* ``table_declaration`` a declaration in the caption / introduction / notes: ``All dimensions in mm``, ``Units: kPa``      0.75
* ``table_text``        one compatible unit merely mentioned in the caption / introduction / notes                        0.50
* ``section_text``      a compatible unit mentioned in the paragraphs of the same section (KG neighbours)                 0.45
* ``kg_entity``         a unit-like entity / identifier the KG links to the table                                         0.40
* ``document``          the dominant compatible unit of the whole document (>= 3 mentions, >= 70 % share)                 0.40
* ``header_semantics``  the customary unit of the quantity the header names (``Damping`` -> %, ``Frequency`` -> Hz)         0.35

"Compatible" = same physical dimension as the quantity the header suggests (``Pressure`` accepts psi / kPa, not mm). All candidates are
stored with their evidence; ``decide`` picks one at *query time* by mode:

* ``strict`` (narrow)  only the first four sources (>= 0.70), and only when they agree: a conflict yields no unit;
* ``broad`` (diverse)  every source down to 0.35, aggregated per unit (independent evidence reinforces), best first, alternatives listed,
                       the answer flagged ``inferred``;
* ``off``              no deduced unit (only what the header or the cells say explicitly).

Nothing is converted and no value is changed: units are labels with provenance. Pure functions."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Optional

STRICT_SOURCES = ("header", "cells", "header_phrase", "table_declaration")
BASE_CONF = {"header": 0.97, "cells": 0.90, "header_phrase": 0.85, "table_declaration": 0.75, "table_text": 0.50, "section_text": 0.45,
             "kg_entity": 0.40, "document": 0.40, "header_semantics": 0.35}
MODES = ("strict", "broad", "off")
STRICT_MIN, BROAD_MIN = 0.70, 0.35


@dataclass(frozen=True)
class U:
    symbol: str
    dim: str
    aliases: tuple[str, ...]
    ambiguous: bool = False         # short / common words: only accepted in brackets, headers and declarations, never in running text


# canonical symbol, physical dimension, aliases (matched case-insensitively unless the alias is case-sensitive: see _CS)
LEXICON: list[U] = [
    U("psi", "pressure", ("psi", "lb/in²", "lb/in2", "lbf/in²", "pounds per square inch")), U("ksi", "pressure", ("ksi", "kips per square inch")),
    U("psf", "pressure", ("psf", "lb/ft²", "lb/ft2")), U("Pa", "pressure", ("Pa", "pascal", "pascals"), True), U("kPa", "pressure", ("kPa", "kilopascal", "kilopascals")),
    U("MPa", "pressure", ("MPa", "megapascal", "megapascals")), U("GPa", "pressure", ("GPa", "gigapascal")), U("bar", "pressure", ("bar", "bars"), True),
    U("atm", "pressure", ("atm", "atmosphere", "atmospheres"), True), U("inHg", "pressure", ("inHg", "in. Hg", "inches of mercury")),
    U("mm", "length", ("mm", "millimeter", "millimeters", "millimetre", "millimetres")), U("cm", "length", ("cm", "centimeter", "centimeters")),
    U("m", "length", ("m", "meter", "meters", "metre", "metres"), True), U("km", "length", ("km", "kilometer", "kilometers")),
    U("in", "length", ("in.", "in", "inch", "inches"), True), U("ft", "length", ("ft", "foot", "feet"), True), U("mi", "length", ("mi", "mile", "miles"), True),
    U("g", "acceleration", ("g", "gravity"), True), U("m/s²", "acceleration", ("m/s²", "m/s2", "m/s^2")), U("ft/s²", "acceleration", ("ft/s²", "ft/s2")),
    U("Hz", "frequency", ("Hz", "hertz", "cps", "cycles per second")), U("kHz", "frequency", ("kHz",)), U("rad/s", "angular_frequency", ("rad/s", "radians per second")),
    U("s", "time", ("s", "sec", "second", "seconds"), True), U("ms", "time", ("ms", "millisecond", "milliseconds")), U("min", "time", ("min", "minute", "minutes"), True),
    U("h", "time", ("h", "hr", "hour", "hours"), True), U("d", "time", ("day", "days")), U("month", "time", ("month", "months")), U("yr", "time", ("yr", "year", "years")),
    U("1/yr", "event_rate", ("per year", "/yr", "1/yr", "/year", "per annum", "yr⁻¹", "yr-1", "annual")), U("1/h", "event_rate", ("per hour", "/hr", "1/h")),
    U("%", "ratio", ("%", "percent", "per cent", "pct")), U("ppm", "ratio", ("ppm",)), U("ppb", "ratio", ("ppb",)),
    U("rem", "dose", ("rem", "rems")), U("mrem", "dose", ("mrem",)), U("Sv", "dose", ("Sv", "sievert", "sieverts")), U("mSv", "dose", ("mSv", "millisievert")),
    U("Gy", "dose", ("Gy", "gray"), True), U("rad", "dose", ("rad", "rads"), True), U("Ci", "activity", ("Ci", "curie", "curies")), U("Bq", "activity", ("Bq", "becquerel")),
    U("kg", "mass", ("kg", "kilogram", "kilograms")), U("lb", "mass", ("lb", "lbs", "lbm", "pound", "pounds"), True), U("t", "mass", ("tonne", "tonnes", "metric ton"), True),
    U("N", "force", ("N", "newton", "newtons"), True), U("kN", "force", ("kN", "kilonewton", "kilonewtons")), U("lbf", "force", ("lbf",)), U("kip", "force", ("kip", "kips")),
    U("°C", "temperature", ("°C", "deg C", "degrees C", "degrees Celsius", "celsius")), U("°F", "temperature", ("°F", "deg F", "degrees F", "degrees Fahrenheit", "fahrenheit")),
    U("K", "temperature", ("kelvin",)), U("W", "power", ("W", "watt", "watts"), True), U("kW", "power", ("kW", "kilowatt", "kilowatts")), U("MW", "power", ("MW", "megawatt", "megawatts")),
    U("MWt", "power", ("MWt", "MW(t)", "MWth")), U("MWe", "power", ("MWe", "MW(e)")), U("Btu", "energy", ("Btu", "BTU")), U("J", "energy", ("J", "joule", "joules"), True),
    U("kJ", "energy", ("kJ", "kilojoule")), U("MeV", "energy", ("MeV",)), U("ft-lb", "energy", ("ft-lb", "ft·lb", "ft-lbf")),
    U("gpm", "flow", ("gpm", "gal/min", "gallons per minute")), U("cfm", "flow", ("cfm", "ft³/min")), U("m³/s", "flow", ("m³/s", "m3/s")), U("L/s", "flow", ("L/s", "l/s")),
    U("kg/m³", "density", ("kg/m³", "kg/m3")), U("lb/ft³", "density", ("lb/ft³", "lb/ft3", "pcf")), U("mol", "amount", ("mol", "mole", "moles"), True),
    U("dB", "level", ("dB", "decibel", "decibels")), U("V", "voltage", ("V", "volt", "volts"), True), U("kV", "voltage", ("kV", "kilovolt")), U("A", "current", ("A", "amp", "amps", "ampere"), True),
    U("rpm", "rotation", ("rpm", "r/min")), U("deg", "angle", ("°", "degree", "degrees"), True),
]
_CASE_SENSITIVE = {"Pa", "kPa", "MPa", "GPa", "N", "kN", "K", "W", "kW", "MW", "MWt", "MWe", "J", "kJ", "MeV", "V", "kV", "A", "Hz", "kHz", "Sv", "mSv", "Gy", "Bq", "Ci", "dB", "L/s", "g", "h", "s", "m", "t", "mi", "psi", "ksi"}
_BY_ALIAS: dict[str, U] = {}
for _u in LEXICON:
    for _a in _u.aliases:
        _BY_ALIAS.setdefault(_a.lower() if _u.symbol not in _CASE_SENSITIVE and _a not in _CASE_SENSITIVE else _a, _u)
_BY_SYMBOL = {u.symbol: u for u in LEXICON}

# header keywords -> physical dimension (what the column measures) and the customary unit used when nothing else is known
QUANTITY_HINTS: list[tuple[str, str, Optional[str]]] = [
    (r"\bdamping\b", "ratio", "%"), (r"\bfrequenc(?:y|ies)\b(?!\s+of\s+(?:occurrence|exceedance))", "frequency", "Hz"), (r"\b(?:probabilit|likelihood|exceedance|occurrence|event rate|failure rate)", "event_rate", "1/yr"),
    (r"\b(?:pressure|stress|strength|modulus|yield|tensile|compressive|bearing|shear stress)\b", "pressure", None), (r"\b(?:length|distance|thickness|height|depth|width|diameter|radius|span|spacing|elevation|displacement|drift|clearance|gap)\b", "length", None),
    (r"\b(?:acceleration|pga|zpa|spectral accel)", "acceleration", "g"), (r"\b(?:period|duration|interval|time|lifetime|life|delay)\b", "time", None),
    (r"\btemperature\b", "temperature", None), (r"\bdose\b", "dose", None), (r"\b(?:activity|radioactivity)\b", "activity", None), (r"\b(?:mass|weight)\b", "mass", None),
    (r"\b(?:force|load|thrust)\b", "force", None), (r"\b(?:flow|flow rate|discharge)\b", "flow", None), (r"\bpower\b", "power", None), (r"\benergy\b", "energy", None),
    (r"\bdensity\b", "density", None), (r"\b(?:concentration|fraction|ratio|percentage|percent)\b", "ratio", "%"), (r"\bvoltage\b", "voltage", None),
]
_NO_UNIT = re.compile(r"\b(?:count|counts|number|numbers|no\.?|nr|#|id|index|quantity|qty|rank|item|items|row|rows|ratio of|factor|coefficient|category|class|level|grade)\b|\bno\.?$", re.I)


def normalize(text: str) -> Optional[U]:
    """Lexicon entry for a unit spelling ('kips per square inch', 'KPA' is not accepted: units are case-aware), or None."""
    t = re.sub(r"\s+", " ", text.strip().strip("().,;:[]"))
    return _BY_ALIAS.get(t) or _BY_ALIAS.get(t.lower())


def _alias_regex(u: U, alias: str) -> str:
    esc = re.escape(alias)
    return (rf"(?<![\w/]){esc}(?![\w²³])" if re.match(r"\w", alias[-1]) else rf"(?<![\w]){esc}")


def find_units(text: str, allow_ambiguous: bool = False) -> list[tuple[U, str]]:
    """Units mentioned in ``text`` (unit, matched spelling). Ambiguous spellings ('m', 'in', 'g', 'N', 'bar' ...) are returned only when
    ``allow_ambiguous`` (they are only trusted in brackets, headers and declarations, where the context says 'this is a unit')."""
    found: list[tuple[U, str]] = []
    for u in LEXICON:
        if u.ambiguous and not allow_ambiguous:
            continue
        for a in u.aliases:
            flags = 0 if (u.symbol in _CASE_SENSITIVE or a in _CASE_SENSITIVE) else re.I
            for m in re.finditer(_alias_regex(u, a), text, flags):
                found.append((u, m.group(0)))
    found.sort(key=lambda x: -len(x[1]))                                   # prefer 'kPa' over 'Pa', 'per year' over 'year'
    out: list[tuple[U, str]] = []
    for u, s in found:
        if not any(s in t or t in s for _, t in out):                      # drop duplicates and spellings inside longer ones ('Pa' in 'kPa')
            out.append((u, s))
    return out


# ---------------------------------------------------------------------------------------------------- hints
def quantity_hint(header: str) -> tuple[Optional[str], Optional[str]]:
    """(dimension, customary unit) a column header suggests."""
    h = header.lower()
    for pat, dim, default in QUANTITY_HINTS:
        if re.search(pat, h):
            return dim, default
    return None, None


_DECL = re.compile(r"(?:\bunits?\s*(?:are|is|:)|\bunless otherwise (?:noted|stated|specified)|\ball\s+(?:\w+\s+){0,3}(?:are\s+)?(?:given\s+|shown\s+|expressed\s+)?in\b|"
                   r"\b(?:values?|dimensions?|data|entries|stresses|loads|limits)\s+(?:are\s+)?(?:given\s+|shown\s+|expressed\s+|listed\s+|reported\s+)?in\b|"
                   r"\b(?:given|shown|expressed|listed|reported|measured|stated)\s+in\b|\bin\s+units\s+of\b|"
                   r"\b(?:lengths?|thicknesses|heights?|widths?|diameters?|distances?|depths?|pressures?|stresses|forces|loads|temperatures?|times?|periods?|frequencies|rates?|doses?|masses|weights?)\s+in\b)", re.I)


def declared_units(text: str) -> list[tuple[U, str]]:
    """Units named in a declarative phrase ('All dimensions in mm', 'Units: kPa', 'values are given in psi', 'unless otherwise noted')."""
    out: list[tuple[U, str]] = []
    add = lambda u, s: out.append((u, s)) if (u, s) not in out else None
    for m in _DECL.finditer(text):
        after = text[m.end(): m.end() + 40]
        first = re.match(r"\s*([^\s,;)(]+(?:\s+per\s+[a-z ]{3,18})?)", after)
        if first:                                                            # the word right after the cue may be an ambiguous unit ('in m')
            u = normalize(first.group(1).rstrip(".")) or normalize(first.group(1))
            if u:
                add(u, first.group(1))
        for u, s in find_units(after):
            add(u, s)
        if re.search(r"unless otherwise", m.group(0), re.I):                 # 'Dimensions in mm unless otherwise noted': the unit comes before the cue
            before = re.search(r"([^\s,;(]+)\s*$", text[max(0, m.start() - 24): m.start()])
            if before and normalize(before.group(1)):
                add(normalize(before.group(1)), before.group(1))
    return out


def header_units(header: str) -> tuple[list[tuple[U, str]], list[tuple[U, str]]]:
    """(bracketed units, phrase units) of a header: 'Damping (%)' -> ([%], []); 'Annual probability per year' -> ([], [1/yr])."""
    bracket = []
    for grp in re.findall(r"\(([^)]{1,24})\)|\[([^\]]{1,24})\]", header):
        raw = (grp[0] or grp[1]).strip()
        u = normalize(raw)
        if u:
            bracket.append((u, raw))
    rest = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", header)
    phrase = [(u, s) for u, s in find_units(rest, allow_ambiguous=False)
              if re.search(rf"(?:\bin\s+|,\s*|\bper\s+|/\s*){re.escape(s)}|{re.escape(s)}\s*$", rest, re.I) or u.dim == "event_rate"]
    return bracket, phrase


def cell_unit(cells: list[str]) -> Optional[tuple[U, int, int]]:
    """Unit suffix shared by >= 90 % of the numeric cells: (unit, cells with it, numeric cells)."""
    from kg_tables import parse_number
    numeric = [(c, parse_number(c)) for c in cells if c and parse_number(c)]
    if len(numeric) < 2:
        return None
    suff = Counter()
    for _, p in numeric:
        if p[1]:
            u = normalize(p[1])
            if u:
                suff[u.symbol] += 1
    if not suff:
        return None
    sym, n = suff.most_common(1)[0]
    return (_BY_SYMBOL[sym], n, len(numeric)) if n / len(numeric) >= 0.9 else None


# --------------------------------------------------------------------------------------------- candidates
def doc_unit_stats(texts: list[str]) -> dict[str, dict[str, int]]:
    """Unit mentions by dimension over the texts of a whole document: {dimension: {unit: count}} (unambiguous spellings only)."""
    stats: dict[str, Counter] = {}
    for t in texts:
        taken: list[tuple[int, int]] = []
        for u in LEXICON:
            if u.ambiguous:
                continue
            for a in sorted(u.aliases, key=len, reverse=True):
                flags = 0 if (u.symbol in _CASE_SENSITIVE or a in _CASE_SENSITIVE) else re.I
                for m in re.finditer(_alias_regex(u, a), t, flags):
                    if not any(s < m.end() and m.start() < e for s, e in taken):       # every occurrence counts once
                        taken.append((m.start(), m.end()))
                        stats.setdefault(u.dim, Counter())[u.symbol] += 1
    return {d: dict(c) for d, c in stats.items()}


def _add(cands: dict[str, dict[str, Any]], u: U, source: str, evidence: str, conf: Optional[float] = None) -> None:
    c = cands.setdefault(f"{u.symbol}|{source}", {"unit": u.symbol, "quantity": u.dim, "source": source, "confidence": conf if conf is not None else BASE_CONF[source],
                                                  "evidence": evidence[:200]})
    c["confidence"] = max(c["confidence"], conf if conf is not None else BASE_CONF[source])


def candidates(header: str, cells: list[str], numeric: bool, ctx: Optional[dict[str, Any]] = None, stats: Optional[dict[str, dict[str, int]]] = None) -> list[dict[str, Any]]:
    """All unit hypotheses for one column with their evidence (no choice is made here). ``ctx``: caption, intro, notes, section_texts, entities."""
    if not numeric or _NO_UNIT.search(header) and not header_units(header)[0]:
        return []                                                         # counts, numbers, factors, ranks: dimensionless, never given a unit
    ctx = ctx or {}
    out: dict[str, dict[str, Any]] = {}
    hint_dim, default = quantity_hint(header)
    bracket, phrase = header_units(header)
    for u, raw in bracket:
        _add(out, u, "header", f"header {header!r} has the unit {raw!r}")
    for u, raw in phrase:
        if not bracket:
            _add(out, u, "header_phrase", f"header {header!r} says {raw!r}")
    cu = cell_unit(cells)
    if cu:
        _add(out, cu[0], "cells", f"{cu[1]} of {cu[2]} numeric cells end in {cu[0].symbol}")
    if any(c["source"] in ("header", "cells") for c in out.values()):           # a column with its own unit does not inherit others
        return sorted(out.values(), key=lambda c: -c["confidence"])
    compatible = lambda u: hint_dim is None or u.dim == hint_dim or (hint_dim == "ratio" and u.dim == "ratio")
    meta_texts = {"caption": ctx.get("caption", ""), "introduction": ctx.get("intro", ""), "notes": " ".join(ctx.get("notes", []) or [])}
    for where, text in meta_texts.items():
        if not text:
            continue
        decl = declared_units(text)
        generic = bool(re.search(r"\b(?:all|every|each|unless otherwise|units?\s*[:=])", text, re.I))
        for u, s in decl:
            if compatible(u):                                             # without a hint from the header only a general declaration is strong evidence
                _add(out, u, "table_declaration", f"{where}: declares {s!r} ({text[:90]!r})", None if (hint_dim is not None or generic) else 0.60)
        declared = {u.symbol for u, _ in decl}
        for u, s in find_units(text):
            if u.symbol not in declared and compatible(u) and hint_dim is not None:
                _add(out, u, "table_text", f"{where} mentions {s!r}")
            elif u.symbol not in declared and compatible(u) and hint_dim is None:
                _add(out, u, "table_text", f"{where} mentions {s!r}", 0.40)
    sect = " ".join(ctx.get("section_texts", []) or [])
    for u, s in find_units(sect):
        if compatible(u) and (hint_dim is not None):
            _add(out, u, "section_text", f"the same section mentions {s!r}")
    for lab in ctx.get("entities", []) or []:
        u = normalize(lab)
        if u and compatible(u):
            _add(out, u, "kg_entity", f"the KG links the table to the entity {lab!r}")
    if hint_dim and stats and stats.get(hint_dim):
        total = sum(stats[hint_dim].values())
        sym, n = max(stats[hint_dim].items(), key=lambda kv: kv[1])
        if total >= 3 and n / total >= 0.7 and sym in _BY_SYMBOL:
            _add(out, _BY_SYMBOL[sym], "document", f"{n} of {total} {hint_dim} units mentioned in the document are {sym}")
    if default and default in _BY_SYMBOL:
        _add(out, _BY_SYMBOL[default], "header_semantics", f"{header!r} is a {hint_dim}; customary unit {default}")
    return sorted(out.values(), key=lambda c: -c["confidence"])


def decide(cands: list[dict[str, Any]], mode: str = "strict") -> dict[str, Any]:
    """The unit of a column for a mode, from its stored candidates. Returns ``unit`` (or None), ``confidence``, ``source``, ``inferred``,
    ``conflict`` and the ``evidence``; in broad mode also ``alternatives``."""
    if mode not in MODES:
        raise ValueError(f"units mode must be one of {MODES}")
    none = {"unit": None, "confidence": 0.0, "source": None, "inferred": False, "conflict": False, "evidence": []}
    if mode == "off":
        explicit = [c for c in cands if c["source"] in ("header", "cells")]
        return {**none, **({"unit": explicit[0]["unit"], "confidence": explicit[0]["confidence"], "source": explicit[0]["source"], "evidence": [explicit[0]["evidence"]]} if explicit else {})}
    if mode == "strict":
        pool = [c for c in cands if c["source"] in STRICT_SOURCES and c["confidence"] >= STRICT_MIN]
        by_unit: dict[str, dict[str, Any]] = {}
        for c in pool:
            if c["unit"] not in by_unit or c["confidence"] > by_unit[c["unit"]]["confidence"]:
                by_unit[c["unit"]] = c
        ranked = sorted(by_unit.values(), key=lambda c: -c["confidence"])
        if not ranked:
            return none
        if len(ranked) > 1 and ranked[0]["confidence"] - ranked[1]["confidence"] < 0.10:
            return {**none, "conflict": True, "evidence": [f"{c['unit']} ({c['source']}): {c['evidence']}" for c in ranked[:3]]}
        return {**none, "unit": ranked[0]["unit"], "confidence": ranked[0]["confidence"], "source": ranked[0]["source"], "evidence": [ranked[0]["evidence"]]}
    # broad: independent evidence for the same unit reinforces it
    agg: dict[str, dict[str, Any]] = {}
    for c in cands:
        if c["confidence"] < BROAD_MIN:
            continue
        a = agg.setdefault(c["unit"], {"unit": c["unit"], "quantity": c["quantity"], "miss": 1.0, "sources": [], "evidence": []})
        a["miss"] *= 1 - c["confidence"]
        a["sources"].append(c["source"])
        a["evidence"].append(c["evidence"])
    ranked = sorted(({**a, "confidence": round(1 - a["miss"], 3)} for a in agg.values()), key=lambda a: -a["confidence"])
    if not ranked:
        return none
    top = ranked[0]
    return {"unit": top["unit"], "confidence": top["confidence"], "source": top["sources"][0], "sources": top["sources"],
            "inferred": not any(s in ("header", "cells") for s in top["sources"]), "conflict": False, "evidence": top["evidence"][:4],
            "alternatives": [{"unit": a["unit"], "confidence": a["confidence"], "sources": a["sources"]} for a in ranked[1:4]]}


def apply(cols: list, rows: list[list[str]], ctx: Optional[Any] = None, stats: Optional[dict[str, dict[str, int]]] = None) -> None:
    """Fill ``unit_candidates`` / ``quantity`` and the strict decision (``unit``, ``unit_source``, ``unit_confidence``) of every numeric column.
    ``ctx`` is a ``kg_tables.Context``; the explicit unit that ``kg_tables.profile`` found in the header stays unless the strict decision says otherwise."""
    cdict = {} if ctx is None else {"caption": ctx.caption, "intro": ctx.intro, "notes": ctx.notes, "section_texts": ctx.section_texts, "entities": ctx.entities + ctx.idents}
    for j, c in enumerate(cols):
        if c.role == "text" or (c.pg_type == "text" and c.role != "numeric"):
            continue
        if c.pg_type == "text":
            continue
        c.unit_candidates = candidates(c.header, [r[j] for r in rows], True, cdict, stats)
        c.quantity = quantity_hint(c.header)[0] or (c.unit_candidates[0]["quantity"] if c.unit_candidates else "")
        d = decide(c.unit_candidates, "strict")
        if d["unit"]:
            c.unit, c.unit_source, c.unit_confidence = d["unit"], d["source"], d["confidence"]
        elif c.unit:                                                        # explicit header / cell unit that the lexicon does not know
            c.unit_source, c.unit_confidence = "header", 0.9
