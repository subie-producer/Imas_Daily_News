#!/usr/bin/env python3
"""assemble: 組版。判断は小さな構造化セッション、反映はコード。

  python3 scripts/assemble.py --date YYYY-MM-DD [--number N] [--dry-run]

以前の組版は Claude の1セッションに「号スナップショットの digest を組み、既報台帳に
事実を追記し、続報予約を作り、pending を整え、lint が通るまで自分で直す」を丸ごと
やらせていた。実測 2026-09-11: 40本で 24.7分、ツール呼び出し 96回(シェル 63・うち
即席 python 24、記事ファイルの編集 11、lint 3回)。判断が要るのは digest の12行と
「どの事実を既報として残すか」「どの日に続報を予約するか」だけで、その入力は見出し・
リード・面・日付付きの事実、40行分で足りる。残りは frontmatter と候補から決定的に出せる。

この設計では:

- コードが**入力を圧縮して**渡す(記事1本 = 数行)
- セッションは**判断だけを JSON で返す**(schema/assemble.schema.json)。ファイルに触らない
- コードが**反映する**。号スナップショット・stock/stories.yml・stock/scheduled/・stock/pending.yml
- 反映は**冪等**。この号の寄与(台帳の事実・予約・pending の足し引き)には号の印を付け、再実行時はまず
  それを剥がしてから付け直す。記事が落ちても増えても、もう一度走らせるだけでよい。
  組版前へ巻き戻す・成果物から抜く、という手順は要らない
- 校閲が既報判定に使う**組版前の台帳**(metrics/stories-before-<日付>.yml)もここで書く
  (この号の寄与を剥がした状態がそれである)
- lint はコードが回す。赤なら**組版の欠陥**なので直さずに報告する
"""
import argparse
import collections
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import renderlib
from pipelib import (ROOT, CLAUDE_MODEL, COMPOSE_ARTICLE_MAX_BUDGET_USD, COMPOSE_WAVE, classify_source,
                     anomaly, claude_traced, err_tail, extract_json_array, read_answer, notify, prompt_file, render_prompt,
                     schema_ok, write_checked, align_x_published_date, CliBlocked)

POSTS = ROOT / "docs" / "_posts"
EDITIONS = ROOT / "docs" / "_editions"
STORIES = ROOT / "stock" / "stories.yml"
SCHEDULED = ROOT / "stock" / "scheduled"
PENDING = ROOT / "stock" / "pending.yml"
PENDING_HEADER = ("# 日付未確定の追跡事項(watchlist)。日付が判明したら組版が stock/scheduled/<日付>.json へ予約して、ここから消す。\n"
                  "# added_on は足した号、removed_on は消した(決着した)号。removed_on の付いた行は号の組み直し・巻き戻しのために残し、判断には渡さない")
BRANDS = ("general", "765", "cg", "million", "shiny", "sidem", "gaku", "dsva", "joint", "other")
KINDS = ("締切前", "締切", "開幕", "千秋楽", "発売", "結果", "終了", "開始", "その他")
K_VOCAB = "誕開終配売受締発演"
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日|\d{1,2}/\d{1,2}")
FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)


# ---------------------------------------------------------------- 入力
def load_posts(date: str) -> list[dict]:
    out = []
    for p in sorted(POSTS.glob(f"{date}-*.md")):
        m = FM_RE.match(p.read_text(encoding="utf-8"))
        if not m:
            continue
        fm = yaml.safe_load(m.group(1)) or {}
        fm["_name"] = p.name
        out.append(fm)
    return out


def load_materials(date: str) -> dict[str, dict]:
    """候補と、その日の続報予約(素材スナップショット)を id で引けるようにする。"""
    mats: dict[str, dict] = {}
    p = ROOT / "candidates" / f"{date}.json"
    if p.exists():
        for c in json.loads(p.read_text(encoding="utf-8")):
            mats[c["id"]] = c
    q = SCHEDULED / f"{date}.json"
    if q.exists():
        for s in json.loads(q.read_text(encoding="utf-8")):
            mats.setdefault(s["id"], {**s, "origin": "scheduled"})
    # X の投稿の掲載日は投稿 ID の日本時間(compose.load_window_candidates と同じ。保存済みのファイルは書き換えない)
    for c in mats.values():
        align_x_published_date(c)
    return mats


def restore_reservation_facts(date: str) -> list[str]:
    """発行日以降の続報予約のうち、facts が元素材の facts の途中で切れているものを元素材から全部写し直す。

    以前の組版は予約に facts を先頭12件しか写さなかった(実測 2026-10-07: 13件目の締切日時が落ち、締切前の
    記事を見送った)。元素材は予約を付けた号の素材(candidates/<reserved_on>.json か、その号の素材だった
    stock/scheduled/<reserved_on>.json の予約)で、予約を元に付けた予約は元をたどって候補まで戻る(source_facts)。
    保存済みの facts が元の facts の真の先頭部分であるときだけ直す(別物にすり替えない)。元が無ければそのまま。
    過去日の予約は読むだけで書き換えない。冪等。
    """
    log, cache = [], {}
    for p in sorted(SCHEDULED.glob("*.json")):
        if p.stem < date:
            continue
        rows = json.loads(p.read_text(encoding="utf-8"))
        n = 0
        for r in rows:
            have = list(r.get("facts") or [])
            full = source_facts(r.get("reserved_on"), r.get("src_candidate_id"), cache)
            if full is not None and len(full) > len(have) and full[:len(have)] == have:
                r["facts"] = full; n += 1
        if n:
            write_checked(p, json.dumps(rows, ensure_ascii=False, indent=1) + "\n")
            log.append(f"予約 {p.name}: 切れていた facts を元素材から {n} 件復元")
    return log


SNAP_SOURCE_TYPES = ("公式", "準公式", "当事者", "演者", "報道", "ファン", "二次情報", "もちより", "未確認")


def material_snapshot(c: dict) -> dict:
    """続報予約に写す素材1件の中身(title・url・出典の種別・facts 全部・日付・リンク元)。出典の種別は判定表から出す。"""
    st = classify_source(c.get("url") or "")
    snap = {"title": c.get("title") or "", "url": c.get("url") or "",
            "source_type": st if st in SNAP_SOURCE_TYPES else "未確認", "facts": list(c.get("facts") or []),
            "via": c.get("via") or "", "verify": c.get("verify") or "unconfirmed"}
    for k in ("event_date", "deadline", "link_from"):
        if c.get(k):
            snap[k] = str(c[k])
    return snap


def source_topic(c: dict | None, cache: dict, depth: int = 0) -> str | None:
    """素材 c の元の話題(元候補の dedup_key)。予約・添えの行は dedup_key に添え先の予約の話題を写しているので、src_dedup_key を
    先に見る。src_dedup_key の無い添え(2026-10-20 の組版が残さなかった)は、予約を付けた号の素材(reserved_on の src_candidate_id。
    load_materials と同じ引き方で、cache に号ごとに溜める)をたどる。たどれなければ None(予約の話題を元の話題と偽らない)。
    以前は c の dedup_key を写し、添えを素材にして次の予約を付けると、残した元の話題を予約の話題で上書きした(監査指摘: 山梨の
    WALK & WANDER 開幕の予約に添えた歩数計の素材が、終了の予約で開幕の話題に変わった)。"""
    if not c or depth > 30:
        return None
    if c.get("src_dedup_key"):
        return c["src_dedup_key"]
    if not c.get("companion_of"):
        return c.get("dedup_key") or None
    on, cid = c.get("reserved_on"), c.get("src_candidate_id")
    if not on or not cid:
        return None
    if on not in cache:
        cache[on] = load_materials(on)
    return source_topic(cache[on].get(cid), cache, depth + 1)


def material_hay(c: dict) -> str:
    """素材の中で日付を探す文言(事実・開催日・締切・見出し)。"""
    return _norm(" ".join(list(c.get("facts") or []) + [str(c.get("event_date") or ""), str(c.get("deadline") or ""),
                                                       str(c.get("title") or "")]))


def article_verified_facts(fm: dict) -> list[dict]:
    """記事の verified_facts(執筆が読んだページで確かめた事実)のうち、文と url のあるもの。"""
    return [v for v in fm.get("verified_facts") or [] if isinstance(v, dict) and v.get("text") and v.get("url")]


def reservation_dates(r: dict) -> list[str]:
    """予約の行が指しうる日(予約の日、出来事の日、種別のずれの先)。"""
    dt = datetime.date.fromisoformat(r["date"])
    days = [dt] + [dt + datetime.timedelta(days=n) for n in renderlib.RESERVATION_LAG.get(r["kind"], (0,)) if n]
    return list(dict.fromkeys([d.isoformat() for d in days] + ([r["occurs_on"]] if r.get("occurs_on") else [])))


def mentions_any(dates: list[str], hay: str, date: str) -> bool:
    years = renderlib.years_in(hay + " " + date)
    return any(renderlib.date_mentioned(d, hay, years) for d in dates)


def verified_rows(entry: dict, r: dict, fm: dict, c: dict, companions: list[dict], date: str) -> list[dict]:
    """予約の素材 c に予約 r の日付が無く、記事の verified_facts にあるとき、その事実を予約に添える(予約はその日の唯一の素材なので、
    写さないと当日の執筆に日付が届かない)。同じ url の素材があればその facts に足し、無ければ添えの素材を1行足す。戻りは足した行。
    2026-10-31: 公式の投稿に終了日が無く、記事が攻略ページで確かめた 11/27 終了のガシャの予約を、素材に日付が無いとして捨てていた。"""
    dates = reservation_dates(r)
    if mentions_any(dates, material_hay(c), date):
        return []
    labels = {s.get("url"): s.get("label") for s in fm.get("sources") or [] if isinstance(s, dict)}
    by_url = {entry.get("url"): entry} | {r.get("url"): r for r in companions}
    added: list[dict] = []
    for v in article_verified_facts(fm):
        if not mentions_any(dates, _norm(str(v["text"])), date):
            continue
        row = by_url.get(v["url"])
        if row is None:
            dk = entry.get("src_dedup_key") or source_topic(c, {})
            row = {"id": f"{entry['id']}-src{len(companions) + len(added) + 1}",
                   **{k: entry[k] for k in ("dedup_key", "brand", "subject", "kind", "note", "reserved_on", "occurs_on") if k in entry},
                   **material_snapshot({"title": labels.get(v["url"]) or c.get("title"), "url": v["url"], "via": "verified_fact"}),
                   "src_candidate_id": entry["src_candidate_id"], "companion_of": entry["id"],
                   **({"src_dedup_key": dk} if dk else {})}
            by_url[v["url"]] = row
            added.append(row)
        if v["text"] not in row["facts"]:
            row["facts"].append(v["text"])
    return added


