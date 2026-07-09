from __future__ import annotations

import unittest

from video_agent.redaction import redact_mapping, redact_text


class RedactionTest(unittest.TestCase):
    def test_redacts_sensitive_mapping_keys(self) -> None:
        payload = {"api_token": "abc", "credentials": {"password": "secret"}, "normal": "ok"}
        self.assertEqual(redact_mapping(payload)["api_token"], "***")
        self.assertEqual(redact_mapping(payload)["credentials"]["password"], "***")
        self.assertEqual(redact_mapping(payload)["normal"], "ok")

    def test_redacts_bearer_token_text(self) -> None:
        self.assertEqual(redact_text("Authorization: Bearer abc.def"), "Authorization: Bearer ***")


if __name__ == "__main__":
    unittest.main()
