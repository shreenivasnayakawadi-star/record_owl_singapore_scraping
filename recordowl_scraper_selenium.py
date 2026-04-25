"""
RecordOwl Company Scraper - Selenium-based

Scrapes company data from RecordOwl using Selenium for JavaScript rendering
and cookie-based authentication. Handles Cloudflare email obfuscation,
proper tab navigation, and precise HTML parsing.

Usage:
    python recordowl_scraper_selenium.py "psa-international-pte-ltd"
    python recordowl_scraper_selenium.py "dbs-group-holdings-ltd" --output dbs.json
    python recordowl_scraper_selenium.py "PSA International Pte Ltd" --no-auth
"""

import json
import time
import re
import os
import random
import argparse
import urllib.robotparser
from urllib.parse import urlparse

import requests
from typing import Any, Optional
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

# ─────────────────────────── Constants ───────────────────────────────────────
BASE_URL    = "https://recordowl.com"
COOKIE_FILE = ".cookies/recordowl-cookies.json"

# Rotating user-agents — required by SG scraping regulations for private sites.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# robots.txt check is run once per process; cached here.
_ROBOTS_CHECKED = False


def check_robots_txt(base_url: str = BASE_URL, path: str = "/company/") -> bool:
    """
    Fetch robots.txt for base_url and check if `path` is allowed for generic crawlers.
    Logs a prominent warning if Disallow matches; does not block (auth cookies imply
    authorised access). Run once per process.
    """
    global _ROBOTS_CHECKED
    if _ROBOTS_CHECKED:
        return True
    _ROBOTS_CHECKED = True

    try:
        parsed = urlparse(base_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        allowed = rp.can_fetch("*", base_url + path)
        if allowed:
            print(f"[ROBOTS] OK — {path} is allowed per {robots_url}")
        else:
            print(
                f"[ROBOTS] ⚠  {path} is DISALLOWED per {robots_url}. "
                f"Proceeding under authorised-account (cookie) assumption. "
                f"Ensure written authorisation exists before continuing at scale."
            )
        return allowed
    except Exception as e:
        print(f"[ROBOTS] Could not read robots.txt ({e}); proceeding.")
        return True

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", re.I)
PHONE_RE = re.compile(r"[\+\d][\d\s\-\(\)]{6,}\d")
NOISE_RE = re.compile(
    r"(example\.com|sentry\.io|cloudflare|@2x\.|\.png|\.jpg|\.svg|noreply|no-reply"
    r"|arnifi\.com)",   # arnifi.com = logged-in user navbar email, not company data
    re.I,
)


# ─────────────────────────── Cookie helpers ──────────────────────────────────
def load_cookies(driver: webdriver.Chrome, cookie_file: str = COOKIE_FILE) -> bool:
    """Load cookies from JSON file into Selenium driver."""
    if not os.path.exists(cookie_file):
        print(f"[WARN] Cookie file not found: {cookie_file}")
        return False
    try:
        with open(cookie_file, "r", encoding="utf-8") as f:
            cookies = json.load(f)
    except Exception as e:
        print(f"[WARN] Failed to load cookies: {e}")
        return False

    loaded = 0
    for cookie in cookies:
        cd = {
            "name":   cookie.get("name"),
            "value":  cookie.get("value"),
            "domain": cookie.get("domain", ".recordowl.com"),
            "path":   cookie.get("path", "/"),
        }
        if "expirationDate" in cookie:
            cd["expiry"] = int(float(cookie["expirationDate"]))
        cd = {k: v for k, v in cd.items() if v is not None}
        try:
            driver.add_cookie(cd)
            loaded += 1
        except Exception:
            pass

    if loaded:
        print(f"[AUTH] Loaded {loaded} cookies from {cookie_file}")
        return True
    return False


# ─────────────────────────── Driver setup ────────────────────────────────────
def create_driver() -> webdriver.Chrome:
    """Create a stealth Selenium Chrome driver."""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    # Rotate user-agent per driver instance (SG regulations: rotating user agents).
    opts.add_argument(f"--user-agent={random.choice(USER_AGENTS)}")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    return webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=opts,
    )


# ─────────────────────────── Email helpers ───────────────────────────────────
def decode_cf_email(encoded: str) -> Optional[str]:
    """
    Decode Cloudflare email obfuscation.

    First byte = XOR key; subsequent byte-pairs = XOR'd char codes.
    """
    try:
        key = int(encoded[:2], 16)
        decoded = "".join(
            chr(int(encoded[i : i + 2], 16) ^ key) for i in range(2, len(encoded), 2)
        )
        if EMAIL_RE.match(decoded):
            return decoded
    except Exception:
        pass
    return None


