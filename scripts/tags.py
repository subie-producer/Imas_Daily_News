#!/usr/bin/env python3
"""タグ語彙の共通ユーティリティ(PIPELINE.md §9.5)。

docs/_data/tags.yml を単一ソースとして、
  - 執筆プロンプトへ埋め込む語彙ブロックの生成(compose.py が利用)
  - 過去記事の表記ゆれ正規化(索引生成・レポート)
を提供する。過去記事の frontmatter は書き換えない(append-only)。

  python3 scripts/tags.py --report   # 正規化後の分布と未登録タグを表示
  python3 scripts/tags.py --prompt   # 執筆プロンプト用の語彙ブロックを表示
"""
import argparse
import collections
import glob
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TAGS_YML = ROOT / "docs" / "_data" / "tags.yml"
POSTS = ROOT / "docs" / "_posts"


def load() -> dict:
    doc = yaml.safe_load(TAGS_YML.read_text(encoding="utf-8")) or {}
    return {
        "drop": set(str(t) for t in (doc.get("drop") or [])),
        # YAML は "765" のようなキーを数値化し得るため str() で正規化して引く
        "alias": {str(k): str(v) for k, v in (doc.get("alias") or {}).items()},
        "vocabulary": doc.get("vocabulary") or {},
    }


def normalize(tags, spec: dict | None = None) -> list[str]:
    """表記ゆれを吸収し、drop 対象を除き、順序を保って重複を潰す。"""
    spec = spec or load()
    out = []
    for t in tags or []:
        t = str(t).strip()
        if not t:
            continue
        t = spec["alias"].get(t, t)
        if t in spec["drop"] or t in out:
            continue
        out.append(t)
    return out


def vocabulary_block(spec: dict | None = None) -> str:
    """執筆プロンプトに埋め込む語彙ブロック。カテゴリ見出し付きで列挙する。"""
    spec = spec or load()
    lines = []
    for category, terms in spec["vocabulary"].items():
        lines.append(f"  - {category}: " + " / ".join(str(t) for t in terms))
    return "\n".join(lines)


def iter_post_tags():
    for path in sorted(glob.glob(str(POSTS / "*.md"))):
        text = Path(path).read_text(encoding="utf-8")
        m = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            continue
        yield Path(path).name, (fm.get("tags") or [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="正規化後の分布と未登録タグ")
    ap.add_argument("--prompt", action="store_true", help="執筆プロンプト用の語彙ブロック")
    args = ap.parse_args()
    spec = load()

    if args.prompt:
        print(vocabulary_block(spec))
        return 0

    if args.report:
        raw = collections.Counter()
        norm = collections.Counter()
        for _, tags in iter_post_tags():
            for t in tags:
                raw[str(t)] += 1
            for t in normalize(tags, spec):
                norm[t] += 1
        known = {str(t) for terms in spec["vocabulary"].values() for t in terms}
        print(f"異なりタグ: {len(raw)} → 正規化後 {len(norm)}(延べ {sum(raw.values())} → {sum(norm.values())})")
        print(f"\n--- 正規化後 上位30 ---")
        for t, n in norm.most_common(30):
            mark = " " if t in known else "*"
            print(f"{n:3d} {mark} {t}")
        free = [t for t in norm if t not in known]
        print(f"\n* = 語彙未登録(固有名詞なら正常): {len(free)} 種")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
