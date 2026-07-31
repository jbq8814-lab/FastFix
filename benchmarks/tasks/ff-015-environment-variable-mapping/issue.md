# The configured application name is ignored

`GET /health` returns HTTP 200, and `GET /config/version` returns the configured version.

However, `GET /config/name` returns the fallback application name instead of the configured value.

Fix the application name configuration without changing the endpoint paths, fallback value, or version behavior.