def extract_emails_from_soup(soup: BeautifulSoup) -> set:
    """
    Extract all emails from BeautifulSoup using multiple strategies.
    Decodes Cloudflare-obfuscated emails, filters noise.
    Returns only emails that are genuinely company-related (not personal).
    """
    emails: set = set()

    # Strategy 1: data-cfemail attributes (Cloudflare obfuscation)
    for el in soup.find_all(attrs={"data-cfemail": True}):
        decoded = decode_cf_email(el["data-cfemail"])
        if decoded:
            emails.add(decoded)

    # Strategy 2: mailto: href links
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if EMAIL_RE.fullmatch(addr):
                emails.add(addr)

    # Strategy 3: Regex scan on all text nodes
    for text in soup.stripped_strings:
        for m in EMAIL_RE.finditer(text):
            emails.add(m.group(0))

    # Strategy 4: Scan raw HTML for any remaining patterns
    raw = str(soup)
    for m in EMAIL_RE.finditer(raw):
        emails.add(m.group(0))

    # Filter obvious false positives (asset filenames, tracking pixels, etc.)
    emails = {e for e in emails if not NOISE_RE.search(e) and len(e) > 5}
    print(
        f"[EMAILS] Found {len(emails)}: {', '.join(sorted(emails)[:8])}"
        f"{'…' if len(emails) > 8 else ''}"
    )
    return emails


def extract_emails_from_driver(driver: webdriver.Chrome) -> set:
    """
    Extract emails from a live Selenium page (handles CF-protected emails
    by reading DOM attributes directly before page_source snapshot).
    """
    emails: set = set()

    # JS-side: collect all data-cfemail attributes and decode them
    try:
        cf_emails = driver.execute_script(
            """
            var results = [];
            document.querySelectorAll('[data-cfemail]').forEach(function(el) {
                results.push(el.getAttribute('data-cfemail'));
            });
            return results;
            """
        )
        for encoded in cf_emails or []:
            decoded = decode_cf_email(encoded)
            if decoded:
                emails.add(decoded)
    except Exception:
        pass

    # JS-side: collect all mailto: hrefs
    try:
        mailtos = driver.execute_script(
            """
            var results = [];
            document.querySelectorAll('a[href^="mailto:"]').forEach(function(a) {
                results.push(a.getAttribute('href'));
            });
            return results;
            """
        )
        for href in mailtos or []:
            addr = href.replace("mailto:", "").split("?")[0].strip()
            if EMAIL_RE.fullmatch(addr):
                emails.add(addr)
    except Exception:
        pass

    # BeautifulSoup scan of the full page source
    soup = BeautifulSoup(driver.page_source, "lxml")
    emails |= extract_emails_from_soup(soup)
    return emails


# ─────────────────────────── Page fetch ──────────────────────────────────────

# How long (seconds) to wait for each condition before giving up.
# Raise these only if you're on a very slow connection.
_WAIT_PAGE_LOAD  = 15   # main content to appear after navigation
_WAIT_TAB_PANEL  = 6    # tab panel to become visible after click
_WAIT_COOKIE_SET = 1    # brief pause so cookies are committed before refresh


def _wait_for_page_ready(driver: webdriver.Chrome, timeout: int = _WAIT_PAGE_LOAD) -> None:
    """Wait until JS reports the document is interactive/complete."""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
        )
    except Exception:
        pass


def _wait_for_content(driver: webdriver.Chrome, timeout: int = _WAIT_PAGE_LOAD) -> None:
    """Wait for the first meaningful content element to appear."""
    try:
        WebDriverWait(driver, timeout).until(
            EC.any_of(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#people table tbody tr")),
                EC.presence_of_element_located((By.CSS_SELECTOR, "div[role='tabpanel']")),
                EC.presence_of_element_located((By.TAG_NAME, "tbody")),
            )
        )
    except Exception:
        print("[WARN] Timed out waiting for content; continuing anyway")


