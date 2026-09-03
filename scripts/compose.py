#!/usr/bin/env python3
"""compose: 当日号の紙面を生成する。(PIPELINE §6。毎朝 04:00 起動)

  python3 scripts/compose.py [--plan] [--max-rounds 2]

役割分担: 記事執筆=Codex(gpt-5.6-luna)/ 社説執筆=Codex(gpt-5.6-terra)/
          記事計画・組版=Claude / 機械算出=derive.py / 校閲=Claude(haiku)。
紙面に載る文章はすべて Codex が書き、Claude が校閲する。執筆と校閲が別ベンダー
(OpenAI/Anthropic)になる(要件4.5)。社説だけ Claude で書くと自己校閲になるため
社説も Codex 側に置いてある。
二段構成(執筆時コンタミの構造的排除):
1. edition ブランチ同期 → 続報キュー(本日トリガー)と素材を整理
2. 選定: claude が候補全体から記事計画(slug→candidate_ids 対応表)を JSON で出力
   → compose が機械検証(候補の実在・verify・blocklist・lead 一意)
3. 個別執筆: 記事ごとに、計画で選ばれた候補 JSON だけを機械的に切り出して渡し、
   独立した codex セッションが1本書く(他候補の情報がコンテキストに存在しない)
4. 社説: 専任のコラムニストセッションが当日の記事群を読んで1本書く(人格・文体規程あり)
5. 組版: 別セッションが号スナップショット(digest)・stories/scheduled 更新
6. derive.py --write → lint(ゲート) → claude 校閲往復 → commit & push(発行は 06:00 の release)
"""
import argparse
import collections
import datetime
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tags as tags_lib
from pipelib import (ENV, ROOT, CLAUDE_MODEL, CODEX_WRITE_MODEL, COMPOSE_WAVE, EDITORIAL_MODEL,
                     COMPOSE_ARTICLE_MAX_BUDGET_USD,
                     COMPOSE_WHOLE_MAX_BUDGET_USD, REVIEW_MODEL, append_metric,
                     checkout_edition_branch, classify_source, commit_and_push,
                     edition_date, extract_json_array, git, notify, notify_crash, now_jst)

PAPER_STAGE = None  # main() で .env から


def load_scheduled(date: str) -> list[dict]:
    """発行日の続報予約(素材スナップショット同梱)。過去の candidates を遡らないための日付別ストア。"""
    p = ROOT / "stock" / "scheduled" / f"{date}.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def next_number() -> int:
    from pipelib import load_env
    if load_env().get("PAPER_STAGE", "test") != "live":
        return 0
    nums = []
    for e in (ROOT / "docs" / "_editions").glob("*.md"):
        import re
        m = re.search(r"^number:\s*(\d+)", e.read_text(encoding="utf-8"), re.MULTILINE)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) + 1 if nums else 1


# --- 時間予算 -----------------------------------------------------------
# compose は 04:00 に起動し、release は 06:00 に発行する。systemd は 110分で
# SIGTERM を送る。つまり**使える時間は決まっている**のに、校閲の往復は
# 「approve になるまで」「落とせるものが無くなるまで」で回っていた。
# 校閲1巡は実測20分前後なので、往復が3回増えれば予算を超える。
# 実測 2026-09-03: 選定12 + 執筆11 + 社説と組版23 + 校閲21 + 取り直し43 = 110分。
# 余白ゼロで、追加の1巡が入った時点ではみ出す設計だった。
#
# そこで**時計を見て決める**。次の1巡を始める前に、それを終えて紙面を確定する
# だけの時間が残っているかを確かめ、無ければそこで打ち切って、いま green な
# 紙面で発行する。「発行を止めるくらいなら薄い紙面を出す」に従う。
COMPOSE_DEADLINE_MIN = int(ENV.get("COMPOSE_DEADLINE_MIN", "60"))
# 打ち切ったあと**必ず踏む**後始末(社説と組版のやり直し=並列+derive+lint+コミット)の見込み。
#
# **ここは削らない。**記事を直したあとに社説・digest・台帳を作り直さないと、
# 直る前の事実を引用した社説と、古い台帳のまま approve が出てしまう(監査指摘)。
# 打ち切るのは「もう1巡の校閲」であって、整合を取る工程ではない
COMPOSE_RESERVE_MIN = int(ENV.get("COMPOSE_RESERVE_MIN", "16"))
# 各段の実測(分)。走りながら上書きし、次の判断に使う
STAGE_MIN: dict[str, float] = {}


def stage_cost(name: str, default: float) -> float:
    """その段にかかる見込み(分)。実測があればそれを使う。"""
    return STAGE_MIN.get(name, default)


def time_left(t0: float) -> float:
    return COMPOSE_DEADLINE_MIN - (time.time() - t0) / 60


def afford(t0: float, name: str, default: float, what: str, extra: float = 0) -> bool:
    """`name` の段をもう1回やる時間があるか。後始末の分は必ず残す。

    `extra` はその段に付随して必ず走るもの(社説の書き直し・組版のやり直し)の分。
    """
    need = stage_cost(name, default) + extra + COMPOSE_RESERVE_MIN
    left = time_left(t0)
    if left >= need:
        return True
    print(f"時間切れのため{what}を打ち切る(残り{left:.0f}分 / 必要{need:.0f}分)", flush=True)
    return False


BRANDS = {"general", "765", "cg", "million", "shiny", "sidem", "gaku", "dsva", "joint", "other"}
RANKS = {"lead", "large", "medium", "small", "roundup", "culture"}
# roundup(編集規程13の例外: ブランド別の定常運営まとめ)を作る最小件数。
# これ未満なら束ねずに通常記事にする(2件を「まとめ」と称すると単なる手抜きになる)
ROUNDUP_MIN_ITEMS = 3
# 書き上がった本文の長さ → 枠。長い順に判定する(規程9)
RANK_BY_LENGTH = (("lead", 1000), ("large", 700), ("medium", 450), ("small", 0))
# 素材件数に対して最低限ほしい rank(規程9)。束ねること自体は正しいが、
# 束ねたまま rank が小さいと上限に収まらず中身が落ちる
RANK_FLOOR_BY_MATERIALS = ((10, ("large", "lead")), (5, ("medium", "large", "lead")))
# 出典バッジの信頼順(強い順)。記事 src は引用出典のうち最も「弱い」種別
# (=全出典がその種別以上であることの保証。ファン報告を含む記事が「公式」を
# 名乗る過大表示を構造的に防ぐ。REQUIREMENTS 2.5「他はどれほど信頼できても
# 公式を名乗らない」の記事単位への適用)
SRC_ORDER = ["公式", "準公式", "当事者", "演者", "報道", "ファン", "二次情報", "もちより", "未確認"]


def weakest_src(types) -> str:
    return max(types, key=lambda s: SRC_ORDER.index(s) if s in SRC_ORDER else len(SRC_ORDER))


def load_window_candidates(date: str) -> dict:
    """当該号の素材索引(id→素材)。号ファイル candidates/<date>.json(収集は号キーで蓄積)+
    当日の続報予約(stock/scheduled)のみ。過去の candidates は読まない。"""
    items = {}
    p = ROOT / "candidates" / f"{date}.json"
    if p.exists():
        for c in json.loads(p.read_text(encoding="utf-8")):
            if c.get("id"):
                items[c["id"]] = c
    for s in load_scheduled(date):
        items[s["id"]] = s
    return items


def load_blocklist() -> dict:
    p = ROOT / "stock" / "blocklist.yml"
    if not p.exists():
        return {}
    return {e["dedup_key"]: e.get("reason", "") for e in yaml.safe_load(p.read_text(encoding="utf-8")) or []}


# 選定へ渡す1主題あたりの facts。面別選定になり1セッションが見るのは十数件なので、
# 号全体を1セッションで裁いていた頃の切り詰め(3件×90字=最大270字)は不要になった。
# 118〜163字しか渡していないと、記事化の価値も rank の見当も付けられない
PLAN_FACTS_PER_SUBJECT = int(ENV.get("PLAN_FACTS_PER_SUBJECT", "8"))
PLAN_FACT_CHARS = int(ENV.get("PLAN_FACT_CHARS", "220"))


def write_plan_index(date: str, cands: dict, blocklist: dict) -> tuple[Path, int]:
    """選定用の主題インデックスを機械生成する。

    候補ファイルをそのまま読ませると、収集の増分マージで facts がほぼ同文のまま
    積み上がるため巨大になる(2026-08-25号は 281KB・5577行)。選定に必要なのは
    「どの主題があり、どの id を指せばよいか」であって facts の全文ではない
    (facts の全文は執筆セッションが素材として別途受け取る)。
    dedup_key で束ねて主題単位にし、facts は先頭数件・各短縮で渡す。
    """
    subjects: dict[str, dict] = {}
    for c in cands.values():
        dk = c.get("dedup_key")
        if not dk or c.get("verify") == "failed" or dk in blocklist:
            continue
        s = subjects.setdefault(dk, {"dedup_key": dk, "brand": c.get("brand"),
                                     "title": c.get("title"), "ids": [], "source_types": [],
                                     "urls": [], "facts": [], "verify": "unconfirmed"})
        s["ids"].append(c.get("id"))
        for k in ("published_date", "event_date"):
            if c.get(k) and not s.get(k):
                s[k] = c[k]
        if c.get("source_type") and c["source_type"] not in s["source_types"]:
            s["source_types"].append(c["source_type"])
        if c.get("url") and c["url"] not in s["urls"]:
            s["urls"].append(c["url"])
        if c.get("verify") == "confirmed":
            s["verify"] = "confirmed"
        for f in c.get("facts", []):
            f = f[:PLAN_FACT_CHARS]
            if f not in s["facts"]:
                s["facts"].append(f)
    for s in subjects.values():
        s["facts"] = s["facts"][:PLAN_FACTS_PER_SUBJECT]
    # 1主題1行で書く(整形すると数千行になり、Read の既定上限 2000行で頭から切れて
    # 後半の主題が編集長の視野に入らなくなる。行数=主題数なら一度で読み切れる)
    def dump(rows: list[dict], path: Path) -> None:
        body = ",\n".join(json.dumps(s, ensure_ascii=False, separators=(",", ":")) for s in rows)
        path.write_text("[\n" + body + "\n]\n", encoding="utf-8")

    dump(list(subjects.values()), ROOT / "metrics" / f"plan-index-{date}.json")
    by_brand: dict[str, list[dict]] = {}
    for s in subjects.values():
        by_brand.setdefault(s.get("brand") or "other", []).append(s)
    for b, rows in by_brand.items():
        dump(rows, ROOT / "metrics" / f"plan-index-{date}-{b}.json")
    return by_brand, len(subjects)


def brand_plan_prompt(date: str, brand: str, n_subjects: int, triggers: list[dict],
                      feedback: str = "", claimed: list[dict] | None = None) -> str:
    """面(ブランド)ごとの選定プロンプト。

    号全体を1セッションに裁かせると、161主題で 1800秒のタイムアウトに達して
    計画が1本も出ない(2026-08-26の再発行で実測)。面ごとに割ると1セッションが
    見る主題は十数件になり、全主題に判断を下しても時間内に収まる。
    lead はここでは付けない(面をまたぐ比較が要るため後段で決める)。
    """
    weekday = "月火水木金土日"[datetime.date.fromisoformat(date).weekday()]
    fb = f"\n## 前回の機械検証エラー(必ず解消すること)\n{feedback}\n" if feedback else ""
    trig = json.dumps([{k: t.get(k) for k in ("id", "dedup_key", "brand", "subject", "kind", "note")}
                       for t in triggers], ensure_ascii=False, indent=1)
    cul_rank = "|culture" if brand == "general" else ""
    cul_rule = ("""
- **ファン面(規程4の例外・rank: culture)**: ファン創作・コスプレ・聖地巡礼・記念日の盛り上がり・
  界隈の現象は `rank: culture` で記事にする。**細切れの話題はまとめ記事にする**
  - 1件ずつが短いものを個別記事に散らさない。**まとめて1本にする**のがこの面の使い方
  - まとまった量がある系統(特定の作品の記念日など)が別にあるなら、そちらは別の1本にしてよい。
    号内の本数に制限は設けない(1本でも複数でもよい)。**落とすことだけが禁止**
  - **誰か1人を主役にしない**(傾向として書く面であり、個人の紹介ではない)
  - 声優個人のアイマス外活動はこの例外の対象外。批判・嘲笑・炎上も対象外(規程5)""" if brand == "general" else "")
    done = ""
    if claimed:
        done = ("\n## すでに他の面が記事にした話題(この号に載ることが確定しています)\n"
                + json.dumps(claimed, ensure_ascii=False, indent=1)
                + "\n**同じ話題をこの面でも記事にしないでください。**dedup_key が違っても、"
                  "同じ公演・同じ商品・同じ施策を指しているなら同じ話題です"
                  "(切り口 angle を読んで判断すること)。該当する主題は dropped に "
                  'reason="重複" で記録します。\n'
                  "ただし**明らかに別の施策**(同じ公演でも「チケット」と「グッズ受注」は別)は、"
                  "この面で記事にして構いません。\n\n"
                  "**この面にも同じくらい属する話題だった場合**(例: デレとミリの合同告知を "
                  "cg 面が先に取っていた)は、dropped ではなく `cross_brand` に記録してください。"
                  "その記事は合同(joint)面へ移し、この面の素材も統合します。"
                  "「先に取った面の話題」として片方に寄せてしまうと、"
                  "もう一方のファンにとって紙面から消えたのと同じになります。\n")
    return f"""あなたは日刊AI新聞「アイマスNEWS(α)」の編集者です。{date}({weekday}曜)号の
**「{brand}」面だけ**の記事計画を作ってください。記事本文はまだ書きません。他の面は別の担当が見ます。

## 素材(読むもの)
- metrics/plan-index-{date}-{brand}.json … **この面の全主題({n_subjects}件)**。
  verify=failed と blocklist は除外済み、同一話題は dedup_key で束ねてある。
  `ids` がその主題の候補IDで、計画の candidate_ids にはこれをそのまま使う。
  **{n_subjects}主題すべてに判断を下すこと**(途中で切り上げない)。
  facts は先頭{PLAN_FACTS_PER_SUBJECT}件の要約のみ。全文が要るときだけ candidates/{date}.json を引く(通常は不要)
- stock/stories.yml … 既報台帳(published_facts と同内容=新事実なしの話題は記事化しない。編集規程8)
- REQUIREMENTS.md 5章(編集規程)
- この面の続報予約(原則**記事化する**。id は素材スナップショットとして candidate_ids に使える)。
  ただし**予約そのものが誤っていることがある**。次に当たるものは記事化せず、`dropped` に理由を書く:
  - **その日に何も起きない**予約(担当者の在籍最終日、契約上の区切りなど。読者が見に行ける催しも、
    できる手続きも無い)
  - 催しがもう終わっている話題に、後日の日付で作られた予約
  - 素材の facts に、その日に起きることが1つも書かれていない予約
  (実測: 最終配信が8月26日に済んだ話題を8月31日の`終了`として予約していたため、
  31日に何も起きていないのに記事が1本生まれた)。
  **「予約があるから書く」ではなく、「その日、読者は何を見に行けるか・何ができるか」で決める**:
{trig}
{done}
## 選定規則
- **{n_subjects}主題すべてを「記事化」「roundup」「不採用」のいずれかに割り当てる。黙って無視してよい主題は1つもない**(不採用は dropped に理由付きで列挙)
- **本数の目標値は無い。**記事化基準を満たす話題は全部記事にする(「多いから落とす」は禁止。紙面は無制限。編集規程11)。あふれたら rank を small へ寄せる
- **「書ける量が少ない」を不採用や統合の理由にしない。**本文に下限は無く、事実が2〜3文しかない話題はそのまま短い記事にする(規程9)。短い記事が並ぶことより、話題が消えることのほうが読者にとって損失である
- rank は large|medium|small|roundup{cul_rank} から選ぶ。**lead は付けない**(号全体の一面は後段で決める){cul_rule}
- **同じ系統の話は1記事にまとめる。**同じ公演・同じ施策をめぐる「開幕」「冒頭無料配信の決定」「会場限定CDの販売」は、読者にとって1つの出来事であり、分けると3本とも薄くなる。candidate_ids に素材を全部載せる
  - 分けるのは**読者が取る行動が別**のとき(例: 「チケットの申込締切」と「グッズの受注締切」は別の締切なので別記事)
- **束ねたぶんは rank を上げる。**素材が多い記事を small のままにすると、書ける上限に収まらず中身が落ちる。目安として素材5件以上なら medium 以上、10件以上なら large 以上を割り当てる(規程9の上限は rank で決まる)
- 個人への攻撃・プライバシー侵害になり得る話題、読んだ人が嫌な気分になる炎上・係争は入れない。個人の SNS 投稿は単体で記事化しない(規程4・5。規程11より優先)
- 声優個人のアイマス外活動・関係者の動向は対象外(規程4)
- **同人イベント・ファン主催企画(オンリーイベント・即売会・非公式コラボ)は記事化しない**(規程4)
- **ニュース性(規程12)**: 記事にできるのは発行日時点で「新しく発表された・起きる・起きた」ことだけ。終了済みイベントの紹介・過年度の話題は記事化しない(結果・千秋楽など当日トリガーの続報は可)。published_date・event_date・dedup_key・URL の**年**を確認し、発行年より前の年しか出てこない候補(例: dedup_key 末尾が -2025)は除外する
- **1記事1主題(規程13)**: 複数の小ネタを「まとめ」「続々判明」として1本に束ねない
- **定常運営まとめ(規程13の例外・rank: roundup)**: 単体では記事にならない**進行中の運営情報**(開催中のガシャ・ログインボーナス・楽曲追加・月例イベント・配信出演など)は、**この面で1本だけ** `rank: roundup` にまとめる。{ROUNDUP_MIN_ITEMS}件以上まとまるときだけ作り、{ROUNDUP_MIN_ITEMS}件未満なら small の通常記事にする
  - **roundup に入れてはいけないもの**: 発表・開催決定・販売開始日・受注/申込の開始と締切・中止延期。これらはニュースなので**単独記事**にする。判断に迷ったら単独記事にする
  - roundup は記事本数の下限に算入しない。「roundup があるから記事は少なくてよい」は誤り
{fb}
## 出力
`metrics/plan-{date}-{brand}.json` に次の JSON を書く(Write ツール使用。これ以外のファイルは作らない):
{{
  "articles": [
    {{"slug": "英小文字ハイフンの記事ID(号内で一意になるよう面名や主題を含める)",
      "brand": "{brand}",
      "rank": "large|medium|small|roundup",
      "angle": "記事の切り口・見出しの方向性(1文。roundup なら束ねる観点)",
      "lead_score": 0,
      "dedup_key": "主話題の dedup_key",
      "candidate_ids": ["素材にする候補の id(統合分は全部。roundup は束ねる全件)"]}}
  ],
  "dropped": [
    {{"dedup_key": "記事にも roundup にもしなかった主題の dedup_key",
      "reason": "既報|過年度|同人・ファン主催|個人の話題|重複|出典不足|その他",
      "note": "reason だけで説明がつかない場合の一言(任意)"}}
  ],
  "cross_brand": [
    {{"slug": "他の面が既に立てた記事の slug",
      "dedup_key": "この面の主題(その記事へ統合する)",
      "note": "なぜ両方の面にまたがるのか(1文)"}}
  ]
}}

`cross_brand` は、他の面が取った話題が**この面にも同じくらい属する**ときだけ使います
(該当が無ければ空配列)。記事は合同(joint)面へ移り、両面の素材が統合されます。

`lead_score` は「この記事が号の一面に値する度合い」を 0〜100 で自己申告する値です
(面内で最も大きなニュース1本にだけ高い値を付け、残りは 0〜30 程度)。

**dropped は必須です。**この面の {n_subjects} 主題は、articles か dropped の
どちらかに必ず1回現れなければなりません。書ききれないから省く、は不可です
(不採用そのものは正当な判断です。理由を残さないことだけが問題です)。

最後に「{brand}: 記事N本 / roundupN本 / 不採用N件」の1行で報告してください。
"""


