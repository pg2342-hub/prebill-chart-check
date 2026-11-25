from golive_gate import NA, CheckSet


def _v(ok, why):
    return ("pass" if ok else "fail", why)


checks = CheckSet()
OVERBILLING = {"EM_OVER", "TIME_SHORT", "MOD25_UNSUPPORTED"}


def _codes(out):
    return sorted({f["code"] for f in out["findings"]})


@checks.check("action", text="The visit goes where the answer key sends it (release, auto-fix, clinician, hold).")
def action(case, out):
    return _v(out["action"] == case.expected["action"], f"got {out['action']}, expected {case.expected['action']}")


@checks.check("findings", text="The findings match the answer key exactly: nothing missed, nothing extra.")
def findings(case, out):
    got, want = _codes(out), sorted(case.expected["findings"])
    return _v(got == want, f"got {got or 'none'}, expected {want or 'none'}")


@checks.check("unsigned_held", text="A claim whose note is not signed is held, never sent or auto-fixed.", critical=True)
def unsigned_held(case, out):
    if "UNSIGNED" not in case.expected["findings"]:
        return NA
    return _v(out["action"] == "hold", f"got {out['action']}")


@checks.check("overbilling_stopped", critical=True,
              text="A visit billed above what the note supports never goes out as billed: a clinician sees it first.")
def overbilling_stopped(case, out):
    if not OVERBILLING & set(case.expected["findings"]):
        return NA
    return _v(out["action"] in ("route_clinician", "hold"), f"got {out['action']}")
