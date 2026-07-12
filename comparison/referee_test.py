"""Neutral referee: score any app.py (Flask) from demo/requirements_toolbox.md.

Usage: python referee_test.py <dir containing app.py>

Loads <dir>/app.py, finds its Flask instance, and exercises every endpoint plus
each edge case the spec states EXPLICITLY, via Flask's test client. Prints one
PASS/FAIL line per check and a final SCORE n/total. This file is written only
from the requirements spec — it is identical for both contestants, so the
comparison is judged by the same yardstick, not by either side's own tests.
"""

import importlib.util
import json
import sys
from pathlib import Path


def load_flask_app(app_dir: Path):
    app_py = app_dir / "app.py"
    if not app_py.is_file():
        # some agents name it differently; take the first *.py that defines a Flask app
        cands = [p for p in app_dir.rglob("*.py") if "test" not in p.name.lower()]
        for p in cands:
            if "Flask(" in p.read_text(encoding="utf-8", errors="replace"):
                app_py = p
                break
    if not app_py.is_file():
        raise FileNotFoundError(f"no app.py under {app_dir}")
    sys.path.insert(0, str(app_py.parent))
    spec = importlib.util.spec_from_file_location("contestant_app", app_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ("app", "application"):
        obj = getattr(mod, name, None)
        if obj is not None and hasattr(obj, "test_client"):
            return obj
    # last resort: any attribute that looks like a Flask app
    for v in vars(mod).values():
        if hasattr(v, "test_client"):
            return v
    raise RuntimeError("no Flask app instance found in the module")


def run(app_dir: str):
    app_dir = Path(app_dir).resolve()
    results = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001 — a crash on a check is just a fail
            ok, detail = False, f"exception: {e.__class__.__name__}: {e}"
        results.append((name, ok, detail))

    try:
        flask_app = load_flask_app(app_dir)
    except Exception as e:  # noqa: BLE001
        print(f"COULD NOT LOAD APP: {e}")
        print("SCORE 0/11")
        return 0, 11
    c = flask_app.test_client()

    def j(path):
        r = c.get(path)
        try:
            return r.status_code, r.get_json(silent=True)
        except Exception:
            return r.status_code, None

    # 1. wordcount basic
    def _wc():
        s, d = j("/wordcount?text=hello world")
        return s == 200 and d and d.get("words") == 2 and d.get("lines") == 1, str(d)
    check("wordcount basic (words=2, lines=1)", _wc)

    # 2. wordcount EMPTY edge — must be all zeros, not an error
    def _wce():
        s, d = j("/wordcount?text=")
        return s == 200 and d and d.get("words") == 0 and d.get("chars") == 0 and d.get("lines") == 0, str(d)
    check("[EDGE] wordcount empty -> all zeros", _wce)

    # 3. palindrome true
    def _p1():
        s, d = j("/palindrome?text=racecar")
        return s == 200 and d and d.get("is_palindrome") is True, str(d)
    check("palindrome 'racecar' -> true", _p1)

    # 4. palindrome false
    def _p2():
        s, d = j("/palindrome?text=hello")
        return s == 200 and d and d.get("is_palindrome") is False, str(d)
    check("palindrome 'hello' -> false", _p2)

    # 5. palindrome PANAMA edge — ignore case/space/punctuation
    def _p3():
        s, d = j("/palindrome?text=A man, a plan, a canal: Panama")
        return s == 200 and d and d.get("is_palindrome") is True, str(d)
    check("[EDGE] palindrome Panama (ignore punctuation) -> true", _p3)

    # 6. slug basic
    def _s1():
        s, d = j("/slug?text=Hello World")
        return s == 200 and d and d.get("slug") == "hello-world", str(d)
    check("slug 'Hello World' -> hello-world", _s1)

    # 7. slug MESSY edge
    def _s2():
        s, d = j("/slug?text=  Hello,   World!!  ")
        return s == 200 and d and d.get("slug") == "hello-world", str(d)
    check("[EDGE] slug messy punctuation/spaces -> hello-world", _s2)

    # 8. caesar basic
    def _c1():
        s, d = j("/caesar?text=abc&shift=1")
        return s == 200 and d and d.get("result") == "bcd", str(d)
    check("caesar 'abc' shift 1 -> bcd", _c1)

    # 9. caesar WRAP edge
    def _c2():
        s, d = j("/caesar?text=XYZ&shift=3")
        return s == 200 and d and d.get("result") == "ABC", str(d)
    check("[EDGE] caesar 'XYZ' shift 3 -> ABC (wrap)", _c2)

    # 10. caesar NON-LETTERS edge — digits/spaces unchanged, case preserved
    def _c3():
        s, d = j("/caesar?text=Hi 9&shift=1")
        return s == 200 and d and d.get("result") == "Ij 9", str(d)
    check("[EDGE] caesar 'Hi 9' shift 1 -> 'Ij 9' (non-letters pass)", _c3)

    # 11. index page lists the tools
    def _idx():
        r = c.get("/")
        body = r.get_data(as_text=True).lower()
        hits = sum(t in body for t in ("wordcount", "palindrome", "slug", "caesar"))
        return r.status_code == 200 and hits >= 3, f"status={r.status_code}, tool-mentions={hits}"
    check("index page lists the tools", _idx)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n=== {app_dir.name} ===")
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"   -> got {detail}"))
    print(f"SCORE {passed}/{total}")
    return passed, total


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python referee_test.py <app_dir>")
        raise SystemExit(2)
    run(sys.argv[1])
