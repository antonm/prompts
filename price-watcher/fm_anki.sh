#!/bin/bash
# Generate a structured Anki flashcard (TSV) using Apple's on-device `fm` CLI (0 cloud tokens, works offline).
# Usage: ./fm_anki.sh "der Schneematsch"
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
INPUT="${1:-}"
if [ -z "$INPUT" ]; then
  echo "Usage: $0 \"word, phrase, or concept\"" >&2
  exit 1
fi

JSON_OUT="$(/usr/bin/fm respond --model system --no-stream --greedy \
  --schema "$DIR/anki_schema.json" \
  -i "Create a concise Anki flashcard for a German/English learner. Put the prompt/term on front, clear meaning/grammar note on back, a natural example sentence in example, and space-separated lowercase tags in tags." \
  "$INPUT")"

python3 -c '
import json, sys
d = json.loads(sys.argv[1])
clean = lambda s: str(s).replace("\t", " ").replace("\n", " ").strip()
print(f"{clean(d[\"front\"])}\t{clean(d[\"back\"])}<br><i>{clean(d[\"example\"])}</i>\t{clean(d[\"tags\"])}")
' "$JSON_OUT"
