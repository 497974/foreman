# Expense tracker — mini checklist (for cheap planner testing)

Delivery spec (applies to every item): the application entry point must be
`app.py` exposing a Flask instance named `app`; tests are plain pytest files in
the project root; everything must be verifiable offline via `python -m pytest`
(use Flask's test client, never a live server). The tests covering requirement N must live in a file named test_reqNN.py (requirement 3 -> test_req03.py) so delivery can be audited requirement by requirement.

1. Create a Flask app skeleton with a health-check route at GET /health.
2. Add a SQLite Expense model with fields: id, amount, category, note, created_at.
3. Add POST /expenses that creates an expense; reject amounts <= 0 with HTTP 400.
4. Add GET /expenses that returns all expenses as JSON, newest first.
5. Add a pytest suite covering the create-and-list happy path and the negative-amount rejection.