def companion_rows(entry: dict, fm: dict, mats: dict) -> list[dict]:
    """続報予約 entry に添える、予約を付けた記事(frontmatter fm)のほかの素材。記事が出典(sources)にした url の素材だけを、
    予約の素材と同じ url は除いて1素材1行で写す(id は <予約の id>-src<n>、companion_of に予約の id)。予約の欄(話題・種別・
    出来事の日)は予約のものを写すので、選定では予約と同じ主題に束なり、執筆には予約の素材と一緒に渡る。予約(トリガー)にはならない。
    以前は組版が予約に選んだ1素材(日付の載った二次情報のページ)だけを写し、記事が出典にした公式の投稿を落とした。当日の執筆は
    公式の出典に届かず、締切の知らせを見送った(実測 2026-10-20: シャニソン第17回テクニカルチャレンジ杯の最終日)。
    素材の元の話題(候補の dedup_key)は src_dedup_key に残す。予約の欄で上書きすると、複数の話題を扱うまとめ記事の別の話題の素材が
    予約の話題の素材に見え、執筆・校閲が区別できず、その話題の既報も引けない(当番の指摘 34fdc7491c: あさり先生のゼミ終了の予約に
    Agitato花海咲季ガシャの公式投稿が添わった)。"""
    used = {s.get("url") for s in fm.get("sources") or [] if isinstance(s, dict) and s.get("url")}
    seen, out, cache = {entry.get("url")}, [], {}
    for cid in fm.get("candidate_ids") or []:
        c = mats.get(cid)
        if cid == entry.get("src_candidate_id") or not c or c.get("url") not in used or c.get("url") in seen:
            continue
        seen.add(c.get("url"))
        dk = source_topic(c, cache)
        out.append({"id": f"{entry['id']}-src{len(out) + 1}",
                    **{k: entry[k] for k in ("dedup_key", "brand", "subject", "kind", "note", "reserved_on", "occurs_on") if k in entry},
                    **material_snapshot(c), "src_candidate_id": cid, "companion_of": entry["id"],
                    **({"src_dedup_key": dk} if dk else {})})
    return out


def restore_reservation_companions(date: str) -> list[str]:
    """発行日以降の続報予約のうち、添える素材(companion_rows)の無いものに、予約を付けた記事(予約した号の記事で、candidate_ids に
    予約の元の素材を含むもの。複数あれば予約の url を出典にしたもの)の素材から添える。予約の素材を記事が出典にしていなくても引く
    (実測 2026-10-11 学マスの運営情報まとめ: 日付確認の二次情報を素材に使い、出典は公式の投稿だけ)。予約を付けた号の素材は load_materials と同じに引く。
    2026-10-20 より前の組版は添えなかった。記事が見つからなければそのまま。過去日の予約は書き換えない。冪等。
    元の話題(src_dedup_key)の無い添えには、予約を付けた号の素材(src_candidate_id)の元の話題(source_topic)を足す(2026-10-20 の組版は残さなかった)。"""
    log, posts, mats = [], {}, {}
    for p in sorted(SCHEDULED.glob("*.json")):
        if p.stem < date:
            continue
        rows = json.loads(p.read_text(encoding="utf-8"))
        has = {r.get("companion_of") for r in rows if r.get("companion_of")}
        out, n, k = [], 0, 0
        for r in rows:
            out.append(r)
            on = r.get("reserved_on")
            if not on or (r.get("id") in has and not r.get("companion_of")):
                continue
            if on not in posts:
                posts[on] = []
                for q in sorted((ROOT / "docs" / "_posts").glob(f"{on}-*.md")):
                    m = FM_RE.match(q.read_text(encoding="utf-8"))
                    if m:
                        posts[on].append(yaml.safe_load(m.group(1)) or {})
                mats[on] = load_materials(on)
            if r.get("companion_of"):
                dk = source_topic(mats[on].get(r.get("src_candidate_id")), mats)
                if dk and not r.get("src_dedup_key"):
                    r["src_dedup_key"] = dk; k += 1
                continue
            # 予約の素材(日付の載った二次情報など)を記事が出典にしないことがあるので、元の素材の id で記事を引く。
            # 同じ素材を複数の記事が使っていたら、予約の url を出典にした記事に絞る。それでも1本に決まらなければ添えない
            hits = [f for f in posts[on] if r.get("src_candidate_id") in (f.get("candidate_ids") or [])]
            if len(hits) > 1:
                hits = [f for f in hits if r.get("url") in {s.get("url") for s in f.get("sources") or [] if isinstance(s, dict)}]
            fm = hits[0] if len(hits) == 1 else None
            add = companion_rows(r, fm, mats[on]) if fm else []
            out += add; n += len(add)
        if n or k:
            write_checked(p, json.dumps(out, ensure_ascii=False, indent=1) + "\n")
        if n:
            log.append(f"予約 {p.name}: 予約を付けた記事の出典の素材を {n} 件添えた")
        if k:
            log.append(f"予約 {p.name}: 添えた素材 {k} 件に元の話題を記した")
    return log


def source_facts(on, cid, cache: dict, depth: int = 0) -> list | None:
    """予約を付けた号 on の素材 cid の facts を、元の候補までたどって返す(load_materials と同じ引き方: 候補が先、
    無ければその号の予約)。素材が予約なら、その予約の facts が元の facts の先頭部分であるときだけ元の facts を採る
    (各段で先頭一致を確かめる。食い違えば、その予約の facts のまま)。見つからなければ None。
    以前は候補だけを引き、予約を元に付けた予約(src_candidate_id が sched-*)を復元できなかった(当番の指摘 f1a5050dcc)。"""
    if not on or not cid or depth > 30:
        return None
    if on not in cache:
        c, q = ROOT / "candidates" / f"{on}.json", SCHEDULED / f"{on}.json"
        cache[on] = ({x["id"]: x for x in json.loads(c.read_text(encoding="utf-8")) if x.get("id")} if c.exists() else {},
                     {x["id"]: x for x in json.loads(q.read_text(encoding="utf-8")) if x.get("id")} if q.exists() else {})
    cands, sched = cache[on]
    if cid in cands:
        return list(cands[cid].get("facts") or [])
    s = sched.get(cid)
    if s is None:
        return None
    mine = list(s.get("facts") or [])
    up = source_facts(s.get("reserved_on"), s.get("src_candidate_id"), cache, depth + 1)
    return up if up is not None and len(up) > len(mine) and up[:len(mine)] == mine else mine


def load_yaml_list(p: Path) -> list:
    """台帳・控えを読む。無いファイルは0件。**有るのに空・配列でないファイルは0件にしない**(書き込みの途中で
    止まって空になった控えを「追跡事項0件」と読み、台帳を空で上書きする。監査指摘)。0件は dump_yaml が `[]` と書く。"""
    if not p.exists():
        return []
    v = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(v, list):
        raise ValueError(f"{p} が読めない(空・配列でない)。書き込みの途中で止まった可能性がある")
    return v


def dump_yaml(p: Path, data, header: str = "") -> None:
    """一時ファイルに書いてから置き換える(途中で止まっても、空や半端なファイルを残さない)。
    台帳・控えの YAML には書式(schema)が無いので write_checked は通さず、置き換えの形だけを揃える: 一時ファイルは書く先と
    同じディレクトリの `.<名前>.<pid>.tmp`(.gitignore の対象)で、失敗しても消す(2026-10-14 監査: 書き込みの途中の容量不足で
    `stories.yml.tmp` が残り、作業ツリーが汚れて release と次の収集が止まる経路があった)。"""
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=200)
    tmp = p.parent / f".{p.name}.{os.getpid()}.tmp"
    try:
        tmp.write_text((header + "\n" if header else "") + text, encoding="utf-8")
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def primary_key(fm: dict, mats: dict) -> str:
    """記事の主題キー。素材の dedup_key の最頻値。無ければ slug。"""
    keys = [mats[i].get("dedup_key") for i in fm.get("candidate_ids") or [] if i in mats and mats[i].get("dedup_key")]
    if not keys:
        return fm.get("slug", "")
    # 最頻値。同数なら candidate_ids の並びで先に出たものを採る(set の反復順は
    # プロセスごとに変わり、再実行で story_id が変わっていた。監査指摘 P0-3)
    order = {k: i for i, k in reversed(list(enumerate(keys)))}
    return max(keys, key=lambda k: (keys.count(k), -order[k], k))


def cited_facts(fm: dict, drafts: dict | None) -> list[str] | None:
    """記事が根拠として指した事実の文(見出し・リードの根拠 id、本文の各段落の根拠の控え、verified_facts の N)。
    F は、その稿を採ったときの「F 番号 → 事実」の記録(drafts = {稿の指紋: 対応}。組版が metrics/factmap-<号>.json に残す)のうち、
    **いまの稿の指紋に一致する対応**で引く。いまの素材から番号を振り直さない(執筆の後に素材の事実が増減すると番号がずれ、書いていない
    事実を「掲載済み」にする。監査指摘)。指紋で選ぶので、除外・組み直しの失敗で旧い稿に巻き戻っても旧い稿の対応が選ばれる(監査指摘)。
    根拠 id が1つも無い記事・記録の無い記事(この仕組みより前の稿)は None(呼び手は素材の事実で代える)。
    記録があるのに、いまの稿の対応が無い・引けない根拠 id があれば、誤った事実を載せないよう空を返し、異常として申告する(見出しが事実になる)。"""
    fmap = None
    if drafts:
        try:
            fmap = drafts.get(renderlib.draft_key(POSTS / fm["_name"]))
            if fmap is None:
                # 指紋から断り書きを外す前に残した記録(断り書きを含めた指紋)も引く
                fmap = drafts.get(renderlib.draft_key(POSTS / fm["_name"], keep_notice=True))
        except (OSError, KeyError):
            fmap = None
        if fmap is None or "__conflict__" in fmap:
            from pipelib import anomaly
            anomaly("compose", f"台帳: {fm.get('_name')} のいまの稿に、稿を採ったときの事実の記録が"
                               + ("食い違って残っている(同じ稿に違う対応)" if fmap else "無い(記録は別の稿のもの)")
                               + "。誤った事実を掲載済みにしないよう、この記事は見出しだけを台帳に残す")
            return []
    by_id = {str(k): str(v) for k, v in (fmap or {}).items()}
    by_id.update({str(v.get("id")): str(v.get("text")) for v in fm.get("verified_facts") or []
                  if isinstance(v, dict) and v.get("id") and v.get("text")})
    used = [str(i) for i in (fm.get("title_fact_ids") or []) + (fm.get("lede_fact_ids") or [])]
    try:
        text = (POSTS / fm["_name"]).read_text(encoding="utf-8")
    except (OSError, KeyError):
        text = ""
    m = FM_RE.match(text)
    for para in re.split(r"\n\s*\n", text[m.end():] if m else text):
        n = renderlib.FACT_NOTE.search(para.strip())
        if n:
            used += n.group(1).split()
    if not used or (fmap is None and any(i.startswith("F") for i in used)):
        return None
    missing = sorted({i for i in used if i not in by_id})
    if missing:
        from pipelib import anomaly
        anomaly("compose", f"台帳: {fm.get('_name')} の根拠 id {', '.join(missing)} を、稿を採ったときの事実の記録から引けない。"
                           "誤った事実を掲載済みにしないよう、この記事は見出しだけを台帳に残す")
        return []
    return list(dict.fromkeys(by_id[i] for i in used))


