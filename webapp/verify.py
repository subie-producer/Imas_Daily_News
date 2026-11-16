#!/usr/bin/env python3
"""配信する前に、版のフォルダ(docs と同じ並び)を、これから配るアプリのコードで全ページ描いて確かめる。

  <アプリの python> webapp/verify.py --site <版のフォルダ>

描けないページ(200 以外・空)・必須のページの欠け・最新号がトップに出ないことを ERROR 行で出し、終了コード 1。
最後の行に {"articles": 記事数, "pages": 描いたページ数} を JSON で出す(deploy が索引の記事数と突き合わせる)。
"""
import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    a = ap.parse_args()
    os.environ["SITE_ROOT"] = str(Path(a.site).resolve())
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import app as webapp
    import paper

    errors: list[str] = []
    try:
        site = paper.load(Path(a.site))
    except Exception as e:      # noqa: BLE001 — 読めない版は配らない
        print(f"ERROR: 版を読めない: {type(e).__name__}: {e}", flush=True)
        print(json.dumps({"articles": 0, "pages": 0}))
        return 1
    if not site.posts:
        errors.append("記事が1本も無い")
    if not site.editions:
        errors.append("号が1つも無い")
    client = webapp.app.test_client()
    urls = ["/", "/archive/", "/about/", "/404.html", "/feed.xml", "/sitemap.xml", "/robots.txt"]
    urls += [d.url for d in site.editions + site.posts + site.editorials]
    for u in urls:
        r = client.get(u)
        if r.status_code != 200:
            errors.append(f"{u} が HTTP {r.status_code}")
        elif not r.data.strip():
            errors.append(f"{u} が空")
    if site.editions:
        latest = site.editions[-1]
        top = client.get("/").get_data(as_text=True)
        if f"第{latest.number}号" not in top:
            errors.append(f"トップに最新号(第{latest.number}号)が出ていない")
    for e in errors[:50]:
        print(f"ERROR: {e}", flush=True)
    if len(errors) > 50:
        print(f"ERROR: ほか {len(errors) - 50}件", flush=True)
    print(json.dumps({"articles": sum(1 for p in site.posts if p.data.get("slug")), "pages": len(urls)}))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
