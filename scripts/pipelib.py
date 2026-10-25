"""パイプライン共通ユーティリティ(collect/compose/release/watch)。標準ライブラリのみ。"""
import atexit
import contextlib
import datetime
import html as html_lib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
JST = ZoneInfo("Asia/Tokyo")
BRANDS = ["general", "765", "cg", "million", "shiny", "sidem", "gaku", "dsva", "joint", "other"]


def now_jst() -> datetime.datetime:
    return datetime.datetime.now(JST)


def edition_date(now: datetime.datetime | None = None) -> str:
    """次の 06:00 発行に対応する発行日(= 収集・生成の対象号)。"""
    now = now or now_jst()
    d = now.date() if now.hour < 6 else now.date() + datetime.timedelta(days=1)
    return d.isoformat()


def x_status_time(url: str) -> datetime.datetime | None:
    """x.com/twitter.com の投稿の status ID(Snowflake)から投稿日時を日本時間で復元する。X の投稿でなければ None。"""
    m = re.match(r"https?://(?:[\w-]+\.)?(?:x|twitter)\.com/[^?#\s]*?/status(?:es)?/(\d{15,20})", str(url or ""))
    if not m:
        return None
    ms = (int(m.group(1)) >> 22) + 1288834974657  # Twitter epoch
    return datetime.datetime.fromtimestamp(ms / 1000, tz=JST)


def align_x_published_date(c: dict) -> bool:
    """X の投稿の素材(候補・続報予約)の published_date を、投稿 ID の日本時間の日付に揃える。変えたら True。
    収集の正規化・候補ファイルへの書き込み(保存済みの候補を含む)・組版の素材の読み込みが同じこれを使う(冪等)。
    2026-10-21号: Grok が投稿日時を GMT で書き、10/21 0:00 JST の公式の投稿3件が published_date 10/20 の素材になって、
    出典 label の日付が 10/20 で書かれた(校閲 R8 が2巡ブロック)。"""
    t = x_status_time(c.get("url")) if isinstance(c, dict) else None
    if not t or c.get("published_date") == t.date().isoformat():
        return False
    c["published_date"] = t.date().isoformat()
    return True


def candidate_misplaced(c: dict) -> bool:
    """写しの照合で落ちた候補(URL の取り違え)。事実がこのページのものではない。"""
    return bool(c.get("quotes")) and str(c.get("verify_note") or "").startswith("写しが出典の本文に無い")


def merge_candidate_facts(tgt: dict, c: dict) -> bool:
    """同じページの候補 c の facts を tgt へ増分マージする(収集の再検出と、組版が前の号の未読の事実を引き継ぐときの共通)。
    足したら裏取りの結果も引き継ぎ、弱いほうを採る。締切(deadline)は遅いほうを採る。facts か締切かリンク元か原本の時刻が変わったら True。"""
    # **締切は facts と別に、遅いほうを採る。**同じページに締切が複数ある(受付締切と入金期限)と、候補ごとに deadline が違う。
    # 既存の候補の早い締切のままだと、まとめた素材が翌号への戻しで締切切れとして捨てられる(2026-10-15 監査指摘: 入金期限
    # 10/18 のレトラの予約の素材が、同じ URL の候補の受付締切 10/11 で 10/17 号に戻らない)。facts が増えなくても採る
    dl_changed = False
    dls = [d for d in (str(tgt.get("deadline") or ""), str(c.get("deadline") or "")) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)]
    if dls and max(dls) != tgt.get("deadline"):
        tgt["deadline"], dl_changed = max(dls), True
    # リンク元(そのリンクが載っていた投稿・ページ)も、無ければ引き継ぐ。出典の種別の判定がリンク元を引く(classify_sources)
    if c.get("link_from") and not tgt.get("link_from"):
        tgt["link_from"], dl_changed = c["link_from"], True
    # 収集時点の原本(page_archive)は、新しく読んだほうの取得時刻を指す
    if c.get("archived_at") and str(c["archived_at"]) > str(tgt.get("archived_at") or ""):
        tgt["archived_at"], dl_changed = c["archived_at"], True     # 変わったら保存させる(Sol 監査 r2)
    merged =list(dict.fromkeys((tgt.get("facts") or []) + (c.get("facts") or [])))
    if len(merged) <= len(tgt.get("facts") or []):
        return dl_changed
    tgt["facts"] = merged
    # **裏取りの結果も引き継ぐ。**facts だけ足して verify を据え置くと、
    # 後から来た「出典に無い事実」が confirmed の候補に紛れ込む(監査指摘)。
    # 同じ URL を何度も拾うのは通常経路なので、実際に起きる
    ub = list(dict.fromkeys((tgt.get("unbacked_facts") or []) + (c.get("unbacked_facts") or [])))
    if ub:
        tgt["unbacked_facts"] = ub[:12]
    # **弱いほうを採る。**未一致の粒だけを見ていると、
    # 新しく拾ったときに URL が死んでいて failed でも、
    # 粒が欠けていなければ既存の confirmed が残る(監査指摘)
    order = ["failed", "unconfirmed", "confirmed"]
    cur = "unconfirmed" if ub else tgt.get("verify", "unconfirmed")
    new = c.get("verify", "unconfirmed")
    tgt["verify"] = min([cur, new], key=lambda v: order.index(v) if v in order else 1)
    return True


def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = load_env()
# 計画・組版などに使う Claude モデル。短縮名 "sonnet" は Sonnet 5 を指したままなので、版を明示する(編集長 2026-10-02)
CLAUDE_MODEL = ENV.get("CLAUDE_MODEL", "claude-sonnet-5-5")
# **探索役**(Web 検索でネタを見つける工程)。codex exec -m に渡す。
# codex には WebSearch 専用ツールが無いが、sandbox の通信を開けばシェルから
# 検索も本文取得もできる(実測で確認済み)。
EXPLORE_MODEL = ENV.get("EXPLORE_MODEL", "gpt-6-luna")
# 定点観測(sources.yml の巡回結果を facts 化する)に使う Claude モデル。
# 探索とは別役で、こちらは渡されたページ本文を読むだけなので安いモデルでよい
COLLECT_MODEL = ENV.get("COLLECT_MODEL", "haiku")
# 記事本文の執筆に使う Codex モデル(codex exec -m に渡す)。校閲とベンダーを分離するため執筆側に配置
CODEX_WRITE_MODEL = ENV.get("CODEX_WRITE_MODEL", "gpt-6-luna")
# 社説の執筆に使う Codex モデル。記事とは求めるものが違う(事実の要約ではなく人格と文章)ため
# 別枠にしてある。GPT-6 では terra に当たる枠が sol(編集長 2026-10-02: GPT-5.6-Terra → GPT-6.1-Sol)
EDITORIAL_MODEL = ENV.get("EDITORIAL_MODEL", "gpt-6.1-sol")
# 校閲・機械検収エラーの修正に使う Claude モデル(claude -p --model に渡す)。
# 執筆(Codex)と別ベンダーにするため Claude 側。既定は haiku(検品はコスト重視)
REVIEW_MODEL = ENV.get("REVIEW_MODEL", "haiku")


# --- 使い捨ての作業場所(/tmp に置かない) ---------------------------------------------------
# /tmp は 3.8GB の tmpfs。当番の作業ツリー・監査の使い捨ての作業ツリー・Sol が監査中に作る一時ファイル・テストの一時ディレクトリ・
# 描画の Chrome の一時ディレクトリが溜まって 100% になり、当番の作業ツリー作成・描画・回帰テストが全部止まった(2026-10-21)。
# 本番の工程が作る一時のものは /home 側(既定 ~/.cache/imas-news。IMAS_SCRATCH で変えられる)に置き、片付けに失敗して残ったものは
# 次に作業場所を作るとき古い順に消す(sweep_scratch)。子プロセス(モデルの CLI・テスト・Chrome)の TMPDIR も同じ置き場の tmp/ へ向ける
SCRATCH_MAX_AGE_SEC = 24 * 3600     # これより古い残り物は片付け損ねたもの(工程は最長でも数時間)


