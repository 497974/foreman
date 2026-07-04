# Expense reporting system — full 20-item checklist

A small internal expense-reporting web service (Flask + SQLite). Every item
below is independently verifiable by an automated test — no aesthetic or
subjective requirements.

Delivery spec (applies to every item): the application entry point must be
`app.py` exposing a Flask instance named `app`; tests are plain pytest files in
the project root; everything must be verifiable offline via `python -m pytest`
(use Flask's test client, never a live server). The tests covering requirement N must live in a file named test_reqNN.py (requirement 3 -> test_req03.py) so delivery can be audited requirement by requirement.

1. Flask app skeleton with GET /health returning JSON {"status": "ok"} and HTTP 200.
2. SQLite Expense model with fields: id, amount, category, note, created_at (auto-set on creation).
3. User model and POST /auth/register accepting username + password; the password must be stored hashed, never in plaintext.
4. POST /auth/login returning an auth token for valid credentials and HTTP 401 for wrong ones.
5. All /expenses endpoints require authentication: requests without a valid token get HTTP 401.
6. POST /expenses creates an expense for the authenticated user; amounts <= 0 are rejected with HTTP 400.
7. POST /expenses rejects a missing or empty category with HTTP 400 and a JSON error message.
8. GET /expenses returns the authenticated user's expenses as JSON, newest first.
9. GET /expenses?category=<name> filters results to that category.
10. GET /expenses?from=<date>&to=<date> filters results to a creation-date range (ISO dates).
11. GET /expenses/<id> returns one expense; unknown ids return HTTP 404.
12. PUT /expenses/<id> updates amount/category/note with the same validation rules as creation.
13. DELETE /expenses/<id> removes the expense; a subsequent GET for it returns HTTP 404.
14. GET /expenses supports pagination via ?page=&per_page= with default per_page=20 and a hard cap of 100.
15. GET /expenses/summary returns total amount per category for the authenticated user.
16. GET /expenses/export returns all of the user's expenses as CSV (text/csv) with a header row.
17. POST /expenses/<id>/submit moves an expense from status "draft" to "pending_approval"; only draft expenses can be submitted.
18. Users can have an "approver" role; POST /expenses/<id>/approve sets status "approved" and requires the approver role — non-approvers get HTTP 403.
19. Every status change (submit/approve) is recorded in an audit table; GET /expenses/<id>/history returns the ordered audit trail.
20. The project ships a pytest suite covering all of the above; `python -m pytest -q` passes in the project directory.