def build_input(date: str, posts: list[dict], mats: dict, stories: list[dict], pending: list[dict]) -> dict:
    by_id = {e.get("story_id"): e for e in stories}
    arts = []
    # 稿ごとの事実の記録。無い(この仕組みより前の号)なら、記録の無い稿は素材の事実で代える。壊れている・一度壊れた印があるなら、
    # 記録の無い稿を素材の事実で代えない(書いていない事実を掲載済みにしない)。見出しだけにして申告する(監査指摘)
    fmp = ROOT / "metrics" / f"factmap-{date}.json"
    has_record = fmp.exists()     # 記録のファイルがある号では、記録の無い稿を素材の事実で代えない(書き出し後・記録前の中断。監査指摘)
    trusted = True
    try:
        factmap = json.loads(fmp.read_text(encoding="utf-8")) if has_record else {}
    except (OSError, ValueError):
        factmap, trusted = {}, False
    if not renderlib.factmap_valid(factmap) or factmap.get("__corrupted__"):
        factmap, trusted = (factmap if renderlib.factmap_valid(factmap) else {}), False
    if not trusted:
        from pipelib import anomaly
        anomaly("compose", f"台帳: {fmp.name} が壊れている(または一度壊れた)。記録の無い稿は見出しだけを台帳に残す")
    for fm in posts:
        ids = fm.get("candidate_ids") or []
        key = primary_key(fm, mats)
        # 事実には id を振る(F1, F2, …)。モデルは既報にする事実を id で指せるので、
        # 言い換えの検算で正しい事実を落とすことが無い(監査指摘)。日付付きを先に、
        # 価格・出演者など日付の無い事実も少し渡す(台帳に残せるように)
        # 渡すのは**記事が根拠として指した事実**だけ(本文に書かなかった素材の事実を「掲載済み」にしない。2026-10-10: 学マス運営まとめは
        # プロデュースゼミだけを書いたのに、素材にあったログボの事実まで台帳に載り、以後ログボが既報扱いになった)。
        # 根拠 id の無い古い形の記事だけ、素材の事実で代える
        raw = cited_facts(fm, factmap.get(fm.get("slug")) if isinstance(factmap, dict) else None)
        if raw is None and (has_record or not trusted):
            # 記録のファイルがある号・記録が壊れた号では、記録の無い稿を素材の事実で代えない(見出しだけが台帳に残る)。
            # 素材の事実で代えるのは、記録のファイルが無い号(この仕組みより前の号)だけ
            if trusted:
                from pipelib import anomaly
                anomaly("compose", f"台帳: {fm.get('_name')} の稿の事実の記録が無い(書き出しの後、記録の前に止まった など)。"
                                   "見出しだけを台帳に残す")
            raw = []
        if raw is None:
            raw = []
            for i in ids:
                for f in (mats.get(i) or {}).get("facts") or []:
                    if f not in raw:
                        raw.append(f)
        dated = [f for f in raw if DATE_RE.search(f)]
        undated = [f for f in raw if not DATE_RE.search(f)]
        # 事実ごとに出所の素材(その文を facts に持つ素材)を、素材ごとに見出し・締切・開催日を添える。まとめ記事は別々の話題の素材を
        # 束ねるので、事実だけを並べると、どの素材の日付かが分からず、予約が日付を持たない素材を指して検算で捨てられた
        # (2026-10-24: シャニマスのまとめ記事の「8.5周年プレゼント 10/26 23:59 まで」の締切予約が、セレチケの素材を指して捨てられた)
        src_of = lambda f: next((i for i in ids if f in ((mats.get(i) or {}).get("facts") or [])), "")
        facts = [{"id": f"F{n + 1}", "text": f[:140], "src": src_of(f)} for n, f in enumerate((dated[:6] + undated)[:9])]
        materials = [{"id": i, "title": str((mats.get(i) or {}).get("title") or "")[:60],
                      "deadline": str((mats.get(i) or {}).get("deadline") or ""),
                      "event_date": str((mats.get(i) or {}).get("event_date") or "")} for i in ids]
        exist = by_id.get(key)
        arts.append({
            "slug": fm.get("slug"), "brand": fm.get("brand"), "rank": fm.get("rank"),
            "title": fm.get("title"), "lede": fm.get("lede"),
            "event_date": str(fm.get("event_date") or ""),
            "dedup_key": key, "candidate_ids": ids, "materials": materials,
            "existing_story": ({"story_id": exist.get("story_id"), "subject": exist.get("subject"),
                                "known_facts": (exist.get("published_facts") or [])[-3:]} if exist else None),
            "facts": facts,
        })
    d = datetime.date.fromisoformat(date)
    tomorrow = (d + datetime.timedelta(days=1)).isoformat()
    tq = SCHEDULED / f"{tomorrow}.json"
    tomorrow_rows = [{"subject": s.get("subject"), "kind": s.get("kind"), "brand": s.get("brand")}
                     for s in (json.loads(tq.read_text(encoding="utf-8")) if tq.exists() else [])][:8]
    return {"date": date, "articles": arts, "tomorrow_reservations": tomorrow_rows,
            "pending": [{"dedup_key": x.get("dedup_key"), "subject": x.get("subject"), "watch": x.get("watch")}
                        for x in pending]}


# ---------------------------------------------------------------- 判断
def _v(x) -> str:
    """入力に埋める値を**必ず1行**にする(空白・改行の連続 → 空白1個)。識別子も自由文も同じ扱い。

    素材の schema は文字列に改行を禁じていない。値をそのまま行へ足すと、1つの値が何十行にも化けて
    入力の行数が読めなくなる(監査指摘: 自由文だけ畳んで識別子を畳み忘れる、という漏れ方をする)。
    だから項目ごとに畳むのではなく、**埋める値は全部ここを通す**。
    """
    return re.sub(r"\s+", " ", str(x if x is not None else "")).strip()


def render_articles(arts: list[dict], full: bool) -> list[str]:
    """記事の一覧を、1記事 = 見出し行 + 数行(+ facts 1件1行)で並べる。full=False は見出し・リードまで。"""
    L = [f"### 記事({len(arts)}本)"]
    for a in arts:
        L.append(f"- slug: {_v(a.get('slug'))} / brand: {_v(a.get('brand'))} / rank: {_v(a.get('rank'))}"
                 + (f" / dedup_key: {_v(a.get('dedup_key'))}" if full else ""))
        L.append(f"  title: {_v(a.get('title'))}")
        L.append(f"  lede: {_v(a.get('lede'))}")
        if a.get("event_date"):
            L.append(f"  event_date: {_v(a.get('event_date'))}")
        if not full:
            continue
        mats = a.get("materials")
        if mats is None:
            L.append(f"  candidate_ids: {', '.join(_v(i) for i in a.get('candidate_ids') or [])}")
        for m in mats or []:
            L.append(f"  candidate {_v(m.get('id'))}: {_v(m.get('title'))}"
                     + (f" / deadline: {_v(m['deadline'])}" if m.get("deadline") else "")
                     + (f" / event_date: {_v(m['event_date'])}" if m.get("event_date") else ""))
        ex = a.get("existing_story")
        if ex:
            L.append(f"  existing_story: story_id: {_v(ex.get('story_id'))} / subject: {_v(ex.get('subject'))}")
            L.extend(f"    known_fact: {_v(k)}" for k in ex.get("known_facts") or [])
        else:
            L.append("  existing_story: なし")
        L.extend(f"  fact {_v(f.get('id'))}" + (f" [{_v(f['src'])}]" if f.get("src") else "") + f": {_v(f.get('text'))}"
                 for f in a.get("facts") or [])
    return L


def _weekday(date: str) -> str:
    return "月火水木金土日"[datetime.date.fromisoformat(date).weekday()]


def prompt_digest(date: str, inp: dict) -> str:
    """「本日の紙面」の判断(本文は prompts/assemble-digest.md)。紙面全体を見渡すので全記事を渡すが、
    見出しとリードまで(facts は要らない)。指示が先、入力が後(入力は号によって伸びる)。"""
    tr = inp.get("tomorrow_reservations") or []
    L = [f"発行日: {_v(inp.get('date'))}", ""] + render_articles(inp.get("articles") or [], full=False)
    L += ["", f"### tomorrow_reservations({len(tr)}件)"]
    L.extend(f"- subject: {_v(t.get('subject'))} / kind: {_v(t.get('kind'))} / brand: {_v(t.get('brand'))}" for t in tr)
    return render_prompt("assemble-digest", DATE=date, WEEKDAY=_weekday(date), K_VOCAB=K_VOCAB, INPUT="\n".join(L))


def prompt_ledger(date: str, inp: dict, arts: list[dict]) -> str:
    """台帳まわり(既報・続報予約・日付未確定の追跡)の判断。**記事ごとに完結する**ので、記事を LEDGER_CHUNK 本ずつに
    分けて別々のセッションに渡す(arts がその1組)。pending は決着の判定に要るので毎回全部渡す。"""
    pend = inp.get("pending") or []
    L = [f"発行日: {_v(inp.get('date'))}", ""] + render_articles(arts, full=True)
    L += ["", f"### pending(日付未確定の追跡 {len(pend)}件)"]
    L.extend(f"- dedup_key: {_v(x.get('dedup_key'))} / subject: {_v(x.get('subject'))} / watch: {_v(x.get('watch'))}"
             for x in pend)
    return render_prompt("assemble-ledger", DATE=date, WEEKDAY=_weekday(date), N=len(arts), INPUT="\n".join(L))


SESSION_CAP = 900        # セッション1回に待つ上限(秒)
SESSION_TRIES = 2        # 時間切れ・読めない出力のとき、時間が残っていればもう1回だけ
LEDGER_CHUNK = 8         # 台帳の判断を1セッションに何記事まで渡すか
LEDGER_KEYS = ("stories", "reservations", "pending_add", "pending_remove")


