"""Local stand-in for an athenaOne-style FHIR R4 endpoint, serving the synthetic bundle.

What it copies from athenahealth's public FHIR R4 documentation: a token endpoint using
the OAuth 2.0 client-credentials grant, bearer tokens with a short lifetime, and the
`ah-practice` parameter required on every request. What it adds to test the client: paged
search results, one 429 with Retry-After on the first search, and a token that expires
after a set number of requests. It is a test double, not athenahealth's API.

Run: python -m prebill.mock_server --port 8765
"""
from __future__ import annotations

import argparse
import base64
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

PRACTICE = "Organization/a-1.Practice-1959001"
CLIENT_ID, CLIENT_SECRET = "demo-client", "demo-secret"  # local test double only

REF_FIELDS = {
    "DocumentReference": lambda r: [e["reference"] for e in r.get("context", {}).get("encounter", [])],
    "Observation": lambda r: [r.get("encounter", {}).get("reference")],
    "MedicationRequest": lambda r: [r.get("encounter", {}).get("reference")],
    "ServiceRequest": lambda r: [r.get("encounter", {}).get("reference")],
    "Claim": lambda r: [e["reference"] for it in r.get("item", []) for e in it.get("encounter", [])],
}


class Store:
    def __init__(self, bundle: dict, token_ttl_requests: int = 40, throttle_first: bool = True):
        self.by_type: dict[str, dict[str, dict]] = {}
        for e in bundle["entry"]:
            r = e["resource"]
            self.by_type.setdefault(r["resourceType"], {})[r["id"]] = r
        self.tokens: dict[str, int] = {}
        self.token_ttl_requests = token_ttl_requests
        self.throttle_pending = throttle_first
        self.lock = threading.Lock()
        self.log: list[str] = []


def make_handler(store: Store, page_size_cap: int = 10):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep test output quiet
            pass

        def _send(self, code: int, body: dict, headers: dict | None = None):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/fhir+json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _outcome(self, code: int, msg: str, headers: dict | None = None):
            self._send(code, {"resourceType": "OperationOutcome",
                              "issue": [{"severity": "error", "code": "processing", "diagnostics": msg}]}, headers)

        def do_POST(self):
            if urlparse(self.path).path != "/oauth2/v1/token":
                return self._outcome(404, "not found")
            body = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            auth = self.headers.get("Authorization", "")
            ok = auth == "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if not ok or body.get("grant_type") != ["client_credentials"]:
                return self._send(401, {"error": "invalid_client"})
            tok = secrets.token_hex(12)
            with store.lock:
                store.tokens[tok] = store.token_ttl_requests
            self._send(200, {"access_token": tok, "token_type": "Bearer", "expires_in": 300})

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            tok = self.headers.get("Authorization", "").removeprefix("Bearer ")
            with store.lock:
                left = store.tokens.get(tok, 0)
                if left <= 0:
                    store.tokens.pop(tok, None)
                    return self._outcome(401, "token missing or expired")
                store.tokens[tok] = left - 1
            if q.get("ah-practice") != PRACTICE:
                return self._outcome(400, "ah-practice parameter is required")
            parts = [p for p in u.path.split("/") if p][2:]  # drop fhir/r4
            store.log.append(self.path)
            if len(parts) == 2:
                r = store.by_type.get(parts[0], {}).get(parts[1])
                return self._send(200, r) if r else self._outcome(404, "not found")
            rtype = parts[0]
            with store.lock:
                if store.throttle_pending:
                    store.throttle_pending = False
                    return self._outcome(429, "rate limited", {"Retry-After": "0"})
            matches = sorted(store.by_type.get(rtype, {}).values(), key=lambda r: r["id"])
            if "identifier" in q:
                system = q["identifier"].split("|")[0]
                matches = [r for r in matches if any(i.get("system") == system for i in r.get("identifier", []))]
            if "encounter" in q:
                matches = [r for r in matches if q["encounter"] in REF_FIELDS[rtype](r)]
            if "beneficiary" in q:
                matches = [r for r in matches if r.get("beneficiary", {}).get("reference") == q["beneficiary"]]
            count = min(int(q.get("_count", page_size_cap)), page_size_cap)
            offset = int(q.get("_offset", 0))
            page = matches[offset:offset + count]
            links = [{"relation": "self", "url": self.path}]
            if offset + count < len(matches):
                nq = {**q, "_offset": offset + count, "_count": count}
                links.append({"relation": "next", "url": f"http://{self.headers['Host']}{u.path}?{urlencode(nq)}"})
            self._send(200, {"resourceType": "Bundle", "type": "searchset", "total": len(matches), "link": links,
                             "entry": [{"fullUrl": f"{rtype}/{r['id']}", "resource": r} for r in page]})

    return H


def serve(bundle: dict, port: int = 0, **kw) -> tuple[ThreadingHTTPServer, Store]:
    store = Store(bundle, **kw)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, store


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bundle", default="data/fhir_bundle.json")
    a = ap.parse_args()
    srv, _ = serve(json.load(open(a.bundle)), a.port)
    print(f"mock FHIR at http://127.0.0.1:{srv.server_port}/fhir/r4 (practice {PRACTICE})")
    threading.Event().wait()
