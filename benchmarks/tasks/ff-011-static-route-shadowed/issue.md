# The current-user endpoint returns a validation error

`GET /health` returns HTTP 200, and `GET /users/1` returns the expected user response.

However, `GET /users/me` returns HTTP 422 instead of HTTP 200 with the current-user response.

Fix the endpoint behavior without changing its path, response content, or the numeric user ID endpoint.
