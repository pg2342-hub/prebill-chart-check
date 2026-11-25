"""Build data/fhir_bundle.json (FHIR R4 transaction Bundle) from data/visits.yml."""
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from prebill.fhir import to_bundle  # noqa: E402

root = Path(__file__).resolve().parent.parent
visits = yaml.safe_load((root / "data/visits.yml").read_text())["visits"]
bundle = to_bundle(visits)
(root / "data/fhir_bundle.json").write_text(json.dumps(bundle, indent=1))
print(f"{len(visits)} visits -> {len(bundle['entry'])} FHIR resources")
