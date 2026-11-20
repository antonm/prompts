#!/usr/bin/env python3
"""
Local zero-cost Swiss price & deal watcher for macOS (launchd).

Monitors:
  1. Direct Swiss product pages (JSON-LD Product/Offer or structured price fields)
     with exact size/variant URLs and stock verification.
  2. Swiss community deal RSS feeds (e.g. Preispirat.ch) filtered by keywords
     so Black Friday / flash deals across Digitec, Galaxus, Brack, Interdiscount,
     Fust, Transa, Bergfreunde, etc. trigger instant alerts.

Only notifies via native macOS Notification Center when:
  - A tracked product is IN STOCK and at or below its target CHF price
    (and hasn't already been alerted at the same or lower price), OR
  - A new matching deal post appears in a tracked Swiss RSS feed.

Zero external dependencies (Python 3.9+ standard library only).
"""

import argparse
import html
import json
import logging
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
DEAL_SCHEMA_PATH = BASE_DIR / "deal_schema.json"
FM_BIN = Path("/usr/bin/fm")
LOG_DIR = BASE_DIR / "logs"
LOG_PATH = LOG_DIR / "watcher.log"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class Redirect308Handler(urllib.request.HTTPRedirectHandler):
    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_302(req, fp, code, msg, headers)


def build_opener() -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        Redirect308Handler(),
    )


OPENER = build_opener()


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Trim log if it exceeds 512 KB to keep disk usage tiny
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > 512 * 1024:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
        LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    handlers: List[logging.Handler] = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
    if verbose or sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def fetch_url(url: str, timeout: int = 15) -> Tuple[int, str, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
        },
    )
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, resp.geturl(), body
    except urllib.error.HTTPError as exc:
        return exc.code, url, ""
    except Exception as exc:
        logging.warning("Fetch error for %s: %s", url, exc)
        return -1, url, ""


def parse_chf_number(raw: Any) -> Optional[float]:
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().replace("'", "").replace("’", "").replace(" ", "")
    cleaned = cleaned.replace(".–", "").replace(".-", "").rstrip(",")
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", cleaned)
    return float(m.group(0)) if m else None


def parse_jsonld_product(body: str) -> Optional[Dict[str, Any]]:
    """Extract price, currency, and availability from schema.org Product JSON-LD."""
    pattern = re.compile(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.S | re.I,
    )
    for m in pattern.finditer(body):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "Product":
                continue
            offers = item.get("offers")
            if isinstance(offers, list) and offers:
                offers = offers[0]
            if not isinstance(offers, dict):
                continue
            price = parse_chf_number(offers.get("price") or offers.get("lowPrice"))
            if price is None:
                continue
            avail = str(offers.get("availability", ""))
            in_stock = (
                "InStock" in avail
                or "LimitedAvailability" in avail
                or "OnlineOnly" in avail
                or avail == ""
            )
            if "OutOfStock" in avail or "SoldOut" in avail or "Discontinued" in avail:
                in_stock = False
            return {
                "price": price,
                "currency": offers.get("priceCurrency", "CHF"),
                "in_stock": in_stock,
                "availability": avail,
                "title": item.get("name", ""),
            }
    return None


def parse_heiniger_product(body: str) -> Optional[Dict[str, Any]]:
    """Extract CHF price and stock status from shop.heinigerag.ch product page."""
    # Heiniger embeds `"price": 899,` or `"price": 1099,` in product JS/JSON data
    m = re.search(r'"price"\s*:\s*(\d+(?:\.\d+)?)', body)
    if not m:
        return None
    price = float(m.group(1))
    lower = body.lower()
    out_of_stock = any(
        phrase in lower
        for phrase in ("nicht verfügbar", "ausverkauft", "nicht mehr lieferbar", "out of stock")
    )
    return {
        "price": price,
        "currency": "CHF",
        "in_stock": not out_of_stock,
        "availability": "OutOfStock" if out_of_stock else "InStock",
    }


def parse_regex_product(body: str, pattern: str, in_stock_marker: Optional[str] = None) -> Optional[Dict[str, Any]]:
    m = re.search(pattern, body, re.S | re.I)
    if not m:
        return None
    price = parse_chf_number(m.group(1))
    if price is None:
        return None
    in_stock = True
    if in_stock_marker:
        in_stock = bool(re.search(in_stock_marker, body, re.S | re.I))
    return {
        "price": price,
        "currency": "CHF",
        "in_stock": in_stock,
        "availability": "InStock" if in_stock else "Unknown/OutOfStock",
    }


