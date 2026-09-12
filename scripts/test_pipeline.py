#!/usr/bin/env python3
"""回帰テスト(selfcheck から呼ばれる。単体でも走る): 監査で見つかった欠陥を二度と通さない。

  python3 scripts/test_pipeline.py

外部サービス・本体の作業ツリー・Git には触らない。全部一時ディレクトリの中で済ませる。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assemble
import compose
import pipelib
import renderlib

FAILS: list[str] = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


MATS = [{"id": "c1", "url": "https://a.example/1", "title": "告知 2026-10-01", "published_date": "2026-09-01",
         "facts": ["9月13日開催", "価格3000円"], "unbacked_facts": ["9月20日"]},
        {"id": "c2", "url": "https://b.example/2", "facts": ["出演A"], "deadline": "2026-09-30"},
        {"id": "c3", "url": "https://c.example/3", "facts": ["出演B"]}]
OK = {"status": "ok", "decline_code": "", "decline_detail": "", "title": "t", "title_fact_ids": ["F1"], "lede": "l",
      "lede_fact_ids": ["F1"], "blocks": [{"markdown": "x", "fact_ids": ["F1", "F3", "F4"]}], "tags": ["a", "b"],
      "sources": [{"url": "https://a.example/1", "label": "x"}, {"url": "https://b.example/2", "label": "y"},
                  {"url": "https://c.example/3", "label": "z"}],
      "event_date": "2026-09-13", "new_facts": [], "addressed_issue_ids": []}


def test_check_output():
    _, fb = renderlib.materials_with_ids(MATS)
    C = lambda o, **k: renderlib.check_output(o, fb, MATS, **k)
    check(C(OK) == [], f"合格するはずの出力が落ちた: {C(OK)}")
    for rank in ("roundup", "culture"):
        check(C(OK, rank=rank) == [], f"{rank} 合格が落ちた: {C(OK, rank=rank)}")
        bad = dict(OK, blocks=[{"markdown": "x", "fact_ids": ["F1"]}], sources=OK["sources"][:1])
        check(any(rank in p for p in C(bad, rank=rank)), f"{rank} の素材不足が通った")
    # 出典を隠していないか・日付が素材と合うかは校閲(モデル)の判断。機械は形しか見ない
    check(C(dict(OK, sources=OK["sources"][:1])) == [], "出典の取捨(校閲の判断)を機械が落とした")
    check(C(dict(OK, event_date="2026-10-01")) == [], "日付の整合(校閲の判断)を機械が落とした")
    check(any("event_date" in p for p in C(dict(OK, event_date="2026-9-1"))), "日付の形が崩れているのに通った")
    good = dict(OK, sources=OK["sources"] + [{"url": "https://z.example/9", "label": "w"}],
                new_facts=[{"id": "N1", "text": "9月25日発売", "url": "https://z.example/9"}], event_date="2026-09-25",
                blocks=[{"markdown": "x", "fact_ids": ["F1", "F3", "F4", "N1"]}])
    check(C(good) == [], f"new_facts 付きの出力が落ちた: {C(good)}")
    # 素材に無い URL(執筆が見つけた出典)は許す。実在・一致の確認は校閲(項目3)
    check(C(dict(OK, sources=OK["sources"] + [{"url": "https://q.example", "label": "q"}])) == [], "素材に無い出典 URL を機械が落とした")
    check(C(dict(OK, sources=OK["sources"][:2] + [{"url": "https://c.example/3", "label": "#タグ_付き ID"}])) == [], "label の # や _ を落とした")
    check(any("Markdown" in p for p in C(dict(OK, sources=OK["sources"][:2] + [{"url": "https://c.example/3", "label": "[リンク](x)"}]))),
          "label の Markdown リンクが通った")
    check(any("tags" in p for p in C(dict(OK, tags=["a"]))), "tags 1個が通った")
    check(any("見出し" in p for p in C(dict(OK, title_fact_ids=[]))), "見出しの根拠無しが通った")
    check(len(C({"status": "decline", "decline_code": "", "decline_detail": ""})) == 2, "理由の無い decline が通った")
    check(C({"status": "decline", "decline_code": "NOT_NEWS", "decline_detail": "x"}) == [], "正当な decline が落ちた")
    check(any("decline_code" in p for p in C(dict(OK, decline_code="OTHER"))), "ok なのに decline_code ありが通った")


def test_render_and_length():
    _, fb = renderlib.materials_with_ids(MATS)
    good = dict(OK, new_facts=[{"id": "N1", "text": "9月25日発売", "url": "https://a.example/1"}],
                blocks=[{"markdown": "本文", "fact_ids": ["F1", "N1"]}])
    p = Path(tempfile.mkdtemp()) / "2026-09-12-x.md"
    renderlib.render_article(p, "2026-09-12", {"slug": "x", "brand": "765", "candidate_ids": ["c1"], "rank": "small"},
                             good, lambda u: "公式", lambda ts: "公式", compose.yaml_dump_keeping_strings)
    t = p.read_text(encoding="utf-8")
    check("<!-- F1 N1 -->" in t and "verified_facts" in t and "title_fact_ids" in t, "根拠が記事に残らない")
    check(compose.body_length(p) == 2, f"根拠コメントが字数に入っている: {compose.body_length(p)}")


def test_schema_hash_dates():
    """生成物が記事 schema を通る / 根拠メタデータが指紋に入る / 日付照合は境界付き(監査指摘 R14)。"""
    from jsonschema import Draft202012Validator
    import yaml
    schema = json.loads((Path(__file__).resolve().parent.parent / "schema" / "article.schema.json").read_text(encoding="utf-8"))
    v = Draft202012Validator(schema)
    good = dict(OK, new_facts=[{"id": "N1", "text": "9月25日発売", "url": "https://a.example/1"}],
                blocks=[{"markdown": "段落A\n\n段落B", "fact_ids": ["F1", "N1"]}])
    p = Path(tempfile.mkdtemp()) / "2026-09-12-x.md"
    renderlib.render_article(p, "2026-09-12", {"slug": "x", "brand": "765", "candidate_ids": ["c1"], "rank": "small"},
                             good, lambda u: "公式", lambda ts: "公式", compose.yaml_dump_keeping_strings)
    text = p.read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("\n---\n", 1)[0].lstrip("-\n"))
    errs = [e.message for e in v.iter_errors(fm)]
    check(errs == [], f"生成した記事が article.schema.json に通らない: {errs[:3]}")
    body = text.split("\n---\n", 1)[1]
    check(body.count("<!-- F1 N1 -->") == 2, f"1 block 内の複数段落に根拠が付かない: {body!r}")
    h0 = pipelib.article_hash(p)
    for key, val in (("title_fact_ids", ["F2"]), ("lede_fact_ids", ["F2"]),
                     ("verified_facts", [{"id": "N1", "text": "別の事実", "url": "https://a.example/1"}])):
        fm2 = dict(fm, **{key: val})
        p.write_text("---\n" + compose.yaml_dump_keeping_strings(fm2) + "---\n" + body, encoding="utf-8")
        check(pipelib.article_hash(p) != h0, f"{key} を変えても指紋が変わらない")
    check(renderlib.date_mentioned("2026-09-13", "9/13開催") and not renderlib.date_mentioned("2026-09-01", "9/13開催"),
          "日付照合の境界(9/1 が 9/13 に当たる)")
    check(not renderlib.date_mentioned("2026-09-01", "9-13") and renderlib.date_mentioned("2026-09-01", "9月1日に"), "日付照合")
    # 年なし表記は許容年のときだけ(2099-09-13 を「9月13日」で通さない。監査指摘 R15-P1-1)
    check(not renderlib.date_mentioned("2099-09-13", "9月13日開催", {2026}), "年なし表記で別の年が通った")
    check(renderlib.date_mentioned("2026-09-13", "9月13日開催", {2026}) and renderlib.date_mentioned("2099-09-13", "2099年9月13日", {2026}),
          "年付き・許容年の表記が落ちた")
    # 別の年の年付き日付を、号の年の年なし日付として拾わない(監査指摘 R16-P0-1)
    for hay in ("2099年9月13日開催", "2099/9/13開催", "2099-9-13", "2099.9.13"):
        check(not renderlib.date_mentioned("2026-09-13", hay, {2026, 2099}), f"別年の年付き日付を拾った: {hay}")
    check(renderlib.date_mentioned("2026-09-13", "2099年9月13日と9月13日", {2026, 2099}), "年なし表記が別年表記の巻き添えで消えた")
    # 同年のゼロ埋め年付き表記は通り、対応する別年表記は根拠にならない(監査指摘 R17-P0-1)
    for hay in ("2026年09月13日", "2026/09/13", "2026.09.13", "2026-09-13", "2026年9月13日", "2026/9/13"):
        check(renderlib.date_mentioned("2026-09-13", hay, {2026}), f"同年の年付き表記が落ちた: {hay}")
        check(not renderlib.date_mentioned("2026-09-13", hay.replace("2026", "2099"), {2026, 2099}), f"別年の年付き表記を拾った: {hay}")
    # 区切りなしの8桁は日付にしない(商品コード・URL の一部。監査指摘 R18-P1-1)
    check(not renderlib.date_mentioned("2026-09-13", "商品コード20260913版", {2026}), "8桁数字を日付として拾った")
    mats8 = {"c1": {"id": "c1", "url": "https://a.example/1", "facts": ["商品コード20260920版"], "dedup_key": "k", "title": "t"}}
    out8 = {"reservations": [{"candidate_id": "c1", "date": "2026-09-20", "kind": "締切", "slug": "x", "subject": "s", "note": "n"}],
            "digest": [], "stories": [], "pending_add": [], "pending_remove": []}
    res8, _ = assemble.validate("2026-09-12", out8, [{"slug": "x", "brand": "765", "candidate_ids": ["c1"], "title": "t", "lede": "l", "rank": "small"}], mats8,
                                {"facts": [], "existing_story": {}, "tomorrow_reservations": [], "pending": [], "subjects": []})
    check(not res8.get("reservations"), "予約検証が8桁数字を根拠にした")
    for hay in ("2026/09/13開催", "2026年09月13日開催"):
        mats0 = {"c1": {"id": "c1", "url": "https://a.example/1", "facts": [hay.replace("13", "20")], "dedup_key": "k", "title": "t"}}
        out0 = {"reservations": [{"candidate_id": "c1", "date": "2026-09-20", "kind": "締切", "slug": "x", "subject": "s", "note": "n"}],
                "digest": [], "stories": [], "pending_add": [], "pending_remove": []}
        res0, _ = assemble.validate("2026-09-12", out0, [{"slug": "x", "brand": "765", "candidate_ids": ["c1"], "title": "t", "lede": "l", "rank": "small"}], mats0,
                                    {"facts": [], "existing_story": {}, "tomorrow_reservations": [], "pending": [], "subjects": []})
        check([r["date"] for r in res0.get("reservations", [])] == ["2026-09-20"], f"予約検証がゼロ埋め年付き表記を落とした: {hay}")
    mats_r = {"c1": {"id": "c1", "url": "https://a.example/1", "facts": ["2099年9月20日締切"], "dedup_key": "k", "title": "t", "published_date": "2026-09-01"}}
    out_r = {"reservations": [{"candidate_id": "c1", "date": "2026-09-20", "kind": "締切", "slug": "x", "subject": "s", "note": "n"}],
             "digest": [], "stories": [], "pending_add": [], "pending_remove": []}
    res_r, _ = assemble.validate("2026-09-12", out_r, [{"slug": "x", "brand": "765", "candidate_ids": ["c1"], "title": "t", "lede": "l", "rank": "small"}], mats_r,
                                 {"facts": [], "existing_story": {}, "tomorrow_reservations": [], "pending": [], "subjects": []})
    check(not res_r.get("reservations"), "素材が 2099 年の予約を 2026 年で通した")
    _, fb = renderlib.materials_with_ids(MATS)
    # 1 block = 1 段落。中見出しだけの block は根拠なしでよい
    check(any("複数段落" in p for p in renderlib.check_output(dict(OK, blocks=[{"markdown": "A\n\nB", "fact_ids": ["F1", "F3", "F4"]}]), fb, MATS)),
          "1 block 複数段落が通った")
    check(renderlib.check_output(dict(OK, blocks=[{"markdown": "## 会場", "fact_ids": []}] + OK["blocks"]), fb, MATS) == [],
          "中見出しだけの block が落ちた")
    check(any("根拠" in p for p in renderlib.check_output(dict(OK, blocks=[{"markdown": "本文", "fact_ids": []}] + OK["blocks"]), fb, MATS)),
          "根拠の無い段落が通った")
    # assemble の予約検証も同じ照合(年なし表記で 2099 年の予約は捨てる)
    mats = {"c1": {"id": "c1", "url": "https://a.example/1", "facts": ["9月20日締切"], "dedup_key": "k", "title": "t"}}
    out = {"reservations": [{"candidate_id": "c1", "date": "2099-09-20", "kind": "締切", "slug": "x", "subject": "s", "note": "n"},
                            {"candidate_id": "c1", "date": "2026-09-20", "kind": "締切", "slug": "x", "subject": "s", "note": "n"}],
           "digest": [], "stories": [], "pending_add": [], "pending_remove": []}
    try:
        res, notes = assemble.validate("2026-09-12", out, [{"slug": "x", "brand": "765", "candidate_ids": ["c1"], "title": "t", "lede": "l", "rank": "small"}], mats,
                                       {"facts": [], "existing_story": {}, "tomorrow_reservations": [], "pending": [], "subjects": []})
        dates = [r["date"] for r in res.get("reservations", [])]
        check("2099-09-20" not in dates and "2026-09-20" in dates, f"予約の年検証: {dates} / {notes[:3]}")
    except Exception as e:  # validate の入力契約が変わったら、この検査は別途書き直す
        check(False, f"assemble.validate を呼べない: {type(e).__name__}: {e}")


def test_earliest_date():
    E = compose.earliest_date
    check(E("2026-09-13〜2026-09-12") == "2026-09-12", "範囲の早い方を採らない")
    check(E("令和8年9月12日") == "2026-09-12" and E("令和元年5月1日") == "2019-05-01", "令和が読めない")
    check(E("2026-02-30") is None and E("２０２６／９／１") == "2026-09-01" and E("なし") is None, "暦・全角の扱い")


def test_revise_check():
    old_fm = {"title": "t", "lede": "l", "tags": ["a", "b"], "event_date": "2026-09-13",
              "sources": [{"url": u["url"]} for u in OK["sources"]]}
    old_body = "価格は三千円である。 <!-- F1 -->\n\n別の段落。 <!-- F3 -->"
    iss = [{"issue_id": "I1", "rule_id": "R1", "repair": "drop_claim", "quote": "価格は三千円である"}]
    R = lambda a, i=iss, b=old_body: compose.revise_check(a, i, old_fm, b)
    a = dict(OK, addressed_issue_ids=["I1"], blocks=[{"markdown": "価格は三千円である。", "fact_ids": ["F1"]},
                                                     {"markdown": "別の段落。", "fact_ids": ["F3"]}])
    # 指摘の記述を残す判断は執筆のもので、正しいかは次の巡の校閲(モデル)が判定する。機械は落とさない
    check(R(a) == [], f"記述を残した稿(校閲の判断)を機械が落とした: {R(a)}")
    a2 = dict(a, new_facts=[{"id": "N1", "text": "3000円", "url": "https://a.example/1"}],
              blocks=[{"markdown": "価格は三千円である。", "fact_ids": ["F1", "N1"]}, {"markdown": "別の段落。", "fact_ids": ["F3"]}])
    check(R(a2) == [], f"根拠を付けて残した稿が落ちた: {R(a2)}")
    a3 = dict(a, blocks=[{"markdown": "価格は改めて三千円と告知された。", "fact_ids": ["F1"]}, {"markdown": "別の段落。", "fact_ids": ["F3"]}])
    check(R(a3) == [], f"指摘の段落だけ直した稿が落ちた: {R(a3)}")
    a4 = dict(a3, blocks=[{"markdown": "価格は改めて三千円と告知された。", "fact_ids": ["F1"]}, {"markdown": "別の段落を書き換えた。", "fact_ids": ["F3"]}])
    check(any("段落" in p for p in R(a4)), "指摘に無い段落の改変が通った")
    a5 = dict(a3, blocks=[{"markdown": "価格は改めて三千円と告知された。", "fact_ids": ["F1"]}])
    check(any("消した" in p for p in R(a5)), "指摘に無い段落の削除が通った")
    a6 = dict(a3, blocks=a3["blocks"] + [{"markdown": "無関係な新段落。", "fact_ids": ["F4"]}])
    check(any("足した" in p for p in R(a6)), "指摘に無い段落の追加が通った")
    a7 = dict(a3, blocks=[a3["blocks"][0], {"markdown": "別の段落。", "fact_ids": ["F1"]}])
    check(any("根拠 id" in p for p in R(a7)), "未指摘段落の根拠 id 改変が通った")
    old_body3 = old_body + "\n\n三つ目。 <!-- F4 -->"
    a8 = dict(a3, blocks=[a3["blocks"][0], {"markdown": "三つ目。", "fact_ids": ["F4"]}, {"markdown": "別の段落。", "fact_ids": ["F3"]}])
    check(any("順序" in p for p in R(a8, b=old_body3)), "段落の並べ替えが通った")
    check(R(dict(a3, blocks=a3["blocks"] + [{"markdown": "三つ目。", "fact_ids": ["F4"]}]), b=old_body3) == [], "順序どおりの稿が落ちた")
    # 同じ未指摘段落が2つ: 片方を消す / 1つ足す(複製)はどちらも拒否、そのままなら合格(監査指摘 R12-P1-1)
    old_dup = old_body + "\n\n別の段落。 <!-- F3 -->"
    dup_ok = dict(a3, blocks=[a3["blocks"][0], {"markdown": "別の段落。", "fact_ids": ["F3"]}, {"markdown": "別の段落。", "fact_ids": ["F3"]}])
    check(R(dup_ok, b=old_dup) == [], f"同一段落2つをそのまま残した稿が落ちた: {R(dup_ok, b=old_dup)}")
    check(any("消した" in p for p in R(a3, b=old_dup)), "同一段落の片方の削除が通った")
    dup_add = dict(dup_ok, blocks=dup_ok["blocks"] + [{"markdown": "別の段落。", "fact_ids": ["F3"]}])
    check(any("足した" in p for p in R(dup_add, b=old_dup)), "未指摘段落の複製が通った")
    iss_tag = [{"issue_id": "I1", "rule_id": "R8", "repair": "rewrite_claim", "quote": "価格は三千円である", "issue": "タグ「b」が語彙に無い"}]
    check(R(dict(a3, tags=["a", "c"]), iss_tag) == [], "指摘されたタグの修正が落ちた")
    check(any("見出し" in p for p in R(dict(a3, title="別"))), "見出しの改変が通った")
    check(any("リード" in p for p in R(dict(a3, lede="別"))), "リードの改変が通った")
    check(any("tags" in p for p in R(dict(a3, tags=["a", "c"]))), "tags の改変が通った")
    check(any("対応していない" in p for p in R(dict(a3, addressed_issue_ids=[]))), "未対応が通った")
    check(any("知らない" in p for p in R(dict(a3, addressed_issue_ids=["I9"]))), "知らない id が通った")
    check(any("出典を変えた" in p for p in R(dict(a3, sources=OK["sources"][:2]))), "出典の無断変更が通った")
    iss_add = [{"issue_id": "I1", "rule_id": "R2", "repair": "add_source", "quote": ""}]
    check(any("add_source" in p for p in R(dict(a3, addressed_issue_ids=["I1"], sources=OK["sources"][:2]), iss_add)),
          "add_source で出典を外したのが通った")
    iss_lint = [{"issue_id": "I1", "rule_id": "LINT", "repair": "rewrite_claim", "quote": ""}]
    check(R(dict(a4, addressed_issue_ids=["I1"], title="x"), iss_lint) == [], "LINT 修正に外の検査が掛かった")


def test_rollback(tmp: Path):
    """既存話題から事実だけ剥がす場合もディスクへ保存する(監査指摘 R10-P0-1)。"""
    st = tmp / "stock"; (st / "scheduled").mkdir(parents=True)
    (tmp / "metrics").mkdir()
    stories = [{"story_id": "s1", "first_published": "2026-09-01", "published_facts": ["旧", "新"],
                "edition_facts": {"2026-09-01": ["旧"], "2026-09-12": ["新"]}}]
    assemble.dump_yaml(st / "stories.yml", stories)
    (st / "scheduled" / "2026-09-20.json").write_text(json.dumps([{"id": "a", "reserved_on": "2026-09-12"}, {"id": "b", "reserved_on": "2026-09-01"}]), encoding="utf-8")
    assemble.dump_yaml(st / "pending.yml", [{"dedup_key": "after"}])
    assemble.dump_yaml(tmp / "metrics" / "pending-before-2026-09-12.yml", [{"dedup_key": "before"}])
    saved = (assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING)
    try:
        assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING = tmp, st / "stories.yml", st / "scheduled", st / "pending.yml"
        log = assemble.rollback("2026-09-12")
        after = assemble.load_yaml_list(st / "stories.yml")
        check(after[0]["published_facts"] == ["旧"] and "2026-09-12" not in after[0].get("edition_facts", {}),
              f"台帳の剥がしが保存されない: {after}")
        rows = json.loads((st / "scheduled" / "2026-09-20.json").read_text(encoding="utf-8"))
        check([r["id"] for r in rows] == ["b"], "予約の剥がしが保存されない")
        check(assemble.load_yaml_list(st / "pending.yml") == [{"dedup_key": "before"}], "pending が控えに戻らない")
        check(assemble.rollback("2026-09-12") == [], "rollback が冪等でない")
    finally:
        assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING = saved


def test_job_lock():
    code = ("import sys; sys.path.insert(0, %r)\nimport pipelib\n"
            "try:\n    pipelib.job_lock('t', wait_min=0); print('GOT')\n"
            "except pipelib.JobLockTimeout: print('TIMEOUT')") % str(Path(__file__).resolve().parent)
    fd = pipelib.job_lock("t")
    check(fd is not None, "job_lock が取れない")
    run = lambda env=None: subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env).stdout.strip()
    check(run() == "TIMEOUT", "他プロセスが持っているのに取れた")
    check(run({**os.environ, "IMAS_JOB_LOCK": "held"}) == "GOT", "held の子が待ってしまう")
    os.close(fd)
    check(run() == "GOT", "離したのに取れない")


def test_notify_require():
    pipelib.set_quiet(True)
    check(pipelib.notify("t", "x") is True, "通常通知が試験実行で False")
    check(pipelib.notify("t", "x", require=True) is False, "必須通知が届いていないのに True")
    pipelib.set_quiet(False)
    saved = pipelib.ENV.get("DISCORD_WEBHOOK_URL")
    try:
        pipelib.ENV.pop("DISCORD_WEBHOOK_URL", None)
        check(pipelib.notify("t", "x") is True, "webhook 未設定の通常通知が False")
        check(pipelib.notify("t", "x", require=True) is False, "webhook 未設定の必須通知が True(監査指摘 R10-P0-4)")
    finally:
        if saved is not None:
            pipelib.ENV["DISCORD_WEBHOOK_URL"] = saved


def test_oncall_rerun_policy():
    import oncall
    check(oncall.needs_full_rerun(["scripts/renderlib.py"]) and not oncall.needs_full_rerun(["scripts/collect.py"]),
          "作り直し判定")
    # release 起点でも、生成層を直したか rebuild と答えたら作り直し(監査指摘 R10-P0-2)
    for stage in ("compose", "release"):
        check(oncall.rerun_policy(stage, ["scripts/assemble.py"], "resume")[0], f"{stage}: 生成層の修正で作り直しにならない")
        check(oncall.rerun_policy(stage, ["scripts/collect.py"], "rebuild")[0], f"{stage}: rebuild 指定で作り直しにならない")
        check(not oncall.rerun_policy(stage, ["scripts/collect.py"], "resume")[0], f"{stage}: 続きのはずが作り直し")
        check(oncall.rerun_policy(stage, ["scripts/assemble.py"], "none") == (False, "none"), f"{stage}: none が効かない")
    # release 起点の作り直しは compose 先頭 → release の順に走る
    calls = []
    saved = (oncall.reset_edition, oncall.run_stage, oncall.commit_paths, oncall.ROOT)
    tmp = Path(tempfile.mkdtemp()); (tmp / "metrics").mkdir()
    try:
        oncall.ROOT = tmp
        oncall.reset_edition = lambda d, e: calls.append("reset") or "backup/x"
        oncall.run_stage = lambda cmd, log, t: calls.append(Path(cmd[1]).name + ("(reuse)" if "--reuse-plan" in cmd else "")) or 0
        oncall.commit_paths = lambda *a, **k: calls.append("commit")
        code = oncall.rerun_stage("release", "2026-09-12", "edition/2026-09-12", full=True)
        check(code == 0 and calls == ["reset", "compose.py", "release.py"], f"release 起点 full の順序: {calls}")
        calls.clear()
        oncall.rerun_stage("compose", "2026-09-12", "edition/2026-09-12", full=False)
        check(calls == ["commit", "compose.py(reuse)"], f"compose 続きの順序: {calls}")
        calls.clear()
        oncall.rerun_stage("release", "2026-09-12", "edition/2026-09-12", full=False)
        check(calls == ["release.py"], f"release 続きの順序: {calls}")
    finally:
        oncall.reset_edition, oncall.run_stage, oncall.commit_paths, oncall.ROOT = saved


def test_oncall_report_text(tmp: Path):
    """修正報告の戻し方が、実際に作る merge commit を指す(監査指摘 R15-P1-3)。Git は使わず sh を差し替える。"""
    import oncall

    class R:
        returncode, stdout, stderr = 0, "diff --stat\n+added\n", ""
    saved = (oncall.sh, oncall.ROOT, oncall.notify)
    try:
        oncall.ROOT = tmp
        (tmp / "metrics").mkdir(parents=True, exist_ok=True)
        oncall.sh = lambda args, cwd, timeout=600: R()
        sent = []
        oncall.notify = lambda job, msg, ok=True, require=False: sent.append((msg, require)) or True
        ok = oncall.report_change("compose", "2026-09-12", {"diagnosis": "d" * 3000, "test_evidence": "t"}, [], "base", "headhash00", "mergehash00",
                                  "repair/x", ["main", "edition/2026-09-12"], "続き")
        text = (tmp / "metrics" / "oncall-2026-09-12-compose-report.md").read_text(encoding="utf-8")
        check(ok and "git revert -m 1 mergehash0" in text and "headhash00"[:10] in text, f"報告の戻し方が merge commit を指さない: {text[:300]}")
        check(sent and all(req for _, req in sent), "修正報告が必須通知(require)で送られていない")
        # Discord へ送った本文をつなぐと、記録した本文(往復の記録を除く)と一致する(切り詰めない。監査指摘 R23-P1-2)
        import re as _re
        joined = "".join(_re.sub(r"^\(\d+/\d+\) ", "", m) for m, _ in sent)
        check(joined == text.split("\n## 往復の記録\n")[0], "Discord に送った本文が記録と一致しない(切り詰めている)")
        check(all(len(m) <= 1900 for m, _ in sent), "Discord の上限を超える塊がある")
        # 取り込み前の全文報告(merge commit はまだ無い)は「追送」と明示し、診断・検証を含む
        sent.clear()
        ok2 = oncall.report_change("compose", "2026-09-12", {"diagnosis": "d", "test_evidence": "t"}, [], "base", "headhash00", "",
                                   "repair/x", ["main", "edition/2026-09-12"], "続き")
        text2 = (tmp / "metrics" / "oncall-2026-09-12-compose-report.md").read_text(encoding="utf-8")
        check(ok2 and "取り込み後に追送" in text2 and "診断: d" in text2 and "検証: t" in text2, f"取り込み前の報告が不完全: {text2[:300]}")
        # 届かなければ False(取り込みの前提)
        oncall.notify = lambda job, msg, ok=True, require=False: False
        check(oncall.report_change("compose", "2026-09-12", {"diagnosis": "d"}, [], "base", "h", "", "b", ["main"], "続き") is False,
              "報告が届かないのに True")
    finally:
        oncall.sh, oncall.ROOT, oncall.notify = saved


def test_oncall_rollback_subprocess(tmp: Path):
    """rollback は取り込んだあとの scripts/assemble.py を別プロセスで走らせる(監査指摘 R16-P0-2)。"""
    import oncall
    (tmp / "scripts").mkdir(parents=True)
    (tmp / "scripts" / "assemble.py").write_text(
        "def rollback(date):\n    open('marker.txt','w').write('NEW ' + date)\n    return ['新しい実装が走った']\n", encoding="utf-8")
    saved = oncall.ROOT
    try:
        oncall.ROOT = tmp
        lines = oncall.rollback_in_subprocess("2026-09-12")
        check(lines == ["新しい実装が走った"] and (tmp / "marker.txt").read_text() == "NEW 2026-09-12",
              f"取り込み後のコードで rollback が走らない: {lines}")
    finally:
        oncall.ROOT = saved


def test_oncall_apply_integrate():
    """監査往復で no_fix_needed → fixed、fixed → no_fix_needed に収束できる(監査指摘 R16-P1-2)。"""
    import oncall
    fix = {"status": "no_fix_needed", "rerun_mode": "resume", "diagnosis": "d0"}
    oncall.apply_integrate(fix, {"status": "fixed", "rerun_mode": "rebuild", "diagnosis": "d1", "root_cause": "", "recovery": ""})
    check(fix["status"] == "fixed" and fix["rerun_mode"] == "rebuild" and fix["diagnosis"] == "d1", f"no_fix_needed→fixed: {fix}")
    oncall.apply_integrate(fix, {"status": "no_fix_needed", "rerun_mode": "unchanged", "diagnosis": "", "root_cause": "", "recovery": "枠が戻った"})
    check(fix["status"] == "no_fix_needed" and fix["rerun_mode"] == "rebuild" and fix["recovery"] == "枠が戻った", f"fixed→no_fix_needed: {fix}")
    oncall.apply_integrate(fix, {"status": "unchanged", "rerun_mode": "unchanged"})
    check(fix["status"] == "no_fix_needed" and fix["rerun_mode"] == "rebuild", "unchanged が値を変えた")
    # 2巡目の変更・検証・リスクは最終報告に合流する(監査指摘 R18-P1-2)
    fix2 = {"status": "fixed", "changed_files": ["scripts/a.py"], "test_evidence": "初稿の検証", "risk": "r0"}
    oncall.apply_integrate(fix2, {"status": "unchanged", "rerun_mode": "unchanged", "changed_files": ["scripts/b.py"],
                                  "test_evidence": "2巡目の検証", "risk": "r1"})
    check(fix2["changed_files"] == ["scripts/a.py", "scripts/b.py"] and "2巡目の検証" in fix2["test_evidence"]
          and "初稿の検証" in fix2["test_evidence"] and "r0" in fix2["risk"] and "r1" in fix2["risk"],
          f"2巡目の内容が報告に合流しない(初稿の risk を消してはいけない): {fix2}")
    oncall.apply_integrate(fix2, {"status": "unchanged", "rerun_mode": "unchanged", "risk": "r1"})
    check(fix2["risk"].count("r1") == 1, "同じ risk が二重に積まれた")
    # 包含関係にある別の risk は両方残る。完全一致だけが重複(監査指摘 R20-P1-1)
    fix3 = {"status": "fixed", "risk": "重大な情報漏えい"}
    oncall.apply_integrate(fix3, {"status": "unchanged", "rerun_mode": "unchanged", "risk": "情報漏えい"})
    check(fix3["risk"].splitlines() == ["重大な情報漏えい", "[監査後の変更で] 情報漏えい"], f"部分一致で別の risk を捨てた: {fix3}")
    oncall.apply_integrate(fix3, {"status": "unchanged", "rerun_mode": "unchanged", "risk": "情報漏えい"})
    check(len(fix3["risk"].splitlines()) == 2, "完全一致の risk が重複した")
    # status と差分の整合: fixed ⇔ 回帰テスト以外を変えた(テストだけ足した no_fix_needed は許す。配管テスト 2026-09-12)
    check(oncall.status_consistent("fixed", ["scripts/compose.py"]) and oncall.status_consistent("no_fix_needed", [])
          and oncall.status_consistent("no_fix_needed", ["scripts/test_pipeline.py"])
          and not oncall.status_consistent("no_fix_needed", ["scripts/compose.py"])
          and not oncall.status_consistent("fixed", []) and not oncall.status_consistent("fixed", ["scripts/test_pipeline.py"]),
          "status と差分の整合判定")
    # 複数行の risk を2回渡しても各項目は1回だけ(監査指摘 R21-P1-1)
    fix4 = {"status": "fixed", "risk": "重大な情報漏えい"}
    for _ in range(2):
        oncall.apply_integrate(fix4, {"status": "unchanged", "rerun_mode": "unchanged", "risk": "情報漏えい\n予約消失"})
    check(fix4["risk"].splitlines() == ["重大な情報漏えい", "[監査後の変更で] 情報漏えい", "[監査後の変更で] 予約消失"],
          f"複数行の risk の重複除去: {fix4}")


def test_revise_apply_decline(tmp: Path):
    """書き直しの decline は理由付きだけが記事を消す。理由なしは元の稿を残す(監査指摘 R21-P1-2)。"""
    tmp.mkdir(parents=True, exist_ok=True)
    _, fb = renderlib.materials_with_ids(MATS)
    art = {"slug": "x", "brand": "765", "candidate_ids": ["c1"], "rank": "small"}
    p = tmp / "2026-09-12-x.md"
    p.write_text("---\ntitle: t\n---\n本文\n", encoding="utf-8")
    bad = {"status": "decline", "decline_code": "", "decline_detail": "", "new_facts": []}
    outcome, msg = compose.revise_apply("2026-09-12", art, p, bad, fb, MATS, [])
    check(outcome == "kept" and p.exists(), f"理由なし decline で記事が消えた: {outcome} {msg}")
    good = {"status": "decline", "decline_code": "NOT_NEWS", "decline_detail": "既報", "new_facts": []}
    outcome, msg = compose.revise_apply("2026-09-12", art, p, good, fb, MATS, [])
    check(outcome == "dropped" and not p.exists(), f"理由付き decline で記事が消えない: {outcome} {msg}")
    # 形の検算: HTML コメント・タグ・文字参照・不可視文字・参照リンクを書いた稿は差し戻し(元稿を残す)
    old = ("---\n" + compose.yaml_dump_keeping_strings({"title": "t", "lede": "l", "tags": ["a", "b"], "event_date": "2026-09-13",
                                                         "sources": [{"url": u["url"], "label": "x", "type": "公式"} for u in OK["sources"]]})
           + "---\n価格は三千円である。 <!-- F1 -->\n")
    iss = [{"issue_id": "I1", "rule_id": "R1", "repair": "drop_claim", "quote": "価格は三千円である"}]
    for md in ("価格は<!-- F1 -->三千円である。", "価格は<span>三千円</span>である。", "価格は三​千円である。",
               "価格は三&#21315;円である。", "価格は三͏千円である。", "価格は三ᅟ千円である。",
               "[p]: https://a.example/1\n価格は[三千円][p]である。"):
        p.write_text(old, encoding="utf-8")
        ans = dict(OK, addressed_issue_ids=["I1"], blocks=[{"markdown": md, "fact_ids": ["F1"]}])
        outcome, msg = compose.revise_apply("2026-09-12", art, p, ans, fb, MATS, iss)
        check(outcome == "kept" and p.read_text(encoding="utf-8") == old, f"形の崩れた稿が通った: {md!r} → {outcome} {msg}")
    # 指摘の記述を(根拠付きで)残した稿は形が正しければ通す。正しいかは校閲が見る
    p.write_text(old, encoding="utf-8")
    ans = dict(OK, addressed_issue_ids=["I1"], blocks=[{"markdown": "価格は三千円である。", "fact_ids": ["F1"]}])
    outcome, msg = compose.revise_apply("2026-09-12", art, p, ans, fb, MATS, iss)
    check(outcome == "fixed", f"記述を残す判断を機械が落とした: {outcome} {msg}")
    check(any("HTML コメント" in p_ for p_ in renderlib.check_output(dict(OK, blocks=[{"markdown": "x<!-- F1 -->", "fact_ids": ["F1", "F3", "F4"]}]), fb, MATS)),
          "本文の HTML コメントが通った")


def test_oncall_state(tmp: Path):
    """試行状態は原子的に保存し、壊れていても当番の入口を塞がない(監査指摘 R24-P1-1)。"""
    import oncall
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / "oncall-2026-09-12-compose.json"
    oncall.save_state(p, {"log": [{"a": 1}]})
    check(json.loads(p.read_text(encoding="utf-8")) == {"log": [{"a": 1}]} and not (tmp / (p.name + ".tmp")).exists(),
          "原子的保存の結果が正しくない")
    check(oncall.load_state(p) == {"log": [{"a": 1}]}, "保存した state が読めない")
    saved = (oncall.notify, oncall.ROOT)
    try:
        sent = []
        oncall.notify = lambda job, msg, ok=True, require=False: sent.append(msg) or True
        p.write_text("{\"lo", encoding="utf-8")   # 途中で死んだ書き込み
        st = oncall.load_state(p)
        p.write_text("[1, 2]", encoding="utf-8")   # 形が違う(同じ瞬間に2つ目の破損)
        st2 = oncall.load_state(p)
        corrupts = sorted(f for f in tmp.iterdir() if f.name.startswith(p.name + ".corrupt-"))
        check(st == {"log": [{"corrupted": corrupts[0].name}]} if corrupts else False, f"壊れた state の扱い: {st}")
        check(len(corrupts) == 2 and {f.read_text(encoding="utf-8") for f in corrupts} == {"{\"lo", "[1, 2]"},
              f"退避が上書きされた・欠けた: {[f.name for f in corrupts]}")
        check(st2 is not None and not p.exists() and len(sent) == 2, "2つ目の破損の扱い")
        check(oncall.load_state(tmp / "none.json") == {"log": []}, "無い state")
        # 退避できなければ None(証拠を失ったまま起動しない)
        p.write_text("{", encoding="utf-8")
        saved_link = oncall.os.link
        try:
            def fail_link(*a, **k):
                raise OSError("no link")
            oncall.os.link = fail_link
            check(oncall.load_state(p) is None and p.exists(), "退避に失敗したのに起動を許した")
        finally:
            oncall.os.link = saved_link
        # 試行回数は印ファイルの個数。JSON が壊れても・消えても緩まない(監査指摘 R25-P0-1)
        oncall.ROOT = tmp
        (tmp / "metrics").mkdir(exist_ok=True)
        check(oncall.attempt_count("2026-09-12", "compose") == 0, "印なしで 0 でない")
        check(oncall.consume_attempt("2026-09-12", "compose") == 1 and oncall.consume_attempt("2026-09-12", "compose") == 2, "印の消費")
        check(oncall.attempt_count("2026-09-12", "compose") == 2 >= oncall.MAX_ATTEMPTS, "2回で上限に達しない")
        (tmp / "metrics" / "oncall-2026-09-12-compose.json").write_text("{", encoding="utf-8")
        check(oncall.attempt_count("2026-09-12", "compose") == 2, "JSON の破損で回数が緩んだ")
        # 印の競合(同じ番号を2つ作れない)
        (tmp / "metrics" / "oncall-2026-09-12-compose-attempt-3").unlink(missing_ok=True)
        fd = oncall.os.open(tmp / "metrics" / "oncall-2026-09-12-compose-attempt-3", oncall.os.O_WRONLY | oncall.os.O_CREAT | oncall.os.O_EXCL)
        oncall.os.close(fd)
        try:
            oncall.consume_attempt("2026-09-12", "compose")
            check(oncall.attempt_count("2026-09-12", "compose") == 4, "印の番号が飛んだ")
        except FileExistsError:
            check(False, "既存の印の次を作れない")
    finally:
        oncall.notify, oncall.ROOT = saved


def test_notify_long():
    """分割はつなぐと元に戻る(長い1行も)。途中の塊が落ちたら False(監査指摘 R23-P1-2)。"""
    import oncall
    text = "短い行\n" + "x" * 5000 + "\n最後\n"
    chunks = oncall.split_chunks(text, 1800)
    check("".join(chunks) == text and all(len(c) <= 1800 for c in chunks) and len(chunks) >= 3, "split_chunks が元に戻らない")
    saved = oncall.notify
    try:
        results = iter([True, False, True])
        oncall.notify = lambda job, msg, ok=True, require=False: next(results, True)
        check(oncall.notify_long("t", text) is False, "途中の塊が落ちたのに True")
        oncall.notify = lambda job, msg, ok=True, require=False: True
        check(oncall.notify_long("t", text) is True, "全部届いたのに False")
    finally:
        oncall.notify = saved


def test_oncall_restore_cleans_untracked():
    """控えへ戻すとき、この号の未追跡の生成物だけを消し、clean でなければ失敗にする(監査指摘 R30-P1-2)。"""
    import oncall
    calls = []

    class R:
        def __init__(self, rc=0, out=""): self.returncode, self.stdout, self.stderr = rc, out, ""
    state = {"dirty": "?? docs/_posts/2026-09-12-x.md\n"}

    def fake_sh(args, cwd, timeout=600):
        calls.append(args)
        if args[:2] == ["git", "clean"]:
            state["dirty"] = ""
        if args[:2] == ["git", "status"]:
            return R(0, state["dirty"])
        return R(0)
    saved = oncall.sh
    try:
        oncall.sh = fake_sh
        oncall.restore_edition("2026-09-12", "edition/2026-09-12", "backup/x")
        clean = [c for c in calls if c[:2] == ["git", "clean"]]
        check(clean and "-f" in clean[0] and "docs/_posts/2026-09-12-*.md" in clean[0] and "-d" not in clean[0] and "-x" not in clean[0],
              f"生成物の掃除がこの号に限定されていない: {clean}")
        check(clean and "docs/_editorials/2026-09-12.md" in clean[0], "社説が掃除の対象に無い(監査指摘 R31-P1-2)")
        check(any(c[:2] == ["git", "push"] for c in calls), "掃除後に push していない")
        # git status 自体が失敗したら clean と見なさず push しない(監査指摘 R31-P0-1)
        calls.clear()
        def fake_sh3(args, cwd, timeout=600):
            calls.append(args)
            return R(128, "") if args[:2] == ["git", "status"] else R(0)
        oncall.sh = fake_sh3
        try:
            oncall.restore_edition("2026-09-12", "edition/2026-09-12", "backup/x")
            check(False, "status 失敗なのに復元が成功扱い")
        except RuntimeError:
            check(not any(c[:2] == ["git", "push"] for c in calls), "status 失敗なのに push した")
        # 掃除しても clean にならなければ push せず失敗
        calls.clear()
        state["dirty"] = " M stock/stories.yml\n"
        def fake_sh2(args, cwd, timeout=600):
            calls.append(args)
            return R(0, state["dirty"]) if args[:2] == ["git", "status"] else R(0)
        oncall.sh = fake_sh2
        try:
            oncall.restore_edition("2026-09-12", "edition/2026-09-12", "backup/x")
            check(False, "clean でないのに復元が成功扱い")
        except RuntimeError:
            check(not any(c[:2] == ["git", "push"] for c in calls), "clean でないのに push した")
    finally:
        oncall.sh = saved


def test_classify_consensus(tmp: Path):
    """合議: 不明は棄権(具体的な答えを採る)、公式・準公式は一致が要る、X の節は x_accounts の中に足す。"""
    import classify_sources as cs
    saved = (cs.ask, cs.ROOT)
    calls = []
    # 1巡目: A(claude)と B(codex)が独立に答える。割れたら2巡目で相手の根拠を見て答え直す
    r1a = [{"host": "yuzu_yng", "type": "不明"}, {"host": "onkyodav", "type": "不明"}, {"host": "idolmaster_en", "type": "公式", "why": "英語公式"},
           {"host": "jimushiny_oa", "type": "公式", "why": "作品公式"}, {"host": "x1", "type": "ファン", "why": "感想"}, {"host": "x2", "type": "報道"}]
    r1b = [{"host": "yuzu_yng", "type": "ファン", "why": "目撃投稿"}, {"host": "onkyodav", "type": "当事者", "why": "メーカー"}, {"host": "idolmaster_en", "type": "公式"},
           {"host": "jimushiny_oa", "type": "不明"}, {"host": "x1", "type": "当事者", "why": "店舗"}, {"host": "x2", "type": "報道"}]
    # 2巡目: A は相手の根拠を検証して yuzu/onkyo に賛成、jimushiny は公式を維持。x1 は双方譲らず
    r2a = [{"host": "yuzu_yng", "type": "ファン", "why": "相手の根拠どおり個人の目撃"}, {"host": "onkyodav", "type": "当事者", "why": "メーカー"},
           {"host": "jimushiny_oa", "type": "公式", "why": "作品公式"}, {"host": "x1", "type": "ファン", "why": "店舗の根拠が無い"}]
    r2b = [{"host": "yuzu_yng", "type": "ファン"}, {"host": "onkyodav", "type": "当事者"},
           {"host": "jimushiny_oa", "type": "公式", "why": "作品名を名乗る告知"}, {"host": "x1", "type": "当事者", "why": "店舗名"}]
    answers = iter([r1a, r1b, r2a, r2b])

    def fake_ask(cmd, prompt):
        calls.append((cmd[0], prompt))
        return next(answers)
    try:
        cs.ask = fake_ask
        agreed, split = cs.consensus("p", ["yuzu_yng", "onkyodav", "idolmaster_en", "jimushiny_oa", "x1", "x2"])
        check(len(calls) == 4 and calls[2][0] == "claude" and calls[3][0] == "codex", f"2巡目が両モデルに掛かっていない: {[c[0] for c in calls]}")
        check("相手=ファン(目撃投稿)" in calls[2][1] and "あなた=不明" in calls[2][1], "2巡目の prompt に相手の答えと根拠が無い")
        check("x2" not in calls[2][1].split("2巡目")[1], "一致した対象まで議論させている")
        check(agreed.get("yuzu_yng", ("",))[0] == "ファン" and agreed.get("onkyodav", ("",))[0] == "当事者",
              f"議論で一致した答えが採られない: {agreed} {split}")
        check(agreed.get("idolmaster_en", ("",))[0] == "公式" and agreed.get("jimushiny_oa", ("",))[0] == "公式", f"公式が採られない: {agreed}")
        check("x1" not in agreed and any(s.startswith("x1") and "店舗の根拠が無い" in s and "店舗名" in s for s in split),
              f"議論後も割れたものは両方の言い分を付けて人へ: {split}")
        check(agreed.get("x2", ("",))[0] == "報道", "1巡目で一致したものが消えた")
        # 1巡目で全部一致なら2巡目は掛からない
        calls.clear()
        answers = iter([[{"host": "h", "type": "報道"}], [{"host": "h", "type": "報道"}]])
        check(cs.consensus("p", ["h"])[0].get("h", ("",))[0] == "報道" and len(calls) == 2, "一致しているのに2巡目を掛けた")
        # x_accounts の節に足す(video_channels の「公式:」に差し込まない)
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "source_types.yml").write_text(
            "video_channels:\n  公式:\n    - imas-official\nx_accounts:\n  公式:\n    - imas_official\n  ファン:\n    - a\n", encoding="utf-8")
        cs.ROOT = tmp
        cs.add_x_accounts({"idolmaster_en": ("公式", "英語公式"), "newfan": ("ファン", "f"), "actor": ("演者", "e")})
        import yaml
        t = yaml.safe_load((tmp / "source_types.yml").read_text(encoding="utf-8"))
        check(t["video_channels"]["公式"] == ["imas-official"], f"動画チャンネルの節に混入した: {t}")
        check("idolmaster_en" in t["x_accounts"]["公式"] and "newfan" in t["x_accounts"]["ファン"] and t["x_accounts"].get("演者") == ["actor"],
              f"x_accounts への追加: {t}")
    finally:
        cs.ask, cs.ROOT = saved


def test_oncall_undo_merge():
    """merge --abort が失敗しても、merge 前のハッシュへ reset --hard する(監査指摘 R13-P0-1)。"""
    import oncall
    calls = []

    class R:
        def __init__(self, rc): self.returncode, self.stdout, self.stderr = rc, "", "boom"

    def fake_sh(args, cwd, timeout=600):
        calls.append(args)
        return R(1 if args[:2] == ["git", "merge"] else 0)
    saved = oncall.sh
    try:
        oncall.sh = fake_sh
        oncall.undo_merge("abc123", "main")
        check(["git", "reset", "-q", "--hard", "abc123"] in calls, f"abort 失敗後に reset --hard が呼ばれない: {calls}")
        calls.clear()
        oncall.sh = lambda args, cwd, timeout=600: (calls.append(args) or R(1))
        try:
            oncall.undo_merge("abc123", "main")
            check(False, "reset --hard の失敗が握り潰された")
        except RuntimeError:
            pass
    finally:
        oncall.sh = saved


def test_oncall_restore_on_exception(tmp: Path):
    """控えを作ったあとに例外が出ても控えへ戻す(監査指摘 R10-P0-3)。Git は使わず関数を差し替える。"""
    import oncall
    calls = []
    saved = (oncall.reset_edition, oncall.run_stage, oncall.restore_edition, oncall.notify, oncall.ROOT)
    try:
        oncall.ROOT = tmp
        (tmp / "metrics").mkdir(parents=True, exist_ok=True)
        oncall.reset_edition = lambda d, e: "backup/x"
        def boom(*a, **k):
            raise OSError("popen failed")
        oncall.run_stage = boom
        oncall.restore_edition = lambda d, e, b: calls.append(("restore", b))
        oncall.notify = lambda *a, **k: True
        try:
            oncall.rerun_stage("compose", "2026-09-12", "edition/2026-09-12", full=True)
        except OSError:
            pass
        check(calls == [("restore", "backup/x")], f"例外時に控えへ戻さない: {calls}")
        # compose は成功、続く release の起動で例外 → それでも控えへ戻す(監査指摘 R30-P0-1)
        calls.clear()
        def compose_ok_release_boom(cmd, log, t):
            if cmd[1].endswith("release.py"):
                raise OSError("popen failed")
            return 0
        oncall.run_stage = compose_ok_release_boom
        try:
            oncall.rerun_stage("release", "2026-09-12", "edition/2026-09-12", full=True)
        except OSError:
            pass
        check(calls == [("restore", "backup/x")], f"release 起動の例外で控えへ戻さない: {calls}")
        # 全部成功なら戻さない
        calls.clear()
        oncall.run_stage = lambda cmd, log, t: 0
        check(oncall.rerun_stage("release", "2026-09-12", "edition/2026-09-12", full=True) == 0 and calls == [],
              f"成功したのに控えへ戻した: {calls}")
    finally:
        oncall.reset_edition, oncall.run_stage, oncall.restore_edition, oncall.notify, oncall.ROOT = saved


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="imas-test-"))
    test_check_output()
    test_render_and_length()
    test_earliest_date()
    test_schema_hash_dates()
    test_revise_check()
    test_rollback(tmp / "rb")
    test_job_lock()
    test_notify_require()
    test_oncall_rerun_policy()
    test_revise_apply_decline(tmp / "ra")
    test_notify_long()
    test_oncall_state(tmp / "st")
    test_oncall_restore_cleans_untracked()
    test_classify_consensus(tmp / "cs")
    test_oncall_undo_merge()
    test_oncall_rollback_subprocess(tmp / "rs")
    test_oncall_apply_integrate()
    test_oncall_report_text(tmp / "rp")
    test_oncall_restore_on_exception(tmp / "oc")
    for f in FAILS:
        print(f"  [FAIL] {f}")
    print(f"test_pipeline: {len(FAILS)} failures")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
