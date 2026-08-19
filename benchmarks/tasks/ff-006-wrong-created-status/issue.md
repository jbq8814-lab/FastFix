# Created users return HTTP 200

## Observed behavior

- `GET /health` returns a successful response.
- `POST /users` returns the correct response content with HTTP 200.

## Expected behavior

`POST /users` should preserve the response JSON while returning HTTP 201.

Fix the route declaration without changing the service, schemas, or tests. Run `python -m pytest -q` to reproduce the
failure.
