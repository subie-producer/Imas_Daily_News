#!/usr/bin/env python3
"""source_type: 出典 URL の種別を判定表(source_types.yml)から引く。

  python3 scripts/source_type.py <url> [<url> ...]

記事を書くセッションが、sources の `type` を決めるために使う。

種別は収集役や執筆役の見立てではなく、`source_types.yml` が決める。
自己申告のままにしていたころ、攻略サイトを「公式」と申告した候補が
そのまま紙面のバッジになっていた(実測 26件・18記事)。
lint も同じ表と照合するので、ここで引いた値をそのまま書けば食い違わない。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import classify_source


def main() -> int:
    urls = sys.argv[1:]
    if not urls:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    for u in urls:
        print(f"{classify_source(u)}\t{u}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
