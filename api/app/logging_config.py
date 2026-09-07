"""Logging setup + request-id propagation.

A ``contextvar`` holds the current request/job id so every log line can be tagged
without threading it through call sites. ``setup_logging`` installs a formatter
(plain or JSON) that includes it.
"""
import contextvars
import json
import logging
import sys
import uuid

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def set_request_id(rid: str) -> None:
    request_id_var.set(rid)


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", json_logs: bool = False) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    if json_logs:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # quiet noisy libraries
    for noisy in ("botocore", "boto3", "urllib3", "strands"):
        logging.getLogger(noisy).setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
