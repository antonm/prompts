# Swiss Price Watcher

Zero-dependency Python 3 price & deal watcher with urgent 1-tap `ntfy.sh` push alerts and optional on-device macOS `fm` CLI triage.

## GitHub Actions Setup (Free, 24/7)
1. In your GitHub repository: **Settings → Secrets and variables → Actions → New repository secret**.
2. Add `NTFY_TOPIC` with your private `ntfy.sh` topic name (never commit your topic into `config.json`).
3. Enable the **Swiss Price Watcher** workflow under the **Actions** tab (runs every 30 min; also supports manual *Run workflow*).

## Local macOS Usage (with optional Apple `fm` CLI triage)
```bash
# Dry-run
python3 price-watcher/watcher.py --dry-run

# Test urgent 1-tap phone push (requires NTFY_TOPIC env var)
NTFY_TOPIC="your-private-topic" python3 price-watcher/watcher.py --test-notify
```

## Offline Anki Card Helper (macOS `fm` CLI)
```bash
price-watcher/fm_anki.sh "der Schneematsch" >> cards.tsv
```
