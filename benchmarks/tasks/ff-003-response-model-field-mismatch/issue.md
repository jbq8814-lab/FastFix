# User endpoint returns HTTP 500

## Observed behavior

- `GET /health` returns a successful response.
- `GET /users/7` unexpectedly returns HTTP 500.
- The existing user service already returns the correct user data and should not be modified.

## Expected behavior

`GET /users/7` should return HTTP 200 with:

```json
{"id": 7, "name": "Ada Lovelace"}
```

Fix the response contract without changing tests. Run `python -m pytest -q` to reproduce the failure.
