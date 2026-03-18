"""
scrapers.py — Product availability checkers for fashion sites.

Supported:
  - Zara (zara.com) — uses Inditex internal API
  - Massimo Dutti (massimodutti.com) — same Inditex API
  - Generic fallback — HTML parsing

Returns a ScrapeResult dataclass with:
  product_name: str
  available_sizes: list[str]   — sizes currently in stock
  all_sizes: list[str]         — all sizes (for asking the user)
  is_size_available(size) -> bool
"""
import re
import json
import logging
import urllib.parse
import html as html_module
from dataclasses import dataclass, field
from typing import Optional

import requests


def _clean_name(raw: str) -> str:
    """Decode HTML entities, strip all kinds of whitespace, return 'Товар' if empty."""
    name = html_module.unescape(raw or "")
    name = name.replace("\xa0", " ").replace("\u200b", "").strip()
    return name if name else "Товар"

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

TIMEOUT = 15


@dataclass
class ScrapeResult:
    product_name: str = "Товар"
    available_sizes: list = field(default_factory=list)
    all_sizes: list = field(default_factory=list)
    error: Optional[str] = None

    def is_size_available(self, size: str) -> bool:
        """Case-insensitive size check."""
        size_lower = size.strip().lower()
        return any(s.strip().lower() == size_lower for s in self.available_sizes)


# ─────────────────────────────────────────────
#  INDITEX (Zara + Massimo Dutti)
# ─────────────────────────────────────────────

def _inditex_product_id(url: str) -> Optional[str]:
    """Extract product ID from Inditex URL.
    Zara uses    -p12345678.html  (prefix p)
    Massimo uses -l05724300       (prefix l)
    """
    # Try standard Zara pattern first
    m = re.search(r'-p(\d{8,})', url)
    if m:
        return m.group(1)
    # Massimo Dutti uses letter l before ID
    m = re.search(r'-l(\d{7,})', url)
    if m:
        return m.group(1)
    # Try query param pelement= (Massimo Dutti sometimes uses this)
    m = re.search(r'pelement=(\d+)', url)
    if m:
        return m.group(1)
    return None


# All Inditex-family domains (same API structure)
INDITEX_DOMAINS = {
    "zara.com":          "www.zara.com",
    "massimodutti.com":  "www.massimodutti.com",
    "bershka.com":       "www.bershka.com",
    "pullandbear.com":   "www.pullandbear.com",
    "stradivarius.com":  "www.stradivarius.com",
    "oysho.com":         "www.oysho.com",
    "zarahome.com":      "www.zarahome.com",
}

# Country code → language override (for countries where lang ≠ country code)
COUNTRY_LANG_OVERRIDE = {
    "GB": "en",
    "US": "en",
    "AU": "en",
    "CA": "en",
    "IE": "en",
    "NZ": "en",
    "SG": "en",
    "HK": "zh",
    "TW": "zh",
    "BE": "fr",
    "CH": "de",
}


def _inditex_domain(url: str) -> str:
    """Return the canonical domain for this Inditex brand URL."""
    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc.lower().lstrip("www.")
    for key, domain in INDITEX_DOMAINS.items():
        if key in netloc:
            return domain
    return parsed.netloc  # fallback


def _inditex_country_locale(url: str) -> tuple[str, str]:
    """Guess country/locale from any Inditex URL pattern."""
    # Build a combined brand pattern
    brand_pat = r'(?:' + '|'.join(re.escape(k) for k in INDITEX_DOMAINS) + r')'

    # Two-segment path: /ru/ru/, /es/es/, /us/en/
    m = re.search(brand_pat + r'/([a-z]{2})/([a-z]{2})(?:/|$)', url)
    if m:
        country = m.group(1).upper()
        lang = m.group(2)
        return country, f"{lang}_{country}"

    # Single-segment: /pl/, /de/, /gb/
    m = re.search(brand_pat + r'/([a-z]{2})(?:/|$|\?)', url)
    if m:
        country = m.group(1).upper()
        lang = COUNTRY_LANG_OVERRIDE.get(country, country.lower())
        return country, f"{lang}_{country}"

    return "RU", "ru_RU"


