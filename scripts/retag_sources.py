#!/usr/bin/env python3
"""retag_sources: 過去記事の出典種別を、URL からの判定で付け直す。

  python3 scripts/retag_sources.py            # 何が変わるかを出すだけ
  python3 scripts/retag_sources.py --apply    # 書き換える

配信中の記事で、攻略サイトやまとめサイトが「公式」バッジで出典表示されていた。
種別が収集役の自己申告で、lint の検査も「記事の src == 引用出典の最弱種別」という
ラベル同士の照合だったため、攻略サイトを「公式」と申告すれば最弱も「公式」になり、
**過大表示を1件も防げなかった**(実測 26件・18記事)。

判定の根拠は `source_types.yml` に移した(`pipelib.classify_source`)。
これはその判定を過去の記事へ反映するための、一度きりの移行スクリプトである。

書き換えるのは frontmatter の `sources[].type` と `src` だけ。本文には触らない。
`未確認` は「一次ソース未到達」という確認状態であって情報源の性格ではないので、
種別の判定で上書きしない(規程 2.5)。

第0号(試験発行)の期間は過去号を直してよい、という運用に基づく。
第1号以降は規程 2.6 のとおり訂正ボックスを立てる。
"""
import argparse
import re
import sys
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import classify_source

ROOT = Path(__file__).resolve().parent.parent
POSTS = ROOT / "docs" / "_posts"
SRC_ORDER = ["公式", "準公式", "当事者", "演者", "報道", "ファン", "二次情報", "もちより", "未確認"]
FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)


def rank(t: str) -> int:
    return SRC_ORDER.index(t) if t in SRC_ORDER else len(SRC_ORDER)


# 「一次ソースに到達できていない」と本文が断っている記事。
# 助詞が入るので連続一致では拾えない(実測: 「一次ソースには未到達のため」)。
CAVEAT_RE = re.compile(r"未到達|一次ソース.{0,8}(到達|確認)できて")


def url_alive(url: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ImasNews/1.0)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status < 400
    except Exception:
        return False


def resolve(fm: dict, body: str = "", recheck: bool = False) -> tuple[list[str], str | None]:
    """この記事の出典種別と src を、URL から決め直す。

    `未確認` は「一次ソース未到達」という確認状態なので、通常は上書きしない。

    ただし実際には、**判定表がそのドメインを知らなかっただけ**で 未確認 が
    付いたものが混ざる(livepocket.jp のチケット販売ページなど)。
    表に足しても上がらないままだと、記事のバッジが実態より弱いまま残る。

    `recheck=True` のときだけ、次を全部満たす 未確認 を判定し直す:
      1. 判定表が今はそのドメインを知っている
      2. URL に実際に到達できる(「確認」とはそういう意味である)
      3. 記事本文が「一次ソース未到達」と断っていない
         (断っている記事は、ページに到達できるかとは別の理由で未確認である)
    """
    types = []
    for s in fm.get("sources") or []:
        cur = s.get("type")
        if cur != "未確認":
            types.append(classify_source(s.get("url", "") or ""))
            continue
        want = classify_source(s.get("url", "") or "")
        if (recheck and want != "未確認" and not CAVEAT_RE.search(body)
                and url_alive(s.get("url", "") or "")):
            types.append(want)
        else:
            types.append("未確認")
    src = max(types, key=rank) if types else None
    return types, src


def rewrite(text: str, types: list[str], src: str | None) -> str:
    """frontmatter の type 行と src 行だけを差し替える。

    YAML を丸ごと書き戻すと引用符や行順が変わって差分が読めなくなるため、
    **該当行だけ**を触る。`sources:` の中の `type:` は出典の並び順に現れるので、
    n 番目の `type:` が n 番目の出典に対応する(整合は下の verify で確かめる)。
    """
    m = FM_RE.match(text)
    head, body = m.group(1).splitlines(), text[m.end():]
    out, in_sources, i = [], False, 0
    for line in head:
        if re.match(r"^[A-Za-z_]+:", line):          # 最上位キーで sources ブロックを抜ける
            in_sources = line.startswith("sources:")
        if line.startswith("src:") and src:
            out.append(f"src: {src}")
            continue
        # キーの並びは号によって違う。古い号は `  - type:` が要素の先頭に来て、
        # 新しい号は `  - label:` が先頭で `    type:` が最後に来る。どちらも拾う
        mt = in_sources and re.match(r"^(\s*-\s+|\s+)type:\s*\S", line)
        if mt:
            out.append(f"{mt.group(1)}type: {types[i]}")
            i += 1
            continue
        out.append(line)
    if i != len(types):
        raise SystemExit(f"type 行の数({i})が出典の数({len(types)})と合わない")
    return "---\n" + "\n".join(out) + "\n---\n" + body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="実際に書き換える")
    ap.add_argument("--recheck-unverified", action="store_true",
                    help="未確認の出典を、到達確認のうえ判定し直す")
    args = ap.parse_args()

    n_src, n_type, n_files = 0, 0, 0
    for path in sorted(POSTS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        m = FM_RE.match(text)
        if not m:
            continue
        fm = yaml.safe_load(m.group(1))
        types, src = resolve(fm, text[m.end():], args.recheck_unverified)
        olds = [s.get("type") for s in fm.get("sources") or []]
        d_type = sum(1 for a, b in zip(olds, types) if a != b)
        d_src = 1 if src and fm.get("src") != src else 0
        if not (d_type or d_src):
            continue
        n_files += 1
        n_type += d_type
        n_src += d_src
        for a, b, s in zip(olds, types, fm.get("sources") or []):
            if a != b:
                d = "過大→適正" if rank(a) < rank(b) else "過小→適正"
                print(f"  {path.name}\n    {a} → {b}  [{d}]  {s.get('url','')}")
        if d_src:
            print(f"  {path.name}\n    src: {fm.get('src')} → {src}")
        if args.apply:
            new = rewrite(text, types, src)
            # 書き戻した YAML を読み直し、狙いどおりになっているかを確かめる。
            # 行単位で触っているので、ここで整合を取らないと静かにずれる
            got = yaml.safe_load(FM_RE.match(new).group(1))
            assert [s.get("type") for s in got.get("sources") or []] == types, path
            assert got.get("src") == src, path
            path.write_text(new, encoding="utf-8")

    verb = "書き換えた" if args.apply else "書き換わる"
    print(f"\n記事 {n_files}本 / 出典 {n_type}件 / src {n_src}本 が{verb}")
    if not args.apply:
        print("(--apply で実行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
