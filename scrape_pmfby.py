#!/usr/bin/env python3
"""
scrape_pmfby.py

Scrapes the PMFBY Admin Statistics dashboard
(https://pmfby.gov.in/adminStatistics/dashboard) and saves whatever
tables / stat cards are found into a CSV file that opens cleanly in Excel.

WHY PLAYWRIGHT (NOT requests/BeautifulSoup):
The dashboard is a client-side rendered app. The raw HTML response
contains no data - everything is injected by JavaScript after the page
loads and calls its internal APIs. A plain `requests.get()` will return
an almost-empty shell. Playwright launches a real (headless) browser,
waits for the JS to run, and then reads the fully-rendered DOM - this is
the only reliable way to get the numbers you see on screen.

USAGE:
    python scrape_pmfby.py [--output pmfby_dashboard.csv] [--headed] [--timeout 45000]

NOTES / CAVEATS:
- Government dashboards change their markup periodically. This script
  uses a few different generic strategies (HTML <table> elements, and
  common "stat card" div patterns) to be resilient, but if PMFBY
  redesigns the page you may need to update the CSS selectors in
  `extract_stat_cards()` / `extract_tables()` below.
- Some state/season/year filters on the dashboard may need to be
  selected via dropdowns before data appears. If you need a specific
  filter combination, see the `apply_filters()` stub - it's left as a
  clearly marked extension point since the exact dropdown IDs need to
  be confirmed against the live page in a real browser session.
- Respect the site's terms of service and robots.txt, and avoid
  hammering the server - this script does a single page load.
"""

import argparse
import csv
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

DASHBOARD_URL = "https://pmfby.gov.in/adminStatistics/dashboard"


def clean_text(s: str) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def extract_tables(page):
    """
    Find every <table> on the rendered page and turn each into a list of
    row dicts. Returns a list of (table_index, headers, rows).
    """
    results = []
    tables = page.query_selector_all("table")
    for t_idx, table in enumerate(tables):
        headers = [clean_text(th.inner_text()) for th in table.query_selector_all("thead th")]
        if not headers:
            # fall back to first row as header if no <thead>
            first_row = table.query_selector("tr")
            if first_row:
                headers = [clean_text(c.inner_text()) for c in first_row.query_selector_all("th, td")]

        body_rows = table.query_selector_all("tbody tr")
        if not body_rows:
            all_rows = table.query_selector_all("tr")
            body_rows = all_rows[1:] if len(all_rows) > 1 else all_rows

        rows = []
        for r in body_rows:
            cells = [clean_text(c.inner_text()) for c in r.query_selector_all("td, th")]
            if any(cells):
                rows.append(cells)

        if rows:
            results.append((t_idx, headers, rows))
    return results


def extract_stat_cards(page):
    """
    Many government dashboards show headline numbers in "card" widgets
    rather than tables (e.g. a div with a big number and a label
    underneath). This grabs common patterns as a label/value fallback.
    Adjust the selector list below if the real page uses different
    class names - inspect the page in a browser's DevTools (Elements
    tab) and update accordingly.
    """
    candidate_selectors = [
        ".card", ".stat-card", ".dashboard-card", ".counter-box",
        "[class*='card']", "[class*='count']", "[class*='stat']",
    ]
    seen = set()
    stats = []
    for sel in candidate_selectors:
        for el in page.query_selector_all(sel):
            text = clean_text(el.inner_text())
            if not text or text in seen or len(text) > 200:
                continue
            seen.add(text)
            stats.append(text)
    return stats


def apply_filters(page):
    """
    Extension point: if the dashboard requires selecting a Year/Season/
    State from dropdowns before showing data, add that interaction here,
    e.g.:

        page.select_option("select#yearId", label="2024-25")
        page.select_option("select#seasonId", label="Kharif")
        page.wait_for_timeout(2000)

    Left as a no-op by default since exact element IDs must be verified
    against the live, rendered page.
    """
    pass


def scrape(url: str, output: str, headless: bool, timeout_ms: int, scraped_at: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        print(f"Loading {url} ...")
        try:
            page.goto(url, wait_until="networkidle")
        except PWTimeoutError:
            print("Warning: networkidle timeout hit, continuing with whatever loaded so far.")

        # Give Angular/React a little extra time to paint after XHRs resolve.
        page.wait_for_timeout(3000)

        apply_filters(page)
        page.wait_for_timeout(1000)

        tables = extract_tables(page)
        stat_cards = extract_stat_cards(page)

        browser.close()

    if not tables and not stat_cards:
        print(
            "No tables or stat cards found. The site may require login, "
            "dropdown selections, or its markup has changed. Try running "
            "with --headed to watch it load and inspect the DOM manually."
        )

    out_path = Path(output)
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)

        # Timestamp header so anyone opening the CSV knows exactly when
        # this data was pulled.
        writer.writerow(["Scraped At (UTC)", scraped_at])
        writer.writerow(["Source URL", url])
        writer.writerow([])

        if stat_cards:
            writer.writerow(["Summary Stats"])
            writer.writerow(["value"])
            for s in stat_cards:
                writer.writerow([s])
            writer.writerow([])

        for t_idx, headers, rows in tables:
            writer.writerow([f"Table {t_idx + 1}"])
            if headers:
                writer.writerow(headers)
            for row in rows:
                writer.writerow(row)
            writer.writerow([])

    print(f"Saved {len(tables)} table(s) and {len(stat_cards)} stat item(s) to {out_path.resolve()}")


def main():
    now = datetime.now(timezone.utc)
    scraped_at_display = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    scraped_at_filename = now.strftime("%Y%m%d_%H%M%S")

    parser = argparse.ArgumentParser(description="Scrape the PMFBY admin statistics dashboard into a CSV.")
    parser.add_argument("--url", default=DASHBOARD_URL, help="Dashboard URL to scrape.")
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Output CSV file path. If omitted, defaults to "
            "'data/pmfby_dashboard_<UTC timestamp>.csv' so every run produces "
            "its own timestamped file instead of overwriting the last one."
        ),
    )
    parser.add_argument("--headed", action="store_true", help="Run browser with a visible window (for debugging).")
    parser.add_argument("--timeout", type=int, default=45000, help="Timeout in ms for page operations.")
    args = parser.parse_args()

    output = args.output
    if output is None:
        Path("data").mkdir(exist_ok=True)
        output = f"data/pmfby_dashboard_{scraped_at_filename}.csv"

    scrape(args.url, output, headless=not args.headed, timeout_ms=args.timeout, scraped_at=scraped_at_display)


if __name__ == "__main__":
    main()
