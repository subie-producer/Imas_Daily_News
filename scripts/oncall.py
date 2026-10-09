#!/usr/bin/env python3
"""oncall: 実装の欠陥で工程が止まったとき、人へ投げる前に**当番が直す**。

  python3 scripts/oncall.py --stage compose|release|classify --date YYYY-MM-DD --reason "…"

呼ばれ方は2つ: 工程が止まった(compose / release)と、**コードの不足が分かった**(classify: 出典の持ち主を取る
パーサが無い種類の URL が来た。人に「決まらない」と申告する前に当番がパーサを書く。編集長 2026-09-26
「申告前に診断しろ」)。どちらも当番が直し、監査が査読し、合意した commit だけを取り込む

パイプラインが「人間判断が必要」で止まるのは、ほぼ毎回コードかプロンプトの欠陥である
(実測: 2026-09-06〜12、7日連続で毎朝止まった。原因は全部こちらの実装)。人が起きて
直すまで号が出ないのは実用ではない。そこで:

1. **当番(Opus)** が、ログと状態から診断し、origin/main から切った使い捨ての作業ツリーで最小の
   修正を書き、selfcheck と再現テストを通し、報告を JSON で返す(schema/oncall-fix.schema.json)
2. 変更はその場で **commit して内容を固定**し、**監査(Sol)** がその commit の差分(差分が無ければ
   診断そのもの)を敵対的にレビューし、判定を JSON で返す
3. **当番と監査の仕事の範囲は「発行に必要な最小限で、今後もちゃんと動く正しい修正」**(編集長の整理
   2026-09-18)。監査が must_fix に入れるのはこの範囲の問題だけ: 止まった原因に当たっていない /
   現実の入力でまた止まる / データや他の工程を壊す / その場しのぎ(握りつぶす・やり直すだけ)。
   この範囲なら**合意(approve で must_fix が空)するまで**当番が直し、監査が見直す
   (上限は回数 MAX_ROUNDS と時間 ONCALL_LIMIT_MIN)。指摘が事実誤認か、編集方針で判定対象外のもの
   (POLICY_EXCLUDED)に当たるときだけ、根拠を示して「当たらない」と答えてよい。
   以前は範囲を決めずに合意を求めたので、監査が「現実には来ない入力での堅牢化」を must_fix に積み、
   当番がそれを追いかけて、発行できる修正が2往復で時間切れになっていた(実測 2026-09-18)
3a. **起きる道筋を言えない指摘は、指摘として扱わない**(編集長 2026-09-18:「起きねーよ。どういうときに起きると
   言えるんだ、どのくらいの確率で起きる見込みなんだ、で弾いていい」)。監査は指摘ごとに `occurs`=どういうときに
   起きるか(実データ・ログ・運用から到達する道筋)と、どのくらい起きそうか、を書く。「schema 上は可能」は道筋ではない。
   書けないものは must_fix にも later にも入れない。当番は道筋の無い指摘を、実データの根拠を示して弾いてよい。
   occurs が空の later はコードでも保管しない
3b. **発行後でよい指摘(later)は別に保管する**: いまは止まらないが現実に起きる見込みのあるもの(堅牢化・設計の改善・
   テストの追加・書き方)。監査はこれを must_fix に入れず later に書く。当番はこの場では直さない。
   取り込みのあと `metrics/oncall-backlog.jsonl` に積み、修正報告に載せる。watch(09:00)が残っている指摘を
   当番に渡し(--stage backlog --backlog-keys …)、当番が直して取り込めたら消し込む(この工程では later も直す)。
   手で直したときは `oncall.py --backlog-done <id>` で消し込み(`--backlog` で一覧)。
   保管の失敗は発行を妨げない(報告には必ず載る)
4. 合意したもの(approve で must_fix が空)だけ、**監査した commit のハッシュそのもの**を
   main へ merge し、`edition/<日付>` へ取り込み、止まった工程を再実行する
5. 上限までに合意できなければ、そこまでの修正(commit)と残った指摘を state に残して人へ渡す。
   次の試行は**その続きから**始める(同じ基準の main なら。以前は毎回ゼロから診断し直し、
   1回目と2回目で別々の仮説を立てて、どちらも途中で終わっていた。実測 2026-09-18)
6. **修正を入れたら Discord に報告する(必須)**: 何を・なぜ・どう検証したか・監査の判定・
   差分の要点・戻し方(編集長の指示: 勝手に変な変更が入っていないかを人が見られるように)

守ること(監査の指摘で固めた):
- 当番の作業ツリーには **.env を置かない**。セッションの環境変数は最小の allowlist だけ渡す。
  触ってよいのは scripts/ prompts/ schema/ と設計文書だけ(判定表・収集対象は含めない)。
  セッションのあと**本体の作業ツリーが汚れていない・origin/main が動いていない**ことを確かめる
  (どちらかが破れていれば取り込まない)
- 当番は一度に1つ(flock)。compose/release/collect が走っているあいだは待つ(起動直後は親の
  compose が生きている)。取り込みの直前と再実行の直前にも確かめる。試行回数は待ち終えてから消費
- 本体の作業ツリーは**開始時に clean でなければ何もしない**。取り込み先は `edition/<日付>` を
  名前で解決する(「今いるブランチ」を信用しない)。`git add` は対象パスだけ
- Git 操作は全部 fail-closed。記録ブランチは一意な名前で force しない。main の push が失敗したら
  edition には触らない。当番の状態・記録(metrics/oncall-*)は Git 管理外
- 再実行は直した箇所を踏ませる。計画・執筆・組版の層を直したなら**その号を作り直す**:
  作り直す前に edition ブランチの控え(backup/…)を push し、stock からこの号の寄与を剥がし
  (assemble.rollback。組版前の控えは消さない)、成果物を外して compose を最初から。失敗したら
  控えへ戻す。それ以外は古い校閲記録を消して `--reuse-plan`
- 同じ日・同じ工程で2回までしか試みない(ループ防止)。予算と時間の上限
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hashlib

from pipelib import (COLLECT_ONCALL_MARK, ENV, ROOT, SCHEDULED, SLOT_MARGIN_MIN, JobLockTimeout, job_lock, loads_strict, mark_collect_oncall,
                     next_slot_end, notify, now_jst, partial_output, prompt_file, render_prompt, save_raw, schema_ok, tool_path)

ONCALL_MODEL = ENV.get("ONCALL_MODEL", "opus")
AUDIT_MODEL = ENV.get("AUDIT_MODEL", "gpt-6.1-sol")
ONCALL_MAX_BUDGET_USD = ENV.get("ONCALL_MAX_BUDGET_USD", "15")
MAX_ATTEMPTS = 2
MAX_ROUNDS = 6                       # 当番の修正 → 監査、の往復の上限(指摘が無くなれば途中で終わる)
ONCALL_LIMIT_MIN = int(ENV.get("ONCALL_LIMIT_MIN", "150"))   # 1回の試行で往復に使う時間の上限(分)
# 編集方針で決着済みの事項。監査がこれを理由に差し戻さないよう、監査の依頼文に明記する
POLICY_EXCLUDED = ("校閲・内容判断をコードで機械化せよ、という要求(内容の判断はモデルの校閲が行う。コードは形だけを見る)",
                   "当番・各セッションの HOME や OS レベルの隔離の要求(AI は開発者の一員として扱う。過剰防御はしない)")
WAIT_IDLE_MIN = 90
DIFF_LIMIT = 60000
EDITABLE = ("scripts/", "prompts/", "schema/", "REQUIREMENTS.md", "PIPELINE.md", "AUDIT.md", "README.md", "PROMPTS.md")
# これを直したら、その号は作り直す(--reuse-plan では直した箇所を踏まない。監査指摘)
FULL_RERUN_IF = ("scripts/compose.py", "scripts/planlib.py", "scripts/renderlib.py", "scripts/assemble.py",
                 "prompts/", "schema/article-out", "schema/assemble")
# 当番・監査のセッションに渡す環境変数(資格情報は渡さない。監査指摘)
ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "USER", "LOGNAME", "TZ", "TMPDIR")
CO_AUTHOR = "\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"


def sh(args, cwd, timeout=600) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def must(r: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    """Git 操作は fail-closed。失敗を無視して先へ進むと、監査していない差分が main に入る(監査指摘)。"""
    if r.returncode != 0:
        raise RuntimeError(f"{what} に失敗: {(r.stderr or r.stdout)[-300:]}")
    return r


def undo_merge(before: str, what: str) -> None:
    """merge を無かったことにする。abort は補助で、**必ず** merge 前のハッシュへ reset する(監査指摘)。"""
    sh(["git", "merge", "--abort"], cwd=ROOT)
    must(sh(["git", "reset", "-q", "--hard", before], cwd=ROOT), f"{what} の巻き戻し")


def session_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOW}
    env["PATH"] = tool_path()   # service 由来の PATH には ~/.local/bin(claude/codex)が無い(実測 2026-09-17)
    return env


def save_state(path: Path, state: dict) -> None:
    """試行状態の原子的な保存(一時ファイル → fsync → os.replace)。途中で死んでも壊れた JSON を残さない(監査指摘)。"""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(state, ensure_ascii=False, indent=1))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def attempt_count(date: str, stage: str) -> int:
    """試行回数は state の JSON ではなく、試行ごとに O_EXCL で作る印ファイルの個数で数える
    (JSON が壊れても・書き換えられても、上限が緩まない単調な証跡。監査指摘)。"""
    return len(list((ROOT / "metrics").glob(f"oncall-{date}-{stage}-attempt-*")))


def consume_attempt(date: str, stage: str) -> int:
    """次の試行の印を作る(既にあれば失敗=競合)。戻り値は消費後の回数。"""
    n = attempt_count(date, stage) + 1
    fd = os.open(ROOT / "metrics" / f"oncall-{date}-{stage}-attempt-{n}", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    os.write(fd, datetime.datetime.now().isoformat(timespec="seconds").encode())
    os.close(fd)
    return n


def load_state(path: Path) -> dict | None:
    """試行の記録(log)を読む。壊れていれば一意な名前で退避して通知し、新規として扱う。
    退避できなければ None(証拠を失ったまま「退避した」と言わない。監査指摘)。
    試行回数はここでは扱わない(attempt_count)。"""
    if not path.exists():
        return {"log": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("log"), list):
            raise ValueError("形が違う")
        return state
    except Exception as e:
        broken = path.with_name(f"{path.name}.corrupt-{time.time_ns()}-{os.getpid()}")
        try:
            os.link(path, broken)       # 既存を上書きしない(同名があれば失敗する)
            os.unlink(path)
        except OSError as e2:
            notify("oncall", f"当番の試行記録 {path.name} が壊れていて({type(e).__name__})、退避にも失敗({e2})。当番は起動しない", ok=False)
            return None
        notify("oncall", f"当番の試行記録 {path.name} が壊れていた({type(e).__name__})。{broken.name} に退避して新規として扱う", ok=False)
        return {"log": [{"corrupted": broken.name}]}


def env_fingerprint() -> str:
    """本体の .env(Git 管理外)の指紋。当番が触っていないことを事後に検める(監査指摘)。"""
    import hashlib
    p = ROOT / ".env"
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def root_clean() -> bool:
    """判定不能(git status の失敗)は clean 扱いにしない(監査指摘)。"""
    return not must(sh(["git", "status", "--porcelain"], cwd=ROOT), "git status").stdout.strip()


def ensure_edition(edition: str, cwd: Path) -> str:
    """取り込み先の号の branch を origin と揃える。戻り値は問題の説明("" なら揃った)。fetch の通信失敗は RuntimeError。
    origin にだけある(新しい clone・ローカルの branch を消したあと)なら、fetch した参照からローカルを作る(監査指摘 r88)。"""
    try:
        fetched = sh(["git", "fetch", "-q", "origin", edition], cwd=cwd, timeout=120)
        if fetched.returncode != 0:
            if sh(["git", "ls-remote", "--exit-code", "--heads", "origin", edition], cwd=cwd, timeout=120).returncode == 2:
                return f"{edition} が origin に無い"
            raise RuntimeError(f"{edition} の fetch に失敗: {(fetched.stderr or '')[-200:]}")
    except subprocess.TimeoutExpired as e:       # 通信の停滞(WSL の DNS は断続的に失敗する)も「前提の確認に失敗」として通知する
        raise RuntimeError(f"{edition} の origin との通信が {e.timeout:.0f} 秒で応答しない") from e
    if sh(["git", "rev-parse", "--verify", "-q", edition], cwd=cwd).returncode != 0:
        must(sh(["git", "branch", edition, f"origin/{edition}"], cwd=cwd), f"{edition} のローカル作成")
    if sh(["git", "rev-parse", edition], cwd=cwd).stdout.strip() != sh(["git", "rev-parse", f"origin/{edition}"], cwd=cwd).stdout.strip():
        return f"{edition} がリモートと食い違う"
    return ""


def remote_main() -> str:
    must(sh(["git", "fetch", "-q", "origin", "main"], cwd=ROOT, timeout=120), "fetch")
    return sh(["git", "rev-parse", "origin/main"], cwd=ROOT).stdout.strip()


STAGES = ("compose", "release", "classify", "collect", "watch", "update", "backlog")
# backlog = 当番が「発行後に直す」として保管した指摘を、発行後の昼に直す依頼(watch が渡す)。保管して誰も直さない、を防ぐ
# (2026-10-06: 9/30 からの7件が、毎朝の覚え書きの通知だけで1週間手つかずだった。編集長「なんで直すリストを直せてなかった?」)
BACKLOG_STAGE = "backlog"
# 工程 → journal の unit。出典の判定(classify)は収集(collect)と組版(compose)の中で走る。当番に見せるのは収集のログ
UNIT_OF = {"classify": "collect"}
# 当番を呼んだ理由の言い方(止まったときと、止まらずに異常があった=なぜなぜ、とがある)
CALLED_BECAUSE = {"classify": "に持ち主を取るパーサの無い出典があった", "collect": "に異常があった(なぜなぜ)",
                  "watch": "が異常を検知した(なぜなぜ)", "update": "で道具の CLI の更新・動作確認に失敗した(なぜなぜ)",
                  "backlog": "の時点で、後で直すとして保管した指摘が残っていた"}


def gather_context(stage: str, date: str, reason: str) -> str:
    parts = [f"# 当番を呼んだ工程: {stage} / 号: {date}\n\n## 呼び出し側の理由\n{reason}\n"]
    r = sh(["journalctl", "--user", "-u", f"imas-{UNIT_OF.get(stage, stage)}", "--since", f"{date} 00:00", "--no-pager", "-n", "250"],
           cwd=ROOT, timeout=60)
    if r.returncode == 0 and r.stdout.strip():
        lines = [re.sub(r"^.*?python3\[\d+\]: ", "", l) for l in r.stdout.splitlines()]
        parts.append("## journal(直近250行)\n" + "\n".join(lines[-250:]))
    parts.append("## ops の直近 commit\n" + sh(["git", "log", "--oneline", "-8"], cwd=ROOT).stdout)
    return "\n\n".join(parts)


# --stage backlog で渡された保管の指摘(main が設定する)。監査の依頼文に元の指摘を全件載せ、1件ずつの扱いを確かめさせる
BACKLOG_ROWS: list[dict] = []
# 往復を終える時刻の上限(None なら ONCALL_LIMIT_MIN だけ)。backlog は次の定時工程の前に終える(backlog_deadline)
DEADLINE_AT: float | None = None
# 定時工程の時刻は pipelib(SCHEDULED・next_slot_end)と共有する。backlog はこの前に終え、発行の時間帯には始めない
# (01:30 は道具の CLI の更新。同じ工程の排他を使い、02:00 の収集の前に終える必要がある。監査指摘)
BACKLOG_MARGIN_MIN = SLOT_MARGIN_MIN   # 次の定時工程の何分前に終えるか
BACKLOG_MIN_WINDOW_MIN = 60  # これより短い枠なら始めない(取り込みの分を残して1往復も回らない)
BACKLOG_MERGE_RESERVE_MIN = 15   # 往復を終えてから、取り込み・後始末に残す時間
COLLECT_RERUN_RESERVE_MIN = 20   # 収集の当番: 往復を終えてから、取り込みと定点観測の取り直しに残す時間
COLLECT_MIN_WINDOW_MIN = 30      # 収集の当番: ロックを取れた時点でこれだけ無ければ直して取り直す時間が無い(往復10分+取り直し20分)
CLEANUP_RESERVE_SEC = 300        # 収集の当番: 取り直しを打ち切ってから、後始末(素材の確定・push・通知)に残す秒数
# 収集の当番が、取り直しまで含めて終えるべき時刻(epoch 秒。呼び出し側の収集が --end-at で固定して渡す)
STAGE_END_AT: float | None = None
# 落とした記事をこの号に戻す当番(組版が承認まで進んだが記事を落とした。compose の --recover)。往復を終えてから
# 組み直し(--reuse-plan の書き直しと校閲)に残す時間。終える時刻は組版が --end-at で渡す(発行の締切)
RECOVER_RERUN_RESERVE_MIN = 25
RECOVER = False


def backlog_deadline(now: datetime.datetime) -> float | None:
    """保管の指摘を直す当番が使ってよい時刻の上限(epoch 秒)。次の定時工程の BACKLOG_MARGIN_MIN 分前。
    道具の更新から発行まで(01:15〜06:30)・枠が BACKLOG_MIN_WINDOW_MIN 分未満なら None(始めない。指摘は残り、翌朝また渡る)。
    12:30 の収集や 03:00 の組版・01:30 の道具の更新が、当番の持つ工程の排他で待たされて止まるのを防ぐ(監査指摘)。"""
    if datetime.time(1, 15) <= now.time() <= datetime.time(6, 30):
        return None
    cands = []
    for d in (0, 1):
        for h, m in SCHEDULED:
            t = (now + datetime.timedelta(days=d)).replace(hour=h, minute=m, second=0, microsecond=0)
            if t > now:
                cands.append(t)
    end = min(cands) - datetime.timedelta(minutes=BACKLOG_MARGIN_MIN)
    if (end - now).total_seconds() < BACKLOG_MIN_WINDOW_MIN * 60:
        return None
    return end.timestamp()


def fix_prompt(stage: str, date: str, context: str, objections: list[dict] | None) -> str:
    """当番への依頼文(本文は prompts/oncall-fix.md。監査の指摘があるときは oncall-fix.objections.md を足す)。
    仕事の範囲は、工程が止まった依頼は oncall-scope、保管していた指摘を直す依頼(backlog)は oncall-scope.backlog。"""
    obj = render_prompt("oncall-fix.objections", POLICY=_policy_lines(),
                        ITEMS=json.dumps(objections, ensure_ascii=False, indent=1)) if objections else ""
    scope = render_prompt("oncall-scope.backlog" if stage == BACKLOG_STAGE else "oncall-scope")
    return render_prompt("oncall-fix", STAGE=stage, DATE=date, SCOPE=scope, OBJECTIONS=obj, CONTEXT=context)


def backlog_items_text(rows: list[dict]) -> str:
    return "\n".join(f"- [{r['key']}] {r.get('claim', '')}\n  起きるとき: {r.get('occurs', '')}\n  根拠: {r.get('evidence', '')}"
                     f"\n  (保管: {str(r.get('at') or '')[:16]} {r.get('stage', '')} の当番)" for r in rows)


def settled_backlog_keys(fix: dict, keys: list[str]) -> list[str]:
    """当番の最終報告が、渡した保管の指摘のうちどれを扱ったか(扱いが契約の値で、key がちょうど1回)。
    消し込むのはこれだけ(監査が承認したときに限る。扱っていない指摘は残り、翌朝また渡る。監査指摘)。"""
    rows = [x for x in (fix.get("backlog_items") or []) if isinstance(x, dict)]
    count: dict = {}
    for x in rows:
        count[x.get("key")] = count.get(x.get("key"), 0) + 1
    return [k for k in keys if count.get(k) == 1
            and next(x for x in rows if x.get("key") == k).get("result") in ("fixed", "already_fixed", "invalid")]


def _policy_lines() -> str:
    return "\n".join(f"    - {x}" for x in POLICY_EXCLUDED)


def review_prompt(stage: str, date: str, fix_report: dict, diff: str, integ: dict | None) -> str:
    """監査への依頼文(本文は prompts/oncall-review.md)。"""
    scope = (render_prompt("oncall-review.scope.backlog", N=len(BACKLOG_ROWS), ITEMS=backlog_items_text(BACKLOG_ROWS))
             if stage == BACKLOG_STAGE else render_prompt("oncall-review.scope"))
    return render_prompt(
        "oncall-review", STAGE=stage, DATE=date, SCOPE=scope, POLICY=_policy_lines(),
        REPORT=json.dumps(fix_report, ensure_ascii=False, indent=1),
        PREVIOUS=("\n## 前回の指摘に対する当番の対応\n" + json.dumps(integ, ensure_ascii=False, indent=1) + "\n") if integ else "",
        DIFF=f"```diff\n{diff}\n```" if diff.strip() else "(変更なし。当番は「コードの欠陥ではない」と判断した)")


def parse_json(text: str) -> dict | None:
    """同じキーが2回ある答えは読めない(後の値で黙って上書きされ、reject と approve が並ぶと承認になる。loads_strict。監査指摘)。"""
    try:
        return loads_strict(text.strip())
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return loads_strict(m.group(0))
            except Exception:
                return None
    return None


def run_claude(prompt: str, schema_file: Path, cwd: Path, timeout: int = 1800) -> dict:
    # 指示は作業ツリー側のファイルで渡す(引数に詰めない。作業ツリーの metrics/work/ は Git 管理外)
    name = "fix-" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
    short = prompt_file("oncall", name, prompt, base=cwd)
    try:
        r = subprocess.run(["claude", "-p", short, "--model", ONCALL_MODEL, "--dangerously-skip-permissions",
                            "--json-schema", schema_file.read_text(encoding="utf-8"),
                            "--max-budget-usd", ONCALL_MAX_BUDGET_USD],
                           cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env=session_env())
    except subprocess.TimeoutExpired as e:
        save_raw("oncall", name, *partial_output(e))      # 時間切れの回も、それまでの出力を残す
        raise
    # 作業ツリーは取り込み後に消えるので、生の出力は本体側(metrics/work/oncall/raw/)に残す
    save_raw("oncall", name, r.stdout, r.stderr, r.returncode)
    # 採るのは正常に終わったセッションの、渡した schema どおりの答えだけ。{"status":"no_fix_needed"} だけの答えを
    # 診断も報告も無いまま「修正不要」にしない(監査指摘)
    ans = parse_json(r.stdout or "") if r.returncode == 0 else None
    if ans is None or not schema_ok(ans, schema_file.read_text(encoding="utf-8")):
        raise RuntimeError(f"当番セッションの出力が読めない・schema のとおりでない(exit {r.returncode}): {(r.stderr or r.stdout or '')[-300:]}")
    return ans


def run_codex(prompt: str, schema_file: Path, cwd: Path, timeout: int = 1800) -> dict:
    fd, out_name = tempfile.mkstemp(prefix="oncall-review-", suffix=".json")
    os.close(fd)
    out_path = Path(out_name)
    try:
        name = "review-" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
        short = prompt_file("oncall", name, prompt, base=cwd)
        try:
            r = subprocess.run(["codex", "exec", "-m", AUDIT_MODEL, "-s", "read-only", "--skip-git-repo-check",
                                "--output-schema", str(schema_file), "-o", str(out_path), short],
                               cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                               env=session_env())
            so, se, code = r.stdout, r.stderr, r.returncode
        except subprocess.TimeoutExpired as e:
            (so, se), code = partial_output(e), None
            # 時間切れの回も、それまでの出力と答えのファイルを残してから上げる(finally で答えのファイルは消える)
            save_raw("oncall", name, so, se, code,
                     files={"answer": out_path.read_text(encoding="utf-8") if out_path.exists() else None})
            raise
        text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        save_raw("oncall", name, so, se, code, files={"answer": text})
    finally:
        out_path.unlink(missing_ok=True)
    # 採るのは正常に終わったセッションの、渡した schema どおりの答えだけ({"verdict":"approve"} だけの答えで承認にしない。監査指摘)
    ans = parse_json(text) if code == 0 else None
    if ans is None or not schema_ok(ans, schema_file.read_text(encoding="utf-8")):
        raise RuntimeError(f"監査セッションの出力が読めない・schema のとおりでない(exit {code}): {text[-300:]}")
    return ans


def freeze(wt: Path, base: str, rnd: int) -> tuple[str, str, list[str]]:
    """作業ツリーの変更を commit して内容を固定し、(HEAD, base からの差分, 変えたパス) を返す。
    監査した差分と取り込む差分を**同じ commit** にする(監査指摘)。"""
    must(sh(["git", "add", "-A"], cwd=wt), "add")
    if sh(["git", "status", "--porcelain"], cwd=wt).stdout.strip():
        must(sh(["git", "commit", "-q", "-m", f"oncall: 当番の修正({rnd}回目)" + CO_AUTHOR], cwd=wt), "commit")
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt).stdout.strip()
    diff = sh(["git", "diff", f"{base}..{head}"], cwd=wt).stdout
    changed = [l for l in sh(["git", "diff", "--name-only", f"{base}..{head}"], cwd=wt).stdout.splitlines() if l.strip()]
    return head, diff, changed


def out_of_bounds(paths: list[str]) -> list[str]:
    return [p for p in paths if not any(p.startswith(e) or p == e for e in EDITABLE)]


from pipelib import split_chunks   # noqa: E402  分割の本体は pipelib(notify 自身が分割する)


def notify_long(job: str, text: str, ok: bool = True, limit: int = 1800) -> bool:
    """必須の長い通知(修正報告)。notify が分割して全部送る。戻り値は全部届いたか。"""
    return notify(job, text, ok=ok, require=True)


def collect_later(transcript: list[dict]) -> list[dict]:
    """往復の記録から、監査の later を重複なしで集める(前の試行から引き継いだ分も含む)。"""
    out: list[dict] = []
    for t in transcript:
        for it in (t.get("carried_later") or []) + ((t.get("review") or {}).get("later") or []):
            # 起きる道筋(occurs)が書かれていない指摘は、保管と同じく報告にも載せない
            if (isinstance(it, dict) and it.get("claim") and str(it.get("occurs") or "").strip()
                    and not any(x.get("claim") == it.get("claim") for x in out)):
                out.append(it)
    return out


def report_change(stage: str, date: str, fix: dict, transcript: list[dict], base: str, head: str, merge_commit: str,
                  branch: str, targets: list[str], rerun_mode: str) -> bool:
    """当番が入れた修正の報告。**必須**。全文は metrics/oncall-<日付>-<工程>-report.md、要約を Discord へ。
    戻り値は Discord に届いたか(届かなければ当番は成功終了しない。監査指摘)。"""
    stat = sh(["git", "diff", "--stat", f"{base}..{head}"], cwd=ROOT).stdout.strip()
    diff = sh(["git", "diff", "--unified=2", f"{base}..{head}"], cwd=ROOT).stdout
    reviews = [t.get("review") for t in transcript if t.get("review")]
    integ = [t.get("integrate") for t in transcript if t.get("integrate")]
    last = reviews[-1] if reviews else {}
    verdict = f"{last.get('verdict', '?')}(往復 {len(reviews)}回)"
    if integ:
        verdict += (f" / 当番が受け入れた指摘 {sum(len(x.get('accepted') or []) for x in integ)}件"
                    f"・反論 {sum(len(x.get('refuted') or []) for x in integ)}件")
    mc = merge_commit[:10] if merge_commit else "(取り込み後に追送)"
    # 記録(全文・差分・往復)はファイルに残す。Discord には編集長が読める1通だけを送る(編集長 2026-10-04: 診断・検証・
    # 差分の全文を10通に割って送り、英語やファイル名が混ざっていた。「本当にユーザーにとって分かりやすいと思ってんのか」)
    report = (f"🛠 当番の修正報告: {stage} {date}\n"
              f"修正 commit: {head[:10]} / main の merge commit: {mc}"
              f"(取り込み先: {', '.join(targets)})/ 記録ブランチ: {branch}\n"
              f"診断: {fix.get('diagnosis') or ''}\n"
              f"原因: {fix.get('root_cause') or ''}\n"
              f"検証: {fix.get('test_evidence') or ''}\n"
              f"監査(Sol): {verdict}\n"
              f"{later_text(collect_later(transcript))}\n"
              f"リスク: {fix.get('risk') or ''} / 確信度 {fix.get('confidence', '?')}\n"
              f"変更:\n{stat}\n"
              f"再実行: {rerun_mode}\n"
              f"戻し方: ops の main で `git revert -m 1 {mc}` → push → edition/{date} に main を merge → push\n"
              f"\n## 差分\n{diff}\n")
    path = ROOT / "metrics" / f"oncall-{date}-{stage}-report.md"
    path.write_text(report + "\n## 往復の記録\n" + json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
    return notify("oncall", editor_report(stage, date, fix, last.get("verdict"), len(reviews), len(collect_later(transcript)),
                                          path.relative_to(ROOT), branch), require=True)


STAGE_JA = {"compose": "組版", "release": "発行", "collect": "収集", "classify": "出典の判定", "watch": "監視", "update": "道具の更新",
            "backlog": "保管していた指摘"}


def editor_report(stage: str, date: str, fix: dict, verdict: str | None, rounds: int, n_later: int, path, branch: str) -> str:
    """編集長に届く1通(Discord の1通に収まる長さ)。中身は当番が編集長向けに書いた要約(editor_summary)で、
    ファイル名や差分は載せず、記録の場所だけを示す。"""
    from pipelib import editor_notice
    s = fix.get("editor_summary") if isinstance(fix.get("editor_summary"), dict) else {}
    head = {"fixed": "🛠 当番が直しました", "no_fix_needed": "🛠 当番が調べました(直す箇所なし)"}.get(fix.get("status"), "🛠 当番が調べました")
    md = f"{int(date[5:7])}/{int(date[8:10])}" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else date
    footer = (f"記録(差分・検証の全文): {path} / 監査: {'承認' if verdict == 'approve' else (verdict or '不明')}({rounds}往復)"
              + (f" / 後で直す指摘 {n_later}件" if n_later else ""))
    return editor_notice(f"{head}: {md}号の{STAGE_JA.get(stage, stage)}",
                         [(label, s.get(k) or "(書かれていない。記録を参照)") for k, label in SUMMARY_LABELS], footer=footer)


# 編集長への報告の項目(編集長 2026-10-04「何故こういう事が起きたか、どう修正したか、他に当該事象の類似が起きないことは説明できるのか」)
SUMMARY_LABELS = (("what_happened", "起きたこと"), ("why", "なぜ起きたか"), ("what_changed", "どう直したか"),
                  ("similar", "同じ型の箇所"), ("paper_impact", "紙面への影響"))
SUMMARY_KEYS = tuple(k for k, _ in SUMMARY_LABELS)


BACKLOG = ROOT / "metrics" / "oncall-backlog.jsonl"      # Git 管理外(作業ツリーを汚さない=発行を妨げない)


def backlog_rows() -> list[dict]:
    """保管してある「発行後に直す」指摘。まだ何も保管していない(ファイルが無い)なら []。
    **ファイルを読めない(権限・I/O)ときは例外**: 「残件なし」と読むと、指摘が誰にも直されないまま消える(監査指摘)。
    壊れた行は飛ばして、その行番号を BACKLOG_BROKEN に残す(1行の壊れで他の指摘まで止めない。watch が異常として名指しする)。"""
    BACKLOG_BROKEN.clear()
    try:
        text = BACKLOG.read_text(encoding="utf-8")
    except FileNotFoundError:     # 不在とみなすのはこれだけ(exists() は権限エラーでも False を返す。監査指摘)
        return []
    rows = []
    for i, ln in enumerate(text.splitlines(), 1):
        if not ln.strip():
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            d = None
        if isinstance(d, dict) and d.get("key"):
            rows.append(d)
        else:
            BACKLOG_BROKEN.append(i)
    return rows


BACKLOG_BROKEN: list[int] = []      # 直前に読んだ保管の、壊れた行の行番号


def backlog_add(items: list[dict], date: str, stage: str, head: str) -> list[dict]:
    """監査の later を保管する。key は中身から作る(同じ指摘を二度積まない)。戻り値は今回新しく積んだもの。"""
    have = {r["key"] for r in backlog_rows()}
    new = []
    for it in items:
        key = hashlib.sha256(str(it.get("claim") or "").encode("utf-8")).hexdigest()[:10]
        # 起きる道筋(どういうときに・どのくらい)の無い指摘は積まない(「起きないもの」を後の仕事にしない)
        if not it.get("claim") or not str(it.get("occurs") or "").strip() or key in have:
            continue
        have.add(key)
        new.append({"key": key, "status": "open", "at": datetime.datetime.now().isoformat(timespec="minutes"),
                    "date": date, "stage": stage, "fix_commit": head[:10], "id": str(it.get("id") or ""),
                    "claim": str(it["claim"]), "evidence": str(it.get("evidence") or ""), "occurs": str(it["occurs"])})
    if new:
        BACKLOG.parent.mkdir(parents=True, exist_ok=True)
        with open(BACKLOG, "a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return new


def backlog_open() -> list[dict]:
    """未着手のもの(同じ key の最後の行が open)。"""
    last: dict[str, dict] = {}
    for r in backlog_rows():
        last[r["key"]] = {**last.get(r["key"], {}), **r}
    return [r for r in last.values() if r.get("status") == "open"]


def backlog_done(keys: list[str]) -> list[str]:
    """消し込み。追記だけで行う(行を書き換えない)。戻り値は消し込めた key。"""
    open_keys = {r["key"] for r in backlog_open()}
    done = [k for k in keys if k in open_keys]
    if done:
        with open(BACKLOG, "a", encoding="utf-8") as f:
            for k in done:
                f.write(json.dumps({"key": k, "status": "done",
                                    "done_at": datetime.datetime.now().isoformat(timespec="minutes")}, ensure_ascii=False) + "\n")
    return done


def keep_later(items: list[dict], date: str, stage: str, head: str) -> int:
    """later を保管する。**例外を出さない**(保管の失敗で発行を止めない。later は修正報告に必ず載る)。
    戻り値は新しく積んだ件数。"""
    try:
        new = backlog_add(items, date, stage, head)
        if new:
            print(f"発行後に直す指摘を {len(new)}件 保管した({BACKLOG.name})", flush=True)
        return len(new)
    except Exception as e:      # noqa: BLE001
        print(f"発行後に直す指摘の保管に失敗({type(e).__name__}: {e})。報告には載せる", flush=True)
        return 0


def arm_backlog_exit(keys: list[str], date: str) -> None:
    """どの経路で終わっても(前提の失敗・取り込みの失敗・例外・unit の停止 SIGTERM)、残った指摘を名指しで知らせる。
    既定の SIGTERM では atexit が走らないので、通常の終了(SystemExit)に変える(監査指摘)。
    SIGKILL などで報告できなくても、指摘は保管に残り翌朝また渡る。"""
    import atexit
    import signal as _signal
    atexit.register(lambda: report_left_backlog(keys, date))
    _signal.signal(_signal.SIGTERM, lambda *_: sys.exit(143))


def report_left_backlog(keys: list[str], date: str) -> None:
    """--stage backlog の終わりに、渡した指摘のうちまだ残っているものを名指しで知らせる(全経路で。atexit)。
    残った指摘は保管に残り、翌朝の監視がまた当番に渡す。"""
    try:
        left = [r for r in backlog_open() if r["key"] in set(keys)]
    except (OSError, ValueError) as e:
        notify("oncall", f"{date} 保管していた指摘が残ったか確かめられない({type(e).__name__}: {e})", ok=False)
        return
    if left:
        notify("oncall", f"保管していた指摘のうち {len(left)}件は直っていないので残す(翌朝の監視がまた当番に渡す):\n"
                         + "\n".join(f"- [{r['key']}] {str(r.get('claim') or '')[:120]}" for r in left), ok=False)


def backlog_reason(rows: list[dict]) -> str:
    """保管していた指摘を当番に直させる依頼の理由(本文は prompts/oncall-backlog.md)。"""
    return render_prompt("oncall-backlog", N=len(rows), ITEMS=backlog_items_text(rows))


def later_text(items: list[dict]) -> str:
    if not items:
        return "発行後に直す指摘(later): なし"
    return (f"発行後に直す指摘(later){len(items)}件(metrics/oncall-backlog.jsonl に保管。`python3 scripts/oncall.py --backlog` で一覧):\n"
            + "\n".join(f"- {it.get('claim', '')[:300]}"
                        + (f"\n  起きるとき: {it['occurs'][:300]}" if it.get("occurs") else "")
                        + (f"\n  根拠: {it['evidence'][:300]}" if it.get("evidence") else "") for it in items))


def resume_point(state: dict, base: str) -> dict | None:
    """前の試行が直し切れずに残した続き(state["wip"])から始められるなら、それを返す。

    続けてよいのは: 基準の main が同じ、残した commit がこのリポジトリにあって基準の子孫、
    当番の報告と残った指摘が揃っている、のとき。どれかが欠ければ None(ゼロから診断する)。
    """
    wip = state.get("wip")
    if not isinstance(wip, dict) or wip.get("base") != base:
        return None
    head, fix, open_ = wip.get("head"), wip.get("fix"), wip.get("open")
    # head == base(差分の無い診断に指摘が付いたまま終わった)も続きにする。続けたあとも毎往復、基準からの
    # 差分の全体を freeze → 範囲検査 → selfcheck → 監査に通すので、監査していない内容は入らない(監査指摘)
    if not (isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head)
            and isinstance(fix, dict) and fix and isinstance(open_, list) and open_
            and all(isinstance(o, dict) for o in open_)):
        return None
    if sh(["git", "cat-file", "-e", f"{head}^{{commit}}"], cwd=ROOT).returncode != 0:
        return None
    if sh(["git", "merge-base", "--is-ancestor", base, head], cwd=ROOT).returncode != 0:
        return None
    return wip


MIN_STEP_SEC = 120      # これより残りが短ければ、次のセッション(当番・監査)を始めない


def run_rounds(stage: str, date: str, context: str, wt: Path, base: str, wip: dict | None,
               env_before: str, transcript: list[dict]) -> dict:
    """当番の修正 → 監査、の往復。指摘が無くなる(approve で must_fix が空)まで回す。

    戻り値: {"approved", "fix", "head", "diff", "changed", "kept", "error"}。
    - 上限は回数(MAX_ROUNDS)と**時間**(ONCALL_LIMIT_MIN)。時間は各セッションの直前に残りを確かめ、
      セッションの timeout も残り時間で切る(「往復を始めてよいか」だけを見ると、残り1秒から 65 分走れる。監査指摘)
    - `kept` は**最後に内容を固定できた(freeze して範囲検査を通った)ところ**の {head, fix, open}。
      例外・時間切れで終わっても、次の試行はここから続けられる(監査指摘)。差分が無い診断も残す
    - 例外はここで受けて `error` に入れる(呼び出し側の共通の終了処理が state を保存できるように)
    """
    schemas = ROOT / "schema"
    deadline = min(time.time() + ONCALL_LIMIT_MIN * 60, DEADLINE_AT or float("inf"))
    left = lambda: int(deadline - time.time())
    objections: list[dict] | None = None
    integ: dict | None = None
    fix: dict = {}
    head, diff, changed = base, "", []
    kept = {"head": base, "fix": {}, "open": []}
    res = {"approved": False, "error": ""}
    later: list[dict] = list((wip or {}).get("later") or [])     # 発行後でよい指摘(往復・試行をまたいで積む)
    try:
        if wip:    # 前の試行の続き: その commit と、残った指摘から始める
            # kept を**先に**前の続きで満たす(この先の checkout が失敗しても、残してあった続きを消さない。監査指摘)
            kept = {"head": wip["head"], "fix": json.loads(json.dumps(wip["fix"])), "open": list(wip["open"])}
            if wip["head"] != base:
                must(sh(["git", "checkout", "-q", "--detach", wip["head"]], cwd=wt), "前の試行の続きの checkout")
            fix, objections = dict(wip["fix"]), list(wip["open"])
            head = wip["head"]
            transcript.append({"round": 0, "resumed_from": head, "open": objections, "carried_later": list(later)})
            print(f"前の試行の続きから({head[:10]}、残った指摘 {len(objections)}件)", flush=True)
        for rnd in range(1, MAX_ROUNDS + 1):
            if left() < MIN_STEP_SEC:
                transcript.append({"round": rnd, "time_up": ONCALL_LIMIT_MIN})
                break
            print(f"当番 {rnd}回目", flush=True)
            if not fix:
                fix = run_claude(fix_prompt(stage, date, context, None), schemas / "oncall-fix.schema.json", wt,
                                 timeout=min(1800, left()))
                transcript.append({"round": rnd, "fix": fix})
            else:
                integ = run_claude(fix_prompt(stage, date, context, objections),
                                   schemas / "oncall-integrate.schema.json", wt, timeout=min(1800, left()))
                transcript.append({"round": rnd, "integrate": integ})
                # 監査の指摘で status(誤診の訂正)や再実行の仕方を改めたなら、それを最終判断にする(監査指摘)
                apply_integrate(fix, integ)
            # 作業ツリーの外への副作用は、答えが何であれ**毎回**検める(cannot_fix でも省かない。監査指摘):
            # 本体が汚れた / origin/main が動いた / 本体の .env(Git 管理外)が変わった
            if not root_clean():
                transcript.append({"round": rnd, "root_touched": sh(["git", "status", "--short"], cwd=ROOT).stdout[:500]})
                fix.update(status="cannot_fix", notes="当番が本体の作業ツリーに触った。取り込まない")
                break
            if remote_main() != base:
                transcript.append({"round": rnd, "remote_moved": True})
                fix.update(status="cannot_fix", notes="セッション中に origin/main が動いた(当番が push した疑い、または他の工程)。取り込まない")
                break
            if env_fingerprint() != env_before:
                transcript.append({"round": rnd, "env_touched": True})
                fix.update(status="cannot_fix", notes="当番が本体の .env に触った。取り込まない")
                break
            if fix.get("status") == "cannot_fix":
                break
            head, diff, changed = freeze(wt, base, rnd)
            bad = out_of_bounds(changed)
            if bad:
                transcript.append({"round": rnd, "out_of_bounds": bad})
                fix.update(status="cannot_fix", notes=f"触ってはいけないファイルを変えた: {bad}")
                break
            if len(diff) > DIFF_LIMIT:
                transcript.append({"round": rnd, "diff_too_large": len(diff)})
                fix.update(status="cannot_fix", notes=f"差分が大きすぎて監査できない({len(diff)} 字)")
                break
            # 報告と差分の整合(監査指摘): 直したと言うのに差分が無い / 欠陥ではないと言うのに差分がある
            if not status_consistent(str(fix.get("status") or ""), changed):
                transcript.append({"round": rnd, "status_mismatch": {"status": fix.get("status"), "diff": bool(diff.strip())}})
                fix.update(status="cannot_fix", notes="報告の status と差分が食い違う")
                break
            # ここまで来た内容は固定できている。監査がまだなので、その旨を残りの指摘に足しておく
            # (このあと例外・時間切れで終わっても、次の試行が「監査から」続けられる)
            pending_review = {"id": "review-incomplete", "severity": "quality", "evidence": "",
                              "claim": f"{rnd}往復目の修正は監査が終わっていない(前の指摘が直ったかは未確認)。直っているか確かめ、足りなければ直す"}
            objections = [o for o in (objections or []) if o.get("id") != "review-incomplete"]   # 印は毎往復1つだけ
            kept = {"head": head, "fix": json.loads(json.dumps(fix)), "open": objections + [pending_review]}
            if left() < 30:
                transcript.append({"round": rnd, "time_up": ONCALL_LIMIT_MIN})
                break
            sc = sh([sys.executable, "scripts/selfcheck.py"], cwd=wt, timeout=min(300, left()))
            if sc.returncode != 0:
                transcript.append({"round": rnd, "selfcheck": sc.stdout[-500:]})
                # 前の指摘は**まだ監査で確かめられていない**ので捨てない。selfcheck の赤を足す(置き換えない。監査指摘)
                objections = ([o for o in (objections or []) if o.get("id") != "selfcheck"]
                              + [{"id": "selfcheck", "claim": "selfcheck が赤", "evidence": sc.stdout[-400:],
                                  "severity": "blocks_publish"}])
                kept["open"] = list(objections) + [pending_review]
                continue
            if left() < MIN_STEP_SEC:
                transcript.append({"round": rnd, "time_up": ONCALL_LIMIT_MIN})
                break
            print(f"監査 {rnd}回目", flush=True)
            # 差分が無くても(no_fix_needed)監査は通す: 誤診を検める機会を残す(監査指摘)
            rev = run_codex(review_prompt(stage, date, fix, diff, integ), schemas / "oncall-review.schema.json", wt,
                            timeout=min(1800, left()))
            transcript.append({"round": rnd, "review": rev})
            round_later = []
            for item in rev.get("later") or []:
                if not (isinstance(item, dict) and item.get("claim")):
                    continue
                if not str(item.get("occurs") or "").strip():
                    # 起きる道筋(どういうときに・どのくらい)が書かれていない指摘は保管もしない(形の検査)
                    print(f"  later を捨てた(起きる道筋が書かれていない): {str(item['claim'])[:80]}", flush=True)
                    continue
                round_later.append({"id": str(item.get("id") or ""), "claim": str(item["claim"]),
                                    "evidence": str(item.get("evidence") or ""), "occurs": str(item["occurs"])})
            must_fix = list(rev.get("must_fix") or [])
            if stage == BACKLOG_STAGE:
                # 保管していた指摘を直す昼の仕事では、「後でよい」指摘もその場で直す(また保管に積むと、直すリストが減らない)
                must_fix += [dict(x, severity="quality") for x in round_later]
            else:
                for item in round_later:
                    if not any(x.get("claim") == item.get("claim") for x in later):
                        later.append(item)
            if rev.get("verdict") == "approve" and not must_fix:
                res["approved"] = True
                kept["open"] = []
                break
            # reject なのに must_fix が空、という答えでも次の往復が「指摘なし」で始まらないようにする
            objections = must_fix or [{"id": "reject-without-items", "severity": "quality", "evidence": "",
                                                  "claim": "監査は reject したが must_fix が空だった: " + str(rev.get("notes") or "")[:600]}]
            kept["open"] = list(objections)
    except Exception as e:      # noqa: BLE001 — 何で終わっても、固定できたところまでは次へ残す
        res["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        transcript.append({"error": res["error"]})
    kept["later"] = later
    res.update(fix=fix, head=head, diff=diff, changed=changed, kept=kept, later=later)
    return res


def needs_full_rerun(changed: list[str]) -> bool:
    return any(p.startswith(pre) for p in changed for pre in FULL_RERUN_IF)


def status_consistent(status: str, changed: list[str]) -> bool:
    """報告の status と差分の整合: fixed ⇔ 回帰テスト以外のファイルを変えた。
    「欠陥ではないが回帰テストだけ足した」(no_fix_needed + test_pipeline.py の差分)は許す
    (配管テスト 2026-09-12 で、テストだけ足した2巡目が status_mismatch で落ちた)。"""
    code_changed = any(p != "scripts/test_pipeline.py" for p in changed)
    return (status == "fixed") == code_changed


def apply_integrate(fix: dict, integ: dict) -> dict:
    """2巡目以降の当番の答え(監査の指摘の取り込み)を最終報告に反映する(監査指摘)。
    status(no_fix_needed → fixed、fixed → no_fix_needed の収束)と rerun_mode は 'unchanged' 以外なら更新。"""
    if integ.get("status") in ("fixed", "no_fix_needed", "cannot_fix"):
        fix["status"] = integ["status"]
    if integ.get("rerun_mode") in ("rebuild", "resume", "none"):
        fix["rerun_mode"] = integ["rerun_mode"]
    for k in ("diagnosis", "root_cause", "recovery"):
        if str(integ.get(k) or "").strip():
            fix[k] = integ[k]
    # 編集長向けの要約は、この巡の最終の内容で置き換える(訂正したのに初回の要約が届く、を防ぐ。監査指摘 r118)
    s = integ.get("editor_summary")
    if isinstance(s, dict) and all(str(s.get(k) or "").strip() for k in SUMMARY_KEYS):
        fix["editor_summary"] = s
    # 保管していた指摘の1件ずつの扱いも、この巡の最終の内容で置き換える(全件を書き直す契約)
    # (空の配列も最終報告として置き換える。初稿の扱いを残すと、最終巡で扱っていない指摘まで消し込む。監査指摘)
    if isinstance(integ.get("backlog_items"), list):
        fix["backlog_items"] = integ["backlog_items"]
    # 2巡目の変更・検証・リスクも最終報告に**累積**する(初稿の分を消さない。監査指摘)
    if integ.get("changed_files"):
        fix["changed_files"] = list(dict.fromkeys(list(fix.get("changed_files") or []) + list(integ["changed_files"])))
    if str(integ.get("test_evidence") or "").strip():
        fix["test_evidence"] = (str(fix.get("test_evidence") or "") + "\n[監査後の再検証] " + integ["test_evidence"]).strip()
    new_lines = [l.strip() for l in str(integ.get("risk") or "").splitlines() if l.strip()]
    if new_lines:
        # 項目(行)単位の完全一致で重複を除く。新しい側も行に割る(部分一致だと別の risk を捨て、
        # 複数行を塊で比べると同じ項目を二重に積む。監査指摘)
        items = [l.strip() for l in str(fix.get("risk") or "").splitlines() if l.strip()]
        for line in new_lines:
            if line not in items and "[監査後の変更で] " + line not in items:
                items.append("[監査後の変更で] " + line)
        fix["risk"] = "\n".join(items)
    return fix


def rerun_policy(stage: str, changed: list[str], rerun_mode: str, recover: bool = False) -> tuple[bool, str]:
    """再実行の仕方を決める(テストで固定するため関数にする。監査指摘)。戻りは (作り直すか, 表示名)。

    生成層(FULL_RERUN_IF)を直したか、当番が rebuild と答えたら、**stage を問わず**号を作り直す
    (release 起点でも生成済みの号をそのまま発行しない)。none なら再実行しない。
    recover(承認済みの号で落とした記事を戻す)は、何を直しても・当番が none と答えても、書けている記事はそのままに
    落とした記事だけを書き直して戻す(--reuse-plan)。承認済みの紙面を作り直さない(控えを取り、悪くなれば控えに戻す)
    """
    if recover and stage == "compose":
        return False, "落とした記事をこの号に戻す(--reuse-plan)"
    if rerun_mode == "none":
        return False, "none"
    if stage == "classify":
        # 出典の判定は号を作らない。パーサを足したら、その号の判定(合議 → 付け直し → lint)をやり直すだけ
        # (prompts/ を触っても組版はやり直さない。組版はまだ走っていないか、走るなら 03:00 に新しいコードで走る)
        return False, "出典の判定のやり直し(classify_retag_lint)"
    if stage == "collect":
        # 収集の当番は、依頼文などの生成層を直しても号を作り直さない。やるのは定点観測の取り直しだけ(落とした新着を
        # その号の素材に入れる)。号はまだ組んでいないか、組むなら新しいコードで組まれる(監査指摘: 作り直しに入ると
        # 取り直しを飛ばして組版を始め、次の定時工程も待たせる)
        return False, "定点観測の取り直し(落とした新着をその号の素材へ)"
    full = needs_full_rerun(changed) or rerun_mode == "rebuild"
    if full:
        return True, "作り直し(compose 全工程" + (" → release" if stage == "release" else "") + ")"
    if stage == "compose":
        return False, "続き(--reuse-plan)"
    if stage in ("collect", "watch"):
        return False, "続き(定点観測の繰り越しを拾い直す)"
    return False, "続き(release)"


def commit_paths(paths: list[str], msg: str, branch: str, push_timeout: int = 120) -> None:
    """対象パスだけを stage して commit・push(包括的な add -A は使わない。監査指摘)。"""
    must(sh(["git", "add", "-A", "--"] + paths, cwd=ROOT), "add")
    if must(sh(["git", "status", "--porcelain"], cwd=ROOT), "git status").stdout.strip():
        must(sh(["git", "commit", "-q", "-m", msg + CO_AUTHOR], cwd=ROOT), "commit")
        must(sh(["git", "push", "-q", "origin", branch], cwd=ROOT, timeout=push_timeout), "push")


def rollback_in_subprocess(date: str) -> list[str]:
    """stock からこの号の寄与を剥がす(assemble.rollback)。**取り込んだあとのコード**を別プロセスで
    走らせる。当番が assemble 自体を直した場合、この process に読み込み済みの古い実装で
    stock を触ってはいけない(監査指摘)。"""
    code = ("import sys; sys.path.insert(0, 'scripts'); import assemble; "
            "print('\\n'.join(assemble.rollback(sys.argv[1])))")
    r = must(sh([sys.executable, "-c", code, date], cwd=ROOT, timeout=600), "assemble.rollback")
    return [l for l in r.stdout.splitlines() if l.strip()]


def edition_artifacts(date: str) -> list[str]:
    """その号の成果物のパス(glob)。作り直しで外す・復元で掃除する対象。社説(EDITORIAL_UNTIL 以前の号)も含む(監査指摘)。"""
    return [f"docs/_posts/{date}-*.md", f"docs/_editions/{date}.md", f"docs/_editorials/{date}.md",
            f"metrics/plan-{date}*.json", f"metrics/review-{date}-*.json",
            f"metrics/stories-before-{date}.yml", f"metrics/pending-before-{date}.yml"]


def untracked_files() -> set[str] | None:
    """作業ツリーの未追跡ファイル(.gitignore の対象は除く)。一覧を取れなければ None(復元はこの号の成果物だけを掃除し、
    clean でなければ止まって人へ渡す)。"""
    r = sh(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT)
    return {x for x in r.stdout.split("\0") if x} if r.returncode == 0 else None


def restore_edition(date: str, edition: str, backup: str, untracked_before: set[str] | None = None) -> None:
    """号を控えのブランチへ戻す。untracked_before を渡せば、そのあと(作り直し・組み直しの間)に新しく出来た未追跡ファイルも
    消す(組版が作った先日付の予約 stock/scheduled/ 等。残ると clean 判定で復元も発行も止まる。開始前からあったものは消さない)。"""
    sh(["git", "merge", "--abort"], cwd=ROOT)
    must(sh(["git", "checkout", "-q", "-f", edition], cwd=ROOT), f"{edition} の checkout")
    must(sh(["git", "reset", "-q", "--hard", backup], cwd=ROOT), "控えへの reset")
    # reset は未追跡の生成物(作り直し中に compose が作った記事・号・計画・校閲記録)を消さない。
    # 残ると次の工程が clean 判定で拒否される。**この号のものだけ**を対象に消す(広い git clean は使わない。監査指摘)
    must(sh(["git", "clean", "-q", "-f", "--"] + edition_artifacts(date), cwd=ROOT), "生成物の掃除")
    now_untracked = untracked_files() if untracked_before is not None else None
    if now_untracked is not None:
        new = sorted(now_untracked - untracked_before)
        if new:
            must(sh(["git", "clean", "-q", "-f", "--"] + new, cwd=ROOT), "作り直しの間に出来た未追跡ファイルの掃除")
    # 判定不能(status の失敗)は clean 扱いにしない(監査指摘)
    if must(sh(["git", "status", "--porcelain"], cwd=ROOT), "git status").stdout.strip():
        raise RuntimeError(f"控えへ戻したが作業ツリーが clean でない: {sh(['git', 'status', '--short'], cwd=ROOT).stdout[:300]}")
    must(sh(["git", "push", "-q", "--force-with-lease", "origin", edition], cwd=ROOT, timeout=120), "控えへ戻す push")


def backup_edition(date: str, edition: str) -> str:
    """号の控えのブランチを作って push する(戻せるようにしてから号に手を入れる)。戻り値は控えのブランチ名。"""
    backup = f"backup/{date}-{int(time.time())}"
    must(sh(["git", "branch", backup, edition], cwd=ROOT), "控えブランチの作成")
    must(sh(["git", "push", "-q", "origin", backup], cwd=ROOT, timeout=120), "控えブランチの push")
    return backup


def reset_edition(date: str, edition: str) -> str:
    """号を作り直せる状態にする。戻せるように控えのブランチを push してから、stock の寄与を剥がし、
    成果物を外す。**控えを作ったあとはどこで失敗しても控えへ戻す**(監査指摘)。戻り値は控えのブランチ名。"""
    backup = backup_edition(date, edition)
    try:
        for line in rollback_in_subprocess(date):
            print(f"  {line}", flush=True)
        # 組版前の控え(metrics/*-before-<日付>.yml)は**残す**(消すと組版後の状態を控え直す。監査指摘)
        for p in list((ROOT / "docs" / "_posts").glob(f"{date}-*.md")) \
                + [ROOT / "docs" / "_editions" / f"{date}.md", ROOT / "docs" / "_editorials" / f"{date}.md"] \
                + list((ROOT / "metrics").glob(f"plan-{date}*.json")) + list((ROOT / "metrics").glob(f"review-{date}-*.json")):
            if p.exists():
                p.unlink()
        commit_paths(["docs/_posts", "docs/_editions", "docs/_editorials", "metrics", "stock"],
                     f"oncall: {date} 号を作り直すため成果物と stock の寄与を外す(控え: {backup})", edition)
    except Exception as e:
        try:
            restore_edition(date, edition, backup)
            notify("oncall", f"{date}: 作り直しの準備に失敗({str(e)[:200]})。{edition} を控え {backup} に戻した", ok=False)
        except Exception as e2:
            notify("oncall", f"{date}: 作り直しの準備に失敗し、控え {backup} への復元も失敗: {e2}。**手で戻すこと**", ok=False)
        raise
    return backup


CLASSIFY_RERUN = ("import sys; sys.path.insert(0, 'scripts'); import pipelib; "
                  "ok, why = pipelib.classify_retag_lint(sys.argv[1], require_parsers=True); print(why); sys.exit(0 if ok else 1)")


def ensure_pushed(branch: str, what: str, budget: int = 300) -> bool:
    """branch のローカルの commit がリモートに届いているかを確かめ、届いていなければ送る。届かなければ名指しで通知して False。
    budget(秒)は fetch と push に使ってよい合計(期限のある後始末から呼ぶとき、残り時間で切る)。"""
    try:
        sh(["git", "fetch", "-q", "origin", branch], cwd=ROOT, timeout=max(10, budget // 3))
        ahead = sh(["git", "rev-list", "--count", f"origin/{branch}..{branch}"], cwd=ROOT).stdout.strip()
        if ahead in ("", "0"):
            return True
        r = sh(["git", "push", "-q", "origin", branch], cwd=ROOT, timeout=max(10, budget * 2 // 3))
        if r.returncode == 0:
            print(f"{what}: リモートに届いていなかった commit {ahead}件を送った", flush=True)
            return True
        why = (r.stderr or r.stdout or "")[-300:]
    except (subprocess.TimeoutExpired, OSError) as e:
        why = f"{type(e).__name__}: {e}"
    notify("oncall", f"{what}がリモート({branch})に届いていない。次の工程がリモートから号を取り直すと消える: {why}", ok=False)
    return False


def run_stage(cmd: list[str], log: Path, timeout: int) -> int:
    """再実行する工程を走らせる。時間切れなら子(モデルのセッション)ごと落とす(親だけ落とすと子が作業ツリーを触り続ける)。"""
    from pipelib import reap
    become_subreaper()
    with log.open("a", encoding="utf-8") as f:
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             env={**os.environ, "ONCALL": "off", "IMAS_JOB_LOCK": "held"}, start_new_session=True)
        # 組版は執筆・校閲のセッションを別のセッション(プロセスグループ)で起動するので、親のグループを落としても残る。
        # 当番は subreaper なので、親を失った孫もこの process の子に付け替わる。**終わり方を問わず**(時間切れ・異常終了・
        # 正常終了・待機中の例外)、子孫が1つも残らなくなるまで、辿って落として刈り取るのを繰り返してから返す(写しを
        # 1回取るだけだと取ったあとに生まれた孫が、時間切れのときだけだと異常終了で残った孫が、控えへ戻した作業ツリーに
        # 書き込み、発行の clean 判定を壊す。監査指摘)
        try:
            return p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            reap(p)
            return 124
        finally:
            if p.poll() is None:
                reap(p)
            kill_all_descendants()


def become_subreaper() -> None:
    """この process を subreaper にする(PR_SET_CHILD_SUBREAPER)。子孫の親が死ぬと、孫は init ではなくこの process の子になる。"""
    import ctypes
    try:
        ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def kill_all_descendants(limit_sec: float = 15) -> list[int]:
    """この process の子孫を、残らなくなるまで落として刈り取る(subreaper の下で使う)。戻りは最後まで残った pid。"""
    import signal as _signal
    end = time.time() + limit_sec
    left: list[int] = []
    while time.time() < end:
        left = [q for q in descendants(os.getpid()) if _proc_state(q) not in ("Z", "X")]
        for q in left:
            try:
                os.kill(q, _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        while True:   # 子になったものを刈り取る(ゾンビのまま残さない)
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        if not left:
            break
        time.sleep(0.05)
    return left


def descendants(pid: int) -> list[int]:
    """pid の子孫(/proc の親子関係を辿る。別セッション・別グループの孫も含む)。"""
    children: dict[int, list[int]] = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            ppid = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(d.name))
    out, todo = [], [pid]
    while todo:
        for c in children.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


def _proc_state(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return "X"


RECOVER_TRIED = False   # 落とした記事を戻す組み直しを、この当番で既に試みたか(main の後段が二度目を走らせない)


def posted_slugs(date: str) -> set[str]:
    """その号の紙面にいまある記事の slug。"""
    return {p.stem[len(date) + 1:] for p in (ROOT / "docs" / "_posts").glob(f"{date}-*.md")}


def planned_slugs(date: str) -> list[str]:
    """その号の計画にある記事の slug(計画が読めなければ空)。"""
    try:
        return [a["slug"] for a in json.loads((ROOT / "metrics" / f"plan-{date}.json").read_text(encoding="utf-8"))["articles"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def latest_review(date: str) -> Path | None:
    """その号のいちばん新しい巡の校閲記録(metrics/review-<日付>-<巡>.json)。無ければ None。"""
    rows = [(int(m.group(1)), p) for p in (ROOT / "metrics").glob(f"review-{date}-*.json")
            if (m := re.fullmatch(re.escape(f"review-{date}-") + r"(\d+)\.json", p.name))]
    return max(rows)[1] if rows else None


def recover_outcome(before: set[str], after: set[str], planned: list[str]) -> tuple[list[str], list[str], list[str]]:
    """組み直しの結果: (戻った記事, まだ戻らない記事, 承認済みだったのに失った記事)。失った記事があれば控えに戻す。"""
    return (sorted(after - before), [s for s in planned if s not in after], sorted(before - after))


def rerun_stage(stage: str, date: str, edition: str, full: bool, recover: bool = False) -> int:
    """止まった工程を再実行する。作り直し(full)なら、release 起点でも compose を先頭から走らせてから
    release する(生成層を直したのに生成済みの号をそのまま発行しない。監査指摘)。
    recover(承認済みの号で落とした記事を戻す)は、控えを取ってから --reuse-plan で組み直し、発行の締切(STAGE_END_AT)までに
    承認されて、承認済みだった記事を1本も失っていなければ採る。それ以外は控え(承認済みの紙面)に戻す。"""
    global RECOVER_TRIED
    log = ROOT / "metrics" / f"oncall-{date}-{stage}-rerun.log"
    log.write_text("", encoding="utf-8")
    compose_py = str(ROOT / "scripts" / "compose.py")
    backup = ""
    code = 1
    done = False   # compose と(release 起点なら)release が**全部**終わったときだけ True(監査指摘)
    untracked_before = untracked_files()   # 控えへ戻すとき、この間に出来た未追跡ファイルだけを消す(監査指摘)
    try:
        if full:
            backup = reset_edition(date, edition)
            code = run_stage([sys.executable, compose_py, "--date", date], log, 7200)
        elif stage == "compose":
            limit, extra = 7200, []
            if recover:
                RECOVER_TRIED = True
                before, planned = posted_slugs(date), planned_slugs(date)
                backup = backup_edition(date, edition)
                limit = max(60, int((STAGE_END_AT or time.time() + 3600) - time.time()))
                # 承認済みの校閲結果を控えて組版に渡す(承認済みの記事の判定を引き継ぎ、今回書いた記事だけを校閲させる)
                carry = latest_review(date)
                if carry:
                    keep = ROOT / "metrics" / "work" / date / "review-carry.json"
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(carry, keep)
                    extra = ["--carry-review", str(keep)]
            for p in (ROOT / "metrics").glob(f"review-{date}-*.json"):
                p.unlink()   # 古い校閲記録が残ると release が最大巡数の古い判定を読む(監査指摘)
            commit_paths(["metrics"], f"oncall: {date} の古い校閲記録を外す", edition)
            code = run_stage([sys.executable, compose_py, "--date", date, "--reuse-plan", *extra], log, limit)
            if recover:
                back, still, lost = recover_outcome(before, posted_slugs(date), planned)
                if code == 0 and lost:
                    code = 1   # 戻すために承認済みの記事を失うなら採らない(下で控えに戻す)
                notify("oncall", f"{date} compose: 落とした記事を戻す組み直し(exit {code})。"
                                 f"戻った {len(back)}本: {', '.join(back) or 'なし'} / 戻らない {len(still)}本: {', '.join(still) or 'なし'}"
                                 + (f" / 承認済みだったのに組み直しで外れた {len(lost)}本: {', '.join(lost)}(採らずに控えへ戻す)" if lost else "")
                                 + ("" if code == 0 else "。承認済みの紙面(控え)で発行する"),
                       ok=(code == 0 and not still))
        elif stage == "classify":
            # 取り込んだあとのコードで、その号の出典の判定(合議 → 紙面の付け直し → lint)をやり直し、結果を commit する。
            # 取引が失敗すれば判定表と記事は開始時の中身に戻る(classify_retag_lint)
            code = run_stage([sys.executable, "-c", CLASSIFY_RERUN, date], log, 3600)
            if code == 0:
                commit_paths(["source_types.yml", "docs/_posts"], f"oncall: {date} 出典の判定をやり直す(パーサ追加後)", edition)
        elif stage in ("collect", "watch"):
            # 収集の異常(定点観測の facts 化が読めなかった等)を直したら、繰り越し(watch-state の
            # _pending)を**実際に拾い直す**。resume を no-op にすると、直した効きは次の定時収集まで
            # 来ず、この号の収集に間に合わない(当番 2026-10-02 の未処理6件)。探索(Luna)・Grok は
            # 今回の収集で済んでおり(Grok は週次セッション上限を食う)、やり直すのは定点観測だけでよい。
            #   - --date は取り込み先の号(edition)を渡す。渡さないと collect が壁時計から号を取り、
            #     06:00 境界をまたいだ往復や発行後 watch で別号を checkout してしまう(監査指摘)
            #   - --oncall-rerun は、直しが効かず再び読めなかったバッチを諦めさせない。既読にせず繰り越し、
            #     残れば collect が非0で返す(原因未確定のまま新着を失わない。監査指摘)
            target = edition.removeprefix("edition/")
            # 取り直しも次の定時工程の前に終える(STAGE_END_AT)。後始末(素材の確定・push・通知)の分
            # (CLEANUP_RESERVE_SEC)を残して打ち切り、後始末の git 操作も残り時間で切る(監査指摘: 後始末で期限を越えると
            # 組版が待つのをやめる)
            left = lambda: (STAGE_END_AT - time.time()) if STAGE_END_AT else 3600
            limit = max(60, min(3600, int(left() - CLEANUP_RESERVE_SEC)))
            code = run_stage([sys.executable, str(ROOT / "scripts" / "collect.py"),
                              "--skip-explore", "--skip-grok", "--oncall-rerun", "--date", target], log, limit)
            if not root_clean():
                # 時間切れ・異常終了で取り直しが途中で終わっても、保存できた素材と既読状態は確定させ、作業ツリーを
                # clean にして次の工程(組版・道具の更新)を止めない(監査指摘)。素材のファイルは丸ごと書き直す形なので途中は無い
                try:
                    commit_paths(["candidates", "stock/watch-state.json", "metrics"],
                                 f"oncall: {target} 取り直しの途中までを確定(exit {code})", edition,
                                 push_timeout=max(15, min(120, int(left() / 3))))
                except Exception as e:      # noqa: BLE001
                    notify("oncall", f"{date} collect: 取り直しの途中までを確定できない({type(e).__name__}: {e})", ok=False)
                if not root_clean():
                    notify("oncall", f"{date} collect: 取り直しのあと作業ツリーが clean でない。次の工程が止まりうる:\n"
                                     + sh(["git", "status", "--short"], cwd=ROOT).stdout[:500], ok=False)
            # clean でも、取り直しの commit がリモートに届いていなければ(push の途中で時間切れ・通信の停滞)、ここで送る。
            # 届かないまま次の工程がリモートから号を取り直すと、取り直した新着がその号の素材から消える(監査指摘)
            ensure_pushed(edition, f"{date} collect: 取り直した素材", budget=max(20, int(left() - 30)))
        else:
            code = 0
        if code == 0 and stage == "release":
            code = 1
            code = run_stage([sys.executable, str(ROOT / "scripts" / "release.py"), "--date", date], log, 1800)
        done = code == 0
    finally:
        # 控えを作ったあとは、失敗の種類を問わず(exit 非0・例外。release 起動時の例外も)控えへ戻す(監査指摘)
        if not done and backup:
            try:
                restore_edition(date, edition, backup, untracked_before)
                notify("oncall", f"{date} {stage}: 作り直しが失敗(exit {code})したので {edition} を控え {backup} に戻した", ok=False)
            except Exception as e:
                notify("oncall", f"{date} {stage}: 作り直しが失敗し、控え {backup} への復元も失敗: {e}。**手で戻すこと**", ok=False)
    return code


RECOVER_TARGET: tuple[str, str] | None = None   # 落とした記事を戻す号(日付, edition ブランチ)。main が設定する


def main() -> int:
    """当番の本体(_main)のあと、落とした記事を戻す当番(--recover)で組み直しをまだ試していなければ、いまのコードで1回試す。
    往復が承認されなかった・時間切れ・試行の上限・例外のどれで終わっても、戻すのを諦めない(編集長 2026-10-10
    「復帰保証までが Opus/Sol の仕事だ」)。直せなかったことと、戻せたかどうかは別に報告する。"""
    try:
        code = _main()
    except Exception as e:      # noqa: BLE001
        # 往復の前(fetch の失敗・通信の停滞 等)の例外でも、戻す仕事は続ける(監査指摘)
        if not (RECOVER and RECOVER_TARGET):
            raise
        notify("oncall", f"{RECOVER_TARGET[0]} compose: 当番の処理で例外({type(e).__name__}: {str(e)[:300]})。"
                         "落とした記事を戻す組み直しは続ける", ok=False)
        code = 1
    if not (RECOVER and RECOVER_TARGET and not RECOVER_TRIED):
        return code
    release_held_job_locks()   # _main が例外で抜けて掴んだままの工程の排他があれば離す(同じ process の別 fd でも flock は競合する)
    date, edition = RECOVER_TARGET
    left = (STAGE_END_AT or 0) - time.time()
    if left < RECOVER_RERUN_RESERVE_MIN * 60:
        notify("oncall", f"{date} compose: 発行の締切までに、落とした記事を戻す時間が無い(残り {int(left) // 60}分)。"
                         f"戻せない記事: {', '.join(s for s in planned_slugs(date) if s not in posted_slugs(date)) or 'なし'}", ok=False)
        return code or 1
    try:
        lock_fd = job_lock("oncall", wait_min=2)
    except JobLockTimeout as e:
        notify("oncall", f"{date} compose: 落とした記事を戻す組み直しに入れない({e})", ok=False)
        return code or 1
    try:
        got = recover_rerun(date, edition)
        return got if got is not None else (code or 1)
    finally:
        os.close(lock_fd)


def release_held_job_locks() -> None:
    """この process が開いたままの工程の排他(metrics/jobs.lock)の fd を閉じる。flock は open ごとなので、同じ process でも
    開き直した fd とは競合する。例外で抜けた経路が閉じ損ねても、後段の組み直しが排他を取れるようにする。"""
    target = (ROOT / "metrics" / "jobs.lock").resolve()
    for fd in Path("/proc/self/fd").iterdir():
        try:
            if Path(os.readlink(fd)) == target:
                os.close(int(fd.name))
        except (OSError, ValueError):
            pass


def recover_rerun(date: str, edition: str) -> int | None:
    """落とした記事を戻す組み直し(排他は呼び手が持つ)。往復と同じ前提(作業ツリーが clean・edition がリモートと一致)を
    確かめてから入る。前提を満たさなければ何も変えずに None(汚れを控えへの復元で消さない。監査指摘)。"""
    try:
        if not root_clean():
            notify("oncall", f"{date} compose: 作業ツリーが clean でないので、落とした記事を戻す組み直しに入れない:\n"
                             + sh(["git", "status", "--short"], cwd=ROOT).stdout[:500], ok=False)
            return None
        problem = ensure_edition(edition, ROOT)
        if problem:
            notify("oncall", f"{date} compose: {problem}。落とした記事を戻す組み直しに入れない", ok=False)
            return None
        must(sh(["git", "checkout", "-q", edition], cwd=ROOT), f"{edition} の checkout")
        return rerun_stage("compose", date, edition, False, recover=True)
    except Exception as e:      # noqa: BLE001
        notify("oncall", f"{date} compose: 落とした記事を戻す組み直しで例外: {type(e).__name__}: {str(e)[:300]}", ok=False)
        return 1


def _main() -> int:
    global DEADLINE_AT, STAGE_END_AT, RECOVER, RECOVER_TARGET
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=list(STAGES))
    ap.add_argument("--date")
    ap.add_argument("--reason", default="")
    ap.add_argument("--no-rerun", action="store_true")
    ap.add_argument("--edition", default="", metavar="YYYY-MM-DD",
                    help="修正を取り込む号の日付(既定は --date)。発行後の release・watch は異常の発生日と取り込み先の号が違う")
    ap.add_argument("--backlog", action="store_true", help="発行後に直す指摘(未着手)を一覧する")
    ap.add_argument("--backlog-done", nargs="+", metavar="KEY", help="直し終えた指摘を消し込む")
    ap.add_argument("--end-at", default="", metavar="EPOCH",
                    help="収集の当番: 取り直しまで含めて終える時刻(呼び出し側の収集が固定して渡す。起動し直しても延びない)。"
                         "組版の --recover: 落とした記事を戻し終える締切(発行の締切)")
    ap.add_argument("--recover", action="store_true",
                    help="組版の当番: 承認まで進んだが記事を落とした。直したあと --reuse-plan で落とした記事をこの号に戻す")
    ap.add_argument("--backlog-keys", nargs="+", default=[], metavar="KEY",
                    help="--stage backlog で渡した保管の指摘。取り込めたら(直す箇所なしの承認も)消し込む")
    a = ap.parse_args()
    if a.backlog or a.backlog_done:
        if a.backlog_done:
            done = backlog_done(a.backlog_done)
            print(f"消し込み: {', '.join(done) or 'なし'}"
                  + (f" / 見つからない: {', '.join(k for k in a.backlog_done if k not in done)}" if len(done) != len(a.backlog_done) else ""))
        rows = backlog_open()
        print(f"発行後に直す指摘(未着手){len(rows)}件")
        for r in rows:
            print(f"- [{r['key']}] {r.get('at', '')} {r.get('date', '')} {r.get('stage', '')} 修正 {r.get('fix_commit', '')}\n"
                  f"  {r.get('claim', '')}\n  起きるとき: {r.get('occurs', '')}\n  根拠: {r.get('evidence', '')}")
        return 0
    if not a.stage or not a.date:
        ap.error("--stage と --date が要る")
    date, stage = a.date, a.stage
    edition = f"edition/{a.edition or date}"
    if stage == BACKLOG_STAGE:
        # どの経路で終わっても(前提の失敗・取り込みの失敗・例外・扱わなかった指摘)、残った指摘を名指しで知らせる
        arm_backlog_exit(list(a.backlog_keys), date)
        try:
            BACKLOG_ROWS[:] = [r for r in backlog_open() if r["key"] in set(a.backlog_keys)]
        except (OSError, ValueError) as e:
            notify("oncall", f"{date} 保管していた指摘を読めない({type(e).__name__}: {e})。当番は何もしない", ok=False)
            return 1
        if not BACKLOG_ROWS:
            print("渡された保管の指摘は、もう残っていない", flush=True)
            return 0
        end = backlog_deadline(now_jst())
        if end is None:
            print("いまは定時工程・発行の時間帯に近いので、保管の指摘は直さない(翌朝また渡る)", flush=True)
            return 0
        # 往復はさらに取り込み・後始末の分(BACKLOG_MERGE_RESERVE_MIN)を残して終える
        DEADLINE_AT = end - BACKLOG_MERGE_RESERVE_MIN * 60
    elif stage == "compose" and a.recover and not a.no_rerun and a.end_at:
        # 組版は承認まで進んだが記事を落とした。直したあと、落とした記事をこの号に戻すまでが当番の仕事(編集長 2026-10-10
        # 「落としていいと誰が言った? 復帰保証までが Opus/Sol の仕事だ」)。往復は組み直しの時間を残して終える
        RECOVER, RECOVER_TARGET = True, (date, edition)
        STAGE_END_AT = float(a.end_at)
        DEADLINE_AT = STAGE_END_AT - RECOVER_RERUN_RESERVE_MIN * 60
    elif stage == "collect" and not a.no_rerun:
        # 収集で落とした新着は、直して**取り直し、その号の選定リスト(素材)に入れる**までが当番の仕事(編集長 2026-10-07
        # 「やらかしてドロップしたのは責任を持って修正して紙面に乗せろ」「正しくは選定リストにちゃんと乗せろ」)。
        # 終える時刻は呼び出し側の収集が固定して渡す(--end-at。起動し直しても延びない。監査指摘)。02:00 の収集なら組版が待つ分を含む。
        # 動いている間は印を置き、組版はそれを見て開始を待つ(pipelib.compose_lock_wait_min)
        STAGE_END_AT = float(a.end_at) if a.end_at else next_slot_end(now_jst())
        DEADLINE_AT = STAGE_END_AT - COLLECT_RERUN_RESERVE_MIN * 60
        if time.time() < STAGE_END_AT:
            # 収集が置いた印を引き継ぐ(手で起動したときは収集が置いていないので、ここで置く)。排他を取ったら held にする
            mark_collect_oncall(STAGE_END_AT, date)
            import atexit
            atexit.register(COLLECT_ONCALL_MARK.unlink, missing_ok=True)   # 置いた印そのもの(後から名前が差し替わっても他を消さない)

    # 工程の排他(collect/compose/release/当番で共通の flock)。起動直後は親の compose がまだ持って
    # いるので、ここで終わるまで待つ。以後、再実行が終わるまで持ち続ける(pgrep の隙間を作らない。監査指摘)。
    # backlog は待つのも往復の期限まで(期限を過ぎてロックを取ると、次の定時工程を待たせる。監査指摘)。
    # 収集は、取れた時点で COLLECT_MIN_WINDOW_MIN 残る時刻まで待つ(親の収集が終わるまで。02:00 の収集が 03:02 に呼んでも
    # 3分は待つ。backlog と同じ「期限-30分」では 0分になり、親が離す前に諦めて取り直せなかった。監査指摘)
    if stage == "collect" and STAGE_END_AT is not None:
        wait_min = max(0.0, min(WAIT_IDLE_MIN, (STAGE_END_AT - time.time()) / 60 - COLLECT_MIN_WINDOW_MIN))
    elif RECOVER:
        # 落とした記事を戻す当番も、親の組版が離すまで、組み直しの時間が残る時刻まで待つ(収集と同じ理由。期限-30分にしない)
        wait_min = max(0.0, min(WAIT_IDLE_MIN, (STAGE_END_AT - time.time()) / 60 - RECOVER_RERUN_RESERVE_MIN))
    else:
        wait_min = WAIT_IDLE_MIN if DEADLINE_AT is None else max(0, min(WAIT_IDLE_MIN, int((DEADLINE_AT - time.time()) // 60) - 30))
    try:
        lock_fd = job_lock("oncall", wait_min=wait_min)
    except JobLockTimeout as e:
        if stage == BACKLOG_STAGE:
            print(f"工程の排他を時間内に取れない({e})。保管の指摘は翌朝また渡る", flush=True)
            return 0
        notify("oncall", f"{date} {stage}: {e}。当番は諦める", ok=False)
        return 1
    if stage == "collect" and not a.no_rerun and time.time() < (STAGE_END_AT or 0):
        mark_collect_oncall(STAGE_END_AT, date, held=True)   # 受け渡しは済んだ(組版は譲るのをやめ、印の end_at まで待つ)
    need_min = 30 if stage == BACKLOG_STAGE else COLLECT_MIN_WINDOW_MIN - COLLECT_RERUN_RESERVE_MIN
    if DEADLINE_AT is not None and DEADLINE_AT - time.time() < need_min * 60:
        # ロックを取れた時点で、1往復(当番・監査)と取り込みの時間が残っていなければ始めない
        if RECOVER and STAGE_END_AT - time.time() >= RECOVER_RERUN_RESERVE_MIN * 60:
            # 直す往復の時間は無くても、組み直しの時間が残っていれば、いまのコードで落とした記事を戻しに行く(戻すのを諦めない)。
            # 排他は持ったまま(離すと、その隙に別の工程が取る)。往復と同じ前提の確認を通す(汚れた作業ツリーを控えへの
            # 復元で消さない。監査指摘)
            try:
                return recover_rerun(date, edition)
            finally:
                os.close(lock_fd)
        os.close(lock_fd)
        if stage == BACKLOG_STAGE:
            print("ロックを取れたが、期限までに1往復できない。保管の指摘は翌朝また渡る", flush=True)
            return 0
        if RECOVER:
            notify("oncall", f"{date} compose: 発行の締切までに、落とした記事を戻す時間が無い(残り {int(STAGE_END_AT - time.time()) // 60}分)。"
                             "戻せない記事:\n" + a.reason[-1500:], ok=False)
            return 1
        # 収集: 次の定時工程までに直して取り直す時間が無い。落とした新着がこの号に載らないことを、通知で終えずに名指しする
        # (次の収集で同じ失敗が起きれば、その収集がまた当番を呼ぶ)
        notify("oncall", f"{date} collect: 次の定時工程までに、直して取り直す時間が無い(残り {int((STAGE_END_AT or 0) - time.time()) // 60}分)。"
                         "収集で落とした新着は、この号に載らず1号遅れうる。異常の一覧:\n" + a.reason[-1500:], ok=False)
        return 1
    # 試行回数はロックの中で数える・判定する・増やす(同時起動で上限をすり抜けない。監査指摘)。
    # 回数は印ファイルの個数(JSON の壊れ・書き換えで緩まない)、記録(log)は JSON
    state_p = ROOT / "metrics" / f"oncall-{date}-{stage}.json"
    state = load_state(state_p)
    if state is None:
        os.close(lock_fd)
        return 1
    if attempt_count(date, stage) >= MAX_ATTEMPTS:
        notify("oncall", f"{date} {stage}: 当番は既に{MAX_ATTEMPTS}回試みた。人の判断が要る:\n"
                         + "\n".join(str(x)[:200] for x in state["log"][-3:]), ok=False)
        os.close(lock_fd)
        return 1
    # 前提を全部確かめてから試行を消費する(dirty で何もしなかった回で回数を減らさない。監査指摘)
    try:
        if not root_clean():
            notify("oncall", f"{date} {stage}: 本体の作業ツリーが clean でない。当番は何もしない:\n"
                             + sh(["git", "status", "--short"], cwd=ROOT).stdout[:500], ok=False)
            return 1
        problem = ensure_edition(edition, ROOT)
        if problem:
            notify("oncall", f"{date} {stage}: {problem}。当番は何もしない", ok=False)
            return 1
    except RuntimeError as e:
        notify("oncall", f"{date} {stage}: 前提の確認に失敗: {e}", ok=False)
        return 1
    try:
        attempt_no = consume_attempt(date, stage)
    except FileExistsError:
        notify("oncall", f"{date} {stage}: 試行の印が競合した(同時起動)。当番は起動しない", ok=False)
        return 1
    save_state(state_p, state)
    notify("oncall", f"{date} {stage} {CALLED_BECAUSE.get(stage, 'が止まった')}。当番({ONCALL_MODEL})が診断・修正に入る({attempt_no}回目)")

    context = gather_context(stage, date, a.reason)
    (ROOT / "metrics" / f"oncall-{date}-{stage}-context.txt").write_text(context, encoding="utf-8")
    base = remote_main()
    env_before = env_fingerprint()
    wt = Path(tempfile.mkdtemp(prefix=f"oncall-{date}-{stage}-"))
    shutil.rmtree(wt)
    sh(["git", "worktree", "prune"], cwd=ROOT)
    must(sh(["git", "worktree", "add", "--detach", str(wt), base], cwd=ROOT, timeout=120), "worktree の作成")
    # .env は置かない(資格情報を当番に渡さない。監査指摘)
    try:
        transcript: list[dict] = []
        res = run_rounds(stage, date, context, wt, base, resume_point(state, base), env_before, transcript)
        approved, fix, head, diff, changed = res["approved"], res["fix"], res["head"], res["diff"], res["changed"]
        (ROOT / "metrics" / f"oncall-{date}-{stage}-transcript.json").write_text(
            json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
        state["log"].append({"at": datetime.datetime.now().isoformat(timespec="minutes"), "approved": approved,
                             "status": fix.get("status"), "diagnosis": (fix.get("diagnosis") or "")[:300]})
        # 直し切れなかったら、**最後に内容を固定できたところまで**の修正と残った指摘を残す(次の試行はこの続きから)。
        # 例外・時間切れで終わった往復でも残す。差分が無い診断(no_fix_needed)への指摘も残す。直し切れたら消す
        kept = res["kept"]
        if not approved and kept["fix"] and kept["open"] and kept["fix"].get("status") != "cannot_fix":
            state["wip"] = {"base": base, "head": kept["head"], "fix": kept["fix"], "open": kept["open"],
                            "later": kept.get("later") or []}
        else:
            state.pop("wip", None)
        save_state(state_p, state)
        open_left = (state.get("wip") or {}).get("open") or []

        if not approved:
            last = transcript[-1] if transcript else {}
            wip_branch = ""
            if state.get("wip") and kept["head"] != base:
                # 人が続きを見られるように記録ブランチにも置く(置けなくても続きは手元の commit から始められる)
                wip_branch = f"repair-wip/{date}-{stage}-{attempt_no}-{int(time.time())}"
                try:
                    if sh(["git", "push", "-q", "origin", f"{kept['head']}:refs/heads/{wip_branch}"], cwd=wt, timeout=120).returncode != 0:
                        wip_branch = ""
                except (subprocess.TimeoutExpired, OSError):
                    wip_branch = ""
            # 編集長に届くのは、なぜ取り込めなかったかの1文と、当番が書いた編集長向けの要約だけ(診断の全文・記録の JSON は記録ファイル)
            if res["error"]:
                why = "当番の作業が途中で止まった"
            elif last.get("remote_moved"):
                why = "作業中に main が別の変更で動いたので、取り込まなかった"
            elif fix.get("status") == "cannot_fix" or not open_left:
                why = "当番は直せないと判断した(人の判断が要る)"
            else:
                why = f"監査の指摘を上限({MAX_ROUNDS}往復)までに直し切れなかった(残り {len(open_left)}件)"
            rec = ROOT / "metrics" / f"oncall-{date}-{stage}-failed.md"
            rec.write_text(f"理由: {why}\n例外: {res['error'] or ''}\n状態: {fix.get('status')}\n診断: {fix.get('diagnosis') or ''}\n"
                           f"メモ: {fix.get('notes') or ''}\n残った指摘:\n" + json.dumps(open_left, ensure_ascii=False, indent=1)
                           + "\n最後の記録:\n" + json.dumps(last, ensure_ascii=False, indent=1), encoding="utf-8")
            from pipelib import editor_notice
            s = fix.get("editor_summary") if isinstance(fix.get("editor_summary"), dict) else {}
            md = f"{int(date[5:7])}/{int(date[8:10])}"
            notify("oncall", editor_notice(
                f"🚧 当番は修正を取り込めませんでした: {md}号の{STAGE_JA.get(stage, stage)}",
                [("取り込めなかった理由", why + ("。次の試行は、ここまでの修正の続きから始める" if wip_branch else ""))]
                # 以下は当番の書いた要約で、監査で承認されていない(紙面への影響も確かめられていない)。そう明示する(監査指摘)
                + [(label + "(監査で未承認)", s[k]) for k, label in SUMMARY_LABELS if s.get(k)],
                footer=f"記録(差分・検証の全文): {rec.relative_to(ROOT)}" + (f"(記録ブランチ {wip_branch})" if wip_branch else "")), ok=False)
            return 1

        # 発行後でよい指摘(later)を保管する。**保管の失敗で発行を止めない**(修正報告には必ず載る)
        keep_later(res["later"], date, stage, head)

        # release 起点でも、生成層を直したなら号を作り直す(生成済みの号をそのまま発行しない。監査指摘)
        full, rerun_mode = rerun_policy(stage, changed, str(fix.get("rerun_mode") or ""), recover=RECOVER)
        if a.no_rerun:
            full, rerun_mode = False, "再実行なし(呼び出し側の指定: 工程は終わっている。次の実行から効く)"
        targets: list[str] = []
        branch = ""
        reported = True
        if diff.strip():
            branch = f"repair/{date}-{stage}-{attempt_no}-{int(time.time())}"
            # 記録用ブランチ(force しない・一意な名前)。main に入れるのは**監査した head そのもの**
            must(sh(["git", "push", "-q", "origin", f"{head}:refs/heads/{branch}"], cwd=wt, timeout=120), "記録ブランチの push")
            # **取り込む前に**修正報告の全文(診断・原因・検証・監査判定・差分の要点)を人に届ける。
            # 届かなければ取り込まない。merge commit のハッシュだけは取り込み後に追送する(報告は必須。監査指摘)
            if not report_change(stage, date, fix, transcript, base, head, "", branch, ["main", edition], rerun_mode):
                notify("oncall", f"{date} {stage}: 修正報告が Discord に届かないので取り込まない(記録ブランチ {branch})", ok=False)
                return 1
            if not root_clean():
                notify("oncall", f"{date} {stage}: 取り込み前に本体の作業ツリーが汚れた。取り込まない(記録ブランチ {branch})", ok=False)
                return 1
            must(sh(["git", "checkout", "-q", "main"], cwd=ROOT), "main の checkout")
            must(sh(["git", "pull", "-q", "--ff-only", "origin", "main"], cwd=ROOT, timeout=120), "main の pull")
            # ローカル main に監査していない commit が乗っていたら、一緒に push してしまう(監査指摘)。
            # origin/main と完全一致のときだけ取り込む。push に失敗したら merge を巻き戻す
            main_before = sh(["git", "rev-parse", "main"], cwd=ROOT).stdout.strip()
            # 監査の基準(base)から origin/main が動いていないこと。動いていれば監査していない変更との
            # 組み合わせになるので取り込まない(監査指摘)
            if main_before != remote_main() or main_before != base:
                sh(["git", "checkout", "-q", edition], cwd=ROOT)
                notify("oncall", f"{date} {stage}: main が監査の基準から動いた(ローカル {main_before[:10]} / 基準 {base[:10]})。取り込まない(記録ブランチ {branch})", ok=False)
                return 1
            # --no-ff で必ず merge commit を作る。戻すときは `git revert -m 1 <merge commit>` 1つで済み、
            # 報告にそのハッシュを書ける(fast-forward だと -m 1 が使えない。監査指摘)
            m = sh(["git", "merge", "--no-ff", "--no-edit", head], cwd=ROOT)
            if m.returncode != 0:
                undo_merge(main_before, "main")
                must(sh(["git", "checkout", "-q", edition], cwd=ROOT), f"{edition} の checkout")
                notify("oncall", f"{date} {stage}: 修正を main に入れられない(衝突)。記録ブランチ {branch} に置いた", ok=False)
                return 1
            merge_commit = sh(["git", "rev-parse", "main"], cwd=ROOT).stdout.strip()
            # リモートの main が基準のままのときだけ push が通る(競合検出。監査指摘)
            pushed = sh(["git", "push", "-q", f"--force-with-lease=refs/heads/main:{base}", "origin", "main"], cwd=ROOT, timeout=120)
            if pushed.returncode != 0:
                undo_merge(main_before, "main")
                must(sh(["git", "checkout", "-q", edition], cwd=ROOT), f"{edition} の checkout")
                notify("oncall", f"{date} {stage}: main の push に失敗したので merge を巻き戻した: {(pushed.stderr or '')[-200:]}", ok=False)
                return 1
            targets.append("main")
            must(sh(["git", "checkout", "-q", edition], cwd=ROOT), f"{edition} の checkout")
            ed_before = sh(["git", "rev-parse", edition], cwd=ROOT).stdout.strip()
            m2 = sh(["git", "merge", "--no-edit", "main"], cwd=ROOT)
            if m2.returncode != 0:
                undo_merge(ed_before, edition)
                report_change(stage, date, fix, transcript, base, head, merge_commit, branch, targets, "取り込み衝突のため未実行")
                notify("oncall", f"{date} {stage}: 修正は main に入ったが {edition} への取り込みが衝突。手で解くこと", ok=False)
                return 1
            pushed = sh(["git", "push", "-q", "origin", edition], cwd=ROOT, timeout=120)
            if pushed.returncode != 0:
                # ローカルだけ進んだままにしない(次回の前提検査で「リモートと食い違う」になり、当番が
                # 二度と動けなくなる。監査指摘)。main には入っているので、次回は merge し直せば済む
                undo_merge(ed_before, edition)
                report_change(stage, date, fix, transcript, base, head, merge_commit, branch, targets, "edition の push 失敗のため未実行")
                notify("oncall", f"{date} {stage}: 修正は main に入ったが {edition} の push に失敗したので巻き戻した: {(pushed.stderr or '')[-200:]}", ok=False)
                return 1
            targets.append(edition)
            # 追送: 取り込んだ merge commit と戻し方(全文は取り込み前に届いている)
            (ROOT / "metrics" / f"oncall-{date}-{stage}-report.md").open("a", encoding="utf-8").write(
                f"\n\n## 取り込み結果\nmain の merge commit: {merge_commit}\n戻し方: git revert -m 1 {merge_commit}\n")
            reported = notify("oncall", f"✅ 当番の修正を取り込みました。戻すときは ops の main で `git revert -m 1 {merge_commit[:10]}`",
                              require=True)
        else:
            # 直す箇所が無かったときも、編集長向けの1通(記録の全文はファイル)
            path = ROOT / "metrics" / f"oncall-{date}-{stage}-report.md"
            path.write_text(f"診断: {fix.get('diagnosis') or ''}\n再実行の根拠: {fix.get('recovery') or ''}\n再実行: {rerun_mode}\n"
                            f"{later_text(res['later'])}\n\n## 往復の記録\n" + json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
            reviews = [t.get("review") for t in transcript if t.get("review")]
            reported = notify("oncall", editor_report(stage, date, fix, (reviews[-1] or {}).get("verdict") if reviews else None,
                                                      len(reviews), len(res["later"]), path.relative_to(ROOT), ""))
        if stage == BACKLOG_STAGE and a.backlog_keys:
            # 監査が承認して取り込めた(直す箇所なしの承認を含む)。消し込むのは、当番の最終報告が1件ずつ扱った指摘だけ
            # (監査はその扱いを元の指摘と突き合わせて確かめている)。扱わなかった指摘・取り込めなかったときは残り、
            # 終わりに名指しで知らせる(report_left_backlog)。翌朝の監視がまた渡す
            done = backlog_done(settled_backlog_keys(fix, list(a.backlog_keys)))
            print(f"保管していた指摘を消し込んだ: {', '.join(done) or 'なし'}", flush=True)
        if not reported:
            # 報告が人に届いていないなら再実行(=発行)へ進まない(監査指摘)。修正は main に入っているので
            # 報告ファイル(metrics/oncall-*-report.md)を人が見て、手で再実行する
            notify("oncall", f"{date} {stage}: 修正報告が Discord に届かないので再実行しない。metrics/oncall-{date}-{stage}-report.md を見て手で再実行すること", ok=False)
            return 1
        if a.no_rerun:
            # 呼び出し側が「再実行しない」(工程は終わっている・号は確定している。なぜなぜと修正だけ)。次の実行から効く
            return 0
        if rerun_mode == "none":
            notify("oncall", f"{date} {stage}: 再実行しても通らない、と当番が判断。人の判断が要る", ok=False)
            return 1
        if not root_clean():
            notify("oncall", f"{date} {stage}: 再実行前に本体の作業ツリーが汚れた。再実行しない", ok=False)
            return 1
        must(sh(["git", "checkout", "-q", edition], cwd=ROOT), f"{edition} の checkout")
        code = rerun_stage(stage, date, edition, full, recover=RECOVER)
        notify("oncall", f"{date} {stage}: 再実行({rerun_mode})が終わった(exit {code})。"
                         f"{'成功' if code == 0 else '失敗。metrics/oncall-' + date + '-' + stage + '-rerun.log を見ること'}",
               ok=(code == 0))
        return code
    except Exception as e:
        notify("oncall", f"{date} {stage}: 当番の処理で例外: {type(e).__name__}: {str(e)[:300]}", ok=False)
        return 1
    finally:
        sh(["git", "worktree", "remove", "--force", str(wt)], cwd=ROOT)
        sh(["git", "worktree", "prune"], cwd=ROOT)
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
