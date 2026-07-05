#!/usr/bin/env python3
"""Amazon Keyword Placement Checker.

Opens a real browser, searches Amazon for each keyword in config.json,
and reports where your brand's products rank on the search results pages
(organic rank + sponsored placements).

Usage:
    python3 placement_checker.py                 # uses config.json
    python3 placement_checker.py my_config.json  # custom config file
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

RESULT_CARD = 'div.s-main-slot div[data-component-type="s-search-result"]'
CAPTCHA_MARKERS = ["Enter the characters you see", "Type the characters you see",
                   "not a robot", "api-services-support@amazon.com"]


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("marketplace", "https://www.amazon.in")
    cfg.setdefault("max_pages", 3)
    cfg.setdefault("headless", False)
    cfg.setdefault("min_delay_seconds", 1)
    cfg.setdefault("max_delay_seconds", 2.5)
    cfg["brand_asins"] = [a.strip().upper() for a in cfg.get("brand_asins", []) if a.strip()]
    cfg["brand_name"] = cfg.get("brand_name", "").strip()
    if not cfg.get("keywords"):
        sys.exit("No keywords in config. Add them under \"keywords\".")
    if not cfg["brand_asins"] and not cfg["brand_name"]:
        sys.exit("Add at least brand_asins or brand_name to the config so I can identify your products.")
    return cfg


def human_pause(cfg, factor=1.0):
    time.sleep(random.uniform(cfg["min_delay_seconds"], cfg["max_delay_seconds"]) * factor)


BLOCKED_RESOURCES = ("image", "media", "font")


def _route_handler(route):
    if route.request.resource_type in BLOCKED_RESOURCES:
        route.abort()
    else:
        route.continue_()


def make_context(browser):
    """Browser context with realistic fingerprint; images/fonts blocked for speed
    (product titles come from the DOM, so blocking images doesn't lose data)."""
    context = browser.new_context(
        viewport={"width": 1440, "height": 900},
        locale="en-IN",
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"),
    )
    context.route("**/*", _route_handler)
    return context


def wait_out_captcha(page):
    """If Amazon throws a captcha, wait for the user to solve it in the open browser."""
    body = page.inner_text("body", timeout=15000)
    if any(m.lower() in body.lower() for m in CAPTCHA_MARKERS):
        if os.environ.get("CI"):
            # No human to solve it on a CI runner — fail with a clear reason.
            raise RuntimeError("Amazon showed a CAPTCHA/robot check — this runner's "
                               "IP is blocked. Datacenter IPs (GitHub Actions) are "
                               "often blocked by Amazon; use the Bright Data version "
                               "for reliable cloud runs.")
        print("\n  !! Amazon is showing a CAPTCHA. Please solve it in the browser window...")
        while True:
            time.sleep(3)
            body = page.inner_text("body", timeout=15000)
            if not any(m.lower() in body.lower() for m in CAPTCHA_MARKERS):
                print("  CAPTCHA cleared, continuing.")
                time.sleep(2)
                return


def scroll_full_page(page):
    """Scroll down in steps so lazy-loaded result cards render."""
    height = page.evaluate("document.body.scrollHeight")
    pos = 0
    while pos < height:
        pos += 1000
        page.evaluate("window.scrollTo(0, %d)" % pos)
        time.sleep(0.12)
        height = page.evaluate("document.body.scrollHeight")


def extract_results(page):
    """Return list of dicts for every product card on the current results page."""
    cards = page.query_selector_all(RESULT_CARD)
    out = []
    for card in cards:
        asin = (card.get_attribute("data-asin") or "").strip().upper()
        if not asin:
            continue
        # Cards can have one h2 (full title) or two (first = brand label,
        # second = product title) — brand-keyword searches use the two-h2 layout.
        h2_texts = []
        for h in card.query_selector_all("h2"):
            t = h.inner_text().strip()
            if t:
                h2_texts.append(t)
        brand = ""
        title = ""
        if len(h2_texts) >= 2:
            brand = h2_texts[0]
            title = max(h2_texts[1:], key=len)
        elif h2_texts:
            title = h2_texts[0]

        img = card.query_selector("img.s-image")
        alt = ""
        if img:
            alt = re.sub(r"^Sponsored Ad\s*[-–]\s*", "",
                         (img.get_attribute("alt") or "").strip())
        if not title:
            title = alt
        elif not brand and alt and len(title) < 25 and len(alt) > len(title) * 2:
            # single short h2 is likely just the brand label; alt has the full title
            brand, title = title, alt
        card_text = ""
        try:
            card_text = card.inner_text()
        except Exception:
            pass
        sponsored = bool(re.search(r"^\s*sponsored\s*$", card_text, re.IGNORECASE | re.MULTILINE))
        out.append({"asin": asin, "title": title, "brand": brand, "sponsored": sponsored})
    return out


def check_keyword(page, cfg, keyword):
    """Search one keyword and return placement rows for brand products."""
    brand_asins = set(cfg["brand_asins"])
    brand_name_lc = cfg["brand_name"].lower()
    rows = []
    abs_position = 0   # position counting every product card (sponsored + organic)
    organic_rank = 0   # position counting organic cards only

    for page_num in range(1, cfg["max_pages"] + 1):
        url = "%s/s?k=%s&page=%d" % (cfg["marketplace"].rstrip("/"), quote_plus(keyword), page_num)
        try:
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
        except PWTimeout:
            print("    page %d timed out, skipping" % page_num)
            continue
        wait_out_captcha(page)
        try:
            page.wait_for_selector(RESULT_CARD, timeout=20000)
        except PWTimeout:
            print("    no results found on page %d" % page_num)
            break
        scroll_full_page(page)
        results = extract_results(page)
        if not results:
            break

        for idx, r in enumerate(results, 1):
            abs_position += 1
            if not r["sponsored"]:
                organic_rank += 1
            matched_by = None
            if r["asin"] in brand_asins:
                matched_by = "ASIN"
            elif brand_name_lc and brand_name_lc in ("%s %s" % (r["brand"], r["title"])).lower():
                matched_by = "brand name"
            if matched_by:
                if r["brand"] and r["brand"].lower() not in r["title"].lower():
                    full_title = "%s %s" % (r["brand"], r["title"])
                else:
                    full_title = r["title"]
                rows.append({
                    "keyword": keyword,
                    "asin": r["asin"],
                    "product_title": full_title[:150],
                    "placement_type": "Sponsored" if r["sponsored"] else "Organic",
                    "page": page_num,
                    "position_on_page": idx,
                    "overall_position": abs_position,
                    "organic_rank": None if r["sponsored"] else organic_rank,
                    "matched_by": matched_by,
                })
        print("    page %d: %d products scanned, %d brand placements so far"
              % (page_num, abs_position, len(rows)))
        human_pause(cfg)

    if not rows:
        rows.append({
            "keyword": keyword, "asin": "-", "product_title": "NOT FOUND",
            "placement_type": "-", "page": "-", "position_on_page": "-",
            "overall_position": "> %d" % abs_position, "organic_rank": "-",
            "matched_by": "-",
        })
    return rows


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    cfg = load_config(cfg_path)
    print("Marketplace : %s" % cfg["marketplace"])
    print("Brand       : %s | ASINs: %s" % (cfg["brand_name"] or "-", ", ".join(cfg["brand_asins"]) or "-"))
    print("Keywords    : %d | Pages per keyword: %d\n" % (len(cfg["keywords"]), cfg["max_pages"]))

    all_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg["headless"],
                                    args=["--disable-blink-features=AutomationControlled"])
        page = make_context(browser).new_page()
        for i, kw in enumerate(cfg["keywords"], 1):
            print("[%d/%d] Searching: %s" % (i, len(cfg["keywords"]), kw))
            try:
                all_rows.extend(check_keyword(page, cfg, kw))
            except Exception as e:
                print("    ERROR on '%s': %s" % (kw, e))
                all_rows.append({"keyword": kw, "asin": "-", "product_title": "ERROR: %s" % e,
                                 "placement_type": "-", "page": "-", "position_on_page": "-",
                                 "overall_position": "-", "organic_rank": "-", "matched_by": "-"})
            human_pause(cfg)
        browser.close()

    df = pd.DataFrame(all_rows)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    out_dir = Path(__file__).resolve().parent / "reports"
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / ("placements_%s.csv" % stamp)
    xlsx_path = out_dir / ("placements_%s.xlsx" % stamp)
    df.to_csv(csv_path, index=False)
    df.to_excel(xlsx_path, index=False)

    print("\n================ PLACEMENT SUMMARY ================")
    print(df.to_string(index=False, max_colwidth=60))
    print("\nSaved: %s" % csv_path)
    print("Saved: %s" % xlsx_path)


if __name__ == "__main__":
    main()
