"""Unit tests for anki/build_apkg.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_apkg  # noqa: E402


class TestAnkiBuilder(unittest.TestCase):
    def test_stable_guid_normalization(self) -> None:
        """Formatting, whitespace, case, and Unicode variants keep the same GUID."""
        g1 = build_apkg.stable_guid(build_apkg.normalize_key("der Schneematsch"))
        g2 = build_apkg.stable_guid(build_apkg.normalize_key("  Der   Schneematsch "))
        g3 = build_apkg.stable_guid(build_apkg.normalize_key("<b>der</b> Schneematsch"))
        # NFD decomposed umlaut vs NFC composed umlaut
        g_nfc = build_apkg.stable_guid(build_apkg.normalize_key("überrascht"))
        g_nfd = build_apkg.stable_guid(build_apkg.normalize_key("u\u0308berrascht"))
        g_other = build_apkg.stable_guid(build_apkg.normalize_key("das Glatteis"))

        self.assertEqual(g1, g2)
        self.assertEqual(g1, g3)
        self.assertEqual(g_nfc, g_nfd)
        self.assertNotEqual(g1, g_other)
        self.assertTrue(all(ch in build_apkg.ANKI_BASE91_TABLE for ch in g1))

    def test_deduplication_last_write_wins(self) -> None:
        lines = [
            "# comment",
            "front\tback\ttags\tdeck\n",
            "der Schneematsch\told meaning\tgerman\tGerman\n",
            "das Glatteis\tblack ice\tgerman\tGerman\n",
            "  Der Schneematsch \tupdated meaning<br><i>Example</i>\tgerman winter\tGerman::Winter\n",
        ]
        cards, dupes, errors = build_apkg.parse_inbox(lines)
        self.assertEqual(errors, [])
        self.assertEqual(len(cards), 2)
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0].dropped_line, 3)
        self.assertEqual(dupes[0].kept_line, 5)
        self.assertEqual(cards[0].front, "das Glatteis")
        self.assertEqual(cards[1].back, "updated meaning<br><i>Example</i>")
        self.assertEqual(cards[1].deck, "German::Winter")
        self.assertEqual(cards[1].tags, ("german", "winter"))

    def test_html_sanitization(self) -> None:
        raw = 'good <b>bold</b><br><i>italic</i> <script>alert(1)</script><img src=x onerror=alert(1)> a < b'
        cleaned = build_apkg.sanitize_html(raw)
        self.assertIn("<b>bold</b><br><i>italic</i>", cleaned)
        self.assertNotIn("<script>", cleaned)
        self.assertNotIn("alert(1)", cleaned)
        self.assertNotIn("<img", cleaned)
        self.assertNotIn("onerror", cleaned)
        self.assertIn("a &lt; b", cleaned)

    def test_validation_errors(self) -> None:
        lines = [
            "only one column\n",
            "\tmissing front\n",
            "missing back\t   \n",
            "too\tmany\tcols\there\textra\n",
            "ok\tback\tbad tag;drop\n",
        ]
        cards, _, errors = build_apkg.parse_inbox(lines)
        self.assertEqual(cards, [])
        self.assertEqual(len(errors), 5)

    def test_two_and_three_column_rows(self) -> None:
        lines = [
            "die Pfütze\tpuddle\n",
            "abwägen\tto weigh up\tgerman verb\n",
        ]
        cards, dupes, errors = build_apkg.parse_inbox(lines, default_deck="Anki-Inbox")
        self.assertEqual(errors, [])
        self.assertEqual(dupes, [])
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0].tags, ())
        self.assertEqual(cards[0].deck, "Anki-Inbox")
        self.assertEqual(cards[1].tags, ("german", "verb"))
        self.assertEqual(cards[1].deck, "Anki-Inbox")

    def test_deterministic_deck_ids(self) -> None:
        d1 = build_apkg.stable_deck_id("Anki-Inbox::German")
        d2 = build_apkg.stable_deck_id("Anki-Inbox::German")
        d3 = build_apkg.stable_deck_id("Anki-Inbox::English")
        self.assertEqual(d1, d2)
        self.assertNotEqual(d1, d3)
        self.assertGreaterEqual(d1, 1 << 30)
        self.assertLess(d1, 1 << 31)

    @unittest.skipUnless(
        importlib.util.find_spec("genanki") is not None,
        "genanki not installed locally",
    )
    def test_apkg_contains_stable_guids(self) -> None:
        lines = [
            "der Schneematsch\tslush\tgerman\tAnki-Inbox::German\n",
            "etwas auf dem Schirm haben\ton one's radar\tgerman idiom\tAnki-Inbox::German\n",
        ]
        cards, _, errors = build_apkg.parse_inbox(lines)
        self.assertEqual(errors, [])

        with tempfile.TemporaryDirectory() as tmp:
            apkg_path = Path(tmp) / "test.apkg"
            build_apkg.build_apkg(cards, apkg_path)
            self.assertTrue(apkg_path.is_file())

            with zipfile.ZipFile(apkg_path, "r") as zf:
                db_name = "collection.anki21" if "collection.anki21" in zf.namelist() else "collection.anki2"
                db_path = Path(tmp) / db_name
                db_path.write_bytes(zf.read(db_name))

            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute("SELECT guid, flds, tags FROM notes").fetchall()
            finally:
                conn.close()

            guids = {row[0] for row in rows}
            self.assertEqual(guids, {c.guid for c in cards})


if __name__ == "__main__":
    unittest.main()
