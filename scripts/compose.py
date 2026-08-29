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
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tags as tags_lib
from pipelib import (ENV, ROOT, CLAUDE_MODEL, CODEX_WRITE_MODEL, EDITORIAL_MODEL,
                     COMPOSE_ARTICLE_MAX_BUDGET_USD,
                     COMPOSE_WHOLE_MAX_BUDGET_USD, REVIEW_MODEL, append_metric,
                     checkout_edition_branch, commit_and_push, edition_date, git,
                     notify, notify_crash, now_jst)

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
SRC_ORDER = ["公式", "準公式", "当事者", "報道", "ファン", "もちより", "未確認"]


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
- この面の続報予約(**必ず記事化する**。id は素材スナップショットとして candidate_ids に使える):
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

## 出力
`metrics/plan-lead-{date}.json` に次の JSON を書く(Write ツール使用。これ以外のファイルは作らない):
{{"lead_slug": "一面にする記事の slug(上のリストから1つ)",
  "editorial_topic": "社説の主題(1文)"}}
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
- **先頭の「期間」ブロックは原文のラベルそのままなので、`facts` と食い違ったらそちらが正**。
  「入金期間」を「販売期間」と書き換えるような取り違えは、ここで必ず正す(規程15)
- **掲載日(公開日)と話題の年を本文で確認する**。掲載年がイベント年・発行年と食い違う、
  または話題自体がすでに終了した過去のもの(当日トリガーの続報を除く)なら、その素材は使わない
- `FETCH_FAILED` が返った url は照合不能として扱う(その url だけを根拠に断定しない)
**記事の存在理由になる中核の事実が確認できない場合、または話題が過年度・終了済みと判明した場合は、
ファイルを作らず「ABORT: 理由」とだけ出力して終了すること。**

## 出力
`docs/_posts/{date}-{art['slug']}.md` を Write ツールで作成(これ以外のファイルは作らない・読む必要もない):
- frontmatter は次の値を**そのまま**使う: slug: {art['slug']} / edition: {date} / brand: {art['brand']} / src: {src} / rank: {art['rank']}(**仮の値**。発行前に機械が付け直します) / corrected: false / corrections: [] / candidate_ids: {json.dumps(art['candidate_ids'])}
- title(全角換算〜28字)・lede(1文。字数指定なし。記事の中身を1文で言い切る)・tags(2〜4個。下記「タグ語彙」に従う)・sources(素材の url から。label は内容がわかる短い日本語、type は各候補の source_type)・event_date(素材にあれば)は自分で書く
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
- 内規の文言(「全記事に必須」等)を紙面に書かない
- 事実の伝聞元がファン発・未確認の場合は断定を避ける文体にする
"""


def editorial_prompt(date: str, number: int, editorial_topic: str) -> str:
    weekday = "月火水木金土日"[datetime.date.fromisoformat(date).weekday()]
    core = (ROOT / "prompts" / "columnist-core.md").read_text(encoding="utf-8")
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
- 自分がAIだと名乗ったり、AIであることを主題にしたりはしない(書いている本人にその自覚はなくていい。紙面の欄外名義が「AI疑似プロデューサー」であることは編集部の決めごとで、あなたが気にすることではない)

## 面白さの規程
- ユーモアは歓迎。ただし笑いの矛先は自分と状況に向ける。運営・公式・個人への批判、苦言、注文、皮肉は書かない(編集規程5)
- 紙面の要約をしない。社説は2度目のダイジェストではない。記事を3本以上並べて紹介しはじめたら失敗と思うこと
- 型は破ってよい: 一つの数字だけで書く、一つの固有名詞から連想で転がす、読者への手紙にする、など。ただしオチはつける
- **偶然の一致を意味ありげに扱わない**。番号が続いている・日付が同じ・名前が似ている、といった無関係な符合を柱にしない。
  書きながら「これは関係ないのだが」と断らなければ成立しない発想は、その時点で捨てて別の切り口を探すこと。
  心が動いた理由を自分の言葉で説明できるものだけを書く
- 主題は「{editorial_topic}」。ただしこれは編集会議のメモにすぎない。書き出してみて別の切り口が面白ければ、紙面の事実の範囲内で乗り換えてよい

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
   - 本文 400〜700字。段落は3つまで
   - 相対表現(本日/昨日/明日)は発行日 {date} 基準。絶対日付と相対語を同一文で併用しない。内規の文言を紙面に書かない
2. 書き終えたら `stock/columnist.md`(手帳)を更新する。これは明日の自分への引き継ぎであり、あなたの人格が育つ唯一の場所だ:
   - 日誌に {date} の行を追記(1〜3行: 何に心が動いたか・どの切り口で書いたか・次に試したいこと)。14日より古い日誌は消す
   - 今日の執筆で偏愛・持論が生まれた/深まった/古びたと感じたら「私という書き手」を改稿(最大20行を厳守。増やすなら削る)。ただし根幹(上記の憲法)に反する方向へは育てない
   - 「追いかけている物語」の増減(紙面に結末を見届けたものは消し、新しく気になり始めたものを足す。最大10件)
   - 手帳は紙面に載らない私的なメモ。正直に書いてよい
"""


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
   **素材スナップショット同梱が必須**: 形式は schema/scheduled.schema.json と既存ファイルを確認し、元候補の
   title/url/source_type/facts/src_candidate_id を必ず写す(発火日は古い candidates を読まないため、ここが唯一の素材になる)。
   既に同じ id の予約がある場合は重複させない
4. stock/pending.yml … 日付未確定の追跡事項の増減(日付が判明した項目は scheduled へ移して消す)

## 注意
- 社説 docs/_editorials/{date}.md は**執筆済み**(専任セッション)。書き換えない
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


def run_plan(date: str, by_brand: dict, triggers: list[dict], wave: int = 4) -> dict:
    """面別に選定させ、機械的に1つの計画へ束ねる。

    面ごとに独立したセッションなので、1面が失敗しても他面は生きる(全滅しない)。
    面内の主題数は十数件なので、全主題に判断を下しても時間内に収まる。
    """
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
    plan["editorial_topic"] = pick.get("editorial_topic") or (lead or {}).get("angle", "")


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
                   stories: dict, wave: int = 4,
                   reuse: bool = False) -> tuple[list[dict], list[str]]:
    """記事ごとに素材を機械的に切り出して個別 codex セッションで執筆(wave 並列)。
    校閲(claude)とベンダーを分離するため執筆は Codex。機械検収エラーの修正は
    校閲と同じ Claude(REVIEW_MODEL=haiku)で行う。

    reuse=True(--reuse-plan)のときは、既に書けている記事を再執筆しない。
    執筆層だけを直して落ちた記事を拾い直す、という再試行を安く保つため。
    """
    trig_by_key = {t["dedup_key"]: t for t in triggers}
    jobs, written_before = [], []
    for art in plan["articles"]:
        if reuse and (ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md").exists():
            written_before.append(art)
            continue
        materials = [cands[cid] for cid in art["candidate_ids"]]
        src = weakest_src(c.get("source_type", "未確認") for c in materials)
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


def claude_review(date: str, round_no: int) -> dict:
    """校閲。執筆(Codex)と別ベンダーにするため Claude(REVIEW_MODEL=haiku)で実施。"""
    checklist = (ROOT / "prompts" / "review-checklist.md").read_text(encoding="utf-8").replace("{DATE}", date)
    if round_no > 1:
        checklist += f"\n\nこれは再校閲({round_no}回目)です。前回の指摘への修正が反映されています。"
    schema = (ROOT / "prompts" / "review-schema.json").read_text(encoding="utf-8")
    out = ROOT / "metrics" / f"review-{date}-{round_no}.json"
    r = subprocess.run(
        ["claude", "-p", checklist, "--model", REVIEW_MODEL,
         "--json-schema", schema, "--dangerously-skip-permissions",
         "--max-budget-usd", COMPOSE_WHOLE_MAX_BUDGET_USD],
        capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL, cwd=ROOT)
    text = r.stdout.strip()
    result = None
    try:
        result = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                result = json.loads(m.group(0))
            except Exception:
                result = None
    if result is None:
        result = {"verdict": "block",
                  "blockers": [{"file": "-", "issue": f"校閲実行失敗: {r.stderr[-200:]}", "quote": ""}],
                  "comments": []}
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


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
    ed_path = ROOT / "docs" / "_editorials" / f"{date}.md"
    for _ in range(2):  # 未作成なら同一プロンプトでもう一度だけ
        codex_run(editorial_prompt(date, number, plan.get("editorial_topic", "")),
                  timeout=1200, model=EDITORIAL_MODEL)
        if ed_path.exists():
            break
    if not ed_path.exists():
        notify("compose", f"{date}: 社説が2回とも未作成。組版セッションの lint 修正に委ねる", ok=False)

    # 1d. 組版: 号スナップショット・台帳更新(lint 自己修正まで)
    log = claude_run(assembly_prompt(date, number, aborted))
    print(log[-1000:], flush=True)

    # 2. 機械算出の確定 + lint ゲート
    subprocess.run([sys.executable, str(ROOT / "scripts" / "derive.py"), "--date", date, "--write"],
                   cwd=ROOT, capture_output=True, text=True)
    code, lint_out = run_lint(date)
    print(lint_out, flush=True)
    if code != 0:
        # 一度だけ Claude(検品=REVIEW_MODEL)に lint 修正を依頼
        claude_run(f"アイマスNEWS {date}号の lint がエラーです。`python3 scripts/lint.py --base origin/main` を実行し、"
                   f"エラー0になるまで docs/ と stock/ を修正してください。修正後 derive.py --date {date} --write も再実行すること。",
                   model=REVIEW_MODEL)
        code, lint_out = run_lint(date)
        if code != 0:
            notify("compose", f"{date}: lint 赤が解消できず。人間判断が必要\n{lint_out[-500:]}", ok=False)
            commit_and_push(branch, f"compose {date}: lint未解消(要人間判断)", "compose")
            return 1

    # 3. 校閲往復
    rounds = 0
    review = None
    for rounds in range(1, args.max_rounds + 2):
        review = claude_review(date, rounds)
        if review.get("verdict") == "approve":
            break
        if rounds > args.max_rounds:
            break
        fix = ("校閲AIから以下のブロック指摘がありました。candidates の facts と照合して記事を修正してください。"
               "修正後に derive.py --write と lint を再実行してエラー0にすること。\n"
               + json.dumps(review.get("blockers", []), ensure_ascii=False, indent=2))
        claude_run(fix, timeout=1200, model=REVIEW_MODEL)
        subprocess.run([sys.executable, str(ROOT / "scripts" / "derive.py"), "--date", date, "--write"],
                       cwd=ROOT, capture_output=True, text=True)

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
                              "aborted": aborted, "duration_s": int(time.time() - t0)})
    commit_and_push(branch, f"compose {date}: 紙面生成(校閲{'approve' if approved else '未approve'}・{rounds}往復)", "compose")
    if ok:
        notify("compose", f"{date}号 準備完了(校閲{rounds}往復で approve)。06:00 に発行されます")
        return 0
    reasons = []
    if not approved:
        blockers = (review or {}).get("blockers", [])
        reasons.append(f"校閲{rounds}往復でも未 approve。残ブロック:\n"
                       + json.dumps(blockers[:5], ensure_ascii=False, indent=2))
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
