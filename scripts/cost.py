#!/usr/bin/env python3
"""cost: 1号を作るのにかかった費用を、号のサイクル単位で出す。

  python3 scripts/cost.py [--editions 7]

**集計単位は暦日ではなく号のサイクル**。1号は
  前号のリリース(06:00 JST) → 収集 → 翌 04:00 compose → 06:00 リリース
で1周するので、境界は 06:00 JST に置く(暦日で切ると、04:00 の compose が
前日側に落ちて号と費用が対応しなくなる)。

**対話セッションは除外する**。紙面を作った費用だけを見るため、
paper のパイプラインが起こしたセッションに限る。判定はログの実データで行う:
  claude … ~/.claude/projects のディレクトリ名、および冒頭がパイプラインの
            プロンプトかどうか(手で再実行した分が対話側に混ざるため)
  codex  … rollout ログ先頭の cwd
  grok   … ccusage grok session の projectPath

費用は ccusage が API 公開価格で算出した換算値であって、
サブスクリプションの請求額ではない。
"""
import argparse
import collections
import datetime
import json
import pathlib
import re
import subprocess
import sys

JST = datetime.timezone(datetime.timedelta(hours=9))
HOME = pathlib.Path.home()
ROOT = pathlib.Path(__file__).resolve().parent.parent
OPS_DIR = "-home-akabanep-git-imas-ops"
PIPE_MARK = re.compile(r"あなたは日刊AI新聞|機械検収エラーを修正|の記事計画|面だけ|校閲してください")
RELEASE_HOUR = 6


def ccusage(*args) -> dict:
    # ccusage の shebang は node を指すが、この環境には node が無く bun しか無い
    bun, cli = HOME / ".bun/bin/bun", HOME / ".bun/install/global/node_modules/ccusage/src/cli.js"
    if not (bun.exists() and cli.exists()):
        sys.exit("ccusage が未導入です: bun add -g ccusage")
    r = subprocess.run([str(bun), str(cli), *args, "--json"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ccusage 実行失敗: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


def work_dirs() -> dict:
    """codex/grok のセッションID → 実行時の作業ディレクトリ。"""
    out = {}
    root = HOME / ".codex/sessions"
    files = {p.stem: p for p in root.rglob("rollout-*.jsonl")} if root.exists() else {}
    for s in ccusage("codex", "session").get("sessions", []):
        p = files.get(s.get("sessionFile", ""))
        if not p:
            continue
        for i, line in enumerate(p.open(encoding="utf-8", errors="replace")):
            m = re.search(r'"cwd"\s*:\s*"([^"]+)"', line)
            if m:
                out[s["sessionId"]] = m.group(1)
                break
            if i > 5:
                break
    for s in ccusage("grok", "session").get("sessions", []):
        out[s["sessionId"]] = s.get("projectPath", "")
    return out


def first_user_message(path: pathlib.Path) -> str:
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") != "user":
            continue
        c = (o.get("message") or {}).get("content")
        if isinstance(c, list):
            c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
        if isinstance(c, str) and c.strip():
            return c[:300]
    return ""


def is_pipeline(sess: dict, index: dict, cwds: dict) -> bool:
    if sess.get("agent") in ("codex", "grok"):
        return any(k in cwds.get(sess["period"], "") for k in ("imas-ops", "Imas_Daily_News"))
    p = index.get(sess["period"])
    if p is None:
        return False
    return p.parent.name == OPS_DIR or bool(PIPE_MARK.search(first_user_message(p)))


def edition_of(ts: str) -> str:
    """その時刻の作業が寄与する号。06:00 JST を境界にする。"""
    t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(JST)
    d = t.date() + datetime.timedelta(days=1) if t.hour >= RELEASE_HOUR else t.date()
    return d.isoformat()


def article_counts() -> dict:
    n = collections.Counter()
    for p in (ROOT / "docs" / "_posts").glob("*.md"):
        m = re.match(r"(\d{4}-\d{2}-\d{2})-", p.name)
        if m:
            n[m.group(1)] += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--editions", type=int, default=7, help="直近何号ぶんを表示するか")
    args = ap.parse_args()

    index = {p.stem: p for p in (HOME / ".claude/projects").glob("*/*.jsonl")}
    cwds = work_dirs()
    arts = article_counts()

    per = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0.0]))
    total = collections.Counter()
    for s in ccusage("session")["session"]:
        if not is_pipeline(s, index, cwds):
            continue
        ed = edition_of(s["metadata"]["lastActivity"])
        for m in s["modelBreakdowns"]:
            row = per[ed][m["modelName"]]
            row[0] += 1
            row[1] += m["cost"]
        total[ed] += s["totalCost"]

    for ed in sorted(per)[-args.editions:]:
        n = arts.get(ed, 0)
        head = f"■ {ed}号   {total[ed]:>6.2f}$"
        if n:
            head += f"   記事{n}本 → 1本あたり {total[ed]/n:.2f}$"
        print(head)
        for m, (c, cost) in sorted(per[ed].items(), key=lambda x: -x[1][1]):
            print(f"    {m:<28}{c:>4}ｾｯｼｮﾝ{cost:>8.2f}${cost/total[ed]*100:>5.0f}%")
        print()
    print("  ※ パイプラインのみ(対話セッションは除外)")
    print("  ※ API 公開価格での換算値。サブスクリプションの請求額ではない")
    return 0


if __name__ == "__main__":
    sys.exit(main())
