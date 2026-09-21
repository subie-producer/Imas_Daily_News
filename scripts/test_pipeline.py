#!/usr/bin/env python3
"""回帰テスト(selfcheck から呼ばれる。単体でも走る): 監査で見つかった欠陥を二度と通さない。

  python3 scripts/test_pipeline.py

外部サービス・本体の作業ツリー・Git には触らない。全部一時ディレクトリの中で済ませる。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assemble
import compose
import lint
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
    # roundup・culture は、確かめて残った素材が1件でも載せる(編集長の決定 2026-09-18〜19。件数の下限は計画が持つ)
    one = dict(OK, blocks=[{"markdown": "x", "fact_ids": ["F1"]}], sources=OK["sources"][:1])
    for rank in ("roundup", "culture"):
        check(C(one, rank=rank) == [], f"素材1件の {rank} を落とした: {C(one, rank=rank)}")
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
    # label の制御文字・不可視文字は差し戻さず、書き出しで取り除く(出典ページの題名のゼロ幅空白を執筆が写す。2026-09-19)
    check(C(dict(OK, sources=OK["sources"][:2] + [{"url": "https://c.example/3", "label": "お​知らせ ~ for\x044"}])) == [], "label の不可視文字で差し戻した")
    check(renderlib.clean_label("お​知らせ\n ~  for\x044  ") == "お知らせ ~ for4", f"clean_label: {renderlib.clean_label('お​知らせ ~ for\x044')!r}")
    # 検査は取り除いたあとの値で(不可視文字で割った Markdown 記号・空になる label を通さない。監査指摘)
    check(any("Markdown" in p for p in C(dict(OK, sources=OK["sources"][:2] + [{"url": "https://c.example/3", "label": "[公式]​(https://evil)"}]))),
          "不可視文字で割った Markdown リンクの label が通った")
    check(any("空" in p for p in C(dict(OK, sources=OK["sources"][:2] + [{"url": "https://c.example/3", "label": "​\x04"}]))), "空になる label が通った")
    check(any("制御文字" in p for p in C(dict(OK, title="見出し\x04"))), "見出しの制御文字が通った")
    check(C(dict(OK, blocks=[{"markdown": "- 1行目\n- 2行目", "fact_ids": ["F1"]}])) == [], "箇条書きの改行を制御文字として落とした")
    # 文字として残った `\n`(二重エスケープ)は、検算の前に本物の改行へ戻す(2026-09-21: セットリスト37曲が1段落で出た)
    esc = dict(OK, title="見出し\\nです", blocks=[{"markdown": "次の通り。\\n- A\\n- B", "fact_ids": ["F1"]}],
               sources=OK["sources"][:1] + [{"url": "https://b.example/2", "label": "題名\\n続き"}])
    check(C(esc) == [] and esc["blocks"][0]["markdown"] == "次の通り。\n- A\n- B" and esc["title"] == "見出し です"
          and esc["sources"][1]["label"] == "題名 続き", f"\\n の戻し方: {esc['blocks'][0]['markdown']!r} {esc['title']!r}")
    # 地の文の直後の箇条書きは、書き出すときに空行で分ける(kramdown は空行が無いと段落に吸収する)
    S = renderlib.split_list_blocks
    check(S("次の通り。\n- A\n- B") == ["次の通り。", "- A\n- B"], f"地の文と箇条書きの境目: {S('次の通り。\n- A\n- B')}")
    check(S("- A\n  続き\n- B\n以上。") == ["- A\n  続き\n- B", "以上。"], f"箇条書きの続きの行と、後ろの地の文: {S('- A\n  続き\n- B\n以上。')}")
    check(S("1行目\n2行目") == ["1行目\n2行目"] and S("## 見出し") == ["## 見出し"] and S("a\n\nb") == ["a", "b"], "ふつうの段落を分けてしまう")
    check(any("tags" in p for p in C(dict(OK, tags=["a"]))), "tags 1個が通った")
    check(any("見出し" in p for p in C(dict(OK, title_fact_ids=[]))), "見出しの根拠無しが通った")
    check(len(C({"status": "decline", "decline_code": "", "decline_detail": ""})) == 2, "理由の無い decline が通った")
    check(C({"status": "decline", "decline_code": "NOT_NEWS", "decline_detail": "x"}) == [], "正当な decline が落ちた")
    check(any("decline_code" in p for p in C(dict(OK, decline_code="OTHER"))), "ok なのに decline_code ありが通った")


def test_render_and_length():
    _, fb = renderlib.materials_with_ids(MATS)
    good = dict(OK, new_facts=[{"id": "N1", "text": "9月25日発売", "url": "https://a.example/1"}],
                blocks=[{"markdown": "本文", "fact_ids": ["F1", "N1"]}],
                sources=[{"url": "https://a.example/1", "label": "ポータル「お​知らせ」"}])
    p = Path(tempfile.mkdtemp()) / "2026-09-12-x.md"
    renderlib.render_article(p, "2026-09-12", {"slug": "x", "brand": "765", "candidate_ids": ["c1"], "rank": "small"},
                             good, lambda u: "公式", lambda ts: "公式", compose.yaml_dump_keeping_strings)
    t = p.read_text(encoding="utf-8")
    check("<!-- F1 N1 -->" in t and "verified_facts" in t and "title_fact_ids" in t, "根拠が記事に残らない")
    check("​" not in t and "ポータル「お知らせ」" in t, "label の不可視文字が書き出しで残った")
    check(compose.body_length(p) == 2, f"根拠コメントが字数に入っている: {compose.body_length(p)}")
    # 地の文 + 箇条書きの block は、空行で分かれた2段落で書き出され、どちらにも根拠の控えが付く
    lst = dict(good, blocks=[{"markdown": "次の通り。\n- A\n- B", "fact_ids": ["F1"]}])
    p2 = p.with_name("2026-09-12-y.md")
    renderlib.render_article(p2, "2026-09-12", {"slug": "y", "brand": "765", "candidate_ids": ["c1"], "rank": "small"},
                             lst, lambda u: "公式", lambda ts: "公式", compose.yaml_dump_keeping_strings)
    body = p2.read_text(encoding="utf-8").split("\n---\n", 1)[1]
    check(body.strip() == "次の通り。 <!-- F1 -->\n\n- A\n- B <!-- F1 -->", f"箇条書きの書き出し: {body!r}")


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
    # 壊れた改行(文字としての `\n`)だけを直した稿は通る(2026-09-21 のセットリスト。旧稿も新稿も同じ単位で比べる。監査指摘 r71)
    broken = "前の段落。 <!-- F3 -->\n\n公開されたセットリストは次の通り。\\n- 曲A\\n- 曲B <!-- F1 -->"
    iss_nl = [{"issue_id": "I1", "rule_id": "R16", "repair": "rewrite_claim", "quote": "公開されたセットリストは次の通り。\\n- 曲A"}]
    fix_nl = dict(OK, addressed_issue_ids=["I1"], blocks=[{"markdown": "前の段落。", "fact_ids": ["F3"]},
                                                          {"markdown": "公開されたセットリストは次の通り。\n- 曲A\n- 曲B", "fact_ids": ["F1"]}])
    check(compose.revise_check(fix_nl, iss_nl, old_fm, broken) == [], f"改行だけ直した稿が落ちた: {compose.revise_check(fix_nl, iss_nl, old_fm, broken)}")
    # コードの中の `\n` は触らない
    check(renderlib.unescape_text("改行は `\\n` と書く。\\n- A") == "改行は `\\n` と書く。\n- A", f"コード内の \\n: {renderlib.unescape_text('改行は `\\n` と書く。\\n- A')!r}")
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
        check("発行後に直す指摘(later): なし" in text2, "later が無いことが報告に出ない")
        # 監査の later(発行後に直す指摘)は、前の試行から引き継いだ分も含めて報告に載る
        tr = [{"round": 0, "carried_later": [{"id": "l0", "claim": "前の試行の分", "evidence": "", "occurs": "月初の号で毎回"}]},
              {"round": 1, "review": {"verdict": "approve", "must_fix": [], "later": [
                  {"id": "l1", "claim": "堅牢化A", "evidence": "scripts/x.py:1", "occurs": "記事が60本を超えた号(月に1〜2回)"},
                  {"id": "l9", "claim": "改行が2100個入ったら", "evidence": "schema 上は可能", "occurs": " "}]}}]
        oncall.report_change("compose", "2026-09-12", {"diagnosis": "d"}, tr, "base", "headhash00", "", "repair/x", ["main"], "続き")
        text3 = (tmp / "metrics" / "oncall-2026-09-12-compose-report.md").read_text(encoding="utf-8").split("\n## 往復の記録\n")[0]
        check("発行後に直す指摘(later)2件" in text3 and "前の試行の分" in text3 and "堅牢化A" in text3 and "scripts/x.py:1" in text3
              and "起きるとき: 記事が60本を超えた号" in text3, f"later が報告に載らない: {text3[:400]}")
        check("改行が2100個" not in text3, "起きる道筋の無い指摘が報告の later に載った")
        # 保管(keep_later): 成功すれば積んだ件数、失敗しても例外を出さない(保管の失敗で発行を止めない)
        saved_b, saved_add = oncall.BACKLOG, oncall.backlog_add
        try:
            oncall.BACKLOG = tmp / "metrics" / "oncall-backlog.jsonl"
            check(oncall.keep_later([{"id": "l1", "claim": "堅牢化A", "evidence": "", "occurs": "60本を超えた号"}], "2026-09-12", "compose", "h" * 40) == 1
                  and len(oncall.backlog_open()) == 1, "later が保管されない")
            def boom(*a, **k):
                raise OSError("disk full")
            oncall.backlog_add = boom
            check(oncall.keep_later([{"id": "l2", "claim": "B", "evidence": "", "occurs": "毎号"}], "2026-09-12", "compose", "h" * 40) == 0, "保管の失敗が例外になる(発行が止まる)")
        finally:
            oncall.BACKLOG, oncall.backlog_add = saved_b, saved_add
        # main が合意のあと・再実行の判断の前に保管を呼び、差分なしの通知にも later を載せている(呼び出しを消したら落ちる)
        import inspect
        src = inspect.getsource(oncall.main)
        i_gate, i_keep, i_rerun = src.find("if not approved:"), src.find('keep_later(res["later"], date, stage, head)'), src.find("rerun_policy(")
        check(0 <= i_gate < i_keep < i_rerun, "main が合意のあとに later を保管していない")
        check("later_text(res['later'])" in src, "差分なし(no_fix_needed)の通知に later が載らない")
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
        # 答えの対象の書き方が依頼と違っても対応付ける(`youtube.com/@Foo` を頼んで `@foo` や URL で返る。
        # 2026-09-18: 答えているのに無回答扱いになり、議論しても決まらなかった)
        calls.clear()
        forms = iter([[{"host": "@TogawaNonoha", "type": "ファン", "why": "個人"}, {"host": "https://www.Example.com/", "type": "報道"}],
                      [{"host": "https://www.youtube.com/@togawanonoha/about", "type": "ファン", "why": "個人"}, {"host": "example.com", "type": "報道"},
                       {"host": "頼んでいない", "type": "報道"}]])
        cs.ask = lambda cmd, prompt, timeout=900: next(forms)
        ag, sp = cs.consensus("p", ["youtube.com/@TogawaNonoha", "example.com"])
        check(ag.get("youtube.com/@TogawaNonoha", ("",))[0] == "ファン" and ag.get("example.com", ("",))[0] == "報道" and not sp,
              f"書き方の違う答えを対応付けられない: {ag} {sp}")
        # 末尾が同じ依頼が2つあるときは、末尾だけの答えを当てない(取り違えない)
        amb = cs._by_host([{"host": "@foo", "type": "ファン"}], ["youtube.com/@foo", "tiktok.com/@foo"])
        check(amb == {}, f"曖昧な答えをどちらかに当てた: {amb}")
        # 同じ host の2対象のうち1つだけが2巡目へ進み、両モデルが host だけで答えても決まる。2巡目の依頼文は
        # 割れた対象だけで作り直す(監査指摘)
        prompts = []
        rounds = iter([[{"host": "youtube.com/@bar", "type": "報道"}, {"host": "youtube.com/@foo", "type": "ファン"}],
                       [{"host": "youtube.com/@bar", "type": "報道"}, {"host": "youtube.com", "type": "ファン"}],
                       [{"host": "youtube.com", "type": "ファン"}], [{"host": "youtube.com", "type": "ファン"}]])
        def ask2(cmd, prompt, timeout=900):
            prompts.append(prompt)
            return next(rounds)
        cs.ask = ask2
        ag, sp = cs.consensus(lambda ks: "対象: " + " ".join(ks), ["youtube.com/@bar", "youtube.com/@foo"])
        check(ag.get("youtube.com/@foo", ("",))[0] == "ファン" and ag.get("youtube.com/@bar", ("",))[0] == "報道" and not sp,
              f"同じ host の2対象で、2巡目の host だけの答えを捨てた: {ag} {sp}")
        check(len(prompts) == 4 and "@bar" not in prompts[2].split("\n")[0] and "@foo" in prompts[2], f"2巡目の依頼文に割れていない対象が残っている: {prompts[2][:80]}")
        cs.ask = fake_ask
        # パス付きの対象を host だけで返した答え: その host の依頼が1つだけなら当てる。2つあれば当てない
        one = cs._by_host([{"host": "youtube.com", "type": "ファン"}], ["youtube.com/@foo", "example.com"])
        two = cs._by_host([{"host": "youtube.com", "type": "ファン"}], ["youtube.com/@foo", "youtube.com/@bar"])
        check(list(one) == ["youtube.com/@foo"] and two == {}, f"host だけの答えの対応付け: {one} {two}")
        check("一字一句そのまま" in cs.build_prompt([("youtube.com/@foo", "https://www.youtube.com/@foo", "x")]), "依頼文が対象の写し方を指示していない")
        cs.ask = fake_ask
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


def test_classify_posts_only(tmp: Path):
    """組版前の判定は、その号の記事に載った未確認の出典だけを対象にする(候補は見ない)。
    取引(判定表 → 付け直し → lint)は、失敗したら判定表と記事を戻す。"""
    import classify_sources as cs
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "docs" / "_posts").mkdir(parents=True)
    (tmp / "candidates").mkdir()
    post = lambda d, u, t: f"---\nslug: s\nsources:\n- label: l\n  url: {u}\n  type: {t}\n---\n本文\n"
    (tmp / "docs" / "_posts" / "2026-09-19-a.md").write_text(post("2026-09-19", "https://www.youtube.com/watch?v=AAAAAAAAAAA", "未確認"), encoding="utf-8")
    (tmp / "docs" / "_posts" / "2026-09-18-b.md").write_text(post("2026-09-18", "https://www.youtube.com/watch?v=BBBBBBBBBBB", "未確認"), encoding="utf-8")
    (tmp / "candidates" / "2026-09-19.json").write_text(json.dumps([{"url": "https://www.youtube.com/watch?v=CCCCCCCCCCC"}]), encoding="utf-8")
    saved = (cs.ROOT, cs.POSTS_ONLY)
    try:
        cs.ROOT = tmp
        cs.POSTS_ONLY = None
        urls = {r["url"] for r in cs.target_rows("2026-09-19")}
        check(urls == {"https://www.youtube.com/watch?v=AAAAAAAAAAA", "https://www.youtube.com/watch?v=BBBBBBBBBBB",
                       "https://www.youtube.com/watch?v=CCCCCCCCCCC"}, f"収集の対象(候補+全号の未確認): {urls}")
        cs.POSTS_ONLY = "2026-09-19"
        urls = {r["url"] for r in cs.target_rows("2026-09-19")}
        check(urls == {"https://www.youtube.com/watch?v=AAAAAAAAAAA"}, f"組版前の対象(その号の記事だけ): {urls}")
    finally:
        cs.ROOT, cs.POSTS_ONLY = saved
    # 判定の単位: フォーム・文書は文書 ID まで、ブログ・ショップはサブドメイン、取れないものは理由付きで飛ばす
    # (2026-09-20: 公式のおたよりフォーム docs.google.com/forms/… を黙って飛ばし、未確認のまま載った)
    form = "https://docs.google.com/forms/d/e/1FAIpQLSdyUPmVP5FEpa-g80fdmZpBfyyynct6fHia4POevGc9mqJfuQ/viewform?usp=send_form"
    check(cs.platform_unit(form) == ("path", "docs.google.com/forms/d/e/1FAIpQLSdyUPmVP5FEpa-g80fdmZpBfyyynct6fHia4POevGc9mqJfuQ"), f"フォームの単位: {cs.platform_unit(form)}")
    check(cs.platform_unit("https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit")[1].endswith("/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345"), "文書の単位")
    check(cs.platform_unit("https://forms.gle/AbCdEf123") == ("path", "forms.gle/AbCdEf123"), "短縮フォームの単位")
    check(cs.platform_unit("https://someone.hatenablog.com/entry/2026/09/20/1") == ("domain", "someone.hatenablog.com"), "ブログはサブドメインが主体")
    check(cs.platform_unit("https://docs.google.com/")[0] == "skip" and "単位" in cs.platform_unit("https://docs.google.com/")[1], "単位を取れない URL の理由")
    check(cs.platform_unit("https://shop.example.jp/item/1") == ("domain", "shop.example.jp"), "ふつうのサイトはドメイン")
    # Drive はフォルダ・ファイルの ID まで。ID を取れない Drive の URL をホスト全体の主体にしない(監査指摘)
    check(cs.platform_unit("https://drive.google.com/drive/folders/19gSAbCdEfGhIjKlMnOpQrStUv") == ("path", "drive.google.com/drive/folders/19gSAbCdEfGhIjKlMnOpQrStUv"), "Drive のフォルダの単位")
    check(cs.platform_unit("https://drive.google.com/")[0] == "skip", "ID の無い Drive の URL をドメインとして判定対象にした")
    # X: アカウントに紐づく URL はアカウント単位、トレンドや検索は理由付きで飛ばす(黙って捨てない。監査指摘)
    check(cs.platform_unit("https://x.com/imas_official/status/1") == ("x", "imas_official"), "X のアカウントの単位")
    check(cs.platform_unit("https://x.com/i/trending/2091443421450039546")[0] == "skip", "X のトレンドの URL をアカウント扱いした")
    (tmp / "source_types.yml").write_text("path_types:\n  docs.google.com/forms/d/e/1FAIpQLSdyUPmVP5FEpa-g80fdmZpBfyyynct6fHia4POevGc9mqJfuQ: 公式\n", encoding="utf-8")
    saved_p = (pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY)
    try:
        pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY = tmp, None, None
        check(pipelib.classify_source(form) == "公式", "判定表に足したフォームのキーで、紙面の URL(/viewform?usp=…)が判定されない")
        check(pipelib.classify_source(form.replace("1FAIpQLS", "2XXXXXXX")) == "未確認", "別のフォームまで同じ種別になった")
    finally:
        pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY = saved_p
    # 飛ばした URL は SKIPPED に理由付きで残り、使われ方(記事の題名)は判定の材料に付く
    (tmp / "docs" / "_posts" / "2026-09-19-a.md").write_text(post("2026-09-19", "https://docs.google.com/", "未確認"), encoding="utf-8")
    (tmp / "docs" / "_posts" / "2026-09-19-c.md").write_text(
        "---\nslug: c\ntitle: 特別配信のおたより募集\nsources:\n- label: おたよりフォーム\n  url: " + form + "\n  type: 未確認\n---\n本文\n", encoding="utf-8")
    saved = (cs.ROOT, cs.POSTS_ONLY, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY)
    try:
        cs.ROOT = pipelib.ROOT = tmp
        pipelib._ST_TABLE, pipelib._ST_KEY = None, None
        (tmp / "source_types.yml").write_text("official_domains:\n  - a.example\n", encoding="utf-8")
        cs.POSTS_ONLY = "2026-09-19"
        doms, accts, paths = cs.unknown_targets("2026-09-19")
        check(list(paths) == ["docs.google.com/forms/d/e/1FAIpQLSdyUPmVP5FEpa-g80fdmZpBfyyynct6fHia4POevGc9mqJfuQ"] and not doms, f"フォームが合議の対象にならない: {paths} {doms}")
        check(list(cs.SKIPPED) == ["https://docs.google.com/"], f"飛ばした URL が記録されない: {cs.SKIPPED}")
        check("特別配信のおたより募集" in cs.used_in(list(paths)[0]) and "おたよりフォーム" in cs.used_in(list(paths)[0]), f"使われ方が材料に付かない: {cs.used_in(list(paths)[0])}")
    finally:
        cs.ROOT, cs.POSTS_ONLY, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY = saved
    # 文書の種別は**リンク元**で決める(編集長 2026-09-20)。公式の投稿の facts にフォームの URL があれば公式。合議に掛けない
    fid = "1FAIpQLSdyUPmVP5FEpa-g80fdmZpBfyyynct6fHia4POevGc9mqJfuQ"
    fkey = f"docs.google.com/forms/d/e/{fid}"
    saved = (cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY)
    try:
        cs.ROOT = pipelib.ROOT = tmp
        pipelib._ST_TABLE, pipelib._ST_KEY = None, None
        (tmp / "source_types.yml").write_text("x_accounts:\n  公式:\n    - valiv_official\n  当事者:\n    - some_shop\n  ファン:\n    - fan1\n"
                                              "press_domains:\n  - news.example\n", encoding="utf-8")
        def cands(rows):
            (tmp / "candidates" / "2026-09-19.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        off = {"url": "https://x.com/valiv_official/status/1", "facts": [f"配信へのお便り募集: {form}"]}
        cands([off, {"url": form, "facts": []}])
        check(cs.is_document(fkey) and not cs.is_document("tiktok.com/@a"), "文書の単位の見分け")
        check(cs.link_sources(fkey) == [("https://x.com/valiv_official/status/1", "公式")], f"リンク元: {cs.link_sources(fkey)}")
        # 一次発信が張っている文書は機械では決めない(公式が第三者の受付を紹介しているだけのことがある。監査指摘 r69)。
        # リンク元を材料に付けて、合議に確かめさせる
        check(cs.by_link_source(fkey) is None, "公式が張っているだけで、機械的に公式と決めた(紹介かもしれない)")
        check("https://x.com/valiv_official/status/1(公式)" in cs.link_hint(fkey), f"合議の材料にリンク元が付かない: {cs.link_hint(fkey)}")
        rule = (pipelib.PROMPTS / "classify-site.md").read_text(encoding="utf-8")
        check("リンク元と同じ種別" in rule and "紹介しているだけ" in rule, "依頼文に、リンク元での決め方(自身の文書か紹介か)が無い")
        cands([{"url": "https://x.com/fan1/status/3", "facts": [form]}])
        check(cs.by_link_source(fkey)[0] == "ファン", "ファンだけが張っている文書がファンにならない")
        cands([{"url": "https://news.example/a", "facts": [form]}, {"url": "https://x.com/fan1/status/3", "facts": [form]}])
        check(cs.by_link_source(fkey) is None, "報道が紹介しただけの文書を機械で決めた(合議へ回すべき)")
        cands([{"url": form, "facts": []}])
        check(cs.by_link_source(fkey) is None, "リンク元が無いのに決めた")
        # 判定表には**どうやって決めたか**を偽らずに残す(合議に掛けていないものを「合議で追加」と書かない)
        cs.add_paths({fkey: ("公式", "リンク元 https://x.com/valiv_official/status/1(公式)が張っている")}, how="機械で追加")
        row = next(ln for ln in (tmp / "source_types.yml").read_text(encoding="utf-8").splitlines() if fid in ln)
        check("機械で追加" in row and "合議" not in row, f"決め方の記録: {row}")
    finally:
        cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY = saved
    (tmp / "candidates" / "2026-09-19.json").write_text(json.dumps([{"url": "https://www.youtube.com/watch?v=CCCCCCCCCCC"}]), encoding="utf-8")
    (tmp / "docs" / "_posts" / "2026-09-19-c.md").unlink()
    (tmp / "docs" / "_posts" / "2026-09-19-a.md").write_text(post("2026-09-19", "https://www.youtube.com/watch?v=AAAAAAAAAAA", "未確認"), encoding="utf-8")
    # 取引: 失敗したら判定表と記事を**開始時の中身**へ戻す(未追跡の記事も。git の HEAD ではない。監査指摘)。
    # 組版前(lint=False)は lint を掛けない。戻せなければ理由に「戻せない」
    (tmp / "source_types.yml").write_text("official_domains:\n  - a.example\n", encoding="utf-8")
    post_a = tmp / "docs" / "_posts" / "2026-09-19-a.md"
    before = post_a.read_bytes()
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        if any("classify_sources.py" in str(c) for c in cmd):    # 合議が表と記事を書き換えたあと失敗
            (tmp / "source_types.yml").write_text("official_domains:\n  - a.example\n  - b.example\n", encoding="utf-8")
            post_a.write_text(post_a.read_text(encoding="utf-8").replace("未確認", "公式"), encoding="utf-8")
            (tmp / "docs" / "_posts" / "2026-09-19-new.md").write_text("x", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    saved_run, saved_root = pipelib.subprocess.run, pipelib.ROOT
    try:
        pipelib.subprocess.run, pipelib.ROOT = fake_run, tmp
        ok, why = pipelib.classify_retag_lint("2026-09-19", posts_only=True, lint=False)
        check(not ok and "合議 exit 1" in why and "boom" in why and "戻せない" not in why, f"取引の失敗の扱い: {ok} {why}")
        check("--posts-only" in calls[0] and not any("lint.py" in str(c) for cmd in calls for c in cmd), f"組版前の旗と lint 無し: {calls}")
        check(post_a.read_bytes() == before and "b.example" not in (tmp / "source_types.yml").read_text(encoding="utf-8")
              and not (tmp / "docs" / "_posts" / "2026-09-19-new.md").exists(), "失敗した取引が開始時の中身へ戻っていない")
        # 成功の経路(組版前): 合議 → 付け直し で終わり、lint は掛けない
        calls.clear()
        pipelib.subprocess.run = lambda cmd, **kw: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1]
        ok, why = pipelib.classify_retag_lint("2026-09-19", posts_only=True, lint=False)
        check(ok and len(calls) == 2, f"組版前の成功の経路: {ok} {len(calls)}")
        ok, why = pipelib.classify_retag_lint("2026-09-19")
        check(ok and any("lint.py" in str(c) for c in calls[-1]), "収集の取引で lint が掛からない")
        # 戻せないとき
        saved_restore = pipelib._restore
        pipelib._restore = lambda snap, new_under=None: "a.md: Permission denied"
        pipelib.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "cannot")
        ok, why = pipelib.classify_retag_lint("2026-09-19")
        check(not ok and "戻せない" in why, f"戻せないときの理由: {why}")
        pipelib._restore = saved_restore
    finally:
        pipelib.subprocess.run, pipelib.ROOT = saved_run, saved_root


def test_table_write_and_reload(tmp: Path):
    """判定表に書き足したら、同じプロセスの次の判定は新しい表で行う。同じものを2回足しても二重にならない。
    種別の節が無い動画 ID を、後ろの x_accounts の節へ差し込まない(2026-09-18 02:40: 同じ動画 ID が
    2回足されて表が二重定義になり、付け直しが exit 1 で落ちた)。"""
    import classify_sources as cs
    import pipelib
    import yaml
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / "source_types.yml"
    p.write_text("official_domains:\n  - a.example\nparty_domains:\n  - b.example\n"
                 "video_channels:\n  公式:\n    - imas-official\n"
                 "video_ids:\n  公式:\n    - AAAAAAAAAAA\n"
                 "x_accounts:\n  公式:\n    - imas_official\n  ファン:\n    - somebody\n", encoding="utf-8")
    saved = (cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY)
    try:
        cs.ROOT = pipelib.ROOT = tmp
        pipelib._ST_TABLE, pipelib._ST_KEY = None, None
        url = "https://www.youtube.com/watch?v=BBBBBBBBBB-"
        check(pipelib.classify_source(url) == "未確認", "表に無い動画が未確認にならない")
        cs.add_video_ids({"BBBBBBBBBB-": ("公式", "@imas-official「x」")})
        check(pipelib.classify_source(url) == "公式", "書き足した直後の判定が古い表のまま(キャッシュ)")
        cs.add_video_ids({"BBBBBBBBBB-": ("公式", "@imas-official「x」")})     # 2回目は足さない
        cs.add_video_ids({"CCCCCCCCCCC": ("ファン", "@somebody「y」")})        # video_ids に無い種別
        cs.add_video_channels({"Somebody": ("ファン", "個人")})
        cs.add_video_channels({"somebody": ("ファン", "個人")})                # 大文字小文字違いは同じ相手
        cs.add_domains({"c.example": ("当事者", "店")})
        cs.add_domains({"c.example": ("当事者", "店")})
        t = yaml.safe_load(p.read_text(encoding="utf-8"))
        check(t["video_ids"]["公式"].count("BBBBBBBBBB-") == 1, f"同じ動画 ID が二重に入った: {t['video_ids']}")
        check(t["video_ids"].get("ファン") == ["CCCCCCCCCCC"], f"種別の節が無い動画 ID の行き先: {t['video_ids']}")
        check(t["x_accounts"] == {"公式": ["imas_official"], "ファン": ["somebody"]}, f"x_accounts に混入した: {t['x_accounts']}")
        check(t["video_channels"].get("ファン") == ["Somebody"], f"チャンネルの追加: {t['video_channels']}")
        check(t["party_domains"].count("c.example") == 1, f"ドメインが二重に入った: {t['party_domains']}")
        check(pipelib.classify_source("https://youtu.be/CCCCCCCCCCC") == "ファン", "足した種別で判定されない")
        # 検査に通らない表はディスクに届かない
        before = p.read_text(encoding="utf-8")
        try:
            pipelib.write_source_table(before + "party_domains:\n  - z.example\n", p)
            check(False, "二重定義の表を書けてしまった")
        except SystemExit:
            pass
        check(p.read_text(encoding="utf-8") == before and not list(tmp.glob("source_types.yml.tmp-*")),
              "検査に落ちた表が書かれた、または一時ファイルが残った")
        # 置き換えで権限を変えない
        os.chmod(p, 0o664)
        old_umask = os.umask(0o027)
        try:
            pipelib.write_source_table(before, p)
        finally:
            os.umask(old_umask)
        check((p.stat().st_mode & 0o7777) == 0o664, f"表の権限が変わった: {oct(p.stat().st_mode & 0o7777)}")
        # 取引失敗の通知に載せる「子プロセスの言い分」: stderr、stdout だけ、lint の ::error、出力なし
        import types
        R = lambda out, err: types.SimpleNamespace(stdout=out, stderr=err, returncode=1)
        check("fatal: 原因" in pipelib.err_tail(R("", "x\nfatal: 原因\n")), "stderr の最後の行が載らない")
        check(pipelib.err_tail(R("a\n最後の行\n", "")) == "最後の行", "stdout だけのときの最後の行")
        check("posts/x.md: 赤い理由" in pipelib.err_tail(R("::error::posts/x.md: 赤い理由\nlint: 1 errors\n", "")), "lint の指摘が載らない")
        check(pipelib.err_tail(R("", "")) == "(出力なし)", "出力なし")
        # 外から書き換えられた表(merge・人の編集)も読み直す
        p.write_text(before.replace("  - a.example\n", "  - a.example\n  - d.example\n"), encoding="utf-8")
        check(pipelib.classify_source("https://d.example/x") == "公式", "外から変わった表を読み直していない")
    finally:
        cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY = saved


def _assemble_input(n_articles: int, inject: str = "") -> dict:
    """組版の入力の見本。inject を**全部の文字列値**(識別子も)に混ぜる。"""
    s = lambda x: f"{x}{inject}"
    arts = [{"slug": s(f"slug-{i}"), "brand": s("765"), "rank": s("small"), "title": s(f"見出し{i}"), "lede": s(f"リード{i}"),
             "event_date": s("2026-09-18"), "dedup_key": s(f"key-{i}"), "candidate_ids": [s(f"c{i}a"), s(f"c{i}b")],
             "existing_story": {"story_id": s(f"key-{i}"), "subject": s("件名"), "known_facts": [s("既報1"), s("既報2"), s("既報3")]},
             "facts": [{"id": s(f"F{k + 1}"), "text": s("事実" * 60)} for k in range(9)]} for i in range(n_articles)]
    return {"date": "2026-09-18", "articles": arts,
            "tomorrow_reservations": [{"subject": s("予約"), "kind": s("開幕"), "brand": s("765")}] * 8,
            "pending": [{"dedup_key": s(f"p{i}"), "subject": s("未確定"), "watch": s("発表を待つ")} for i in range(40)]}


def test_assemble_prompt_shape():
    """組版の指示ファイル: 指示が入力より先にあり、行数は記事数と facts の件数だけで決まる
    (値に改行が入っていても増えない。識別子も含めて全部の値で確かめる)。台帳の判断は LEDGER_CHUNK 本ずつ。
    2026-09-18: json.dumps(indent=1) の入力が 2005 行になり、末尾の指示が Read の1回(2000行)に入らず時間切れ。"""
    inp = _assemble_input(40)
    for label, make in (("digest", lambda x: assemble.prompt_digest("2026-09-18", x)),
                        ("ledger", lambda x: assemble.prompt_ledger("2026-09-18", x, x["articles"][:assemble.LEDGER_CHUNK]))):
        plain = make(inp)
        lines = plain.split("\n")
        check(plain.rstrip().endswith(plain.split("\n## 入力\n", 1)[1].rstrip()) and "\n## " not in plain.split("\n## 入力\n", 1)[1],
              f"{label}: 入力のあとに指示の節がある(入力が伸びると指示が切れる)")
        check(len(lines) < 400, f"{label}: {len(lines)} 行。1記事あたりの行数が増えている")
        check(max(len(ln) for ln in lines) < pipelib.READ_COLS, f"{label}: Read が切る長さの行がある")
        check(not re.search(r"(?<!ほかの)ファイルは読まず", plain), f"{label}: 「ファイルは読むな」と「指示ファイルを読め」が矛盾したまま")
        for bad in ("\n", "\n" * 300, "\r\n\t x y"):
            got = make(_assemble_input(40, bad)).split("\n")
            check(len(got) == len(lines), f"{label}: 値に {bad[:6]!r} が混ざると行数が {len(lines)} → {len(got)} に変わる")
    dg = assemble.prompt_digest("2026-09-18", inp)
    check("digest の規則" in dg and "stories(" not in dg and "  fact F1" not in dg and "slug-39" in dg, "digest に台帳の指示・facts が混ざる、または全記事が無い")
    lg = assemble.prompt_ledger("2026-09-18", inp, inp["articles"][8:16])
    check("stories(" in lg and "digest の規則" not in lg and "slug: slug-8 " in lg and "slug: slug-16 " not in lg and "slug: slug-7 " not in lg
          and "dedup_key: p39" in lg, "台帳の1組に、その組の記事と pending 全部が入っていない")
    # 200本の号でも digest の指示ファイルは Read の1回に収まる(台帳は組の大きさが一定)
    check(len(assemble.prompt_digest("2026-09-18", _assemble_input(200)).split("\n")) < pipelib.READ_LINES, "200本で digest が Read の1回を超える")
    # 行数が Read の1回に迫る・超えるファイルは、呼び出しの指示文で行数と読み方を伝える
    tmp = Path(tempfile.mkdtemp(prefix="imas-pf-"))
    short = pipelib.prompt_file("2026-09-18", "s", "a\nb\n", base=tmp)
    long_ = pipelib.prompt_file("2026-09-18", "l", "x\n" * 2300, base=tmp)
    check("offset" not in short and "全 2301 行" in long_ and "offset" in long_, f"行数の案内: {short!r} / {long_[-80:]!r}")
    # Read は絶対パスしか受けない。相対パスで渡すとモデルが作業フォルダを推測して外し、find / で探し始める
    check(f"`{(tmp / 'metrics' / 'work' / '2026-09-18' / 's.md').resolve()}`" in short, f"指示ファイルを絶対パスで渡していない: {short!r}")


ACTIVE_PROMPTS = ("plan-brand", "plan-rules", "plan-lead", "plan-missing", "write-article", "write-article.roundup",
                  "write-article.culture", "revise-article", "review-article", "review-paper", "assemble-digest", "assemble-ledger",
                  "collect-rules", "collect-item", "grok-collect", "grok-normalize", "explore", "watch-facts",
                  "classify-rules", "classify-site", "classify-x", "classify-debate",
                  "oncall-fix", "oncall-fix.objections", "oncall-review")


def test_prompts_are_instructions_only():
    """依頼文は prompts/ に置き、**指示だけ**を書く(編集長 2026-09-18:「意図みたいなデータはプロンプトじゃない」)。
    規則の理由・経緯・事故の記録は PROMPTS.md に置く。依頼文の本文をコードに埋め戻さない。"""
    for name in ACTIVE_PROMPTS:
        p = pipelib.PROMPTS / f"{name}.md"
        check(p.exists(), f"prompts/{name}.md が無い")
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        for pat, what in ((r"実測[::で]|[((]実測|監査指摘|編集長の|編集長[::]|事故|起きています", "経緯・事故の記録"),
                          (r"20\d\d-\d\d-\d\d|\(\d{1,2}/\d{1,2}\)", "日付入りの経緯"),
                          (r"規程\s*\d", "規程番号の参照(根拠の所在はデータ)")):
            m = re.search(pat, text)
            check(m is None, f"prompts/{name}.md に{what}が書かれている: {m.group(0) if m else ''}")
        check(len(text.split("\n")) <= 90, f"prompts/{name}.md が {len(text.split(chr(10)))} 行(90 行を超えたら、分けるか削る)")
    # 埋め残し・使われない値はエラー(差し込みの取り違えを黙って通さない)
    for kwargs in ({"DATE": "2026-09-18"}, {"DATE": "d", "WEEKDAY": "金", "ARTICLES": "[]", "EXTRA": "x"}):
        try:
            pipelib.render_prompt("plan-lead", **kwargs)
            check(False, f"render_prompt が取り違えを通した: {sorted(kwargs)}")
        except ValueError:
            pass
    out = pipelib.render_prompt("plan-lead", DATE="2026-09-18", WEEKDAY="金", ARTICLES='[{"slug": "{DATE}"}]')
    check('"{DATE}"' in out and "2026-09-18(金曜)" in out, "値の中の {…} を埋め直した、または埋まっていない")
    # 依頼文の本文をコードに埋め戻していない(残っているのは、2026-09-06 号で終了した社説の依頼文だけ)
    for f in ("compose.py", "assemble.py", "collect.py", "classify_sources.py", "oncall.py"):
        src = (Path(__file__).resolve().parent / f).read_text(encoding="utf-8")
        n = src.count("あなたは日刊AI新聞")
        check(n == (1 if f == "compose.py" else 0), f"{f}: 依頼文の本文がコードに埋まっている({n}か所。prompts/ に置く)")
    # 各依頼文が、実際の呼び出しで埋まる
    art = {"slug": "s", "brand": "765", "rank": "roundup", "angle": "a", "candidate_ids": ["c1"]}
    t = compose.article_prompt("2026-09-18", art, [{"id": "c1"}], [], None)
    check("rank: roundup" in t and t.rstrip().endswith("]") and t.index("## 1. 手順") < t.index("## 素材"), "執筆の依頼文: rank 別の追加・素材が最後、になっていない")
    t = compose.brand_plan_prompt("2026-09-18", "general", 3, [], claimed=[{"slug": "x"}])
    check("rank: culture" in t and "plan-index-2026-09-18-general.json" in t and '"slug": "x"' in t, "選定の依頼文が埋まっていない")
    # 選定の規則は1か所(plan-rules)。面別の選定と、判定から漏れた主題の拾い直しが同じものを使う(監査指摘 r56)
    rules = compose.plan_rules()
    miss = compose.missing_plan_prompt("2026-09-18", [{"dedup_key": "k"}], [])
    check(rules in t and rules in miss and "同人イベント" in miss and "共同名義" in miss, "拾い直しに選定の規則が渡っていない")
    # X の調査は対象期間を明示し、角度を変えた検索にも since を付けさせる(監査指摘 r56)
    import collect
    gp = collect.write_grok_prompt(Path(tempfile.mkdtemp()), {"key": "k", "brand": "765", "topic": "t", "accounts": ["a"]}).read_text(encoding="utf-8")
    since = re.search(r"対象期間: (\d{4}-\d{2}-\d{2}) 以降", gp)
    check(since and f"since:{since.group(1)}" in gp.split("2. 角度を変えて掘る")[1] and "対象期間より前" in gp, "Grok の依頼文に対象期間の規則が無い")
    check("lead_slug" in compose.lead_prompt("2026-09-18", [{"slug": "s", "rank": "small"}]) and
          "社説" not in compose.lead_prompt("2026-09-18", []), "一面の依頼文(社説は選ばせない)")


def test_assemble_judge():
    """判断を分けて取り、従来と同じ形の1つの出力にまとめる。記事の並び順のまま、pending_remove は重複なし。
    1本でも答えが無ければ全体が上がる(半端な台帳を作らない)。"""
    inp = _assemble_input(19)
    seen = []
    def fake_session(text, date, name, schema, budget=None):
        seen.append((name, sorted(json.loads(schema)["required"])))
        if name == "assemble-digest":
            return {"digest": [{"label": "本日", "rows": []}]}
        slugs = [ln.split()[2] for ln in text.split("\n") if ln.startswith("- slug: ")]
        return {"stories": [{"slug": s} for s in slugs] + [{"slug": "slug-0"}, {"slug": "どこにも無い"}],   # 余計な行(他の組の記事)
                "reservations": [{"slug": slugs[0]}], "pending_add": [{"dedup_key": "よその話題"}], "pending_remove": ["p2", "p1"]}
    saved = assemble.run_session
    try:
        assemble.run_session = fake_session
        out = assemble.judge("2026-09-18", inp)
        check(sorted(n for n, _ in seen) == ["assemble-digest", "assemble-ledger-1", "assemble-ledger-2", "assemble-ledger-3"], f"セッションの分け方: {seen}")
        check(dict(seen)["assemble-digest"] == ["digest"] and dict(seen)["assemble-ledger-1"] == sorted(assemble.LEDGER_KEYS), f"schema の切り分け: {seen}")
        check([s["slug"] for s in out["stories"]] == [f"slug-{i}" for i in range(19)], "stories が記事の並び順でない・欠けている")
        check(out["pending_remove"] == ["p1", "p2"] and len(out["reservations"]) == 3 and out["digest"], f"まとめ方: {out['pending_remove']} {len(out['reservations'])}")
        check(out["pending_add"] == [], f"その組の記事のものでない pending_add を採った: {out['pending_add']}")
        # モデルが返す順に依存しない: 逆順で返しても、まとめた結果は同じ(監査指摘)
        def reversed_session(text, date, name, schema, budget=None):
            o = fake_session(text, date, name, schema)
            return {k: list(reversed(v)) for k, v in o.items()}
        assemble.run_session = reversed_session
        check(assemble.judge("2026-09-18", inp) == out, "返ってきた順で結果が変わる")
        # まとめの規則(形と順序だけ)
        arts = [{"slug": "a", "dedup_key": "ka"}, {"slug": "b", "dedup_key": "kb"}, {"slug": "c", "dedup_key": "ka"}]
        inp2 = {"articles": arts, "pending": [{"dedup_key": "p1"}, {"dedup_key": "p2"}, {"dedup_key": "p3"}]}
        m = assemble.merge_judgments(inp2, [arts[:2], arts[2:]], {"digest": []}, [
            {"stories": [{"slug": "b", "n": 1}, {"slug": "a", "n": 1}, {"slug": "a", "n": 2}],
             "reservations": [{"slug": "b", "date": "2026-10-02"}, {"slug": "a", "date": "2026-10-09"}, {"slug": "a", "date": "2026-10-01"}],
             "pending_add": [{"dedup_key": "kb", "subject": "x"}, {"dedup_key": "ka", "subject": "先"}, {"dedup_key": "よそ"}],
             "pending_remove": ["p3", "無い"]},
            {"stories": [{"slug": "c", "n": 1}], "reservations": [],
             "pending_add": [{"dedup_key": "ka", "subject": "後"}], "pending_remove": ["p1", "p3"]}])
        check([(s["slug"], s["n"]) for s in m["stories"]] == [("a", 1), ("b", 1), ("c", 1)], f"stories の順・1件化: {m['stories']}")
        check([(r["slug"], r["date"]) for r in m["reservations"]] == [("a", "2026-10-01"), ("a", "2026-10-09"), ("b", "2026-10-02")], f"reservations の順: {m['reservations']}")
        check(m["pending_add"] == [{"dedup_key": "ka", "subject": "先"}, {"dedup_key": "kb", "subject": "x"}], f"pending_add の規則: {m['pending_add']}")
        check(m["pending_remove"] == ["p1", "p3"], f"pending_remove は入力にあるものを入力の順で: {m['pending_remove']}")
        # 同じ記事・同じ key に**中身の違う**行が重なっても、返ってきた順で採用が変わらない(監査指摘)
        rows = [{"stories": [{"slug": "a", "subject": "甲", "published_facts": ["F1"]}, {"slug": "a", "subject": "乙", "published_facts": ["F2"]}],
                 "pending_add": [{"dedup_key": "ka", "watch": "甲"}, {"dedup_key": "ka", "watch": "乙"}]},
                {"pending_add": [{"dedup_key": "ka", "watch": "丙"}]}]
        flip = [{k: list(reversed(v)) for k, v in rows[0].items()}, rows[1]]
        m1 = assemble.merge_judgments(inp2, [arts[:2], arts[2:]], {"digest": []}, rows)
        m2 = assemble.merge_judgments(inp2, [arts[:2], arts[2:]], {"digest": []}, flip)
        check(m1 == m2 and len(m1["stories"]) == 1 and len(m1["pending_add"]) == 1, f"重複行の採用が返却順で変わる: {m1} / {m2}")
        def broken(text, date, name, schema, budget=None):
            if name == "assemble-ledger-2":
                raise RuntimeError("組版セッション(assemble-ledger-2)が答えを返さなかった")
            return fake_session(text, date, name, schema)
        assemble.run_session = broken
        try:
            assemble.judge("2026-09-18", inp)
            check(False, "1組の答えが無いのに出力をまとめた")
        except RuntimeError as e:
            check("assemble-ledger-2" in str(e), f"どの組が駄目だったか分からない: {e}")
    finally:
        assemble.run_session = saved


def test_claude_traced(tmp: Path):
    """経過を残す実行: 成功なら structured_output を返し、時間切れでもそこまでの道具の呼び出しが要約に残る。"""
    tmp.mkdir(parents=True, exist_ok=True)
    fake = tmp / "claude"
    fake.write_text("#!/bin/sh\n"
                    "echo '{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"tool_use\",\"name\":\"Read\",\"input\":{\"file_path\":\"metrics/work/x/assemble.md\",\"offset\":2000}}]}}'\n"
                    "case \"$*\" in *SLOW*) sleep 41 & echo $! > grandchild.pid; sleep 30;; esac\n"
                    "echo '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":\"{}\",\"structured_output\":{\"digest\":1},\"total_cost_usd\":0.1}'\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    saved = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = f"{tmp}:{saved}"
        ok = pipelib.claude_traced(["FAST"], tmp / "t1.jsonl", timeout=20, cwd=tmp)
        check(ok["ok"] and ok["structured"] == {"digest": 1} and "Read(metrics/work/x/assemble.md+2000)" in ok["summary"], f"成功時: {ok}")
        slow = pipelib.claude_traced(["SLOW"], tmp / "t2.jsonl", timeout=2, cwd=tmp)
        check(slow["timed_out"] and not slow["ok"] and slow["structured"] is None
              and "時間切れ" in slow["summary"] and "Read(" in slow["summary"], f"時間切れ時に経過が残らない: {slow}")
        # 時間切れでは子孫(道具が起動したプロセス)まで止める。残すと、やり直しのセッションと並走する(監査指摘)
        gpid = int((tmp / "grandchild.pid").read_text().strip())
        try:
            os.kill(gpid, 0)
            alive = "Z" not in Path(f"/proc/{gpid}/stat").read_text().split(")")[-1].split()[0]   # ゾンビは止まっている
        except (ProcessLookupError, FileNotFoundError):
            alive = False
        check(not alive, f"時間切れのあとに孫プロセス {gpid} が残った")
        # 正常に終わらなかったセッションの出力は、形が合っていても採らない
        half = dict(slow, structured={"digest": 9})
        saved_h = assemble.claude_traced
        try:
            assemble.claude_traced = lambda *a, **k: half
            saved_r, pipelib.ROOT = pipelib.ROOT, tmp
            try:
                assemble.run_session("x", "2026-09-18", "assemble-digest", "{}", budget=lambda: 100)
                check(False, "時間切れのセッションの出力を採った")
            except RuntimeError:
                pass
            finally:
                pipelib.ROOT = saved_r
        finally:
            assemble.claude_traced = saved_h
        # 組版セッション: 1回目が時間切れでも、時間が残っていれば2回目の答えを採る。残っていなければ経過を付けて上げる
        calls = []
        def fake_traced(args, trace, timeout, cwd=None):
            calls.append(timeout)
            return slow if len(calls) == 1 else ok
        saved_t, saved_root = assemble.claude_traced, pipelib.ROOT
        try:
            assemble.claude_traced = fake_traced
            pipelib.ROOT = tmp
            check(assemble.run_session("x", "2026-09-18", "assemble-digest", "{}", budget=lambda: 700) == {"digest": 1} and calls == [700, 700], f"やり直し: {calls}")
            calls.clear()
            try:
                assemble.run_session("x", "2026-09-18", "assemble-digest", "{}", budget=lambda: 100)
                check(False, "残り 100 秒でやり直した")
            except RuntimeError as e:
                check(len(calls) == 1 and "時間切れ" in str(e) and "Read(" in str(e) and "やり直せない" in str(e), f"上げる文面: {e}")
        finally:
            assemble.claude_traced, pipelib.ROOT = saved_t, saved_root
    finally:
        os.environ["PATH"] = saved


def test_time_budget():
    """締切の判断: 校閲の往復は「最後のサイクル(落とす→組版→校閲1波→commit)」が丸ごと残るときだけ、
    最後のサイクル自体はその分だけあれば始める(2026-09-17: 残り17分で除外を諦めて号が止まった)。"""
    import time as _t
    saved = dict(compose.STAGE_MIN)
    try:
        compose.STAGE_MIN.clear()
        compose.STAGE_MIN.update({"組版": 9.0, "校閲1波": 2.0})
        tc = compose.terminal_cost()
        check(abs(tc - (9.0 * 1.2 + 2.0 * 1.2 + 3)) < 0.01, f"terminal_cost の計算: {tc}")
        # 残り17分: 最後のサイクル(≈15.8分)は始められる。校閲の往復(その後に最後のサイクルが要る)は始められない
        t0 = _t.time() - (compose.COMPOSE_LIMIT_MIN - 17) * 60
        check(compose.afford(t0, None, 0, "除外", extra=compose.terminal_cost(), terminal=False), "残り17分で最後のサイクルを諦めた")
        check(not compose.afford(t0, "校閲1波", 4, "往復", extra=4), "残り17分で校閲の往復を始めた")
        # 残り12分: 最後のサイクルも始められない(完走できない)
        t0 = _t.time() - (compose.COMPOSE_LIMIT_MIN - 12) * 60
        check(not compose.afford(t0, None, 0, "除外", extra=compose.terminal_cost(), terminal=False), "完走できないのに最後のサイクルを始めた")
        # 実測が無いときの既定は保守的(組版9分)
        compose.STAGE_MIN.clear()
        check(compose.terminal_cost() >= 9 * 1.2 + 2 + 3, f"既定の terminal_cost が小さい: {compose.terminal_cost()}")
        # 実測が長ければ長いほうを使う(固定値で見積もらない。監査指摘)
        compose.STAGE_MIN.update({"組版": 15.0, "校閲1波": 5.0})
        check(compose.terminal_cost() >= 15 * 1.2 + 5 * 1.2 + 3, "実測が長いのに反映されない")
        # 締切は絶対時刻: 04:00 起動なら 06:00 の HANDOFF_MIN 前(遅れて起動しても発行時刻を越えない)。
        # 発行時刻を過ぎた手動再実行は相対 120 分
        import datetime as _dt
        t_0400 = _dt.datetime(2026, 9, 17, 4, 0, tzinfo=pipelib.JST).timestamp()
        dl = compose.hard_deadline(t_0400, "2026-09-17")
        check(abs(dl - (_dt.datetime(2026, 9, 17, 6, 0, tzinfo=pipelib.JST).timestamp() - compose.HANDOFF_MIN * 60)) < 1,
              "04:00 起動の締切が 06:00 前になっていない")
        t_0430 = t_0400 + 30 * 60
        check(compose.hard_deadline(t_0430, "2026-09-17") == dl, "遅れて起動しても締切が伸びている")
        t_1100 = t_0400 + 7 * 3600
        check(abs(compose.hard_deadline(t_1100, "2026-09-17") - (t_1100 + compose.COMPOSE_LIMIT_MIN * 60)) < 1,
              "発行後の手動再実行が相対 120 分になっていない")
        saved_dl = compose.DEADLINE
        try:
            compose.DEADLINE = _t.time() + 120
            check(60 <= compose.remaining_seconds() <= 120, f"子プロセスの待ち時間が締切で切られない: {compose.remaining_seconds()}")
            compose.DEADLINE = _t.time() + 5000
            check(compose.remaining_seconds() == 900, "cap を超えた")
            compose.DEADLINE = None
            check(compose.remaining_seconds() == 900, "DEADLINE 未設定の既定")
        finally:
            compose.DEADLINE = saved_dl
    finally:
        compose.STAGE_MIN.clear()
        compose.STAGE_MIN.update(saved)


def test_clean_url_and_table():
    """URL の唯一の入口(clean_url)と、判定表 path_types の検査(監査指摘)。"""
    C = pipelib.clean_url
    check(C("https://x.com/a/status/1\n-") == "https://x.com/a/status/1", "末尾のゴミが落ちない")
    check(C("https://Example.com/Path?q=1#f") == "https://example.com/Path?q=1#f", "host の小文字化・パスの保持")
    check(C("https://ja.wikipedia.org/wiki/A_(B)") == "https://ja.wikipedia.org/wiki/A_(B)", "括弧つき URL を壊した")
    check(C("http://user@a.com/") is None and C("ftp://a") is None and C("") is None and C("https://a.com/x\x00y") is None,
          "使えない形を通した")
    check(C("https://a.com/" + "x" * 3000) is None, "長すぎる URL を通した")
    # 前後の空白・タブ・URL 内の空白は黙って strip せず不正(監査指摘 r35)。既知のゴミ形「URL\n-」だけ外す
    for raw in (" https://a.com/x", "https://a.com/x ", "\thttps://a.com/x", "https://a.com/x y", "https://a.com/x\nhttps://b.com/"):
        check(C(raw) is None, f"空白入りの URL を通した: {raw!r}")
    check(C("https://a.com/x\n-") == "https://a.com/x" and C("https://a.com/x\n-\n") == "https://a.com/x", "既知のゴミ形を外せない")
    # Unicode の空白・制御・書式文字も不正(監査指摘 r36)
    for ch in ("", " ", " ", " ", " ", " ", " ", " ", "​", "‎", "﻿"):
        check(C(f"https://a.com/x{ch}") is None and C(f"{ch}https://a.com/x") is None and C(f"https://a.com/x{ch}y") is None,
              f"Unicode の空白・制御文字 U+{ord(ch):04X} を通した")
    # 検算: 出典 URL は clean_url で変わらない形でなければ差し戻し
    _, fb = renderlib.materials_with_ids(MATS)
    bad = dict(OK, sources=OK["sources"][:2] + [{"url": "https://c.example/3\n-", "label": "z"}])
    check(any("形が不正" in p for p in renderlib.check_output(bad, fb, MATS)), "汚れた出典 URL が通った")
    # path_types の検査: 値域・重複・親子競合
    ok = {"path_types": {"ch.nicovideo.jp/sidem": "公式", "tiktok.com/@x": "当事者"}}
    try:
        pipelib._check_table(ok, "t")
    except SystemExit as e:
        check(False, f"正しい path_types が落ちた: {e}")
    for bad_t, why in (({"path_types": {"a.com/x": "公式", "a.com/x/y": "ファン"}}, "親子競合"),
                       ({"path_types": {"a.com/x": "神"}}, "値域"),
                       ({"path_types": {"https://a.com/x": "公式"}}, "scheme 付き"),
                       ({"path_types": {"a.com/x": "公式", "A.com/x/": "公式"}}, "正規化後の重複"),
                       ({"path_types": ["a.com/x"]}, "形")):
        try:
            pipelib._check_table(bad_t, "t")
            check(False, f"path_types の{why}が通った")
        except SystemExit:
            pass


def test_no_prompt_in_argv():
    """モデル(claude / codex)を起動する箇所で、可変のプロンプトを引数に直接渡していない(全経路 prompt_file)。
    引数は 128KB で落ちる(監査指摘: 経路の取りこぼしを静的に検出する)。"""
    import re as _re
    root = Path(__file__).resolve().parent
    bad = []
    for f in sorted(root.glob("*.py")):
        if f.name in ("test_pipeline.py", "pipelib.py"):
            continue
        src = f.read_text(encoding="utf-8")
        for m in _re.finditer(r'"-p",\s*([A-Za-z_][A-Za-z_0-9\.]*)\s*[,\]]|\+\s*\[\s*([A-Za-z_][A-Za-z_0-9]*)\s*\]|,\s*(prompt|cp|text)\s*\]', src):
            name = m.group(1) or m.group(2) or m.group(3)
            # モデル起動の文脈(直前5行に claude / codex)だけを見る
            window = src[max(0, src.rfind("\n", 0, max(0, src.rfind("\n", 0, m.start()) - 240))):m.end()]
            if name in ("prompt", "cp", "text", "p", "q") and ("claude" in window or "codex" in window):
                line = src.count("\n", 0, m.start()) + 1
                bad.append(f"{f.name}:{line}: {m.group(0)}")
    check(not bad, f"プロンプトを引数に直接渡している箇所: {bad[:6]}")
    # 指示はファイルなので、道具を絞る呼び出し(--allowedTools)には Read が要る(2026-09-17: 無くて 420 秒待って落ちた)
    for f in sorted(root.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for m in _re.finditer(r'"--allowedTools",\s*"([^"]*)"', src):
            if "Read" not in m.group(1).split(","):
                check(False, f"{f.name}: --allowedTools に Read が無い({m.group(1)})")


def test_next_number(tmp: Path):
    """号数は「自分より前の号の最大+1」。自分の号に誤った値(進んだ番号)が書いてあっても直る(監査指摘 r35)。"""
    ed = tmp / "docs" / "_editions"
    ed.mkdir(parents=True)
    for d, n in (("2026-09-14", 0), ("2026-09-15", 1), ("2026-09-16", 2), ("2026-09-17", 4), ("2026-09-18", 9)):
        (ed / f"{d}.md").write_text(f"---\nnumber: {n}\ndate: '{d}'\n---\n", encoding="utf-8")
    saved = compose.ROOT
    try:
        compose.ROOT = tmp
        check(compose.next_number("2026-09-17", live=True) == 3, "誤って進んだ自分の号数を直せない")
        check(compose.next_number("2026-09-17", live=True) == compose.next_number("2026-09-17", live=True), "再実行で号数が変わる")
        check(compose.next_number("2026-09-19", live=True) == 10, "次号の号数")
        check(compose.next_number("2026-09-17", live=False) == 0, "試験段階は 0")
    finally:
        compose.ROOT = saved


def test_dedupe_source_table(tmp: Path):
    """union merge で二重になった判定表の行を、節ごとに1つにする(2026-09-17)。"""
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / "source_types.yml"
    p.write_text("party_domains:\n  - a.jp   # x\n  - b.jp\n  - A.jp   # dup\nsecondary_domains:\n  - a.jp   # 別の節は別\n"
                 "video_ids:\n  公式:\n    - v1\n    - v1\n    - v3\n  準公式:\n    - v2\n    - v3   # 別の種別は競合(消さない)\n"
                 "path_types:\n  t.com/@x: 公式\n  t.com/@x/: 公式   # dup\n  t.com/@y: 公式\n  t.com/@y: 当事者   # 値が違う=競合(消さない)\n",
                 encoding="utf-8")
    removed = pipelib.dedupe_source_table(p)
    text = p.read_text(encoding="utf-8")
    check(len(removed) == 3 and text.count("a.jp") == 2 and text.count("- v1") == 1 and text.count("t.com/@x") == 1,
          f"二重行の除去: {removed} / {text}")
    # 種別・値の違う同一キーは分類の競合として残す(監査指摘 R38-1)
    check(text.count("- v3") == 2 and text.count("t.com/@y") == 2, f"競合を二重として消した: {text}")
    check(pipelib.dedupe_source_table(p) == [], "冪等でない")
    # 残した競合は、種類ごとに**単独で**検査に掛かる(複合テストで片方が隠れないように。監査指摘 r39)
    import yaml

    def stops(txt: str) -> bool:
        try:
            pipelib._check_table_text(txt, "t")
            pipelib._check_table(yaml.safe_load(txt), "t")
            return False
        except SystemExit:
            return True
    check(stops("video_ids:\n  公式:\n    - v3\n  準公式:\n    - v3\n"), "video_ids の別種別の同一 ID を止めない")
    check(stops("path_types:\n  t.com/@y: 公式\n  t.com/@y: 当事者\n"), "path_types の同一キー・異値を止めない(YAML が後勝ちにする)")
    check(stops("path_types:\n  t.com/@y: 公式\n  T.com/@y/: 公式\n"), "path_types の正規化後に同じキーを止めない")
    check(stops("suffix_types:\n  \".lg.jp\": 当事者\n  \".lg.jp\": 公式\n"), "suffix_types の同一キー・異値を止めない")
    # 書き方が違っても YAML が同じキーと解釈すれば止める(監査指摘 r40): 1空白・キーとコロンの間の空白・引用符・flow mapping
    check(stops("path_types:\n t.com/@y: 公式\n t.com/@y: 当事者\n"), "1空白インデントの二重キーを止めない")
    check(stops("path_types:\n  t.com/@y : 公式\n  t.com/@y: 当事者\n"), "キーとコロンの間の空白で二重キーを見逃した")
    check(stops("path_types:\n  \"t.com/@y\": 公式\n  't.com/@y': 当事者\n"), "引用符違いの二重キーを止めない")
    check(stops("path_types: {t.com/@y: 公式, t.com/@y: 当事者}\n"), "flow mapping の二重キーを止めない")
    # 同名の節を2回書く(節をまたぐ重複)も止める(監査指摘 r41)
    check(stops("path_types:\n  t.com/@y: 公式\npath_types:\n  t.com/@y: 当事者\n"), "path_types の節の二重を止めない")
    check(stops("suffix_types:\n  \".lg.jp\": 当事者\nsuffix_types:\n  \".go.jp\": 当事者\n"), "suffix_types の節の二重を止めない")
    check(stops("party_domains:\n  - a.jp\nparty_domains:\n  - b.jp\n"), "リストの節の二重を止めない")
    # 行番号は正しく報告される(監査指摘 r42)。エイリアス/アンカーは字句の段階で使用行を示して拒否

    def message(txt: str) -> str:
        try:
            pipelib._check_table_text(txt, "t")
            return ""
        except SystemExit as e:
            return str(e)
    check("1 行目と 3 行目" in message("path_types:\n  t.com/@y: 公式\npath_types:\n  t.com/@y: 当事者\n"), "節の二重の行番号")
    check("2 行目と 3 行目" in message("path_types:\n  t.com/@y: 公式\n  t.com/@y: 当事者\n"), "キーの二重の行番号")
    check("3 行目と 4 行目" in message("suffix_types:\n  \".go.jp\": 当事者\n  \".lg.jp\": 当事者\n  \".LG.jp/\": 公式\n"), "正規化後の二重の行番号")
    # アンカーとエイリアスは別々に(片方だけ残しても通らないように。監査指摘 r43)
    m_anchor = message("path_types:\n  a/b: 公式\nsuffix_types: &s\n  \".lg.jp\": 当事者\n")
    check("3 行目" in m_anchor and "アンカー(&s)" in m_anchor, f"アンカーを使用行付きで拒否しない: {m_anchor}")
    m_alias = message("path_types: *sec\n")
    check("1 行目" in m_alias and "エイリアス(*sec)" in m_alias, f"エイリアスを使用行付きで拒否しない: {m_alias}")
    check(not stops("path_types:\n  t.com/@y: 公式\n  t.com/@z: 当事者\nsuffix_types:\n  \".lg.jp\": 当事者\n"), "正しい対応を止めた")


def test_tool_path():
    import os
    p = pipelib.tool_path()
    check(os.path.expanduser("~/.local/bin") in p.split(":") and "/usr/bin" in p.split(":"), f"tool_path に利用者の bin が無い: {p}")


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


def test_oncall_fix_until_clean(tmp: Path):
    """当番と監査の範囲は「発行に必要な最小限で、今後もちゃんと動く正しい修正」(編集長の整理 2026-09-18)。
    その範囲の指摘(must_fix)は合意するまで直す。発行後でよい指摘(later)は別に保管して、発行してから直す。
    直し切れなかった試行は、修正の commit と残った指摘を残し、次の試行はその続きから始める。"""
    import oncall
    check(oncall.MAX_ROUNDS >= 4, f"往復の上限が {oncall.MAX_ROUNDS}。2往復で投げ出していた頃に戻っている")
    fp = oncall.fix_prompt("compose", "2026-09-18", "ctx", [{"id": "x", "claim": "c"}])
    rp = oncall.review_prompt("compose", "2026-09-18", {"status": "fixed"}, "diff --git a b", {"accepted": []})
    scope = "発行するのに必要な最小限で、今後もちゃんと動く正しい修正"
    check(scope in fp and scope in rp, "当番・監査の依頼文に仕事の範囲が無い")
    check("指摘されたものは直す" in fp and "反論する" not in fp and "commit 済み" in fp and "範囲は広げない" in fp, "当番への依頼が「範囲の中で直す」になっていない")
    check("**must_fix**" in rp and "**later**" in rp and "later が残っていてもよい" in rp, "監査への依頼が must_fix と later を分けていない")
    # 起きる道筋(どういうときに・どのくらい)を言えない指摘は、指摘として扱わない(編集長 2026-09-18)
    check("`occurs`" in rp and "**書かない**: 起きる道筋を言えないもの" in rp and "schema 上は可能" in rp, "監査への依頼が、起きる道筋の無い指摘を捨てさせていない")
    check("起きる道筋が無い" in fp and "追いかけて直さない" in fp, "当番が、起きる道筋の無い指摘を弾けることになっていない")
    check(all(x in rp for x in oncall.POLICY_EXCLUDED) and all(x in fp for x in oncall.POLICY_EXCLUDED), "判定対象外(編集方針)が依頼文に無い")
    rs = json.loads((Path(oncall.__file__).resolve().parent.parent / "schema" / "oncall-review.schema.json").read_text(encoding="utf-8"))
    check("later" in rs["required"] and rs["properties"]["must_fix"]["items"]["properties"]["severity"]["enum"] == ["blocks_publish", "corrupts_data", "stopgap"],
          "監査の schema: later が必須でない、または must_fix に品質・書き方の指摘を入れられる")
    check(all("occurs" in rs["properties"][k]["items"]["required"] for k in ("must_fix", "later")), "監査の schema: 指摘に occurs(起きる道筋)が必須でない")
    # 続きから始められる条件
    tmp.mkdir(parents=True, exist_ok=True)
    g = lambda *a: subprocess.run(["git", *a], cwd=tmp, capture_output=True, text=True, check=True).stdout.strip()
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@example.com"); g("config", "user.name", "t")
    (tmp / "a").write_text("1"); g("add", "a"); g("commit", "-q", "-m", "base"); base = g("rev-parse", "HEAD")
    (tmp / "a").write_text("2"); g("commit", "-q", "-am", "wip"); head = g("rev-parse", "HEAD")
    g("checkout", "-q", "--orphan", "other"); (tmp / "a").write_text("3"); g("add", "a"); g("commit", "-q", "-m", "other"); other = g("rev-parse", "HEAD")
    saved = oncall.ROOT
    try:
        oncall.ROOT = tmp
        good = {"base": base, "head": head, "fix": {"status": "fixed"}, "open": [{"id": "x"}]}
        check(oncall.resume_point({"wip": good}, base) == good, "続きから始められるはずの状態を拒んだ")
        for label, bad in (("基準が違う", dict(good, base=other)), ("残った指摘が無い", dict(good, open=[])),
                           ("報告が無い", dict(good, fix={})), ("commit が無い", dict(good, head="0" * 40)),
                           ("基準の子孫でない", dict(good, head=other)), ("指摘が dict でない", dict(good, open=["x"])),
                           ("head が hash でない", dict(good, head="main; rm -rf /"))):
            check(oncall.resume_point({"wip": bad}, base) is None, f"続きにしてはいけない状態を採った: {label}")
        check(oncall.resume_point({"log": []}, base) is None and oncall.resume_point({"wip": "x"}, base) is None, "wip が無い・壊れている")
        # 差分の無い診断(no_fix_needed)に指摘が付いたまま終わった試行も、続きから(監査指摘)
        nodiff = dict(good, head=base, fix={"status": "no_fix_needed"})
        check(oncall.resume_point({"wip": nodiff}, base) == nodiff, "差分の無い診断の続きを拒んだ")
    finally:
        oncall.ROOT = saved
    # 往復の本体: 例外・時間切れで終わっても、固定できたところまで(kept)が残る。時間の上限は各セッションの timeout に効く
    names = ("run_claude", "run_codex", "freeze", "root_clean", "remote_main", "env_fingerprint", "sh", "ONCALL_LIMIT_MIN", "MAX_ROUNDS")
    saved_fns = {n: getattr(oncall, n) for n in names}
    try:
        timeouts = []
        oncall.root_clean = lambda: True
        oncall.remote_main = lambda: "B" * 40
        oncall.env_fingerprint = lambda: "env"
        oncall.freeze = lambda wt, base, rnd: ("H%039d" % rnd, "diff --git a/scripts/x.py b/scripts/x.py", ["scripts/x.py"])
        oncall.sh = lambda args, cwd, timeout=600: subprocess.CompletedProcess(args, 0, "", "")
        def claude(prompt, schema, cwd, timeout=1800):
            timeouts.append(timeout)
            return {"status": "fixed", "diagnosis": "d"} if "oncall-fix" in str(schema) else {"accepted": [], "refuted": [], "status": "fixed"}
        oncall.run_claude = claude
        # (1) 監査が例外: 1往復目の修正は固定済みなので kept に残り、「監査が終わっていない」が指摘として付く
        def codex_boom(prompt, schema, cwd, timeout=1800):
            raise subprocess.TimeoutExpired("codex", timeout)
        oncall.run_codex = codex_boom
        tr = []
        r = oncall.run_rounds("compose", "2026-09-18", "ctx", tmp, "B" * 40, None, "env", tr)
        check(not r["approved"] and "TimeoutExpired" in r["error"] and r["kept"]["head"] == "H%039d" % 1
              and r["kept"]["fix"].get("status") == "fixed" and [o["id"] for o in r["kept"]["open"]] == ["review-incomplete"],
              f"監査の例外で途中の修正が残らない: {r['error']} {r['kept']}")
        # (2) 監査が reject し続ける: 上限まで回り、最後の指摘が残る。2往復目からは指摘を直す依頼になる
        oncall.MAX_ROUNDS = 3
        oncall.run_codex = lambda prompt, schema, cwd, timeout=1800: {"verdict": "reject", "must_fix": [{"id": "p1", "claim": "c"}], "notes": ""}
        tr = []
        r = oncall.run_rounds("compose", "2026-09-18", "ctx", tmp, "B" * 40, None, "env", tr)
        check(not r["approved"] and not r["error"] and r["kept"]["head"] == "H%039d" % 3 and [o["id"] for o in r["kept"]["open"]] == ["p1"]
              and sum(1 for t in tr if "integrate" in t) == 2, f"reject が続いたときの往復: {r['kept']} {[list(t) for t in tr]}")
        # (3) 指摘が無くなれば終わる(続きから始めた場合も、監査を通るまで approved にならない)
        answers = iter([{"verdict": "reject", "must_fix": [{"id": "p1", "claim": "c"}], "notes": ""}, {"verdict": "approve", "must_fix": [], "notes": ""}])
        oncall.run_codex = lambda prompt, schema, cwd, timeout=1800: next(answers)
        r = oncall.run_rounds("compose", "2026-09-18", "ctx", tmp, "B" * 40,
                              {"base": "B" * 40, "head": "B" * 40, "fix": {"status": "no_fix_needed"}, "open": [{"id": "old"}]}, "env", [])
        check(r["approved"] and r["kept"]["open"] == [], f"指摘が無くなったのに終わらない: {r}")
        # (3a) 発行後でよい指摘(later)は approve を妨げず、往復と試行をまたいで重複なしで集まる
        #      起きる道筋(occurs)の無い later は集めない(「schema 上は可能」の類を後の仕事にしない)
        la = {"id": "l1", "claim": "堅牢化A", "evidence": "e", "occurs": "記事が60本を超えた号(月に1〜2回)"}
        answers = iter([{"verdict": "reject", "must_fix": [{"id": "p1", "claim": "c"}], "later": [la], "notes": ""},
                        {"verdict": "approve", "must_fix": [], "notes": "",
                         "later": [la, {"id": "l2", "claim": "テストB", "evidence": "", "occurs": "毎号"},
                                   {"id": "l9", "claim": "改行が2100個入ったら", "evidence": "schema 上は可能", "occurs": ""}]}])
        oncall.run_codex = lambda prompt, schema, cwd, timeout=1800: next(answers)
        tr = []
        r = oncall.run_rounds("compose", "2026-09-18", "ctx", tmp, "B" * 40,
                              {"base": "B" * 40, "head": "B" * 40, "fix": {"status": "no_fix_needed"}, "open": [{"id": "old"}],
                               "later": [{"id": "l0", "claim": "前の試行の分", "evidence": "", "occurs": "月初の号"}]}, "env", tr)
        check(r["approved"] and [x["claim"] for x in r["later"]] == ["前の試行の分", "堅牢化A", "テストB"], f"later の集まり方: {r['later']}")
        check([x["claim"] for x in oncall.collect_later(tr)] == ["前の試行の分", "堅牢化A", "テストB"], "報告に載せる later が往復の記録から集まらない")
        check("堅牢化A" in oncall.later_text(r["later"]) and "なし" in oncall.later_text([]), "later の報告文")
        # (3b) 続きの checkout が失敗しても、残してあった続きは消えない(監査指摘)
        old_wip = {"base": "B" * 40, "head": "C" * 40, "fix": {"status": "fixed"}, "open": [{"id": "old"}]}
        oncall.sh = lambda args, cwd, timeout=600: subprocess.CompletedProcess(args, 1 if "checkout" in args else 0, "", "boom")
        r = oncall.run_rounds("compose", "2026-09-18", "ctx", tmp, "B" * 40, old_wip, "env", [])
        check(r["error"] and r["kept"] == {"head": "C" * 40, "fix": {"status": "fixed"}, "open": [{"id": "old"}], "later": []}, f"checkout 失敗で続きが消えた: {r['kept']}")
        # (3c) 続きの最初の往復で selfcheck が赤でも、前の指摘と「監査が終わっていない」は残る(監査指摘)
        oncall.sh = lambda args, cwd, timeout=600: subprocess.CompletedProcess(args, 1 if "scripts/selfcheck.py" in args else 0, "赤", "")
        oncall.MAX_ROUNDS = 1
        r = oncall.run_rounds("compose", "2026-09-18", "ctx", tmp, "B" * 40, dict(old_wip, head="B" * 40), "env", [])
        check(sorted(o["id"] for o in r["kept"]["open"]) == ["old", "review-incomplete", "selfcheck"], f"selfcheck 赤で前の指摘が消えた: {r['kept']['open']}")
        oncall.MAX_ROUNDS = 3
        oncall.sh = lambda args, cwd, timeout=600: subprocess.CompletedProcess(args, 0, "", "")
        # (4) 時間の上限: セッションの timeout は残り時間で切られ、残りが足りなければ始めない
        oncall.ONCALL_LIMIT_MIN = 10
        timeouts.clear()
        oncall.run_codex = lambda prompt, schema, cwd, timeout=1800: (timeouts.append(timeout), {"verdict": "reject", "must_fix": [{"id": "p"}], "notes": ""})[1]
        oncall.run_rounds("compose", "2026-09-18", "ctx", tmp, "B" * 40, None, "env", [])
        check(timeouts and max(timeouts) <= 600, f"セッションの timeout が時間の上限を超える: {timeouts}")
        oncall.ONCALL_LIMIT_MIN = 1
        timeouts.clear()
        tr = []
        r = oncall.run_rounds("compose", "2026-09-18", "ctx", tmp, "B" * 40, None, "env", tr)
        check(not timeouts and any("time_up" in t for t in tr), f"残り時間が足りないのにセッションを始めた: {timeouts} {tr}")
    finally:
        for n, v in saved_fns.items():
            setattr(oncall, n, v)
    # 発行後に直す指摘の保管: 同じ指摘は二度積まない、消し込みは追記、壊れた行で止まらない
    saved_b = oncall.BACKLOG
    try:
        oncall.BACKLOG = tmp / "metrics" / "oncall-backlog.jsonl"
        check(oncall.backlog_open() == [], "保管が無いときの一覧")
        items = [{"id": "l1", "claim": "堅牢化A", "evidence": "e", "occurs": "60本を超えた号"}, {"id": "l2", "claim": "テストB", "evidence": "", "occurs": "毎号"},
                 {"id": "x", "claim": "", "occurs": "毎号"}, {"id": "l9", "claim": "改行が2100個入ったら", "evidence": "schema 上は可能", "occurs": ""}]
        new = oncall.backlog_add(items, "2026-09-18", "compose", "a" * 40)
        check(len(new) == 2 and len(oncall.backlog_add(items, "2026-09-19", "compose", "b" * 40)) == 0,
              "同じ指摘を二度積んだ、または空の指摘・起きる道筋の無い指摘を積んだ")
        with open(oncall.BACKLOG, "a", encoding="utf-8") as f:
            f.write("壊れた行\n")
        keys = [r["key"] for r in oncall.backlog_open()]
        check(len(keys) == 2, f"壊れた行で一覧が止まる: {keys}")
        check(oncall.backlog_done([keys[0], "無い"]) == [keys[0]] and [r["key"] for r in oncall.backlog_open()] == [keys[1]], "消し込み")
        left = oncall.backlog_open()[0]
        check(left["claim"] == "テストB" and left["date"] == "2026-09-18" and left["fix_commit"] == "a" * 10 and left["occurs"] == "毎号", f"保管の中身: {left}")
    finally:
        oncall.BACKLOG = saved_b


def test_slugify_format():
    import re
    import planlib
    fullmatch = lambda s: bool(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", s))
    # 区切り(空白・記号・既存ハイフン)が入り混じった主題キーでも連続ハイフンにならないこと。
    # 2026-09-16: 既存ハイフンを残す実装で `joint-...-ex-----------------------28` のような
    # slug が生成され、compose.validate_plan の英小文字ハイフン検証に落ちて計画不成立になった。
    cases = [
        ("joint", "その他 - 10【S1】EX - アイドル - 9/26"),
        ("shiny", "【MSP】- hopeful feathers"),
        ("million", "ランキング - 300 - 470"),
        ("cg", "foo---bar - - -"),
    ]
    taken: set[str] = set()
    for b, k in cases:
        s = planlib.slugify(b, k, taken)
        check(fullmatch(s), f"slugify が英小文字ハイフン形式でない slug を出した: {b} / {k!r} → {s!r}")
    # dedup の番号付与でも(切り詰めが末尾ハイフンに当たっても)連続ハイフンにしないこと
    t2: set[str] = set()
    s1 = planlib.slugify("cg", "a b", t2)
    s2 = planlib.slugify("cg", "a b", t2)
    check(fullmatch(s1) and fullmatch(s2) and s1 != s2, f"dedup で不正な slug: {s1!r} {s2!r}")


def test_escalate_is_module_global():
    # main() 内で `from pipelib import escalate` すると escalate が関数ローカル扱いになり、
    # それより前にある escalate(...) 呼び出しが UnboundLocalError で落ちる(2026-09-16 実測:
    # 計画不成立を当番へ渡す escalate がこれで死に、「想定外のエラー」で停止した)。
    # escalate はモジュール大域から import し、main の局所変数にしないこと。
    check(hasattr(compose, "escalate"), "compose.escalate がモジュール大域に無い")
    check("escalate" not in compose.main.__code__.co_varnames,
          "main() が escalate を局所変数にしている(UnboundLocalError の再来)")


def test_url_alive_verdict():
    """出典 URL の死活確認: サーバがページ不在を明言した 404/410 だけを発行停止の error とし、
    403 の WAF・bot 遮断、401、429、5xx やネットワーク層の失敗は「死活未確認」の警告どまりに
    する(2026-09-20号: amiami の実在ページが 403 を返して発行が止まった)。"""
    import urllib.error
    orig_urlopen, orig_sleep = lint.urllib.request.urlopen, lint.time.sleep
    slept = []
    lint.time.sleep = lambda *a, **k: slept.append(a)  # 再試行の待ちで遅くしない(回数だけ数える)
    try:
        def raising(code):
            def _open(req, timeout=None):
                raise urllib.error.HTTPError(req.full_url, code, f"HTTP {code}", {}, None)
            return _open
        for code in (404, 410):
            lint.urllib.request.urlopen = raising(code)
            slept.clear()
            res = lint.url_alive("https://x.example/gone")
            check(res == (False, f"HTTP {code}", "dead"), f"HTTP {code} を dead(発行停止)と判定しない: {res}")
            check(not slept, f"HTTP {code}(不在の明言)でやり直しの待ちが入った: {len(slept)}回")
        for code in (403, 401, 429, 500, 503):
            lint.urllib.request.urlopen = raising(code)
            slept.clear()
            res = lint.url_alive("https://x.example/blocked")
            check(res == (False, f"HTTP {code}", "blocked"), f"HTTP {code} を blocked(警告)と判定しない: {res}")
            check(len(slept) == 2, f"HTTP {code}(間欠的なことがある)をやり直していない: {len(slept)}回")
        def netfail(req, timeout=None):
            raise OSError("dns")
        lint.urllib.request.urlopen = netfail
        _ok, _d, verdict = lint.url_alive("https://no.such.host/")
        check(not _ok and verdict == "unreachable", f"ネットワーク層の失敗を unreachable としない: {verdict}")

        class Res:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        lint.urllib.request.urlopen = lambda req, timeout=None: Res()
        check(lint.url_alive("https://ok.example/") == (True, "HTTP 200", "alive"), "生存を alive としない")
    finally:
        lint.urllib.request.urlopen, lint.time.sleep = orig_urlopen, orig_sleep


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="imas-test-"))
    test_slugify_format()
    test_escalate_is_module_global()
    test_url_alive_verdict()
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
    test_table_write_and_reload(tmp / "tw")
    test_classify_posts_only(tmp / "cp")
    test_assemble_prompt_shape()
    test_prompts_are_instructions_only()
    test_assemble_judge()
    test_claude_traced(tmp / "ct")
    test_time_budget()
    test_clean_url_and_table()
    test_no_prompt_in_argv()
    test_next_number(tmp / "nn")
    test_dedupe_source_table(tmp / "dd")
    test_tool_path()
    test_oncall_undo_merge()
    test_oncall_rollback_subprocess(tmp / "rs")
    test_oncall_apply_integrate()
    test_oncall_report_text(tmp / "rp")
    test_oncall_restore_on_exception(tmp / "oc")
    test_oncall_fix_until_clean(tmp / "of")
    for f in FAILS:
        print(f"  [FAIL] {f}")
    print(f"test_pipeline: {len(FAILS)} failures")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