def sub_schema(keys) -> str:
    """組版の出力 schema から、そのセッションが返す項目だけを抜いた schema。"""
    s = json.loads((ROOT / "schema" / "assemble.schema.json").read_text(encoding="utf-8"))
    s["properties"] = {k: s["properties"][k] for k in keys}
    s["required"] = list(keys)
    return json.dumps(s, ensure_ascii=False)


def run_session(text: str, date: str, name: str, schema: str, budget=None) -> dict:
    """判断セッション1本。経過を `metrics/work/<日付>/<name>-trace-<n>.jsonl` に残す。

    budget は「いま子プロセスに待たせてよい秒数」を返す関数(compose が締切から計算して渡す)。
    同じ入力でも稀に時間切れになる。判断は入力の純関数で反映は冪等なので、時間が残っていれば
    やり直してよい。やり直せないとき・2回とも駄目なときは、**何をしていて駄目だったか**
    (道具の呼び出しの経過・出力の量)を付けて上げる。
    """
    # 入力はファイルで渡す(引数に詰めると 128KB で落ちる)
    ask = prompt_file(date or "assemble", name, text)
    fails: list[str] = []
    for n in range(1, SESSION_TRIES + 1):
        wait = int(budget()) if budget else SESSION_CAP
        if n > 1 and wait < 300:
            fails.append(f"{n}回目: 残り {wait} 秒ではやり直せない")
            break
        # 経過の記録は上書きしない(同じ号の組み直しで前の回の記録が消えると、失敗の原因を追えない。監査指摘)
        trace = ROOT / "metrics" / "work" / (date or "assemble") / f"{name}-trace-{n}.jsonl"
        k = 2
        while trace.exists():
            trace = trace.with_name(f"{name}-trace-{n}-{k}.jsonl")
            k += 1
        # 道具は Read だけ。判断に要るものは指示ファイルに全部あり、探し物・検索・下請けに時間を使わせない
        r = claude_traced([ask, "--model", CLAUDE_MODEL, "--json-schema", schema, "--tools", "Read",
                           "--dangerously-skip-permissions", "--max-budget-usd", COMPOSE_ARTICLE_MAX_BUDGET_USD],
                          trace, timeout=min(SESSION_CAP, wait))
        print(f"  組版 {name} {n}回目: {r['summary'][:400]}", flush=True)
        # 答えを採るのは**正常に終わったセッションだけ**。時間切れ・異常終了の間際に出た出力は採らない(監査指摘)
        out = r["structured"] if r["ok"] else None
        if r["ok"] and not isinstance(out, dict):
            out, _ = read_answer(r["result"] or "")     # 同じ答えの繰り返しは1つとして読む(2026-10-23 の当番の答えと同じ型)
        # 採るのは、渡した schema どおりの答えだけ(本文の JSON を拾う経路では形が守られない。{"digest":[null]} で
        # 検算が止まる・{} が空の digest として補われる、を防ぐ。崩れた答えは答えなしとしてやり直す。監査指摘)
        if isinstance(out, dict) and schema_ok(out, schema):
            return out
        fails.append(f"{n}回目: {r['summary'][:500]}" + ("" if out is None else "(答えが schema のとおりでない)"))
    raise RuntimeError(f"組版セッション({name})が答えを返さなかった: " + " || ".join(fails))


def judge(date: str, inp: dict, budget=None) -> dict:
    """組版の判断を**小さなセッションに分けて並列に**取り、1つの出力にまとめる。

    以前は全記事の digest・既報・予約・pending を1セッションで判断させていた。実測(2026-09-18・36本):
    答えの JSON は 1.5 万字なのに出力は 5.4 万トークン(大半が考え込み)で 8 分、1回の出力上限 6.4 万の
    すぐ手前。考え込みが少し伸びた回が 900 秒で時間切れになった。記事数に比例して伸びる構造なので、
    - digest: 紙面全体を見る判断。全記事の見出し・リードだけを渡す1セッション
    - 台帳(stories / reservations / pending): 記事ごとに完結する判断。LEDGER_CHUNK 本ずつのセッション
    に分ける。1セッションの仕事量が記事数で伸びなくなり、壁時計は並列のぶん短くなる。
    まとめた出力は従来と同じ形なので、検算(validate)と反映(apply)は変わらない。
    """
    import concurrent.futures
    arts = inp.get("articles") or []
    jobs = [("assemble-digest", prompt_digest(date, inp), sub_schema(("digest",)))]
    chunks = [arts[i:i + LEDGER_CHUNK] for i in range(0, len(arts), LEDGER_CHUNK)]
    jobs += [(f"assemble-ledger-{i + 1}", prompt_ledger(date, inp, ch), sub_schema(LEDGER_KEYS))
             for i, ch in enumerate(chunks)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(jobs), COMPOSE_WAVE))) as ex:
        futs = [ex.submit(run_session, text, date, name, schema, budget) for name, text, schema in jobs]
        # 全部を回収してから上げる。モデル CLI の認証切れ・利用上限(CliBlocked)は、先に終わったふつうの失敗より先に上げる
        # (先頭の失敗で止めると、後の組の停止が隠れて組版が先へ進んだ。監査指摘 r6)
        concurrent.futures.wait(futs)
        errs = [f.exception() for f in futs if f.exception() is not None]
        stop = [e for e in errs if isinstance(e, CliBlocked)]
        if stop or errs:
            raise (stop or errs)[0]
        outs = [f.result() for f in futs]        # 1本でも答えが無ければ、その経過を付けて上がる
    ledger_outs = outs[1:]
    # **記事ごとの答えを入力と突き合わせる。**stories は記事ごとに必ず1件の契約なので、stories の無い記事は
    # その組の答えが途中で切れている。そのまま採ると、その記事の続報予約・日付未確定の追跡が「無し」と区別できずに消える
    # (2026-10-06 の洗い出しで判明: 空の答えを『判断した・何も無い』と解釈する型。予約が消えると期日の知らせの記事が出ない)。
    # 答えの無い記事だけで1回やり直し、それでも無ければ記事を名指しして異常に上げる(stories は validate が見出しで補う)
    left = uncovered_articles(chunks, ledger_outs)
    if left:
        print(f"  組版: 台帳の答えが無い記事 {len(left)}本をやり直す: {', '.join(a.get('slug') or '' for a in left)}", flush=True)
        try:
            again = run_session(prompt_ledger(date, inp, left), date, "assemble-ledger-again", sub_schema(LEDGER_KEYS), budget)
            chunks, ledger_outs = chunks + [left], ledger_outs + [again]
        except RuntimeError as e:
            print(f"  組版: やり直しも答えなし: {e}", flush=True)
        still = uncovered_articles(chunks, ledger_outs)
        if still:
            anomaly("compose", f"組版: 台帳の判断(既報・続報予約・日付未確定の追跡)が、やり直しても返らなかった記事 {len(still)}本。"
                               "既報は見出しで補うが、続報予約と追跡はこの記事の分が無い:\n"
                               + "\n".join(f"- {a.get('slug')}: {a.get('title')}" for a in still))
    return merge_judgments(inp, chunks, outs[0], ledger_outs)


def _ledger_validator():
    from jsonschema import Draft202012Validator
    return Draft202012Validator(json.loads(sub_schema(LEDGER_KEYS)))


def story_row_ok(r) -> bool:
    """stories の1行が出力 schema(schema/assemble.schema.json)の形をしているか。形だけを見る。
    slug だけの行を「この記事は判断済み」の根拠にしない(監査指摘)。"""
    v = _ledger_validator()
    return v.evolve(schema=v.schema["properties"]["stories"]["items"]).is_valid(r)


def ledger_out_ok(o) -> bool:
    """台帳の答えが出力 schema のとおりか(4項目・各要素の欄と型・日付の形・列挙)。セッションは --json-schema で
    答えるが、構造化出力が無いときは本文の JSON を拾う経路があり、そこでは形が守られない。手で欄を1つずつ足して
    検めると漏れが残る(監査で配列そのもの→要素→要素の欄と、崩れ方が尽きなかった)ので、schema そのもので検める。
    崩れた答えは判断済みの根拠にも、まとめ(merge_judgments)にも使わない(崩れた欄で検算が止まる・辞書のキーを
    削除の指示と読む。監査指摘)。"""
    return _ledger_validator().is_valid(o)


def single_story_rows(o: dict, mine: set) -> dict[str, dict]:
    """1つの答えの stories から、渡した記事(mine)ごとに**ちょうど1行**の、形の揃った行を返す。
    同じ記事に2行(published_facts が F1 と F2 など)は契約違反(記事ごとに1件)で、どちらかを黙って捨てずに答えなしにする
    (やり直し・異常へ。数えるのは形の検査より前。監査指摘)。"""
    rows = [r for r in (o.get("stories") or []) if isinstance(r, dict)]
    count: dict = {}
    for r in rows:
        count[r.get("slug")] = count.get(r.get("slug"), 0) + 1
    return {r["slug"]: r for r in rows if story_row_ok(r) and r["slug"] in mine and count[r["slug"]] == 1}


def twice_pending_keys(o: dict) -> set:
    """1つの答えの pending_add で、同じ dedup_key に2行以上ある key。どちらかを黙って選ぶと、もう片方の追跡事項が
    消える(watch が「Aの日程」「Bの日程」など)。その記事は答えなしとしてやり直し、残れば異常にする(監査指摘)。"""
    count: dict = {}
    for r in o.get("pending_add") or []:
        if isinstance(r, dict):
            count[r.get("dedup_key")] = count.get(r.get("dedup_key"), 0) + 1
    return {k for k, n in count.items() if n > 1}


def uncovered_articles(chunks: list[list[dict]], ledger_outs: list[dict]) -> list[dict]:
    """どの組の答えにも形の揃った stories の行が無い記事(その組に渡した記事に限って数える)。
    台帳の4項目(stories・reservations・pending_add・pending_remove)が配列で揃っていない答えは、その組の記事を
    どれも判断済みにしない(予約・追跡に答えていないのに「無し」として進めない。明示の [] とは区別する。監査指摘)。"""
    covered = set()
    for ch, o in zip(chunks, ledger_outs):
        if not ledger_out_ok(o):
            continue
        twice = twice_pending_keys(o)
        covered |= {s for s in single_story_rows(o, {a.get("slug") for a in ch})
                    if not any(a.get("slug") == s and a.get("dedup_key") in twice for a in ch)}
    seen, out = set(), []
    for ch in chunks:
        for a in ch:
            if a.get("slug") not in covered and a.get("slug") not in seen:
                seen.add(a.get("slug"))
                out.append(a)
    return out