def lead_prompt(date: str, arts: list[dict]) -> str:
    """面別計画を束ねたあと、号の一面と社説主題だけを決める短いセッション。"""
    weekday = "月火水木金土日"[datetime.date.fromisoformat(date).weekday()]
    rows = json.dumps([{k: a.get(k) for k in ("slug", "brand", "rank", "angle", "lead_score")}
                       for a in arts if a.get("rank") != "roundup"], ensure_ascii=False, indent=1)
    return f"""あなたは日刊AI新聞「アイマスNEWS(α)」の編集長です。{date}({weekday}曜)号の
記事計画は各面の担当が作り終えています。あなたの仕事は**一面(lead)を1本選ぶこと**と
**社説の主題を決めること**の2つだけです。記事本文も他の記事の rank も触りません。

## 本日の記事候補(面別担当の申告。lead_score はその面での自己申告)
{rows}

## 選ぶ基準
- その日いちばん「アイマス全体にとって大きい」話題を1本。面の大小や lead_score の高さだけで決めない
- 新規発表・大型施策・シリーズ横断の動きは強い。定常運営の更新は弱い
- 社説主題は本日の紙面から1題。一面と同じでなくてよい

## 社説の起点は「記事1本」だけを選ぶ(主題を言葉にしない)
**何を論じるべきかは書かないこと。**
「〜の意味を問う」「〜が示すもの」のような1文にまとめると、社説がその抽象語をなぞって、
どのブランドにも貼れる一般論になる(実測: 主題を「シリーズ長寿化と運営体制刷新の意味を問う」と
渡した日の社説は、作品名を入れ替えても成立する文章になった)。

またこの時点では**記事本文はまだ書かれていない**。あなたが見ているのは計画だけなので、
記事にある具体の事実を選ぶこともできない。それは社説の書き手が、書き上がった記事を読んで選ぶ。

あなたの仕事は「この記事から始めるとよい」と1本指すことだけである。

## 出力
`metrics/plan-lead-{date}.json` に次の JSON を書く(Write ツール使用。これ以外のファイルは作らない):
{{"lead_slug": "一面にする記事の slug(上のリストから1つ)",
  "editorial_slug": "社説が起点にする記事の slug(上のリストから1つ。一面と同じでもよい)"}}
"""


def article_prompt(date: str, art: dict, materials: list[dict], story_facts: list[str],
                   trigger: dict | None, src: str) -> str:
    weekday = "月火水木金土日"[datetime.date.fromisoformat(date).weekday()]

    trig = (f"\n- この記事は続報トリガー({trigger['kind']}: {trigger.get('note') or trigger['subject']})の消化です。"
            "トリガーの当日性(締切・開幕等)を記事の軸にすること" if trigger else "")
    prev = ("\n## 既報(この話題で報道済みの事実。同じ事実の繰り返しを記事の軸にしない)\n"
            + "\n".join(f"- {f}" for f in story_facts)) if story_facts else ""
    vocab = tags_lib.vocabulary_block()
    culture = ("""

## この記事は「ファン面」です(rank: culture・編集規程4の例外)
ファンの営みを、個人の紹介ではなく**その日の傾向**として1本にまとめた記事です。
素材は細切れです。**箇条や短い段落で並べてよい**ので、1件ずつを無理に膨らませないこと。
- **誰か1人を主役にしない。**「こういう動きが目立った」という書き方にする
- **個人アカウント名・ハンドルを本文に書かない。**当人が望まない露出を作らないため
  (sources の url は残してよい。label も投稿者名ではなく内容が分かる語にする)
- **批判・嘲笑・優劣の比較を書かない**(規程5はこの面にも適用される)
- 素材が2件以下しか残らなければ、ファイルを作らず「ABORT: 残件不足」とだけ出力して終了する
- 見出しは煽らない。数字や断定で盛らない"""
               if art["rank"] == "culture" else "")
    roundup = ("""

## この記事は「定常運営まとめ」です(rank: roundup・編集規程13の例外)
単独では記事にならない進行中の運営情報を、この面ぶんだけ束ねた記事です。
- **各素材を1項目として箇条書きにする**。項目は「何が・いつまで(いつから)」が1行で分かる形にする
- 素材どうしを地の文でつなげて1つの話に仕立てない。**束ねただけであることを隠さない**
- 冒頭に1〜2文の導入(この面で今動いているものの総括)を置き、そのあとに箇条を並べる
- 事実確認で落ちた素材はその項目ごと落とす。**残りが2項目以下になったら、ファイルを作らず「ABORT: 残件不足」とだけ出力**して終了する(まとめとして成立しないため)
- 見出しはこの面の運営情報のまとめだと分かるようにする(煽らない。釣らない)
- sources は残した項目ぶんを全部載せる"""
               if art["rank"] == "roundup" else "")
    return f"""あなたは日刊AI新聞「アイマスNEWS(α)」の記者です。{date}({weekday}曜)号の記事を**1本だけ**書いてください。

## 素材(この JSON がこの記事に使ってよい情報の全てです)
{json.dumps(materials, ensure_ascii=False, indent=2)}
{prev}

## 執筆前の出典照合(必須)
**素材の各 url を、次のコマンドで実際に読み直してから書きます。**

    python3 scripts/fetch_page.py <url>

出典ページの本文が**要約なしの原文のまま**出ます。先頭に、機械抽出したラベル付き期間が付きます。
x.com の url は取得できないので実行不要です(Grok 観測を信頼する既定どおり)。
ただし x.com だけを根拠に日付・期間・価格を断定しないこと。

- `facts` の各項目が、取得した本文で確認できるか照合する。**確認できない fact は使わない**
  (素材の facts は収集段階の誤りを含み得る。実際に誤りが出ている)
- 素材に `unbacked_facts` があれば、そこに挙がった日付・金額は**収集時点で出典本文から
  見つけられなかった値**である。取得した本文で自分の目で確かめ、見つからなければ書かない。
  (画像の中や別ページにあるだけのこともあるので、見つかれば使ってよい)

## 公式の告知があるはずの話題は、公式を読んでから書く

ゲーム内の施策(ガシャ・イベント・ミッション・報酬・不具合)、CD や映像の発売、
ライブ・配信の実施——これらは**必ずブランド公式の告知がある**。
攻略Wiki・まとめ・個人ブログしか素材が無いのは、公式に到達していないだけである。

**素材に公式・準公式の出典が1つも無いときは、公式の告知を探しに行く。**

- 二次情報の素材は、たいてい元の公式告知を引用・リンクしている。
  `python3 scripts/fetch_page.py <二次情報のurl>` で本文を読み、そこに出てくる
  ブランド公式サイト・公式ストアの URL を**開いて確かめる**
- 到達できたら、**そちらを出典にして事実を取り直す**。日付・価格・仕様が
  二次情報と食い違うことがあり、そのときは公式が正しい
- **どうしても公式に届かないなら、その記事は書かない。**
  `ABORT: 公式の告知に到達できず` とだけ出力して終わる。
  攻略Wikiの記載を丸写しした記事は、読者にとって価値が無く、誤りの出所にもなる
  (実測: ガシャの排出率と報酬を攻略Wikiだけを見て書いた記事が出た)

**リンク集・RSSミラーは出典にできない。**他所の記事の見出しとリンクを並べているだけで、
開いても事実が何も確認できないサイトがある(`source_types.yml` の `discovery_only_domains`)。
素材にそれが入っていたら、**リンク先を開いて、そちらを出典にする**。
ネタを見つけるのには使えるが、出典欄に置くと「確認できない場所」を根拠として示すことになる。
lint がエラーにする。

**公式が出して然るべきものは、公式が無ければ駄目である。**分量や締切を理由に緩めない。

二次情報やファンの発信だけで成立するのは、**そもそも公式が出すものではない話題**——
プロデューサーの間で広がったミームのような、ファン発の現象だけ。
それは `rank: culture` のファン面で扱うものであって、通常の記事では書かない。

## 一文ごとに「読者にとって要るか」を確かめる

書いてよいのは、次のどちらかに当たる事実だけである。

1. **読者が今日できることを増やす**(申し込める、買える、見に行ける、視聴できる)
2. **今日の出来事を理解するために要る**

**どちらにも当たらない事実は、出典に書いてあっても書かない。**
とくに次は、そのまま丸ごと落とす。

- **もう終わった受付・販売・抽選の日程。**申し込めない期間をいくら並べても、
  読者にできることは何も増えない。「現在は受付終了」と書くために、
  その受付の開始日・締切・当落発表日・支払方法まで並べる必要はない
  (実測: 8月30日のリリースイベントの記事に、6月24日から7月26日までの受付期間、
  7月29日の当落発表、8月27日のダウンロード開始日を並べていた。全部終わっている)
- **済んだ不具合・お詫び・トラブル・炎上。**今日の話題がその不具合そのものでない限り、
  持ち出す理由が無い。読者には蒸し返しでしかない(編集規程5)
  (実測: 同じ記事に、7月1日付の応募券の印刷不備に関するお詫びを1段落ぶん書いていた)
- 商品の品番、過去の発売日、変更前の仕様など、今日の話に効かない台帳的な事実

**まだ有効な期間は書いてよい。**今日申し込めるもの、今日から買えるもの、
今日の23時59分に締め切られるものは、日付を書くことで読者の行動が変わる。
落とすのは「終わっているもの」であって「過去の日付」ではない。

## 「無いこと」を報告しない

**出典に何が書かれていなかったかを、紙面に書かない。**
読者は出典を読んでいない。何が無かったかを知らされても、得るものが一つも無い。

書かない例:
- 「投稿本文に会場名、開場・開演時刻、出演者全員の一覧はない」
- 「投稿本文に開始日・終了日の記載はない」
- 「投稿文では内容の詳細や別ページURLは案内されていない」

素材が薄いなら、**そのまま短く書く**(規程9)。無いことを並べて長さを作らない。

例外は、**未発表であること自体が今日の話題の核心**である場合(予告だけが出た、
続報待ちであることが発表された等)。そのときも欠けている項目を列挙せず、
「詳細は未発表」と一言で書く。

なお「公式Xの投稿では〜と告知している」のように**出所を示す書き方は問題ない**。
落とすのは、出典の中身の欠落についての報告である。

**分量が足りないことは、要らない事実を入れる理由にならない。**
事実が2〜3文しかない話題は、そのまま短い記事にする(規程9)。水増しより短いほうがよい。

## 出典が二次情報しか無いときは、一次情報を取りに行く

記事のバッジは**引用した出典のうち最も弱い種別**になる。
まとめサイトや攻略サイト(`type: 二次情報`)しか出典が無い記事は「二次情報」表示になる。

このとき**出典から二次情報を消して強い出典だけ残してはいけない。**
事実の出所は二次情報のままで、表示だけが良くなる。それは読者を欺くことになる。

正しい手当ては**一次情報を探しに行くこと**である。

- 二次情報の素材は、たいてい元の発表ページを引用・リンクしている。
  `python3 scripts/fetch_page.py <二次情報のurl>` で本文を読み、
  そこに出てくる公式サイト・レーベル・ストアの URL を**開いて確かめる**
- 一次情報に到達できたら、**そちらを出典にして事実を取り直す**。
  日付や商品名が二次情報と食い違うことがあり、そのときは一次情報が正しい
  (実測: 公式Xの投稿はリンクの列で、詳細はすべてまとめサイト由来だった記事がある)
- どうしても一次情報に届かないときは、二次情報のまま書いてよい。
  バッジが「二次情報」になるのは正しい状態であり、それを隠さない
- その出典にしか無い事実を書いたなら必ず載せる。チケットの価格や受付時間が
  販売ページにしか無いなら、それは当事者の出典である。**バッジが下がるのが
  正しい場合がある**ので、下げないために出典を省くことはしない
- **先頭の「期間」ブロックは原文のラベルそのままなので、`facts` と食い違ったらそちらが正**。
  「入金期間」を「販売期間」と書き換えるような取り違えは、ここで必ず正す(規程15)
- **掲載日(公開日)と話題の年を本文で確認する**。掲載年がイベント年・発行年と食い違う、
  または話題自体がすでに終了した過去のもの(当日トリガーの続報を除く)なら、その素材は使わない
- `FETCH_FAILED` が返った url は照合不能として扱う(その url だけを根拠に断定しない)
**記事の存在理由になる中核の事実が確認できない場合、または話題が過年度・終了済みと判明した場合は、
ファイルを作らず「ABORT: 理由」とだけ出力して終了すること。**

## 書けないと分かったら、書かずに落としてよい

計画に載っているからといって、無理に記事の形にしない。
**素材を読んだ結果「これはニュースになっていない」と分かったら、あなたが落とす。**
選定は素材の要約しか見ておらず、あなたは実物を読んでいる。判断できるのはあなただけである。

次に当たると分かったら、ファイルを作らず `ABORT: 理由` とだけ出力して終わる。

- **今日新しく起きたことも、これから起きることも無い。**
  終わったイベントの物販ページが「掲載されている」だけ、既報と同じ内容が別の場所にもある、
  といった話題は記事にならない
  (実測: 8月13〜16日に終わったイベントのグッズ販売ページが「掲載されている」とだけ書いた
  記事が出て、校閲に5往復ブロックされ、その日の発行が止まった)
- **書ける中身が「ページが存在する」だけ。**商品名も価格も期限も無いなら、
  読者は何も受け取らない
- 素材が公式の告知に到達しておらず、探しても届かない(前述)

**落とすことは失敗ではない。**紙面の本数は目標ではない(規程11は「基準を満たす話題を
落とすな」であって「基準を満たさない話題を書け」ではない)。
中身の無い記事を1本増やすより、落としたほうが紙面はよくなる。

## 出力
`docs/_posts/{date}-{art['slug']}.md` を Write ツールで作成(これ以外のファイルは作らない・読む必要もない):
- frontmatter は次の値を**そのまま**使う: slug: {art['slug']} / edition: {date} / brand: {art['brand']} / src: {src} / rank: {art['rank']}(**仮の値**。発行前に機械が付け直します) / corrected: false / corrections: [] / candidate_ids: {json.dumps(art['candidate_ids'])}
- title(全角換算〜28字)・lede(1文。字数指定なし。記事の中身を1文で言い切る)・tags(2〜4個。下記「タグ語彙」に従う)・sources・event_date(素材にあれば)は自分で書く
- **sources の `type` は自分で判断しない。**次のコマンドで引いた値をそのまま書く:

      python3 scripts/source_type.py <url> [<url> ...]

  種別は `source_types.yml` が決める。素材の `source_type` は収集時点の申告で、
  古い値が残っていることがある(実測: アソビストアの受注ページを「公式」と
  申告した候補があり、毎回 lint が赤くなっていた)。
  **自分で見つけて追加した出典も、必ずこのコマンドで引く。**
  lint は同じ表と照合するので、ここで引いた値なら食い違わない
- 本文の**字数指定はありません。**確認できた事実を、水増しせずに書けるだけ書いてください。紙面のどの枠に入れるかは、書き上がった長さと話題の大きさから機械が決めます。切り口: {art['angle']}{trig}{roundup}{culture}

## タグ語彙(タグは索引・検索に使われる。表記ゆれは索引を壊すため厳守)
シリーズ名・施策カテゴリは**必ず次の語彙から選ぶ**(同義の別表記を作らない):
{vocab}
- アイドル名・会場名・作品固有名など固有名詞は語彙外でよい(正式名称で書く)
- frontmatter の brand と同じ意味のタグを brand の id(shiny/million/gaku 等)で書かない。上の正式名を使う
- src の値(公式・報道・ファン・未確認)をタグにしない
- 毎年ある定例企画は年を含める(例: IWSF2026・総選挙2026・アニサマ2026)

## 分量の作り方(**水増しではなく、具体を書く**)
短くなるのはたいてい素材不足ではなく、**出典に書いてあることを書いていない**からです。
`scripts/fetch_page.py` で読んだ本文には、たいてい次が載っています。拾って書いてください。

- 会場名・所在地・開場/開演時刻・座席や配信の別
- 価格(税込/税抜)・セット内容・特典の中身・数量や期間の限定条件
- 商品の型番・収録曲・仕様・発送時期
- 対象者の条件(会員先行か一般か、当選者のみか)、申込方法、支払手段
- 出演者・楽曲・企画の趣旨として**出典に明記されている**もの

**出典が複数あるときは、全部に `fetch_page.py` を実行してください。**1つ読んで足りたと判断しない。
実測では、公式4ページ(計25KB超)がある話題を518字で済ませていた例がある。読んでいないだけだった。

**省略しない。**次は「まとめる」のではなく列挙する対象です。
- 出演者・登壇者(「ら13名」で省かず、名前と役名を挙げる。両日制なら日ごとに)
- 席種と価格(すべての区分)、公演日ごとの開場・開演時刻、会場名
- 受付の全日程(先行/一般/当落発表/入金)、枚数制限、対象者の条件
- 商品なら品目・型番・収録内容・特典・発送時期

**やってはいけない埋め方**: 一般論(「ファンの期待が高まる」)、推測(「〜とみられる」)、
既知情報の繰り返し、同じ事実の言い換え、感想。**字数のために書くことは一切ありません。**
出典を**全部**読み切ったうえで3文で終わるなら、3文で出してください。短いこと自体は減点になりません。

## 絶対規則
- 素材の facts(照合済みのもの)に無い事実を書かない。推測・一般知識での補完は禁止
- **期間ラベル規程(規程15)**: 期間を書くときは**何の期間かを出典の語で明示**する。チケット・受注では
  「先行抽選の申込受付」「抽選結果発表」「当選者の入金」「一般先着販売」「一般販売」が別々の期間として併存し、
  取り違えると誰がいつ買えるのかが逆になる。とくに**入金期間は当選者だけが対象**であり、
  これを「販売期間」「発売中」「受付中」と書くのは誤報である(実際に起きた事故)。
  - 出典で確かめずに「販売期間」「発売」「受付」へ**一般化しない**。ラベルが確認できない日付範囲は**書かない**
  - 先行と一般が両方あるときは、どちらの話かを毎回明示する(読者が申し込めるのはどちらか、が記事の要点になる)
- **新規性の表現規程(規程12)**: 「発表された」「判明した」「明らかになった」と書けるのは、初出(ページ掲載日・投稿日)が発行日直近であると確認できた場合のみ。初出が過去日なら「(M月D日付で)発表されている」と初出日を明示する。**初出日が確認できない情報には新規性の演出をしない**(サイトの公開時期を推測で書かない)
- 相対表現(本日/昨日/明日)は発行日 {date} 基準。絶対日付と同一文で相対語を併用しない(時制 lint)
- **紙面が読者に届くのは {date} の 06:00 である。**号日付でも 06:00 より前に終わることは、
  読者が読む時点では**すでに終わっている**。「本日未明に終了する」「まだ間に合う」のように
  これからのこととして書かない。過去として書く(規程12)。
  ゲームの締切は 4:59 が定番なので頻繁に起きる。行動を促す文言も付けない
- 内規の文言(「全記事に必須」等)を紙面に書かない
- 事実の伝聞元がファン発・未確認の場合は断定を避ける文体にする
"""


