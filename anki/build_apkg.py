#!/usr/bin/env python3
"""Validate, deduplicate, and build a deterministic Anki .apkg from an inbox TSV.

Design goals:
- Zero cloud LLM cost in CI: purely deterministic validation, sanitization,
  deduplication, and .apkg packaging.
- Stable note GUIDs derived from the normalized target expression (`front`),
  plus fixed Model ID and deterministic Deck IDs, so re-importing the generated
  .apkg into AnkiDroid or Desktop Anki updates existing notes in place instead
  of creating duplicates.
- Safe HTML handling: only a strict allowlist of basic inline/list formatting
  tags (without attributes) is preserved; everything else is HTML-escaped.
"""

from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
from dataclasses import dataclass
from pathlib import Path
import re
import sys
import unicodedata
from typing import Iterable, List, Optional, Sequence, Tuple

# Fixed 31-bit positive Model ID so Anki never creates duplicate note types.
MODEL_ID = 1607392319
MODEL_NAME = "Antonm Inbox Basic (v1)"

# Namespace prefixes keep GUIDs and Deck IDs collision-free and permanently stable.
GUID_NAMESPACE = "antonm-anki-v1"
DECK_ID_NAMESPACE = "antonm-anki-deck-v1"

# Exact 91-character table used by Anki / genanki for note GUIDs.
ANKI_BASE91_TABLE = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "!#$%&()*+,-./:;<=>?@[]^_`{|}~"
)

ALLOWED_HTML_TAGS = frozenset(
    {"b", "i", "u", "em", "strong", "code", "sub", "sup", "br", "ul", "ol", "li"}
)
VOID_HTML_TAGS = frozenset({"br"})

TAG_STRIP_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
SAFE_TAG_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:\-./]+$")

CARD_CSS = """\
.card {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 20px;
  line-height: 1.45;
  text-align: left;
  color: #1f2328;
  background-color: #ffffff;
  padding: 16px;
}
.nightMode .card {
  color: #e6edf3;
  background-color: #0d1117;
}
.front {
  font-size: 24px;
  font-weight: 600;
}
.back {
  font-size: 19px;
}
hr#answer {
  border: 0;
  border-top: 1px solid #d0d7de;
  margin: 14px 0;
}
.nightMode hr#answer {
  border-top-color: #30363d;
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.9em;
  background: rgba(127, 127, 127, 0.15);
  padding: 1px 4px;
  border-radius: 4px;
}
"""


@dataclass(frozen=True)
class Card:
    line_no: int
    front: str
    back: str
    tags: Tuple[str, ...]
    deck: str
    norm_key: str
    guid: str


@dataclass(frozen=True)
class DuplicateRecord:
    norm_key: str
    guid: str
    dropped_line: int
    kept_line: int
    front: str


class _SafeHtmlSanitizer(HTMLParser):
    """Allowlist HTML sanitizer that strips disallowed tags and all attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._suppress_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag_l = tag.lower()
        if tag_l in ("script", "style"):
            self._suppress_depth += 1
            return
        if self._suppress_depth > 0:
            return
        if tag_l in ALLOWED_HTML_TAGS:
            self._parts.append("<br>" if tag_l == "br" else f"<{tag_l}>")

    def handle_startendtag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        tag_l = tag.lower()
        if self._suppress_depth > 0:
            return
        if tag_l in VOID_HTML_TAGS:
            self._parts.append(f"<{tag_l}>")

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l in ("script", "style"):
            if self._suppress_depth > 0:
                self._suppress_depth -= 1
            return
        if self._suppress_depth > 0:
            return
        if tag_l in ALLOWED_HTML_TAGS and tag_l not in VOID_HTML_TAGS:
            self._parts.append(f"</{tag_l}>")

    def handle_data(self, data: str) -> None:
        if self._suppress_depth > 0:
            return
        self._parts.append(html.escape(data, quote=False))

    def get_html(self) -> str:
        return "".join(self._parts).strip()


def sanitize_html(raw: str) -> str:
    """Sanitize a card field, preserving only safe inline formatting tags."""
    parser = _SafeHtmlSanitizer()
    parser.feed(raw)
    parser.close()
    return parser.get_html()


def normalize_key(front: str) -> str:
    """Return a canonical key for the target expression used for GUID & deduping."""
    # Unescape HTML entities, strip formatting tags, normalize Unicode & case.
    plain = html.unescape(TAG_STRIP_RE.sub(" ", front))
    normalized = unicodedata.normalize("NFKC", plain)
    collapsed = WHITESPACE_RE.sub(" ", normalized).strip()
    return collapsed.casefold()


def encode_base91(value: int) -> str:
    """Encode a non-negative integer using Anki's base91 table."""
    if value == 0:
        return ANKI_BASE91_TABLE[0]
    chars: List[str] = []
    base = len(ANKI_BASE91_TABLE)
    while value > 0:
        value, rem = divmod(value, base)
        chars.append(ANKI_BASE91_TABLE[rem])
    chars.reverse()
    return "".join(chars)


