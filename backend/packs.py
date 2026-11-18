"""Loads domain packs from packs/*.json. A pack can have `modes` (the Code Assistant has three audience modes);
resolve() flattens pack + mode into one normal pack dict that the pipeline understands."""
import json
from pathlib import Path

from . import sandbox

PACK_DIR = Path(__file__).resolve().parent.parent / "packs"
_RESOLVED = {}


def load_all():
    found = []
    for f in sorted(PACK_DIR.glob("*.json")):
        p = json.loads(f.read_text(encoding="utf-8"))
        found.append(p)
    found.sort(key=lambda p: p.get("order", 0))      # stable: packs without "order" keep file-name order
    return {p["id"]: p for p in found}


def _band(frac):
    """Expected score range, derived from the share of test cases a submission really passes."""
    return [8, 10] if frac >= 1 else [4, 7] if frac >= 0.5 else [1, 3]


def _build_test(kind, mode, problems, t, tid):
    prob = problems[t["problem"]]
    if kind == "score":
        text = "PROBLEM: %s\n\n%s:\n%s" % (prob["statement"], mode["input_label"], t["submission"])
        if "expected" in t:
            expected, frac = t["expected"], None
        else:   # ground truth comes from actually running the submission
            res = sandbox.run_cases(t["submission"], prob["func"], prob["cases"])
            if "sandbox_unavailable" in str(res["error"]):
                raise RuntimeError("The code runner could not start: " + res["error"])
            frac = res["passed"] / res["total"]
            expected = _band(frac)
        return {"id": tid, "input": text, "expected": expected, "pass_rate": frac}
    text = "CUSTOMER MESSAGE: %s\n\nERROR OR PROBLEM: %s\n\nCODE:\n%s" % (t["message"], t["error"], t["buggy"])
    return {"id": tid, "input": text, "func": prob["func"], "cases": prob["cases"],
            "solution": prob["solution"], "buggy": t["buggy"]}


def resolve(packs, pack_id, mode=None):
    raw = packs[pack_id]                      # KeyError -> unknown pack
    if "modes" not in raw:
        return raw
    if mode not in raw["modes"]:
        raise KeyError("unknown mode: %s" % mode)
    key = (pack_id, mode)
    if key in _RESOLVED:
        return _RESOLVED[key]
    m = raw["modes"][mode]
    flat = {k: v for k, v in raw.items() if k not in ("modes", "problems")}
    flat.update({k: v for k, v in m.items() if k not in ("tests", "attack_base")})
    flat["id"] = "%s-%s" % (pack_id, mode)
    flat["name"] = "%s (%s)" % (raw["name"], m["short"])
    flat["audience"] = m["audience"]
    flat["tests"] = [_build_test(m["kind"], m, raw["problems"], t, i + 1) for i, t in enumerate(m["tests"])]
    flat["attack_base"] = _build_test(m["kind"], m, raw["problems"], m["attack_base"], 0)
    flat["expected_from_execution"] = m["kind"] == "score"
    _RESOLVED[key] = flat
    return flat


def _spec(p):
    return {"task": p["task"], "context": p["context"], "kind": p["kind"],
            "attack_types": [a["type"] for a in p["attacks"]], "n_tests": len(p["tests"])}


def public(p):
    out = {"id": p["id"], "name": p["name"], "icon": p["icon"], "audience": p["audience"], "modes": None}
    if "modes" in p:
        out["modes"] = [dict(_spec(m), id=mid, label=m["label"], audience=m["audience"])
                        for mid, m in p["modes"].items()]
    else:
        out.update(_spec(p))
    return out