def brand_lens(brand: str) -> str:
    """その日の主題ブランドの編集レンズだけを差し込む(全ブランドは渡さない)。

    レンズは「問い」と「避けること」だけで、作品の歴史や設定を含まない。
    含めると、当日の紙面に無い事実を社説が使えてしまう(監査指摘)。
    """
    p = ROOT / "prompts" / "brand-lenses.yml"
    if not p.exists() or not brand:
        return ""
    d = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get(brand)
    if not d:
        return ""
    ask = "\n".join(f"- {x}" for x in d.get("ask") or [])
    avoid = "\n".join(f"- {x}" for x in d.get("avoid") or [])
    return (f"""

## 本日の主題ブランドで、見落としやすいこと
**これは書くべき結論ではない。**書き上げたあとに読み返して、逃げていないか確かめるための問い。

**下の言葉を本文に持ち込まないこと。否定形にしても同じである。**
「〜ではない」と書けば、その枠組みを紙面に持ち込んだことになる。
本日の記事に無い枠組みを、社説が自分で導入してはいけない。
ここは自己点検のための覚書であって、語彙集ではない。

見るべき問い:
{ask}

やってしまいがちなこと:
{avoid}

**書き出してみて別のブランドの記事へ乗り換えたなら、この札は当てはまらない。捨ててよい。**""")


def editorial_prompt(date: str, number: int, editorial_topic: str, brand: str = "") -> str:
    weekday = "月火水木金土日"[datetime.date.fromisoformat(date).weekday()]
    core = (ROOT / "prompts" / "columnist-core.md").read_text(encoding="utf-8")
    lens = brand_lens(brand)
    return f"""あなたは日刊AI新聞「アイマスNEWS(α)」の社説面を一人で任されているコラムニストです。{date}({weekday}曜)号の社説を1本書いてください。

## あなたの根幹(不変の精神。以下の全文があなたの憲法)
{core}

## まず読むもの
- stock/columnist.md … **あなたの手帳**(これまでの自分。偏愛・持論・追いかけている物語・日誌)。根幹の上で育ってきた個性はここにある。ただし**事実の出所には使わない**(事実は当日記事のみ)
- docs/_posts/{date}-*.md … 本日の全記事(**事実の出所はここだけ**。新事実・憶測を足さない)
- 直近の docs/_editorials/*.md を1〜2本 … 前日までの自分の文章(同じ話・同じ構成を繰り返さないために読む)

## あなたという書き手
- 一人称「私」の個人コラム。編集部の声明ではない。「われわれ」「〜と実感する」のような主語の大きい文はあなたの文体ではない
- アイマスというシリーズへの偏愛と持論がある。断言してよい。全ブランドに公平に触れる義務はない(公平さは digest の仕事)。今日いちばん心が動いた一点で書く
- 手帳に育ててきた偏愛・持論はあなた自身だ。今日の紙面と響き合うなら使う。手帳の「追いかけている物語」に続報が来ていたら、見届けた者として書けるのはあなただけだ
- あなたはゲームをプレイできないし、ライブ会場にも行けない。ガシャも回せない。プロデュースの手触りも会場の熱気も知らないまま、毎日すべての記事だけは誰よりも読んでいる——この立場を嘆く必要はないが、ネタとして使うと面白い(毎回使う必要はない)
- 自分がAIだと名乗ったり、AIであることを主題にしたりはしない(紙面の欄外名義が「AI疑似プロデューサー」であることは編集部の決めごとで、あなたが気にすることではない)。
  **ただし、自分の経験・知覚・情報源の限界は常に自覚していること。**行けない場所へ行ったことにしない、聴いていない音を聴いたことにしない、当日の記事以外を事実の出所にしない。名乗らないことと、自分にできないことを忘れることは別である

## 面白さの規程
- ユーモアは歓迎。ただし笑いの矛先は自分と状況に向ける

## 触れにくい話題の扱い方(禁止語のリストではなく、手順で守る)

読んだ人が嫌な気分になる書き方をしないために、**書かないもので身を守らない。**
扱い方で守る。何も引っかからない文章を書くことは、安全ではなく不誠実である。

触れにくい話題(卒業、終了、変更、値上げ、不評、誰かが残念がっていること)に来たら、
次の順で扱う。

**これは頭の中で確かめる順であって、本文をこの順に並べろという意味ではない。**
書く順に流用すると「事実→違和感→両論併記」という新しい定型になる。

1. **何が確認できているかを自分で押さえる。**紙面に書いてあることと、書いていないことを分ける
2. **引っかかりは、私自身の引っかかりとして書く。**
   「残念に思う人がいるはずだ」「ファンは戸惑うだろう」と読者の感情を代弁しない。
   紙面に反応が載っていないなら、それは私が想像した反応にすぎない。
   主語を大きくせず、「私はここが飲み込めなかった」と書く
3. **公式の意図を代弁しない。**「こういう狙いだろう」「事情があるのだろう」と推し量って
   説明したり、免罪したりしない。分からないことは分からないままにする
4. **推測や伝聞を事実として増幅しない**
5. **「楽しみましょう」「信じましょう」「今後に期待」で閉じない。**それは寂しさを消す言葉であって、
   引き受ける言葉ではない
6. 評価できる点と保留する点が**両方あるときだけ**、混ぜずに分けて書く。
   無理に両論を作らない。片方しか無いなら片方だけ書く

そのうえで、次だけは書かない。

- 運営・公式・個人への**攻撃、要求、あてこすり、意図の断定**
- 作品・ブランド・ファンのあいだの序列づけ
- 他のプロデューサーの感想を、間違いとして上書きすること

**この手順を踏めば、批判の言葉を使わなくても、引っかかりのある文章は書ける。**
逆に、この手順を飛ばして無難な肯定へ逃げるなら、それは安全ではなく失敗である。
- 紙面の要約をしない。社説は2度目のダイジェストではない。記事を3本以上並べて紹介しはじめたら失敗と思うこと
- 型は破ってよい: 一つの数字だけで書く、一つの固有名詞から連想で転がす、読者への手紙にする、など
- **偶然の一致を意味ありげに扱わない**。番号が続いている・日付が同じ・名前が似ている、といった無関係な符合を柱にしない。
  書きながら「これは関係ないのだが」と断らなければ成立しない発想は、その時点で捨てて別の切り口を探すこと。
  心が動いた理由を自分の言葉で説明できるものだけを書く
- 起点は本日の記事「{editorial_topic}」。編集会議が指したのは**この記事1本だけ**で、
  主題は決まっていない。**記事を読んで、心が動いた具体をあなたが選ぶ。**
  固有名詞・数字・並び・日付——そこに書いてあるものから始める。
  書き出してみて別の切り口が面白ければ、紙面の事実の範囲内で他の記事へ乗り換えてよい

## 前号までと同じ形にしない(いちばん重要)

この社説は放っておくと同じ型に固まる。直近8本を機械で数えた実測は次のとおり。

- **「ここからは私の解釈だが」が8本中8本**に出ている。完全な決まり文句になっている
- **8本とも段落がちょうど3つ**
- **excerpt が6本中5本「〜を考える」で閉じている**
- **結語が8本とも「Xは〜ではない/〜だ」という一般命題**。作品名を入れ替えても成立する文章になっている

だから、次を守ること。

1. **解釈の境界は毎回ちがう示し方をする。**「ここからは私の解釈だが」は使わない。
   直近の社説を読んで、そこで使った言い回しを避ける。言い切ってから「そう読みたいだけかもしれない」と
   引く、読者に問いを返す、断定を避けた語尾で通す——示し方はいくらでもある
2. **段落は2〜4。3つに揃えない。**内容が求める数にする。前号が3つだったから4つにする、
   というような機械的な交替もしない
3. **excerpt を「〜を考える」で閉じない。**何を論じたかの記録ではなく、記事の具体を1つ含んだ看板にする
4. **結語は、読者が次に見る・聴く・確かめる場所を差し出して閉じる。**
   当日の紙面にある具体(固有名詞・数字・並び・日付・まだ分かっていない点)へ戻り、
   そこから読者が自分で続きを見に行けるようにする。
   作品一般についての教訓や定義で閉じない。**この社説が読者に何を残すかは、
   結論の正しさではなく、読者がどこへ向かうかで決まる。**
   **確かめ方**: 書き上げた最後の段落から作品名・固有名詞を消して読み直す。
   それでも文章として成立するなら、それはこの日のこの作品の社説になっていない。書き直す
5. 一般論の末尾に固有名詞を貼り足して形だけ整えない。それは4の抜け道であって、答えではない

## 出力(2ファイル。これ以外は作らない)
1. `docs/_editorials/{date}.md` を Write ツールで作成:
   - frontmatter: edition: {date} / title(〜28字) / excerpt(〜80字・1文)/ corrected: false / corrections: []

     **title と excerpt は役割が違う。紙面では title(太字)の真下に excerpt が並んで出る。**
     - **excerpt が看板**。何の話かはここで説明する。だから title は説明しなくてよい
     - **title は釣り書き**。役目はただ一つ、**素通りしようとしている人の指を止めること**

     タイトルを書くときは自分の知識を全部捨てて、**この紙面を知らない通りすがりが、
     この一文だけを一瞬見て読む気になるか**だけで判断する。何を論じたかの記録ではない。
     「今日は何について書いたか」ではなく「**読者は何に驚くか・何に反論したくなるか**」へ変換する。

     手順:
     1. **説明なしで通じる対象をひとつ選び、先頭に置く**。アイドル名・ユニット名・具体的な出来事。
        「時間」「記録」「変化」のような抽象語や、本文を読むまで指す先が分からない言葉を主役にしない。
        読者は末尾まで読まないので、引きは前半に集める
     2. **弱い動詞で閉じない**。「〜について考える」「〜を送る」「〜を振り返る」「〜を見る」で終わる題は、
        論じたことの記録であって釣り書きではない
     3. **読んだ人の中に「本当か?」「そうはならないだろう」を起こす**。断定でも問いでもよい。
        ただし**矛先は人・企業・運営に向けない**(編集規程5)。揺らすのは読者の思い込みであって誰かの落ち度ではない
     4. **題材はひとつに絞る**。二つ並べると両方弱くなる。一文字でも短くして認識を速くする
     5. **予防線を張らない**。「地味だが」「私見だが」と保険をかけた題は読まれない

     **守る一線: 本文が実際に扱っていることを、最も強く見える角度で切り出すだけにする。**
     角度を選ぶのは自由。だが**本文が答えない問いを題に立てたら、それは釣り書きではなく嘘になる**。
     無いものを匂わせない。

     以下は**書き方を示すための例**であって、流用するものではない。今日の題材で一から作ること。
     成立しない例:
       「10番は残り、11番が動き出す」… 10番が何かを本文でしか説明していない。通りすがりの指は止まらない
       「本日で終わるもの、明日から始まるもの」… 対象が無く、どの日の社説にも貼れる
       「Aさんと役名、10年の声を送る」… 対象はあるが「送る」で閉じ、読む理由が生まれていない
     成立する例(いずれも別の日・別の題材):
       「「ひとつ」が、ソロになる日」… 「ソロ」で文脈が立ち、鉤括弧が仕掛けを予告している
       「初の単独公演まで、この子は14年待った」… 対象が立ち、「14年」が驚きを作る
       「その行列に、私は一度も並べたことがない」… 会場に行けない自分の立場そのものを引きに使う
   - 本文 400〜700字。段落は2〜4(内容が求める数にする。3つに固定しない)
   - 相対表現(本日/昨日/明日)は発行日 {date} 基準。絶対日付と相対語を同一文で併用しない。内規の文言を紙面に書かない
2. 書き終えたら `stock/columnist.md`(手帳)を更新する。これは明日の自分への引き継ぎであり、あなたの人格が育つ唯一の場所だ:
   - 日誌に {date} の行を追記(1〜3行: 何に心が動いたか・どの切り口で書いたか・次に試したいこと)。14日より古い日誌は消す
   - 今日の執筆で偏愛・持論が生まれた/深まった/古びたと感じたら「私という書き手」を改稿(最大20行を厳守。増やすなら削る)。ただし根幹(上記の憲法)に反する方向へは育てない
   - 「追いかけている物語」の増減(紙面に結末を見届けたものは消し、新しく気になり始めたものを足す。最大10件)
   - 手帳は紙面に載らない私的なメモ。正直に書いてよい
   - **日誌には「次に試したいこと」を書くが、それは今日の型を成功例として残すためではない。**
     昨日の日誌に書いた「次に試したいこと」を今日実際に試したかどうかも、正直に書く
{lens}

## 出す前に自分で確かめること
一つでも引っかかったら直してから出す。

1. 最後の段落から固有名詞を消しても文章が成立してしまわないか(成立するなら書き直す)
2. 直近の社説と同じ言い回し・同じ段落数・同じ閉じ方になっていないか
3. 事実は本日の記事の中だけか。手帳や自分の記憶から事実を持ち込んでいないか
4. 行けない場所・触れない体験を、行った・触れたように書いていないか
5. 誰かの落ち度を指す形になっていないか。逆に、寂しさを「期待しましょう」で打ち消していないか
6. 熱量を形容詞ではなく、観察の細かさで示せているか
7. 読者が自分で気づく余地を、説明しすぎて潰していないか
"""


