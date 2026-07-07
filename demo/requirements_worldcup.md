# World Cup Win-Probability Predictor

1. Create `worldcup_data.py` with a `TEAMS` dict mapping at least 12 national
   teams to Elo ratings (use realistic values, e.g. Argentina 2143, France
   2005, Brazil 2035, England 1980, Spain 2048, Germany 1935, Portugal 1968,
   Netherlands 1953, Italy 1906, Croatia 1893, Japan 1849, Morocco 1849), and
   a function `get_rating(team)` that returns the rating, or `None` for an
   unknown team. Team lookup must be case-insensitive.

2. Create `predictor.py` with `win_probability(team_a, team_b)` implementing
   the Elo expected-score formula: P(a beats b) = 1 / (1 + 10 ** ((rating_b -
   rating_a) / 400)). It returns a dict {"team_a": ..., "team_b": ...,
   "prob_a": float, "prob_b": float} where prob_a + prob_b == 1.0 (rounded to
   4 decimals). Raise ValueError for unknown teams.

3. Create a Flask app in `app.py` with `GET /predict?a=<team>&b=<team>`
   returning the predictor's JSON result with HTTP 200; missing parameters or
   unknown teams return HTTP 400 with a JSON error message.

4. Add `GET /teams` to `app.py` returning all supported teams as a JSON list
   sorted by rating, strongest first, each entry {"team": ..., "rating": ...}.

5. Add a pytest suite covering: equal ratings give 0.5/0.5; a stronger team
   gets prob > 0.5; unknown team returns HTTP 400; /teams is sorted
   descending by rating.
