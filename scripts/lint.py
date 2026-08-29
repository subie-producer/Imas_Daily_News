#!/usr/bin/env python3
"""アイマスNEWS(α) 紙面 lint(required check)。

REQUIREMENTS.md 4.4 の実装。赤なら auto-merge 不可。

  python3 scripts/lint.py                 # 構造検査のみ(ネットワーク検査は変更ファイルが対象)
  python3 scripts/lint.py --base <sha>    # append-only 検査・変更ファイル特定の基準コミット
  python3 scripts/lint.py --full          # 全記事の URL 生存確認を含む(定期監査用)
  python3 scripts/lint.py --no-net        # URL 生存確認を行わない(ローカル用)

CI では BASE_SHA 環境変数(PR の base)を渡す。
エラーは exit 1(マージ不可)、警告は annotation のみ。
"""
import argparse
import collections
import datetime
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import classify_source  # noqa: E402  出典種別は URL から判定する

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
POSTS = DOCS / "_posts"
EDITIONS = DOCS / "_editions"
EDITORIALS = DOCS / "_editorials"
CANDIDATES = ROOT / "candidates"
IDOLS = DOCS / "_data" / "idols.json"

# ---- 紙面の定数(REQUIREMENTS.md に由来。変更時は要件書と要同期) -------------
TITLE_MAX = 45          # 見出し上限(警告)
# リード文は上限だけ見る(規程9)。本文と同じ理由で、字数のために言葉を足させない
LEDE_RANGE = (0, 140)
EXCERPT_MAX = 80        # 社説抜粋の目安(警告)
# 枠(rank)と本文の長さの対応(編集規程9)。
#
# **枠は書き上がりから機械が決める**(compose.assign_ranks)。字数を枠に合わせさせると
# 「large なのに medium 2本ぶんしかない記事」が出る(2026-08-27号の実測: large の
# 中央値 527字)。順序を逆にし、水増しの動機をなくした。
# ここでは「付いている枠が長さと合っているか」だけを検査する。
BODY_RANGE = {
    # lead は長さではなく話題の大きさで決まる枠なので、下限で縛らない
    # (その日最大の話題が、出典の情報量ゆえに短くなることはある)
    "lead": (0, 99999),
    "large": (700, 99999),   # 一面は号内1本なので、1000字超でも lead 以外は large に入る
    "medium": (450, 699),
    "small": (0, 449),
    "roundup": (0, 99999),   # 面の種類であって大小ではない
    "culture": (0, 99999),
}
# 「字数を先に決めるのをやめ、書き上がりから枠を当てる」に切り替えた最初の号。
# これより前は字数を先に決めて書かせていたので、上の範囲では測れない
BODY_RANGE_FROM = "2026-08-28"
RANK_BELOW = {"lead": "large", "large": "medium", "medium": "small", "small": "small",
              "roundup": "roundup", "culture": "culture"}  # roundup/culture は規程2の減格対象外
# 記事本数の下限(編集規程11)。lead 欠落はエラー、下限割れは警告(publish が Discord 通知)
# roundup は本数に数えない(まとめで下限を満たしたことにすると、単独記事の不足が隠れる)
ARTICLE_MIN = 8
# SP ダイジェスト 1画面制約(2.1)。モック実測(4+3+2+2=11行)から導出した暫定値。
DIGEST_MAX_ROWS_PER_GROUP = 4
DIGEST_MAX_TOTAL_ROWS = 12
DIGEST_LABELS = ["本日", "昨日", "継続中", "明日"]
ISSUE_TIME = "T06:00:00+09:00"
WEEKDAYS = "月火水木金土日"

RELATIVE_WORDS = {"本日": 0, "今日": 0, "昨日": -1, "明日": +1}
ABS_DATE_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")

