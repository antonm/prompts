# Anki Inbox Pipeline (Phase 1 — CHF 0/mo)

Zero-hardware, zero-API-cost pipeline that turns a single tab-separated inbox file ([inbox.tsv](inbox.tsv)) into a deterministic `.apkg` deck ready for **AnkiDroid** (and Desktop Anki).

## How It Works

1. **Add / edit cards in `anki/inbox.tsv`** — committed directly by Claude (from phone/web/CLI), edited in GitHub Mobile, or appended locally on macOS via Apple's on-device `/usr/bin/fm`:
   ```bash
   price-watcher/fm_anki.sh "der Schneematsch" >> anki/inbox.tsv
   ```
2. **GitHub Actions runs automatically** ([anki-build.yml](../.github/workflows/anki-build.yml)):
   - Validates syntax, required columns, tags, and safe inline HTML.
   - Deduplicates repeated target expressions (last-write-wins).
   - Assigns **permanently stable note GUIDs** (SHA-256 of the normalized `front` field encoded in Anki's base91 table), a fixed Model ID (`1607392319`), and deterministic Deck IDs.
   - Packages `dist/antonm-anki.apkg` and publishes it both as a workflow artifact and on the rolling `anki-latest` GitHub Release.
3. **Import on AnkiDroid:**
   - Download `antonm-anki.apkg` directly from the `anki-latest` release (no unzipping needed) and tap it to import.
   - AnkiDroid matches existing notes by GUID and **updates them in place** instead of creating duplicates, then syncs to AnkiWeb automatically.

## `inbox.tsv` Format

Tab-separated (`\t`), UTF-8, comments start with `#`:

```text
front	back	tags	deck
```

- **Column 1 (`front`):** Target word, phrase, or expression (used to derive the stable GUID).
- **Column 2 (`back`):** Meaning, grammar note, and example. Safe inline HTML (`<b>`, `<i>`, `<br>`, `<code>`, etc.) is preserved; scripts/attributes are stripped.
- **Column 3 (`tags`, optional):** Space-separated Anki tags (e.g. `german noun winter`).
- **Column 4 (`deck`, optional):** Target deck / subdeck (e.g. `Anki-Inbox::German`). Defaults to `Anki-Inbox` when omitted (matching `price-watcher/fm_anki.sh`'s 3-column output).

## Updating an Existing Card

Either edit the existing row in `anki/inbox.tsv` **or** append a revised row with the same `front` at the bottom. The builder keeps the last row and emits the exact same GUID, so re-importing updates the note in Anki.

## Local Validation & Build

Validate without installing `genanki`:

```bash
python3 anki/build_apkg.py --check
python3 -m unittest discover -s anki/tests -v
```

Build `.apkg` locally (or inside GitHub Codespaces):

```bash
pip install -r anki/requirements.txt
python3 anki/build_apkg.py
```
