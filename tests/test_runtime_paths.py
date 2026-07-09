from __future__ import annotations

import unittest
from pathlib import Path

from video_agent.config import load_config
from video_agent.runtime_paths import apply_runtime_paths, ensure_runtime_files


class RuntimePathsTest(unittest.TestCase):
    def test_creates_runtime_files_without_overwriting_existing_config(self) -> None:
        runtime_dir = Path("test_outputs") / "runtime_paths"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        config_path = runtime_dir / "config.yaml"
        config_path.write_text("agent:\n  agent_id: custom-agent\n", encoding="utf-8")

        returned = ensure_runtime_files(runtime_dir)
        ensure_runtime_files(runtime_dir)

        self.assertEqual(returned, config_path)
        self.assertIn("custom-agent", config_path.read_text(encoding="utf-8"))
        self.assertTrue((runtime_dir / "config" / "iectp_rsa_private.pem").exists())
        self.assertTrue((runtime_dir / "config" / "iectp_rsa_public.pem").exists())
        self.assertTrue((runtime_dir / "recordings").is_dir())
        self.assertTrue((runtime_dir / "logs").is_dir())

    def test_apply_runtime_paths_forces_recordings_and_rsa_paths(self) -> None:
        runtime_dir = Path("test_outputs") / "runtime_paths_apply"
        config = load_config("config.example.yaml")

        adjusted = apply_runtime_paths(config, runtime_dir)

        self.assertEqual(adjusted.obs.recordings_dir, str(runtime_dir / "recordings"))
        self.assertEqual(adjusted.platform.rsa_private_key_path, str(runtime_dir / "config" / "iectp_rsa_private.pem"))
        self.assertEqual(adjusted.platform.rsa_public_key_path, str(runtime_dir / "config" / "iectp_rsa_public.pem"))


if __name__ == "__main__":
    unittest.main()
