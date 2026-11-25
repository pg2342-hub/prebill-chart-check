"""Claude reads the note and returns MDM elements as structured JSON.

The model sees only the note and the list of same-day procedure codes, never the billed
visit level, so it cannot anchor on what was billed. The level itself is computed by the
fixed rules in checker.py from what the model returns.

Recording: every live reply is saved to recordings/claude_reads.json keyed by
(visit, run). `RecordedReader` replays them, so CI and the demo page re-grade the same
model outputs without an API key. A visit with no recording raises; it never falls back.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .checker import Reading

MODEL = "claude-opus-5-5"
PRICE_PER_MTOK = {"input": 4.00, "output": 20.00}  # Claude Opus 5.5 list price, USD
REC_PATH = Path(__file__).resolve().parent.parent / "recordings" / "claude_reads.json"

SYSTEM = """You read urgent care progress notes for a pre-bill coding review.
Grade medical decision making (MDM) with the AMA 2021 office/outpatient E/M table. Use only what the note documents.

Problems addressed:
- minimal: 1 self-limited or minor problem
- low: 2+ self-limited or minor problems; 1 stable chronic illness; 1 acute uncomplicated illness or injury
- moderate: 1+ chronic illness with exacerbation, progression or side effects; 2+ stable chronic illnesses;
  1 undiagnosed new problem with uncertain prognosis; 1 acute illness with systemic symptoms; 1 acute complicated injury
- high: 1+ chronic illness with severe exacerbation; 1 acute or chronic illness or injury that poses a threat to life or bodily function

Data reviewed and analyzed (each unique test ordered or result reviewed counts once; ordering a test includes reviewing its result):
- minimal: none, or 1 element
- low (limited): category 1 = any 2 of: review of external notes from each unique source, each unique test result,
  each unique test ordered; OR category 2 = assessment needing an independent historian (e.g. a parent)
- moderate: 1 of 3 categories: (1) any 3 of: external notes, unique test results, unique tests ordered, independent historian;
  (2) independent interpretation of a test performed by another physician (not separately billed);
  (3) discussion of management with an external physician or qualified professional
- high: 2 of the 3 categories above

Risk of patient management:
- minimal: minimal risk from further testing or treatment
- low: over-the-counter drugs, rest, minor treatment
- moderate: prescription drug management; decision about minor surgery with risk factors; social determinants limiting care
- high: drug therapy needing intensive monitoring for toxicity; decision regarding hospitalization or escalation of hospital-level care

Also report:
- time_min: total minutes on the date of the encounter if the note states it, else null
- separate_em: only when a same-day procedure is listed. true if the note documents a problem evaluated and managed
  beyond the procedure itself (the decision to do a minor procedure is part of the procedure). null if no procedure is listed.
- evidence: one short phrase from the note for each of problems, data and risk."""

SCHEMA = {
    "type": "object",
    "properties": {
        "problems": {"type": "string", "enum": ["minimal", "low", "moderate", "high"]},
        "data": {"type": "string", "enum": ["minimal", "low", "moderate", "high"]},
        "risk": {"type": "string", "enum": ["minimal", "low", "moderate", "high"]},
        "time_min": {"type": ["integer", "null"]},
        "separate_em": {"type": ["boolean", "null"]},
        "evidence": {
            "type": "object",
            "properties": {"problems": {"type": "string"}, "data": {"type": "string"}, "risk": {"type": "string"}},
            "required": ["problems", "data", "risk"], "additionalProperties": False,
        },
    },
    "required": ["problems", "data", "risk", "time_min", "separate_em", "evidence"],
    "additionalProperties": False,
}

GLOBAL_PROCS = {"12001": "simple laceration repair up to 2.5 cm", "12002": "simple laceration repair 2.6 to 7.5 cm",
                "10060": "incision and drainage of abscess", "29125": "short arm splint"}


def _prompt(v: dict) -> str:
    procs = [f"{p['code']} ({GLOBAL_PROCS[p['code']]})" for p in v["billed"]["procedures"] if p["code"] in GLOBAL_PROCS]
    return (f"Visit type: {v['visit_type']} patient, age {v['patient']['age']}.\n"
            f"Same-day procedures with a global period: {', '.join(procs) if procs else 'none'}.\n\n"
            f"Progress note:\n{v['note']}")


def _load() -> dict:
    return json.loads(REC_PATH.read_text()) if REC_PATH.exists() else {}


class ClaudeReader:
    def __init__(self, record: bool = True, effort: str = "low"):
        import anthropic  # optional dependency: pip install anthropic
        self.client = anthropic.Anthropic()
        self.record, self.effort = record, effort
        self.calls: dict[str, int] = {}

    def read(self, v: dict) -> Reading:
        n = self.calls.get(v["id"], 0)
        self.calls[v["id"]] = n + 1
        t0 = time.time()
        resp = self.client.beta.messages.create(
            model=MODEL, max_tokens=4000, system=SYSTEM,
            messages=[{"role": "user", "content": _prompt(v)}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"{v['id']}: model declined to read the note")
        out = json.loads(next(b.text for b in resp.content if b.type == "text"))
        u = resp.usage
        cost = (u.input_tokens * PRICE_PER_MTOK["input"] + u.output_tokens * PRICE_PER_MTOK["output"]) / 1e6
        rec = {**out, "model": resp.model, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
               "cost_usd": round(cost, 6), "latency_s": round(time.time() - t0, 2)}
        if self.record:
            data = _load()
            data[f"{v['id']}#{n}"] = rec
            REC_PATH.parent.mkdir(exist_ok=True)
            REC_PATH.write_text(json.dumps(dict(sorted(data.items())), indent=1))
        return _reading(rec)


class RecordedReader:
    """Replays recorded model replies; the n-th read of a visit gets run n's reply."""

    def __init__(self):
        self.data = _load()
        self.calls: dict[str, int] = {}

    def read(self, v: dict) -> Reading:
        n = self.calls.get(v["id"], 0)
        self.calls[v["id"]] = n + 1
        key = f"{v['id']}#{n}"
        if key not in self.data:
            raise KeyError(f"no recorded reading for {key}; run the live eval first")
        return _reading(self.data[key])


def _reading(rec: dict) -> Reading:
    return Reading(rec["problems"], rec["data"], rec["risk"], rec["time_min"], rec["separate_em"],
                   rec.get("evidence", {}), rec.get("cost_usd", 0.0))
