# User creation fails because the ORM attribute name is inconsistent

`GET /health` returns HTTP 200, and `GET /users` initially returns an empty list.

However, `POST /users` with `{"name": "Ada Lovelace"}` returns HTTP 500 instead of the expected HTTP 201 response.

The request schema, response schema, service, and API contract are correct and should not be changed. Fix the inconsistency in the ORM mapping layer.
