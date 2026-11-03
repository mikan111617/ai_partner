import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from ai_partner.config import load_config
from ai_partner.voice import VoiceOutput


class VoiceSelectionTests(unittest.TestCase):
    def test_first_selection_is_saved_and_other_settings_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / 'config.yaml'
            local = Path(directory) / 'config.user.yaml'
            local.write_text('microphone:\n  enabled: false\n', encoding='utf-8')
            with patch('builtins.input', side_effect=['bad', '1']):
                cfg = load_config(base, select_voice=True)
            self.assertEqual(cfg['voice']['provider'], 'voicevox')
            self.assertFalse(cfg['microphone']['enabled'])
            with patch('builtins.input', side_effect=AssertionError('prompted again')):
                self.assertEqual(load_config(base, select_voice=True)['voice']['provider'], 'voicevox')
            with patch('builtins.input', return_value='2'):
                self.assertEqual(load_config(base, select_voice=True, force_voice_selection=True)['voice']['provider'], 'irodori')

    def test_voicevox_never_constructs_irodori(self):
        with patch('ai_partner.voice.IrodoriSession') as irodori, patch('ai_partner.voice.VoicevoxSession') as voicevox:
            output = VoiceOutput({'provider': 'voicevox'}, MagicMock())
            output.stop()
            irodori.assert_not_called()
            voicevox.return_value.start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
