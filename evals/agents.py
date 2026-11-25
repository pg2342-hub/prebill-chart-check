"""Agents the gate compares. Each takes a case input ({"visit": id}) and returns a review."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from golive_gate import Response  # noqa: E402

from prebill.checker import Reading, StructuredReader, review, scrubber_review  # noqa: E402

VISITS = {v["id"]: v for v in json.loads((ROOT / "data/pulled_visits.json").read_text())["visits"]}
TRUTH = {v["id"]: v["truth"] for v in yaml.safe_load((ROOT / "data/visits.yml").read_text())["visits"]}


def scrubber(inp):
    """Claim-format checks only (never opens the note)."""
    return scrubber_review(VISITS[inp["visit"]])


_structured = StructuredReader()


def structured(inp):
    """Same rules, MDM estimated from structured EHR fields only."""
    return review(VISITS[inp["visit"]], _structured)


class _Truth:
    def read(self, v):
        t = TRUTH[v["id"]]
        return Reading(t["problems"], t["data"], t["risk"], t["time_min"], t["separate_em"])


def answer_key(inp):
    """The rules fed the hand-labeled reading: checks that the rules reproduce the answer key."""
    return review(VISITS[inp["visit"]], _Truth())


_recorded = None


def claude_recorded(inp):
    """Claude's recorded readings of each note, re-graded with no API call."""
    global _recorded
    if _recorded is None:
        from prebill.reader import RecordedReader
        _recorded = RecordedReader()
    out = review(VISITS[inp["visit"]], _recorded)
    return Response(out, cost_usd=out["cost_usd"])


_v1 = None


def claude_v1_recorded(inp):
    """Claude's readings with the first prompt (before the AMA fever note was added)."""
    global _v1
    if _v1 is None:
        from prebill.reader import REC_PATH_V1, RecordedReader
        _v1 = RecordedReader(REC_PATH_V1)
    out = review(VISITS[inp["visit"]], _v1)
    return Response(out, cost_usd=out["cost_usd"])


_live = None


def claude_live(inp):
    """Claude reads each note now (needs ANTHROPIC_API_KEY); replies are recorded."""
    global _live
    if _live is None:
        from prebill.reader import ClaudeReader
        _live = ClaudeReader(record=True)
    out = review(VISITS[inp["visit"]], _live)
    return Response(out, cost_usd=out["cost_usd"])