def stable_guid(norm_key: str) -> str:
    """Compute a permanently stable, Anki-native note GUID from a normalized key."""
    digest = hashlib.sha256(f"{GUID_NAMESPACE}\x00{norm_key}".encode("utf-8")).digest()
    hash_int = int.from_bytes(digest[:8], "big")
    return encode_base91(hash_int)


def stable_deck_id(deck_name: str) -> int:
    """Compute a deterministic 31-bit positive Anki Deck ID for a deck name."""
    digest = hashlib.sha256(
        f"{DECK_ID_NAMESPACE}\x00{deck_name}".encode("utf-8")
    ).digest()
    raw = int.from_bytes(digest[:4], "big")
    # Keep strictly inside [1 << 30, (1 << 31) - 1] (always positive signed 32-bit).
    return (1 << 30) | (raw & ((1 << 30) - 1))


def parse_tags(raw_tags: str, line_no: int) -> Tuple[str, ...]:
    """Parse and validate space-separated Anki tags, preserving order."""
    seen = set()
    result: List[str] = []
    for token in raw_tags.split():
        if not SAFE_TAG_TOKEN_RE.match(token):
            raise ValueError(
                f"Line {line_no}: invalid tag {token!r} "
                "(allowed: letters, digits, _, :, -, ., /)"
            )
        if token not in seen:
            seen.add(token)
            result.append(token)
    return tuple(result)


def parse_inbox(
    lines: Iterable[str],
    default_deck: str = "Anki-Inbox",
) -> Tuple[List[Card], List[DuplicateRecord], List[str]]:
    """Parse, validate, sanitize, and deduplicate TSV inbox lines.

    Duplicate normalized fronts use last-write-wins so appending a revised card
    to the bottom of inbox.tsv cleanly updates the earlier entry.

    Returns (cards, duplicates, errors).
    """
    errors: List[str] = []
    parsed: List[Card] = []
    header_checked = False

    for idx, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\r\n")
        if idx == 1 and line.startswith("\ufeff"):
            line = line[1:]

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        parts = line.split("\t")
        if not header_checked:
            header_checked = True
            lower_parts = [p.strip().lower() for p in parts]
            if len(lower_parts) >= 2 and lower_parts[0] == "front" and lower_parts[1] == "back":
                continue

        if len(parts) < 2 or len(parts) > 4:
            errors.append(
                f"Line {idx}: expected 2 to 4 tab-separated columns "
                f"(front, back[, tags[, deck]]), found {len(parts)}"
            )
            continue

        raw_front = parts[0].strip()
        raw_back = parts[1].strip()
        raw_tags = parts[2].strip() if len(parts) >= 3 else ""
        raw_deck = parts[3].strip() if len(parts) >= 4 else ""

        front = sanitize_html(raw_front)
        back = sanitize_html(raw_back)
        norm_key = normalize_key(front)

        if not norm_key:
            errors.append(f"Line {idx}: `front` is empty after sanitization")
            continue
        if not normalize_key(back):
            errors.append(f"Line {idx}: `back` is empty after sanitization")
            continue

        try:
            tags = parse_tags(raw_tags, idx)
        except ValueError as exc:
            errors.append(str(exc))
            continue

        deck = raw_deck or default_deck
        if not deck.strip():
            errors.append(f"Line {idx}: `deck` name cannot be empty")
            continue

        parsed.append(
            Card(
                line_no=idx,
                front=front,
                back=back,
                tags=tags,
                deck=deck,
                norm_key=norm_key,
                guid=stable_guid(norm_key),
            )
        )

    # Deduplicate by normalized front (last-write-wins), preserving final order.
    last_index_by_key = {}
    for pos, card in enumerate(parsed):
        last_index_by_key[card.norm_key] = pos

    deduped: List[Card] = []
    duplicates: List[DuplicateRecord] = []
    for pos, card in enumerate(parsed):
        kept_pos = last_index_by_key[card.norm_key]
        if pos == kept_pos:
            deduped.append(card)
        else:
            duplicates.append(
                DuplicateRecord(
                    norm_key=card.norm_key,
                    guid=card.guid,
                    dropped_line=card.line_no,
                    kept_line=parsed[kept_pos].line_no,
                    front=card.front,
                )
            )

    return deduped, duplicates, errors


def build_apkg(cards: Sequence[Card], output_path: Path) -> None:
    """Write a deterministic .apkg file using genanki."""
    try:
        import genanki  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "genanki is required to build .apkg files. "
            "Install with: pip install -r anki/requirements.txt"
        ) from exc

    model = genanki.Model(
        MODEL_ID,
        MODEL_NAME,
        fields=[
            {"name": "Front"},
            {"name": "Back"},
        ],
        templates=[
            {
                "name": "Card 1",
                "qfmt": '<div class="front">{{Front}}</div>',
                "afmt": (
                    '<div class="front">{{FrontSide}}</div>'
                    '<hr id="answer">'
                    '<div class="back">{{Back}}</div>'
                ),
            }
        ],
        css=CARD_CSS,
    )

    decks_by_name = {}
    for card in cards:
        deck = decks_by_name.get(card.deck)
        if deck is None:
            deck = genanki.Deck(stable_deck_id(card.deck), card.deck)
            decks_by_name[card.deck] = deck
        note = genanki.Note(
            model=model,
            fields=[card.front, card.back],
            tags=list(card.tags),
            guid=card.guid,
        )
        deck.add_note(note)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    package = genanki.Package(list(decks_by_name.values()))
    package.write_to_file(str(output_path))


