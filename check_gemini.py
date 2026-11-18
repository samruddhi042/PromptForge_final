"""Test your Gemini key and models, outside the website (venv active, project folder).

  python check_gemini.py            one quick test of MODEL_NAME (1 attempt, fails fast)
  python check_gemini.py --models   lists the models your key can use and tests each in turn,
                                    so you can pick one that is WORKING right now for MODEL_NAME / FALLBACK_MODELS
"""
import sys, time

from backend import llm_client


def test(model):
    t = time.time()
    try:
        text, tokens = llm_client._call_gemini("Reply with exactly the word: OK", 0.0, model=model,
                                               max_attempts=1, timeout=25)
        return True, "%.1fs  answered %r" % (time.time() - t, text.strip()[:30])
    except Exception as e:
        return False, "%.1fs  %s" % (time.time() - t, " ".join(str(e).split())[:90])


def list_models():
    from google import genai
    client = genai.Client(api_key=llm_client.os.getenv("GEMINI_API_KEY"))
    names = []
    for m in client.models.list():
        acts = getattr(m, "supported_actions", None) or []
        n = (m.name or "").replace("models/", "")
        if "generateContent" in acts and n.startswith("gemini") and "embed" not in n and "image" not in n \
                and "tts" not in n and "live" not in n:
            names.append(n)
    return sorted(set(names))


def main():
    if llm_client.is_mock():
        print("MOCK mode: GEMINI_API_KEY is empty in your .env file (or MOCK_MODE=1). Put your key in .env and retry.")
        return
    if "--models" in sys.argv:
        print("Asking Google which models your key can use...")
        try:
            names = list_models()
        except Exception as e:
            print("Could not list models: %s: %s\n(Wrong key? No internet?)" % (type(e).__name__, e))
            return
        print("%d models found. Testing each with one tiny request (25s limit each)...\n" % len(names))
        good = []
        for n in names:
            ok, info = test(n)
            print("  %-4s %-40s %s" % ("OK" if ok else "BUSY", n, info))
            if ok:
                good.append(n)
        print()
        if good:
            print("Working right now. Put the first in .env as MODEL_NAME and the next one or two as backups:")
            print("  MODEL_NAME=%s" % good[0])
            if len(good) > 1:
                print("  FALLBACK_MODELS=%s" % ",".join(good[1:3]))
        else:
            print("Nothing answered. Google is overloaded or your key/network is blocked: retry in a few minutes.")
        return
    print("Model:", llm_client.MODEL, "| backups:", llm_client.FALLBACK_MODELS or "none",
          "| thinking:", llm_client.THINKING or "default")
    ok, info = test(llm_client.MODEL)
    if ok:
        print("WORKED:", info, "\nYour key and model are fine.")
    else:
        print("FAILED:", info)
        print("503/504 = Google is overloaded for this model right now (not your code or key). "
              "Run: python check_gemini.py --models   to find one that works.")


if __name__ == "__main__":
    main()
