"""Pull the visits over FHIR into data/pulled_visits.json (the review queue's input).

  python scripts/pull.py                      # local athenaOne-style mock (OAuth + ah-practice)
  python scripts/pull.py --base https://hapi.fhir.org/baseR4 --load   # public HAPI test server
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from prebill.client import FhirClient  # noqa: E402
from prebill import mock_server  # noqa: E402

root = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("--base", help="FHIR R4 base URL of an open server (default: start the local mock)")
ap.add_argument("--load", action="store_true", help="POST the synthetic transaction bundle to --base first")
ap.add_argument("--out", default=str(root / "data/pulled_visits.json"))
a = ap.parse_args()
bundle = json.loads((root / "data/fhir_bundle.json").read_text())

t0 = time.time()
if a.base:
    if a.load:
        req = urllib.request.Request(a.base.rstrip("/"), data=json.dumps(bundle).encode(), method="POST",
                                     headers={"Content-Type": "application/fhir+json",
                                              "User-Agent": "prebill-chart-check/0.1"})
        resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
        print(f"loaded {len(resp.get('entry', []))} resources into {a.base}")
    client = FhirClient(a.base)
    target = a.base
else:
    srv, _ = mock_server.serve(bundle)
    base = f"http://127.0.0.1:{srv.server_port}"
    client = FhirClient(f"{base}/fhir/r4", token_url=f"{base}/oauth2/v1/token", client_id=mock_server.CLIENT_ID,
                        client_secret=mock_server.CLIENT_SECRET, practice=mock_server.PRACTICE)
    target = "local athenaOne-style mock"
visits = client.pull_visits()
Path(a.out).parent.mkdir(parents=True, exist_ok=True)
Path(a.out).write_text(json.dumps({"source": target, "stats": client.stats, "seconds": round(time.time() - t0, 2),
                                   "visits": visits}, indent=1))
print(f"pulled {len(visits)} visits from {target}: {client.stats}")
