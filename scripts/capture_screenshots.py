#!/usr/bin/env python3
"""Capture Trip Hub dashboard screenshots with Playwright.

Usage:
    BASE_URL=http://<pi-host>:5000 python scripts/capture_screenshots.py
    python scripts/capture_screenshots.py --url http://192.0.2.10:5000 --out docs/assets

No credentials are stored here; pass the dashboard URL via --url or $BASE_URL.
Requires: pip install playwright && playwright install chromium
"""
import argparse
import os
import sys

DEFAULT_URL = os.environ.get("BASE_URL", "http://raspberrypi.local:5000")
TABS = [
    ("Speed Distribution", "trip-hub-speed-distribution.png"),
    ("Speed Trend",        "trip-hub-speed-trend.png"),
    ("Activity Log",       "trip-hub-activity-log.png"),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL, help="dashboard base URL")
    ap.add_argument("--out", default=os.path.join("docs", "assets"),
                    help="output directory")
    ap.add_argument("--settle", type=int, default=4000,
                    help="ms to wait for map/data to settle")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright not installed. Run: pip install playwright && "
                 "playwright install chromium")

    os.makedirs(args.out, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(args.url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(args.settle)

        page.screenshot(path=os.path.join(args.out, "trip-hub-map.png"))
        print("saved trip-hub-map.png")
        try:
            page.locator(".chat-panel").screenshot(
                path=os.path.join(args.out, "trip-hub-chat.png"))
            print("saved trip-hub-chat.png")
        except Exception as e:
            print("chat-panel shot skipped:", e)

        for label, fname in TABS:
            try:
                page.click(f".view-tab:has-text('{label}')")
                page.wait_for_timeout(6000)  # let Chart.js paint
                page.screenshot(path=os.path.join(args.out, fname))
                print("saved", fname)
            except Exception as e:
                print(f"tab {label} skipped:", e)

        browser.close()
    print("done ->", args.out)


if __name__ == "__main__":
    main()
