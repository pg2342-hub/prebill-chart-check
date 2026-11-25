"""Map visits to FHIR R4 resources and back.

`to_bundle` turns the synthetic visit file into a FHIR R4 transaction Bundle (what an EHR
would hold). `normalize` turns the resources pulled for one encounter back into the flat
visit record the checker reads. The round trip is tested, so the checker never sees a
field the FHIR pull could not have produced.
"""
from __future__ import annotations

import base64
from datetime import date, timedelta

ID_PREFIX = "pcc-"
IDENT_SYSTEM = "urn:prebill-chart-check:visit"
ENCOUNTER_DATE = "2026-11-02"
LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
CPT = "http://www.ama-assn.org/go/cpt"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
POS = "https://www.cms.gov/Medicare/Coding/place-of-service-codes/Place_of_Service_Code_Set"

TEST_CODES = {
    "strep": ("78012-2", "Streptococcus pyogenes Ag [Presence] in Throat by Rapid immunoassay"),
    "flu_a": ("80382-5", "Influenza virus A Ag [Presence] in Upper respiratory specimen by Rapid immunoassay"),
    "covid": ("94558-4", "SARS-CoV-2 (COVID-19) Ag [Presence] in Respiratory system specimen by Rapid immunoassay"),
}
RESULT_CODES = {"positive": ("10828004", "Positive"), "negative": ("260385009", "Negative")}


def _rid(kind: str, vid: str, n: int | None = None) -> str:
    return f"{ID_PREFIX}{kind}-{vid}" + (f"-{n}" if n is not None else "")


def _entry(resource: dict) -> dict:
    url = f"{resource['resourceType']}/{resource['id']}"
    return {"fullUrl": url, "resource": resource, "request": {"method": "PUT", "url": url}}


def visit_resources(v: dict) -> list[dict]:
    vid = v["id"]
    enc_ref = {"reference": f"Encounter/{_rid('enc', vid)}"}
    pat_id, cov_id = _rid("pat", vid), _rid("cov", vid)
    pat_ref = {"reference": f"Patient/{pat_id}"}
    born = date.fromisoformat(ENCOUNTER_DATE) - timedelta(days=365 * v["patient"]["age"] + 30 + v["patient"]["age"] // 4)
    out: list[dict] = [
        {"resourceType": "Patient", "id": pat_id, "gender": v["patient"]["sex"], "birthDate": born.isoformat(),
         "name": [{"family": "Synthetic", "given": [f"Patient {vid.upper()}"]}]},
        {"resourceType": "Coverage", "id": cov_id, "status": "active", "beneficiary": pat_ref,
         "payor": [{"display": v["payer"]}]},
        {"resourceType": "Encounter", "id": _rid("enc", vid), "status": "finished",
         "identifier": [{"system": IDENT_SYSTEM, "value": vid}],
         "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
         "type": [{"text": f"{v['visit_type']} patient"}], "subject": pat_ref,
         "period": {"start": f"{ENCOUNTER_DATE}T09:00:00-07:00"},
         "serviceProvider": {"display": v["clinic"]}},
    ]
    doc = {"resourceType": "DocumentReference", "id": _rid("doc", vid), "status": "current",
           "docStatus": "final" if v["signed"] else "preliminary",
           "type": {"coding": [{"system": LOINC, "code": "11506-3", "display": "Progress note"}]},
           "subject": pat_ref, "context": {"encounter": [enc_ref]},
           "content": [{"attachment": {"contentType": "text/plain",
                                       "data": base64.b64encode(v["note"].encode()).decode()}}]}
    if v["signed"]:
        doc["authenticator"] = {"display": "Synthetic Clinician, PA-C"}
    out.append(doc)
    for i, r in enumerate(v["results"], 1):
        code, display = TEST_CODES[r["test"]]
        val_code, val_display = RESULT_CODES[r["value"]]
        out.append({"resourceType": "Observation", "id": _rid("obs", vid, i), "status": "final",
                    "code": {"coding": [{"system": LOINC, "code": code, "display": display}]},
                    "subject": pat_ref, "encounter": enc_ref,
                    "valueCodeableConcept": {"coding": [{"system": SNOMED, "code": val_code, "display": val_display}]}})
    for i, name in enumerate(v["rx"], 1):
        out.append({"resourceType": "MedicationRequest", "id": _rid("rx", vid, i), "status": "active",
                    "intent": "order", "medicationCodeableConcept": {"text": name},
                    "subject": pat_ref, "encounter": enc_ref})
    for i, name in enumerate(v["orders"], 1):
        out.append({"resourceType": "ServiceRequest", "id": _rid("sr", vid, i), "status": "completed",
                    "intent": "order", "code": {"text": name}, "subject": pat_ref, "encounter": enc_ref})
    if v["referral_ed"]:
        out.append({"resourceType": "ServiceRequest", "id": _rid("ref", vid), "status": "active",
                    "intent": "order", "category": [{"coding": [{"system": SNOMED, "code": "3457005",
                                                                 "display": "Patient referral"}]}],
                    "code": {"text": "Emergency department referral"}, "subject": pat_ref, "encounter": enc_ref})
    b = v["billed"]
    items = [{"sequence": 1, "productOrService": {"coding": [{"system": CPT, "code": b["em"]}]},
              "modifier": [{"coding": [{"system": CPT, "code": m}]} for m in b["em_modifiers"]],
              "diagnosisSequence": list(range(1, len(b["dx"]) + 1)), "encounter": [enc_ref],
              "locationCodeableConcept": {"coding": [{"system": POS, "code": "20"}]}}]
    for n, p in enumerate(b["procedures"], 2):
        items.append({"sequence": n, "productOrService": {"coding": [{"system": CPT, "code": p["code"]}]},
                      "modifier": [{"coding": [{"system": CPT, "code": m}]} for m in p["modifiers"]],
                      "diagnosisSequence": [1], "encounter": [enc_ref],
                      "locationCodeableConcept": {"coding": [{"system": POS, "code": "20"}]}})
    out.append({"resourceType": "Claim", "id": _rid("claim", vid), "status": "draft",
                "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type",
                                     "code": "professional"}]},
                "use": "claim", "patient": pat_ref, "created": ENCOUNTER_DATE,
                "provider": {"display": v["clinic"]}, "priority": {"coding": [{"code": "normal"}]},
                "insurance": [{"sequence": 1, "focal": True, "coverage": {"reference": f"Coverage/{cov_id}"}}],
                "supportingInfo": [{"sequence": 1, "category": {"text": "em-basis"}, "valueString": b["basis"]}],
                "diagnosis": [{"sequence": i, "diagnosisCodeableConcept": {"coding": [{"system": ICD10, "code": d}]}}
                              for i, d in enumerate(b["dx"], 1)],
                "item": items})
    return out


