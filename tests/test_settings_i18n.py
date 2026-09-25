"""Settings persistence, and the translation catalogue."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT  # noqa: F401

from flacblind.core.models import AudioConfig, SegmentSpec
from flacblind.core.settings import PLAYBACK_BACKENDS, ROUND_CHOICES, AppSettings
from flacblind.ui import i18n


class TestAppSettings(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-test-settings-"))
        self.path = self.tmp / "settings.json"

    def test_defaults(self) -> None:
        settings = AppSettings()
        self.assertEqual(settings.rounds, 10)
        self.assertTrue(settings.normalize)
        self.assertTrue(settings.whole_file)
        self.assertEqual(settings.language, "en")
        self.assertEqual(settings.backend, "auto")
        self.assertTrue(settings.delete_temp_on_exit)
        self.assertEqual(settings.audio_config().target_lufs, settings.target_lufs)
        self.assertTrue(settings.segment().whole_file)

    def test_save_and_load_round_trip(self) -> None:
        settings = AppSettings()
        settings.rounds = 20
        settings.target_lufs = -18.5
        settings.language = "ru"
        settings.whole_file = False
        settings.segment_start = 42.0
        settings.segment_duration = 25.0
        settings.recent = ["/music/a.flac", "/music/a.mp3"]
        settings.save(self.path)
        self.assertTrue(self.path.exists())

        loaded = AppSettings().load(self.path)
        self.assertEqual(loaded.rounds, 20)
        self.assertEqual(loaded.target_lufs, -18.5)
        self.assertEqual(loaded.language, "ru")
        self.assertFalse(loaded.whole_file)
        self.assertEqual(loaded.segment_start, 42.0)
        self.assertEqual(loaded.recent, ["/music/a.flac", "/music/a.mp3"])
        segment = loaded.segment()
        self.assertFalse(segment.whole_file)
        self.assertAlmostEqual(segment.duration_sec or 0.0, 25.0)

    def test_load_tolerates_corruption(self) -> None:
        self.path.write_text("<<<not json>>>", encoding="utf-8")
        self.assertEqual(AppSettings().load(self.path).rounds, 10)
        self.path.write_text('{"rounds": "nonsense"}', encoding="utf-8")
        settings = AppSettings().load(self.path)
        self.assertIn(settings.rounds, ROUND_CHOICES)

    def test_load_clamps_values(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "target_lufs": -999.0,
                    "true_peak_db": 42.0,
                    "lra": -3.0,
                    "sample_rate": 12345,
                    "rounds": 7,
                    "volume": 9.0,
                    "backend": "nonsense",
                    "language": "de",
                    "recent": "not-a-list",
                    "segment_duration": -1.0,
                    "unknown_key": 1,
                }
            ),
            encoding="utf-8",
        )
        settings = AppSettings().load(self.path)
        self.assertEqual(settings.target_lufs, -40.0)
        self.assertEqual(settings.true_peak_db, 0.0)
        self.assertEqual(settings.lra, 1.0)
        self.assertEqual(settings.sample_rate, 44100)
        self.assertEqual(settings.rounds, 10)
        self.assertEqual(settings.volume, 1.0)
        self.assertEqual(settings.backend, "auto")
        self.assertEqual(settings.language, "en")
        self.assertEqual(settings.recent, [])
        self.assertEqual(settings.segment_duration, 1.0)

    def test_load_missing_file_is_default(self) -> None:
        self.assertEqual(AppSettings().load(self.tmp / "ghost.json").rounds, 10)

    def test_recent_deduplicates(self) -> None:
        settings = AppSettings()
        for _ in range(3):
            settings.remember("/a.flac")
        settings.remember("/b.flac")
        settings.remember("/a.flac")
        self.assertEqual(settings.recent, ["/a.flac", "/b.flac"])
        for i in range(20):
            settings.remember(f"/f{i}.flac")
        self.assertLessEqual(len(settings.recent), 8)

    def test_apply_helpers(self) -> None:
        settings = AppSettings()
        settings.apply_audio_config(AudioConfig(target_lufs=-14.0, sample_rate=96000))
        self.assertEqual(settings.target_lufs, -14.0)
        self.assertEqual(settings.sample_rate, 96000)
        settings.apply_segment(SegmentSpec.slice_of(5.0, 12.0))
        self.assertFalse(settings.whole_file)
        self.assertEqual(settings.segment_start, 5.0)
        self.assertEqual(settings.segment_duration, 12.0)
        settings.apply_segment(SegmentSpec.whole())
        self.assertTrue(settings.whole_file)

    def test_backend_choices(self) -> None:
        keys = {key for key, _label in PLAYBACK_BACKENDS}
        self.assertEqual(keys, {"auto", "sounddevice", "qt", "cli"})


class TestTranslations(unittest.TestCase):
    def test_every_language_has_every_key(self) -> None:
        reference = set(i18n._STRINGS["en"])
        for code, table in i18n._STRINGS.items():
            missing = reference - set(table)
            self.assertFalse(missing, f"{code} is missing: {sorted(missing)}")
            extra = set(table) - reference
            self.assertFalse(extra, f"{code} has unknown keys: {sorted(extra)}")

    def test_no_empty_strings(self) -> None:
        for code, table in i18n._STRINGS.items():
            for key, value in table.items():
                self.assertTrue(value.strip(), f"{code}:{key} is empty")

    def test_format_placeholders_match(self) -> None:
        pattern = re.compile(r"\{(\w+)\}")
        for key, english in i18n._STRINGS["en"].items():
            expected = set(pattern.findall(english))
            for code, table in i18n._STRINGS.items():
                self.assertEqual(
                    set(pattern.findall(table[key])), expected, f"{code}:{key} placeholders differ"
                )

    def test_translator_switching(self) -> None:
        translator = i18n.Translator("en")
        self.assertEqual(translator.tr("sources.title"), "Sources")
        calls: list[int] = []
        hook = translator.on_change(lambda: calls.append(1))
        translator.set_language("ru")
        self.assertEqual(translator.tr("sources.title"), "Источники")
        self.assertEqual(len(calls), 1)
        translator.set_language("ru")  # no change, no signal
        self.assertEqual(len(calls), 1)
        hook()
        translator.set_language("en")
        self.assertEqual(len(calls), 1)
        self.assertEqual(translator.tr("sources.title"), "Sources")

    def test_unknown_key_falls_back_to_itself(self) -> None:
        translator = i18n.Translator("en")
        self.assertEqual(translator.tr("no.such.key"), "no.such.key")

    def test_missing_translation_falls_back_to_english(self) -> None:
        table = i18n._STRINGS["en"]
        i18n._STRINGS["ru"]["only.in.english"] = "Only English"
        try:
            translator = i18n.Translator("ru")
            self.assertEqual(translator.tr("only.in.english"), "Only English")
        finally:
            table.pop("only.in.english", None)
            i18n._STRINGS["ru"].pop("only.in.english", None)

    def test_formatting(self) -> None:
        translator = i18n.Translator("en")
        self.assertEqual(translator.tr("session.round", current=2, total=10), "Round 2 / 10")
        self.assertEqual(translator.tr("player.sample", key="B"), "Sample B")

    def test_russian_plurals(self) -> None:
        translator = i18n.Translator("ru")
        self.assertEqual(translator.plural(1), "верный ответ")
        self.assertEqual(translator.plural(2), "верных ответа")
        self.assertEqual(translator.plural(5), "верных ответов")
        self.assertEqual(translator.plural(11), "верных ответов")
        self.assertEqual(translator.plural(21), "верный ответ")
        english = i18n.Translator("en")
        self.assertEqual(english.plural(1), "vote")
        self.assertEqual(english.plural(3), "votes")

    def test_language_registry(self) -> None:
        self.assertIn(("en", "English"), i18n.available_languages())
        self.assertIn(("ru", "Русский"), i18n.available_languages())

    def test_all_tr_calls_reference_known_keys(self) -> None:
        """Every tr("…") in the source must exist in the catalogue."""
        known = set(i18n._STRINGS["en"])
        pattern = re.compile(r"""tr\(\s*["']([a-z0-9_.]+)["']""")
        missing: set[str] = set()
        for path in (ROOT / "flacblind").rglob("*.py"):
            for key in pattern.findall(path.read_text(encoding="utf-8")):
                if key not in known:
                    missing.add(f"{path.name}:{key}")
        self.assertFalse(missing, f"unknown translation keys: {sorted(missing)}")

    def test_unused_keys_are_flagged(self) -> None:
        """Keeps the catalogue honest: no dead strings."""
        pattern = re.compile(r"""tr\(\s*["']([a-z0-9_.]+)["']""")
        used: set[str] = set()
        for path in (ROOT / "flacblind").rglob("*.py"):
            used.update(pattern.findall(path.read_text(encoding="utf-8")))
        # Keys referenced indirectly (through widget constructor arguments or
        # mapped lookups) rather than as a literal tr("…") call.
        dynamic = {
            # DropCard(title_key=…, hint_key=…)
            "sources.lossless", "sources.lossy",
            "sources.hint.lossless", "sources.hint.lossy",
            # _group(title_key)
            "prepare.processing", "prepare.segment", "session.title",
            # StatTile(title_key=…) and its dynamic empty value
            "results.score", "results.pvalue", "results.chance", "common.none",
            # lookups built at runtime
            "prepare.reprepare", "prepare.busy", "app.idle", "results.no_data",
            "vote.need_both", "vote.need_plays",
            # tr(f"player.backend.{key}") lookups
            "player.backend.sounddevice", "player.backend.qt", "player.backend.cli",
            "player.backend.none",
            # tr(f"results.verdict.{level}") / tr(f"results.detail.{level}") lookups
            "results.verdict.strong", "results.verdict.significant",
            "results.verdict.weak", "results.verdict.none",
            "results.detail.strong", "results.detail.significant",
            "results.detail.weak", "results.detail.none",
            # passed as an argument (self._set_feedback(key))
            "session.start_playing",
        }
        unused = set(i18n._STRINGS["en"]) - used - dynamic
        self.assertFalse(unused, f"unused translation keys: {sorted(unused)}")


if __name__ == "__main__":
    unittest.main()
