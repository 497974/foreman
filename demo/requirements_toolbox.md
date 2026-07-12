# Text Toolbox — a small Flask API of 5 text tools

Build a single Flask app (`app.py`) exposing five text utilities. Each has a
specific edge case that must be handled correctly. Also write a pytest suite
that covers every tool AND its edge case.

1. `GET /wordcount?text=...` returns JSON `{"words": N, "chars": M, "lines": L}`.
   Edge case: empty text must return `{"words": 0, "chars": 0, "lines": 0}` with
   HTTP 200 — not an error, not `lines: 1`.

2. `GET /palindrome?text=...` returns `{"is_palindrome": true/false}`, judged
   case-insensitively and ignoring spaces and punctuation. Edge case:
   `"A man, a plan, a canal: Panama"` must return `true`.

3. `GET /slug?text=...` returns `{"slug": "..."}` — lowercase, spaces collapsed
   to single hyphens, punctuation removed, no leading/trailing hyphen. Edge case:
   `"  Hello,   World!!  "` must return `{"slug": "hello-world"}`.

4. `GET /caesar?text=...&shift=N` returns `{"result": "..."}` — a Caesar cipher
   that shifts letters by N, wraps around the alphabet, preserves case, and
   leaves non-letters unchanged. Edge case: `text="XYZ", shift=3` must return
   `"ABC"` (wrap-around), and digits/spaces pass through untouched.

5. `GET /` returns a simple HTML page that lists the four tools with a short
   description and an example URL for each, so a human can click and try them.

6. Add a pytest suite (`test_toolbox.py`) that tests every endpoint above,
   including each stated edge case (empty text, the Panama palindrome, the
   messy slug, the XYZ wrap-around).
