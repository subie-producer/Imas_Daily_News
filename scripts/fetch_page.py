#!/usr/bin/env python3
"""fetch_page: 出典ページの本文を**要約せずに**取り出す(執筆時の出典照合用)。

  python3 scripts/fetch_page.py <URL> [--chars 40000]

執筆セッション(codex)がこれを実行して出典を読み直す。LLM の要約器を通さないことが
このスクリプトの存在理由:

- WebFetch のような「小さいモデルで要約してから渡す」経路だと、ラベルが落ちる。
  実際 2026-08-26号の出典ページには「受付期間 8/8〜8/24」「当落発表 8/26」
  「入金期間 8/26〜8/30」が並んでいたのに、要約経由では期間が1つも残らなかった。
- 当選者向けの入金期間が「販売期間」に化け、誰でも買える期間として報じられた。

先頭に、機械抽出したラベル付き期間(編集規程15)を出す。ここが本文と食い違うことは
ないので、期間はこのブロックを正とする。

**既定の出力上限は大きく取る。**6000字にしていたとき、本文8,219字のページが
末尾(出演者一覧・注意事項)ごと切れていた。切るくらいなら全部渡す。
1ページの本文はたいてい数千字で、読ませても執筆1回ぶんの負担は小さい。
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import RenderFailed, extract_periods, html_to_text, needs_render, render_page

UA = "Mozilla/5.0 (compatible; ImasNewsCollect/1.0)"
FIXED_PAGES_ENV = "IMAS_FETCH_PAGES"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as res:
        if res.status >= 400:
            return ""
        return html_to_text(res.read(400_000), res.headers.get_content_charset())


def fixed_page(pages_file: str, url: str) -> str:
    """規則の事例(scripts/rule_bench.py)が固定で持たせた出典の本文({url: 本文} の JSON)。事例に無いページは空(= 取れない)。
    事例は毎回ページを取りに行かず、事例の外のページも読ませない(2026-10-14 監査指摘: 依頼文の規則を固定の事例でモデルに判断させる)"""
    pages = json.loads(Path(pages_file).read_text(encoding="utf-8"))
    for u in (url, url.rstrip("/"), url.rstrip("/") + "/"):
        if u in pages:
            return pages[u]
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--chars", type=int, default=40000, help="本文の出力上限")
    args = ap.parse_args()

    fixed = os.environ.get(FIXED_PAGES_ENV, "")
    try:
        text = fixed_page(fixed, args.url) if fixed else fetch(args.url)
    except Exception as e:
        print(f"FETCH_FAILED: {type(e).__name__}: {e}")
        return 1

    # JS で描画するページは本文が空同然になる。その場合と、描画必須と分かっているサイトは描画してから読み直す
    # (固定の本文は描画したあとのもの。取りに行かない)
    render_error = ""
    if not fixed and needs_render(args.url, text):
        try:
            text = html_to_text(render_page(args.url, 30).encode("utf-8", "replace"))
        except RenderFailed as e:
            # 素の本文で続けるが、描画できなかった原因(終了コードと stderr の要点)は読み手に見せる
            render_error = str(e)
            print(f"RENDER_FAILED: {e}", file=sys.stderr)

    if not text.strip():
        print("FETCH_FAILED: 本文を取得できませんでした" + (f"({render_error})" if render_error else ""))
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
    # 空行の連なりは1つに詰める。描画した公式ポータルの告知はタイトルと本文の間に百行を超える空行があり、
    # `| head` のように行数で切って読むと本文が見えずタイトルだけになる(2026-10-13 当番の再現: IM＠S RADIO 告知)
    text = re.sub(r"\n[ \t\u3000]*(?:\n[ \t\u3000]*)+", "\n\n", text).strip()
    print(text[:args.chars])
    return 0


if __name__ == "__main__":
    sys.exit(main())
