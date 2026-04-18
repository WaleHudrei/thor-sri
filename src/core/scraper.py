"""
SRI Services scraper core for thor-sri.

Drives properties.sriservices.com for all three sale types.
Integrates with the Job queue: reports progress, checks cancel flag,
emits structured log lines via the Job's _log method.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
)

log = logging.getLogger("thor-sri.scraper")


# ── Sale type config ──────────────────────────────────────────────────────────
class SaleType(str, Enum):
    TAX = "tax_sale"
    COMMISSIONER = "commissioner_sale"
    SHERIFF = "sheriff_sale"


SALE_TYPE_PATHS = {
    SaleType.TAX: "/tax",
    SaleType.COMMISSIONER: "/commissioners",
    SaleType.SHERIFF: "/sheriff",
}

BASE_URL = "https://properties.sriservices.com"

PRIORITY_COUNTIES = ["marion", "allen", "lake", "vanderburgh"]


# ── Record schema ─────────────────────────────────────────────────────────────
@dataclass
class SRIRecord:
    sale_type: str
    state: str
    county: str
    scraped_at: str
    case_number: Optional[str] = None
    parcel: Optional[str] = None
    item_number: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    zip_code: Optional[str] = None
    sale_date: Optional[str] = None
    minimum_bid: Optional[str] = None
    judgment: Optional[str] = None
    status: Optional[str] = None
    plaintiff: Optional[str] = None
    defendant: Optional[str] = None
    attorney: Optional[str] = None
    tax_years: Optional[str] = None
    delinquent_amount: Optional[str] = None
    raw_text: Optional[str] = None
    extras: dict = field(default_factory=dict)


# ── Params ────────────────────────────────────────────────────────────────────
@dataclass
class ScrapeParams:
    sale_types: list[SaleType]
    state: str
    counties: Optional[list[str]]
    headless: bool = True
    timeout_ms: int = 45_000

    @classmethod
    def from_dict(cls, d: dict) -> "ScrapeParams":
        raw_types = d.get("sale_types") or ["tax_sale", "commissioner_sale", "sheriff_sale"]
        if isinstance(raw_types, str):
            raw_types = [raw_types]
        sale_types = [SaleType(t) for t in raw_types]

        counties = d.get("counties")
        if counties == "all" or counties == []:
            counties = None
        elif counties == "priority":
            counties = PRIORITY_COUNTIES
        elif isinstance(counties, str):
            counties = [c.strip().lower() for c in counties.split(",") if c.strip()]
        elif isinstance(counties, list):
            counties = [c.strip().lower() for c in counties if str(c).strip()]

        return cls(
            sale_types=sale_types,
            state=(d.get("state") or "IN").upper(),
            counties=counties,
            headless=d.get("headless", True),
            timeout_ms=int(d.get("timeout_seconds", 45)) * 1000,
        )


# ── Progress callback protocol ────────────────────────────────────────────────
class ProgressSink:
    """The scraper calls these methods; the queue's worker implements them."""
    def log(self, level: str, msg: str) -> None: ...
    def set_progress(self, current: int, total: int) -> None: ...
    def add_results(self, n: int) -> None: ...
    def add_errors(self, n: int) -> None: ...
    def should_cancel(self) -> bool: ...


