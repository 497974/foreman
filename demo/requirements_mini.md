# Expense tracker — mini checklist (for cheap planner testing)

1. Create a Flask app skeleton with a health-check route at GET /health.
2. Add a SQLite Expense model with fields: id, amount, category, note, created_at.
3. Add POST /expenses that creates an expense; reject amounts <= 0 with HTTP 400.
4. Add GET /expenses that returns all expenses as JSON, newest first.
5. Add a pytest suite covering the create-and-list happy path and the negative-amount rejection.
