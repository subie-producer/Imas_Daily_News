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
# **探索役**(Web 検索でネタを見つける工程)。codex exec -m に渡す。
# codex には WebSearch 専用ツールが無いが、sandbox の通信を開けばシェルから
# 検索も本文取得もできる(実測で確認済み)。
EXPLORE_MODEL = ENV.get("EXPLORE_MODEL", "gpt-5.6-luna")
# 定点観測(sources.yml の巡回結果を facts 化する)に使う Claude モデル。
# 探索とは別役で、こちらは渡されたページ本文を読むだけなので安いモデルでよい
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


# 手元の確認・試験実行で Discord へ本物の警報を飛ばさないための抑止。
# 各スクリプトが `--dry-run` `--no-git` 等を受け取ったときに `set_quiet(True)` を呼ぶ。
# 実測: 発行前の点検で release を --dry-run したところ、号スナップショットがまだ無いのは
# 当然なのに「発行中止」の警報が飛び、本物の発行不良と見分けが付かなくなった(2026-08-31)。
_QUIET = False


def set_quiet(on: bool) -> None:
    global _QUIET
    _QUIET = on


def notify(job: str, msg: str, ok: bool = True) -> None:
    prefix = "✅" if ok else "🚨"
    text = f"{prefix} アイマスNEWS {job}: {msg}"
    if _QUIET:
        print(f"[試験実行・通知しない] {text}", flush=True)
        return
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


# --- 出典種別の判定(規程2) -------------------------------------------------
SOURCE_TYPES = ("公式", "準公式", "当事者", "報道", "ファン", "二次情報", "もちより", "未確認")
_ST_TABLE: dict | None = None


def source_type_table() -> dict:
    """`source_types.yml` を読む(初回だけ)。**無ければ落とす。**

    以前は無いとき `{}` を返していたが、それだと表が消えた瞬間に全出典が
    既定へ落ちて素通りする(監査指摘の fail-open)。判定の根拠が無い状態で
    紙面を作らせないため、ここで止める。
    """
    global _ST_TABLE
    if _ST_TABLE is None:
        import yaml
        p = ROOT / "source_types.yml"
        if not p.exists():
            raise SystemExit(f"出典種別の判定表がない: {p}")
        _ST_TABLE = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        _check_table(_ST_TABLE, p)
    return _ST_TABLE


def _check_table(t: dict, p) -> None:
    """同じ相手が2つの種別に載っていないか。

    表が育つほど起きやすい。判定は上から順に最初に当たったものを採るので、
    重複しても**動いてしまう**ぶん気づけない(実測: tcg-supply-navi.com が
    二次情報と当事者の両方に載ったまま通っていた。監査で見つかった)。
    """
    import collections
    seen = collections.defaultdict(list)
    for key in ("official_domains", "semi_official_domains", "press_domains",
                "secondary_domains", "fan_domains", "party_domains",
                "official_paths", "party_paths"):
        for v in t.get(key) or []:
            seen[v].append(key)
    for group in ("x_accounts", "video_ids"):
        for label, names in (t.get(group) or {}).items():
            for v in names or []:
                # 判定器は X アカウントを小文字化して比べるので、検査側も揃える。
                # 揃えないと `imas_official` と `IMAS_OFFICIAL` を別種別に登録できてしまい、
                # 検査を通ったうえで先に載っているほうが勝つ(監査指摘)
                key = v.lower() if group == "x_accounts" else v
                seen[f"{group}:{key}"].append(f"{group}/{label}")
    dup = {v: ks for v, ks in seen.items() if len(ks) > 1}
    if dup:
        raise SystemExit(f"{p} で種別が重複している: "
                         + " / ".join(f"{v}({'・'.join(ks)})" for v, ks in sorted(dup.items())))


def classify_source(url: str) -> str:
    """出典 URL から種別を決める。**収集役の自己申告は使わない。**

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
    t = source_type_table()
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
    for pre in t.get("party_paths") or []:
        if under(pre):
            return "当事者"

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
    #    実データにある形は投稿(420件)とアカウントページ(2件)の2つだけなので、そこに限る
    if host in ("x.com", "twitter.com", "mobile.x.com", "mobile.twitter.com"):
        # 形を見るのは**正規化したパス**に対して行う。`//` や `..` を含む URL は
        # X 上では同じ投稿を指すので、潰したうえで判定するのが実態に合う
        seg = [s for s in path[len(host):].split("/") if s]
        ok = len(seg) == 1 or (len(seg) == 3 and seg[1] == "status" and seg[2].isdigit())
        acct = seg[0].lower() if ok else ""
        for label, accounts in (t.get("x_accounts") or {}).items():
            if acct and acct in {a.lower() for a in accounts}:
                return label
        return "未確認"

    # 4. それ以外はドメイン。**完全一致を先に見る。**
    #    公式ポータル配下でも gakuen-label.(公式レーベル)は準公式なので、
    #    末尾一致より個別指定が勝たないと誤って公式になる(監査指摘)
    for key, label in (("official_domains", "公式"), ("semi_official_domains", "準公式"),
                       ("press_domains", "報道"), ("secondary_domains", "二次情報"),
                       ("fan_domains", "ファン"), ("party_domains", "当事者")):
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
    return "未確認"


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
