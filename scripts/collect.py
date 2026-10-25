#!/usr/bin/env python3
"""collect: 定点観測+探索+X動向 をまとめて candidates/<号日付>.json に記録し、
edition ブランチへ push する。(PIPELINE §1〜2)

  python3 scripts/collect.py [--no-git] [--skip-watch] [--skip-explore] [--skip-grok]
                             [--force-grok]

- 定点観測(A-1): sources.yml の一覧差分 → 新着 URL の本文を Claude(COLLECT_MODEL)が
  facts 化する。渡された本文を読むだけの役なので安いモデルでよい
- 探索(A-2): **Luna(codex / EXPLORE_MODEL)× 面数ぶん並列**。Web を検索してネタを見つける。
  codex に WebSearch 専用ツールは無いが、sandbox の通信を開けばシェルから検索・取得ができる。
  **読み取り専用**で起動する(取得したページの指示でリポジトリを書き換えられないように)。
  codex には --max-budget-usd 相当が無いため、暴走を止めるのは EXPLORE_TIMEOUT だけ
- X 動向(B): 役割を分ける(編集長 2026-10-04)。
  1. Grok が**面ごとに独立セッション**で X の検索だけをし、見つけた投稿を全文のまま書き出す(prompts/grok-collect.md)
  2. Luna が面ごとにその投稿を読み、リンク先を開いて確かめて候補にする(verify_grok_faces / prompts/grok-verify.md)。
     X の原本でしか確かめられない問いも挙げる
  3. 問いがあれば、週の検索予算(セッション記録から数えた直近7日の実使用)を確かめて、Grok に原本を調べさせ(deep_dive_grok /
     prompts/grok-deep.md)、もう一度 Luna が確かめる
- 正規化・URL 重複マージ → candidates へ追記 → 簡易 verify → commit & push
"""
import argparse
import collections
import html as html_lib
import datetime
import json
import os
import shutil
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import (ENV, ROOT, COLLECT_MODEL, CODEX_WRITE_MODEL, EXPLORE_MODEL,
                     EXPLORE_MAX_BUDGET_USD, JST, JobLockTimeout, job_lock, prompt_file, clean_url, append_metric, classify_source,
                     extract_periods, decode_html, html_to_text, loads_strict, needs_render, partial_output, quote_on_page, read_for_raw, reap, save_raw, schema_ok,
                     RenderFailed, browser_only, render_page, candidate_misplaced, maintenance_http_error, maintenance_redirect,
                     merge_candidate_facts,
                     set_quiet, unbacked_facts,
                     anomaly, anomaly_shape, http_status, whywhy_exempt_status, checkout_edition_branch, classify_retag_lint, collect_oncall_end, commit_and_push, diagnose_anomalies,
                     edition_date, mark_collect_oncall, write_checked, write_atomic, COLLECT_OUTPUTS, settle_collect_outputs,
                     extract_json_array, git, notify, notify_crash, now_jst, prompt_part, render_prompt,
                     X_ANON_POST, x_post_author, x_status_time, align_x_published_date,
                     CLI_BLOCKED, CLI_BLOCKED_EXIT, CLI_STOPPED, CliBlocked, cli_blocked_reason, cli_check, grok_session_reasons,
                     scratch_dir, link_key)
from storylink import url_key
# モデル・道具のセッションの起動と回収(自前のプロセスグループ。時間切れ・中断では子孫まで止める)
from pipelib import PAGE_READ_BYTES, STOPPING, exit_on_term, finish_group, next_slot_end, run_group, start_group, stop_groups


def page_key(url) -> str | None:
    """収集が「同じページか(リンク元を付けるか)」を見る比べ方: リンク先のキー(link_key)を、末尾の / を除いて比べる。
    リンク元の記録は判定の材料なので、末尾の / の違いだけの自分自身をリンク元にしない(種別の引き継ぎは link_key のまま)。"""
    s = str(url or "")
    # 末尾の / を除くのはクエリ・フラグメントの無い URL のパスだけ(クエリの値の / は削らない。監査指摘 2026-10-21)
    if "?" in s or "#" in s or s.rstrip("/").count("/") < 3:
        return link_key(s)
    return link_key(s.rstrip("/"))

# 定点観測の新着を1回の実行で facts 化する上限。1回の Claude 呼び出しに載る量の都合で
# 区切るだけであり、超過分は捨てずに次回へ繰り越す(run_watch の状態保存を参照)。
WATCH_BATCH = int(ENV.get("WATCH_BATCH", "12"))
# facts 化のバッチを同時に走らせる数。新着は件数にかかわらず全部を処理する(上限で打ち切らない。編集長 2026-09-30
# 「全部処理してねーじゃん」)。件数が増えても時間が線形に伸びないよう、バッチは並列に走らせる
WATCH_PARALLEL = int(ENV.get("WATCH_PARALLEL", "3"))
# 通常の収集が2度読めずに諦めた新着を、当番の取り直しのために残す上限(state の _given_up)
GIVEN_UP_MAX = 200
# 定点観測で facts 化のために渡すページ本文の量。切り詰めるとそのぶん facts が痩せる
WATCH_BODY_CHARS = int(ENV.get("WATCH_BODY_CHARS", "20000"))
# 探索(Luna)1クエリの打ち切り。codex には --max-budget-usd 相当が無いので、
# 暴走を止められるのは時間だけになる。**起動時から**数える
EXPLORE_TIMEOUT = int(ENV.get("EXPLORE_TIMEOUT", "900"))
# Grok(X 動向)を回す時刻。JST の「時」をカンマ区切りで指定する。空なら毎回回す。
# SuperGrok は週次のセッション上限があり、1回の収集で10セッション消費するため、
# 収集の頻度とは別に絞る必要がある。ブランド10面は減らさない(絞るのは回数だけ)。
GROK_HOURS = ENV.get("GROK_HOURS", "").strip()
# **手数(--max-turns)では制限しない。**週次上限の実体は X 検索の回数だけで、ページを開く・コマンドを実行する・書く手数は
# 枠を消費しない。手数で縛ると、検索を終えたあとの確認や書き出しの途中で切られて成果ごと失う
# (実測: 3 → 9面すべて0件、8 → 9面中5面0件、12 → 9/15〜10/3 の171セッション中20本が打ち切り・7本は面ごと0件。
#  編集長 2026-10-04「手数の概念が悪い。Web Search だけをカウントしないと意味がない」)。
# 検索の回数は依頼文で指示し(GROK_MAX_SEARCHES)、実際の回数をセッション記録から数えて記録・超過を異常にする(grok_search_counts)。
# 暴走は1面あたりの時間(GROK_TIMEOUT)で止める
# 1面あたりの X 検索回数。**週次上限の実体はこれ**(実測 0.087%/検索 = 週およそ1150検索)。
# まとめ方式にしてから 9面23検索で 2% しか使わなかったので、予算に余裕がある。
#   4回/面 → 36検索/回 = 3.1%/日(週22%)
#   6回/面 → 54検索/回 = 4.7%/日(週33%)  ← ここを採る
#   8回/面 → 72検索/回 = 6.3%/日(週44%)
# 数字を書いても厳密には守られない(「最大10回」で34回引いた実績がある)ため、
# 掘る角度を列挙して「言い換えでは足さない」と縛るほうを主にしている。
GROK_MAX_SEARCHES = int(ENV.get("GROK_MAX_SEARCHES", "6"))
# x_keyword_search が1回に返す件数の上限(道具の上限。limit に大きい値を渡しても 10 件で返る。2026-10-10 に実測)。
# 公式アカウントの一括取得が上限まで返ったら、取りこぼしがある: いちばん古い投稿より前を until_time で引き直す(遡る)。
# 以前は「1回だけ」と縛っていたので、忙しい日の古い投稿を落とした(2026-10-10: 学マス公式の 10/8 19:00〜10/9 11:30 の
# 投稿のうち3件(花海咲季の新アイドル『Agitato』ガシャ告知)は、どの回の収集にも入らなかった)
X_SEARCH_LIMIT = 10
# 公式アカウントを遡る検索の回数の上限(角度の検索 GROK_MAX_SEARCHES とは別枠)。対象期間(2日)に公式の投稿が
# 多い面でも届く数: 学マス公式は1日20件前後 → 2日で4〜5回
GROK_OFFICIAL_PAGES = int(ENV.get("GROK_OFFICIAL_PAGES", "6"))
# 10面を1セッションで回すぶん長い。途中で切れても面ごとにファイルへ書かせているので
# そこまでの成果は残る
GROK_TIMEOUT = int(ENV.get("GROK_TIMEOUT", "3000"))
# 週次上限の X 検索回数(実測 0.065〜0.087%/検索 → 週およそ1150〜1550回。少ないほうに合わせる)と、深掘りに使ってよい割合。
# 深掘り(deep_dive_grok)は、直近7日の実使用(セッション記録から数える)を差し引いた残りの範囲でだけ回す。
# 基本の調べ(1日1回・9面×6回=54回、週378回)を必ず残すため、深掘りは週の上限の DEEP_SHARE までに抑える
GROK_WEEKLY_SEARCHES = int(ENV.get("GROK_WEEKLY_SEARCHES", "1100"))
GROK_DEEP_SHARE = float(ENV.get("GROK_DEEP_SHARE", "0.8"))
# 1回の収集で深掘りに使う検索の上限(問い1つにつき2回まで)
GROK_DEEP_MAX = int(ENV.get("GROK_DEEP_MAX", "24"))
# 面別セッションの同時実行数。多すぎると X 側で絞られるおそれがあるので控えめに置く
GROK_WAVE = int(ENV.get("GROK_WAVE", "3"))
# 推論の深さ。既定は high で走っており、消費の最大費目が reasoning だった
# (1回の収集で 1.59MB。tool_result 495KB・assistant 120KB を大きく上回る)。
# 収集は「調べて写す」作業で深い推論を要さないため medium に落とす。
GROK_EFFORT = ENV.get("GROK_EFFORT", "medium")
# 注: grok のヘッドレス実行には --always-approve が必須(無いとツール実行が承認待ちで
# Cancelled になり前置きだけ返る)。結果は標準出力ではなく**ファイルに書かせる**。
# grok はエージェント型 CLI なので、面ごとにファイルを更新させれば1応答の出力上限に
# 縛られない。--json-schema が max_tokens 切りで全滅したのも、応答本文に全件を載せさせる
# 使い方そのものが原因だったとみている(いずれも実測に基づく)。
STATE_PATH = ROOT / "stock" / "watch-state.json"
UA = "Mozilla/5.0 (compatible; ImasNewsCollect/1.0)"
# 収集のモデル・道具のセッションを打ち切る絶対時刻(epoch 秒。collect_deadline で main が起動時に決める)。None なら締切なし
DEADLINE: float | None = None
# 定点観測の facts 化を、収集の締切で始めなかった・打ち切った(claude_exec の戻り。読めなかった None とは別で、回数に数えない)
DEADLINE_CUT = object()


def collect_deadline(oncall_rerun: bool, now: datetime.datetime | None = None) -> float | None:
    """収集の締切。定時の収集(systemd が起動した: INVOCATION_ID)は、次の定時工程の SLOT_MARGIN_MIN 分前(02:00 の収集なら
    02:45)。そのあとの裏取り・保存・commit を済ませて、03:00 の組版(工程の排他を10分待つ)に引き渡す。Grok の面を順に待つと
    時間切れが足し算になり、02:00 の収集が排他を持ったまま 03:00 の組版を妨げた(監査指摘 2026-10-14)。
    当番の取り直し(当番が時刻を決めて止める)と手で回したときは決めない。"""
    if oncall_rerun or not os.environ.get("INVOCATION_ID"):
        return None
    return next_slot_end(now or now_jst())


# この実行の記録(started_at・trigger、記録を積んだか recorded)。main が起動時に決め、実行記録(append_metric)に載せる
RUN: dict = {}


def run_trigger(oncall_rerun: bool) -> str:
    """起動のされ方: 当番の取り直し(oncall)/ systemd の timer(INVOCATION_ID がある: timer)/ 手で回した(manual)。
    当番は timer の起動した収集から呼ばれて INVOCATION_ID を受け継ぐので、取り直しを先に見る(collect_deadline と同じ)。"""
    return "oncall" if oncall_rerun else "timer" if os.environ.get("INVOCATION_ID") else "manual"


def run_record() -> dict:
    """実行記録に載せる開始時刻と起動のされ方(main を通らない呼び出しでは空)。"""
    return {k: RUN[k] for k in ("started_at", "trigger") if k in RUN}


def deadline_passed() -> bool:
    """収集の締切を過ぎたか。過ぎたら新しいセッション(やり直し・深掘りも)を起動しない。"""
    return bool(DEADLINE) and time.time() >= DEADLINE


def session_left(cap: float) -> float:
    """セッションに待たせてよい秒数。収集の締切を越えない(過ぎていれば 0 = 待たずに止める)。"""
    return cap if not DEADLINE else max(0.0, min(cap, DEADLINE - time.time()))
X_HOSTS = ("x.com", "twitter.com")

# 候補1件のスキーマと編集規程。標準出力に吐かせる場合(Claude)とファイルに書かせる場合
# (Grok)で共用するため、「どこへどう出すか」の指示は含めない。
# 収集の依頼文の共通部品(本文は prompts/)。規則(何を書いてよいか)と形(候補1件の JSON)を分けてある:
# Grok は日本語のまとめを書くので規則だけ、探索・定点観測・写し替えは JSON を返すので形も渡す
COLLECT_RULES = prompt_part("collect-rules")
COLLECT_ITEM = prompt_part("collect-item")


def http_get(url: str, timeout: int = 20) -> str:
    # 素の取得に 429 を返すサイト(browser_only)は描画して読む。定点観測の一覧(type: html の観測先)もここを通る
    # (2026-10-26 編集長「429を出すようなアクセスはするな」。Sol 監査 CHR r1 later)。締切があれば待ちは残り時間で切る
    if browser_only(url):
        return fetch_rendered(url, limit=watch_limit(90))
    return watch_get(url, timeout)[0].decode("utf-8", errors="replace")


def watch_get(url: str, timeout: float) -> tuple[bytes, str | None]:
    """定点観測の取得: (本文, 文字コード)。収集の締切があるときは、接続・ヘッダーの受信も含めて締切までしか待たない
    (urlopen はヘッダーを読み終えるまで返らず、少しずつ届くヘッダーは締切を越えて待った。監査指摘 2026-10-14)。
    取得は別スレッドで行い、締切で見切って TimeoutError を上げる(見切ったスレッドは socket の待ち時間で終わり、何も書かない)。
    本文の受信は watch_read が受信ごとに締切で切る。締切が無ければ今までどおりこのスレッドで取る。"""
    def get() -> tuple[bytes, str | None]:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return watch_read(res), res.headers.get_content_charset()
    if not DEADLINE:
        return get()
    import threading
    box: dict = {}

    def run():
        try:
            box["v"] = get()
        except BaseException as e:      # noqa: BLE001  呼び出し側へ渡す
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(max(0.0, DEADLINE - time.time()))
    if t.is_alive():
        raise TimeoutError("収集の締切を過ぎたので受信を打ち切った")
    if "e" in box:
        raise box["e"]
    return box["v"]


def watch_read(res) -> bytes:
    """定点観測の応答を読み切る。収集の締切があるときは、受信のたびに締切を確かめて打ち切る(socket の待ち時間は1回の受信
    ごとにしか効かず、少しずつ届く応答は締切を越えて読み続けた。監査指摘 2026-10-14)。締切が無ければ今までどおり読む。"""
    if not DEADLINE:
        return res.read()
    # 下の受信(socket から読む1回ごと)の待ち時間を、その時点の残り時間に合わせる。応答の読み方(chunked の長さの行を
    # 1字ずつ読む等)によって1回の read1 の中で何度も受信するので、受信の入口(raw.readinto)で合わせる(監査指摘 2026-10-14)。
    # 待ち時間は urlopen が決めた1回の受信の上限(20 秒)より延ばさない。受信の socket を取れない応答では、urlopen に渡した
    # 待ち時間(残り時間と 20 秒の小さいほう)で切れる
    raw = getattr(getattr(res, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    if hasattr(raw, "readinto") and hasattr(sock, "settimeout") and hasattr(sock, "gettimeout"):
        cap = sock.gettimeout()
        inner = raw.readinto

        def readinto(b):
            left = DEADLINE - time.time()
            if left <= 0:
                raise TimeoutError("収集の締切を過ぎたので受信を打ち切った")
            sock.settimeout(left if cap is None else min(cap, left))
            try:
                return inner(b)
            except TimeoutError:
                if deadline_passed():
                    raise TimeoutError("収集の締切を過ぎたので受信を打ち切った") from None
                raise
        try:
            raw.readinto = readinto
        except AttributeError:
            pass
    chunks = []
    read1 = getattr(res, "read1", None) or (lambda n: res.read(n))
    while True:
        if deadline_passed():
            raise TimeoutError("収集の締切を過ぎたので受信を打ち切った")
        b = read1(65536)
        if not b:
            return b"".join(chunks)
        chunks.append(b)


def fetch_rendered(url: str, timeout: int = 30, limit: float | None = None) -> str:
    """描画したページの HTML。失敗は RenderFailed(終了コードと stderr の要点つき)で上げる(空で返さない)。
    limit は待ってよい秒数の上限(定点観測は収集の締切の残り: watch_limit)。"""
    return render_page(url, timeout, limit=limit) if limit is not None else render_page(url, timeout)


def watch_limit(cap: float) -> float | None:
    """定点観測の取得(一覧・本文・描画)に待ってよい秒数。収集の締切が無ければ None(各取得の既定の上限だけ)。
    締切を過ぎたあとに描画を始めて、組版へ引き渡す時刻を越えない(監査指摘 2026-10-14: 締切後に新着48件の描画を起動した)。
    裏取り(verify)は締切で切らない(描画できずに写しを照合できないと、正しい候補を取り違えとして使えなくする)"""
    return session_left(cap) if DEADLINE else None


def watch_fetch(s: dict):
    """定点観測の一覧の取得関数。収集の締切があるときは、取得の待ちを残り時間で切る(無ければ None = list_source の既定)。"""
    if not DEADLINE:
        return None
    if s["type"] == "portal":
        return lambda u: fetch_rendered(u, limit=watch_limit(90))

    def get(u: str) -> str:
        left = watch_limit(20)
        if left <= 0:
            raise RuntimeError(f"{u} を取得しない: 収集の締切を過ぎた")
        return http_get(u, timeout=max(1, int(left)))
    return get


def rendered_or_note(url: str, limit: float | None = None) -> tuple[str, str]:
    """描画を補いとして使う呼び出し側(facts 化の本文・裏取りの読み直し)向け: (HTML, 失敗の要点)。
    失敗しても素の HTML やセッション側の fetch_page.py で続けられるので止めないが、原因はログと呼び出し側の記録に残す"""
    try:
        return (fetch_rendered(url) if limit is None else fetch_rendered(url, limit=limit)) or "", ""
    except RenderFailed as e:
        print(f"  描画取得の失敗: {e}", flush=True)
        return "", str(e)


# 新着ページの本文の取得が HTTP の応答で失敗したときの状態コード(URL → コード)。facts 化で読めなかった新着のうち、
# 本文が 503 だったものをなぜなぜの対象外にするのに使う(WHYWHY_EXEMPT_HTTP。Sol 監査 N503 r1: 一覧は読めても本文が 503 だと、
# 「facts 化で読めなかった新着」と新規0件のまとめが当番を起こした)
BODY_HTTP: dict[str, int] = {}


def page_html_or_note(url: str, csr: bool) -> tuple[str, str]:
    """facts 化に渡す新着ページの HTML: (HTML, 失敗の要点)。描画必須の観測先は描画し、それ以外は素の HTML を取る
    (素の HTML で本文が空同然なら描画し直す)。取れなければ ("", 要点) で、呼び出し側はセッションの fetch_page.py に任せる。
    素の HTML のページも本文を渡す。WebFetch は要約しか返さず、写し(原文どおりの引用)が取れないので読めなかった扱いになる
    (2026-10-09: ランティスの SideM 特典告知2件)"""
    BODY_HTTP.pop(url, None)      # 前の回の状態コードを持ち越さない(この回の取得の結果だけ)
    # 素の取得に 429 を返すサイト(browser_only)は素の HTML を取らずに描画する(2026-10-26 編集長「429を出すようなアクセスはするな」)
    if csr or browser_only(url):
        return rendered_or_note(url, limit=watch_limit(90))
    left = watch_limit(20)
    if left is not None and left <= 0:
        return "", "収集の締切を過ぎたので取得しなかった"
    try:
        raw, cs = watch_get(url, 20 if left is None else max(1, left))
    except (OSError, ValueError) as e:
        if http_status(e) is not None:
            BODY_HTTP[url] = http_status(e)
        return "", f"取得に失敗({type(e).__name__}: {e})"[:400]
    if needs_render(url, html_to_text(raw, cs)):
        t, why = rendered_or_note(url, limit=watch_limit(90))
        if t:
            return t, ""
    # 文字コードの判定は本文の取得の共通処理(decode_html)と同じ。Shift_JIS 宣言で CP932 の拡張文字を含むページを、ここだけ
    # 置換文字だらけで読んでいた(監査指摘 2026-10-14)
    return decode_html(raw, cs), ""


def fragment_section(html: str, url: str) -> str:
    """URL が `#id` で1ページの中の1件を指すとき、その件の部分だけを返す(id を持つ要素から、同じ形の次の要素の手前まで)。
    1ページに多数の告知が並ぶ観測先(ランティスの SideM)で、ページ全体を渡すと上限で切れたり、ほかの件を混ぜたりする。
    id がページに無ければページ全体を返す"""
    frag = urllib.parse.unquote(urllib.parse.urlsplit(url).fragment)
    if not frag:
        return html
    m = re.search(r'<(\w+)([^>]*?)\s(?:id|name)=["\']' + re.escape(frag) + r'["\']', html)
    if not m:
        return html
    nxt = re.compile(r'<' + m.group(1) + re.escape(m.group(2)) + r'\s(?:id|name)=["\']').search(html, m.end())
    return html[m.start():nxt.start() if nxt else len(html)]


# ---- A-1 定点観測 --------------------------------------------------------------

def list_source(s: dict, known: set[str], fetch=None, resume=None) -> tuple[list[tuple[str, str]], int, list[dict]]:
    """観測先の一覧から (URL, 見出し) を集める。戻りは (一覧の並び, 読んだページ数, 未完了の境界の並び)。

    **件数で打ち切らない。**一覧の先頭ページだけを見ていたとき、公式ニュースは毎回 12 件しか見えず、実行と実行の
    あいだに 12 件を超えて更新されると、押し出された記事を定点観測が一度も見ない(編集長 2026-09-30「公式のニュース
    12件しか見ないとか設計不備過ぎる」)。`page_url`(`{n}` がページ番号)を持つ観測先は、**既に見た記事だけの
    ページに行き当たるまで**遡る。`max_pages` まで読んでもまだ新着が続くなら、読んだ最後のページを未完了の境界
    ({"page": ページ番号, "after": そのページで初めて出た URL の並び})として返す(呼び出し側が状態に残し、異常として上げる)。
    空の並びは、一覧を読み終えた(既に見た記事に行き当たった・一覧の終わり)。

    resume は前回までの未完了の境界。境界の先はまだ読んでいないので、手前の既読だけのページに行き当たっても止まらず、
    境界を越えた先を `max_pages` まで読む(2026-10-14 監査指摘: 打ち切った回が手前のページを既読にするので、上限を上げて
    当番が取り直しても先頭の既読ページで巡回を終え、奥の新着を回収できなかった)。境界までのページは飛ばさずに順に読む
    (記事が増えると境界は奥へ、消えると手前へずれる。記録したページ番号へ飛ぶと、手前へずれた境界の先を読まずに失った。
    監査指摘)。境界の URL が一覧から消えていれば越えられないので、一覧の終わりに着くまで未完了として残す(黙って捨てない)。
    """
    fetch = fetch or (fetch_rendered if s["type"] == "portal" else http_get)
    found: list[tuple[str, str]] = []
    paged = bool(s.get("page_url"))
    max_pages = int(s.get("max_pages") or 1) if paged else 1
    marks = sorted((r for r in (resume or []) if paged and isinstance(r, dict) and r.get("after")),
                   key=lambda r: int(r.get("page") or 1))
    passed = [0] * len(marks)     # 境界ごとに 0: まだ / 1: 境界の URL を読んでいる / 2: 越えた(境界の後ろの項目を読んだ)
    target = None                 # いま向かっている境界(None は先頭からの巡回)
    read_pages: set[tuple[str, ...]] = set()     # 読んだページの中身(URL の並び)
    last, last_settled = [], True                # 最後に初めて出た項目のあったページの URL の並びと、それが既読だけだったか
    pages, n, left = 0, 1, max_pages
    while True:
        url = s["url"] if n == 1 else s["page_url"].format(n=n)
        html = fetch(url)
        if not (html or "").strip():
            # 取得の失敗(空の応答)を一覧の終わりと取り違えない。手前のページだけ既読にすると、
            # 次回は手前で止まって奥の新着を見ない(監査指摘 r102)。この観測先は今回まるごと失敗にし、次回同じ境界からやり直す
            raise RuntimeError(f"{url} の取得に失敗(空の応答)")
        pages += 1
        left -= 1
        page_urls, on_page = [], []
        for m in re.finditer(s["list_regex"], html, re.DOTALL):
            u = m.group(1)
            if not u.startswith("http"):
                u = s["base"].rstrip("/") + "/" + u.lstrip("/")
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2) if m.lastindex >= 2 else "")).strip()
            page_urls.append(u)
            if u not in {x for x, _ in found} and u not in {x for x, _ in on_page}:
                on_page.append((u, title))
        # 項目の無いページ・前に読んだページと同じ中身のページ(範囲外のページに1ページ目や最後のページを返すサイト)= 一覧の終わり。
        # 前に読んだ項目の一部だけのページ(読んでいる間に一覧がずれて重なった)は終わりにしない(監査指摘)
        if not page_urls or tuple(page_urls) in read_pages or not paged:
            found += on_page
            return found, pages, []
        read_pages.add(tuple(page_urls))
        found += on_page
        for u, _ in on_page:
            for i, r in enumerate(marks):
                if passed[i] < 2 and u in r["after"]:
                    passed[i] = 1
                elif passed[i] == 1:
                    passed[i] = 2
        if on_page:
            last, last_settled = [u for u, _ in on_page], all(u in known for u, _ in on_page)
            if last_settled:          # 既に見た記事だけのページ
                rest = [i for i, p in enumerate(passed) if p < 2]
                if not rest:
                    return found, pages, []
                if target is None or passed[target] == 2:
                    # まだ越えていない境界へ、ページを飛ばさずに向かう。上限は、境界を記録したページまでの残りに、
                    # 境界の先の max_pages を足した分(境界の記録したページは余裕の1ページとして数えない)
                    target = rest[0]
                    left = max(int(marks[target].get("page") or 1) - n, 0) + max_pages + 1
        if left <= 0:
            break
        n += 1
    # 上限まで読んだ: 新着の続くページで止まったらそこを新しい境界に、まだ越えていない境界は奥へずらして残す
    cut = [] if last_settled else [{"page": n, "after": last}]
    for i, r in enumerate(marks):
        if passed[i] == 0 or (passed[i] == 1 and last_settled):
            cut.append({"page": max(int(r.get("page") or 1), n), "after": r["after"]})
    return found, pages, cut


