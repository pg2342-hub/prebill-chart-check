"""Build the demo page (docs/index.html) from the eval reports and the FHIR pull.

  python scripts/build_demo.py      # after the three golive runs wrote reports/{scrubber,structured,claude}
"""
import json
import subprocess
import sys
from collections import Counter
from html import escape
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
STARTER = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "scripts/starter.html"
VISITS = yaml.safe_load((ROOT / "data/visits.yml").read_text())["visits"]
PULL = json.loads((ROOT / "data/pulled_visits.json").read_text())
BUNDLE = json.loads((ROOT / "data/fhir_bundle.json").read_text())
HAPI = ROOT / "reports/hapi_pull.json"
REC = json.loads((ROOT / "recordings/claude_reads.json").read_text())

SETUPS = [
    ("scrubber", "Claim-format checks", "Reads the claim only, the way a claim scrubber does"),
    ("structured", "Structured-only rules", "Same rules; MDM guessed from orders, results and prescriptions"),
    ("claude", "Claude reads the note", "Same rules; Claude grades the note's MDM, 3 runs"),
]
ACTIONS = [("release", "Release"), ("auto_fix", "Auto-fix"), ("route_clinician", "To the clinician"), ("hold", "Hold")]
ACTION_TEXT = dict(ACTIONS)
GROUPS = {"routine": "Routine", "edge": "Edge", "risky": "Risky"}
FINDING_TEXT = {
    "EM_OVER": "visit level above the note", "EM_UNDER": "visit level below the note",
    "TIME_SHORT": "too few minutes for a time-based level", "MOD25_UNSUPPORTED": "no separate problem for modifier 25",
    "MOD25_MISSING": "modifier 25 missing", "QW_MISSING": "QW missing on a waived test",
    "DX_UNSPECIFIED": "diagnosis less specific than the result", "DX_NOT_SUPPORTING": "procedure without a supporting diagnosis",
    "UNSIGNED": "note not signed",
}

res = {k: json.loads((ROOT / f"reports/{k}/results.json").read_text()) for k, _, _ in SETUPS}
by_case = {k: {c["id"]: c for c in r["cases"]} for k, r in res.items()}
crit_checks = {"unsigned_held", "overbilling_stopped"}


def case_state(k, cid):
    runs = by_case[k][cid]["runs"]
    if any(any(r["verdicts"].get(c, {}).get("result") == "fail" for c in crit_checks) for r in runs):
        return "crit"
    return "pass" if all(r["passed"] for r in runs) else "fail"


def fmt_findings(codes):
    return ", ".join(FINDING_TEXT[c] for c in codes) if codes else "none"


# ---- result card -------------------------------------------------------------------
rows = []
for k, name, sub in SETUPS:
    states = [case_state(k, v["id"]) for v in VISITS]
    runs = res[k]["runs"]
    passed_all = sum(s == "pass" for s in states)
    crit = sum(s == "crit" for s in states)
    decision = res[k]["decision"]
    cells = "".join(
        f'<a class="cell {s}" href="#case-{v["id"]}" data-case="{v["id"]}" '
        f'aria-label="{escape(name)} {v["id"]} {"passed" if s == "pass" else "failed with a critical error" if s == "crit" else "failed"}"></a>'
        for v, s in zip(VISITS, states))
    run_note = f"on all {runs} runs" if runs > 1 else "1 run (rules are deterministic)"
    rows.append(f"""<div class="vrow"><div class="vwho"><b>{name}</b><span>{sub}</span></div>
<div class="strip" role="group" aria-label="{escape(name)}: every visit">{cells}</div>
<div class="vscore"><b>{passed_all}</b><span>of 30 passed {run_note}{f'<br>{crit} critical' if crit else ''}</span></div>
<span class="gate {'go' if decision == 'go' else 'nogo'}">{'GO' if decision == 'go' else 'NO-GO'}</span></div>""")

# ---- the queue: where each setup sent the 30 visits ---------------------------------
def out_of(k, cid, run=0):
    return by_case[k][cid]["runs"][run]["output"]


queue_views = {}
for k, name, _ in SETUPS + [("key", "Answer key", "")]:
    lanes = {a: [] for a, _ in ACTIONS}
    for v in VISITS:
        if k == "key":
            act, ok = v["expected"]["action"], True
        else:
            act = out_of(k, v["id"])["action"]
            ok = act == v["expected"]["action"]
        lanes[act].append((v, ok))
    queue_views[k] = lanes


