from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from ai_partner.game_packs import GamePackManager


class GameEventTests(unittest.TestCase):
    def make_manager(self, received: list) -> GamePackManager:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        pack_dir = Path(temp.name) / "sample"
        pack_dir.mkdir()
        manifest = {
            "id": "sample",
            "name": "Sample",
            "tracking": {"method": "http", "port": 0},
            "lines": {
                "hit": {"emotion": "surprise", "intensity": 0.6, "texts": ["残り{lives}機", "もう一度"]},
                "default": ["なるほど"],
            },
        }
        (pack_dir / "game.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
        return GamePackManager(
            {"enabled": True, "directory": temp.name},
            {},
            lambda pack, event: received.append((pack.id, event)),
        )

    def test_repeated_events_are_not_treated_as_duplicates(self) -> None:
        received: list = []
        manager = self.make_manager(received)
        event = {"event_type": "hit", "summary": "hit", "details": {"lives": 2}}
        self.assertEqual(manager.receive_http({"game": "sample", "event": event}), 1)
        self.assertEqual(manager.receive_http({"game": "sample", "event": event}), 1)
        self.assertEqual(len(received), 2)
        self.assertEqual(manager.current.id, "sample")

    def test_prepared_line_fills_values_and_avoids_immediate_repeat(self) -> None:
        manager = self.make_manager([])
        pack = manager.packs[0]
        event = {"event_type": "hit", "details": {"lives": 2}}
        first = manager.pick_line(pack, event)
        second = manager.pick_line(pack, event)
        self.assertNotEqual(first["utterance"], second["utterance"])
        self.assertIn(first["utterance"], {"残り2機", "もう一度"})
        self.assertEqual(first["local_emotion"], "surprise")
        self.assertEqual(manager.pick_line(pack, {"event_type": "unknown"})["utterance"], "なるほど")


if __name__ == "__main__":
    unittest.main()
