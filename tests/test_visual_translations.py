from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_loop_system.tools.visual_translations import (
    load_translation_catalog,
    relevant_visual_translations,
)


def _write_catalog(path: Path, *, version: int = 30) -> None:
    path.write_text(
        json.dumps(
            {
                "CustomerNumber": "2-赛博",
                "TranslationNumber": "客户编号2-1（传音oraimo）",
                "Version": version,
                "Languages": ["zh-CN", "en-US", "fr-FR"],
                "Entries": [
                    {
                        "Key": "STR_Timer",
                        "Group": "计时器",
                        "Translations": {
                            "zh-CN": "计时器",
                            "en-US": "Timer",
                            "fr-FR": "Minuteur",
                        },
                    },
                    {
                        "Key": "STR_Workout_rem",
                        "Group": "运动",
                        "Translations": {
                            "zh-CN": "运动",
                            "en-US": "Workout",
                            "fr-FR": "Entraînement",
                        },
                    },
                    {
                        "Key": "STR_Sports_record",
                        "Group": "运动记录",
                        "Translations": {
                            "zh-CN": "运动记录",
                            "en-US": "Sports record",
                            "fr-FR": "Dossier sportif",
                        },
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8-sig",
    )


class VisualTranslationsTest(unittest.TestCase):
    def test_catalog_keeps_only_key_and_two_approved_languages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Translations.json"
            _write_catalog(path)

            entries = load_translation_catalog(path)

        self.assertEqual(entries[0].key, "STR_Timer")
        self.assertEqual(entries[0].zh_cn, "计时器")
        self.assertEqual(entries[0].en_us, "Timer")
        self.assertFalse(hasattr(entries[0], "fr_fr"))
        self.assertFalse(hasattr(entries[0], "group"))

    def test_catalog_rejects_any_version_other_than_30(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Translations.json"
            _write_catalog(path, version=31)

            with self.assertRaisesRegex(ValueError, "Version 30"):
                load_translation_catalog(path)

    def test_lookup_only_returns_terms_in_the_current_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Translations.json"
            _write_catalog(path)

            timer = relevant_visual_translations(
                "6202_W5230",
                ["Clock子菜单第3项显示Timer"],
                document_path=path,
            )
            legacy_sports = relevant_visual_translations(
                "6202_W5230",
                ["第2项显示Sports"],
                document_path=path,
            )
            workout = relevant_visual_translations(
                "6202_W5230",
                ["第2项显示Workout"],
                document_path=path,
            )

        self.assertEqual([entry.key for entry in timer], ["STR_Timer"])
        self.assertEqual(legacy_sports, ())
        self.assertEqual([entry.key for entry in workout], ["STR_Workout_rem"])

    def test_other_projects_never_load_the_6202_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"

            result = relevant_visual_translations(
                "620C_W6830",
                ["Timer"],
                document_path=missing,
            )

        self.assertEqual(result, ())


if __name__ == "__main__":
    unittest.main()