def write_clean_tsv(cards: Sequence[Card], output_path: Path) -> None:
    """Write a clean, deduplicated 4-column TSV alongside the .apkg."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("front\tback\ttags\tdeck\n")
        for card in cards:
            tags_str = " ".join(card.tags)
            fh.write(f"{card.front}\t{card.back}\t{tags_str}\t{card.deck}\n")


def format_summary_markdown(
    cards: Sequence[Card],
    duplicates: Sequence[DuplicateRecord],
    output_path: Optional[Path],
) -> str:
    """Build a concise Markdown summary for GitHub Actions step summary."""
    deck_counts = {}
    for card in cards:
        deck_counts[card.deck] = deck_counts.get(card.deck, 0) + 1

    lines = [
        "## Anki Deck Build Summary",
        "",
        f"- **Unique cards:** {len(cards)}",
        f"- **Decks:** {len(deck_counts)}",
        f"- **In-file duplicates merged (last-write-wins):** {len(duplicates)}",
    ]
    if output_path is not None:
        lines.append(f"- **Package:** `{output_path.name}`")
    lines.append("")

    if deck_counts:
        lines.append("### Decks")
        lines.append("")
        lines.append("| Deck | Cards | Stable Deck ID |")
        lines.append("| --- | ---: | ---: |")
        for deck_name in sorted(deck_counts):
            lines.append(
                f"| `{deck_name}` | {deck_counts[deck_name]} | `{stable_deck_id(deck_name)}` |"
            )
        lines.append("")

    if duplicates:
        lines.append("### Merged Duplicates")
        lines.append("")
        for dup in duplicates:
            lines.append(
                f"- `{dup.norm_key}` (`{dup.guid}`): line {dup.dropped_line} "
                f"replaced by line {dup.kept_line}"
            )
        lines.append("")

    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate, deduplicate, and package anki/inbox.tsv into an .apkg."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("anki/inbox.tsv"),
        help="Path to the tab-separated inbox file (default: anki/inbox.tsv)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist/antonm-anki.apkg"),
        help="Output .apkg path (ignored with --check)",
    )
    parser.add_argument(
        "--default-deck",
        default="Anki-Inbox",
        help="Default Anki deck name when column 4 is omitted",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate and deduplicate only without building an .apkg",
    )
    parser.add_argument(
        "--strict-dupes",
        action="store_true",
        help="Exit non-zero if duplicate target expressions exist in the inbox",
    )
    parser.add_argument(
        "--clean-tsv",
        type=Path,
        default=None,
        help="Optional path to write a deduplicated TSV copy",
    )
    parser.add_argument(
        "--summary-md",
        type=Path,
        default=None,
        help="Optional path to append a Markdown summary (e.g. $GITHUB_STEP_SUMMARY)",
    )
    args = parser.parse_args(argv)

    if not args.input.is_file():
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        return 1

    with args.input.open("r", encoding="utf-8") as fh:
        cards, duplicates, errors = parse_inbox(fh, default_deck=args.default_deck)

    if errors:
        print(f"Validation failed with {len(errors)} error(s):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    if not cards:
        print("ERROR: Inbox contains 0 valid cards.", file=sys.stderr)
        return 1

    if duplicates:
        print(
            f"Merged {len(duplicates)} duplicate card(s) (last-write-wins):",
            file=sys.stderr,
        )
        for dup in duplicates:
            print(
                f"  - line {dup.dropped_line} -> line {dup.kept_line}: "
                f"{dup.norm_key!r} (guid={dup.guid})",
                file=sys.stderr,
            )
        if args.strict_dupes:
            print("ERROR: --strict-dupes is enabled.", file=sys.stderr)
            return 1

    output_written: Optional[Path] = None
    if not args.check:
        build_apkg(cards, args.output)
        output_written = args.output
        print(f"Built {args.output} ({len(cards)} unique cards).")
    else:
        print(f"OK: {len(cards)} unique cards validated ({len(duplicates)} merged).")

    if args.clean_tsv is not None:
        write_clean_tsv(cards, args.clean_tsv)
        print(f"Wrote clean TSV to {args.clean_tsv}.")

    if args.summary_md is not None:
        summary = format_summary_markdown(cards, duplicates, output_written)
        args.summary_md.parent.mkdir(parents=True, exist_ok=True)
        with args.summary_md.open("a", encoding="utf-8") as fh:
            fh.write(summary + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