def merge_judgments(inp: dict, chunks: list[list[dict]], digest_out: dict, ledger_outs: list[dict]) -> dict:
    """分けて取った判断を、従来と同じ形の1つの出力にまとめる。**並びと採用規則を入力だけで決める**
    (モデルが返した順に依存すると、同じ答えでも再実行で台帳の中身が変わる。監査指摘)。

    - stories / reservations: その組に渡した記事の行だけを採り、**入力の記事順**に並べる。
      stories は記事ごとに最初の1件。reservations は記事順 → 日付 → 種別 → 件名。
    - pending_add: その組の記事の dedup_key のものだけを採る。同じ dedup_key は**記事順で先の組**の1件。
    - pending_remove: 入力の pending にあるものだけを、**入力の pending の順**に。
    内容の良し悪しは見ない(それは validate と校閲の仕事)。ここは形と順序だけ。
    """
    arts = inp.get("articles") or []
    order = {a.get("slug"): i for i, a in enumerate(arts)}
    stories: dict[str, dict] = {}
    reservations: list[dict] = []
    pending_add: dict[str, dict] = {}
    removed: set[str] = set()
    for ch, o in zip(chunks, ledger_outs):
        if not ledger_out_ok(o):
            print(f"  組版: 形の崩れた台帳の答えをまとめに使わない(記事 {', '.join(str(a.get('slug')) for a in ch)})", flush=True)
            continue
        mine = {a.get("slug") for a in ch}
        keys = {a.get("dedup_key") for a in ch}
        # 採るのは、その組に渡した記事ごとにちょうど1行の、形の揃った行だけ(single_story_rows)。形の崩れた行・同じ記事への
        # 2行・渡していない記事の行は採らない(崩れた行が辞書順で勝って検算が止まる・事実の片方を黙って捨てる。監査指摘)。
        # 1行も残らなければ validate が見出しで補う。別の組(やり直し)の行どうしは _pick で決める
        single = single_story_rows(o, mine)
        dropped_rows = len([r for r in (o.get("stories") or []) if not (isinstance(r, dict) and single.get(r.get("slug")) is r)])
        if dropped_rows:
            print(f"  組版: stories の行 {dropped_rows}件を採らなかった(形の崩れ・同じ記事に2行・渡していない記事)", flush=True)
        for slug, row in single.items():
            stories[slug] = _pick(stories.get(slug), row)
        for row in o.get("reservations") or []:
            slug = row.get("slug") if isinstance(row, dict) else None
            if slug not in mine:
                print(f"  組版: 渡していない記事の reservations を捨てた({slug!r})", flush=True)
            elif row not in reservations:
                reservations.append(row)
        twice = twice_pending_keys(o)
        for row in o.get("pending_add") or []:
            key = row.get("dedup_key") if isinstance(row, dict) else None
            if key not in keys:
                print(f"  組版: 渡していない記事の pending_add を捨てた({key!r})", flush=True)
            elif key in twice:
                # 同じ答えに同じ key の行が2つ: どちらかを黙って選ばない(その記事はやり直し・異常へ。twice_pending_keys)
                print(f"  組版: 同じ追跡事項に2行ある pending_add を採らなかった({key!r})", flush=True)
            else:
                pending_add[key] = _pick(pending_add.get(key), row)
        # 同じ答えで追跡事項の追加が2行に割れて無効になった key は、その答えの削除も採らない(追加を捨てて削除だけ残すと、
        # 既存の追跡事項が消えて代わりも残らない。監査指摘)
        removed |= {str(x) for x in o.get("pending_remove") or [] if x not in twice}
    reservations.sort(key=lambda r: (order[r["slug"]], str(r.get("date") or ""), str(r.get("kind") or ""),
                                     str(r.get("subject") or ""), str(r.get("candidate_id") or ""), str(r.get("note") or "")))
    return {"digest": digest_out.get("digest") or [],
            "stories": [stories[a.get("slug")] for a in arts if a.get("slug") in stories],
            "reservations": reservations,
            "pending_add": _in_article_order(arts, pending_add),
            "pending_remove": [x.get("dedup_key") for x in inp.get("pending") or [] if x.get("dedup_key") in removed]}


def _pick(a: dict | None, b: dict) -> dict:
    """同じ記事(同じ dedup_key)に行が2つ返ってきたとき、どちらを採るか。**返ってきた順では決めない**
    (順序が変わるだけで台帳の中身が変わる。監査指摘)。キー順を揃えた JSON の辞書順で小さいほうを採る。
    どちらが「良い」かは見ない。同じ答えの集まりから、いつも同じ1件を選ぶための規則。"""
    if a is None:
        return b
    canon = lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True)
    return a if canon(a) <= canon(b) else b


def _in_article_order(arts: list[dict], by_key: dict[str, dict]) -> list[dict]:
    """dedup_key → 行 の対応を、その dedup_key を持つ最初の記事の順に並べる(同じ key の記事が複数あっても1件)。"""
    out, done = [], set()
    for a in arts:
        k = a.get("dedup_key")
        if k in by_key and k not in done:
            out.append(by_key[k])
            done.add(k)
    return out


# ---------------------------------------------------------------- 検証
def _date_forms(iso: str) -> list[str]:
    """2026-09-12 → その日付の書き方いろいろ(素材の facts と照合するため)。"""
    try:
        d = datetime.date.fromisoformat(iso)
    except ValueError:
        return []
    return [iso, f"{d.month}月{d.day}日", f"{d.month:02d}月{d.day:02d}日", f"{d.month}/{d.day}",
            f"{d.month:02d}/{d.day:02d}", f"{d.year}/{d.month}/{d.day}", f"{d.year}年{d.month}月{d.day}日",
            f"{d.year}.{d.month}.{d.day}", f"{d.month}.{d.day}", f"{d.month}-{d.day}", f"{d.year}-{d.month}-{d.day}"]


_ZEN = str.maketrans("０１２３４５６７８９／．－", "0123456789/.-")


def _norm(text: str) -> str:
    """全角数字・記号を半角にする(素材の日付照合用。監査指摘)。"""
    return (text or "").translate(_ZEN)


def _supported(fact: str, text: str, n: int = 6) -> bool:
    """fact の連続 n 文字のどれかが入力にあるか(自由文の事実が入力から来ているかの粗い検算)。"""
    f = re.sub(r"\s", "", fact)
    t = re.sub(r"\s", "", text)
    return any(f[i:i + n] in t for i in range(0, max(1, len(f) - n + 1)))