def _fetch_url(url: str, headers: dict) -> Optional[requests.Response]:
    """
    Fetch a URL. If SCRAPER_API_KEY env var is set, route through ScraperAPI
    which handles Cloudflare and other anti-bot measures automatically.
    """
    import os
    scraper_key = os.environ.get("SCRAPER_API_KEY", "").strip()
    if scraper_key:
        proxy_url = f"http://api.scraperapi.com?api_key={scraper_key}&url={urllib.parse.quote(url)}"
        resp = requests.get(proxy_url, headers=headers, timeout=60)
    else:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    return resp if resp.status_code == 200 else None


def scrape_inditex(url: str) -> ScrapeResult:
    """
    Fetch product data from Inditex brands.
    Strategy (in order):
      1. api.zara.com — mobile app API, usually not behind Cloudflare
      2. itxrest/webservices endpoints with mobile User-Agents
      3. ScraperAPI proxy (if SCRAPER_API_KEY env var is set)
      4. HTML fallback
    """
    product_id = _inditex_product_id(url)
    if not product_id:
        return ScrapeResult(error="Не удалось найти ID товара в ссылке.")

    country, locale = _inditex_country_locale(url)
    domain = _inditex_domain(url)
    clean_url = url.split("?")[0]
    lang = locale.split("_")[0]

    # ── Strategy 1: Zara mobile app API (different domain, no Cloudflare) ──
    app_api_candidates = [
        f"https://api.zara.com/article/v1/articles/{product_id}?country={country}&lang={lang}",
        f"https://api.zara.com/catalog/v1/product/{product_id}?country={country}&locale={locale}",
    ]
    app_ua = "com.inditex.zara/1 CFNetwork/1410.0.3 Darwin/22.6.0"
    for api_url in app_api_candidates:
        try:
            resp = requests.get(
                api_url,
                headers={"User-Agent": app_ua, "Accept": "application/json"},
                timeout=TIMEOUT,
            )
            if resp.status_code == 200:
                candidate = resp.json()
                if candidate.get("name") or candidate.get("detail") or candidate.get("product"):
                    logger.info(f"App API success: {api_url}")
                    return _parse_inditex_data(candidate)
        except Exception as e:
            logger.debug(f"App API failed ({api_url}): {e}")

    # ── Strategy 2: classic itxrest/webservices with mobile UAs ──
    api_candidates = [
        f"https://{domain}/itxrest/3/catalog/store/{country}/{locale}/product/{product_id}/detail",
        f"https://{domain}/webservices/zds/catalog/store/{country}/{locale}/product/{product_id}/detail",
    ]
    user_agents = [
        "com.inditex.zara/1 CFNetwork/1410.0.3 Darwin/22.6.0",
        "Zara/12.3.0 (Linux; Android 13; SM-G991B Build/TP1A.220624.014)",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    ]
    for ua in user_agents:
        for api_url in api_candidates:
            try:
                resp = _fetch_url(api_url, {
                    "User-Agent": ua,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": clean_url,
                    "Origin": f"https://{domain}",
                })
                if resp:
                    candidate = resp.json()
                    if candidate.get("name") or candidate.get("detail"):
                        logger.info(f"Inditex API success: {api_url} | UA: {ua[:40]}")
                        return _parse_inditex_data(candidate)
            except Exception as e:
                logger.debug(f"Attempt failed ({api_url}): {e}")

    logger.warning(f"All Inditex API attempts failed for product {product_id}")
    return _scrape_inditex_html(clean_url)


