#!/usr/bin/env python3
"""アプリが描く紙面と、以前の静的ビルド(Jekyll の出力)を全ページ比べる。

  python3 webapp/compare.py --site <版のフォルダ(docs と同じ並び)> --static <Jekyll の出力> [--show 5]

空白・HTML のコメント・文字参照の違いは比べない(見た目に出ない)。違ったページの数と、最初の違いの前後を出す。
違いが0件なら終了コード 0。
"""
import argparse
import difflib
import html
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote


def normalize(s: str) -> str:
    s = re.sub(r"<!--.*?-->", "", s, flags=re.DOTALL)
    s = re.sub(r'<meta name="generator"[^>]*>', "", s)
    s = html.unescape(s)
    # リンク先の日本語の符号化(%E7…)の有無は比べない(ブラウザは同じ URL として開く)
    s = re.sub(r'href="([^"]*)"', lambda m: 'href="' + unquote(m.group(1)) + '"', s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*(<[^>]+>)\s*", r"\1", s)
    return s.strip()


def split_tags(s: str) -> list[str]:
    return [t for t in re.split(r"(<[^>]+>)", s) if t]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--static", required=True)
    ap.add_argument("--show", type=int, default=5)
    a = ap.parse_args()
    site = Path(a.site).resolve()
    rid = Path(a.static) / "release-id.txt"
    if rid.exists():
        # 静的の版と同じ名前のフォルダから読む(社説・feed・sitemap の時刻は版の名前から決まる)
        stamp = rid.read_text(encoding="utf-8").strip()
        tmp = Path(tempfile.mkdtemp(prefix="paper-compare-")) / stamp
        tmp.mkdir()
        for name in ("_posts", "_editions", "_editorials", "_data", "about"):
            (tmp / name).symlink_to(site / name)
        site = tmp
    os.environ["SITE_ROOT"] = str(site)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import app as webapp
    client = webapp.app.test_client()
    static = Path(a.static)
    files = sorted(p for p in static.rglob("*") if p.suffix in (".html", ".xml") and p.is_file())
    bad, shown = [], 0
    for f in files:
        rel = f.relative_to(static).as_posix()
        url = "/" + (rel[: -len("index.html")] if rel.endswith("index.html") else rel)
        r = client.get(url)
        want = normalize(f.read_text(encoding="utf-8"))
        got = normalize(r.get_data(as_text=True)) if r.status_code == 200 else f"HTTP {r.status_code}"
        if want == got:
            continue
        bad.append(url)
        if shown < a.show:
            shown += 1
            print(f"===== {url}")
            d = difflib.unified_diff(split_tags(want), split_tags(got), "static", "app", n=2, lineterm="")
            print("\n".join(list(d)[:40]))
    print(f"比べたページ {len(files)} / 違ったページ {len(bad)}")
    by_kind: dict = {}
    for u in bad:
        by_kind[u.split("/")[1] or "/"] = by_kind.get(u.split("/")[1] or "/", 0) + 1
    if by_kind:
        print("違いの内訳:", by_kind)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
