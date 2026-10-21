#!/usr/bin/env python3
"""compose: 当日号の紙面を生成する。(PIPELINE §6。毎朝 04:00 起動)

  python3 scripts/compose.py [--plan] [--max-rounds 2]

役割分担: 記事執筆=Codex(gpt-6-luna)/ 社説執筆=Codex(gpt-6.1-sol)/
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
import contextlib
import datetime
import difflib
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
import urllib.parse
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipelib
import planlib
import renderlib
import storylink
import tags as tags_lib
from classify_sources import unresolved_post_sources
from pipelib import (ENV, ROOT, CLAUDE_MODEL, CODEX_WRITE_MODEL, COMPOSE_WAVE, EDITORIAL_MODEL,
                     COMPOSE_ARTICLE_MAX_BUDGET_USD, JST, JobLockTimeout, job_lock, prompt_file,
                     COMPOSE_WHOLE_MAX_BUDGET_USD, REVIEW_MODEL, append_metric,
                     checkout_edition_branch, classify_retag_lint, classify_source, commit_and_push,
                     edition_date, escalate, extract_json_array, git, has_editorial, EDITORIAL_UNTIL,
                     SETTLED_DROP_RULES, settled_drops,
                     notify, notify_crash, now_jst, render_prompt, PROMPTS, ANOMALIES, anomaly, diagnose_anomalies, save_raw,
                     partial_output, reap, loads_strict, schema_ok, read_for_raw, compose_lock_wait_min,
                     collect_oncall_handover_pending, candidate_misplaced, merge_candidate_facts, write_checked,
                     FETCH_RECORD_ENV, fetched_urls,
                     align_x_published_date, CLI_BLOCKED, CLI_BLOCKED_EXIT, CLI_STOPPED, CliBlocked, cli_check)
# モデルのセッションの起動と回収(自前のプロセスグループ。時間切れ・中断では子孫まで止める)
from pipelib import LaunchFailed, finish_group, run_group, start_group, stop_groups

# 執筆の出力形式。structured = 判断と文章を JSON で受けてコードがファイルを作る(構造は生成時に強制)。
# 執筆の依頼文(prompts/write-article.md)は structured 専用。以前の file(執筆セッションが Markdown を書く)は
# 依頼文ごと廃止した。.env に別の値が残っていたら、壊れた組み合わせで走らせずに止める
if ENV.get("WRITE_MODE", "structured").lower() != "structured":
    raise SystemExit("WRITE_MODE は structured だけ(file モードは廃止)。.env を直すこと")
STRUCTURED_WRITE = True

PAPER_STAGE = None  # main() で .env から
# この組版の実行の印(起動した時刻。文字列の順が時刻の順)。校閲記録に残し、発行は最新の実行の最後の巡を読む
# (pipelib.last_review_record。巡の番号は実行ごとに1から数え直す。監査指摘 2026-10-14)
RUN_ID = now_jst().strftime("%Y%m%dT%H%M%S.%f")


def load_scheduled(date: str) -> list[dict]:
    """発行日の続報予約(素材スナップショット同梱)。過去の candidates を遡らないための日付別ストア。
    event_date は予約が指す出来事の日(reminder_event_date)に置き換える。決められない予約は event_date を外す(執筆が決める)。
    deadline は予約が指す締切(reservation_deadline)に置き換える。予約に写した元の話題の deadline は受付締切など別の締切のことが
    あり、そのまま渡すと翌号への戻し(carry_dropped_candidates)が締切切れとして捨てる(2026-10-15: 10/18 の入金期限を指す
    レトラの予約の deadline が受付締切の 10/11 だった)。予約のファイルは書き換えない(予約後は原則変更しない)。"""
    p = ROOT / "stock" / "scheduled" / f"{date}.json"
    if not p.exists():
        return []
    rows = []
    for r in json.loads(p.read_text(encoding="utf-8")):
        row = dict(r)
        row.pop("event_date", None)         # 元の話題の日付は渡さない(下)
        ev = reminder_event_date(r, date)
        if ev:
            row["event_date"] = ev
        row.pop("deadline", None)
        dl = reservation_deadline(r, date, ev)
        if dl:
            row["deadline"] = dl
        rows.append(row)
    return rows


def reservation_deadline(row: dict, date: str, ev: str | None) -> str | None:
    """続報予約の素材の締切(素材の deadline として渡す値)。締切前・締切の予約は、予約が指す出来事の日(ev = reminder_event_date)
    が締切そのもの。ほかの予約と、出来事の日を決められない締切の予約は、元の話題の deadline を、予約を消化する号(date)より前
    でなければ渡す。号より前の deadline はこの予約が指す締切ではない(締切の予約は締切の日までに消化する)ので渡さない。"""
    if row.get("kind") in ("締切前", "締切") and ev:
        return ev
    dl = str(row.get("deadline") or "")
    return dl if re.fullmatch(r"\d{4}-\d{2}-\d{2}", dl) and dl >= date else None


def reminder_event_date(row: dict, date: str) -> str | None:
    """続報予約の記事の出来事の日(素材の event_date として渡す値)。予約が指す出来事の日そのもの。
    予約の日(ファイルの日付 = 記事を出す号)とは別: 06:00 より前に終わる締切・終了は前日の号に予約するので、予約の日を渡すと
    1日早い日付の記事になる(2026-10-14 監査指摘: 10/1 4:59 に終わる学マスのミッションパスの終了の予約(9/30)で、記事の
    event_date が 9/30 になり lint も通った)。組版は予約に出来事の日(occurs_on)を書く。
    occurs_on の無い古い予約(2026-10-14 より前)は、予約の日と種別から決まる日(締切前は3日後の締切日、ほかは予約の日)を、
    それが予約に書いてあり、ずれうる先の日(締切前は4日後、締切・終了は翌日)が書いていないときだけ渡す。ほかは、どの日かを
    コードは字面から決められないので None(素材に event_date を付けず、執筆が素材と予約の種別から決める)。例: 8/25 19時の配信の
    結果を翌日の号で伝える 8/26 の「結果」の予約は、予約に 8/26 が書いていない(監査指摘)。
    予約に写した元の話題の event_date(販売・受付の開始日)は渡さない: 書き手が写して1か月前の日付の記事になり、lint の鮮度検査で
    赤になった(実測 2026-10-12: 9/11 開始の予約受付の締切前の記事が event_date 9/11 で lint 赤)。元の話題の deadline も使わない:
    受付締切・入金期限・公演日など、予約が指す締切とは別の日のことがある(監査指摘: 10/18 の申込締切を指す 10/15 の予約で
    deadline は 11/4 の入金期限)"""
    if row.get("occurs_on"):
        return str(row["occurs_on"])
    d = datetime.date.fromisoformat(date)
    lags = renderlib.RESERVATION_LAG.get(row.get("kind"), (0,))
    hay = " ".join([str(f) for f in row.get("facts") or []]
                   + [str(row.get(k) or "") for k in ("note", "title", "subject", "event_date", "deadline")])
    written = [renderlib.date_mentioned((d + datetime.timedelta(days=n)).isoformat(), hay, renderlib.years_in(hay + " " + date))
               for n in lags]
    if not written[0] or any(written[1:]):
        return None
    return (d + datetime.timedelta(days=lags[0])).isoformat()


def next_number(date: str | None = None, live: bool | None = None) -> int:
    """この号の号数。稼働(PAPER_STAGE=live)なら通し番号。

    **自分の号スナップショットは数えない**: 号数は「自分より前の号の最大 + 1」で決まり、再実行しても
    同じ値になる。以前は自分の号も「既存の最大」に入っていたので、--reuse-plan で組み直すたびに番号が
    1つ進み、lint の連番検査で止まった(実測 2026-09-17: 第3号が第4号になった)。自分の号に書いてある
    値は信用しない(誤って進んだ値を自己修復できるように。監査指摘)。
    """
    if live is None:
        from pipelib import load_env
        live = load_env().get("PAPER_STAGE", "test") == "live"
    if not live:
        return 0
    import re
    nums = []
    for e in (ROOT / "docs" / "_editions").glob("*.md"):
        if date and e.stem >= date:   # 自分と、自分より後の号(過去号の組み直し)は数えない
            continue
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
# **締切と目標は別のもの。**
#
# 締切(COMPOSE_LIMIT_MIN)は 04:00 起動・06:00 発行の間、120分。ここを超えると
# 号が出ない。工程を打ち切る判断はこの数字だけで行う。
#
# 目標(COMPOSE_TARGET_MIN)は60分。速さの手当てが効いているかを測るための数字で、
# **打ち切りには使わない**。目標で打ち切ると、時間が余っていても往復を止めて
# しまい、直せたはずの記事を落としたまま毎日発行することになる。
# 超えたら記録と通知に出して、設計を直す材料にする。
# 2026-09-18 から起動は 03:00(編集長の決定: 止まったら当番が直して再実行する時間を残す)。
# 相対の上限は 170 分だが、実際に効くのは絶対の締切(発行 06:00 の 8 分前 = hard_deadline)
COMPOSE_LIMIT_MIN = int(ENV.get("COMPOSE_LIMIT_MIN", "170"))
COMPOSE_TARGET_MIN = int(ENV.get("COMPOSE_TARGET_MIN", "60"))
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


class stage:
    """`with stage("選定"):` で、その段にかかった分数を記録する。

    どこが遅いかを毎回残す。残していなかったので、時間切れのたびに
    人が journal を読み返して段の境目を数えていた。
    """

    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        self.t = time.time()
        return self

    def __exit__(self, *exc):
        STAGE_MIN[self.name] = STAGE_MIN.get(self.name, 0) + (time.time() - self.t) / 60
        print(f"[{self.name}] {STAGE_MIN[self.name]:.0f}分", flush=True)
        return False


DEADLINE: float | None = None   # 締切の絶対時刻(epoch 秒)。main が決める
HANDOFF_MIN = 8                 # 発行(06:00)の前に空けておく分(release の起動・ロック待ち・push)


# 承認まで進んでも記事を落としていたら、当番が直したあとこの号に戻す(--recover)。始めるのは、発行の締切
# (06:00 の HANDOFF_MIN 前)までにこれだけ残っているときだけ: 当番の往復(実測 13分)+ 組み直し(--reuse-plan の書き直しと校閲)
RECOVER_MIN_LEFT_MIN = 45


def recover_until(date: str, now: float | None = None) -> float | None:
    """落とした記事をこの号に戻せる締切(epoch 秒。発行日 06:00 の HANDOFF_MIN 前)。間に合わなければ None。"""
    now = time.time() if now is None else now
    try:
        d = datetime.date.fromisoformat(date)
    except ValueError:
        return None
    publish = datetime.datetime(d.year, d.month, d.day, 6, 0, tzinfo=JST).timestamp() - HANDOFF_MIN * 60
    return publish if now + RECOVER_MIN_LEFT_MIN * 60 <= publish else None


def hard_deadline(t0: float, date: str) -> float:
    """締切の**絶対時刻**。発行日 06:00 の HANDOFF_MIN 前と、起動から COMPOSE_LIMIT_MIN の早いほう。

    起動からの相対時間だけで測ると、遅れて起動した日は発行時刻を越える(監査指摘)。
    発行時刻を過ぎてからの手動再実行(その日の号をあとから出す)は、相対 COMPOSE_LIMIT_MIN 分だけで測る。
    「発行を過ぎたか」は実際の発行時刻(06:00)で見る。作業の締切(06:00 の HANDOFF_MIN 前)で見ると、05:53 の再起動が
    「発行後」になって締切が 08:43 に延び、06:00 の発行を待たせた(監査指摘 2026-10-14)。06:00 より前の起動は、締切を
    過ぎていても締切のまま(main が新しい作業を始めない)。
    """
    rel = t0 + COMPOSE_LIMIT_MIN * 60
    try:
        d = datetime.date.fromisoformat(date)
        publish_at = datetime.datetime(d.year, d.month, d.day, 6, 0, tzinfo=JST).timestamp()
    except ValueError:
        return rel
    return min(rel, publish_at - HANDOFF_MIN * 60) if t0 < publish_at else rel


def too_late_to_start(t0: float, date: str) -> bool:
    """締切(hard_deadline)を過ぎてから始まったか。過ぎていれば知らせ、なぜなぜを当番の保管へ回して True(main は排他も取らず、
    新しい作業を始めない)。
    06:00 の発行より前に始まった組版の締切は 05:52 のまま。始めると排他を持ち続け、06:00 の発行を待たせる(監査指摘 2026-10-14)"""
    dl = hard_deadline(t0, date)
    if time.time() < dl:
        return False
    started = datetime.datetime.fromtimestamp(t0, JST)
    why = (f"{date}: 締切({datetime.datetime.fromtimestamp(dl, JST):%H:%M})を過ぎてから始まった(起動 {started:%H:%M})。"
           "06:00 の発行を待たせないよう、新しい作業を始めずに終える(出し直すなら発行のあとに再実行する)")
    # なぜなぜは発行のあとに回す: この process の異常の台帳を当番の保管へ積み、台帳を空にする(終わりの diagnose_anomalies が
    # 当番を起こさない)。発行前に当番を起こすと、当番が工程の排他を持ったまま診断し、06:00 の発行を待たせる(監査指摘 2026-10-14)。
    # 保管は発行のあとの監視(watch 09:00)が当番に渡す
    try:
        import oncall
        kept = oncall.backlog_add([{"claim": f"{date} の組版が発行前の締切を過ぎて起動された(起動 {started:%H:%M})。誰が・なぜその時刻に"
                                             "起動したかを遡り、起動した仕組み(当番の組み直し・手動の再実行・排他の待ち)の欠陥なら直す",
                                    "occurs": "06:00 の発行より前で、締切(06:00 の HANDOFF_MIN 前)を過ぎてから組版が起動されたとき",
                                    "evidence": "\n".join(ANOMALIES + [why])[:3000]}], date, "compose", "")
        where = "当番の保管に積んだ(発行のあと watch が当番に渡す)" if kept else "当番の保管に既にある"
    except Exception as e:      # noqa: BLE001 — 保管に積めなくても、発行前に当番は起こさない(人には知らせる)
        where = f"当番の保管に積めなかった({type(e).__name__}: {e})。人が原因を確かめること"
    notify("compose", f"{why}。なぜなぜは{where}", ok=False)
    ANOMALIES.clear()
    return True


def time_left(t0: float) -> float:
    """締切まであと何分。**目標ではなく締切で測る。**絶対時刻(DEADLINE)があればそれで測る。"""
    end = DEADLINE if DEADLINE else t0 + COMPOSE_LIMIT_MIN * 60
    return (end - time.time()) / 60


def remaining_seconds(cap: int = 900) -> int:
    """子プロセスに待たせてよい秒数。締切を越えて待たない(見積もりが外れても止まれる。監査指摘)。
    締切を過ぎていれば 1 秒(=起動しても即座に打ち切られる。afford が先に止めるのが本筋)。"""
    if not DEADLINE:
        return cap
    return int(max(1, min(cap, DEADLINE - time.time())))


def deadline_passed() -> bool:
    """締切(DEADLINE)を過ぎたか。過ぎたら新しいセッション(やり直し・差し戻しも)を起動しない。待ち時間を縮めるだけでは
    起動は止まらない(監査指摘 2026-10-14: 締切後も後続の執筆・校閲とその再試行を起動した)。締切前は今どおり全部を起動する。"""
    return bool(DEADLINE) and time.time() >= DEADLINE


def terminal_cost() -> float:
    """**紙面を確定させる最後の1サイクル**にかかる分: ブロック記事を落とす → 社説∥組版のやり直し →
    lint → 最後の校閲1波 → commit/push。これより短い時間しか残っていない状態で校閲の往復を
    始めてはいけない(始めると、落とせる記事を落とす時間が無くなり、号ごと人へ回る。
    実測 2026-09-17: 残り17分で除外を諦め、機械で落とせる2本を残したまま止まった)。

    実測があればそれに2割の余白を足して使う(組版は号によって 6〜9分。固定値で見積もると
    境界で完走できない=監査指摘)。無ければ保守的な既定。commit/push・lint・checkpoint は 3分。
    """
    assemble = max(stage_cost("組版", 9) * 1.2, 6)
    review = max(stage_cost("校閲1波", 4) * 1.2, 2)
    return max(assemble + review + 3, COMPOSE_RESERVE_MIN)


def afford(t0: float, name: str | None, default: float, what: str, extra: float = 0,
           terminal: bool = True) -> bool:
    """`name` の段をもう1回やる時間があるか。

    `extra` はその段に付随して必ず走るもの(書き直し・組版のやり直し)の分。
    `terminal=True` なら、その段のあとに**紙面を確定させる最後の1サイクル**(terminal_cost)が
    まだ入ることを要求する。最後のサイクルそのものを始めるときは terminal=False で呼ぶ
    (以前はここにも後始末の分を上乗せしていて、いま落として整える時間があるのに諦めた)。

    **見るのは締切(120分)であって目標(60分)ではない。**目標で打ち切ると、
    時間が余っていても往復を止めて、直せたはずの記事を落としたまま毎日出す。
    """
    need = (stage_cost(name, default) if name else 0) + extra + (terminal_cost() if terminal else 0)
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
    # X の投稿の掲載日は投稿 ID の日本時間(収集し直さずに組み直す号・続報予約の素材も。保存済みのファイルは書き換えない)
    for c in items.values():
        align_x_published_date(c)
    return items


def with_current_type(c: dict) -> dict:
    """執筆・書き直しに見せる素材。出典の種別は判定表から引き直す(素材の source_type は収集・予約の時点の値。その後に決まった
    種別(リンク元から引き継いだ種別など。2026-10-21)を、紙面に付く種別(pipelib.classify_source)と同じ値で見せる)。"""
    return {**c, "source_type": classify_source(c["url"])} if str(c.get("url") or "").startswith(("http://", "https://")) else c


# 選定が読んだ後に前の号の候補ファイルへ入った候補を、何号前まで遡って引き継ぐか
LATE_LOOKBACK = 3


def read_record_path(date: str) -> Path:
    """その号の選定が読んだ候補 id の記録(metrics/candidates-read-<date>.json)。"""
    return ROOT / "metrics" / f"candidates-read-{date}.json"


def fact_key(fact: str) -> str:
    """選定記録に残す事実の識別(本文そのものは候補ファイルにあるので、短い要約値だけを残す)。"""
    return hashlib.sha1(fact.encode("utf-8")).hexdigest()[:12]


def record_candidates_read(date: str, cands: dict, hidden: set | frozenset = frozenset(),
                           unjudged: set | frozenset = frozenset()) -> None:
    """選定に掛けた候補ファイルの候補を、id → URL・読んだ facts の識別で記録する(選定し直すたびに上書き。--reuse-plan では書かない)。
    選定を始めるときは cands を空で呼び、空の記録にする(選定を始めた号の印。次の号はこの号のファイルを遡る)。
    件数ではなく事実そのものを記録する: 次の号が同じページを拾い直していても、読んだ後に足された事実は次の号の候補に無いことがある。
    hidden(前の号から引き継いだ未読の事実のうち、選定インデックスの件数上限で切れて選定に渡らなかったもの。write_plan_index が返す)は
    読んだことにしない: 記録すれば次の号へも引き継がれず、どの号の選定にも掛からない(監査指摘 late-facts-hidden-by-plan-index)。
    unjudged(判定を得られなかった主題の dedup_key。coverage_gaps の missing_keys)の候補も読んだことにしない(次の号が引き継ぐ)。"""
    p = ROOT / "candidates" / f"{date}.json"
    file_ids = {c["id"] for c in json.loads(p.read_text(encoding="utf-8")) if c.get("id")} if p.exists() else set()
    read_record_path(date).write_text(json.dumps(
        {"read": {i: {"url": cands[i].get("url"),
                      "facts": [k for k in (fact_key(f) for f in cands[i].get("facts") or []) if k not in hidden]}
                  for i in sorted(cands) if i in file_ids and cands[i].get("dedup_key") not in unjudged}},
        ensure_ascii=False, indent=0) + "\n", encoding="utf-8")


def _late_facts(date: str):
    """前の号 P(直近 LATE_LOOKBACK 号のうち選定記録のある号)の候補ごとに、P〜前日の号の選定記録のどれでも(同じ id でも
    同じ URL でも)読まれていない事実を (P, 候補, 未読の事実, 一度も読まれていない候補か) で返す。前の号のファイルと選定記録
    だけで決まる(この号の候補ファイルを書き換えても変わらない)ので、引き継ぎと選定インデックスの並べ替えが同じ集合を見る。"""
    d = datetime.date.fromisoformat(date)
    for k in range(LATE_LOOKBACK, 0, -1):
        prev = (d - datetime.timedelta(days=k)).isoformat()
        src = ROOT / "candidates" / f"{prev}.json"
        if not src.exists() or not read_record_path(prev).exists():
            continue
        seen_id: dict[str, set] = {}
        seen_url: dict[str, set] = {}
        for j in range(k, 0, -1):
            rp = read_record_path((d - datetime.timedelta(days=j)).isoformat())
            if rp.exists():
                for i, e in (json.loads(rp.read_text(encoding="utf-8")).get("read") or {}).items():
                    seen_id.setdefault(i, set()).update(e.get("facts") or [])
                    if e.get("url"):
                        seen_url.setdefault(e["url"], set()).update(e.get("facts") or [])
        for c in json.loads(src.read_text(encoding="utf-8")):
            if not c.get("id"):
                continue
            seen = seen_id.get(c["id"], set()) | seen_url.get(c.get("url"), set())
            unread = [f for f in c.get("facts") or [] if fact_key(f) not in seen]
            never_read = c["id"] not in seen_id and c.get("url") not in seen_url
            if unread or never_read:
                yield prev, c, unread, never_read


def late_fact_keys(date: str) -> set[str]:
    """前の号から引き継ぐ未読の事実の識別(選定インデックスで先に並べ、切れた分は選定記録に入れない)。"""
    return {fact_key(f) for _, _, unread, _ in _late_facts(date) for f in unread}


def carry_late_candidates(date: str) -> list[str]:
    """前の号の選定が候補を読んだ後に、その号の候補ファイルへ入った候補・事実(当番の取り直しが組版の開始後〜06:00 に
    終わった、など)を、この号の候補ファイルへ写す。収集は 06:00 前の分を前の号へ入れ、組版は自分の号のファイルしか
    読まないので、写さなければどの号の選定にも掛からない。
    前の号 P の候補の事実のうち、P〜前日の号の選定記録のどれでも(同じ id でも同じ URL でも)読まれていないものを未読とする。
    この号に同じ URL(または同じ id の写し)の候補があれば、未読の事実だけをその候補へ増分マージし、裏取りの結果も
    収集と同じ規則で引き継ぐ(同じページを拾い直していても、読んだ後に足された事実がこの号の候補にあるとは限らない)。
    無ければ候補を同じ id のまま写す。写した候補・事実はこの号の選定記録に入るので二度は写らない。P の選定記録が無い号
    (組版が選定まで進まなかった・記録を始める前の号)は遡らない。冪等。"""
    import copy
    target = ROOT / "candidates" / f"{date}.json"
    rows = json.loads(target.read_text(encoding="utf-8")) if target.exists() else []
    by_url = {c.get("url"): c for c in rows if c.get("url")}
    by_id = {c.get("id"): c for c in rows if c.get("id")}
    added: dict[str, list] = {}
    merged: dict[str, list] = {}
    for prev, c, unread, _never_read in _late_facts(date):
        tgt = by_url.get(c.get("url")) or by_id.get(c["id"])
        if tgt is None:
            c = copy.deepcopy(c)
            rows.append(c)
            by_url[c.get("url")], by_id[c["id"]] = c, c
            added.setdefault(prev, []).append(c["id"])
        elif candidate_misplaced(c):
            continue
        elif candidate_misplaced(tgt):
            # この号の候補が写しの照合で落ちたもの: 収集と同じく正しい候補で置き換える
            c = copy.deepcopy(c)
            rows[rows.index(tgt)] = c
            by_url[c.get("url")], by_id[c["id"]] = c, c
            added.setdefault(prev, []).append(c["id"])
        elif merge_candidate_facts(tgt, {**c, "facts": unread}):
            merged.setdefault(prev, []).append(tgt["id"])
    log = []
    for prev in sorted(set(added) | set(merged)):
        if added.get(prev):
            log.append(f"{prev}号の選定が読んだ後に入った候補 {len(added[prev])}件をこの号の候補へ写した: "
                       + ", ".join(added[prev][:10]))
        if merged.get(prev):
            log.append(f"{prev}号の選定が読んだ後に足された事実を、この号の同じページの候補 {len(merged[prev])}件へ足した: "
                       + ", ".join(merged[prev][:10]))
    if log:
        write_checked(target, json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    return log


CARRY_DROPPED_MAX = 2   # 前の号で記事にならなかった素材を、次の号の選定に戻す回数の上限(毎号同じ理由で落ち続けるのを止める)


def carried_not_news(cr: dict | None) -> str:
    """戻した印(carried)の理由が「既報と同じ内容で、新しい事実も当日のトリガーも無い」(執筆・書き直しの NOT_NEWS、校閲の R10)なら
    その理由の本文、それ以外は空。既報かどうかは話題・URL の照合(storylink)で決まり、別の発信元の投稿で拾い直した素材は過去の記事に
    つながらないので、この理由を捨てると選定には既報が見えない(2026-10-17〜19: 既報のラジオ・フェアを3号続けて新情報として選び、
    執筆・校閲が毎号同じ理由で落とした)。"""
    why = str((cr or {}).get("why") or "")
    m = re.search(r"(?:執筆で見送り|書き直しで見送り): NOT_NEWS|校閲ブロック: R10", why)
    return why[m.end():].strip().removesuffix(")").strip() or "(理由の本文なし)" if m else ""
# 日付 → {slug: {稿の指紋: その稿を採ったときの F 番号 → 事実の文}}。台帳はこれで記事の根拠 id を引く(素材から番号を振り直すと、
# 執筆の後に素材の事実が増減したとき番号がずれて、書いていない事実を「掲載済み」にする。監査指摘)。稿の指紋(renderlib.draft_key)ごとに
# 残すので、除外・組み直しの失敗で記事を旧い稿へ巻き戻しても、旧い稿の対応が選ばれる(監査指摘)。metrics/factmap-<号>.json に残す
FACT_MAP: dict[str, dict[str, dict[str, dict[str, str]]]] = {}


FACT_CONFLICT = {"__conflict__": "同じ稿の指紋に、違う事実の対応が記録された"}   # 台帳はこれを引かない(申告する)
FACTMAP_CORRUPTED = "__corrupted__"   # 記録が一度壊れた号の印(台帳は、記録の無い稿を素材の事実で代えず見出しだけにする)


def merge_fact_map(drafts: dict, key: str, fmap: dict) -> None:
    """稿の指紋 key の対応を drafts に足す。同じ指紋に違う対応が来たら、黙って上書きせず食い違いとして残す(監査指摘)。"""
    if key in drafts and drafts[key] != fmap:
        drafts[key] = dict(FACT_CONFLICT)
    else:
        drafts[key] = dict(fmap)


def remember_fact_map(date: str, slug: str, path: Path, fact_by_id: dict) -> None:
    """稿を採った(書き出した)直後に、その稿の指紋と F 番号 → 事実の対応を記録する。"""
    merge_fact_map(FACT_MAP.setdefault(date, {}).setdefault(slug, {}), renderlib.draft_key(path), fact_by_id)


def persist_records(date: str) -> None:
    """組版の各コミットの前と途中終了の後始末で、次の工程が読む記録(外れた記事・稿ごとの事実の対応)を残す。"""
    persist_drops(date)
    p = ROOT / "metrics" / f"factmap-{date}.json"
    old, ok = read_record(p)
    ok = ok and renderlib.factmap_valid(old)     # 読めても形が崩れていれば壊れている(監査指摘)
    rows = {s: dict(v) for s, v in (old if isinstance(old, dict) else {}).items() if isinstance(v, dict)} if ok else {}
    if not ok or (isinstance(old, dict) and old.get(FACTMAP_CORRUPTED)):
        # 既存の記録が読めない(書きかけで壊れた 等): 今回書いていない稿の対応は失われた。そうと分かる印を残し、台帳はその稿を素材の
        # 事実で代えず見出しだけにする(印が無いと「記録の無い古い稿」と同じに扱われ、書いていない事実を掲載済みにする。監査指摘)
        if not ok:
            anomaly("compose", f"{p.name} が読めない(壊れている)。今回書いていない稿の事実の対応を失った。台帳はそれらを見出しだけにする")
        rows[FACTMAP_CORRUPTED] = "既存の記録が読めなかった"
    for slug, drafts in (FACT_MAP.get(date) or {}).items():
        for key, fmap in drafts.items():
            merge_fact_map(rows.setdefault(slug, {}), key, fmap)
    # 空でも書く: 記録のファイルがあることが「この号は稿ごとの記録で書いた」の印で、台帳は記録の無い稿を素材の事実で代えない
    # (初回の保存の前に止まった号を、仕組みより前の号と取り違えない。監査指摘)
    write_record(p, rows)


def persist_drops(date: str) -> None:
    """紙面から外れた記事(見出し・段・理由・素材の id)を metrics/drops-<日付>.json に残す。次の号の組版が、その素材を選定に
    戻すのに使う(carry_dropped_candidates)。組版の各コミットの前と途中終了の後始末で呼ぶ(終わり方を問わず残す。監査指摘)。
    以前の記録(同じ号の前の実行・--reuse-plan の組み直し)と合わせ、紙面にある記事(組み直しで戻った記事)だけを除く。
    素材の id は計画のファイルから読む。"""
    p = ROOT / "metrics" / f"drops-{date}.json"
    old, ok = read_record(p)
    if not ok:
        anomaly("compose", f"{p.name} が読めない(壊れている)。前の実行で外れた記事の記録を失った(この号で外れた記事だけを残す)")
    plan, _ = read_record(ROOT / "metrics" / f"plan-{date}.json")
    ids = {a.get("slug"): a.get("candidate_ids") or [] for a in (plan if isinstance(plan, dict) else {}).get("articles") or []}
    rows = dict(old if isinstance(old, dict) else {})
    # 重複の相手は、issue から拾った語のうち計画にある記事に絞る(計画が読めなければ拾ったまま。相手かどうかは settled_drops が
    # 紙面にあるかで決める)。絞って相手が残らない重複は、外した理由が成り立つか分からないので印を付けない
    settled = {}
    for slug, st in (DROP_SETTLED.get(date) or {}).items():
        if st.get("rule") == "P1" and ids:
            st = dict(st, dup_of=[d for d in st.get("dup_of") or [] if d in ids])
            if not st["dup_of"]:
                continue
        settled[slug] = st
    rows.update({slug: {"title": t, "stage": st, "why": why, "candidate_ids": ids.get(slug, []),
                        **({"settled": settled[slug]} if slug in settled else {})}
                 for slug, (t, st, why) in (DROP_LOG.get(date) or {}).items()})
    rows = {s: r for s, r in rows.items() if not (ROOT / "docs" / "_posts" / f"{date}-{s}.md").exists()}
    if rows or p.exists():
        write_record(p, rows)


def read_record(path: Path) -> tuple[object, bool]:
    """記録の JSON を読む。戻りは (中身, 読めたか)。ファイルが無いのは読めた扱いで {}(無いことと壊れていることを分ける。監査指摘)。"""
    if not path.exists():
        return {}, True
    try:
        return json.loads(path.read_text(encoding="utf-8")), True
    except (OSError, ValueError):
        return {}, False


def write_record(path: Path, data) -> None:
    """記録の JSON を一時ファイルに書いてから一度に差し替える(書きかけで止まっても、壊れた記録を残さない。監査指摘)。"""
    import os
    # 一時ファイルは Git 管理外(metrics/work/)に置く。書いている途中で止まっても、未追跡のファイルで発行の clean 判定を止めない
    tmpdir = ROOT / "metrics" / "work"
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmp = tmpdir / f".{path.name}.{os.getpid()}"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def scheduled_as_candidate(s: dict) -> dict | None:
    """続報予約の素材スナップショットを、候補ファイルに置ける形(schema/candidates.schema.json)にする。予約の項目(kind・subject・
    reserved_on 等)をそのまま候補ファイルへ写すと lint のスキーマ違反で号が止まる。出典 URL の無い予約は候補にならない(None)。
    発見日時は予約した日(素材を写し取った日)。"""
    if not str(s.get("url") or "").startswith(("http://", "https://")):
        return None
    schema_p = Path(__file__).resolve().parent.parent / "schema" / "candidates.schema.json"    # 作業ツリーの ROOT ではなくコードの schema
    props = json.loads(schema_p.read_text(encoding="utf-8"))["items"]["properties"]
    c = {k: v for k, v in s.items() if k in props}
    c["title"] = s.get("title") or s.get("subject") or ""
    c["found_at"] = f"{s.get('reserved_on') or ''}T00:00:00+09:00"
    if c.get("source_type") not in props["source_type"]["enum"]:
        c["source_type"] = "未確認"
    if c.get("verify") not in props["verify"]["enum"]:
        c["verify"] = "unconfirmed"
    if "via" in c and c["via"] not in props["via"]["enum"]:
        c.pop("via")
    for k in ("event_date", "deadline"):
        if k in c and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(c[k])):
            c.pop(k)
    return c


def attach_companions(plan: dict, cands: dict) -> None:
    """続報予約に添えた素材(companion_of。予約を付けた記事が出典にしたほかの素材)を、その予約を素材にした記事の candidate_ids に足す。
    選定し直した計画では主題の ids から入っているが、保存した計画(--reuse-plan)は添える前の ids のままのことがある
    (2026-10-20: 予約に添えた公式の投稿が、落とした締切の知らせの書き直しに渡らない)。冪等。"""
    heads: dict[str, list[str]] = {}
    for i, c in cands.items():
        if c.get("companion_of"):
            heads.setdefault(c["companion_of"], []).append(i)
    for a in plan.get("articles") or []:
        ids = a.get("candidate_ids") or []
        a["candidate_ids"] = list(dict.fromkeys(ids + [x for i in ids for x in heads.get(i, [])]))


def carry_dropped_candidates(date: str) -> list[str]:
    """前の号の計画に入ったのに紙面に載らなかった記事(執筆の見送り・校閲のブロック・検算の不合格)の素材を、この号の候補へ写す。
    選定は自分の号の候補しか読まないので、写さなければ、直したあと(規則・道具の修正)でも二度と選定に掛からない
    (2026-10-13: 報道を出典にしてよいと決めたログボが、翌号の選定に戻る経路が無かった。編集長「復帰保証までが仕事」)。
    写した候補には carried(どの号で・なぜ記事にならなかったか・何回目か)を付け、選定に見せる。採るかは選定が決める。
    戻した素材を前の号の選定が選ばなかったときも、締切までは戻す。
    同じ素材を写すのは CARRY_DROPPED_MAX 回まで。この号に同じ URL・id の候補があれば、写さずに carried だけを付ける。冪等。"""
    import copy
    prev = (datetime.date.fromisoformat(date) - datetime.timedelta(days=1)).isoformat()
    drops_p = ROOT / "metrics" / f"drops-{prev}.json"
    src_p = ROOT / "candidates" / f"{prev}.json"
    try:
        # 外れた記事の記録が無い号でも、選定が選ばなかった戻し素材は戻す(下)
        drops = (json.loads(drops_p.read_text(encoding="utf-8")) or {}) if drops_p.exists() else {}
        # 候補ファイルの無い号(当日の続報予約だけで組んだ)でも、予約の素材は戻す(監査指摘)
        src = {c.get("id"): c for c in (json.loads(src_p.read_text(encoding="utf-8")) if src_p.exists() else []) if c.get("id")}
    except (OSError, ValueError):
        return [f"{prev}号の外れた記事の記録が読めない(素材を選定に戻せない)"]
    # 前の号の続報予約から選んだ記事の素材(sched-*)も戻す元にする。日付は前の号で使った値のまま(load_scheduled(前の号)。監査指摘)
    src.update({s["id"]: c for s in load_scheduled(prev) if s.get("id") and (c := scheduled_as_candidate(s))})
    target = ROOT / "candidates" / f"{date}.json"
    rows = json.loads(target.read_text(encoding="utf-8")) if target.exists() else []
    by_url = {c.get("url"): c for c in rows if c.get("url")}
    by_id = {c.get("id"): c for c in rows if c.get("id")}
    # 写す先(同じページ = 同じ URL、無ければ id)ごとに、外れた記事の素材を全部まとめる(同じ URL の素材が複数あっても、
    # 2件目以降の事実を落とさない。監査指摘)
    groups: dict[str, dict] = {}
    settled = settled_drops(prev, ROOT)
    for slug, d in drops.items():
        if (ROOT / "docs" / "_posts" / f"{prev}-{slug}.md").exists():
            continue    # 組み直しで戻った記事
        if slug in settled:
            continue    # 同じ知らせが前の号に載った(重複)。選定に戻しても同じ理由で外れる
        why = f"{prev}号で記事にならなかった({d.get('stage', '')}: {str(d.get('why') or '')[:120]})"
        for cid in d.get("candidate_ids") or []:
            c = src.get(cid)
            if c:
                g = groups.setdefault(c.get("url") or f"id:{cid}", {"srcs": {}, "why": why})
                g["srcs"][cid] = c
    # 戻した素材を前の号の選定がまた選ばなかった(計画に入らなかった)ものも戻す。選定で外れると外れた記事の記録に載らず、次の号に
    # 戻る経路が無かった(2026-10-14: 戻したログボを選定が前の号の理由で外し、締切の 10/15 号に戻らなかった)。回数の上限で止まる
    plan, _ = read_record(ROOT / "metrics" / f"plan-{prev}.json")
    picked = {i for a in (plan if isinstance(plan, dict) else {}).get("articles") or [] for i in a.get("candidate_ids") or []}
    picked_urls = {src[i].get("url") for i in picked if i in src}
    for cid, c in src.items():
        if c.get("carried") and cid not in picked and c.get("url") not in picked_urls:
            # 既報と同じとして外れた理由は引き継ぐ(選定が外したのはその理由が成り立ったから。捨てると次の号の選定に既報が見えない)
            why = f"{prev}号の選定で選ばれなかった(戻した素材)"
            if carried_not_news(c["carried"]):
                why += f" / {c['carried']['why']}"
            g = groups.setdefault(c.get("url") or f"id:{cid}", {"srcs": {}, "why": why})
            g["srcs"][cid] = c
    # 締切を過ぎた素材は、どちらの経路でも戻さない(監査指摘: 外れた記事の経路だけ締切を見ていなかった)
    expired = lambda c: bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(c.get("deadline") or ""))) and c["deadline"] < date
    carried, changed = [], False
    for key, g in groups.items():
        srcs = [g["srcs"][i] for i in sorted(g["srcs"]) if not expired(g["srcs"][i])]       # 順序に依らない
        if not srcs:
            continue
        good = [c for c in srcs if not candidate_misplaced(c)]
        # 回数は、まとめた素材の最大値から一度だけ増やす。この号に同じページの候補があれば、上限を超えても回数を付け続ける
        # (残さないと、毎号拾い直す素材の回数が 1→2→無し→1 と戻る。監査指摘)。写す(候補が無い)のは上限まで
        n = max(int((c.get("carried") or {}).get("count") or 0) for c in srcs) + 1
        tag = {"from": prev, "count": n, "why": g["why"]}
        tgt = (by_url.get(key) if not key.startswith("id:") else None) or next(
            (by_id[c["id"]] for c in srcs if c.get("id") in by_id), None)
        if tgt is None:
            if n > CARRY_DROPPED_MAX or not good:
                continue
            new = copy.deepcopy(good[0])
            for c in good[1:]:
                merge_candidate_facts(new, c)
            new["carried"] = tag
            rows.append(new)
            by_url[new.get("url")], by_id[new.get("id")] = new, new
            carried.append(new.get("id"))
            changed = True
            continue
        if candidate_misplaced(tgt) and good:
            # この号の候補が写しの照合で落ちたもの: 収集・late の引き継ぎと同じく正しい候補で置き換える
            new = copy.deepcopy(good[0])
            rows[rows.index(tgt)] = new
            by_url[new.get("url")], by_id[new.get("id")] = new, new
            tgt, changed = new, True
        for c in good:      # 前の号の事実と裏取りを既存の規則で引き継ぐ(重なりは除かれるので、何度呼んでも同じ)
            changed = merge_candidate_facts(tgt, c) or changed
        if (tgt.get("carried") or {}).get("from") != prev:      # 印と回数は一度だけ(冪等)
            tgt["carried"] = tag
            changed = True
    if changed:
        write_checked(target, json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    return [f"{prev}号で記事にならなかった記事の素材 {len(carried)}件を、この号の選定に戻した: " + ", ".join(carried[:10])] if carried else []


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


def write_plan_index(date: str, cands: dict, blocklist: dict,
                     late: set | frozenset = frozenset()) -> tuple[dict, int, set]:
    """選定用の主題インデックスを機械生成する。

    候補ファイルをそのまま読ませると、収集の増分マージで facts がほぼ同文のまま
    積み上がるため巨大になる(2026-08-25号は 281KB・5577行)。選定に必要なのは
    「どの主題があり、どの id を指せばよいか」であって facts の全文ではない
    (facts の全文は執筆セッションが素材として別途受け取る)。
    dedup_key で束ねて主題単位にし、facts は先頭数件・各短縮で渡す。
    late(前の号から引き継いだ未読の事実の識別。late_fact_keys)は先に並べる: 引き継ぎは候補の末尾へ足すので、
    そのままでは件数上限で切れて選定に渡らない(監査指摘 late-facts-hidden-by-plan-index)。それでも上限で切れた late の
    識別を3つ目の値で返し、選定記録はそれを読んだことにしない(次の号がまた引き継ぐ)。
    late は文字数で短縮せず全文で渡す: 選定記録は全文の識別で読んだと残すので、PLAN_FACT_CHARS 以降にある新情報
    (長い一覧の後半への追記など)が見えないまま読了になり、次の号へも渡らない(監査指摘 late-facts-hidden-by-char-limit)。
    """
    subjects: dict[str, dict] = {}
    pool: dict[str, list] = {}  # 主題 → (短縮した事実, 事実の識別)。候補の順
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
        cr = c.get("carried") or {}
        if cr.get("from") and int(cr.get("count") or 0) <= CARRY_DROPPED_MAX and not s.get("carried"):
            # 前の号で記事にならなかった印だけを見せ、理由は見せない(選定が前の号の理由を今日の判定と読んで外した。2026-10-14 の
            # ログボ: 報道を出典にしてよいと決めた後も「出典不足」で外れた)。記事にできるかは執筆と校閲が今日の規則で判断する。
            # 上限を超えたら見せない。
            # ただし「既報と同じで新しい事実も当日のトリガーも無い」で外れた理由は見せる: 規則・道具の修正で変わる判定ではなく、
            # 既報の照合(storylink)が別の発信元の投稿をつなげないと、選定には既報が見えず毎号新情報として選ぶ(2026-10-17〜19)
            s["carried"] = f"{cr['from']}号までに記事にならず、選定に戻した({int(cr.get('count') or 0)}回目)"
            nn = carried_not_news(cr)
            if nn:
                s["carried"] += f"。既報と同じ内容で、新しい事実も当日のトリガーも無いとして外れた: {nn}"
        for f in c.get("facts", []):
            k = fact_key(f)
            pool.setdefault(dk, []).append((f if k in late else f[:PLAN_FACT_CHARS], k))
    # 既報の照合はコードがする(選定のモデルに約1万行の台帳を読ませない。読み切れず、同じ知らせを新規として通していた)。
    # 候補の dedup_key は storylink.link で、同じ一次情報の過去の記事の話題につなぎ直してある
    # 既報は台帳の要約(1記事1〜4件)ではなく、過去の記事が**読者に出した本文**で渡す(要約で渡したら、価格・店舗を「新しい事実」と
    # 判断して同じ記事をもう一度載せた。実測 2026-10-01 の8番らーめん)
    # 既報は主題のキーだけでなく、候補のつなぎ直す前のキー・候補の URL からも引き、同じ一次情報の別の話題の記事も添える
    # (2026-10-16号: 765×961 ライブの出演者は 9/30 号の記事にあったが、ゲスト発表の話題の既報しか渡らず新情報として選んだ)。
    # 同じ催しの記事は本数で切らずに全部を、催しごとに1度だけ plan-events に書き、主題には催しのキー(events)を付ける
    # (主題ごとに写すと、発表の多い催しの主題が並ぶ面で索引が膨らむ。本数で切ると、発表が重なるにつれ古い発表の記事が
    # 押し出されて同じことが起きる。監査指摘 related-prior-evicts-known-cast)
    prior = storylink.prior_lookup(date, ROOT)
    hidden: set[str] = set()
    for s in subjects.values():
        items = sorted(pool.get(s["dedup_key"], []), key=lambda t: t[1] not in late)
        s["facts"] = list(dict.fromkeys(t for t, _ in items))[:PLAN_FACTS_PER_SUBJECT]
        hidden |= {k for t, k in items if k in late and t not in s["facts"]}
        keys = storylink.prior_keys(cands[i] for i in s["ids"])
        rows = prior(keys, s["urls"], events=False)
        if rows:
            s["prior"] = rows
        evs = prior.events(keys, s["urls"])
        if evs:
            s["events"] = evs
    # 1主題1行で書く(整形すると数千行になり、Read の既定上限 2000行で頭から切れて
    # 後半の主題が編集長の視野に入らなくなる。行数=主題数なら一度で読み切れる)
    def dump(rows: list[dict], path: Path) -> None:
        body = ",\n".join(json.dumps(s, ensure_ascii=False, separators=(",", ":")) for s in rows)
        path.write_text("[\n" + body + "\n]\n", encoding="utf-8")

    dump(list(subjects.values()), ROOT / "metrics" / f"plan-index-{date}.json")
    events = {e: prior.event_rows(e) for s in subjects.values() for e in s.get("events", [])}
    by_brand: dict[str, list[dict]] = {}
    for s in subjects.values():
        by_brand.setdefault(s.get("brand") or "other", []).append(s)
    for b, rows in by_brand.items():
        dump(rows, ROOT / "metrics" / f"plan-index-{date}-{b}.json")
        write_event_index(events, rows, ROOT / "metrics" / f"plan-events-{date}-{b}.json")
    return by_brand, len(subjects), hidden


def write_event_index(events: dict[str, list[dict]], rows: list[dict], path: Path) -> None:
    """選定に渡す、同じ催しの過去の記事(1催し1行。主題の `events` が指す催しだけ。冪等: 無ければ空の配列)。"""
    evs = sorted({e for r in rows for e in r.get("events", [])})
    body = ",\n".join(json.dumps({"event": e, "articles": events.get(e, [])}, ensure_ascii=False, separators=(",", ":")) for e in evs)
    path.write_text("[\n" + body + "\n]\n" if body else "[]\n", encoding="utf-8")


def brand_plan_prompt(date: str, brand: str, n_subjects: int, triggers: list[dict],
                      feedback: str = "", claimed: list[dict] | None = None) -> str:
    """面(ブランド)ごとの選定プロンプト。

    号全体を1セッションに裁かせると、161主題で 1800秒のタイムアウトに達して
    計画が1本も出ない(2026-08-26の再発行で実測)。面ごとに割ると1セッションが
    見る主題は十数件になり、全主題に判断を下しても時間内に収まる。
    lead はここでは付けない(面をまたぐ比較が要るため後段で決める)。
    """
    return render_prompt(
        "plan-brand", DATE=date, WEEKDAY="月火水木金土日"[datetime.date.fromisoformat(date).weekday()],
        BRAND=brand, N=n_subjects, FACTS_PER=PLAN_FACTS_PER_SUBJECT, RULES=plan_rules(),
        TRIGGERS=json.dumps([{k: t.get(k) for k in ("id", "dedup_key", "brand", "subject", "kind", "note")}
                             for t in triggers], ensure_ascii=False, indent=1) if triggers else "(なし)",
        CLAIMED=json.dumps(claimed, ensure_ascii=False, indent=1) if claimed else "(なし)",
        RECENT="\n".join(f"- {t}" for t in recent_for_brand(date, brand)) or "(なし)",
        FEEDBACK_REF="「前回の機械検証エラー」" if feedback else "",
        FEEDBACK=f"\n## 前回の機械検証エラー(必ず解消する)\n{feedback}\n" if feedback else "")


def recent_for_brand(date: str, brand: str) -> list[str]:
    """選定に渡す「この面の直近の記事」: この面と、合同面(joint)の直近の見出し。合同の催し(765×961 ライブ等)の続報は
    単独の面の主題として集まるので、面の見出しだけでは同じ催しの既報が見えない(2026-10-16号: 765 面の出演者の主題)。"""
    recent = storylink.recent_titles(date, ROOT)
    rows = recent.get(brand, []) + ([] if brand == "joint" else [f"{t}(合同面)" for t in recent.get("joint", [])])
    return sorted(rows)


def plan_rules() -> str:
    """選定の規則(prompts/plan-rules.md)。面別の選定と、判定から漏れた主題の拾い直しが**同じもの**を使う。"""
    return render_prompt("plan-rules", ROUNDUP_MIN=ROUNDUP_MIN_ITEMS).rstrip("\n")


def lead_prompt(date: str, arts: list[dict]) -> str:
    """面別計画を束ねたあと、号の一面だけを決める短いセッション(社説は 2026-09-06 号で終了。起点は選ばせない)。"""
    rows = json.dumps([{k: a.get(k) for k in ("slug", "brand", "rank", "angle", "lead_score")}
                       for a in arts if a.get("rank") != "roundup"], ensure_ascii=False, indent=1)
    return render_prompt("plan-lead", DATE=date, WEEKDAY="月火水木金土日"[datetime.date.fromisoformat(date).weekday()],
                         ARTICLES=rows)


def article_prompt(date: str, art: dict, materials: list[dict], story_facts: list[str],
                   trigger: dict | None) -> str:
    """執筆の依頼文。本文は prompts/write-article.md(rank 別の追加は write-article.<rank>.md)。
    ファイルは作らせず、判断と文章だけを schema(article-out)で受ける。frontmatter とファイルはコードが作る。"""
    rank_file = PROMPTS / f"write-article.{art['rank']}.md"
    return render_prompt(
        "write-article", DATE=date, WEEKDAY="月火水木金土日"[datetime.date.fromisoformat(date).weekday()],
        ANGLE=art["angle"],
        # 目的: 続報予約の消化と、選定が「期日の知らせ」とした記事は、期日を伝え直すこと自体が目的(新しい事実は要らない)
        PURPOSE=("期日の知らせ" if trigger or art.get("purpose") == "期日の知らせ" else "新情報"),
        TRIGGER=(f"- 続報予約: {trigger['kind']}(読者がその日にできること: {trigger.get('note') or trigger['subject']})\n" if trigger else ""),
        RANK_RULES=rank_file.read_text(encoding="utf-8") if rank_file.exists() else "",
        TAG_VOCAB=tags_lib.vocabulary_block(),
        STORY_FACTS="\n".join(f"- {f}" for f in story_facts) or "(なし)",
        MATERIALS=json.dumps(materials, ensure_ascii=False, indent=2))


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
    core = (PROMPTS / "columnist-core.md").read_text(encoding="utf-8")
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


def run_assemble(date: str, number: int) -> None:
    """組版(scripts/assemble.py)。判断は小さな構造化セッション、反映はコード、結果は冪等。

    以前はここが Claude の1セッションで、40本に24.7分かかっていた(ツール呼び出し96回。
    大半が台帳の事務処理を即席のシェルと python で組み立てて lint に叩かれる時間)。
    冪等なので、記事が落ちても増えても**もう一度呼ぶだけ**でよい。組版前へ巻き戻す・
    成果物から抜く、という手順はもう要らない。
    """
    import assemble as _assemble
    persist_records(date)    # 台帳は、稿を採ったときの事実の記録(factmap)をファイルから読む。組版の前に残す
    code = _assemble.run(date, number, budget=lambda: remaining_seconds(cap=_assemble.SESSION_CAP))
    if code == 1:
        raise RuntimeError("組版: 記事が無い")
    # 2 = lint が赤い・派生欄(derive)か lint が異常終了した(組版の欠陥として通知済み)。呼び出し側の lint ゲートで扱う


def reassemble(date: str, number: int, plan: dict, written: list[dict]) -> None:
    """**組版をやり直す前に、枠を書き上がりの長さで当て直す。**

    書き直し(lint 赤の修正・校閲の指摘)で本文の長さが変わると、前に当てた枠が字数と合わなくなる。
    当て直さずに組み直すと、組版のあとの lint が「本文と枠が不一致」で赤くなる(2026-10-15号: 394字・438字の
    記事が medium のまま)。一面は動かさない(号スナップショットの lead_slug が指している)
    """
    assign_ranks(date, plan, written, keep_lead=True)
    run_assemble(date, number)


def cli_stop(tool: str, where: str, rc, *texts) -> dict | None:
    """組版のモデル CLI の1回の後に呼ぶ。認証切れ・利用上限で答えなかったなら、その理由(投稿は済み)。呼び側は失敗に数えず
    (記事を落とさない・面を不採用にしない)、その回の並列の子を回収し終えてから CliBlocked で工程を止める。
    号の締切までに CLI は戻らないことが多く、校閲も執筆も同じ CLI なので、続けても記事を落とすだけになる。"""
    return cli_check(tool, "compose", where, rc, *texts)


def claude_run(prompt: str, timeout: int = 2400, model: str | None = None) -> str:
    try:
        r = run_group(
            ["claude", "-p", prompt_file(edition_date(), "claude-" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8], prompt),
             "--model", model or CLAUDE_MODEL, "--dangerously-skip-permissions",
             "--max-budget-usd", COMPOSE_WHOLE_MAX_BUDGET_USD],
            timeout=remaining_seconds(cap=timeout), cwd=ROOT)
    except subprocess.TimeoutExpired as e:
        save_raw(edition_date(), f"claude-run-{time.time_ns() % 10**9}", *partial_output(e))   # 時間切れの回の出力も残す
        raise
    # **予算切れは黙って通り過ぎていた。**組版セッションが途中で打ち切られ、
    # digest と台帳が半端なまま lint が16件赤くなった日がある(2026-08-31)。
    # 戻り値を見ないので呼び出し側は気づけない。ここで鳴らす
    save_raw(edition_date(), f"claude-run-{time.time_ns() % 10**9}", r.stdout, r.stderr, r.returncode)
    blocked = cli_stop("claude", "Claude のセッション", r.returncode, r.stdout, r.stderr)
    if blocked:
        raise CliBlocked(blocked)
    out = (r.stdout or "") + (r.stderr or "")
    if "Exceeded USD budget" in out or r.returncode != 0:
        # 途中で終わったセッションの出力は答えにしない(異常終了の間際の出力を「直すものなし」などと読まない。監査指摘)。
        # 呼び出し側が「答えが無い」として扱う(やり直し・異常)
        why = "予算上限に到達" if "Exceeded USD budget" in out else f"exit {r.returncode}"
        raise RuntimeError(f"Claude セッションが途中で終了({why}): {out.strip()[-300:]}")
    return r.stdout


def log_of(out_path: Path) -> Path:
    """Codex の最終メッセージの一時ファイルに対応する、進行ログの一時ファイル。"""
    return Path(str(out_path) + ".log")


def take_log(out_path: Path) -> str:
    """進行ログを読んで消す(無ければ空)。"""
    lp = log_of(out_path)
    try:
        return lp.read_text(encoding="utf-8", errors="replace") if lp.exists() else ""
    finally:
        lp.unlink(missing_ok=True)


def codex_run(prompt: str, timeout: int = 2400, model: str | None = None) -> str:
    """Codex 側でファイルを書かせる実行。最終メッセージは一時ファイルで受ける
    (codex は stdout に進行ログも混ぜるため、標準出力からの抽出は当てにしない)。"""
    fd, out_name = tempfile.mkstemp(prefix="codexrun-", suffix=".txt")
    os.close(fd)
    out_path = Path(out_name)
    so, se, code = "", "", None
    try:
        r = run_group(
            ["codex", "exec", "-m", model or CODEX_WRITE_MODEL, "-s", "workspace-write",
             "--output-last-message", str(out_path),
             prompt_file(edition_date(), "codex-" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8], prompt)],
            timeout=remaining_seconds(cap=timeout), cwd=ROOT)
        so, se, code = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:
        so, se = partial_output(e)
    text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
    out_path.unlink(missing_ok=True)
    # 最終メッセージに加え、進行ログ(標準出力・エラー)と終了状態も残す(時間切れの回も)
    save_raw(edition_date(), f"codex-run-{time.time_ns() % 10**9}", so, se, code, files={"last-message": text})
    blocked = cli_stop("codex", "Codex のセッション", code, so, se)
    if blocked:
        raise CliBlocked(blocked)
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
    # 読めない答えを「直すものなし([])」と取り違えない(読めないのに記事を外していた。2026-10-06 の洗い出しで判明)。
    # 読めなければもう1回だけ聞き、それでも読めなければ、外される記事を異常として上げる
    from pipelib import extract_json_array_strict
    # 要素の形と対応も見る(null・欄の欠け・空の修復先・依頼していない (slug, old)・実在しない修復先・同じ対象への2つの
    # 答えは読めない答え。明示の [] だけが「直すものなし」。監査指摘)
    def shaped(v) -> bool:
        if v is None:
            return False
        seen = set()
        for r in v:
            if not (isinstance(r, dict) and all(isinstance(r.get(k), str) and r[k] for k in ("slug", "old", "new"))):
                return False
            if r["old"] not in bad.get(r["slug"], []) or r["new"] not in cands or (r["slug"], r["old"]) in seen:
                return False
            seen.add((r["slug"], r["old"]))
        return True
    got = None
    for _ in range(2):
        try:
            got = extract_json_array_strict(claude_run(prompt, timeout=600, model=REVIEW_MODEL))
        except Exception as e:
            print(f"候補IDの修復のセッションが失敗: {e}", flush=True)
            got = None
        if not shaped(got):
            got = None
        if got is not None:
            break
    if got is None:
        anomaly("compose", "実在しない候補 ID の修復の答えが2回とも読めなかった。次の記事は修復されないまま外される: "
                           + ", ".join(bad))
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
        # ここで culture を外すと「残ったのが culture だけ」の号で一面が立たない(監査指摘)。
        # roundup しか残らない号(schema 上は正しい出力)でも一面の無い紙面は出せないので、
        # そのときは roundup を一面に立てる(監査指摘)
        cand = [a for a in arts if a.get("rank") != "roundup"] or list(arts)
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
    taken: set[str] = set()   # slug は号内で一意。面をまたいでコードが付ける
    plan_schemas: dict[str, dict] = {}
    groups = []
    for stage in order:
        present = [b for b in stage if b in by_brand]
        for i in range(0, len(present), wave):
            groups.append(present[i:i + wave])
    for group in groups:
        procs = []
        # 起動は1面ずつ一覧に足し、例外・中断で抜けても finally で起動済みの面を止めて回収する。起動できない面は
        # その面だけの異常終了(start_group が返す LaunchFailed)として下で扱う(2本目の起動の失敗で例外になり、1本目が
        # 回収されずに残った。監査指摘 2026-10-14)
        blocked = None
        try:
            for b in group:
                out = ROOT / "metrics" / f"plan-{date}-{b}.json"
                out.unlink(missing_ok=True)  # 残骸の誤読防止
                prompt = brand_plan_prompt(date, b, len(by_brand[b]), trig_by_brand.get(b, []),
                                           claimed=claimed)
                # **主題ごとに必ず1つの判定**を schema で求める(planlib)。id や slug は書かせない。
                # 以前はファイルに JSON を書かせ、id の写し間違いを別セッションで直し、
                # 漏れを別セッションで拾い直していた(監査指摘 P1-1/P1-2)
                keys = [s["dedup_key"] for s in by_brand[b]]
                schema = planlib.plan_schema(keys, b, [c.get("slug") for c in claimed])
                plan_schemas[b] = schema          # 答えを検めるときに、その面に渡した schema を使う
                procs.append((b, out, start_group(
                    ["claude", "-p", prompt_file(date, f"plan-{b}", prompt), "--model", CLAUDE_MODEL, "--dangerously-skip-permissions",
                     "--json-schema", json.dumps(schema, ensure_ascii=False),
                     "--max-budget-usd", COMPOSE_ARTICLE_MAX_BUDGET_USD], cwd=ROOT)))
            for b, out, p in procs:
                # 時間切れは子も含めて落とし、時間切れまでに出ていた出力を上限つきで回収する(原因を追えるように)
                so, se, timed_out = finish_group(p, remaining_seconds())
                save_raw(date, f"plan-{b}", so, se, p.returncode)
                # 認証切れ・利用上限はその面の計画の失敗ではない(不採用扱いにしない)。この回の子を全部回収してから止める
                got_blocked = cli_stop("claude", f"選定 {b} 面", None if timed_out else p.returncode, so, se)
                if got_blocked:
                    blocked = got_blocked
                    continue
                try:
                    if timed_out or p.returncode != 0:
                        # 途中までの出力・異常終了の間際の出力は判定に使わない(その面の主題は取りこぼしとして拾い直す)
                        raise TimeoutError("時間切れ" if timed_out else f"exit {p.returncode}")
                    text = (so or "").strip()
                    # 同じ主題のキーが2回ある答えは読めない(後の判定で黙って上書きされる。loads_strict。監査指摘)
                    try:
                        ans = loads_strict(text)
                    except json.JSONDecodeError:
                        ans = loads_strict(re.search(r"\{.*\}", text, re.S).group(0))
                    if not isinstance(ans, dict):
                        raise ValueError("答えが JSON の object でない")
                    if not isinstance(ans.get("decisions"), dict):
                        raise ValueError("判定(decisions)が無い")   # 空の判定を「全部不採用」にしない(下で取りこぼしとして拾い直す)
                    # 判定は1主題ずつ schema(planlib.plan_schema)で検め、形の崩れた判定は「判定なし」にする(その主題だけ
                    # 取りこぼしとして拾い直す。崩れた1件で面全体を落とさない・崩れた値で計画を作らない。監査指摘)
                    from jsonschema import Draft202012Validator
                    per_key = plan_schemas[b]["properties"]["decisions"]["properties"]
                    ans["decisions"] = {k: d for k, d in ans["decisions"].items()
                                        if k in per_key and Draft202012Validator(per_key[k]).is_valid(d)}
                    results[b] = planlib.decisions_to_plan(b, by_brand[b], ans["decisions"], taken)
                    out.write_text(json.dumps({"decisions": ans.get("decisions"), "plan": results[b]},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
                except Exception as e:
                    print(f"選定: {b} 面が計画を出せず({type(e).__name__}: {(se or '')[-120:]})。"
                          "この面は不採用扱いで続行", flush=True)
                    continue
                n_a = len(results[b].get("articles") or [])
                n_d = len(results[b].get("dropped") or [])
                # 後続の面へ「もう記事化した話題」として渡す。dedup_key が一致しなくても
                # 同じ話題だと分かるよう、面・切り口・主題キーをまとめて見せる
                for a in results[b].get("articles") or []:
                    claimed.append({k: a.get(k) for k in ("brand", "slug", "dedup_key", "angle")})
                print(f"選定: {b} 面 {len(by_brand[b])}主題 → 記事{n_a}本 / 不採用{n_d}件", flush=True)
        finally:
            stop_groups([p for _, _, p in procs])
        if blocked:
            raise CliBlocked(blocked)
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

    # 同じ wave で並列に選んだ面同士は claimed を見ていない(既定8並列なら通常ブランド7面が
    # 同じ wave)。同じ話題が2面に立ちうるので、dedup_key か素材 id が重なる記事は機械で
    # 1本にする(監査指摘)。残すのは主題の収集時の面に合う記事、どちらも合わなければ先の面。
    # 素材は統合し、消した側は「重複」として不採用に記録する
    subject_brand = {s["dedup_key"]: b for b, rows in by_brand.items() for s in rows}
    merged_away: set[str] = set()
    for i, a in enumerate(arts):
        if a.get("rank") == "roundup" or a["slug"] in merged_away:
            continue
        for b2 in arts[i + 1:]:
            if b2.get("rank") == "roundup" or b2["slug"] in merged_away or b2["brand"] == a["brand"]:
                continue
            same = (a.get("dedup_key") == b2.get("dedup_key")
                    or bool(set(a["candidate_ids"]) & set(b2["candidate_ids"])))
            if not same:
                continue
            keep, drop = a, b2
            if (subject_brand.get(a.get("dedup_key")) != a["brand"]
                    and subject_brand.get(b2.get("dedup_key")) == b2["brand"]):
                keep, drop = b2, a
            keep["candidate_ids"] = keep["candidate_ids"] + [c for c in drop["candidate_ids"]
                                                             if c not in keep["candidate_ids"]]
            merged_away.add(drop["slug"])
            dropped.append({"dedup_key": drop.get("dedup_key"), "reason": "重複",
                            "note": f"{keep['brand']} 面の {keep['slug']} に統合(面をまたぐ重複)"})
            print(f"面をまたぐ重複: {drop['brand']}/{drop['slug']} → {keep['brand']}/{keep['slug']} に統合", flush=True)
            if drop is a:
                break
    arts = [a for a in arts if a["slug"] not in merged_away]
    by_slug = {a["slug"]: a for a in arts}

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
    # 答えは schema で受ける(slug は一覧の中から)。ファイルを書かせない
    slugs = [a["slug"] for a in arts]
    schema = json.dumps({"type": "object", "required": ["lead_slug"], "additionalProperties": False,
                         "properties": {"lead_slug": {"enum": slugs}}}, ensure_ascii=False)
    pick = {}
    try:
        try:
            r = run_group(["claude", "-p", prompt_file(date, "lead", lead_prompt(date, arts)), "--model", CLAUDE_MODEL,
                           "--json-schema", schema, "--dangerously-skip-permissions",
                           "--max-budget-usd", COMPOSE_ARTICLE_MAX_BUDGET_USD],
                          timeout=remaining_seconds(cap=600), cwd=ROOT)
        except subprocess.TimeoutExpired as e:
            save_raw(date, "lead", *partial_output(e))     # 時間切れの回も、それまでの出力を残す
            raise
        save_raw(date, "lead", r.stdout, r.stderr, r.returncode)
        blocked = cli_stop("claude", "一面の選定", r.returncode, r.stdout, r.stderr)
        if blocked:
            raise CliBlocked(blocked)    # 機械のフォールバックで一面を決めて進めない(このあとの執筆・校閲も同じ CLI で止まる)
        if r.returncode != 0:
            raise RuntimeError(f"exit {r.returncode}")
        text = (r.stdout or "").strip()
        pick = loads_strict(text) if text.startswith("{") else loads_strict(re.search(r"\{.*\}", text, re.S).group(0))
        # 渡した schema どおりでない答え({"lead_slug": []} など)は使わず、下の機械のフォールバックへ(監査指摘)
        if not schema_ok(pick, schema):
            raise ValueError("答えが schema のとおりでない")
        (ROOT / "metrics" / f"plan-lead-{date}.json").write_text(json.dumps(pick, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        pick = {}
        print(f"一面の選定セッションが答えを返さなかった({type(e).__name__}: {str(e)[:80]})", flush=True)
    by_slug = {a["slug"]: a for a in arts}
    lead = by_slug.get(pick.get("lead_slug"))
    if lead is None or lead.get("rank") == "roundup":
        # 選定セッションが落ちた場合の機械フォールバック(一面のない紙面は出さない)
        cand = [a for a in arts if a.get("rank") != "roundup"]
        lead = max(cand, key=lambda a: a.get("lead_score") or 0) if cand else None
        print(f"lead 選定が不成立。lead_score 最大の {lead['slug'] if lead else '-'} を一面にする", flush=True)
    if lead is not None:
        lead["rank"] = "lead"
    # 社説は 2026-09-06 号で終了。起点は選ばせない(計画の項目は、残っている社説の経路のために一面で埋めておく)
    plan["editorial_slug"] = (lead or {}).get("slug", "")
    plan["editorial_brand"] = (lead or {}).get("brand", "")


def missing_plan_prompt(date: str, rows: list[dict], existing: list[dict]) -> str:
    """取りこぼした主題**だけ**を見せ、**まずどの面の話か**から判断させるプロンプト。

    面の計画は十数〜三十主題を1セッションで裁くので、たまに1〜2主題が articles にも
    dropped にも現れずに消える(実測 2026-09-06: cg 面 21主題のうち公式ポストが1件)。
    これを「取りこぼし」と通知して終わりにしていたが、それでは主題が消えたまま紙面が出る。

    **面は決めつけない。**主題は収集時に付いた brand で面に配られるが、担当が
    「これは自分の面じゃない」と思ったとき、不採用の理由語彙に「面違い」が無いので
    黙って落とすのがいちばんありそうな経路である(監査指摘)。だから問いは
    「この面でどうするか」ではなく「**この主題はどの面の話で、そこでどうするか**」にする。
    面の役割を持たないセッションに、消えた主題と**全面の既存記事**を見せる。
    """
    ex = json.dumps([{k: a.get(k) for k in ("brand", "slug", "rank", "angle", "dedup_key")}
                     for a in existing], ensure_ascii=False, indent=1)
    return render_prompt("plan-missing", DATE=date, N=len(rows), RULES=plan_rules(), EXISTING=ex)


# 拾い直し(prompts/plan-missing.md「4. 出力」)の1要素の形。形だけを見る(どれを選ぶかの判断はモデル)
# 記事の slug と candidate_ids はコードが付ける(下の replan_missing)ので、答えには求めない
_STR = {"type": "string", "minLength": 1}
MISSING_ROW_SCHEMA = {
    # 目的(purpose)は面別選定と同じ語彙(planlib.PURPOSES)。拾い直しで記事にした期日の知らせを、執筆へ「新情報」で渡さない(監査指摘 2026-10-14)
    "articles": {"type": "object", "required": ["brand", "rank", "angle", "purpose", "lead_score", "dedup_key"],
                 "properties": {"brand": {"enum": sorted(BRANDS)}, "rank": {"enum": ["large", "medium", "small"]},
                                "angle": {"type": "string"}, "purpose": {"enum": list(planlib.PURPOSES)},
                                "lead_score": {"type": "integer", "minimum": 0, "maximum": 100},
                                "dedup_key": _STR}},
    "merge_into": {"type": "object", "required": ["slug", "dedup_key"],
                   "properties": {"slug": _STR, "dedup_key": _STR}},
    "dropped": {"type": "object", "required": ["dedup_key", "brand", "reason"],
                "properties": {"dedup_key": _STR, "brand": {"enum": sorted(BRANDS)}, "note": {"type": "string"},
                               # 依頼文の理由の列挙のとおり(空白・未知の理由の不採用で主題を消さない。監査指摘)
                               "reason": {"enum": ["既報", "過年度", "同人・ファン主催", "個人の話題", "重複", "出典不足", "アイマス外", "その他"]}}},
}


def replan_missing(date: str, plan: dict, by_brand: dict, cands: dict,
                   missing: list[str], wave: int = 0) -> list[str]:
    """取りこぼした主題を**面を決めるところから**問い直し、答えを計画へ機械で反映する。

    1セッションで全部を見る(消えるのは号に1〜2主題なので、面ごとに分ける理由が無い)。
    戻り値はログ行。
    """
    miss = set(missing)
    subjects = [r for rs in by_brand.values() for r in rs if r.get("dedup_key") in miss]
    if not subjects:
        return []
    idx = ROOT / "metrics" / f"plan-index-{date}-missing.json"
    idx.write_text("[\n" + ",\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":"))
                                         for r in subjects) + "\n]\n", encoding="utf-8")
    prior = storylink.prior_lookup(date, ROOT)
    write_event_index({e: prior.event_rows(e) for r in subjects for e in r.get("events", [])}, subjects,
                      ROOT / "metrics" / f"plan-events-{date}-missing.json")
    out = ROOT / "metrics" / f"plan-{date}-missing.json"
    out.unlink(missing_ok=True)
    prompt = missing_plan_prompt(date, subjects, plan.get("articles", []))
    try:
        pr = run_group(["claude", "-p", prompt_file(date, "plan-missing", prompt), "--model", CLAUDE_MODEL,
                        "--dangerously-skip-permissions", "--max-budget-usd", COMPOSE_ARTICLE_MAX_BUDGET_USD],
                       timeout=remaining_seconds(cap=600), cwd=ROOT)
        so, se, code = pr.stdout, pr.stderr, pr.returncode
    except subprocess.TimeoutExpired as e:
        (so, se), code = partial_output(e), None
    # 答えのファイルを書かずに終わったときに理由を追えるよう、標準出力・エラー・答えのファイルを残す
    save_raw(date, "plan-missing", so, se, code,
             files={"answer": read_for_raw(out)})
    blocked = cli_stop("claude", "取りこぼしの拾い直し", code, so, se)
    if blocked:
        raise CliBlocked(blocked)
    try:
        # 採るのは正常に終わったセッションの答えだけ(時間切れの間際に書かれた不採用で主題を消さない)。
        # 3つの欄は、あれば配列(単一の object などは読めない答え。矛盾の検査ができない)。重複キーも読めない(監査指摘)
        if code != 0:
            raise ValueError("正常に終わっていない")
        r = loads_strict(out.read_text(encoding="utf-8"))
        if not isinstance(r, dict) or any(k in r and not isinstance(r[k], list) for k in ("articles", "merge_into", "dropped")):
            raise ValueError("答えの形が崩れている")
    except Exception:
        return ["拾い直しの答えが読めない(取りこぼしは残る)"]

    # 面別選定(planlib)と同じ設計で反映する: モデルの答えは判断だけ。
    # - 要素の形は依頼文(plan-missing)の出力の形の schema で1件ずつ検める(null・欄の欠け・型の崩れた要素で号全体を止めない・
    #   理由の無い不採用で主題を消さない)
    # - candidate_ids は主題の ids からコードが展開する(モデルの写した id は使わない。別の主題の id で記事化して、
    #   この主題を判断済みに見せるのを防ぐ)。slug もコードが付ける(planlib.slugify)
    # - 主題ごとに答えは1つ。2つ以上(同じ主題の記事が2本・記事と不採用の両方など)は反映しない
    # 反映しなかった主題は取りこぼしのまま残り、呼び出し側で異常になる(監査指摘)
    from jsonschema import Draft202012Validator
    # 答えは形を検める**前に**主題キーで数える(崩れた記事化と正しい不採用が同じ主題に来たら、矛盾した答えとして反映しない)
    answers: dict[str, list[tuple[str, dict | None]]] = {}
    for kind in ("articles", "merge_into", "dropped"):
        v = Draft202012Validator(MISSING_ROW_SCHEMA[kind])
        got = r.get(kind)
        for x in (got if isinstance(got, list) else []):
            key = x.get("dedup_key") if isinstance(x, dict) else None
            if isinstance(key, str) and key in miss:
                answers.setdefault(key, []).append((kind, x if v.is_valid(x) else None))
    ids_of = {s["dedup_key"]: [i for i in (s.get("ids") or []) if i in cands] for s in subjects}

    log: list[str] = []
    slugs = {a.get("slug") for a in plan.get("articles", [])}
    by_slug = {a.get("slug"): a for a in plan.get("articles", [])}
    for key, got in answers.items():
        if len(got) != 1:
            log.append(f"{key} → 答えが {len(got)}つあり反映しない(取りこぼしのまま)")
            continue
        kind, x = got[0]
        if x is None:
            log.append(f"{key} → 答えの形が崩れていて反映しない(取りこぼしのまま)")
            continue
        ids = ids_of.get(key) or []
        if kind == "articles":
            if not ids:
                continue
            b = x["brand"]
            a = {"slug": planlib.slugify(b, key, slugs), "brand": b, "rank": x["rank"], "angle": x["angle"],
                 "purpose": x["purpose"], "lead_score": x["lead_score"], "dedup_key": key, "candidate_ids": ids}
            plan["articles"].append(a)
            log.append(f"{key} → {b} 面で記事化({a['slug']})")
        elif kind == "merge_into":
            tgt = by_slug.get(x["slug"])
            if not tgt or not ids:
                continue
            for i in ids:
                if i not in tgt["candidate_ids"]:
                    tgt["candidate_ids"].append(i)
            log.append(f"{key} → {tgt.get('brand')} 面の {tgt['slug']} へ統合")
        else:
            plan.setdefault("dropped", []).append(x)
            log.append(f"{key} → 不採用({x['brand']} 面 / {x['reason']})")
    return log


def coverage_gaps(plan: dict, cands: dict, blocklist: dict) -> tuple[list[str], dict]:
    """主題の取りこぼし検査(編集規程11)。素材に現れた dedup_key が articles にも
    dropped にも現れないなら、それは「判断されずに消えた」主題である。

    発行はブロックしない。**ただし通知で終わらせない。**消えた主題は
    `replan_missing` でその面の担当に突き返し、判断させてから先へ進む。
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
    stats = {"subjects": len(usable), "covered": len(usable) - len(missing),
             "missing": len(missing), "missing_keys": missing}
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


