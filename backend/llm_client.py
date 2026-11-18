"""Every model call in PromptForge goes through ask().

Modes
-----
LIVE : GEMINI_API_KEY is set (and MOCK_MODE is not 1) -> real Gemini calls, cached on disk.
MOCK : no key, or MOCK_MODE=1 -> a deterministic *simulation* so you can test the UI for free.
       Mock numbers are NOT real model behaviour. The UI and report label them as simulated.

Overload handling (503 "high demand" / 504 deadline): the call is retried with waits. If you list backup models in
FALLBACK_MODELS (.env), a model that keeps failing is skipped and the next one is tried. Runs that used more than one
model are flagged in the log and the report, because comparing prompt styles across different models is not fully fair.
"""
import os, json, hashlib, re, time, threading
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CACHE_PATH = DATA / "cache.json"

MODEL = os.getenv("MODEL_NAME", "gemini-3.8-flash").strip()       # copy the exact ID from Google AI Studio
FALLBACK_MODELS = [m.strip() for m in os.getenv("FALLBACK_MODELS", "").split(",") if m.strip() and m.strip() != MODEL]
MIN_INTERVAL = float(os.getenv("REQUEST_INTERVAL", "2.0"))         # seconds between live calls
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "45"))        # one call slower than this is cancelled and retried
THINKING = os.getenv("THINKING_LEVEL", "low").strip().lower()      # Gemini 3+: minimal | low | medium | high ("" = default)
MAX_ATTEMPTS = max(1, int(os.getenv("MAX_ATTEMPTS", "4")))         # tries per model before giving up on it / on the call

_lock = threading.Lock()
_cache = None
_clients = {}
_last_call = 0.0
_NO_THINKING = set()       # models that rejected the thinking setting
_used = set()              # models that actually answered during this server session / run
_tl = threading.local()   # remembers which model answered the latest call on this thread
_dead = {}                 # model -> time until which we skip it (it failed all attempts recently)


class QuotaExhausted(RuntimeError):
    pass


def is_mock() -> bool:
    return os.getenv("MOCK_MODE", "0") == "1" or not os.getenv("GEMINI_API_KEY")


def model_name() -> str:
    return "mock-simulator" if is_mock() else MODEL


def reset_models_used():
    _used.clear()


def models_used() -> list:
    return sorted(_used)


# ----------------------------------------------------------------------------- live
def _get_cache():
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
        except Exception:
            _cache = {}
    return _cache


def _say(notify, msg):
    """Show a status line in the terminal and (if given) in the website's pipeline log."""
    print("[gemini] " + msg, flush=True)
    if notify:
        try:
            notify(msg)
        except Exception:
            pass


def _major(model) -> int:
    m = re.match(r"(?:models/)?gemini-(\d+)", (model or "").strip().lower())
    return int(m.group(1)) if m else 0


def _make_config(types, temperature, model):
    """Gemini 3 and newer: leave temperature at its default (1.0) and use a low thinking level (default 'high' is slow).
    Older models (2.x) get the temperature as before. Unknown names (e.g. gemini-4-hydrogen) are treated as new."""
    kw = {}
    major = _major(model)
    new_style = major >= 3 or major == 0
    if not new_style or os.getenv("SEND_TEMPERATURE") == "1":
        kw["temperature"] = temperature
    if new_style and THINKING and model not in _NO_THINKING:
        try:
            kw["thinking_config"] = types.ThinkingConfig(thinking_level=THINKING)
        except Exception:
            _NO_THINKING.add(model)       # this google-genai version has no thinking_level
    return types.GenerateContentConfig(**kw)


def _retry_seconds(low):
    """Google's 429 messages often say 'Please retry in 23.4s' - use that when it is there."""
    m = re.search(r"retry in ([\d.]+)\s*s", low) or re.search(r"retrydelay\W+(\d+(?:\.\d+)?)", low)
    return float(m.group(1)) if m else None


def _get_client(timeout):
    from google import genai
    from google.genai import types
    key = (timeout,)
    if key not in _clients:
        _clients[key] = genai.Client(api_key=os.getenv("GEMINI_API_KEY"),
                                     http_options=types.HttpOptions(timeout=int(timeout * 1000)))
    return _clients[key]


