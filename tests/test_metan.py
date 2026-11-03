from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from io import BytesIO
from urllib.request import urlopen
import yaml
from ai_partner.persona import persona, persona_prompt
from ai_partner.game_packs import GamePackManager
from ai_partner.voice import VoiceOutput

class MetanTests(unittest.TestCase):
    def test_persona_and_fallbacks(self):
        profile = persona()
        self.assertEqual(profile["name"], "四国めたん")
        prompt = persona_prompt()
        for word in ("旅行", "うどん", "アクション", "縛り", "恋愛", "四国弁"):
            self.assertIn(word, prompt)
        pack = yaml.safe_load((Path(__file__).resolve().parents[1] / "event_games/invader/game.yaml").read_text(encoding="utf-8"))
        for spec in pack["lines"].values():
            for text in spec["texts"]:
                self.assertNotIn("ですわ", text)
                self.assertNotIn("わたくし", text)

    def test_voice_status_endpoint_tracks_playback_and_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"invader"; root.mkdir()
            (root/"game.yaml").write_text("id: invader\ntracking:\n  method: http\n  port: 0\n",encoding="utf-8")
            manager=GamePackManager({"directory":directory},{},lambda *args:None)
            with patch("ai_partner.game_packs.ThreadingHTTPServer") as server:
                manager._start_http()
                Handler = server.call_args.args[1]
                def status():
                    handler = Handler.__new__(Handler)
                    handler.path = "/voice"
                    handler.wfile = BytesIO()
                    handler.send_response = MagicMock()
                    handler.send_header = MagicMock()
                    handler.end_headers = MagicMock()
                    handler.do_GET()
                    handler.send_response.assert_called_once_with(200)
                    handler.send_header.assert_any_call("Cache-Control", "no-store")
                    handler.send_header.assert_any_call("Access-Control-Allow-Origin", "*")
                    return json.loads(handler.wfile.getvalue())
                self.assertFalse(status()["speaking"])
                manager.speaking=True
                self.assertTrue(status()["speaking"])
                self.assertEqual(status()["duck_volume"],0.25)
                manager.speaking=False
                self.assertFalse(status()["speaking"])
                manager.stop()

    def test_failed_synthesis_does_not_publish_playback(self):
        voice=VoiceOutput.__new__(VoiceOutput)
        voice.voicevox=MagicMock()
        voice.voicevox.synthesize.return_value=(False,"offline")
        voice.on_playback=MagicMock()
        with self.assertRaises(RuntimeError):
            voice._voicevox("テスト","neutral",0.4,0.5)
        voice.on_playback.assert_not_called()