def validate(date: str, out: dict, posts: list[dict], mats: dict, inp: dict,
             rejected: list | None = None) -> tuple[dict, list[str]]:
    """モデルの答えを機械で検める。通らないものは捨てて理由を残す(直しはしない)。
    rejected を渡すと、素材が合わない・素材に日付が無いので捨てた予約の行を (行, 理由) で足す(呼び手が差し戻す)。

    schema は形しか守らない。中身の危険(入力に無い記事を digest に書く、無関係な話題へ
    結合する、入力に無い事実を既報にする、素材に無い日付を予約する)はここで止める(監査指摘)。
    """
    notes: list[str] = []
    slugs = {fm.get("slug") for fm in posts}
    fm_of = {fm.get("slug"): fm for fm in posts}
    ids_of = {fm.get("slug"): set(fm.get("candidate_ids") or []) for fm in posts}
    brand_of = {fm.get("slug"): fm.get("brand") for fm in posts}
    art_in = {a["slug"]: a for a in inp.get("articles") or []}
    pending_keys = {x.get("dedup_key") for x in inp.get("pending") or []}
    subject_keys = {a.get("dedup_key") for a in art_in.values()} | {
        (mats.get(i) or {}).get("dedup_key") for s in ids_of.values() for i in s}

    digest = []
    seen_labels = []
    for g in out.get("digest") or []:
        rows = []
        for r in g.get("rows") or []:
            s = r.get("slug") or ""
            if s and s not in slugs:
                notes.append(f"digest: 無い記事 {s} を指す行を捨てた"); continue
            if not s and g.get("label") != "明日":
                # 記事を指さない行は「明日」の予約分だけ。他の群で作られたら入力に無い行
                notes.append(f"digest: 記事を指さない行を {g.get('label')} から捨てた({r.get('t')!r})"); continue
            # 行の文言が入力に根拠を持つか(記事なら見出し・リード・素材、明日の予約行なら予約の件名)
            if s:
                a = art_in.get(s) or {}
                basis = " ".join([str(a.get("title") or ""), str(a.get("lede") or "")]
                                 + [f["text"] for f in (a.get("facts") or [])])
            else:
                basis = " ".join(str(t.get("subject") or "") for t in (inp.get("tomorrow_reservations") or []))
            if not _supported(str(r.get("t") or ""), basis, n=3):
                notes.append(f"digest: 入力に根拠の無い行を捨てた({r.get('t')!r})"); continue
            if r.get("k") not in K_VOCAB or not r.get("t") or len(r["t"]) > 20 or len(r.get("d") or "") > 25:
                notes.append(f"digest: 形式外の行を捨てた({r.get('t')!r})"); continue
            row = {"k": r["k"], "t": r["t"], "d": r.get("d") or "", "brand": brand_of.get(s) or r.get("brand")}
            if row["brand"] not in BRANDS:
                notes.append(f"digest: 面が不正 {row['brand']}"); continue
            if s:
                row["slug"] = s
            rows.append(row)
        digest.append({"label": g.get("label"), "rows": rows[:4]})
        seen_labels.append(g.get("label"))
    if seen_labels != ["本日", "昨日", "継続中", "明日"]:
        notes.append(f"digest: 群が {seen_labels}。4群固定に直す")
        by = {g["label"]: g for g in digest}
        digest = [by.get(l, {"label": l, "rows": []}) for l in ("本日", "昨日", "継続中", "明日")]
    lead = next((fm.get("slug") for fm in posts if fm.get("rank") == "lead"), None)
    if lead and not any(r.get("slug") == lead for g in digest for r in g["rows"]):
        fm = next(f for f in posts if f.get("slug") == lead)
        digest[0]["rows"].insert(0, {"k": "発", "t": str(fm.get("title") or "")[:20], "d": "", "brand": fm.get("brand"), "slug": lead})
        digest[0]["rows"] = digest[0]["rows"][:4]
        notes.append("digest: 一面が無かったので本日の先頭に足した")
    # 全体12行は一面を足した**後**で揃える(先に縮めてから足すと、一面を欠く 4群×3行の答えが13行になり、lint が号を止めた。
    # 2026-10-14 監査)。多いぶんは末尾の群の末尾から削り、一面の行(最初の1行)は削らない
    keep = next(((gi, ri) for gi, g in enumerate(digest) for ri, r in enumerate(g["rows"]) if lead and r.get("slug") == lead), None)
    excess = sum(len(g["rows"]) for g in digest) - 12
    for gi in reversed(range(len(digest))):
        for ri in reversed(range(len(digest[gi]["rows"]))):
            if excess > 0 and (gi, ri) != keep:
                del digest[gi]["rows"][ri]; excess -= 1

    stories = []
    seen_story_slugs: set[str] = set()
    for s in out.get("stories") or []:
        slug = s.get("slug")
        a = art_in.get(slug)
        if a is None or not s.get("story_id") or not s.get("published_facts"):
            notes.append(f"stories: 不正な項目を捨てた({slug})"); continue
        if slug in seen_story_slugs:
            notes.append(f"stories: {slug} の2件目を捨てた(記事ごとに1件)"); continue
        allowed = {a.get("dedup_key")} | ({(a.get("existing_story") or {}).get("story_id")} if a.get("existing_story") else set())
        if s["story_id"] not in allowed:
            notes.append(f"stories: {slug} が無関係な話題 {s['story_id']} に結合しようとした → {a.get('dedup_key')} にする")
            s["story_id"] = a.get("dedup_key")
        fact_by_id = {f["id"]: f["text"] for f in (a.get("facts") or [])}
        basis = " ".join([str(a.get("title") or ""), str(a.get("lede") or "")] + list(fact_by_id.values()))
        facts = []
        for f in s["published_facts"][:4]:
            f = str(f).strip()
            if re.fullmatch(r"F\d+", f):
                if f in fact_by_id and fact_by_id[f] not in facts:
                    facts.append(fact_by_id[f])          # id で指した事実は素材そのもの
                else:
                    notes.append(f"stories: 無い事実 id {f} を捨てた({slug})")
                continue
            f = f[:140]
            # 自由文は見出し・リード・素材との4文字一致で粗く確かめる(言い換えは許す)
            if _supported(f, basis, n=4):
                facts.append(f)
            else:
                notes.append(f"stories: 入力に無い事実を捨てた({slug}: {f[:40]})")
        if not facts:
            notes.append(f"stories: {slug} は残る事実が無いので見出しを事実にする")
            facts = [str(a.get("title") or "")[:140]]
        seen_story_slugs.add(slug)
        stories.append({"slug": slug, "story_id": s["story_id"], "subject": (s.get("subject") or "")[:60],
                        "published_facts": facts})
    # **記事ごとに必ず1件。**返ってこなかった記事は見出しを事実として台帳に残す
    # (台帳に無い記事は翌日「既報」にならず、同じ話題がまた記事になる。監査指摘)
    for slug, a in art_in.items():
        if slug not in seen_story_slugs:
            notes.append(f"stories: {slug} の分が無いので見出しで補った")
            exist = a.get("existing_story") or {}
            stories.append({"slug": slug, "story_id": exist.get("story_id") or a.get("dedup_key"),
                            "subject": (exist.get("subject") or str(a.get("title") or ""))[:60],
                            "published_facts": [str(a.get("title") or "")[:140]]})

    res = []
    for r in out.get("reservations") or []:
        s = r.get("slug")
        cid = r.get("candidate_id")
        if s not in slugs or cid not in ids_of.get(s, set()) or cid not in mats:
            notes.append(f"reservations: 記事か素材が合わないので捨てた({s}/{cid})")
            if rejected is not None and s in slugs:
                rejected.append((r, f"candidate_id {cid} はこの記事の素材に無い"))
            continue
        try:
            dt = datetime.date.fromisoformat(r.get("date") or "")
        except ValueError:
            notes.append(f"reservations: 日付が読めない {r.get('date')!r}"); continue
        if dt.isoformat() <= date or r.get("kind") not in KINDS:
            notes.append(f"reservations: 過去日か種別不正 {r.get('date')}/{r.get('kind')}"); continue
        c = mats[cid]
        # 記事が確かめた事実(verified_facts)の日付も根拠にする(反映が予約に添える。verified_rows)
        hay = material_hay(c) + " " + _norm(" ".join(str(v["text"]) for v in article_verified_facts(fm_of[s])))
        # 予約の日か、予約が指しうる出来事の日(締切前は締切日、06:00 前に終わる締切・終了は翌日。renderlib.RESERVATION_LAG)が
        # 素材にあればよい(出来事の日の答えが外れても、この照合は同じ範囲で掛ける。監査指摘)
        probe = [dt] + [dt + datetime.timedelta(days=n) for n in renderlib.RESERVATION_LAG.get(r["kind"], (0,)) if n]
        years = renderlib.years_in(hay + " " + str(c.get("published_date") or "") + " " + str(c.get("url") or "") + " " + date)
        # 予約が指す出来事の日(occurs_on)は予約の日とは別に持つ(2026-10-14 監査指摘: 10/1 4:59 に終わる催しを 9/30 に予約し、
        # 予約の日を出来事の日として執筆に渡していた)。予約の日からのずれが種別と合わない・素材に無い日は採らない
        # (予約は残し、出来事の日は読み込み側の決め方に任せる)
        try:
            occ = datetime.date.fromisoformat(str(r.get("occurs_on") or ""))
        except ValueError:
            occ = None
        if occ is not None and ((occ - dt).days not in renderlib.RESERVATION_LAG.get(r["kind"], (0,))
                                or not renderlib.date_mentioned(occ.isoformat(), hay, years)):
            notes.append(f"reservations: 出来事の日 {occ.isoformat()} が素材に無いか、予約の日 {dt.isoformat()}・種別 {r['kind']} と"
                         f"合わない。出来事の日を付けずに予約する({s})")
            occ = None
        if occ is None and not any(renderlib.date_mentioned(p.isoformat(), hay, years) for p in probe):
            notes.append(f"reservations: 素材に無い日付 {dt.isoformat()} を捨てた({s})")
            if rejected is not None:
                rejected.append((r, f"candidate_id {cid} の素材に {dt.isoformat()} の日付が無い"))
            continue
        res.append({"slug": s, "candidate_id": cid, "date": dt.isoformat(), "kind": r["kind"],
                    "subject": (r.get("subject") or "")[:60], "note": (r.get("note") or "")[:120],
                    **({"occurs_on": occ.isoformat()} if occ else {})})

    p_add = []
    for x in out.get("pending_add") or []:
        if not x.get("dedup_key") or x.get("brand") not in BRANDS:
            continue
        if x["dedup_key"] not in subject_keys:
            notes.append(f"pending_add: この号の主題でない {x['dedup_key']} を捨てた"); continue
        p_add.append({"dedup_key": x["dedup_key"], "brand": x.get("brand"), "subject": (x.get("subject") or "")[:60],
                      "watch": (x.get("watch") or "")[:140]})
    p_rm = []
    for x in out.get("pending_remove") or []:
        if str(x) in pending_keys:
            p_rm.append(str(x))
        else:
            notes.append(f"pending_remove: 無い項目 {x} を無視した")
    return {"digest": digest, "stories": stories, "reservations": res, "pending_add": p_add, "pending_remove": p_rm}, notes


# ---------------------------------------------------------------- 反映(冪等)
def strip_edition(date: str, stories: list[dict]) -> list[dict]:
    """台帳からこの号の寄与を剥がす。edition_facts[date] に記録した事実だけを消す。"""
    out = []
    for e in stories:
        ef = dict(e.get("edition_facts") or {})
        mine = ef.pop(date, None)
        if mine:
            e["published_facts"] = [f for f in e.get("published_facts") or [] if f not in mine]
        if ef:
            e["edition_facts"] = ef
        else:
            e.pop("edition_facts", None)
        if e.get("first_published") == date and not e.get("published_facts"):
            continue  # この号が作った話題で、剥がしたら空になった
        if e.get("first_published") == date and ef:
            # この号が作った話題だが、後続号の事実が残っている。初出を残っている最古の号にする
            # (剥がした号を初出として指し続けない。監査指摘)
            e["first_published"] = min(ef)
        out.append(e)
    return out


def rollback(date: str) -> list[str]:
    """この号の組版の寄与を stock から剥がし、組版前の状態に戻す(号を作り直すときに使う)。

    台帳は edition_facts[date] の印で剥がす。予約は reserved_on == date を消す。
    pending は行の印 added_on・removed_on で、この号の足し引きだけを外す(rebase_pending)。
    控え(metrics/*-before-<日付>.yml)は**消さない**:
    消すと次の組版が「組版後の状態」を新しい組版前として控え直し、消した追跡事項が
    戻らず、記事の既報判定にこの号自身の事実が混ざる(監査指摘)。
    """
    log = []
    stories = load_yaml_list(STORIES)
    # strip_edition は dict を**その場で**書き換えるので、印の有無は剥がす前に見る(監査指摘)
    had = any(date in (e.get("edition_facts") or {}) for e in stories)
    kept = strip_edition(date, stories)
    if had or len(kept) != len(stories):
        dump_yaml(STORIES, kept)
        log.append(f"台帳からこの号の寄与を剥がした({len(stories)}→{len(kept)} 話題)")
    for p in SCHEDULED.glob("*.json"):
        rows = json.loads(p.read_text(encoding="utf-8"))
        rest = [r for r in rows if r.get("reserved_on") != date]
        if len(rest) != len(rows):
            if rest:
                write_checked(p, json.dumps(rest, ensure_ascii=False, indent=1) + "\n")
            else:
                p.unlink()
            log.append(f"予約 {p.name}: この号の {len(rows) - len(rest)} 件を外した")
    # pending は、この号の足し引き(印 added_on・removed_on)だけを外す。後の号の足し引きは残す(rebase_pending)
    if (ROOT / "metrics" / f"pending-before-{date}.yml").exists():
        rows = pending_rows()
        want = rebase_pending(date, rows, [], [])
        if load_yaml_list(PENDING) != want:
            dump_yaml(PENDING, want, header=PENDING_HEADER)
        if want != rows:
            log.append("pending からこの号の足し引きを外した")
    return log


def baseline_stories(date: str) -> list[dict]:
    """組版前の台帳。

    **入力を作るときも反映するときも、同じこれを使う。**反映時だけ剥がすと、
    再実行のときに前回この号が足した事実を「既報」としてモデルに渡し、モデルが
    それを繰り返さず、反映直前に剥がされて台帳から消える(監査指摘 P0-2)。

    当日の再実行は「現在の台帳からこの号の寄与を剥がしたもの」で足りるが、後続号が
    台帳に入ったあとに過去号を組み直すと、後続号の事実が「組版前」に混ざる。
    初回に控えた metrics/stories-before-<日付>.yml があればそれを起点にする(監査指摘)。
    ただし後続号が同じ話題に足した事実は、控えには無いので現在の台帳から補う
    (剥がすのはこの号の寄与だけ)。
    """
    # **入力用と反映用は別の系統**(監査指摘)。ここは入力用: 初回に控えた組版前の台帳
    # そのもの(後続号の事実を混ぜない。混ぜると過去号の組み直しで未来の事実が「既報」に
    # 見え、当時残すべき事実を省く)。反映は apply が現在の台帳へ合流させる
    saved_p = ROOT / "metrics" / f"stories-before-{date}.yml"
    if saved_p.exists():
        return load_yaml_list(saved_p)
    return strip_edition(date, load_yaml_list(STORIES))


