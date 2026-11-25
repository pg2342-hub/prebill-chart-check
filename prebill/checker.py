"""Check a pulled visit's draft claim against its note before the claim goes out.

The checker has two parts:
- a reader that turns the note into MDM elements (problems, data, risk), documented time,
  and whether a same-day procedure had a separate problem worked up;
- fixed rules that turn the reading plus the draft claim into findings and one action.

Readers are swappable: `StructuredReader` uses only structured EHR fields (orders, results,
prescriptions, diagnosis codes) and never reads the note; `ClaudeReader` (prebill.reader)
reads the note itself. The rules are the same for both, so the eval measures the reading.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RULES = yaml.safe_load((Path(__file__).resolve().parent.parent / "data" / "rules.yml").read_text())
LEVEL = RULES["em"]["element_levels"]
LEVEL_NAMES = {v: k for k, v in LEVEL.items()}

# Where each finding sends the visit. Hold beats route, route beats auto-fix.
ROUTE = {
    "UNSIGNED": "hold",
    "EM_OVER": "route_clinician", "EM_UNDER": "route_clinician", "TIME_SHORT": "route_clinician",
    "MOD25_UNSUPPORTED": "route_clinician", "DX_NOT_SUPPORTING": "route_clinician",
    "QW_MISSING": "auto_fix", "DX_UNSPECIFIED": "auto_fix", "MOD25_MISSING": "auto_fix",
}
ACTION_RANK = {"release": 0, "auto_fix": 1, "route_clinician": 2, "hold": 3}
LEVEL_FINDINGS = {"EM_OVER", "EM_UNDER", "TIME_SHORT"}


@dataclass
class Reading:
    problems: str
    data: str
    risk: str
    time_min: int | None = None
    separate_em: bool | None = None
    evidence: dict = field(default_factory=dict)
    cost_usd: float = 0.0


@dataclass
class Finding:
    code: str
    detail: str
    fix: dict | None = None  # only for auto-fix findings: the exact claim change


def mdm_level(problems: str, data: str, risk: str) -> int:
    vals = sorted((LEVEL[problems], LEVEL[data], LEVEL[risk]), reverse=True)
    return vals[1]  # highest level met by at least two of the three elements


def em_code(visit_type: str, level: int) -> str:
    return RULES["em"]["codes"][visit_type][level]


def time_code(visit_type: str, minutes: int | None) -> str | None:
    best = None
    for lvl in (2, 3, 4, 5):
        code = em_code(visit_type, lvl)
        if minutes is not None and minutes >= RULES["em"]["time_minutes"][code]:
            best = code
    return best


class StructuredReader:
    """Reads only structured fields. A fair stand-in for rules written without the note."""

    def read(self, v: dict) -> Reading:
        tests = len(v["results"]) + len(v["orders"])
        data = "moderate" if tests >= 3 else "low" if tests == 2 else "minimal"
        risk = "high" if v["referral_ed"] else "moderate" if v["rx"] else "low"
        if v["referral_ed"]:
            problems = "high"
        elif len(v["billed"]["dx"]) >= 2 and not any(d.startswith("W5") for d in v["billed"]["dx"]):
            problems = "moderate"
        else:
            problems = "low"
        m = re.search(r"[Tt]otal time[^0-9]{0,40}(\d+)\s*min", v["note"])
        time_min = int(m.group(1)) if m else None
        procs = [p for p in v["billed"]["procedures"] if p["code"] in RULES["global_procedures"]]
        separate = None
        if procs:
            # a second diagnosis outside the injury chapter suggests a separate problem
            separate = any(not d.startswith(("S", "T", "W")) for d in v["billed"]["dx"])
        return Reading(problems, data, risk, time_min, separate)


def check(v: dict, r: Reading) -> tuple[list[Finding], str]:
    b = v["billed"]
    f: list[Finding] = []
    if not v["signed"]:
        f.append(Finding("UNSIGNED", "Progress note is not signed; nothing can be billed until it is."))
    # E/M level (or a same-day procedure with no separate problem)
    procs = {p["code"]: p for p in b["procedures"]}
    global_procs = [c for c in procs if c in RULES["global_procedures"]]
    if global_procs and r.separate_em is False:
        f.append(Finding("MOD25_UNSUPPORTED",
                         f"Visit charge billed with {global_procs[0]} but the note shows no problem beyond the procedure."))
    else:
        if global_procs and r.separate_em and "25" not in b["em_modifiers"]:
            f.append(Finding("MOD25_MISSING", f"Separate problem documented; add modifier 25 to {b['em']}.",
                             {"op": "add_modifier", "code": b["em"], "modifier": "25"}))
        if b["basis"] == "time":
            supported = time_code(v["visit_type"], r.time_min)
            if supported is None or supported < b["em"]:
                f.append(Finding("TIME_SHORT", f"Billed {b['em']} by time; {r.time_min} min documented supports "
                                               f"{supported or 'no time-based code'}."))
        else:
            supported = em_code(v["visit_type"], mdm_level(r.problems, r.data, r.risk))
            if b["em"] > supported:
                f.append(Finding("EM_OVER", f"Billed {b['em']}; note supports {supported} "
                                            f"(problems {r.problems}, data {r.data}, risk {r.risk})."))
            elif b["em"] < supported:
                f.append(Finding("EM_UNDER", f"Billed {b['em']}; note supports {supported} "
                                             f"(problems {r.problems}, data {r.data}, risk {r.risk})."))
    # claim-format rules
    if v["payer"] in RULES["qw_required"]["payers"]:
        for c in RULES["qw_required"]["codes"]:
            if c in procs and "QW" not in procs[c]["modifiers"]:
                f.append(Finding("QW_MISSING", f"{c} is CLIA-waived; Medicare needs modifier QW.",
                                 {"op": "add_modifier", "code": c, "modifier": "QW"}))
    results = {(x["test"], x["value"]) for x in v["results"]}
    for s in RULES["specificity"]:
        if (s["test"], s["value"]) not in results:
            continue
        for d in b["dx"]:
            target = s.get("specific") if d == s.get("unspecified") else s.get("specific_map", {}).get(d)
            if target:
                f.append(Finding("DX_UNSPECIFIED", f"Positive {s['test']} result documented; {d} can be coded as {target}.",
                                 {"op": "replace_dx", "from": d, "to": target}))
    for c, allowed in RULES["dx_support"].items():
        if c in procs and not any(d.startswith(tuple(allowed)) for d in b["dx"]):
            f.append(Finding("DX_NOT_SUPPORTING", f"{c} is billed without a diagnosis that supports it."))
    action = max((ROUTE[x.code] for x in f), key=ACTION_RANK.__getitem__, default="release")
    return f, action


def review(v: dict, reader) -> dict:
    r = reader.read(v)
    findings, action = check(v, r)
    return {"visit": v["id"], "action": action,
            "findings": [{"code": x.code, "detail": x.detail, **({"fix": x.fix} if x.fix else {})} for x in findings],
            "reading": {"problems": r.problems, "data": r.data, "risk": r.risk, "time_min": r.time_min,
                        "separate_em": r.separate_em, "evidence": r.evidence},
            "cost_usd": r.cost_usd}


def scrubber_review(v: dict) -> dict:
    """Claim-format checks only, the kind a claim scrubber runs: it never sees the note."""
    b = v["billed"]
    f = []
    procs = {p["code"]: p for p in b["procedures"]}
    if v["payer"] in RULES["qw_required"]["payers"]:
        f += [{"code": "QW_MISSING", "fix": {"op": "add_modifier", "code": c, "modifier": "QW"}}
              for c in RULES["qw_required"]["codes"] if c in procs and "QW" not in procs[c]["modifiers"]]
    if any(c in RULES["global_procedures"] for c in procs) and "25" not in b["em_modifiers"]:
        f.append({"code": "MOD25_MISSING", "fix": {"op": "add_modifier", "code": b["em"], "modifier": "25"}})
    for c, allowed in RULES["dx_support"].items():
        if c in procs and not any(d.startswith(tuple(allowed)) for d in b["dx"]):
            f.append({"code": "DX_NOT_SUPPORTING"})
    action = max((ROUTE[x["code"]] for x in f), key=ACTION_RANK.__getitem__, default="release")
    return {"visit": v["id"], "action": action, "findings": f, "reading": None, "cost_usd": 0.0}
