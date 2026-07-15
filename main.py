#!/usr/bin/env python3
"""
Scrape PMFBY admin statistics dashboard:
https://pmfby.gov.in/adminStatistics/dashboard

Outputs:
 - pmfby_admin_stats.json
 - pmfby_admin_stats_flat.csv

Requires:
 pip install selenium webdriver-manager bs4 requests pandas

Usage:
 python scrape_pmfby.py
"""
import json
import time
import csv
import re
from typing import List, Dict, Any

# Selenium imports
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# Fallback parser (not used when Selenium works)
import requests
from bs4 import BeautifulSoup
import pandas as pd

URL = "https://pmfby.gov.in/adminStatistics/dashboard"

NUM_RE = re.compile(r"[-+]?\d[\d,\.]*")

def clean_value(s: str):
    if s is None:
        return None
    s = s.strip()
    if s == "" or s.lower() in {"-", "na", "n/a"}:
        return None
    # remove non-breaking spaces etc
    s = s.replace("\xa0", "").replace(" ", "")
    # Keep decimals and commas, then transform to numeric if looks like number
    m = NUM_RE.search(s)
    if not m:
        return s
    raw = m.group(0)
    # If contains comma as thousands separator or lakh separators, remove commas
    raw_clean = raw.replace(",", "")
    # Try int then float
    try:
        if "." in raw_clean:
            return float(raw_clean)
        else:
            return int(raw_clean)
    except:
        return raw_clean

def parse_table_element(table) -> Dict[str, Any]:
    """
    Given a selenium WebElement pointing to a <table>, parse it into structured data.
    """
    result = {}
    # Get thead years
    thead = table.find_element(By.TAG_NAME, "thead")
    header_ths = thead.find_elements(By.CSS_SELECTOR, "tr th")
    # First header is "Year" or label, following are year columns
    years = [th.text.strip() for th in header_ths[1:]]  # skip first label
    result["years"] = years

    tbody = table.find_element(By.TAG_NAME, "tbody")
    rows = tbody.find_elements(By.CSS_SELECTOR, "tr")
    current_section = None
    data = {}
    # Iterate rows
    for r in rows:
        # Some rows are section headers: <td colspan> with Season or highlighted titles
        try:
            cols = r.find_elements(By.TAG_NAME, "td")
            if len(cols) == 1:
                txt = cols[0].text.strip()
                if txt:
                    # identify Season or section label inside
                    if "Season" in txt or "Notification" in txt or "Coverage" in txt or "Demographic" in txt or "Premium" in txt or "Claim" in txt:
                        # use this as current section title
                        current_section = txt
                        if current_section not in data:
                            data[current_section] = {}
                        continue
                    # sometimes season appears in a <strong> element:
                    # try to extract season from inner strong text
                continue
            # Normal data rows: first col is label, others correspond to years
            label = cols[0].text.strip()
            values = [clean_value(c.text) for c in cols[1:1+len(years)]]
            row_map = {year: val for year, val in zip(years, values)}
            # put into the right section; fallback to top-level if no current_section
            sec = current_section or "General"
            data.setdefault(sec, {})[label] = row_map
        except Exception:
            # skip problematic rows
            continue
    result["data"] = data
    return result

def scrape_with_selenium():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    # Create driver (webdriver-manager will fetch chromedriver)
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    try:
        driver.get(URL)
        wait = WebDriverWait(driver, 20)
        # Wait for at least one table to appear (tables use class 'dashboardTableDT' or id 'homeID')
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table.dashboardTableDT, table#homeID")))
        time.sleep(1)  # small extra wait for React render
        tables = driver.find_elements(By.TAG_NAME, "table")
        tables_parsed = []
        for t in tables:
            # ignore empty or unrelated tables
            try:
                if len(t.find_elements(By.TAG_NAME, "thead")) == 0:
                    continue
                parsed = parse_table_element(t)
                # If it has meaningful years and data, accept
                if parsed.get("years") and parsed.get("data"):
                    tables_parsed.append(parsed)
            except Exception:
                continue
        return tables_parsed
    finally:
        driver.quit()

def flatten_and_save(parsed_tables: List[Dict[str, Any]]):
    # Save raw JSON
    with open("pmfby_admin_stats.json", "w", encoding="utf-8") as f:
        json.dump(parsed_tables, f, ensure_ascii=False, indent=2)

    # Flatten for CSV: columns: season, section, metric, year, value
    rows = []
    for tbl in parsed_tables:
        years = tbl.get("years", [])
        data = tbl.get("data", {})
        for section, metrics in data.items():
            for metric_label, year_map in metrics.items():
                for year in years:
                    rows.append({
                        "season_or_section": section,
                        "metric": metric_label,
                        "year": year,
                        "value": year_map.get(year)
                    })
    df = pd.DataFrame(rows)
    df.to_csv("pmfby_admin_stats_flat.csv", index=False)
    print("Saved pmfby_admin_stats.json and pmfby_admin_stats_flat.csv")

def main():
    print("Starting Selenium scrape of:", URL)
    try:
        parsed = scrape_with_selenium()
        if not parsed:
            print("No tables parsed using Selenium, attempting fallback requests+BS4...")
            parsed = scrape_with_bs4()
    except Exception as e:
        print("Selenium failed:", e)
        parsed = scrape_with_bs4()

    if parsed:
        flatten_and_save(parsed)
    else:
        print("Failed to parse tables.")

# Fallback: requests + BeautifulSoup (tries to parse static HTML)
def parse_table_soup(table_soup) -> Dict[str, Any]:
    header_ths = table_soup.select("thead tr th")
    years = [th.get_text(strip=True) for th in header_ths[1:]]
    result = {"years": years, "data": {}}
    tbody = table_soup.find("tbody")
    if not tbody:
        return result
    rows = tbody.find_all("tr")
    current_section = None
    for r in rows:
        cols = r.find_all("td")
        if len(cols) == 1:
            txt = cols[0].get_text(" ", strip=True)
            if "Season" in txt or any(k in txt for k in ["Notification","Coverage","Demographic","Premium","Claim"]):
                current_section = txt
                result["data"].setdefault(current_section, {})
            continue
        if len(cols) >= 2:
            label = cols[0].get_text(" ", strip=True)
            vals = [clean_value(td.get_text(" ", strip=True)) for td in cols[1:1+len(years)]]
            row_map = {year: val for year, val in zip(years, vals)}
            sec = current_section or "General"
            result["data"].setdefault(sec, {})[label] = row_map
    return result

def scrape_with_bs4():
    resp = requests.get(URL, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")
    parsed = []
    for t in tables:
        if t.find("thead") is None:
            continue
        p = parse_table_soup(t)
        if p.get("years") and p.get("data"):
            parsed.append(p)
    return parsed

if __name__ == "__main__":
    main()