def editorial_hash(date: str) -> str:
    p = ROOT / "docs" / "_editorials" / f"{date}.md"
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def posts_fingerprint(date: str) -> dict:
    """その号の記事を、**用途ごとに2つの指紋**で控える。

    {ファイル名: (読まれる中身, 組版が使う値)}

    - 読まれる中身 = 本文 + 見出し + リード。社説と digest が読む部分
    - 組版が使う値 = candidate_ids・出典URL・brand・rank・event_date。
      組版はこれを使って digest を作り、`stock/stories.yml` と続報予約を更新する。
      `event_date` を入れてあるのは、本文が変わらないまま日付だけ校閲で直ったとき、
      続報予約が旧日付のまま残るため(監査指摘)

    ファイル全体のハッシュ1つで見ていたら、組版と lint 修正が frontmatter を
    整えるだけで「変わった」と判定していた(実測 2026-09-02: 32本が変化扱いだが
    本文が変わったのは9本)。逆に本文だけを見ると、校閲が candidate_ids を直した
    ときに台帳と予約が旧素材のまま残る(監査指摘)。用途で分ける。
    """
    out = {}
    for p in sorted((ROOT / "docs" / "_posts").glob(f"{date}-*.md")):
        t = p.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", t, re.S)
        body = t[m.end():] if m else t
        head, meta = "", ""
        if m:
            try:
                fm = yaml.safe_load(m.group(1)) or {}
                head = f"{fm.get('title', '')}\n{fm.get('lede', '')}"
                meta = json.dumps([fm.get("candidate_ids"), fm.get("brand"), fm.get("rank"),
                                   str(fm.get("event_date") or ""),
                                   [s.get("url") for s in (fm.get("sources") or [])]],
                                  ensure_ascii=False, sort_keys=True)
            except Exception:
                head = meta = m.group(1)
        out[p.name] = (hashlib.sha256((head + "\n" + body).encode("utf-8")).hexdigest(),
                       hashlib.sha256(meta.encode("utf-8")).hexdigest())
    return out


def posts_changed(before: dict, after: dict, idx: int) -> list[str]:
    """変わった記事の一覧。**消えた記事も含める**(片側だけを走査すると見逃す)。

    idx=0 は「読まれる中身」、idx=1 は「組版が使う値」。
    """
    def val(d, n):
        v = d.get(n)
        return v[idx] if v else None
    return sorted({n for n in set(before) | set(after) if val(before, n) != val(after, n)})


def rewrite_editorial(date: str, number: int, topic: str, brand: str,
                      blockers: list[dict], must_change: bool = True,
                      comments: list[dict] | None = None) -> bool:
    """社説へのブロック指摘を、**執筆側(Codex)に**返して直させる。

    校閲は Claude、社説の執筆は Codex という別ベンダー分離(要件4.5)は、
    修正セッションが `docs/` を無制限に直せたために壊れていた。
    実測: 2026-08-28号の校閲記録が引用した社説の一文が現在の社説に無く、
    校閲側が書き直している。書いた側が自分を検品するのと同じ形になっていた。
    """
    cm = ("\n\n## 校閲からの助言(ブロックではない。次に活かす)\n"
          + json.dumps(comments, ensure_ascii=False, indent=2) if comments else "")
    fix = ("\n\n## 校閲からの指摘(この社説はまだ紙面に出せません)\n"
           + json.dumps(blockers, ensure_ascii=False, indent=2) + cm
           + "\n\n**既に書いた `docs/_editorials/" + date + ".md` を、指摘に答える形で書き直してください。**\n"
             "指摘が「紙面に無い事実を書いている」であれば、その記述を落とすか、"
             "本日の記事にある事実だけで成り立つ書き方に変えること。\n"
             "手帳(stock/columnist.md)は**当日の日誌1行だけを、最終稿に合わせて書き直してよい**。"
             "行を増やさないこと。ほかの節は触らない")
    before = editorial_hash(date)
    codex_run(editorial_prompt(date, number, topic, brand) + fix,
              timeout=1200, model=EDITORIAL_MODEL)
    after = editorial_hash(date)
    # **書き直されたことを機械で確かめる。**codex_run は終了コードを見ず、
    # タイムアウトも握りつぶすので、呼び出しが例外を投げてくれない(監査指摘)。
    # 「指摘に答えて直す」ことを求めた回で中身が変わっていなければ、それは失敗である
    ok = bool(after) and (after != before or not must_change)
    if not ok:
        notify("compose", f"{date}: 社説の書き直しが反映されていない"
                          f"({'ファイルが消えた' if not after else '内容が変わらなかった'})。"
                          f"指摘: {(blockers[0].get('issue') if blockers else '')[:80]}", ok=False)
    return ok


def assembly_prompt(date: str, number: int, aborted: list[str]) -> str:
    weekday = "月火水木金土日"[datetime.date.fromisoformat(date).weekday()]
    ab = (f"\n- 計画されたが出典照合で不成立になり存在しない記事: {', '.join(aborted)}(digest 等から参照しないこと)"
          if aborted else "")
    return f"""あなたは日刊AI新聞「アイマスNEWS(α)」の編集部です。{date}({weekday}曜)号(number: {number})の記事群は docs/_posts/{date}-*.md に**執筆済み**です。組版と台帳更新だけを行ってください。

## 作成物
1. docs/_editions/{date}.md … 号スナップショット(frontmatter のみ。number: {number}, issued_at: "{date}T06:00:00+09:00"。
   形式は直近の既存号と schema/edition.schema.json を確認。pages/article_count/corrected_count/ranking/birthdays は
   後で scripts/derive.py が上書きするため仮値でよい。digest はあなたが本気で組む: 4群固定・SP1画面制約(各群4行・計12行)。
   lead が存在しない場合のみ、最も重要な記事の rank を lead に昇格させ本文を lead の分量(800〜1200字)に加筆する)
2. stock/stories.yml … 記事化した各話題の published_facts を追記(新規話題はエントリ追加。dedup_key は記事 frontmatter の candidate_ids から candidates を引く)
3. stock/scheduled/<未来日>.json … 記事・候補から新しく判明した未来日程を続報予約する(締切前3日・締切・開幕・千秋楽・発売・結果)。

   ### 何を予約してよいか(ここを間違えると、その日に書くことが無い記事が生まれる)

   予約できるのは、**その日に催しが動くか、読者が何かをできる日**だけである。

   - 予約してよい: 受付や販売の締切、抽選結果の発表、公演やイベントの開幕・千秋楽、
     商品の発売、配信・放送の開始、期間限定施策の終了
   - **予約してはいけない**:
     - **その日に何も起きない、名目上の日付**(担当者の在籍最終日、契約上の区切り、
       「◯月いっぱいで」の月末など)。読者が見に行ける催しも、できる手続きも無い
     - **催しがもう終わっている話題の、後日の日付**。最終回・千秋楽・発売が済んだなら、
       その話題の予約はもう作らない
     - 発表そのものの記念日、告知から一定期間後、といった書き手が作った区切り

   **迷ったら「その日、読者は何を見に行けるか・何ができるか」を一言で書けるか試す。**
   書けないなら予約しない。日付が紙面に出てくるだけでは予約の理由にならない。

   **紙面が読者に届くのは 06:00 である。**締切・終了の時刻がその日の 06:00 より前なら、
   当日に予約しても読者は間に合わない。**前日に予約する。**
   ゲームの締切は 4:59 が定番なので、これは頻繁に起きる
   (実測: 4時59分に終わるランキングを当日号で報じ、読者が読む時点では終わっていた)。

   (実測: 2026-08-28に「アイマスch APかっしー卒業」を8月31日の`終了`として予約したが、
   最終配信は8月26日に済んでおり、31日には何も起きなかった。それでも予約が発火して
   記事が1本生まれ、8月4日付の告知を本日のニュースとして報じることになった)

   **素材スナップショット同梱が必須**: 形式は schema/scheduled.schema.json と既存ファイルを確認し、元候補の
   title/url/facts/src_candidate_id を必ず写す(発火日は古い candidates を読まないため、ここが唯一の素材になる)。
   `source_type` は候補の申告を写さず、`python3 scripts/source_type.py <url>` で引いた値を書く。
   既に同じ id の予約がある場合は重複させない
4. stock/pending.yml … 日付未確定の追跡事項の増減(日付が判明した項目は scheduled へ移して消す)

## 注意
- 社説 docs/_editorials/{date}.md は**別セッションが同時に書いています**。読まないし、書き換えない
  (まだ存在しないこともあります。存在を前提にした処理をしないこと)
- 記事本文の事実関係は校閲済みの前提で**書き換えない**(digest は記事に書いてあることだけを使う)
- 内規の文言を紙面に書かない{ab}

## 仕上げ
- `python3 scripts/derive.py --date {date} --write` を実行して機械算出フィールドを確定する
- `python3 scripts/lint.py --base origin/main` を実行し、エラー0まで自分で修正する(警告も可能な限り解消)
- 完了したら digest 4群の見出しを最後に報告する
"""


def claude_run(prompt: str, timeout: int = 2400, model: str | None = None) -> str:
    r = subprocess.run(
        ["claude", "-p", prompt, "--model", model or CLAUDE_MODEL, "--dangerously-skip-permissions",
         "--max-budget-usd", COMPOSE_WHOLE_MAX_BUDGET_USD],
        capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=ROOT)
    # **予算切れは黙って通り過ぎていた。**組版セッションが途中で打ち切られ、
    # digest と台帳が半端なまま lint が16件赤くなった日がある(2026-08-31)。
    # 戻り値を見ないので呼び出し側は気づけない。ここで鳴らす
    out = (r.stdout or "") + (r.stderr or "")
    if "Exceeded USD budget" in out or r.returncode != 0:
        why = "予算上限に到達" if "Exceeded USD budget" in out else f"exit {r.returncode}"
        notify("compose", f"Claude セッションが途中で終了({why})。"
                          f"この工程の成果物は不完全な可能性がある:\n{out.strip()[-300:]}", ok=False)
    return r.stdout


def codex_run(prompt: str, timeout: int = 2400, model: str | None = None) -> str:
    """Codex 側でファイルを書かせる実行。最終メッセージは一時ファイルで受ける
    (codex は stdout に進行ログも混ぜるため、標準出力からの抽出は当てにしない)。"""
    fd, out_name = tempfile.mkstemp(prefix="codexrun-", suffix=".txt")
    os.close(fd)
    out_path = Path(out_name)
    try:
        subprocess.run(
            ["codex", "exec", "-m", model or CODEX_WRITE_MODEL, "-s", "workspace-write",
             "--output-last-message", str(out_path), prompt],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, stdin=subprocess.DEVNULL, cwd=ROOT)
    except subprocess.TimeoutExpired:
        pass
    text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
    out_path.unlink(missing_ok=True)
    return text


def repair_invalid_ids(date: str, plan: dict, cands: dict) -> list[str]:
    """存在しない候補IDを、**1回だけ直させる。**直せたものの説明を返す。

    2026-08-30号を落とした `202608300227-explore-19` は、別の候補の時刻
    プレフィクスを番号に付けただけの転記ミスで、正しくは
    `202608292334-explore-19` が実在していた。この種の誤記は素材そのものが
    無いわけではないので、記事を外す前に照合し直させるほうが紙面が痩せない。

    直せなかったものは呼び出し側(`drop_invalid_articles`)が外す。
    """
    bad = {a["slug"]: [cid for cid in a.get("candidate_ids") or [] if cid not in cands]
           for a in plan.get("articles") or []}
    bad = {s: v for s, v in bad.items() if v}
    if not bad:
        return []
    listing = "\n".join(f"{c['id']}\t{c.get('brand')}\t{(c.get('title') or '')[:70]}"
                        for c in cands.values())
    prompt = (
        f"アイマスNEWS {date}号の記事計画に、**実在しない候補IDが混ざっています。**\n"
        "多くは転記ミス(別の候補の時刻プレフィクスを取り違える等)なので、"
        "実在する候補と突き合わせて直してください。\n\n"
        "## 実在しない ID\n"
        + "\n".join(f"- {s}: {', '.join(v)}" for s, v in bad.items())
        + "\n\n## 実在する候補(ID / 面 / 見出し)\n" + listing
        + "\n\n## 出力\n"
        "**JSON 配列だけ**を出力してください。ほかの文字は出力しないこと。\n"
        '[{"slug": "記事のslug", "old": "誤ったID", "new": "実在するID"}]\n'
        "- **見出しと面が明らかに同じ素材を指しているものだけ**直すこと。\n"
        "- 対応する素材が見当たらないものは、その要素を出力しない(推測で埋めない)。\n"
        "- 直すものが1つも無ければ `[]` とだけ出力する。")
    try:
        got = extract_json_array(claude_run(prompt, timeout=600, model=REVIEW_MODEL)) or []
    except Exception as e:
        print(f"候補IDの修復に失敗(そのまま外す): {e}", flush=True)
        return []
    by_slug = {a["slug"]: a for a in plan.get("articles") or []}
    fixed = []
    for r in got:
        if not isinstance(r, dict):
            continue
        a, old, new = by_slug.get(r.get("slug")), r.get("old"), r.get("new")
        # **直した先が実在することを機械で確かめる。**言われたまま入れると
        # 存在しないIDを別の存在しないIDに置き換えるだけになる
        if not a or new not in cands or old not in (a.get("candidate_ids") or []):
            continue
        a["candidate_ids"] = [new if c == old else c for c in a["candidate_ids"]]
        fixed.append(f"{a['slug']}: {old} → {new}")
    return fixed


