#!/usr/bin/env python3
"""cost: 稼働コストを「パイプライン」と「対話」に切り分けて集計する。

  python3 scripts/cost.py [--days 7] [--stages]

ccusage(https://github.com/ryoppippi/ccusage)が claude / codex / grok の
ローカルログから算出した費用を読み、この紙面固有の観点で組み替える。

ccusage 単体では足りない点が2つあるため、このラッパを置いている:

1. **JST で集計する。** ccusage の日付は UTC で、発行サイクルの起点である
   04:00 JST が前日側に落ちる。号ごとの費用を見るときに1日ずれる。
2. **パイプラインと対話を分ける。** 3系統とも所属の判定方法が違う。
   - claude: ~/.claude/projects のディレクトリ名。手で再実行すると対話側に
     混ざるため、セッション冒頭がパイプラインのプロンプトかどうかも見る
   - grok:   `ccusage grok session` の projectPath
   - codex:  `ccusage codex session` の sessionFile から rollout ログを引き、
             先頭の cwd を読む
   汎用の `ccusage session` は codex/grok の所属を持たないので、
   専用サブコマンドを併用している(推測でパイプライン扱いにしない)。

費用は API 公開価格での換算値であって、サブスクリプションの請求額ではない。
比較と切り分けに使う数字で、支払額として読まない。
"""
import argparse
import collections
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys

JST = datetime.timezone(datetime.timedelta(hours=9))
LOGS = pathlib.Path.home() / ".claude" / "projects"
OPS_DIR = "-home-akabanep-git-imas-ops"
# ヘッドレス実行(claude -p)は、セッション冒頭がパイプラインのプロンプトになる
PIPE_MARK = re.compile(r"あなたは日刊AI新聞|機械検収エラーを修正|の記事計画|面だけ|校閲してください")
STAGE_MARK = (("面だけ", "選定"), ("記事計画", "選定"), ("一面", "lead選定"),
              ("機械検収エラー", "検収修正"), ("編集部です", "組版"), ("校閲", "校閲"))


def ccusage(*args) -> dict:
    # ccusage の shebang は node を指すが、この環境には node が無く bun しか無い。
    # bun で直接スクリプトを実行する(bun は node 互換の実行系を持つ)
    bun = os.path.expanduser("~/.bun/bin/bun")
    exe = os.path.expanduser("~/.bun/install/global/node_modules/ccusage/src/cli.js")
    if not (os.path.exists(bun) and os.path.exists(exe)):
        sys.exit("ccusage が見つかりません: bun add -g ccusage で導入してください")
    r = subprocess.run([bun, exe, *args, "--json"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ccusage 実行失敗: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


def first_user_message(path: pathlib.Path) -> str:
    for line in path.open(encoding="utf-8"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") != "user":
            continue
        c = o.get("message", {}).get("content")
        if isinstance(c, list):
            c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
        if isinstance(c, str) and c.strip():
            return c.strip()[:400]
    return ""


def codex_cwds() -> dict:
    """codex セッションID → 実行時の作業ディレクトリ。

    ccusage の汎用 session では codex/grok の所属が分からない。専用サブコマンドは
    grok なら projectPath を、codex なら sessionFile を持つので、codex は
    rollout ログの先頭から cwd を読む。作業ディレクトリが分かれば
    「パイプラインが起こしたのか、人が直接叩いたのか」を推測ではなく実データで判定できる。
    """
    out = {}
    root = pathlib.Path.home() / ".codex" / "sessions"
    files = {p.stem: p for p in root.rglob("rollout-*.jsonl")} if root.exists() else {}
    for sess in ccusage("codex", "session").get("sessions", []):
        p = files.get(sess.get("sessionFile", ""))
        if p is None:
            continue
        for i, line in enumerate(p.open(encoding="utf-8")):
            m = re.search(r'"cwd"\s*:\s*"([^"]+)"', line)
            if m:
                out[sess["sessionId"]] = m.group(1)
                break
            if i > 5:
                break
    return out


def grok_paths() -> dict:
    return {s["sessionId"]: s.get("projectPath", "")
            for s in ccusage("grok", "session").get("sessions", [])}


def classify(sess: dict, index: dict, cwds: dict) -> tuple[str, str]:
    """(区分, 工程) を返す。"""
    agent = sess.get("agent")
    if agent in ("codex", "grok"):
        # cwd がリポジトリ配下なら compose/collect が起こしたセッション。
        # それ以外(手元での試し打ち等)はパイプラインの費用に混ぜない
        cwd = cwds.get(sess["period"], "")
        if not any(k in cwd for k in ("imas-ops", "Imas_Daily_News")):
            return "対話", f"手動({agent})"
        return "パイプライン", ("記事執筆(codex)" if agent == "codex" else "収集・X調査(grok)")
    path = index.get(sess["period"])
    if path is None:
        return "対話", "対話"
    head = first_user_message(path)
    if path.parent.name != OPS_DIR and not PIPE_MARK.search(head):
        return "対話", "対話"
    for pat, name in STAGE_MARK:
        if pat in head:
            return "パイプライン", name
    return "パイプライン", "収集・探索(claude)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="直近何日ぶんを表示するか")
    ap.add_argument("--stages", action="store_true", help="工程別の内訳も出す")
    args = ap.parse_args()

    index = {p.stem: p for p in LOGS.glob("*/*.jsonl")}
    cwds = {**codex_cwds(), **grok_paths()}
    sessions = ccusage("session")["session"]

    by_day = collections.defaultdict(lambda: collections.defaultdict(float))
    by_stage = collections.defaultdict(lambda: [0, 0.0])
    top = []
    for s in sessions:
        at = datetime.datetime.fromisoformat(
            s["metadata"]["lastActivity"].replace("Z", "+00:00")).astimezone(JST)
        kind, stage = classify(s, index, cwds)
        by_day[at.date().isoformat()][kind] += s["totalCost"]
        by_stage[stage][0] += 1
        by_stage[stage][1] += s["totalCost"]
        top.append((s["totalCost"], at, kind, stage, s["period"][:8]))

    print(f"  {'日付(JST)':<12}{'パイプライン':>14}{'対話':>12}{'計':>12}")
    for d in sorted(by_day)[-args.days:]:
        v = by_day[d]
        print(f"  {d:<12}{v['パイプライン']:>13.2f}${v['対話']:>11.2f}${v['パイプライン']+v['対話']:>11.2f}$")
    tot = collections.Counter()
    for v in by_day.values():
        tot.update(v)
    total = sum(tot.values())
    print(f"  {'全期間':<12}{tot['パイプライン']:>13.2f}${tot['対話']:>11.2f}${total:>11.2f}$")
    if total:
        print(f"  {'':12}{tot['パイプライン']/total*100:>13.0f}%{tot['対話']/total*100:>11.0f}%")

    if args.stages:
        print(f"\n  {'工程':<20}{'セッション':>10}{'費用':>10}")
        for k, (n, c) in sorted(by_stage.items(), key=lambda x: -x[1][1]):
            print(f"  {k:<20}{n:>10}{c:>9.2f}$")

    print(f"\n  費用の大きいセッション")
    for c, at, kind, stage, sid in sorted(top, reverse=True)[:5]:
        print(f"  {c:>8.2f}$  {at:%m-%d %H:%M}  {kind}/{stage}  ({sid})")
    print("\n  ※ API 公開価格での換算値。サブスクリプションの請求額ではない")
    return 0


if __name__ == "__main__":
    sys.exit(main())
