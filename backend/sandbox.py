"""Restricted runner for AI-generated Python.

This is DEFENCE IN DEPTH, not a perfect sandbox:
  1. Static check (AST): only a whitelist of imports; no eval/exec/open/getattr or dunder tricks.
  2. The code runs in a separate `python -I` process, in an empty temp folder, with a stripped
     environment (your API key is NOT passed to it) and a hard timeout.
  3. On Linux/Mac the child also gets CPU and memory limits (not available on Windows).
Do not expose this app on a public/shared server. It is meant for running on your own laptop.
"""
import ast, json, os, subprocess, sys, tempfile
from pathlib import Path

HARNESS = Path(__file__).resolve().parent / "sandbox_harness.py"

ALLOWED_IMPORTS = {"math", "itertools", "collections", "re", "string", "functools", "heapq", "bisect",
                   "statistics", "operator", "typing", "json", "decimal", "fractions"}
BANNED_NAMES = {"eval", "exec", "compile", "open", "__import__", "input", "globals", "locals", "vars",
                "getattr", "setattr", "delattr", "breakpoint", "exit", "quit", "help", "memoryview",
                "dir", "type"}
SAFE_DUNDER_ATTRS = {"__init__", "__len__", "__iter__", "__next__", "__eq__", "__lt__", "__le__", "__gt__",
                     "__ge__", "__hash__", "__repr__", "__str__", "__contains__", "__getitem__",
                     "__setitem__", "__add__", "__mul__"}
ENV_KEYS = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP")


def check_code(code: str):
    """Return None if the code may run, otherwise a short reason."""
    if not isinstance(code, str) or not code.strip():
        return "empty code"
    if len(code) > 20000:
        return "code too long"
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return "syntax error: %s (line %s)" % (e.msg, e.lineno)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    return "import not allowed: " + a.name
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                return "import not allowed: " + str(node.module)
        elif isinstance(node, ast.Name):
            if node.id in BANNED_NAMES:
                return "name not allowed: " + node.id
            if node.id.startswith("__") and node.id.endswith("__") and node.id != "__name__":
                return "name not allowed: " + node.id
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr not in SAFE_DUNDER_ATTRS:
                return "attribute not allowed: " + node.attr
    return None


def _env():
    env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _fail(n, msg):
    return {"passed": 0, "total": n, "results": [False] * n, "error": msg}


def run_cases(code: str, func: str, cases: list, timeout: float = 4.0) -> dict:
    """Run `func` from `code` on cases [[args_list, expected], ...].
    Returns {"passed", "total", "results": [bool], "error": str|None}."""
    n = len(cases)
    bad = check_code(code)
    if bad:
        return _fail(n, bad)
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cp = subprocess.run([sys.executable, "-I", str(HARNESS)],
                                input=json.dumps({"code": code, "func": func, "cases": cases}),
                                capture_output=True, text=True, timeout=timeout, cwd=tmp, env=_env(), **kw)
    except subprocess.TimeoutExpired:
        return _fail(n, "timeout (possible infinite loop)")
    except Exception as e:
        return _fail(n, "sandbox_unavailable: %s: %s" % (type(e).__name__, e))
    line = next((l for l in reversed((cp.stdout or "").splitlines()) if l.startswith("@@RESULT@@")), None)
    if not line:
        return _fail(n, "no result (crashed or killed): " + (cp.stderr or "")[-200:].strip())
    try:
        data = json.loads(line[len("@@RESULT@@"):])
    except Exception:
        return _fail(n, "unreadable result")
    results = [bool(x) for x in data.get("results", [])]
    results += [False] * (n - len(results))
    return {"passed": sum(results), "total": n, "results": results, "error": data.get("error")}


_SELF_TEST = None


def self_test() -> bool:
    """True if the code runner works on this machine (checked once, then remembered)."""
    global _SELF_TEST
    if _SELF_TEST is None:
        r = run_cases("def f(x):\n    return x + 1", "f", [[[1], 2], [[5], 6]])
        _SELF_TEST = r["passed"] == 2
    return _SELF_TEST
