from __future__ import annotations

import unittest
from pathlib import Path


class PublicRepositoryHygieneTests(unittest.TestCase):
    def test_private_names_and_legacy_public_terms_are_absent(self) -> None:
        root = Path(__file__).resolve().parents[1]
        this_file = Path(__file__).resolve()
        banned = (
            "タクヤ",
            "Takuya",
            "Fate/EXTELLA",
            "fate_extella",
            "Extella_SCRIPTS",
            "extella_scripts",
            "レイラ",
            "wizard_boy",
            "魔法使いの少年",
            "user_name",
            "ScenarioCorpus",
            "scenario_cfg",
            '"scenario_key"',
            "csv_text_column",
            "local_scenarios",
            "シナリオファイル",
            "抽出データ",
            "不正入手",
            "アクセス制限",
            "lawfully used",
            "provider: console",
            "console works without",
        )
        suffixes = {".py", ".md", ".yaml", ".yml", ".json", ".txt", ".cmd"}
        failures: list[str] = []
        for path in root.rglob("*"):
            if (
                not path.is_file()
                or path.resolve() == this_file
                or path.suffix.lower() not in suffixes
            ):
                continue
            if any(
                part in {".git", ".venv", "venv", "local_games", "data"}
                for part in path.parts
            ):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for term in banned:
                if term in text:
                    failures.append(f"{path.relative_to(root)} contains {term!r}")
        self.assertFalse(failures, "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