def fetch_page_soup(
    driver: webdriver.Chrome,
    company_url: str,
    use_cookies: bool = True,
) -> BeautifulSoup:
    """
    Load a RecordOwl company page, inject cookies if needed, activate every
    tab so its content is rendered, then return a BeautifulSoup snapshot.

    All waits are condition-based (WebDriverWait) — no fixed time.sleep()
    except a single 1-second cookie-commit pause.
    """
    print(f"[FETCH] {company_url}")

    if use_cookies:
        driver.get(BASE_URL)
        _wait_for_page_ready(driver)
        load_cookies(driver, COOKIE_FILE)
        time.sleep(_WAIT_COOKIE_SET)   # only sleep: let browser commit cookies
        driver.refresh()
        _wait_for_page_ready(driver)

    driver.get(company_url)
    _wait_for_content(driver)

    # Scroll once to trigger lazy-load, then activate each tab
    _scroll_full_page(driver)

    for tab in ["overview", "people", "financials", "valuation", "reviews"]:
        try:
            _activate_tab(driver, tab)
            # Give Cloudflare email obfuscation + AJAX time to render each tab
            # before we click the next one. Also ensures sequential AJAX completes.
            time.sleep(2)
        except Exception:
            pass

    return BeautifulSoup(driver.page_source, "lxml")


def _scroll_full_page(driver: webdriver.Chrome) -> None:
    """
    Scroll the page in large steps to trigger lazy-load events.
    No fixed sleep per step — just enough to let the browser paint.
    """
    scroll_height = driver.execute_script("return document.body.scrollHeight")
    # 4 steps is enough to hit all intersection observers
    step = max(600, scroll_height // 4)
    for pos in range(0, scroll_height + step, step):
        driver.execute_script(f"window.scrollTo(0, {pos});")
    driver.execute_script("window.scrollTo(0, 0);")


def _activate_tab(driver: webdriver.Chrome, tab_id: str) -> bool:
    """
    Click the tab button for tab_id and wait until its panel is visible.
    Uses WebDriverWait instead of a fixed sleep.
    """
    btn = None

    # Try aria-controls attribute first
    try:
        btn = driver.find_element(
            By.CSS_SELECTOR, f"[role='tab'][aria-controls='{tab_id}']"
        )
    except Exception:
        pass

    # Fallback: match tab label text
    if btn is None:
        try:
            for candidate in driver.find_elements(By.CSS_SELECTOR, "[role='tab']"):
                if tab_id.lower() in candidate.text.lower():
                    btn = candidate
                    break
        except Exception:
            pass

    if btn is None:
        return False

    driver.execute_script("arguments[0].click();", btn)

    # Wait until the panel for this tab is actually visible
    try:
        WebDriverWait(driver, _WAIT_TAB_PANEL).until(
            EC.any_of(
                EC.visibility_of_element_located((By.CSS_SELECTOR, f"#{tab_id}")),
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, f"[aria-labelledby][id='{tab_id}']")
                ),
            )
        )
    except Exception:
        pass  # panel may not have a matching id — content is still rendered

    return True


# ─────────────────────────── Text utilities ──────────────────────────────────
def safe_text(el) -> Optional[str]:
    """Return normalised inner text of a BS4 element, None if blank/dash."""
    if not el:
        return None
    t = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
    return None if t in {"", "-", "—", "N/A", "n/a"} else t


def first_text(container, *selectors: str) -> Optional[str]:
    """Return first non-empty text found via any of the CSS selectors."""
    for sel in selectors:
        try:
            el = container.select_one(sel)
            v = safe_text(el)
            if v:
                return v
        except Exception:
            pass
    return None


def text_or_attr(el, attr: str = "href") -> Optional[str]:
    if not el:
        return None
    v = el.get(attr)
    return v.strip() if v else safe_text(el)


def parse_definition_list(container) -> dict:
    data: dict = {}
    for row in container.select("dl > div"):
        dt = row.select_one("dt")
        dd = row.select_one("dd")
        if not dt or not dd:
            continue
        key   = re.sub(r"\s+", "_", (safe_text(dt) or "").lower())
        value = safe_text(dd)
        if key and value:
            data[key] = value
    return data


def extract_table_rows(table) -> list:
    if not table:
        return []
    return [
        [safe_text(cell) for cell in row.select("td")]
        for row in table.select("tbody tr")
        if any(safe_text(c) for c in row.select("td"))
    ]


def tab_panel(soup: BeautifulSoup, panel_id: str) -> BeautifulSoup:
    return soup.select_one(f"#{panel_id}[role='tabpanel'], #{panel_id}") or soup


