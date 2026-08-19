# Existing user detail returns an internal server error

`GET /health` returns HTTP 200, and `GET /users` returns the expected user list.
However, `GET /users/1` returns HTTP 500 instead of HTTP 200 with the existing user JSON.

Fix the implementation so the detail endpoint returns the expected existing
user. Do not modify the API routes, response schemas, or tests.