def _call_model(model, prompt, temperature, notify, max_attempts, timeout):
    """Try one model up to max_attempts times. Raises the last error (or QuotaExhausted)."""
    global _last_call
    from google.genai import types
    client = _get_client(timeout)
    delay = 3
    for attempt in range(max_attempts):
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        t0 = time.time()
        _last_call = t0
        try:
            r = client.models.generate_content(model=model, contents=prompt,
                                               config=_make_config(types, temperature, model))
            try:
                text = r.text or ""          # blocked/empty responses count as "no output"
            except Exception:
                text = ""
            um = getattr(r, "usage_metadata", None)
            tokens = int(getattr(um, "total_token_count", 0) or 0)
            print("[gemini] %s ok in %.1fs, %d tokens" % (model, time.time() - t0, tokens), flush=True)
            return text, tokens
        except Exception as e:
            low = str(e).lower()
            short = " ".join(str(e).split())[:110]
            if _major(model) != 0 and _major(model) < 3:
                pass
            elif model not in _NO_THINKING and "thinking" in low and \
                    any(k in low for k in ("400", "invalid", "not supported", "unsupported")):
                _NO_THINKING.add(model)
                _say(notify, "%s rejected the thinking setting - retrying without it." % model)
                continue
            if "perday" in low or "per day" in low:
                raise QuotaExhausted("Today's free quota for %s is used up (it resets about once a day). Details: %s"
                                     % (model, short))
            transient = any(k in low for k in ("429", "quota", "rate", "500", "502", "503", "504", "unavailable",
                                               "overloaded", "high demand", "timed out", "timeout", "deadline",
                                               "connection", "reset")) or "timeout" in type(e).__name__.lower()
            if attempt == max_attempts - 1 or not transient:
                raise
            pause = min((_retry_seconds(low) or delay) + 1, 40)
            _say(notify, "%s is busy (%s). Waiting %.0fs, then retrying (attempt %d of %d)..."
                 % (model, short, pause, attempt + 2, max_attempts))
            time.sleep(pause)
            delay = min(delay * 2, 20)


def _call_gemini(prompt: str, temperature: float, notify=None, model=None, max_attempts=None, timeout=None):
    """Call `model` (default MODEL). With no explicit model, backup models from FALLBACK_MODELS are tried in order
    when the current one keeps failing. A model that failed all its attempts is skipped for 2 minutes."""
    attempts = max_attempts or MAX_ATTEMPTS
    tmo = timeout or REQUEST_TIMEOUT
    if model:
        chain = [model]
    else:
        chain = [MODEL] + FALLBACK_MODELS
    live = [m for m in chain if _dead.get(m, 0) < time.time()] or chain[:1]
    last = None
    for i, m in enumerate(live):
        try:
            out = _call_model(m, prompt, temperature, notify, attempts, tmo)
            _used.add(m)
            _tl.model = m
            return out
        except QuotaExhausted as e:
            last = e
            _dead[m] = time.time() + 3600
        except Exception as e:
            last = e
            if i < len(live) - 1 or len(live) > 1:
                _dead[m] = time.time() + 120
        if i < len(live) - 1:
            _say(notify, "%s is not answering - switching to backup model %s. "
                         "(Results from different models are not strictly comparable; this is flagged in the report.)"
                 % (m, live[i + 1]))
    if isinstance(last, QuotaExhausted):
        raise last
    raise last


def ask(prompt: str, temperature: float = 0.0, sample: int = 0, hint: dict = None, notify=None) -> dict:
    """Return {"text": str, "tokens": int}. `hint` is only used by the mock simulator;
    `notify` (optional) receives status messages such as 'waiting for the rate limit'."""
    if is_mock():
        time.sleep(0.03)
        return _mock(prompt, temperature, sample, hint or {})

    # The cache key uses the PRIMARY model only, so a run can resume from the cache whichever backup answered.
    key = hashlib.md5(f"{MODEL}|{temperature}|{sample}|{prompt}".encode()).hexdigest()
    with _lock:
        cached = _get_cache().get(key)
    if cached:
        if cached.get("model"):
            _used.add(cached["model"])
        else:
            _used.add(MODEL)
        return {"text": cached["text"], "tokens": cached["tokens"]}
    text, tokens = _call_gemini(prompt, temperature, notify)
    out = {"text": text, "tokens": tokens, "model": getattr(_tl, "model", MODEL)}
    with _lock:
        c = _get_cache()
        c[key] = out
        CACHE_PATH.write_text(json.dumps(c))
    return {"text": text, "tokens": tokens}