# ── Scraper ───────────────────────────────────────────────────────────────────
class SRIScraper:
    def __init__(self, proxy: Optional[dict] = None):
        """
        proxy: {'server': '...', 'username': '...', 'password': '...'} or None
        """
        self.proxy = proxy

    async def run(self, params: ScrapeParams, sink: ProgressSink) -> list[dict]:
        sink.log("INFO",
                 f"Starting SRI scrape: types={[t.value for t in params.sale_types]} "
                 f"state={params.state} counties={params.counties or 'ALL'}")

        async with async_playwright() as pw:
            browser, context = await self._launch(pw, params.headless)
            try:
                page = await context.new_page()
                page.set_default_timeout(params.timeout_ms)

                # Phase 1: discover county lists per sale type
                plan: list[tuple[SaleType, dict]] = []
                for sale_type in params.sale_types:
                    if sink.should_cancel():
                        sink.log("WARN", "Cancelled during discovery")
                        return []
                    sink.log("INFO", f"Discovering counties for {sale_type.value}")
                    counties = await self._discover_counties(
                        page, sale_type, params.state, params.timeout_ms,
                    )
                    target = self._filter_counties(counties, params.counties)
                    sink.log("INFO",
                             f"  {sale_type.value}: {len(counties)} total, "
                             f"{len(target)} selected")
                    for c in target:
                        plan.append((sale_type, c))

                sink.set_progress(0, len(plan))
                if not plan:
                    sink.log("WARN", "Nothing to scrape — check county filter")
                    return []

                # Phase 2: scrape each (sale_type, county) pair
                all_records: list[dict] = []
                timestamp = datetime.now(timezone.utc).isoformat()

                for i, (sale_type, county) in enumerate(plan, 1):
                    if sink.should_cancel():
                        sink.log("WARN",
                                 f"Cancelled after {i-1}/{len(plan)}")
                        break
                    sink.log("INFO",
                             f"[{i}/{len(plan)}] {sale_type.value} / {county['name']}")
                    try:
                        records = await self._scrape_county(
                            page, sale_type, params.state, county,
                            timestamp, params.timeout_ms,
                        )
                        sink.log("INFO", f"  → {len(records)} listings")
                        all_records.extend(records)
                        sink.add_results(len(records))
                    except Exception as e:
                        sink.log("ERROR", f"  ✗ {county['name']}: {e}")
                        sink.add_errors(1)
                    finally:
                        sink.set_progress(i, len(plan))
                        await asyncio.sleep(1.0)  # polite pause

                sink.log("INFO",
                         f"Scrape complete: {len(all_records)} total records")
                return all_records
            finally:
                await context.close()
                await browser.close()

    # ── Browser setup ─────────────────────────────────────────────────────────
    async def _launch(self, pw, headless: bool) -> tuple[Browser, BrowserContext]:
        launch_args = {"headless": headless}
        if self.proxy:
            launch_args["proxy"] = self.proxy

        browser = await pw.chromium.launch(**launch_args)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=bool(self.proxy),
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        return browser, context

    async def _goto(self, page: Page, url: str, timeout_ms: int,
                    retries: int = 3) -> None:
        last: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=timeout_ms)
                try:
                    await page.wait_for_function(
                        "() => document.querySelector('#root')?.childElementCount > 0",
                        timeout=15_000,
                    )
                except PWTimeout:
                    pass
                return
            except Exception as e:
                last = e
                await asyncio.sleep(2 * attempt)
        raise RuntimeError(f"Navigation to {url} failed after {retries} tries") from last

    # ── County discovery ─────────────────────────────────────────────────────
    async def _discover_counties(
        self, page: Page, sale_type: SaleType, state: str, timeout_ms: int,
    ) -> list[dict]:
        url = f"{BASE_URL}{SALE_TYPE_PATHS[sale_type]}"
        await self._goto(page, url, timeout_ms)

        # Wait for any county tile to render
        for sel in ["[class*='county']", "a[href*='/state/']",
                    "a[href*='/county/']", "table tbody tr"]:
            try:
                await page.wait_for_selector(sel, timeout=6_000)
                break
            except PWTimeout:
                continue

        counties = await page.evaluate(
            """(state) => {
                const rows = []; const seen = new Set();
                document.querySelectorAll('a[href]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const m = href.match(/\\/(tax|commissioners|sheriff)\\/([A-Z]{2})\\/([^/?#]+)/i);
                    if (!m) return;
                    if (m[2].toUpperCase() !== state) return;
                    const slug = m[3].toLowerCase();
                    if (seen.has(slug)) return;
                    seen.add(slug);
                    rows.push({
                        slug,
                        name: (a.textContent || slug).trim().replace(/\\s+County$/i, ''),
                        href: href.startsWith('http') ? href : (location.origin + href),
                        state: m[2].toUpperCase(),
                    });
                });
                return rows;
            }""",
            state,
        )
        return counties

    def _filter_counties(
        self, all_counties: list[dict], wanted: Optional[list[str]],
    ) -> list[dict]:
        if not wanted:
            return all_counties
        wanted_set = {w.lower().replace(" ", "-") for w in wanted}
        return [c for c in all_counties
                if c["slug"] in wanted_set or c["name"].lower() in wanted_set]

    # ── Per-county scrape ────────────────────────────────────────────────────
    async def _scrape_county(
        self, page: Page, sale_type: SaleType, state: str,
        county: dict, scraped_at: str, timeout_ms: int,
    ) -> list[dict]:
        url = county.get("href") or (
            f"{BASE_URL}{SALE_TYPE_PATHS[sale_type]}/{state}/{county['slug']}"
        )
        await self._goto(page, url, timeout_ms)
        await self._wait_for_listings(page)
        await self._exhaust_pagination(page)

        raw_listings = await self._extract_listings(page)
        records = []
        for raw in raw_listings:
            rec = self._normalize(raw, sale_type, state, county["name"], scraped_at)
            records.append(asdict(rec))
        return records

    async def _wait_for_listings(self, page: Page) -> None:
        for sel in ["[class*='property-card']", "[class*='listing']",
                    "table tbody tr", "[role='row']"]:
            try:
                await page.wait_for_selector(sel, timeout=8_000)
                return
            except PWTimeout:
                continue
        await asyncio.sleep(1)

    async def _exhaust_pagination(self, page: Page) -> None:
        prev_count = -1
        for _ in range(50):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.8)
            count = await page.evaluate(
                "document.querySelectorAll(\"[class*='property-card'],"
                "[class*='listing'],table tbody tr\").length"
            )
            if count == prev_count:
                if not await self._click_next(page):
                    break
                await asyncio.sleep(1.5)
            prev_count = count

    async def _click_next(self, page: Page) -> bool:
        for sel in ["button[aria-label='Next']", "button:has-text('Next')",
                    "a:has-text('Next')", "[class*='pagination'] [class*='next']"]:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible() and await btn.is_enabled():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

    async def _extract_listings(self, page: Page) -> list[dict]:
        # Strategy 1 — hydration JSON
        hydration = await page.evaluate(
            """() => {
                const scripts = document.querySelectorAll('script[type=\"application/json\"]');
                for (const s of scripts) {
                    try {
                        const j = JSON.parse(s.textContent);
                        if (j && (j.properties || j.listings || j.items)) return j;
                    } catch (_) {}
                }
                if (window.__INITIAL_STATE__) return window.__INITIAL_STATE__;
                if (window.__NEXT_DATA__) return window.__NEXT_DATA__;
                return null;
            }"""
        )
        if hydration:
            items = self._extract_from_json(hydration)
            if items:
                return items

        # Strategy 2 — DOM
        return await page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll(
                    "[class*='property-card'],[class*='listing-card'],[class*='sale-item']"
                ).forEach(card => {
                    const row = { raw_text: (card.textContent || '').trim() };
                    card.querySelectorAll('[class],[data-field]').forEach(el => {
                        const key =
                            el.getAttribute('data-field') ||
                            (el.className.match(/[a-z]+-(address|city|zip|date|bid|judgment|case|parcel|plaintiff|defendant|status)/i) || [])[1];
                        if (key) row[key.toLowerCase()] = (el.textContent || '').trim();
                    });
                    out.push(row);
                });
                if (!out.length) {
                    document.querySelectorAll('table tbody tr').forEach(r => {
                        const cells = Array.from(r.querySelectorAll('td'))
                                           .map(c => (c.textContent || '').trim());
                        if (cells.length >= 2) out.push({
                            _cells: cells, raw_text: cells.join(' | ')
                        });
                    });
                }
                return out;
            }"""
        )

    def _extract_from_json(self, hydration: dict) -> list[dict]:
        out: list[dict] = []
        def walk(n):
            if isinstance(n, list):
                if n and isinstance(n[0], dict) and self._looks_like_property(n[0]):
                    out.extend(n)
                else:
                    for x in n:
                        walk(x)
            elif isinstance(n, dict):
                for v in n.values():
                    walk(v)
        walk(hydration)
        return out

    @staticmethod
    def _looks_like_property(obj: dict) -> bool:
        keys = {k.lower() for k in obj.keys()}
        return bool(keys & {"address", "parcel", "parcelnumber", "case",
                            "casenumber", "judgment", "minimumbid", "saledate"})

    def _normalize(self, raw: dict, sale_type: SaleType, state: str,
                   county: str, scraped_at: str) -> SRIRecord:
        def pick(*keys):
            for k in keys:
                for v in (k, k.lower(), k.upper(),
                          k.replace("_", ""), k.replace("_", "").lower()):
                    if v in raw and raw[v] is not None:
                        return str(raw[v]).strip()
            return None

        address = pick("address", "property_address", "street", "addressLine1")
        city = pick("city")
        zip_code = pick("zip", "zip_code", "postal_code", "postalcode")

        if not address and raw.get("raw_text"):
            address, city, zip_code = self._parse_address_from_text(raw["raw_text"])

        return SRIRecord(
            sale_type=sale_type.value,
            state=state,
            county=county,
            scraped_at=scraped_at,
            case_number=pick("case_number", "caseNumber", "case"),
            parcel=pick("parcel", "parcel_number", "parcelNumber", "apn"),
            item_number=pick("item_number", "itemNumber", "item"),
            address=address, city=city, zip_code=zip_code,
            sale_date=pick("sale_date", "saleDate", "date"),
            minimum_bid=pick("minimum_bid", "minimumBid", "opening_bid", "openingBid"),
            judgment=pick("judgment", "judgment_amount"),
            status=pick("status", "sale_status", "saleStatus"),
            plaintiff=pick("plaintiff"),
            defendant=pick("defendant", "owner"),
            attorney=pick("attorney", "plaintiff_attorney"),
            tax_years=pick("tax_years", "years_delinquent"),
            delinquent_amount=pick("delinquent_amount", "amount_due"),
            raw_text=(raw.get("raw_text") or "")[:500] or None,
        )

    @staticmethod
    def _parse_address_from_text(text: str):
        m = re.search(
            r"(\d+[^,]+?),\s*([A-Za-z .'-]+?),\s*[A-Z]{2}\s*(\d{5}(?:-\d{4})?)",
            text,
        )
        return (m.group(1).strip(), m.group(2).strip(), m.group(3).strip()) if m else (None, None, None)


# ── Proxy config helper ───────────────────────────────────────────────────────
def proxy_from_env() -> tuple[Optional[dict], str]:
    """Returns (playwright_proxy_dict_or_None, proxy_type_label)."""
    ptype = (os.getenv("PROXY_TYPE") or "").lower()
    if ptype == "zyte":
        key = os.getenv("ZYTE_API_KEY")
        if not key:
            return None, "none"
        return ({
            "server": "http://proxy.zyte.com:8011",
            "username": key,
            "password": "",
        }, "zyte")
    if ptype == "webshare":
        url = os.getenv("PROXY_URL")
        if not url:
            return None, "none"
        # Parse user:pass@host:port from URL
        m = re.match(r"https?://([^:]+):([^@]+)@(.+)$", url)
        if m:
            user, pw, host = m.groups()
            return ({"server": f"http://{host}",
                     "username": user, "password": pw}, "webshare")
        return ({"server": url}, "webshare")
    return None, "direct"
