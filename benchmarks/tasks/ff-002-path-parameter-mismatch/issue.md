# Valid user route request returns HTTP 422

## Observed behavior

- `GET /health` returns a successful response.
- `GET /users/7` unexpectedly returns HTTP 422.
- The existing user service works and should not be modified.

## Expected behavior

`GET /users/7` should return HTTP 200 with:

```json
{"id": 7, "name": "Ada Lovelace"}
```

Fix the route without changing tests. Run `python -m pytest -q` to reproduce the failure.