def card_by_heading(container, heading: str):
    for title in container.select("h2, h3, h4"):
        if safe_text(title) == heading:
            return (
                title.find_parent("div", class_=lambda c: c and "shadow" in c)
                or title.find_parent("div")
            )
    return None


def to_slug(name: str) -> str:
    """Convert company name or URL slug to RecordOwl URL slug."""
    if name.startswith("http"):
        name = name.rstrip("/").split("/company/")[-1]
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9\-\s]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return re.sub(r"-+", "-", slug).strip("-")


# ─────────────────────────── Section parsers ─────────────────────────────────
def parse_people(soup: BeautifulSoup, page_emails: set) -> list:
    """
    Parse the Company Contacts / Officers table.

    Actual DOM structure (from rendered HTML):
      Cell 0 — Name & Position:
        <div>                           ← flex wrapper
          <div>P</div>                  ← avatar letter (skip)
          <div>                         ← content block
            <div>Peter Seah Lim Huat</div>   ← name (first inner div)
            <div>-</div>                     ← position (second inner div)
          </div>
        </div>

      Cell 1 — Email:
        <div>amitsinha@dbs.com</div>    ← plain text OR CF-encoded OR mailto

      Cell 2 — Contact Number: <div>-</div>
      Cell 3 — Fax Number:     <div>-</div>

    Email rules:
      1. CF data-cfemail attribute in cell → decode
      2. mailto: href in cell             → extract
      3. Plain-text regex match in cell   → use if passes noise filter
      4. Nothing found                    → None  (never fall back to pool)
    """
    # The people panel starts with class="hidden" in the static HTML.
    # BeautifulSoup sees it regardless of CSS visibility, so we can
    # parse it directly without needing JS to show the tab.
    panel = tab_panel(soup, "people")
    people: list = []

    rows = panel.select("table tbody tr")
    if not rows:
        rows = soup.select("table tbody tr")

    for row in rows:
        cells = row.select("td")
        if len(cells) < 2:
            continue

        # ── Cell 0: Name & Position ───────────────────────────────────────
        name_cell = cells[0]

        # The cell contains: [avatar-div] [content-div]
        # The content-div contains: [name-div] [position-div]
        # We want the second top-level div child (content block).
        top_divs = name_cell.find_all("div", recursive=False)
        if len(top_divs) >= 2:
            content_block = top_divs[1]
        else:
            content_block = name_cell

        inner_divs = content_block.find_all("div", recursive=False)
        name     = safe_text(inner_divs[0]) if len(inner_divs) > 0 else None
        position = safe_text(inner_divs[1]) if len(inner_divs) > 1 else None

        # Fallback: try class-based selectors from older layout
        if not name:
            name = (
                safe_text(name_cell.select_one("div.text-sm.font-medium.text-gray-900"))
                or safe_text(name_cell.select_one("[class*='font-medium']"))
            )
        if not position:
            position = safe_text(
                name_cell.select_one("div.text-sm.text-gray-500")
                or name_cell.select_one("div.text-gray-500")
            )

        # ── Cell 1: Email ─────────────────────────────────────────────────
        email: Optional[str] = None
        email_cell = cells[1]

        # 1. Cloudflare obfuscated email
        cf_el = email_cell.find(attrs={"data-cfemail": True})
        if cf_el:
            email = decode_cf_email(cf_el["data-cfemail"])

        # 2. mailto: href
        if not email:
            mailto_el = email_cell.find(
                "a", href=lambda h: h and h.startswith("mailto:")
            )
            if mailto_el:
                addr = mailto_el["href"][7:].split("?")[0].strip()
                if EMAIL_RE.fullmatch(addr):
                    email = addr

        # 3. Plain visible text (most common in this site's layout)
        if not email:
            cell_text = email_cell.get_text(separator=" ", strip=True)
            m = EMAIL_RE.search(cell_text)
            if m:
                candidate = m.group(0)
                if not NOISE_RE.search(candidate):
                    email = candidate

        # ── Cell 2 & 3: Contact / Fax ─────────────────────────────────────
        def clean_phone(text: Optional[str]) -> Optional[str]:
            if not text:
                return None
            m = PHONE_RE.search(text)
            return m.group(0).strip() if m else None

        contact_number = clean_phone(
            cells[2].get_text(strip=True) if len(cells) > 2 else None
        )
        fax_number = clean_phone(
            cells[3].get_text(strip=True) if len(cells) > 3 else None
        )

        person: dict = {
            "name":           name,
            "position":       position,
            "email":          email,
            "contact_number": contact_number,
            "fax_number":     fax_number,
        }
        if any(person.values()):
            people.append(person)

    return people