# 出典バッジの信頼順(強い順)。記事 src は引用出典の最弱種別(compose と同一規則)。
# 以前は「2026-07-14 以前は旧規則(最強種別)で発行済み」として検査から外していたが、
# 最強種別を名乗ることは過大表示そのものであり、除外は誤りを守っていた。
# scripts/retag_sources.py で全号を最弱規則へ揃えたので、除外はもう無い
SRC_ORDER = ["公式", "準公式", "当事者", "報道", "ファン", "二次情報", "もちより", "未確認"]

URL_TIMEOUT = 15
URL_UA = "Mozilla/5.0 (compatible; ImasNewsLint/1.0; +https://github.com/subie-producer/Imas_Daily_News)"
# ログイン必須で機械的な生存確認ができないホスト(候補照合・出典明示は通常どおり)
URL_CHECK_SKIP_HOSTS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}


class Report:
    def __init__(self):
        self.errors = 0
        self.warnings = 0

    def _emit(self, kind, path, msg):
        rel = str(Path(path).relative_to(ROOT)) if str(path) != "-" else ""
        print(f"::{kind} file={rel}::{msg}" if rel else f"::{kind}::{msg}")
        print(f"  [{kind.upper()}] {rel}: {msg}", file=sys.stderr)

    def error(self, path, msg):
        self.errors += 1
        self._emit("error", path, msg)

    def warn(self, path, msg):
        self.warnings += 1
        self._emit("warning", path, msg)

    def notice(self, msg):
        print(f"::notice::{msg}")


def load_schema(name):
    return Draft202012Validator(
        json.loads((ROOT / "schema" / name).read_text(encoding="utf-8"))
    )


def _norm(v):
    """YAML が date 型に解釈した値を ISO 文字列へ戻す(スキーマは文字列契約)。"""
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def parse_frontmatter(path):
    """--- で挟まれた frontmatter と本文を返す。不正なら (None, None)。"""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", text, re.DOTALL)
    if not m:
        return None, None
    return _norm(yaml.safe_load(m.group(1))), m.group(2)


def schema_check(rep, path, validator, data):
    ok = True
    for err in validator.iter_errors(data):
        loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
        rep.error(path, f"スキーマ違反 {loc}: {err.message}")
        ok = False
    return ok


def git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True
    )


# ---- 出典種別 lint -----------------------------------------------------------

def src_rank(t):
    return SRC_ORDER.index(t) if t in SRC_ORDER else len(SRC_ORDER)


def check_source_types(rep, path, fm):
    """出典の種別が **URL からの判定**と合っているか。記事 src は最弱種別か。

    照合の相手は `source_types.yml` であって、収集役の申告ではない。
    申告同士を突き合わせていたころは、攻略サイトを「公式」と申告すれば
    候補も記事も最弱も「公式」で揃い、過大表示を1件も止められなかった。
    """
    if not fm.get("sources"):
        return
    resolved = []
    for s in fm["sources"]:
        # 未確認 は「一次ソース未到達」という確認状態。種別の判定で上書きしない(規程2.5)
        if s.get("type") == "未確認":
            resolved.append("未確認")
            continue
        want = classify_source(s["url"])
        if s.get("type") != want:
            d = "過大表示" if src_rank(s.get("type")) < src_rank(want) else "過小表示"
            rep.error(path, f"出典 type が URL の判定と不一致({d}。{s['url']}: "
                            f"記事 {s.get('type')} / 判定 {want})")
        resolved.append(want)
    # 記事バッジは引用出典の**最弱**種別(compose の検収と同一規則)
    want = max(resolved, key=src_rank)
    got = fm.get("src")
    if got != want:
        d = "過大表示" if src_rank(got) < src_rank(want) else "過小表示"
        rep.error(path, f"src は引用出典の最弱種別 {want} にする(現: {got}。{d})")


# ---- 時制 lint ---------------------------------------------------------------

