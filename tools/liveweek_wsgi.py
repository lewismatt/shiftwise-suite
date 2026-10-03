"""Gunicorn entrypoint recording the same server evidence as liveweek's local server."""

import json
import os
import time
import traceback
from pathlib import Path

from flask import got_request_exception, request

from app import app

SERVERLOG = Path(os.environ["SHIFTWISE_LIVEWEEK_SERVERLOG"])


def record(status, error=None):
    entry = {
        "ts": time.time(),
        "method": request.method,
        "path": request.path,
        "status": status,
        "error": error,
    }
    # One append write per JSON line; each process opens its own file handle.
    with SERVERLOG.open("a") as stream:
        stream.write(json.dumps(entry) + "\n")


@app.after_request
def after_response(response):
    record(response.status_code)
    return response


def on_exception(sender, exception, **kwargs):
    record(500, "".join(traceback.format_exception(exception)))


got_request_exception.connect(on_exception, app)
