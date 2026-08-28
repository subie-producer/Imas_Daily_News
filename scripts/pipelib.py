"""パイプライン共通ユーティリティ(collect/compose/release/watch)。標準ライブラリのみ。"""
import datetime
import html as html_lib
import json
import re
import subprocess
import sys
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
CLAUDE_MODEL = ENV.get("CLAUDE_MODEL", "sonnet")
COLLECT_MODEL = ENV.get("COLLECT_MODEL", "haiku")
# 記事本文の執筆に使う Codex モデル(codex exec -m に渡す)。校閲とベンダーを分離するため執筆側に配置
CODEX_WRITE_MODEL = ENV.get("CODEX_WRITE_MODEL", "gpt-5.6-luna")
# 社説の執筆に使う Codex モデル。記事とは求めるものが違う(事実の要約ではなく人格と文章)ため
# 別枠にしてある。既定は terra
EDITORIAL_MODEL = ENV.get("EDITORIAL_MODEL", "gpt-5.6-terra")
# 校閲・機械検収エラーの修正に使う Claude モデル(claude -p --model に渡す)。
# 執筆(Codex)と別ベンダーにするため Claude 側。既定は haiku(検品はコスト重視)
REVIEW_MODEL = ENV.get("REVIEW_MODEL", "haiku")
# 暴走セッション対策の安全弁(--max-budget-usd)。通常運用なら到達しない額を目安に設定。
# 注: codex exec には同等のコスト上限フラグが無いため、執筆(Codex)側には適用できない
EXPLORE_MAX_BUDGET_USD = ENV.get("EXPLORE_MAX_BUDGET_USD", "1.5")
COMPOSE_ARTICLE_MAX_BUDGET_USD = ENV.get("COMPOSE_ARTICLE_MAX_BUDGET_USD", "3")
COMPOSE_WHOLE_MAX_BUDGET_USD = ENV.get("COMPOSE_WHOLE_MAX_BUDGET_USD", "8")


def notify(job: str, msg: str, ok: bool = True) -> None:
    prefix = "✅" if ok else "🚨"
    text = f"{prefix} アイマスNEWS {job}: {msg}"
    print(text, flush=True)
    url = ENV.get("DISCORD_WEBHOOK_URL")
    if url:
        try:
            req = urllib.request.Request(
                url, data=json.dumps({"content": text}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "ImasNewsBot/1.0"})
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"(Discord 通知失敗: {e})", flush=True)


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


def checkout_edition_branch(date: str, job: str) -> bool:
    """edition/<date> を origin と同期して checkout。無ければ main から作る。"""
    if git("status", "--porcelain").stdout.strip():
        notify(job, f"作業ツリーに未コミットの変更があるため中止", ok=False)
        return False
    f = git_net("fetch", "origin", "--prune")
    if f.returncode != 0:
        notify(job, f"origin への fetch に失敗(再試行後も回復せず)。中止:\n{f.stderr.strip()[:200]}", ok=False)
        return False
    branch = f"edition/{date}"
    if branch_exists(branch, remote=True):
        git("checkout", "-B", branch, f"origin/{branch}")
    elif branch_exists(branch):
        git("checkout", branch)
    else:
        git("checkout", "-B", branch, "origin/main")
        git_net("push", "-u", "origin", branch)
        notify(job, f"{branch} が無かったため main から作成した(release の作成漏れ?)")
    # edition ブランチは release が前日に作るため、その後 main に入ったスクリプト修正を
    # 持たない。取り込まないと、直した当日の号が直っていない版で生成される
    # (2026-08-26号がこれで旧ロジックのまま13本で発行された)。
    # 紙面ファイルは main 側に無いので、ここでの取り込みが号の中身を壊すことはない。
    # --is-ancestor は「祖先でない」を終了コード1で返す。これは異常ではないので check=False
    if git("merge-base", "--is-ancestor", "origin/main", "HEAD", check=False).returncode != 0:
        m = git("merge", "origin/main", "--no-edit")
        if m.returncode != 0:
            git("merge", "--abort")
            notify(job, f"{branch} への main 取り込みが衝突。**古いスクリプトのまま続行**します:\n"
                        f"{m.stdout.strip()[:300]}", ok=False)
        else:
            print(f"{branch}: main を取り込んだ({git('rev-parse', '--short', 'origin/main').stdout.strip()})",
                  flush=True)
            git_net("push", "origin", branch)
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


def commit_and_push(branch: str, message: str, job: str) -> None:
    bad = conflict_markers()
    if bad:
        notify(job, "マージ衝突マーカーが残ったファイルがあるためコミットを中止:\n- "
                    + "\n- ".join(bad[:8]), ok=False)
        return
    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        print("変更なし(コミットせず)", flush=True)
        return
    git("commit", "-m", message)
    r = git_net("push", "origin", branch)
    if r.returncode != 0:
        # collect 同士/release との競合: リモートを取り込んで積み直す
        git_net("pull", "--rebase", "origin", branch)
        r2 = git_net("push", "origin", branch)
        if r2.returncode != 0:
            notify(job, f"push 失敗: {r2.stderr.strip()[:200]}", ok=False)


def append_metric(kind: str, data: dict) -> None:
    d = now_jst().strftime("%Y-%m-%d")
    p = ROOT / "metrics" / f"{d}.json"
    doc = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    doc.setdefault(kind, []).append({"at": now_jst().isoformat(timespec="seconds"), **data})
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def extract_json_array(text: str):
    """LLM 出力から最初の JSON 配列を寛容に取り出す。"""
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


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
_TAG_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>|<[^>]+>")


def html_to_text(raw: bytes, charset: str | None = None) -> str:
    t = raw.decode(charset or "utf-8", errors="replace")
    t = _TAG_RE.sub("\n", t)
    t = html_lib.unescape(t)
    return re.sub(r"[ \t　]+", " ", t)


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