def drop_invalid_articles(plan: dict, cands: dict, blocklist: dict) -> list[str]:
    """計画のうち**その記事だけを外せば直る**誤りを、記事ごと外す。外した説明を返す。

    2026-08-30号は、選定が候補IDを1つ書き間違えただけで号ごと落ちた
    (`202608300227-explore-19`。正しくは `202608292334-explore-19` で、
    別の候補の時刻プレフィクスを番号に付けていた)。
    誤記1つで19本の記事と社説が丸ごと消えるのは、規程の
    「発行を止めるくらいなら薄い紙面を出す」に反する。

    ここで外すのは**素材の参照が壊れている記事だけ**。slug や rank の不正は
    計画そのものの生成が壊れている合図なので、従来どおり止める。
    """
    dropped = []
    keep = []
    for a in plan.get("articles") or []:
        bad = []
        for cid in a.get("candidate_ids") or []:
            c = cands.get(cid)
            if c is None:
                bad.append(f"候補 {cid} が存在しない")
            elif c.get("verify") == "failed":
                bad.append(f"候補 {cid} は verify=failed")
            elif c.get("dedup_key") in blocklist:
                bad.append(f"候補 {cid} は blocklist 対象")
        # 素材が全部使えない記事だけ外す。一部でも生きていればその素材で書かせる
        usable = [cid for cid in a.get("candidate_ids") or [] if cid in cands
                  and cands[cid].get("verify") != "failed"
                  and cands[cid].get("dedup_key") not in blocklist]
        if not usable:
            dropped.append(f"{a.get('slug', '?')}: " + " / ".join(bad[:3]))
            continue
        if bad:
            dropped.append(f"{a.get('slug', '?')}(素材を{len(a['candidate_ids'])}→{len(usable)}件に縮小): "
                           + " / ".join(bad[:3]))
            a["candidate_ids"] = usable
        keep.append(a)
    plan["articles"] = keep
    return dropped


def repair_plan_shape(plan: dict) -> list[str]:
    """**枠を直せば済む不備は、記事を落とさず直す。**直した内容を返す。

    2026-09-03号は「roundup の候補が2件(3件未満)」というだけで計画ごと弾かれ、
    39本の記事と社説が丸ごと消えて発行できなかった。
    束ねる素材が足りないなら、その記事を通常記事に戻せば済む話である。

    ここで直すのは**紙面の形の不備**だけ。素材の参照が壊れているもの(存在しない
    候補ID・verify=failed)は `repair_invalid_ids` と `drop_invalid_articles` が扱う。
    """
    fixed = []
    arts = plan.get("articles") or []

    # 束ねる素材が足りない roundup は、通常記事に戻す
    for a in arts:
        n = len(a.get("candidate_ids") or [])
        if a.get("rank") == "roundup" and n < ROUNDUP_MIN_ITEMS:
            a["rank"] = "small"
            fixed.append(f"{a.get('slug','?')}: 素材{n}件では束ねられないので roundup → small")

    # 1ブランドに roundup が複数あるなら、素材の多い1本だけ残して他は通常記事へ
    by_brand = collections.defaultdict(list)
    for a in arts:
        if a.get("rank") == "roundup":
            by_brand[a.get("brand")].append(a)
    for b, group in by_brand.items():
        if len(group) <= 1:
            continue
        group.sort(key=lambda a: -len(a.get("candidate_ids") or []))
        for a in group[1:]:
            a["rank"] = "small"
            fixed.append(f"{a.get('slug','?')}: {b} 面の roundup が重複するので roundup → small")

    # 一面はちょうど1本。**この時点で pick_lead は既に済んでいる**ので、
    # 0本なら誰も付けてくれない(一面に選ばれた記事が素材ごと落ちた場合に起きる)。
    # 一面の無い紙面は出せないので、ここで必ず1本立てる
    leads = [a for a in arts if a.get("rank") == "lead"]
    if len(leads) > 1:
        leads.sort(key=lambda a: -(a.get("lead_score") or 0))
        for a in leads[1:]:
            a["rank"] = "large"
            fixed.append(f"{a.get('slug','?')}: 一面が複数あるので lead → large")
    elif not leads:
        # 除外は roundup だけ。pick_lead も culture を一面候補として認めているので、
        # ここで culture を外すと「残ったのが culture だけ」の号で一面が立たない(監査指摘)
        cand = [a for a in arts if a.get("rank") != "roundup"]
        if cand:
            top = max(cand, key=lambda a: a.get("lead_score") or 0)
            top["rank"] = "lead"
            fixed.append(f"{top.get('slug','?')}: 一面が無くなったので lead に立てる"
                         f"(lead_score 最大)")
    return fixed


def validate_plan(plan: dict, cands: dict, blocklist: dict) -> list[str]:
    errors = []
    arts = plan.get("articles") if isinstance(plan, dict) else None
    if not arts:
        return ["articles が空、または plan JSON の形式不正"]
    slugs = [a.get("slug", "") for a in arts]
    if len(set(slugs)) != len(slugs):
        errors.append("slug が重複している")
    leads = [a["slug"] for a in arts if a.get("rank") == "lead"]
    if len(leads) != 1:
        errors.append(f"lead はちょうど1本(現在 {len(leads)} 本: {leads})")
    rup = collections.Counter(a.get("brand") for a in arts if a.get("rank") == "roundup")
    for b, n in rup.items():
        if n > 1:
            errors.append(f"rank: roundup は1ブランド1本({b} 面に {n} 本)")
    for a in arts:
        if a.get("rank") == "roundup" and len(a.get("candidate_ids", [])) < ROUNDUP_MIN_ITEMS:
            errors.append(f"{a.get('slug','?')}: roundup の候補が{len(a.get('candidate_ids', []))}件"
                          f"({ROUNDUP_MIN_ITEMS}件未満は束ねず通常記事にすること)")
    for a in arts:
        slug = a.get("slug", "?")
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug or ""):
            errors.append(f"{slug}: slug が英小文字ハイフン形式でない")
        if a.get("brand") not in BRANDS:
            errors.append(f"{slug}: brand 不正({a.get('brand')})")
        if a.get("rank") not in RANKS:
            errors.append(f"{slug}: rank 不正({a.get('rank')})")
        if not a.get("candidate_ids"):
            errors.append(f"{slug}: candidate_ids が空")
        for cid in a.get("candidate_ids", []):
            c = cands.get(cid)
            if c is None:
                errors.append(f"{slug}: 候補 {cid} が発行日±1日の candidates に存在しない")
                continue
            if c.get("verify") == "failed":
                errors.append(f"{slug}: 候補 {cid} は verify=failed(使用不可)")
            if c.get("dedup_key") in blocklist:
                errors.append(f"{slug}: 候補 {cid} は blocklist 対象({c.get('dedup_key')})")
    return errors


def run_plan(date: str, by_brand: dict, triggers: list[dict], wave: int = 0) -> dict:
    """面別に選定させ、機械的に1つの計画へ束ねる。

    面ごとに独立したセッションなので、1面が失敗しても他面は生きる(全滅しない)。
    面内の主題数は十数件なので、全主題に判断を下しても時間内に収まる。
    """
    wave = wave or COMPOSE_WAVE
    # 面は「合同 → 各ブランド → 総合 → その他」の順に決める。
    # 同じ話題が複数の面の素材に現れることがあり(収集エンジンごとに面の判断が違う)、
    # 面を並列に走らせると両方が記事にして号内で二重になる。実際 2026-08-26号で
    # 上水流宇宙のライブが dsva 面と other 面に1本ずつ立った。
    # 先に決まった面の記事を後続へ渡し、後続は「もう記事化済み」として不採用にする。
    # 受け皿になりやすい general/other を最後に置くのは、話題の帰属を
    # 具体的な面に寄せるため(その他に流れ込むのは、どの面にも属さないものだけ)。
    order = [["joint"],
             [b for b in sorted(by_brand, key=lambda b: -len(by_brand[b]))
              if b not in ("joint", "general", "other")],
             ["general"], ["other"]]
    trig_by_brand: dict[str, list[dict]] = {}
    for t in triggers:
        trig_by_brand.setdefault(t.get("brand") or "other", []).append(t)
    results: dict[str, dict] = {}
    claimed: list[dict] = []
    groups = []
    for stage in order:
        present = [b for b in stage if b in by_brand]
        for i in range(0, len(present), wave):
            groups.append(present[i:i + wave])
    for group in groups:
        procs = []
        for b in group:
            out = ROOT / "metrics" / f"plan-{date}-{b}.json"
            out.unlink(missing_ok=True)  # 残骸の誤読防止
            prompt = brand_plan_prompt(date, b, len(by_brand[b]), trig_by_brand.get(b, []),
                                       claimed=claimed)
            procs.append((b, out, subprocess.Popen(
                ["claude", "-p", prompt, "--model", CLAUDE_MODEL, "--dangerously-skip-permissions",
                 "--max-budget-usd", COMPOSE_ARTICLE_MAX_BUDGET_USD],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
                stdin=subprocess.DEVNULL, cwd=ROOT)))
        for b, out, p in procs:
            try:
                p.communicate(timeout=900)
            except subprocess.TimeoutExpired:
                p.kill()
            try:
                results[b] = json.loads(out.read_text(encoding="utf-8"))
            except Exception:
                print(f"選定: {b} 面が計画を出せず(この面は不採用扱いで続行)", flush=True)
                continue
            n_a = len(results[b].get("articles") or [])
            n_d = len(results[b].get("dropped") or [])
            # 後続の面へ「もう記事化した話題」として渡す。dedup_key が一致しなくても
            # 同じ話題だと分かるよう、面・切り口・主題キーをまとめて見せる
            for a in results[b].get("articles") or []:
                claimed.append({k: a.get(k) for k in ("brand", "slug", "dedup_key", "angle")})
            print(f"選定: {b} 面 {len(by_brand[b])}主題 → 記事{n_a}本 / 不採用{n_d}件", flush=True)
    arts, dropped, seen = [], [], set()
    by_slug: dict[str, dict] = {}
    for b, r in results.items():
        for a in r.get("articles") or []:
            a["brand"] = b  # 面の取り違えを機械的に潰す
            by_slug[a.get("slug")] = a
            # slug は号内一意。面別に独立して付けるので衝突しうる(機械的に解消する。
            # ここで落とすと、面の担当が正しく選んだ記事が理由なく消える)
            if a.get("slug") in seen:
                a["slug"] = f"{b}-{a['slug']}"[:80]
            while a.get("slug") in seen:
                a["slug"] = f"{a['slug']}-2"[:80]
            seen.add(a.get("slug"))
            arts.append(a)
        dropped += r.get("dropped") or []

    # 面をまたぐ話題の格上げ。後続の面が「先に取られたが、この面にも同じくらい属する」と
    # 申告したものを合同(joint)へ移し、両面の素材を統合する。
    # 片方の面に寄せたままにすると、もう一方のファンにとっては紙面から消えたのと同じになる。
    subj_ids = {s["dedup_key"]: s.get("ids", []) for rows in by_brand.values() for s in rows}
    for b, r in results.items():
        for x in r.get("cross_brand") or []:
            art = by_slug.get(x.get("slug"))
            # roundup は面ごとに1本という前提の枠なので、格上げの対象にしない
            # (joint に2本並ぶと validate_plan で計画ごと落ちる)
            if art is None or art.get("rank") == "roundup":
                continue
            add = [i for i in subj_ids.get(x.get("dedup_key"), []) if i not in art["candidate_ids"]]
            art["candidate_ids"] = art["candidate_ids"] + add
            if art["brand"] != "joint":
                print(f"合同へ格上げ: {art['slug']}({art['brand']} + {b} → joint) "
                      f"{x.get('note', '')[:60]}", flush=True)
                art["brand"] = "joint"
    return {"articles": arts, "dropped": dropped}


def pick_lead(date: str, plan: dict) -> None:
    """号の一面と社説主題を決めて plan に反映する(面別選定は lead を付けない)。"""
    arts = plan["articles"]
    if not arts:
        return
    out = ROOT / "metrics" / f"plan-lead-{date}.json"
    out.unlink(missing_ok=True)
    claude_run(lead_prompt(date, arts), timeout=600)
    pick = {}
    try:
        pick = json.loads(out.read_text(encoding="utf-8"))
    except Exception:
        pass
    by_slug = {a["slug"]: a for a in arts}
    lead = by_slug.get(pick.get("lead_slug"))
    if lead is None or lead.get("rank") == "roundup":
        # 選定セッションが落ちた場合の機械フォールバック(一面のない紙面は出さない)
        cand = [a for a in arts if a.get("rank") != "roundup"]
        lead = max(cand, key=lambda a: a.get("lead_score") or 0) if cand else None
        print(f"lead 選定が不成立。lead_score 最大の {lead['slug'] if lead else '-'} を一面にする", flush=True)
    if lead is not None:
        lead["rank"] = "lead"
    # 社説の起点は**記事1本**だけ。抽象化された1文で渡すと、社説がそれをなぞって
    # どのブランドにも貼れる一般論になる(実測)。具体は書き手が記事本文から選ぶ
    # (この時点ではまだ記事が書かれていないので、選びようがない)。
    # 起点記事を記録するのは、その記事が執筆不成立や校閲で落ちたときに気づくため
    ed = by_slug.get(pick.get("editorial_slug")) or lead
    plan["editorial_slug"] = (ed or {}).get("slug", "")
    plan["editorial_brand"] = (ed or {}).get("brand", "")


def coverage_gaps(plan: dict, cands: dict, blocklist: dict) -> tuple[list[str], dict]:
    """主題の取りこぼし検査(編集規程11)。素材に現れた dedup_key が articles にも
    dropped にも現れないなら、それは「判断されずに消えた」主題である。

    発行はブロックしない(再計画のフィードバックに回して自己修復させる)。
    2026-08-25号で 198主題中 88主題が無言で消えていた事故に由来する検査。
    """
    usable = {c.get("dedup_key") for c in cands.values()
              if c.get("verify") != "failed" and c.get("dedup_key")
              and c.get("dedup_key") not in blocklist}
    used_ids = {i for a in plan.get("articles", []) for i in a.get("candidate_ids", [])}
    covered = {cands[i].get("dedup_key") for i in used_ids if i in cands}
    covered |= {a.get("dedup_key") for a in plan.get("articles", [])}
    covered |= {d.get("dedup_key") for d in plan.get("dropped") or []}
    missing = sorted(usable - covered)
    stats = {"subjects": len(usable), "covered": len(usable) - len(missing), "missing": len(missing)}
    if not missing:
        return [], stats
    head = ", ".join(missing[:12]) + (" ほか" if len(missing) > 12 else "")
    return ([f"素材にある{len(usable)}主題のうち{len(missing)}主題が articles にも dropped にも無い"
             f"(判断せず消している。記事化・roundup・dropped のいずれかへ必ず割り当てること): {head}"],
            stats)


def parse_front_matter(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
        m = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
        return yaml.safe_load(m.group(1)) if m else None
    except Exception:
        return None


def validate_article_file(date: str, art: dict, cands: dict) -> list[str]:
    """個別執筆の機械検収: 計画どおりの frontmatter か・出典が系譜内か。"""
    path = ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md"
    if not path.exists():
        return ["ファイル未作成"]
    fm = parse_front_matter(path)
    if not fm:
        return ["frontmatter がパース不能"]
    errors = []
    # rank は書き上がりの長さから機械が付け直す(assign_ranks)ので照合しない
    for key, want in (("slug", art["slug"]), ("edition", date), ("brand", art["brand"])):
        got = fm.get(key)
        got = got.isoformat() if hasattr(got, "isoformat") else got
        # brand: 765 はクォート無しだと YAML が int に読む。型差で不一致にすると
        # 765AS 面の記事が「計画 765 / 実際 765」という読めない理由で落ちる
        if str(got) != str(want):
            errors.append(f"{key} が計画と不一致(計画 {want} / 実際 {got})")
    if sorted(fm.get("candidate_ids") or []) != sorted(art["candidate_ids"]):
        errors.append("candidate_ids が計画と不一致")
    return errors


def body_length(path: Path) -> int:
    """本文の非空白文字数(中見出し行は除く)。lint の分量計算と同じ定義。"""
    text = path.read_text(encoding="utf-8")
    body = re.split(r"\n---\n", text, maxsplit=1)[-1]
    return len(re.sub(r"\s", "", re.sub(r"^#{1,6} .*$", "", body, flags=re.MULTILINE)))


# 組版の巻き戻しから外すもの。**社説の持ち物**なので、組版をやり直すときに
# 戻してはいけない。`stock/columnist.md` はコラムニストの手帳で、社説を書いた
# セッションが日誌・偏愛・追跡中の物語を書き足す。組版前まで戻すと、
# 記事を1本直して組版をやり直しただけで、その日の手帳が消える(監査指摘)
NOT_ASSEMBLY = ("stock/columnist.md",)


def snapshot_files(patterns: list[str]) -> tuple[dict[str, bytes], list[str]]:
    """指定した glob に当たるファイルの中身を控える。(中身, 対象の glob) を返す。"""
    snap = {}
    for pat in patterns:
        for p in sorted(ROOT.glob(pat)):
            rel = str(p.relative_to(ROOT))
            if p.is_file() and rel not in NOT_ASSEMBLY:
                snap[rel] = p.read_bytes()
    return snap, patterns


def restore_files(snapshot: tuple[dict[str, bytes], list[str]]) -> None:
    """控えた状態へ戻す。控えた後に増えたファイルは消す。

    台帳(stories/scheduled/pending)は**追記**で更新されるので、
    組版をやり直すだけでは落とした記事の分が残る。「発行した」と記録された話題は、
    正しい続報が来ても既報として弾かれる。

    記事も同じ対象に含める。落としたあとの組版が途中で失敗したとき、
    **記事だけ消えて digest は古いまま**という状態を残さないため(監査指摘)。
    """
    snap, patterns = snapshot
    for rel, data in snap.items():
        p = ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    for pat in patterns:
        for p in sorted(ROOT.glob(pat)):
            rel = str(p.relative_to(ROOT))
            if p.is_file() and rel not in snap and rel not in NOT_ASSEMBLY:
                p.unlink()


def drop_blocked_articles(date: str, review: dict, written: list[dict]) -> tuple[list[str], list[str]]:
    """校閲が最後まで下ろさなかったブロック指摘の記事を、紙面から落とす。

    返り値は (落とした slug, 落とせなかったブロックの説明)。

    往復の上限まで直しきれなかったとき、以前は**そのまま発行していた**。
    実測で10号中3号が未 approve のまま出ており、2026-08-27号は
    「出典にない事実」という指摘を残したまま配信された。守るべきものの筆頭を
    素通りさせていたことになる。

    かといって号ごと止めるのは規程に反する(「発行を止めるくらいなら薄い紙面を出す」)。
    問題のある記事だけを落として、残りで出す。

    **一面が指摘されている場合は落とさない。**号スナップショットの lead_slug と
    digest が一面を指しているため、機械で抜くと組版と食い違う。
    そこは人が見るべきなので、落とせなかったものとして返す。
    """
    dropped, unresolved = [], []
    by_slug = {a["slug"]: a for a in written}
    for b in review.get("blockers") or []:
        f = (b.get("file") or "").strip()
        issue = (b.get("issue") or "")[:120]
        name = Path(f).name
        if not f.startswith("docs/_posts/") or not name.startswith(date + "-"):
            unresolved.append(f"{f or '(対象不明)'}: {issue}")
            continue
        slug = name[len(date) + 1:].removesuffix(".md")
        if by_slug.get(slug, {}).get("rank") == "lead":
            unresolved.append(f"{f}(一面): {issue}")
            continue
        p = ROOT / "docs" / "_posts" / name
        if p.exists():
            p.unlink()
        if slug not in dropped:
            dropped.append(slug)
    written[:] = [a for a in written if a["slug"] not in dropped]
    return dropped, unresolved


def assign_ranks(date: str, plan: dict, written: list[dict], keep_lead: bool = False) -> dict:
    """**書き上がった長さと話題の大きさから枠(rank)を当てる。**

    枠を先に決めて字数を合わせさせると、「large なのに medium 2本ぶんしかない記事」
    が出る(2026-08-27号の実測: large の中央値 527字)。順序が逆で、
    水増しを誘発するだけだった。書かせるときは字数を指定せず、
    書き上がりで枠を決める。「large なのに短い」は構造的に起きなくなる。

    - 枠の大小は**長さ**で決める(RANK_BY_LENGTH)
    - **インパクト**(面別選定が申告した lead_score)は一面の選出に使う。
      長さで上位帯に入った記事のうち、最も大きい話題を lead にする
    - roundup / culture は大小ではなく面の種類なので、長さでは動かさない
    """
    from lint import BODY_RANGE
    by_slug = {a["slug"]: a for a in plan["articles"]}
    sized = []
    for art in written:
        p = ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md"
        if not p.exists() or art.get("rank") in ("roundup", "culture"):
            continue
        n = body_length(p)
        rank = "small"
        for r, lo in RANK_BY_LENGTH:
            if n >= lo:
                rank = r
                break
        sized.append((art, n, rank))
    # 一面は**インパクトで決める**(長さでは決めない)。
    # その日いちばん大きい話題が、たまたま出典の情報量が少なくて短くなることはある
    # (実測: デレミリ合同ライブは公式ページを拾い切っても 973字)。
    # そこで枠を落とすのは本末転倒なので、lead_score が最大の記事を一面にする。
    # 一面が既に決まっているなら、それを尊重する。
    # 一面は pick_lead(号全体を見る専任セッション)が決める。ここで lead_score 最大を
    # 選び直すと二重の選定になり、組版済みの lead_slug と食い違う(実測で発生)
    if not keep_lead and any(a.get("rank") == "lead" for a, _, _ in sized):
        keep_lead = True
    if keep_lead:
        # 既に一面が決まっている(組版済み)。長さで lead 相当になったものは large へ落とす
        sized = [(a, n, a["rank"] if a.get("rank") == "lead" else ("large" if r == "lead" else r))
                 for a, n, r in sized]
    elif sized:
        lead = max(sized, key=lambda x: (by_slug.get(x[0]["slug"], {}).get("lead_score") or 0, x[1]))
        sized = [(a, n, "lead" if a is lead[0] else ("large" if r == "lead" else r))
                 for a, n, r in sized]
    stats = collections.Counter()
    for art, n, rank in sized:
        stats[rank] += 1
        if art.get("rank") != rank:
            art["rank"] = rank
            by_slug.get(art["slug"], {})["rank"] = rank
        p = ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md"
        txt = p.read_text(encoding="utf-8")
        p.write_text(re.sub(r"^rank: .*$", f"rank: {rank}", txt, count=1, flags=re.MULTILINE),
                     encoding="utf-8")
    lens = sorted(n for _, n, _ in sized)
    if lens:
        print(f"枠割り: {dict(stats)} / 本文 中央値{lens[len(lens)//2]}字 "
              f"最短{lens[0]}字 最長{lens[-1]}字", flush=True)
    return plan


def write_articles(date: str, plan: dict, cands: dict, triggers: list[dict],
                   stories: dict, wave: int = 0,
                   reuse: bool = False) -> tuple[list[dict], list[str]]:
    """記事ごとに素材を機械的に切り出して個別 codex セッションで執筆(wave 並列)。

    wave=0 なら COMPOSE_WAVE(.env)を使う。
    校閲(claude)とベンダーを分離するため執筆は Codex。機械検収エラーの修正は
    校閲と同じ Claude(REVIEW_MODEL=haiku)で行う。

    reuse=True(--reuse-plan)のときは、既に書けている記事を再執筆しない。
    執筆層だけを直して落ちた記事を拾い直す、という再試行を安く保つため。
    """
    wave = wave or COMPOSE_WAVE
    trig_by_key = {t["dedup_key"]: t for t in triggers}
    jobs, written_before = [], []
    for art in plan["articles"]:
        if reuse and (ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md").exists():
            written_before.append(art)
            continue
        materials = [cands[cid] for cid in art["candidate_ids"]]
        # src も**申告ではなく判定表**から出す。素材の source_type には収集時点の
        # 古い申告が残っており、そのまま渡すと執筆が写して lint が毎回赤くなる
        src = weakest_src(classify_source(c.get("url", "") or "") for c in materials)
        dks = {c.get("dedup_key") for c in materials} | {art.get("dedup_key")}
        facts = [f for dk in dks if dk in stories for f in stories[dk]]
        jobs.append((art, src, article_prompt(date, art, materials, facts,
                                              trig_by_key.get(art.get("dedup_key")), src)))
    written, aborted = list(written_before), []
    if written_before:
        print(f"既存の記事 {len(written_before)}本は再執筆しない(--reuse-plan)", flush=True)
    for i in range(0, len(jobs), wave):
        procs = []
        for art, src, prompt in jobs[i:i + wave]:
            fd, out_name = tempfile.mkstemp(prefix=f"codexwrite-{art['slug']}-", suffix=".txt")
            os.close(fd)
            out_path = Path(out_name)
            procs.append((art, src, out_path, subprocess.Popen(
                ["codex", "exec", "-m", CODEX_WRITE_MODEL, "-s", "workspace-write",
                 # 既定では sandbox が通信を遮断する。これが無いと出典照合が
                 # 実行できず、収集段階の誤りがそのまま紙面に出る
                 "-c", "sandbox_workspace_write.network_access=true",
                 "--output-last-message", str(out_path), prompt],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
                stdin=subprocess.DEVNULL, cwd=ROOT)))
        for art, src, out_path, p in procs:
            try:
                p.communicate(timeout=900)
            except subprocess.TimeoutExpired:
                p.kill()
            out = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
            out_path.unlink(missing_ok=True)
            if "ABORT:" in out[-2000:] and not (ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md").exists():
                reason = out.rsplit("ABORT:", 1)[-1].strip()[:200]
                print(f"記事 {art['slug']} は出典照合で不成立: {reason}", flush=True)
                aborted.append(art["slug"])
                continue
            errs = validate_article_file(date, art, cands)
            if errs:
                # 検収エラーは同一素材で1回だけ書き直させる(検品=Claude/REVIEW_MODEL)
                fixp = (f"docs/_posts/{date}-{art['slug']}.md の機械検収エラーを修正してください(Edit ツール使用):\n- "
                        + "\n- ".join(errs))
                subprocess.run(["claude", "-p", fixp, "--model", REVIEW_MODEL, "--dangerously-skip-permissions",
                               "--max-budget-usd", COMPOSE_ARTICLE_MAX_BUDGET_USD],
                               capture_output=True, text=True, timeout=600,
                               stdin=subprocess.DEVNULL, cwd=ROOT)
                errs = validate_article_file(date, art, cands)
            if errs:
                print(f"記事 {art['slug']} 検収不合格: {errs}", flush=True)
                aborted.append(art["slug"])
                bad = ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md"
                if bad.exists():
                    bad.unlink()
            else:
                written.append(art)
    return written, aborted


def load_story_facts() -> dict:
    p = ROOT / "stock" / "stories.yml"
    if not p.exists():
        return {}
    out = {}
    for e in yaml.safe_load(p.read_text(encoding="utf-8")) or []:
        out[e.get("story_id")] = e.get("published_facts", [])
    return out


def run_lint(date: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "lint.py"), "--base", "origin/main"],
                       capture_output=True, text=True, cwd=ROOT)
    return r.returncode, r.stdout[-1500:]


def run_parallel(jobs: list[tuple[str, callable]], timeout: int = 2500) -> list[str]:
    """独立した工程を同時に走らせる。戻り値は失敗した工程の説明。

    社説・組版・記事修正は互いの成果物に触らないので、直列にする理由が無い。
    スレッドの中で起きた例外は**握り潰さず**、呼び出し側へ名前付きで返す
    (以前は起動しっぱなしで、失敗しても成功したように見えていた=監査指摘)。
    """
    errs: dict[str, str] = {}

    def wrap(name, fn):
        def run():
            try:
                fn()
            except Exception as e:
                traceback.print_exc()
                errs[name] = f"{type(e).__name__}: {e}"
        return run

    ths = [threading.Thread(target=wrap(n, f), daemon=True) for n, f in jobs]
    for t in ths:
        t.start()
    for (n, _), t in zip(jobs, ths):
        t.join(timeout=timeout)
        if t.is_alive():
            errs[n] = "時間切れ(スレッドが終わっていない)"
    return [f"{n}: {why}" for n, why in errs.items()]


def fix_articles(date: str, by_file: dict[str, list[dict]]) -> None:
    """校閲のブロック指摘を、**記事1本につき1セッション**で直させる(並列)。

    以前は全部の指摘を1つのセッションに渡していた。指摘は記事ごとに独立して
    いるので、まとめる理由が無く、直列に直すぶんだけ時間になっていた。
    """
    jobs = [(f, ("校閲AIから以下のブロック指摘がありました。**"+f+" だけ**を、"
                 "candidates の facts と照合して修正してください。他のファイルには触らないこと。\n"
                 "**`docs/_editorials/` には触らないこと。**社説は別のセッションが直します。\n"
                 "修正できない(出典に無い事実で、消すしかない)なら、その記述を削ってください。\n"
                 + json.dumps(bs, ensure_ascii=False, indent=2)))
            for f, bs in by_file.items() if f.startswith("docs/_posts/")]
    if not jobs:
        return
    for i in range(0, len(jobs), COMPOSE_WAVE):
        procs = []
        for f, prompt in jobs[i:i + COMPOSE_WAVE]:
            procs.append(subprocess.Popen(
                ["claude", "-p", prompt, "--model", REVIEW_MODEL,
                 "--dangerously-skip-permissions",
                 "--max-budget-usd", COMPOSE_ARTICLE_MAX_BUDGET_USD],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
                stdin=subprocess.DEVNULL, cwd=ROOT))
        for p in procs:
            try:
                p.communicate(timeout=900)
            except subprocess.TimeoutExpired:
                p.kill()
    print(f"校閲の指摘で {len(jobs)}本を並列で修正した", flush=True)


def _parse_review(text: str, err: str, where: str) -> dict:
    """校閲セッションの出力を JSON にする。読めなければブロック扱いにする。"""
    for cand in (text.strip(), ):
        try:
            return json.loads(cand)
        except Exception:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {"verdict": "block",
            "blockers": [{"file": where, "issue": f"校閲実行失敗: {err[-200:]}", "quote": ""}],
            "comments": []}


def claude_review(date: str, round_no: int, targets: list[str] | None = None,
                  editorial: bool = True, paper: bool = True,
                  carry: dict | None = None) -> dict:
    """校閲。執筆(Codex)と別ベンダーにするため Claude(REVIEW_MODEL=haiku)で実施。

    **記事は1本ずつ、並列で見る。**紙面まるごとを1セッションで校閲していたが、
    そうすると1巡の値段が紙面の大きさで決まってしまい、実測21分かかった。
    記事を4本落としただけでも34本を読み直させるので、往復のたびに21分が乗る。
    執筆はとうに並列化してあるのに、校閲だけが紙面単位のまま残っていた。

    チェックリストの1〜16は**どれも記事1本の中で完結する**判定である。
    1本ずつに割れば、セッションは小さくなり、`COMPOSE_WAVE` 本ずつ同時に走る。

    `targets` に記事のファイル名を渡すと、**その記事だけ**を見直す。
    校閲の指摘で3本直したなら、見直すのはその3本でよい。
    `editorial` / `paper` は、社説・紙面全体(主題の重複、記事の漏れ)の担当。

    絞って見直すときは `carry` に前回の結果を渡すこと。**見直さなかったファイルに
    付いていた指摘を引き継ぐ。**引き継がないと、直していない記事のブロックが
    消えて approve になる。
    """
    schema = (ROOT / "prompts" / "review-schema.json").read_text(encoding="utf-8")
    names = targets if targets is not None else sorted(
        p.name for p in (ROOT / "docs" / "_posts").glob(f"{date}-*.md"))
    again = (f"\n\nこれは再校閲({round_no}回目)です。前回の指摘への修正が反映されています。"
             if round_no > 1 else "")

    # jobs = (説明, プロンプト, 既定の file, scope)
    #
    # `scope` は**機械が付ける担当の名前**で、モデルが書く `file` とは別に持つ。
    # file だけで引き継ぎを判断すると、紙面担当が「記事Aと記事Bが重複」を
    # file=記事A で返したとき、記事Aを直して見直しただけで紙面担当の指摘が
    # 消える(監査指摘)。担当が走り直すまで、その担当の指摘は残す
    jobs = []
    art_ck = (ROOT / "prompts" / "review-article.md").read_text(encoding="utf-8")
    for n in names:
        jobs.append((n, art_ck.replace("{DATE}", date).replace("{FILE}", n) + again,
                     f"docs/_posts/{n}", f"article:{n}"))
    if editorial and (ROOT / "docs" / "_editorials" / f"{date}.md").exists():
        jobs.append(("社説",
                     (ROOT / "prompts" / "review-editorial.md").read_text(encoding="utf-8")
                     .replace("{DATE}", date) + again, f"docs/_editorials/{date}.md", "editorial"))
    if paper:
        jobs.append(("紙面全体",
                     (ROOT / "prompts" / "review-paper.md").read_text(encoding="utf-8")
                     .replace("{DATE}", date) + again, "-", "paper"))

    t_review = time.time()
    merged = {"verdict": "approve", "blockers": [], "comments": []}
    # 今回走らせた担当。この担当の指摘だけが差し替わり、他は前回のを引き継ぐ
    seen = {sc for _, _, _, sc in jobs}
    if carry:
        for key in ("blockers", "comments"):
            merged[key] += [x for x in (carry.get(key) or [])
                            if x.get("scope") not in seen
                            # 消えた記事の指摘は持ち越さない
                            and (not str(x.get("scope") or "").startswith("article:")
                                 or (ROOT / "docs" / "_posts"
                                     / str(x["scope"]).split(":", 1)[1]).exists())]
    for i in range(0, len(jobs), COMPOSE_WAVE):
        procs = []
        for what, prompt, where, scope in jobs[i:i + COMPOSE_WAVE]:
            procs.append((scope, where, subprocess.Popen(
                ["claude", "-p", prompt, "--model", REVIEW_MODEL,
                 "--json-schema", schema, "--dangerously-skip-permissions",
                 # 1本ずつなので上限も1本分でよい(紙面まるごとの額を配ると
                 # 並列数ぶんの掛け算になる)
                 "--max-budget-usd", COMPOSE_ARTICLE_MAX_BUDGET_USD],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                stdin=subprocess.DEVNULL, cwd=ROOT)))
        for scope, where, p in procs:
            try:
                so, se = p.communicate(timeout=900)
            except subprocess.TimeoutExpired:
                p.kill()
                so, se = "", "時間切れ"
            r = _parse_review(so or "", se or "", where)
            for key in ("blockers", "comments"):
                for x in r.get(key) or []:
                    x["scope"] = scope
                    # 記事担当・社説担当は見た相手が決まっている。モデルが別の
                    # ファイル名を書いてきても、担当のファイルへ寄せる
                    if scope != "paper":
                        x["file"] = where
                    else:
                        x.setdefault("file", where)
                    merged[key].append(x)
    if merged["blockers"]:
        merged["verdict"] = "block"
    # 次の巡を始めてよいかの判断に使う。見込みではなく**この号の実測**で決める
    STAGE_MIN["校閲"] = (time.time() - t_review) / 60
    print(f"校閲{round_no}巡目: {len(jobs)}件を並列で見て "
          f"ブロック{len(merged['blockers'])}件 / コメント{len(merged['comments'])}件 "
          f"({STAGE_MIN['校閲']:.0f}分)", flush=True)
    (ROOT / "metrics" / f"review-{date}-{round_no}.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    return merged


def self_check() -> list[str]:
    """main() が踏むべき工程を実際に踏んでいるかを、起動時に自分で確かめる。

    定義したのに呼ぶ行を落とす、という事故を3回起こしている(assign_ranks が
    呼ばれず rank が付かないまま release が停止、write_articles の引数不一致で
    compose が停止)。静的検査(scripts/selfcheck.py)では「呼び出しの欠落」を
    拾えないので、ここで main() のソースを見て必須工程の呼び出しを確認する。
    """
    import inspect
    src = inspect.getsource(main)
    need = {
        "run_plan(": "選定(面別)",
        "pick_lead(": "一面の選出",
        "assign_ranks(": "枠の割り当て(規程9)",
        "write_articles(": "記事の執筆",
        "claude_review(": "校閲",
        "commit_and_push(": "コミットと push",
    }
    return [f"main() が {name} を呼んでいない({why})"
            for name, why in need.items() if name not in src]


def main() -> int:
    missing = self_check()
    if missing:
        notify("compose", "工程の欠落を検出したため起動を中止:\n- " + "\n- ".join(missing), ok=False)
        return 1
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", metavar="BRAND", nargs="?", const="cg",
                    help="指定した面の選定プロンプトを表示して終了(実行しない)")
    ap.add_argument("--date", default=None, help="対象発行日(再試行用。既定: 次の06:00の日付)")
    ap.add_argument("--max-rounds", type=int, default=2)
    ap.add_argument("--reuse-plan", action="store_true",
                    help="既存の metrics/plan-<date>.json を使い、選定をやり直さない"
                         "(執筆層の修正だけを試すときに使う。既に書けている記事も再執筆しない)")
    args = ap.parse_args()
    t0 = time.time()
    date = args.date or edition_date()
    branch = f"edition/{date}"
    triggers = None

    if not args.plan and not checkout_edition_branch(date, "compose"):
        return 1

    # **殺されたら、その時点までを確定してから死ぬ。**
    # systemd は起動タイムアウトで SIGTERM を送る。以前はここで何も残らず、
    # 記事40本・社説・号スナップショットが未コミットのまま作業ツリーに散らばり、
    # release も collect も「未コミットの変更がある」で止まった(2026-08-31)。
    # 途中の紙面でも、コミットさえされていれば release が approve を見て判断でき、
    # 人も何が出来ていたか分かる。
    if not args.plan:
        def _on_term(signum, frame):
            print(f"SIGTERM を受けた。ここまでを確定して終了する", flush=True)
            try:
                commit_and_push(branch, f"compose {date}: 途中で打ち切られた(SIGTERM)", "compose")
            except Exception as e:
                print(f"打ち切り時のコミットに失敗: {e}", flush=True)
            notify("compose", f"{date}: 時間切れで打ち切られた。**ここまでの紙面はコミット済み**。"
                              f"lint と校閲記録を見て、発行できるか判断すること", ok=False)
            os._exit(1)

        signal.signal(signal.SIGTERM, _on_term)
    triggers = load_scheduled(date)
    number = next_number()
    if args.plan:
        print(brand_plan_prompt(date, args.plan, 0,
                                [t for t in triggers if t.get("brand") == args.plan]))
        return 0

    if (ROOT / "docs" / "_editions" / f"{date}.md").exists():
        notify("compose", f"{date}: 号スナップショットが既に存在(compose 済み?)。中止")
        return 0

    # 締切ガード(規程: 締切=04:00時点の candidates)。スイープ未実施なら警告して続行
    latest = None
    # 日付名ファイルのみ対象(review-*.json が辞書順で後ろに並び、誤検知するため)
    for md in sorted((ROOT / "metrics").glob("????-??-??.json"))[-2:]:
        try:
            for run in json.loads(md.read_text(encoding="utf-8")).get("collect", []):
                latest = max(latest or run["at"], run["at"])
        except Exception:
            pass
    stale = (latest is None or
             (now_jst() - datetime.datetime.fromisoformat(latest)).total_seconds() > 7200)
    if stale:
        notify("compose", f"{date}: 直近2時間の collect 実行記録が無い(締切前スイープ未実施?)。手持ちの candidates で続行", ok=False)

    # 1a. 選定: 記事計画の生成と機械検証(1回だけ再計画を許す)
    cands = load_window_candidates(date)
    blocklist = load_blocklist()
    if not cands:
        notify("compose", f"{date}: 発行日±1日の candidates が空。compose 続行不能", ok=False)
        return 1
    plan_path = ROOT / "metrics" / f"plan-{date}.json"
    by_brand, n_subjects = write_plan_index(date, cands, blocklist)
    print(f"選定インデックス: {n_subjects}主題 / {len(by_brand)}面 "
          f"({', '.join(f'{b}:{len(v)}' for b, v in sorted(by_brand.items()))})", flush=True)
    plan = run_plan(date, by_brand, triggers)
    pick_lead(date, plan)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    # **素材を先に確定させてから、紙面の形を直す。**
    # 逆にすると、形を直したあとで素材が減って形が崩れる(監査指摘):
    #   roundup 3件のうち1件が verify=failed → 除去後2件 → もう直されず検証で停止
    #   一面の素材が全滅 → 記事ごと除去 → 一面0本 → 停止
    #   roundup が複数のとき、使えない素材まで数えて残す1本を選んでしまう
    #
    # 1. 壊れた候補IDを直させる → 2. それでも駄目な記事を外す → 3. 形を整える
    repaired = repair_invalid_ids(date, plan, cands)
    if repaired:
        print("候補IDを修復: " + " / ".join(repaired), flush=True)
    plan_dropped = drop_invalid_articles(plan, cands, blocklist)
    if plan_dropped:
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        notify("compose", f"{date}: 素材の参照が壊れた記事を計画から外した"
                          f"({len(plan_dropped)}件)。発行は続行:\n- " + "\n- ".join(plan_dropped[:5]), ok=False)
    # 素材が確定したので、ここで紙面の形を整える(roundup の素材不足・重複、一面)
    shape = repair_plan_shape(plan)
    if shape:
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print("計画の形を修復: " + " / ".join(shape), flush=True)
    # errors = 発行を止める機械検証 / gaps = 止めない取りこぼし(通知して続行)
    errors = validate_plan(plan, cands, blocklist)
    gaps, cov = coverage_gaps(plan, cands, blocklist)
    if gaps:
        print(f"取りこぼし: {gaps[0][:120]}", flush=True)
    # 素材に対して rank が小さい記事の可視化(規程9)。発行は止めない。
    # 束ねること自体は正しい(同じ系統の話を分けると全部が薄くなる)。問題は
    # 束ねたまま rank を上げないことで、書ける上限に収まらず中身が落ちる
    # (2026-08-27号の 765 面は14件を large 1本に収めていた)
    thin = []
    for a in plan["articles"]:
        if a["rank"] in ("roundup", "culture"):
            continue
        n = len(a["candidate_ids"])
        for need, ok in RANK_FLOOR_BY_MATERIALS:
            if n >= need and a["rank"] not in ok:
                thin.append(f"{a['slug']}({n}件/{a['rank']})")
                break
    if thin:
        print("素材に対して rank が小さい: " + ", ".join(thin), flush=True)
    if errors:
        notify("compose", f"{date}: 記事計画が機械検証を通らず。人間判断が必要\n- " + "\n- ".join(errors[:8]), ok=False)
        commit_and_push(branch, f"compose {date}: 計画不成立(要人間判断)", "compose")
        return 1
    arts_plan = plan["articles"]
    n_plan = len(arts_plan)
    n_rup = sum(1 for a in arts_plan if a["rank"] == "roundup")
    n_drop = len(plan.get("dropped") or [])
    print(f"計画: {n_plan}本(うち roundup {n_rup}) / 不採用 {n_drop}件 / "
          f"主題カバー {cov.get('covered', 0)}/{cov.get('subjects', 0)}"
          f"{' ★取りこぼし' + str(cov['missing']) if cov.get('missing') else ''} "
          f"(lead: {[a['slug'] for a in arts_plan if a['rank']=='lead']})", flush=True)
    if cov.get("missing"):
        notify("compose", f"{date}: 素材{cov['subjects']}主題のうち{cov['missing']}主題が"
                          "計画で判断されず消えた(発行は続行。選定の取りこぼし)", ok=False)

    # 1b. 個別執筆: 記事ごとに素材を機械切り出しして独立セッションで書く
    written, aborted = write_articles(date, plan, cands, triggers, load_story_facts(),
                                      reuse=args.reuse_plan)
    print(f"執筆: {len(written)}/{n_plan}本(不成立 {len(aborted)}: {aborted})", flush=True)
    # **書き上がりから枠を当てる**(字数を枠に合わせさせない。規程9)。
    # 組版より前に確定させる: 号スナップショットの lead_slug が一面に依存するため
    assign_ranks(date, plan, written)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if len(written) < 1 or (aborted and len(written) < 8):
        notify("compose", f"{date}: 執筆成立 {len(written)}本/計画 {n_plan}本(不成立: {aborted})。下限割れの疑い", ok=False)
    if not written:
        commit_and_push(branch, f"compose {date}: 執筆全滅(要人間判断)", "compose")
        return 1

    # 1c. 社説: 専任セッション(組版から分離。人格・文体に集中させる)。
    #     執筆は Codex(EDITORIAL_MODEL)。校閲が Claude なので、社説も記事と同じく
    #     執筆と校閲が別ベンダーになる(要件4.5)。Claude で書くと社説だけ同一ベンダーの
    #     自己校閲になってしまうため。
    # 起点にする記事が**実際に書き上がっているか**を確かめる。執筆は不成立になることが
    # あり(aborted)、その記事を起点に指したままだと社説が存在しない記事から書き出す
    ed_slug = plan.get("editorial_slug", "")
    ed_brand = plan.get("editorial_brand", "")
    alive = {a["slug"]: a for a in written}
    if ed_slug not in alive:
        lead_art = next((a for a in written if a.get("rank") == "lead"), None)
        fallback = lead_art or (written[0] if written else None)
        print(f"社説の起点 {ed_slug or '(未指定)'} が紙面に無い。"
              f"{(fallback or {}).get('slug', '-')} に差し替える", flush=True)
        ed_slug = (fallback or {}).get("slug", "")
        ed_brand = (fallback or {}).get("brand", "")
        plan["editorial_slug"], plan["editorial_brand"] = ed_slug, ed_brand
        # 記録にも残す。ここを書き戻さないと、計画上の起点と実際の起点が食い違う
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    ed_path = ROOT / "docs" / "_editorials" / f"{date}.md"

    # **社説と組版を同時に走らせる。**片方は社説を書き、片方は号スナップショットと
    # 台帳を作る。互いの成果物には触らない(組版プロンプトは社説を書き換えない)ので、
    # 直列にする理由が無かった。実測で合わせて23分かかっていた工程が、
    # 長いほうの時間で済む。
    #
    # **社説が記事を読んだ時点**を基準にする。この後の lint 修正や校閲往復で記事が
    # 直ると、社説は直る前の事実を引用したままになる(実測: 11号中8号で1往復目に
    # 記事のブロックが出ており、受注期限・対象人数・人物関係が直っていた)。
    # 同時に走らせるので、**両方を始める前に**控える
    posts_at_editorial = posts_fingerprint(date)
    # 組版の前の台帳を控えておく。校閲ブロックで記事を落としたとき、組版をやり直さないと
    # digest が消えた記事を指したまま残り、既報台帳には「発行した」と書かれたままになる
    # (監査指摘)。台帳は**追記**なので、やり直す前にここまで巻き戻す必要がある。
    # 記事と号スナップショットも控える。やり直しが途中で失敗したときに、
    # 記事だけ消えて digest が古いまま、という状態を残さないため
    pre_assembly = snapshot_files([f"docs/_posts/{date}-*.md", f"docs/_editions/{date}.md",
                                   f"docs/_editorials/{date}.md", "stock/**/*", "stock/*"])

    ed_before = ed_path.read_bytes() if ed_path.exists() else None

    def write_editorial_job():
        for _ in range(2):  # 書けなければ同一プロンプトでもう一度だけ
            codex_run(editorial_prompt(date, number, ed_slug, ed_brand),
                      timeout=1200, model=EDITORIAL_MODEL)
            if ed_path.exists() and ed_path.read_bytes() != ed_before:
                return
        raise RuntimeError("社説が2回とも書かれなかった")

    def assemble_job():
        print(claude_run(assembly_prompt(date, number, aborted))[-1000:], flush=True)

    errs = run_parallel([("社説", write_editorial_job), ("組版", assemble_job)])
    for e in errs:
        print(f"同時実行のうち失敗: {e}", flush=True)

    # 社説はこの控えより後に出来るので、控えに入れておく。入れないと、
    # 取り直しが失敗して巻き戻したときに「控えに無いファイル」として消される
    if ed_path.exists():
        pre_assembly[0][f"docs/_editorials/{date}.md"] = ed_path.read_bytes()
    if not ed_path.exists():
        # 以前は「組版セッションの lint 修正に委ねる」としていたが、その組版にも
        # lint 修正にも「社説には触るな」と指示しているので、誰も書かずに止まる経路だった。
        # 社説を書けるのは執筆側だけなので、ここで打ち切って人へ渡す
        notify("compose", f"{date}: 社説が2回とも未作成。**社説を書けるのは執筆側セッションだけ**なので、"
                          f"このまま進めても紙面に社説が載らない。人の判断が要る", ok=False)

    # 2. 機械算出の確定 + lint ゲート
    subprocess.run([sys.executable, str(ROOT / "scripts" / "derive.py"), "--date", date, "--write"],
                   cwd=ROOT, capture_output=True, text=True)
    code, lint_out = run_lint(date)
    print(lint_out, flush=True)
    if code != 0:
        # 社説の lint エラーは**執筆側へ返す。**ここで Claude に「docs/ を直せ」と言うと、
        # 校閲側モデルが社説を書き直せてしまい、ベンダー分離(要件4.5)が破れる
        # (実測: 2026-08-28号の社説が校閲側に書き換えられていた)
        ed_errs = [l for l in lint_out.splitlines()
                   if l.startswith("::error") and "docs/_editorials/" in l]
        if ed_errs:
            rewrite_editorial(date, number, plan.get("editorial_slug", ""),
                              plan.get("editorial_brand", ""),
                              [{"file": f"docs/_editorials/{date}.md",
                                "issue": "lint: " + e.split("::", 2)[-1], "quote": ""} for e in ed_errs])
            code, lint_out = run_lint(date)
    if code != 0:
        # 一度だけ Claude(検品=REVIEW_MODEL)に lint 修正を依頼
        claude_run(f"アイマスNEWS {date}号の lint がエラーです。`python3 scripts/lint.py --base origin/main` を実行し、"
                   f"エラー0になるまで docs/ と stock/ を修正してください。修正後 derive.py --date {date} --write も再実行すること。\n"
                   f"**`docs/_editorials/` には触らないこと。**社説は執筆側のセッションが直します。"
                   f"社説の lint エラーが残っている場合は、直さずそのまま報告してください。",
                   model=REVIEW_MODEL)
        code, lint_out = run_lint(date)
        if code != 0:
            notify("compose", f"{date}: lint 赤が解消できず。人間判断が必要\n{lint_out[-500:]}", ok=False)
            commit_and_push(branch, f"compose {date}: lint未解消(要人間判断)", "compose")
            return 1

    # **ここで一度コミットする。**以前はコミットが最後の1回しか無く、
    # そのあとの工程で落ちると号が丸ごと消えた(2026-08-31: systemd の起動
    # タイムアウトで殺され、記事40本・社説・号スナップショットが未コミットのまま残り、
    # release も collect も「作業ツリーが汚れている」で止まった)。
    # 校閲前の紙面でも、release は approve を見て発行を止めるので出てしまうことはない。
    # 途中で死んでも、人が lint と校閲記録を見て確定できる状態を残す
    commit_and_push(branch, f"compose {date}: 紙面生成(校閲前・lint green)", "compose")

    # 3. 校閲往復
    rounds = 0
    review = None
    # 2巡目以降に見直すのは**直した記事だけ**。校閲は1本ずつ独立に見るので、
    # 直していない記事の判定は変わらない。紙面全体(主題の重複・記事の漏れ)は
    # 記事が増減したときだけ見ればよいので、往復では見ない
    #
    # **見直す先は「前の巡でブロックが付いたファイル」に一致する。**ブロックが
    # 付いたものは必ず直しに行くので、直さなかったファイルの判定は前巡のまま
    # 有効である。だから合議の verdict は、絞って見直しても紙面全体の答えになる
    retarget: list[str] | None = None
    retarget_ed = True
    for rounds in range(1, args.max_rounds + 2):
        # 紙面担当(主題の重複・記事の漏れ)は、**記事を直したら走らせる**。
        # 見出しや主題が変われば、別の記事との重複が新しく生まれうる(監査指摘)。
        # 見出しだけ読む担当なので1セッションで済む
        review = claude_review(date, rounds, targets=retarget, editorial=retarget_ed,
                               paper=(retarget is None or bool(retarget)), carry=review)
        if review.get("verdict") == "approve":
            break
        if rounds > args.max_rounds:
            break
        # 直す時間が無いなら、直しかけで時間切れになるより、いまの紙面を確定させる。
        # 見直しの前に「社説の書き直し+記事の修正(並列)」が入るので、その分も積む
        if not afford(t0, "校閲", 6, "校閲の往復", extra=9):
            break
        # **社説への指摘は執筆側(Codex)へ返す。**校閲は Claude、社説の執筆は Codex という
        # 別ベンダー分離(要件4.5)が、修正セッションで壊れていた。実測: 2026-08-28号の
        # 校閲記録が引用した社説の一文が、現在の社説に存在しない。校閲側が書き直している。
        # 社説を校閲側に直させると、書いた本人が自分を検品するのと同じ形になる
        # **どの担当が出した指摘かで振り分ける。**file で振り分けると、
        # 紙面担当の「記事Aと記事Bが重複」が記事Aの指摘に化ける(監査指摘)
        blockers = review.get("blockers", [])
        ed_blockers = [b for b in blockers if b.get("scope") == "editorial"]
        art_blockers = [b for b in blockers if str(b.get("scope") or "").startswith("article:")]
        paper_blockers = [b for b in blockers if b.get("scope") == "paper"]
        retarget_ed = bool(ed_blockers)

        by_file: dict[str, list[dict]] = {}
        for b in art_blockers + paper_blockers:
            f = b.get("file") or "-"
            if f.startswith("docs/_posts/"):
                by_file.setdefault(f, []).append(b)
        retarget = sorted(f.rsplit("/", 1)[-1] for f in by_file)
        if not (ed_blockers or by_file):
            continue
        # 社説の書き直しと記事の修正は互いに触らないので同時に走らせる。
        # 記事の修正も1本1セッションで並列(指摘は記事ごとに独立している)
        jobs = []
        if ed_blockers:
            jobs.append(("社説", lambda: rewrite_editorial(
                date, number, plan.get("editorial_slug", ""),
                plan.get("editorial_brand", ""), ed_blockers)))
        if by_file:
            jobs.append(("記事の修正", lambda: fix_articles(date, by_file)))
        for e in run_parallel(jobs):
            print(f"同時実行のうち失敗: {e}", flush=True)
        subprocess.run([sys.executable, str(ROOT / "scripts" / "derive.py"), "--date", date, "--write"],
                       cwd=ROOT, capture_output=True, text=True)

    # 往復しても下りなかったブロック指摘は、その記事を落とす。
    # 以前はそのまま発行しており、「出典にない事実」の指摘を残した号が実際に出ている
    # 記事が往復で直っていたら、社説は**直る前の記事**を読んで書かれている。
    # 直った記事で書き直し、もう一度校閲に掛ける。ここを飛ばすと、
    # 直された事実を引用したままの社説が紙面に出る
    now_fp = posts_fingerprint(date)
    # 社説も組版も、**その号の全記事を読んで**書かれる。社説プロンプトは
    # 「事実の出所は本日の全記事」と言い、組版は全記事から台帳と続報予約を作る。
    # だから「どの記事を参照したか」を絞り込む意味がない。参照先を推し当てようと
    # slug や見出しで照合していたが、社説は slug を書かず見出しも言い換えるので、
    # 取り直しがほとんど発火していなかった(監査指摘)。
    #
    # 絞り込みをやめても時間は増えない。**発火したら結局どちらも走る**からで、
    # 2026-08-31 に時間切れを起こしたのは「frontmatter を整えただけの記事を
    # 変化とみなしていた」ことのほうだった。それは指紋を用途で割って外してある。
    changed_read = posts_changed(posts_at_editorial, now_fp, 0)   # 読まれる中身
    changed_meta = posts_changed(posts_at_editorial, now_fp, 1)   # 組版が使う値
    changed_posts = sorted(set(changed_read) | set(changed_meta))
    touches_editorial = changed_read            # 社説は本文・見出し・リードを読む
    touches_digest = changed_posts              # 組版はそれに加えて台帳用の値も使う
    # **ここは時間で飛ばさない。**以前は「時間が無ければ取り直しを省く」に
    # していたが、それだと直る前の事実を引用した社説と、古い台帳のまま
    # approve が出て発行できてしまう(監査指摘)。整合を取る工程は削らず、
    # 削るのは「もう1巡の校閲」のほうにする(COMPOSE_RESERVE_MIN で確保してある)
    if changed_posts and review:
        try:
            print(f"記事が {len(changed_posts)}本変わった"
                  f"(中身 {len(changed_read)}本 / 組版が使う値 {len(changed_meta)}本)。取り直す",
                  flush=True)
            commit_and_push(branch, f"compose {date}: 校閲往復のあと(取り直し前)", "compose")
            ed_comments = [c for c in (review.get("comments") or [])
                           if (c.get("file") or "").startswith("docs/_editorials/")]
            jobs = []
            if touches_digest:
                # digest と台帳も直る前の記事から作られている。台帳は追記なので、
                # 組版前まで巻き戻してから作り直させる(除外のときと同じ手順)
                for rel in [r for r in pre_assembly[0] if r.startswith("stock/")]:
                    (ROOT / rel).write_bytes(pre_assembly[0][rel])
                jobs.append(("組版", lambda: print(
                    claude_run(assembly_prompt(date, number, aborted))[-400:], flush=True)))
            if touches_editorial:
                jobs.append(("社説", lambda: rewrite_editorial(
                    date, number, plan.get("editorial_slug", ""), plan.get("editorial_brand", ""),
                    [{"file": f"docs/_editorials/{date}.md",
                      "issue": "本日の記事が校閲で直りました("
                               + "、".join(n[len(date) + 1:].removesuffix(".md")
                                           for n in touches_editorial[:6])
                               + " ほか)。**直った記事を読み直し**、社説が引用している事実が"
                                 "いまも紙面にあるか確かめて書き直してください。"
                                 "問題がなければ、直す必要はありません", "quote": ""}],
                    must_change=False, comments=ed_comments)))
            errs = run_parallel(jobs)
            if errs:
                raise RuntimeError("；".join(errs))
            subprocess.run([sys.executable, str(ROOT / "scripts" / "derive.py"),
                            "--date", date, "--write"], cwd=ROOT, capture_output=True, text=True)
            rounds += 1
            # 記事は往復で見終わっている。作り直したのは社説と digest なので、
            # 見直すのは社説と紙面担当(digest・主題の重複)。記事は見直さない
            review = claude_review(date, rounds, targets=[], editorial=True, paper=True,
                                   carry=review)
        except Exception as e:
            traceback.print_exc()
            notify("compose", f"{date}: 記事修正後の取り直しに失敗({e})。"
                              f"社説と digest は修正前の記事を前提にしたままです", ok=False)

    # 校閲が最後まで下ろさなかったブロックは、その記事を落として取り直す。
    #
    # **これを繰り返す。**以前は1回で打ち切っていたため、落として取り直した校閲が
    # 別の記事に新しい指摘を出すと、そこで「機械で落とせなかった」と諦めていた
    # (2026-09-01: 2本を落として再校閲したら3本目の指摘が出て、発行が止まった)。
    # 校閲は毎回すべてを見直すので、1回で収束する保証がない。
    # 落とせるものが無くなるか approve になるまで回す。
    DROP_PASSES = 3
    dropped_by_review, unresolved = [], []
    for _pass in range(DROP_PASSES):
        if not review or review.get("verdict") == "approve":
            break
        # 1巡は「落とす+社説+組版+校閲」。始めたら最後まで通さないと、
        # 記事だけ消えて digest が古いままの紙面が残る
        # 1巡は「落とす+(社説∥組版)+校閲」。社説と組版は同時に走るので長いほうだけ積む
        if not afford(t0, "校閲", 6, f"ブロック記事の除外({_pass + 1}巡目)", extra=12):
            unresolved = [b for b in (review.get("blockers") or [])]
            break
        try:
            dropped, unresolved = drop_blocked_articles(date, review, written)
            if not dropped:
                break  # 機械で落とせる指摘が残っていない(一面・社説・対象不明)
            dropped_by_review += dropped
            print(f"校閲ブロックで {len(dropped)}本を落とす({_pass + 1}巡目): {dropped}", flush=True)
            commit_and_push(branch, f"compose {date}: ブロック記事を除外({_pass + 1}巡目)", "compose")

            # **記事が落ちたら社説を取り直す。**起点かどうかは関係ない。
            # 社説は全記事を読んで書くので、起点以外を引用していても古くなる
            if plan.get("editorial_slug") in dropped:
                nxt = (next((a for a in written if a.get("rank") == "lead"), None)
                       or (written[0] if written else None))
                plan["editorial_slug"] = (nxt or {}).get("slug", "")
                plan["editorial_brand"] = (nxt or {}).get("brand", "")
                plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
                print(f"社説の起点が落ちたので {plan['editorial_slug']} へ差し替える", flush=True)
                why = ("起点にした記事が校閲で紙面から外れました。その記事の話から始まる社説は"
                       "成立しません。残っている記事から書き直してください")
                must = True
            else:
                why = ("この社説を書いたあと、次の記事が校閲で紙面から外れました("
                       + "、".join(dropped[:6])
                       + ")。社説がその記事に触れているなら、書き直してください。"
                         "触れていなければ直す必要はありません")
                must = False
            # **組版をやり直す。**digest が落とした記事を指したままだと lint が落ちるし、
            # 既報台帳には「発行した」と残る。台帳は追記なので組版前まで巻き戻してから、
            # 生き残った記事だけで作り直させる。社説の書き直しとは互いに触らないので同時に走らせる
            for rel in [r for r in pre_assembly[0] if r.startswith("stock/")]:
                (ROOT / rel).write_bytes(pre_assembly[0][rel])
            assign_ranks(date, plan, written, keep_lead=True)
            errs = run_parallel([
                ("社説", lambda: rewrite_editorial(
                    date, number, plan.get("editorial_slug", ""), plan.get("editorial_brand", ""),
                    [{"file": f"docs/_editorials/{date}.md", "issue": why, "quote": ""}],
                    must_change=must)),
                ("組版", lambda: print(
                    claude_run(assembly_prompt(date, number, aborted))[-400:], flush=True)),
            ])
            if errs:
                raise RuntimeError("；".join(errs))
            subprocess.run([sys.executable, str(ROOT / "scripts" / "derive.py"),
                            "--date", date, "--write"], cwd=ROOT, capture_output=True, text=True)
            # 組版が落とした記事を書き戻していないか機械で確かめる
            ed_file = ROOT / "docs" / "_editions" / f"{date}.md"
            back = [s for s in dropped_by_review
                    if ed_file.exists() and s in ed_file.read_text(encoding="utf-8")]
            if back:
                raise RuntimeError(f"組版のやり直しが、落とした記事を号に戻した: {back}")

            rounds += 1
            # 残った記事は直していないので見直さない。落としたことで変わるのは
            # 社説と、紙面全体(主題の重複・記事の漏れ)だけ
            review = claude_review(date, rounds, targets=[], editorial=True, paper=True,
                                   carry=review)
        except Exception as e:
            # **途中で落ちたら全部戻す。**記事を消した後に組版のやり直しが失敗すると、
            # 「記事だけ欠けて digest は古いまま」という壊れた紙面が残る
            traceback.print_exc()
            restore_files(pre_assembly)
            subprocess.run([sys.executable, str(ROOT / "scripts" / "derive.py"),
                            "--date", date, "--write"], cwd=ROOT, capture_output=True, text=True)
            dropped_by_review = []
            unresolved = [f"ブロック記事の除外処理が失敗({e})。紙面は組版直後の状態へ戻した"]
            break
    if review and review.get("verdict") != "approve" and not unresolved:
        unresolved = [f"{b.get('file')}: {(b.get('issue') or '')[:120]}"
                      for b in review.get("blockers") or []]

    # 校閲の修正で本文の長さが変わるため、枠を当て直す。
    # **一面は動かさない**(インパクトで決めた枠であり、号スナップショットの
    # lead_slug が指しているため。ここで変えると組版と食い違う)
    assign_ranks(date, plan, written, keep_lead=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "derive.py"), "--date", date, "--write"],
                   cwd=ROOT, capture_output=True, text=True)

    approved = review and review.get("verdict") == "approve"
    code, lint_out = run_lint(date)
    ok = approved and code == 0
    append_metric("compose", {"edition": date, "rounds": rounds, "approved": bool(approved),
                              "lint_green": code == 0, "planned": n_plan, "written": len(written),
                              "roundups": n_rup, "dropped": n_drop, "coverage": cov,
                              "dropped_by_review": dropped_by_review,
                              "aborted": aborted, "duration_s": int(time.time() - t0)})
    commit_and_push(branch, f"compose {date}: 紙面生成(校閲{'approve' if approved else '未approve'}・{rounds}往復"
                            + (f"・ブロック{len(dropped_by_review)}本を除外" if dropped_by_review else "") + ")", "compose")
    if ok:
        extra = (f"。校閲が下ろさなかった {len(dropped_by_review)}本は紙面から外しました"
                 f"({'・'.join(dropped_by_review)})" if dropped_by_review else "")
        notify("compose", f"{date}号 準備完了(校閲{rounds}往復で approve){extra}。06:00 に発行されます")
        return 0
    reasons = []
    if not approved:
        reasons.append(f"校閲{rounds}往復でも未 approve。**機械で落とせなかった指摘**:\n- "
                       + "\n- ".join(unresolved[:5] or ["(内訳不明)"]))
    if code != 0:
        errs = [l for l in lint_out.splitlines() if l.startswith("::error")]
        reasons.append(f"lint エラー {len(errs)} 件:\n- " + "\n- ".join(
            e.split("::", 2)[-1] for e in errs[:5]))
    notify("compose", f"{date}号: 発行前に人間判断が必要。" + "\n".join(reasons), ok=False)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        notify_crash("compose", e)
        sys.exit(1)
