#!/usr/bin/env python3
"""fetch_page: 出典ページの本文を**要約せずに**取り出す(執筆時の出典照合用)。

  python3 scripts/fetch_page.py <URL> [--chars 6000]

執筆セッション(codex)がこれを実行して出典を読み直す。LLM の要約器を通さないことが
このスクリプトの存在理由:

- WebFetch のような「小さいモデルで要約してから渡す」経路だと、ラベルが落ちる。
  実際 2026-08-26号の出典ページには「受付期間 8/8〜8/24」「当落発表 8/26」
  「入金期間 8/26〜8/30」が並んでいたのに、要約経由では期間が1つも残らなかった。
- 当選者向けの入金期間が「販売期間」に化け、誰でも買える期間として報じられた。

先頭に、機械抽出したラベル付き期間(編集規程15)を出す。ここが本文と食い違うことは
ないので、期間はこのブロックを正とする。
"""
import argparse
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import ROOT, extract_periods, html_to_text

UA = "Mozilla/5.0 (compatible; ImasNewsCollect/1.0)"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as res:
        if res.status >= 400:
            return ""
        return html_to_text(res.read(400_000), res.headers.get_content_charset())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--chars", type=int, default=6000, help="本文の出力上限")
    args = ap.parse_args()

    try:
        text = fetch(args.url)
    except Exception as e:
        print(f"FETCH_FAILED: {type(e).__name__}: {e}")
        return 1

    # JS で描画するページは本文が空同然になる。その場合だけ描画してから読み直す
    if len(text.strip()) < 400:
        try:
            r = subprocess.run(
                [str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "scripts" / "fetch_rendered.py"),
                 args.url, "--timeout", "30"],
                capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and r.stdout:
                text = html_to_text(r.stdout.encode("utf-8", "replace"))
        except Exception:
            pass

    if not text.strip():
        print("FETCH_FAILED: 本文を取得できませんでした")
        return 1

    periods = extract_periods(text)
    print(f"URL: {args.url}")
    if periods:
        print("--- 期間(原文ラベルのまま。本文と食い違う場合はこちらが正) ---")
        for p in periods:
            print(f"  {p}")
    else:
        print("--- 期間: ラベル付きの期間は見つかりませんでした ---")
    print("--- 本文(要約なし) ---")
    print(text[:args.chars])
    return 0


if __name__ == "__main__":
    sys.exit(main())