def yaml_dump_keeping_strings(data) -> str:
    """frontmatter の再ダンプで**文字列の字面を壊さない**。

    PyYAML は未引用の `yes/on/off` を bool、`0012` を整数、`1:20` を整数(六十進)、
    日付形を date として読む。見出し・tags・出典 label にそういう文字列があると、
    素の safe_dump → safe_load の往復で型が変わる(監査指摘)。読み戻して同じ文字列に
    ならない値だけを引用符付きで書く。
    """
    class Quoted(str):
        pass

    def rep(dumper, s):
        return dumper.represent_scalar("tag:yaml.org,2002:str", str(s), style='"')

    yaml.add_representer(Quoted, rep, Dumper=yaml.SafeDumper)

    def wrap(v):
        if isinstance(v, str):
            try:
                back = yaml.safe_load(v)
            except Exception:
                back = None
            return v if (isinstance(back, str) and back == v) else Quoted(v)
        if isinstance(v, list):
            return [wrap(x) for x in v]
        if isinstance(v, dict):
            return {k: wrap(x) for k, x in v.items()}
        return v

    # ブロック形式に固定する(None だと短い出典が `- {label: …, type: …}` の1行になり、過去号と
    # 形が違って retag_sources が type 行を数えられなかった。実測 2026-09-12)
    return yaml.safe_dump(wrap(data), allow_unicode=True, sort_keys=False, width=200,
                          default_flow_style=False)