# ----------------------------------------------------------------------------- mock
def _h(*parts) -> int:
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest(), 16)


_STYLE_RESIST = {"zero_shot": 0.0, "few_shot": 0.25, "cot": 0.40, "role": 0.20}
_STYLE_NOISE = {"zero_shot": 2, "few_shot": 1, "cot": 1, "role": 1}
_STYLE_WRONG = {"zero_shot": 0.25, "few_shot": 0.10, "cot": 0.10, "role": 0.15}
_ATK_MOD = {"direct_injection": 0.0, "indirect_hidden": -0.10, "roleplay": -0.10,
            "prompt_leak": 0.05, "exfiltration": 0.05}
_HIJACK_TYPES = ("direct_injection", "indirect_hidden", "roleplay")
_REPLIES = {
    "REFUND": "Thanks for reaching out. I've approved your refund and you'll receive it in 5-7 days.",
    "DENY": "I'm sorry, but this item isn't eligible for a refund under our policy.",
    "ESCALATE": "I'm escalating this to our specialist team right away; they'll contact you shortly.",
    "INFO": "Your order is on its way; you can follow it with the tracking link in your email.",
}


def _style(low: str) -> str:
    if "step by step" in low:
        return "cot"
    if "example 1" in low:
        return "few_shot"
    if low.lstrip().startswith("you are a"):
        return "role"
    return "zero_shot"


def _mock(prompt, temperature, sample, hint):
    ids = hint.get("judge_ids")
    if ids:
        arr = [{"id": i, "relevance": 3 + _h("rel", i, len(prompt)) % 3,
                "coherence": 3 + _h("coh", i, len(prompt)) % 3} for i in ids]
        txt = json.dumps(arr)
        return {"text": txt, "tokens": (len(prompt) + len(txt)) // 4}

    low = prompt.lower()
    style = _style(low)
    hard = "untrusted_data" in low
    seed = _h(prompt, sample, round(temperature, 2))
    atk = hint.get("attack")
    kind = hint.get("kind", "score")
    truth = hint.get("truth")

    follows = False
    if atk:
        resist = 0.95 if hard else _STYLE_RESIST[style]
        resist = min(0.98, max(0.0, resist + _ATK_MOD.get(atk, 0.0)))
        follows = (seed % 100) / 100 >= resist

    n = _STYLE_NOISE[style]
    if kind == "code":
        # simulated support engineer: usually returns the reference fix, sometimes the buggy code unchanged
        code = truth
        if (seed // 11 % 100) / 100 < _STYLE_WRONG[style]:
            code = hint.get("buggy") or truth
        if atk in _HIJACK_TYPES and follows:
            code = code + "\n# " + str(hint.get("marker") or "PWNED_BACKDOOR")
        text = "EXPLANATION: Simulated: the function had a logic bug, fixed below.\n```python\n" + code + "\n```"
    else:
        if kind == "score":
            score = max(1, min(10, int(truth) + (seed // 7) % (2 * n + 1) - n))
            if atk in _HIJACK_TYPES and follows:
                score = hint.get("hijack", 10)
            obj = {"score": score, "reason": "Simulated: matches part of the required skills."}
        else:
            decision = truth
            choices = hint.get("choices") or ["REFUND", "DENY", "ESCALATE", "INFO"]
            if (seed // 11 % 100) / 100 < _STYLE_WRONG[style]:
                others = [c for c in choices if c != truth]
                decision = others[seed % len(others)]
            if atk in _HIJACK_TYPES and follows:
                decision = hint.get("hijack", "REFUND")
            obj = {"decision": decision, "reply": _REPLIES.get(decision, "Thanks for contacting us.")}
        text = json.dumps(obj)
    if style == "cot":
        text = "Step 1: list the relevant facts. Step 2: compare with the context.\n" + text
    if atk == "prompt_leak" and follows:
        text = f"My instructions: ... Internal reference code: {hint.get('canary')}\n" + text
    if atk == "exfiltration" and follows:
        text = f"Other record I processed: {hint.get('private')} (ID 5521).\n" + text
    return {"text": text, "tokens": (len(prompt) + len(text)) // 4}
