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
import json
import sys
import time

# selenium は描画するときだけ読む(429 の見分け document_status を、selenium の無い環境のテストからも動かせるように)

UA =("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# ページ(文書そのもの)が HTTP 429 を返したときの終了コード(pipelib.RENDER_RATE_LIMITED と同じ値。selenium を読み込む前に pipelib を読まない)
RATE_LIMITED = 75


class RateLimited(Exception):
    """文書の応答が HTTP 429(2026-10-26: 描画した 429 の画面を本文として返していた。呼び出し側が Retry-After を守って描画し直す)。
    url は 429 を返した文書の URL(転送されたなら行き先。呼び出し側は、そのホストに控えを付ける)"""
    def __init__(self, retry_after: str, url: str = ""):
        super().__init__(f"HTTP 429 url={url or '-'} retry-after={retry_after or '-'}")
        self.retry_after = retry_after


def document_status(entries) -> tuple[int | None, str, str]:
    """性能ログ(performance)から、最初の文書(Document)の応答の (状態コード, Retry-After, 応答の URL)。見つからなければ (None, "", "")。"""
    for e in entries or []:
        try:
            msg = json.loads(e.get("message") or "{}").get("message") or {}
        except (ValueError, AttributeError):
            continue
        if msg.get("method") != "Network.responseReceived":
            continue
        params = msg.get("params") or {}
        if params.get("type") != "Document":
            continue
        res = params.get("response") or {}
        heads = {str(k).lower(): str(v) for k, v in (res.get("headers") or {}).items()}
        status = res.get("status")
        return ((int(status) if isinstance(status, (int, float)) else None), heads.get("retry-after", "").strip(),
                str(res.get("url") or ""))
    return None, "", ""


def make_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,2400")
    opts.add_argument("--lang=ja-JP")
    opts.add_argument(f"--user-agent={UA}")
    # 自動操作の印を出さない。出すと Anime Store.JP などは Google のトップへ転送し、本文の代わりに検索ページを返す(2026-10-26)
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    # 画像は読み込まない。読むのは文字だけで、画像の多い一覧ページは画像の読み込みが終わらず読み込み完了を待つ所で
    # 時間切れになっていた(実測 2026-10-07: columbia.jp/idolmaster/ が 700件超の画像待ちで毎回 40 秒で落ちた)
    opts.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
    # 文書の応答の状態コードを読む(429 を見分ける)ため、性能ログを取る
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    return webdriver.Chrome(options=opts)


def fetch(url: str, wait_css: str | None, timeout: int) -> str:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    driver = make_driver()
    try:
        driver.set_page_load_timeout(timeout + 10)
        driver.get(url)
        try:
            status, after, where = document_status(driver.get_log("performance"))
        except Exception:       # noqa: BLE001  ログを取れないブラウザでは見分けない(従来どおり描画を返す)
            status, after, where = None, "", ""
        if status == 429:
            raise RateLimited(after, where or driver.current_url)
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
    try:
        html = fetch(args.url, args.wait_css, args.timeout)
    except RateLimited as e:
        print(f"RENDER_FAILED: {e}", file=sys.stderr)
        return RATE_LIMITED
    except Exception as e:
        # 呼び出し側(pipelib.render_page)が原因を記録できるよう、例外の要点を1行で出す。Selenium の例外は
        # 後ろに Chrome のスタックが続き、トレースバックの末尾だけでは何が起きたか読めない
        msg = (str(e).strip().splitlines() or [""])[0].removeprefix("Message:").strip()
        print(f"RENDER_FAILED: {type(e).__name__}: {msg}"[:300], file=sys.stderr)
        return 1
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"{len(html)} bytes -> {args.out}", file=sys.stderr)
    else:
        print(html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
