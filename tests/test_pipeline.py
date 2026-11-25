import json
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from prebill import mock_server  # noqa: E402
from prebill.checker import Reading, StructuredReader, check, mdm_level, review, scrubber_review, time_code  # noqa: E402
from prebill.client import FhirClient  # noqa: E402
from prebill.fhir import VISIT_FIELDS, to_bundle  # noqa: E402

VISITS = yaml.safe_load((ROOT / "data/visits.yml").read_text())["visits"]


def truth_reading(v):
    t = v["truth"]
    return Reading(t["problems"], t["data"], t["risk"], t["time_min"], t["separate_em"])


class TestConnector(unittest.TestCase):
    def test_round_trip_through_mock(self):
        srv, store = mock_server.serve(to_bundle(VISITS), token_ttl_requests=25)
        base = f"http://127.0.0.1:{srv.server_port}"
        c = FhirClient(f"{base}/fhir/r4", token_url=f"{base}/oauth2/v1/token", client_id=mock_server.CLIENT_ID,
                       client_secret=mock_server.CLIENT_SECRET, practice=mock_server.PRACTICE)
        got = {v["id"]: v for v in c.pull_visits(page_size=7)}
        srv.shutdown()
        self.assertEqual(len(got), 30)
        for v in VISITS:
            for k in VISIT_FIELDS:
                self.assertEqual(got[v["id"]][k], v[k], f"{v['id']}.{k}")
        self.assertEqual(c.stats["retries_429"], 1)       # the throttled first search was retried
        self.assertGreater(c.stats["token_fetches"], 1)   # the expired token was refreshed mid-pull
        self.assertGreater(c.stats["pages"], 30)          # encounters came back over several pages

    def test_practice_parameter_required(self):
        srv, _ = mock_server.serve(to_bundle(VISITS[:1]), throttle_first=False)
        base = f"http://127.0.0.1:{srv.server_port}"
        c = FhirClient(f"{base}/fhir/r4", token_url=f"{base}/oauth2/v1/token", client_id=mock_server.CLIENT_ID,
                       client_secret=mock_server.CLIENT_SECRET, practice=None)
        with self.assertRaises(Exception):
            c.search("Encounter", {})
        srv.shutdown()

    def test_bad_credentials_rejected(self):
        srv, _ = mock_server.serve(to_bundle(VISITS[:1]))
        base = f"http://127.0.0.1:{srv.server_port}"
        c = FhirClient(f"{base}/fhir/r4", token_url=f"{base}/oauth2/v1/token", client_id="x", client_secret="y",
                       practice=mock_server.PRACTICE)
        with self.assertRaises(Exception):
            c.pull_visits()
        srv.shutdown()


class TestRules(unittest.TestCase):
    def test_mdm_two_of_three(self):
        self.assertEqual(mdm_level("low", "minimal", "moderate"), 3)
        self.assertEqual(mdm_level("moderate", "minimal", "moderate"), 4)
        self.assertEqual(mdm_level("high", "minimal", "high"), 5)
        self.assertEqual(mdm_level("minimal", "minimal", "low"), 2)

    def test_time_thresholds(self):
        self.assertEqual(time_code("established", 22), "99213")
        self.assertEqual(time_code("established", 30), "99214")
        self.assertEqual(time_code("new", 44), "99203")
        self.assertIsNone(time_code("established", 5))

    def test_answer_key_reproduced(self):
        for v in VISITS:
            f, action = check(v, truth_reading(v))
            self.assertEqual(sorted(x.code for x in f), sorted(v["expected"]["findings"]), v["id"])
            self.assertEqual(action, v["expected"]["action"], v["id"])

    def test_auto_fixes_never_touch_visit_level(self):
        for v in VISITS:
            for x in check(v, truth_reading(v))[0]:
                if x.fix and x.fix["op"] == "replace_dx":
                    continue
                if x.fix:
                    self.assertEqual(x.fix["op"], "add_modifier")
                    self.assertIn(x.fix["modifier"], ("QW", "25"))

    def test_scrubber_releases_unsigned_and_overbilled(self):
        out = {v["id"]: scrubber_review(v) for v in VISITS}
        self.assertEqual(out["v16"]["action"], "release")
        self.assertEqual(out["v05"]["action"], "release")

    def test_structured_reader_runs(self):
        for v in VISITS:
            r = review(v, StructuredReader())
            self.assertIn(r["action"], ("release", "auto_fix", "route_clinician", "hold"))


class TestRecorded(unittest.TestCase):
    def test_missing_recording_raises(self):
        from prebill.reader import RecordedReader
        rr = RecordedReader()
        rr.data = {}
        with self.assertRaises(KeyError):
            rr.read(VISITS[0])


if __name__ == "__main__":
    unittest.main()