def run_watch(claude_call, oncall_rerun: bool = False) -> tuple[list[dict], dict]:
    sources = yaml.safe_load((ROOT / "sources.yml").read_text(encoding="utf-8"))
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    new_items = []  # {source_id, brand, url, title, source_type}
    found_by_source = {}  # 状態の保存は facts 化の後に行うため、巡回結果を持ち越す
    stats = {}
    # 一覧を上限で打ち切った観測先の未完了の境界(観測先 → 境界の並び)。次回(当番の取り直しを含む)はその先から読む(list_source)
    cuts = {k: v for k, v in state["_cut"].items() if isinstance(v, list)} if isinstance(state.get("_cut"), dict) else {}
    late: list[str] = []
    for s in sources:
        if not s.get("enabled"):
            continue
        if deadline_passed():
            # 収集の締切を過ぎたら一覧を読まない(既読は更新しないので、次の取り直しが同じ境界から読む)。組版の開始を妨げない
            stats[s["id"]] = {"error": "収集の締切を過ぎたので一覧を読まなかった"}
            late.append(s["id"])
            continue
        try:
            # 締切があるときは、一覧の取得の待ちも残り時間で切る(締切で切った観測先は例外で抜け、境界・既読を変えない)
            listed, pages, cut = list_source(s, set(state.get(s["id"], [])), fetch=watch_fetch(s), resume=cuts.get(s["id"]))
            if s["id"] not in state:
                # 初めて見る観測先: いまの一覧は「既に見た」として記録するだけ(過去の記事を新着として候補に流さない。
                # 流すと、古い知らせが今日の記事として載る)。次の実行から、これより新しいものが新着になる
                found_by_source[s["id"]] = [u for u, _ in listed]
                stats[s["id"]] = {"found": len(listed), "pages": pages, "new": 0, "baseline": True}
                cuts.pop(s["id"], None)
                continue
            found = []
            for u, title in listed:
                found.append(u)
                if u not in state.get(s["id"], []):
                    new_items.append({"source_id": s["id"], "brand": s["brand"], "url": u,
                                      "title": title, "source_type": s.get("source_type", "公式"),
                                      "csr": s["type"] == "portal"})
            found_by_source[s["id"]] = found
            stats[s["id"]] = {"found": len(found), "pages": pages,
                              "new": len([n for n in new_items if n["source_id"] == s["id"]])}
            if cut:
                # 上限のページまで読んでも新着が続いた(か、前回打ち切った境界を越えられない)= それより古い新着をまだ見ていない。
                # 読んだ位置を境界として残し、次回(当番の取り直しを含む)はその先から読む。申告で終えずに当番がなぜなぜする
                cuts[s["id"]] = cut
                stats[s["id"]]["unfinished"] = len(cut)
                notify("collect", f"定点観測 {s['id']}: {pages}ページ読んでも、一覧の読んでいない区間が残った(新着が上限を超えた"
                                  f"か、一覧の形が変わった)。読んだ位置を残し、次回はその先から読む(残った区間の新着はまだ素材に無い)", ok=False)
            else:
                cuts.pop(s["id"], None)
        except Exception as e:
            # 既読は更新しない(found_by_source に入れない)ので、次回同じ境界からやり直す。見えない失敗にしない(当番のなぜなぜへ)
            # 原因(描画取得なら終了コードと stderr の要点)まで残す。切り詰めて「空の応答」だけにしない
            code = http_status(e)
            # 状態コードは取得の失敗の形として残す(まとめの異常から 503 の系統を除くのに使う。collect_health)
            stats[s["id"]] = {"error": str(e)[:400], **({"http": code} if code is not None else {})}
            msg = f"定点観測 {s['id']}: 一覧を読めなかった({str(e)[:400]})。既読は更新せず、次回やり直す"
            if whywhy_exempt_status(code):
                # 503 はなぜなぜの対象外: 人には知らせるが、当番に渡す台帳には積まない(WHYWHY_EXEMPT_HTTP)
                notify("collect", msg, ok=False, ledger=False)
                continue
            # 形の鍵は観測先と失敗の種類(HTTP の状態コード、それ以外は例外の型)。同じ号で当番が診断し監査が承認した型なら、
            # 次の収集は当番を起こさない(2026-10-23〜24: ランティスのメンテナンスの 503 を 3回の収集が当番に渡した)
            anomaly_shape("collect", msg, f"watch-unreadable:{s['id']}:"
                          + (f"HTTP{code}" if code is not None else type(e).__name__))
            notify("collect", msg, ok=False)
    if cuts:
        state["_cut"] = cuts
    else:
        state.pop("_cut", None)
    if late:
        notify("collect", f"定点観測: 収集の締切を過ぎたので、観測先 {len(late)}件の一覧を読まなかった(既読は更新せず、"
                          f"当番の取り直しで読む): {', '.join(late)}", ok=False)

    # 前回 facts 化の出力が読めずにやり直しになった新着(未処理の列)を**先に**処理する。それらは既読にしていないが、一覧の上では
    # 既読のページの奥に隠れるので、一覧を遡っても二度と見えない(監査指摘 r102)
    pending = [it for it in state.get("_pending", []) if isinstance(it, dict) and it.get("url")]
    # 当番の取り直しでは、通常の収集が2度読めずに諦めた新着(_given_up)も**先頭で**取り直す。諦めたものは既読なので、
    # 一覧からも未処理の列からも二度と見えない。これを当番が直しても拾えないと、落とした新着が選定リストに入らない(監査指摘)
    pending = ([it for it in state.get("_given_up", []) if isinstance(it, dict) and it.get("url")] if oncall_rerun else []) + pending
    pending = list({it["url"]: it for it in reversed(pending)}.values())[::-1]   # 同じ URL は先に出たほうだけ
    pending_urls = {it["url"] for it in pending}
    # 一覧にもまだ出ている繰り越し記事も、未処理の列の位置(先頭)で処理する(一覧の位置だと、また上限の外に回る。監査指摘 r103)
    new_items = pending + [n for n in new_items if n["url"] not in pending_urls]

    # 新着を facts 化。1回のプロンプトに載る量で WATCH_BATCH 件ずつに区切り、**件数にかかわらず全部を処理する**
    # (打ち切って残りを次の実行(5〜6時間後)へ回すと、その分だけ紙面が1日遅れる)。バッチは WATCH_PARALLEL 本ずつ同時に走らせる。
    # 次回へ回るのは、出力が読めなかったバッチ(やり直し)だけ
    from concurrent.futures import ThreadPoolExecutor
    batches = [new_items[i:i + WATCH_BATCH] for i in range(0, len(new_items), WATCH_BATCH)]
    cands, attempted, blocked, unavailable = [], set(), set(), set()
    with ThreadPoolExecutor(max_workers=max(1, WATCH_PARALLEL)) as ex:
        try:
            for got, tried in ex.map(lambda b: facts_batch(b, claude_call, state, oncall_rerun, blocked, unavailable), batches):
                cands += got
                attempted |= tried
        except BaseException:
            # 中断(SIGTERM・例外)では、まだ始めていないバッチを取り消す(後始末のあとに次のバッチが CLI を起動しない。
            # 走っているバッチの起動は STOPPING が断る。監査指摘 2026-10-14)
            ex.shutdown(wait=False, cancel_futures=True)
            raise
    return finish_watch(new_items, attempted, found_by_source, stats, state, cands,
                        retried_given_up={it["url"] for it in state.get("_given_up", []) if isinstance(it, dict)} if oncall_rerun else set(),
                        blocked=blocked, unavailable=unavailable)


def facts_batch(batch: list[dict], claude_call, state: dict, oncall_rerun: bool = False,
                blocked: set | None = None, unavailable: set | None = None) -> tuple[list[dict], set[str]]:
    """新着1バッチを facts 化する。戻りは (候補, 処理を試みた URL)。読めなかったページは、2回目で諦めたもの以外は試みたことにしない。
    モデルの CLI が認証切れ・利用上限で答えなかったバッチは、読めなかった回に数えず、全部を未処理の列に残す(URL を blocked に足す)。
    本文が HTTP 503 で読めなかったページも、回数に数えず未処理の列に残す(URL を unavailable に足す。なぜなぜの対象外)。"""
    cands = []
    if batch and claude_call and "claude" in CLI_BLOCKED:
        # この収集で既に認証切れ・利用上限を見た: 本文を取りに行かず、そのまま未処理の列へ(下と同じ扱い)
        if blocked is not None:
            blocked |= {it["url"] for it in batch}
        return [], set()
    if batch and claude_call:
        blobs, codes = [], {}
        for it in batch:
            if deadline_passed() or STOPPING.is_set():
                break       # 収集の締切を過ぎた・工程を終えるところなら本文も取らない(下で、このバッチを始めずに未処理の列へ残す)
            t, why = page_html_or_note(it["url"], it["csr"])
            codes[it["url"]] = BODY_HTTP.get(it["url"])     # この回の本文の取得の状態コード(HTTP の応答で失敗したときだけ)
            # 本文の切り詰めは facts の情報量に直結する。2500字にしていたとき、
            # 本文8,219字のページから facts を359字しか起こせていなかった
            body = html_to_text(fragment_section(t, it["url"]).encode("utf-8", "replace"), "utf-8")[:WATCH_BODY_CHARS]
            blobs.append({"url": it["url"], "title": it["title"], "brand_hint": it["brand"], "rendered_text": body, "render_error": why})
        # 素材は**人が読める形**(Markdown、1行が長くならない)で渡す。1行 60KB の JSON にすると Read ツールが
        # 行を切り詰めてモデルが本文を読めず、Bash で開けようとして時間切れになる(実測 2026-09-17: 420 秒)
        material = "\n\n".join(
            f"### {i + 1}. {b['url']}\n- title: {b['title'] or '(なし)'}\n- brand_hint: {b['brand_hint']}\n"
            + ("- 本文(取得済み):\n" + b["rendered_text"].strip() if b["rendered_text"].strip()
               else f"- 本文: (取得できず{'(' + b['render_error'][:200] + ')' if b['render_error'] else ''}。URL を fetch_page.py で読むこと)")
            for i, b in enumerate(blobs))
        prompt = render_prompt("watch-facts", RULES=COLLECT_RULES, ITEM=COLLECT_ITEM, MATERIAL=material)
        try:
            got = DEADLINE_CUT if deadline_passed() or STOPPING.is_set() else claude_call(prompt, timeout=420)
        except CliBlocked:
            # 認証切れ・利用上限(投稿は済み)。新着の失敗ではない: 回数(_unreadable)を進めず、諦めず、異常にもしない。
            # 試みたことにしないので、全部が未処理の列(_pending)に残り、次の収集が一覧より先に読む
            # (2026-10-20 18:30 の認証切れを「読めなかった1回目」と数え、20:29 の再実行で新着を諦めた)
            if blocked is not None:
                blocked |= {it["url"] for it in batch}
            return [], set()
        if got is DEADLINE_CUT or STOPPING.is_set():
            # 収集の締切を過ぎたら facts 化を始めない・締切で打ち切った(本文の取得・依頼文の準備のあいだに過ぎることもあるので、
            # 起動の直前にも確かめる: claude_exec。取得の待ちは残り時間で切ってある: watch_limit)。工程を終えるところも同じ。
            # 試みなかった新着として未処理の列に残し、読めなかった回数に数えない。当番の取り直しが先に処理して、この号の素材に入れる
            if not STOPPING.is_set():
                notify("collect", f"定点観測: 収集の締切で、新着 {len(batch)}件の facts 化を始めなかった・打ち切った(未処理の列に残す):"
                                  + "".join(f"\n  - {it['url']}" for it in batch), ok=False)
            return [], set()
        # 出力は新着ページごとの結果(抽出済み/対象外/読めなかった)。処理済みにするのは、結果が契約どおりに返った
        # extracted・none のページだけ。読めなかった・応答に無い・形が崩れたページは未処理(0件の [] とは別)。
        # 以前は候補の配列だけを受け、[] をバッチ全件の「候補なし」として既読にしていたので、WebFetch で
        # 本文を読めなかったページも黙って既読になり、新着を失った(監査指摘 watch-read-status-contract)
        done = watch_page_results(got, len(batch))
        failed = [it for i, it in enumerate(batch) if i + 1 not in done]
        # 本文が 503 で読めなかった新着は、なぜなぜの対象外(WHYWHY_EXEMPT_HTTP)。人には知らせ、読めなかった回数に数えず
        # (諦めて既読にしない。当番の取り直しも来ないので、諦めると新着を失う)、未処理の列に残して次の収集が読み直す
        # 503 のせいと言えるのは、モデルが契約どおりに答え、そのページを unreadable とした場合だけ。出力が読めない・時間切れ・
        # 答えに無い・形が崩れたページは、503 と別の失敗として従来どおり扱う(Sol 監査 N503 r2: 本文の 503 が facts 化の時間切れを隠した)
        said = {r["page"] for r in (got if isinstance(got, list) else [])
                if schema_ok(r, WATCH_PAGE_SCHEMA) and type(r.get("page")) is int and r.get("status") == "unreadable"}
        unavail = [it for i, it in enumerate(batch) if i + 1 in said and i + 1 not in done
                   and whywhy_exempt_status(codes.get(it["url"]))]
        failed = [it for it in failed if it not in unavail]
        if unavail:
            if unavailable is not None:
                unavailable |= {it["url"] for it in unavail}
            notify("collect", f"定点観測: 新着 {len(unavail)}件の本文が HTTP 503 で読めなかった。次回に持ち越す"
                              + "".join(f"\n  - {it['url']}" for it in unavail), ok=False, ledger=False)
        bad = state.setdefault("_unreadable", {})   # url → 読めなかった回数
        give_up = []
        if failed and not oncall_rerun:
            # 読めなかったページは既読にしない(次回そのまま拾い直す)。ただし同じ URL が2回読めなければ諦めて既読にする。
            # 毎回同じ先頭バッチをやり直すと、上限の外の新着が永久に後回しになる(監査指摘)
            # 当番の拾い直し(直後に同じバッチを再実行)では諦めない。原因が途中切れ等で直しが効いていなければここでも
            # 読めないが、既読にすると**原因未確定のまま新着を失う**(監査指摘 rerun-second-unreadable-drops-pending)。
            # 回数も進めず未処理の列に残し、残れば main が非0で「まだ直っていない」と申告する。
            for it in failed:
                bad[it["url"]] = bad.get(it["url"], 0) + 1
            give_up = [it for it in failed if bad[it["url"]] >= 2]
            for it in give_up:
                bad.pop(it["url"], None)   # 諦めたら回数も消す。残すと再登場時に1回で即既読になる(監査指摘)
                # 諦めても捨てない: 当番が原因を直したあとの取り直し(--oncall-rerun)が拾えるよう、諦めた新着として残す
                # (通常の収集では読み直さない。毎回先頭で読み直すと上限の外の新着が後回しになるため)
                state.setdefault("_given_up", []).append({**it, "given_up_at": now_jst().isoformat(timespec="seconds")})
        if failed:
            notify("collect", f"定点観測: facts 化で読めなかった新着 {len(failed)}/{len(batch)}件"
                              f"({'出力が読めない' if got is None else 'ページの結果が読めなかった・欠けた'})。次回に持ち越す"
                              f"(2度読めず通常の収集では諦めたもの {len(give_up)}件。当番が直したあとの取り直しで読み直す"
                              + "".join(f"\n  - {it['url']}" for it in give_up) + ")", ok=False)
        for i, it in enumerate(batch):
            if i + 1 in done:
                bad.pop(it["url"], None)
        cands = [c for i in sorted(done) for c in done[i]]
        for i in sorted(done):
            # 新着ページと違う url の候補は、そのページに載っていたリンク(まとめの一次ソースなど)。リンク元にそのページを残す
            page = batch[i - 1]["url"]
            for c in done[i]:
                if not c.get("link_from") and page_key(clean_url(c.get("url")) or "") not in (None, page_key(page)):
                    c["link_from"] = page
        for c in cands:
            c["_via"] = "watch"
        batch = [it for i, it in enumerate(batch) if i + 1 in done] + give_up
    return cands, {it["url"] for it in batch}