def added_on(x: dict) -> str:
    """pending の行を足した号(印。無ければ空 = この仕組みより前・手で足した行)。"""
    return str(x.get("added_on") or "")


def removed_on(x: dict) -> str:
    """pending の行を消した(決着させた)号(印。無ければ空 = 追跡中)。"""
    return str(x.get("removed_on") or "")


def active_pending(rows: list[dict]) -> list[dict]:
    """追跡中の行(消した印の無い行)。判断に渡すのはこれだけ。"""
    return [x for x in rows if not removed_on(x)]


def baseline_pending(date: str, dry: bool) -> list[dict]:
    """組版前の pending(判断に渡す入力。追跡中の行だけ)。初回に控え(metrics/pending-before-<日付>.yml)、以後はそれを起点にする。
    初回だけは現在の pending がそのまま控えになる(監査指摘 P0-4)。反映は rebase_pending がいまの pending へ当てる。
    """
    p = ROOT / "metrics" / f"pending-before-{date}.yml"
    if p.exists():
        return active_pending(load_yaml_list(p))
    cur = active_pending(load_yaml_list(PENDING))
    if not dry:
        dump_yaml(p, cur)
    return cur


def pending_rows() -> list[dict]:
    """号の足し引きを当て直す起点: pending の全行(消した印の付いた行も)。

    印の仕組みで書いた pending(1行目が PENDING_HEADER)はそのまま。それより前に書かれた pending は、行に印が無いので、
    控え(metrics/pending-before-<日付>.yml)の並びから号ごとの足し引きを読んで印に直す(mark_history)。印に直した pending は、
    次に書くとき PENDING_HEADER と**同じ1回の書き込み**で残る(印と見出しが食い違わない)。号ごとに控えの差から読むと、ある号の
    巻き戻しが、行の無い(後の号が消した)追加に印を残せず、後の号の巻き戻しがよみがえらせた(監査指摘)。"""
    if not PENDING.exists():
        return []
    cur = load_yaml_list(PENDING)
    if PENDING.read_text(encoding="utf-8").startswith(PENDING_HEADER):
        return cur
    return mark_history(cur)


def mark_history(cur: list[dict]) -> list[dict]:
    """印の無い pending に、控えの並びから読んだ号ごとの足し引きの印を付ける。号 d の足し引きは「d の控え」と「次の号の控え
    (最後の号はいまの pending)」の差。追跡中の行には足した号を added_on に、消えた行は控えの中身に added_on(分かれば)・
    removed_on を付けて足す。最初の控えより前からある行は added_on を付けない。すでにある印は上書きしない。"""
    key = lambda x: x.get("dedup_key")
    snaps = sorted((q.stem[len("pending-before-"):], q) for q in (ROOT / "metrics").glob("pending-before-*.yml")
                   if re.fullmatch(r"pending-before-\d{4}-\d{2}-\d{2}\.yml", q.name))
    if not snaps:
        return cur
    rows = [active_pending(load_yaml_list(q)) for _, q in snaps] + [active_pending(cur)]
    adder = {key(x): "" for x in rows[0]}
    tombs = []
    for i, (d, _) in enumerate(snaps):
        now, nxt = {key(x): x for x in rows[i]}, {key(x) for x in rows[i + 1]}
        for k, x in now.items():
            if k not in nxt:
                tombs.append({**x, **({"added_on": adder[k]} if adder.get(k) else {}), "removed_on": d})
                adder.pop(k, None)
        for k in nxt - set(now):
            adder[k] = d
    have = {(key(x), removed_on(x)) for x in cur if removed_on(x)}
    out = [{**x, "added_on": adder[key(x)]} if not removed_on(x) and not added_on(x) and adder.get(key(x)) else dict(x) for x in cur]
    return out + [t for t in tombs if (key(t), removed_on(t)) not in have]


def rebase_pending(date: str, rows: list[dict], add: list[dict], remove: list[str]) -> list[dict]:
    """pending の全行 rows から、この号の前回の足し引きを外し、今回の足し引き(add・remove)を当てた全行を返す。
    add・remove が空なら巻き戻し(この号の寄与だけを外す)。

    以前は組版前の控えから毎回組み立て直したので、過去の号を組み直すと後の号が足した追跡事項が黙って消えた(2026-10-14 監査:
    10/12 の組み直しで 10/13 の追加が消えた)。台帳(edition_facts)と同じく号の寄与に印を付け、**印は stock/pending.yml の行に
    持たせる**(別のファイルに記録すると、2つの書き込みの間で止まったときに印と中身が食い違い、消した行を戻せなくなった。監査指摘):
    - 足した行には added_on: <号>。消した行は消さずに removed_on: <号> を付けて残す(判断には渡さない)
    - 前回この号が足した行は、今回も足すなら同じ位置で差し替え、足さないなら外す。前回この号が消した行は、今回も消すならそのまま、
      消さないなら戻す(同じ key の行が追跡中なら戻さない)。今回消すのは、この号より前からある追跡中の行だけ
    - 後の号の足し引きは残す: 後の号が足した行は消さない・後の号が消した key は足し直さない。この号が足して後の号が消した行は、
      今回も足すなら決着済みのまま残し、足さないなら voided(足した号が取り消した)にする。voided の行は、それを消した号の組み直しで
      戻さない(取り消した追加を、後の号の組み直しがよみがえらせない。監査指摘)
    """
    key = lambda x: x.get("dedup_key")
    rm = set(remove)
    new: dict[str, dict] = {}
    for x in add:
        new.setdefault(key(x), {**x, "added_on": date})
    later_rm = {key(x) for x in rows if removed_on(x) > date}
    removes = lambda x: not removed_on(x) and key(x) in rm and added_on(x) < date      # 今回この号が消す行
    tracked = {key(x) for x in rows if not removed_on(x) and added_on(x) != date and not removes(x)}
    out, placed = [], set()
    for x in rows:
        k = key(x)
        if added_on(x) == date:                      # 前回この号が足した行
            if removed_on(x):                        # 後の号が消した: 今回も足すなら決着済みのまま、足さないなら取り消し
                if k in new:
                    out.append({y: v for y, v in x.items() if y != "voided"}); placed.add(k)
                else:
                    out.append({**x, "voided": True})
            elif k in new and k not in placed and k not in tracked and k not in later_rm:
                out.append(new[k]); placed.add(k)
        elif removed_on(x) == date:                  # 前回この号が消した行
            if k in rm:
                out.append(x)
            elif not x.get("voided") and k not in tracked:
                out.append({y: v for y, v in x.items() if y != "removed_on"}); tracked.add(k)
        elif removes(x):
            out.append({**x, "removed_on": date})
        else:
            out.append(x)
    for k, x in new.items():
        if k not in placed and k not in tracked and k not in later_rm:
            out.append(x); placed.add(k)
    return out


def apply(date: str, number: int, out: dict, posts: list[dict], mats: dict, dry: bool,
          stories: list[dict] | None = None) -> list[str]:
    log: list[str] = []
    d = datetime.date.fromisoformat(date)

    # 1. 既報台帳。**反映用**は現在の台帳からこの号の寄与を剥がしたもの(後続号の事実は残る)。
    #    **入力用**の控え(stories-before)は初回だけ書き、以後は触らない(監査指摘)
    before = ROOT / "metrics" / f"stories-before-{date}.yml"
    if not dry and not before.exists():
        dump_yaml(before, stories if stories is not None else strip_edition(date, load_yaml_list(STORIES)))
    stories = strip_edition(date, load_yaml_list(STORIES))
    by_id = {e.get("story_id"): e for e in stories}
    brand_of = {fm.get("slug"): fm.get("brand") for fm in posts}
    fm_of = {fm.get("slug"): fm for fm in posts}
    n_new = n_app = 0
    for s in out["stories"]:
        e = by_id.get(s["story_id"])
        if e is None:
            e = {"story_id": s["story_id"], "brand": brand_of.get(s["slug"]), "subject": s["subject"],
                 "status": "active", "first_published": date, "published_facts": []}
            stories.append(e); by_id[s["story_id"]] = e; n_new += 1
        added = [f for f in s["published_facts"] if f not in (e.get("published_facts") or [])]
        if added:
            e.setdefault("published_facts", []).extend(added)
            # 同じ話題に同号の記事が2本寄与しても、所有記録は**足す**(上書きすると
            # 先の記事の分が剥がせなくなる。監査指摘)
            mine = e.setdefault("edition_facts", {}).setdefault(date, [])
            mine.extend(f for f in added if f not in mine)
            n_app += len(added)
    log.append(f"台帳: 新規 {n_new} 話題 / 事実 {n_app} 件")

    # 2. 続報予約(この号が付けた分を剥がしてから付け直す)
    files = {}
    for p in SCHEDULED.glob("*.json"):
        rows = json.loads(p.read_text(encoding="utf-8"))
        kept = [r for r in rows if r.get("reserved_on") != date]
        if len(kept) != len(rows):
            files[p] = kept
    n_res = 0
    for r in out["reservations"]:
        c = mats[r["candidate_id"]]
        p = SCHEDULED / f"{r['date']}.json"
        rows = files.get(p)
        if rows is None:
            rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        dk = c.get("dedup_key") or r["slug"]
        rid = reservation_id(r, mats)
        # 添えを素材に付けた予約は、話題は添え先の予約のまま、素材の元の話題を src_dedup_key に残す(上書きしない)
        src_dk = source_topic(c, {})
        if any(x.get("id") == rid for x in rows):
            continue
        # facts は全部写す(予約はその日の唯一の素材。過去の candidates は読まないので、切り詰めると
        # 予約の根拠の日付ごと落ちる。実測 2026-10-07: 13件目の「10/10 14:59まで」が落ち、締切前の記事を見送った)
        entry = {"id": rid, "dedup_key": dk, "brand": brand_of.get(r["slug"]) or c.get("brand") or "other",
                 "subject": r["subject"] or c.get("title") or dk, "kind": r["kind"], "note": r["note"],
                 "reserved_on": date, **({"occurs_on": r["occurs_on"]} if r.get("occurs_on") else {}),
                 **material_snapshot(c), "src_candidate_id": r["candidate_id"],
                 **({"src_dedup_key": src_dk} if src_dk and src_dk != dk else {})}
        # 記事が出典にしたほかの素材も添える(予約はその日の唯一の素材。1つだけ写すと、記事を成り立たせた公式の出典が落ちる)
        comps = companion_rows(entry, fm_of.get(r["slug"]) or {}, mats)
        comps += verified_rows(entry, r, fm_of.get(r["slug"]) or {}, c, comps, date)
        rows += [entry] + comps; files[p] = rows; n_res += 1
    log.append(f"予約: {n_res} 件")

    # 3. pending(この号の足し引きを、いまの pending に当て直す。後の号の足し引きは残す。rebase_pending)
    baseline_pending(date, dry)          # 組版前の控え(初回だけ書く)
    pending = rebase_pending(date, pending_rows(), out["pending_add"], out["pending_remove"])
    log.append(f"pending: -{len(set(out['pending_remove']))} +{len(out['pending_add'])} → {len(active_pending(pending))} 件")

    # 4. 号スナップショット(機械算出欄は derive が上書きする)
    lead = next((fm.get("slug") for fm in posts if fm.get("rank") == "lead"), (posts[0].get("slug") if posts else ""))
    ed_path = EDITIONS / f"{date}.md"
    old = {}
    if ed_path.exists():
        m = FM_RE.match(ed_path.read_text(encoding="utf-8"))
        old = yaml.safe_load(m.group(1)) if m else {}
    snap = {"number": number if number is not None else old.get("number", 0), "date": date,
            "weekday": "月火水木金土日"[d.weekday()], "issued_at": f"{date}T06:00:00+09:00",
            "pages": old.get("pages", 0), "article_count": old.get("article_count", 0),
            "corrected_count": old.get("corrected_count", 0), "lead_slug": lead,
            "digest": out["digest"], "ranking": old.get("ranking", []), "birthdays": old.get("birthdays", [])}
    log.append(f"digest: {sum(len(g['rows']) for g in out['digest'])} 行 / 一面 {lead}")

    if dry:
        return log
    dump_yaml(STORIES, stories)
    for p, rows in files.items():
        if rows:
            write_checked(p, json.dumps(rows, ensure_ascii=False, indent=1) + "\n")
        else:
            p.unlink(missing_ok=True)
    dump_yaml(PENDING, pending, header=PENDING_HEADER)
    write_snapshot(ed_path, snap)
    return log


