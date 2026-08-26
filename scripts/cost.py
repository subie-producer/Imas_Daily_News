#!/usr/bin/env python3
"""cost: 稼働コストを 日 × モデル × 回数 で分解する。

  python3 scripts/cost.py [--days 7] [--sessions]

費用は ccusage(https://github.com/ryoppippi/ccusage)が claude / codex / grok の
ローカルログから API 公開価格で算出した値を使う。回数はログから自前で数える。

**費用は API 公開価格での換算値であって、サブスクリプションの請求額ではない。**
比較と切り分けに使う数字で、支払額として読まない。

## ccusage をそのまま使わない理由

1. **日付が UTC。** 発行サイクルの起点 04:00 JST が前日側へ落ちる。
   `--timezone Asia/Tokyo` を必ず渡す。
2. **呼び出し回数を持たない。** 費用の内訳は出るが「何回叩いたか」が出ない。
   1回あたりの重さが分からないと、頻度を下げるべきか1回を軽くすべきかを判断できない。
   回数は3系統それぞれ別の場所から数える(下記)。

## 回数の数え方(1回 = モデルへの1リクエスト)

- claude: ~/.claude/projects/**/*.jsonl の assistant メッセージ(usage 付き)
- codex : ~/.codex/sessions/**/rollout-*.jsonl の token_count イベント
- grok  : ~/.grok/logs/unified.jsonl の shell.turn.inference_done

セッション数(= CLI の起動回数)は --sessions で併記する。
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
HOME = pathlib.Path.home()


def ccusage(*args) -> dict:
    # ccusage の shebang は node を指すが、この環境には node が無く bun しか無い
    bun = HOME / ".bun/bin/bun"
    cli = HOME / ".bun/install/global/node_modules/ccusage/src/cli.js"
    if not (bun.exists() and cli.exists()):
        sys.exit("ccusage が未導入です: bun add -g ccusage")
    r = subprocess.run([str(bun), str(cli), *args, "--json"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ccusage 実行失敗: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


def jst_date(ts) -> str | None:
    try:
        return datetime.datetime.fromisoformat(
            str(ts).replace("Z", "+00:00")).astimezone(JST).date().isoformat()
    except Exception:
        return None


def count_calls() -> tuple[collections.Counter, collections.Counter]:
    """(日,モデル) → リクエスト数 / セッション数。"""
    calls, sess = collections.Counter(), collections.Counter()

    for p in (HOME / ".claude/projects").glob("*/*.jsonl"):
        seen = set()
        for line in p.open(encoding="utf-8", errors="replace"):
            if '"usage"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            m = o.get("message") or {}
            if o.get("type") == "assistant" and m.get("usage") and m.get("model"):
                d = jst_date(o.get("timestamp"))
                if d:
                    calls[(d, m["model"])] += 1
                    seen.add((d, m["model"]))
        for k in seen:
            sess[k] += 1

    root = HOME / ".codex/sessions"
    for p in (root.rglob("rollout-*.jsonl") if root.exists() else []):
        model, seen = None, set()
        for line in p.open(encoding="utf-8", errors="replace"):
            if model is None:
                m = re.search(r'"model"\s*:\s*"([^"]+)"', line)
                if m:
                    model = m.group(1)
            if '"total_token_usage"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            d = jst_date(o.get("timestamp"))
            if d:
                calls[(d, model or "codex")] += 1
                seen.add((d, model or "codex"))
        for k in seen:
            sess[k] += 1

    # grok はセッションディレクトリにトークン数を残さない。統合ログの推論完了イベントを数える
    ulog = HOME / ".grok/logs/unified.jsonl"
    grok_sess = collections.defaultdict(set)
    if ulog.exists():
        for line in ulog.open(encoding="utf-8", errors="replace"):
            if '"prompt_tokens"' not in line:
                continue
            m = re.search(r'"ts":"([^"]+)"', line)
            d = jst_date(m.group(1)) if m else None
            if not d:
                continue
            calls[(d, "grok-4.6-build")] += 1
            sid = re.search(r'"sid":"([^"]+)"', line)
            if sid:
                grok_sess[d].add(sid.group(1))
    for d, s in grok_sess.items():
        sess[(d, "grok-4.6-build")] += len(s)
    return calls, sess


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--sessions", action="store_true", help="セッション数(CLI起動回数)も出す")
    args = ap.parse_args()

    calls, sess = count_calls()
    rows = next(v for v in ccusage("daily", "--timezone", "Asia/Tokyo").values()
                if isinstance(v, list))
    grand = 0.0
    for r in rows[-args.days:]:
        d = r["period"]
        grand += r["totalCost"]
        print(f"■ {d}(JST)   合計 {r['totalCost']:.2f}$")
        head = f"   {'モデル':<26}{'呼出':>7}"
        if args.sessions:
            head += f"{'ｾｯｼｮﾝ':>7}"
        print(head + f"{'出力tok':>10}{'ｷｬｯｼｭ読':>10}{'費用':>9}{'割合':>6}{'単価/回':>9}")
        for m in sorted(r["modelBreakdowns"], key=lambda x: -x["cost"]):
            name = m["modelName"]
            n = calls.get((d, name), 0)
            line = f"   {name:<26}{n:>7}"
            if args.sessions:
                line += f"{sess.get((d, name), 0):>7}"
            per = f"{m['cost']/n:.3f}$" if n else "-"
            print(line + f"{m['outputTokens']:>10,}{m['cacheReadTokens']/1e6:>9.0f}M"
                         f"{m['cost']:>8.2f}${m['cost']/r['totalCost']*100:>5.0f}%{per:>9}")
        print()
    print(f"  表示期間の合計 {grand:.2f}$")
    print("  ※ API 公開価格での換算値。サブスクリプションの請求額ではない")
    return 0


if __name__ == "__main__":
    sys.exit(main())