def watch_page_results(got, n: int) -> dict[int, list[dict]]:
    """facts 化の出力(新着ページごとの結果)から、処理済みのページ番号 → 候補 を返す。

    処理済みは status が extracted(候補1件以上)か none(候補0件)で、番号が 1〜n に1度だけ現れるページ。
    unreadable・番号の重複・形の崩れ(status と items が食い違う等)・応答に無いページは含めない(未処理として残す)。"""
    if not isinstance(got, list):
        return {}
    # 番号は真偽値を除いた整数だけ(Python では True が 1 として数えられ、ページ1が既読になる。監査指摘)
    page_no = lambda r: r["page"] if isinstance(r, dict) and type(r.get("page")) is int else None
    seen: dict[int, int] = {}
    for r in got:
        if page_no(r) is not None:
            seen[r["page"]] = seen.get(r["page"], 0) + 1
    done = {}
    for r in got:
        if not schema_ok(r, WATCH_PAGE_SCHEMA) or page_no(r) is None or not 1 <= r["page"] <= n or seen[r["page"]] != 1:
            continue
        items = r.get("items")
        # 候補は正規化で捨てられない形で事実のあるものだけ(1件でも崩れていれば、そのページは未処理。candidate_usable を共有。監査指摘)
        if not isinstance(items, list) or not all(candidate_usable(c) for c in items):
            continue
        if (r.get("status") == "extracted" and items) or (r.get("status") == "none" and not items):
            done[r["page"]] = items
    return done


def finish_watch(new_items: list[dict], attempted: set[str], found_by_source: dict, stats: dict, state: dict,
                 cands: list[dict], retried_given_up: set[str] = frozenset(),
                 blocked: set[str] = frozenset(), unavailable: set[str] = frozenset()) -> tuple[list[dict], dict]:
    # 状態の保存は facts 化の**後**。既知にするのは「今回処理を試みた URL」だけで、
    # 上限を超えて手つかずのまま残った新着は未読のままにする。
    #   - 巡回直後に全件を既知にすると、上限超過分は次回 new と判定されず、
    #     candidates に一度も載らないまま消える(カバレッジの穴になる)
    #   - 処理を試みた URL は、結果が0件でも既知にする(毎回同じページを
    #     読み直して上限枠を食い潰さないため)
    # 途中で落ちた場合も未保存なので、次回の実行がそのまま拾い直す。
    deferred = [n for n in new_items if n["url"] not in attempted]
    deferred_urls = {n["url"] for n in deferred}
    for sid, found in found_by_source.items():
        keep = [u for u in found if u not in deferred_urls]
        seen = state.get(sid, [])
        state[sid] = (keep + [u for u in seen if u not in keep])[:500]
        if sid in stats:
            stats[sid]["deferred"] = len([n for n in deferred if n["source_id"] == sid])
    # 未処理の列から処理したもの(もう一覧に出ていないことがある)も既読にする
    for it in new_items:
        if it["url"] in attempted and it["url"] not in state.get(it["source_id"], []):
            state[it["source_id"]] = ([it["url"]] + state.get(it["source_id"], []))[:500]
    # やり直しになった新着(出力が読めなかったバッチ)は未処理の列に残し、次回は一覧より先に処理する(一覧の上では既読のページの奥に隠れるため)
    state["_pending"] = deferred
    # 当番の取り直しで読み直した「諦めた新着」は、読めたら候補へ、読めなければ未処理の列(上)へ移ったので、諦めた列から外す。
    # 今回新しく諦めたもの(通常の収集)は残す。古いものから上限で切る
    given_up = [g for g in state.get("_given_up", []) if isinstance(g, dict) and g.get("url") not in retried_given_up]
    if len(given_up) > GIVEN_UP_MAX:
        notify("collect", f"定点観測: 諦めた新着が {len(given_up)}件たまり、古い {len(given_up) - GIVEN_UP_MAX}件を取り直しの対象から外した"
                          + "".join(f"\n  - {g['url']}" for g in given_up[:-GIVEN_UP_MAX][:20]), ok=False)
    state["_given_up"] = given_up[-GIVEN_UP_MAX:]
    # **ここでは保存しない。**既読の確定は candidates への書き込みと同じ成功境界にする。
    # 先に既読にすると、後段(正規化・verify・保存)が例外で落ちたとき、新着は既読なのに
    # candidates に無い、という取りこぼしになる(監査指摘)。main が保存後に書く
    stats["_state"] = state

    if deferred:
        print(f"定点観測: 新着 {len(new_items)}件のうち {len(attempted)}件を処理、"
              f"{len(deferred)}件を次回へ繰り越し", flush=True)
    # 繰り越しのうち、モデルの CLI が認証切れ・利用上限で答えなかった分(取得の失敗ではない。異常にしない。投稿は済み)と、
    # 本文が HTTP 503 で読めなかった分(人には通知済み。なぜなぜの対象外)
    return cands, {"stats": stats, "new": len(new_items), "facted": len(cands),
                   "deferred": len(deferred), "blocked": len([n for n in deferred if n["url"] in blocked]),
                   "unavailable": len([n for n in deferred if n["url"] in unavailable and n["url"] not in blocked])}


# ---- A-2 / B 探索 --------------------------------------------------------------

def build_prompts() -> list[dict]:
    queries = yaml.safe_load((ROOT / "prompts" / "queries.yml").read_text(encoding="utf-8"))
    return queries


def read_written(p: Path) -> str:
    """セッションが書き出したファイルの中身。無い・読めない(ファイルでなくディレクトリが作られた など)は空として扱う
    (書き出しが無い面として、やり直し・異常へ回る。読み取りの例外で収集全体を止めない。監査指摘)。"""
    try:
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    except OSError:
        return ""


# 時間切れ・異常終了の書きかけを退けられなかった書き出し → その時の中身。正常に終わった試みが中身を書き換えるまで、
# この収集の間は「完了した書き出し」と読まない
STUCK_OUTPUTS: dict[Path, str] = {}


GROK_BLOCKED_MARK = "CLI 停止"      # errs の印: Grok が認証切れ・利用上限で答えなかった面


def grok_blocked(errs: dict, key: str) -> bool:
    """その面の最後の試みが、Grok の認証切れ・利用上限で答えなかったか(調べの失敗に数えない)。"""
    return str(errs.get(key, "")).startswith(GROK_BLOCKED_MARK)


def grok_wrote(outdir: Path, q: dict) -> bool:
    """その面の Grok がまとめのファイルを残したか(「なし」と書いた面も残したことになる)。
    退けられなかった書きかけ(STUCK_OUTPUTS)は、その後の試みが正常に終わっても完了と読まない(監査指摘)。"""
    p = outdir / f"{q['key']}.md"
    return p not in STUCK_OUTPUTS and bool(read_written(p).strip())


def run_grok_faces(queries: list[dict], outdir: Path, errs: dict, retry: bool = False) -> set[str]:
    """面ごとに Grok の基本の調べ(X の検索だけ)を走らせる。戻りは正常に終わらなかった面。"""
    return run_grok_prompts([(q, write_grok_prompt(outdir, q, retry=retry)) for q in queries], errs)


def grok_basic(queries: list[dict], outdir: Path, grok_errs: dict) -> list[str]:
    """Grok の基本の調べを全面で走らせ、終わらなかった面だけ1回やり直す。戻りはやり直しても終わらなかった面(名指しで異常にした)。

    まとめのファイルを残せなかった面・正常に終わらなかった面は、「先に書く」順で1回だけやり直す
    (実測 2026-10-03: 学マスの面が打ち切られてファイルを書けず0件。公式Xの4コマ第174話を1日遅れで載せた)。
    未完了は終了状態と書き出しの両方で決める(書きかけを退けられずに残った面を完了とみなさない。監査指摘)。"""
    failed = run_grok_faces(queries, outdir, grok_errs)
    # 認証切れ・利用上限で答えなかった面はやり直さない(同じ理由で止まるだけ)。失敗にも数えない(投稿は済み)
    missing = [q for q in queries if (not grok_wrote(outdir, q) or q["key"] in failed) and not grok_blocked(grok_errs, q["key"])]
    if not missing:
        return []
    print(f"grok: まとめを残せなかった面 {', '.join(q['key'] for q in missing)} をやり直す", flush=True)
    failed = run_grok_faces(missing, outdir, grok_errs, retry=True)
    still = [q["key"] for q in missing
             if (not grok_wrote(outdir, q) or q["key"] in failed) and not grok_blocked(grok_errs, q["key"])]
    # やり直しも時間切れ・異常終了した面は、途中までの書き出しがあればそれを使う(全部は失わない)。未完了は下で名指しする
    for k in still:
        if read_written(outdir / f"{k}.md").strip():
            # 書きかけを退けられずに残った面: それを途中までの書き出しとして使い、そう名指しする
            grok_errs[k] = "退けられなかった書きかけを使う(調べが終わっていない): " + grok_errs.get(k, "")
            continue
        # 試みごとの途中までの書き出し(.partial-<n>.md)を全部つなぐ(初回と、やり直しで書けた投稿が違うことがある。監査指摘)
        part = "\n\n".join(t for t in (read_written(p).strip() for p in sorted(outdir.glob(f"{k}.partial-*.md"))) if t)
        if part.strip():
            try:
                (outdir / f"{k}.md").write_text(part, encoding="utf-8")
                grok_errs[k] = "途中までの書き出しを使う(調べが終わっていない): " + grok_errs.get(k, "")
            except OSError as e:      # 書き出し先がディレクトリになっている等。その面は失う(下で名指し)
                grok_errs[k] = f"途中までの書き出しも戻せない({type(e).__name__}): " + grok_errs.get(k, "")
    if still:     # 候補の数に関係なく名指しする(全面が未完了でも、途中までの書き出しから候補が出ると全面0件にならない。監査指摘)
        notify("collect", f"Grok(X 調査)の {', '.join(still)} 面が、やり直しても調べを終えられなかった"
                          "(途中までの書き出しがあればそれだけを使い、無ければその面の X の動きを丸ごと失う):\n"
                          + "\n".join(f"- {k}: {grok_errs.get(k, 'エラー出力なし')}" for k in still)
                          + "\n- セッション記録の失敗理由: " + grok_session_error(), ok=False)
    return still


def run_grok_prompts(targets: list[tuple[dict, Path]], errs: dict) -> set[str]:
    """Grok のセッションを GROK_WAVE 本ずつ走らせる。手数では縛らない(時間 GROK_TIMEOUT だけ)。エラー出力は errs に残す。
    戻りは、正常に終わらなかった(時間切れ・非0終了)面のキー(書き出しの有無では判定しない。監査指摘)。"""
    failed: set[str] = set()
    for i in range(0, len(targets), GROK_WAVE):
        if "grok" in CLI_BLOCKED:
            # この収集で既に認証切れ・利用上限を見た: 残りの面は起動しない(同じ理由で止まるだけ。投稿は済み)
            for q, _ in targets[i:]:
                errs[q["key"]] = f"{GROK_BLOCKED_MARK}: この収集で既に止まっている"
            break
        wave = targets[i:i + GROK_WAVE]
        if deadline_passed():
            # 収集の締切を過ぎたら、残りの面を起動しない(組版の開始を妨げない)。正常に終わらなかった面として、やり直し・名指しの異常へ
            for q, _ in wave:
                errs[q["key"]] = "収集の締切を過ぎたので起動しなかった"
                failed.add(q["key"])
            continue
        # **締切は起動時に絶対時刻で決め、残り時間で回収する。**1本ずつ GROK_TIMEOUT を与え直すと、応答しない面が並ぶほど
        # 待ちが足し算になる(3本で 3×GROK_TIMEOUT。監査指摘 2026-10-14)。収集の締切も越えない
        wave_end = time.time() + session_left(GROK_TIMEOUT)
        wave_started = time.time()     # この波のセッションの記録だけを、この試行の失敗理由として読む(grok_session_reasons)
        procs, finished = [], []
        try:
            for q, pp in wave:
                if deadline_passed():
                    # 波の途中で収集の締切を過ぎたら、残りの面も起動しない(起動の直前に確かめる)
                    errs[q["key"]] = "収集の締切を過ぎたので起動しなかった"
                    failed.add(q["key"])
                    continue
                procs.append((q, start_group(
                    ["grok", "--prompt-file", str(pp), "--always-approve",
                     "--cwd", str(ROOT), "--reasoning-effort", GROK_EFFORT],
                    cwd=ROOT, stdout=subprocess.DEVNULL), pp))
            for q, pr, pp in procs:
                # 時間切れは子も落とし、それまでのエラー出力を上限つきで回収する
                _, err, timed_out = finish_group(pr, max(0, wave_end - time.time()))
                finished.append((q, pr, pp, err, timed_out))
        finally:
            stop_groups([pr for _, pr, _ in procs])
        for q, pr, pp, err, timed_out in finished:
            if timed_out:
                errs[q["key"]] = f"時間切れ: {(err or '').strip()[-300:]}"
                print(f"grok: {q['key']} 面がタイムアウト(そこまでの記録は残る)", flush=True)
            elif pr.returncode or (err or "").strip():
                errs[q["key"]] = f"exit {pr.returncode}: {(err or '').strip()[-300:]}"
            else:
                errs.pop(q["key"], None)     # errs は最後の試みの状態(やり直しで直った面の古いエラーを残さない。監査指摘)
            out = pp.with_name(pp.name.removeprefix("prompt-"))
            # エラー出力の全文と、この試みの書き出しを、基本の調べ・やり直し・深掘りの別に残す(要約は末尾 300 字だけ。
            # 書き出しは次の試み・次の収集で消える。監査指摘)。経過は Grok のセッション記録に残る
            save_raw(edition_date(), f"grok-{pp.stem}", "", err or "", pr.returncode, files={out.name: read_for_raw(out)})
            # 認証切れ・利用上限(Grok は API に拒まれても終了コード 0 で、理由はセッション記録にだけ残る)は、面の調べの失敗に
            # 数えない(やり直さない・異常にしない。投稿は済み)。印を errs に残し、呼び側が grok_blocked で見分ける
            # 書き出しがあっても、この試行のセッション記録は必ず読む(途中まで書いてから利用上限で止まり、終了 0 で終わることがある。
            # 依頼文は見つけた投稿を先に書かせる。監査指摘 r8)。止まっていれば答えたことにしない
            reasons = "" if timed_out else grok_session_reasons(ROOT, wave_started, str(out))
            stopped = not timed_out and cli_blocked_reason("grok", err or "", reasons) is not None
            answered = not timed_out and pr.returncode == 0 and bool(read_written(out).strip()) and not stopped
            blk = cli_check("grok", "collect", f"{now_jst():%H:%M} の収集 X の調べ {pp.stem.removeprefix('prompt-')} 面",
                            None if timed_out else pr.returncode, err or "", reasons, answered=answered)
            if blk:
                errs[q["key"]] = f"{GROK_BLOCKED_MARK}: {blk.get('detail', '')}"
            if timed_out or pr.returncode != 0 or blk:
                # 止まった試行の書き出しも途中のもの(退けて、keep_blocked_partials が読み直しの列へ回す)
                failed.add(q["key"])
                # 時間切れ・異常終了の回が書いた書き出し(一部の投稿だけ・途中の「なし」)は答えにしない。試みごとに別の
                # <名前>.partial-<n>.md に退けて、面の書き出しが無い状態にする(基本の調べは「書けなかった面」としてやり直し・異常へ、
                # 深掘りは下で異常へ。非空のファイルを完了とみなして残りの検索を黙って失わない。やり直しが初回の途中出力を
                # 上書きしない。監査指摘)。退けられなければ、その面の異常として残して他の面を続ける
                if out.exists():
                    n = 1
                    while out.with_suffix(f".partial-{n}.md").exists():
                        n += 1
                    try:
                        out.replace(out.with_suffix(f".partial-{n}.md"))
                    except OSError as e:
                        # 退けられなければ、この収集の間はその書きかけを「完了」と読まない(STUCK_OUTPUTS に中身を控える。
                        # やり直しが新しく書かずに正常終了しても、古い書きかけで完了にしない。監査指摘)
                        STUCK_OUTPUTS[out] = read_written(out)
                        errs[q["key"]] = f"書きかけを退けられない({type(e).__name__}): " + errs.get(q["key"], "")
            elif out in STUCK_OUTPUTS and read_written(out) != STUCK_OUTPUTS[out]:
                STUCK_OUTPUTS.pop(out)      # 正常に終わった試みが新しく書いた: 完了した書き出し
    return failed


def grok_search_counts(since_ts: float) -> dict[str, int]:
    """since_ts 以降に始まった、このリポジトリの Grok セッションの面(brand)ごとの X 検索の回数(週次上限を消費するのはこれだけ)。
    回数は Grok のセッション記録(updates.jsonl の XSearch の呼び出し)から数える。"""
    import urllib.parse as _up
    base = Path.home() / ".grok" / "sessions" / _up.quote(str(ROOT), safe="")
    out: dict[str, int] = {}
    try:
        dirs = [d for d in base.iterdir() if d.is_dir() and d.stat().st_mtime >= since_ts]
    except OSError:
        return out
    for d in dirs:
        try:
            ch = (d / "chat_history.jsonl").read_text(encoding="utf-8", errors="replace")
            up = (d / "updates.jsonl").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'brand=\\"([a-z0-9-]+)', ch)
        if m:
            out[m.group(1)] = out.get(m.group(1), 0) + len(re.findall(r'"variant":\s*"XSearch"', up))
    return out


def grok_session_error(since_s: int = 7200) -> str:
    """直近 since_s 秒に、このリポジトリで開いた Grok セッションの失敗理由(Grok は API に拒まれても終了コード 0 で、
    理由はセッション記録 updates.jsonl にしか残らない)。見つからなければ「記録なし」。"""
    import urllib.parse as _up
    base = Path.home() / ".grok" / "sessions" / _up.quote(str(ROOT), safe="")
    reasons: list[str] = []
    try:
        for d in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if time.time() - d.stat().st_mtime > since_s:
                break
            u = d / "updates.jsonl"
            if u.exists():
                for m in re.finditer(r'"agent_result":"([^"]{0,300})"', u.read_text(encoding="utf-8", errors="replace")):
                    if "error" in m.group(1).lower() and m.group(1) not in reasons:
                        reasons.append(m.group(1))
    except OSError:
        pass
    return " / ".join(reasons[:3]) or "記録なし"


def parse_grok(out: str) -> list:
    """grok --output-format json はエンベロープ {"text": <本文>} で返す。素の JSON にも対応。"""
    try:
        obj = json.loads(out)
        out = obj.get("text", out)
    except (json.JSONDecodeError, AttributeError):
        pass
    return extract_json_array(out)


def explore_workdir(key: str, entry: str = "fetch_page.py") -> Path:
    """探索セッション専用の作業ディレクトリを作る(**リポジトリの外**)。

    探索は取得したページの中身を読む。そこに「このファイルを書き換えろ」と
    仕込まれていた場合、workspace-write で走る探索はリポジトリを書き換えられる。

    cwd をリポジトリ外に置けば、workspace-write の書き込み範囲からリポジトリが
    外れ、読み取り専用になる。探索が必要とするのは本文取得だけなので、
    実体を絶対パスで呼ぶだけの薄い入口を1つ置けば足りる。

    ここで止める。**探索に妙なページを踏ませない運用のほうが本筋**であり、
    セッション同士の相互汚染まで機械で塞ごうとすると、得るものに対して
    仕掛けが重くなりすぎる。

    entry は入口の置き場(cwd からの相対)。出典の判定の依頼文は `scripts/fetch_page.py` で読ませる
    (classify_sources.ask の codex 側。2026-10-21)。
    """
    wd = scratch_dir(f"explore-{key}-")     # /tmp(tmpfs)に置かない。呼び側が消し、消し損ねは次に作るとき片付く
    try:
        (wd / entry).parent.mkdir(parents=True, exist_ok=True)
        (wd / entry).write_text(
            "#!/usr/bin/env python3\n"
            "# 本体はリポジトリ側(読み取り専用)。ここは呼び出すだけの入口\n"
            "import os, sys\n"
            f"os.execv(sys.executable, [sys.executable, {str(ROOT / 'scripts' / 'fetch_page.py')!r}]\n"
            "         + sys.argv[1:])\n", encoding="utf-8")
    except BaseException:
        # 入口を書けない(容量不足など)ときも作業ディレクトリを残さない。呼び出し側は wd を受け取れず片付けられない(監査指摘 2026-10-21)
        shutil.rmtree(wd, ignore_errors=True)
        raise
    return wd


