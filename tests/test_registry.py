from __future__ import annotations

import unittest

from video_agent.providers.registry import create_provider, list_providers


class RegistryTest(unittest.TestCase):
    def test_tencent_provider_is_registered(self) -> None:
        self.assertIn("tencent_meeting", list_providers())
        provider = create_provider("tencent_meeting", {"tencent_meeting": {}})
        self.assertEqual(provider.provider_name, "tencent_meeting")

    def test_dingtalk_provider_is_registered(self) -> None:
        self.assertIn("dingtalk", list_providers())
        provider = create_provider("dingtalk", {"dingtalk": {}})
        self.assertEqual(provider.provider_name, "dingtalk")

    def test_mixlink_provider_is_registered(self) -> None:
        self.assertIn("mixlink", list_providers())
        provider = create_provider("mixlink", {"mixlink": {}})
        self.assertEqual(provider.provider_name, "mixlink")

    def test_unknown_provider_fails_clearly(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown_provider"):
            create_provider("zoom", {})


if __name__ == "__main__":
    unittest.main()
