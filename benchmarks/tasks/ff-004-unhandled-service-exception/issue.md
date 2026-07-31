# Missing users return HTTP 500

## Observed behavior

- `GET /health` returns a successful response.
- `GET /users/7` returns the existing user.
- `GET /users/404` unexpectedly returns HTTP 500.
- The existing user service behavior should not be modified.

## Expected behavior

`GET /users/404` should return HTTP 404 with:

```json
{"detail": "User not found"}
```

Fix the route behavior without changing tests. Run `python -m pytest -q` to reproduce the failure.