def explore_argv(short: str) -> list[str]:
    """探索(Luna / codex)の起動引数。`short` は prompt_file が返す短い指示(素材はファイル側)。

    `-s read-only` では通信も遮断される(実測: 名前解決に失敗し fetch_page.py が
    動かない)。`sandbox_workspace_write.network_access` は名前のとおり
    workspace-write 用の設定なので、通信を使う以上 workspace-write で走らせる。
    ただし cwd は `explore_workdir()` が作るリポジトリ外のディレクトリなので、
    書き込めるのはそこと一時ファイルの置き場(TMPDIR)に限られ、リポジトリには手が届かない。
    """
    return ["codex", "exec", "-m", EXPLORE_MODEL, "-s", "workspace-write",
            # cwd が git リポジトリでないため、codex の作業前確認を外す
            "--skip-git-repo-check",
            "-c", "sandbox_workspace_write.network_access=true", short]


def claude_exec(prompt: str, timeout: int = 300):
    """定点観測の facts 化に使う Claude 呼び出し(探索とは別役)。

    **読めなかったら None を返す**(0件の [] とは別)。呼び出し側はそのバッチを
    既読にしない。以前は読めない出力も0件として既読にし、新着が二度と候補に
    ならなかった(監査指摘 P1-5)。
    """
    from pipelib import extract_json_array_strict, prompt_file
    import hashlib as _hl
    name = "watch-" + _hl.sha256(prompt.encode("utf-8")).hexdigest()[:8]
    short = prompt_file(edition_date(), name, prompt)
    if deadline_passed():
        return DEADLINE_CUT     # 依頼文を書くあいだに収集の締切を過ぎた: 起動しない(読めなかったのとは別)
    try:
        # 自前のグループで起動し、時間切れでは道具ごと止める(run_group)。待つのは収集の締切まで
        r = run_group(
            ["claude", "-p", short,
             "--model", COLLECT_MODEL,
             # 指示と素材はファイルなので **Read が要る**。WebSearch/WebFetch だけに絞っていたら、ファイルを読めずに
             # 420 秒待って落ちた(実測 2026-09-17)
             # ページは執筆・校閲と同じ fetch_page.py で読ませ、WebFetch は外す。WebFetch は JS を描画せず、まとめサイトの
             # 新着の一次ソース(公式ポータル)がタイトルだけに見えて unreadable にした(2026-10-21: imasnews765 ?p=32576)
             # 許可は前方一致で、非対話では許可に当たらない形(絶対パス・前に cd や ls を繋ぐ・括弧でまとめる・grep/sed へ流す)は
             # 承認待ちのまま拒まれる。モデルは絶対パスで打ち直しがちなので、その形も許す(2026-10-22 23:30: imasnews765 ?p=32585 の
             # 一次ソースを絶対パスで打って拒まれ、unreadable にした)。どちらも fetch_page.py の起動だけ
             "--allowedTools", f"Read,WebSearch,Bash(python3 scripts/fetch_page.py:*),Bash(python3 {ROOT / 'scripts' / 'fetch_page.py'}:*)",
             "--disallowedTools", "WebFetch",
             "--max-budget-usd", EXPLORE_MAX_BUDGET_USD],
            timeout=session_left(timeout), cwd=ROOT)
    except (subprocess.TimeoutExpired, OSError) as e:
        save_raw(edition_date(), name, *partial_output(e))     # 時間切れまでの出力も残す
        if deadline_passed():
            return DEADLINE_CUT     # 収集の締切で打ち切った(読めなかったのとは別。読めなかった回数に数えない)
        # 1バッチの失敗で収集全体を落とさない。読めなかった扱い(既読にしない)
        print(f"定点観測: facts 化のセッションが失敗({type(e).__name__})。このバッチは既読にしない", flush=True)
        return None
    # 生の出力を必ず残す(依頼文 metrics/work/<日付>/<name>.md と同じ名前。候補が出なかった理由を後から確かめるため。
    # 2026-10-04: まとめサイトの新着1件を失ったが出力が残っておらず、当番も原因を確定できなかった。当番の指摘 b39801ce34・8bdc37f36c)
    save_raw(edition_date(), name, r.stdout, r.stderr, r.returncode)
    blocked = cli_check("claude", "collect", f"{now_jst():%H:%M} の収集 定点観測の facts 化", r.returncode, r.stdout, r.stderr)
    if blocked:
        raise CliBlocked(blocked)      # 読めなかった回ではない(facts_batch が数えずに未処理の列へ残す)
    if r.returncode != 0:
        # 異常終了の間際の出力は答えにしない(読めなかった扱い。既読にしない。監査指摘)
        print(f"定点観測: facts 化のセッションが異常終了(exit {r.returncode})。このバッチは既読にしない", flush=True)
        return None
    got = extract_json_array_strict(r.stdout)
    if got is None:
        print(f"定点観測: facts 化の出力が読めない(stderr: {(r.stderr or '')[-160:]})", flush=True)
    return got