def to_bundle(visits: list[dict]) -> dict:
    return {"resourceType": "Bundle", "type": "transaction",
            "entry": [_entry(r) for v in visits for r in visit_resources(v)]}


def _code(cc: dict) -> str:
    return cc["coding"][0]["code"]


def normalize(encounter: dict, related: dict[str, list[dict]], patient: dict, coverage: dict | None) -> dict:
    """Flatten one encounter's resources into the record the checker reads."""
    vid = next(i["value"] for i in encounter.get("identifier", []) if i.get("system") == IDENT_SYSTEM)
    start = date.fromisoformat(encounter["period"]["start"][:10])
    born = date.fromisoformat(patient["birthDate"])
    age = start.year - born.year - ((start.month, start.day) < (born.month, born.day))
    docs = related.get("DocumentReference", [])
    doc = docs[0] if docs else None
    note = base64.b64decode(doc["content"][0]["attachment"]["data"]).decode() if doc else ""
    claims = related.get("Claim", [])
    if len(claims) != 1:
        raise ValueError(f"{vid}: expected one draft claim, found {len(claims)}")
    claim = claims[0]
    items = sorted(claim["item"], key=lambda it: it["sequence"])
    em_item, proc_items = items[0], items[1:]
    basis = next((s.get("valueString") for s in claim.get("supportingInfo", [])
                  if s.get("category", {}).get("text") == "em-basis"), "mdm")
    test_by_loinc = {c: t for t, (c, _) in TEST_CODES.items()}
    value_by_snomed = {c: name for name, (c, _) in RESULT_CODES.items()}
    results = [{"test": test_by_loinc[_code(o["code"])], "value": value_by_snomed[_code(o["valueCodeableConcept"])]}
               for o in sorted(related.get("Observation", []), key=lambda r: r["id"])]
    srs = sorted(related.get("ServiceRequest", []), key=lambda r: r["id"])
    is_referral = lambda sr: any(c.get("code") == "3457005" for cat in sr.get("category", []) for c in cat.get("coding", []))
    return {
        "id": vid,
        "clinic": encounter.get("serviceProvider", {}).get("display", ""),
        "payer": (coverage or {}).get("payor", [{}])[0].get("display", "Self-pay"),
        "patient": {"age": age, "sex": patient["gender"]},
        "visit_type": encounter["type"][0]["text"].split()[0],
        "billed": {
            "em": _code(em_item["productOrService"]),
            "em_modifiers": [_code(m) for m in em_item.get("modifier", [])],
            "basis": basis,
            "procedures": [{"code": _code(it["productOrService"]),
                            "modifiers": [_code(m) for m in it.get("modifier", [])]} for it in proc_items],
            "dx": [_code(d["diagnosisCodeableConcept"]) for d in sorted(claim["diagnosis"], key=lambda d: d["sequence"])],
        },
        "signed": bool(doc and doc.get("docStatus") == "final" and doc.get("authenticator")),
        "results": results,
        "rx": [m["medicationCodeableConcept"]["text"] for m in sorted(related.get("MedicationRequest", []), key=lambda r: r["id"])],
        "orders": [sr["code"]["text"] for sr in srs if not is_referral(sr)],
        "referral_ed": any(is_referral(sr) for sr in srs),
        "note": note,
    }


VISIT_FIELDS = ["id", "clinic", "payer", "patient", "visit_type", "billed", "signed", "results", "rx",
                "orders", "referral_ed", "note"]
