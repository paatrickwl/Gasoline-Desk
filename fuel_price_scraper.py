"""
fuel_price_scraper.py -- Real retail fuel price ingestion (Pertamina, Shell,
BP-AKR, Vivo)

Source strategy (per explicit decision -- see project notes):
  * Pertamina  -> official Patra Niaga API (JSON, all 34 provinces + FTZ
                  zones, both Gasoline and Gasoil tabs). No headless browser
                  needed -- the page is a Next.js app whose data comes from
                  a plain JSON API discovered via the browser's network tab.
  * Shell      -> official Shell Indonesia AEM `.model.json` endpoint (every
                  AEM `.html` page has a matching `.model.json` mirror).
                  Shell Indonesia currently sells ONLY V-Power Diesel --
                  gasoline products are not listed because they are not
                  sold here, not because scraping failed.
  * BP-AKR,    -> no official structured price page exists for these two.
    Vivo          Falls back to a small WHITELIST of trusted news outlets
                  (currently: Metro TV News, which publishes a consistently
                  formatted per-brand price list and was cross-validated
                  against the two official sources above -- its Pertamina
                  and Shell numbers matched exactly). This is deliberately
                  NOT an open-ended Google-search scrape: unstructured
                  search-result scraping is fragile, likely against target
                  ToS, and prone to picking up stale or wrong numbers.
  * Cross-check -> Pertamina/Shell numbers from the news whitelist are also
                  compared against the official scrape; disagreements above
                  a small tolerance are surfaced as anomalies rather than
                  silently trusted.

Guardrail: every scraper function is independent and wrapped so one source
failing doesn't take down the others. The orchestrator `get_live_fuel_prices()`
degrades to `data/fuel_price_cache.json` (last-known-good) and, failing
that, to the static baseline catalog -- exactly the same pattern as
osint_scraper.py, so a stale/unreachable source never produces a fabricated
number, only a clearly-labeled fallback.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "data", "fuel_price_cache.json")
REQUEST_TIMEOUT_SECONDS = 10
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) energy-desk-fuel-price-scraper"}

PERTAMINA_API_URL = "https://pertaminapatraniaga.com/api/api/v1/post/get-by-slug/page/harga-terbaru-bbm?language=en"
SHELL_MODEL_JSON_URL = "https://www.shell.co.id/in_id/pengendara-bermotor/bahan-bakar-shell/harga-bahan-bakar-shell.model.json"

# Trusted news whitelist for brands with no official structured source
# (BP-AKR, Vivo) -- deliberately a short, curated list of outlets that
# publish a clean "Product: RpX per liter" list (not prose paragraphs,
# which are much easier to misparse -- e.g. Kompas.com covers the same
# story but embeds prices in sentences like "turun Rp 300 menjadi Rp
# 15.950 dari sebelumnya Rp 16.250", where a naive regex could grab the
# OLD price instead of the new one; deliberately excluded for that reason).
# Each outlet gets its OWN pattern set rather than one shared regex, since
# assuming every outlet phrases things identically is how you silently
# mismatch a value.
NEWS_WHITELIST = [
    {
        "name": "Metro TV News",
        "url": "https://www.metrotvnews.com/read/NG9CzX0m-daftar-harga-bbm-pertamina-shell-vivo-hingga-bp-24-agustus-2026",
        "patterns": {
            "pertamax_ron92": r"Pertamax \(RON 92\):\s*Rp([\d.,]+)",
            "pertamax_green95": r"Pertamax Green \(RON 95\):\s*Rp([\d.,]+)",
            "pertamax_turbo_ron98": r"Pertamax Turbo \(RON 98\):\s*Rp([\d.,]+)",
            "dexlite": r"Dexlite[^:]*:\s*Rp([\d.,]+)",
            "pertamina_dex": r"Pertamina Dex[^:]*:\s*Rp([\d.,]+)",
            "bp_92_ron92": r"BP 92:\s*Rp([\d.,]+)",
            "bp_ultimate_ron95": r"BP Ultimate:\s*Rp([\d.,]+)",
            "shell_vpower_diesel": r"Shell V-Power Diesel:\s*Rp([\d.,]+)",
            "vivo_revvo92": r"Revvo 92:\s*Rp([\d.,]+)",
            "vivo_revvo95": r"Revvo 95:\s*Rp([\d.,]+)",
        },
    },
    {
        "name": "CNBC Indonesia",
        "url": "https://www.cnbcindonesia.com/news/20260801083817-4-755645/daftar-harga-bbm-pertamina-shell-vivo-bp-1-agustus-banyak-yang-turun",
        "patterns": {
            # (?:Rp\s*)? because the source has at least one typo missing
            # "Rp" entirely ("Pertamax Turbo: 18.300 per liter") -- anchor
            # on "per liter" instead of requiring the currency prefix.
            "pertamax_ron92": r"(?<!Green )(?<!Turbo )Pertamax:\s*(?:Rp\s*)?([\d.,]+)\s*per liter",
            "pertamax_green95": r"Pertamax Green 95:\s*(?:Rp\s*)?([\d.,]+)\s*per liter",
            "pertamax_turbo_ron98": r"Pertamax Turbo:\s*(?:Rp\s*)?([\d.,]+)\s*per liter",
            "dexlite": r"Dexlite:\s*(?:Rp\s*)?([\d.,]+)\s*per liter",
            "pertamina_dex": r"Pertamina Dex:\s*(?:Rp\s*)?([\d.,]+)\s*per liter",
            "bp_92_ron92": r"BP 92:\s*(?:Rp\s*)?([\d.,]+)\s*per liter",
            "bp_ultimate_ron95": r"BP Ultimate:\s*(?:Rp\s*)?([\d.,]+)\s*per liter",
            "shell_vpower_diesel": r"Shell V-Power Diesel:\s*(?:Rp\s*)?([\d.,]+)\s*per liter",
            "vivo_revvo92": r"Vivo Revvo 92:\s*(?:Rp\s*)?([\d.,]+)\s*per liter",
            "vivo_revvo95": r"Vivo Revvo 95:\s*(?:Rp\s*)?([\d.,]+)\s*per liter",
        },
    },
]

# Maps a substring found in Pertamina's column image-URL filename to our
# internal fuel_key -- longest/most specific substrings must be checked
# first, since e.g. "pertamax" is a substring of "pertamax-turbo" too.
PERTAMINA_COLUMN_MAP = [
    ("pertamax-turbo", "pertamax_turbo_ron98"),
    ("pertamax-green-95", "pertamax_green95"),
    ("pertamax-pertashop", None),  # different retail channel (mini-outlet), not in our catalog
    ("pertamax", "pertamax_ron92"),
    ("pertalite", "pertalite_ron90"),
    ("pertamina-dex", "pertamina_dex"),
    ("dexlite", "dexlite"),
    ("bio-solar-non-subsidi", None),  # commercial diesel variant, not yet in our catalog
    ("bio-solar-subsidi", "solar_subsidi"),
]

# Regex patterns for the Metro TV News whitelist format: "Product Name: RpX.XXX per liter"
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_idr(raw: str) -> Optional[float]:
    """'16,300' -> 16300.0. ' -   ' / '-' / empty -> None (not available, not zero)."""
    raw = raw.strip()
    if not raw or raw == "-":
        return None
    try:
        return float(raw.replace(",", "").replace(".", ""))
    except ValueError:
        return None


def scrape_pertamina_prices() -> dict:
    """Official Pertamina Patra Niaga prices, all provinces, both Gasoline
    and Gasoil. Returns {fuel_key: {province: price_or_None}}."""
    resp = requests.get(PERTAMINA_API_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("succeeded"):
        raise ValueError(f"Pertamina API returned succeeded=false: {payload.get('message')}")

    content = payload["data"]["content"]
    table_nodes = [v for v in content.values() if v.get("type", {}).get("resolvedName") == "ProductTable"]
    if not table_nodes:
        raise ValueError("No ProductTable node found in Pertamina API response -- page structure may have changed.")

    result: dict = {}
    as_of = None
    for heading in content.values():
        if heading.get("type", {}).get("resolvedName") == "Heading":
            text = heading.get("props", {}).get("text", "")
            if "Update per tanggal" in text:
                as_of = text.replace("Update per tanggal", "").strip()

    for table in table_nodes:
        for item_group in table["props"]["items"]:
            for row in item_group["data"]:
                province = row.get("WILAYAH")
                if not province:
                    continue
                for col_url, raw_price in row.items():
                    if col_url == "WILAYAH":
                        continue
                    fuel_key = None
                    for substr, mapped_key in PERTAMINA_COLUMN_MAP:
                        if substr in col_url:
                            fuel_key = mapped_key
                            break
                    if fuel_key is None:
                        continue
                    price = _parse_idr(raw_price)
                    result.setdefault(fuel_key, {})[province] = price

    return {"prices_by_province": result, "as_of": as_of, "source": "official:pertaminapatraniaga.com", "fetched_at": _now_iso()}


def _find_table_html(node) -> Optional[str]:
    """Recursively search the already-JSON-decoded payload for the string
    value that contains the price <table> -- walking the real decoded
    strings (real \\r\\n bytes) rather than re-serializing with json.dumps,
    which would re-escape those bytes into literal backslash-r-backslash-n
    text and break any regex expecting real whitespace."""
    if isinstance(node, str):
        return node if "<table" in node else None
    if isinstance(node, dict):
        for v in node.values():
            found = _find_table_html(v)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_table_html(item)
            if found:
                return found
    return None


def scrape_shell_prices() -> dict:
    """Official Shell Indonesia prices via the AEM .model.json mirror.
    Currently only V-Power Diesel is sold in Indonesia -- an empty result
    for gasoline products is the real market state, not a parse failure."""
    resp = requests.get(SHELL_MODEL_JSON_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    payload = resp.json()

    table_html = _find_table_html(payload)
    if table_html is None:
        raise ValueError("No price <table> found in Shell's model.json -- page structure may have changed.")

    date_match = re.search(r"<p><b>(.*?)</b></p>", table_html)
    as_of = re.sub(r"&nbsp;", " ", date_match.group(1)).strip() if date_match else None

    rows = re.findall(r"<tr><td>(.*?)</td>\s*<td>Rp([\d.,]+)</td>\s*</tr>", table_html)
    prices = {}
    for label, raw_price in rows:
        label_clean = label.strip()
        price = _parse_idr(raw_price)
        if "Diesel" in label_clean:
            prices["shell_vpower_diesel"] = price
        elif "V-Power" in label_clean:
            prices["shell_vpower_ron95"] = price
        elif "Super" in label_clean:
            prices["shell_super_ron92"] = price

    return {"prices": prices, "as_of": as_of, "source": "official:shell.co.id", "fetched_at": _now_iso()}


def scrape_news_whitelist_prices() -> dict:
    """BP-AKR and Vivo have no official structured price page, so this pulls
    from the curated news whitelist above -- also used to cross-check the
    Pertamina/Shell official numbers. Fetches EVERY whitelisted outlet
    (not just the first that succeeds) and builds a per-fuel consensus, so
    two outlets can actually be compared against each other, not just used
    as a fallback chain."""
    by_outlet = {}
    errors = []

    for outlet in NEWS_WHITELIST:
        try:
            resp = requests.get(outlet["url"], headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            text = resp.text

            prices = {}
            for fuel_key, pattern in outlet["patterns"].items():
                m = re.search(pattern, text)
                if m:
                    prices[fuel_key] = _parse_idr(m.group(1))

            date_match = re.search(r"(\d{1,2} \w+ \d{4})", outlet["url"]) or re.search(r"per (\d{1,2} \w+ \d{4})", text)
            as_of = date_match.group(1) if date_match else None

            by_outlet[outlet["name"]] = {"prices": prices, "as_of": as_of, "url": outlet["url"]}
        except Exception as e:
            errors.append(f"{outlet['name']}: {e}")

    if not by_outlet:
        raise RuntimeError(f"All whitelisted news sources failed: {errors}")

    # Consensus per fuel: agreement across whichever outlets reported a
    # price for it. Disagreement beyond tolerance is flagged, not averaged
    # away -- a silent average could hide a real data-quality problem.
    all_fuel_keys = {fk for o in by_outlet.values() for fk in o["prices"]}
    consensus = {}
    for fuel_key in all_fuel_keys:
        votes = {name: o["prices"][fuel_key] for name, o in by_outlet.items() if fuel_key in o["prices"] and o["prices"][fuel_key] is not None}
        if not votes:
            continue
        values = list(votes.values())
        spread = max(values) - min(values)
        consensus[fuel_key] = {
            "price": values[0],  # outlets agree (or there's only one) -- see agreement field
            "agreement": "unanimous" if spread == 0 and len(votes) > 1 else ("single_source" if len(votes) == 1 else "DISAGREEMENT"),
            "votes": votes,
        }

    # Flat {fuel_key: price} view for the rest of the pipeline, preferring
    # unanimous/single-source values; a fuel in disagreement still gets a
    # value (first vote) but callers can check `consensus` for the flag.
    flat_prices = {fk: c["price"] for fk, c in consensus.items()}

    return {
        "prices": flat_prices,
        "consensus": consensus,
        "by_outlet": by_outlet,
        "source": f"news:{'+'.join(by_outlet.keys())}",
        "fetched_at": _now_iso(),
        "errors": errors or None,
    }


def cross_check(official_value: Optional[float], news_value: Optional[float], tolerance_idr: float = 50) -> dict:
    """Flags a disagreement between an official-source price and the news
    cross-check for the same product, rather than silently trusting either."""
    if official_value is None or news_value is None:
        return {"status": "incomplete", "official": official_value, "news": news_value}
    diff = abs(official_value - news_value)
    return {
        "status": "match" if diff <= tolerance_idr else "MISMATCH",
        "official": official_value,
        "news": news_value,
        "diff_idr": diff,
    }


def _load_cache() -> Optional[dict]:
    if not os.path.exists(CACHE_PATH):
        return None
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_cache(payload: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def get_live_fuel_prices() -> dict:
    """Main entry point. Pulls all sources independently, cross-checks
    Pertamina/Shell against the news whitelist, and degrades to the last
    cached snapshot (then, if that's also unavailable, lets the caller fall
    back to the static baseline catalog) on total failure.

    Returns:
        {
          "pertamina": {...} | None,
          "shell": {...} | None,
          "news_whitelist": {...} | None,
          "cross_check": {fuel_key: {...}},
          "source": "live" | "partial_live" | "cached_fallback",
          "fetched_at": iso timestamp,
          "errors": [...],
        }
    """
    errors = []
    pertamina = shell = news = None

    try:
        pertamina = scrape_pertamina_prices()
    except Exception as e:
        errors.append(f"Pertamina: {e}")

    try:
        shell = scrape_shell_prices()
    except Exception as e:
        errors.append(f"Shell: {e}")

    try:
        news = scrape_news_whitelist_prices()
    except Exception as e:
        errors.append(f"News whitelist: {e}")

    if pertamina is None and shell is None and news is None:
        cached = _load_cache()
        if cached:
            return {**cached, "source": "cached_fallback", "fetched_at": _now_iso(), "errors": errors}
        raise RuntimeError(f"All live fuel price sources failed and no cache exists: {errors}")

    cross_checks = {}
    if news:
        news_prices = news.get("prices", {})
        if pertamina:
            # Compare Jakarta (most-cited reference region) against news numbers.
            jakarta_prices = {k: v.get("Prov. DKI Jakarta") for k, v in pertamina["prices_by_province"].items()}
            for fuel_key, news_price in news_prices.items():
                if fuel_key in jakarta_prices:
                    cross_checks[fuel_key] = cross_check(jakarta_prices[fuel_key], news_price)
        if shell:
            for fuel_key, news_price in news_prices.items():
                if fuel_key in shell.get("prices", {}):
                    cross_checks[fuel_key] = cross_check(shell["prices"][fuel_key], news_price)

    result = {
        "pertamina": pertamina,
        "shell": shell,
        "news_whitelist": news,
        "cross_check": cross_checks,
        "source": "live" if not errors else "partial_live",
        "fetched_at": _now_iso(),
        "errors": errors or None,
    }
    _save_cache(result)
    return result


if __name__ == "__main__":
    result = get_live_fuel_prices()
    print(json.dumps({k: v for k, v in result.items() if k != "pertamina"}, indent=2, default=str))
    if result.get("pertamina"):
        jakarta = {k: v.get("Prov. DKI Jakarta") for k, v in result["pertamina"]["prices_by_province"].items()}
        print("\nPertamina (DKI Jakarta):", json.dumps(jakarta, indent=2))