def parse_overview(soup: BeautifulSoup) -> dict:
    """Parse company overview section."""
    overview = tab_panel(soup, "overview")
    data: dict = {}

    data["company_name"] = first_text(
        soup, "h1[itemprop='name']", "h1.text-2xl", "h1"
    )

    # Definition list (registration number, status, address, etc.)
    data.update(parse_definition_list(overview))

    # Website
    website_dt = overview.find("dt", string=re.compile(r"^Website$", re.I))
    if website_dt:
        data["website"] = text_or_attr(website_dt.find_next("a")) or data.get("website")

    # Operating status badge
    status_badge = overview.select_one(".rounded-full, .badge, [class*='status']")
    if status_badge and not data.get("operating_status"):
        data["operating_status"] = safe_text(status_badge)

    # Description
    for h3 in overview.select("h3"):
        if (safe_text(h3) or "").lower().startswith("about "):
            data["description"] = safe_text(h3.find_next_sibling("p"))
            break

    # Timeline
    timeline = []
    for item in overview.select("ul > li"):
        event = safe_text(item.select_one(".font-medium, span.font-medium"))
        date  = safe_text(item.select_one("p.text-gray-500, p.text-sm"))
        if event and date:
            timeline.append({"date": date, "event": event})
    if timeline:
        data["company_timeline"] = timeline

    # News articles
    news_card = card_by_heading(overview, "In the News")
    if news_card:
        articles = []
        for item in news_card.select("[itemtype='http://schema.org/NewsArticle']"):
            link    = item.select_one("[itemprop='headline'] a, h4 a")
            date_el = item.select_one("[itemprop='datePublished'], time")
            desc_el = item.select_one("[itemprop='description'], p.text-sm")
            article = {
                "title":          safe_text(link),
                "url":            text_or_attr(link),
                "published_date": safe_text(date_el),
                "description":    safe_text(desc_el),
            }
            if any(article.values()):
                articles.append(article)
        if articles:
            data["news"] = articles

    return data


def parse_financials(soup: BeautifulSoup) -> dict:
    """Parse financials section."""
    financials = tab_panel(soup, "financials")
    data: dict = parse_definition_list(financials)

    share_card = card_by_heading(financials, "Shareholding Structure")
    if share_card:
        rows = extract_table_rows(share_card.select_one("table"))
        shares = [
            {"share_type": r[0], "quantity": r[1], "currency": r[2]}
            for r in rows
            if len(r) >= 3
        ]
        if shares:
            data["shareholding_structure"] = shares

    auditor_card = card_by_heading(financials, "Auditor Information")
    if auditor_card:
        data["auditor_name"] = safe_text(
            auditor_card.select_one("a[href*='/company/']")
        )
        addr_spans = [safe_text(s) for s in auditor_card.select("span.text-sm")]
        addr_spans = [a for a in addr_spans if a]
        if addr_spans:
            data["auditor_address"] = addr_spans[0]

    return data


def parse_valuation(soup: BeautifulSoup) -> dict:
    """Parse valuation section."""
    valuation = tab_panel(soup, "valuation")
    data: dict = parse_definition_list(valuation)

    ipo_card = card_by_heading(valuation, "IPO Status")
    if ipo_card:
        data["ipo_status"] = safe_text(ipo_card.select_one(".rounded-full, p, span"))

    indicators_card = card_by_heading(valuation, "Company Value Indicators")
    if indicators_card:
        data["value_indicators"] = parse_definition_list(indicators_card) or None

    return data


def parse_reviews(soup: BeautifulSoup) -> list:
    """Parse reviews section."""
    reviews_panel = tab_panel(soup, "reviews")
    reviews = []
    for review in reviews_panel.select(".review-card, .review-item, .review"):
        rd = {
            "title":  safe_text(review.select_one(".review-title, h3")),
            "author": safe_text(review.select_one(".review-author, .author")),
            "rating": safe_text(review.select_one(".review-rating, .rating")),
            "date":   safe_text(review.select_one(".review-date, .date, time")),
            "text":   safe_text(review.select_one(".review-text, .text, p")),
        }
        if any(rd.values()):
            reviews.append(rd)
    return reviews


# ─────────────────────────── 404 pre-check ───────────────────────────────────