def scratch_root() -> Path:
    """使い捨ての作業場所の置き場(呼ぶたびに環境から決める。テストは IMAS_SCRATCH で一時ディレクトリの中へ向ける)。"""
    p = Path(os.environ.get("IMAS_SCRATCH") or ENV.get("IMAS_SCRATCH") or (Path.home() / ".cache" / "imas-news"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _under_tmp(p: str) -> bool:
    try:
        return not p or Path(p).resolve().is_relative_to("/tmp")
    except (OSError, ValueError):
        return True


def use_scratch_tmpdir() -> None:
    """この process と子プロセスの一時ファイルの置き場(TMPDIR)を、/tmp でなければそのまま、/tmp(か未設定)なら置き場の tmp/ にする。"""
    import tempfile
    if not _under_tmp(os.environ.get("TMPDIR", "")):
        return
    t = scratch_root() / "tmp"
    t.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(t)
    tempfile.tempdir = str(t)


def sweep_scratch(max_age: float = SCRATCH_MAX_AGE_SEC) -> list[str]:
    """置き場(と TMPDIR の置き場)に残った、max_age より古いものを消す。戻りは消したもの。消せないものは飛ばす(次の回にまた試す)。"""
    gone = []
    root = scratch_root()
    for base in (root, root / "tmp"):
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.name == "tmp" and base == root:
                continue
            try:
                if time.time() - e.lstat().st_mtime < max_age:
                    continue
                if e.is_dir() and not e.is_symlink():
                    shutil.rmtree(e, ignore_errors=True)
                else:
                    e.unlink()
            except OSError:
                continue
            if not e.exists():
                gone.append(str(e))
    return gone


def scratch_dir(prefix: str) -> Path:
    """使い捨ての作業ディレクトリを置き場の下に作る(作る前に古い残り物を片付ける)。片付けは呼び側の finally。"""
    import tempfile
    sweep_scratch()
    return Path(tempfile.mkdtemp(prefix=prefix, dir=scratch_root()))


use_scratch_tmpdir()

# **社説は 2026-09-06 号までで終了。**面白く作れなかったため断念(2026-09-06 決定)。
# 過去号の社説は append-only でそのまま残す。日付で切るのは、既存号の lint と
# 表示を壊さずに、翌号から書かない・求めない・校閲しないを一度に切り替えるため。
EDITORIAL_UNTIL = "2026-09-06"


def has_editorial(date: str) -> bool:
    """その号に社説があるべきか(=書く・lint で求める・校閲する)。"""
    return str(date) <= EDITORIAL_UNTIL


# 書き直しても紙面に戻らない取り下げの規則(校閲の規則番号)。判断が記事の書き方ではなく**話題そのもの**に掛かり、
# いまも成り立つかをコードが確かめられるものだけ:
#   P1 = 同じ知らせの記事がもう1本紙面にある(相手が紙面に残っているあいだだけ成り立つ)
# R10(既報と同じ内容)は入れない。校閲は稿を見て判断するので、同じ素材でも期日の知らせとして書き直せば載りうる
# (実測 2026-10-09: 永吉昴のガシャ記事は「本文に 10/10 14:59 の終了が無い」ことを含めて R10。監査指摘)
SETTLED_DROP_RULES = ("P1",)


def settled_drops(date: str, root: Path | None = None) -> dict[str, str]:
    """その号で紙面から外れた記事(metrics/drops-<日付>.json)のうち、外した理由がいまも成り立つものの {slug: 理由}。
    組み直し(--reuse-plan)はこれを書き直さず、救済(--recover)の戻す対象にも、次の号の選定に戻す素材にもしない
    (実測 2026-10-12: 重複で取り下げた上映会記事を救済の組み直しが書き直し、承認済みの記事と同じ重複がまた出た)。
    記録が無い・読めないときは空(全部を書き直す対象のまま。取り下げの理由が分からない記事を戻さない側に倒さない)。"""
    root = root or ROOT
    p = root / "metrics" / f"drops-{date}.json"
    try:
        rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}
    posts = root / "docs" / "_posts"
    out = {}
    for slug, r in (rows if isinstance(rows, dict) else {}).items():
        s = r.get("settled") if isinstance(r, dict) else None
        if not isinstance(s, dict) or (posts / f"{date}-{slug}.md").exists():
            continue
        if s.get("rule") == "P1":
            alive = [d for d in s.get("dup_of") or [] if isinstance(d, str) and d != slug and (posts / f"{date}-{d}.md").exists()]
            if alive:
                out[slug] = f"P1 同じ知らせの記事 {alive[0]} が紙面にある"
    return out


# --- 当番(oncall)への引き継ぎ -------------------------------------------------
# 工程が「人間判断が必要」で止まるとき、人へ投げる前に scripts/oncall.py を起動する。
# 当番(Opus)が診断・修正し、監査(Sol)の敵対的レビューを通ったものだけを取り込んで再実行する。
# 呼び出し側は今までどおり通知して終わってよい(当番は別プロセスで動く)。
# ONCALL=off で止められる。当番自身が再実行する工程には ONCALL=off が付く(無限ループ防止)
def tool_path() -> str:
    """claude / codex が見つかる PATH。systemd の service(release 等)の PATH には
    ~/.local/bin が無く、そこから起動した当番が `FileNotFoundError: 'claude'` で落ちた
    (実測 2026-09-17 06:14)。呼び出し元の PATH に、利用者の標準の bin を足す。"""
    import os
    home = os.path.expanduser("~")
    extra = [f"{home}/.local/bin", f"{home}/.npm-global/bin", f"{home}/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    cur = [p for p in os.environ.get("PATH", "").split(":") if p]
    return ":".join(dict.fromkeys(cur + extra))


# この process が人に「異常」として通知した内容の台帳(notify(ok=False) と anomaly() が積む)。
# **人に申告する前に、当番がなぜなぜ(原因まで遡る診断)をする**(編集長 2026-09-26「すべてのエラーがなぜ起きたかなぜなぜしろ」
# 「申告前に診断しろ」)。工程の終わりに diagnose_anomalies が当番へ渡し、escalate は毎回この台帳を依頼文に付ける
ANOMALIES: list[str] = []
_ESCALATED = False
# 台帳の1件(文面)→ 形の鍵。同じ号・同じ工程で当番が診断し監査が承認した事柄(同じ原因の同じ型)を、止まっていない工程の
# 終わりに当番へ渡し直さないための照合に使う。鍵は呼び出し側がコードで形だけ決める(観測先・失敗の種類。本文の細部で別物にしない)。
# 鍵の無い事柄は文面そのもの(空白を詰めた全文)で照合する(2026-10-24: lantis-sidem の 503 を、承認済みの診断のあとも
# 23:30・02:00 の収集が当番に渡し直し、「人の判断が要る」が繰り返し届いた)
ANOMALY_KEYS: dict[str, str] = {}


def anomaly(job: str, text: str) -> None:
    """人に「異常」として見せる事柄を台帳に積む(通知の成否とは独立)。空文字は積まない。"""
    if text and text.strip():
        ANOMALIES.append(f"[{job}] {text.strip()}")


# 取得の失敗のうち、なぜなぜ(当番の起動)の対象にしない HTTP の状態コード。503 は相手のサイトの一時的な停止(メンテナンスの画面か
# 否かを問わない)で、こちらに直す箇所は無い。人への通知と「既読を進めず次回読み直す」は変えない。号・工程をまたいで常に対象外
# (編集長 2026-10-25「503はなぜなぜの対象外にしろ」。10/25 07:32・12:31 の収集が号の変わった lantis-sidem の 503 で当番を起こし、
# 12:31 は収集のまとめの文が text: の鍵で渡って、当番が30分の時間切れで止まった)
WHYWHY_EXEMPT_HTTP = frozenset({503})


def http_status(e: BaseException) -> int | None:
    """例外が HTTP の応答(urllib の HTTPError)なら、その状態コード。それ以外(時間切れ・描画の失敗 等)は None。"""
    import urllib.error
    code = getattr(e, "code", None) if isinstance(e, urllib.error.HTTPError) else None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def whywhy_exempt_status(code) -> bool:
    """HTTP の状態コード(http_status)が、なぜなぜの対象外(WHYWHY_EXEMPT_HTTP)か。None・数でないものは対象(当番を起こす側に倒す)。"""
    return isinstance(code, int) and not isinstance(code, bool) and code in WHYWHY_EXEMPT_HTTP


def anomaly_shape(job: str, text: str, key: str) -> None:
    """これから積む(notify(ok=False) / anomaly で)事柄の文面に、形の鍵を付ける。key は観測先・失敗の種類などをコードで決めた文字列。"""
    if text and text.strip() and key:
        ANOMALY_KEYS[f"[{job}] {text.strip()}"] = f"{job}:{key}"


def anomaly_key(entry: str) -> str:
    """台帳の1件の形の鍵。積んだときに鍵を付けていなければ、文面の全文(空白を詰めたもの)の指紋。"""
    import hashlib
    return ANOMALY_KEYS.get(entry) or "text:" + hashlib.sha256(" ".join(entry.split()).encode("utf-8")).hexdigest()[:16]


def settled_anomaly_keys(date: str, stage: str) -> set[str]:
    """同じ号・同じ工程で、当番が診断し監査が承認した(コードの欠陥ではない = no_fix_needed)事柄の鍵。
    当番の試行の記録(metrics/oncall-<date>-<stage>.json の log の settled_keys)から読む。読めなければ空(当番を起こす側に倒す)。
    直した(fixed)事柄は数えない: 直したあとにまた起きたなら、直し方が足りないという新しい事実である。"""
    try:
        return settled_keys_of(json.loads((ROOT / "metrics" / f"oncall-{date}-{stage}.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def settled_keys_of(state) -> set[str]:
    """当番の試行の記録(読み込んだ JSON)から、承認された no_fix_needed の回が残した鍵。形が違えば空。"""
    log = state.get("log") if isinstance(state, dict) else None
    if not isinstance(log, list):
        return set()
    return {k for e in log if isinstance(e, dict) and e.get("approved") is True and e.get("status") == "no_fix_needed"
            and isinstance(e.get("settled_keys"), list) for k in e["settled_keys"] if isinstance(k, str)}


# --- 定時工程の時刻(当番が工程の排他を手放すべき時刻の計算) ------------------------------------------
# 定時工程の開始(時, 分)。01:30 道具の更新 / 02:00 収集 / 03:00 組版 / 06:00 発行 / 07:30・12:30・18:30・23:30 収集
SCHEDULED = ((1, 30), (2, 0), (3, 0), (6, 0), (7, 30), (12, 30), (18, 30), (23, 30))
SLOT_MARGIN_MIN = 15             # 次の定時工程の何分前に工程の排他を手放すか
# 組版は、収集の当番(落とした新着の取り直し)が動いていれば、開始をこの分だけ待つ。組版の締切は 06:00 の8分前なので、
# 03:40 に始めても実測の所要(40〜60分)と当番の立て直しの余裕は残る。その間に取り直して、落とした新着をその号の選定に間に合わせる
COMPOSE_WAITS_FOR_COLLECT_ONCALL_MIN = 40
COLLECT_ONCALL_MARK = ROOT / "metrics" / "oncall-collect-running.json"   # 収集の当番が動いている印({end_at, date, handover_at, held})
# 収集が当番を呼んでから、当番が工程の排他を取るまでの受け渡しの猶予。この間は組版が排他を取らずに譲る
# (収集が終わって排他が空いた瞬間に組版が取ると、当番は取り直せないまま組版が始まる。監査指摘)
COLLECT_HANDOVER_SEC = 180


def next_slot_end(now: datetime.datetime, compose_grace_min: int = 0) -> float:
    """次の定時工程の SLOT_MARGIN_MIN 分前(epoch 秒)。当番はこの時刻までに工程の排他を手放す。
    compose_grace_min は、03:00 の組版をその分だけ遅く数える(組版が収集の当番を待つ分)。"""
    cands = []
    for d in (0, 1):
        for h, m in SCHEDULED:
            t = (now + datetime.timedelta(days=d)).replace(hour=h, minute=m, second=0, microsecond=0)
            if (h, m) == (3, 0):
                t += datetime.timedelta(minutes=compose_grace_min)
            if t > now:
                cands.append(t)
    return (min(cands) - datetime.timedelta(minutes=SLOT_MARGIN_MIN)).timestamp()


def collect_oncall_end(now: datetime.datetime) -> float:
    """収集が当番を呼ぶときに固定する、当番の終える時刻(取り直しまで含む)。呼び出し時に決めて引数で渡すので、
    当番が起動し直しても延びない(監査指摘)。02:00 の収集なら、組版が待つ分を見込む: 組版は「この時刻+5分」まで
    (上限 COMPOSE_WAITS_FOR_COLLECT_ONCALL_MIN)待つので、終える時刻は 03:00 + 上限 - 5分(=03:35)。"""
    return next_slot_end(now, COMPOSE_WAITS_FOR_COLLECT_ONCALL_MIN + SLOT_MARGIN_MIN - 5)


def compose_lock_wait_min(now_ts: float | None = None) -> int:
    """組版が工程の排他を待つ分。収集の当番が動いている印があり、その終える時刻が先なら、それまで(+5分、上限
    COMPOSE_WAITS_FOR_COLLECT_ONCALL_MIN)待つ。それ以外は10分。"""
    now_ts = time.time() if now_ts is None else now_ts
    try:
        end_at = float(json.loads(COLLECT_ONCALL_MARK.read_text(encoding="utf-8"))["end_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return 10
    if end_at <= now_ts:
        return 10
    return max(10, min(COMPOSE_WAITS_FOR_COLLECT_ONCALL_MIN, int((end_at - now_ts) // 60) + 5))


def mark_collect_oncall(end_at: float, date: str, held: bool = False) -> None:
    """収集の当番が動いている印を置く。収集は当番を呼べたら(排他を持ったまま)置き、当番は排他を取ったら held にする。"""
    import os
    COLLECT_ONCALL_MARK.parent.mkdir(parents=True, exist_ok=True)
    # 置き換えは一度に(書きかけの空の印を組版が読むと「受け渡し中でない」と見て排他を取る)
    tmp = COLLECT_ONCALL_MARK.with_name(f".{COLLECT_ONCALL_MARK.name}.{os.getpid()}")
    tmp.write_text(json.dumps({"end_at": end_at, "date": date, "handover_at": time.time(), "held": held}), encoding="utf-8")
    os.replace(tmp, COLLECT_ONCALL_MARK)


def collect_oncall_handover_pending(now_ts: float | None = None) -> bool:
    """収集が当番を呼び、当番がまだ工程の排他を取っていない(受け渡しの最中)か。組版はこの間、空いていても排他を取らない。
    当番が起動できずに死んだときも組版を止め続けないよう、COLLECT_HANDOVER_SEC で打ち切る。"""
    now_ts = time.time() if now_ts is None else now_ts
    try:
        m = json.loads(COLLECT_ONCALL_MARK.read_text(encoding="utf-8"))
        return (not m.get("held") and float(m["end_at"]) > now_ts
                and now_ts - float(m["handover_at"]) < COLLECT_HANDOVER_SEC)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def diagnose_anomalies(stage: str, date: str, rerun: bool = False, reason: str = "", edition: str = "",
                       extra_args: list[str] | None = None) -> bool:
    """工程の終わりに、台帳の異常を当番へ渡す(この process でまだ当番を呼んでいなければ)。
    既定は「工程は終わった(止まっていない)」ので再実行しない。止まったときは reason と rerun=True で呼ぶ。
    date は異常の発生日(当番はその日の journal を読む)。修正の取り込み先の号がそれと違うとき(発行後の release・watch)は
    edition に生きている号の日付を渡す。戻り値は当番を呼んだか。"""
    if not ANOMALIES or _ESCALATED:
        return False
    items = None
    if not reason:
        # 工程は止まっていない: 同じ号・同じ工程で当番が診断し監査が承認した事柄(同じ形の鍵)は、判定し直させない。
        # 新しい事柄だけを渡す。全部が判定済みなら当番を起こさない(人への通知は見つけたときに済んでいる)
        settled = settled_anomaly_keys(date, stage)
        items = [a for a in ANOMALIES if anomaly_key(a) not in settled]
        if not items:
            print(f"当番を起こさない({stage} {date}): 異常は全部、この号・この工程で当番が診断し監査が承認した事柄と同じ型"
                  f"({len(ANOMALIES)}件)", flush=True)
            return False
    # reason が無い = 工程は止まっていない。当番に伝え、「いま再実行しても変わらない」を「人の判断が要る」にさせない(oncall --finished)
    return escalate(stage, date, reason or "工程は終わった(止まっていない)が、人に異常として通知した事柄がある。下の一覧をなぜなぜすること",
                    rerun=rerun, edition=edition, extra_args=list(extra_args or []) + ([] if reason else ["--finished"]), items=items)


ESCALATE_ITEMS_MAX = 30      # 当番に渡す台帳の件数の上限(引数1つの上限 128KB に収める: 30件 × 1200 字まで)
# 当番が「人に渡し終えた」で終わるときの終了値(止まった工程で試行の枠を使い切った・承認された「再実行しても通らない」・
# 時間切れ)。起動し直しても同じ判断と同じ通知を繰り返すだけなので、systemd に起動し直させない(RestartPreventExitStatus)。
# 異常終了のままにして、unit の失敗としては見えるようにする(2026-10-24 02:29: 枠切れの通知が起動し直しで重なった)
ONCALL_HANDED_EXIT = 79


def escalate_reason(reason: str, items: list[str] | None = None) -> str:
    """当番に渡す理由 = 呼び出し側の理由 + 異常の台帳(なぜなぜの依頼文 prompts/oncall-whywhy)。台帳が空なら理由だけ。
    items を渡したら、台帳の代わりにその事柄だけ(判定済みを除いた残り)。"""
    items = ANOMALIES if items is None else items
    if not items:
        return reason[:4000]
    return reason[:4000] + "\n\n" + render_prompt("oncall-whywhy", ITEMS="\n".join(f"- {a[:1200]}" for a in items[:ESCALATE_ITEMS_MAX]))


def escalate(stage: str, date: str, reason: str, rerun: bool = True, edition: str = "",
             extra_args: list[str] | None = None, ledger: bool = True, restart: bool = True,
             items: list[str] | None = None) -> bool:
    """当番(oncall)を起動する。依頼の理由に、この process の異常の台帳(なぜなぜの対象)を必ず付ける。
    rerun=False は「止まった工程を再実行しない」(号が確定している・工程が終わっている)。
    edition は修正を取り込む号の日付(既定は date。発行後の工程では生きている次の号を渡す)。
    ledger=False は台帳を付けない(保管していた指摘を直す依頼 backlog は、その指摘だけを渡す)。extra_args は当番への追加の引数。
    restart=False は当番が失敗しても起動し直さない(backlog: やり直しは翌朝。起動し直すと次の定時工程の排他を奪う)。
    items は台帳の代わりに渡す事柄(判定済みを除いた残り。diagnose_anomalies)。渡した事柄の形の鍵は --anomaly-keys で当番に渡し、
    当番が診断し監査が承認したら試行の記録に残す(次から同じ型を判定し直さない)。"""
    import os
    global _ESCALATED
    if _QUIET:                 # 試験実行(通知しない)では当番も呼ばない
        print(f"[試験実行・当番を呼ばない] {stage} {date}: {reason[:200]}", flush=True)
        return False
    if ENV.get("ONCALL", "on").lower() == "off" or os.environ.get("ONCALL", "").lower() == "off":
        return False
    if oncall_clis_blocked():
        # 当番も同じ CLI で同じ理由で止まり、1日2回の枠を使うだけになる(2026-10-20 18:34・18:35 の当番が2回とも認証切れで即死)。
        # 人への投稿は見つけたときに済んでいる(report_cli_blocked)
        print(f"当番を呼ばない({stage} {date}): {', '.join(oncall_clis_blocked())} が認証切れ・利用上限で答えない", flush=True)
        return False
    script = ROOT / "scripts" / "oncall.py"
    if not script.exists():
        return False
    # _ESCALATED は**起動を確かめてから**立てる(起動に失敗したのに立てると、工程末尾の diagnose が二度目を抑止して、
    # 当番が誰も呼ばれないまま終わる。監査指摘 r87)
    log = ROOT / "metrics" / f"oncall-{date}-{stage}.log"
    handed = (ANOMALIES if items is None else items)[:ESCALATE_ITEMS_MAX] if ledger else []
    keys = list(dict.fromkeys(anomaly_key(a) for a in handed))
    args = ([sys.executable, str(script), "--stage", stage, "--date", date,
             "--reason", escalate_reason(reason, items) if ledger else reason[:100_000]]
            + (["--no-rerun"] if not rerun else []) + (["--edition", edition] if edition and edition != date else [])
            + list(extra_args or []) + (["--anomaly-keys", *keys] if keys else []))
    # **systemd の service から呼ばれたときは、別の transient unit として起動する。**
    # Popen(start_new_session=True) で子を切り離しても cgroup は同じなので、compose の service が
    # 終わった瞬間に systemd が当番ごと殺す(実測 2026-09-13 04:15: 当番のログが空のまま消えた)
    if shutil.which("systemd-run") and os.environ.get("INVOCATION_ID"):
        unit = f"imas-oncall-{date}-{stage}-{int(time.time())}"
        r = subprocess.run(["systemd-run", "--user", "--collect", "--unit", unit,
                            f"--working-directory={ROOT}", f"--setenv=PATH={tool_path()}",
                            f"--setenv=HOME={os.environ.get('HOME', '')}",
                            f"--property=StandardOutput=append:{log}", "--property=StandardError=inherit",
                            # 当番自身が落ちたら1回だけ起動し直す(2回目は attempt の印と flock が二重起動を防ぐ)。
                            # 人に渡し終えた終わり方(ONCALL_HANDED_EXIT)は起動し直さない(同じ通知を繰り返さない)
                            *(["--property=Restart=on-failure", "--property=RestartSec=60",
                               f"--property=RestartPreventExitStatus={ONCALL_HANDED_EXIT}",
                               "--property=StartLimitBurst=2", "--property=StartLimitIntervalSec=3600"] if restart else [])] + args,
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if r.returncode == 0:
            # 起動できたことを確かめる(unit が active か)。確かめずに「起動した」と言わない(監査指摘)
            st = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True)
            if st.stdout.strip() in ("active", "activating"):
                print(f"当番(oncall)を起動した: {stage} {date}(unit {unit})", flush=True)
                # 台帳を渡した起動だけを「この process の異常は当番に渡した」とする(保管していた指摘の依頼で、
                # 同じ process のあとの異常のなぜなぜを止めない)
                _ESCALATED = _ESCALATED or ledger
                return True
            notify("oncall", f"{date} {stage}: 当番の unit {unit} が動いていない({st.stdout.strip()})。人の判断が要る", ok=False)
            return False
        # service の cgroup の中へ Popen で逃がすと、親の service が終わった瞬間に殺される(09-13 と同型)。
        # systemd の下では代替に走らず、起動できなかったと明示して人へ渡す(監査指摘)
        notify("oncall", f"{date} {stage}: 当番を systemd-run で起動できない({r.stderr.strip()[:200]})。人の判断が要る", ok=False)
        return False
    try:
        with log.open("a", encoding="utf-8") as f:
            subprocess.Popen(args, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True, env={**os.environ, "PATH": tool_path()})
        print(f"当番(oncall)を起動した: {stage} {date}", flush=True)
        _ESCALATED = _ESCALATED or ledger
        return True
    except Exception as e:
        print(f"当番の起動に失敗: {e}", flush=True)
        return False


# --- 校閲した中身の指紋 ---------------------------------------------------
# approve は「その時点の中身」に対する判定である。release が「最後の review JSON に
# approve と書いてあるか」しか見ないと、校閲のあとに記事や digest が変わっても古い
# approve で発行できる(監査指摘)。校閲が見る部分だけの指紋を review JSON に残し、
# release は現在の指紋と一致する approve だけを有効にする。
# rank・src・機械算出欄は校閲対象外(校閲後に assign_ranks / derive が書き換える)なので含めない。
def article_hash(path) -> str:
    import hashlib
    import json as _json
    import re as _re
    import yaml as _yaml
    text = Path(path).read_text(encoding="utf-8")
    m = _re.match(r"^---\n(.*?)\n---\n", text, _re.S)
    fm = (_yaml.safe_load(m.group(1)) or {}) if m else {}
    body = text[m.end():] if m else text
    # 公式ポータルのメンテナンスの断り書き(コードが決まった文面で付ける段落)は入れない。後のセッションが見分けたメンテナンスで
    # 承認済みの記事に付け直しても、承認を外さない(2026-10-22 Sol 監査 r2)
    from renderlib import strip_notice
    body = strip_notice(body)
    # 校閲が判断に使う項目と、読者に表示される項目を全部入れる(監査指摘)。
    # event_date は時制・鮮度の判断対象、tags と corrections は記事ページに出る
    seen = {"title": fm.get("title"), "lede": fm.get("lede"), "brand": fm.get("brand"),
            "candidate_ids": fm.get("candidate_ids"), "event_date": str(fm.get("event_date") or ""),
            "tags": fm.get("tags"), "corrected": fm.get("corrected"), "corrections": fm.get("corrections"),
            # 校閲が突き合わせる根拠(見出し・リードの id、執筆が確かめた事実)も指紋に入れる(監査指摘)
            "title_fact_ids": fm.get("title_fact_ids"), "lede_fact_ids": fm.get("lede_fact_ids"),
            "verified_facts": fm.get("verified_facts"),
            # 出典の種別も読者に表示される。校閲後に変えても指紋が一致してはいけない(監査指摘)
            "sources": [[s.get("url"), s.get("label"), s.get("type")] for s in (fm.get("sources") or [])]}
    return hashlib.sha256((_json.dumps(seen, ensure_ascii=False, sort_keys=True, default=str) + "\n" + body)
                          .encode("utf-8")).hexdigest()


def edition_hash(date: str) -> str:
    import hashlib
    import json as _json
    import re as _re
    import yaml as _yaml
    p = ROOT / "docs" / "_editions" / f"{date}.md"
    if not p.exists():
        return ""
    m = _re.match(r"^---\n(.*?)\n---\n", p.read_text(encoding="utf-8"), _re.S)
    fm = (_yaml.safe_load(m.group(1)) or {}) if m else {}
    seen = {k: fm.get(k) for k in ("number", "date", "weekday", "issued_at", "lead_slug", "digest")}
    return hashlib.sha256(_json.dumps(seen, ensure_ascii=False, sort_keys=True, default=str)
                          .encode("utf-8")).hexdigest()


def review_manifest(date: str) -> dict[str, str]:
    """校閲が見る中身の指紋。{相対パス: sha256}。

    号スナップショットは digest・一面・号の識別情報(機械算出欄は除く)。
    既報判定の根拠(組版前の台帳)も入れる。校閲後に根拠が差し替わったら approve は
    その紙面のものではない(監査指摘)。
    """
    import hashlib
    out = {}
    for p in sorted((ROOT / "docs" / "_posts").glob(f"{date}-*.md")):
        out[f"docs/_posts/{p.name}"] = article_hash(p)
    ed = ROOT / "docs" / "_editorials" / f"{date}.md"
    if ed.exists():
        out[f"docs/_editorials/{date}.md"] = article_hash(ed)
    out[f"docs/_editions/{date}.md"] = edition_hash(date)
    before = ROOT / "metrics" / f"stories-before-{date}.yml"
    if before.exists():
        out[f"metrics/stories-before-{date}.yml"] = hashlib.sha256(before.read_bytes()).hexdigest()
    return out


def last_review_record(date: str, root: Path | None = None) -> Path | None:
    """その号の校閲の判定: **いちばん新しい組版の実行**(記録の run)の最後の巡の記録(metrics/review-<日付>-<巡>.json)。無ければ None。
    巡の番号は実行ごとに1から数え直す。実行をまたいで番号の最大を採ると、組み直しの新しい判定より前の実行の後ろの巡を読む
    (監査指摘 2026-10-14: 組み直しが1巡目で承認したのに前の実行の5巡目の承認を読み、戻した記事を「承認後の変更」として発行を止めた。
    逆に新しい実行が承認しなかったときに古い承認へ戻ることもある)。run の無い記録(印を付ける前)はいちばん古い実行とみなす。
    読めない記録はどの実行か分からないので、それを返す(読めないと判定させる。新しい実行の記録を飛ばして古い承認を採らない)。"""
    base = (root or ROOT) / "metrics"
    rows, unreadable = [], []
    for p in base.glob(f"review-{date}-*.json"):
        m = re.fullmatch(re.escape(f"review-{date}-") + r"(\d+)\.json", p.name)
        if not m:
            continue
        try:
            run = json.loads(p.read_text(encoding="utf-8")).get("run")
        except (OSError, ValueError, AttributeError):
            unreadable.append((int(m.group(1)), p))
            continue
        rows.append((run if isinstance(run, str) else "", int(m.group(1)), p))
    if unreadable:
        return max(unreadable)[1]
    return max(rows)[2] if rows else None


# 暴走セッション対策の安全弁(--max-budget-usd)。通常運用なら到達しない額を目安に設定。
# 注: codex exec には同等のコスト上限フラグが無いため、執筆(Codex)側には適用できない
EXPLORE_MAX_BUDGET_USD = ENV.get("EXPLORE_MAX_BUDGET_USD", "1.5")
COMPOSE_ARTICLE_MAX_BUDGET_USD = ENV.get("COMPOSE_ARTICLE_MAX_BUDGET_USD", "3")
COMPOSE_WHOLE_MAX_BUDGET_USD = ENV.get("COMPOSE_WHOLE_MAX_BUDGET_USD", "8")
# 記事執筆・面別選定の同時実行数。1本ずつは短いので、並べたぶんだけ短くなる
# (実測 2026-09-02: 35本を4並列で11分)。上げすぎると API 側で詰まるので、
# 効かなくなったら戻す。.env で調整できる
COMPOSE_WAVE = int(ENV.get("COMPOSE_WAVE", "8"))


# 手元の確認・試験実行で Discord へ本物の警報を飛ばさないための抑止。
# 各スクリプトが `--dry-run` `--no-git` 等を受け取ったときに `set_quiet(True)` を呼ぶ。
# 実測: 発行前の点検で release を --dry-run したところ、号スナップショットがまだ無いのは
# 当然なのに「発行中止」の警報が飛び、本物の発行不良と見分けが付かなくなった(2026-08-31)。
_QUIET = False


def set_quiet(on: bool) -> None:
    global _QUIET
    _QUIET = on


def notify(job: str, msg: str, ok: bool = True, require: bool = False, ledger: bool = True) -> bool:
    """Discord へ通知する。戻り値は**届いたか**。
    require=True は「人に届かなければ先へ進んではいけない」通知(当番の修正報告)で、webhook 未設定・
    試験実行も**届いていない**として False を返す(監査指摘)。通常の通知は未設定なら True(黙認)。
    ledger=False は異常の台帳に積まない(当番に回さない。モデル CLI の認証切れ・利用上限: 当番も同じ CLI で止まる)。"""
    prefix = "✅" if ok else "🚨"
    text = f"{prefix} アイマスNEWS {job}: {msg}"
    if not ok and job != "oncall" and ledger:     # 人に見せる異常は当番のなぜなぜの対象(当番自身の通知は除く)
        anomaly(job, msg)
    if _QUIET:
        print(f"[試験実行・通知しない] {text}", flush=True)
        return not require
    print(text, flush=True)
    url = ENV.get("DISCORD_WEBHOOK_URL")
    if not url:
        return not require
    # Discord は1件 2000 字まで。長い通知は分割して**全部**送る(超えると webhook が落ちて通知ごと届かない。監査指摘)
    chunks = split_chunks(text, DISCORD_LIMIT)
    delivered = True
    for i, c in enumerate(chunks):
        body = (f"({i + 1}/{len(chunks)}) " if len(chunks) > 1 else "") + c
        try:
            req = urllib.request.Request(
                url, data=json.dumps({"content": body}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "ImasNewsBot/1.0"})
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"(Discord 通知失敗: {e})", flush=True)
            delivered = False
    return delivered


DISCORD_LIMIT = 1800


def split_chunks(text: str, limit: int) -> list[str]:
    """Discord の 2000 字上限に合わせて分割する。行単位で詰め、1行が上限を超えるなら文字で割る
    (つないだ結果が元の文字列と一致する)。"""
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur:
        chunks.append(cur)
    return chunks


# 編集長への報告の型(編集長 2026-10-04)。「本当にユーザーにとって分かりやすいと思ってんのか」「何故こういう事が起きたか、どう修正したか、
# 他に当該事象の類似が起きないことは説明できるのか」「コード差分はゴミだが、説明のために長くなる分には致し方ない。短くして情報が0になったらゴミだ」。
# 項目の見出しを立て、各項目は必要な長さで書く。コードの差分は載せず、記録の場所だけを示す。内部の名前は避ける(混入は internal_names で検出)
INTERNAL_NAME = re.compile(r"[A-Za-z0-9_\-/]+\.(py|md|json|jsonl|yml)\b|\b[a-z]+_[a-z_]+\b|\b[A-Z]{2,}_[A-Z_]+\b|\br\d{2,3}\b|\bR\d{1,2}\b")
# 障害・修正の報告の項目
INCIDENT_LABELS = ("起きたこと", "なぜ起きたか", "どう直したか", "同じ型の箇所", "紙面への影響")


def editor_notice(title: str, sections: list[tuple[str, str]], ask: str = "", footer: str = "") -> str:
    """編集長に届く報告を組み立てる。title = 何の報告か(1行)、sections = (見出し, 本文) の並び(本文の長さは制限しない)、
    ask = 判断がほしいこと、footer = 記録の場所・監査・戻し方(1行)。"""
    lines = [title]
    for label, body in sections:
        lines += ["", f"■ {label}", str(body).strip()]
    if ask:
        lines += ["", "■ 判断がほしいこと", ask.strip()]
    if footer:
        lines += ["", footer]
    return "\n".join(lines)


def internal_names(text: str) -> list[str]:
    """報告の本文に混ざった内部の名前(ファイル名・項目名・定数名・監査の番号)。形だけを見る(書き直すのは書き手)。"""
    return sorted({m.group(0) for m in INTERNAL_NAME.finditer(text)})


def notify_long(job: str, text: str, ok: bool = True, limit: int = DISCORD_LIMIT) -> bool:
    """必須の長い通知(当番の修正報告)。notify が分割するので、require=True で送るだけ。"""
    return notify(job, text, ok=ok, require=True)


# --- モデル CLI の認証切れ・利用上限 ---------------------------------------------------------
# CLI(claude・codex・grok)が認証切れ・利用上限で答えなかった回は、新着・素材・記事の失敗ではない。失敗の回数に数えず、
# 読み直しの列に残す。人にすぐ知らせる(ログインし直す・戻る時刻を待つのは人の仕事)が、当番には回さない(当番も同じ CLI で止まる)。
# 実測 2026-10-20 18:30: Claude の OAuth が切れて facts 化が読めなかった1回を新着の「読めなかった1回目」と数え、20:29 の再実行で
# 新着を諦めた。18:34・18:35 の当番は2回とも同じ認証切れで即死し、1日2回の枠を使い切った。
# 文言は各 CLI の実物(claude 2.1.304・codex 0.163.0・grok 1.0.62 の実行ファイルの文字列と ops の生の出力)から採った
CLI_BLOCK_RULES = (
    ("auth", re.compile(
        r"Failed to authenticate|OAuth (?:session|token) (?:expired|revoked)|\bNot logged in\b|Please run /login"
        r"|Invalid API key|access token could not be refreshed|authentication session could not be refreshed"
        r"|log out and sign in again|re-?run `?codex login|\b401 Unauthorized|Auth retry budget exhausted", re.I)),
    ("limit", re.compile(
        r"\bhit (?:your|the) [\w'’ -]{0,40}limit|reached your [\w'’ -]{0,40}limit|usage limit reached|You're out of credits|out of credits\."
        r"|credit balance (?:is )?too low|rate_limit_error|429 Too Many Requests|API Error: 429"
        r"|monthly spend limit|team's shared budget|credit limit for your plan", re.I)),
)
CLI_RESETS = re.compile(r"(?:resets?|try again)\s+(?:at\s+|in\s+)?([0-9][^\n·|]{0,40})", re.I)
CLI_SCAN_CHARS = 1500           # 出力の末尾だけを見る(素材の本文に同じ語があっても拾わない。CLI の失敗の文言は最後に出る)
CLI_NAMES = {"claude": "Claude", "codex": "Codex(GPT)", "grok": "Grok"}
CLI_LOGIN = {"claude": "ops のターミナルで `claude` を開いて /login(または `claude auth login`)",
             "codex": "ops のターミナルで `codex login`(headless なら `codex login --device-auth`)",
             "grok": "ops のターミナルで `grok login`"}
CLI_BLOCK_STATE = ROOT / "metrics" / "cli-blocked.json"   # Git 管理外。どの CLI がいつから止まっているか(工程をまたいで連投しない)
CLI_BLOCK_REMIND_SEC = 12 * 3600   # 止まったままなら、この間隔で1回だけ知らせ直す
CLI_BLOCKED: dict[str, dict] = {}  # この process で見つけた {CLI: 理由}(当番を呼ばない判断・工程の打ち切りに使う)
_CLI_LOCK = __import__("threading").Lock()


class CliBlocked(BaseException):
    """モデル CLI が認証切れ・利用上限で答えなかった。BaseException なのは、途中の `except Exception`(1件の失敗として数えて
    続ける所)に飲まれずに、工程の入口まで上がって「数えずに止める」ため(collect の Terminated と同じ扱い)。"""

    def __init__(self, info: dict):
        self.info = info
        super().__init__(f"{CLI_NAMES.get(info.get('tool'), info.get('tool'))} が{cli_block_kind(info)}で答えない: {info.get('detail', '')}")


def cli_block_kind(info: dict) -> str:
    return "認証切れ" if info.get("kind") == "auth" else "利用上限"


CODEX_ERROR_LINE = re.compile(r"^(?:\[[^\]]*\]\s*)?ERROR:")   # codex exec 自身が最後に出す失敗の行(`ERROR: …`)
CODEX_ERROR_TAIL = 5      # codex は失敗の行を出して終わる。最後の数行の中にあるものだけを CLI 自身の失敗とみる


def cli_error_lines(tool: str, text) -> list[str]:
    """CLI の出力のうち、**CLI 自身の**エラーとして読める行だけ(素材の本文・道具の出力を見ない。監査指摘: 探索のログの道具の
    出力に `Not logged in` があると認証切れと取り違えた)。codex は進行ログに道具の出力が混ざるので、出力の最後の数行のうち
    最後の `ERROR:` の行だけ(道具が `ERROR: …` を出しても、そのあとに codex 自身の失敗の行か進行ログが続く。監査指摘 r5)、
    claude -p は答えかエラーの1行だけを出すので最後の1行だけ、grok はエラー出力とセッション記録の失敗理由(呼び側が渡す)だけ。"""
    lines = [ln.strip() for ln in str(text or "")[-CLI_SCAN_CHARS:].splitlines() if ln.strip()]
    if tool == "codex":
        return [ln for ln in lines[-CODEX_ERROR_TAIL:] if CODEX_ERROR_LINE.match(ln)][-1:]
    if tool == "claude":
        return lines[-1:]
    return lines


def cli_blocked_reason(tool: str, *texts) -> dict | None:
    """CLI 自身のエラーの行(cli_error_lines)から、認証切れ・利用上限の文言を探す。無ければ None(本物の失敗)。
    codex は標準出力・エラーのどれかの最後の `ERROR:` が認証切れ・利用上限でない(ふつうの失敗)なら止まったとしない
    (別々に受けた出力の片方の末尾に道具の出力の `ERROR:` が来ても、codex 自身のふつうの失敗を隠さない。監査指摘 r5)。"""
    found = [ln for t in texts for ln in cli_error_lines(tool, t)]
    if tool == "codex" and any(not any(pat.search(ln) for _, pat in CLI_BLOCK_RULES) for ln in found):
        return None
    tail = "\n".join(found)
    for kind, pat in CLI_BLOCK_RULES:
        m = pat.search(tail)
        if m:
            start = tail.rfind("\n", 0, m.start()) + 1
            end = tail.find("\n", m.end())
            line = tail[start:end if end >= 0 else None].strip()[:240]
            r = CLI_RESETS.search(line) or CLI_RESETS.search(tail)
            return {"tool": tool, "kind": kind, "detail": line, "resets": r.group(1).strip().rstrip(".") if r else ""}
    return None


def _cli_state_io(update):
    """止まっている CLI の記録を、工程どうし(別 process)・並列のバッチどうしで食い違わずに読み書きする。"""
    import fcntl
    with _CLI_LOCK:
        CLI_BLOCK_STATE.parent.mkdir(parents=True, exist_ok=True)
        with open(CLI_BLOCK_STATE.with_suffix(".lock"), "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                st = json.loads(CLI_BLOCK_STATE.read_text(encoding="utf-8"))
                st = st if isinstance(st, dict) else {}
            except (OSError, ValueError):
                st = {}
            out = update(st)
            write_atomic(CLI_BLOCK_STATE, json.dumps(st, ensure_ascii=False, indent=1) + "\n")
            return out


def cli_block_text(info: dict, job: str, where: str, n: int, since: str) -> str:
    name = CLI_NAMES.get(info["tool"], info["tool"])
    if info["kind"] == "auth":
        what = f"🔑 {name} の認証が切れて答えない。ログインし直しが要る: {CLI_LOGIN.get(info['tool'], 'その CLI にログインし直す')}"
    else:
        what = (f"⏳ {name} が利用上限で答えない。戻るのは "
                + (f"{info['resets']}(CLI の表示)" if info.get("resets") else "CLI の表示に無い(各 CLI の使用量の画面で確かめる)"))
    return (f"{what}\n- 工程: {job} {where}(この原因で {n}回目。{since} から)\n- CLI の出力: {info.get('detail') or '(なし)'}\n"
            "- 答えなかった回は失敗に数えず、読み直しの列に残す(素材・記事は諦めない)。当番には回さない(同じ CLI で止まる)"
            + (f"\n- 戻ったら: {CLI_RESUME[job]}" if job in CLI_RESUME else ""))


CLI_RESUME = {   # 戻ったあとに続きをどう拾うか(工程ごと)
    "collect": "次の収集が、読めなかった新着を読み直しの列から先に読む",
    "compose": "組版はここで止めた(書けた記事と計画は残してある)。計画まで済んでいれば ops で "
               "`python3 scripts/compose.py --reuse-plan` で書けていない記事から組み直す。済んでいなければ `python3 scripts/compose.py`",
    "oncall": "当番はこの回を試行の回数に数えずに終えた(途中までの修正は次の試行の続きに残す)。次に工程が当番を呼べば続きから直す",
    "update": "翌日 01:30 の道具の更新がもう一度確かめる",
}


def report_cli_blocked(info: dict, job: str, where: str) -> bool:
    """認証切れ・利用上限を記録し、その CLI の同じ原因では1回だけ Discord に投稿する(同じ工程の中・続く工程で連投しない。
    止まったままなら CLI_BLOCK_REMIND_SEC ごとに1回)。異常の台帳には積まない(当番に回さない)。戻りは投稿したか。"""
    tool = info["tool"]
    CLI_BLOCKED[tool] = info
    now = time.time()
    stamp = now_jst().isoformat(timespec="minutes")

    def upd(st):
        cur = st.get(tool) if isinstance(st.get(tool), dict) and st[tool].get("kind") == info["kind"] else None
        post = cur is None or now - float(cur.get("posted_at") or 0) >= CLI_BLOCK_REMIND_SEC
        st[tool] = {"kind": info["kind"], "detail": info.get("detail", ""), "resets": info.get("resets", ""),
                    "since": (cur or {}).get("since") or stamp, "count": int((cur or {}).get("count") or 0) + 1,
                    "posted_at": now if post else float(cur.get("posted_at") or 0), "last": f"{job} {where}".strip(),
                    "last_at": now}
        return post, st[tool]["count"], st[tool]["since"]
    try:
        post, n, since = _cli_state_io(upd)
    except OSError as e:          # 記録できなくても知らせる(黙って止まらない)
        print(f"(CLI の停止の記録を書けない: {e})", flush=True)
        post, n, since = True, 1, stamp
    if not post:
        print(f"{CLI_NAMES.get(tool, tool)} が{cli_block_kind(info)}で答えない({job} {where}。この原因で {n}回目。投稿は済み)", flush=True)
        return False
    # 届いたか(require=True: 送り先が未設定・試験実行も「届いていない」)で決める。届いていないのに投稿済みにすると、
    # 送り先を直した次の工程でも12時間黙る(監査指摘 r3)
    if not notify(job, cli_block_text(info, job, where, n, since), ok=False, ledger=False, require=True):
        try:     # 届かなければ、次の回にもう一度投稿する
            _cli_state_io(lambda st: st.get(tool, {}).update(posted_at=0))
        except OSError:
            pass
    return True


def cli_recovered(tool: str, job: str) -> None:
    """CLI が答えた。止まっていた記録があれば消し、戻ったことを1回だけ知らせる。
    この process で既に止まったのを見た CLI は、戻ったことにしない: 止まる前に起動していた並列の子が後から成功で回収されると、
    記録を消して同じ原因をまた投稿していた(監査指摘)。戻ったかどうかは次の工程(別の process)の最初の答えで決める。"""
    if tool in CLI_BLOCKED:
        return
    try:      # 記録に無ければ何もしない(答えるたびに排他を取って書き直さない)
        if tool not in json.loads(CLI_BLOCK_STATE.read_text(encoding="utf-8")):
            return
    except (OSError, ValueError, TypeError):
        return
    try:
        # 消すかどうかは排他の中で決め直す(入口の確かめのあとに、並列の別のスレッドが止まったのを記録していれば消さない。監査指摘 r3)
        old = _cli_state_io(lambda st: None if tool in CLI_BLOCKED else st.pop(tool, None))
    except OSError:
        return
    if isinstance(old, dict) and float(old.get("posted_at") or 0) > 0:
        if not notify(job, f"{CLI_NAMES.get(tool, tool)} がまた答えるようになった({old.get('since', '')} から"
                           f"{'認証切れ' if old.get('kind') == 'auth' else '利用上限'}で {old.get('count', '?')}回止まっていた)",
                      require=True):
            # 届かなければ記録を戻し、次に答えたときにもう一度知らせる(監査指摘 r2)。その間に止まり直していれば、そちらを残す
            try:
                _cli_state_io(lambda st: st.setdefault(tool, old))
            except OSError:
                pass


def cli_check(tool: str, job: str, where: str, rc, *texts, answered: bool | None = None) -> dict | None:
    """モデル CLI を1回走らせた後に呼ぶ。答えた(既定は終了コード 0)なら止まっていた記録を消して None。答えなかった回は出力から
    認証切れ・利用上限を探し、見つかれば記録・投稿して理由を返す(呼び側は失敗に数えない)。時間切れ(rc が None)は判定しない。
    Grok は API に拒まれても終了コードが 0 なので、呼び側が answered(書き出しがあるか)を渡す。"""
    if answered if answered is not None else rc == 0:
        cli_recovered(tool, job)
        return None
    if rc is None:
        return None
    info = cli_blocked_reason(tool, *texts)
    if info:
        report_cli_blocked(info, job, where)
    return info


def grok_session_reasons(cwd: Path, since_ts: float, needle: str = "") -> str:
    """cwd で since_ts 以降に始まった Grok セッションの失敗理由(Grok は API に拒まれても終了コード 0 で、理由はセッション記録
    updates.jsonl にしか残らない)。needle を渡したら、会話の記録(chat_history.jsonl)にそれを含むセッションだけ(同じ cwd で並列に
    走る別の面・前の試行の拒否を、この試行の理由と取り違えない。監査指摘 r2)。"""
    import urllib.parse as _up
    base = Path.home() / ".grok" / "sessions" / _up.quote(str(cwd), safe="")
    reasons: list[str] = []
    try:
        dirs = [d for d in base.iterdir() if d.is_dir() and d.stat().st_mtime >= since_ts - 1]
    except OSError:
        return ""
    for d in dirs:
        try:
            ch = (d / "chat_history.jsonl").read_text(encoding="utf-8", errors="replace") if needle else ""
            # 記録の JSON が ASCII 以外や / を escape して書いていても見つける
            if needle and not any(n in ch for n in (needle, json.dumps(needle)[1:-1], json.dumps(needle)[1:-1].replace("/", "\\/"))):
                continue
            up = (d / "updates.jsonl").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'"agent_result":\s*"([^"]{0,300})"', up):
            if "error" in m.group(1).lower() and m.group(1) not in reasons:
                reasons.append(m.group(1))
    return "\n".join(reasons)


def oncall_clis_blocked() -> list[str]:
    """当番が使う CLI(修正の claude・監査の codex)のうち、この process で認証切れ・利用上限を見たもの。"""
    return [t for t in ("claude", "codex") if t in CLI_BLOCKED]


CLI_BLOCKED_EXIT = 75     # 子の工程(出典の判定)が CLI の停止でやめたときの終了コード(EX_TEMPFAIL)
CLI_STOPPED = "モデル CLI が認証切れ・利用上限で答えない"   # 取引の失敗の理由の頭。呼び側は異常にしない(投稿は子が済ませた)


def adopt_cli_blocked(since: float) -> list[str]:
    """子の工程が since 以降に記録した CLI の停止を、この process で見たものとして引き取る(当番を呼ばない判断に使う)。
    戻りは引き取った CLI。"""
    try:
        st = json.loads(CLI_BLOCK_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    got = []
    for tool, e in (st.items() if isinstance(st, dict) else []):
        if isinstance(e, dict) and float(e.get("last_at") or 0) >= since:
            CLI_BLOCKED[tool] = {"tool": tool, "kind": e.get("kind", ""), "detail": e.get("detail", ""), "resets": e.get("resets", "")}
            got.append(tool)
    return sorted(got)


def partial_output(e: BaseException) -> tuple[str, str]:
    """subprocess の時間切れ(TimeoutExpired)が持っている、それまでの標準出力・エラー(bytes のこともある)を文字列で返す。
    時間切れの回こそ、どこまで進んで何を言っていたかを残す(save_raw へ渡す)。"""
    txt = lambda v: v.decode("utf-8", "replace") if isinstance(v, bytes) else (v or "")
    return txt(getattr(e, "stdout", None) or getattr(e, "output", None)), txt(getattr(e, "stderr", None)) + f"\n({type(e).__name__}: {e})"


def reap(p, grace: int = 10) -> tuple[str, str]:
    """時間切れのセッションを落とし、それまでの出力を回収する。**回収にも上限を付ける。**
    claude / codex が起動した子(道具の実行)が出力のパイプを持ったまま残ると、親だけを kill しても
    上限なしの communicate() が子の終了まで待ち、締切を越えて工程が止まる(監査指摘)。
    プロセスグループと子孫(自分でグループを分けた道具も、親が先に終わって辿れなくなった道具も)を止め、死んだのを確かめてから
    出力を読む(stop_tree)。"""
    if isinstance(p, LaunchFailed):
        return "", p.error
    pid = getattr(p, "pid", None)
    try:
        # 頭がまだ回収前(生きている・ゾンビ)なら pid は再利用されていないので、頭から子孫を辿ってよい
        stop_tree(pid, root=pid if p.returncode is None else None, mark=getattr(p, "session_mark", None))
    except Exception:       # noqa: BLE001  止め方の失敗で回収を飛ばさない(下で頭だけは必ず落とす)
        traceback.print_exc()
    try:
        p.kill()
    except OSError:
        pass
    try:
        so, se = p.communicate(timeout=grace)
    except subprocess.TimeoutExpired as e:
        so, se = partial_output(e)
        se += "\n(出力を回収しきれなかった)"
    except ValueError:      # すでに回収済み・パイプが閉じている
        so, se = "", ""
    finally:
        _forget(p)
    return so or "", (se or "") + "\n(時間切れ)"


# --- モデル・道具のセッションの起動と回収 ---------------------------------------------------------
# モデルの CLI(claude・codex・grok)と、それが起動する道具(Bash・fetch_page.py・描画のブラウザ)は、すべて start_group / run_group で
# 自前のセッション(プロセスグループ)に起動し、時間切れ・中断(例外・SIGTERM)ではグループと子孫を止めて、死んだのを確かめてから返す。
# subprocess.run(timeout=) は直接の子しか止めず、CLI が起動した道具が残って、失敗を返したあとも作業ツリーへ書き続けた
# (監査指摘 2026-10-14: compose.claude_run・oncall.run_claude の時間切れのあとに孫がファイルを書いた)
_SESSIONS: set = set()          # 起動して、まだ回収していないセッション(中断のとき stop_groups がまとめて止める)
_SESSIONS_LOCK = threading.RLock()     # SIGTERM の後始末(同じスレッドの割り込み)から取り直せるように RLock
# セッションごとの印。起動するときに環境変数へ足し(入れ子のセッションは空白区切りで足していく)、子孫は受け継ぐ。親が先に終わって
# 親子の関係から辿れなくなった道具(自分でセッションを分けて親より長く生きる子)も、この印で見つけて止める(監査指摘 2026-10-14)
SESSION_ENV = "IMAS_SESSION"
# 工程を終えるところ(SIGTERM・途中終了の後始末で stop_groups() を呼んだ)。以後の起動は断る(別スレッドの担当が、後始末のあとに
# 次の担当を起動しない。監査指摘 2026-10-14: 収集の SIGTERM のあと、待っていたバッチが CLI を起動した)
STOPPING = threading.Event()


class LaunchFailed:
    """起動できなかったセッション(Popen の OSError)。その担当だけの異常終了(終了コード 127、理由は標準エラー)として扱う。
    並列に起動している途中で1つが起動できなくても、例外で抜けて先に起動した担当を置き去りにしない(監査指摘 2026-10-14:
    選定・校閲の2本目の起動の失敗で、1本目が回収されずに残った)。"""
    pid = None
    returncode = 127

    def __init__(self, args, err: OSError):
        self.args = args
        self.error = f"起動できない({type(err).__name__}: {err})"

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self, timeout=None):
        return "", self.error

    def kill(self):
        pass


def _forget(p) -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.discard(p)


def _proc_table() -> dict[int, tuple[int, int, str]] | None:
    """/proc の全プロセス: pid → (親の pid, プロセスグループ, 状態)。/proc が無ければ None。"""
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    out: dict[int, tuple[int, int, str]] = {}
    for d in names:
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat", "rb") as f:
                rest = f.read().decode("utf-8", "replace").rsplit(")", 1)[1].split()
            out[int(d)] = (int(rest[1]), int(rest[2]), rest[0])
        except (OSError, ValueError, IndexError):
            continue
    return out


def _proc_gone(pid: int) -> bool:
    """死んだ(消えた・ゾンビ)か。"""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return f.read().decode("utf-8", "replace").rsplit(")", 1)[1].split()[0] in ("Z", "X")
    except (OSError, IndexError):
        return True


_SUBREAPER = {"n": 0, "lock": threading.Lock(), "was": 0}


def subreaper(on: bool) -> None:
    """この process を孤児の受け皿(PR_SET_CHILD_SUBREAPER)にする/やめる。重なっても最後の1つが終わるまで続け、終わったら
    始める前の状態に戻す(当番のように、もとから受け皿の process の受け皿を外さない)。使うのは compose の ChildTracker と、
    出典の判定の取引(_txn_run)。"""
    import ctypes
    with _SUBREAPER["lock"]:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if on and _SUBREAPER["n"] == 0:
                cur = ctypes.c_int(0)
                libc.prctl(37, ctypes.byref(cur), 0, 0, 0)     # PR_GET_CHILD_SUBREAPER
                _SUBREAPER["was"] = cur.value
            _SUBREAPER["n"] = max(0, _SUBREAPER["n"] + (1 if on else -1))
            libc.prctl(36, 1 if _SUBREAPER["n"] > 0 else _SUBREAPER["was"], 0, 0, 0)     # PR_SET_CHILD_SUBREAPER
        except (OSError, AttributeError):
            pass


def _proc_marked(table: dict, mark: str) -> set[int]:
    """環境変数にセッションの印 mark を受け継いでいるプロセス(読めないものは除く)。"""
    key = (SESSION_ENV + "=").encode()
    out = set()
    for q in table:
        try:
            with open(f"/proc/{q}/environ", "rb") as f:
                env = f.read()
        except OSError:
            continue
        for kv in env.split(b"\0"):
            if kv.startswith(key) and mark.encode() in kv[len(key):].split():
                out.add(q)
                break
    return out


def stop_tree(pgid: int | None, root: int | None = None, wait: float = 5, mark: str | None = None) -> list[int]:
    """プロセスグループ pgid の全員と、その子孫(自分でグループ・セッションを分けた道具も)と、セッションの印 mark を受け継いだ
    プロセス(親が先に終わって辿れなくなった道具も)を止め、死んだのを確かめる(上限 wait 秒)。
    root はまだ回収していないセッションの頭(この process の子。グループの頭でなくても、そこから子孫を辿る)。CLI が道具を別の
    グループ・セッションで起動すると、グループだけを落としても残る。先に SIGSTOP で凍らせてから辿り直す(止めている間に起動された
    子を取りこぼさない)。この process・そのグループ・この process の子でない root には触らない。頭が回収済み(root なし)のときは、
    グループ pgid は印を受け継いだメンバーがいるときだけ自分のものとして止める(番号が別のグループに再利用されていても触らない)。
    戻りは止めた pid。"""
    if not isinstance(pgid, int) or pgid <= 1:
        return []
    me, own = os.getpid(), os.getpgrp()
    if pgid in (me, own) or root == me:      # 自分自身・自分のグループを指す回収は断る(この process とその子を止めてしまう)
        return []
    caught: set[int] = set()
    groups: set[int] = set()
    table = _proc_table()
    if table is None:           # /proc が無い: 生きている頭のグループを落とすだけ
        if root is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
        return []
    if root is not None and root not in table:
        root = None             # 頭はもう無い(回収済み)
    if root is not None and table[root][0] != me:
        # この process の子でない対象は回収しない(グループも辿らない。再利用された番号・偽の対象で、無関係なプロセスを止めない)
        return []
    marked = _proc_marked(table, mark) if mark else set()
    # グループ pgid が自分のものか: 生きている頭(この process の子)がいる、または印を受け継いだメンバーがいる
    mine = root is not None or any(table[q][1] == pgid for q in marked if q in table)
    for _ in range(50):
        marked = _proc_marked(table, mark) if mark else set()
        # 止めたプロセスのグループに広げるのは、そのグループが自分のものと分かるときだけ: グループの頭を止めた(この
        # セッションの子孫が作ったグループ)か、印を受け継いだメンバーがいる。分からないグループ(頭でない子が入った、ほかの親の
        # グループ)は広げない。止めたプロセス自身は下で1つずつ止める
        trusted = {table[q][1] for q in marked if q in table}
        groups = (({pgid} if mine else set())
                  | {g for g in (table[q][1] for q in caught if q in table) if g in caught or g in trusted}) - {own, 0}
        found = {q for q, (_, pg, _) in table.items() if pg in groups} | caught | marked
        if root and root in table:
            found.add(root)
        kids: dict[int, list[int]] = {}
        for q, (pp, _, _) in table.items():
            kids.setdefault(pp, []).append(q)
        todo = list(found)
        while todo:
            for c in kids.get(todo.pop(), []):
                if c not in found:
                    found.add(c)
                    todo.append(c)
        new = {q for q in found - caught - {me, 1} if table.get(q, (0, 0, "X"))[2] not in ("Z", "X")}
        if not new:
            break
        for q in new:
            try:
                os.kill(q, signal.SIGSTOP)
            except OSError:
                pass
        caught |= new
        table = _proc_table() or {}
    # 自分のものと分かったグループのうち、止めたプロセスのいるグループにだけ送る(誰もいないグループの番号は再利用されうる)
    for g in {pg for q, (_, pg, _) in table.items() if q in caught} & groups:
        try:
            os.killpg(g, signal.SIGKILL)
        except OSError:
            pass
    for q in caught:
        try:
            os.kill(q, signal.SIGKILL)
        except OSError:
            pass
    end = time.time() + wait
    while time.time() < end and not all(_proc_gone(q) for q in caught):
        time.sleep(0.01)
    return sorted(caught)


def start_group(cmd: list[str], *, cwd=None, env: dict | None = None, stdout=subprocess.PIPE, stderr=subprocess.PIPE):
    """モデル・道具のセッションを自前のセッション(プロセスグループ)で起動する。回収は finish_group。
    並列に起動するときは、起動のたびに一覧へ足し、finally で stop_groups(一覧) を呼ぶ(途中の例外・中断で置き去りにしない)。
    起動できなければ例外にせず LaunchFailed(その担当だけの異常終了)を返す。
    環境変数にセッションの印(SESSION_ENV)を足して起動する(親が先に終わった道具も、回収のときに印で見つける)。
    工程を終えるところ(STOPPING)なら起動しない。起動から控えるまでのあいだに届いた SIGTERM・SIGINT は、控えたあとで扱う
    (控える前に中断されると、起動した担当を見失う。監査指摘 2026-10-14)。"""
    import uuid
    if STOPPING.is_set():
        return LaunchFailed(cmd, OSError("工程を終えるところなので起動しない"))
    mark = uuid.uuid4().hex
    env = dict(os.environ if env is None else env)
    env[SESSION_ENV] = f"{env.get(SESSION_ENV, '')} {mark}".strip()
    p = None
    late = False
    try:
        with _hold_signals():
            p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=stdout, stderr=stderr, text=True,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                p.session_mark = mark
            except AttributeError:      # 差し替えた起動(テストの偽物)が属性を持てないときは、印なしで回収する
                pass
            # 控えるのと「終えるところ」を確かめるのは、終了の後始末(stop_groups)と同じ鍵の中で一度に行う。起動のあいだに
            # 別のスレッドで後始末が始まっていたら控えずに自分で止める(後始末の一覧に載らないまま待ち続けない。監査指摘 2026-10-14)
            with _SESSIONS_LOCK:
                late = STOPPING.is_set()
                if not late:
                    _SESSIONS.add(p)
            if late:
                reap(p, grace=2)
                return LaunchFailed(cmd, OSError("工程を終えるところなので起動しない"))
    except OSError as e:
        if p is None:
            return LaunchFailed(cmd, e)
        stop_groups([p])
        raise
    except BaseException:
        if p is not None:
            stop_groups([p])        # 控えたあとで扱った中断: この担当を止めてから上げる
        raise
    return p


@contextlib.contextmanager
def _hold_signals():
    """主スレッドで、起動から控えるまでのあいだ SIGTERM・SIGINT を後回しにし、抜けるときにもとの扱いで処理する
    (ハンドラは主スレッドでしか動かないので、ほかのスレッドの起動は中断されない)。"""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    got: list[int] = []
    old = {}
    for s in (signal.SIGTERM, signal.SIGINT):
        prev = signal.getsignal(s)
        if prev is not None:        # Python の外で決めた扱いは触らない
            old[s] = signal.signal(s, lambda signum, frame: got.append(signum))
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        for s in got:
            h = old[s]
            if callable(h):
                h(s, None)
            elif h == signal.SIG_DFL:   # 既定の扱い(終了): 起動したセッションを止めてから、同じ信号で終わる
                stop_groups()
                os.kill(os.getpid(), s)


def finish_group(p, timeout: float | None) -> tuple[str, str, bool]:
    """セッションの終わりを待ち、(標準出力, 標準エラー, 時間切れか) を返す(ファイルに出させたものは空)。timeout は秒(None は無制限)。
    時間切れはグループと子孫を止めて、それまでの出力を回収する(reap)。中断(例外・SIGTERM を変えた SystemExit・Ctrl-C)でも
    止めて回収してから上げる。正常に終わっても、グループに残った道具は止める(答えを読んだあとに作業ツリーへ書かせない)。"""
    if isinstance(p, LaunchFailed):
        return "", p.error, False
    try:
        so, se = p.communicate(timeout=None if timeout is None else max(0, timeout))
    except subprocess.TimeoutExpired:
        try:
            so, se = reap(p)
        except BaseException:
            stop_groups([p])    # 回収の途中の中断: 控えたまま止め直してから上げる(凍らせたまま残さない)
            raise
        return so, se, True
    except BaseException:
        stop_groups([p])        # 中断でも止めて回収してから、元の例外を上げる
        raise
    try:
        stop_tree(getattr(p, "pid", None), mark=getattr(p, "session_mark", None))
    except BaseException:
        stop_groups([p])        # 残った道具を止めている途中の中断: 控えたまま止め直してから上げる(凍らせたまま残さない)
        raise
    _forget(p)
    return so or "", se or "", False


ANSWER_SETTLE_SEC = 30     # 答えが出てから、セッションが自分で終わるのを待つ秒数(過ぎたら残った道具ごと止めて、その答えを採る)
ANSWER_POLL_SEC = 0.5


def run_group(cmd: list[str], timeout: float | None, *, cwd=None, env: dict | None = None,
              answered=None, settle: float | None = None) -> subprocess.CompletedProcess:
    """モデル・道具を1つ走らせて終わりを待つ(subprocess.run(capture_output=True, text=True, stdin=DEVNULL, timeout=) の代わり)。
    時間切れは TimeoutExpired(それまでの出力を持つ。partial_output で読める)で上げる。起動できなければ終了コード 127(理由は stderr)。
    answered(標準出力のファイルの Path → 答えが採用の形で出ているか)を渡すと、待つあいだ答えを見張る(_run_group_answered)。"""
    if answered is not None:
        return _run_group_answered(cmd, timeout, answered, cwd=cwd, env=env,
                                   settle=ANSWER_SETTLE_SEC if settle is None else settle)
    p = start_group(cmd, cwd=cwd, env=env)
    so, se, timed_out = finish_group(p, timeout)
    if timed_out:
        raise subprocess.TimeoutExpired(cmd, timeout, output=so, stderr=se)
    return subprocess.CompletedProcess(cmd, p.returncode, so, se)


def _alive_frozen(p, wait: float = 5) -> bool:
    """頭を凍らせ(SIGSTOP。凍った頭は自分では終われない)、凍ったことを停止の通知(CLD_STOPPED)で確かめられたら True。
    先に終わった(終了の通知)・確かめられない(回収済み・wait 秒で通知が来ない)は False。送っただけでは止まっていない
    (Sol 監査 r3: 送った直後の覗きでは生きていて、止まる前に自分で終了 1 になった回を採った)。
    通知は WNOWAIT で覗く(状態を消さないので、終了コードはあとの回収で Popen が読む。Popen は停止の通知を読まない)。"""
    try:
        os.kill(p.pid, signal.SIGSTOP)
    except OSError:
        return False
    end = time.monotonic() + wait
    while True:
        try:
            info = os.waitid(os.P_PID, p.pid, os.WEXITED | os.WSTOPPED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return False
        if info is not None:
            return info.si_code == os.CLD_STOPPED
        if time.monotonic() >= end:
            return False
        time.sleep(0.005)


def _run_group_answered(cmd: list[str], timeout: float | None, answered, *, cwd, env, settle: float) -> subprocess.CompletedProcess:
    """答えを見張りながらセッションを待つ。標準出力・エラーはパイプでなくファイルに受ける(答えのあとに残った道具がパイプを
    握っていても待たされない)。答えが出たのに頭が settle 秒で終わらない、または締切の時点で答えが出ているなら、頭と子孫を
    止めてその答えを採る(戻りの settled が True。終了コードは止めたときのもの)。答えが出ないまま締切なら TimeoutExpired。
    2026-10-25 12:31 の当番は 13:00:15 に schema どおりの答えを出したが、裏で走らせたままの全テストを claude -p が待って終わらず、
    13:01:52 に 1800 秒の時間切れになり「当番の作業が途中で止まった」と人に届いた。"""
    box = scratch_dir("session-out-")
    out, err = box / "stdout", box / "stderr"
    try:
        with open(out, "a", encoding="utf-8") as fo, open(err, "a", encoding="utf-8") as fe:
            p = start_group(cmd, cwd=cwd, env=env, stdout=fo, stderr=fe)
        if isinstance(p, LaunchFailed):
            return subprocess.CompletedProcess(cmd, p.returncode, "", p.error)

        def has_answer() -> bool:
            try:
                return bool(answered(out))
            except Exception:       # noqa: BLE001  見張りの失敗は「まだ答えが無い」(待ちを止めない)
                return False
        end = None if timeout is None else time.monotonic() + max(0, timeout)
        seen = None
        settled = timed_out = False
        try:
            while p.poll() is None:
                now = time.monotonic()
                if seen is None and has_answer():
                    seen = now
                late = end is not None and now >= end
                if seen is not None and (late or now - seen >= settle):
                    settled = True
                    break
                if late:
                    timed_out = True
                    break
                time.sleep(ANSWER_POLL_SEC)
            if settled or timed_out:
                # 見張りのあいだに頭が終わっていたら(自分で・ほかの誰かに止められて)、その終わり方を採る。settled・時間切れに
                # するのは、生きている頭をここで止めたときだけ(Sol 監査 r1: 答えを読むあいだに終了 1 で終わった回を採った。
                # r2: 終了コードが SIGKILL でも、こちらが止めた証拠にはならない)
                if not _alive_frozen(p):
                    settled = timed_out = False
                reap(p)             # 頭・グループ・子孫を止めて回収する(出力はファイルにある)
            else:
                finish_group(p, 10)     # 頭は終わっている。グループ・印に残った道具を止める
        except BaseException:
            stop_groups([p])        # 待ち・回収の途中の中断でも止めてから上げる
            raise
        so = out.read_text(encoding="utf-8", errors="replace")
        se = err.read_text(encoding="utf-8", errors="replace")
        if timed_out:
            raise subprocess.TimeoutExpired(cmd, timeout, output=so, stderr=se + "\n(時間切れ)")
        if settled:
            se += f"\n(答えを出したあと {settle:.0f} 秒で終わらなかったので、残った道具ごと止めて答えを採った)"
        r = subprocess.CompletedProcess(cmd, p.returncode, so, se)
        r.settled = settled
        return r
    finally:
        shutil.rmtree(box, ignore_errors=True)


def stop_groups(procs=None) -> None:
    """起動して、まだ回収していないセッションを止めて回収する。並列の起動・回収の finally(procs にその一覧)と、
    SIGTERM・途中終了の後始末(引数なし=この process の全部。以後の起動も断る: STOPPING)から呼ぶ。回収済みのものには何もしない。"""
    with _SESSIONS_LOCK:        # 「終えるところ」を立てるのと一覧を写すのを一度に(起動の控えと同じ鍵。どちらかに必ず載る)
        if procs is None:
            STOPPING.set()
        live = [p for p in (list(_SESSIONS) if procs is None else procs) if p in _SESSIONS]
    for p in live:
        try:
            reap(p, grace=2)
        except Exception:       # noqa: BLE001  後始末の途中で止めない(finally から呼ばれ、元の例外を隠さない)
            traceback.print_exc()
            _forget(p)


def exit_on_term() -> None:
    """SIGTERM を「起動したセッションを子孫ごと止めてから終わる」終了(SystemExit 143)に変える。モデル・道具を起動する工程の
    入口(main の最初)で呼ぶ。既定の SIGTERM は Python の後始末(finally・回収)を走らせずに落ち、起動した CLI が残った
    (監査指摘 2026-10-14: 手で回した update_clis を止めると、CLI が生き残った)。別スレッドのセッションも先に止める。"""
    def _term(signum, frame):
        stop_groups()
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, _term)


# 工程が例外・Ctrl-C・SystemExit で終わるときも、回収していないセッション(別スレッドの担当も)を残さない(interpreter の終わりに
# 止める。os._exit・既定の SIGTERM では走らないので、それは exit_on_term・組版の SIGTERM の後始末が受け持つ)
atexit.register(stop_groups)


def read_for_raw(p: Path) -> str | None:
    """生の出力に添えるため、セッションが書いたファイルを読む。無ければ None、読めなければ理由(例外で工程を止めない。
    ファイルの代わりにディレクトリが作られていた、など。監査指摘)。"""
    try:
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else None
    except OSError as e:
        return f"(読めない: {type(e).__name__}: {e})"


def save_raw(date: str, name: str, out: str = "", err: str = "", code=None, files: dict | None = None) -> Path | None:
    """モデルのセッションの**生の出力**を残す(metrics/work/<日付>/raw/<name>.txt。Git 管理外)。
    読めた・読めなかったに関係なく必ず残す。解釈した結果だけを残すと、「なぜ候補が0件だったか」「なぜ判定が空だったか」を
    後から確かめられない(2026-10-04: まとめサイトの新着1件を失ったが、出力が残っておらず、当番も原因を確定できなかった)。
    files は、セッションが書いたファイル(名前 → 中身)。作業ディレクトリごと消す前に残す。残せなくても工程は止めない。
    **上書きしない。**同じ名前(同じ依頼のやり直し・同じ日の別の収集回)は <name>-2.txt, -3.txt … と別に残す
    (やり直しが成功すると、失敗した1回目の出力が消えて原因を追えない。監査指摘)。"""
    try:
        d = ROOT / "metrics" / "work" / date / "raw"
        d.mkdir(parents=True, exist_ok=True)
        base = re.sub(r'[^A-Za-z0-9_.-]', '_', name)
        parts = [f"# at: {now_jst().isoformat(timespec='seconds')}", f"# exit: {code}", "# stdout", str(out or ""), "# stderr", str(err or "")]
        for k, v in (files or {}).items():
            parts += [f"# file: {k}", str(v if v is not None else "(無い)")]
        n = 1
        while True:
            p = d / (f"{base}.txt" if n == 1 else f"{base}-{n}.txt")
            try:
                with open(p, "x", encoding="utf-8") as f:     # 排他作成: 並列の呼び出しどうしでも上書きしない
                    f.write("\n".join(parts) + "\n")
                return p
            except FileExistsError:
                n += 1
    except OSError as e:
        print(f"生の出力を残せない({name}: {e})", flush=True)
        return None


def prompt_file(date: str, name: str, text: str, base: Path | None = None) -> str:
    """指示と素材を**ファイルに書き**、短い指示だけを返す。**モデルを呼ぶ全経路がこれを通る**
    (引数に可変の素材を渡す経路を残さない。監査指摘)。

    claude / codex は単発の API ではなく、このフォルダで自律して動くエージェントである。
    素材をプロンプトに詰め込む必要は無く、ファイルを読ませればよい(編集長の指示)。
    引数に詰めると 128KB(MAX_ARG_STRLEN)を超えたところで
    `OSError: [Errno 7] Argument list too long` で工程ごと落ちる(実測 2026-09-13 04:15)。
    置き場は <base>/metrics/work/<日付>/(Git 管理外)。base はセッションの cwd(既定はリポジトリ)。
    リポジトリ外の cwd で動くセッション(探索)には、その cwd を base に渡す
    """
    root = base or ROOT
    d = root / "metrics" / "work" / date
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(text, encoding="utf-8")
    rel = p.relative_to(root)
    # Read は1回に READ_LINES 行・1行 READ_COLS 字までしか返さない。超える分は**黙って切れる**ので、
    # 行数を伝えて最後まで読ませる(実測 2026-09-18: 組版の指示ファイルが初めて 2000 行を超えた 2005 行になり、
    # 末尾に置いていた「返すもの」の指示が1回の Read に入らず、セッションが 900 秒で時間切れになった)。
    # 長すぎる行は読ませ方では救えないので、書いた側の欠陥としてログに出す
    lines = text.split("\n")
    longest = max((len(ln) for ln in lines), default=0)
    if longest > READ_COLS:
        print(f"★{rel}: {longest} 字の行がある(Read は1行 {READ_COLS} 字で切る)。素材を改行のある形で書くこと", flush=True)
    how = (f"このファイルは全 {len(lines)} 行あります。Read は1回に {READ_LINES} 行までしか返さないので、"
           f"offset を進めて**最後の行まで**読んでから始めてください。" if len(lines) > READ_LINES - 200 else "")
    # **絶対パスで渡す。**claude の Read は絶対パスしか受けない。相対パスで渡すと、モデルは作業フォルダを
    # 推測して絶対パスを作り(実測: `/home/akabanep/git/Imas-Daily-News/…` と取り違える)、外れると
    # `find / -name …` で探し始める。WSL の `/` には Windows 側のドライブが繋がっていて、これが 120 秒の
    # 道具の時間切れまで返らない(2026-09-18 の経過で判明。6セッション中4つが最初の Read を外していた)
    return (f"指示と素材は `{p.resolve()}` に書いてあります。まずそのファイルを全部読み、書かれたとおりに実行して、"
            f"そこで指示された形式で答えてください。" + how)


READ_LINES, READ_COLS = 2000, 2000      # claude の Read ツールが1回に返す上限(行数・1行の字数)


PROMPTS = Path(__file__).resolve().parent.parent / "prompts"     # コードと同じ版の依頼文(作業対象の ROOT とは別に決める)


def prompt_part(name: str) -> str:
    """`prompts/<name>.md` をそのまま返す(複数の依頼文が共有する部品。末尾の改行は落とす)。"""
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").rstrip("\n")


def render_prompt(name: str, **values) -> str:
    """`prompts/<name>.md` の `{大文字の名前}` を埋めて返す。**プロンプトの本文は prompts/ に置く**
    (コードに埋めない。規則の理由・経緯はプロンプトに書かず PROMPTS.md に置く)。

    埋め残し・使われない値はどちらもエラーにする(差し込みの取り違えを黙って通さない)。
    値の中に `{大文字}` が現れても埋め直さない(素材の文面を指示として解釈しない)。
    """
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    wanted = set(re.findall(r"\{([A-Z][A-Z0-9_]*)\}", text))
    given = set(values)
    if wanted != given:
        raise ValueError(f"prompts/{name}.md: 埋め残し {sorted(wanted - given)} / 使われない値 {sorted(given - wanted)}")
    return re.sub(r"\{([A-Z][A-Z0-9_]*)\}", lambda m: str(values[m.group(1)]), text)


def trace_summary(trace: Path) -> str:
    """stream-json の経過ファイルから「何ターン・どの道具を・何に使ったか」を1行にまとめる。"""
    turns, tools, last = 0, [], ""
    try:
        with open(trace, encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e.get("type") != "assistant":
                    continue
                turns += 1
                for c in (e.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_use":
                        arg = c.get("input") or {}
                        what = arg.get("file_path") or arg.get("command") or arg.get("url") or arg.get("pattern") or ""
                        extra = f"+{arg['offset']}" if arg.get("offset") else ""
                        tools.append(f"{c.get('name')}({str(what)[-60:]}{extra})")
                    elif c.get("type") == "text" and c.get("text"):
                        last = c["text"].strip().replace("\n", " ")[:120]
    except OSError as e:
        return f"経過を読めない({type(e).__name__})"
    return f"{turns}ターン / 道具 {len(tools)}回: {' → '.join(tools[-12:]) or 'なし'}" + (f" / 最後の発言: {last}" if last else "")


def claude_traced(args: list[str], trace: Path, timeout: int, cwd: Path | None = None,
                  job: str = "compose", where: str = "") -> dict:
    """`claude -p` を**経過を残しながら**走らせる。戻り値は
    {"ok", "timed_out", "returncode", "structured", "result", "seconds", "cost", "summary"}。

    素の `-p` は終わるまで何も出さないので、時間切れのとき「何をしていて遅かったのか」が残らず、
    当番も監査も推測で直すしかなかった(実測 2026-09-18 の組版。監査指摘: 観測が無い)。
    stream-json を経過ファイルへ直接書かせ、時間切れでもそこまでの道具の呼び出しを読めるようにする。
    `--json-schema` の答えは最後の result イベントの structured_output に入る。
    """
    trace.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["claude", "-p", *args, "--output-format", "stream-json", "--verbose"]
    t0, timed_out, rc, err = time.time(), False, None, ""
    with open(trace, "w", encoding="utf-8") as out:
        # 自前のプロセスグループで起動し、時間切れ・中断には**グループと子孫ごと**止める(start_group / finish_group)。直系の claude
        # だけを殺すと、それが起動した道具(Bash の find など)が残り、やり直しのセッションと並走する(監査指摘)
        proc = start_group(cmd, cwd=cwd or ROOT, stdout=out)
        _, err, timed_out = finish_group(proc, timeout)
        rc = None if timed_out else proc.returncode
    if err.strip():
        # 標準エラーは全文を経過の記録の隣に残す(要約は末尾 200 字だけで、先頭の原因が消える。監査指摘)
        try:
            Path(str(trace) + ".stderr.txt").write_text(err, encoding="utf-8")
        except OSError:
            pass
    final: dict = {}
    with open(trace, encoding="utf-8", errors="replace") as f:
        for ln in f:
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if e.get("type") == "result":
                final = e
    secs = int(time.time() - t0)
    summary = (("時間切れ" if timed_out else f"exit {rc}") + f" {secs}秒 / " + trace_summary(trace)
               + (f" / stderr: {err.strip()[-200:]}" if err.strip() else ""))
    # 認証切れ・利用上限で答えなかった回は、呼び側のやり直しの回数に数えずに工程の入口まで上げる(CliBlocked)
    # 見るのは CLI 自身のエラーだけ: エラーで終わった result の本文と、経過のうち JSON の事象でない行(CLI が直に出す失敗)。
    # 道具の結果(Read した本文など)は JSON の事象の中にあるので見ない(監査指摘: 本文の `Not logged in` を認証切れと取り違えた)
    try:
        plain = "\n".join(ln for ln in trace.read_text(encoding="utf-8", errors="replace").splitlines()
                          if ln.strip() and not ln.lstrip().startswith("{"))
    except OSError:
        plain = ""
    blocked = cli_check("claude", job, where or trace.stem, None if timed_out else rc,
                        str(final.get("result") or "") if final.get("is_error") else "", plain, err,
                        answered=bool(final) and not final.get("is_error") and rc == 0)
    if blocked:
        raise CliBlocked(blocked)
    return {"ok": bool(final) and not final.get("is_error") and rc == 0, "timed_out": timed_out, "returncode": rc,
            "structured": final.get("structured_output"), "result": final.get("result") or "",
            "seconds": secs, "cost": final.get("total_cost_usd"), "summary": summary}


_URL_BAD_CHARS = re.compile(r"[\x00-\x20\x7f　]")


def clean_url(raw) -> str | None:
    """外から来る URL の**唯一の入口**。使える形に正規化し、駄目なら None(黙って切り詰めない。監査指摘)。

    - 最初の空白で切る(探索の出力が末尾に改行と `-` を付ける。実測 2026-09-15: 50件)。末尾の句読点・括弧を落とす
    - scheme は http/https、userinfo(`user@host`)・制御文字・全角空白は不可、2048 字まで
    - host は小文字に。それ以外(パス・クエリ)は触らない
    """
    s = str(raw or "")
    # 探索の出力に固有の末尾ゴミ(URL のあとに改行と「-」)だけを、形を認識して外す。それ以外の
    # 空白・制御文字が混ざっていれば不正として捨てる(黙って切り詰めない。監査指摘)
    m = re.fullmatch(r"(\S+)\n-\n?", s)
    if m:
        print(f"URL の末尾ゴミを外した: {s[:80]!r}", flush=True)
        s = m.group(1)
    # 前後の空白・タブ・制御文字も不正(黙って strip しない)。ASCII だけでなく Unicode の空白(NBSP、
    # U+2028 等)・制御(Cc)・書式(Cf)も全体で検査する(監査指摘)
    import unicodedata
    if (not s or len(s) > 2048 or _URL_BAD_CHARS.search(s)
            or any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf", "Zs", "Zl", "Zp") for ch in s)):
        return None
    s = s.rstrip(">,。、」』")
    if s.endswith(")") and s.count("(") < s.count(")"):   # 閉じ括弧の食い込み(Wikipedia の (…) は残す)
        s = s[:-1]
    try:
        u = urllib.parse.urlsplit(s)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname or u.username is not None or u.password is not None:
        return None
    try:
        u.port
    except ValueError:
        return None
    netloc = u.hostname.lower() + (f":{u.port}" if u.port else "")
    return urllib.parse.urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))


# fetch_page.py が本文を取れたページの記録(JSON Lines。1行 {"url": 頼んだ URL, "final": リダイレクトの行き先 or null})。
# 置き場は執筆・書き直しのセッションを起動する側(compose・writer_bench)が決め、この環境変数で子に渡す。検算は、根拠にした URL が
# 素材かこの記録にあるかを見る(2026-10-14 rule_bench: 執筆が fetch_page.py の取れなかった報道を Web 検索で読んで期間を断定し、
# 綴りの誤った URL を出典に書いた答えが検算を通った。校閲は fetch_page.py だけで出典を読むので確かめられない)
FETCH_RECORD_ENV = "IMAS_FETCH_RECORD"


# サイト全体のメンテナンス画面(公式ポータルは告知の URL を /mainte/index.html へ、ASOBI STORE は mainte.asobistore.jp へ転送する。
# 2026-10-21〜23)
MAINTENANCE_PATH = re.compile(r"/(?:mainte|maintenance)(?:[/._-]|$)", re.I)
MAINTENANCE_HOST = re.compile(r"^(?:mainte|maintenance)[.-]", re.I)
# メンテナンスの画面の言葉(5xx の応答の title・本文で見分ける。2026-10-23: ランティスは転送せず 503 で「現在メンテナンス中です」を返す)
MAINTENANCE_WORD = re.compile(r"メンテナンス|maintenance", re.I)


def maintenance_redirect(url: str, final: str | None) -> bool:
    """頼んだページがメンテナンス画面へ転送されたか(転送先のパスが /mainte/・/maintenance/、またはホストが mainte.・maintenance. で始まる)。
    転送先の本文はメンテナンスの案内で、頼んだページの中身ではない。これを本文として読むと、収集時に本文で確かめた素材の facts を
    「出典と食い違う」とし、公式の出典を「別の話題のページ」として記事を落とす(2026-10-22号: 公式ポータルのメンテナンスで見送り7本・
    校閲ブロック2本)。404 などの読めないページ・ただの転送は含めない"""
    import urllib.parse
    if not final or final == url:
        return False
    a, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(final)
    by_path = bool(MAINTENANCE_PATH.search(b.path)) and not MAINTENANCE_PATH.search(a.path)
    by_host = bool(MAINTENANCE_HOST.match(b.hostname or "")) and not MAINTENANCE_HOST.match(a.hostname or "")
    return by_path or by_host


def maintenance_screen(status: int, body: bytes, charset: str | None = None) -> bool:
    """5xx の応答がメンテナンスの画面か(title か本文にメンテナンスの言葉がある)。200 の普通のページ(メンテナンスのお知らせの記事を含む)・
    404・メンテナンスの表示の無い 5xx は含めない(読めなかっただけで、メンテナンスとは限らない)"""
    if not (500 <= int(status or 0) < 600):
        return False
    text = html_to_text(body or b"", charset)
    m = re.search(rb"<title[^>]*>(.*?)</title>", body or b"", re.S | re.I)
    title = m.group(1).decode(charset or "utf-8", "replace") if m else ""
    return bool(MAINTENANCE_WORD.search(title) or MAINTENANCE_WORD.search(text))


def maintenance_http_error(e: BaseException) -> bool:
    """urlopen の HTTPError がメンテナンスの画面の応答か(maintenance_screen)。本文を読むので、1つの例外に1回だけ呼ぶ。"""
    import urllib.error
    if not isinstance(e, urllib.error.HTTPError) or not (500 <= e.code < 600):
        return False
    try:
        body = e.read(400_000)
    except Exception:
        return False
    return maintenance_screen(e.code, body, e.headers.get_content_charset() if e.headers else None)


# メンテナンスで読めなかったサイトの、断り書きでの呼び名(ここに無いサイトはホスト名で書く)
MAINTENANCE_SITE_NAMES = {"idolmaster-official.jp": "公式ポータル", "lantis.jp": "ランティス", "asobistore.jp": "ASOBI STORE"}
# 断り書きを付け始めた号と、それより前に発行した号の文面(発行済みの号は再生成しても文面を変えない)。
# 編集長の決定 2026-10-22: 10/23 号は公式ポータルのメンテナンスに限って断り書きを付けて掲載。2026-10-23: どのサイトでも、読んだときに
# メンテナンスの画面だった出典は「確認できていない」と断って掲載する(常設)
MAINTENANCE_NOTICE_FROM = "2026-10-24"
MAINTENANCE_LEGACY_NOTICES = {
    "2026-10-23": "※公式ポータルがシステムメンテナンス中のため、公式サイトの告知ページは確認できていません(2026年10月23日時点)。"
                  "内容は収集時点の公式の告知・公式Xなどに基づきます。",
}
MAINTENANCE_NOTICE_TAIL = "がメンテナンス中のため、原本は確認できていません({date}時点)。内容は収集時点の公式の告知・公式Xなどに基づきます。"
# 読み直せず、保存の原本(page_archive)で書いた出典の断り書き(編集長 2026-10-26。読み直して同じ内容を読めたら付けない)。
# {read} は原本を読んだ日(収集が読んだ版は「収集時点(10月25日)」、執筆などが読み直した版は「10月26日」)
ARCHIVE_NOTICE_FROM = "2026-10-27"
ARCHIVE_NOTICE_TAILS = {
    "maintenance": "がメンテナンス中のため、原本は現在確認できていません({date}時点)。内容は{read}に読んだ原本に基づきます。",
    "failed": "の原本を読み直せず、現在の内容は確認できていません({date}時点)。内容は{read}に読んだ原本に基づきます。",
}


def site_host(url) -> str:
    """URL のホスト(小文字。末尾のドットと先頭の www. は外し、英数字でない名前は IDNA の表記にする)。同じサイトの www の有無を
    同じホストとして扱い、断り書きに書くホスト名を renderlib.strip_notice が外せる形に揃える(Sol 監査 Z2 r1: 末尾ドットで断り書きが重なった)"""
    import urllib.parse
    host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return ""
    return host[4:] if host.startswith("www.") else host


def maintenance_site_name(host: str) -> str:
    """断り書きに書くサイトの名前(MAINTENANCE_SITE_NAMES。無ければホスト名)。"""
    for dom, name in MAINTENANCE_SITE_NAMES.items():
        if host == dom or host.endswith("." + dom):
            return name
    return host


def maintenance_notice(edition: str, hosts, archived=None) -> str | None:
    """メンテナンスで原本を読めなかったホスト(hosts)と、読み直せず保存の原本で書いたホスト(archived: (ホスト, why, 原本の取得時刻,
    via) の並び)の断り書き(1つの段落にまとめる)。無ければ None。
    MAINTENANCE_NOTICE_FROM より前の号は発行したときの規則のまま(10/23 号は公式ポータルだけ、その前の号は付けない)。
    保存の原本の断り書きは ARCHIVE_NOTICE_FROM の号から"""
    import datetime
    hosts = sorted({h for h in hosts if h})
    edition = str(edition)
    if edition in MAINTENANCE_LEGACY_NOTICES:
        return MAINTENANCE_LEGACY_NOTICES[edition] if any(maintenance_site_name(h) == "公式ポータル" for h in hosts) else None
    if edition < MAINTENANCE_NOTICE_FROM:
        return None
    d = datetime.date.fromisoformat(edition)
    day = f"{d.year}年{d.month}月{d.day}日"
    parts = []
    if hosts:
        names = sorted({maintenance_site_name(h) for h in hosts})       # 同じサイトの別ホストは1つにまとめ、並びは名前で決める
        parts.append("・".join(names) + MAINTENANCE_NOTICE_TAIL.format(date=day))
    groups: dict[tuple[str, str], set[str]] = {}
    for host, why, at, via in (archived or []) if edition >= ARCHIVE_NOTICE_FROM else []:
        try:
            t = datetime.datetime.fromisoformat(str(at))
        except ValueError:
            continue
        if not host:
            continue
        read = f"収集時点({t.month}月{t.day}日)" if via == "collect" else f"{t.month}月{t.day}日"
        groups.setdefault(("maintenance" if why == "maintenance" else "failed", read), set()).add(maintenance_site_name(host))
    for (why, read), names in sorted(groups.items(), key=lambda kv: (sorted(kv[1]), kv[0])):
        parts.append("・".join(sorted(names)) + ARCHIVE_NOTICE_TAILS[why].format(date=day, read=read))
    return ("※" + "".join(parts)) if parts else None


def maintenance_rules(role: str) -> str:
    """執筆・書き直し・校閲の依頼文に足す、メンテナンス中の出典の扱い(prompts/site-maintenance.<role>.md。常設)と、
    保存の原本(page_archive)で読んだ・内容が変わった・無くなった出典の扱い(prompts/source-archive.<role>.md)。"""
    return render_prompt(f"site-maintenance.{role}") + render_prompt(f"source-archive.{role}")


def maintenance_record(rec) -> Path:
    """fetch_page.py がメンテナンス画面へ転送されたページの記録(本文を取れたページの記録の隣。読めたことにはしない)。"""
    return Path(rec).with_suffix(".mainte")


def failed_record(rec) -> Path:
    """fetch_page.py がメンテナンスでない理由(404・メンテナンスの表示の無い 5xx・時間切れ・本文が空 など)で読めなかったページの記録。
    同じホストの別のページがメンテナンスでも、これらのページに断り書きを付けない(Sol 監査 Z2 r1)"""
    return Path(rec).with_suffix(".failed")


def changed_record(rec) -> Path:
    """fetch_page.py が読み直して、保存の原本(page_archive)から内容が変わっていたページの記録(続報・訂正の材料)。"""
    return Path(rec).with_suffix(".changed")


def record_rows(path) -> list[dict]:
    """記録(JSON Lines)の行。記録が無ければ空。読めない行は飛ばす。"""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("url"), str) and row["url"]:
            out.append(row)
    return out


def archived_reads(rec) -> list[dict]:
    """記録のうち、読み直せず保存の原本を返したページ({url, final, archived: 原本の取得時刻, why: maintenance / failed})。"""
    return [r for r in record_rows(rec) if r.get("archived")]


def live_fetched_urls(rec) -> set[str]:
    """記録のうち、相手のサイトから本文を読めたページ(保存の原本を返したものを除く)の URL(頼んだ URL と転送先)。"""
    return {r[k] for r in record_rows(rec) if not r.get("archived") for k in ("url", "final") if isinstance(r.get(k), str) and r[k]}


def record_failed_marker(rec) -> Path:
    """記録(本文・メンテナンス・取得失敗)を書けなかった印。組版が異常として申告する(子の fetch_page.py の stderr は組版に届かない)。"""
    return Path(rec).with_suffix(".record-failed")


def failed_urls(rec) -> set[str]:
    """記録(note_fetched(failed=True))にある、メンテナンスでない理由で読めなかったページの URL(頼んだ URL と転送先。
    /old → /gone → 404 の /gone も読めなかったページ。Sol 監査 Z2 r5)。記録が無ければ空。"""
    return fetched_urls(failed_record(rec))


def maintenance_urls(rec, path: Path | None = None) -> set[str]:
    """記録(note_fetched(maintenance=True))にある、メンテナンスで読めなかったページの URL(頼んだ URL)。記録が無ければ空。"""
    try:
        lines = (path or maintenance_record(rec)).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    out: set[str] = set()
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("url"), str) and row["url"]:
            out.add(row["url"])
    return out


def _append_line(path, line: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))      # 1回の追記(並列の fetch_page.py が同じ記録に書いても行が混ざらない)
    finally:
        os.close(fd)


def note_fetched(url: str, final: str | None = None, maintenance: bool = False, failed: bool = False,
                 archived: str | None = None, why: str | None = None, changed: dict | None = None, via: str | None = None) -> None:
    """本文を取れたページを FETCH_RECORD_ENV の記録に書き足す(変数が無ければ何もしない)。書けなくても読み取りは止めない
    (stderr に出し、書けなかった印(record_failed_marker)を残す。組版が異常として申告する。記録に無いページは検算が差し戻す)。
    maintenance=True はメンテナンス画面へ転送されたページで、隣の記録(maintenance_record)へ書く(根拠にできるページには入れない)。
    failed=True はメンテナンスでない理由で読めなかったページで、隣の記録(failed_record)へ書く。
    archived は、読み直せず保存の原本(page_archive)を返したときのその版の取得時刻。本文を取れたページとして書き(根拠にできる)、
    why(maintenance / failed)・via(その版を読んだのが収集 collect か、執筆などの読み直し read か)と一緒に残す(組版が断り書きを付ける)。
    changed は読み直して内容が変わったページ({from_at, to_at})で、
    隣の記録(changed_record)へ書く"""
    base = os.environ.get(FETCH_RECORD_ENV)
    if not base:
        return
    rec = str(maintenance_record(base) if maintenance else failed_record(base) if failed
              else changed_record(base) if changed else base)
    row = {"url": url, "final": final if final and final != url else None}
    if archived and not (maintenance or failed or changed):
        row.update(archived=archived, why=why or "failed", via=via or "collect")
    if changed:
        row.update({k: changed.get(k) for k in ("from_at", "to_at")})
    line = json.dumps(row, ensure_ascii=False) + "\n"
    try:
        _append_line(rec, line)
    except OSError as e:
        print(f"FETCH_RECORD_FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        try:
            _append_line(record_failed_marker(base), json.dumps({"record": Path(rec).name, "url": url,
                                                                 "error": f"{type(e).__name__}: {e}"[:200]}, ensure_ascii=False) + "\n")
        except OSError:
            pass


def fetched_urls(path) -> set[str]:
    """記録(note_fetched)にある、本文を取れたページの URL(頼んだ URL とリダイレクトの行き先)。記録が無ければ空。読めない行は飛ばす。"""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    out: set[str] = set()
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict):
            out |= {row[k] for k in ("url", "final") if isinstance(row.get(k), str) and row[k]}
    return out


class JobLockTimeout(RuntimeError):
    pass


LOCK_POLL_SEC = 10


def job_lock(job: str, wait_min: float = 0, wait_fn=None, yield_fn=None):
    """collect / compose / release / oncall が**同じ作業ツリーと Git を同時に触らない**ための排他。

    pgrep で「いま走っていないか」を見るだけでは、確認した直後に timer が別の工程を起動できる
    (監査指摘)。flock は OS が保証し、プロセスが死ねば外れる。
    wait_min=0 なら取れなければ即 JobLockTimeout、それ以外はその分だけ待つ。
    当番が起動する再実行(子プロセス)は、親が持っているので IMAS_JOB_LOCK=held で取らない。
    wait_fn(t0) は待つ分を待つ間に見直す(待ち始めてから現れた事情を見落とさない。組版が、待っている間に呼ばれた
    収集の当番を待つ。監査指摘)。待つのは wait_min と wait_fn(t0) の大きいほう(t0 からの分)。
    yield_fn() が真の間は、空いていても取らずに譲る(収集→当番の受け渡しの最中。待つ分の内だけ)。
    戻り値は開いた fd(閉じるまで保持。プロセス終了で自動的に外れる)。
    """
    import fcntl
    import os
    if os.environ.get("IMAS_JOB_LOCK") == "held":
        return None
    (ROOT / "metrics").mkdir(exist_ok=True)
    fd = os.open(ROOT / "metrics" / "jobs.lock", os.O_RDWR | os.O_CREAT, 0o644)
    t0 = time.time()
    while True:
        limit = max(wait_min, wait_fn(t0)) if wait_fn else wait_min
        in_time = time.time() - t0 < limit * 60
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not (in_time and yield_fn and yield_fn()):
                return fd
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            if not in_time:
                os.close(fd)
                raise JobLockTimeout(f"{job}: 他の工程が作業ツリーを使っている(metrics/jobs.lock)。"
                                     + (f"{limit:g}分待ったが空かない" if limit else "同時には走らせない"))
        time.sleep(LOCK_POLL_SEC)


def notify_crash(job: str, e: Exception) -> None:
    """想定外エラーの通知。例外メッセージだけでは原因が追えないため traceback 末尾まで載せる。"""
    tb = traceback.format_exc()
    print(tb, file=sys.stderr, flush=True)
    tail = "\n".join(tb.strip().splitlines()[-6:])
    notify(job, f"想定外のエラーで停止: {e}\n```\n{tail}\n```\n(全文: journalctl --user -u imas-{job})", ok=False)


def git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失敗: {r.stderr.strip()}")
    return r


# WSL のホスト側 DNS プロキシ(10.255.255.254)は断続的に名前解決に失敗する。
# 数秒後には復旧することが多いので、通信を伴う git 操作は待って再試行する。
# これが無いと DNS の一瞬の不調だけで収集や発行が落ちる。
_NET_ERRORS = ("could not resolve host", "couldn't resolve host", "connection timed out",
               "could not read from remote repository", "operation timed out",
               "temporary failure in name resolution", "connection reset by peer")


def git_net(*args, attempts: int = 5, base_delay: float = 4.0) -> subprocess.CompletedProcess:
    """fetch/push など通信する git 操作。ネットワーク起因の失敗だけを再試行する
    (認証エラーや non-fast-forward は待っても直らないので即座に返す)。"""
    last = None
    for i in range(attempts):
        last = git(*args, check=False)
        if last.returncode == 0:
            return last
        err = (last.stderr or "").lower()
        if not any(m in err for m in _NET_ERRORS):
            return last  # ネットワーク以外の失敗は再試行しない
        if i < attempts - 1:
            wait = base_delay * (i + 1)
            print(f"git {args[0]} がネットワーク起因で失敗。{wait:.0f}秒後に再試行 "
                  f"({i + 1}/{attempts - 1})", flush=True)
            time.sleep(wait)
    return last


def branch_exists(name: str, remote: bool = False) -> bool:
    ref = f"refs/remotes/origin/{name}" if remote else f"refs/heads/{name}"
    return git("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def unsent_commits(branch: str) -> int | None:
    """手元の branch にあって origin/<branch>(最後に fetch・push した時点の origin)に無い commit の数。
    origin に branch が無い・数えられないときは None(送ってみるまで届いているとは言えない)。"""
    if not branch_exists(branch, remote=True):
        return None
    r = git("rev-list", "--count", f"origin/{branch}..{branch}", check=False)
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def send_branch(branch: str) -> str:
    """いまいる branch を origin へ送り、**届いたことを確かめる**(origin/<branch> が手元の先端を含む)。戻り値は届かなかった理由
    ("" なら届いた)。届いていれば通信しない。拒まれたら(collect 同士/release/当番が先に push した)origin の上に積み直して
    送り直す。積み直せなければ戻す(手元の commit は消さない)。"""
    if unsent_commits(branch) == 0:
        return ""
    r = git_net("push", "origin", branch)
    if r.returncode != 0:
        # collect 同士/release との競合: リモートを取り込んで積み直す
        p = git_net("pull", "--rebase", "origin", branch)
        if p.returncode != 0:
            git("rebase", "--abort", check=False)
            return f"push 失敗(pull --rebase も失敗): {(p.stderr or r.stderr).strip()[:200]}"
        r2 = git_net("push", "origin", branch)
        if r2.returncode != 0:
            return f"push 失敗: {r2.stderr.strip()[:200]}"
    left = unsent_commits(branch)
    if left != 0:
        return f"push は成功と返ったが、{branch} の commit が origin に届いていない(未送信 {'不明' if left is None else f'{left}件'})"
    return ""


def _merge_leftover(before: str) -> str:
    """取り込み(merge)の後始末の確認。取り込む前(HEAD が before・clean・merge の途中でない)に戻っていなければ、残っているものを返す。"""
    left = []
    if git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
        left.append("merge の途中(MERGE_HEAD)")
    head = git("rev-parse", "HEAD", check=False).stdout.strip()
    if head != before:
        left.append(f"HEAD が {head[:10] or '(不明)'}(取り込む前は {before[:10]})")
    st = git("status", "--porcelain", check=False)
    if st.returncode != 0 or st.stdout.strip():
        left.append("作業ツリーが clean でない: " + (st.stdout.strip() or st.stderr.strip())[:300])
    return "; ".join(left)


def _stop_to_oncall(job: str, date: str, msg: str) -> bool:
    """checkout で止まった工程を、人に異常として知らせ、当番へ渡す(直して再実行させる)。組版は工程の終わりの診断が
    再実行なしで呼ぶので、止まったここで渡す(その process の診断は重ねて呼ばない)。戻り値はいつも False(工程は止まる)。"""
    notify(job, msg, ok=False)
    escalate(job, date, msg)
    return False


def checkout_edition_branch(date: str, job: str) -> bool:
    """edition/<date> を origin と同期して checkout。無ければ main から作る。"""
    # 前の工程の出典の判定の取引が後始末できずに終わっていたら(SIGKILL 等)、未コミットの変更を見る前に開始時へ戻す(監査指摘 2026-10-14)
    back = recover_classify_txn()
    if back:
        print(back, flush=True)
        if "戻せない" in back:
            notify(job, back, ok=False)
            return False
        anomaly(job, back)
    if git("status", "--porcelain").stdout.strip():
        notify(job, f"作業ツリーに未コミットの変更があるため中止", ok=False)
        return False
    f = git_net("fetch", "origin", "--prune")
    if f.returncode != 0:
        notify(job, f"origin への fetch に失敗(再試行後も回復せず)。中止:\n{f.stderr.strip()[:200]}", ok=False)
        return False
    branch = f"edition/{date}"
    # **手元だけにある commit を捨てない。**下の `checkout -B` は手元のブランチを origin の位置へ引き戻すので、前の工程が
    # commit したが push できなかった成果物(収集の素材・組版の紙面)が消える(監査指摘 2026-10-14 ar9: push を拒まれた収集の
    # commit が、次の工程の checkout で消えた)。先に送り(分かれていれば origin の上に積み直す)、送れなければ捨てずに止まる
    unsent = unsent_commits(branch) if branch_exists(branch) and branch_exists(branch, remote=True) else 0
    if unsent != 0:
        n = "数が不明の" if unsent is None else f"{unsent}件の"
        c = git("checkout", branch, check=False)
        why = f"{branch} の checkout 失敗: {(c.stderr or c.stdout).strip()[:200]}" if c.returncode != 0 else send_branch(branch)
        if why:
            return _stop_to_oncall(job, date, f"{branch} に origin へ出していない {n} commit があるが送れない({why})。"
                                              "origin に合わせると消えるので、checkout せずに止めた")
        print(f"{branch}: origin へ出していなかった {n} commit を送った", flush=True)
    if branch_exists(branch, remote=True):
        git("checkout", "-B", branch, f"origin/{branch}")
    elif branch_exists(branch):
        git("checkout", branch)
    else:
        git("checkout", "-B", branch, "origin/main")
        git_net("push", "-u", "origin", branch)
        notify(job, f"{branch} が無かったため main から作成した(release の作成漏れ?)")
    # checkout できたことを**現在のブランチで**確かめる(git() は失敗で例外を投げるが、成功の証明は
    # これ。所有権の境界=STARTED_ON はこの True にだけ依存する。監査指摘)
    cur = git("branch", "--show-current", check=False).stdout.strip()
    if cur != branch:
        notify(job, f"{branch} を checkout できていない(いま {cur or '(detached)'})。中止", ok=False)
        return False
    # edition ブランチは release が前日に作るため、その後 main に入ったスクリプト修正を
    # 持たない。取り込まないと、直した当日の号が直っていない版で生成される
    # (2026-08-26号がこれで旧ロジックのまま13本で発行された)。
    # 紙面ファイルは main 側に無いので、ここでの取り込みが号の中身を壊すことはない。
    # --is-ancestor は「祖先でない」を終了コード1で返す。これは異常ではないので check=False
    if git("merge-base", "--is-ancestor", "origin/main", "HEAD", check=False).returncode != 0:
        # 取り込みの失敗(衝突)は例外にせず受けて、取り込む前の clean な状態へ戻してから止まり、当番へ渡す。以前は git() の例外で
        # abort に届かず、merge の途中(UU)のまま残って次の工程の checkout も止まった(監査指摘 2026-10-14 ar9)。古いスクリプトの
        # ままは進めない(直した当日の号が直っていない版で生成される)
        before = git("rev-parse", "HEAD").stdout.strip()
        m = git("merge", "origin/main", "--no-edit", check=False)
        if m.returncode != 0:
            conflicted = git("diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines()
            git("merge", "--abort", check=False)
            if _merge_leftover(before):
                # abort で戻らなければ取り込む前の commit へ戻す(この関数の頭で clean を確かめたので、消えるのは merge が持ち込んだものだけ)
                git("reset", "-q", "--hard", before, check=False)
            left = _merge_leftover(before)
            why = (f"{branch} へ main を取り込むと衝突した({', '.join(conflicted[:8])})。edition と main が同じファイルを別々に変えている。"
                   if conflicted else f"{branch} へ main を取り込めない({(m.stderr or m.stdout).strip()[:300]})。")
            return _stop_to_oncall(job, date, why + (f"取り込む前の状態に戻せない({left})。作業ツリーを確かめること" if left else
                                                      "取り込む前の状態に戻して、工程を止めた(古いスクリプトのまま進めない)"))
        else:
            print(f"{branch}: main を取り込んだ({git('rev-parse', '--short', 'origin/main').stdout.strip()})",
                  flush=True)
            # union merge で判定表の行が二重になったら、ここで除いて同じ push に載せる
            removed = dedupe_source_table()
            if removed:
                a = git("add", "--", "source_types.yml", check=False)
                c = git("commit", "-q", "-m", f"判定表: merge で二重になった {len(removed)} 行を除く", check=False) \
                    if a.returncode == 0 else a
                if c.returncode != 0:   # 除去を commit できなければ、二重のまま push しない(監査指摘)
                    notify(job, f"判定表の二重行を除いたが commit できない: {(c.stderr or c.stdout).strip()[:200]}", ok=False)
                    return False
                print(f"判定表の二重行 {len(removed)} 件を除いた", flush=True)
            # 送れなくても取り込みは手元に残る(この工程の commit_and_push が一緒に送り、届いたかを確かめる。次の工程の checkout も捨てない)
            sent = send_branch(branch)
            if sent:
                print(f"{branch}: main の取り込みをまだ送れない({sent})。この工程の commit と一緒に送る", flush=True)
    return True


# マージ衝突マーカー。解決漏れのままコミットすると、次の実行が JSON/YAML の
# パースで落ちる(2026-08-28: metrics/*.json を直さず commit し collect が停止した)
_CONFLICT_RE = re.compile(r"^(<{7} |={7}$|>{7} )", re.MULTILINE)


def conflict_markers() -> list[str]:
    """作業ツリーに衝突マーカーが残っているファイルを返す。"""
    bad = []
    for line in git("ls-files").stdout.splitlines():
        p = ROOT / line
        if not p.is_file() or p.suffix not in (".json", ".yml", ".yaml", ".md", ".py", ".html"):
            continue
        try:
            if _CONFLICT_RE.search(p.read_text(encoding="utf-8", errors="replace")):
                bad.append(line)
        except OSError:
            continue
    return bad


def changed_paths(patterns: list[str]) -> list[str] | None:
    """作業ツリーで変わったファイル(追跡中の変更・削除と、無視されていない未追跡)のうち、patterns に当たるもの。
    patterns は git の pathspec と同じ読み方(glob の * は / もまたぐ。ディレクトリ名はその下全部)。git status が失敗したら None。"""
    import fnmatch
    r = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", check=False)
    if r.returncode != 0:
        return None
    hits = set()
    for entry in r.stdout.split("\0"):
        path = entry[3:]
        if path and any(fnmatch.fnmatchcase(path, p) or path.startswith(p.rstrip("/") + "/") for p in patterns):
            hits.add(path)
    return sorted(hits)


def commit_and_push(branch: str, message: str, job: str, paths: list[str] | None = None) -> bool:
    """成果物を commit して push する。**戻り値は「リモートに載ったか」**。

    以前は commit / push の失敗を通知するだけで戻り値が無く、呼び出し側は「コミット済み・clean」と
    して先へ進んでいた(監査指摘: fail-open)。失敗したら False を返し、呼び出し側が判断する。
    `paths`(glob 可)を渡すと、それに当たる変わったファイルだけを stage する(渡さなければ全部。工程の途中終了時は必ず渡す)。
    現在のブランチが `branch` でなければ何もしない(別のブランチに成果物を積まない)。
    """
    cur = git("branch", "--show-current", check=False).stdout.strip()
    if cur != branch:
        notify(job, f"commit 中止: いるブランチが {cur or '(detached)'} で {branch} ではない", ok=False)
        return False
    if not classify_txn_settled(job):
        return False
    bad = conflict_markers()
    if bad:
        notify(job, "マージ衝突マーカーが残ったファイルがあるためコミットを中止:\n- "
                    + "\n- ".join(bad[:8]), ok=False)
        return False
    if paths:
        # 既に stage されていた無関係の変更を commit に混ぜない。reset できなければ進まない(監査指摘)
        rs = git("reset", "-q", check=False)
        if rs.returncode != 0:
            notify(job, f"git reset 失敗(stage を空にできない): {rs.stderr.strip()[:200]}", ok=False)
            return False
        # 渡されたパターンに当たる「変わったファイル」を列挙して、それだけを渡す。パターンをそのまま git add に渡すと、まだ無い成果物
        # (号を作る前に止まった)で pathspec エラーになり、計画や記録まで確定できずに残った(監査指摘 2026-10-14)
        found = changed_paths(paths)
        if found is None:
            notify(job, "git status 失敗(stage するファイルを列挙できない)", ok=False)
            return False
        if not found:
            # 送れなかった前の commit は下で送る(ここで成功と返すと、push を拒まれた後の後始末が未送信の commit を残したまま
            # 「確定した」と報告し、当番の取り込みは食い違いで止まった。監査指摘 2026-10-14 取り込みの監査)
            print("stage する変更なし(コミットせず)", flush=True)
        paths = found
    if paths != []:
        r = (git("--literal-pathspecs", "add", "-A", "--", *paths, check=False) if paths
             else git("add", "-A", check=False))
        if r.returncode != 0:
            notify(job, f"git add 失敗: {r.stderr.strip()[:200]}", ok=False)
            return False
        if not git("status", "--porcelain").stdout.strip():
            print("変更なし(コミットせず)", flush=True)
        elif git("diff", "--cached", "--quiet", check=False).returncode == 0:
            print("stage した変更なし(コミットせず)", flush=True)
        else:
            r = git("commit", "-m", message, check=False)
            if r.returncode != 0:
                notify(job, f"git commit 失敗: {(r.stderr or r.stdout).strip()[:200]}", ok=False)
                return False
    # 変更が無くても、前の回に commit して送れなかったもの(push を拒まれた・通信の失敗)があれば送り、届いたことを確かめてから
    # 成功と返す。以前は「変更なし」で送らずに True を返し、次の工程の checkout がその commit を外して素材が消えた(監査指摘 2026-10-14 ar9)
    why = send_branch(branch)
    if why:
        notify(job, why, ok=False)
        return False
    return True


def append_metric(kind: str, data: dict) -> None:
    d = now_jst().strftime("%Y-%m-%d")
    p = ROOT / "metrics" / f"{d}.json"
    doc = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    doc.setdefault(kind, []).append({"at": now_jst().isoformat(timespec="seconds"), **data})
    write_atomic(p, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")


def _no_duplicate_keys(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"JSON のキーが重複している: {k!r}")
        d[k] = v
    return d


def loads_strict(text: str):
    """JSON を読む。**同じキーが2回ある object は読めない答えにする**(json.loads は後の値で黙って上書きし、
    崩れた判定と正しい判定が同じ主題に並ぶと、前者が消えて後者だけが採られる。監査指摘)。"""
    return json.loads(text, object_pairs_hook=_no_duplicate_keys)


# 書式(schema/)で形を決めているファイルと、その定義。lint が発行前に検めるのと同じもの
SCHEMA_OF_PATH = (
    (re.compile(r"(^|/)candidates/\d{4}-\d{2}-\d{2}\.json$"), "candidates.schema.json"),
    (re.compile(r"(^|/)stock/scheduled/\d{4}-\d{2}-\d{2}\.json$"), "scheduled.schema.json"),
    (re.compile(r"(^|/)docs/_posts/[^/]+\.md$"), "article.schema.json"),
    (re.compile(r"(^|/)docs/_editions/[^/]+\.md$"), "edition.schema.json"),
)


class SchemaViolation(RuntimeError):
    """書式で形を決めているファイルに、定義に合わない中身を書こうとした。"""


def _norm_yaml(v):
    """YAML が date 型に解釈した値を ISO 文字列へ戻す(lint と同じ正規化。書式は文字列の契約)。"""
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _norm_yaml(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm_yaml(x) for x in v]
    return v


def schema_errors(path, text: str) -> list[str]:
    """path が書式で決めているファイルなら、text を lint と同じ定義・正規化で検めた誤り(無ければ空)。対象外のファイルは空。"""
    from jsonschema import Draft202012Validator
    import yaml
    rel = str(path).replace("\\", "/")
    name = next((s for pat, s in SCHEMA_OF_PATH if pat.search(rel)), None)
    if not name:
        return []
    try:
        if rel.endswith(".json"):
            data = json.loads(text)
        else:
            m = re.match(r"\A---\s*\n(.*?)\n---", text, re.DOTALL)
            if not m:
                return ["frontmatter が無い"]
            data = _norm_yaml(yaml.safe_load(m.group(1)) or {})
    except (ValueError, yaml.YAMLError) as e:
        return [f"読めない: {e}"]
    # 書式の定義はデータではなくコードの一部: ROOT(作業ツリー・テストでは一時ディレクトリ)ではなく、このファイルの場所から引く
    v = Draft202012Validator(json.loads((Path(__file__).resolve().parent.parent / "schema" / name).read_text(encoding="utf-8")))
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in v.iter_errors(data)]


def strict_writes() -> bool:
    """書式に合わない書き込みを例外にするか。テスト(test_pipeline・selfcheck)は例外にする(IMAS_STRICT_WRITES=1)。"""
    return os.environ.get("IMAS_STRICT_WRITES") == "1"


def write_checked(path, text: str) -> None:
    """書式で形を決めているファイルを書く唯一の入口。書く**前に** lint と同じ定義で検める
    (2026-10-14: 素材ファイルに書式の定義に無い欄を足した変更が、単体テストも監査も通って、03:11 の発行前の検査で初めて号を止めた。
    書く側で検めれば、その変更のテストが書いた瞬間に落ちる)。
    - テストでは(strict_writes)合わなければ SchemaViolation で止める: 書式と食い違う変更を、作った時点で捕まえる
    - 本番では止めずに書き、どのファイルのどの欄が合わないかを異常として申告する(当番のなぜなぜへ)。最後の門は発行前の lint で、
      止まり方(lint の段でその1本を落とす 等)は変えない。書く場所で例外にすると、1本を落とせば済む誤りで組版全体が止まる
    一時ファイルに書いて一度に差し替える(書きかけを残さない)。一時ファイルは書く先と同じディレクトリに置き(別の置き場からの
    差し替えは、同じ装置でもサンドボックスの境で EXDEV になった。監査指摘)、失敗しても消す。名前(`.<名前>.<pid>.tmp`)は
    .gitignore の対象で、止まって残っても作業ツリーを汚さない。"""
    path = Path(path)
    errs = schema_errors(path, text)
    if errs:
        msg = (f"{path.name} が書式({next(s for p, s in SCHEMA_OF_PATH if p.search(str(path).replace(chr(92), '/')))})に合わない: "
               + " / ".join(errs[:5]))
        if strict_writes():
            raise SchemaViolation(msg)
        anomaly("schema", msg + "(書いたうえで申告。発行前の lint が止める)")
    write_atomic(path, text)


def write_atomic(path, text: str) -> None:
    """ファイルを一時ファイルに書いて一度に差し替える(書きかけを残さない)。書式で決めたファイルは write_checked を通す。
    工程の記録(metrics/<日付>.json)・既読状態(stock/watch-state.json)もこれで書く(2026-10-14 監査指摘: 直接の上書きが途中で
    止まると、切れた JSON が残り、当番の後始末がそれを確定して次の工程が読めなくなる)。一時ファイルの置き方は write_checked と同じ。"""
    path = Path(path)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# 収集が書く成果(素材・既読状態・工程の記録)。途中で終わった収集の後始末(collect の例外・SIGTERM、当番の取り直しの時間切れ)が
# 確定する範囲
COLLECT_OUTPUTS = ("candidates", "stock/watch-state.json", "metrics")


def output_problem(path) -> str:
    """成果のファイルを確定してよいか: 読めること、JSON は JSON として読めること、書式(schema/)で決めたファイルは書式に合うこと。
    問題があれば理由、無ければ空。形だけを見る(中身の判断はしない)。"""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return f"読めない({type(e).__name__})"
    if path.suffix == ".json":
        try:
            json.loads(text)
        except ValueError as e:
            return f"JSON が読めない({str(e)[:80]})"
    return " / ".join(schema_errors(path, text)[:3])


def settle_collect_outputs(root=None) -> list[str]:
    """途中で終わった収集の成果を、確定する前に検める(2026-10-14 監査指摘: 書きかけで切れた素材の JSON を当番の後始末が
    commit・push し、次の組版・発行前の lint が読めずに止まる)。COLLECT_OUTPUTS の下で HEAD から変わったファイルのうち、
    壊れたもの(output_problem)は確定せずに HEAD の中身へ戻す(HEAD に無ければ消す)。素材(candidates)を1つでも戻したら
    既読状態も戻す(既読にした新着が素材に無い、を作らない。戻せば次の収集が同じ新着を拾い直す)。戻したファイルと理由を返す。
    git の状態が読めない・戻せないなら例外(確定してよいか分からないまま確定しない)。"""
    root = Path(root or ROOT)
    run = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True)
    st = run("status", "--porcelain", "-z", "--untracked-files=all", "--", *COLLECT_OUTPUTS)
    if st.returncode != 0:
        raise RuntimeError(f"git status 失敗: {st.stderr.strip()[:200]}")
    changed, entries, i = [], st.stdout.split("\0"), 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) > 3:
            changed.append(e[3:])
            if e[0] in "RC":
                i += 1        # 名前を変えた元のパス(使わない)
    bad = {p: why for p in changed if (why := output_problem(root / p))}
    if any(p.startswith("candidates/") for p in bad) and "stock/watch-state.json" in changed:
        bad.setdefault("stock/watch-state.json", "素材を戻したので既読も戻す")
    for p in bad:
        if run("cat-file", "-e", f"HEAD:{p}").returncode == 0:
            r = run("checkout", "-q", "HEAD", "--", p)
            if r.returncode != 0:
                raise RuntimeError(f"{p} を HEAD の中身へ戻せない: {r.stderr.strip()[:200]}")
        else:
            (root / p).unlink(missing_ok=True)
    return [f"{p}: {why}" for p, why in bad.items()]


def schema_ok(value, schema) -> bool:
    """モデルの答えが、そのセッションに渡した出力 schema のとおりか(形だけを見る)。schema は dict か JSON 文字列。
    **モデルの答えは、正常に終わったセッションのものを、渡した schema で検めてから使う**(2026-10-06 の洗い出し:
    --json-schema / --output-schema を渡していても、構造化出力の無い回は本文の JSON を拾う経路があり、そこでは形が
    守られない。崩れた欄で検算・組版が止まる、欠けた答えが「承認」「判断済み」になる、が各所で起きうる)。"""
    from jsonschema import Draft202012Validator
    s = json.loads(schema) if isinstance(schema, str) else schema
    return Draft202012Validator(s).is_valid(value)


def schema_error(value, schema) -> str:
    """schema_ok の裏: 答えが schema のとおりでない箇所を1つ、短く返す(とおりなら "")。形を直させる依頼に添える。"""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import best_match
    s = json.loads(schema) if isinstance(schema, str) else schema
    e = best_match(Draft202012Validator(s).iter_errors(value))
    if e is None:
        return ""
    where = "/".join(str(p) for p in e.absolute_path) or "(全体)"
    return f"{where}: {e.message[:300]}"


def read_answer(text: str) -> tuple[object, str]:
    """モデルの答え(標準出力・答えのファイル)から JSON を1つ取り出す。戻りは (答え, 読めない理由)。読めなければ (None, 理由)。
    1. 全体が1つの JSON ならそれ。2. 前後に地の文があれば、最初の `{` から最後の `}` まで
    3. それでも読めなければ、最初の `{` から JSON の object を順に読み、**同じ答えの繰り返しだけなら**その1つ
       (2026-10-23 18:56 の当番は正しい答えを空行をはさんで2回出し、2で2つを1つとして読んで「読めない答え」にした。
       作業は終わっていたのに、試行を1回失い「当番の作業が途中で止まった」と人に届いた)。
    違う答えが並ぶ(どれを採るかを決めない)・途中で切れている・同じキーが2回ある(loads_strict)は読めない。"""
    s = (text or "").strip()
    if not s:
        return None, "答えが空"
    try:
        return loads_strict(s), ""
    except ValueError:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None, "JSON の object が無い"
    # 答えの前後は地の文だけ。閉じていない配列の内側(前に `[`)・続きの JSON(後ろに `[ ] {`)なら読まない(監査指摘: `[` A、
    # A `{"verdict":"reject", …` を承認として採った)。開いた object は最初の `{` より前に来ないので、前は `[` だけを見る
    outside = "答えの外に JSON の記号がある(閉じていない配列・object の内側か、続きの答えがあるかもしれない)"
    if "[" in s[:m.start()] or re.search(r"[\[\]{]", s[m.end():]):
        return None, outside
    try:
        return loads_strict(m.group(0)), ""
    except ValueError:
        pass
    dec = json.JSONDecoder(object_pairs_hook=_no_duplicate_keys)
    # 答えと答えの間も地の文だけ(監査指摘: `[` A `,` A(閉じない配列)の内側の2つを繰り返しとして採った)
    found, i, j = [], m.start(), m.start()
    while i >= 0:
        if re.search(r'[\[\]{},"]', s[j:i]):
            return None, outside
        try:
            v, j = dec.raw_decode(s, i)
        except ValueError as e:
            return None, f"JSON が途中で切れている・形が崩れている({str(e)[:120]})"
        found.append(v)
        i = s.find("{", j)
    if re.search(r'[\[\]{},"]', s[j:]):
        return None, outside
    if len({json.dumps(v, ensure_ascii=False, sort_keys=True) for v in found}) > 1:
        return None, f"違う答えが {len(found)}個並んでいる(どれを採るか決められない)"
    return found[0], ""


def extract_json_array(text: str):
    """LLM 出力から最初の JSON 配列を寛容に取り出す。読めなければ空配列。

    「読めなかった」と「0件だった」を区別したい呼び出し側は extract_json_array_strict を使う。
    """
    v = extract_json_array_strict(text)
    return v if v is not None else []


def extract_json_array_strict(text: str):
    """LLM 出力から最初の JSON 配列を取り出す。**読めなければ None**(0件とは別)。

    「配列だけ出せ」と指示しても、安いモデル(定点観測の COLLECT_MODEL など)は前後に
    地の文を添える。最初の `[` から**末尾の** `]` までをまとめて取ると、配列のあとに
    `]` を含む注記が1つあるだけで json.loads が壊れ、正しく出ていた配列を丸ごと
    「読めなかった」として落とす(当番 2026-10-02: 定点観測 facts 化 6件。実測で
    「…6件中4件[次回WebFetchで再取得]」のような後置きで再現)。そこで最初の `[` から
    **括弧の釣り合う位置**までを取り出す(文字列リテラル内の括弧は数えない)。
    釣り合う `]` が無い(途中切れ)・中身が壊れている・配列が無いときだけ None を返す。

    定点観測は「処理を試みた URL」を既読にするので、出力が読めなかっただけの
    バッチを 0 件として既読にすると、その新着は二度と候補にならない(監査指摘 P1-5)。
    """
    s = text or ""
    i = 0
    while True:
        start = s.find("[", i)
        if start < 0:
            return None
        end = _match_json_bracket(s, start)
        if end is None:
            # 釣り合わない(途中切れ)。**内側の配列を別の答えとして拾わない**(切れた候補一覧の中の `[]` を
            # 「0件」と読み、取得の失敗を記録しなかった。監査指摘)
            return None
        try:
            v = loads_strict(s[start:end + 1])      # 同じキーが2回ある要素は読めない(後の値で黙って上書きされる)
            if isinstance(v, list):
                return v
        except ValueError:
            pass
        i = end + 1          # 壊れた塊の内側も拾わない(次の塊から探す)


def _match_json_bracket(s: str, start: int) -> int | None:
    """`s[start]` の開き括弧に対応する閉じ括弧の位置を返す。釣り合わなければ None。

    JSON の文字列リテラル("…" の中)にある括弧・引用符は数えない(本文に `]` や
    エスケープした `"` が入っていても配列の切れ目を見失わない)。
    """
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(s)):
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return j
    return None


# 期間ラベル(編集規程15)。誰がいつ買えるのかが変わる語だけを列挙する。
# 「発売」「受付」のような単語だけの語は誤検出が多いので複合語に限る。
PERIOD_LABELS = (
    "受付期間", "申込期間", "申込受付期間", "応募期間", "エントリー期間",
    "先行受付期間", "先行抽選受付期間", "先行販売期間", "抽選申込期間",
    "当落発表", "抽選結果発表", "結果発表",
    "入金期間", "入金締切", "支払期間", "決済期間",
    "一般販売", "一般発売", "一般先着", "先着販売",
    "販売期間", "発売日", "受注期間", "受注締切", "予約期間", "予約受付期間",
    "開催期間", "開催日", "公演日", "配信期間", "視聴期間", "アーカイブ配信期間",
    "応募締切", "申込締切", "販売終了", "受付終了",
)
_DATE_RE = re.compile(r"(?:\d{4}\s*年)?\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[/.-]\d{1,2}[/.-]\d{1,2}|\d{1,2}/\d{1,2}")
# 閉じタグの無い script / style は末尾まで落とす。取り出す側はバイト数で HTML を切る(fetch_page の 400KB 等)ので、
# 巨大なインライン CSS の途中で切れると閉じタグが無く、CSS が本文として残る。残った CSS で字数が足り、JS で描くページを
# 描画し直さずに「本文あり」として執筆へ渡していた(2026-10-26: アソビチケットの告知が CSS だけになり、SideM の記事が見送られた)
_TAG_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?(?:</\1>|\Z)|<[^>]+>")
# 本文を取る応答の読み取りの上限(バイト)。head にインライン CSS を数百 KB 積むページがあり、400KB で切ると本文の手前で切れる
# (2026-10-26: gameo.jp の記事は本文が 490KB 目から始まり、取れたのは題名だけだった)
PAGE_READ_BYTES = 5_000_000


def decode_html(raw: bytes, charset: str | None = None) -> str:
    """HTML のバイト列を文字列にする(ページの本文を取る経路はこれを共有する)。ヘッダの charset、<meta> の charset、utf-8・cp932・
    euc_jp の順に、置換文字を出さずに読めるものを採る。
    - ヘッダに charset が無い古いサイト(Shift_JIS / EUC-JP の会社概要ページ等)は <meta> から拾う。決め打ちの utf-8 だと文字化けして
      運営者名が読めない(実測 2026-09-13: ホビーストック会社概要)
    - Shift_JIS と宣言したページは CP932 の拡張文字(①・髙 など)を含むことがあり、宣言どおりには読めない。cp932 で読み直す
      (2026-10-14 監査指摘: 定点観測の本文取得が cp932 を試さず、置換文字だらけの本文を「取得済み」として facts 化へ渡した)"""
    m = re.search(rb'<meta[^>]+charset=["\']?\s*([A-Za-z0-9_-]+)', raw[:4000], re.I)
    meta = m.group(1).decode("ascii", "ignore") if m else None
    for enc in dict.fromkeys(e for e in (charset, meta, "utf-8", "cp932", "euc_jp") if e):
        try:
            return raw.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    for enc in (charset, meta):      # どれでも読めなければ、宣言どおりに読んで読めない字だけを置き換える
        if enc:
            try:
                return raw.decode(enc, errors="replace")
            except LookupError:
                continue
    return raw.decode("utf-8", errors="replace")


def html_to_text(raw: bytes, charset: str | None = None) -> str:
    t = decode_html(raw, charset)
    t = _TAG_RE.sub("\n", t)
    t = html_lib.unescape(t)
    return re.sub(r"[ \t　]+", " ", t)


def looks_unrendered(text: str) -> bool:
    """素の HTML から取った本文が空同然(JS で描画するページ)か。空白・改行を除いた字数で測る。

    html_to_text はタグを改行に置き換えるので、描画前のページでも改行が何百も残る。空白込みで数えると
    タイトルとメニューしか無いページが「本文あり」になり、描画し直さずに執筆へ渡っていた
    (実測 2026-10-08: 公式ポータルの LP が空白込み 591字・空白抜き 248字で、規約変更の記事が見送られた)。"""
    return len(re.sub(r"\s+", "", text or "")) < 400


def render_hosts() -> set[str]:
    """描画しないと本文が出ないと分かっているサイトのホスト(sources.yml で type: portal の観測先)。"""
    import urllib.parse
    import yaml
    try:
        sources = yaml.safe_load((ROOT / "sources.yml").read_text(encoding="utf-8")) or []
    except OSError:
        return set()
    return {urllib.parse.urlsplit(u).hostname for s in sources if s.get("type") == "portal"
            for u in (s.get("url"), s.get("base")) if u}


class RenderFailed(RuntimeError):
    """描画取得(fetch_rendered.py)の失敗。終了コードと stderr の要点を持つ。"""


def render_failure(returncode, stderr: str) -> str:
    """描画取得の失敗の要点: 終了コードと、stderr のうち例外の行(fetch_rendered.py が出す RENDER_FAILED の行、
    無ければ最後の例外の行、それも無ければ末尾の行)。Selenium の例外は後ろに Chrome のスタックが続くので、末尾だけでは原因が読めない"""
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    key = ([l for l in lines if l.startswith("RENDER_FAILED:")]
           or [l for l in lines if re.match(r"^[\w.]*(?:Error|Exception|Exit|Interrupt)\b", l)] or lines[-2:])
    return f"終了コード {returncode}: " + (" / ".join(key[-2:])[:300] if key else "(stderr なし)")


def render_page(url: str, timeout: int = 30, limit: float | None = None) -> str:
    """ヘッドレスブラウザで描画したページの HTML。**失敗を空で返さない**: 非正常終了・時間切れ・空の出力は、
    終了コードと stderr の要点を持った RenderFailed で上げる。以前は空文字に変えていて、呼び出し側の記録は「空の応答」
    だけになり、原因(2026-10-07 コロムビアの一覧の TimeoutException)を当番が別に再現して確かめていた(当番の指摘 dc23e62472)。
    limit は待ってよい秒数の上限(工程の締切の残り。0 以下なら起動しない)。"""
    wait = timeout + 60 if limit is None else min(timeout + 60, limit)
    if wait <= 0:
        raise RenderFailed(f"{url} を描画しない: 工程の締切を過ぎた")
    try:
        # ブラウザ(chromedriver・chrome)ごと止めるため、自前のグループで起動する(run_group)
        r = run_group([str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "scripts" / "fetch_rendered.py"), url, "--timeout", str(timeout)],
                      timeout=wait, cwd=ROOT)
    except subprocess.TimeoutExpired as e:
        raise RenderFailed(f"{url} の描画が {int(wait)} 秒で終わらず打ち切った: "
                           + render_failure("(時間切れ)", partial_output(e)[1])) from None
    except OSError as e:
        raise RenderFailed(f"{url} の描画を起動できない: {type(e).__name__}: {e}") from None
    if r.returncode != 0:
        raise RenderFailed(f"{url} の描画に失敗: " + render_failure(r.returncode, r.stderr))
    if not (r.stdout or "").strip():
        raise RenderFailed(f"{url} の描画が空の HTML を返した: " + render_failure(r.returncode, r.stderr))
    m = re.search(r"(?m)^RENDERED_URL: (\S+)\s*$", getattr(r, "stderr", None) or "")
    if m:
        RENDER_FINAL[url] = m.group(1)
    else:
        RENDER_FINAL.pop(url, None)
    return r.stdout


# 描画したページ → 描画し終えたときの URL(JS でメンテナンス画面へ転送されたページを、原本として残さない・読めたことにしない。
# Sol 監査 r1: 本文の言葉と字数で見分けると、「メンテナンス終了・締切延長」の普通の告知を案内の画面として捨てた)
RENDER_FINAL: dict[str, str] = {}


def needs_render(url: str, text: str) -> bool:
    """素の HTML から取った本文を描画し直して読むべきか。本文が空同然か、描画必須と分かっているサイトか。

    字数だけでは足りない。公式ポータルはナビだけで非空白 3千字を超え、本文が JS で描かれる記事でも
    「本文あり」に見える(実測: 配信チケット記事で 14,000円 を取りこぼした。collect.verify の注記)。"""
    import urllib.parse
    return looks_unrendered(text) or urllib.parse.urlsplit(url).hostname in render_hosts()


def extract_periods(text: str, limit: int = 12) -> list[str]:
    """本文から「ラベル: 値」の形で期間を原文のまま抜き出す(編集規程15)。

    LLM に読ませて要約させるとラベルが落ちる。実際 2026-08-26号では、出典ページに
    「受付期間 8/8〜8/24」「当落発表 8/26」「入金期間 8/26〜8/30」と明記されていたのに、
    収集は「チケット販売期間: 8/26〜8/30」という別物を facts に書いていた
    (当選者向けの入金期間を、誰でも買える販売期間に変えてしまった)。
    ここは機械で抜く。機械なら言い換えない。
    """
    out, seen = [], set()
    for label in PERIOD_LABELS:
        for m in re.finditer(re.escape(label), text):
            tail = text[m.end():m.end() + 120]
            # ラベル直後の空白・区切りを飛ばし、日付を含む最初の行を値とする
            for line in (ln.strip(" :：\t") for ln in tail.split("\n")):
                if not line:
                    continue
                if _DATE_RE.search(line):
                    v = re.sub(r"\s+", " ", line)[:80]
                    key = (label, v)
                    if key not in seen:
                        seen.add(key)
                        out.append(f"{label}: {v}")
                break
            if len(out) >= limit:
                return out
    return out


# --- 出典種別の判定(規程2) -------------------------------------------------
SOURCE_TYPES = ("公式", "準公式", "当事者", "報道", "ファン", "二次情報", "もちより", "未確認")
_ST_TABLE: dict | None = None
_ST_KEY: tuple | None = None     # _ST_TABLE を読んだときのファイルの同一性(mtime_ns, size, inode)

# 短縮 URL のサービス。ホスト自身は持ち主を持たず、誰の何を指すかは展開先で決まる。**ドメインでは判定しない**
# (2026-10-14 監査指摘: t.co が丸ごと公式に登録され、LivePocket・Amazon・ザッキャラへの短縮まで公式として紙面に出た)。
# 短縮1つずつ、展開先の種別を path_types へ写す(classify_sources.resolve_short_links)
SHORTENERS = ("t.co", "bit.ly", "x.gd", "lin.ee", "tinyurl.com", "ow.ly", "buff.ly", "is.gd", "cutt.ly",
              "amzn.to", "amzn.asia", "goo.gl")
# 文書 ID がクエリの id にある共有サービス(Microsoft Forms)。どの文書もパスが同じなので、id まで含めて1つの文書とする
# (同: forms.cloud.microsoft が丸ごと公式に登録され、文書 ID の違う別のフォームまで公式になった)
QUERY_DOC_PAGES = {"forms.cloud.microsoft": "/pages/responsepage.aspx", "forms.office.com": "/pages/responsepage.aspx"}
# 多数の会社が同居し、持ち主がパスの先頭(アカウント)で決まるメールフォームのサービス(同: krs.bz が丸ごと準公式だった)
PATH_FORM_HOSTS = ("krs.bz",)


def query_doc_key(url: str) -> str | None:
    """クエリの id で文書が決まる URL なら、判定表に書く文書のキー(`<host>/pages/responsepage.aspx?id=<ID>`、
    YouTube の再生リストは `youtube.com/playlist?list=<ID>`)。それ以外は None。"""
    import urllib.parse
    u = urllib.parse.urlparse(url if "//" in url else "//" + url)
    host = (u.hostname or "").removeprefix("www.")
    if host.removeprefix("m.") == "youtube.com" and u.path.rstrip("/") == "/playlist":
        # YouTube の再生リストも、どれもパスが同じでクエリの list が1つの再生リスト。種別は持ち主のチャンネルを写す
        # (classify_sources.resolve_playlists)。キーは `youtube.com/playlist?list=<ID>`(`&si=` などは含めない)
        ids = urllib.parse.parse_qs(u.query).get("list") or []
        return f"youtube.com/playlist?list={ids[0]}" if len(ids) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{10,}", ids[0]) else None
    page = QUERY_DOC_PAGES.get(host)
    if not page or u.path.rstrip("/").lower() != page:
        return None
    ids = urllib.parse.parse_qs(u.query).get("id") or []
    return f"{host}{page}?id={ids[0]}" if len(ids) == 1 and re.fullmatch(r"[A-Za-z0-9._-]{16,}", ids[0]) else None


# 文書・短縮の ID がパスかクエリに入り、大小文字だけ違う別の文書・短縮がありうるホスト(Google の文書・フォーム、Microsoft Forms、
# 短縮 URL)。判定表のキーを比べるとき、このホストのパスは大小文字を区別する(同一視すると、別の文書に判定が付く。監査指摘 2026-10-14)
ID_CASE_HOSTS = ("docs.google.com", "drive.google.com", "forms.gle") + tuple(QUERY_DOC_PAGES) + SHORTENERS


def path_type_norm(key) -> str:
    """path_types のキーを比べる形(表の重複の検査・merge 後の二重行の除去・合議の追加で同じ比べ方をする)。末尾の / を落とし、
    ホストは小文字。パスも小文字にするが、ID_CASE_HOSTS のパス(文書・短縮の ID)はそのまま。"""
    k = str(key).strip().rstrip("/")
    host, sep, rest = k.partition("/")
    host = host.lower()
    # クエリの ID で決まるキー(再生リストの list=)も大小文字を区別する
    return host + sep + (rest if host in ID_CASE_HOSTS or query_doc_key(k) else rest.lower())


def shared_host(host: str) -> bool:
    """ドメイン全体では判定しない共有サービス(短縮 URL・フォーム・メールフォーム)のホストか。判定表のドメインの一覧に
    載っていても使わない(merge で撤去した行が戻っても効かない)。"""
    return host in SHORTENERS or host in QUERY_DOC_PAGES or host in PATH_FORM_HOSTS


# 出典種別の強い順(retag_sources・lint・compose と同じ並び)
SRC_ORDER = ("公式", "準公式", "当事者", "演者", "報道", "ファン", "二次情報", "もちより", "未確認")


def src_rank(t) -> int:
    """種別の順位(小さいほど強い)。知らない値は未確認より弱い。"""
    return SRC_ORDER.index(t) if t in SRC_ORDER else len(SRC_ORDER)


# リンク先のキーで落とすクエリ(どのサイトでも追跡の印でしかないものだけ。`s`・`ref` などはサイトによって中身を区別するので
# 残す。監査指摘 2026-10-21: `?s=event-A` だけを確かめたのに `?s=event-B` まで同じキーになった)
LINK_NOISE_QUERY = re.compile(r"^(utm_.*|fbclid|gclid|igsh|igshid|ref_src)$")


LINK_KEY_RE = re.compile(r"[A-Za-z0-9._~/?=&%+@!$*,;()'-]+")     # 判定表 link_types に書けるキーの文字
_YAML_RESOLVER = None     # (YAML のスカラーの型を決める Resolver, ScalarNode)。link_key が毎回作り直さない


def link_key(url) -> str | None:
    """リンク先のキー(_link_key)。判定表に書けない文字が残るキーは None(引き継ぎの対象にしない。書けないまま合議を
    繰り返さない。監査指摘 2026-10-21)。"""
    # 返すキーは、読み直しても同じキーになる(判定表に書いて引ける)ものだけ(監査指摘 2026-10-21: `event%3Bid=A` のキーが
    # 読み直すと `event` になり、保存できないまま合議を繰り返した)
    # 読めない URL(`https://[broken]/` など。urlparse が ValueError)は None。過去の素材の facts にあっても索引を止めない
    try:
        k = _link_key(url)
        # 1000 字を超えるキーは判定表の YAML のキーに書けない(1024 字まで)ので対象外(監査指摘 2026-10-21)
        if not (k and len(k) <= 1000 and LINK_KEY_RE.fullmatch(k) and _link_key("https://" + k) == k):
            return None
        # 判定表の YAML が文字列として同じ値に読むキーだけ(`1.e-3`・`.nan` は数として読まれる。監査指摘 2026-10-21)
        global _YAML_RESOLVER
        if _YAML_RESOLVER is None:
            import yaml
            _YAML_RESOLVER = (yaml.resolver.Resolver(), yaml.ScalarNode)
        tag = _YAML_RESOLVER[0].resolve(_YAML_RESOLVER[1], k, (True, False))    # 引用符の無いスカラーの型(YAML の読み方)
        return k if tag == "tag:yaml.org,2002:str" else None
    except ValueError:
        return None


def _link_key(url) -> str | None:
    """リンク元から種別を引き継ぐ単位(判定表 link_types のキー)。リンク先1つを指す形で、同じ先を指す URL の書き方の揺れ
    (`/viewform?usp=…`、`youtu.be/<ID>`、`x.com/<名>/status/<ID>/photo/1` など)は同じキーになる。読めない URL は None。
    短縮は `<host>/<ID>`、X の投稿は `x.com/i/status/<ID>`、YouTube の動画は `youtube.com/watch?v=<ID>`、ニコニコは
    `nicovideo.jp/watch/<ID>`、文書は文書 ID まで、ほかは書かれたままの host + パス + クエリ(追跡の印の項だけ除く)。"""
    s = str(url or "").strip()
    # 入口: 書かれた形のまま判定表に書ける ASCII の https/http の URL だけ。ホストは小文字の ASCII、認証情報・ポート・フラグメント
    # は持たない。別の先を同じキーにしうる形(パスのパラメータ `;`、`//`・`.`・`..` の区切り、区切り・ドットの符号化、末尾の `?`、
    # 空のクエリの項、同じ名前のクエリ)は None。サービス固有の形にもここで効かせる(監査指摘 2026-10-21: r3〜r10 で
    # 書き換え・切り捨て・畳み込みのたびに別の先が同じキーになった。キーは書き換えず、揺れの吸収は決まったサービスの形だけ)
    # 元の URL の全体を、判定表に書ける ASCII の文字だけに限る(生の `\` などはブラウザが区切りとして読み、別の先になる。
    # サービス固有の形が後半を捨てて先頭の ID を返す前に止める。監査指摘 2026-10-21)
    m = re.fullmatch(r"(https?)://([a-z0-9.-]+)(/[A-Za-z0-9._~/!$*,;=@()'+%-]*)?(?:\?([A-Za-z0-9._~/!$*,;=@()'+%&-]*))?", s)
    if not m:
        return None
    scheme, host, path, query = m.group(1), m.group(2), m.group(3) or "", m.group(4)
    # ホストは英字を含むドメイン名だけ(`123`・`yes`・`null` のようなキーは判定表の YAML で文字列にならない。監査指摘 2026-10-21)
    if "." not in host or not re.search(r"[a-z]", host):
        return None
    if (query == "" or ";" in path or re.search(r"//|(^|/)\.\.?(/|$)", path)
            or re.search(r"%(2F|3B|3F|23|25|5C|2E)", s, re.I)):
        return None
    parts = query.split("&") if query else []
    names = [p.split("=", 1)[0] for p in parts]
    # クエリの名前に符号化(%)があるものも対象外(復号すると同じ名前になりうる。`%76=` は `v=`。監査指摘 2026-10-21)
    if "" in parts or len(set(names)) != len(names) or any("%" in n or "+" in n for n in names):
        return None
    q = {n: [p.split("=", 1)[1] if "=" in p else ""] for n, p in zip(names, parts)}
    host_given, host = host, host.removeprefix("www.")
    seg = [x for x in path.split("/") if x]
    if host in SHORTENERS:
        return f"{host}/{seg[0]}" if len(seg) == 1 else None
    if host in ("x.com", "twitter.com", "mobile.x.com", "mobile.twitter.com"):
        sid = (seg[2] if len(seg) >= 3 and seg[1] in ("status", "statuses") else
               seg[2] if seg[:2] == ["i", "status"] and len(seg) >= 3 else
               seg[3] if seg[:3] == ["i", "web", "status"] and len(seg) >= 4 else "")
        if sid.isdigit():
            return f"x.com/i/status/{sid}"
        # アカウントのページはクエリの無い `x.com/<ハンドル>` だけ(`search?q=…` などの機能のページは対象外。監査指摘 2026-10-21)
        ok = (len(seg) == 1 and not parts and re.fullmatch(r"[A-Za-z0-9_]{1,15}", seg[0])
              and seg[0].lower() not in ("search", "hashtag", "home", "explore", "i", "intent", "share", "settings",
                                         "notifications", "messages", "login", "signup", "compose", "tos", "privacy"))
        return f"x.com/{seg[0].lower()}" if ok else None
    yt = host.removeprefix("m.")
    vid = ""
    if yt == "youtube.com":
        if seg == ["watch"]:
            vid = (q.get("v") or [""])[0]
        elif len(seg) == 2 and seg[0] in ("live", "shorts", "embed"):
            vid = seg[1]
    elif yt == "youtu.be" and len(seg) == 1:
        vid = seg[0]
    if vid:
        return f"youtube.com/watch?v={vid}" if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) else None
    if (host == "nicovideo.jp" or host.endswith(".nicovideo.jp")) and len(seg) == 2 and seg[0] == "watch":
        return f"nicovideo.jp/watch/{seg[1]}"
    qdoc = query_doc_key(s)
    if qdoc:
        return qdoc
    if host in ("docs.google.com", "drive.google.com"):
        for i, x in enumerate(seg):
            if i >= 1 and seg[i - 1] in ("d", "e", "folders") and len(x) >= 20:
                return host + "/" + "/".join(seg[:i + 1])
    if host == "forms.gle":
        return f"{host}/{seg[0]}" if len(seg) == 1 else None
    # 一般のページは書かれたまま: ホスト(`www.` も)・パス(末尾の / も、符号化も)・クエリ(並びも)を変えない。https だけ。
    # 落とすのは、どのサイトでも追跡の印でしかないクエリの項だけ
    if scheme != "https":
        return None
    keep = [p for p, n in zip(parts, names) if not LINK_NOISE_QUERY.match(n)]
    return host_given + path + ("?" + "&".join(keep) if keep else "")


def parse_source_table(text: str, p) -> dict:
    """判定表の本文を検査して dict にする。読むときも書くときも**同じ検査**を通す。"""
    import yaml
    _check_table_text(text, p)   # YAML が黙って潰す「同じキーの二重定義」は dict になる前に見る
    t = yaml.safe_load(text) or {}
    _check_table(t, p)
    return t


def source_type_table() -> dict:
    """`source_types.yml` を読む(ファイルが変わっていなければ前回の結果を使う)。**無ければ落とす。**

    以前は無いとき `{}` を返していたが、それだと表が消えた瞬間に全出典が
    既定へ落ちて素通りする(監査指摘の fail-open)。判定の根拠が無い状態で
    紙面を作らせないため、ここで止める。

    以前は「初回だけ読む」だった。合議(classify_sources)は同じプロセスの中で表へ書き足しては
    判定し直すので、古い表のまま「まだ表に無い」と判断して同じ動画 ID を2回足し、表が二重定義に
    なって次の付け直しが落ちた(実測 2026-09-18 02:40)。ファイルの同一性を毎回確かめ、
    変わっていたら読み直す。
    """
    global _ST_TABLE, _ST_KEY
    p = ROOT / "source_types.yml"
    try:
        st = p.stat()
    except FileNotFoundError:
        raise SystemExit(f"出典種別の判定表がない: {p}")
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    if _ST_TABLE is None or key != _ST_KEY:
        _ST_TABLE = parse_source_table(p.read_text(encoding="utf-8"), p)
        _ST_KEY = key
    return _ST_TABLE


def write_source_table(text: str, path=None) -> None:
    """判定表を書く唯一の入口。**検査に通った本文だけ**を、置き換えで書く。

    壊れた表(重複・二重定義・値域外)はディスクに届く前に SystemExit で止まる。表を書いたあとで
    別プロセス(付け直し・lint)が読んで初めて壊れに気づく、という順番を無くす。
    書いたら手元の読み込み結果を捨て、次の判定は必ず新しい表で行う。
    """
    global _ST_TABLE, _ST_KEY
    p = Path(path) if path else ROOT / "source_types.yml"
    parse_source_table(text, p)
    # 一時ファイルの名前は write_checked と同じ `.<名前>.<pid>.tmp`(.gitignore の対象)。取引の中断で書き手を止めたときに
    # 残っても作業ツリーを汚さない(取引の後始末も消す)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            if p.exists():      # 置き換えで権限を変えない(新規ファイルは umask で決まるため。監査指摘)
                os.fchmod(f.fileno(), p.stat().st_mode & 0o7777)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)
    _ST_TABLE, _ST_KEY = None, None


def err_tail(r) -> str:
    """失敗した子プロセスの言い分(stdout の `::error` 行、stderr の最後の行、無ければ stdout の最後の行)。"""
    err = [ln.strip() for ln in (r.stderr or "").splitlines() if ln.strip()]
    out = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    marks = [ln.split("::", 2)[-1] for ln in out if ln.startswith("::error")][:3]
    parts = marks + ([err[-1]] if err else []) + ([out[-1]] if out and not err else [])
    return " / ".join(p[:160] for p in parts) if parts else "(出力なし)"


def _snapshot(paths) -> dict[str, bytes | None]:
    """取引の開始時の中身を控える(無いファイルは None)。git の HEAD ではなく**開始時**へ戻すため:
    組版前の記事はまだ未追跡で、`git checkout` では戻らない(監査指摘)。"""
    out: dict[str, bytes | None] = {}
    for p in paths:
        out[str(p)] = p.read_bytes() if p.is_file() else None
    return out


def _restore(snap: dict[str, bytes | None], new_under: Path | None = None) -> str:
    """控えへ戻す。new_under を渡すと、その下で控えに無いファイル(取引の途中で増えたもの)も消す。
    戻せなかったファイルの名前を返す(空なら成功)。"""
    failed = []
    if new_under is not None:
        for p in sorted(new_under.glob("*.md")):
            if str(p) not in snap:
                try:
                    p.unlink()
                except OSError as e:
                    failed.append(f"{p.name}: {e}")
    for name, data in snap.items():
        try:
            p = Path(name)
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(data)
        except OSError as e:
            failed.append(f"{Path(name).name}: {e}")
    return " / ".join(failed)


def classify_retag_lint(date: str, posts_only: bool = False, lint_base: str = "origin/main",
                        timeout: int = 1800, lint: bool = True, require_parsers: bool = False) -> tuple[bool, str]:
    """判定表の更新(合議)→ 紙面の種別付け直し(→ lint)を**1つの取引**にする。

    どれかが失敗したら、この取引が触った判定表と記事を**開始時の中身**に戻す(表だけ進んで次の lint が
    赤くなる、部分的な付け直しが混ざる、を防ぐ)。戻り値は (成功したか, 失敗の理由)。戻すことにも
    失敗したら理由に「戻せない」を含める(呼び出し側は commit せずに終えること)。
    収集(候補 + 紙面の未確認。lint あり)と組版前(その号の記事の未確認だけ。posts_only。号スナップショットが
    まだ無いので lint は直後の組版に任せる: lint=False)が同じ取引を使う。
    成功が確定していない終わり方(子の失敗・時間切れ・例外・SIGTERM・SIGINT)は、**子をプロセスグループごと止めてから**
    開始時へ戻し、受けた中断はそのあとで元の扱いへ送り直す(2026-10-14 監査指摘: 表を書いた後・付け直しの前に親へ SIGTERM・SIGINT が
    来ると戻さずに終わり、出典の lint が赤く、次の工程の checkout も「未コミットの変更」で止まった)。開始時の中身は控え
    (TXN_JOURNAL)にも書き、親が SIGKILL などで後始末できずに死んでも、次の取引・次の工程の checkout が戻す(recover_classify_txn)。
    """
    back = recover_classify_txn()
    if back:
        print(back, flush=True)
        if "戻せない" in back:
            return False, back
        anomaly("classify", back)
    scripts = ROOT / "scripts"
    posts = ROOT / "docs" / "_posts"
    snap = _snapshot([ROOT / "source_types.yml"] + sorted(posts.glob("*.md")))
    _TXN.update(proc=None, signum=None, stray=[])
    # 中断を受け止めてから控えを書く(書いた直後・受け止める前に、工程の終了の例外で抜けて控えだけが残る、を無くす)
    prev = _arm_txn_signals()
    try:
        _write_txn_journal(snap, posts)
    except BaseException as e:      # 戻せる控えが無いまま表を書き換えない(まだ何も変えていない)。控えを消して終える
        (ROOT / TXN_JOURNAL).unlink(missing_ok=True)
        _disarm_txn_signals(prev)
        if _TXN["signum"] is not None:
            signal.raise_signal(_TXN["signum"])
        if not isinstance(e, OSError):
            raise
        return False, f"取引の控えを書けない({type(e).__name__}: {str(e)[:160]})。判定表と記事は変えずに終えた"
    ok, why, done = False, "", False
    txn_started = time.time()
    try:
        cmd = [sys.executable, str(scripts / "classify_sources.py"), "--date", date, "--apply"] + (["--posts-only"] if posts_only else [])
        if require_parsers:      # 当番の再実行: パーサの無い URL が残っていたら失敗(成功扱いにしない。監査指摘 r85)
            cmd.append("--require-parsers")
        # 各段は _txn_run(自前のセッションで起動し、時間切れ・中断は reap=stop_tree で子孫まで止める)。合議のモデルのセッションは
        # classify_sources.py の中で start_group 越しに起動する
        steps = [("合議", cmd, timeout), ("付け直し", [sys.executable, str(scripts / "retag_sources.py"), "--apply"], 600)]
        if lint:
            steps.append(("lint", [sys.executable, str(scripts / "lint.py"), "--base", lint_base], 600))
        for name, step, limit in steps:
            if _TXN["signum"] is not None:
                break
            r = _txn_run(step, limit)
            if name == "合議":
                print(r.stdout[-1200:], flush=True)
            elif name == "付け直し":
                tail = (r.stdout or "").strip().splitlines()[-1:] if r.stdout else []
                print("紙面の種別付け直し: " + (tail[0] if tail else f"exit {r.returncode}"), flush=True)
            if _TXN["signum"] is not None:
                break
            if name == "合議" and r.returncode == CLI_BLOCKED_EXIT:
                # 判定のモデルが認証切れ・利用上限で答えなかった(子が投稿済み)。判定の失敗ではないので、開始時へ戻して次回やり直す
                why = f"{CLI_STOPPED}({', '.join(adopt_cli_blocked(txn_started)) or '?'})。判定表と記事は開始時のまま、次の判定でやり直す"
                break
            if r.returncode != 0:
                why = f"{name} exit {r.returncode}: {err_tail(r)}"
                break
        else:
            ok = True
        done = True
    except (subprocess.TimeoutExpired, OSError) as e:   # 途中の timeout・起動失敗も取引の失敗
        why = f"{type(e).__name__}: {str(e)[:200]}"
        done = True
    finally:
        # ほかの例外(KeyboardInterrupt・工程のハンドラが投げる終了の例外など)で抜けるときも done は False のままで、ここで戻す。
        # 決める・戻す・ハンドラを戻すあいだは中断を保留する(戻す最中に届いた中断を落とさない。監査指摘 2026-10-14)。
        # 保留したものは、元のハンドラへ戻したあとで届く。決める時点で届いていれば、子が成功していても開始時へ戻す
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, TXN_SIGNALS) if prev else None
        pending = (set(signal.sigpending()) & set(TXN_SIGNALS)) if prev else set()
        signum = _TXN["signum"] if _TXN["signum"] is not None else (min(pending) if pending else None)
        keep = ok and done and signum is None and not _TXN["stray"]
        failed = "" if keep else _txn_rollback(snap, posts)
        if keep and _TXN["signum"] is not None:     # 保留の直前に届き、保留の後で Python のハンドラが走ったものも戻す
            keep, signum = False, _TXN["signum"]
            failed = _txn_rollback(snap, posts)
        if not failed:
            (ROOT / TXN_JOURNAL).unlink(missing_ok=True)    # 戻せなければ控えを残す(次の取引・checkout が戻し直す)
        _disarm_txn_signals(prev)
        if signum is not None:
            print(f"取引の途中でシグナル {signal.Signals(signum).name} を受けた。子を止めて判定表と記事を開始時へ戻した"
                  + (f"(戻せない: {failed[:200]})" if failed else ""), flush=True)
            if signum not in pending:
                signal.raise_signal(signum)      # 保留中に送る。保留を解くと、元の扱い(既定なら終了、組版なら途中終了の後始末)へ届く
        if old_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
    if keep:
        return True, ""
    if signum is not None:
        why = why or f"シグナル {signal.Signals(signum).name} で中断"
    if failed:
        return False, f"{why}。判定表と記事を戻せない({failed[:200]})"
    return False, why


# 進行中の取引の子プロセス、取引の途中で受けた中断のシグナル(_arm_txn_signals のハンドラが書く)、止めきれなかった子孫
_TXN: dict = {"proc": None, "signum": None, "stray": []}
TXN_JOURNAL = Path("metrics") / "work" / "classify-txn.json"   # 取引の開始時の中身の控え(ROOT から。Git 管理外)
TXN_CHILD = Path("metrics") / "work" / "classify-txn.child.json"   # 動いている取引の子(pid・起動時刻)。親が死んでも次の起動が止める
TXN_SIGNALS = (signal.SIGTERM, signal.SIGINT)
# 取引の子の入口: 標準入力から合図("1")を受けてから child_guard(孤児の受け皿)に置き換わり、本来のコマンドを起動する(pid と起動時刻は
# 変わらない)。合図が来なければ動かない。child_guard は、コマンドの子孫が別のセッションに抜けて環境を読めなくし、親が先に終わっても
# 自分の子として受け、コマンドが終わったら全部止めてから終わる。止めるときも、受けた孤児が親子関係で辿れる(2026-10-21 取り込みの
# 監査: 環境を読めない孤児の道具が、取引を戻したあとの判定表を書き直した)
_TXN_GATE = ("import os, sys\n"
             "if sys.stdin.buffer.read(1) != b'1':\n    sys.exit(125)\n"
             "os.execv(sys.executable, [sys.executable] + sys.argv[1:])\n")
_TXN_GUARD = Path(__file__).resolve().parent / "child_guard.py"


def _write_txn_child(child: Path, pid: int, started: str, mark: str = "") -> None:
    """取引の子の記録(pid・起動時刻・取引の印)を書く(一時ファイル → 置き換え)。書けなければ OSError(段の失敗にする)。"""
    tmp = child.with_name(f".{child.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"pid": pid, "started": started, "mark": mark}), encoding="utf-8")
        os.replace(tmp, child)
    finally:
        tmp.unlink(missing_ok=True)


def _txn_stop_child() -> None:
    """取引の子をプロセスグループ・子孫・印ごと止める(止めずに戻すと、戻した後で子が判定表を書き直す)。子(child_guard)を先に
    落とすと、受けていた孤児が init へ移って辿れなくなるので、凍らせてから子孫を辿って止める(stop_tree)。"""
    p = _TXN.get("proc")
    if p is None or p.poll() is not None:
        return
    try:
        stop_tree(p.pid, root=p.pid, mark=getattr(p, "session_mark", None))
    except Exception:       # noqa: BLE001  止め方の失敗でも、下でグループだけは落とす
        traceback.print_exc()
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        try:
            p.kill()
        except OSError:
            pass


def _txn_run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    """取引の1段を子プロセスで走らせる。子は自分のプロセスグループで起動し、時間切れ・例外では**グループごと止めて回収してから**
    抜ける。中断のシグナルはハンドラが子を止める(communicate が子の終わりで戻る)。"""
    # 子は、親が子の記録(TXN_CHILD)を書き終えて合図を送るまで動き出さない(_TXN_GATE)。記録の無い子を残さない: 書けなければ
    # 子を動かさずに止めて段を失敗にし、親が合図の前に死ねば子は動かずに終わる(監査指摘 2026-10-14)
    # 子には取引の印(SESSION_ENV)を渡す。子が start_group で別のセッションに起動したモデル・道具も印を受け継ぐので、中断・親の死の
    # あとの後始末が印で見つけて止める(2026-10-21 取り込みの監査: セッションだけを見ると、合議のモデルが残って戻したあとに書いた)
    import uuid
    mark = uuid.uuid4().hex
    env = dict(os.environ)
    env[SESSION_ENV] = f"{env.get(SESSION_ENV, '')} {mark}".strip()
    # 段は child_guard を2重にして走らせる(外側 → 内側 → 段のコマンド)。内側だけが SIGKILL で落ちても、受けていた孤児は外側へ移り、
    # 外側が全部止めてから終わる。外側だけが落ちても、内側は取引の子のセッションに残り、受けた孤児ごと親子関係で辿れる。
    # 取引を持つ工程を受け皿にすると、ほかのスレッドの子と取り違えた(2026-10-21 取り込みの監査)
    p = subprocess.Popen([sys.executable, "-c", _TXN_GATE, str(_TXN_GUARD), sys.executable, str(_TXN_GUARD), *cmd], cwd=ROOT,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, env=env)
    _TXN["proc"] = p
    p.session_mark = mark       # 時間切れ・例外の reap(stop_tree)も印で探す
    started = _proc_started(p.pid)
    child = ROOT / TXN_CHILD
    try:
        if _TXN["signum"] is not None:      # 起動の直前に中断を受けていた
            _txn_stop_child()
        _write_txn_child(child, p.pid, started, mark)
        out, err = p.communicate(input="1", timeout=timeout)
    except subprocess.TimeoutExpired:
        out, err = reap(p)
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
    except BaseException:
        reap(p)
        raise
    finally:
        _TXN["proc"] = None
        # 子が先に終わっても、子が起こした孫(同じセッション・印を受け継いだ別のセッション)が残っていれば止める。成功した段でも残さない
        stray = _stop_session(p.pid, started, mark=mark)
        if stray:
            _TXN["stray"] = stray
        else:
            child.unlink(missing_ok=True)       # 止めきれなければ記録を残す(次の起動がまた止める)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def _arm_txn_signals() -> dict:
    """取引のあいだ SIGTERM・SIGINT を受け止める。ハンドラは子を止めて印を付けるだけで、戻すのは取引の後始末(どの行で
    受けても、戻す処理の途中で例外にならない)。戻り値は元のハンドラ(_disarm_txn_signals で戻す)。無視している・Python の外で
    決めたシグナル、メインスレッドでないときは差し替えない。"""
    import threading
    if threading.current_thread() is not threading.main_thread():
        return {}
    prev = {}
    for sig in TXN_SIGNALS:
        h = signal.getsignal(sig)
        if h in (signal.SIG_IGN, None):
            continue

        def on_signal(signum, frame):
            if _TXN["signum"] is None:
                _TXN["signum"] = signum
            _txn_stop_child()
        prev[sig] = h
        signal.signal(sig, on_signal)
    return prev


def _disarm_txn_signals(prev: dict) -> None:
    for sig, h in prev.items():
        signal.signal(sig, h)


def _txn_rollback(snap: dict[str, bytes | None], posts: Path) -> str:
    """子を止め、判定表と記事を開始時へ戻し、止めた子が書きかけた表の一時ファイルを消す。戻せなかったものの説明を返す。
    止めきれない子孫が残っていれば、戻しても書き直されうるので「戻せない」扱いにする(控えを残し、次の起動がまた止めて戻す)。"""
    _txn_stop_child()
    stray = [q for q in _TXN.get("stray") or [] if _proc_started(q)]
    if stray:
        return f"止めきれない子孫が残っている(pid {', '.join(map(str, stray[:5]))})"
    failed = _restore(snap, posts)
    for t in ROOT.glob(".source_types.yml.*.tmp"):
        try:
            t.unlink()
        except OSError as e:
            failed = (failed + " / " if failed else "") + f"{t.name}: {e}"
    return failed


def _proc_started(pid: int) -> str:
    """プロセスの起動時刻(/proc/<pid>/stat の starttime)。終わっていれば(ゾンビも)""。pid の使い回しと見分けるため。"""
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return "" if f[0] == "Z" else f[19]
    except (OSError, IndexError):
        return ""


def _session_members(sid: int, started: str = "") -> list[int]:
    """セッション sid に残っているプロセス(ゾンビは除く)。sid の pid を別のプロセスが使い直していれば(起動時刻が違う)、
    そのセッションは取引の子のものではないので空(Linux はセッションに属するプロセスが残るあいだ、その pid を使い直さない)。"""
    if started:
        cur = _proc_started(sid)
        if cur and cur != started:
            return []
    out = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for d in names:
        if not d.isdigit():
            continue
        try:
            f = Path(f"/proc/{d}/stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(f) > 3 and f[3] == str(sid) and f[0] != "Z":
            out.append(int(d))
    return out


def _txn_members(sid: int, started: str = "", mark: str = "") -> list[int]:
    """取引の子のセッション sid に残っているプロセス(段の child_guard の外側・内側と、内側が受けた孤児)と、取引の印 mark を環境に
    受け継いだプロセス(合議のモデル・道具は start_group で別のセッションに起動されるので、セッションでは見つからない)と、
    それらの子孫(ゾンビ・自分は除く)。"""
    found = set(_session_members(sid, started)) if sid > 1 else set()
    table = _proc_table()
    if table:
        if mark:
            found |= _proc_marked(table, mark)
        kids: dict[int, list[int]] = {}
        for q, (pp, _, _) in table.items():
            kids.setdefault(pp, []).append(q)
        todo = list(found)
        while todo:
            for c in kids.get(todo.pop(), []):
                if c not in found:
                    found.add(c)
                    todo.append(c)
        found = {q for q in found if q in table and table[q][2] not in ("Z", "X")}
    return sorted(found - {os.getpid()})


def _stop_session(sid: int, started: str = "", wait: float = 10.0, mark: str = "") -> list[int]:
    """取引の子のセッション(子は start_new_session で起動するので、子と、子が起こした孫は setsid しない限り同じセッション)と、
    取引の印 mark を受け継いだプロセスとその子孫を、残らなくなるまで止める。直接の子が先に終わっていても、残った孫を止める
    (止めずに戻すと、戻した後で孫が判定表を書き直す。監査指摘 2026-10-14)。合議のモデルは start_group で別のセッションに
    起動されるので、印でも探す(2026-10-21 取り込みの監査: 中断で子のグループだけを落とすと、別のセッションのモデルが残り、
    戻したあとに判定表を書き直した)。戻りは止めきれなかった pid。"""
    # 先に全員を凍らせてから止める(子(child_guard)を先に落とすと、受けていた孤児が init へ移り、親子関係で辿れなくなる)
    end = time.monotonic() + wait
    left = _txn_members(sid, started, mark)
    while left:
        frozen: set[int] = set()
        for _ in range(50):
            new = set(left) - frozen
            if not new:
                break
            for q in new:
                try:
                    os.kill(q, signal.SIGSTOP)
                except OSError:
                    pass
            frozen |= new
            left = _txn_members(sid, started, mark)
        for q in frozen | set(left):
            try:
                os.kill(q, signal.SIGKILL)
            except OSError:
                pass
        if time.monotonic() > end:
            break
        time.sleep(0.05)
        left = _txn_members(sid, started, mark)
    return left


def _stop_txn_orphan() -> list[int]:
    """親が死んで残った取引の子と孫(子のセッション)を止め、止まるまで待つ。戻した後で書き直させない。
    直接の子が終わっていても孫は残りうるので、子の生死に関わらずセッションを見る。戻りは止めきれなかった pid。"""
    c = ROOT / TXN_CHILD
    try:
        rec = json.loads(c.read_text(encoding="utf-8"))
        pid, started, mark = int(rec.get("pid") or 0), str(rec.get("started") or ""), str(rec.get("mark") or "")
    except (OSError, ValueError, TypeError, AttributeError):
        return []
    left = _stop_session(pid, started, mark=mark) if pid or mark else []
    if not left:
        c.unlink(missing_ok=True)
    return left


def _txn_place() -> tuple[str, str]:
    """取引をした作業ツリーの (ブランチ, HEAD)。Git でなければ ("", "")。"""
    return (git("branch", "--show-current", check=False).stdout.strip(),
            git("rev-parse", "--verify", "-q", "HEAD", check=False).stdout.strip())


def _write_txn_journal(snap: dict[str, bytes | None], posts: Path) -> None:
    """取引の開始時の中身を控えに書く(一時ファイル → fsync → 置き換え)。子を起動する前に書き終える。"""
    import base64
    branch, head = _txn_place()
    body = {"pid": os.getpid(), "started": _proc_started(os.getpid()), "branch": branch, "head": head,
            "posts": str(posts.relative_to(ROOT)),
            "files": {str(Path(k).relative_to(ROOT)): (None if v is None else base64.b64encode(v).decode("ascii"))
                      for k, v in snap.items()}}
    j = ROOT / TXN_JOURNAL
    j.parent.mkdir(parents=True, exist_ok=True)
    tmp = j.parent / f".{j.name}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(body))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, j)
    finally:
        tmp.unlink(missing_ok=True)


def classify_txn_settled(job: str) -> bool:
    """確定(commit)してよいか。出典の判定の取引の控えが残っていれば、確定の前に戻す。戻せない・控えを書いた工程がまだ動いている
    なら False(確定しない)。先に commit すると HEAD が控えと食い違い、判定表と記事を二度と戻せず、次の工程の checkout が止まる
    (2026-10-14 取り込みの監査: 当番の取り直しの後始末、戻しに失敗した後の収集・組版の後始末)。"""
    if not (ROOT / TXN_JOURNAL).exists():
        return True
    back = recover_classify_txn()
    if back:
        print(back, flush=True)
    if "戻せない" in back or (ROOT / TXN_JOURNAL).exists():
        notify(job, "出典の判定の取引を戻せないまま残っているので確定しない(先に確定すると戻せなくなる): "
                    + (back or "控えを書いた工程がまだ動いている"), ok=False)
        return False
    if back:
        anomaly(job, back)
    return True


def recover_classify_txn() -> str:
    """前の取引が後始末できずに終わっていたら(親が SIGKILL・電源断で死んだ)、控え(TXN_JOURNAL)から判定表と記事を開始時へ戻す。
    戻り値は説明(何もしなければ "")。戻せなければ「戻せない」を含める(呼び出し側は先へ進まない)。
    - 控えを書いたプロセスがまだ動いていれば触らない(その取引の途中)
    - ブランチか HEAD が取引の開始時から変わっていれば戻さない(別の号・その後の commit を古い中身で上書きしない)。控えは脇へ退ける"""
    import base64
    j = ROOT / TXN_JOURNAL
    if not j.exists():
        return ""
    try:
        rec = json.loads(j.read_text(encoding="utf-8"))
        files = {str(ROOT / k): (None if v is None else base64.b64decode(v)) for k, v in rec["files"].items()}
        posts = ROOT / rec["posts"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return f"前の出典の判定の取引の控えが読めない({type(e).__name__}: {str(e)[:120]})。判定表と記事を戻せない({j})"
    pid = int(rec.get("pid") or 0)
    if pid and pid != os.getpid() and rec.get("started") and _proc_started(pid) == rec.get("started"):
        return ""
    left = _stop_txn_orphan()
    if left:
        return (f"前の出典の判定の取引(pid {pid})の子孫が止まらない(pid {', '.join(map(str, left[:5]))})。"
                "判定表と記事を戻せない(戻しても書き直されうる)")
    if tuple(_txn_place()) != (rec.get("branch", ""), rec.get("head", "")):
        aside = j.with_name(f"classify-txn.stale-{int(time.time())}.json")
        os.replace(j, aside)
        return (f"前の出典の判定の取引の控えが残っていたが、ブランチか HEAD が開始時({rec.get('branch')} {str(rec.get('head'))[:10]})と"
                f"違うので戻さなかった(控えは {aside.name} へ退けた)")
    failed = _txn_rollback(files, posts)
    if failed:
        return f"前の出典の判定の取引(pid {pid})が途中で終わっていた。判定表と記事を戻せない({failed[:200]})"
    j.unlink(missing_ok=True)
    return f"前の出典の判定の取引(pid {pid})が途中で終わっていたので、判定表と記事を開始時へ戻した"


def dedupe_source_table(path=None) -> list[str]:
    """判定表の**同じ節の中の二重行**を除く(先に書いてあるほうを残す)。戻り値は除いた行。

    source_types.yml は `merge=union` で結合するので、2つのブランチ(main と次号)がそれぞれ合議で
    同じ相手を足すと、merge 後に同じ行が2つ並ぶ。_check_table はそれを重複として止める
    (実測 2026-09-17: 発行後の main 取り込みで 5 行が二重になり selfcheck が赤)。
    merge の直後に必ずこれを通す。
    """
    p = Path(path) if path else ROOT / "source_types.yml"
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    out, seen, section, label, removed = [], set(), None, None, []
    link_pos: dict[tuple, tuple[int, str]] = {}      # link_types のキー → (out の位置, 値)
    for ln in lines:
        m_top = re.match(r"^([a-z_]+):", ln)
        if m_top:
            section, label = m_top.group(1), None
        m_label = re.match(r"^  (\S+):\s*$", ln)          # x_accounts / video_ids / video_channels の種別
        if m_label:
            label = m_label.group(1)
        m_item = re.match(r"^\s+-\s+(\S+)", ln)
        m_kv = re.match(r"^  (\S+?):\s*(\S+)", ln)         # path_types / suffix_types のキー: 値
        if section == "link_types" and m_kv:
            # リンク元から引き継いだ種別は、どちらも合議で関係を確かめた値。同じリンク先が2つのブランチで別の値になったら
            # 強いほうを残す(種別 = max の決まり。競合として表の検査で止めると組版・付け直しが止まる。監査指摘 2026-10-21)
            k = ("link_types", m_kv.group(1))
            if k in link_pos:
                i, v = link_pos[k]
                if src_rank(m_kv.group(2)) < src_rank(v):
                    removed.append(out[i].rstrip("\n"))
                    out[i], link_pos[k] = ln, (i, m_kv.group(2))
                else:
                    removed.append(ln.rstrip("\n"))
                continue
            link_pos[k] = (len(out), m_kv.group(2))
            out.append(ln)
            continue
        if section and (m_item or m_kv):
            # 同じ節・**同じ種別**・同じ値のものだけが二重。種別や値が違う同一キーは分類の競合なので
            # ここでは消さず、_check_table に止めさせる(監査指摘)
            # 動画 ID・文書や短縮の ID は大小文字を区別する(同一視すると別の ID の行を二重として消す。監査指摘 2026-10-14)
            if m_item:
                item = m_item.group(1).rstrip("/")
                key = (section, label, item if section == "video_ids" else item.lower(), None)
            else:
                key = (section, None, path_type_norm(m_kv.group(1)) if section == "path_types" else m_kv.group(1)
                       if section == "link_types" else m_kv.group(1).rstrip("/").lower(), m_kv.group(2))
            if key in seen:
                removed.append(ln.rstrip("\n"))
                continue
            seen.add(key)
        out.append(ln)
    if removed:
        p.write_text("".join(out), encoding="utf-8")
    return removed


def _check_table_text(text: str, p) -> None:
    """dict になる前の**字面**の検査: 対応(path_types / suffix_types)の同じキーが2回書かれていないか。

    YAML はマッピングの重複キーを黙って後勝ちにするので、dict を見る _check_table には届かない
    (監査指摘: 値の違う同一パスが両方残っても検出できずに片方が消える)。正規化(末尾 / と大小)で比べる。
    """
    # 行の正規表現ではなく YAML の構文木(compose)で見る。インデント幅・キーとコロンの間の空白・引用符・
    # flow mapping のどれで書かれても、YAML が解釈したキーそのもので比べる(監査指摘)
    import yaml
    # アンカー・エイリアス(&x / *x)は判定表では使わない。compose はエイリアスを元ノードに解決するので
    # 行番号が元の位置になり、二重定義の位置を正しく示せない(監査指摘)。字句の段階で拒否する
    for tok in yaml.scan(text):
        if isinstance(tok, yaml.AnchorToken):
            raise SystemExit(f"{p} の {tok.start_mark.line + 1} 行目: 判定表でアンカー(&{tok.value})は使えない")
        if isinstance(tok, yaml.AliasToken):
            raise SystemExit(f"{p} の {tok.start_mark.line + 1} 行目: 判定表でエイリアス(*{tok.value})は使えない")
    root = yaml.compose(text)
    if not isinstance(root, yaml.MappingNode):
        return
    # 同名の節が2回書かれていたら、それ自体を止める(YAML は後勝ちで前の節ごと消す。監査指摘)
    tops: dict[str, int] = {}
    for k_node, _v in root.value:
        name, line = str(getattr(k_node, "value", "")), k_node.start_mark.line + 1
        if name in tops:
            raise SystemExit(f"{p}: 節 {name} が {tops[name]} 行目と {line} 行目に二重に書かれている(YAML は後勝ちで前を消す)")
        tops[name] = line
    for k_node, v_node in root.value:
        section = getattr(k_node, "value", None)
        if section not in ("path_types", "suffix_types", "link_types") or not isinstance(v_node, yaml.MappingNode):
            continue
        seen: dict[str, int] = {}
        for kk, _vv in v_node.value:
            # link_types のキーは link_key の正規形そのもの(ID の大小文字を区別する)
            key = (path_type_norm(getattr(kk, "value", "")) if section == "path_types" else str(getattr(kk, "value", ""))
                   if section == "link_types" else str(getattr(kk, "value", "")).rstrip("/").lower())
            line = kk.start_mark.line + 1
            if key in seen:
                raise SystemExit(f"{p} の {section}: {kk.value} が {seen[key]} 行目と {line} 行目に二重に書かれている"
                                 "(YAML は黙って後勝ちにする。1つにすること)")
            seen[key] = line


def _check_table(t: dict, p) -> None:
    """同じ相手が2つの種別に載っていないか。

    表が育つほど起きやすい。判定は上から順に最初に当たったものを採るので、
    重複しても**動いてしまう**ぶん気づけない(実測: tcg-supply-navi.com が
    二次情報と当事者の両方に載ったまま通っていた。監査で見つかった)。
    """
    import collections
    seen = collections.defaultdict(list)
    for key in ("official_domains", "semi_official_domains", "press_domains",
                "secondary_domains", "fan_domains", "party_domains", "performer_domains",
                "official_paths", "semi_official_paths", "party_paths"):
        for v in t.get(key) or []:
            seen[v].append(key)
    for group in ("x_accounts", "x_posts", "video_ids", "video_channels"):
        for label, names in (t.get(group) or {}).items():
            for v in names or []:
                v = str(v)
                # 判定器は X アカウントを小文字化して比べるので、検査側も揃える。
                # 揃えないと `imas_official` と `IMAS_OFFICIAL` を別種別に登録できてしまい、
                # 検査を通ったうえで先に載っているほうが勝つ(監査指摘)
                key = v.lower() if group == "x_accounts" else v
                seen[f"{group}:{key}"].append(f"{group}/{label}")
    dup = {v: ks for v, ks in seen.items() if len(ks) > 1}
    if dup:
        raise SystemExit(f"{p} で種別が重複している: "
                         + " / ".join(f"{v}({'・'.join(ks)})" for v, ks in sorted(dup.items())))
    # path_types(パス → 種別): 値域、正規化後の重複、親子の prefix が別種別で重なる競合を検査する
    # (記載順で最初に当たる方式なので、競合すると順序依存になる。監査指摘)
    pt = t.get("path_types") or {}
    if not isinstance(pt, dict):
        raise SystemExit(f"{p} の path_types はパス → 種別の対応でなければならない")
    allowed = {"公式", "準公式", "当事者", "演者", "報道", "ファン", "二次情報"}
    norm: dict[str, str] = {}
    for k, v in pt.items():
        key = path_type_norm(k)
        if str(v) not in allowed:
            raise SystemExit(f"{p} の path_types: {k} の種別 {v!r} は使えない({'/'.join(sorted(allowed))})")
        if "/" not in key or key.startswith(("http://", "https://")):
            raise SystemExit(f"{p} の path_types: {k} は host/path の形で書く(scheme 無し)")
        if key in norm:
            raise SystemExit(f"{p} の path_types: {k} が重複している")
        norm[key] = str(v)
    # 親子の競合は**全パス分類群を横断して**見る。official_paths → semi_official_paths → party_paths →
    # path_types の順に判定するので、先行群の親と後続群の子が別種別だと子は永遠に当たらない(監査指摘)
    families = [("official_paths", "公式"), ("semi_official_paths", "準公式"), ("party_paths", "当事者")]
    index: list[tuple[str, str, str]] = []   # (正規化キー, 種別, 群)
    for fam, typ in families:
        for v in t.get(fam) or []:
            index.append((path_type_norm(v), typ, fam))
    index += [(k, v, "path_types") for k, v in norm.items()]
    for a, ta, fa in index:
        for b, tb, fb in index:
            if a != b and b.startswith(a + "/") and ta != tb:
                raise SystemExit(f"{p}: {fa} の {a}({ta}) と {fb} の {b}({tb}) が親子で種別が違う"
                                 "(先に当たる親が勝ち、子の指定は届かない)")
    # link_types(リンク先1つ → リンク元から引き継いだ種別): 値域と、キーが link_key の正規形であること(違う形だと当たらない)。
    # 完全一致で引き、リンク先そのものの種別と強いほうを採るので、親子の競合は無い
    lt = t.get("link_types") or {}
    if not isinstance(lt, dict):
        raise SystemExit(f"{p} の link_types はリンク先 → 種別の対応でなければならない")
    for k, v in lt.items():
        if not isinstance(k, str):
            raise SystemExit(f"{p} の link_types: {k!r} のキーが文字列でない(YAML が数・真偽値として読んだ)")
        if str(v) not in allowed:
            raise SystemExit(f"{p} の link_types: {k} の種別 {v!r} は使えない({'/'.join(sorted(allowed))})")
        if link_key("https://" + str(k)) != str(k):
            raise SystemExit(f"{p} の link_types: {k} はリンク先のキーの形でない({link_key('https://' + str(k))} と書く)")


# 投稿者の無い X の投稿 URL(`x.com/i/status/<ID>`・`i/web/status/<ID>`)。種別はアカウントで決まるので、
# 投稿者を引かないと判定できない(2026-10-03: Grok の trend 面がこの形で書き、紙面に未確認として残った)
X_ANON_PATH = re.compile(r"i/(?:web/)?status/(\d+)(?:/.*)?", re.I)
X_ANON_POST = re.compile(r"https?://(?:(?:www|mobile)\.)?(?:x|twitter)\.com/i/(?:web/)?status/(\d+)(?:[/?#].*)?", re.I)
X_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")


def x_post_author(post_id: str, fetch=None) -> str:
    """X の投稿 ID から投稿者のハンドルを引く(X の oEmbed)。引けなければ ""。"""
    import urllib.parse
    import urllib.request

    def get(url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ImasNews/1.0)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    meta = {}
    try:
        q = urllib.parse.urlencode({"url": f"https://x.com/i/status/{post_id}", "omit_script": "1"})
        meta = json.loads((fetch or get)(f"https://publish.twitter.com/oembed?{q}"))
        au = urllib.parse.urlparse(str(meta.get("author_url") or ""))
        handle = au.path.strip("/") if (au.hostname or "").removeprefix("www.") in ("x.com", "twitter.com") else ""
    except Exception as e:
        print(f"X の投稿者を引けない: {post_id} {type(e).__name__}: {str(e)[:120]}", flush=True)
        return ""
    if not X_HANDLE.fullmatch(handle):
        print(f"X の投稿者を引けない: {post_id} author_url={meta.get('author_url')!r}", flush=True)
        return ""
    return handle


def classify_source(url: str) -> str:
    """出典 URL の種別。リンク先そのものの種別(own_source_type)と、リンク元から引き継いだ種別(判定表 link_types。
    合議がリンク元の告知と関係していると確かめたものだけが載る)の**強いほう**(編集長 2026-10-21: 公式の投稿・ページから
    リンクで行った先は公式、準公式からなら準公式。リンク先そのものが上位なら上位)。"""
    t = source_type_table()
    own = own_source_type(url, t)
    lt = t.get("link_types") or {}
    k = link_key(url) if lt else None
    got = str(lt.get(k)) if k and k in lt else None
    return got if got and src_rank(got) < src_rank(own) else own


def own_source_type(url: str, t: dict | None = None) -> str:
    """出典 URL **そのもの**から種別を決める(リンク元から引き継いだ種別は見ない)。**収集役の自己申告は使わない。**

    以前は種別が申告任せで、lint も「記事の src == 出典の最弱種別」という
    ラベル同士の照合しかしていなかった。攻略サイトを「公式」と申告すれば
    最弱も「公式」になって一致するため、過大表示を1件も防げなかった
    (実測 26件・18記事)。判定の根拠を URL 側に移す。

    表に載っていなければ **未確認**。「当事者が最も多いから」を理由に既定を
    当事者にしていたが、それでは未知の個人ブログもファン動画も一次発信を
    名乗れてしまう。多いことは判定の根拠にならない(監査指摘)。
    """
    import posixpath
    import urllib.parse
    t = source_type_table() if t is None else t
    u = urllib.parse.urlparse(url if "//" in url else "//" + url)
    # netloc をそのまま切ると userinfo をホストと取り違える。
    # `https://idolmaster-official.jp:443@evil.example/...` が公式になった(監査指摘)。
    # hostname は userinfo とポートを落とし、小文字にして返す
    # `\` はブラウザ(WHATWG)では `/` と同じ扱いだが urlparse は区切りにしない。
    # `https://evil.example\@x.com/...` を urlparse は x.com と読み、
    # ブラウザは evil.example へ繋ぐ。**解釈が割れる URL は判定しない**(監査指摘)。
    # スキームとポートも確かめる。`javascript://idolmaster-official.jp/...` は
    # 公式サイトへの参照ではないし、`x.com:443.evil` はポートが壊れている
    if "\\" in u.netloc or u.scheme not in ("", "http", "https"):
        return "未確認"
    try:
        u.port
    except ValueError:
        return "未確認"
    host = (u.hostname or "").removeprefix("www.")
    # `.../idolmaster-tours/../other` が公式になっていたので、前方一致の前に畳む。
    # `%2e%2e` のように符号化された `..` も畳めるよう、**ドットだけ**を先に復号する。
    # パス全体を復号すると `%2F` まで区切りになってしまい、
    # `x.com/zutapoke%2F..%2Fimas_official/status/123` が公式になる。
    # `%2F` は URL の解決では区切りではないので、復号してはいけない(監査指摘)
    raw = re.sub(r"%2e", ".", u.path, flags=re.I)
    path = f"{host}{posixpath.normpath(raw) if raw else ''}"

    # 1. ドメインの一部だけで決まるもの。作品公式サイトはドメインを問わず公式、
    #    多数の利用者が同居するプラットフォーム(github.com・楽天市場)は特定の主体だけ当事者。
    #    **前方一致は区切りまで見る。**素の startswith だと
    #    `.../Imas_Daily_Newsletter` のような別物まで通る(監査指摘)
    def under(pre: str) -> bool:
        base = pre.rstrip("/")          # 表の末尾スラッシュの有無で結果を変えない
        return path.rstrip("/") == base or path.startswith(base + "/")

    for pre in t.get("official_paths") or []:
        if under(pre):
            return "公式"
    # 多数のレーベルが同居する配信リンク(lnk.to など)は、パスのレーベル記号で決まる。
    # `lnk.to/LZC-` のように**品番の頭**で当てるので、ここだけは区切りを見ない素の前方一致
    # (区切りまで見る under() では `LZC-3650` が `LZC` に当たらない)
    for pre in t.get("semi_official_paths") or []:
        if path.startswith(pre):
            return "準公式"
    for pre in t.get("party_paths") or []:
        if under(pre):
            return "当事者"
    # プラットフォーム上のアカウント・チャンネル・作品ページ(tiktok.com/@…, ch.nicovideo.jp/…,
    # manga.nicovideo.jp/comic/…)は、パス単位で種別が決まる。合議が足す(path_types: パス → 種別)
    # クエリの id で決まる文書(Microsoft Forms)は、その文書のキーとの完全一致だけ(パスの前方一致ではどの文書にも当たる)
    qdoc = query_doc_key(url)
    for pre, typ in (t.get("path_types") or {}).items():
        if (str(pre).strip().rstrip("/") == qdoc) if qdoc else under(str(pre)):
            return str(typ)
    # 共有サービス(短縮 URL・フォーム・メールフォーム)は、短縮・文書・アカウント1つずつの登録(上の path_types)にだけ当てる。
    # ドメインの一覧に載っていても使わない(t.co の丸ごと登録で、どこへの短縮も公式になった。監査指摘 2026-10-14)
    if shared_host(host):
        return "未確認"

    # 2. 動画・生放送は**投稿者**で決まる。ドメインでは決まらない。
    #    同じ youtube.com に公式チャンネルの PV とレーベルの試聴動画が混ざる。
    #    ホストの一致もドット境界まで見る。素の endswith では
    #    `notyoutube.com` が youtube.com として通る(監査指摘)
    def is_host(*names: str) -> bool:
        return any(host == n or host.endswith("." + n) for n in names)

    # パスの**形**まで見る。`v=` だけを見ていたため
    # `youtube.com/@attacker?v=<公式の動画ID>` が公式になり、次に前だけを見たため
    # `youtube.com/watch/not-a-video?v=...` が公式になった(いずれも監査指摘)。
    # 動画1本を指す形にだけ当てる
    seg = [s for s in path[len(host):].split("/") if s]
    vid = ""
    if is_host("youtube.com"):
        if len(seg) == 1 and seg[0] == "watch":
            vid = urllib.parse.parse_qs(u.query).get("v", [""])[0]
        elif len(seg) == 2 and seg[0] in ("live", "shorts", "embed"):
            vid = seg[1]
    elif is_host("youtu.be") and len(seg) == 1:
        vid = seg[0]
    elif is_host("nicovideo.jp") and len(seg) == 2 and seg[0] == "watch":
        vid = seg[1]
    if vid:
        for label, ids in (t.get("video_ids") or {}).items():
            if vid in set(ids):
                return label
        return "未確認"

    # 3. X も**アカウント**で決まる。ただしパスの形も見る。
    #    先頭セグメントを無条件にアカウント名として読むと、
    #    `x.com/<公式アカウント>/not-a-status` のような別物まで公式になる(監査指摘)。
    #
    #    許す形は実データ(候補+紙面 1742件)に出てくる4つだけ:
    #      <acct>                     アカウントページ
    #      <acct>/status/<数字>       投稿。写真や動画の枝(/photo/1 等)が付くことがある
    #      <acct>/article/<数字>      X の記事機能。公式アカウントが使っている
    #      i/trending/<数字>          アカウントではない(トレンド)。これは弾く
    if host in ("x.com", "twitter.com", "mobile.x.com", "mobile.twitter.com"):
        # 形を見るのは**正規化したパス**に対して行う。`//` や `..` を含む URL は
        # X 上では同じ投稿を指すので、潰したうえで判定するのが実態に合う
        seg = [s for s in path[len(host):].split("/") if s]
        ok = (len(seg) == 1
              or (len(seg) >= 3 and seg[1] in ("status", "article") and seg[2].isdigit()))
        # `x.com/i/...` はアカウント名ではなく X 自身の機能への入口
        acct = seg[0].lower() if ok and seg[0].lower() != "i" else ""
        # `i/status/<ID>` は投稿者の無い投稿 URL。アカウントでは引けないので、投稿者を引いて種別が決まった
        # 投稿 ID の表(x_posts。classify_sources が機械で足す)で引く
        m = X_ANON_PATH.fullmatch("/".join(seg))
        if m:
            for label, ids in (t.get("x_posts") or {}).items():
                if m.group(1) in {str(i) for i in ids or []}:
                    return label
        for label, accounts in (t.get("x_accounts") or {}).items():
            if acct and acct in {a.lower() for a in accounts}:
                return label
        return "未確認"

    # 4. それ以外はドメイン。**完全一致を先に見る。**
    #    公式ポータル配下でも gakuen-label.(公式レーベル)は準公式なので、
    #    末尾一致より個別指定が勝たないと誤って公式になる(監査指摘)
    # 演者(出演者の所属事務所のサイト)もドメインで持つ(合議が答えてよい種別。監査指摘 2026-10-14: 保存先が無く KeyError)
    for key, label in (("official_domains", "公式"), ("semi_official_domains", "準公式"),
                       ("press_domains", "報道"), ("secondary_domains", "二次情報"),
                       ("discovery_only_domains", "二次情報"),
                       ("fan_domains", "ファン"), ("party_domains", "当事者"), ("performer_domains", "演者")):
        if host in set(t.get(key) or []):
            return label
    # 5. 公式ポータル配下の特設サイト(号ごとに増える)。個別指定に当たらなかったものだけ
    for suf in t.get("official_suffixes") or []:
        if host.endswith(suf):
            return "公式"
    # 6. 種類でまとめて決まるもの。自治体(.lg.jp)や政府機関(.go.jp)は、
    #    どこであれコラボ・寄贈・観光施策の**当事者**であり、1つずつ表に足す意味がない
    for suf, label in (t.get("suffix_types") or {}).items():
        if host == suf.lstrip(".") or host.endswith(suf):
            return label
    # 7. 表に載っているドメインの**下にあるサブドメイン**。
    #    会社やサービスのドメインを1つ載せれば、その配下の特設サイトまで届く
    #    (実測: bandainamcomusiclive.co.jp を載せても pylonport. が拾えなかった)。
    #    個別指定・特設サイト・種類の後に見るので、上の判断を覆さない
    for key, label in (("semi_official_domains", "準公式"), ("press_domains", "報道"),
                       ("secondary_domains", "二次情報"),
                       ("discovery_only_domains", "二次情報"),
                       ("fan_domains", "ファン"), ("party_domains", "当事者"), ("performer_domains", "演者")):
        for d in t.get(key) or []:
            if host.endswith("." + d):
                return label
    return "未確認"


def is_discovery_only(url: str) -> bool:
    """リンク集・RSSミラーか。**出典に載せてはいけない**サイト。

    中身を持たず、他所の記事の見出しとリンクを並べているだけなので、
    そこを開いても事実は何も確認できない。ネタを見つけるのには使えるが、
    出典にすると「確認できない場所」を根拠として示すことになる。
    """
    import urllib.parse
    u = urllib.parse.urlparse(url if "//" in url else "//" + url)
    host = (u.hostname or "").removeprefix("www.")
    return any(host == d or host.endswith("." + d)
               for d in source_type_table().get("discovery_only_domains") or [])


# --- facts の裏取り(日付・金額が出典本文にあるか) ----------------------------

def fact_atoms(facts: list[str]) -> list[tuple[str, list[str]]]:
    """facts から**照合できる粒**(日付・金額)を抜く。

    返すのは (表示名, 許容表記の一覧)。表記ゆれはどれか1つ当たれば一致とみなす。
    日本語の言い回しは照合できないが、日付と金額は書き換えの効かない値であり、
    ここが出典に無ければ「出典にない事実」である可能性が高い。
    """
    import unicodedata
    out, seen = [], set()

    def add(name, forms):
        if name not in seen:
            seen.add(name)
            out.append((name, forms))

    for fact in facts or []:
        f = re.sub(r"[,\s]", "", unicodedata.normalize("NFKC", fact))
        for y, m, d in re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", f):
            # 年つきの粒。照合は unbacked_facts 側で年まで見る
            add(f"{y}-{int(m):02d}-{int(d):02d}",
                [f"{y}年{int(m)}月{int(d)}日", f"{y}/{int(m)}/{int(d)}",
                 f"{y}-{int(m):02d}-{int(d):02d}", f"{y}/{int(m):02d}/{int(d):02d}"])
        for m, d in re.findall(r"(?<!\d)(\d{1,2})月(\d{1,2})日", f):
            add(f"{int(m)}月{int(d)}日",
                [f"{int(m)}月{int(d)}日", f"{int(m)}/{int(d)}", f"{int(m):02d}/{int(d):02d}"])
        for v in re.findall(r"(\d{3,})円", f):
            # **裸の数字は照合に使わない。**商品番号や日付の一部に偶然一致して、
            # 出典に無い価格を「裏取りできた」ことにしてしまう(監査指摘)。
            # 円記号は NFKC で ¥ に揃う
            add(f"{v}円", [f"{v}円", f"¥{v}", f"{v}yen"])
    return out


def unbacked_facts(facts: list[str], page_text: str) -> list[str]:
    """facts の粒のうち、出典本文に見つからなかったものを返す。

    実測(2026-08 の候補30件・粒834個)では 97% が一致した。
    外れるのは主に「発表日」(ページ自身が自分の掲載日を本文に書かない)と、
    画像や別ページに置かれた価格である。**つまりこれは捏造の証拠ではない。**
    発行を止める根拠には弱いので、ゲートにはせず、
    「この値は出典本文で確認できていない」という申し送りとして残す。
    """
    import unicodedata
    t = re.sub(r"[,\s]", "", unicodedata.normalize("NFKC", page_text or ""))
    out = []
    for name, forms in fact_atoms(facts):
        if any(x in t for x in forms):
            continue
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", name)
        if not m:
            out.append(name)
            continue
        # 年つきの日付が、年ぬきでしか本文に出ていない場合。
        # 日本語のページは「8月28日より開催」と年を省くことが多いので、
        # 年ぬきの一致は認める。**ただし本文が同じ月日に別の年を書いているなら認めない。**
        # 素通しにしていたため、facts の 2025年7月30日 と 2026年7月30日 が
        # どちらも「7月30日」1つで裏取りできたことになっていた(監査指摘)。
        # 年を必ず要求すると、年を書かないページで一斉に誤検知するので、この形にする
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        bare = [f"{mo}月{d}日", f"{mo}/{d}", f"{mo:02d}/{d:02d}", f"{mo:02d}月{d:02d}日"]
        if not any(x in t for x in bare):
            out.append(name)
            continue
        others = set(re.findall(rf"(\d{{4}})年{mo}月{d}日", t)) | set(re.findall(rf"(\d{{4}})/{mo}/{d}", t))
        if others and y not in others:
            out.append(name)
    return out


QUOTE_CHUNK = 10    # 写しを何字ずつに切って本文と照らすか


def quote_on_page(quote: str, page_text: str) -> bool | None:
    """収集役が「url のページから写した」とする1文が、そのページの本文にあるか。写しが無ければ None(判定しない)。

    日付・金額の粒(unbacked_facts)だけでは、隣の記事の URL を付けた取り違えを捕まえられない
    (2026-09-28〜10-01: noctchill の事実に Master ShowPiece の記事の URL が付いていた。同じ公式サイトの近い記事は
    日付が重なり、粒は一致してしまう)。写しは**形の照合**で、内容の良し悪しは見ない。
    - 空白・句読点・全角半角の違いをならした上で、写し**全体**が本文に連続してあれば True(ある)
    - 全体は無いが、QUOTE_CHUNK 字ずつの断片の半分以上が本文にあれば None(確かめられない): 写し損ないかもしれないが、
      同じサイトの記事は前置き(「〜についてお知らせします」)が共通で、断片の一致だけでは取り違えを見分けられない(監査指摘)。
      使わせはしないが confirmed にもしない
    - それ未満なら False(無い = URL の取り違え)。取り違えたページには、その話題の文はほぼ1片も無い"""
    import unicodedata
    norm = lambda s: re.sub(r"[\s、。,.・「」『』()()!！?？:：]", "", unicodedata.normalize("NFKC", s or ""))
    q = norm(quote)
    if len(q) < QUOTE_CHUNK:
        return None
    t = norm(page_text)
    if q in t:
        return True
    chunks = [q[i:i + QUOTE_CHUNK] for i in range(0, len(q) - QUOTE_CHUNK + 1, QUOTE_CHUNK)]
    return None if sum(c in t for c in chunks) * 2 >= len(chunks) else False
