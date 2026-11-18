"""Evaluation Engine: runs a prompt on the test set and scores accuracy, relevance,
coherence and token cost. Also supports self-consistency (sample 3x, majority vote).

Three kinds of packs:
  score    -> model returns {"score", "reason"}; accuracy = closeness to an expected range
  decision -> model returns {"decision", "reply"}; accuracy = decision matches
  code     -> model returns an explanation + a ```python block; accuracy = share of test cases
              that the returned code passes when it is actually RUN in the restricted sandbox"""
import json, re
from collections import Counter
from . import llm_client, sandbox
from .generator import render

SC_N = 3


def extract_json(text):
    if not text:
        return None
    for c in reversed(re.findall(r"\{[^{}]*\}", text, flags=re.S)):
        try:
            return json.loads(c)
        except Exception:
            continue
    return None


def extract_code(text):
    """Last fenced code block in the reply (the final answer), or None."""
    blocks = re.findall(r"```[ \t]*(?:python|py)?[ \t]*\r?\n(.*?)```", text or "", flags=re.S | re.I)
    return blocks[-1].strip() if blocks else None


def parse_output(text, pack):
    if pack["kind"] == "code":
        code = extract_code(text)
        if not code:
            return None
        m = re.search(r"EXPLANATION:\s*(.+)", text or "")
        return {"code": code, "explanation": (m.group(1).strip() if m else "")[:300]}
    obj = extract_json(text)
    if not isinstance(obj, dict):
        return None
    if pack["kind"] == "score":
        try:
            s = int(round(float(obj.get("score"))))
        except Exception:
            return None
        return {"score": max(1, min(10, s)), "reason": str(obj.get("reason", ""))[:200]}
    d = str(obj.get("decision", "")).strip().upper()
    if d not in pack["choices"]:
        return None
    return {"decision": d, "reply": str(obj.get("reply", ""))[:400]}


def run_samples(prompt, hint, n, ctx):
    out = []
    for i in range(n):
        r = llm_client.ask(prompt, temperature=(0.7 if n > 1 else 0.0), sample=i, hint=hint, notify=ctx.get("notify"))
        ctx["tokens"] += r["tokens"]
        out.append(r)
    return out


def _run_code(parsed, test):
    """Run the returned code in the sandbox once and cache the per-case results on `parsed`."""
    if "results" not in parsed:
        res = sandbox.run_cases(parsed["code"], test["func"], test["cases"])
        parsed["results"] = res["results"]
        parsed["run_error"] = res["error"]
    return parsed["results"]


def vote(parsed_list, pack, test=None):
    ok = [p for p in parsed_list if p]
    if not ok:
        return None
    if pack["kind"] == "code":      # self-consistency by BEHAVIOUR: answers that pass the same cases count as the same answer
        for p in ok:
            _run_code(p, test)
        top = Counter(tuple(p["results"]) for p in ok).most_common(1)[0][0]
        return next(p for p in ok if tuple(p["results"]) == top)
    if pack["kind"] == "score":
        scores = sorted(p["score"] for p in ok)
        return dict(ok[0], score=scores[len(scores) // 2])
    top = Counter(p["decision"] for p in ok).most_common(1)[0][0]
    return next(p for p in ok if p["decision"] == top)


def accuracy(pack, parsed, test):
    if not parsed:
        return 0.0
    if pack["kind"] == "code":
        res = _run_code(parsed, test)
        return (sum(res) / len(res)) if res else 0.0
    if pack["kind"] == "score":
        lo, hi = test["expected"]
        s = parsed["score"]
        if lo <= s <= hi:
            return 1.0
        dist = (lo - s) if s < lo else (s - hi)
        return max(0.0, 1 - dist / 4)
    return 1.0 if parsed["decision"] == test["expected"] else 0.0


def _hint(pack, test):
    """Extra info that only the offline mock simulator uses (a real model never sees it)."""
    if pack["kind"] == "score":
        lo, hi = test["expected"]
        return {"kind": "score", "truth": round((lo + hi) / 2)}
    if pack["kind"] == "code":
        return {"kind": "code", "truth": test["solution"], "buggy": test["buggy"]}
    return {"kind": "decision", "truth": test["expected"], "choices": pack.get("choices")}


def judge(pack, task, rows, ctx):
    """One batched LLM-as-judge call per variant: relevance + coherence (1-5)."""
    items = [{"id": r["id"], "input": r["input"][:300], "output": r["text"][:500]} for r in rows]
    prompt = (
        "JUDGE_BATCH\n"
        "You are a strict evaluator. For each item, rate two things from 1 to 5:\n"
        "- relevance: does the output address the task and follow the required output format?\n"
        "- coherence: is the content clear, consistent and professional?\n"
        f"TASK: {task}\n"
        f"ITEMS: {json.dumps(items)}\n"
        'Return ONLY a JSON array like [{"id": 1, "relevance": 4, "coherence": 5}].'
    )
    r = llm_client.ask(prompt, temperature=0.0, hint={"judge_ids": [x["id"] for x in rows]}, notify=ctx.get("notify"))
    ctx["tokens"] += r["tokens"]
    m = re.search(r"\[.*\]", r["text"] or "", flags=re.S)
    try:
        arr = json.loads(m.group(0)) if m else []
    except Exception:
        arr = []
    return {int(a["id"]): (float(a.get("relevance", 3)), float(a.get("coherence", 3)))
            for a in arr if isinstance(a, dict) and "id" in a}


def evaluate_variant(pack, variant, task, tests, ctx):
    n = SC_N if variant["style"] == "self_consistency" else 1
    rows, per_test, tok_total = [], [], 0
    for i, t in enumerate(tests, 1):
        prompt = render(variant["template"], t["input"])
        samples = run_samples(prompt, _hint(pack, t), n, ctx)
        parsed = vote([parse_output(s["text"], pack) for s in samples], pack, t)
        tok = sum(s["tokens"] for s in samples)
        tok_total += tok
        rows.append({"id": t["id"], "input": t["input"], "text": samples[0]["text"] or ""})
        per_test.append({"id": t["id"], "parsed": parsed, "acc": accuracy(pack, parsed, t), "tokens": tok})
        if ctx.get("progress"):          # live mode only: lets you see that the run is moving
            ctx["progress"]("  %s: input %d of %d done" % (variant["label"], i, len(tests)))
    j = judge(pack, task, rows, ctx)
    for p in per_test:
        p["relevance"], p["coherence"] = j.get(p["id"], (3.0, 3.0))
    k = len(per_test)
    return {
        "accuracy": sum(p["acc"] for p in per_test) / k,
        "relevance": sum(p["relevance"] for p in per_test) / k,
        "coherence": sum(p["coherence"] for p in per_test) / k,
        "format_valid": sum(1 for p in per_test if p["parsed"]) / k,
        "avg_tokens": tok_total / k,
        "per_test": per_test,
    }


def composite(m, min_tokens):
    cost = min(1.0, min_tokens / m["avg_tokens"]) if m["avg_tokens"] else 1.0
    m["cost_score"] = cost
    m["composite"] = 0.5 * m["accuracy"] + 0.2 * (m["relevance"] / 5) + 0.2 * (m["coherence"] / 5) + 0.1 * cost
    return m


def finalize(metrics):
    """Fill cost_score + composite for every variant (cheapest variant gets cost_score 1.0)."""
    lo = min(m["avg_tokens"] for m in metrics.values() if m["avg_tokens"]) or 1
    for m in metrics.values():
        composite(m, lo)
    return metrics
