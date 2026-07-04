# Backend-swap demo checklist (2 items)

Delivery spec: the app entry point is `app.py` exposing a Flask instance named
`app`; tests are pytest files in the project root named test_reqNN.py;
everything verifiable offline via `python -m pytest` (Flask test client, no live server).

1. Create app.py with a Flask app and a GET /ping route returning JSON {"pong": true} with HTTP 200.
2. Create app.py's GET /square/<int:n> route returning JSON {"result": n*n}.
