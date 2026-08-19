# SQLAlchemy Session not closed after request ends

`GET /health` returns HTTP 200, `GET /users` returns an empty list, and `POST /users` returns HTTP 201 with the created user JSON.

However, the `get_db()` function directly returns a `Session` instance without using a generator with `yield` and a `finally` block. This means the Session is never explicitly closed after a request ends, which can lead to connection leaks and resource exhaustion under load.

Fix the implementation without modifying the API contract, schemas, models, or tests.