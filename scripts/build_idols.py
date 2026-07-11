#!/usr/bin/env python3
"""誕生日マスタ birthdays.json から名鑑 docs/_data/idols.json を生成する。

名鑑は (1) 「きょうの誕生日」欄、(2) 24時間ランキングの名寄せ辞書、の基盤
(REQUIREMENTS.md 3.3)。原本の brandId を本紙のブランド enum に写像する。

usage: python3 scripts/build_idols.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "birthdays.json"
DST = ROOT / "docs" / "_data" / "idols.json"

# 原本の brandId → 本紙ブランド id(REQUIREMENTS.md 2.4)
# 3=DS(876プロ), 10=ヴイアラ は dsva に統合。7=.KR, 9=961プロ, 255=その他 は other。
BRAND_ID_MAP = {
    1: "765",     # 765AS
    3: "dsva",    # DS
    4: "cg",      # シンデレラ
    5: "million", # ミリオン
    6: "sidem",   # SideM
    7: "other",   # .KR
    8: "shiny",   # シャニ
    9: "other",   # 961プロ
    10: "dsva",   # ヴイアラ
    11: "gaku",   # 学園
    255: "other",
}


def main() -> int:
    raw = json.loads(SRC.read_text(encoding="utf-8"))
    idols = []
    for e in raw:
        brand = BRAND_ID_MAP.get(e["brandId"])
        if brand is None:
            print(f"ERROR: 未知の brandId {e['brandId']} ({e['name']})", file=sys.stderr)
            return 1
        month, day = (int(x) for x in e["birthday"].split("/"))
        idols.append({
            "name": e["name"],
            "brand": brand,
            "birthday": f"{month:02d}-{day:02d}",  # MM-DD(ゼロ埋め)
            "characterId": e["characterId"],
        })
    idols.sort(key=lambda x: (x["birthday"], x["characterId"]))
    DST.write_text(
        json.dumps(idols, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"{len(idols)} idols -> {DST.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
