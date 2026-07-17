import json
import logging

from django.test import SimpleTestCase

from .logging import REDACTED_VALUE, JsonFormatter, RedactingFilter, redact_value


class LoggingRedactionTests(SimpleTestCase):
    def test_redacts_sensitive_mapping_keys_recursively(self):
        value = redact_value({'api_key': 'top-secret', 'nested': {'password': 'hidden'}, 'safe': 'shown'})

        self.assertEqual(value['api_key'], REDACTED_VALUE)
        self.assertEqual(value['nested']['password'], REDACTED_VALUE)
        self.assertEqual(value['safe'], 'shown')

    def test_json_formatter_redacts_extra_fields_and_message(self):
        record = logging.LogRecord(
            'exports', logging.INFO, __file__, 1, 'token=%s', ('not-safe',), None
        )
        record.api_key = 'also-not-safe'
        RedactingFilter().filter(record)

        payload = json.loads(JsonFormatter().format(record))
        self.assertIn(REDACTED_VALUE, payload['message'])
        self.assertEqual(payload['api_key'], REDACTED_VALUE)

    def test_json_formatter_redacts_exception_text(self):
        try:
            raise RuntimeError('password=not-safe')
        except RuntimeError:
            record = logging.LogRecord('exports', logging.ERROR, __file__, 1, 'Export failed', (), None)
            record.exc_info = __import__('sys').exc_info()

        payload = json.loads(JsonFormatter().format(record))
        self.assertIn(REDACTED_VALUE, payload['exception'])