def check_tense(rep, path, text, edition_date, where):
    """相対表現(本日/昨日/明日)と絶対日付(M月D日)が同一文内で矛盾しないか。"""
    for sentence in re.split(r"[。\n]", text or ""):
        rels = [w for w in RELATIVE_WORDS if w in sentence]
        if not rels:
            continue
        for m in ABS_DATE_RE.finditer(sentence):
            month, day = int(m.group(1)), int(m.group(2))
            expected = {
                (edition_date + datetime.timedelta(days=RELATIVE_WORDS[w])) for w in rels
            }
            if not any(e.month == month and e.day == day for e in expected):
                rep.error(
                    path,
                    f"時制矛盾({where}): 「{'/'.join(rels)}」と「{m.group(0)}」が同一文に共起するが"
                    f"号日付 {edition_date} と整合しない",
                )


# ---- URL 生存確認 -------------------------------------------------------------

def url_alive(url):
    """(ok, detail, transient) を返す。

    transient=True はネットワーク層の失敗(DNS 解決不能・接続不能・タイムアウト等)。
    サーバが返した HTTP 4xx/5xx(=ページの死)とは区別し、呼び出し側は警告に
    格下げする(一過性の回線事故が発行中止に直結しないため。捏造 URL の検査は
    collect の verify と candidates 系譜検査が担っており、ここが最後の砦ではない)。
    """
    req = urllib.request.Request(url, headers={"User-Agent": URL_UA})
    last = (False, "unreachable", True)
    for attempt in (1, 2, 3):
        if attempt > 1:
            time.sleep(5 * (attempt - 1))  # 5秒 → 10秒
        try:
            with urllib.request.urlopen(req, timeout=URL_TIMEOUT) as res:
                return res.status < 400, f"HTTP {res.status}", False
        except urllib.error.HTTPError as e:
            last = (False, f"HTTP {e.code}", False)
        except Exception as e:  # DNS・接続・タイムアウト等のネットワーク層
            last = (False, str(e), True)
    return last


