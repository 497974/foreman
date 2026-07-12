# Dev Toolkit — an interactive single-page web app (Flask)

Build ONE Flask app (`app.py`) with FIVE developer tools as JSON endpoints,
PLUS a working interactive web page that lets a human use every tool with a
button. Each tool has a specific edge case that MUST be handled. Also write a
pytest suite that tests every tool and every stated edge case.

1. `GET /base64?text=...&mode=encode|decode` returns `{"result": "..."}`.
   `mode=encode` base64-encodes the text; `mode=decode` decodes it; encoding
   then decoding must return the original. Edge case: decoding INVALID base64
   (e.g. `mode=decode&text=@@@notb64@@@`) must return HTTP 400 with
   `{"error": "..."}` — it must NOT crash with a 500.

2. `GET /roman?n=...` converts an integer 1–3999 to a Roman numeral,
   `{"roman": "..."}`. Edge cases: `n=4` must be `"IV"` (never `"IIII"`),
   `n=9` -> `"IX"`, `n=40` -> `"XL"`, `n=2024` -> `"MMXXIV"`. `n` out of range
   (0, 4000, negative) returns HTTP 400 with `{"error": "..."}`.

3. `GET /prime?n=...` returns `{"is_prime": true/false}`. Edge cases: `n=1`
   must be `false`, `n=2` must be `true`, `n=0` and negatives must be `false`.

4. `GET /temp?value=...&from=C|F&to=C|F` converts temperature, `{"result": N}`
   rounded to 2 decimals. Edge case: `value=-40&from=C&to=F` must be `-40.0`
   (the point where the scales meet); `value=100&from=C&to=F` must be `212.0`.

5. `GET /anagram?a=...&b=...` returns `{"is_anagram": true/false}`, judged
   case-insensitively and ignoring spaces. Edge case:
   `a="Listen"&b="Silent "` must be `true`; strings of different letter
   content must be `false`.

6. `GET /` returns a WORKING interactive single-page UI. For EACH of the five
   tools it must show: a short title, a labeled text input (or two, for
   anagram / the mode+text tools), and a **button**. Clicking the button must
   call that tool's endpoint via JavaScript `fetch` and display the returned
   JSON in a result area next to that tool. Every button must actually work
   end-to-end — no dead buttons, no "coming soon".

7. Add a pytest suite (`test_toolkit.py`) that tests every endpoint above and
   every stated edge case (invalid base64 -> 400, roman IV/IX/XL/2024 and
   out-of-range -> 400, prime 1/2/0, temp -40 and 212, anagram Listen/Silent).
