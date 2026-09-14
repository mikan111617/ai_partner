from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_partner.mind import MindEngine


class MindEngineTests(unittest.TestCase):
    def make_mind(self) -> MindEngine:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return MindEngine(Path(temp.name) / "mind.json")

    def test_high_excitement_does_not_amplify_light_anger(self) -> None:
        mind = self.make_mind()
        mind.state["excitement"] = 0.95
        mind.state["joy"] = 0.65
        mind.state["arousal"] = 0.95
        result = mind.presentation("angry", 0.25)
        self.assertEqual(result["emotion"], "angry")
        self.assertLessEqual(result["intensity"], 0.25)
        self.assertGreater(result["delivery_energy"], 0.8)

    def test_story_character_event_does_not_change_relationship(self) -> None:
        mind = self.make_mind()
        before = {key: mind.state[key] for key in ("trust", "affection", "respect", "antipathy")}
        mind.appraise(
            {
                "actor_scope": "story_character",
                "importance": 1.0,
                "signals": {"inconsistency": 1.0, "cowardice": 1.0, "cruelty": 1.0},
            }
        )
        after = {key: mind.state[key] for key in before}
        self.assertEqual(before, after)

    def test_player_defeat_affects_respect_and_concern_not_trust(self) -> None:
        mind = self.make_mind()
        trust_before = mind.state["trust"]
        respect_before = mind.state["respect"]
        concern_before = mind.state["concern"]
        mind.appraise(
            {
                "actor_scope": "user",
                "importance": 0.8,
                "signals": {"player_defeated": 1.0, "heavy_damage": 0.4},
            }
        )
        self.assertEqual(mind.state["trust"], trust_before)
        self.assertLess(mind.state["respect"], respect_before)
        self.assertGreater(mind.state["concern"], concern_before)


if __name__ == "__main__":
    unittest.main()