# ---- メイン -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None, help="append-only/変更検出の基準コミット")
    ap.add_argument("--full", action="store_true", help="全記事の URL 生存確認")
    ap.add_argument("--no-net", action="store_true", help="URL 生存確認をスキップ")
    args = ap.parse_args()
    import os
    base = args.base or os.environ.get("BASE_SHA") or None
    if base and set(base) == {"0"}:  # push イベントの新規ブランチ等(ゼロSHA)
        base = None

    rep = Report()
    article_schema = load_schema("article.schema.json")
    edition_schema = load_schema("edition.schema.json")
    editorial_schema = load_schema("editorial.schema.json")
    candidates_schema = load_schema("candidates.schema.json")
    idols = json.loads(IDOLS.read_text(encoding="utf-8")) if IDOLS.exists() else []
    idol_by_name = {i["name"]: i for i in idols}
    if not idols:
        rep.warn(IDOLS, "名鑑 idols.json が空または未生成(scripts/build_idols.py)")

    # -- 変更ファイルの特定(URL 検査・append-only の対象) --
    changed = set()
    if base:
        # base とワーキングツリーを比較(CI では HEAD=ワーキングツリー。ローカルの未コミット変更も拾う)
        r = git("diff", "--name-status", "--no-renames", base)
        if r.returncode != 0:
            rep.error("-", f"git diff 失敗: {r.stderr.strip()}")
            diff_entries = []
        else:
            diff_entries = [l.split("\t") for l in r.stdout.splitlines() if l.strip()]
            changed = {e[1] for e in diff_entries}
    else:
        diff_entries = []
        # 基準が無い場合は未コミット/未追跡ファイルを「変更」とみなす
        r = git("status", "--porcelain", "-uall")
        for line in r.stdout.splitlines():
            changed.add(line[3:].strip())
        rep.notice("--base/BASE_SHA 未指定: append-only 検査はスキップ")

    # -- 記事 --
    posts = {}  # slug -> (path, fm, body)
    by_edition = {}  # date str -> [fm]
    for path in sorted(POSTS.glob("*.md")):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})-([a-z0-9][a-z0-9-]*)\.md$", path.name)
        if not m:
            rep.error(path, "ファイル名が YYYY-MM-DD-<slug>.md 形式でない")
            continue
        fdate, fslug = m.group(1), m.group(2)
        fm, body = parse_frontmatter(path)
        if fm is None:
            rep.error(path, "frontmatter を解釈できない")
            continue
        if not schema_check(rep, path, article_schema, fm):
            continue
        if fm["slug"] != fslug:
            rep.error(path, f"slug '{fm['slug']}' がファイル名 '{fslug}' と不一致")
        if fm["edition"] != fdate:
            rep.error(path, f"edition '{fm['edition']}' がファイル名日付 '{fdate}' と不一致")
        pkey = f"{fm['edition']}/{fm['slug']}"  # URL は日付込みのため一意性は号内のみ要求
        if pkey in posts:
            rep.error(path, f"slug '{fm['slug']}' が同一号内で重複({posts[pkey][0].name})")
        posts[pkey] = (path, fm, body)
        by_edition.setdefault(fm["edition"], []).append(fm)

        if len(fm["title"]) > TITLE_MAX:
            rep.warn(path, f"見出しが{len(fm['title'])}字(上限目安{TITLE_MAX}字)")
        if len(fm["lede"]) > LEDE_RANGE[1]:
            rep.warn(path, f"リード文が{len(fm['lede'])}字(上限{LEDE_RANGE[1]}字)")
        if fm["corrected"] != bool(fm["corrections"]):
            rep.error(path, "corrected と corrections の有無が矛盾")
        if not body.strip():
            rep.error(path, "本文が空")
        else:
            # 分量基準: 中見出し行を除いた本文の非空白文字数
            prose = re.sub(r"^#{1,6} .*$", "", body, flags=re.MULTILINE)
            body_len = len(re.sub(r"\s", "", prose))
            # 枠と長さの整合は、**その規則で組まれた号だけ**に適用する。
            # それ以前は字数を先に決めて書かせていたので、いまの範囲には収まらない。
            #
            # 以前はこれを「変更されたファイルか」で代用していたが、それだと
            # 出典種別の付け直しのように**過去記事を正当に触ったとき**に、
            # 当時存在しなかった基準で古い号を叱り出す(実測: 19件が一斉に出て
            # 発行を止めかけた)。基準の適用範囲は号の日付で決める。
            if fm["edition"] >= BODY_RANGE_FROM:
                lo, hi = BODY_RANGE[fm["rank"]]
                if not (lo <= body_len <= hi):
                    rep.error(path, f"本文{body_len}字と枠 {fm['rank']} が不一致"
                                    f"({fm['rank']} は {lo}〜{hi}字。枠は compose.assign_ranks が"
                                    f"書き上がりから決める)")
        for h in re.finditer(r"^(#{1,6}) ", body, re.MULTILINE):
            if len(h.group(1)) != 2:
                rep.warn(path, f"本文の中見出しは h2(##)のみ(h{len(h.group(1))} を検出)")
        edate = datetime.date.fromisoformat(fm["edition"])
        for where, text in (("title", fm["title"]), ("lede", fm["lede"]), ("本文", body)):
            check_tense(rep, path, text, edate, where)
        # 鮮度検査(編集規程12): 号日付より大きく過去の event_date は「終了済み・過年度の
        # 話題の新報扱い」の徴候(第0号期に2025年の話題を2本発行した事故に由来)。
        # 継続中キャンペーンの開始日を event_date に持つ正当な記事が既存最大21日過去のため、
        # 警告 >21日・エラー >30日とする
        if fm.get("event_date"):
            try:
                behind = (edate - datetime.date.fromisoformat(str(fm["event_date"]))).days
            except ValueError:
                behind = None
            if behind is not None and behind > 30:
                rep.error(path, f"event_date {fm['event_date']} が号日付より{behind}日過去"
                                "(終了済み・過年度の話題は記事化しない。編集規程12)")
            elif behind is not None and behind > 21:
                rep.warn(path, f"event_date {fm['event_date']} が号日付より{behind}日過去"
                               "(過去の話題の新報扱いでないか確認。編集規程12)")

    # -- 社説 --
    editorials = {}
    for path in sorted(EDITORIALS.glob("*.md")):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\.md$", path.name)
        if not m:
            rep.error(path, "ファイル名が YYYY-MM-DD.md 形式でない")
            continue
        fm, body = parse_frontmatter(path)
        if fm is None:
            rep.error(path, "frontmatter を解釈できない")
            continue
        if not schema_check(rep, path, editorial_schema, fm):
            continue
        if fm["edition"] != m.group(1):
            rep.error(path, f"edition '{fm['edition']}' がファイル名日付と不一致")
        if not body.strip():
            rep.error(path, "社説本文が空")
        if len(fm["excerpt"]) > EXCERPT_MAX:
            rep.warn(path, f"社説抜粋が{len(fm['excerpt'])}字(目安{EXCERPT_MAX}字)")
        if fm["corrected"] != bool(fm["corrections"]):
            rep.error(path, "corrected と corrections の有無が矛盾")
        check_tense(rep, path, fm["title"] + "。" + fm["excerpt"] + "。" + body,
                    datetime.date.fromisoformat(m.group(1)), "社説")
        editorials[m.group(1)] = fm

    # -- 号スナップショット --
    editions = {}
    for path in sorted(EDITIONS.glob("*.md")):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\.md$", path.name)
        if not m:
            rep.error(path, "ファイル名が YYYY-MM-DD.md 形式でない")
            continue
        fm, body = parse_frontmatter(path)
        if fm is None:
            rep.error(path, "frontmatter を解釈できない")
            continue
        if not schema_check(rep, path, edition_schema, fm):
            continue
        date_s = m.group(1)
        if fm["date"] != date_s:
            rep.error(path, f"date '{fm['date']}' がファイル名日付と不一致")
        editions[date_s] = (path, fm)
        edate = datetime.date.fromisoformat(date_s)

        if fm["weekday"] != WEEKDAYS[edate.weekday()]:
            rep.error(path, f"weekday '{fm['weekday']}' が実際の曜日 '{WEEKDAYS[edate.weekday()]}' と不一致")
        if fm["number"] >= 1 and fm["issued_at"] != date_s + ISSUE_TIME:
            rep.warn(path, f"issued_at が定時 {date_s}{ISSUE_TIME} でない")

        # 機械算出フィールドの照合
        arts = by_edition.get(date_s, [])
        if fm["article_count"] != len(arts):
            rep.error(path, f"article_count {fm['article_count']} ≠ 実記事数 {len(arts)}")
        n_news = sum(1 for a in arts if a["rank"] not in ("roundup", "culture"))
        if fm["number"] >= 1 and n_news < ARTICLE_MIN:
            rep.warn(path, f"記事本数が{n_news}本(下限{ARTICLE_MIN}本。roundup/culture は不算入。発行は可・Discord 通知対象)")
        pages = len({a["brand"] for a in arts})
        if fm["pages"] != pages:
            rep.error(path, f"pages {fm['pages']} ≠ ブランド異なり数 {pages}")
        corrected = sum(1 for a in arts if a["corrected"])
        if fm["corrected_count"] != corrected:
            rep.error(path, f"corrected_count {fm['corrected_count']} ≠ corrected 記事数 {corrected}")

        # lead
        leads = [a for a in arts if a["rank"] == "lead"]
        if len(leads) != 1:
            rep.error(path, f"rank: lead の記事が{len(leads)}本(号内1本であること)")
        elif fm["lead_slug"] != leads[0]["slug"]:
            rep.error(path, f"lead_slug '{fm['lead_slug']}' が lead 記事 '{leads[0]['slug']}' と不一致")

        # roundup(編集規程13の例外)はブランドあたり号内1本まで
        rup = collections.Counter(a["brand"] for a in arts if a["rank"] == "roundup")
        for b, n in rup.items():
            if n > 1:
                rep.error(path, f"rank: roundup の記事が {b} 面に{n}本(ブランドあたり号内1本であること)")


        # ダイジェスト
        slugs_in_edition = {a["slug"] for a in arts}
        labels = [g["label"] for g in fm["digest"]]
        if labels != DIGEST_LABELS:
            rep.error(path, f"digest の群構成 {labels} が {DIGEST_LABELS} でない")
        total_rows = 0
        for g in fm["digest"]:
            total_rows += len(g["rows"])
            if len(g["rows"]) > DIGEST_MAX_ROWS_PER_GROUP:
                rep.error(path, f"digest 群「{g['label']}」が{len(g['rows'])}行(SP 1画面制約: 群{DIGEST_MAX_ROWS_PER_GROUP}行まで)")
            for r_ in g["rows"]:
                if r_.get("slug") and r_["slug"] not in slugs_in_edition:
                    rep.error(path, f"digest 行「{r_['t']}」の slug '{r_['slug']}' が同号の記事に存在しない")
                check_tense(rep, path, f"{r_['t']}。{r_.get('d', '')}", edate, f"digest「{g['label']}」")
        if total_rows > DIGEST_MAX_TOTAL_ROWS:
            rep.error(path, f"digest 全体が{total_rows}行(SP 1画面制約: 全体{DIGEST_MAX_TOTAL_ROWS}行まで)")

        # ランキング(スコア算出一致の検査は compose スクリプト実装後に追加 = Step 4)
        names = [r_["name"] for r_ in fm["ranking"]]
        if len(set(names)) != len(names):
            rep.error(path, "ranking にアイドル名の重複")
        for i, r_ in enumerate(fm["ranking"]):
            if r_["n"] != i + 1:
                rep.error(path, f"ranking {i + 1}位の n が {r_['n']}")
            idol = idol_by_name.get(r_["name"])
            if idol is None:
                rep.error(path, f"ranking「{r_['name']}」が名鑑に存在しない")
            elif idol["brand"] != r_["brand"]:
                rep.error(path, f"ranking「{r_['name']}」の brand '{r_['brand']}' が名鑑 '{idol['brand']}' と不一致")

        # 誕生日(名鑑から機械算出)
        mmdd = f"{edate.month:02d}-{edate.day:02d}"
        expected = {(i["name"], i["brand"]) for i in idols if i["birthday"] == mmdd}
        actual = {(b["name"], b["brand"]) for b in fm["birthdays"]}
        if expected != actual:
            rep.error(path, f"birthdays が名鑑と不一致(名鑑: {sorted(n for n, _ in expected)})")

        if date_s not in editorials:
            rep.error(path, "同日の社説(docs/_editorials/)が存在しない")

    # -- 記事側から号への整合 --
    for _k, (path, fm, _) in posts.items():
        if fm["edition"] not in editions:
            rep.error(path, f"号スナップショット docs/_editions/{fm['edition']}.md が存在しない")

    # -- 号数の連番と delta の前号整合 --
    dated = sorted(editions.items())  # date 順
    numbered = [(d, fm) for d, (p, fm) in dated if fm["number"] >= 1]
    seen_no = {}
    for d, (p, fm) in dated:
        if fm["number"] >= 1 and fm["number"] in seen_no:
            rep.error(p, f"号数 {fm['no']} が重複({seen_no[fm['no']]})")
        seen_no.setdefault(fm["number"], d) if fm["number"] >= 1 else None
    for i, (d, fm) in enumerate(numbered):
        if fm["number"] != i + 1:
            rep.error(editions[d][0], f"号数が連番でない: {d} は {i + 1} 号であるべきところ {fm['no']} 号")
    for i, (d, (p, fm)) in enumerate(dated):
        prev_rank = dated[i - 1][1][1]["ranking"] if i > 0 else None
        prev_pos = {r_["name"]: r_["n"] for r_ in prev_rank} if prev_rank else {}
        for r_ in fm["ranking"]:
            if r_["name"] not in prev_pos:
                if r_["delta"] != "new":
                    rep.error(p, f"ranking「{r_['name']}」は前号圏外なので delta は new(現: {r_['delta']})")
            else:
                want = ("up" if r_["n"] < prev_pos[r_["name"]]
                        else "down" if r_["n"] > prev_pos[r_["name"]] else "stay")
                if r_["delta"] != want:
                    rep.error(p, f"ranking「{r_['name']}」の delta は前号比 {want}(現: {r_['delta']})")

    # -- candidates スキーマ+出典照合(突合は記事の発行日±1日の窓のみ参照) --
    candidate_urls_by_day = {}
    candidate_items_by_day = {}
    candidate_files = sorted(CANDIDATES.glob("*.json"))
    for path in candidate_files:
        data = json.loads(path.read_text(encoding="utf-8"))
        schema_check(rep, path, candidates_schema, data)
        candidate_urls_by_day[path.stem] = {c.get("url", "") for c in data}
        candidate_items_by_day[path.stem] = {c["id"]: c for c in data if c.get("id")}
    # 続報予約(素材スナップショット)は発行日の素材として candidates と同格に扱う
    scheduled_schema = load_schema("scheduled.schema.json")
    for path in sorted((ROOT / "stock" / "scheduled").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        schema_check(rep, path, scheduled_schema, data)
        day = path.stem
        candidate_urls_by_day.setdefault(day, set()).update(s.get("url", "") for s in data)
        candidate_items_by_day.setdefault(day, {}).update({s["id"]: s for s in data if s.get("id")})

    # 出典種別の照合は**常に全記事**を見る。ネットを使わないので安い。
    #
    # 以前は「記事の type == 候補の source_type」「記事の src == 出典の最弱種別」と、
    # ラベル同士を突き合わせていた。収集役が攻略サイトを「公式」と申告すれば
    # 候補も記事も最弱も「公式」で揃うため、**過大表示を1件も防げない検査が
    # 「過大表示防止」と名乗っていた**(実測 26件・18記事が素通り)。
    # 照合の相手を source_types.yml へ移した。
    #
    # 変更ファイルだけを見ると、`source_types.yml` の判定を直したときに
    # 既存記事が再検査されず、過大表示が残ったまま通る(監査指摘)。
    # 判定表は全記事に一斉に効くものなので、対象を変更差分で絞ってはいけない。
    for _k, (path, fm, _) in sorted(posts.items()):
        check_source_types(rep, path, fm)

    net_targets = [
        (path, fm) for _k, (path, fm, _) in posts.items()
        if args.full or str(path.relative_to(ROOT)) in changed
    ]
    # 2026-07-14 号から candidates は号日付キー(1号=1ファイル)。それ以前は収集日キーで
    # 1つの号の素材が2ファイルに割れていたため、旧号の検査のみ前日窓を残す
    EDITION_KEYED_FROM = "2026-07-14"
    for path, fm in net_targets:
        ed_day = datetime.date.fromisoformat(fm["edition"])
        if fm["edition"] >= EDITION_KEYED_FROM:
            window = {fm["edition"]}
        else:
            window = {fm["edition"], (ed_day - datetime.timedelta(days=1)).isoformat()}
        allowed = set()
        for day in window:
            allowed |= candidate_urls_by_day.get(day, set())
        # 系譜(candidates 由来か)は**警告**にとどめる。
        # この検査は URL 捏造の代理指標にすぎず、執筆が出典を読みに行けるようになった今
        # (scripts/fetch_page.py)、自力で見つけた正しい公式ページも弾いてしまう
        # (2026-08-28号でツアマス公式の実在ページを系譜外として落とし、発行が止まった)。
        # 捏造の判定は「URL が生きているか」(この下の生存確認・エラー)と
        # 「開いて内容が記事と一致するか」(校閲のブロック項目2)が担う。
        # 実物を開いて確かめられるのは校閲だけであり、そこが判断すべき検査である。
        if allowed:
            for s in fm["sources"]:
                if s["url"] not in allowed:
                    rep.warn(path, f"出典 URL が candidates に無い(執筆が追加した出典。"
                                   f"校閲が内容を照合すること): {s['url']}")

        # 系譜検査: candidate_ids がある記事は、出典 URL がその候補群の URL に限定される
        if fm.get("candidate_ids"):
            window_items = {}
            for day in window:
                window_items.update(candidate_items_by_day.get(day, {}))
            own_urls = set()
            for cid in fm["candidate_ids"]:
                item = window_items.get(cid)
                if item is None:
                    rep.error(path, f"candidate_ids の {cid} が発行日前後の candidates に存在しない")
                else:
                    own_urls.add(item.get("url", ""))
            if own_urls:
                for s in fm["sources"]:
                    if s["url"] not in own_urls:
                        rep.warn(path, f"出典 URL が candidate_ids の候補群に無い(系譜外。"
                                       f"校閲が内容を照合すること): {s['url']}")
        if not args.no_net:
            for s in fm["sources"]:
                host = urllib.parse.urlparse(s["url"]).hostname or ""
                if host in URL_CHECK_SKIP_HOSTS:
                    continue
                ok, detail, transient = url_alive(s["url"])
                if not ok:
                    if transient:
                        rep.warn(path, f"出典 URL に到達できない(ネットワーク層の失敗: {detail})。"
                                       f"死活未確認のまま通過: {s['url']}")
                    else:
                        rep.error(path, f"出典 URL 生存確認に失敗({detail}): {s['url']}")
    if not candidate_files and net_targets:
        rep.notice("candidates が空のため出典照合(candidates 突合)はスキップ(collect 稼働後に有効化)")

    # -- append-only(基準コミットとの diff) --
    if base:
        watched = ("docs/_posts/", "docs/_editions/", "docs/_editorials/")
        # 試験発行(number: 0)の号は検証用のため append-only 検査の対象外(REQUIREMENTS 2.1)
        test_dates = {d for d, (_p, efm) in editions.items() if efm.get("number") == 0}
        for entry in diff_entries:
            status, p = entry[0], entry[1]
            if not p.startswith(watched):
                continue
            dm = re.search(r"(\d{4}-\d{2}-\d{2})", p)
            if dm and dm.group(1) in test_dates:
                continue
            if status == "D":
                rep.error(ROOT / p, "append-only 違反: 紙面ファイルの削除は禁止")
            elif status == "M":
                r = git("show", f"{base}:{p}")
                if r.returncode != 0:
                    rep.error(ROOT / p, f"基準版の取得に失敗: {r.stderr.strip()}")
                    continue
                m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", r.stdout, re.DOTALL)
                old = _norm(yaml.safe_load(m.group(1))) if m else {}
                new_fm, _ = parse_frontmatter(ROOT / p)
                if new_fm is None:
                    continue
                if p.startswith("docs/_editions/"):
                    diff_keys = {k for k in set(old) | set(new_fm) if old.get(k) != new_fm.get(k)}
                    if diff_keys - {"corrected_count"}:
                        rep.error(ROOT / p, f"append-only 違反: 過去号の変更は corrected_count 加算のみ許可(変更: {sorted(diff_keys)})")
                    elif new_fm.get("corrected_count", 0) <= old.get("corrected_count", 0):
                        rep.error(ROOT / p, "append-only 違反: corrected_count が加算されていない")
                else:  # 記事・社説: 訂正(corrections 追記)を伴う変更のみ許可
                    if len(new_fm.get("corrections", [])) <= len(old.get("corrections", [])):
                        rep.error(ROOT / p, "append-only 違反: 過去紙面の変更には corrections の追記が必要")
                    for k in ("slug", "edition", "brand", "rank"):  # src はソース再分類の訂正を許容
                        if k in old and old.get(k) != new_fm.get(k):
                            rep.error(ROOT / p, f"append-only 違反: 訂正で {k} は変更できない")

    print(f"\nlint: {rep.errors} errors, {rep.warnings} warnings "
          f"(posts={len(posts)}, editions={len(editions)}, editorials={len(editorials)})")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())
