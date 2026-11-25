# prebill-chart-check

[![CI](https://github.com/pg2342-hub/prebill-chart-check/actions/workflows/ci.yml/badge.svg)](https://github.com/pg2342-hub/prebill-chart-check/actions/workflows/ci.yml)

Demo page: https://pranavgudise.dev/prebill-chart-check/

Pulls a day's urgent care visits from a FHIR R4 API, reads each progress note, and checks the draft claim against it before the claim goes out. Every visit ends in one of four places: **release** (claim goes out as is), **auto-fix** (a simple claim change such as adding modifier QW), **route to the clinician** (the visit level or a modifier is not supported by the note), or **hold** (the note is not signed).

All patients, clinics, clinicians, notes and claims here are synthetic.

## What is in it

| Part | File | What it does |
|---|---|---|
| Connector | `prebill/client.py` | FHIR R4 client: OAuth 2.0 client credentials, the `ah-practice` parameter athenahealth documents for its FHIR R4 API, paged searches, 429 retry with Retry-After, token refresh mid-pull |
| Test server | `prebill/mock_server.py` | Local athenaOne-style stand-in that serves the synthetic bundle, throttles the first search and expires tokens, so the client's failure handling is tested |
| Mapping | `prebill/fhir.py` | Visits to FHIR (Encounter, Patient, Coverage, DocumentReference, Observation, MedicationRequest, ServiceRequest, draft Claim) and back; the round trip is tested field by field |
| Reader | `prebill/reader.py` | Claude reads the note and returns the three AMA 2021 MDM elements, documented time, and whether a same-day procedure had a separate problem, as schema-checked JSON. It never sees the billed level |
| Rules | `prebill/checker.py`, `data/rules.yml` | Fixed rules turn the reading and the claim into findings: visit level over or under the note, time short, modifier 25 missing or unsupported, QW missing on Medicare waived tests, unspecified diagnosis despite a positive test, procedure without a supporting diagnosis, unsigned note |
| Eval | `evals/` | 30 hand-labeled visits (10 routine, 10 edge, 10 risky) and a go-live gate on [golive-gate](https://github.com/pg2342-hub/golive-gate) |

## The gate

Go live only if, on every one of 3 runs: at least 28 of 30 visits match the answer key exactly (findings and action), every risky visit passes, and there are zero critical failures. The two critical checks: an unsigned note is never released, and a visit billed above what its note supports never goes out without a clinician seeing it.

Three setups run through the same cases and rules:

| Setup | What reads the note |
|---|---|
| Claim-format checks | Nothing: only the claim, the way a claim scrubber sees it |
| Structured-only rules | Nothing: the MDM elements are estimated from orders, results, prescriptions and diagnosis codes |
| Claude reader | Claude reads the note (recorded replies in `recordings/`, re-graded in CI without an API call) |

Results are on the demo page and in each CI run's `reports` artifact.

## Run it

```
pip install -r requirements.txt
python scripts/make_bundle.py                 # data/visits.yml -> FHIR R4 transaction bundle
python scripts/pull.py                        # pull over FHIR from the local mock into data/pulled_visits.json
python -m pytest -q tests
cd evals && golive run golive.yml             # re-grade Claude's recorded readings (no key needed)
golive run golive.yml --agent agents:claude_live   # Claude reads every note now (needs ANTHROPIC_API_KEY)
```

Against a real open FHIR server instead of the mock:

```
python scripts/pull.py --base https://hapi.fhir.org/baseR4 --load
```

CI runs that against the public HAPI FHIR R4 test server on every push (synthetic data only).

## Limits

- The mock copies athenahealth's documented FHIR R4 auth flow and practice parameter; it is not athenahealth's API, and draft charges are modeled as a FHIR `Claim`, which is not how athenaOne exposes charges.
- The rule set is a small teaching subset of public coding rules (AMA E/M guidelines, CMS QW and global-period lists, NCCI modifier 25 policy). Real payer rules vary.
- 30 visits is a small eval. It shows how a go-live decision would be made, not that a checker is ready for real claims.
