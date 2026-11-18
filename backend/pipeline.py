"""Orchestrator: the 6-stage prompt lifecycle (design -> test -> attack -> harden -> re-attack -> re-test)."""
import datetime
from . import llm_client, generator, evaluator, redteam

STAGES = ["Generate variants", "Evaluate", "Red-team", "Harden", "Re-attack", "Re-evaluate"]


def accuracy_basis(pack):
    if pack["kind"] == "code":
        return "Accuracy = share of test cases that the returned code passes. The code is actually run in a restricted sandbox."
    if pack["kind"] == "score":
        if pack.get("expected_from_execution"):
            return ("Accuracy = how close the score is to the expected range. The expected range comes from actually running "
                    "each submission against its test cases.")
        return "Accuracy = how close the score is to the expected range for each test input."
    return "Accuracy = the chosen decision matches the expected decision for each test input."


def _asr(attacks, phase, variant):
    rows = [a for a in attacks if a["phase"] == phase and a["variant"] == variant]
    return (sum(1 for a in rows if a["success"]) / len(rows)) if rows else 0.0


def _live_notice(variants, n_tests, n_attacks):
    """Rough number of model calls in a live run, so a slow first run does not look like a hang."""
    def samples(v):
        return 3 if v["style"] == "self_consistency" else 1
    calls = sum(n_tests * samples(v) + 1 + n_attacks * samples(v) for v in variants)
    calls += n_attacks + n_tests + 1              # hardened prompt: re-attack + re-evaluate (about)
    return ("Live mode: about %d Gemini calls (answers already in the cache are free). Each call takes a few seconds "
            "and the free tier can add waits, so the first run may take several minutes." % calls)


def run(run_id, pack, opts, emit):
    ctx = {"tokens": 0, "notify": lambda m: emit("log", msg=m)}
    task = (opts.get("task") or "").strip() or pack["task"]
    # The context box in the Studio is pre-filled with the domain's default; the user may edit it.
    custom = (opts.get("context") or "").strip()
    context_edited = bool(custom) and custom != pack["context"].strip()
    if context_edited:
        pack = {**pack, "context": custom}
    n_tests = max(1, min(int(opts.get("n_tests") or len(pack["tests"])), len(pack["tests"])))
    tests = pack["tests"][:n_tests]
    mock = llm_client.is_mock()
    llm_client.reset_models_used()
    basis = accuracy_basis(pack)
    emit("start", mock=mock, model=llm_client.model_name(), pack=pack["name"], n_tests=n_tests,
         kind=pack["kind"], context_edited=context_edited, accuracy_basis=basis)

    # 1. generate variants -------------------------------------------------------------
    emit("stage", n=1, label=STAGES[0])
    variants = generator.build_variants(pack, task, bool(opts.get("self_consistency")))
    emit("log", msg=f"Built {len(variants)} prompt variants from one template (context + constraints + output format).")
    emit("log", msg="Context: " + ("EDITED by the user." if context_edited else "default for this domain."))
    emit("variants", variants=variants)
    if not mock:
        emit("log", msg=_live_notice(variants, n_tests, len(pack["attacks"])))
        ctx["progress"] = lambda m: emit("log", msg=m)

    # 2. evaluate -----------------------------------------------------------------------
    emit("stage", n=2, label=STAGES[1])
    metrics = {}
    for v in variants:
        emit("log", msg=f"Evaluating {v['label']} on {n_tests} test inputs...")
        metrics[v["id"]] = evaluator.evaluate_variant(pack, v, task, tests, ctx)
        emit("eval", variant=v["id"], metrics=metrics[v["id"]])
    evaluator.finalize(metrics)
    winner_id = max(metrics, key=lambda k: metrics[k]["composite"])
    winner = next(v for v in variants if v["id"] == winner_id)
    emit("leaderboard", metrics=metrics, winner=winner_id)
    emit("log", msg=f"Winner on quality: {winner['label']} (composite {metrics[winner_id]['composite']:.2f}).")

    # 3. red-team every variant ----------------------------------------------------------
    emit("stage", n=3, label=STAGES[2])
    attacks = []
    for v in variants:
        emit("log", msg=f"Attacking {v['label']} with {len(pack['attacks'])} attack types...")
        for atk in pack["attacks"]:
            rec = redteam.attack(pack, v["template"], v["style"], atk, ctx)
            rec.update(phase="before", variant=v["id"])
            attacks.append(rec)
            emit("attack", **rec)
    asr_before = _asr(attacks, "before", winner_id)
    emit("log", msg=f"Winner's attack success rate BEFORE hardening: {asr_before:.0%}.")

    # 4. harden the winner ---------------------------------------------------------------
    emit("stage", n=4, label=STAGES[3])
    hard_template = generator.harden(pack, winner, task)
    diff = generator.diff_lines(winner["template"], hard_template)
    emit("hardened", template=hard_template, diff=diff, defenses=generator.DEFENSES)
    emit("log", msg="Added 4 defense layers: delimiters, instruction hierarchy, confidentiality, self-check.")

    # 5. re-attack -----------------------------------------------------------------------
    emit("stage", n=5, label=STAGES[4])
    for atk in pack["attacks"]:
        rec = redteam.attack(pack, hard_template, winner["style"], atk, ctx)
        rec.update(phase="after", variant="hardened")
        attacks.append(rec)
        emit("attack", **rec)
    asr_after = _asr(attacks, "after", "hardened")
    emit("log", msg=f"Attack success rate AFTER hardening: {asr_after:.0%}.")

    # 6. re-evaluate the hardened prompt (defenses can hurt quality) ------------------------
    emit("stage", n=6, label=STAGES[5])
    hv = {"id": "hardened", "style": winner["style"], "label": "Hardened " + winner["label"], "template": hard_template}
    hm = evaluator.evaluate_variant(pack, hv, task, tests, ctx)
    evaluator.composite(hm, min(metrics[winner_id]["avg_tokens"], hm["avg_tokens"]))
    emit("reeval", metrics=hm)

    used = [] if mock else llm_client.models_used()
    if len(used) > 1:
        emit("log", msg="NOTE: this run used more than one model (%s) because of overload; compare styles with care." % ", ".join(used))
    result = {
        "run_id": run_id, "domain": pack["id"], "pack_name": pack["name"], "task": task,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "mock": mock, "model": llm_client.model_name(), "models_used": ([] if mock else llm_client.models_used()),
        "n_tests": n_tests,
        "kind": pack["kind"], "accuracy_basis": basis,
        "context": pack["context"], "context_edited": context_edited,
        "variants": variants, "metrics": metrics, "winner": winner_id, "attacks": attacks,
        "attack_types": [a["type"] for a in pack["attacks"]],
        "asr_before": asr_before, "asr_after": asr_after,
        "hardened": {"template": hard_template, "diff": diff, "defenses": generator.DEFENSES, "metrics": hm},
        "tokens_total": ctx["tokens"],
    }
    return result
