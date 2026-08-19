import os

os.environ["FASTFIX_APP_NAME"] = "FastFix Test"
os.environ["FASTFIX_APP_VERSION"] = "2.0.0"
os.environ.pop("FASTFIX_SERVICE_NAME", None)
