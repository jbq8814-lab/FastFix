# Created users are unavailable to later requests

`GET /health` returns HTTP 200, and `GET /users` initially returns an empty list.
`POST /users` returns HTTP 201 with the created user JSON, but a subsequent
`GET /users/{id}` returns HTTP 404 instead of HTTP 200 with that user.

Fix the implementation without modifying the API contract, schemas, models, or tests.