def write_snapshot(ed_path: Path, snap: dict) -> None:
    """号のスナップショットの途中の形(ranking・birthdays は派生 derive が埋める)を書く。最終の書式では検めず、最終の形を書く
    derive が書式で検める。書式を素通しで書くのはここだけ(テストの見張りの許可は、この関数に限る。監査指摘)。
    書き方は dump_yaml と同じ置き換え(途中で止まっても前の号ファイルを残す)。"""
    tmp = ed_path.parent / f".{ed_path.name}.{os.getpid()}.tmp"
    try:
        tmp.write_text("---\n" + yaml.safe_dump(snap, allow_unicode=True, sort_keys=False, width=200) + "---\n", encoding="utf-8")
        os.replace(tmp, ed_path)
    finally:
        tmp.unlink(missing_ok=True)


def reservation_id(r: dict, mats: dict) -> str:
    """予約の行が反映される予約の id(日・素材の話題・種別。apply は同じ id を重ねない)。"""
    c = mats.get(r.get("candidate_id")) or {}
    return f"sched-{r.get('date')}-{c.get('dedup_key') or r.get('slug')}-{r.get('kind')}"


def return_reservations(date: str, inp: dict, out: dict, posts: list[dict], mats: dict, rejected: list,
                        notes: list[str], budget=None) -> tuple[dict, list[str]]:
    """検算で採らなかった予約(素材が合わない・素材にその日付が無い)を、その記事だけ理由を添えて1回だけ答え直させる
    (捨てて終えない。2026-10-24: まとめ記事の締切予約が日付を持たない素材を指して捨てられ、10/26 の当日締切の知らせが
    その号の素材に戻らなかった)。答え直しの予約で置き換えて検め直し、それでも採れない記事は名指しして異常にする。
    戻りは validate と同じ (検めた答え, 検算の記録)。"""
    slugs = list(dict.fromkeys(r.get("slug") for r, _ in rejected))
    arts = [a for a in inp.get("articles") or [] if a.get("slug") in slugs]
    items = "\n".join(f"- {_v(r.get('slug'))}: {_v(r.get('date'))} {_v(r.get('kind'))}「{_v(r.get('subject'))}」"
                      f"(candidate_id {_v(r.get('candidate_id'))}): {why}" for r, why in rejected)
    print(f"  組版: 採らなかった予約 {len(rejected)}件を、記事 {len(arts)}本に差し戻す", flush=True)
    try:
        again = run_session(prompt_ledger(date, inp, arts) + render_prompt("assemble-ledger.returned", ITEMS=items),
                            date, "assemble-ledger-returned", sub_schema(LEDGER_KEYS), budget)
    except RuntimeError as e:
        anomaly("compose", f"組版: 採らなかった続報予約の差し戻しに答えが無い({str(e)[:200]})。この予約は無い:\n{items}")
        return validate(date, out, posts, mats, inp)
    gone = [r for r, _ in rejected]
    redo = [r for r in again.get("reservations") or [] if isinstance(r, dict) and r.get("slug") in slugs]
    kept_rows = [r for r in out.get("reservations") or [] if r not in gone]
    # 初めから採れていた予約(差し戻していないもの)は、答え直しを足す前の答えで検めて控える(答え直しと同じ行を除かない。Sol 監査 r3)
    first, _ = validate(date, {**out, "reservations": kept_rows}, posts, mats, inp)
    out = {**out, "reservations": kept_rows + redo}
    clean, notes2 = validate(date, out, posts, mats, inp)
    # 差し戻した予約が、答え直しで採れたかを数で照合する: 記事・日・種別ごとに、答え直しで**新しく反映される**予約(反映が重ねない
    # 単位 reservation_id で、初めから採れていた予約に無いもの)の数が、差し戻した数に足りなければ、その組を名指しする。答え直しが
    # 予約を省いた・また採れない形だった(Sol 監査 r1)、同じ日・種別の2件の片方だけ戻った(r2)、採れていた予約を答え直しが繰り返した
    # (r3。繰り返しは反映で1件になり、戻ったことにならない)のどれも黙って消さない。答え直しは candidate_id も件名も直しうるので、
    # どの予約が戻ったかは数でしか言えない。足りない組の差し戻した予約を全部挙げる
    key = lambda r: (r.get("slug"), r.get("date"), r.get("kind"))
    had = {reservation_id(r, mats) for r in first.get("reservations") or []}
    new: dict[tuple, set] = collections.defaultdict(set)
    for r in clean.get("reservations") or []:
        if reservation_id(r, mats) not in had:
            new[key(r)].add(reservation_id(r, mats))
    sent = collections.Counter(key(r) for r, _ in rejected)
    short = {k: n - len(new[k]) for k, n in sent.items() if n > len(new[k])}
    if short:
        anomaly("compose", "組版: 差し戻しても採れなかった続報予約(答え直しに無い・素材が合わない・素材にその日付が無い)。"
                           "次の組の予約が、差し戻した数より少ない:\n"
                           + "\n".join(f"- {_v(r.get('slug'))}: {_v(r.get('date'))} {_v(r.get('kind'))}「{_v(r.get('subject'))}」"
                                       f"(candidate_id {_v(r.get('candidate_id'))}): {why}"
                                       f"(同じ記事・日・種別の差し戻し {sent[key(r)]}件のうち {short[key(r)]}件が戻らない)"
                                       for r, why in rejected if key(r) in short))
    return clean, notes + ["差し戻しのあと:"] + notes2


def run(date: str, number: int | None = None, dry: bool = False, budget=None) -> int:
    posts = load_posts(date)
    if not posts:
        print(f"{date}: 記事が無い", flush=True)
        return 1
    mats = load_materials(date)
    # 入力も反映も**同じ組版前の状態**から(この号の寄与を剥がした台帳・控えた pending)
    stories = baseline_stories(date)
    pending = baseline_pending(date, dry)
    inp = build_input(date, posts, mats, stories, pending)
    out = judge(date, inp, budget)
    rejected: list = []
    clean, notes = validate(date, out, posts, mats, inp, rejected=rejected)
    if rejected:
        clean, notes = return_reservations(date, inp, out, posts, mats, rejected, notes, budget)
    for n in notes:
        print("  検算:", n, flush=True)
    for line in apply(date, number, clean, posts, mats, dry, stories=stories):
        print("  " + line, flush=True)
    if dry:
        return 0
    # 派生欄(derive)と lint は**終了コードで**判定する。stdout の `::error` だけを見ていたので、検査そのものが例外で落ちても
    # (別の号の書きかけの候補 JSON で lint が JSONDecodeError)組版は 0 を返していた(2026-10-14 監査)。
    # 非0(signal での終了を含む)は組版の失敗とし、言い分(stderr)を添えて上げる。derive が落ちたら lint へ進まない
    d = subprocess.run([sys.executable, str(ROOT / "scripts" / "derive.py"), "--date", date, "--write"],
                       cwd=ROOT, capture_output=True, text=True)
    if d.returncode != 0:
        print(f"  derive: 異常終了(exit {d.returncode}): {err_tail(d)}", flush=True)
        notify("compose", f"{date}: 組版のあと派生欄の算出(derive)が異常終了した(exit {d.returncode})。"
                          f"組版の欠陥として扱う: {err_tail(d)}", ok=False)
        return 2
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "lint.py"), "--no-net"],
                       cwd=ROOT, capture_output=True, text=True)
    errs = [l for l in (r.stdout or "").splitlines() if l.startswith("::error")]
    print("  " + ((r.stdout or "").strip().splitlines() or ["lint: (出力なし)"])[-1], flush=True)
    if errs:
        # 組版の欠陥。セッションに直させない(直させると何が壊れたか分からなくなる)
        notify("compose", f"{date}: 組版のあと lint が赤い({len(errs)}件)。組版の欠陥として扱う:\n- "
                          + "\n- ".join(e.split("::", 2)[-1][:120] for e in errs[:5]), ok=False)
        return 2
    if r.returncode != 0:
        print(f"  lint: 異常終了(exit {r.returncode}): {err_tail(r)}", flush=True)
        notify("compose", f"{date}: 組版のあと lint が異常終了した(exit {r.returncode})。検査が済んでいないので組版の失敗として扱う: "
                          f"{err_tail(r)}", ok=False)
        return 2
    return 0


def main() -> int:
    from pipelib import exit_on_term
    exit_on_term()      # SIGTERM でも、起動した組版のセッションを子孫ごと止めてから終わる
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--number", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="判断まで行い、ファイルへ反映しない")
    a = ap.parse_args()
    return run(a.date, a.number, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
