"""JSON logging with recursive redaction for secrets and credentials."""

import json
import logging
import re
from collections.abc import Mapping

SENSITIVE_KEY_PATTERN = re.compile(
    r'(api[_-]?key|authorization|cookie|credential|password|secret|token)', re.IGNORECASE
)
REDACTED_VALUE = '[redacted]'


def redact_value(value):
    """Return a log-safe value without leaking values carried in mappings or text."""
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED_VALUE if SENSITIVE_KEY_PATTERN.search(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, (list, set)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return re.sub(
            r'(?i)(api[_-]?key|authorization|cookie|credential|password|secret|token)\s*[=:]\s*[^\s,;]+',
            r'\1=' + REDACTED_VALUE,
            value,
        )
    return value


class RedactingFilter(logging.Filter):
    """Redact known sensitive keys before a record reaches any handler."""

    def filter(self, record):
        # Render positional arguments before changing the message. Django's own
        # request logger uses multiple ``%s`` placeholders, which must retain a
        # tuple rather than a list during interpolation.
        if record.args:
            record.args = redact_value(record.args)
        record.msg = redact_value(record.getMessage())
        record.args = ()
        for key, value in list(record.__dict__.items()):
            if key.startswith('_') or key in {'msg', 'args', 'exc_info', 'exc_text', 'stack_info'}:
                continue
            record.__dict__[key] = REDACTED_VALUE if SENSITIVE_KEY_PATTERN.search(key) else redact_value(value)
        return True


class JsonFormatter(logging.Formatter):
    """Emit one structured, log-aggregator-friendly JSON object per record."""

    reserved_fields = set(logging.LogRecord('', 0, '', 0, '', (), None).__dict__) | {'message'}

    def format(self, record):
        message = redact_value(record.getMessage())
        payload = {
            'timestamp': self.formatTime(record, self.datefmt),
            'level': record.levelname,
            'logger': record.name,
            'message': message,
        }
        for key, value in record.__dict__.items():
            if key not in self.reserved_fields and not key.startswith('_'):
                payload[key] = REDACTED_VALUE if SENSITIVE_KEY_PATTERN.search(key) else redact_value(value)
        if record.exc_info:
            payload['exception'] = redact_value(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)