def lane_html(k):
    out = []
    for a, label in ACTIONS:
        chips = "".join(
            f'<a class="chip{"" if ok else " off"}" href="#case-{v["id"]}" data-case="{v["id"]}" '
            f'title="{escape(v["title"])}{"" if ok else " (answer key: " + ACTION_TEXT[v["expected"]["action"]] + ")"}">'
            f'{v["id"]}</a>' for v, ok in queue_views[k][a])
        out.append(f'<div class="lane"><div class="lh"><b>{label}</b><span class="n">{len(queue_views[k][a])}</span></div>'
                   f'<div class="chips">{chips or "<span class=empty>none</span>"}</div></div>')
    return "".join(out)


queue_panels = "".join(
    f'<div class="lanes" data-view="{k}"{"" if k == "claude" else " hidden"}>{lane_html(k)}</div>'
    for k in ["key", "scrubber", "structured", "claude"])
wrong = {k: sum(not ok for a in queue_views[k] for _, ok in queue_views[k][a]) for k, _, _ in SETUPS}

# ---- Claude run stats --------------------------------------------------------------
recs = list(REC.values())
cost = sum(r["cost_usd"] for r in recs)
lat = sorted(r["latency_s"] for r in recs)
p50 = lat[len(lat) // 2]
models = Counter(r["model"] for r in recs)
claude_runs = res["claude"]["runs"]
level_agree = sum(
    all(by_case["claude"][v["id"]]["runs"][i]["output"]["reading"][f] == v["truth"][f] for f in ("problems", "data", "risk"))
    for v in VISITS for i in range(claude_runs))

# ---- every visit ---------------------------------------------------------------------
trs = []
for i, v in enumerate(VISITS, 1):
    b = v["billed"]
    claim = escape(b["em"] + ("-" + "-".join(b["em_modifiers"]) if b["em_modifiers"] else "") +
                   "".join(f", {p['code']}" + ("-" + "-".join(p["modifiers"]) if p["modifiers"] else "") for p in b["procedures"]))
    exp = f'{ACTION_TEXT[v["expected"]["action"]]}<div class="ch">{escape(fmt_findings(v["expected"]["findings"]))}</div>'
    cells = []
    for k, name, _ in SETUPS:
        s = case_state(k, v["id"])
        o = out_of(k, v["id"])
        got = sorted({f["code"] for f in o["findings"]})
        why = "" if s == "pass" else f'<div class="why">{ACTION_TEXT[o["action"]]}: {escape(fmt_findings(got))}</div>'
        cells.append(f'<td class="v {"ok" if s == "pass" else "bad" + (" crit" if s == "crit" else "")}">'
                     f'{"Pass" if s == "pass" else "Fail"}{why}</td>')
    r = out_of("claude", v["id"])["reading"]
    ev = r.get("evidence") or {}
    reading = (f'<details><summary>Claude: problems {r["problems"]}, data {r["data"]}, risk {r["risk"]}'
               f'{", " + str(r["time_min"]) + " min" if r["time_min"] else ""}'
               f'{", separate problem" if r["separate_em"] else ", no separate problem" if r["separate_em"] is False else ""}</summary>'
               f'<ul class="ev">' + "".join(f'<li><b>{k}</b> “{escape(ev.get(k, ""))}”</li>' for k in ("problems", "data", "risk")) +
               f'</ul><pre class="note">{escape(v["note"].strip())}</pre></details>')
    trs.append(f"""<tr id="case-{v['id']}" data-tier="{v['group']}"><td class="n">{v['id']}</td>
<td class="tx">{escape(v['title'])}<div class="ch">{escape(v['payer'])}, {v['visit_type']} patient · billed {claim} · {', '.join(b['dx'])}{'' if v['signed'] else ' · <b>unsigned</b>'}</div>{reading}</td>
<td>{exp}</td>{''.join(cells)}</tr>""")
counts = Counter(v["group"] for v in VISITS)

# ---- connector ----------------------------------------------------------------------
st = PULL["stats"]
types = Counter(e["resource"]["resourceType"] for e in BUNDLE["entry"])
hapi_line = ""
if HAPI.exists():
    h = json.loads(HAPI.read_text())
    hapi_line = (f'<p class="note">The same client also ran against the public HAPI FHIR R4 test server: it loaded the '
                 f'synthetic bundle, pulled {len(h["visits"])} visits back over {h["stats"]["pages"]} search pages and '
                 f'{h["stats"]["requests"]} requests, and every field matched the source.</p>')

html = STARTER.read_text()
head, body = html.split("<main>", 1)
head = (head.replace("<title>BUILD NAME</title>", "<title>Pre-bill chart check</title>")
        .replace("ONE SENTENCE WITH THE RESULT AND ITS KEY NUMBER.",
                 f"Pulls urgent care visits over FHIR R4 and checks each draft claim against its note; with Claude reading "
                 f"the note, {sum(case_state('claude', v['id']) == 'pass' for v in VISITS)} of 30 visits match the answer key on all 3 runs.")
        .replace("/* ---- page-specific rules go here ---- */", PAGE_CSS := """/* ---- page-specific rules go here ---- */
.strip{grid-template-columns:repeat(30,minmax(0,1fr))}
.lanes{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.lane{border:1px solid var(--rule);border-radius:16px;background:#fbfaf6;padding:14px 14px 16px;min-height:150px}
.lane:nth-child(4){background:var(--ink);color:var(--paper);border-color:var(--ink)}
.lh{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px}
.lh b{font:400 19px/1.2 var(--serif)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font:500 12px/1 var(--mono);padding:6px 8px;border-radius:999px;background:var(--green-wash);color:var(--green-deep);text-decoration:none;border:1.5px solid transparent}
.lane:nth-child(4) .chip{background:rgb(244 242 236/.14);color:var(--paper)}
.chip.off{background:var(--paper);color:var(--ink);border:1.5px dashed var(--ink)}
.empty{font-size:13px;opacity:.7}
.qnote{color:var(--ink-2);font-size:14px;margin:10px 0 0}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:8px 0 0}
.stat{border:1px solid var(--rule);border-radius:16px;background:#fbfaf6;padding:16px}
.stat b{display:block;font:400 30px/1 var(--serif);font-variant-numeric:tabular-nums}
.stat span{display:block;color:var(--ink-2);font-size:13px;margin-top:6px;line-height:1.35}
details summary{cursor:pointer;font-size:13px;color:var(--ink-2);margin-top:6px}
.ev{margin:6px 0;padding-left:18px;font-size:13px;color:var(--ink-2)}
pre.note{white-space:pre-wrap;font-size:12px;max-width:44em;background:var(--paper);border:1px solid var(--rule-soft);border-radius:10px;padding:10px;margin:6px 0 0}
.scroll{overflow-x:auto}
table.t{min-width:860px}
@media (max-width:1000px){.lanes,.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:640px){.strip{grid-template-columns:repeat(15,minmax(0,1fr))}}"""))

c_pass = sum(case_state("claude", v["id"]) == "pass" for v in VISITS)
s_pass = sum(case_state("structured", v["id"]) == "pass" for v in VISITS)
k_pass = sum(case_state("scrubber", v["id"]) == "pass" for v in VISITS)
body = f"""<main>
<div class="head">
<h1>Pre-bill chart check</h1>
<p class="lead">Pulls a day's urgent care visits over FHIR R4, reads each progress note, and checks the draft claim against it before
the claim goes out. Each visit is released, auto-fixed, sent to the clinician, or held.</p>
</div>

<section class="verdict" aria-labelledby="verdict-h">
  <div class="vhead"><h2 id="verdict-h">Same 30 visits, same rules, same gate</h2><p>Gate: <b>28 of 30 match the answer key on every run, all 10 risky visits, zero critical errors</b></p></div>
  {''.join(rows)}
  <div class="legend"><span><i class="pass"></i>Matched the answer key</span><span><i class="fail"></i>Wrong findings or wrong place</span><span><i class="crit"></i>Critical: an unsigned or overbilled claim would have gone out</span><span>Each square is one visit. Select one to read it.</span></div>
</section>

<div class="layout">
<div class="content">
  <h2 id="why">Why it matters</h2>
  <p class="point">Most of what decides whether a claim is right lives in the note, not the claim. Checking only the claim's format
  sent every overbilled and unsigned visit out the door ({k_pass} of 30 matched). Rules that guess the visit level from orders and
  prescriptions avoided those critical misses but raised false alarms ({s_pass} of 30). With Claude reading the note and the same
  fixed rules deciding, {c_pass} of 30 visits matched the answer key on all {claude_runs} runs.</p>

  <h2 id="queue">Where the visits went</h2>
  <div class="seg" role="radiogroup" aria-label="Whose routing to show">
    <button type="button" role="radio" aria-checked="false" data-q="key">Answer key</button>
    <button type="button" role="radio" aria-checked="false" data-q="scrubber">Claim-format <span>{wrong['scrubber']} off</span></button>
    <button type="button" role="radio" aria-checked="false" data-q="structured">Structured-only <span>{wrong['structured']} off</span></button>
    <button type="button" role="radio" aria-checked="true" data-q="claude">Claude <span>{wrong['claude']} off</span></button>
  </div>
  {queue_panels}
  <p class="qnote">Dashed chips went somewhere other than the answer key says (run 1). Auto-fixes only add modifier QW or 25 or swap an
  unspecified diagnosis for the one a positive test supports; a visit level is never changed without the clinician.</p>

  <h2 id="pull">The pull</h2>
  <p class="point">The visits arrive the way they would from the EHR: a FHIR R4 search per encounter, with OAuth client credentials and the
  <span class="n">ah-practice</span> parameter athenahealth documents for its FHIR API, against a local stand-in that throttles and expires tokens.</p>
  <div class="stats">
    <div class="stat"><b>{len(PULL['visits'])}</b><span>visits normalized from {sum(types.values())} FHIR resources</span></div>
    <div class="stat"><b>{st['pages']}</b><span>search pages over {st['requests']} requests</span></div>
    <div class="stat"><b>{st['retries_429']}</b><span>429 retried after Retry-After</span></div>
    <div class="stat"><b>{st['token_fetches']}</b><span>tokens fetched as each one expired</span></div>
  </div>
  {hapi_line}

  <h2 id="detail">Every visit</h2>
  <div class="seg" role="radiogroup" aria-label="Filter visits">
    <button type="button" role="radio" aria-checked="true" data-t="all">All&nbsp; <span>30</span></button>
    {''.join(f'<button type="button" role="radio" aria-checked="false" data-t="{g}">{GROUPS[g]}&nbsp; <span>{counts[g]}</span></button>' for g in GROUPS)}
  </div>
  <div class="scroll"><table class="t" id="cases"><thead><tr><th>#</th><th>Visit</th><th>Answer key</th><th>Claim-format</th><th>Structured</th><th>Claude</th></tr></thead><tbody>
  {''.join(trs)}
  </tbody></table></div>
</div>

<aside class="side" aria-label="About this build">
  <nav aria-label="On this page"><a href="#why">Why it matters</a><a href="#queue">Where the visits went</a><a href="#pull">The pull</a><a href="#detail">Every visit</a></nav>
  <div><h3>Data</h3><p>All 30 visits, notes, patients, clinics and claims are synthetic and hand-labeled. The coding rules are a small subset of public ones:
  AMA 2021 E/M guidelines, the CMS CLIA-waived (QW) list, CMS global periods, and NCCI modifier 25 policy. The FHIR server is a local stand-in, not athenahealth's API.</p></div>
  <div><h3>Model</h3><p>{', '.join(models)} at low effort, structured JSON output. It never sees the billed level. {len(recs)} readings,
  ${cost:.2f} in total, median {p50:.1f} s each. Its MDM grades matched the hand labels on {level_agree} of {len(VISITS) * claude_runs} readings.</p></div>
  <div><h3>Run it yourself</h3><p>Python 3.10+. Re-grading the recorded readings needs no API key.</p><pre>pip install -r requirements.txt
python scripts/pull.py
cd evals && golive run golive.yml</pre></div>
  <div><a class="pill" href="https://github.com/pg2342-hub/prebill-chart-check">Code on GitHub</a></div>
</aside>
</div>
</main>
<script>
document.querySelectorAll('.seg').forEach(g=>{{const bs=[...g.querySelectorAll('button')];bs.forEach(b=>b.addEventListener('click',()=>{{
 bs.forEach(x=>x.setAttribute('aria-checked',String(x===b)));
 if(b.dataset.q){{document.querySelectorAll('.lanes').forEach(l=>l.hidden=l.dataset.view!==b.dataset.q);}}
 if(b.dataset.t){{const t=b.dataset.t;document.querySelectorAll('#cases tbody tr').forEach(r=>r.hidden=!(t==='all'||r.dataset.tier===t));}}
}}))}});
document.querySelectorAll('.cell,.chip').forEach(c=>c.addEventListener('click',()=>{{
 document.querySelectorAll('#cases tbody tr').forEach(r=>r.hidden=false);
 document.querySelectorAll('.seg button[data-t]').forEach(x=>x.setAttribute('aria-checked',String(x.dataset.t==='all')));
 document.querySelectorAll('#cases tr.lit').forEach(r=>r.classList.remove('lit'));
 const row=document.getElementById('case-'+c.dataset.case); row&&row.classList.add('lit');
}}));
</script></body></html>
"""
out = ROOT / "docs/index.html"
out.parent.mkdir(exist_ok=True)
(ROOT / "docs/.nojekyll").write_text("")
out.write_text(head + body)
subprocess.run([sys.executable, str(ROOT / "scripts/demo_chrome.py"), str(out), "--repo", "prebill-chart-check"], check=True)
print("wrote", out)
