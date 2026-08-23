"""パイプライン共通ユーティリティ(collect/compose/release/watch)。標準ライブラリのみ。"""
import datetime
import json
import subprocess
import sys
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


def branch_exists(name: str, remote: bool = False) -> bool:
    ref = f"refs/remotes/origin/{name}" if remote else f"refs/heads/{name}"
    return git("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def checkout_edition_branch(date: str, job: str) -> bool:
    """edition/<date> を origin と同期して checkout。無ければ main から作る。"""
    if git("status", "--porcelain").stdout.strip():
        notify(job, f"作業ツリーに未コミットの変更があるため中止", ok=False)
        return False
    git("fetch", "origin", "--prune")
    branch = f"edition/{date}"
    if branch_exists(branch, remote=True):
        git("checkout", "-B", branch, f"origin/{branch}")
    elif branch_exists(branch):
        git("checkout", branch)
    else:
        git("checkout", "-B", branch, "origin/main")
        git("push", "-u", "origin", branch, check=False)
        notify(job, f"{branch} が無かったため main から作成した(release の作成漏れ?)")
    return True


def commit_and_push(branch: str, message: str, job: str) -> None:
    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        print("変更なし(コミットせず)", flush=True)
        return
    git("commit", "-m", message)
    r = git("push", "origin", branch, check=False)
    if r.returncode != 0:
        # collect 同士/release との競合: リモートを取り込んで積み直す
        git("pull", "--rebase", "origin", branch, check=False)
        r2 = git("push", "origin", branch, check=False)
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
    import re
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []
