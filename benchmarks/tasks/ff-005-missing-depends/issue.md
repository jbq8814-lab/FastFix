# Protected endpoint returns HTTP 422

## Observed behavior

- `GET /health` returns a successful response.
- `GET /protected` incorrectly returns HTTP 422 with or without an API key.
- The API key is supplied through the `X-API-Key` request header.

## Expected behavior

- A missing or invalid API key should return HTTP 401 with `{"detail": "Invalid API key"}`.
- The key `secret-key` should return HTTP 200 with the access-granted response.

Fix the protected route without changing the existing API-key validation helper or tests. Run `python -m pytest -q` to
reproduce the failure.