def _parse_inditex_data(data: dict) -> ScrapeResult:
    """Parse size/availability from any Inditex API JSON response."""
    # Handle nested structures from different API versions
    product = data.get("product", data)
    product_name = _clean_name(product.get("name", data.get("name", "")))

    all_sizes: list = []
    available_sizes: list = []

    # Format 1: detail.colors[].sizes[]
    for detail in product.get("detail", data.get("detail", {})).get("colors", []):
        for size_info in detail.get("sizes", []):
            size_label = size_info.get("name", "").strip()
            if not size_label:
                continue
            if size_label not in all_sizes:
                all_sizes.append(size_label)
            sku_avail = size_info.get("availability", "")
            if sku_avail in ("IN_STOCK", "BACK_IN_STOCK", "LOW_ON_STOCK"):
                if size_label not in available_sizes:
                    available_sizes.append(size_label)

    # Format 2: sizes[] at root level (app API)
    for size_info in data.get("sizes", product.get("sizes", [])):
        size_label = (size_info.get("name") or size_info.get("label") or "").strip()
        if not size_label or size_label in all_sizes:
            continue
        all_sizes.append(size_label)
        avail = size_info.get("availability", size_info.get("stock", ""))
        if str(avail).upper() in ("IN_STOCK", "BACK_IN_STOCK", "LOW_ON_STOCK", "TRUE", "1"):
            if size_label not in available_sizes:
                available_sizes.append(size_label)

    if not all_sizes:
        return ScrapeResult(product_name=product_name,
                            error="Размеры не найдены в ответе API. Введи вручную.")

    return ScrapeResult(product_name=product_name,
                        all_sizes=all_sizes,
                        available_sizes=available_sizes)


def _scrape_inditex_html(url: str) -> ScrapeResult:
    """
    Last-resort HTML scraper for Inditex sites.
    Tries mobile User-Agent which sometimes bypasses Cloudflare.
    """
    mobile_ua = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
    )
    try:
        resp = _fetch_url(url, {**HEADERS, "User-Agent": mobile_ua})
        if not resp:
            raise ValueError("HTTP error")
        html = resp.text
    except Exception:
        return ScrapeResult(
            error=(
                "Сайт блокирует автоматические запросы. "
                "Введи размер вручную — бот сохранит товар и будет проверять наличие каждый час."
            )
        )

    product_name = _clean_name(_extract_og_title(html) or "")
    all_sizes: list = []
    available_sizes: list = []

    # Zara stores product data in a large JSON inside <script> tags
    # Look for any JSON blob containing "availability" + "name" fields
    for script_content in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
        if '"availability"' not in script_content:
            continue
        # Try to find JSON objects with size data
        for blob in re.findall(r'\{[^{}]{20,}\}', script_content):
            try:
                obj = json.loads(blob)
            except Exception:
                continue
            name_val = obj.get("name", "")
            avail_val = obj.get("availability", "")
            if name_val and avail_val and len(str(name_val)) <= 10:
                sz = str(name_val).strip()
                if sz not in all_sizes:
                    all_sizes.append(sz)
                if avail_val in ("IN_STOCK", "BACK_IN_STOCK", "LOW_ON_STOCK"):
                    if sz not in available_sizes:
                        available_sizes.append(sz)

    if all_sizes:
        return ScrapeResult(
            product_name=product_name,
            all_sizes=all_sizes,
            available_sizes=available_sizes,
        )

    # Last resort: full generic parse
    result = _parse_jsonld(html, product_name)
    if result.all_sizes:
        return result
    return _parse_size_keywords(html, product_name)


# ─────────────────────────────────────────────
#  GENERIC HTML SCRAPER (fallback / Loewe / etc.)
# ─────────────────────────────────────────────

