"""A small FHIR R4 client that pulls a day's visits into the review queue.

Two targets, same code:
- an athenaOne-style server: OAuth 2.0 client credentials, every search scoped with the
  `ah-practice` parameter (as athenahealth documents for its FHIR R4 API);
- any open FHIR R4 server (the CI job uses the public HAPI test server).

Handles search paging (Bundle.link next), 429 with Retry-After, and a token that expires
mid-pull.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .fhir import IDENT_SYSTEM, normalize

RELATED = ["DocumentReference", "Observation", "MedicationRequest", "ServiceRequest", "Claim"]


class FhirError(RuntimeError):
    pass


class FhirClient:
    def __init__(self, base_url: str, token_url: str | None = None, client_id: str | None = None,
                 client_secret: str | None = None, practice: str | None = None, max_retries: int = 4,
                 timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.token_url, self.client_id, self.client_secret = token_url, client_id, client_secret
        self.practice = practice
        self.max_retries, self.timeout = max_retries, timeout
        self._token: str | None = None
        self._token_expires = 0.0
        self.stats = {"requests": 0, "retries_429": 0, "token_fetches": 0, "pages": 0}

    # ---- auth -------------------------------------------------------------------------
    def _bearer(self, force: bool = False) -> dict:
        if not self.token_url:
            return {}
        if force or not self._token or time.time() > self._token_expires - 30:
            creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            req = urllib.request.Request(
                self.token_url, data=urllib.parse.urlencode({"grant_type": "client_credentials",
                                                             "scope": "system/*.read"}).encode(),
                headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"})
            body = json.loads(urllib.request.urlopen(req, timeout=self.timeout).read())
            self._token = body["access_token"]
            self._token_expires = time.time() + int(body.get("expires_in", 300))
            self.stats["token_fetches"] += 1
        return {"Authorization": f"Bearer {self._token}"}

    # ---- http -------------------------------------------------------------------------
    def _get(self, url: str) -> dict:
        refreshed = False
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, headers={"Accept": "application/fhir+json",
                                                       "User-Agent": "prebill-chart-check/0.1", **self._bearer()})
            self.stats["requests"] += 1
            try:
                return json.loads(urllib.request.urlopen(req, timeout=self.timeout).read())
            except urllib.error.HTTPError as e:
                if e.code == 401 and self.token_url and not refreshed:
                    self._bearer(force=True)  # token expired early or was revoked
                    refreshed = True
                    continue
                if e.code in (429, 502, 503) and attempt < self.max_retries:
                    self.stats["retries_429"] += e.code == 429
                    time.sleep(min(float(e.headers.get("Retry-After") or 2 ** attempt), 30))
                    continue
                raise FhirError(f"GET {url} -> {e.code} {e.read()[:200]!r}") from e
        raise FhirError(f"GET {url} failed after {self.max_retries} retries")

    def _params(self, params: dict) -> dict:
        return {**params, **({"ah-practice": self.practice} if self.practice else {})}

    def search(self, rtype: str, params: dict) -> list[dict]:
        url = f"{self.base}/{rtype}?{urllib.parse.urlencode(self._params(params))}"
        out: list[dict] = []
        while url:
            bundle = self._get(url)
            self.stats["pages"] += 1
            out += [e["resource"] for e in bundle.get("entry", []) if e.get("resource", {}).get("resourceType") == rtype]
            url = next((l["url"] for l in bundle.get("link", []) if l.get("relation") == "next"), None)
        return out

    def read(self, rtype: str, rid: str) -> dict:
        q = urllib.parse.urlencode(self._params({}))
        return self._get(f"{self.base}/{rtype}/{rid}" + (f"?{q}" if q else ""))

    # ---- the pull ---------------------------------------------------------------------
    def pull_visits(self, page_size: int = 10) -> list[dict]:
        """Every encounter tagged with this project's identifier, normalized for the checker."""
        encounters = self.search("Encounter", {"identifier": f"{IDENT_SYSTEM}|", "_count": page_size})
        visits = []
        for enc in sorted(encounters, key=lambda e: e["id"]):
            ref = f"Encounter/{enc['id']}"
            related = {rt: self.search(rt, {"encounter": ref}) for rt in RELATED}
            pid = enc["subject"]["reference"].split("/", 1)[1]
            patient = self.read("Patient", pid)
            cov = self.search("Coverage", {"beneficiary": f"Patient/{pid}"})
            visits.append(normalize(enc, related, patient, cov[0] if cov else None))
        return visits