def _url_is_404(company_url: str) -> bool:
    """
    Cheap HEAD request to short-circuit dead pages before launching Selenium.
    A 30s Selenium retry on a 404 wastes time when a 200ms HEAD answers it.
    Returns True only if the response is unambiguously 404; any other status
    (or a network error) returns False so the caller falls through to Selenium.
    """
    try:
        r = requests.head(
            company_url,
            allow_redirects=True,
            timeout=10,
            headers={"User-Agent": random.choice(USER_AGENTS)},
        )
    except requests.RequestException:
        return False
    return r.status_code == 404


def _not_found_result(company_url: str, slug: str, use_cookies: bool) -> dict:
    """Shape-compatible result for a confirmed-404 page (no scrape attempted)."""
    return {
        "source": company_url,
        "slug":   slug,
        "auth": {
            "mode":      "cookies" if use_cookies else "none",
            "available": os.path.exists(COOKIE_FILE) if use_cookies else False,
        },
        "not_found":               True,
        "request_required":        False,
        "financials_request_only": False,
        "overview":   {},
        "people":     [],
        "financials": {},
        "valuation":  {},
        "reviews":    [],
        "page_emails": [],
    }


# ─────────────────────────── Main scrape entry ───────────────────────────────
def scrape_company(company: str, use_cookies: bool = True) -> dict:
    """
    Scrape a RecordOwl company page and return structured JSON data.

    Args:
        company:     Company slug ("psa-international-pte-ltd"),
                     full name ("PSA International Pte Ltd"),
                     or full URL.
        use_cookies: Whether to inject saved authentication cookies.

    Returns:
        Dict with keys: source, slug, auth, overview, people,
                        financials, valuation, reviews.
    """
    # SG scraping regulation: check robots.txt before fetching (once per process).
    check_robots_txt(BASE_URL, "/company/")

    slug        = to_slug(company)
    company_url = f"{BASE_URL}/company/{slug}"

    # Short-circuit 404s without spinning up Chrome / cookies / retries.
    if _url_is_404(company_url):
        print(f"[404] {company_url} — skipping (page does not exist)")
        return _not_found_result(company_url, slug, use_cookies)

    driver = create_driver()

    try:
        soup        = fetch_page_soup(driver, company_url, use_cookies)
        page_emails = extract_emails_from_driver(driver)

        # Detect RecordOwl "Request For Information" gate.
        # The People tab renders a button #request-team-info-btn ("Request Team
        # Information") in place of real officer/email data. Company is useless
        # for lead-gen when this is present.
        team_locked = bool(soup.find(id="request-team-info-btn"))
        fin_locked  = bool(soup.find(id="request-financial-info-btn"))

        result = {
            "source": company_url,
            "slug":   slug,
            "auth": {
                "mode":      "cookies" if use_cookies else "none",
                "available": os.path.exists(COOKIE_FILE) if use_cookies else False,
            },
            "request_required":        team_locked,
            "financials_request_only": fin_locked,
            "overview":   parse_overview(soup),
            "people":     [] if team_locked else parse_people(soup, page_emails),
            "financials": parse_financials(soup),
            "valuation":  parse_valuation(soup),
            "reviews":    parse_reviews(soup),
            # Raw email harvest from the whole page (footer, overview, etc.).
            # Used as a fallback in _save_to_contacts when the People tab
            # is RFI-locked so companies with a visible business email
            # (e.g. sales@company.com in the overview) are still captured.
            "page_emails": sorted(page_emails),
        }

        if team_locked:
            print(f"[RFI] {slug} — people tab is locked (Request Team Information). "
                  f"No contact data available; caller should skip.")
        print(
            f"[OK] Scraped {slug} — "
            f"{len(result['people'])} contacts, "
            f"{len(page_emails)} emails found on page"
        )
        return result

    except Exception as e:
        print(f"[ERROR] {e}")
        raise
    finally:
        driver.quit()


# ─────────────────────────── CLI ─────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape company data from RecordOwl using Selenium"
    )
    parser.add_argument("company", help="Company name, slug, or full RecordOwl URL")
    parser.add_argument("--output", "-o", help="Save JSON to this file path")
    parser.add_argument("--no-auth", action="store_true", help="Skip cookie authentication")
    args = parser.parse_args()

    print(f"[START] {args.company}")
    result = scrape_company(args.company, use_cookies=not args.no_auth)
    out    = json.dumps(result, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"[SAVED] {args.output}")
    else:
        print(out)


if __name__ == "__main__":
    main()