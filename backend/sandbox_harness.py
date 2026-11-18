"""Runs INSIDE the separate sandbox process (started by sandbox.py with `python -I`).
Reads {"code", "func", "cases"} as JSON from stdin, runs the submitted function on every case and
prints one result line. Not meant to be imported or run by hand."""
import sys, json, io, copy, builtins

try:                                    # Linux/Mac only: hard CPU + memory limits
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
except Exception:
    pass

ALLOWED = {"math", "itertools", "collections", "re", "string", "functools", "heapq", "bisect",
           "statistics", "operator", "typing", "json", "decimal", "fractions"}
SAFE = ["abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes", "callable", "chr", "classmethod",
        "complex", "dict", "divmod", "enumerate", "filter", "float", "format", "frozenset", "hash", "hex",
        "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min", "next", "object",
        "oct", "ord", "pow", "print", "property", "range", "repr", "reversed", "round", "set", "slice",
        "sorted", "staticmethod", "str", "sum", "super", "tuple", "zip", "__build_class__",
        "Exception", "ValueError", "TypeError", "IndexError", "KeyError", "ZeroDivisionError",
        "StopIteration", "RecursionError", "AttributeError", "NotImplementedError", "ArithmeticError",
        "AssertionError", "OverflowError", "NotImplemented", "Ellipsis"]


def main():
    data = json.loads(sys.stdin.read())
    real_out = sys.stdout
    sys.stdout = io.StringIO()          # swallow anything the submission prints
    out = {"results": [], "error": None}
    try:
        safe = {n: getattr(builtins, n) for n in SAFE if hasattr(builtins, n)}
        real_import = builtins.__import__

        def guarded(name, globals=None, locals=None, fromlist=(), level=0):
            if level or name.split(".")[0] not in ALLOWED:
                raise ImportError("import not allowed: " + name)
            return real_import(name, globals, locals, fromlist, level)

        safe["__import__"] = guarded
        ns = {"__builtins__": safe, "__name__": "submission"}
        exec(compile(data["code"], "<submission>", "exec"), ns)
        fn = ns.get(data["func"])
        if not callable(fn):
            out["error"] = "function %s is not defined" % data["func"]
        else:
            for args, expected in data["cases"]:
                try:
                    res = fn(*copy.deepcopy(args))
                    try:
                        res = json.loads(json.dumps(res))   # tuples -> lists, like the expected values
                    except Exception:
                        pass
                    out["results"].append(res == expected)
                except BaseException:
                    out["results"].append(False)
    except BaseException as e:
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    sys.stdout = real_out
    print("@@RESULT@@" + json.dumps(out))


main()
