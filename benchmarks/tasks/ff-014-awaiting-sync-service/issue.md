# The user detail endpoint fails while other user routes work

`GET /health` and `GET /users` both return HTTP 200 with the expected responses.

However, `GET /users/1` returns HTTP 500 instead of HTTP 200 with the expected user response.

Fix the user detail endpoint without changing its path, response content, or the behavior of the healthy endpoints.