def write_grok_prompt(outdir: Path, q: dict, retry: bool = False) -> Path:
    """1面ぶんの基本の調べ(X の検索だけ)の指示を書き出す(面ごとに1セッション)。

    **役割分担(編集長 2026-10-04)**: Grok は X の検索だけをして、見つけた投稿を全文のまま書き出す。リンク先を開いて確かめ、
    候補にするのは Luna(verify_grok_faces)。X の原本でしか分からない問いだけ、予算を確かめて Grok に深掘りさせる(deep_dive_grok)。
    以前は Grok に「リンク先を開いて確かめ、まとめる」まで頼み、JavaScript で描画されるページの解析に手数を使い切って
    面ごと0件になっていた(9/15〜10/3 の171セッション中20本が打ち切り・7本が0件)。
    """
    out = outdir / f"{q['key']}.md"
    days = 3 if q["key"] in ("trend", "fan-culture") else 2
    since = (now_jst() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    accounts = q.get("accounts") or []
    at = "、".join(f"@{a}" for a in accounts)
    froms = " OR ".join(f"from:{a}" for a in accounts)
    step1 = (f"1. 公式アカウントをまとめて引く。`x_keyword_search` に query `({froms}) since:{since}`、limit {X_SEARCH_LIMIT}、"
             f"mode Latest を渡す({at} の投稿がまとめて取れる。1アカウントずつ引き直さない)。\n"
             f"   返ってきたのが {X_SEARCH_LIMIT}件(道具の上限)なら、それより古い投稿を取りこぼしている。いちばん古い投稿の日時を "
             f"`date -u -d '<その日時>' +%s` で UNIX 秒にし、同じ query に `until_time:<その秒+1>` を足して引き直す(遡る。"
             f"+1 は同じ秒の投稿を落とさないため)。いちばん古い投稿の秒が前の回と同じで進まないときは、+1 をせず "
             f"`until_time:<その秒>` で進む。{X_SEARCH_LIMIT}件未満になるか、{since} より前に届くまで繰り返す"
             f"(この遡りは最初の1回とは別に、追加で最大 {GROK_OFFICIAL_PAGES}回。下の検索回数の上限とは別に数える)。"
             f"遡りの結果は前の回と重なるので、全部の回を通して同じ url の投稿は1回だけ書く" if froms
             else "1. (この面には公式アカウントの指定が無い。2 の角度から始める)")
    prompt = render_prompt("grok-collect", BRAND=q["brand"], TOPIC=q["topic"], TODAY=now_jst().strftime("%Y-%m-%d"),
                           SINCE=since, OUT=out, MAX_SEARCHES=GROK_MAX_SEARCHES, STEP1=step1,
                           RETRY=("- **やり直し**: 前回は書き出す前に終わった。1 の結果をまず書き、そのあとで 3 に進む\n"
                                  if retry else ""))
    pp = outdir / f"prompt-{q['key']}.md"
    pp.write_text(prompt, encoding="utf-8")
    return pp


def facts_left(watch_info, whywhy: bool = False) -> int:
    """事実に起こせず次回へ回した新着の数(CLI の認証切れ・利用上限の分を除く)。whywhy=True は、本文が HTTP 503 で
    読めなかった分(unavailable)も除く(なぜなぜ・当番の取り直しの成否に数えるもの)。"""
    if not isinstance(watch_info, dict):
        return 0
    return watch_info.get("deferred", 0) - watch_info.get("blocked", 0) - (watch_info.get("unavailable", 0) if whywhy else 0)


def collect_health(watch_info, per_query: dict, whywhy: bool = False) -> list[str]:
    """収集の各系統で、取得に失敗したもの(人が読める説明)。空なら全系統の取得は正常。
    定点観測は観測先ごとの取得の誤りと未処理の繰り越し、探索は出力が読めない・打ち切られた面(per_query の explore_failed:<面>)。
    whywhy=True は、なぜなぜ(当番)に渡すものだけ: HTTP の状態コードが対象外(503)の観測先を除く。"""
    out = []
    stats = (watch_info or {}).get("stats") or {} if isinstance(watch_info, dict) else {}
    for sid, st in stats.items():
        if isinstance(st, dict) and st.get("error") and not (whywhy and whywhy_exempt_status(st.get("http"))):
            out.append(f"定点観測 {sid}: 一覧を読めなかった({st['error'][:80]})")
    # モデルの CLI の認証切れ・利用上限で答えなかった分は取得の失敗に数えない(人には見つけたときに投稿済み。当番に回さない)
    # whywhy=True は本文が 503 で読めなかった分も除く(facts_left)
    left = facts_left(watch_info, whywhy)
    if left > 0:
        out.append(f"定点観測: 事実に起こせず次回へ回した新着 {left}件")
    out += [f"探索 {k.split(':', 1)[1]}: 出力が読めない・打ち切られた" for k, v in per_query.items() if k.startswith("explore_failed:") and v]
    return out


def report_zero_new(summary: str, watch_info, per_query: dict) -> bool:
    """新規0件の回に、取得に失敗した系統を人に知らせる。戻り値は失敗した系統があったか。
    当番に渡す台帳には、なぜなぜの対象の失敗だけを積む: 全部が 503 なら積まない。503 以外が混ざれば、その失敗だけの文にして積む
    (2026-10-25 12:31: lantis-sidem の 503 だけのまとめの文が text: の鍵で当番に渡り、判定済みの照合も効かなかった)"""
    failed = collect_health(watch_info, per_query)
    if not failed:
        return False
    left = collect_health(watch_info, per_query, whywhy=True)
    notify("collect", f"{summary} — 新規0件。取得に失敗した系統がある:\n- " + "\n- ".join(failed), ok=False, ledger=left == failed)
    if left and left != failed:
        anomaly("collect", f"{summary} — 新規0件。取得に失敗した系統がある(HTTP 503 で読めなかった観測先は除く):\n- " + "\n- ".join(left))
    return True


def unreadable_for_oncall(watch_info) -> list[str]:
    """当番の取り直しで一覧が取れなかった観測先(取り直しを失敗にするもの)。503 の観測先は当番の仕事の外なので数えない
    (WHYWHY_EXEMPT_HTTP。既読は進めていないので、次の定時収集が読み直す)"""
    stats = (watch_info.get("stats") or {}) if isinstance(watch_info, dict) else {}
    return sorted(sid for sid, st in stats.items()
                  if isinstance(st, dict) and st.get("error") and not whywhy_exempt_status(st.get("http")))


def wait_session(proc, deadline: float) -> bool:
    """codex のセッションを締切まで待つ。超えたらプロセスグループと子孫ごと落とす(finish_group)。戻り値は締切内に終わったか。"""
    return not finish_group(proc, max(0, deadline - time.time()))[2]


def read_json_list(p: Path) -> list | None:
    """セッションが書いた JSON 配列を読む。無い・読めない・配列でない・同じキーが2回ある(後の値で黙って上書きされる)なら None。"""
    try:
        v = loads_strict(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return v if isinstance(v, list) else None


GOOD_SOURCE_TYPES = ("公式", "準公式", "当事者", "報道")


def repoint_to_post(items: list[dict], posts_md: str) -> list[dict]:
    """候補の url が、X の投稿に貼られたリンク(動画など)で、種別が公式・準公式・当事者・報道でないなら、
    そのリンクを貼った投稿(公式の投稿を先)の url に付け直し、元の url は facts に残す。
    url が投稿の候補には、その投稿に貼られたリンクを facts(「リンク: <url>」)に写す。
    事実は投稿の本文から読んだもので、リンク先は出典として確かめられない(2026-10-05: 公式 X の
    コラボ動画告知が YouTube の url で候補になり、未確認の出典として執筆に見送られた)。"""
    posts = post_links(posts_md)
    posts.sort(key=lambda p: classify_source(p[0]) != "公式")
    for it in items:
        url = it["url"]
        if is_x(url) or classify_source(url) in GOOD_SOURCE_TYPES:
            # 投稿に貼られたリンクを候補の url にしたなら、その投稿をリンク元に残す(公式を先。2026-10-08 のレトラの t.co は
            # @valiv_official の投稿に載っていたが、素材に残らず判定に使えなかった)
            k = page_key(url)
            post = next((p for p, links in posts if k and k in {page_key(x) for x in links}), None) if not is_x(url) else None
            if post and not it.get("link_from"):
                it["link_from"] = post
            continue
        post = next((p for p, links in posts if url in links), None)
        if post:
            it["url"] = post
            it["facts"] = [*(it.get("facts") or []), f"リンク: {url}"]
    return copy_post_links(items, posts)


def post_links(posts_md: str) -> list[tuple[str, list[str]]]:
    """Grok の書き出しの投稿ごとの (url, 貼られたリンク)。投稿の区切りは突き合わせ(post_blocks / post_url)と共有する。
    url の無い塊(url の行が空・崩れた投稿)のリンクはどの投稿にも帰属させない(前の投稿へ付け直して、誤った出典にしない。監査指摘)"""
    posts: list[tuple[str, list[str]]] = []
    for block in post_blocks(posts_md):
        url = post_url(block)
        if not url:
            continue
        links: list[str] = []
        in_links = False
        for line in block.splitlines():
            if line.startswith("- リンク:"):
                in_links = True
            elif in_links and (m := re.match(r"\s+-\s*(https?://\S+)", line)):
                links.append(m.group(1))
            elif not line.startswith(" "):
                in_links = False
        posts.append((url, links))
    return posts


def copy_post_links(items: list[dict], posts: list[tuple[str, list[str]]]) -> list[dict]:
    """投稿を出典にする候補には、その投稿に貼られたリンクを facts に写す(「リンク: <url>」。既にあるものは重ねない)。
    執筆が開くリンクと、既報の照合(storylink)の手がかりになる。写さないと、執筆は投稿のページに出る途中で切れた表示
    (youtube.com/live/E0d4UT59x…)から url を推し量って別の動画を出典にし、照合は同じ配信ページを載せた前日の記事に届かず
    既報を新規として選んだ(2026-10-17号)"""
    links_of: dict[str, list[str]] = {}
    for p, links in posts:
        links_of.setdefault(post_key(p), []).extend(links)
    for it in items:
        if is_x(it["url"]):
            have = set(it.get("facts") or [])
            it["facts"] = [*(it.get("facts") or []),
                           *(f for f in dict.fromkeys(f"リンク: {u}" for u in links_of.get(post_key(it["url"]), [])) if f not in have)]
    return items


def backfill_post_links(day: str, root: Path = ROOT) -> int:
    """号の素材(candidates/<号>.json)の、投稿を出典にする候補へ、その号の確かめが読んだ投稿の原本(生の出力
    metrics/work/<号>/raw/grok-verify-*.txt の x-posts.md)に貼られたリンクを写す(copy_post_links)。号を作り直す前に、
    写す前に収集した候補を今のコードの形に揃える(2026-10-17号: 写さずに収集したラジオの候補が、作り直しでも配信ページを持たず、
    前日の既報につながらなかった。監査指摘 R3)。冪等。書き換えた候補の数を返す。原本・素材が無ければ 0。"""
    p = root / "candidates" / f"{day}.json"
    if not p.exists():
        return 0
    posts: list[tuple[str, list[str]]] = []
    for raw in sorted((root / "metrics" / "work" / day / "raw").glob("grok-verify-*.txt")):
        text = raw.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"(?ms)^# file: x-posts\.md\n(.*?)(?=^# file: |\Z)", text)
        if m:
            posts += post_links(m.group(1))
    if not posts:
        return 0
    cands = json.loads(p.read_text(encoding="utf-8"))
    before = [list(c.get("facts") or []) for c in cands]
    copy_post_links(cands, posts)
    n = sum(1 for c, f in zip(cands, before) if list(c.get("facts") or []) != f)
    if n:
        write_checked(p, json.dumps(cands, ensure_ascii=False, indent=2) + "\n")   # 書式(candidates.schema)で検めてから書く
    return n


def verify_grok_faces(queries: list[dict], outdir: Path, suffix: str = "",
                      ledger: dict | None = None) -> tuple[list[dict], dict[str, list[dict]]]:
    """Grok が書き出した X の投稿(<key><suffix>.md)を、面ごとに Luna が読み、リンク先を開いて確かめて候補にする(並列)。
    戻りは (候補, 面 → X の原本でしか確かめられない問い)。「なし」だけの面・ファイルの無い面は飛ばす。
    投稿ごとの結果(posts.json)と入力の投稿を突き合わせ、結果の無い投稿だけで1回やり直す(suffix に -again)。
    2回とも残った投稿と、済んだ投稿は ledger(lost_posts_ledger)に記録する。ledger を渡されなければこの段の結果を最終として
    喪失を通知し、渡されたら通知は呼び手が最後の段(深掘りの確かめ)のあとで report_lost_posts で1度だけ出す
    (深掘りの前に通知すると、深掘りで済んだ投稿を失ったと言い、深掘りでも読めない投稿は二度通知した。当番の指摘 11fc3de74b)。
    深掘りの確かめ(suffix に -deep)の問い(deep.json)は使わないので、読まず、読めなくても通知しない。
    Luna は探索と同じくリポジトリ外の作業ディレクトリで走る(ページの中身に指示が仕込まれていても、リポジトリに手が届かない)。"""
    own = ledger is None
    if own:
        ledger = lost_posts_ledger()
    wants_deep = "-deep" not in suffix
    deadline = time.time() + session_left(EXPLORE_TIMEOUT)    # 収集の締切も越えない
    jobs, waited = [], []
    # 起動は1面ずつ一覧に足し、例外・中断で抜けても finally で起動済みの面を止めて回収する。収集の締切を過ぎたら起動しない
    # (その面の投稿は未処理のまま残り、下でやり直し・名指しの喪失になる)
    try:
        for q in queries:
            text = read_written(outdir / f"{q['key']}{suffix}.md").strip()
            if not text or re.fullmatch(r"(なし|見つからない)[。\s]*", text):
                continue
            text = dedupe_posts(text)   # 公式アカウントの遡りのページの境目で重なった投稿を1つにする
            text = with_jst_post_time(text)
            if "codex" in CLI_BLOCKED:
                # この収集で確かめ役の CLI が止まっている: 起動せず読み直しの列へ(下の止まった面と同じ扱い。投稿は済み)
                ledger.setdefault("blocked", set()).add(q["key"])
                keep_pending_posts(q["key"], text)
                continue
            wd = explore_workdir(f"verify-{q['key']}{suffix}")
            (wd / "x-posts.md").write_text(text + "\n", encoding="utf-8")
            if not deadline_passed():
                prompt = render_prompt("grok-verify", INPUT=wd / "x-posts.md", OUT=wd / "items.json", DEEP=wd / "deep.json", POSTS=wd / "posts.json",
                                       BRAND=q["brand"], TOPIC=q["topic"], TODAY=now_jst().strftime("%Y-%m-%d"),
                                       RULES=COLLECT_RULES, ITEM=COLLECT_ITEM)
                short = prompt_file(edition_date(), f"grok-verify-{q['key']}{suffix}", prompt, base=wd)
                # 進行ログ(標準出力・エラー)は作業ディレクトリのファイルで受け、生の出力に残す(ファイルを書かずに終わった回の原因を追うため)
                with open(wd / "session.log", "w", encoding="utf-8") as lf:
                    if not deadline_passed():       # 準備のあいだに締切を過ぎることもあるので、起動の直前にも確かめる
                        jobs.append((q, wd, text, start_group(explore_argv(short), cwd=wd, stdout=lf, stderr=subprocess.STDOUT)))
                        continue
            jobs.append((q, wd, text, None))
        for q, wd, text, p in jobs:
            waited.append((q, wd, text, p, p is not None and wait_session(p, deadline)))
    finally:
        stop_groups([p for *_, p in jobs if p is not None])
    items, deep = [], {}
    final = suffix.endswith("-again")
    again: list[dict] = []
    for q, wd, text, p, in_time in waited:
        late = p is None      # 収集の締切を過ぎて起動しなかった
        # 何より先に、Luna が書いたものを生の出力として残す(このあとの処理で止まっても原因を追えるように)
        save_raw(edition_date(), f"grok-verify-{q['key']}{suffix}", "", "収集の締切を過ぎたので起動しなかった" if late else "",
                 None if late else p.returncode,
                 files={n: read_for_raw(wd / n) for n in ("x-posts.md", "items.json", "posts.json", "deep.json", "session.log")})
        # 認証切れ・利用上限で答えなかった面は、投稿の確かめの失敗(やり直し・喪失の異常)に数えない(投稿は済み。当番に回さない)
        if not late and cli_check("codex", "collect", f"{now_jst():%H:%M} の収集 X の投稿の確かめ {q['key']} 面{suffix}",
                                  p.returncode if in_time else None, read_for_raw(wd / "session.log") or ""):
            ledger.setdefault("blocked", set()).add(q["key"])
            keep_pending_posts(q["key"], text)      # 捨てずに読み直しの列へ(次の収集で確かめ直す)
            shutil.rmtree(wd, ignore_errors=True)
            continue
        # 時間切れ・異常終了のセッションが書いたファイルは答えにしない(全投稿を未処理としてやり直し・異常へ。監査指摘)
        got = read_json_list(wd / "items.json") if in_time and p.returncode == 0 else None
        # 終わり方(通知・記録用)。異常終了を「締切内に終わった」と書くと、原因(落ちた)が見えない
        ended = ("収集の締切を過ぎて起動しなかった" if late else "時間切れ" if not in_time
                 else (f"異常終了(exit {p.returncode})" if p.returncode else "締切内に終わった"))
        # 投稿1件ずつの結果を、入力の投稿と突き合わせる。結果の無い・読めなかった投稿を黙って捨てない
        # (2026-10-06 の洗い出しで判明: 候補の配列だけを受けていたので、Luna が開けなかった・飛ばした投稿が記録なしに消えた。
        #  定点観測で新着を失ったのと同じ型)。候補そのものが読めなければ「候補にした」の申告も当てにならないので、全投稿が残り
        left = (post_blocks(text) or [text]) if got is None else unaccounted_posts(text, read_json_list(wd / "posts.json"), got)
        # 候補にするのは、投稿を済みと数えるのと同じ判定(candidate_usable)を通ったものだけ。崩れた候補を欄を落として候補にしながら
        # 投稿は未処理と数えると、候補にした投稿を「失った」と通知する(2026-10-22 02:39 の gakuen 面)。崩れた候補の元の投稿は
        # 未処理として確かめ直し(-again)へ回り、どの投稿にも結び付かない崩れた候補は、確かめ直せないのでここで名指しする
        items += repoint_to_post([shape_item(x) for x in got or [] if candidate_usable(x)], text)
        tied, stray = broken_items(text, got)
        # 確かめ直し(-again)では、1回目に崩れた候補のために残した投稿も崩れの喪失として数える(確かめ直しの答えが空でも、
        # 1回目の崩れた候補の中身は戻っていない。Sol 監査 r2)
        tied |= ledger.setdefault("tied", {}).pop((q["key"], suffix.removesuffix("-again")), set()) if final else set()
        ledger.setdefault("stray", []).extend((q["key"], u) for u in stray)
        left_keys = {post_key(post_url(b)) for b in left}
        ledger["settled"] |= {k for k in map(post_key, map(post_url, post_blocks(text))) if k and k not in left_keys}
        if left and not final:
            # 残った投稿だけを <key><suffix>-again.md に書き出し、もう1回だけ確かめさせる(下でまとめて)
            (outdir / f"{q['key']}{suffix}-again.md").write_text("\n\n".join(left) + "\n", encoding="utf-8")
            again.append(q)
            ledger.setdefault("tied", {})[(q["key"], suffix)] = tied & left_keys
        elif left:
            # 2回目でも残った投稿は、申告で終えない(当番のなぜなぜへ)。どの投稿を失うかを最後の段のあとで名指しする
            why = f"{'候補の一覧が読めない' if got is None else '投稿ごとの結果に無い・読めなかった'}、{ended}"
            ledger["lost"].setdefault(q["key"], []).extend(
                (b, f"候補の形が崩れていた、{ended}" if post_key(post_url(b)) in tied else why) for b in left)
            # 崩れて候補にしなかった候補の中身は、ほかの段でこの投稿が済んでいても戻らない(その段の答えにしか無い)。済みで消さない
            ledger.setdefault("broken", set()).update(k for k in map(post_key, map(post_url, left)) if k in tied)
        raw_asks = read_json_list(wd / "deep.json") if in_time and p.returncode == 0 else None
        # 候補の一覧も読めなかった回は、全投稿を -again でもう1回確かめるので、問いもそこで出し直される(失っていない)。
        # 以前はここでも「深掘りの機会を失う」と通知していた(2026-10-21 02:19 の sidem 面: /tmp が満杯で Luna が exit 101 で
        # 落ちたが、-again が問いを書き、深掘りも回っていた)
        if raw_asks is None and wants_deep and not (got is None and not final):
            # 「問いなし([])」と「書けなかった」を分ける。原本の確かめを黙って失わない(監査指摘 r116)
            notify("collect", f"Grok の {q['key']} 面: X の原本でしか確かめられない問い(deep.json)が読めない"
                              f"({ended})。深掘りの機会を失う", ok=False)
        asks = [a for a in (raw_asks or []) if schema_ok(a, DEEP_ASK_SCHEMA) and a["question"].strip()]
        if wants_deep and raw_asks is not None and len(asks) < len(raw_asks):
            # 崩れた問いを「問いなし」にしない(深掘りの機会を黙って失わない。監査指摘)。正しい問いは使う
            notify("collect", f"Grok の {q['key']} 面: X の原本でしか確かめられない問いのうち {len(raw_asks) - len(asks)}件の形が崩れていて使えない"
                              "(深掘りの機会を失う)", ok=False)
        if asks and wants_deep:
            deep[q["key"]] = asks
        shutil.rmtree(wd, ignore_errors=True)
    print(f"grok: 確かめ{suffix or ''} {len(jobs)}面 → 候補 {len(items)}件、原本の問い {sum(len(v) for v in deep.values())}件", flush=True)
    if again:
        print(f"grok: 結果の無い投稿が残った面 {', '.join(q['key'] for q in again)} を、残った投稿だけでもう1回確かめる", flush=True)
        more, more_deep = verify_grok_faces(again, outdir, suffix=suffix + "-again", ledger=ledger)
        items += more
        for k, v in more_deep.items():
            deep.setdefault(k, []).extend(v)
    if own:
        report_lost_posts(ledger)
    return items, deep


def lost_posts_ledger() -> dict:
    """X の投稿の確かめの記録(全段で共有)。lost: 面 → [(2回確かめても残った投稿の塊, 理由)] / settled: どこかの段で済んだ投稿の鍵(post_key)
    / blocked: 確かめ役の CLI が認証切れ・利用上限で答えなかった面(投稿は読み直しの列 GROK_PENDING へ)
    / broken: 形の崩れた候補を候補にしないまま確かめ直しでも直らなかった投稿の鍵(ほかの段で済んでいても、その中身は失っている)
    / stray: [(面, 候補の url)] どの投稿にも結び付かない崩れた候補(確かめ直せない。投稿ではないので読み直しの列にも入れない)
    / tied: (面, 段) → 崩れた候補のために確かめ直しへ回した投稿の鍵(確かめ直しの段へ引き継ぐ)。"""
    return {"lost": {}, "settled": set(), "blocked": set(), "broken": set(), "stray": [], "tied": {}}


# 確かめ役(Luna)が認証切れ・利用上限で答えなかった面の X の投稿(読み直しの列。Git 管理外)。Grok の調べは週の枠を使い、回す時刻も
# 限られるので、確かめられなかった投稿を捨てずに次の収集で確かめ直す
GROK_PENDING = Path("metrics") / "work" / "grok-pending"


def keep_pending_posts(key: str, text: str) -> None:
    """確かめられなかった面の投稿を、読み直しの列に足す(同じ投稿は次に読むとき dedupe_posts が1つにする)。"""
    p = ROOT / GROK_PENDING / f"{key}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(text.strip() + "\n\n")


def keep_blocked_partials(outdir: Path, keys, pattern: str) -> None:
    """Grok が認証切れ・利用上限で止まる前に書いた途中の書き出し(退けた <pattern>)の投稿を、読み直しの列に残す
    (止まった面は途中の書き出しを使うやり直しにも入らないので、そのままでは次の収集で .grok ごと消える。監査指摘 r5)。"""
    for k in keys:
        text = "\n\n".join(t for t in (read_written(p).strip() for p in sorted(outdir.glob(pattern.format(k=k))))
                           if t and not re.fullmatch(r"(なし|見つからない)[。\s]*", t))
        if post_blocks(text):
            keep_pending_posts(k, text)


def take_pending_posts(outdir: Path, keys: list[str]) -> list[Path]:
    """読み直しの列の投稿を、その面の書き出し(<key>.md)へ足す(「なし」だけの書き出しは置き換える)。列のファイルは確かめが
    終わるまで消さず .taken に退ける(呼び側が確かめのあとで消す。止まれば確かめ役がまた列に足す)。戻りは退けたファイル。"""
    taken = []
    for k in keys:
        p = ROOT / GROK_PENDING / f"{k}.md"
        t = p.with_suffix(".taken")
        # 前の回が確かめの途中で落ちて残した .taken も読み直す(消すのは確かめ終えてから)
        text = "\n\n".join(x for x in (read_written(t).strip(), read_written(p).strip()) if x)
        if not text:
            continue
        out = outdir / f"{k}.md"
        cur = read_written(out).strip()
        out.write_text((text if not cur or re.fullmatch(r"(なし|見つからない)[。\s]*", cur) else cur + "\n\n" + text) + "\n",
                       encoding="utf-8")
        t.write_text(text + "\n", encoding="utf-8")
        p.unlink(missing_ok=True)
        taken.append(t)
    return taken


def report_lost_posts(ledger: dict) -> None:
    """最後の段まで確かめ終えて、どの段でも済まなかった投稿だけを、まとめて1度、投稿ごとに1行で名指しする。
    途中の段で残っても、後の段(深掘りの確かめ)で済んだ投稿は失っていない。同じ投稿が複数の段で残っても、
    複数の面で残っても(面どうしで同じアカウントを検索するので、同じ投稿が複数の面に書き出される)1行にし、所属の面を併記する
    (面ごとに数えると、同じ投稿の喪失を面の数だけ通知した。監査指摘 lost-post-cross-face)。"""
    rows: dict[str, list] = {}  # 投稿 → [表示, 面の並び, 理由の並び](最初に現れた順)
    # 済んだ投稿は失っていない。ただし崩れた候補を候補にしなかった投稿(broken)は、ほかの段で済んでもその中身が戻らない
    done = ledger["settled"] - (ledger.get("broken") or set())
    # 後の段(深掘りの確かめ)で確かめ役の CLI が認証切れ・利用上限で止まった面は、まだ確かめ終えていない: 残った投稿を失ったと
    # 言わず、読み直しの列に残して次の収集で確かめ直す(そこでも済まなければ、そのとき名指しする。監査指摘 r3)
    for key in sorted(set(ledger["lost"]) & set(ledger.get("blocked") or ())):
        left = [b for b, _ in ledger["lost"].pop(key) if not (post_url(b) and post_key(post_url(b)) in done)]
        if left:
            keep_pending_posts(key, "\n\n".join(left))
    for key in sorted(ledger["lost"]):
        for b, why in ledger["lost"][key]:
            u = post_url(b)
            if u and post_key(u) in done:
                continue
            row = rows.setdefault(post_key(u) or b, [u or "(投稿に分けられない書き出し) " + b[:80], [], []])
            for lst, v in ((row[1], key), (row[2], why)):
                if v not in lst:
                    lst.append(v)
    strays: dict[str, list] = {}    # 候補の url → 面の並び
    for key, u in ledger.get("stray") or []:
        if key not in strays.setdefault(u, []):
            strays[u].append(key)
    if not rows and not strays:
        return
    lines = [f"- {shown}(面: {', '.join(keys)}。{' / '.join(whys)})" for shown, keys, whys in rows.values()]
    faces = sorted({k for _, keys, _ in rows.values() for k in keys} | {k for keys in strays.values() for k in keys})
    msg = f"Grok の {', '.join(faces)} 面: "
    if lines:
        msg += (f"X の投稿 {len(lines)}件を、深掘りまで確かめても候補にも「事実なし」にもできなかった。この投稿の X の動きを失う:\n"
                + "\n".join(lines[:10]) + (f"\n- ほか {len(lines) - 10}件" if len(lines) > 10 else ""))
    if strays:
        msg += (("\n" if lines else "") + f"確かめ役が書いた候補 {len(strays)}件の形が崩れていて、どの投稿にも結び付かない(確かめ直せない)。この候補の中身を失う:\n"
                + "\n".join(f"- {u}(面: {', '.join(keys)})" for u, keys in list(strays.items())[:10])
                + (f"\n- ほか {len(strays) - 10}件" if len(strays) > 10 else ""))
    notify("collect", msg, ok=False)


# 投稿の url の行。見本は `- url: …` だが、行頭の記号が抜けた・全角のコロンなどの崩れも投稿の区切りとして拾う
# (区切りを取りこぼすと、その投稿は前の塊に吸われるか捨てられ、突き合わせから消える。監査指摘)。
# 字下げした行(投稿に付いたリンクの一覧)は区切りにしない
# 区切り(url の行があること)と値(その行の URL)は分けて読む: 値が空・不正でも url の行は投稿の区切り
# (区切りにしないと、その投稿が前の投稿の塊に吸われ、前の投稿の結果で済みにされて消える。監査指摘)。
# 値は同じ行の http(s) の URL だけ(空の url の行で次の行の文字を URL と読まない。監査指摘)
POST_SPLIT_LINE = re.compile(r"(?m)^(?:[-*・][ \t]*)?url[ \t]*[:：]", re.I)
POST_URL_LINE = re.compile(r"(?m)^(?:[-*・][ \t]*)?url[ \t]*[:：][ \t]*(https?://\S+)", re.I)
X_STATUS = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/\w+/status/\d+")


def post_blocks(text: str) -> list[str]:
    """Grok の書き出しを投稿ごとの塊に分ける。まず見出し(`### 番号`)で区切り、1つの区間に url の行が2つ以上あれば
    url の行でさらに区切る(見出しの抜けた投稿)。url の行が無い区間は、番号の見出しで始まるか X の投稿の URL を含めば
    url の無い塊として残す(url の行の抜けた投稿。突き合わせで必ず未処理になる)。位置(途中・末尾・先頭)に関係なく
    同じ規則で拾う(前の投稿の塊に吸われて、その投稿の結果で済みにされて消えるのを防ぐ。監査指摘)。"""
    # 投稿の中身(X の投稿 URL・投稿の欄)があるか。見出しは含めない(見出しだけの前置きを投稿にしない)
    has_post = lambda s: bool(X_STATUS.search(s) or re.search(r"(?m)^\s*[-*・]?\s*(本文|投稿者|投稿日時)\s*[:：]", s))
    out: list[str] = []
    for seg in re.split(r"(?m)^(?=#{1,6}\s)", text):
        seg = seg.strip()
        if not seg:
            continue
        starts = [m.start() for m in POST_SPLIT_LINE.finditer(seg)]
        if not starts:
            # url の行の無い区間でも、投稿の中身・番号だけの見出しがあれば投稿として残す
            # (見出しが「### 投稿2」のように番号で始まらない崩れも拾う。監査指摘)。題だけの見出し(# 765 面のまとめ)は投稿にしない
            if re.match(r"#{1,6}[ \t]*\d+[.．)]?[ \t]*(\n|$)", seg) or has_post(seg):
                out.append(seg)
            continue
        # 先頭の url の行より前は、見出しだけなら最初の塊に含め、投稿の中身があれば(見出しも url の行も無い投稿)別の塊にする
        if has_post(seg[:starts[0]]):
            out.append(seg[:starts[0]].strip())
            cuts = starts
        else:
            cuts = [0] + starts[1:]
        out += [seg[a:b].strip() for a, b in zip(cuts, cuts[1:] + [len(seg)])]
    return out


def post_url(block: str) -> str:
    m = POST_URL_LINE.search(block)
    return m.group(1) if m else ""


def post_key(url: str) -> str:
    """投稿の同一性を比べる鍵。X の投稿は status ID(同じ投稿が x.com/i/status/<ID> と x.com/<アカウント>/status/<ID> の
    どちらの形でも書かれる)、それ以外は url そのもの。url の文字列で比べると、深掘りで原本の形の url で済んだ投稿を
    「失った」と通知した(2026-10-17: 返信先として x.com/i/status/… で書き出された投稿)。"""
    u = (url or "").strip()
    k = url_key(u) if is_x(u) else ""
    return k if k.startswith("x:") else u


def dedupe_posts(text: str) -> str:
    """同じ投稿(post_key)の塊を1つにする。公式アカウントの遡りはページの境目が重なるので、同じ投稿が2回書かれうる。
    2回あると、確かめの結果が同じ url に2つ返り、突き合わせ(unaccounted_posts)が「結果が1つでない」で未処理にする(監査指摘)。
    残すのは、url の行を除いた中身がいちばん多い塊(同じなら先のもの)を、最初に現れた位置に置く。先のものを残すと、
    返信先として本文なしで書かれた投稿(x.com/i/status/<ID>)が、後ろの本文付きの同じ投稿を消した(監査指摘 dedupe-discards-readable-post)。
    重なりが無ければ書き出しをそのまま返す。url の無い塊は全部残す(突き合わせで未処理として名指しされる)。"""
    blocks = post_blocks(text)
    urls = [post_key(post_url(b)) for b in blocks]
    if len([u for u in urls if u]) == len({u for u in urls if u}):
        return text
    size = lambda b: len(POST_URL_LINE.sub("", b, count=1).strip())
    best: dict[str, str] = {}
    for b, u in zip(blocks, urls):
        if u and (u not in best or size(b) > size(best[u])):
            best[u] = b
    kept = []
    for b, u in zip(blocks, urls):
        if not u:
            kept.append(b)
        elif u in best:
            kept.append(best.pop(u))
    return "\n\n".join(kept)


def unaccounted_posts(text: str, results, written: list) -> list[str]:
    """投稿の塊のうち、Luna の結果が済んでいないもの(読めなかった・結果に無い・形が崩れた)。
    済んだとみなすのは none と、**実際に書かれた候補**(written = items.json の中身)を指す extracted だけ(「候補にした」と
    言って候補を書いていない投稿を済みにしない。監査指摘)。結果の一覧そのものが読めなければ、全部の投稿が未処理。
    書き出しが投稿に分けられない(形が崩れた)なら、書き出し全体を1つの未処理にする(黙って0件にしない。監査指摘)。"""
    # 後段(normalize)で捨てられる形・事実の無い候補は「書かれた候補」に数えない(判定は candidate_usable で共有。監査指摘)
    items = {post_key(x["url"]) for x in written or [] if candidate_usable(x)}
    # 投稿1件ごとに結果を数え、**契約どおりの結果がちょうど1つ**ある投稿だけを済みにする
    # (同じ投稿に none と unreadable が返る矛盾を、none で済みにしない。監査指摘)
    # 形の検査(POST_RESULT_SCHEMA)より前に数える(崩れた結果も「同じ投稿への2つ目の結果」として矛盾に数える)
    per_url: dict[str, list[dict]] = {}
    for r in results if isinstance(results, list) else []:
        if not (isinstance(r, dict) and isinstance(r.get("url"), str) and r["url"].strip()):
            continue    # 空の url の結果は、どの投稿の結果にも数えない(url の無い塊を済みにしてしまう。監査指摘)
        per_url.setdefault(post_key(r["url"]), []).append(r)

    def settled(r) -> bool:
        # none は item が空、extracted は item が実際に書かれた候補の url(契約。依頼文 grok-verify 3b)
        if not schema_ok(r, POST_RESULT_SCHEMA):
            return False
        return (r["status"] == "none" and not r["item"].strip()) or (r["status"] == "extracted" and post_key(r["item"]) in items)
    # 形の崩れた候補は候補にしない(verify_grok_faces)。その候補の元の投稿(候補の url がその投稿か、投稿に貼られたリンク)は、
    # 結果がほかの候補を指していても済みにしない(確かめ直しで候補にし直す。崩れた候補の中身を黙って捨てない)
    done = {u for u, rs in per_url.items() if len(rs) == 1 and settled(rs[0])} - broken_items(text, written)[0]
    blocks = post_blocks(text)
    if not blocks:
        return [text] if text.strip() else []
    return [b for b in blocks if not post_url(b) or post_key(post_url(b)) not in done]     # url の無い塊は必ず未処理


def broken_items(text: str, written) -> tuple[set[str], list[str]]:
    """確かめ役が書いた候補のうち形の崩れたもの(candidate_usable でない)について、(結び付く投稿の鍵, どの投稿にも結び付かない候補の url)。
    結び付きは形で決める: 候補の url がその投稿そのもの(post_key が同じ)か、その投稿に貼られたリンク(repoint_to_post と同じ結び付き)。"""
    links = [(post_key(u), set(ls)) for u, ls in post_links(text)]
    tied, stray = set(), []
    for x in written if isinstance(written, list) else []:
        if x is None or candidate_usable(x):
            continue    # null は中身が無い。配列・文字列など dict でない要素は中身があり得るので、結び付かない崩れた候補として名指しする
        u = x.get("url") if isinstance(x, dict) else None
        hit = {k for k, ls in links if isinstance(u, str) and u.strip() and (k == post_key(u) or u in ls)}
        if hit:
            tied |= hit
        else:
            stray.append(u if isinstance(u, str) and u.strip() else f"(url の崩れた候補) {json.dumps(x, ensure_ascii=False)[:80]}")
    return tied, stray


def grok_week_usage(now_ts: float | None = None) -> int:
    """直近7日に、この利用者の**すべての** Grok セッションが使った X 検索の回数(週次上限の消費の実数)。
    利用枠は利用者で共有なので、本番のクローン以外(dev クローン・手での利用)の検索も差し引く(監査指摘 r116)。"""
    since = (now_ts or time.time()) - 7 * 86400
    n = 0
    for u in (Path.home() / ".grok" / "sessions").glob("*/*/updates.jsonl"):
        try:
            if u.stat().st_mtime >= since:
                n += len(re.findall(r'"variant":\s*"XSearch"', u.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return n


def deep_budget(used_7d: int) -> int:
    """この回の深掘りに使ってよい X 検索の回数。週の上限の GROK_DEEP_SHARE までの残りと、1回の上限 GROK_DEEP_MAX の小さいほう。"""
    return max(0, min(GROK_DEEP_MAX, int(GROK_WEEKLY_SEARCHES * GROK_DEEP_SHARE) - used_7d))


def plan_deep_dive(deep: dict[str, list[dict]], budget: int) -> dict[str, list[dict]]:
    """予算の範囲で、深掘りする問いを面ごとに選ぶ(問い1つに検索2回。面を順に1問ずつ配って偏らせない。1面3問まで)。"""
    chosen: dict[str, list[dict]] = {}
    left = budget
    for i in range(3):
        for key, asks in sorted(deep.items()):
            if i < len(asks) and left >= 2:
                chosen.setdefault(key, []).append(asks[i])
                left -= 2
    return chosen


def deep_dive_grok(queries: list[dict], outdir: Path, chosen: dict[str, list[dict]], errs: dict,
                   suffix: str = "-deep", retry: bool = False) -> set[str]:
    """選んだ問いだけを、Grok に X の原本で調べさせる(<key><suffix>.md に書き出す)。戻りは正常に終わらなかった面。
    retry は、1回目で答え(投稿)が無かった問いの投げ直し: 1回目と違う角度(その面の公式アカウント・ハッシュタグ・企画名)で探させる。"""
    by_key = {q["key"]: q for q in queries}
    targets = []
    for key, asks in chosen.items():
        q = by_key[key]
        accounts = "、".join(f"@{a}" for a in (q.get("accounts") or [])) or "(指定なし)"
        pp = outdir / f"prompt-{key}{suffix}.md"
        pp.write_text(render_prompt(
            "grok-deep", BRAND=q["brand"], TODAY=now_jst().strftime("%Y-%m-%d"), OUT=outdir / f"{key}{suffix}.md",
            MAX_SEARCHES=2 * len(asks),
            RETRY=(f"- **投げ直し**: どの問いも、1回目の検索では答えの投稿が見つからなかった。1回目と違う角度で探す"
                   f"(この面の公式アカウント {accounts} の from: 検索、ハッシュタグ、企画名・商品名)\n" if retry else ""),
            QUESTIONS="\n".join(f"{i + 1}. {a['question']}(なぜ X の原本が要るか: {a.get('why') or '記載なし'})" for i, a in enumerate(asks))),
            encoding="utf-8")
        targets.append((q, pp))
    return run_grok_prompts(targets, errs)


DEEP_ASK_NO = re.compile(r"(?m)^\s*[-*・]?\s*問い\s*[:：]\s*(\d+)")


def unanswered_asks(text: str, asks: list[dict]) -> list[dict]:
    """深掘りの書き出しで、答えの投稿(url のある塊の「- 問い: 番号」)が1つも無かった問い。
    番号の無い答えの投稿は、問いが1つならその問いの答えとみなす。問いが複数で番号の無い答えがあれば、どの問いの答えか
    分からないので投げ直さない(答えのある問いに予算を使わない。監査指摘)。"""
    answered, unnumbered = set(), False
    for b in post_blocks(text):
        if not post_url(b):
            continue
        m = DEEP_ASK_NO.search(b)
        if m:
            answered.add(int(m.group(1)))
        else:
            unnumbered = True
    if unnumbered:
        if len(asks) == 1:
            answered.add(1)
        else:
            print("grok: 深掘りの答えに問いの番号が無い投稿がある(どの問いの答えか分からないので、この面は投げ直さない)", flush=True)
            return []
    return [a for i, a in enumerate(asks) if i + 1 not in answered]


def grok_scheduled_now(now=None) -> bool:
    """今回の実行で Grok を回すか。GROK_HOURS が空なら毎回回す(従来動作)。"""
    if not GROK_HOURS:
        return True
    hours = {h.strip().lstrip("0") or "0" for h in GROK_HOURS.split(",") if h.strip()}
    return str((now or now_jst()).hour) in hours


def collect_explore(key: str, proc, out_f, err_f, deadline: float) -> tuple[list, str]:
    """探索プロセス1つを回収する。**締切は起動時から数えた絶対時刻**。

    残り時間が無ければ待たずに落とす(以前は `max(30, ...)` としており、
    締切を過ぎても1本あたり30秒ずつ延びていた)。
    出力は一時ファイルで受けるので、回収の順番待ちでパイプが詰まることはない。
    """
    # codex 本体を kill しても配下(fetch_page.py 等)は生き残るので、プロセスグループと子孫ごと落として回収する(finish_group)。
    # 起動できなかった探索は、その理由を標準エラーに添える
    _, note, timed_out = finish_group(proc, max(0, deadline - time.time()))
    cut = " [打ち切り]" if timed_out else ""
    out = err = ""
    for f, box in ((out_f, "out"), (err_f, "err")):
        try:
            f.flush()
            text = Path(f.name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        finally:
            f.close()
            Path(f.name).unlink(missing_ok=True)
        if box == "out":
            out = text
        else:
            err = text
    err = (err or "") + note + cut
    save_raw(edition_date(), f"explore-{key}-{time.time_ns() % 10**6}", out, err, proc.returncode)   # 生の出力を必ず残す
    # 認証切れ・利用上限は面の取得の失敗ではない(異常にしない。投稿は済み)。次の収集がまた探す
    if not cut and cli_check("codex", "collect", f"{now_jst():%H:%M} の収集 探索 {key} 面", proc.returncode, out, err):
        return [], (err or "") + "\n[CLI 停止]"
    from pipelib import extract_json_array_strict
    # 打ち切り・異常終了の間際の出力は答えにしない(途中までの候補を「全部」と読まない。監査指摘)
    got = extract_json_array_strict(out) if not cut and proc.returncode == 0 else None
    if got is None:
        # 「読めなかった」は「0件」と別の失敗。前置き・途中切れ・JSON 破損で調査結果が丸ごと
        # 消える経路なので、そう分かる形で残す(監査指摘 P1-5)
        print(f"探索: {key} の出力が読めない(本文 {len(out or '')}字) stderr: {err.strip()[-200:]}", flush=True)
        err = (err or "") + "\n[出力が読めない]"
        got = []
    elif not got:
        # 失敗の原因を捨てない(全滅したときに理由が分からなくなる)
        print(f"探索: {key} が0件 stderr: {err.strip()[-200:]}", flush=True)
    else:
        # 要素の形を揃える(null・崩れた要素で収集全体を止めない)。崩れた要素があれば「読めない出力」として記録する
        # (面の取得の失敗として collect_health に上がる。監査指摘)
        shaped = [s for s in (shape_item(x) for x in got) if s]
        broken = sum(1 for x in got if not item_intact(x))     # 捨てた候補と、欄・要素を落として揃えた候補
        if broken:
            print(f"探索: {key} の候補 {broken}/{len(got)}件の形が崩れていた(捨てた・欄を落とした)", flush=True)
            err = (err or "") + f"\n[出力が読めない] 形の崩れた候補 {broken}件"
        got = shaped
    return got, err


def run_explores(skip_explore: bool, skip_grok: bool) -> tuple[list[dict], dict]:
    queries = build_prompts()
    items, per = [], {}

    # **探索役は Luna(codex)**。全クエリ並列。締切は**起動前**に決める
    # (起動後に決めると、後から起動したぶんだけ実質の持ち時間が延びる)
    explore_deadline = time.time() + session_left(EXPLORE_TIMEOUT)    # 収集の締切も越えない
    explore_procs = []
    grok_jobs = []
    # 起動は1本ずつ一覧に足し、例外・中断で抜けても finally で起動済みの探索を止めて回収する
    try:
        for q in queries:
            window = "直近72時間" if q["key"] in ("trend", "fan-culture") else "直近48時間"
            if skip_explore:
                continue
            if not deadline_passed():
                cp = render_prompt("explore", WINDOW=window, TOPIC=q["topic"], MAX_ITEMS=8,
                                   RULES=COLLECT_RULES, ITEM=COLLECT_ITEM)
                wd = explore_workdir(q["key"])
                # 指示と素材は隔離ディレクトリ側のファイルで渡す(引数に詰めない。cwd がリポジトリ外なので base=wd)
                short = prompt_file(edition_date(), f"explore-{q['key']}", cp, base=wd)
                # 出力は**一時ファイル**へ落とす。PIPE のまま並列起動して順番に
                # communicate すると、後続プロセスはパイプが埋まった時点で止まり、
                # 正常な探索が打ち切り扱いになる(監査指摘)
                of = tempfile.NamedTemporaryFile(prefix=f"explore-{q['key']}-", suffix=".out",
                                                 delete=False, mode="w+", encoding="utf-8")
                ef = tempfile.NamedTemporaryFile(prefix=f"explore-{q['key']}-", suffix=".err",
                                                 delete=False, mode="w+", encoding="utf-8")
                if not deadline_passed():       # 準備のあいだに締切を過ぎることもあるので、起動の直前にも確かめる
                    explore_procs.append((q["key"], start_group(
                        # 打ち切り時に子孫ごと落とせるよう、独立したプロセスグループにする(start_group)
                        # cwd は隔離ディレクトリ。ここが workspace-write の書き込み範囲になる
                        explore_argv(short), cwd=wd, stdout=of, stderr=ef), of, ef, wd))
                    continue
                for f in (of, ef):
                    f.close()
                    Path(f.name).unlink(missing_ok=True)
                shutil.rmtree(wd, ignore_errors=True)
            # 収集の締切を過ぎたら起動しない(取得の失敗として collect_health に上がる)
            print(f"探索: 収集の締切を過ぎたので {q['key']} を起動しない", flush=True)
            per[f"explore_failed:{q['key']}"] = 1

        # **探索は Grok より先に回収する。**後回しにすると、Grok が長引くあいだ
        # 探索プロセスが締切を超えて走り続けてしまう(監査指摘)
        for key, p, of, ef, wd in explore_procs:
            got, err = collect_explore(key, p, of, ef, explore_deadline)
            for it in got:
                it["_via"] = "explore"
            items += got
            per[f"explore:{key}"] = len(got)
            if "[出力が読めない]" in err or "[打ち切り]" in err:
                per[f"explore_failed:{key}"] = 1     # 0件(正常な空振り)と取得の失敗を分ける(collect_health)
            elif err.endswith("[CLI 停止]"):
                per[f"explore_blocked:{key}"] = 1    # CLI の認証切れ・利用上限(取得の失敗に数えない)
            shutil.rmtree(wd, ignore_errors=True)
    finally:
        stop_groups([p for _, p, _, _, _ in explore_procs])

    # -- Grok(X 動向) --
    # **面ごとに独立したセッション**で回す。
    # 以前は「セッション数ではなく作業量に比例する」と考えて1セッションに10面を
    # まとめていたが、実測はその逆だった: 1セッション内では面が進むたびに
    # それまでの全検索結果を読み直すため、1呼出あたりの入力が 15k → 2,439k まで膨らむ。
    # 総入力は呼出数の2乗で効き、週次消費%は入力トークン量にきれいに比例する
    #   (実測: 6.9M→5pp / 17.9M→14pp / 20.4M→16pp)。
    # 面ごとに切れば文脈がリセットされ、総入力は分割数ぶんの1になる。
    # セッション起動の固定費(手順書+プロンプト)は約2kトークンで、削減額に対して誤差。
    # ブランド10面は減らさない。減らすのは1セッションが抱える文脈の量だけ。
    if not skip_grok:
        outdir = ROOT / "candidates" / ".grok"
        shutil.rmtree(outdir, ignore_errors=True)
        outdir.mkdir(parents=True, exist_ok=True)
        grok_errs: dict[str, str] = {}
        grok_started = time.time()
        grok_basic(queries, outdir, grok_errs)
        keep_blocked_partials(outdir, [q["key"] for q in queries if grok_blocked(grok_errs, q["key"])], "{k}.partial-*.md")
        # 前の収集で確かめ役が止まって確かめられなかった投稿(読み直しの列)も、同じ面の書き出しに足して確かめる
        taken = take_pending_posts(outdir, [q["key"] for q in queries])
        # Luna が面ごとに確かめて候補にする。X の原本でしか確かめられない問いは、予算を確かめて Grok に深掘りさせ、もう一度 Luna が確かめる
        # 投稿の喪失は、深掘りの確かめまで終えた最終結果で1度だけ通知する(当番の指摘 11fc3de74b)
        ledger = lost_posts_ledger()
        got, deep = verify_grok_faces(queries, outdir, ledger=ledger)
        if deep:
            used = grok_week_usage()
            budget = deep_budget(used)
            chosen = plan_deep_dive(deep, budget)
            print(f"grok: 深掘りの予算 {budget}回(直近7日の使用 {used}/{GROK_WEEKLY_SEARCHES}回)。"
                  f"問い {sum(len(v) for v in deep.values())}件のうち {sum(len(v) for v in chosen.values())}件を調べる", flush=True)
            per["grok_week_searches"] = used
            if chosen:
                # 深掘りが時間切れ・異常終了した面(書き出しは退けてある)と、正常に終わったのに書き出しが無い・空の面は、
                # 選んだ問いの確かめを失う。名指しで異常にする(判定は終了状態と書き出しの両方。監査指摘)
                deep_started = time.time()
                failed = deep_dive_grok(queries, outdir, chosen, grok_errs)
                # 深掘りの Grok が認証切れ・利用上限で止まった面は、まだ確かめ終えていない: 残った投稿を失ったと言わず、
                # 読み直しの列に残す(report_lost_posts。監査指摘 r4)
                ledger["blocked"] |= {k for k in chosen if grok_blocked(grok_errs, k)}
                keep_blocked_partials(outdir, [k for k in chosen if grok_blocked(grok_errs, k)], "{k}-deep.partial-*.md")
                spent1 = sum(grok_search_counts(deep_started).values())   # 1回目の実際の検索回数(指示は厳密には守られない)
                lost = sorted(k for k in failed | {k for k in chosen if not read_written(outdir / f"{k}-deep.md").strip()}
                              if not grok_blocked(grok_errs, k))
                if lost:
                    notify("collect", "Grok の深掘りが時間切れ・異常終了し、X の原本の確かめを失った面: "
                                      + ", ".join(f"{k}({grok_errs.get(k, '')[:120]})" for k in lost), ok=False)
                more, _ = verify_grok_faces([q for q in queries if q["key"] in chosen], outdir, suffix="-deep", ledger=ledger)
                got += more
                # 答えの投稿が無かった問いは、予算の範囲で1回だけ、違う角度で投げ直す(「見つからない」で終えない。
                # 2026-10-09: アスラン=BBⅡ世の誕生日ガシャの公式告知を1〜2回の検索で「見つからない」として、その号に載らなかった)。
                # 予算は、この回の深掘りの予算から1回目の分(配った回数と実際の検索回数の大きいほう)を差し引いた残りと、
                # 週の実使用から求め直した残りの小さいほう(監査指摘: 1回目が配分を超えて検索しても、この回の上限を超えない)
                retry = {k: v for k, asks in chosen.items() if k not in failed
                         for v in [unanswered_asks(read_written(outdir / f"{k}-deep.md"), asks)] if v}
                budget2 = max(0, min(budget - max(2 * sum(len(v) for v in chosen.values()), spent1),
                                     deep_budget(grok_week_usage())))
                chosen2 = plan_deep_dive(retry, budget2)
                if retry:
                    print(f"grok: 深掘りで答えの無かった問い {sum(len(v) for v in retry.values())}件のうち "
                          f"{sum(len(v) for v in chosen2.values())}件を投げ直す(予算 {budget2}回)", flush=True)
                if chosen2:
                    failed2 = deep_dive_grok(queries, outdir, chosen2, grok_errs, suffix="-deep2", retry=True)
                    ledger["blocked"] |= {k for k in chosen2 if grok_blocked(grok_errs, k)}
                    keep_blocked_partials(outdir, [k for k in chosen2 if grok_blocked(grok_errs, k)], "{k}-deep2.partial-*.md")
                    lost2 = sorted(k for k in failed2 | {k for k in chosen2 if not read_written(outdir / f"{k}-deep2.md").strip()}
                                   if not grok_blocked(grok_errs, k))
                    if lost2:
                        # 投げ直しの失敗も、1回目と同じく名指しで異常にする(黙って問いの確かめを失わない。監査指摘)
                        notify("collect", "Grok の深掘りの投げ直しが時間切れ・異常終了し、X の原本の確かめを失った面: "
                                          + ", ".join(f"{k}({grok_errs.get(k, '')[:120]})" for k in lost2), ok=False)
                    more, _ = verify_grok_faces([q for q in queries if q["key"] in chosen2], outdir, suffix="-deep2", ledger=ledger)
                    got += more
        report_lost_posts(ledger)
        for t in taken:     # 読み直しの列は確かめ終えたら消す(止まった面は確かめ役が列に足し直している)
            t.unlink(missing_ok=True)
        # 全面が正常に終わって明示的に「なし」と書いた(エラー出力も無い)なら、正常な空振りで異常ではない(監査指摘)
        is_none = lambda k: re.fullmatch(r"(なし|見つからない)[。\s]*", read_written(outdir / f"{k}.md").strip())
        explicit_none = not grok_errs and all(is_none(q["key"]) for q in queries)
        # 調べか確かめの CLI が認証切れ・利用上限で答えなかった面(投稿は済み。確かめの投稿は読み直しの列にある)
        stopped = {q["key"] for q in queries if grok_blocked(grok_errs, q["key"]) or q["key"] in ledger["blocked"]}
        if not got and explicit_none:
            print("grok 0件(全面が正常に終わり「なし」)", flush=True)
        elif not got and stopped and all(q["key"] in stopped or is_none(q["key"]) for q in queries):
            print(f"grok 0件(認証切れ・利用上限で答えなかった面 {', '.join(sorted(stopped))}。ほかは「なし」。投稿は済み)", flush=True)
        elif not got:
            print("grok 0件", flush=True)
            # 全面0件を黙って流さない(2026-10-02: CLI が古く API に 426 で拒まれ、9面すべて0件。記録は「grok 0件」だけで、
            # 候補が半分以下になり号が16本に落ちたのに誰も気づかなかった)。面ごとのエラーと、Grok のセッション記録の失敗理由を添える
            notify("collect", f"Grok(X 調査)が {len(queries)}面すべてで0件。候補の約半分を失う:\n"
                              + ("\n".join(f"- {k}: {v}" for k, v in grok_errs.items()) or "- 面ごとのエラー出力なし")
                              + "\n- セッション記録の失敗理由: " + grok_session_error(), ok=False)
        for it in got:
            it["_via"] = "grok"
        items += got

        # 面ごとの取得数に割り戻す(watch の全滅検知が per_query を見るため)
        by_brand = {}
        for it in got:
            b = str(it.get("brand", ""))
            by_brand[b] = by_brand.get(b, 0) + 1
        for q in queries:
            per[f"grok:{q['key']}"] = by_brand.get(q["brand"], 0)
            if q["key"] in stopped:
                per[f"grok_blocked:{q['key']}"] = 1     # 認証切れ・利用上限(watch の全滅検知に数えない)
            elif is_none(q["key"]) and q["key"] not in grok_errs:
                per[f"grok_none:{q['key']}"] = 1        # 正常に終わって「なし」と書いた面(停止した面と混ざっても watch が全滅にしない)
        print(f"grok: {len(queries)}面を面別セッションで {len(got)}件(面別 {by_brand})", flush=True)
        # 週次上限を消費するのは X 検索の回数だけ。面ごとに実数を記録し、指示の2倍を超えた面は異常にする(依頼文の数字は厳密には守られない)
        searches = grok_search_counts(grok_started)
        for b, n in searches.items():
            per[f"grok_searches:{b}"] = n
        cap = (GROK_MAX_SEARCHES + GROK_OFFICIAL_PAGES) * 2    # 角度の検索と公式アカウントの遡りの、指示の2倍
        over = {b: n for b, n in searches.items() if n > cap}
        print(f"grok: X 検索 {sum(searches.values())}回(面別 {searches})", flush=True)
        if over:
            notify("collect", f"Grok(X 調査)の検索が指示(角度 {GROK_MAX_SEARCHES}回+公式の遡り {GROK_OFFICIAL_PAGES}回/面)の2倍を超えた面: "
                              f"{over}。週次の利用枠を余分に消費している", ok=False)
    elif (ROOT / GROK_PENDING).is_dir() and any(p.suffix in (".md", ".taken") for p in (ROOT / GROK_PENDING).iterdir()):
        # Grok を回さない回でも、前の収集で確かめ役が止まって確かめられなかった投稿は確かめる(週の検索枠は使わない。紙面を1日遅らせない)
        outdir = ROOT / "metrics" / "work" / "grok-pending-run"
        shutil.rmtree(outdir, ignore_errors=True)
        outdir.mkdir(parents=True, exist_ok=True)
        keys = [q["key"] for q in queries if any((ROOT / GROK_PENDING / f"{q['key']}{s}").exists() for s in (".md", ".taken"))]
        taken = take_pending_posts(outdir, keys)
        ledger = lost_posts_ledger()
        got, _ = verify_grok_faces([q for q in queries if q["key"] in keys], outdir, suffix="", ledger=ledger)
        report_lost_posts(ledger)
        for t in taken:
            t.unlink(missing_ok=True)
        shutil.rmtree(outdir, ignore_errors=True)
        for it in got:
            it["_via"] = "grok"
        items += got
        print(f"grok: 読み直しの列の投稿 {len(keys)}面を確かめて {len(got)}件", flush=True)

    return items, per


# ---- 正規化・記録 --------------------------------------------------------------

# 収集役が申告する kind は**種別の判定には使わない**(source_types.yml が決める)。
# 申告を採っていたころ、攻略サイトを「公式」と申告した候補がそのまま紙面の
# バッジになっていた。kind は候補に写さない(shape_item で落とし、正規化も使わない)ので、崩れていても中身は何も失わない。
# だから形の検査(ITEM_SCHEMA)にも入れない(2026-10-22: Luna が kind にアイドル名の配列を入れ、候補にはなったのに
# 投稿を「結果が無い」と数え、確かめ直しでも済まずに「失った」と通知した)


def is_x(url: str) -> bool:
    return any(h in url for h in X_HOSTS)


def needs_classify(cands: list[dict]) -> bool:
    """判定表の更新(合議)→ 紙面の付け直しを走らせるか。今回の候補に未確認があるか、**紙面に未確認の出典が
    残っている**とき。候補だけで決めると、空振りや判定済みの候補ばかりの収集では紙面の未確認が残り続ける
    (合議の対象は紙面の未確認も含むのに、入口が候補だけで閉じていた。監査指摘 2026-10-03)"""
    import classify_sources
    return any(c.get("source_type") == "未確認" and c.get("url") for c in cands) \
        or bool(classify_sources.unresolved_post_sources())


def x_post_url(url: str, fetch=None) -> str:
    """`x.com/i/status/<ID>`(投稿者の無い投稿 URL)を `x.com/<投稿者>/status/<ID>` に直す。

    種別は X のアカウント単位で決まるので、投稿者の無い形は判定できず、紙面に未確認の出典として残る
    (2026-10-03: Grok の trend 面が 16件すべてこの形で書いた)。投稿者は X の oEmbed が返す。
    引けなければ元のまま返す(合議が投稿者を引き直し、決まらなければ watch が未確認の出典として知らせる)
    """
    m = X_ANON_POST.fullmatch(url)
    if not m:
        return url
    handle = x_post_author(m.group(1), fetch or http_get)
    return f"https://x.com/{handle}/status/{m.group(1)}" if handle else url


# 面が判別できる語 → ブランド。名鑑(アイドル名)で拾えない作品名・ブランド呼称を補う。
BRAND_WORDS = {
    "dsva": ("vα-liv", "ヴイアラ", "va-liv", "valiv", "876プロ", "ディアリースターズ"),
    "gaku": ("学園アイドルマスター", "学マス", "初星学園", "gakuen"),
    "shiny": ("シャイニーカラーズ", "シャニマス", "シャニソン", "283プロ", "shinycolors"),
    "million": ("ミリオンライブ", "ミリシタ", "ミリアニ", "765プロオールスターズ"),
    "cg": ("シンデレラガールズ", "デレステ", "デレマス", "シンデレラ"),
    "sidem": ("sidem", "315プロ", "サイドエム"),
    "765": ("765pro allstars", "765ミリオンスターズ", "765as"),
    "joint": ("ツアーズ", "ツアマス", "iwsf", "合同ライブ", "ポプマス"),
}


def guess_brand(text: str, idols: dict) -> str | None:
    """本文から面を1つに特定できるときだけ返す(複数該当・不明なら None)。

    定点観測は sources.yml のブランドを起点にするため、公式ポータルのような
    横断ソースの新着は brand が general/other のまま残りやすい。実際
    「上水流宇宙 BIRTHDAY ONLINE LIVE 2026」が other になり、grok/claude が
    dsva と付けた同じ話題と別の面に分かれて二重に記事化された。
    アイドル名は名鑑(docs/_data/idols.json)で面が確定するので、機械で直す。
    """
    hit = brand_hits(text, idols)
    return hit.pop() if len(hit) == 1 else None


def brand_hits(text: str, idols: dict) -> set[str]:
    """本文が指す面の全部(作品名・ブランド呼称と、名鑑のアイドル名)。idols を空にすると作品名・呼称だけ。"""
    low = text.lower()
    hit = {b for b, words in BRAND_WORDS.items() if any(w.lower() in low for w in words)}
    return hit | {b for name, b in idols.items() if name and name in text}


def load_idol_brands() -> dict:
    p = ROOT / "docs" / "_data" / "idols.json"
    if not p.exists():
        return {}
    try:
        return {x.get("name"): x.get("brand") for x in json.loads(p.read_text(encoding="utf-8"))}
    except Exception:
        return {}


ITEM_STR_KEYS = ("title", "brand", "url", "event_date", "published_date", "deadline", "dedup_key", "engagement", "quote",
                 "link_from", "_via")


def shape_item(it) -> dict | None:
    """候補1件の形を揃える(文字列の欄は文字列に、facts・mentioned_idols は文字列の配列に)。使えない(dict でない・
    url が不正)なら None。**正規化と、投稿・ページの突き合わせが同じ判定を使う**: 突き合わせで「候補を書いた」と
    数えたのに正規化で捨てる、という食い違いを作らない(監査指摘 r3: 日付が数値の候補が例外で黙って消え、元の投稿は処理済みになった)。"""
    if not isinstance(it, dict) or not clean_url(it.get("url") if isinstance(it.get("url"), str) else ""):
        return None
    s = {k: it[k] for k in ITEM_STR_KEYS if isinstance(it.get(k), str)}     # 文字列でない欄は無かったことにする(既定値が効く)
    s["facts"] = [f for f in (it.get("facts") if isinstance(it.get("facts"), list) else []) if isinstance(f, str) and f.strip()]
    s["mentioned_idols"] = [x for x in (it.get("mentioned_idols") if isinstance(it.get("mentioned_idols"), list) else [])
                            if isinstance(x, str) and x.strip()]
    return s


# モデルの出力の契約(prompts/collect-item.md ほか)の形。**処理済みにしてよいかの判定は、この schema で検める**
# (手で欄を1つずつ足すと、日付の形・真偽値の番号・重複など崩れ方が尽きなかった。2026-10-06 の監査)。
# 文字列の欄の null は「空」で何も失わないので許す。日付は YYYY-MM-DD か空。
# 検めるのは候補に写す欄(ITEM_STR_KEYS・facts・mentioned_idols)だけ。写さない欄(kind)は崩れても何も失わないので検めない
_S = {"type": ["string", "null"]}
_DATE = {"anyOf": [{"type": "null"}, {"type": "string", "pattern": r"^(\d{4}-\d{2}-\d{2})?$"}]}
ITEM_SCHEMA = {"type": "object", "required": ["url", "facts"],
               "properties": {"url": {"type": "string", "minLength": 1}, "title": _S, "brand": _S,
                              "event_date": _DATE, "published_date": _DATE, "deadline": _DATE,
                              # 空白だけの事実は正規化で消える(事実なしで処理済みにしない。監査指摘)
                              "facts": {"type": "array", "items": {"type": "string", "pattern": r"\S"}, "minItems": 1},
                              "quote": _S, "dedup_key": _S, "engagement": _S, "link_from": _S,
                              "mentioned_idols": {"type": "array", "items": {"type": "string"}}}}
WATCH_PAGE_SCHEMA = {"type": "object", "required": ["page", "status", "items"],
                     "properties": {"page": {"type": "integer"}, "status": {"enum": ["extracted", "none", "unreadable"]},
                                    "items": {"type": "array"}}}
POST_RESULT_SCHEMA = {"type": "object", "required": ["url", "status", "item"],
                      "properties": {"url": {"type": "string", "minLength": 1},
                                     "status": {"enum": ["extracted", "none", "unreadable"]}, "item": {"type": "string"}}}
DEEP_ASK_SCHEMA = {"type": "object", "required": ["question"],
                   "properties": {"question": {"type": "string", "minLength": 1}, "why": _S}}


def item_intact(it) -> bool:
    """候補1件が依頼した形のまま(正規化で何も落とさない)か。ITEM_SCHEMA で検め、url は正規化できること。
    shape_item は崩れた欄・要素を落として使える形に揃えるが、**落としたことは「処理済み」の根拠にしない**
    (facts の要素が1つ崩れていても残りで「抽出した」となる・締切の形が崩れて正規化で消える、のまま既読・処理済みに
    なる。監査指摘)。"""
    return schema_ok(it, ITEM_SCHEMA) and shape_item(it) is not None


def candidate_usable(it) -> bool:
    """「候補を書いた」と数えてよいか: 依頼した形のまま(item_intact。事実 facts が1件以上ある)。
    url だけの候補を指して「抽出した」とされたページ・投稿を処理済みにしない(事実を抽出していないのに既読になる。監査指摘)。
    形の検査であって、事実の中身は見ない。"""
    return item_intact(it)


def normalize(items: list[dict]) -> list[dict]:
    out, seen_url = [], {}
    ts = now_jst().isoformat(timespec="seconds")
    idols = load_idol_brands()
    for i, raw in enumerate(items):
        it = shape_item(raw)
        if it is None:
            if isinstance(raw, dict) and raw.get("url"):
                print(f"候補の URL を捨てた(形が不正): {str(raw.get('url'))[:80]!r}", flush=True)
            continue
        try:
            # URL は pipelib.clean_url(唯一の入口)で正規化する。探索の出力は末尾に改行やゴミ(`\n-`)を
            # 付けてくる(実測 2026-09-15: 50件)。使えない形は捨てて理由を残す(黙って切り詰めない)
            url = clean_url(it.get("url"))
            if not url:
                if it.get("url"):
                    print(f"候補の URL を捨てた(形が不正): {str(it.get('url'))[:80]!r}", flush=True)
                continue
            url = x_post_url(url)
            valid = {"general", "765", "cg", "million", "shiny", "sidem", "gaku", "dsva", "joint", "other"}
            brand = it.get("brand") if it.get("brand") in valid else "other"
            head = " ".join([it.get("title") or "", it.get("dedup_key") or ""])
            text = " ".join([head, *(it.get("facts") or [])])
            if brand in ("general", "other"):
                # 面が付いていない候補は、本文から面を1つに特定できれば補正する
                g = guess_brand(text, idols)
                if g:
                    brand = g
            elif brand != "joint":
                # collector が具体的な面を選んでいれば原則その判断を尊重する(合同を各面へ割り直したりしない)。
                # ただし見出しか主題キーが別の作品を1つだけ名指しし、候補のどこにも collector の面の作品名・アイドル名が
                # 無いときは、面の取り違えとして名指しの作品へ直す(2026-10-18号: トレンドの探索が vα-liv の告知に gaku を付け、
                # 学マス面と vα-liv 面に同じ話題の記事が1本ずつ立った。面は並列に選ぶので、選定では重なりを見つけられない)
                named = brand_hits(head, {})
                if len(named) == 1 and brand not in brand_hits(text, idols):
                    brand = named.pop()
            dk = re.sub(r"[^a-z0-9-]", "-", (it.get("dedup_key") or "").lower()).strip("-") or f"auto-{i}"
            facts = [f for f in (it.get("facts") or []) if isinstance(f, str) and f.strip()]
            if it.get("engagement"):
                facts.append(f"エンゲージメント: {it['engagement']}")
            if it.get("mentioned_idols"):
                facts.append("言及アイドル: " + "、".join(it["mentioned_idols"]))
            c = {
                "id": f"{now_jst().strftime('%Y%m%d%H%M')}-{it.get('_via','x')}-{i}",
                "title": (it.get("title") or "")[:120] or "(無題)",
                "brand": brand,
                # 種別は**URL から判定する**。収集役の自己申告(kind)は採らない。
                # 申告のままにしていたため、攻略サイトを「公式」と申告した候補が
                # そのまま紙面のバッジになっていた(実測 26件・18記事)
                "source_type": classify_source(url),
                "url": url,
                "found_at": ts,
                "dedup_key": dk,
                "facts": facts,
                "origin": "watch" if it.get("_via") == "watch" else "explore",
                "via": it.get("_via", "explore"),
                "verify": "unconfirmed",
            }
            for k in ("event_date", "deadline", "published_date"):
                v = (it.get(k) or "").strip()
                if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
                    c[k] = v
            # X の投稿の掲載日は投稿 ID から日本時間で決める(モデルの申告は採らない。pipelib.align_x_published_date)
            align_x_published_date(c)
            # 出典ページからの写し(verify が本文と照らして URL の取り違えを捕まえる)
            if isinstance(it.get("quote"), str) and it["quote"].strip():
                c["quotes"] = [it["quote"].strip()[:200]]
            # リンク元(url がリンクとして載っていた投稿・ページ)。出典の種別の判定が、リンク元の種別を引き継ぐかを確かめる材料
            lf = clean_url(it.get("link_from") or "")
            if lf and link_key(lf) and page_key(lf) != page_key(url):
                c["link_from"] = lf
            if url in seen_url and not c.get("quotes") and not seen_url[url].get("quotes"):  # URL 重複は facts をマージ
                tgt = seen_url[url]
                tgt["facts"] = list(dict.fromkeys(tgt["facts"] + c["facts"]))
                if c.get("link_from") and not tgt.get("link_from"):     # 後から得たリンク元も落とさない(監査指摘 2026-10-21)
                    tgt["link_from"] = c["link_from"]
                continue
            if url in seen_url:
                # 写しのある候補は、裏取り(写しの照合)の**前に**混ぜない。取り違えた候補の事実と写しを混ぜると、
                # このページの正しい候補まで failed になる(監査指摘)。別の候補として裏取りし、日別ファイルへ入れるときに結合する
                out.append(c)
                continue
            seen_url[url] = c
            out.append(c)
        except Exception as e:
            # 形は shape_item で揃えてあるので、ここに来るのはコードの欠陥。黙って捨てない
            print(f"候補を正規化できずに捨てた({type(e).__name__}: {e}): {it.get('url')[:80]!r}", flush=True)
            anomaly("collect", f"候補を正規化できずに捨てた({type(e).__name__}: {e}): {it.get('url')}")
            continue
    return out


STALE_DAYS = 14  # 収集窓は48〜72時間。これを大きく超えて古い情報は鮮度切れ(2025年告知を新報扱いした事故の再発防止)


def x_status_date(url: str) -> datetime.date | None:
    """x.com/twitter.com の status ID(Snowflake)から投稿日(日本時間)を復元する(機械検証可能な鮮度情報)。"""
    t = x_status_time(url)
    return t.date() if t else None


# Grok の書き出しの投稿に、コードが足す日本時間の投稿日時の行(grok-verify の依頼文がこの名前で指す)
JST_POST_TIME = "投稿日時(日本時間・投稿 ID から)"


def with_jst_post_time(text: str) -> str:
    """Grok の書き出しの X の投稿ごとに、投稿 ID から復元した日本時間の投稿日時の行を url の行の次に足す。
    2026-10-21号: Grok は `投稿日時` を GMT で書く(Tue, 20 Oct 2026 15:00:03 GMT)。10/21 0:00 JST の「本日は千鶴さんの誕生日」を、
    確かめ役が 10/20 の出来事・掲載日として素材にした。すでに足してある投稿(やり直しの書き出し)には足さない。"""
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        out.append(line)
        m = POST_URL_LINE.match(line)
        t = x_status_time(m.group(1)) if m and is_x(m.group(1)) else None
        if t and not (i + 1 < len(lines) and JST_POST_TIME in lines[i + 1]):
            out.append(f"- {JST_POST_TIME}: {t.strftime('%Y-%m-%d %H:%M')}")
    return "\n".join(out)


def check_stale(c: dict) -> str | None:
    """鮮度切れなら理由を返す。X は Snowflake、それ以外は自己申告の published_date で判定。"""
    today = now_jst().date()
    posted = x_status_date(c["url"]) if is_x(c["url"]) else None
    if posted and (today - posted).days > STALE_DAYS:
        return f"Xポストが古い(投稿日 {posted.isoformat()})。過去の告知を新報として扱わない"
    pub = c.get("published_date")
    if pub and (today - datetime.date.fromisoformat(pub)).days > STALE_DAYS:
        return f"掲載日 {pub} が古い。過去の告知を新報として扱わない"
    return None


def verify(cands: list[dict]) -> dict:
    """候補の裏取り。鮮度切れは種別を問わず failed。

    以前 `confirmed` は「URL が生きている」だけを意味していた。名前が実態より
    強く、lint の側も「捏造の検査は collect の verify が担う」と書いて手を抜いていた。
    URL の実在は確かに見ているが、**書かれている事実が出典にあるか**は見ていなかった。

    そこで、取得した本文に facts の日付・金額が出てくるかまで見る。
    出てこない粒は `unbacked_facts` に残し、`confirmed` は名乗らせない。
    ただし**発行は止めない**。実測では未一致の多くが「ページ自身の掲載日」や
    画像の中の価格で、捏造の証拠にはならないため(`unbacked_facts` の説明を参照)。

    X は Grok の観測をもって verify とする(ログイン必須で機械的に読めないため)。
    """
    counts = {"confirmed": 0, "unconfirmed": 0, "failed": 0}
    for c in cands:
        try:
            stale = check_stale(c)
            if stale:
                c["verify"] = "failed"
                c["verify_note"] = stale
                counts["failed"] += 1
                continue
            if is_x(c["url"]):
                c["verify"] = "confirmed" if (c["via"] == "grok" and c["source_type"] == "公式") else "unconfirmed"
            else:
                # 素の取得に 429 を返すサイト(browser_only)は urllib で叩かず、描画した本文で照合する(2026-10-26 編集長
                # 「429を出すようなアクセスはするな。Chromeを使え。」)。描画できなければ、今は確かめられないだけ(メンテナンスと同じ扱い)
                pre = browser_only(c["url"])
                if pre:
                    html, why = rendered_or_note(c["url"])
                    if not html:
                        c["verify"] = "unconfirmed"
                        c["verify_note"] = "出典は描画でしか読まないサイトで、描画できず facts を本文で確かめられない"
                        c["render_error"] = why[:300]
                        counts["unconfirmed"] += 1
                        continue
                    ok, body, cs, final = True, html.encode("utf-8", "replace"), "utf-8", c["url"]
                else:
                    req = urllib.request.Request(c["url"], headers={"User-Agent": UA})
                    try:
                        opened = urllib.request.urlopen(req, timeout=15)
                    except urllib.error.HTTPError as e:
                        # 転送せずに 5xx でメンテナンスの画面を返すサイト(2026-10-23: ランティスの 503)も、今は確かめられないだけ。
                        # 404・メンテナンスの表示の無い 5xx は従来どおり取得できない(failed)
                        if not maintenance_http_error(e):
                            raise
                        c["verify"] = "unconfirmed"
                        c["verify_note"] = f"出典がメンテナンス中(HTTP {e.code} でメンテナンスの画面)で、facts を本文で確かめられない"
                        counts["unconfirmed"] += 1
                        continue
                    with opened as res:
                        ok = res.status < 400
                        body = res.read(PAGE_READ_BYTES) if ok else b""
                        cs = res.headers.get_content_charset()
                        final = res.geturl()
                # 読み直しの描画。描画で読んだページ(pre)は描画済みなので、ブラウザをもう一度起動しない
                reread = (lambda: ("", "")) if pre else (lambda: rendered_or_note(c["url"]))
                # メンテナンス画面へ転送されたら、案内は出典の本文ではない。写しが無いのは URL の取り違えではなく、今は確かめられないだけ
                # (failed にすると、メンテナンスのあいだに集めた公式の候補を使えなくする)
                if ok and maintenance_redirect(c["url"], final):
                    c["verify"] = "unconfirmed"
                    c["verify_note"] = f"出典がメンテナンス中({final} へ転送)で、facts を本文で確かめられない"[:300]
                    counts["unconfirmed"] += 1
                    continue
                if ok:
                    text = html_to_text(body, cs)
                    # 描画の失敗は素の本文で続けるが、原因は判定の記録に残す。捨てると、描画の時間切れで写しが見えなかったのを
                    # 「URL の取り違え」と記録し、なぜ落ちたかを当番が再現して確かめることになる(監査指摘
                    # render-diagnostic-lost-in-classification と同じ型)
                    render_errs: list[str] = []
                    # CSR で本文が空同然か、描画必須のサイト(定点観測の portal)なら描画してから読み直す(定点観測と同じ経路)
                    if needs_render(c["url"], text):
                        rendered, why = reread()
                        render_errs += [why] if why else []
                        if rendered:
                            text = html_to_text(rendered.encode("utf-8", "replace"))
                    periods = extract_periods(text)
                    if periods:
                        c["periods"] = periods
                    # facts の日付・金額が本文にあるか。取ってあるのに捨てていた本文を使う。
                    #
                    # **食い違ったときも描画して確かめ直す。**素の HTML では
                    # 本文が JS で描かれるサイトがあり、ナビゲーションだけが取れて
                    # 「価格が本文に無い」と誤って判定していた(実測: 公式ポータルの
                    # 配信チケット記事で 14,000円 を取りこぼした)。
                    # portal 以外のサイトの描画漏れに備え、粒が欠けたときにも描画する
                    unbacked = unbacked_facts(c.get("facts") or [], text)
                    if unbacked:
                        rendered, why = reread()
                        render_errs += [why] if why else []
                        if rendered:
                            unbacked = unbacked_facts(c.get("facts") or [],
                                                      html_to_text(rendered.encode("utf-8", "replace")))
                    if unbacked:
                        c["unbacked_facts"] = unbacked[:12]
                    # **写しがそのページに無ければ、URL の取り違え。**事実は別のページのもので、この候補には出典が無い。
                    # 使わせない(failed)。日付・金額の粒は近い記事どうしで重なるので、粒の照合だけでは通ってしまう
                    # (2026-09-28〜10-01 の noctchill / Master ShowPiece)。描画しないと本文が出ないページがあるので、
                    # 無かったときだけ描画して確かめ直す
                    quotes = c.get("quotes") or []
                    missing = [q for q in quotes if quote_on_page(q, text) is False]
                    matched = any(quote_on_page(q, text) for q in quotes)
                    if missing:
                        rendered, why = reread()
                        render_errs += [why] if why else []
                        if rendered:
                            rtext = html_to_text(rendered.encode("utf-8", "replace"))
                            missing = [q for q in missing if quote_on_page(q, rtext) is False]
                            matched = matched or any(quote_on_page(q, rtext) for q in quotes)
                    if missing:
                        c["verify"] = "failed"
                        c["verify_note"] = (f"写しが出典の本文に無い(URL の取り違えの疑い): {missing[0][:60]}"
                                            + (f"。ただし描画できず素の本文で照合した({render_errs[-1][:200]})" if render_errs else ""))
                        counts["failed"] += 1
                        print(f"  裏取り: {c['url']} に写しが無い → 使わない({c.get('title')})", flush=True)
                        continue
                    # URL をモデルが選んだ候補(探索・Grok の確かめ)で、照合できる写しが無ければ、URL の取り違えを確かめられて
                    # いない。confirmed を名乗らせない(未確認として記録する。採否は選定・校閲のモデル。監査指摘)。
                    # 定点観測の候補は URL を巡回先からコードが決めるので要らない
                    if c.get("via") != "watch" and not matched:
                        c["verify_note"] = "出典ページからの写しが無い・短すぎて、URL の取り違えを確かめられない"
                    if render_errs:
                        c["render_error"] = render_errs[-1][:300]
                good_type = c["source_type"] in GOOD_SOURCE_TYPES
                c["verify"] = ("confirmed" if ok and good_type and not c.get("unbacked_facts") and not c.get("verify_note")
                               else ("unconfirmed" if ok else "failed"))
        except Exception as e:
            # 取れなかった原因を候補に残す(採否・当番の診断で「なぜ failed か」を追えるように)
            c["verify"] = "failed"
            c["verify_note"] = f"出典を取得できない: {type(e).__name__}: {e}"[:300]
        counts[c["verify"]] += 1
    return counts


def merge_into_day_file(cands: list[dict], day: str) -> int:
    """candidates は号(edition)日付でキーする。1つの号の素材=1ファイルで、収集サイクル
    (07:30〜翌03:30)が暦日をまたいでも分割されない。パイプラインが前日以前の
    ファイルを読む必要は無い(未来日程は stock/scheduled、既報判定は stock/stories.yml。選定が読んだ後にこのファイルへ
    入った候補は、次の号の組版が自分のファイルへ写す: compose.carry_late_candidates)。
    day は対象号(main で決めた date)を使う。壁時計から取り直すと、06:00 をまたいだ
    当番の拾い直しで checkout した号と別日のファイルへ書く(監査指摘 rerun-collect-ignores-target-edition)。"""
    p = ROOT / "candidates" / f"{day}.json"
    existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    by_url = {c["url"]: c for c in existing}
    added = 0
    # 写しの照合で落ちた候補(URL の取り違え)は、事実がこのページのものではない。このページの候補へ混ぜない
    # (混ぜると「弱いほうを採る」でこのページの正しい候補まで failed になる。監査指摘)。正しい候補を先に入れ、
    # 落ちた候補は同じ URL の候補が無いときだけ記録として残す。既にあるのが落ちた候補なら、正しい候補で置き換える
    misplaced = candidate_misplaced
    for c in sorted(cands, key=misplaced):
        if c["url"] in by_url:
            tgt = by_url[c["url"]]
            if misplaced(c):
                continue
            if misplaced(tgt):
                existing[existing.index(tgt)] = c
                by_url[c["url"]] = c
                continue
            merge_candidate_facts(tgt, c)
        else:
            existing.append(c)
            by_url[c["url"]] = c
            added += 1
    # 保存済みの候補(直す前に GMT の日付で入ったもの)も、X の投稿の掲載日を投稿 ID の日本時間に揃える(監査指摘: マージは
    # published_date を引き継がず、再収集しても 10/20 のまま選定・執筆へ渡った)
    for c in existing:
        align_x_published_date(c)
    write_checked(p, json.dumps(existing, ensure_ascii=False, indent=2) + "\n")   # 書式(candidates.schema)で検めてから書く
    return added


def hand_to_oncall(code: int = 0) -> None:
    """人に「異常」として通知したものは、申告で終えずに当番がなぜなぜする。直したら定点観測を取り直し、落とした新着を
    この号の選定リストに入れるまでが当番の仕事(編集長 2026-10-07「やらかしてドロップしたのは責任を持って修正して紙面に乗せろ」。
    以前は「収集は終わっているので再実行しない」で、直しても次の定時収集まで拾われず、最後の収集なら1号遅れた)。
    当番が取り直しまで終える時刻は、ここ(呼び出し時)で固定して渡す(起動し直しても延びない)。
    当番を呼べたら、**工程の排他を持ったまま**動いている印を置く(収集が終わって排他が空いた瞬間に組版が取ると、
    当番は取り直せない。組版は印を見て待ち、受け渡しの間は譲る。監査指摘)"""
    end_at = collect_oncall_end(now_jst())
    date = edition_date()
    # 収集が止まった(例外・SIGTERM・非0)なら理由を渡す。理由の無い呼び出しは「工程は止まっていない」(--finished)になり、
    # 当番の「いま再実行しない」が人に渡らなくなる(監査指摘 2026-10-24)
    stopped = f"収集が止まった(exit {code})。下の一覧をなぜなぜし、原因を直して取り直すこと" if code else ""
    if diagnose_anomalies("collect", date, rerun=True, reason=stopped, extra_args=["--end-at", str(int(end_at))]):
        mark_collect_oncall(end_at, date)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-git", action="store_true", help="ブランチ操作・push をしない(テスト用)")
    ap.add_argument("--skip-watch", action="store_true")
    ap.add_argument("--skip-explore", "--skip-claude", dest="skip_explore",
                    action="store_true", help="Web 探索(Luna)を回さない")
    ap.add_argument("--skip-grok", action="store_true")
    ap.add_argument("--force-grok", action="store_true",
                    help="GROK_HOURS の時刻判定を無視して Grok を回す(手動の再収集用)")
    ap.add_argument("--date", metavar="YYYY-MM-DD", default=None,
                    help="対象号の発行日(既定は壁時計から算出)。当番の拾い直しは 06:00 の号境界や"
                         "発行後 watch で異常発生日と取り込み先の号がずれるため、取り込み先の号を明示する")
    ap.add_argument("--oncall-rerun", action="store_true",
                    help="当番が繰り越し(_pending)を拾い直す再実行。読めないバッチを諦めず未処理のまま残し、残れば非0で終える")
    args = ap.parse_args()
    # 試験実行(--no-git)では Discord へ通知しない。本物の警報と見分けが付かなくなる
    set_quiet(args.no_git)
    # 収集の締切は**排他を待つ前**に決める(待った分だけ延ばさない)
    global DEADLINE
    DEADLINE = collect_deadline(args.oncall_rerun)
    # 実行記録の開始と起動のされ方(組版が、定時の回ごとに記録があるかを検める: pipelib.collect_slot_gaps)
    RUN.update(started_at=now_jst().isoformat(timespec="seconds"), trigger=run_trigger(args.oncall_rerun), recorded=False)
    exit_on_term()      # SIGTERM でも、起動したセッションを子孫ごと止めてから終わる(既定の SIGTERM は後始末を走らせない)
    # 同じ作業ツリーを compose/release/当番と同時に触らない(監査指摘)。当番の再実行が長引くと
    # ここで待つ。空かなければこの回は諦める(次の timer で拾う)
    try:
        _lock = job_lock("collect", wait_min=30)
    except JobLockTimeout as e:
        notify("collect", str(e), ok=False)
        return 1
    t0 = time.time()
    date = args.date or edition_date()
    branch = f"edition/{date}"

    if not args.no_git and not checkout_edition_branch(date, "collect"):
        return 1
    # 例外・SIGTERM で途中で終わっても、保存した素材と既読状態を検めて確定し、作業ツリーを clean にして終える(settle_interrupted)
    prev = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        return run_collect(args, date, branch, t0)
    except BaseException as e:
        if not args.no_git:
            settle_interrupted(date, branch, e)
        raise
    finally:
        signal.signal(signal.SIGTERM, prev)
        _TERM.update(defer=False, got=False)


def run_collect(args, date: str, branch: str, t0: float) -> int:
    """収集の本体(main が号のブランチを checkout したあと)。"""
    # Grok は SuperGrok の週次上限を消費するため、収集のたびに回すと枠を使い切る
    # (実測: 1回の収集で10セッション。日5回だと週350セッションになり上限の数倍)。
    # 定点観測と Claude 探索は安価なので毎回回し、Grok の実行時刻だけを GROK_HOURS で絞る。
    skip_grok = args.skip_grok or (not args.force_grok and not grok_scheduled_now())
    if skip_grok and not args.skip_grok:
        print(f"Grok は今回スキップ(GROK_HOURS={GROK_HOURS or '毎回'} の対象時刻ではない)", flush=True)

    watch_cands, watch_info = ([], {"skipped": True}) if args.skip_watch else run_watch(claude_exec, args.oncall_rerun)
    explore_items, per_query = run_explores(args.skip_explore, skip_grok)
    cands = normalize(watch_cands + explore_items)
    vcounts = verify(cands)
    added = merge_into_day_file(cands, date)
    # candidates が保存できてから、定点観測の既読を確定する(同じ成功境界。監査指摘)
    watch_state = (watch_info.get("stats") or {}).pop("_state", None) if isinstance(watch_info, dict) else None
    if watch_state is not None:
        # 一度に差し替える(直接の上書きが途中で止まると、切れた JSON が残り次の収集が既読を読めない。監査指摘 2026-10-14)
        write_atomic(STATE_PATH, json.dumps(watch_state, ensure_ascii=False, indent=1) + "\n")
    dur = int(time.time() - t0)
    append_metric("collect", {"edition": date, **run_record(), "watch": watch_info, "per_query": per_query,
                              "normalized": len(cands), "added": added, "verify": vcounts,
                              "duration_s": dur})
    RUN["recorded"] = True
    summary = f"{date}号向け: 新規{added}件(正規化{len(cands)}・{vcounts}) {dur}秒"
    print(summary, flush=True)
    # 判定表に無い出典は「未確認」になる。放っておくと未確認の記事が増えるだけなので、
    # **何を足せばよいか**をここに出す。表は人が育てるものであり、
    # 既定を強い種別にして誤魔化さない(監査指摘)
    unknown = collections.Counter(
        urllib.parse.urlparse(c["url"]).netloc.lower().removeprefix("www.")
        for c in cands if c.get("source_type") == "未確認" and c.get("url"))
    if unknown:
        print(f"source_types.yml に無い出典 {sum(unknown.values())}件 / {len(unknown)}ドメイン: "
              + ", ".join(f"{h}({n})" for h, n in unknown.most_common(12)), flush=True)
    if needs_classify(cands) and args.oncall_rerun:
        # 当番の取り直しでは判定表の取引(合議。単独で最大30分)をしない。取り直しは次の定時工程の前に終えて、
        # 素材を commit するのが先(監査指摘)。未知の出典の判定は次の定時収集で行う
        print("当番の取り直しのため、未知の出典の判定は次の定時収集で行う", flush=True)
    elif needs_classify(cands) and deadline_passed():
        # 収集の締切を過ぎたら合議を始めない(組版の開始を妨げない)。素材の commit を先にし、判定は次の定時収集で行う
        print("収集の締切を過ぎたので、未知の出典の判定は次の定時収集で行う", flush=True)
    elif needs_classify(cands):
        # **未知のドメインは合議で振り分ける。**表を人が育てるまで待つと、
        # 会場・チケット販売・自治体が未確認のまま紙面に載る。
        # 別ベンダーの2モデルが一致したものだけを足し、公式・準公式は自動で足さない
        # (過大表示はこの製品がいちばん避けたい事故なので、機械には名乗らせない)
        if not args.no_git:
            # 判定表の更新 → 紙面の種別付け直し → lint を**1つの取引**にする。どれかが失敗したら、
            # この取引が触った判定表と記事を開始時に戻し、commit しない(表だけ進んで次の lint が赤くなる、
            # 部分的な付け直しが混ざる、を防ぐ。監査指摘)
            # 合議も収集の締切までに終える(越えたら取引の失敗として戻す)
            ok, why = classify_retag_lint(date, timeout=max(1, int(session_left(1800))))
            if not ok:
                if "戻せない" in why:
                    notify("collect", f"{date}: 判定表の取引に失敗({why[:400]})。commit せずに終える。作業ツリーを確かめること", ok=False)
                    return 1
                if why.startswith(CLI_STOPPED):
                    print(f"{date}: 出典の判定は次回の収集でやり直す({why[:300]})", flush=True)   # 投稿は判定が済ませた
                else:
                    notify("collect", f"{date}: 判定表の更新〜紙面の付け直し〜lint で失敗({why[:400]})。判定表と記事を"
                                      f"戻した(候補は残す)。次回の収集で再試行する", ok=False)
    if not args.no_git:
        _TERM["defer"] = True     # 確定の途中では SIGTERM で中断しない(受けたら確定のあとで終える)
        pushed = commit_and_push(branch, f"collect {now_jst().strftime('%H:%M')}: +{added}件", "collect")
        _TERM["defer"] = False    # 確定のあと(通知など)の SIGTERM は、すぐ受けて終える(飲み込まない。監査指摘)
        if _TERM["got"]:
            raise Terminated()
        if not pushed:
            # 永続化できなかった収集は「無かった」のと同じ。成功として終わらない(監査指摘: fail-open)
            notify("collect", f"{date}: 候補を commit/push できなかった。作業ツリーを確かめること", ok=False)
            return 1
    # 新規0件の警報は「定時実行が空振りした」ことを知らせるためのもの。
    # 収集系統を手で止めた実行(--skip-*)では0件が当たり前なので鳴らさない
    # (鳴らすと本物の空振りと区別がつかず、警報として役に立たなくなる)
    skipped = [n for n, on in (("watch", args.skip_watch), ("explore", args.skip_explore),
                               ("grok", args.skip_grok)) if on]
    if added == 0 and not skipped:
        # 0件そのものは異常ではない(全系統が正常に動き、拾ったものが既知だった回もある。当番の指摘 3e0904d4fe:
        # 2026-10-05 18:33 は全9系統が取得に成功し、探索の結果も既存の候補だけだったのに異常として当番まで起動した)。
        # 取得に失敗した系統があるときだけ、その内訳を添えて異常にする
        if not report_zero_new(summary, watch_info, per_query):
            print(f"新規0件(全系統の取得は正常。拾ったもの {len(cands)}件はすべて既知だった)", flush=True)
    elif added == 0:
        print(f"新規0件({'/'.join(skipped)} を手動でスキップ中のため通知しない)", flush=True)
    # 当番の拾い直しで、読めないバッチが残った(=諦めずに繰り越した)場合は、直しが効いていない。
    # 読めた分と繰り越しは保存済み(既読にしていない)。成功として終えず、当番へ非0で返す(監査指摘)。
    # 本文が 503 で読めなかった分は当番の仕事の外なので数えない(未処理の列に残り、次の定時収集が読み直す)
    if args.oncall_rerun and facts_left(watch_info, whywhy=True) > 0:
        notify("collect", f"{date}: 当番の拾い直しでも facts 化できないバッチが {facts_left(watch_info, whywhy=True)}件 残った。"
                          f"繰り越しは既読にせず保持した。直しが効いていない(原因が未確定の可能性)", ok=False)
        return 1
    if args.oncall_rerun and isinstance(watch_info, dict) and watch_info.get("blocked"):
        # 取り直しの途中でモデルの CLI が止まった(投稿は済み)。直しが効いていないのではないが、新着はまだ素材に無い
        print(f"{date}: 取り直しの {watch_info['blocked']}件は CLI の停止で読めず、未処理の列に残した", flush=True)
        return CLI_BLOCKED_EXIT
    # 当番の取り直しで、観測先の一覧がまた取れなかったら成功にしない(一覧から新着を見つけられないので deferred は0のまま。
    # 落とした新着がその号の素材に入っていない。監査指摘)
    list_failed = unreadable_for_oncall(watch_info)
    if args.oncall_rerun and list_failed:
        notify("collect", f"{date}: 当番の取り直しでも観測先の一覧が取れない: " + ", ".join(list_failed)
               + "。そこで落とした新着は、この号の素材に入っていない", ok=False)
        return 1
    # 当番の取り直しでも、一覧の読んでいない区間が残ったら成功にしない(残った区間の新着はこの号の素材に入っていない)
    unfinished = sorted(sid for sid, st in ((watch_info or {}).get("stats") or {}).items()
                        if isinstance(st, dict) and st.get("unfinished")) if isinstance(watch_info, dict) else []
    if args.oncall_rerun and unfinished:
        notify("collect", f"{date}: 当番の取り直しでも観測先の一覧を読み終えられない: " + ", ".join(unfinished)
               + "。読んだ位置は残した。残った区間の新着は、この号の素材に入っていない", ok=False)
        return 1
    return 0


class Terminated(BaseException):
    """SIGTERM を受けた(systemd の打ち切り・停止)。main の後始末で素材を確定してから終える。"""


# SIGTERM の扱い。確定(commit・push)の途中では中断しない(git を途中で落とすと index.lock が残り、次の工程の git が止まる)
_TERM = {"defer": False, "got": False}


def _on_sigterm(signum, frame) -> None:
    if _TERM["defer"]:
        _TERM["got"] = True
        return
    _TERM["defer"] = True      # 後始末の間にもう一度受けても中断しない
    # 起動したセッションを子孫ごと止め、以後の起動を断ってから抜ける(exit_on_term と同じ)。止めずに抜けると、待っている
    # 定点観測のバッチが CLI の終わりまで返らず、後始末の確定のあとにも素材を書く(2026-10-21 組 P と収集の後始末の取り込み)
    stop_groups()
    raise Terminated()


def settle_interrupted(date: str, branch: str, e: BaseException) -> None:
    """例外・SIGTERM で途中で終わった収集の後始末(2026-10-14 監査指摘: 素材と既読状態を保存してから記録・出典の判定・commit へ
    進むので、その途中で落ちると保存した変更が未コミットのまま残り、次の工程が作業ツリーの汚れで止まった)。
    **開始時へ戻さずに、検めた成果を確定する。**素材は書式で検めて一度に差し替えて書き、既読状態は素材を保存できてから書く
    (同じ成功境界)ので、壊れていなければそのまま正しい。戻すと、この回の探索・X 調査の素材(X 調査は週の検索枠を使い、
    回す時刻も限られる)を捨てて紙面から落とす。壊れた出力だけは確定せずに HEAD の中身へ戻す(settle_collect_outputs)。
    確定できなければ、作業ツリーに残ったものを名指しする(当番のなぜなぜへ)。"""
    _TERM["defer"] = True
    why = "SIGTERM で打ち切られた" if isinstance(e, Terminated) else f"例外 {type(e).__name__}"
    if not RUN.get("recorded"):
        # 実行記録を残す前に終わった回は、終わり方を記録に積んで素材と一緒に確定する(組版が定時の回ごとに名指しする)
        try:
            append_metric("collect", {"edition": date, **run_record(), "interrupted": why})
        except Exception as e3:      # noqa: BLE001
            print(f"途中で終わった実行の記録を積めなかった: {type(e3).__name__}: {e3}", flush=True)
    try:
        dropped = settle_collect_outputs(ROOT)
        ok = commit_and_push(branch, f"collect {now_jst().strftime('%H:%M')}: 途中で終わった収集の素材を確定({why})",
                             "collect", paths=list(COLLECT_OUTPUTS))
        left = git("status", "--porcelain", check=False).stdout.strip()
    except Exception as e2:      # noqa: BLE001
        dropped, ok, left = [], False, f"{type(e2).__name__}: {e2}"
    msg = (f"{date}: 収集が途中で終わった({why})。保存できた素材と既読状態を検めて" + ("確定した" if ok else "確定できなかった")
           + (f"。壊れていたので確定せずに戻したもの: {' / '.join(dropped)}" if dropped else "")
           + (f"。作業ツリーに残っているもの(次の工程が止まる): {left[:300]}" if left else ""))
    if isinstance(e, Terminated) or dropped or not ok or left:
        notify("collect", msg, ok=False)
    else:
        print(msg, flush=True)


if __name__ == "__main__":
    try:
        code = main()
    except Terminated:
        code = 143      # SIGTERM で打ち切られた(素材は main の後始末で確定した。当番のなぜなぜへ渡す)
    except Exception as e:
        notify_crash("collect", e)
        code = 1
    hand_to_oncall(code)
    sys.exit(code)