def earliest_date(raw: str) -> str | None:
    """文字列に含まれる日付のうち**いちばん早い**ものを YYYY-MM-DD で返す(無ければ None)。

    範囲(2026-09-12〜13)は開始日、配列は最も早い日。和暦(令和8年9月12日)も読む。
    暦に無い日(2026-02-30)は捨てる。最初に見つかった日付を採ると「9月13日〜12日」のような
    書き方で終了日を拾う(監査指摘)
    """
    raw = (raw or "").translate(str.maketrans("０１２３４５６７８９／．－", "0123456789/.-"))
    found = []
    for m in re.finditer(r"(?:(\d{4})|令和\s*(\d{1,2}|元))\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", raw):
        year = int(m.group(1)) if m.group(1) else (2018 + (1 if m.group(2) == "元" else int(m.group(2))))
        try:
            found.append(datetime.date(year, int(m.group(3)), int(m.group(4))))
        except ValueError:
            continue
    return min(found).isoformat() if found else None


def canonicalize_article(date: str, art: dict, cands: dict) -> list[str]:
    """記事 frontmatter の**固定項目を計画からコードで上書きする**。戻り値はログ。

    以前は執筆セッションが転記した slug/edition/brand/candidate_ids が計画と食い違うと、
    別の Claude セッションに「直せ」と頼んでいた(最大600秒。直らなければ記事ごと落とす)。
    正解は全部計画側にあるので、写す係にモデルを使う理由がない(監査指摘 P0-1)。
    出典の種別も判定表で決まる(src は最弱)。label が無ければ候補の題名で埋める。
    モデルから受け取るのは本文・見出し・リード・出典 URL・tags・event_date だけ。
    """
    path = ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return []
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return []
    body = text[m.end():]
    log = []
    for key, want in (("slug", art["slug"]), ("edition", date), ("brand", art["brand"])):
        if str(fm.get(key)) != str(want):
            log.append(f"{key}: {fm.get(key)!r} → {want!r}")
            fm[key] = want
    if sorted(fm.get("candidate_ids") or []) != sorted(art["candidate_ids"]):
        log.append("candidate_ids を計画に合わせた")
        fm["candidate_ids"] = list(art["candidate_ids"])
    # 新規記事の訂正欄は固定値。初稿が corrected: true や架空の訂正を書いても残さない(監査指摘)
    if fm.get("corrected") or fm.get("corrections"):
        log.append("corrected/corrections を初期値に戻した")
    fm["corrected"] = False
    fm["corrections"] = []
    fm.setdefault("rank", art.get("rank") or "small")
    # event_date は YYYY-MM-DD 1つだけ。執筆は範囲(2026-09-12〜13)・配列・和暦で書いてくる。
    # 以前は lint の赤を Claude が直していたので見えていなかった(実測 2026-09-12: 5本が
    # schema 違反になり、その記事を指す digest 行まで「記事が無い」扱いで lint 赤)。
    # 最初の日付を採り、日付が取れなければ欄ごと外す(任意項目)
    ev = fm.get("event_date")
    if ev is not None:
        raw = " ".join(str(x) for x in ev) if isinstance(ev, list) else str(ev)
        norm = earliest_date(raw)
        if norm != (str(ev) if isinstance(ev, str) else None):
            log.append(f"event_date: {raw[:30]!r} → {norm!r}")
        if norm:
            fm["event_date"] = norm
        else:
            fm.pop("event_date", None)
    title_by_url = {c.get("url"): c.get("title") for i in art["candidate_ids"] for c in [cands.get(i) or {}] if c.get("url")}
    srcs = []
    for s in fm.get("sources") or []:
        if not isinstance(s, dict) or not s.get("url"):
            # 黙って捨てない。弱い出典の url の転記ミスで、強い出典だけが残ると
            # 過大表示になる(監査指摘)。捨てた事実をログに残す
            log.append(f"url の無い出典を捨てた({str(s)[:60]})")
            continue
        t = classify_source(s["url"])
        if s.get("type") != t:
            log.append(f"出典種別: {s.get('type')} → {t}({s['url'][:50]})")
        s["type"] = t
        if not s.get("label"):
            s["label"] = title_by_url.get(s["url"]) or urllib.parse.urlparse(s["url"]).hostname or s["url"]
            log.append("出典 label を補った")
        srcs.append({"label": s["label"], "url": s["url"], "type": s["type"]})
    # 無条件に置き換える。空のときに元の不正な sources を残すと lint 赤から戻れない(監査指摘)
    fm["sources"] = srcs
    if srcs:
        src = weakest_src(s["type"] for s in srcs)
        if fm.get("src") != src:
            log.append(f"src: {fm.get('src')} → {src}")
            fm["src"] = src
    else:
        log.append("出典が1つも残らない(記事は不成立として落とす)")
    order = ["slug", "edition", "brand", "src", "rank", "corrected", "corrections", "candidate_ids",
             "title", "lede", "tags", "sources", "event_date"]
    ordered = {k: fm[k] for k in order if k in fm}
    ordered.update({k: v for k, v in fm.items() if k not in ordered})
    head = yaml_dump_keeping_strings(ordered)
    write_checked(path, "---\n" + head + "---\n" + body)
    return log


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
    if not fm.get("sources"):
        # 正規化で url の無い出典を捨てた結果、出典が残らなかった記事は成立しない
        errors.append("出典が1つも無い")
    return errors


