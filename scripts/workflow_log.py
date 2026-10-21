"""Readable, flushed workflow logs with escaped remote text and masked tokens."""

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone


def safe_text(value):
    text = str(value)
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(key)
        if token:
            text = text.replace(token, "***")
    return json.dumps(text, ensure_ascii=False)[1:-1]


def log(stage, message, repo=None):
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    repository = repo or os.environ.get("TARGET_REPO", "workflow")
    print(
        f"[{timestamp}] [{safe_text(repository)}] [{safe_text(stage)}] {safe_text(message)}",
        flush=True,
    )


@contextmanager
def group(title):
    enabled = os.environ.get("GITHUB_ACTIONS") == "true"
    if enabled:
        print(f"::group::{safe_text(title)}", flush=True)
    try:
        yield
    finally:
        if enabled:
            print("::endgroup::", flush=True)