def send_macos_notification(title: str, subtitle: str, message: str, sound: str = "Glass") -> None:
    """Send a native macOS Notification Center alert via osascript (no shell interpolation)."""
    script = (
        "on run argv\n"
        "  set t to item 1 of argv\n"
        "  set s to item 2 of argv\n"
        "  set m to item 3 of argv\n"
        "  set snd to item 4 of argv\n"
        "  display notification m with title t subtitle s sound name snd\n"
        "end run\n"
    )
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", script, "--", title, subtitle, message, sound],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        logging.error("Failed to send macOS notification: %s", exc)


def send_ntfy_notification(
    ntfy_cfg: Dict[str, Any],
    title: str,
    message: str,
    click_url: Optional[str] = None,
) -> None:
    """Send a free phone push notification via ntfy.sh (zero credentials needed)."""
    if not ntfy_cfg or not ntfy_cfg.get("enabled"):
        return
    topic = (os.environ.get("NTFY_TOPIC") or ntfy_cfg.get("topic") or "").strip()
    if not topic:
        return
    server = (ntfy_cfg.get("server") or "https://ntfy.sh").rstrip("/")
    url = f"{server}/{topic}"
    # HTTP headers must be ASCII/Latin-1 safe
    safe_title = title.encode("ascii", "replace").decode("ascii")
    headers = {
        "Title": safe_title,
        "Priority": str(ntfy_cfg.get("priority", "high")),
        "Tags": str(ntfy_cfg.get("tags", "shopping_bags")),
        "User-Agent": USER_AGENT,
    }
    if click_url:
        headers["Click"] = click_url
        headers["Actions"] = f"view, Open Deal, {click_url}"
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with OPENER.open(req, timeout=10) as resp:
            logging.info("Sent ntfy push (%s) to %s", resp.status, url)
    except Exception as exc:
        logging.error("Failed to send ntfy notification: %s", exc)


def notify_all(
    config: Dict[str, Any],
    title: str,
    subtitle: str,
    message: str,
    click_url: Optional[str] = None,
) -> None:
    notif_cfg = config.get("notifications", {})
    if notif_cfg.get("macos", {}).get("enabled", True):
        send_macos_notification(title=title, subtitle=subtitle, message=message)
    ntfy_body = f"{subtitle}\n{message}" if subtitle else message
    send_ntfy_notification(
        notif_cfg.get("ntfy", {}),
        title=title,
        message=ntfy_body,
        click_url=click_url,
    )


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.warning("Could not parse %s (%s); using default.", path, exc)
        return default


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def check_product_watch(
    watch: Dict[str, Any],
    config: Dict[str, Any],
    state: Dict[str, Any],
    dry_run: bool,
) -> Optional[Dict[str, Any]]:
    wid = watch["id"]
    name = watch["name"]
    url = watch["url"]
    parser = watch.get("parser", "jsonld")
    target_chf = float(watch["target_chf"])
    require_in_stock = bool(watch.get("require_in_stock", True))

    status, final_url, body = fetch_url(url)
    if status != 200 or not body:
        logging.warning("[%s] HTTP %s for %s", wid, status, url)
        return {"id": wid, "name": name, "status": f"HTTP {status}", "url": url}

    if any(k in body for k in ("Just a moment", "cf-browser-verification", "Quick check")):
        logging.warning("[%s] Skipped (bot-check page) for %s", wid, url)
        return {"id": wid, "name": name, "status": "bot-check", "url": url}

    if parser == "jsonld":
        parsed = parse_jsonld_product(body)
    elif parser == "heiniger":
        parsed = parse_heiniger_product(body)
    elif parser == "regex":
        parsed = parse_regex_product(body, watch["price_regex"], watch.get("in_stock_regex"))
    else:
        logging.error("[%s] Unknown parser %s", wid, parser)
        return None

    if not parsed:
        logging.warning("[%s] Could not parse price from %s", wid, final_url)
        return {"id": wid, "name": name, "status": "parse-error", "url": final_url}

    price = parsed["price"]
    in_stock = parsed["in_stock"]
    currency = parsed.get("currency", "CHF")
    now_iso = datetime.now(timezone.utc).isoformat()

    products_state = state.setdefault("products", {})
    prev = products_state.get(wid, {})
    last_alerted_price = prev.get("last_alerted_price")

    is_deal = price <= target_chf and (in_stock or not require_in_stock)
    should_notify = is_deal and (last_alerted_price is None or price < float(last_alerted_price) - 0.5)

    logging.info(
        "[%s] %s: %s %.2f (target <= %.2f) | in_stock=%s | deal=%s",
        wid, name, currency, price, target_chf, in_stock, is_deal,
    )

    if should_notify and not dry_run:
        notify_all(
            config=config,
            title=f"Deal Alert: {name}",
            subtitle=f"{currency} {price:.2f} (target <= {target_chf:.0f})",
            message=f"{'In stock' if in_stock else 'Check stock'} — {final_url}",
            click_url=final_url,
        )
        prev["last_alerted_price"] = price
        prev["last_alerted_at"] = now_iso

    if not dry_run:
        prev["last_price"] = price
        prev["last_in_stock"] = in_stock
        prev["last_checked_at"] = now_iso
        prev["url"] = final_url
        products_state[wid] = prev

    return {
        "id": wid,
        "name": name,
        "price": price,
        "currency": currency,
        "target_chf": target_chf,
        "in_stock": in_stock,
        "is_deal": is_deal,
        "notified": should_notify and not dry_run,
        "url": final_url,
    }