def _scrape_generic(url: str) -> ScrapeResult:
    """
    Tries to extract size/availability from page HTML.
    Looks for:
      1. JSON-LD schema (schema.org Product)
      2. Next.js __NEXT_DATA__
      3. Common size selector patterns
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        return ScrapeResult(error=f"Не удалось загрузить страницу: {e}")

    product_name = _clean_name(_extract_og_title(html) or "")

    # Try JSON-LD
    result = _parse_jsonld(html, product_name)
    if result.all_sizes:
        return result

    # Try Next.js __NEXT_DATA__
    result = _parse_next_data(html, product_name)
    if result.all_sizes:
        return result

    # Last resort — generic size keywords
    result = _parse_size_keywords(html, product_name)
    return result


def _extract_og_title(html: str) -> Optional[str]:
    # Try og:title (order of attributes can vary)
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', html, re.I)
    if m:
        return _clean_name(m.group(1))
    m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.I)
    if m:
        return _clean_name(m.group(1))
    return None


def _parse_jsonld(html: str, product_name: str) -> ScrapeResult:
    """Look for schema.org Product in JSON-LD script tags."""
    pattern = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
    all_sizes = []
    available_sizes = []

    for match in pattern.finditer(html):
        try:
            data = json.loads(match.group(1))
        except Exception:
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if item.get("@type") != "Product":
                continue

            name = item.get("name", product_name)

            offers = item.get("offers", [])
            if isinstance(offers, dict):
                offers = [offers]

            for offer in offers:
                avail = offer.get("availability", "")
                size = offer.get("name") or offer.get("sku", "")
                if not size:
                    continue
                size = size.strip()
                if size not in all_sizes:
                    all_sizes.append(size)
                if "InStock" in avail or "in_stock" in avail.lower():
                    if size not in available_sizes:
                        available_sizes.append(size)

            if all_sizes:
                return ScrapeResult(product_name=name, all_sizes=all_sizes, available_sizes=available_sizes)

    return ScrapeResult(product_name=product_name)


def _parse_next_data(html: str, product_name: str) -> ScrapeResult:
    """Extract sizes from Next.js __NEXT_DATA__ embedded JSON."""
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.S)
    if not m:
        return ScrapeResult(product_name=product_name)

    try:
        data = json.loads(m.group(1))
    except Exception:
        return ScrapeResult(product_name=product_name)

    # Walk the JSON tree looking for size-like keys
    all_sizes = []
    available_sizes = []

    def walk(obj):
        if isinstance(obj, dict):
            # Common keys in fashion sites
            for key in ("size", "sizeName", "label", "name"):
                if key in obj and isinstance(obj[key], str):
                    size_val = obj[key].strip()
                    # Filter out non-size strings (too long)
                    if size_val and len(size_val) <= 10:
                        avail_key = obj.get("available", obj.get("inStock", obj.get("stock", None)))
                        if size_val not in all_sizes:
                            all_sizes.append(size_val)
                        if avail_key is True or avail_key == "true" or (isinstance(avail_key, int) and avail_key > 0):
                            if size_val not in available_sizes:
                                available_sizes.append(size_val)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)

    return ScrapeResult(product_name=product_name, all_sizes=all_sizes, available_sizes=available_sizes)


def _parse_size_keywords(html: str, product_name: str) -> ScrapeResult:
    """Last resort: look for common size strings in the page."""
    # Common fashion sizes
    size_patterns = [
        r'\b(XXS|XS|S|M|L|XL|XXL|XXXL)\b',
        r'\b(3[4-9]|4[0-9]|5[0-4])\b',   # EU numeric sizes
        r'\b(6|8|10|12|14|16|18)\b',       # UK sizes
    ]
    found = []
    for pat in size_patterns:
        found.extend(re.findall(pat, html))

    # Deduplicate preserving order
    seen = set()
    all_sizes = []
    for s in found:
        if s not in seen:
            seen.add(s)
            all_sizes.append(s)

    if not all_sizes:
        return ScrapeResult(
            product_name=product_name,
            error=(
                "Не удалось автоматически определить размеры на этой странице. "
                "Введи нужный размер вручную (например: S, M, 38)."
            )
        )

    return ScrapeResult(product_name=product_name, all_sizes=all_sizes, available_sizes=[])


# ─────────────────────────────────────────────
#  PUBLIC ENTRY POINT
# ─────────────────────────────────────────────

def check_product(url: str) -> ScrapeResult:
    """
    Main function — choose the right scraper based on URL domain.
    Returns a ScrapeResult with product info and size availability.

    Inditex brands (API):  Zara, Massimo Dutti, Bershka, Pull&Bear,
                           Stradivarius, Oysho, Zara Home
    Generic (HTML):        Loewe, Toteme, Arket, COS, NET-A-PORTER,
                           Farfetch, SSENSE, and everything else
    """
    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc.lower()

    if any(brand in netloc for brand in INDITEX_DOMAINS):
        return scrape_inditex(url)
    else:
        return _scrape_generic(url)
