# The protected endpoint accepts invalid credentials

`GET /health` and `GET /public` both return HTTP 200 with the expected responses.

However, `GET /admin/stats` returns HTTP 200 when called with invalid credentials instead of rejecting the request with HTTP 401.

Fix the protected endpoint behavior without changing its path, successful response, or the public endpoints.