def matches_rss_rule(text: str, rule: Dict[str, Any]) -> bool:
    hay = text.lower()
    for term in rule.get("must_include", []):
        if term.lower() not in hay:
            return False
    any_terms = rule.get("include_any", [])
    if any_terms and not any(t.lower() in hay for t in any_terms):
        return False
    for bad in rule.get("exclude", []):
        if bad.lower() in hay:
            return False
    return True


def is_recent_rss_item(pub_date: str, max_age_hours: int = 48) -> bool:
    if not pub_date:
        return True
    try:
        dt = parsedate_to_datetime(pub_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return age <= timedelta(hours=max_age_hours)
    except Exception:
        return True


def triage_rss_deal_with_fm(
    rule: Dict[str, Any],
    title: str,
    desc: str,
    config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Use Apple's on-device `fm` CLI (0 cloud tokens) to verify deal relevance & price.

    Returns parsed DealTriage JSON dict on success, or None if `fm` is unavailable
    (e.g. on Linux / GitHub Actions) so callers fall back gracefully to keyword rules.
    """
    fm_cfg = config.get("fm_triage", {})
    if not fm_cfg.get("enabled", True):
        return None
    if not FM_BIN.exists() or not DEAL_SCHEMA_PATH.exists():
        return None

    criteria = rule.get("fm_criteria") or rule.get("name", "")
    snippet = re.sub(r"\s+", " ", f"{title}. {desc}").strip()[:1400]
    instructions = (
        "Evaluate whether this Swiss deal matches the user's target criteria. "
        "Set relevant=false for accessories, wrong gender/size, or wrong generation. "
        "Set brand_new=false for open-box, returns, used, or refurbished items. "
        "Set price_chf to the effective CHF integer price (0 if no CHF price is stated). "
        "Set meets_target=true if price_chf is 0 (price unknown) OR price_chf meets the target. "
        "Keep summary under 100 characters in English."
    )
    prompt = f"Target criteria: {criteria}\nDeal text: {snippet}"

    try:
        proc = subprocess.run(
            [
                str(FM_BIN),
                "respond",
                "--model",
                "system",
                "--no-stream",
                "--greedy",
                "--schema",
                str(DEAL_SCHEMA_PATH),
                "-i",
                instructions,
                prompt,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=25,
        )
        data = json.loads(proc.stdout.strip())
        if isinstance(data, dict) and "relevant" in data:
            return data
    except Exception as exc:
        logging.debug("On-device fm triage skipped/failed (falling back to keywords): %s", exc)
    return None


def check_rss_feeds(
    feeds: List[Dict[str, Any]],
    rules: List[Dict[str, Any]],
    config: Dict[str, Any],
    state: Dict[str, Any],
    dry_run: bool,
) -> List[Dict[str, Any]]:
    seen_links: Dict[str, str] = state.setdefault("seen_rss_links", {})
    # Keep seen_links bounded to last 500 entries
    if len(seen_links) > 500:
        recent_items = sorted(seen_links.items(), key=lambda kv: kv[1], reverse=True)[:400]
        seen_links.clear()
        seen_links.update(recent_items)

    matches: List[Dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for feed in feeds:
        fid = feed["id"]
        furl = feed["url"]
        max_age_hours = int(feed.get("max_age_hours", 48))
        status, _, body = fetch_url(furl)
        if status != 200 or not body:
            logging.warning("[rss:%s] HTTP %s for %s", fid, status, furl)
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            logging.warning("[rss:%s] XML parse error: %s", fid, exc)
            continue

        for item in root.findall(".//item"):
            title = html.unescape((item.findtext("title") or "").strip())
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            desc = html.unescape(re.sub(r"<[^>]+>", " ", item.findtext("description") or ""))
            combined = f"{title} {desc}"

            if not is_recent_rss_item(pub_date, max_age_hours=max_age_hours):
                continue

            for rule in rules:
                if not matches_rss_rule(combined, rule):
                    continue
                already_seen = link in seen_links
                fm_result = None
                if not already_seen:
                    fm_result = triage_rss_deal_with_fm(rule, title, desc, config)
                    if fm_result is not None:
                        logging.info("[rss:%s] fm triage (%s): %s", fid, rule["id"], fm_result)
                        if (
                            not fm_result.get("relevant", True)
                            or not fm_result.get("brand_new", True)
                            or (fm_result.get("price_chf", 0) > 0 and not fm_result.get("meets_target", True))
                        ):
                            logging.info("[rss:%s] Filtered out by on-device fm: %s", fid, title)
                            if not dry_run:
                                seen_links[link] = now_iso
                            break

                display_subtitle = (
                    (fm_result.get("summary") or title) if fm_result else title
                )[:100]

                logging.info(
                    "[rss:%s] MATCH (%s) seen=%s: %s | %s",
                    fid, rule["id"], already_seen, display_subtitle, link,
                )
                matches.append({
                    "feed": fid,
                    "rule": rule["name"],
                    "title": display_subtitle,
                    "pub_date": pub_date,
                    "link": link,
                    "new": not already_seen,
                })
                if not already_seen and not dry_run:
                    notify_all(
                        config=config,
                        title=f"Swiss Deal ({rule['name']})",
                        subtitle=display_subtitle[:80],
                        message=link,
                        click_url=link,
                    )
                    seen_links[link] = now_iso
                break

    return matches


def main() -> int:
    parser = argparse.ArgumentParser(description="Local macOS Swiss price watcher.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="Path to config.json")
    parser.add_argument("--dry-run", action="store_true", help="Check prices without notifying or saving state")
    parser.add_argument("--test-notify", action="store_true", help="Send a test macOS notification and exit")
    parser.add_argument("--verbose", action="store_true", help="Print detailed logs")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose or args.dry_run)

    config = load_json(args.config, None)
    if not config:
        logging.error("Missing or invalid config file: %s", args.config)
        return 1

    if args.test_notify:
        notify_all(
            config=config,
            title="Swiss Price Watcher",
            subtitle="Test alert (1-tap buy link)",
            message="Mac + ntfy urgent phone alerts are live! Tap 'Open Deal' to test.",
            click_url="https://www.digitec.ch/en/s1/product/apple-mac-mini-2026-256-gb-16-gb-m6-apple-m6-12-core-pc-76106105",
        )
        ntfy_topic = config.get("notifications", {}).get("ntfy", {}).get("topic", "")
        print(f"Sent test macOS notification + ntfy push (topic: {ntfy_topic}).")
        return 0

    state = load_json(STATE_PATH, {"products": {}, "seen_rss_links": {}})

    product_results = []
    for watch in config.get("products", []):
        if not watch.get("enabled", True):
            continue
        res = check_product_watch(watch, config, state, dry_run=args.dry_run)
        if res:
            product_results.append(res)

    rss_matches = check_rss_feeds(
        config.get("rss_feeds", []),
        config.get("rss_rules", []),
        config,
        state,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        save_json(STATE_PATH, state)

    if args.dry_run or args.verbose:
        print("\n=== Product Summary ===")
        for r in product_results:
            if "price" in r:
                flag = "DEAL!" if r["is_deal"] else "ok"
                stock = "InStock" if r["in_stock"] else "OutOfStock"
                print(
                    f"  [{flag:5s}] {r['name']}: {r['currency']} {r['price']:.2f} "
                    f"(target <= {r['target_chf']:.0f}) [{stock}] -> {r['url']}"
                )
            else:
                print(f"  [ERR  ] {r['name']}: {r['status']} -> {r['url']}")
        if rss_matches:
            print("\n=== Matching RSS Deals ===")
            for m in rss_matches:
                tag = "NEW" if m["new"] else "SEEN"
                print(f"  [{tag:4s}] ({m['rule']}) {m['title']} ({m['pub_date']}) -> {m['link']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
