import os


def get_app_name() -> str:
    app_name = os.getenv("FASTFIX_SERVICE_NAME", "FastFix")
    return app_name


def get_app_version() -> str:
    app_version = os.getenv("FASTFIX_APP_VERSION", "1.0.0")
    return app_version
