# `/users/{user_id}` returns an internal server error

## Observed behavior

- `GET /health` returns a successful response.
- `GET /users/7` returns HTTP 500.
- `tests/test_users.py::test_get_user_returns_user` fails.

## Expected behavior

`GET /users/7` should return HTTP 200 with:

```json
{"id": 7, "name": "Ada Lovelace"}
```

Run `python -m pytest -q` to reproduce the failure.
