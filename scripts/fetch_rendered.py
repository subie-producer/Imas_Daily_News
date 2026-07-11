#!/usr/bin/env python3
"""ヘッドレスブラウザで JS レンダリング後のページを取得する(CSR サイトの定点観測用)。

Selenium Manager が Chrome for Testing と chromedriver を自動取得するため、
ブラウザの手動インストールは不要(初回のみダウンロードが走る)。

usage:
  .venv/bin/python scripts/fetch_rendered.py <URL> [--wait-css SELECTOR] [--timeout 20] [--out FILE]

--wait-css を指定すると、そのCSSセレクタが現れるまで待ってから page_source を出力する。
未指定時は document.readyState=complete 後さらに2秒待つ。
"""
import argparse
import sys
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,2400")
    opts.add_argument("--lang=ja-JP")
    opts.add_argument(f"--user-agent={UA}")
    return webdriver.Chrome(options=opts)


def fetch(url: str, wait_css: str | None, timeout: int) -> str:
    driver = make_driver()
    try:
        driver.set_page_load_timeout(timeout + 10)
        driver.get(url)
        if wait_css:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_css))
            )
        else:
            WebDriverWait(driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(2)  # CSR の描画待ち
        return driver.page_source
    finally:
        driver.quit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--wait-css", default=None)
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    html = fetch(args.url, args.wait_css, args.timeout)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"{len(html)} bytes -> {args.out}", file=sys.stderr)
    else:
        print(html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