def body_length(path: Path) -> int:
    """本文の非空白文字数(中見出し行は除く)。lint の分量計算と同じ定義。"""
    text = path.read_text(encoding="utf-8")
    body = renderlib.strip_fact_notes(re.split(r"\n---\n", text, maxsplit=1)[-1])
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

    **一面も落とす。**以前は「lead_slug と digest が一面を指しているので機械で抜くと
    組版と食い違う」として落とさず、人へ回していた。だが呼び出し側は落としたあとに
    必ず組版をやり直すので、食い違いは起きない。一面を残す運用は、一面1本の指摘で
    号ごと止まる結果になった(実測 2026-09-07: ファミ通の時刻が公式と食い違う、
    という一面の指摘1件で発行中止)。落として、残りから lead_score 最大を一面に立てる。
    """
    dropped, unresolved = [], []
    by_slug = {a["slug"]: a for a in written}
    issues_of = collections.defaultdict(list)
    for b in review.get("blockers") or []:
        issues_of[Path((b.get("file") or "").strip()).name].append(b)
    for b in review.get("blockers") or []:
        f = (b.get("file") or "").strip()
        issue = (b.get("issue") or "")[:120]
        name = Path(f).name
        if not f.startswith("docs/_posts/") or not name.startswith(date + "-"):
            unresolved.append(f"{f or '(対象不明)'}: {issue}")
            continue
        slug = name[len(date) + 1:].removesuffix(".md")
        p = ROOT / "docs" / "_posts" / name
        if slug not in dropped:
            log_drop(date, slug, "校閲ブロック", f"{b.get('rule_id') or ''} {issue}".strip(), p, issues=issues_of[name])
        if p.exists():
            p.unlink()
        if slug not in dropped:
            dropped.append(slug)
    written[:] = [a for a in written if a["slug"] not in dropped]
    restand_lead(date, written)
    return dropped, unresolved


def restand_lead(date: str, written: list[dict]) -> str | None:
    """**紙面に一面が無ければ、残りの記事から立て直す。**立てた記事の slug を返す(一面があれば何もしない)。

    一面は校閲ブロックでも、書き直しの見送り(執筆側が不成立と判断)でも、lint 赤の落としでも外れる。
    立て直しを校閲ブロックの経路だけに置いていたため、書き直しで一面が見送られた号は一面0本のまま組版され、
    lint が赤くなった(2026-10-16号: 一面の 765×961 ライブの記事が書き直しで NOT_NEWS になった)。
    枠を当て直す場所(assign_ranks)で必ず通す。pick_lead と同じく roundup だけを除外する"""
    if not written or any(a.get("rank") == "lead" for a in written):
        return None
    cand = [a for a in written if a.get("rank") != "roundup"] or written
    top = max(cand, key=lambda a: a.get("lead_score") or 0)
    top["rank"] = "lead"
    p = ROOT / "docs" / "_posts" / f"{date}-{top['slug']}.md"
    if p.exists():
        txt = p.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", txt, re.S)
        if m:
            head = re.sub(r"^rank:.*$", "rank: lead", m.group(1), count=1, flags=re.M)
            write_checked(p, "---\n" + head + "\n---\n" + txt[m.end():])
    print(f"一面が落ちたので {top['slug']} を一面に立てる(lead_score 最大)", flush=True)
    return top["slug"]


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
    if keep_lead:
        # 一面を動かさない当て直しでも、一面が紙面から外れていたら立て直す(外す経路は校閲ブロックだけではない)
        top = restand_lead(date, [a for a in written if (ROOT / "docs" / "_posts" / f"{date}-{a['slug']}.md").exists()])
        if top and top in by_slug:
            by_slug[top]["rank"] = "lead"
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
        write_checked(p, re.sub(r"^rank: .*$", f"rank: {rank}", txt, count=1, flags=re.MULTILINE))
    lens = sorted(n for _, n, _ in sized)
    if lens:
        print(f"枠割り: {dict(stats)} / 本文 中央値{lens[len(lens)//2]}字 "
              f"最短{lens[0]}字 最長{lens[-1]}字", flush=True)
    return plan


WRITE_OUTCOMES: dict[str, dict[str, str]] = {}   # 日付 → {slug: 書けなかった理由(見送り/落とした)}
# 日付 → {slug: (見出し, どの段で, なぜ)}。紙面から外れた記事の**概略**。slug だけ並べても人には何の記事か
# 分からない(編集長の指摘 2026-09-23)。落とす前に見出しを控え、完了通知に載せる
DROP_LOG: dict[str, dict[str, tuple[str, str, str]]] = {}
# 日付 → {slug: {"rule": 規則, "dup_of": [重複の相手]}}。外した理由が話題そのものに掛かる記事(log_drop が控え、persist_drops が
# 外れた記事の記録の settled に写す)
DROP_SETTLED: dict[str, dict[str, dict]] = {}


def log_drop(date: str, slug: str, stage: str, why: str, path: Path | None = None,
             issues: list[dict] | None = None) -> None:
    """紙面から外す記事の概略を控える。**ファイルを消す前に**呼ぶ(見出しはファイルから取る)。
    issues は外す理由になった校閲の指摘。話題そのものに掛かる規則(SETTLED_DROP_RULES: 重複)なら、その規則と
    重複の相手を DROP_SETTLED に控える(組み直しが書き直さない・救済が戻さない印。pipelib.settled_drops)。"""
    title = ""
    p = path or (ROOT / "docs" / "_posts" / f"{date}-{slug}.md")
    fm = parse_front_matter(p) if p.exists() else None
    if fm:
        title = str(fm.get("title") or "")
    DROP_LOG.setdefault(date, {})[slug] = (title, stage, why[:160])
    settled = DROP_SETTLED.setdefault(date, {})
    settled.pop(slug, None)
    for b in issues or []:
        rule = str(b.get("rule_id") or "")
        if rule not in SETTLED_DROP_RULES:
            continue
        # 重複の相手は issue に slug で書かれる(review-paper.md の P1)。紙面の記事のパスで書かれても slug に直す。
        # slug らしい語を全部控え、相手かどうかは紙面にその記事があるかで決める(settled_drops)
        dup = sorted({t.removeprefix(f"{date}-") for t in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(b.get("issue") or ""))}
                     - {slug, date})
        if dup:
            settled[slug] = {"rule": rule, "dup_of": dup}
            break


# 日付 → {slug: 直前の書き直しを機械の検査が戻した理由("" なら戻していない)}。校閲の依頼文に載せる
REVISE_NOTES: dict[str, dict[str, str]] = {}
# 日付 → {slug: 直前の書き直しが指摘の外も変えた箇所}。採ったうえで、次の巡の校閲に確かめさせる(revise_apply)
OFFSCOPE_NOTES: dict[str, dict[str, str]] = {}

# 日付 → **書き手が直せなかった**指摘の記録。書き直しをはさんでも同じ記事に同じ規則の指摘が残った / 機械検算を
# 差し戻しても通らなかった。書き手が直せないのは、たいてい書き手ではなく契約(執筆 schema・依頼文・検算・校閲規則)の欠陥
# (実測 2026-09-26: 出典 label の schema 上限 80 字で告知タイトルが切れ、校閲 R16 が3巡ブロックして記事を落とした。
# 人には「校閲ブロックで落とした」とだけ届いた)。人に「落とした」と申告する前に当番(Opus 修正 → Sol 査読)が診断する
# (編集長:「なんでできねーんだ、とキレろ」「申告前に診断しろ」「Opus/Sol 系に落とせない設計がカス」)
STUCK: dict[str, list[dict]] = {}


def note_stuck(date: str, kind: str, file: str, rule_id: str, issue: str, rounds: int) -> None:
    STUCK.setdefault(date, []).append({"kind": kind, "file": file, "rule_id": rule_id, "issue": (issue or "")[:300], "rounds": rounds})


def same_issue_left(b: dict, prev_quotes: list[str] | None) -> bool:
    """前の巡で書き直しに回した同じ記事・同じ規則の指摘(の quote 群)と、今の巡の指摘 b が**同じ箇所**か。

    規則が同じでも別の記述への指摘(前の巡で見落とされ、今の巡で初めて出たもの)は「直せなかった」ではない
    (実測 2026-10-10 shiny-ops-roundup: 1巡目の R1 は STEP の確定内容、2巡目の R1 は「投票企画」で、
    1巡目の指摘は直っていたのに「2巡目まで残った」と通知した)。quote が8字以上の字面を共有するときだけ同じとみる。
    quote が無い・短いときは場所を比べられないので同じとみる(見逃して申告しないより、診断に回す)。"""
    if prev_quotes is None:
        return False
    q = renderlib.visible_text(b.get("quote"))
    olds = [renderlib.visible_text(x) for x in prev_quotes]
    if len(q) < 8 or any(len(o) < 8 for o in olds):
        return True
    return any(difflib.SequenceMatcher(None, q, o, autojunk=False).find_longest_match(0, len(q), 0, len(o)).size >= 8
               for o in olds)


def stuck_summary(date: str) -> str:
    """通知と当番の台帳に載せる「書き直しても直らなかった指摘」の一覧。無ければ空。"""
    rows = STUCK.get(date) or []
    if not rows:
        return ""
    return (f"\n書き直しても直らなかった指摘 {len(rows)}件(書き手ではなく契約=schema・依頼文・検算の欠陥の疑い):\n"
            + "\n".join(f"- [{r['kind']}] {Path(r['file']).name} {r['rule_id']}({r['rounds']}巡目まで残った): {r['issue'][:200]}" for r in rows))


def carry_usable(c, date: str) -> bool:
    """--carry-review で引き継げる校閲結果か: この号の承認(verdict approve で、号スナップショットの指紋を持つ)。"""
    return (isinstance(c, dict) and c.get("verdict") == "approve" and isinstance(c.get("hashes"), dict)
            and f"docs/_editions/{date}.md" in c["hashes"])


def carry_targets(date: str, carried: dict) -> list[str]:
    """承認を引き継ぐ組み直しで校閲する記事: 承認時と中身の指紋が違う記事(今回書いた記事・組み直しの途中で変わった記事)。"""
    from pipelib import review_manifest
    return sorted(k.split("/")[-1] for k, h in review_manifest(date).items()
                  if k.startswith("docs/_posts/") and carried["hashes"].get(k) != h)


def carry_kept(date: str, carried: dict) -> list[str]:
    """承認を引き継ぐ組み直しで、承認時から中身が変わっていない記事(承認済みのまま残す記事)。carry_targets の裏。"""
    from pipelib import review_manifest
    return sorted(k.split("/")[-1] for k, h in review_manifest(date).items()
                  if k.startswith("docs/_posts/") and carried["hashes"].get(k) == h)


def hand_over_anomalies(date: str, rerun: bool, reason: str = "", extra_args: list[str] | None = None) -> None:
    """号の終わりに、外れた記事と直らなかった指摘を異常の台帳に積み、台帳ごと当番へ渡す(なぜなぜ)。
    人に「落とした」「決まらない」と申告するだけで終えない(編集長 2026-09-26「すべてのエラーがなぜ起きたかなぜなぜしろ」)。"""
    anomaly("compose", drop_summary(date))
    anomaly("compose", stuck_summary(date))
    diagnose_anomalies("compose", date, rerun=rerun, reason=reason, extra_args=extra_args)


def recover_pending(date: str) -> bool:
    """この号で外れた記事に、戻す組み直し(--recover)で書き直せば戻りうるものがあるか。外した理由がいまも成り立つ記事
    (重複の相手が紙面にある。settled_drops)だけなら、組み直しは何も書き直さないので当番に戻させない。
    外れた記事の記録(persist_records)を書いたあとに呼ぶ。"""
    settled = settled_drops(date, ROOT)
    return any(s not in settled for s in DROP_LOG.get(date) or {})


def drop_summary(date: str) -> str:
    """完了通知に載せる、外れた記事の一覧(見出し・段・理由)。"""
    rows = DROP_LOG.get(date) or {}
    if not rows:
        return ""
    return "\n紙面から外れた記事 " + f"{len(rows)}本:\n" + "\n".join(
        f"- {t or slug}({stage}): {why}" for slug, (t, stage, why) in rows.items())


def returned_note(problems: list[str]) -> str:
    """差し戻しの追記: 機械検算で落ちた点だけを示し、同じ契約で書き直させる。"""
    return ("\n\n## 差し戻し(機械検算の不合格。この点だけ直して、同じ形の JSON を返す)\n"
            + "\n".join(f"- {p}" for p in problems[:8])
            + "\n記事として成立しないと判断するなら status を decline にし、理由コードと説明を書く。")


def _late_article(date: str, art: dict, outcomes: dict, aborted: list) -> None:
    """締切を過ぎて執筆を始めなかった記事(差し戻し・やり直しを含む)を、理由付きで外れた記事に記録する(後始末・当番へ流れる)。
    締切後も後続の記事の担当を起動していた(監査指摘 2026-10-14)。打ち切りは締切だけで、締切前は今どおり全部書く"""
    outcomes[art["slug"]] = "落とした(締切を過ぎ、執筆を始めなかった)"
    print(f"記事 {art['slug']} を{outcomes[art['slug']]}", flush=True)
    if art["slug"] not in aborted:
        aborted.append(art["slug"])
    DROP_LOG.setdefault(date, {})[art["slug"]] = (str(art.get("angle") or ""), "締切で未着手", outcomes[art["slug"]][4:])


def fetch_record(date: str, slug: str) -> Path:
    """記事ごとの、fetch_page.py が本文を取れたページの記録(pipelib.note_fetched。Git 管理外)。初稿・差し戻し・書き直しの
    セッションが同じ記録に書き足す(書き直しは、初稿のセッションが読んだページの事実を verified_facts から写して使う)。"""
    return ROOT / "metrics" / "work" / date / "fetched" / f"{slug}.jsonl"


FETCH_RECORD_BROKEN: set[str] = set()     # 置き場を作れなかった記録のフォルダ(同じ障害を記事ごとに重ねて申告しない)


def fetch_env(date: str, slug: str) -> dict:
    """執筆・書き直しの子に渡す環境。子が走らせた fetch_page.py が、この記事の記録に書き足す。
    置き場を作れなくても工程は止めない(例外を外へ出さない。2026-10-14 監査指摘: フォルダの作成の失敗で執筆ごと止まった)。
    記録は空のまま検算へ進み(素材の外のページは根拠にできないので、差し戻し・元の稿のまま)、障害は異常として申告する"""
    rec = fetch_record(date, slug)
    try:
        rec.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        if str(rec.parent) not in FETCH_RECORD_BROKEN:
            FETCH_RECORD_BROKEN.add(str(rec.parent))
            anomaly("compose", f"fetch_page.py の記録の置き場 {rec.parent} を作れない({type(e).__name__}: {str(e)[:120]})。"
                               "執筆・書き直しは素材の外のページを根拠にできず、差し戻し・元の稿のままになる")
    return {**os.environ, FETCH_RECORD_ENV: str(rec)}


def draft_urls(fm: dict | None) -> set[str]:
    """現在の稿の verified_facts・sources の url(書いたときに検算を通ったもの)。書き直しが verified_facts を写して使うとき、
    初稿のセッションで読んだページを読み直させない。"""
    return {str(x["url"]) for k in ("verified_facts", "sources") for x in (fm or {}).get(k) or [] if isinstance(x, dict) and x.get("url")}


def write_articles(date: str, plan: dict, cands: dict, triggers: list[dict],
                   stories: dict, wave: int = 0,
                   reuse: bool = False) -> tuple[list[dict], list[str]]:
    """記事ごとに素材を機械的に切り出して個別 codex セッションで執筆(wave 並列)。

    wave=0 なら COMPOSE_WAVE(.env)を使う。
    校閲(claude)とベンダーを分離するため執筆は Codex。機械検収エラーの修正は
    校閲と同じ Claude(REVIEW_MODEL=haiku)で行う。

    reuse=True(--reuse-plan)のときは、既に書けている記事を再執筆しない。
    執筆層だけを直して落ちた記事を拾い直す、という再試行を安く保つため。
    前の実行で外した理由が話題そのものに掛かり、いまも成り立つ記事(重複の相手が紙面にある。settled_drops)も書き直さない。
    書き直しても同じ指摘で落ちるか、承認済みの記事との重複をまた作るだけ(実測 2026-10-12: 救済の組み直しが重複で外した
    上映会記事を書き直し、承認済みの記事と同じ重複がまた出た)。
    """
    wave = wave or COMPOSE_WAVE
    trig_by_key = {t["dedup_key"]: t for t in triggers}
    prior = storylink.prior_lookup(date, ROOT)
    jobs, written_before = [], []
    settled = settled_drops(date, ROOT) if reuse else {}
    for art in plan["articles"]:
        if reuse and (ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md").exists():
            written_before.append(art)
            continue
        if art["slug"] in settled:
            print(f"記事 {art['slug']} は書き直さない(前の実行で外した理由がいまも成り立つ: {settled[art['slug']]})", flush=True)
            continue
        materials = [with_current_type(cands[cid]) for cid in art["candidate_ids"]]
        # src も**申告ではなく判定表**から出す。素材の source_type には収集時点の
        # 古い申告が残っており、そのまま渡すと執筆が写して lint が毎回赤くなる
        src = weakest_src(classify_source(c.get("url", "") or "") for c in materials)
        dks = (storylink.prior_keys(materials) | {art.get("dedup_key")}) - {None}
        facts = [f for dk in dks if dk in stories for f in stories[dk]]
        # 既報には、この話題の過去の記事が**読者に出した本文**も渡す(台帳の要約だけだと、書いた価格・店舗をまた書く。2026-10-01)。
        # 同じ一次情報・同じ催しの別の話題の記事も添える(選定と同じ範囲。催しの記事は全部。2026-10-16号の 765×961 ライブの出演者)
        facts += [f"{p['edition']}号「{p['title']}」の本文{'(同じ催しの別の発表の記事)' if p.get('related') else ''}: {p['text']}"
                  for p in prior(dks, [c.get("url") for c in materials])]
        # 構造化モード: 素材の事実に id を振り、執筆は JSON を返す(ファイルはコードが作る)
        mats_in, fact_by_id = (renderlib.materials_with_ids(materials) if STRUCTURED_WRITE else (materials, {}))
        jobs.append((art, src, article_prompt(date, art, mats_in, facts, trig_by_key.get(art.get("dedup_key"))),
                     fact_by_id, materials))
    written, aborted = list(written_before), []
    if written_before:
        print(f"既存の記事 {len(written_before)}本は再執筆しない(--reuse-plan)", flush=True)
    schema_out = ROOT / "schema" / "article-out.schema.json"
    WRITE_OUTCOMES[date] = outcomes = {}
    DROP_LOG.setdefault(date, {})
    # 執筆の結果は3種類で、名前を分ける(「不成立」で一括りにしない。編集長の指摘):
    #   見送り   = 執筆側の判断(記事として成立しない。理由コード付き)。落ちるのが正しい
    #   差し戻し = 機械検算の不合格。**その点だけを示して1回やり直させる**(例外で終わらせない)
    #   落とした = 差し戻しても通らなかった・出力が読めなかった。理由を記録して紙面から外す
    queue = [(art, src, prompt, fact_by_id, materials, 0) for art, src, prompt, fact_by_id, materials in jobs]
    unreadable: set[str] = set()   # 出力が読めず、同じ依頼でもう1回書かせた記事(実行の失敗のやり直しは1回だけ)
    while queue:
        batch, queue = queue[:wave], queue[wave:]
        # 子は本体の作業ツリー(cwd=ROOT)で動き、この号のほかの記事(--reuse-plan・--recover の組み直しでは承認済みの記事)を
        # 直接書き換え・作り・消しうる。稿は答えの JSON からコードが書くので、子の直接の書き込みは採らない: この号の記事を控え、
        # 子が全部終わったら(異常終了・時間切れ・答えの不採用を問わず)書かせる記事以外を控えへ戻し、子が作った記事を消す。
        # 書かせる記事は従来どおり答えから書く(2026-10-14 監査: 書き直し(revise_articles)と同じ型の残り)
        kept_posts = posts_snapshot(date)
        procs, waited, cannot = [], [], []
        # 起動は1本ずつ一覧に足し、全員の終わり(時間切れは child_guard に SIGTERM を先に送って片付けさせる)を待ってから答えを読む。
        # 例外・中断で抜けても finally で起動済みの担当を止めて回収し、子孫の記録(ChildTracker)からも止める(置き去りの担当が、
        # 後始末のあとに記事を書かない)
        with guarded_batch(date, kept_posts, "執筆") as tracker:
            for art, src, prompt, fact_by_id, materials, tries in batch:
                if deadline_passed():
                    _late_article(date, art, outcomes, aborted)
                    continue
                fd, out_name = tempfile.mkstemp(prefix=f"codexwrite-{art['slug']}-", suffix=".txt")
                os.close(fd)
                out_path = Path(out_name)
                cmd = ["codex", "exec", "-m", CODEX_WRITE_MODEL, "-s", "workspace-write",
                       # 既定では sandbox が通信を遮断する。これが無いと出典照合が
                       # 実行できず、収集段階の誤りがそのまま紙面に出る
                       "-c", "sandbox_workspace_write.network_access=true",
                       "--output-last-message", str(out_path)]
                if STRUCTURED_WRITE:
                    cmd += ["--output-schema", str(schema_out)]
                # 素材と指示はファイルで渡す(引数に詰めると 128KB で落ちる。エージェントは自分で読める)。
                # 進行ログ(標準出力・エラー)は一時ファイルで受けて生の出力に残す(最終メッセージを残さず終わった回の原因を追うため)
                short = prompt_file(date, f"write-{art['slug']}-{tries}", prompt)
                with open(log_of(out_path), "w", encoding="utf-8") as lf:
                    if not deadline_passed():       # 準備のあいだに締切を過ぎることもあるので、起動の直前にも確かめる
                        # child_guard 越しに起動し、印は答えのファイル名(子ごとに一意)。子の fetch_page.py が本文を取れたページは、
                        # この記事の記録に残る(fetch_env。検算が根拠の URL と照らす)
                        p = start_group(guarded(cmd + [short], out_path), cwd=ROOT,
                                        env=child_env(out_path.name, fetch_env(date, art["slug"])),
                                        stdout=lf, stderr=subprocess.STDOUT)
                        procs.append((art, src, prompt, out_path, fact_by_id, materials, tries, p))
                        tracker.add(p, out_path.name)
                        continue
                out_path.unlink(missing_ok=True)
                log_of(out_path).unlink(missing_ok=True)
                _late_article(date, art, outcomes, aborted)
            for job in procs:
                se, timed_out = finish_guarded(job[-1], remaining_seconds(), tracker)   # CLI と道具の実行を止めさせてから答えを読む
                if not guard_result(job[3], f"執筆 {job[0]['slug']}"):
                    cannot.append(job[0]["slug"])
                waited.append((*job, se, timed_out))
        targets = {ROOT / "docs" / "_posts" / f"{date}-{w[0]['slug']}.md" for w in waited}
        back = restore_posts(date, kept_posts, skip=targets)
        if back:
            print(f"執筆の子が直接書き換えた記事を控えへ戻した(稿は答えから書く): {', '.join(back[:8])}", flush=True)
        if cannot:
            # CLI を起動できない: 直接起動したときと同じく例外で止める。答えを当てないので、この束ねで書かせる記事(ほかの子が
            # 直接書いたもの)も残さない
            for t in targets:
                t.unlink(missing_ok=True)
            for w in waited:
                w[3].unlink(missing_ok=True)
                take_log(w[3])
            raise FileNotFoundError(f"執筆の子の CLI を起動できない: {', '.join(cannot)}")
        blocked = None
        for art, src, prompt, out_path, fact_by_id, materials, tries, p, se, timed_out in waited:
            out = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
            out_path.unlink(missing_ok=True)
            # 進行ログに、起動できなかった理由・時間切れの印(finish_group の標準エラー)も添える
            log = take_log(out_path) + se
            save_raw(date, f"write-{art['slug']}-{tries}", "", log, p.returncode, files={"last-message": out})
            # 認証切れ・利用上限は記事の失敗ではない: 読めなかった回(unreadable)にも差し戻しにも数えず、落とさない。
            # この回の子を全部回収してから工程を止める(記事は組み直しでそのまま書かれる)
            got_blocked = cli_stop("codex", f"執筆 {art['slug']}({tries + 1}回目の依頼)", None if timed_out else p.returncode, log, out)
            if got_blocked:
                blocked = got_blocked
                if STRUCTURED_WRITE:     # 書きかけを残さない(組み直しは「ファイルがある記事」を書けたとみなす)
                    (ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md").unlink(missing_ok=True)
                continue
            target = ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md"
            if STRUCTURED_WRITE:
                # 判断と文章を JSON で受け、検算して、コードがファイルを作る(renderlib)。
                # 形は schema、中身(事実 id・出典・タグ・日付)はここで検める
                target.unlink(missing_ok=True)   # モデルが勝手に書いたファイルは使わない
                try:
                    # 採るのは**正常に終わったセッションの、渡した schema どおりの答えだけ**(時間切れ・異常終了の間際に
                    # 書かれた答え、欄の型の崩れた答えは読めない出力。崩れた欄で書き出しが止まる・{} の理由で見送りになる。監査指摘)
                    if timed_out or p.returncode != 0:
                        raise ValueError(f"正常に終わっていない({'時間切れ' if timed_out else f'exit {p.returncode}'})")
                    ans = loads_strict(out.strip())
                    if not schema_ok(ans, json.loads(schema_out.read_text(encoding="utf-8"))):
                        raise ValueError("答えが出力 schema のとおりでない")
                    problems = None
                except Exception:
                    # **出力が読めないのは実行の失敗で、検算の不合格(書いた中身の問題)とは別。**差し戻しの1回を使わず、
                    # 同じ依頼でもう1回だけ書かせる。2回とも読めなければ落とし、実行の失敗として上げる(書き手の契約の欠陥として
                    # 記録しない)。以前は検算の不合格と同じに扱い、空の出力や時間切れで差し戻しを使い切って記事を落としていた
                    # (2026-10-06 の洗い出しで判明)
                    if art["slug"] not in unreadable:
                        unreadable.add(art["slug"])
                        print(f"記事 {art['slug']}: 出力が読めない(exit {p.returncode})。同じ依頼でもう1回書かせる", flush=True)
                        queue.append((art, src, prompt, fact_by_id, materials, tries))
                        continue
                    outcomes[art["slug"]] = f"落とした(執筆の出力が2回とも読めない。exit {p.returncode})"
                    print(f"記事 {art['slug']} を{outcomes[art['slug']]}", flush=True)
                    aborted.append(art["slug"])
                    DROP_LOG.setdefault(date, {})[art["slug"]] = (str(art.get("angle") or ""), "執筆の実行の失敗", outcomes[art["slug"]][4:])
                    anomaly("compose", f"記事 {art['slug']}: 執筆の出力が2回とも読めなかった(空・時間切れ・JSON でない)")
                    continue
                if problems is None:
                    # 検算を status の分岐より**先に**掛ける(理由の無い見送りを素通りさせない。監査指摘)
                    try:
                        problems = renderlib.check_output(ans, fact_by_id, materials, rank=art.get("rank") or "",
                                                          edition=date, fetched=fetched_urls(fetch_record(date, art["slug"])))
                    except Exception as e:
                        # 中の要素の形が崩れた答え(null の段落など)で検算が例外になっても、号全体を止めない。
                        # 検算の不合格として差し戻しへ回す(監査指摘)
                        problems = [f"答えの形が崩れていて検算できない({type(e).__name__}: {str(e)[:80]})"]
                if not problems and ans.get("status") == "decline":
                    outcomes[art["slug"]] = f"見送り: {ans.get('decline_code')} {str(ans.get('decline_detail') or '')[:120]}"
                    print(f"記事 {art['slug']} は{outcomes[art['slug']]}", flush=True)
                    aborted.append(art["slug"])
                    # 記事は無いので、見出しの代わりに計画の切り口を残す
                    DROP_LOG.setdefault(date, {})[art["slug"]] = (str(art.get("angle") or ""), "執筆で見送り", outcomes[art["slug"]][4:].strip()[:160])
                    continue
                if problems:
                    if tries == 0:
                        # 差し戻し: 落ちた点だけを示して、同じ契約でもう一度だけ書かせる
                        print(f"記事 {art['slug']} を差し戻し: " + " / ".join(problems[:4]), flush=True)
                        queue.append((art, src, prompt + returned_note(problems), fact_by_id, materials, 1))
                    else:
                        outcomes[art["slug"]] = "落とした(差し戻しても検算不合格): " + " / ".join(problems[:4])
                        print(f"記事 {art['slug']} を{outcomes[art['slug']]}", flush=True)
                        aborted.append(art["slug"])
                        DROP_LOG.setdefault(date, {})[art["slug"]] = (str(art.get("angle") or ""), "執筆で落とした", " / ".join(problems[:3])[:160])
                        # 差し戻しても同じ契約を通せない = 契約(schema・依頼文・検算)の欠陥の疑い。当番が診断する
                        note_stuck(date, "機械検算", f"docs/_posts/{date}-{art['slug']}.md", "", " / ".join(problems[:4]), 2)
                    continue
                renderlib.render_article(target, date, art, ans, classify_source, weakest_src, yaml_dump_keeping_strings)
                remember_fact_map(date, art["slug"], target, fact_by_id)
                if tries:
                    print(f"記事 {art['slug']}: 差し戻し後に合格", flush=True)
            elif "ABORT:" in out[-2000:] and not target.exists():
                reason = out.rsplit("ABORT:", 1)[-1].strip()[:200]
                print(f"記事 {art['slug']} は出典照合で不成立: {reason}", flush=True)
                aborted.append(art["slug"])
                continue
            # 固定項目はコードで計画に合わせる(モデルに写させない・直させない)。
            # 残る検収エラーは「ファイルが無い」「frontmatter が読めない」だけ
            fixed = canonicalize_article(date, art, cands)
            if fixed:
                print(f"記事 {art['slug']} の固定項目を正規化: " + " / ".join(fixed[:4]), flush=True)
            errs = validate_article_file(date, art, cands)
            if errs:
                print(f"記事 {art['slug']} 検収不合格: {errs}", flush=True)
                aborted.append(art["slug"])
                bad = ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md"
                log_drop(date, art["slug"], "検収不合格", " / ".join(str(e) for e in errs[:3]), bad)
                if bad.exists():
                    bad.unlink()
            else:
                written.append(art)
        if blocked:
            raise CliBlocked(blocked)
    return written, aborted


def load_story_facts() -> dict:
    p = ROOT / "stock" / "stories.yml"
    if not p.exists():
        return {}
    out = {}
    for e in yaml.safe_load(p.read_text(encoding="utf-8")) or []:
        out[e.get("story_id")] = e.get("published_facts", [])
    return out


def run_derive(date: str) -> bool:
    """派生(derive --write)を走らせる。失敗(0 以外の終了・起動できない・時間切れ)は黙って進まず、理由(終了コードと最後の行)を
    異常として申告し False を返す。最後の門はこのあとの lint(古い・途中の号は lint が止める)。以前は終了コードを見ず、derive が
    落ちても lint の書式の誤りとしてしか見えなかった(監査指摘 2026-10-14)。"""
    try:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "derive.py"), "--date", date, "--write"],
                           cwd=ROOT, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        anomaly("compose", f"{date}: 派生(derive)を走らせられない: {type(e).__name__}: {str(e)[:200]}")
        return False
    if r.returncode != 0:
        last = ((r.stderr or r.stdout or "").strip().splitlines() or ["(出力なし)"])[-1][:300]
        anomaly("compose", f"{date}: 派生(derive)が失敗(終了 {r.returncode}): {last}")
        return False
    return True


def run_lint(date: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "lint.py"), "--base", "origin/main"],
                       capture_output=True, text=True, cwd=ROOT)
    # 全文を返す。末尾だけ返すと、先頭側の記事の所見(::warning)が校閲に渡らない(監査指摘)
    return r.returncode, r.stdout


def run_parallel(jobs: list[tuple[str, callable]], timeout: int = 2500) -> list[str]:
    """独立した工程を同時に走らせる。戻り値は失敗した工程の説明。

    社説・組版・記事修正は互いの成果物に触らないので、直列にする理由が無い。
    スレッドの中で起きた例外は**握り潰さず**、呼び出し側へ名前付きで返す
    (以前は起動しっぱなしで、失敗しても成功したように見えていた=監査指摘)。
    """
    errs: dict[str, str] = {}
    blocked: list[CliBlocked] = []

    def wrap(name, fn):
        def run():
            try:
                fn()
            except CliBlocked as e:     # モデル CLI の認証切れ・利用上限: 工程の失敗ではない。全部を回収してから親で止める
                blocked.append(e)
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
    if blocked:
        raise blocked[0]
    if errs and CLI_BLOCKED:
        # 失敗したスレッドの中で CLI の停止を見たのに、ふつうの失敗として上がってきた(中の並列が先の失敗で停止を隠した等。
        # 監査指摘 r6)。停止として止める(失敗の工程として先へ進まない)
        raise CliBlocked(next(iter(CLI_BLOCKED.values())))
    return [f"{n}: {why}" for n, why in errs.items()]


def run_in_turn(jobs: list[tuple[str, callable]], timeout: int = 2500) -> list[str]:
    """工程を1つずつ順に走らせる(失敗の扱いは run_parallel と同じ)。子の子孫を記録して止める工程(記事の書き直し)を、ほかに子を
    起こす工程と並べないため(ChildTracker)。前の工程が時間切れでまだ動いていれば、あとの工程は始めない(並んでしまう。監査指摘)。"""
    errs: list[str] = []
    for k, job in enumerate(jobs):
        got = run_parallel([job], timeout=timeout)
        errs += got
        if any(e.endswith("時間切れ(スレッドが終わっていない)") for e in got):
            errs += [f"{n}: 前の工程({job[0]})が終わっていないので始めない" for n, _ in jobs[k + 1:]]
            break
    return errs


def revise_prompt(date: str, art: dict, mats_in: list[dict], current: str, issues: list[dict]) -> str:
    """校閲の指摘を受けた**執筆側(Codex)**の書き直し。同じ出力契約(article-out)で返す。"""
    iss = json.dumps([{k: b.get(k) for k in ("issue_id", "rule_id", "issue", "quote", "repair", "fact_ids", "expected")}
                      for b in issues], ensure_ascii=False, indent=1)
    return render_prompt("revise-article", DATE=date, ISSUES=iss, CURRENT=current[:12000],
                         MATERIALS=json.dumps(mats_in, ensure_ascii=False, indent=1))


def revise_check(ans: dict, issues: list[dict], old_fm: dict | None, old_body: str) -> list[str]:
    """書き直しが**指摘の外を変えていないか**の機械検査(形だけ。中身の判断はしない)。

    - 渡した指摘は全部 addressed_issue_ids に入っている(知らない id は不可)。「対応」には、
      確かめた上で記述を残す判断も含む。**残した記述が正しいかは次の巡の校閲(モデル)が判定する**
      (引用が残っているかを機械で見るのは校閲の機械化になるのでしない。編集長の指示)
    - 見出し・リード・tags・event_date は、指摘の quote が掛かっていない限り変えない
    - 出典の増減は、校閲の repair の種類で縛らない(repair は提案で、直し方を決めるのは執筆側。依頼文 revise-article)。
      足すのはいつでもよい。根拠 id の付け替えは出典についての指摘(R3/R4 か add_source / drop_source)があるとき、
      外すのは出典の誤りの指摘(R3/R4 か drop_source)があるときだけ。
      足した・外した出典が正しいかは、次の巡の校閲(R3/R4)が判定する
    - 本文は、指摘の quote を含む段落だけ変えてよい(他の段落は字面も根拠 id も順序もそのまま)
    """
    ids = [b["issue_id"] for b in issues]
    addressed = [str(x) for x in (ans.get("addressed_issue_ids") or [])]
    problems = []
    unknown = [x for x in addressed if x not in ids]
    if unknown:
        problems.append(f"知らない issue_id を対応済みにした: {unknown[:4]}")
    left = [i for i in ids if i not in addressed]
    if left:
        problems.append(f"対応していない指摘がある: {left[:4]}")
    # 「指摘の外」の照合は読者に見える字面で行う(renderlib.visible_text)
    norm = renderlib.visible_text

    def qnorm(b):
        # 校閲が引用の外側に付けた鉤括弧・引用符は本文に無いことがあるので外してから照合する。
        # 外さないと `q in 本文` が成り立たず、直した段落を「触っていない」と誤認して局所修正まで落ちる(監査指摘)。
        q = norm(b.get("quote"))
        pairs = {"「": "」", "『": "』", "“": "”", "‘": "’", '"': '"', "'": "'"}
        while len(q) >= 2 and q[0] in pairs and q[-1] == pairs[q[0]]:
            q = q[1:-1]
        return q
    blocks = ans.get("blocks") or []
    repairs = {b.get("repair") for b in issues}
    # 出典についての指摘(出典の規則 R3 出典隠し / R4 出典の不一致、または add_source / drop_source の提案)のとき、
    # 根拠段落は「どの出典に紐づくか」が正当に変わる(誤出典 F を正しい候補の F/N に差し替える。R4「記事は強い出典に合わせる」)。
    # その巡では字面だけを見て根拠 id の変化は通し、根拠の正しさは次の巡の校閲(R1/R3/R4)が見る(監査指摘 MF-1)。
    # repair の種類だけでは決めない(R4 の誤出典に rewrite_claim が付くと差し替えが弾かれて記事が落ちる。監査指摘)
    source_repair = bool(repairs & {"add_source", "drop_source"}) or any(b.get("rule_id") in ("R3", "R4") for b in issues)
    # lint の指摘(構造の赤)は直し方を限定できないので、指摘の外の検査は掛けない
    if old_fm and not any(b.get("rule_id") == "LINT" for b in issues):
        quotes = [qnorm(b) for b in issues if len(qnorm(b)) >= 8]
        # 束ねた quote(校閲は該当箇所が複数あるとき、見出し・リード・本文を1つの quote にまとめて返す)から、
        # 指定した欄の区画の中身を取り出す。実測した束ね方は3通り(欄名の表記も「見出し」「タイトル」で揺れる):
        #   A 「…」(見出し)   閉じ鉤の直後に(欄名)          実測 2026-10-02 R1「日々、発見的ステップ！」
        #   B 見出し「…」      欄名の直後に「…」(後ろに根拠注記)  実測 metrics/review-2026-09-21-1.json
        #   C 見出し：…        欄名：本文(次の欄名まで)       実測 metrics/review-2026-09-25-1.json
        # quotes は qnorm→visible_text 済みで、NFKC により全角（）：は半角()に畳まれ、改行は落ちる(C の
        # 「…\nリード：…」は欄名で連結された1文字列になる)ので、C は次の欄名ラベルまでで区切る。「」『』は残る。
        # 欄 ⊂ quote の逆向き一致を1表記(A)だけで見ると、B/C の束ね方では該当欄も「指摘の外」と誤認し、
        # 正しく直した稿を検算が戻す(同じ指摘が次の巡に残り当番に上がる。監査指摘 MF-1)。
        field_labels = {"見出し": ("見出し", "タイトル"), "リード": ("リード",), "本文": ("本文",)}
        all_labels = [l for ls in field_labels.values() for l in ls]

        def bracket_after(q, i):  # q[i] が「のとき、入れ子を数えて対応する」までの中身。無ければ None
            if i >= len(q) or q[i] != "「":
                return None
            depth = 0
            for k in range(i, len(q)):
                if q[k] == "「":
                    depth += 1
                elif q[k] == "」":
                    depth -= 1
                    if depth == 0:
                        return q[i + 1:k]
            return None

        def bracket_before(q, i):  # 位置 i の直前で閉じる「…」の中身。入れ子を数える。無ければ None
            j = q.rfind("」", 0, i)
            if j < 0:
                return None
            depth = 0
            for k in range(j, -1, -1):
                if q[k] == "」":
                    depth += 1
                elif q[k] == "「":
                    depth -= 1
                    if depth == 0:
                        return q[k + 1:j]
            return None

        def field_segments(q, field):
            segs = []
            for L in field_labels.get(field, (field,)):
                s = 0  # A: 「…」(L)
                while (i := q.find(f"({L})", s)) >= 0:
                    seg = bracket_before(q, i)
                    if seg is not None:
                        segs.append(seg)
                    s = i + 1
                s = 0  # B: L「…」
                while (i := q.find(L + "「", s)) >= 0:
                    seg = bracket_after(q, i + len(L))
                    if seg is not None:
                        segs.append(seg)
                    s = i + len(L)
                s = 0  # C: L：…(改行は消えているので次の欄名ラベルまでで切る)
                while (i := q.find(L + ":", s)) >= 0:
                    start = i + len(L) + 1
                    end = len(q)
                    for L2 in all_labels:
                        for mk in (L2 + ":", L2 + "「"):
                            p = q.find(mk, start)
                            if 0 <= p < end:
                                end = p
                    segs.append(q[start:end])
                    s = start
            return segs
        # 「触った」= quote ⊂ 欄(校閲がその欄の一部を literal に引用した = その欄は指摘の対象)。これは常に見る。
        # 逆向き(欄 ⊂ quote)は、束ねた quote の **その欄の区画** に欄が丸ごと収まるときだけ認める。全欄共通で
        # 認めると、本文だけの長い quote がリード等の全文を偶然含む記事で指摘外の欄の改変まで通してしまう
        # (既存 docs/_posts で20/1301件がこの形。監査指摘 MF-1)。欄ごとに区画を分け、該当欄だけ照合する。
        # 校閲は欄の**一部**を抜き出し、省略を「…」で示して引用する(リード「…事務所を越えた…公開され…」。
        # NFKC で「...」になる)。抜き出しは区画 ⊂ 欄の向きで、省略記号で割った各片が欄に順に全部あれば触った
        # とみなす。見ないと、指摘どおりリードを直した稿を「指摘に無いリードを変えた」で戻し、同じ指摘が
        # 次の巡に残って記事が落ちる(実測 2026-10-09 joint-starmas・gaku-katamarion)
        def excerpt_in(x, s):
            pieces = [p for p in re.split(r"\.{3,}|…+", x) if p]
            if sum(len(p) for p in pieces) < 8:
                return False
            k = 0
            for p in pieces:
                k = s.find(p, k)
                if k < 0:
                    return False
                k += len(p)
            return True

        def touched(s, label=None):
            for q in quotes:
                if q in s or excerpt_in(q, s):
                    return True
                if label and len(s) >= 8:
                    if any(s in seg or excerpt_in(seg, s) for seg in field_segments(q, label)):
                        return True
            return False
        old_title = norm(old_fm.get("title"))
        if norm(ans.get("title")) != old_title and not touched(old_title, "見出し"):
            problems.append("指摘に無い見出しを変えた")
        old_lede = norm(old_fm.get("lede"))
        if norm(ans.get("lede")) != old_lede and not touched(old_lede, "リード"):
            problems.append("指摘に無いリードを変えた")
        old_tags = [str(t) for t in (old_fm.get("tags") or [])]
        tags_targeted = any(touched(norm(t)) for t in old_tags) or any(
            re.search(r"tag|タグ", str(b.get("issue") or ""), re.I) for b in issues)
        if sorted(str(t) for t in (ans.get("tags") or [])) != sorted(old_tags) and not tags_targeted:
            problems.append("指摘に無い tags を変えた")
        if str(ans.get("event_date") or "") != str(old_fm.get("event_date") or "") and not event_date_targeted(issues, old_fm):
            problems.append("指摘に無い event_date を変えた")
        # 本文: 指摘の quote を含む段落だけ変えてよい。他の段落は**字面も根拠 id も**そのまま、順序も
        # 保つ。指摘の段落1つにつき新しい段落は1つまで(勝手な追加を許さない)(監査指摘)。
        # quote の無い指摘(場所を特定できない)が混ざっているときは段落の検査を掛けない
        if all(len(qnorm(b)) >= 8 for b in issues):
            # R2(確かめられない verified_facts)が名指した N の id は、その事実ごと根拠から外すのが正しい直し方で、
            # 指摘の quote の段落以外に付いていても外してよい(字面はそのまま)。見ないと、N1 を全段落から外した稿を
            # 「指摘に無い段落を変えた」で戻し、R2 が次の巡に残って記事が落ちた(実測 2026-10-10 joint-dere-miri-fureaka)
            retracted = {str(i) for b in issues if b.get("rule_id") == "R2"
                         for i in (b.get("fact_ids") or []) if str(i).startswith("N")}

            def para(text, ids=None):
                m = renderlib.FACT_NOTE.search(text or "")
                ids = ids if ids is not None else (m.group(1).split() if m else [])
                # 出典の付け替え中は根拠 id の変化を段落の同一性から外す(字面が同じなら未指摘段落として通す)。
                # 字面の無断改変は従来どおり止まる(監査指摘 MF-1)
                key_ids = () if source_repair else tuple(sorted(str(i) for i in ids if str(i) not in retracted))
                return norm(renderlib.strip_fact_notes(text)), key_ids
            # 旧稿も新稿も、**同じ正規化と同じ単位**で比べる(文字として残った `\n` を改行に戻し、
            # 地の文と箇条書きの境目で分ける = 書き出しと同じ renderlib の規則)。揃えないと、壊れた改行を
            # 直しただけの稿が「指摘に無い段落を足した」で落ちる(監査指摘 r71)。
            # 旧稿は段落ごとに根拠の控えが付いているので、空行で分けてから、その控えを分けた各単位に引き継ぐ
            def units(text, ids=None):
                m = renderlib.FACT_NOTE.search(text or "")
                ids = ids if ids is not None else (m.group(1).split() if m else [])
                fixed = renderlib.unescape_text(renderlib.strip_fact_notes(text or ""))
                # 箇条書きは項目単位まで割ってから照合する。1 block にまとめた稿も項目ごとに分けた稿も
                # 同じ単位に揃え、分け方の違いだけで落とさない(監査指摘)
                return [para(md, ids) for blk in renderlib.split_list_blocks(fixed)
                        for md in renderlib.split_list_items(blk)]
            old_paras = [u for p in re.split(r"\n\s*\n", old_body or "") if p.strip() for u in units(p)]
            new_paras = [u for b in blocks for u in units(b.get("markdown"), b.get("fact_ids") or [])]
            # 一対一の対応: 未指摘の旧段落を順に、新しい段落から**1つずつ消費**して探す
            # (同じ段落が2つあれば2つ要る。集合の membership では複製・片方の削除を見逃す。監査指摘)
            # 校閲は別々の段落から抜いた文を空白でつないで1つの quote にすることがある(「DAY1…400円(税込)。 DAY2…」)。
            # どの段落にも quote が丸ごと入らないので、該当する段落を「指摘の外」と誤認して、指摘どおり直した稿を
            # 戻していた(実測 2026-10-10 million-14th-live-goods: DAY1・DAY2 の2段落から抜いた R2)。
            # 正規化(visible_text)は空白を落とすので、正規化の前の quote を空白で割り、各片(8字以上)を含む段落も
            # 指摘の段落とする
            pieces = [x for b in issues for raw in [re.split(r"\s+", str(b.get("quote") or "").strip())] if len(raw) > 1
                      for x in map(norm, raw) if len(x) >= 8]
            spanned = {i for i, p in enumerate(old_paras) if any(x in p[0] for x in pieces)}
            hit = [i in spanned or touched(p[0], "本文") for i, p in enumerate(old_paras)]
            consumed = [False] * len(new_paras)
            cursor = 0
            for i, p in enumerate(old_paras):
                if hit[i]:
                    continue
                try:
                    j = next(k for k in range(cursor, len(new_paras)) if new_paras[k] == p and not consumed[k])
                except StopIteration:
                    if any(new_paras[k] == p and not consumed[k] for k in range(len(new_paras))):
                        problems.append("指摘に無い段落の順序を入れ替えた")
                    else:
                        problems.append(f"指摘に無い段落を変えた・消した(字面か根拠 id): {p[0][:30]}")
                    continue
                consumed[j] = True
                cursor = j + 1
            n_allowed = sum(hit)
            n_new = sum(1 for c in consumed if not c)
            if n_new > n_allowed:
                problems.append(f"指摘に無い段落を足した(新しい段落 {n_new} / 指摘の段落 {n_allowed})")
        old_urls = {s.get("url") for s in (old_fm.get("sources") or []) if isinstance(s, dict)}
        new_urls = {s.get("url") for s in (ans.get("sources") or [])}
        # 出典を「足す」のは、出典にない事実・出典隠し・公式が無い(R1/R3/R5)を直す正規の手で、決めるのは
        # 執筆(revise-article.md「repair は校閲の提案で、決めるのはあなた」)。素材裏付けの出典を足すのは
        # 冪等に安全で(実在・一致の照合は次の巡の校閲=R3/R4 がやる)、add_source の提案が無くても常に通す。
        # 機械が止めるのは、黙って出典を「外す」ほう。外せるのは出典の誤りについての指摘(R3/R4 か drop_source)があるとき
        # — 誤出典を外して正しい一次情報に付け替える差し替え(外す+足す)も通る(add_source は「足せ」なので外す根拠にしない)。
        # 外した判断が正しいかは次の巡の校閲。以前は drop_source があっても「足した」で差し替えを弾き、
        # 誤 URL の R4 が何巡もブロックして記事が落ちた(監査指摘 MF-1)
        removed = old_urls - new_urls
        may_remove = "drop_source" in repairs or any(b.get("rule_id") in ("R3", "R4") for b in issues)
        if removed and not may_remove:
            problems.append(f"出典についての指摘が無いのに出典を外した: {sorted(removed)[:2]}")
    return problems


def event_date_targeted(issues: list[dict], old_fm: dict) -> bool:
    """指摘が event_date に掛かっているか(quote が元の稿の event_date を含む、または issue・quote・expected が
    event_date を名指す)。LINT の指摘は直し方を限定できないので掛かっている扱い。"""
    ev = str(old_fm.get("event_date") or "")
    for b in issues:
        if b.get("rule_id") == "LINT":
            return True
        text = " ".join(str(b.get(k) or "") for k in ("issue", "quote", "expected"))
        if "event_date" in text or (ev and ev in str(b.get("quote") or "")):
            return True
    return False


def revise_apply(date: str, art: dict, path: Path, ans: dict, fact_by_id: dict, materials: list[dict],
                 issues: list[dict]) -> tuple[str, str]:
    """書き直しの答えを記事に反映する。戻りは ("fixed"|"dropped"|"kept", 説明)。

    検算(check_output)を status の分岐より**先に**掛ける。理由(decline_code/decline_detail)の無い decline は
    不合格で、既存の記事は残す(理由なしで記事を消させない。監査指摘)。
    """
    # event_date は指摘が掛かっていなければ元の稿の値を写す(写すのはコード)。依頼文の「現在の記事」には
    # null の event_date が出ないので、書き手は値が無かったことを知らずに日付を埋める(2026-10-03 に
    # 「指摘に無い event_date を変えた」で2巡とも戻され、R1 が直らずブロックで落ちた)
    old_fm = parse_front_matter(path) if path.exists() else None
    if ans.get("status") != "decline" and old_fm and not event_date_targeted(issues, old_fm):
        ev = old_fm.get("event_date")
        ans = dict(ans, event_date=ev.isoformat() if hasattr(ev, "isoformat") else (str(ev) if ev else None))
    # 根拠にできるページ: 素材と、この記事のセッション(初稿・書き直し)で fetch_page.py が本文を取れたページと、現在の稿の根拠のページ
    fetched = fetched_urls(fetch_record(date, art["slug"])) | draft_urls(old_fm)
    try:
        problems = renderlib.check_output(ans, fact_by_id, materials, rank=art.get("rank") or "", edition=date, fetched=fetched)
    except Exception as e:
        # 中の要素の形が崩れた答えで検算が例外になっても号全体を止めない。元の稿のまま(監査指摘)
        return "kept", f"答えの形が崩れていて検算できない({type(e).__name__}: {str(e)[:80]})。元の稿のまま"
    if ans.get("status") == "decline":
        if problems:
            return "kept", "理由の無い decline(" + " / ".join(problems[:2]) + ")。元の稿のまま"
        log_drop(date, art["slug"], "書き直しで見送り", f"{ans.get('decline_code')} {str(ans.get('decline_detail') or '')}".strip(), path,
                 issues=issues)
        path.unlink(missing_ok=True)
        return "dropped", f"執筆側が不成立と判断({ans.get('decline_code')})。落とす"
    old_text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        problems += revise_check(ans, issues, old_fm,
                                 re.split(r"\n---\n", old_text, maxsplit=1)[-1])
    except Exception as e:
        # 欄の形が崩れた答え(addressed_issue_ids が数値など)で号全体を止めない。元の稿のまま(監査指摘)
        return "kept", f"答えの形が崩れていて照合できない({type(e).__name__}: {str(e)[:80]})。元の稿のまま"
    # 「指摘の外を変えた」(範囲)は形の不合格ではなく、その変更が指摘に応じるのに要ったか・素材に基づくかという**中身の判断**で、
    # 校閲(モデル)が確かめる(コードは形だけ)。範囲で戻すと、校閲の quote の書き方しだいで正しい直しが戻され、記事が落ちた
    # (2026-09-25・10-03・10-10 に繰り返し。10日号の救済では一面を含む4本の書き直しが戻された)。範囲を外れた変更は記録して
    # 次の巡の校閲に添える。書き直した記事は次の巡で記事ごと校閲され、校閲されないまま時間切れになれば承認の指紋が合わず出ない
    scope = [x for x in problems if x.startswith("指摘に無い")]
    hard = [x for x in problems if not x.startswith("指摘に無い")]
    if hard:
        return "kept", "検算不合格 " + " / ".join(hard[:3]) + "(元の稿のまま)"
    renderlib.render_article(path, date, art, ans, classify_source, weakest_src, yaml_dump_keeping_strings)
    remember_fact_map(date, art["slug"], path, fact_by_id)
    if scope:
        OFFSCOPE_NOTES.setdefault(date, {})[art["slug"]] = " / ".join(scope)[:400]
        return "fixed", f"対応 {ans.get('addressed_issue_ids')}(指摘の外も変えた: {' / '.join(scope)[:120]}。次の巡の校閲が確かめる)"
    return "fixed", f"対応 {ans.get('addressed_issue_ids')}"


GUARD = Path(__file__).resolve().parent / "child_guard.py"


def guarded(cmd: list[str], out_path: Path) -> list[str]:
    """執筆・書き直しの子(CLI)を child_guard 越しに起動するコマンド。child_guard は孤児の受け皿になってから CLI を起動し、
    CLI が終わったら、CLI が残した道具の実行を(別のセッションに抜けた・環境を読めなくしたものも)全部止めて回収してから終わる
    (2026-10-14 監査: プロセスグループ・環境の印で探す止め方では、/proc/<pid>/environ を読めなくした道具を見落とした)。
    起動できたか・片付けを確かめられたかは、終了コード(CLI のもの)ではなく結果のファイル(guard_status_of)で返る。"""
    return [sys.executable, str(GUARD), "--status", str(guard_status_of(out_path))] + cmd


def guard_status_of(out_path: Path) -> Path:
    """Codex の最終メッセージの一時ファイルに対応する、child_guard の結果のファイル。"""
    return Path(str(out_path) + ".guard")


def guard_result(out_path: Path, who: str) -> bool:
    """child_guard の結果を読んで消す。CLI を起動できていなければ False(呼び出し側は直接起動したときと同じく FileNotFoundError に
    する。出力の読めない回と取り違えない)。道具の実行を片付けたと確かめられなかったら異常として申告する。結果が無い(child_guard が
    落とされた・使われていない)ときは ChildTracker の確かめに任せる。"""
    p = guard_status_of(out_path)
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = None
    p.unlink(missing_ok=True)
    if not isinstance(st, dict):
        return True
    if st.get("started") is False:
        return False
    if st.get("cleaned") is not True:
        anomaly("compose", f"{who}: 子の CLI が残した道具の実行を片付けたと確かめられない({st.get('note') or '理由不明'})")
    return True


def end_guarded(p, grace: int = 15) -> None:
    """時間切れの子(child_guard)を止める: SIGTERM で CLI を止めさせ、残した道具の実行の片付けを待つ。待ちきれなければグループごと落とす。"""
    try:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        reap(p)
    except (OSError, AttributeError):
        reap(p)


GUARD_GRACE = 15      # 時間切れの子(child_guard)に片付けを待つ秒数。束ねの全員で共有する


def finish_guarded(p, timeout: float | None, tracker: "ChildTracker | None" = None) -> tuple[str, bool]:
    """child_guard 越しに起動した子(start_group)の終わりを待つ。戻りは (標準エラー, 時間切れか)(出力はファイルで受ける)。
    時間切れは SIGTERM を先に送り(child_guard が CLI を止め、CLI が残した道具の実行を片付けて結果のファイルを書く)、待ちきれなければ
    グループと子孫ごと止める(end_guarded)。どちらでも最後に finish_group で、印を受け継いで残った道具を止めて回収する。
    中断(例外・SIGTERM の SystemExit)は止めて回収してから上げる(2026-10-21 組 P の起動の関数と組 W の child_guard の取り込み)。
    tracker を渡すと、最初の時間切れで束ねの残りの子にもまとめて SIGTERM を送り、片付けの待ちを束ねで GUARD_GRACE 秒に揃える
    (1本ずつ 15 秒待つと、応答しない8本で締切を約2分越えた。2026-10-21 取り込みの監査)。"""
    if isinstance(p, LaunchFailed):
        return finish_group(p, timeout)[1], False
    timed_out = False
    try:
        p.communicate(timeout=None if timeout is None else max(0, timeout))
    except subprocess.TimeoutExpired:
        grace = GUARD_GRACE
        if tracker is not None:
            if tracker.term_until is None:
                tracker.term_until = time.monotonic() + GUARD_GRACE
                for q in tracker.procs:
                    if q is not p and not isinstance(q, LaunchFailed) and q.poll() is None:
                        try:
                            q.send_signal(signal.SIGTERM)
                        except (OSError, AttributeError):
                            pass
            grace = max(1, tracker.term_until - time.monotonic())
        end_guarded(p, grace)
        timed_out = True
    except BaseException:
        stop_groups([p])
        raise
    _, se, late = finish_group(p, 0)
    return se + ("\n(時間切れ)" if timed_out else ""), timed_out or late


@contextlib.contextmanager
def guarded_batch(date: str, kept_posts: dict[Path, bytes], who: str):
    """執筆・書き直しの束ね。子孫の記録(ChildTracker)を始めて渡し、抜けるときは終わり方を問わず、起動した担当を止めて回収し
    (stop_groups)、記録した子孫を全部止める(sweep)。呼び出し側は束ねの子を全部待ってから抜け、そのあとで控えへ戻す。
    例外・中断で抜けるときは答えを当てないので、この号の記事を控えへ戻してから上げる(途中終了の後始末が、子の直接の書き込みを
    確定しない)。"""
    tracker = ChildTracker().start()
    done = False
    try:
        yield tracker
        done = True
    finally:
        try:
            stop_groups(list(tracker.procs))
        finally:
            swept = tracker.sweep()
        if not swept:
            anomaly("compose", f"{who}の子が残した道具の実行を止められない(控えへ戻したあとに記事を書き換えうる)")
        if not done:
            try:
                restore_posts(date, kept_posts)
            except Exception as e:      # noqa: BLE001  元の例外を隠さない
                print(f"{who}の途中終了で記事を控えへ戻せない: {type(e).__name__}: {e}", flush=True)


# 執筆・書き直しの子(CLI)に渡す印の環境変数。子が起動した道具は環境を受け継ぐので、別のセッション・プロセスグループを作って
# 抜けても印で見つかる。名前に KEY・SECRET・TOKEN を含めない(codex は道具に渡す環境から、それらを含む名前の変数を外す)
CHILD_MARK = "IMAS_WRITER_RUN"


def child_env(mark: str, base: dict | None = None) -> dict:
    """子に渡す環境: base(既定はいまの環境。執筆・書き直しは fetch_env)に印(CHILD_MARK=mark)を足したもの。mark は子ごとに一意にする。"""
    return dict(os.environ if base is None else base, **{CHILD_MARK: mark})


def live_procs(pgid: int | None, mark: str = "") -> list[int]:
    """動いている(ゾンビでない)プロセスのうち、プロセスグループが pgid のもの・環境に印 mark を持つもの(自分は除く)。"""
    proc = Path("/proc")
    if not proc.is_dir():           # /proc の無い環境ではプロセスグループだけを見る
        if not pgid:
            return []
        try:
            os.killpg(pgid, 0)
            return [pgid]
        except ProcessLookupError:
            return []
        except OSError:
            return [pgid]
    needle = f"{CHILD_MARK}={mark}\0".encode() if mark else None
    found = []
    for d in proc.glob("[0-9]*"):
        try:
            pid = int(d.name)
            if pid == os.getpid():
                continue
            f = (d / "stat").read_text().rsplit(")", 1)[1].split()     # 状態・親・プロセスグループ …
            if len(f) < 3 or f[0] in ("Z", "X"):
                continue
            if (pgid and f[2] == str(pgid)) or (needle and needle in (d / "environ").read_bytes()):
                found.append(pid)
        except (OSError, ValueError, IndexError):
            continue
    return found




def subreaper(on: bool) -> None:
    """compose を孤児の受け皿にする/やめる(ChildTracker が使っているあいだだけ。重なっても最後の1つが終わるまで続ける)。
    出典の判定の取引(pipelib)も受け皿を使うので、数えるのは pipelib.subreaper に任せる(片方がやめて、もう片方の受け皿を外さない)。"""
    pipelib.subreaper(on)


def proc_table() -> dict[int, tuple[str, int, int, int]]:
    """プロセスの表 {pid: (状態, 親, プロセスグループ, 起動時刻)}(/proc/<pid>/stat。環境を読めなくした道具のものも読める)。
    記録のスレッドが繰り返し読むので、Path を通さずに読む(2026-10-21 実測: 300 プロセスで 1回 15ms → 8ms 程度)。"""
    table = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return table
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                s = fh.read()
            f = s[s.rindex(b")") + 1:].split()
            table[int(name)] = (f[0].decode(), int(f[1]), int(f[2]), int(f[19]))
        except (OSError, ValueError, IndexError):
            continue
    return table


class ChildTracker:
    """執筆・書き直しの子(child_guard)の子孫を、束ねのあいだ記録し続け(ジョブ所属の追跡)、終わったら全部止める。
    主な片付けは child_guard(孤児の受け皿)がする。これは child_guard 自身が落とされた(SIGKILL・時間切れの reap)ときの備え
    (2026-10-14 監査: child_guard だけを SIGKILL すると、別のセッションに抜けて環境を読めなくした道具が残り、控えへ戻したあとに
    承認済みの記事を書き換えた)。compose 自身も孤児の受け皿になる(child_guard が落ちたあとの孤児が init へ行かず、ここの子として
    残る)。止めるのは: 記録した子孫(pid と起動時刻で照合)とその子孫 / child_guard のプロセスグループ / 印(child_env)を環境に持つ
    プロセス / ほかのスレッドが子を起こしていないときは、束ねの間に起動してここへ付け替わった孤児。起こしうるスレッドがあれば、
    孤児がどの仕事のものか決められないので、片付けたとは言わない(記事の書き直しはほかの工程と並べない: run_in_turn)。"""

    # 記録の間隔。孤児はここ(受け皿)へ付け替わるので、記録は付け替わる前の親子の手がかりを残す備え。50ms ごとに /proc を全部読むと、
    # 300 プロセスで1コアの約25%を使った(2026-10-21 実測)。0.25 秒で約3%
    def __init__(self, interval: float = 0.25):
        self.roots: dict[int, int] = {}            # child_guard の pid → 起動時刻
        self.procs: list = []                       # 起動した子(束ねを抜けるとき stop_groups で止めて回収する)
        self.term_until: float | None = None        # 時間切れで束ねの子に SIGTERM を送ったときの、片付けを待つ期限(finish_guarded)
        self.marks: list[str] = []
        self.seen: set[tuple[int, int]] = set()     # (pid, 起動時刻)
        self.interval = interval
        self.stop_ev = threading.Event()
        self.thread: threading.Thread | None = None
        self.t0 = 0
        self.before: set[tuple[int, int]] = set()
        self.foreign: set[tuple[int, int]] = set()    # 始める前からの子の子孫(束ねのものではない)
        self.ok = Path("/proc").is_dir()

    def start(self) -> "ChildTracker":
        if not self.ok:
            return self
        subreaper(True)
        try:
            self.t0 = int(float(Path("/proc/uptime").read_text().split()[0]) * os.sysconf("SC_CLK_TCK"))
        except (OSError, ValueError):
            self.t0 = 0
        # 始める前からの子は孤児として扱わない(起動時刻は時計刻みなので、直前に生まれた子は t0 と同じ刻みになりうる。監査指摘)
        self.before = {(pid, st) for pid, (_, ppid, _, st) in proc_table().items() if ppid == os.getpid()}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def add(self, p, mark: str) -> None:
        """起動した子(child_guard)と、その子に渡した印を記録する。"""
        self.procs.append(p)
        self.marks.append(mark)
        pid = getattr(p, "pid", None)
        if self.ok and isinstance(pid, int) and pid > 1:     # 0 は自分のプロセスグループを指す。テストの偽物は見ない
            st = proc_table().get(pid)
            if st:
                self.roots[pid] = st[3]
        self._poll()

    def _poll(self) -> dict:
        if not self.ok:
            return {}
        table = proc_table()
        kids: dict[int, list[int]] = {}
        for pid, (_, ppid, _, _) in table.items():
            kids.setdefault(ppid, []).append(pid)
        for roots, rec in ((list(self.roots.items()) + list(self.seen), self.seen), (list(self.before | self.foreign), self.foreign)):
            # 束ねの子の子孫は seen へ、始める前からの子の子孫は foreign へ(あとで付け替わっても、束ねの孤児と取り違えない。監査指摘)
            todo = [pid for pid, st in roots if table.get(pid, ("", 0, 0, -1))[3] == st]
            while todo:
                for k in kids.get(todo.pop(), []):
                    key = (k, table[k][3])
                    if key not in rec:
                        rec.add(key)
                        todo.append(k)
        return table

    def _run(self) -> None:
        while not self.stop_ev.wait(self.interval):
            self._poll()

    def _alone(self) -> bool:
        """ほかに子を起こしうるスレッドが無いか(このスレッド・main(run_parallel の join で待つ)・記録のスレッドだけ)。"""
        mine = {threading.current_thread(), threading.main_thread(), self.thread}
        return all(t in mine for t in threading.enumerate())

    def sweep(self, grace: float = 3.0) -> bool:
        """記録を止め、子孫を全部止めて、動いているものが無くなったのを確かめる(確かめられたら True)。"""
        self.stop_ev.set()
        if self.thread:
            self.thread.join(timeout=5)
        if not self.ok:     # /proc の無い環境: child_guard のプロセスグループだけを止める
            for pid in self.roots:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
            return True
        try:
            return self._sweep(grace)
        finally:
            subreaper(False)

    def _sweep(self, grace: float) -> bool:
        me, killed = os.getpid(), set()
        alone = self._alone()
        end = time.monotonic() + grace
        while True:
            table = self._poll()
            targets = {pid for pid, (s, ppid, pgrp, st) in table.items()
                       if (pid, st) in self.seen or pgrp in self.roots
                       or (alone and ppid == me and st >= self.t0 and pid not in self.roots
                           and (pid, st) not in self.before and (pid, st) not in self.foreign)}
            for mark in self.marks:
                targets.update(live_procs(None, mark))
            kids: dict[int, list[int]] = {}
            for pid, (_, ppid, _, _) in table.items():
                kids.setdefault(ppid, []).append(pid)
            todo = list(targets)
            while todo:
                for k in kids.get(todo.pop(), []):
                    if k not in targets:
                        targets.add(k)
                        todo.append(k)
            mine = {pid for pid in targets if pid != me and pid in table}
            # 止めた孤児と、所属を確かめた終わった孤児(ここの子になったもの)を回収する(ゾンビを残さない。監査指摘)
            for pid in killed | {q for q in mine if table[q][1] == me and table[q][0] in ("Z", "X") and q not in self.roots}:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    pass
            targets = {pid for pid in mine if table[pid][0] not in ("Z", "X")}
            if not targets:
                # ほかに子を起こしうるスレッドがあれば、付け替わった孤児がこの束ねのものか確かめられないので、確かめたとは言わない
                return alone
            if time.monotonic() >= end:
                return False
            for pid in targets:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            killed |= targets
            time.sleep(0.05)


def posts_snapshot(date: str) -> dict[Path, bytes]:
    """この号の記事ファイルの控え(パス → 中身)。執筆の子が作業ツリーに直接書いた変更を戻すため(restore_posts)。"""
    return {p: p.read_bytes() for p in (ROOT / "docs" / "_posts").glob(f"{date}-*.md")}


def restore_posts(date: str, snap: dict[Path, bytes], skip: set[Path] | frozenset = frozenset()) -> list[str]:
    """控えを取ってからの、この号の記事ファイルの変更(書き換え・削除・新しいファイル)を控えへ戻す。戻したファイル名を返す。
    skip の記事(呼び出し側が答えから書く・消す記事)には触らない。戻せなかったファイルは異常として申告する(1つ戻せなくても残りは戻す)。"""
    back = []
    for p in sorted((ROOT / "docs" / "_posts").glob(f"{date}-*.md")):
        if p not in snap and p not in skip:
            try:
                p.unlink(missing_ok=True)
                back.append(p.name)
            except OSError as e:
                anomaly("compose", f"執筆の子が作った {p.name} を消せない({type(e).__name__}: {str(e)[:120]})")
    for p, data in snap.items():
        if p in skip:
            continue
        try:
            if not p.is_file() or p.read_bytes() != data:
                write_checked(p, data.decode("utf-8"))
                back.append(p.name)
        except OSError as e:
            anomaly("compose", f"執筆の子が書き換えた {p.name} を控えへ戻せない({type(e).__name__}: {str(e)[:120]})")
    return back


def revise_articles(date: str, by_file: dict[str, list[dict]], plan: dict, cands: dict,
                    aborted: list[str] | None = None) -> dict[str, str]:
    """校閲の指摘を**執筆側(Codex)**に構造化で直させる。校閲側(Claude)には編集させない(監査の設計レビュー)。

    直した稿は初稿と同じ検算(事実 id・出典・タグ・日付)を通してからファイルにする。子が記事を直接書き換えても採らない(控えへ戻す)。
    decline(見送り)が返れば記事を落とす。何も直せないままなら次の巡でまたブロックが付き、上限で落ちる。
    戻り値は {slug: 結果}(fixed / kept / dropped / unreadable / not_started=締切を過ぎて始めなかった)。呼び出し側は「書き手が書き直した(fixed / kept)」記事に
    同じ指摘が残ったときだけ「直せなかった」と記録する(実行の失敗を契約の欠陥と取り違えない。監査指摘 r86)。
    """
    by_slug_plan = {a["slug"]: a for a in plan.get("articles") or []}
    jobs = []
    for f, issues in by_file.items():
        if not f.startswith("docs/_posts/"):
            continue
        name = Path(f).name
        slug = name[len(date) + 1:].removesuffix(".md")
        art = by_slug_plan.get(slug)
        path = ROOT / f
        if art is None or not path.exists():
            continue
        materials = [with_current_type(cands[c]) for c in art["candidate_ids"] if c in cands]
        mats_in, fact_by_id = renderlib.materials_with_ids(materials)
        REVISE_NOTES.setdefault(date, {}).pop(slug, None)    # この巡の書き直しの結果だけを校閲に伝える(古い巡を引きずらない)
        OFFSCOPE_NOTES.setdefault(date, {}).pop(slug, None)
        # 指摘に機械の id を振る(rule_id は同じ規則で複数付くので、対応の照合に使えない。監査指摘)
        issues = [{**b, "issue_id": f"I{k + 1}"} for k, b in enumerate(issues)]
        jobs.append((art, path, fact_by_id, materials, issues,
                     revise_prompt(date, art, mats_in, path.read_text(encoding="utf-8"), issues)))
    results: dict[str, str] = {}
    if not jobs:
        return results
    schema_out = ROOT / "schema" / "article-out.schema.json"
    n_fixed = n_dropped = 0
    for i in range(0, len(jobs), COMPOSE_WAVE):
        # 子は本体の作業ツリー(cwd=ROOT)で動き、記事を直接書き換えられる。稿は答えの JSON からコードが書くので、子の直接の
        # 書き込みは採らない: この号の記事を控え、子が全部終わったら(異常終了・時間切れでも)控えへ戻してから答えを当てる
        # (2026-10-14 監査指摘: 記事を直接書き換えて異常終了した子の変更が残り、答えは不採用なのに記事は書き換わっていた)
        kept_posts = posts_snapshot(date)
        procs, waited, cannot = [], [], []
        # 執筆と同じ: 1本ずつ一覧に足し、全員の終わりを待ってから答えを読む。例外・中断でも起動済みの担当と子孫の記録を止めて回収する
        with guarded_batch(date, kept_posts, "書き直し") as tracker:
            for art, path, fact_by_id, materials, issues, prompt in jobs[i:i + COMPOSE_WAVE]:
                if not deadline_passed():
                    fd, out_name = tempfile.mkstemp(prefix=f"codexrevise-{art['slug']}-", suffix=".txt")
                    os.close(fd)
                    short = prompt_file(date, f"revise-{art['slug']}", prompt)
                    # 進行ログ(標準出力・エラー)は一時ファイルで受けて生の出力に残す(執筆と同じ)
                    with open(log_of(Path(out_name)), "w", encoding="utf-8") as lf:
                        if not deadline_passed():       # 準備のあいだに締切を過ぎることもあるので、起動の直前にも確かめる
                            # child_guard 越しに起動し、印は答えのファイル名(子ごとに一意)。fetch_page.py の記録は記事ごと(fetch_env)
                            p = start_group(guarded(["codex", "exec", "-m", CODEX_WRITE_MODEL, "-s", "workspace-write",
                                                     "-c", "sandbox_workspace_write.network_access=true",
                                                     "--output-last-message", out_name, "--output-schema", str(schema_out), short],
                                                    Path(out_name)),
                                            cwd=ROOT, env=child_env(Path(out_name).name, fetch_env(date, art["slug"])),
                                            stdout=lf, stderr=subprocess.STDOUT)
                            procs.append((art, path, fact_by_id, materials, issues, Path(out_name), p))
                            tracker.add(p, Path(out_name).name)
                            continue
                    Path(out_name).unlink(missing_ok=True)
                    log_of(Path(out_name)).unlink(missing_ok=True)
                # 締切を過ぎたら書き直しを始めない(稿はそのまま。指摘は次の判断へ残る)
                print(f"書き直し {art['slug']}: 締切を過ぎたので始めない", flush=True)
                REVISE_NOTES.setdefault(date, {})[art["slug"]] = "締切を過ぎ、書き直しを始めなかった"
                results[art["slug"]] = "not_started"
            for job in procs:
                se, timed_out = finish_guarded(job[-1], remaining_seconds(), tracker)   # CLI と道具の実行を止めさせてから答えを読む
                if not guard_result(job[5], f"書き直し {job[0]['slug']}"):
                    cannot.append(job[0]["slug"])
                waited.append((*job, se, timed_out))
        back = restore_posts(date, kept_posts)
        if back:
            print(f"書き直しの子が直接書き換えた記事を控えへ戻した(稿は答えから書く): {', '.join(back[:8])}", flush=True)
        if cannot:      # CLI を起動できない: 直接起動したときと同じく例外で止める(記事は控えへ戻してある)
            for w in waited:
                w[5].unlink(missing_ok=True)
                take_log(w[5])
            raise FileNotFoundError(f"書き直しの子の CLI を起動できない: {', '.join(cannot)}")
        blocked = None
        for art, path, fact_by_id, materials, issues, out_path, p, se, timed_out in waited:
            out = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
            out_path.unlink(missing_ok=True)
            log = take_log(out_path) + se
            save_raw(date, f"revise-{art['slug']}", "", log, p.returncode, files={"last-message": out})
            # 認証切れ・利用上限は書き直しの失敗ではない(読めなかったと数えない)。この回を回収し終えてから止める
            got_blocked = cli_stop("codex", f"書き直し {art['slug']}", None if timed_out else p.returncode, log, out)
            if got_blocked:
                blocked = got_blocked
                continue
            try:
                # 採るのは正常に終わったセッションの、schema どおりの答えだけ(時間切れの間際に書かれた見送りで
                # 既存の記事を消さない・{} の理由を理由と認めない。執筆と同じ。監査指摘)
                if timed_out or p.returncode != 0:
                    raise ValueError(f"正常に終わっていない({'時間切れ' if timed_out else f'exit {p.returncode}'})")
                ans = loads_strict(out.strip())
                if not schema_ok(ans, json.loads(schema_out.read_text(encoding="utf-8"))):
                    raise ValueError("答えが出力 schema のとおりでない")
            except Exception:
                print(f"書き直し {art['slug']}: 出力が読めない(そのまま次の巡へ)", flush=True)
                # 注記は**この巡の結果**に更新する(古い巡の検算理由を引きずらない。監査指摘)
                REVISE_NOTES.setdefault(date, {})[art["slug"]] = "書き直しの出力が読めず(空・時間切れ・JSON でない)、稿を適用できなかった"
                results[art["slug"]] = "unreadable"
                # 実行の失敗も申告で終えない(なぜ出力が空・時間切れになったか)
                anomaly("compose", f"書き直し {art['slug']}: 出力が読めない(空・時間切れ・JSON でない。exit {getattr(p, 'returncode', '?')})")
                continue
            outcome, msg = revise_apply(date, art, path, ans, fact_by_id, materials, issues)
            results[art["slug"]] = outcome
            print(f"書き直し {art['slug']}: {msg}", flush=True)
            # 書き直しの稿を**機械の形の検査**が戻したときは、次の巡の校閲にそう伝える。伝えないと校閲は
            # 「執筆が指摘を無視した」と読んで同じ指摘を繰り返し、記事が落ちる(実測 2026-09-25: 誤字の指摘を
            # 3巡繰り返して落とした。執筆は毎回直していたが、検査が「指摘に無い段落を変えた」で戻していた)
            REVISE_NOTES.setdefault(date, {})[art["slug"]] = "" if outcome != "kept" else msg[:300]
            if outcome == "dropped":
                if aborted is not None:
                    aborted.append(art["slug"])
                n_dropped += 1
            elif outcome == "fixed":
                n_fixed += 1
        if blocked:
            raise CliBlocked(blocked)
    print(f"校閲の指摘で執筆側が {n_fixed}本を書き直し、{n_dropped}本を落とした", flush=True)
    return results


def fix_articles(date: str, by_file: dict[str, list[dict]]) -> None:
    """校閲のブロック指摘を、**記事1本につき1セッション**で直させる(並列)。

    以前は全部の指摘を1つのセッションに渡していた。指摘は記事ごとに独立して
    いるので、まとめる理由が無く、直列に直すぶんだけ時間になっていた。
    """
    jobs = [(f, ("校閲AIから以下のブロック指摘がありました。**"+f+" だけ**を、"
                 "candidates の facts と照合して修正してください。他のファイルには触らないこと。\n"
                 "**`docs/_editorials/` には触らないこと。**社説は別のセッションが直します。\n"
                 "**「出典隠し」「sources に無い」の指摘なら、答えは出典を足すことです。**指摘に URL が"
                 "書いてあるか、candidates の facts にその事実の出典 URL があるなら、それを sources に"
                 "足してください(label は出典元表記「告知タイトル」(日付) の形)。記述を削って逃げないこと。\n"
                 "修正できない(出典に無い事実で、消すしかない)なら、その記述を削ってください。\n"
                 "出典のページを開くときは `python3 scripts/fetch_page.py <url>` で読んでください(WebFetch は使えません)。\n"
                 "**出典同士が食い違っているなら、公式・当事者の記述に合わせ、食い違う弱いほうの出典を "
                 "sources から外してください。**記事を弱い出典に合わせて書き換えてはいけません"
                 "(実測 2026-09-07: 公式と電撃が『開場11時』、ファミ通だけ『12時』。記事は正しかったのに、"
                 "ファミ通に合わせようとして4巡直らず、一面ごと発行が止まった)。\n"
                 + json.dumps(bs, ensure_ascii=False, indent=2)))
            for f, bs in by_file.items() if f.startswith("docs/_posts/")]
    if not jobs:
        return
    def digest(f: str) -> str:
        p = ROOT / f
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""

    before = {f: digest(f) for f, _ in jobs}
    for i in range(0, len(jobs), COMPOSE_WAVE):
        procs = []
        # 自前のグループで起動し、時間切れ・中断では道具ごと止める(親だけ kill すると道具が記事を書き続ける)
        blocked = None
        try:
            for f, prompt in jobs[i:i + COMPOSE_WAVE]:
                short = None if deadline_passed() else prompt_file(date, "fix-" + Path(f).stem, prompt)
                if short is None or deadline_passed():      # 準備のあいだに締切を過ぎることもあるので、起動の直前にも確かめる
                    print(f"記事の修正 {Path(f).name}: 締切を過ぎたので始めない", flush=True)
                    continue
                procs.append((f, start_group(
                    ["claude", "-p", short, "--model", REVIEW_MODEL,
                     "--dangerously-skip-permissions",
                     # 出典は執筆・校閲と同じ fetch_page.py で読ませる(WebFetch は JS を描画しない。2026-10-21)
                     "--disallowedTools", "WebFetch",
                     "--max-budget-usd", COMPOSE_ARTICLE_MAX_BUDGET_USD],
                    # 出力は認証切れ・利用上限を見分けるためだけに受ける(答えは記事のファイル)
                    cwd=ROOT)))
            for f, p in procs:
                so, se, timed_out = finish_group(p, remaining_seconds())
                if not timed_out:
                    blocked = cli_stop("claude", f"校閲の指摘の修正 {Path(f).name}", p.returncode, so, se) or blocked
        finally:
            stop_groups([p for _, p in procs])
        if blocked:
            raise CliBlocked(blocked)
    # **何も変えなかった修正セッションを見えるようにする。**同じ指摘が次の巡にも
    # そのまま出て、最後に記事ごと落ちる。黙って通り過ぎると誰も気づけない
    untouched = [f for f, _ in jobs if digest(f) == before[f]]
    print(f"校閲の指摘で {len(jobs)}本を並列で修正した"
          + (f"(うち {len(untouched)}本は何も変わらなかった: "
             + ", ".join(Path(f).name for f in untouched[:5]) + ")" if untouched else ""), flush=True)


def _parse_review(text: str, err: str, where: str) -> dict:
    """校閲セッションの出力を JSON にする。読めない・判定の形をしていないなら、判定なし(error)にする。

    **判定の形をしていない答えを、判定にしない。**以前は JSON でさえあれば受け取っていたので、`{}` や
    verdict の無い答えが「指摘なし」として取り込まれ、その記事は校閲済みとみなされた(校閲を通っていない記事が
    発行されうる。2026-10-06 の洗い出しで判明。モデルの空の出力を『処理した・何も無い』と解釈する型の欠陥)"""
    # block なのに理由(blockers)が無い答えも判定にしない。まとめる側は blockers だけを拾うので、理由の無い block は
    # 承認に化ける(監査指摘)
    # 形は出力 schema(prompts/review-schema.json)そのもので検める。指摘の1件ずつの欄まで(null・文字列の要素は、
    # まとめる側で例外になり工程が止まる。監査指摘)。手で欄を足して検めると漏れが残るので schema に寄せる
    from jsonschema import Draft202012Validator
    validator = Draft202012Validator(json.loads((PROMPTS / "review-schema.json").read_text(encoding="utf-8")))   # 校閲セッションに渡すのと同じ schema

    def shaped(v):
        return validator.is_valid(v) and (v["verdict"] == "approve" or len(v["blockers"]) > 0)
    # 重複キーの答えは読めない答え(後の値で黙って上書きされる。loads_strict)
    for cand in (text.strip(), ):
        try:
            v = loads_strict(cand)
            if shaped(v):
                return v
        except Exception:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            v = loads_strict(m.group(0))
            if shaped(v):
                return v
        except Exception:
            pass
    # **実行できなかったことを、判定にしない。**以前はここで「校閲実行失敗」という
    # ブロックを返していた。Claude の枠切れで校閲セッションが全滅した日、
    # そのブロックを根拠に記事11本が「校閲が下ろさなかった」として紙面から
    # 落とされた(実測 2026-09-06)。動かなかった検査は、答えが無いだけである
    return {"verdict": "error", "error": (err[-200:].strip() or "出力が読めない"),
            "blockers": [], "comments": []}


def review_hint(date: str, name: str, notes_by_file: dict[str, list[str]]) -> str:
    """記事1本の校閲の依頼文に足す注記: lint の機械所見と、前回の書き直しを機械の検査が戻したこと。"""
    hint = ""
    if notes_by_file.get(f"docs/_posts/{name}"):
        hint = ("\n\n## 機械の所見(語の近接で見た仮説。文脈を確かめ、当たっていればブロック、外れていれば無視)\n- "
                + "\n- ".join(notes_by_file[f"docs/_posts/{name}"]))
    kept_why = (REVISE_NOTES.get(date) or {}).get(name[len(date) + 1:].removesuffix(".md"), "")
    if kept_why:
        hint += ("\n\n## 前回の書き直しについて\n執筆は前回の指摘に対して稿を書き直したが、それを適用できず、"
                 f"記事は**前回のまま**になっている(理由: {kept_why})。執筆が指摘を無視したのではない。"
                 "この記事の blockers は**最優先の1件だけ**にし、quote はその1つの誤りに絞り(同じ誤りが見出し・リード・本文の"
                 "複数の欄にあれば、その欄は欄名を付けて全部入れる)、repair は1つにすること"
                 "(執筆が1か所だけ直せば次の巡で通るように)")
    offscope = (OFFSCOPE_NOTES.get(date) or {}).get(name[len(date) + 1:].removesuffix(".md"), "")
    if offscope:
        hint += ("\n\n## 前回の書き直しについて(指摘の外の変更)\n執筆は前回の指摘に応じて書き直したが、指摘の箇所の外も変えた"
                 f"({offscope})。その変更が、指摘に応じるのに要った変更(構造の誤りを直すための組み直し 等)か、素材・出典に基づくかを"
                 "確かめること。誤りを持ち込んでいれば blockers に挙げる。要った変更で正しければ、範囲を外れたこと自体は指摘しない")
    # 続報予約の素材は、執筆に渡したのと同じ読み込み(event_date を予約が指す日に直したもの)を載せる。校閲が予約のファイルを
    # 直接読むと元の話題の開始日を見て、執筆の日付を「素材と合わない」とブロックした(監査指摘 2026-10-12: レトラ・深海フェス)
    fm = parse_front_matter(ROOT / "docs" / "_posts" / name) or {}
    sched_ids = [c for c in fm.get("candidate_ids") or [] if str(c).startswith("sched-")]
    if sched_ids:
        # 執筆と同じ素材索引から引く(当日の予約に加え、前の号から戻した予約の素材は候補ファイルにある。監査指摘)
        window = load_window_candidates(date)
        rows = [window[i] for i in sched_ids if i in window]
        hint += ("\n\n## 続報予約の素材(candidate_ids の sched- の素材。執筆に渡したものと同じ)\n"
                 + (json.dumps(rows, ensure_ascii=False, indent=1) if rows else "(予約に見つからない)"))
    return hint


def scope_hash(date: str, scope: str, manifest: dict[str, str]) -> str:
    """校閲の担当が見る中身の指紋(manifest は review_manifest の形)。記事・社説はそのファイル、紙面全体は全記事と号スナップショット
    (主題の重複・記事の漏れは、どの記事が変わっても変わりうる)。"""
    if scope.startswith("article:"):
        return manifest.get(f"docs/_posts/{scope.split(':', 1)[1]}", "")
    if scope == "editorial":
        return manifest.get(f"docs/_editorials/{date}.md", "")
    seen = {k: v for k, v in manifest.items() if k.startswith(("docs/_posts/", "docs/_editions/"))}
    return hashlib.sha256(json.dumps(seen, sort_keys=True).encode("utf-8")).hexdigest()


def claude_review(date: str, round_no: int, targets: list[str] | None = None,
                  editorial: bool = True, paper: bool = True,
                  carry: dict | None = None, findings: str = "", strict: bool = False,
                  kept: list[str] | None = None) -> dict:
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
    schema = (PROMPTS / "review-schema.json").read_text(encoding="utf-8")
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
    art_ck = (PROMPTS / "review-article.md").read_text(encoding="utf-8")
    # lint の機械所見(時制・発行前の時刻など、語の近接で見るヒューリスティック)は赤にせず、
    # その記事の校閲へ渡す。校閲が文脈を確かめ、当たっていればブロックにする(監査指摘)
    notes_by_file: dict[str, list[str]] = {}
    for l in (findings or "").splitlines():
        m3 = re.match(r"::warning file=(docs/_posts/[^:]+)::(\[所見\].*)$", l)
        if m3:
            notes_by_file.setdefault(m3.group(1), []).append(m3.group(2))
    for n in names:
        hint = review_hint(date, n, notes_by_file)
        jobs.append((n, art_ck.replace("{DATE}", date).replace("{FILE}", n) + hint + again,
                     f"docs/_posts/{n}", f"article:{n}"))
    if editorial and (ROOT / "docs" / "_editorials" / f"{date}.md").exists():
        jobs.append(("社説",
                     (PROMPTS / "review-editorial.md").read_text(encoding="utf-8")
                     .replace("{DATE}", date) + again, f"docs/_editorials/{date}.md", "editorial"))
    if paper:
        # 号スナップショット(digest)への lint 所見は紙面担当に渡す(記事担当には見えない。監査指摘)
        paper_notes = [l.split("::", 2)[-1] for l in (findings or "").splitlines()
                       if re.match(r"::warning file=docs/_editions/", l)]
        hint = (("\n\n## 機械の所見(号スナップショットについて。当たっていればブロック、外れていれば無視)\n- "
                 + "\n- ".join(paper_notes)) if paper_notes else "")
        # 承認済みの紙面に落とした記事を戻す組み直しでは、承認済みのまま残す記事を名指しする。名指しが無いと、戻した記事と
        # 承認済みの記事の重複(P1)で承認済みのほうを落とし、組み直しごと捨てられた(実測 2026-10-12: 宇宙誕生祭の上映会)
        if kept:
            hint += ("\n\n## 承認済みの記事(承認時から中身が変わっていない。P1 で落とすほうにしない)\n- "
                     + "\n- ".join(f"docs/_posts/{n}" for n in kept))
        jobs.append(("紙面全体",
                     (PROMPTS / "review-paper.md").read_text(encoding="utf-8")
                     .replace("{DATE}", date) + hint + again, "-", "paper"))

    # 担当が見る中身の指紋を起動の前に採る。成功した担当だけ「この中身を検めた」と残す(checked)
    from pipelib import review_manifest
    at_start = review_manifest(date)
    t_review = time.time()
    merged = {"verdict": "approve", "blockers": [], "comments": []}
    failed: list[tuple[str, str]] = []   # (scope, 理由)。2回とも動かなかった担当
    QUOTA = ("session limit", "Exceeded USD budget", "rate limit", "usage limit")

    def spawn(prompt: str):
        """担当を起動する。締切を過ぎていれば起動せず None(依頼文を書くあいだに過ぎることもあるので、起動の直前にも確かめる)。"""
        if deadline_passed():
            return None
        short = prompt_file(date, "review-" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8], prompt)
        if deadline_passed():
            return None
        # 起動できなければ、その担当だけの異常終了(LaunchFailed)として collect が扱う(例外で抜けない)
        return start_group(
            ["claude", "-p", short, "--model", REVIEW_MODEL,
             "--json-schema", schema, "--dangerously-skip-permissions",
             # 出典は執筆と同じ fetch_page.py で読ませる。WebFetch は JS を描画せず、公式ポータルの告知が見出しだけに見えて
             # 執筆が読めた事実を「確認できない」とブロックし、記事が落ちた(2026-10-13: スタマス5周年の IM＠S RADIO)
             "--disallowedTools", "WebFetch",
             # 1本ずつなので上限も1本分でよい(紙面まるごとの額を配ると
             # 並列数ぶんの掛け算になる)
             "--max-budget-usd", COMPOSE_ARTICLE_MAX_BUDGET_USD], cwd=ROOT)

    def collect(p, where):
        # 時間切れは子(道具)ごと止め、それまでの出力も残す(判定には使わない)
        so, se, timed_out = finish_group(p, remaining_seconds())
        save_raw(date, f"review-r{round_no}-{where}", so, se, p.returncode)
        # 認証切れ・利用上限は担当の失敗ではない(やり直し・「校閲できなかった記事は載せない」に数えない)。この回を回収し終えてから止める
        blocked = cli_stop("claude", f"校閲 {round_no}巡目 {where}", None if timed_out else p.returncode, so, se)
        if blocked:
            return {"verdict": "error", "error": "CLI 停止", "blocked": blocked, "blockers": [], "comments": []}
        # 採るのは正常に終わったセッションの答えだけ(異常終了の間際に出た approve で校閲済みにしない。監査指摘)
        if timed_out or p.returncode != 0:
            why = "時間切れ" if timed_out else f"exit {p.returncode}: {(se or so or '')[-200:].strip()}"
            return {"verdict": "error", "error": why, "blockers": [], "comments": []}
        return _parse_review(so or "", se or "", where)

    def actionable(x) -> bool:
        """機械が対応できる指摘か。**対応できない指摘に発行を止める権限を持たせない。**

        紙面担当が「記事が漏れている」を file="-" のブロックで返し、直すことも落とす
        こともできないまま5巡回って号が止まった(実測 2026-09-09。しかもその記事は
        計画にあり、別の担当が「新事実なしの続報」として落としたものだった)。
        指摘が指す先が存在する記事か社説でなければ、ブロックではなくコメントにする。
        記事の漏れ(規程11)は選定と拾い直しの仕事で、校閲が止める種類の話ではない。
        """
        f = str(x.get("file") or "")
        return (f.startswith("docs/_posts/") or f.startswith("docs/_editorials/")) and (ROOT / f).exists()

    def absorb(scope, where, r):
        for key in ("blockers", "comments"):
            for x in r.get(key) or []:
                x["scope"] = scope
                # 記事担当・社説担当は見た相手が決まっている。モデルが別の
                # ファイル名を書いてきても、担当のファイルへ寄せる
                if scope != "paper":
                    x["file"] = where
                else:
                    x.setdefault("file", where)
                if key == "blockers" and not actionable(x):
                    x["issue"] = "(対象の記事を指していないためコメント扱い) " + str(x.get("issue") or "")
                    merged["comments"].append(x)
                    continue
                merged[key].append(x)

    # **1回の失敗で担当を失格にしない。**出力が読めない・時間切れは一過性が多い。
    # 一度だけ同じプロンプトでやり直す。枠切れ(QUOTA)は待っても戻らないので再試行しない。
    # 実測 2026-09-08: 23件中1件が「出力が読めない」だっただけで、残り4件の指摘の
    # 修正も除外もせずに号ごと止めていた
    # 起動は1件ずつ一覧に足し、例外・中断で抜けても finally で起動済みの担当を止めて回収する(2件目の起動の失敗で例外になり、
    # 1件目が残って確定後の記事に書いた。監査指摘 2026-10-14)。締切を過ぎたら新しい担当もやり直しも起動せず、その担当は
    # 動かなかった担当(failed)として下の既存の扱い(判定なし・未校閲の記事は落とす・前の判定の引き継ぎ)へ回す
    late = "締切を過ぎたので起動しなかった"
    retry: list[tuple[str, str, str]] = []
    for i in range(0, len(jobs), COMPOSE_WAVE):
        procs, rs = [], []
        try:
            for what, prompt, where, scope in jobs[i:i + COMPOSE_WAVE]:
                p = spawn(prompt)
                if p is None:
                    failed.append((scope, late))
                    continue
                procs.append((scope, where, prompt, p))
            rs = [(scope, where, prompt, collect(p, where)) for scope, where, prompt, p in procs]
        finally:
            stop_groups([p for *_, p in procs])
        # 認証切れ・利用上限: この回の子を全部回収し終えてから工程を止める(組 U)
        stop = [r["blocked"] for *_, r in rs if r.get("blocked")]
        if stop:
            raise CliBlocked(stop[0])
        for scope, where, prompt, r in rs:
            if r.get("verdict") != "error":
                absorb(scope, where, r)
            elif any(q.lower() in str(r.get("error", "")).lower() for q in QUOTA):
                failed.append((scope, r.get("error", "")))
            else:
                retry.append((scope, where, prompt))
    for i in range(0, len(retry), COMPOSE_WAVE):
        procs, rs = [], []
        try:
            for scope, where, prompt in retry[i:i + COMPOSE_WAVE]:
                p = spawn(prompt)
                if p is None:
                    failed.append((scope, "やり直し: " + late))
                    continue
                procs.append((scope, where, p))
            rs = [(scope, where, collect(p, where)) for scope, where, p in procs]
        finally:
            stop_groups([p for *_, p in procs])
        stop = [r["blocked"] for *_, r in rs if r.get("blocked")]
        if stop:
            raise CliBlocked(stop[0])
        for scope, where, r in rs:
            if r.get("verdict") != "error":
                absorb(scope, where, r)
            else:
                failed.append((scope, "2回とも: " + str(r.get("error", ""))[:60]))
    # 引き継ぎは**セッションを走らせたあと**に決める。今回見た担当の指摘だけが
    # 差し替わり、見なかった担当と**動かなかった担当**は前回の指摘を引き継ぐ
    # (動かなかった担当を「見た」扱いにすると、前回のブロックが消えて approve になる)
    seen = {sc for _, _, _, sc in jobs} - {sc for sc, _ in failed}
    if carry:
        for key in ("blockers", "comments"):
            merged[key] += [x for x in (carry.get(key) or [])
                            if x.get("scope") not in seen
                            # 消えた記事の指摘は持ち越さない
                            and (not str(x.get("scope") or "").startswith("article:")
                                 or (ROOT / "docs" / "_posts"
                                     / str(x["scope"]).split(":", 1)[1]).exists())]
    # 前回の書き直しを適用できなかった記事は、ブロックを**最優先の1件**に絞る(形の処理。校閲にもそう頼んでいる)。
    # 執筆が1か所だけ直せば次の巡で通るように。今回の応答だけでなく、持ち越した指摘にも掛ける(監査指摘 r77/r78)
    notes = REVISE_NOTES.get(date) or {}
    if notes:
        kept_once: set[str] = set()
        trimmed = []
        for x in merged["blockers"]:
            sc = str(x.get("scope") or "")
            slug = sc.split(":", 1)[1][len(date) + 1:].removesuffix(".md") if sc.startswith("article:") else ""
            if slug and notes.get(slug):
                if sc in kept_once:
                    continue
                kept_once.add(sc)
            trimmed.append(x)
        if len(trimmed) != len(merged["blockers"]):
            print(f"校閲: 書き直しを適用できなかった記事のブロックを1件ずつに絞った({len(merged['blockers'])} → {len(trimmed)}件)", flush=True)
        merged["blockers"] = trimmed
    # これまでに一度でも校閲できた担当。動かなかった担当の扱いを決めるのに使う
    reviewed_before = set((carry or {}).get("reviewed") or [])
    merged["reviewed"] = sorted(reviewed_before | seen)
    # 担当ごとに、最後に成功したときに見た中身の指紋。巡の終わりの指紋(hashes)は担当が動いたかと関係なく採るので、動かなかった
    # 担当の判定を引き継げるかはこれで決める(hashes で決めると、紙面担当が動かなかった巡の紙面を「検めた中身」と取り違え、
    # 次の巡でまた動かなくても承認になった。監査指摘 2026-10-14)。checked の無い古い記録は hashes で代える
    checked_before = (carry or {}).get("checked")
    if not isinstance(checked_before, dict):
        prev = (carry or {}).get("hashes") or {}
        checked_before = {sc: scope_hash(date, sc, prev) for sc in reviewed_before}
    now_manifest = review_manifest(date)
    merged["checked"] = {sc: h for sc, h in {**checked_before, **{sc: scope_hash(date, sc, at_start) for sc in seen}}.items()
                         if not sc.startswith("article:") or f"docs/_posts/{sc.split(':', 1)[1]}" in now_manifest}
    quota_hit = any(any(q.lower() in why.lower() for q in QUOTA) for _, why in failed)
    if failed and (quota_hit or len(failed) == len(jobs)):
        # **全滅か枠切れのときだけ、巡ごと「判定なし」にする。**呼び出し側はこれを見て
        # 往復も除外もやめ、紙面を確定して人へ渡す(release は approve が無ければ止まる)。
        # 枠切れは待っても号の締切には戻らない
        merged["verdict"] = "error"
        merged["failed"] = [f"{sc}: {why[:80]}" for sc, why in failed]
        print(f"校閲{round_no}巡目: 実行できなかった({'枠切れ' if quota_hit else '全滅'}): "
              + " / ".join(merged["failed"][:3]), flush=True)
    elif failed:
        # 一部の担当だけが2回とも動かなかった。**巡は成立させる。**
        # - 一度も校閲できていない記事は、校閲なしで載せられないので、落とす指摘を付ける
        #   (号を止めるより、その1本を落とすほうが規程に合う)
        # - 前に校閲できている記事・社説・紙面全体は、前回の指摘を引き継ぐだけでよい。ただし**その担当が最後に検めた時点から中身が
        #   変わっていない**ときだけ(checked と今の指紋を比べる)。書き直した記事の再校閲が動かないのに、担当の名前だけの
        #   「校閲済み」で補うと、変わった中身を誰も見ないまま承認になり、release も新しい指紋で通す(監査指摘)
        # - 紙面全体・社説は落とせないので、検めていない中身なら巡ごと判定なしにする(監査指摘 2026-10-14: 記事の担当が通り、
        #   紙面担当だけ2回とも動かないと、紙面全体を誰も見ないまま approve になり release も通した)
        merged["failed"] = [f"{sc}: {why[:80]}" for sc, why in failed]
        unchecked = []
        for sc, why in failed:
            changed = checked_before.get(sc) != scope_hash(date, sc, now_manifest)
            if not sc.startswith("article:"):
                if sc not in reviewed_before or changed:
                    unchecked.append(sc)
                continue
            name = sc.split(":", 1)[1]
            changed = changed and (ROOT / "docs" / "_posts" / name).exists()
            if sc not in reviewed_before or changed:
                merged["blockers"].append({"scope": sc, "file": f"docs/_posts/{name}",
                                           "issue": f"校閲が2回とも実行できなかった({why[:60]})。"
                                                    + ("前の校閲のあとに中身が変わっている。" if changed and sc in reviewed_before else "")
                                                    + "校閲できていない記事は紙面に載せない", "quote": ""})
        print(f"校閲{round_no}巡目: {len(failed)}件の担当が2回とも動かなかった{'' if unchecked else '(巡は成立)'}: "
              + " / ".join(merged["failed"][:3]), flush=True)
        if unchecked:
            merged["verdict"] = "error"
            print(f"校閲{round_no}巡目: 動かなかった担当({', '.join(unchecked)})がいまの中身を検めていない。承認にしない(判定なし)", flush=True)
    if strict and failed and merged["verdict"] != "error":
        # 承認を引き継いだ組み直し(--carry-review)の1巡目は、担当が1つでも動かなければ承認にしない
        # (前の承認で補うと、変わった中身や紙面全体を見ないまま承認になる。監査指摘)
        merged["verdict"] = "error"
        merged["failed"] = [f"{sc}: {why[:80]}" for sc, why in failed]
        print(f"校閲{round_no}巡目: 承認を引き継いだ巡で動かなかった担当がある。承認にしない: " + " / ".join(merged["failed"][:3]), flush=True)
    if merged["blockers"] and merged["verdict"] != "error":
        merged["verdict"] = "block"
    # 次の巡を始めてよいかの判断に使う。見込みではなく**この号の実測**で決める。
    # **1波あたりの時間**で持つ。巡の合計で持つと、49件を見た1巡目の24分を
    # 「次の10件の巡」の見込みに使ってしまい、7分で済む修正の巡を時間切れと誤判定して
    # 飛ばし、直せた10本を全部落とした(実測 2026-09-10)
    took = (time.time() - t_review) / 60
    waves = max(1, -(-len(jobs) // COMPOSE_WAVE))
    STAGE_MIN["校閲"] = took
    STAGE_MIN["校閲1波"] = took / waves
    print(f"校閲{round_no}巡目: {len(jobs)}件を並列で見て "
          f"ブロック{len(merged['blockers'])}件 / コメント{len(merged['comments'])}件 "
          f"({STAGE_MIN['校閲']:.0f}分)", flush=True)
    # approve が「どの中身」に対するものかを残す。release はこれと現在の指紋を照合し、
    # 校閲のあとに変わった紙面を古い approve で出さない(監査指摘)
    merged["hashes"] = now_manifest
    merged["run"] = RUN_ID     # どの実行の巡か(発行は最新の実行の最後の巡を読む)
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


def classify_post_sources(date: str, branch: str) -> bool:
    """その号の記事に載った判定表に無い出典を、**発行前に**決める(登録済みチャンネルの動画 ID は機械で、
    それ以外は合議で判定表に足し、記事の種別を付け直す。失敗したら開始時の中身へ戻す)。
    戻り値 False = 判定表と記事を戻せなかった(commit して当番へ上げた。呼び出し側は止める)"""
    with stage("出典の判定"):
        ok_cls, why_cls = classify_retag_lint(date, posts_only=True, timeout=remaining_seconds(cap=1200), lint=False)
    if not ok_cls:
        if "戻せない" in why_cls:
            commit_and_push(branch, f"compose {date}: 出典の判定に失敗(当番へ)", "compose")
            escalate("compose", date, f"紙面の出典の判定で判定表と記事を戻せなかった: {why_cls[:300]}")
            return False
        if why_cls.startswith(CLI_STOPPED) and CLI_BLOCKED:
            # 判定のモデル(執筆・校閲と同じ CLI)が認証切れ・利用上限で答えない。判定の失敗ではない。投稿は判定が済ませた
            raise CliBlocked(next(iter(CLI_BLOCKED.values())))
        notify("compose", f"{date}: 紙面の出典の判定で失敗({why_cls[:300]})。判定表と記事は戻した。未確認のまま先へ進む(翌朝の watch に出る)", ok=False)
    return True


def classify_revised_sources(date: str, branch: str, t0: float) -> bool:
    """校閲往復の書き直しのあと、その号の記事に未確認の出典が残っていれば判定に掛け直す(classify_post_sources)。
    残っていなければ合議を起こさない。戻り値は classify_post_sources と同じ(False = 止める)"""
    if unresolved_post_sources(date) and afford(t0, "出典の判定", 5.0, "出典の判定(書き直し後)"):
        return classify_post_sources(date, branch)
    return True


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
    ap.add_argument("--carry-review", metavar="JSON", default="",
                    help="承認済みの校閲結果。--reuse-plan と使い、承認済みの記事の判定を引き継いで今回書いた記事だけを校閲する"
                         "(承認済みの号に、落とした記事を戻す組み直し)")
    args = ap.parse_args()
    t0 = time.time()
    date = args.date or edition_date()
    branch = f"edition/{date}"
    triggers = None
    carried = None   # --carry-review: 引き継ぐ承認済みの校閲結果(承認でなければ使わず、全部を校閲する)
    if args.carry_review:
        try:
            c = json.loads(Path(args.carry_review).read_text(encoding="utf-8"))
            carried = c if args.reuse_plan and carry_usable(c, date) else None
        except (OSError, ValueError):
            carried = None
        if carried is None:
            notify("compose", f"{date}: 引き継ぐ承認済みの校閲結果が使えない({args.carry_review})。全部を校閲する", ok=False)

    # 同じ作業ツリーを collect/release/当番と同時に触らない(監査指摘)
    if not args.plan:
        if too_late_to_start(t0, date):
            return 1
        try:
            # 収集の当番が落とした新着を取り直している間は、その号の選定に間に合わせるため開始を待つ(上限40分。
            # pipelib.compose_lock_wait_min。それ以外は10分)。待つ間に当番が呼ばれても待つ分を見直し、収集から当番へ
            # 排他を渡している最中は、空いていても取らない
            _lock = job_lock("compose", wait_min=compose_lock_wait_min(), wait_fn=compose_lock_wait_min,
                             yield_fn=collect_oncall_handover_pending)
        except JobLockTimeout as e:
            notify("compose", str(e), ok=False)
            # 排他を取れないまま締切を過ぎたなら、同じくなぜなぜを発行のあとへ回す(当番を起こすと発行の排他を塞ぐ。監査 2026-10-14)
            too_late_to_start(t0, date)
            return 1
        if too_late_to_start(t0, date):      # 排他を待つ間に締切を過ぎた
            return 1
    if not args.plan and not checkout_edition_branch(date, "compose"):
        return 1
    global STARTED_ON, STARTED_DATE, DEADLINE
    STARTED_ON, STARTED_DATE = branch, date   # checkout に成功したときだけ(--plan では設定しない)
    DEADLINE = hard_deadline(t0, date)

    # **殺されたら、その時点までを確定してから死ぬ。**
    # systemd は起動タイムアウトで SIGTERM を送る。以前はここで何も残らず、
    # 記事40本・社説・号スナップショットが未コミットのまま作業ツリーに散らばり、
    # release も collect も「未コミットの変更がある」で止まった(2026-08-31)。
    # 途中の紙面でも、コミットさえされていれば release が approve を見て判断でき、
    # 人も何が出来ていたか分かる。
    if not args.plan:
        def _on_term(signum, frame):
            print(f"SIGTERM を受けた。ここまでを確定して終了する", flush=True)
            # 途中終了の commit は1経路(_leave_tree_clean: 自分の成果物だけ・開始後だけ)に一本化(監査指摘)
            _leave_tree_clean("SIGTERM")
            notify("compose", f"{date}: 時間切れで打ち切られた。ここまでの成果物は commit を試みた。"
                              f"lint と校閲記録を見て、発行できるか判断すること", ok=False)
            try:
                diagnose_anomalies("compose", date, rerun=False)   # 打ち切りも申告で終えない(なぜ時間切れになったか)
            except Exception as e:      # noqa: BLE001
                print(f"当番(なぜなぜ)の起動に失敗: {e}", flush=True)
            os._exit(1)

        signal.signal(signal.SIGTERM, _on_term)
    # 予約に添えた素材(companion_of)は素材で、予約(トリガー)ではない
    triggers = [t for t in load_scheduled(date) if not t.get("companion_of")]
    number = next_number(date)
    if args.plan:
        print(brand_plan_prompt(date, args.plan, 0,
                                [t for t in triggers if t.get("brand") == args.plan]))
        return 0

    if (ROOT / "docs" / "_editions" / f"{date}.md").exists() and not args.reuse_plan:
        # 二重実行のガード。--reuse-plan は「出来ているところから続ける」なので通す
        # (実測 2026-09-12: lint 赤で止まった号を --reuse-plan で回し直そうとして、ここで中止された)
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
    # 旧い組版が先頭12件で切った予約の facts を、素材として読む前に元素材から復元する(実測 2026-10-07)
    import assemble as _assemble
    for line in _assemble.restore_reservation_facts(date) + _assemble.restore_reservation_companions(date):
        print(line, flush=True)
    for line in carry_late_candidates(date):
        print(line, flush=True)
    for line in carry_dropped_candidates(date):
        print(line, flush=True)
    cands = load_window_candidates(date)
    blocklist = load_blocklist()
    if not cands:
        notify("compose", f"{date}: 発行日±1日の candidates が空。compose 続行不能", ok=False)
        return 1
    plan_path = ROOT / "metrics" / f"plan-{date}.json"
    # 同じ一次情報の候補を、過去の記事・今日の他の候補と同じ話題(dedup_key)につなぎ直す。以後の選定・執筆の既報・台帳は
    # この話題で動く(二度載せを防ぐ。実測 2026-09-15〜30: 遅れて載った記事 97本のうち 42本が前の号の再報道)
    relinked = storylink.link(cands, date, ROOT)
    if relinked:
        print(f"既報の話題につなぎ直した候補 {len(relinked)}件:", flush=True)
        for cid, old, new, why in relinked[:40]:
            print(f"  {cid}: {old} → {new}({why})", flush=True)
    by_brand, n_subjects, hidden_late = write_plan_index(date, cands, blocklist, late_fact_keys(date))
    print(f"選定インデックス: {n_subjects}主題 / {len(by_brand)}面 "
          f"({', '.join(f'{b}:{len(v)}' for b, v in sorted(by_brand.items()))})", flush=True)
    plan_saved = ROOT / "metrics" / f"plan-{date}.json"
    reused = args.reuse_plan and plan_saved.exists()
    if reused:
        # **保存した計画を本当に使う。**以前は旗があるだけで毎回選定し直しており、
        # 再試行で判断が変わると新しい計画と古い記事が混在した(監査指摘)
        plan = json.loads(plan_saved.read_text(encoding="utf-8"))
        print(f"選定を再利用: {plan_saved.name}({len(plan.get('articles') or [])}本)", flush=True)
    else:
        # 選定記録は空で始める(この号は選定を始めた、の印)。読んだと書くのは判定を得た主題だけ(拾い直しのあと、下)。
        # 先に全候補を読んだと書くと、選定が起動できず再実行も成功しない号で、選ばれていない素材が次の号へ引き継がれない
        # (監査指摘 2026-10-14: FileNotFoundError: claude で読了記録あり・計画なし・翌号の引き継ぎ 0件)。記録を消すだけでは、
        # 記録の無い号を遡らない carry_late_candidates で拾えない
        record_candidates_read(date, {})
        with stage("選定"):
            plan = run_plan(date, by_brand, triggers)
        pick_lead(date, plan)
    attach_companions(plan, cands)
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
    # errors = 発行を止める機械検証 / gaps = 止めない取りこぼし
    errors = validate_plan(plan, cands, blocklist)
    gaps, cov = coverage_gaps(plan, cands, blocklist)
    if gaps:
        print(f"取りこぼし: {gaps[0][:120]}", flush=True)
        # **通知で終わらせない。**消えた主題だけをその面の担当に突き返し、
        # 記事化・統合・不採用のどれかを答えさせて計画に反映する。
        # 答えが返ったぶんは取りこぼしでなくなる。残ったものだけを通知する
        with stage("選定の拾い直し"):
            picked = replan_missing(date, plan, by_brand, cands, cov.get("missing_keys") or [])
        for line in picked:
            print(f"  拾い直し: {line}", flush=True)
        if picked:
            shape = repair_plan_shape(plan)
            if shape:
                print("計画の形を修復(拾い直し後): " + " / ".join(shape), flush=True)
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            errors = validate_plan(plan, cands, blocklist)
            gaps, cov = coverage_gaps(plan, cands, blocklist)
        if gaps:
            # 残った主題は、この号では判断されないまま消える。print で終えず、主題を名指しして異常にする(当番のなぜなぜへ)。
            # 面別選定の判定の欠け・知らない判定もここへ来る(planlib.decisions_to_plan。2026-10-06 の洗い出し)
            print(f"拾い直しても残った取りこぼし: {gaps[0][:120]}", flush=True)
            anomaly("compose", "選定で判断されないまま残った主題(拾い直しても答えが無い)。この号では扱われない:\n"
                               + "\n".join(f"- {k}" for k in (cov.get("missing_keys") or [])[:20]))
    if not reused:
        # 選定に掛けた候補のうち、判定を得た主題(面別の選定か拾い直しで、記事・統合・不採用のどれか)の候補を読んだと記録する。
        # 判定の無いまま残った主題(面の答えが無い・崩れた・非0終了で、拾い直しも答えなかった)は未読のまま、次の号が引き継ぐ。
        # これより後にこの号の候補ファイルへ入った候補も次の号が引き継ぐ(carry_late_candidates)。前の号から引き継いだ事実のうち、
        # 選定インデックスの件数上限で選定に渡らなかったものは読んだことにしない
        record_candidates_read(date, cands, hidden_late, unjudged=set(cov.get("missing_keys") or []))
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
        notify("compose", f"{date}: 記事計画が機械検証を通らず。当番に渡す\n- " + "\n- ".join(errors[:8]), ok=False)
        persist_records(date)
        commit_and_push(branch, f"compose {date}: 計画不成立(当番へ)", "compose")
        escalate("compose", date, "記事計画が機械検証を通らず:\n- " + "\n- ".join(errors[:20]))
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
    # 執筆を始める前に、稿ごとの事実の記録のファイルを作る(この号は記録で書いた、の印。初回の保存の前に止まっても、台帳が
    # 記録の無い稿を素材の事実で代えない。監査指摘)
    persist_records(date)
    with stage("執筆"):
        written, aborted = write_articles(date, plan, cands, triggers, load_story_facts(),
                                          reuse=args.reuse_plan)
    outcomes = WRITE_OUTCOMES.get(date) or {}
    n_decl = sum(1 for v in outcomes.values() if v.startswith("見送り"))
    n_drop = len(aborted) - n_decl
    print(f"執筆: {len(written)}/{n_plan}本(見送り {n_decl} / 落とした {n_drop})", flush=True)
    for slug, why in outcomes.items():
        print(f"  {slug}: {why}", flush=True)
    if n_drop:
        # 落としたのは機械検算を2回通らなかった記事。検算が厳しすぎるのか執筆が守れないのかを
        # 人が見られるように、理由ごと知らせる(黙って落とさない)
        notify("compose", f"{date}: 差し戻しても通らず落とした記事 {n_drop}本:\n"
                          + "\n".join(f"- {s}: {w[:200]}" for s, w in outcomes.items() if not w.startswith("見送り")), ok=False)
    # 1b'. 紙面の出典で判定表に無いもの(執筆が自分で見つけた URL)を、**発行前に**決める。
    #      登録済みチャンネルの動画 ID は機械で、それ以外は合議で判定表に足し、記事の種別を付け直す。
    #      収集の取引と同じ(判定表 → 付け直し。失敗したら開始時の中身へ戻す)。lint は号スナップショットが
    #      まだ無いのでここでは掛けず、直後の組版の lint に任せる。以前は翌朝の watch が
    #      「未確認の出典」を鳴らし、昼の収集で直っていた(実測 2026-09-19: 公式チャンネルの動画3件)
    if written and afford(t0, "出典の判定", 5.0, "出典の判定", extra=stage_cost("校閲", 25.0)):
        if not classify_post_sources(date, branch):
            return 1
    # **書き上がりから枠を当てる**(字数を枠に合わせさせない。規程9)。
    # 組版より前に確定させる: 号スナップショットの lead_slug が一面に依存するため
    assign_ranks(date, plan, written)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if len(written) < 1 or (aborted and len(written) < 8):
        notify("compose", f"{date}: 執筆成立 {len(written)}本/計画 {n_plan}本(不成立: {aborted})。下限割れの疑い", ok=False)
    if not written:
        persist_records(date)
        commit_and_push(branch, f"compose {date}: 執筆全滅(当番へ)", "compose")
        escalate("compose", date, f"執筆が全滅(計画 {n_plan}本、不成立 {aborted})。執筆の出力契約・検算・素材の切り出しを疑うこと")
        return 1

    # 1c. 社説: 専任セッション(組版から分離。人格・文体に集中させる)。
    #     執筆は Codex(EDITORIAL_MODEL)。校閲が Claude なので、社説も記事と同じく
    #     執筆と校閲が別ベンダーになる(要件4.5)。Claude で書くと社説だけ同一ベンダーの
    #     自己校閲になってしまうため。
    # 起点にする記事が**実際に書き上がっているか**を確かめる。執筆は不成立になることが
    # あり(aborted)、その記事を起点に指したままだと社説が存在しない記事から書き出す
    # **社説は EDITORIAL_UNTIL 号まで。**それより後の号は書かない・校閲しない(pipelib を参照)
    with_editorial = has_editorial(date)
    ed_slug = plan.get("editorial_slug", "")
    ed_brand = plan.get("editorial_brand", "")
    alive = {a["slug"]: a for a in written}
    if with_editorial and ed_slug not in alive:
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
        run_assemble(date, number)

    with stage("社説と組版" if with_editorial else "組版"):
        errs = run_parallel(([("社説", write_editorial_job)] if with_editorial else [])
                            + [("組版", assemble_job)])
    for e in errs:
        print(f"同時実行のうち失敗: {e}", flush=True)

    # 社説はこの控えより後に出来るので、控えに入れておく。入れないと、
    # 取り直しが失敗して巻き戻したときに「控えに無いファイル」として消される
    if ed_path.exists():
        pre_assembly[0][f"docs/_editorials/{date}.md"] = ed_path.read_bytes()
    if with_editorial and not ed_path.exists():
        # 以前は「組版セッションの lint 修正に委ねる」としていたが、その組版にも
        # lint 修正にも「社説には触るな」と指示しているので、誰も書かずに止まる経路だった。
        # 社説を書けるのは執筆側だけなので、ここで打ち切って人へ渡す
        notify("compose", f"{date}: 社説が2回とも未作成。**社説を書けるのは執筆側セッションだけ**なので、"
                          f"このまま進めても紙面に社説が載らない。人の判断が要る", ok=False)

    # 2. 機械算出の確定 + lint ゲート
    run_derive(date)
    code, lint_out = run_lint(date)
    print(lint_out, flush=True)
    if code != 0:
        # 社説の lint エラーは**執筆側へ返す。**ここで Claude に「docs/ を直せ」と言うと、
        # 校閲側モデルが社説を書き直せてしまい、ベンダー分離(要件4.5)が破れる
        # (実測: 2026-08-28号の社説が校閲側に書き換えられていた)
        ed_errs = [l for l in lint_out.splitlines()
                   if l.startswith("::error") and "docs/_editorials/" in l]
        if with_editorial and ed_errs:
            rewrite_editorial(date, number, plan.get("editorial_slug", ""),
                              plan.get("editorial_brand", ""),
                              [{"file": f"docs/_editorials/{date}.md",
                                "issue": "lint: " + e.split("::", 2)[-1], "quote": ""} for e in ed_errs])
            code, lint_out = run_lint(date)
    if code != 0:
        # **lint の赤をモデルに直させない。**以前は「エラー0になるまで docs/ と stock/ を
        # 修正せよ」と Claude に丸投げしていた(最大2400秒、lint 対象外の文章や台帳まで
        # 変えられる)。機械で直せるものは機械で直す: 記事の固定項目は計画から正規化し、
        # 組版は冪等なのでもう一度呼ぶ。それでも赤なら組版か執筆の欠陥なので、人へ渡す
        print("lint 赤。固定項目の正規化と組版のやり直しで対応する(モデルには直させない)", flush=True)
        by_slug_plan = {a["slug"]: a for a in plan["articles"]}
        for a in written:
            if a["slug"] in by_slug_plan:
                canonicalize_article(date, by_slug_plan[a["slug"]], cands)
        try:
            reassemble(date, number, plan, written)
        except Exception as e:
            print(f"組版のやり直しに失敗: {e}", flush=True)
        code, lint_out = run_lint(date)

        def post_errors(out: str) -> dict[str, list[dict]]:
            by_file: dict[str, list[dict]] = {}
            for l in out.splitlines():
                m2 = re.match(r"::error file=(docs/_posts/[^:]+)::(.*)$", l)
                if m2:
                    by_file.setdefault(m2.group(1), []).append({"file": m2.group(1), "issue": "lint: " + m2.group(2), "quote": ""})
            return by_file

        if code != 0 and post_errors(lint_out):
            # 機械で直せない記事の赤(時制矛盾など本文の話)は、**その記事だけ**を小さな修正セッションへ。
            # 直らなければ落として組版し直す(号を止めない)
            by_file = post_errors(lint_out)
            print(f"lint 赤(記事の中身) {len(by_file)}本を個別に修正させる", flush=True)
            if STRUCTURED_WRITE:
                for f, bs in by_file.items():   # lint の指摘には rule_id が無いので補う
                    for b in bs:
                        b.setdefault("rule_id", "LINT"); b.setdefault("repair", "rewrite_claim")
                        b.setdefault("fact_ids", []); b.setdefault("expected", "lint が通る状態")
                revise_articles(date, by_file, plan, cands, aborted)
                written[:] = [a for a in written if (ROOT / "docs" / "_posts" / f"{date}-{a['slug']}.md").exists()]
            else:
                fix_articles(date, by_file)
            for a in written:
                if a["slug"] in by_slug_plan:
                    canonicalize_article(date, by_slug_plan[a["slug"]], cands)
            # 書き直しで本文の長さが変わる。枠を当て直してから見る(当て直さないと枠の不一致で記事を落とす)
            assign_ranks(date, plan, written, keep_lead=True)
            code, lint_out = run_lint(date)
            still = post_errors(lint_out)
            if still:
                names = [Path(f).name for f in still]
                print(f"直らなかった {len(still)}本を落とす: {names}", flush=True)
                for f in still:
                    log_drop(date, Path(f).name[len(date) + 1:].removesuffix(".md"), "lint 赤",
                             " / ".join(l.split("::", 2)[-1][:100] for l in (lint_out or "").splitlines()
                                        if l.startswith("::error") and f in l)[:160] or "lint の指摘が直らなかった", ROOT / f)
                    (ROOT / f).unlink(missing_ok=True)
                gone = {n[len(date) + 1:].removesuffix(".md") for n in names}
                written[:] = [a for a in written if a["slug"] not in gone]
                aborted.extend(sorted(gone))
            try:
                reassemble(date, number, plan, written)
            except Exception as e:
                print(f"組版のやり直しに失敗: {e}", flush=True)
            code, lint_out = run_lint(date)
        if code != 0:
            notify("compose", f"{date}: lint 赤が解消できず。当番に渡す\n{lint_out[-500:]}", ok=False)
            persist_records(date)
            commit_and_push(branch, f"compose {date}: lint未解消(当番へ)", "compose")
            escalate("compose", date, "lint 赤が解消できず:\n" + lint_out[-3000:])
            return 1

    # **ここで一度コミットする。**以前はコミットが最後の1回しか無く、
    # そのあとの工程で落ちると号が丸ごと消えた(2026-08-31: systemd の起動
    # タイムアウトで殺され、記事40本・社説・号スナップショットが未コミットのまま残り、
    # release も collect も「作業ツリーが汚れている」で止まった)。
    # 校閲前の紙面でも、release は approve を見て発行を止めるので出てしまうことはない。
    # 途中で死んでも、人が lint と校閲記録を見て確定できる状態を残す
    persist_records(date)
    commit_and_push(branch, f"compose {date}: 紙面生成(校閲前・lint green)", "compose")

    # 校閲が既報判定に使う台帳は**組版前の**もの(metrics/stories-before-<日付>.yml)。
    # 組版(assemble.py)がこの号の寄与を剥がした状態を書いている。組版後の台帳を読ませると
    # どの記事も自分自身の記入を根拠に「既報」に見える(実測 2026-09-10)

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
    force_paper = False
    if carried:
        # 承認済みの号に、落とした記事を戻す組み直し。承認済みの記事の判定は引き継ぎ、1巡目から**承認時と中身(指紋)が違う
        # 記事だけ**を見る: 今回書いた記事と、組み直しの途中(出典の付け直し・lint の直し 等)で変わった記事。紙面全体の担当は
        # 必ず見る。2巡目以降と同じ絞り方。全部を見直すと、承認済みの記事に新しいブロックが付いて書き直しが検査で戻され、
        # 戻すために承認済みの記事を失った(実測 2026-10-10 の救済: 1巡目でブロック13件、一面を含む4本の書き直しが戻された)
        retarget = carry_targets(date, carried)
        # 中身が変わった記事は「校閲済み」から外す(前の承認はその中身のものではない。校閲が動かなければ載せない側に倒れる)
        review = dict(carried, reviewed=[s for s in (carried.get("reviewed") or []) if s not in {f"article:{n}" for n in retarget}])
        force_paper = True
        print(f"校閲: 承認済みの判定を引き継ぎ、承認時と中身が違う {len(retarget)}本だけを見る: {', '.join(retarget) or 'なし'}", flush=True)
    retarget_ed = with_editorial
    revised_keys: dict[tuple[str, str], list[str]] = {}   # 直前の巡で書き直しに回した (file, rule_id) → quote 群。次の巡にも同じ箇所に残れば「直せなかった」
    for rounds in range(1, args.max_rounds + 2):
        # 紙面担当(主題の重複・記事の漏れ)は、**記事を直したら走らせる**。
        # 見出しや主題が変われば、別の記事との重複が新しく生まれうる(監査指摘)。
        # 見出しだけ読む担当なので1セッションで済む
        review = claude_review(date, rounds, targets=retarget, editorial=retarget_ed,
                               paper=(retarget is None or bool(retarget) or force_paper), carry=review,
                               findings=lint_out, strict=force_paper,
                               kept=carry_kept(date, carried) if carried else None)
        force_paper = False
        if review.get("verdict") == "approve":
            break
        if review.get("verdict") == "error":
            # 校閲が動かなかった(枠切れ等)。往復しても同じなので、ここで止めて人へ渡す
            notify("compose", f"{date}: 校閲が実行できなかった({'; '.join(review.get('failed', [])[:3])})。"
                              "往復を止めて紙面を確定する。approve が無いので release は止まる", ok=False)
            break
        if rounds > args.max_rounds:
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
        # 書き直しをはさんでも、同じ記事に同じ規則の指摘が残った = 書き手が直せない。人に「落とした」と言う前に当番が診断する
        for b in art_blockers + paper_blockers:
            key = (str(b.get("file") or ""), str(b.get("rule_id") or ""))
            if same_issue_left(b, revised_keys.get(key)):
                note_stuck(date, "校閲", key[0], key[1], str(b.get("issue") or ""), rounds)
        revised_keys = {}

        by_file: dict[str, list[dict]] = {}
        for b in art_blockers + paper_blockers:
            f = b.get("file") or "-"
            if f.startswith("docs/_posts/"):
                by_file.setdefault(f, []).append(b)
        retarget = sorted(f.rsplit("/", 1)[-1] for f in by_file)
        if not (ed_blockers or by_file):
            continue
        # 直す時間が無いなら、直しかけで時間切れになるより、いまの紙面を確定させる。
        # **次の巡の値段は、見直す件数から出す。**前の巡の合計(紙面全体)で見積もると、
        # 49件の巡の24分を10件の巡に当てはめて「時間が無い」と誤判定し、修正を飛ばして
        # 10本を落とした(実測 2026-09-10)。見直すのは指摘の付いた記事+社説+紙面担当だけ
        n_next = len(retarget) + (1 if ed_blockers else 0) + 1
        waves = -(-n_next // COMPOSE_WAVE)
        # もう1巡の値段 = この巡の校閲(波数ぶん) + 書き直し(4分) + 取り直し(組版のやり直し+校閲1波)。
        # そのあとに**最後のサイクル(terminal_cost)が丸ごと残る**ことを afford が要求する
        rework = max(stage_cost("組版", 9) * 1.2, 6) + stage_cost("校閲1波", 4)
        if not afford(t0, "校閲1波", 4, f"校閲の往復({n_next}件・{waves}波)",
                      extra=(stage_cost("校閲1波", 4) * (waves - 1)) + 4 + rework):
            break
        # 社説の書き直しと記事の修正は互いに触らないので同時に走らせる。
        # 記事の修正も1本1セッションで並列(指摘は記事ごとに独立している)
        jobs = []
        if ed_blockers:
            jobs.append(("社説", lambda: rewrite_editorial(
                date, number, plan.get("editorial_slug", ""),
                plan.get("editorial_brand", ""), ed_blockers)))
        revised: dict[str, str] = {}     # slug → 書き直しの結果(revise_articles)
        if by_file:
            if STRUCTURED_WRITE:
                # 中身の修正は**執筆側(Codex)**が構造化で書き直す。校閲側(Claude)に記事を編集させない
                jobs.append(("記事の書き直し", lambda: revised.update(revise_articles(date, by_file, plan, cands, aborted))))
            else:
                jobs.append(("記事の修正", lambda: fix_articles(date, by_file)))
        # 記事の書き直しは、子の子孫を記録して止める(ChildTracker)。ほかに子を起こす仕事と並べると、付け替わった孤児がどの仕事の
        # ものか決められないので、順に走らせる(2026-10-14 監査: 社説の書き直しと並べた過去号で、残った道具が記事を書き換えた)。
        # 社説は 2026-09-06 号で終わっていて、並ぶのは過去号の組み直しだけ
        for e in run_in_turn(jobs):
            print(f"同時実行のうち失敗: {e}", flush=True)
            anomaly("compose", f"校閲の往復({rounds}巡目)の同時実行のうち失敗: {e}")
        # 「書き手が書き直した(稿を出した)」記事だけを次の巡の照合の対象にする。出力が読めなかった・ジョブが失敗した記事に
        # 同じ指摘が残るのは実行の失敗であって、契約の欠陥ではない(監査指摘 r86)
        revised_keys = {}
        for f, bs in by_file.items():
            if revised.get(Path(f).name[len(date) + 1:].removesuffix(".md")) in ("fixed", "kept"):
                for b in bs:
                    revised_keys.setdefault((f, str(b.get("rule_id") or "")), []).append(str(b.get("quote") or ""))
        # 書き直しで落ちた記事(見送り)を written から外す
        written[:] = [a for a in written if (ROOT / "docs" / "_posts" / f"{date}-{a['slug']}.md").exists()]
        run_derive(date)

    # 3'. 往復の書き直し(lint 赤の修正・校閲の指摘)では書き手が新しい出典を引きうる。1b' の判定のあとなので、
    #     ここで掛け直さないと判定表に無い出典が未確認のまま発行される(監査指摘 2026-10-08)。
    #     付け直しで記事の値が変わったら、直後の取り直しが組版をやり直す
    if not classify_revised_sources(date, branch, t0):
        return 1

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
    if changed_posts and review and review.get("verdict") != "error":
        try:
            print(f"記事が {len(changed_posts)}本変わった"
                  f"(中身 {len(changed_read)}本 / 組版が使う値 {len(changed_meta)}本)。取り直す",
                  flush=True)
            persist_records(date)
            commit_and_push(branch, f"compose {date}: 校閲往復のあと(取り直し前)", "compose")
            ed_comments = [c for c in (review.get("comments") or [])
                           if (c.get("file") or "").startswith("docs/_editorials/")]
            jobs = []
            if touches_digest:
                # digest と台帳も直る前の記事から作られている。組版は冪等なので
                # もう一度呼ぶだけでよい(巻き戻しは要らない)
                jobs.append(("組版", lambda: reassemble(date, number, plan, written)))
            if with_editorial and touches_editorial:
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
            run_derive(date)
            rounds += 1
            # 記事は往復で見終わっている。作り直したのは社説と digest なので、
            # 見直すのは社説と紙面担当(digest・主題の重複)。記事は見直さない
            review = claude_review(date, rounds, targets=[], editorial=with_editorial, paper=True,
                                   carry=review, kept=carry_kept(date, carried) if carried else None)
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
        # 校閲が動かなかった巡(error)には指摘が無い。落とすものも無いし、
        # 落としてはいけない(実測 2026-09-06: 実行失敗を指摘とみなして11本を落とした)
        if not review or review.get("verdict") in ("approve", "error"):
            break
        # 1巡は「落とす+社説+組版+校閲」。始めたら最後まで通さないと、
        # 記事だけ消えて digest が古いままの紙面が残る
        # 1巡は「落とす+(社説∥組版)+校閲」。社説と組版は同時に走るので長いほうだけ積む
        # 除外後の校閲は社説+紙面担当だけ(1波)。紙面全体の巡の合計で見積もらない。
        # 組版は作り直さず「抜く」だけなので数分(作り直しは抜き損ねたときの保険)
        # ここが最後のサイクルそのもの(落とす → 社説∥組版 → 校閲1波 → commit)。その分だけあればよい
        if not afford(t0, None, 0, f"ブロック記事の除外({_pass + 1}巡目)", extra=terminal_cost(), terminal=False):
            # ここは**人へ見せる文字列**の一覧。dict を入れると最後の通知組み立てで
            # 落ちる(実測 2026-09-04: 紙面は出来ていたのに compose が例外で終わった)
            unresolved = [f"{b.get('file')}: {(b.get('issue') or '')[:120]}"
                          for b in (review.get("blockers") or [])]
            break
        try:
            dropped, unresolved = drop_blocked_articles(date, review, written)
            if not dropped:
                break  # 機械で落とせる指摘が残っていない(一面・社説・対象不明)
            dropped_by_review += dropped
            print(f"校閲ブロックで {len(dropped)}本を落とす({_pass + 1}巡目): {dropped}", flush=True)
            persist_records(date)
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
            # **組版をもう一度呼ぶ。**冪等なので、落とした記事の痕跡(digest の行・この号で
            # 足した台帳の事実・予約)は再実行で消える。以前は台帳を組版前へ巻き戻して
            # セッションに作り直させ(43本で25分)、締切前にそれが払えず号が止まった(2026-09-11)。
            # 巻き戻しも「抜く」セッションも要らない
            ed_file = ROOT / "docs" / "_editions" / f"{date}.md"
            jobs = [("組版", lambda: reassemble(date, number, plan, written))]
            if with_editorial:
                jobs.insert(0, ("社説", lambda: rewrite_editorial(
                    date, number, plan.get("editorial_slug", ""), plan.get("editorial_brand", ""),
                    [{"file": f"docs/_editorials/{date}.md", "issue": why, "quote": ""}],
                    must_change=must)))
            errs = run_parallel(jobs)
            if errs:
                raise RuntimeError("；".join(errs))
            back = [s for s in dropped_by_review
                    if ed_file.exists() and s in ed_file.read_text(encoding="utf-8")]
            if back:
                raise RuntimeError(f"組版のやり直しが、落とした記事を号に戻した: {back}")

            rounds += 1
            # 残った記事は直していないので見直さない。落としたことで変わるのは
            # 社説と、紙面全体(主題の重複・記事の漏れ)だけ
            review = claude_review(date, rounds, targets=[], editorial=with_editorial, paper=True,
                                   carry=review, kept=carry_kept(date, carried) if carried else None)
        except Exception as e:
            # **途中で落ちたら全部戻す。**記事を消した後に組版のやり直しが失敗すると、
            # 「記事だけ欠けて digest は古いまま」という壊れた紙面が残る
            traceback.print_exc()
            restore_files(pre_assembly)
            run_derive(date)
            dropped_by_review = []
            unresolved = [f"ブロック記事の除外処理が失敗({e})。紙面は組版直後の状態へ戻した"]
            break
    if review and review.get("verdict") == "error":
        unresolved = ["校閲が実行できなかった(判定なし): " + "; ".join(review.get("failed", [])[:3])] + [
            f"{b.get('file')}: {(b.get('issue') or '')[:120]}" for b in review.get("blockers") or []]
    elif review and review.get("verdict") != "approve" and not unresolved:
        unresolved = [f"{b.get('file')}: {(b.get('issue') or '')[:120]}"
                      for b in review.get("blockers") or []]

    # 校閲の修正で本文の長さが変わるため、枠を当て直す。
    # **一面は動かさない**(インパクトで決めた枠であり、号スナップショットの
    # lead_slug が指しているため。ここで変えると組版と食い違う)
    assign_ranks(date, plan, written, keep_lead=True)
    run_derive(date)

    approved = review and review.get("verdict") == "approve"
    code, lint_out = run_lint(date)
    ok = approved and code == 0
    took_min = (time.time() - t0) / 60
    append_metric("compose", {"edition": date, "rounds": rounds, "approved": bool(approved),
                              "lint_green": code == 0, "planned": n_plan, "written": len(written),
                              "roundups": n_rup, "dropped": n_drop, "coverage": cov,
                              "dropped_by_review": dropped_by_review, "stages_min": STAGE_MIN,
                              "target_min": COMPOSE_TARGET_MIN, "over_target": took_min > COMPOSE_TARGET_MIN,
                              "aborted": aborted, "duration_s": int(time.time() - t0)})
    # **目標超過は打ち切りの理由にしない。**遅いことを見えるようにするだけ。
    # 見えないと「今日はたまたま」で済み、直列が残っていても気づけない
    if took_min > COMPOSE_TARGET_MIN:
        detail = " / ".join(f"{k} {v:.0f}分" for k, v in STAGE_MIN.items()) or "(段の実測なし)"
        print(f"目標({COMPOSE_TARGET_MIN}分)を超えた: {took_min:.0f}分 — {detail}", flush=True)
    persist_records(date)   # 最後の commit に含める(後に書くと作業ツリーが汚れて発行が止まる)
    pushed = commit_and_push(branch, f"compose {date}: 紙面生成(校閲{'approve' if approved else '未approve'}・{rounds}往復"
                             + (f"・ブロック{len(dropped_by_review)}本を除外" if dropped_by_review else "") + ")", "compose")
    if not pushed:
        # 紙面がリモートに載っていないのに「準備完了」と言わない(監査指摘: fail-open)。当番へ
        notify("compose", f"{date}: 紙面は出来たが commit/push できなかった。発行できない", ok=False)
        escalate("compose", date, "最終の commit/push に失敗(紙面は作業ツリーにある)")
        return 1
    if ok:
        # 外れた記事は slug ではなく概略(見出し・段・理由)で知らせる(編集長の指摘 2026-09-23)
        notify("compose", f"{date}号 準備完了(校閲{rounds}往復で approve)。06:00 に発行されます" + drop_summary(date) + stuck_summary(date)
               + ("\n(異常は当番がなぜなぜして報告する)" if ANOMALIES or DROP_LOG.get(date) or STUCK.get(date) else ""))
        # 外れた記事・直らなかった指摘・途中の異常の原因を当番に診断させ、欠陥なら直す。外れた記事があり、発行まで時間が
        # 残っていれば、当番は直したあと落とした記事をこの号に戻す(--recover。承認済みの紙面は控えに取り、戻せなければ控えに戻す)。
        # 以前は「号は確定している」として再実行させず、発行前に直したのに2本を戻さなかった(2026-10-10。編集長「復帰保証までが
        # Opus/Sol の仕事だ」)
        until = recover_until(date) if recover_pending(date) else None
        hand_over_anomalies(date, rerun=until is not None,
                            extra_args=["--recover", "--end-at", str(int(until))] if until else None)
        return 0
    reasons = []
    if not approved:
        # 通知を組み立てるところで落ちない。**紙面は出来ているのに compose が
        # 例外で終わる**のが最悪で、人は「また失敗した」としか分からない
        told = [u if isinstance(u, str) else f"{u.get('file')}: {(u.get('issue') or '')[:120]}"
                for u in unresolved[:5]]
        reasons.append(f"校閲{rounds}往復でも未 approve。**機械で落とせなかった指摘**:\n- "
                       + "\n- ".join(told or ["(内訳不明)"]))
    if code != 0:
        errs = [l for l in lint_out.splitlines() if l.startswith("::error")]
        reasons.append(f"lint エラー {len(errs)} 件:\n- " + "\n- ".join(
            e.split("::", 2)[-1] for e in errs[:5]))
    notify("compose", f"{date}号: 発行前に人間判断が必要。当番に渡す。" + "\n".join(reasons), ok=False)
    # 校閲が下ろさなかった指摘や lint 赤で止まった号は、実装の欠陥であることが多い。
    # 人が起きるまで待たず、当番(Opus)が診断・修正し、監査(Sol)を通して回し直す。
    # 外れた記事・直らなかった指摘・途中の異常も同じ当番がなぜなぜする(当番は1日2回までなので分けて呼ばない)
    hand_over_anomalies(date, rerun=True, reason="発行前に人間判断が必要(校閲未 approve か lint 赤)。原因を直して回し直すこと:\n" + "\n".join(reasons)[:3000])
    return 1


STARTED_ON: str | None = None    # main が edition ブランチの checkout に成功したらそのブランチ名
STARTED_DATE: str | None = None  # 同じく、その号の日付(--date で過去号を組み直すときも正しい日付を使う)


def own_artifacts(date: str) -> list[str]:
    """compose が自分で作る成果物のパス(glob)。途中終了時に stage してよいのはこれだけ。"""
    return [f"docs/_posts/{date}-*.md", f"docs/_editions/{date}.md", f"docs/_editorials/{date}.md",
            f"metrics/plan-{date}*.json", f"metrics/plan-lead-{date}.json", f"metrics/review-{date}-*.json",
            f"metrics/stories-before-{date}.yml", f"metrics/pending-before-{date}.yml", f"metrics/{date}.json",
            f"metrics/candidates-read-{date}.json", f"candidates/{date}.json", f"metrics/drops-{date}.json",
            f"metrics/factmap-{date}.json",
            "stock/stories.yml", "stock/pending.yml", "stock/scheduled/*.json", "stock/columnist.md"]


def _leave_tree_clean(reason: str) -> None:
    """途中で落ちても**自分の成果物**を dirty のまま残さない。残すと release も collect も当番も
    「未コミットの変更がある」で拒否し、号が出ない(実測 2026-09-15: 計画ファイルだけ残って
    06:00 の発行と 07:30 の収集が止まった)。

    ただし commit するのは、main が edition ブランチを checkout できた後(STARTED_ON)で、いまも
    そのブランチにいて、かつ compose 自身の成果物(own_artifacts)だけ。checkout 前に既にあった
    変更や、別ブランチ・別工程の変更を compose 名義で commit しない(監査指摘)。"""
    # 起動したまま回収していないセッション(例外・SIGTERM で抜けた担当。別スレッドの担当も)を、確定の前に子孫ごと止めて回収する。
    # 残すと commit のあとに記事を書き、作業ツリーがまた dirty になる(監査指摘 2026-10-14)
    try:
        stop_groups()
    except Exception as e:      # noqa: BLE001  後始末を止めない
        print(f"途中終了時にセッションを止められない: {e}", flush=True)
    date = STARTED_DATE or edition_date()
    if not STARTED_ON or git("branch", "--show-current", check=False).stdout.strip() != STARTED_ON:
        print("途中終了: 開始前の状態か別ブランチなので commit しない", flush=True)
        return
    try:
        try:
            persist_records(date)      # 途中終了でも、外れた記事を次の号の選定に戻せるよう残す(監査指摘)
        except Exception as e:      # noqa: BLE001  後始末を止めない
            print(f"途中終了時に外れた記事の記録を残せない: {e}", flush=True)
        if git("status", "--porcelain").stdout.strip():
            ok = commit_and_push(STARTED_ON, f"compose {date}: 途中終了時点の成果物({reason[:60]})", "compose",
                                 paths=own_artifacts(date))
            if not ok:
                notify("compose", f"{date}: 途中終了時の成果物を commit/push できなかった。作業ツリーを確かめること", ok=False)
            rest = git("status", "--porcelain").stdout.strip()
            if rest:
                notify("compose", f"{date}: 途中終了。compose の成果物ではない変更が残っている(commit しない):\n{rest[:400]}", ok=False)
    except Exception as e:
        print(f"途中終了時の commit に失敗: {e}", flush=True)


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit as e:
        # 判定表の検査などが SystemExit で止める。Exception ではないので、以前は commit も当番も無しに
        # 黙って落ちていた(実測 2026-09-15: 判定表の重複で執筆前に停止)
        code = e.code if isinstance(e.code, int) else 1
        if code:
            notify("compose", f"停止: {e.code}", ok=False)
            _leave_tree_clean(str(e.code))
            try:
                from pipelib import escalate
                escalate("compose", STARTED_DATE or edition_date(), f"SystemExit で停止: {e.code}")
            except Exception:
                pass
    except CliBlocked as e:
        # モデル CLI が認証切れ・利用上限で答えない(投稿は見つけたときに済んでいる)。記事・面を失敗に数えずに止め、
        # 書けた記事と計画は残す(ログインし直し・上限が戻ったあと `compose.py --reuse-plan` で、書けていない記事から組み直せる)。
        # 当番には回さない(当番も同じ CLI で止まる。escalate も CLI_BLOCKED を見て呼ばない)
        print(f"組版を止めた: {e}", flush=True)
        _leave_tree_clean(f"CLI 停止: {e}"[:120])
        code = CLI_BLOCKED_EXIT
    except Exception as e:
        notify_crash("compose", e)
        _leave_tree_clean(f"{type(e).__name__}: {e}")
        # 例外で落ちたら実装の欠陥。当番へ(人が起きるまで待たない)
        try:
            from pipelib import escalate
            escalate("compose", STARTED_DATE or edition_date(), f"例外で停止: {type(e).__name__}: {e}\n{traceback.format_exc()[-2500:]}")
        except Exception:
            pass
        code = 1
    else:
        if code:
            _leave_tree_clean(f"exit {code}")
    # どの終わり方でも、人に「異常」として通知したものは当番がなぜなぜする(既に当番を呼んだ process では重ねて呼ばない。
    # 途中で止まった号の作り直しは当番の判断に任せず、ここでは再実行しない: 止まった理由で呼ぶ経路は上にある)
    try:
        diagnose_anomalies("compose", STARTED_DATE or edition_date(), rerun=False)
    except Exception as e:      # noqa: BLE001 — 診断の起動で終了処理を落とさない
        print(f"当番(なぜなぜ)の起動に失敗: {e}", flush=True)
    sys.exit(code)
