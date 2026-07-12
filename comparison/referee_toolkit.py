"""Neutral referee for demo/requirements_toolkit.md. Same yardstick for both
contestants. Usage: python referee_toolkit.py <dir containing app.py>

Scores the BACKEND correctness of all five tools + their edge cases (this is
exactly what each button calls, so backend-correct == button-works), AND
checks the FRONTEND actually wires an interactive button+fetch for each tool.
"""

import importlib.util
import sys
from pathlib import Path


def load_flask_app(app_dir: Path):
    app_py = app_dir / "app.py"
    if not app_py.is_file():
        for p in app_dir.rglob("*.py"):
            if "test" not in p.name.lower() and "Flask(" in p.read_text(encoding="utf-8", errors="replace"):
                app_py = p
                break
    if not app_py.is_file():
        raise FileNotFoundError(f"no app.py under {app_dir}")
    sys.path.insert(0, str(app_py.parent))
    spec = importlib.util.spec_from_file_location("contestant_toolkit", app_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ("app", "application"):
        obj = getattr(mod, name, None)
        if obj is not None and hasattr(obj, "test_client"):
            return obj
    for v in vars(mod).values():
        if hasattr(v, "test_client"):
            return v
    raise RuntimeError("no Flask app instance found")


def run(app_dir: str):
    app_dir = Path(app_dir).resolve()
    results = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{e.__class__.__name__}: {e}"
        results.append((name, bool(ok), detail))

    try:
        flask_app = load_flask_app(app_dir)
    except Exception as e:  # noqa: BLE001
        print(f"COULD NOT LOAD APP: {e}\nSCORE 0/17")
        return 0, 17
    c = flask_app.test_client()

    def j(path):
        r = c.get(path)
        return r.status_code, (r.get_json(silent=True) or {})

    # ---- base64 (3) ----
    check("base64 encode 'hi' works", lambda: (j("/base64?text=hi&mode=encode")[1].get("result") == "aGk=", j("/base64?text=hi&mode=encode")[1]))
    check("base64 round-trip encode->decode == original", lambda: (
        (lambda enc: j(f"/base64?text={enc}&mode=decode")[1].get("result") == "hello")(
            j("/base64?text=hello&mode=encode")[1].get("result", "")), "roundtrip"))
    check("[EDGE] base64 decode invalid -> 400 not 500", lambda: (j("/base64?text=@@@notb64@@@&mode=decode")[0] == 400, j("/base64?text=@@@notb64@@@&mode=decode")[0]))

    # ---- roman (5) ----
    check("roman 2024 -> MMXXIV", lambda: (j("/roman?n=2024")[1].get("roman") == "MMXXIV", j("/roman?n=2024")[1]))
    check("[EDGE] roman 4 -> IV (not IIII)", lambda: (j("/roman?n=4")[1].get("roman") == "IV", j("/roman?n=4")[1]))
    check("[EDGE] roman 9 -> IX", lambda: (j("/roman?n=9")[1].get("roman") == "IX", j("/roman?n=9")[1]))
    check("[EDGE] roman 40 -> XL", lambda: (j("/roman?n=40")[1].get("roman") == "XL", j("/roman?n=40")[1]))
    check("[EDGE] roman 4000 out-of-range -> 400", lambda: (j("/roman?n=4000")[0] == 400, j("/roman?n=4000")[0]))

    # ---- prime (3) ----
    check("[EDGE] prime 1 -> false", lambda: (j("/prime?n=1")[1].get("is_prime") is False, j("/prime?n=1")[1]))
    check("prime 2 -> true", lambda: (j("/prime?n=2")[1].get("is_prime") is True, j("/prime?n=2")[1]))
    check("prime 17 -> true, 18 -> false", lambda: (j("/prime?n=17")[1].get("is_prime") is True and j("/prime?n=18")[1].get("is_prime") is False, "17/18"))

    # ---- temp (2) ----
    check("temp 100C -> 212F", lambda: (j("/temp?value=100&from=C&to=F")[1].get("result") == 212.0, j("/temp?value=100&from=C&to=F")[1]))
    check("[EDGE] temp -40C -> -40F", lambda: (j("/temp?value=-40&from=C&to=F")[1].get("result") == -40.0, j("/temp?value=-40&from=C&to=F")[1]))

    # ---- anagram (2) ----
    check("[EDGE] anagram 'Listen'/'Silent ' -> true", lambda: (j("/anagram?a=Listen&b=Silent%20")[1].get("is_anagram") is True, j("/anagram?a=Listen&b=Silent%20")[1]))
    check("anagram 'abc'/'abd' -> false", lambda: (j("/anagram?a=abc&b=abd")[1].get("is_anagram") is False, j("/anagram?a=abc&b=abd")[1]))

    # ---- frontend interactivity (2) ----
    def _idx_buttons():
        r = c.get("/")
        body = r.get_data(as_text=True).lower()
        has_buttons = body.count("<button") >= 4 or body.count("onclick") >= 4
        has_fetch = "fetch(" in body or "xmlhttprequest" in body
        mentions = sum(t in body for t in ("base64", "roman", "prime", "temp", "anagram"))
        return r.status_code == 200 and has_buttons and has_fetch and mentions >= 4, f"buttons={body.count('<button')}, fetch={'fetch(' in body}, tools={mentions}"
    check("index has interactive buttons wired to fetch (>=4)", _idx_buttons)

    def _idx_inputs():
        body = c.get("/").get_data(as_text=True).lower()
        return body.count("<input") >= 4, f"inputs={body.count('<input')}"
    check("index has input fields for the tools (>=4)", _idx_inputs)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n=== {app_dir.name} ===")
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"   -> {detail}"))
    print(f"SCORE {passed}/{total}")
    return passed, total


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python referee_toolkit.py <app_dir>")
        raise SystemExit(2)
    run(sys.argv[1])
