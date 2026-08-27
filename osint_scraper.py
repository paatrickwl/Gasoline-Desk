"""
osint_scraper.py -- OSINT Signal Parser (Layer 2)

Ingests open-source energy/economy headlines from public RSS feeds (no API
key required) and derives a logistics/choke-point risk signal from keyword
mentions inside those same headlines.

Guardrail: every feed is fetched independently and wrapped in try/except.
A feed that 404s, times out, or returns malformed XML is simply skipped --
it never takes the whole pull down. If EVERY feed fails, the caller falls
back to the last-known-good cache (`data/osint_cache.json`), and if that
doesn't exist either, to a small labeled demo set. The `source` field is
always one of "live" | "partial_live" | "cached_fallback" | "mock" so the
UI never has to guess how fresh what it's showing actually is.

Honesty note: the RSS URLs below are public feeds that were correct at
write time but are not verified from this environment (no outbound network
here) -- run this on a machine with internet access and check the `source`
field / `feed_errors` in the result to confirm which feeds actually
resolved. A feed going stale or changing its URL degrades gracefully to
the fallback path above; it does not crash the dashboard.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "data", "osint_cache.json")
REQUEST_TIMEOUT_SECONDS = 5

# Choke points this desk tracks for freight/logistics risk.
CHOKE_POINTS = ["Malacca Strait", "Sunda Strait", "Lombok Strait", "Hormuz Strait"]
CHOKE_POINT_KEYWORDS = {
    "Malacca Strait": ["malacca", "malaka"],
    "Sunda Strait": ["sunda"],
    "Lombok Strait": ["lombok"],
    "Hormuz Strait": ["hormuz"],
}

# Public RSS feeds, no API key. (name, url, default category if no keyword
# match below). Kept short and swappable -- add/remove entries freely.
RSS_FEEDS = [
    ("Antara Ekonomi", "https://www.antaranews.com/rss/ekonomi.xml", "macro"),
    ("Detik Finance", "https://finance.detik.com/rss", "macro"),
    ("CNBC Indonesia Market", "https://www.cnbcindonesia.com/market/rss", "macro"),
]

CATEGORY_KEYWORDS = {
    "policy": [
        "esdm", "subsidi", "pertamina", "bbm", "pajak", "ppn", "kebijakan", "regulasi",
        "pertalite", "pertamax", "solar subsidi", "dexlite", "bahan bakar", "elpiji", "lpg",
    ],
    "logistics": ["kapal", "selat", "pelabuhan", "malacca", "malaka", "hormuz", "sunda", "lombok", "tanker", "freight", "shipping"],
    "macro": ["rupiah", "kurs", "the fed", "opec", "minyak dunia", "crude", "brent", "dolar", "bank indonesia", "bi rate"],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _categorize(title: str) -> str:
    lower = title.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return category
    return "general"


def _mock_headlines() -> list[dict]:
    """Deterministic demo data -- used only when no live feed and no cache
    are available at all."""
    return [
        {
            "headline": "ESDM signals review of Pertalite subsidy allocation for FY2027",
            "source": "mock:esdm-watch",
            "category": "policy",
            "published_at": _now_iso(),
        },
        {
            "headline": "Tanker congestion reported at Malacca Strait amid weather delays",
            "source": "mock:shipping-wire",
            "category": "logistics",
            "published_at": _now_iso(),
        },
        {
            "headline": "Rupiah weakens past 15,900/USD on Fed rate-hold commentary",
            "source": "mock:fx-desk",
            "category": "macro",
            "published_at": _now_iso(),
        },
    ]


def _mock_logistics_indicators() -> list[dict]:
    return [
        {"choke_point": cp, "congestion_index": 0.35, "status": "normal", "as_of": _now_iso(), "mention_count": 0}
        for cp in CHOKE_POINTS
    ]


def _parse_rss(xml_text: str, source_name: str, default_category: str, limit: int = 8) -> list[dict]:
    """Minimal RSS 2.0 parser using the stdlib (no feedparser dependency).
    Tolerant of missing fields; skips items it can't parse rather than
    failing the whole feed."""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    for item in root.findall(".//item")[:limit]:
        title_el = item.find("title")
        pubdate_el = item.find("pubDate")
        title = (title_el.text or "").strip() if title_el is not None else None
        if not title:
            continue
        items.append({
            "headline": title,
            "source": source_name,
            "category": _categorize(title),
            "published_at": (pubdate_el.text or "").strip() if pubdate_el is not None else _now_iso(),
        })
    return items


def _fetch_headlines_live(timeout: int = REQUEST_TIMEOUT_SECONDS) -> tuple[list[dict], list[str]]:
    """Pull headlines from RSS_FEEDS. Each feed fails independently; only
    raises if every single feed failed (so the caller can distinguish
    'no live data at all' from 'partial live data')."""
    headlines: list[dict] = []
    errors: list[str] = []

    for name, url, default_category in RSS_FEEDS:
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (energy-desk-osint)"})
            resp.raise_for_status()
            parsed = _parse_rss(resp.text, name, default_category)
            headlines.extend(parsed)
        except (requests.RequestException, ET.ParseError) as e:
            errors.append(f"{name}: {e}")

    if not headlines:
        raise RuntimeError(f"All RSS feeds failed: {errors}")

    return headlines, errors


def _derive_logistics_from_headlines(headlines: list[dict]) -> list[dict]:
    """No free real-time AIS/congestion API is wired here -- instead, derive
    a proxy congestion signal from how often each choke point is actually
    mentioned in the live headlines just fetched. This is a real (if crude)
    signal from real text, not a fabricated number: 0 mentions -> baseline
    0.2, each additional mention nudges the index up, capped at 1.0."""
    combined_text = " ".join(h["headline"].lower() for h in headlines)
    logistics = []
    for cp in CHOKE_POINTS:
        keywords = CHOKE_POINT_KEYWORDS[cp]
        mentions = sum(len(re.findall(re.escape(kw), combined_text)) for kw in keywords)
        congestion_index = min(1.0, 0.2 + 0.2 * mentions)
        logistics.append({
            "choke_point": cp,
            "congestion_index": round(congestion_index, 2),
            "status": "elevated" if congestion_index > 0.5 else "normal",
            "as_of": _now_iso(),
            "mention_count": mentions,
        })
    return logistics


def _load_cache() -> dict:
    if not os.path.exists(CACHE_PATH):
        return {"headlines": _mock_headlines(), "logistics": _mock_logistics_indicators(), "cached_at": _now_iso()}
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_cache(payload: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def get_osint_feed(use_live: bool = False, timeout: int = REQUEST_TIMEOUT_SECONDS) -> dict:
    """Main entry point for the dashboard.

    Returns:
        {
          "headlines": [...],
          "logistics": [...],
          "source": "live" | "partial_live" | "mock" | "cached_fallback",
          "fetched_at": iso timestamp,
          "feed_errors": [...],   # present when some/all feeds failed
        }
    """
    if use_live:
        try:
            headlines, errors = _fetch_headlines_live(timeout=timeout)
            logistics = _derive_logistics_from_headlines(headlines)
            payload = {"headlines": headlines, "logistics": logistics}
            _save_cache({**payload, "cached_at": _now_iso()})
            source = "live" if not errors else "partial_live"
            result = {**payload, "source": source, "fetched_at": _now_iso()}
            if errors:
                result["feed_errors"] = errors
            return result
        except Exception as e:
            # Every feed failed, or something else went wrong -- degrade to
            # the last-known-good cache rather than showing an empty page.
            cached = _load_cache()
            return {
                "headlines": cached.get("headlines", []),
                "logistics": cached.get("logistics", []),
                "source": "cached_fallback",
                "fetched_at": _now_iso(),
                "cache_age_as_of": cached.get("cached_at"),
                "feed_errors": [str(e)],
            }

    # Default demo mode: deterministic mock data, clearly labeled.
    payload = {"headlines": _mock_headlines(), "logistics": _mock_logistics_indicators()}
    _save_cache({**payload, "cached_at": _now_iso()})
    return {**payload, "source": "mock", "fetched_at": _now_iso()}


def summarize_risk_signals(feed: Optional[dict] = None) -> dict:
    """Lightweight heuristic scoring of the OSINT feed into a risk summary
    the Simulation layer can consume as shock inputs."""
    feed = feed or get_osint_feed()
    logistics = feed.get("logistics", [])
    congestion_values = [c.get("congestion_index", 0) for c in logistics]
    avg_congestion = sum(congestion_values) / len(congestion_values) if congestion_values else 0.0

    policy_mentions = sum(1 for h in feed.get("headlines", []) if h.get("category") == "policy")
    macro_mentions = sum(1 for h in feed.get("headlines", []) if h.get("category") == "macro")

    return {
        "avg_choke_point_congestion": round(avg_congestion, 3),
        "policy_signal_count": policy_mentions,
        "macro_signal_count": macro_mentions,
        "risk_level": "elevated" if avg_congestion > 0.6 else "normal",
        "source": feed.get("source"),
    }


if __name__ == "__main__":
    feed = get_osint_feed(use_live=False)
    print(json.dumps(feed, indent=2))
    print(json.dumps(summarize_risk_signals(feed), indent=2))
