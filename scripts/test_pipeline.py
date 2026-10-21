#!/usr/bin/env python3
"""回帰テスト(selfcheck から呼ばれる。単体でも走る): 監査で見つかった欠陥を二度と通さない。

  python3 scripts/test_pipeline.py

外部サービス・本体の作業ツリー・Git には触らない。全部一時ディレクトリの中で済ませる。
"""
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 本番のコードが書式(schema/)で決めたファイルを書式に合わない形で書いたら、テストでは例外にする(pipelib.write_checked)
os.environ["IMAS_STRICT_WRITES"] = "1"
# テストの一時ファイルは全部1つのディレクトリの下に作り、終わったら消す。子プロセスも同じ置き場(TMPDIR)。/tmp は 3.8GB の tmpfs で、
# 走らせるたびに一時ディレクトリを残して埋まり、監査のテストが容量不足で止まった(2026-10-14。selfcheck・当番の往復でも走る)
TEST_TMP = Path(tempfile.mkdtemp(prefix="imas-test-"))
tempfile.tempdir = str(TEST_TMP)
os.environ["TMPDIR"] = str(TEST_TMP)
atexit.register(shutil.rmtree, TEST_TMP, True)
import assemble
import compose
import lint
import pipelib
import renderlib

FAILS: list[str] = []
# 書式で決めたファイルを、本番のコードが write_checked を通さずに書いたら控える(見張り。全テストの終わりに検める)。
# 書く側の検査を足しても、あとから足した書き込みが素通しなら同じ止まり方(2026-10-14 03:11 の組版停止)をする。
# 素通しを認めるのは、下の理由のある所だけ
STRAY_WRITES: list[str] = []
WRITE_PASSTHROUGH = {
    ("assemble.py", "write_snapshot"): "組み立て途中の号スナップショット(ランキング・誕生日が空)。最後の形は derive が write_checked で書く",
    ("rewrite_past.py", "main"): "校閲を通ったと確かめる前に止まった差し替えを、元の文面に戻す",
    ("rewrite_past.py", "replace_and_review"): "校閲を通らなかった差し替えを、元の文面に戻す",
    ("pipelib.py", "_restore"): "失敗した取引(出典の判定・付け直し・lint)のファイルを、開始時の中身へバイト単位で戻す",
    ("compose.py", "restore_files"): "記事を落としたあとの組み直しが失敗したとき、記事・号・台帳・予約を組み直しの前の中身へバイト単位で戻す",
}


def _watch_governed_writes() -> None:
    """書く口は全部見張る: open(書く・足す・作るの mode)・Path.open・Path.write_text/write_bytes(内部で io.open)・
    os.replace/os.rename の行き先(監査指摘: write_text/write_bytes だけを見ていて、open("w") の迂回を見逃した)。"""
    import builtins
    import io
    scripts = Path(__file__).resolve().parent
    stdlib = Path(os.__file__).resolve().parent

    def note(target) -> None:
        try:
            s = Path(os.fspath(target)).as_posix()
        except TypeError:
            return
        if not any(rx.search(s) for rx, _ in pipelib.SCHEMA_OF_PATH):
            return
        f = sys._getframe(2)
        while f is not None and Path(f.f_code.co_filename).resolve().is_relative_to(stdlib):
            f = f.f_back          # pathlib・io の中は飛ばし、書いた本人のフレームを見る
        if f is None:
            return
        if f.f_code.co_name == "write_atomic" and Path(f.f_code.co_filename).name == "pipelib.py":
            # 差し替えの本体。write_checked から呼ばれたなら書式で検めた後。ほかから書式のファイルへ呼んだなら、呼んだ所を控える
            if f.f_back is not None and f.f_back.f_code.co_name == "write_checked":
                return
            f = f.f_back or f
        fn = Path(f.f_code.co_filename)
        if fn.parent == scripts and fn.name != Path(__file__).name and f.f_code.co_name != "write_checked" \
                and (fn.name, f.f_code.co_name) not in WRITE_PASSTHROUGH:
            STRAY_WRITES.append(f"{fn.name}:{f.f_lineno} {f.f_code.co_name} → {Path(s).name}")

    orig_open = io.open

    def watched_open(file, mode="r", *a, **k):
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            note(file)
        return orig_open(file, mode, *a, **k)

    def moved(orig):
        def move(src, dst, *a, **k):
            note(dst)
            return orig(src, dst, *a, **k)
        return move
    io.open = builtins.open = watched_open
    os.replace, os.rename = moved(os.replace), moved(os.rename)


_watch_governed_writes()


def article_fm(slug: str = "k", edition: str = "2026-10-03", **kw) -> str:
    """記事の frontmatter(書式 article.schema の必須の欄を満たす最小の形)の YAML 本文。kw で欄を足す・差し替える。"""
    import yaml
    fm = {"slug": slug, "edition": edition, "brand": "cg", "src": "未確認", "rank": "small", "title": "t", "lede": "l",
          "sources": [{"label": "l1", "url": "https://a.example/1", "type": "未確認"}], "corrected": False, "corrections": []}
    fm.update(kw)
    return yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)


def cand_row(i: str, url: str, **kw) -> dict:
    """素材の行(書式 candidates.schema の必須の欄を満たす最小の形)。kw で欄を足す・差し替える。"""
    return {"id": i, "title": kw.pop("title", i), "brand": kw.pop("brand", "cg"), "source_type": kw.pop("source_type", "公式"),
            "url": url, "found_at": kw.pop("found_at", "2026-10-01T00:00:00+09:00"), "dedup_key": kw.pop("dedup_key", f"k-{i}"),
            "verify": kw.pop("verify", "confirmed"), **kw}


def sched_row(i: str, reserved_on: str, **kw) -> dict:
    """続報予約の行(書式 scheduled.schema の必須の欄を満たす最小の形)。本番のコードは書式で検めてから書くので、テストの下準備も
    書式に合う形にする(pipelib.write_checked)。"""
    return {"id": i, "dedup_key": kw.pop("dedup_key", f"k-{i}"), "brand": kw.pop("brand", "cg"), "subject": kw.pop("subject", i),
            "kind": kw.pop("kind", "その他"), "reserved_on": reserved_on, "facts": kw.pop("facts", [f"{i} の事実"]), **kw}
# 監査(Sol)の承認の偽の答えに付ける「実際に走らせた記録」。これの無い承認は承認にならない(oncall.audit_ran_checks)
AUDIT_RAN = [{"command": "python3 scripts/test_pipeline.py", "exit_code": 0, "result": "test_pipeline: 0 failures"},
             {"command": "python3 scripts/selfcheck.py", "exit_code": 0, "result": "selfcheck: 0 errors"},
             {"command": "python3 scripts/lint.py --base origin/main", "exit_code": 0, "result": "lint: 0 errors"}]

# **テストは本物の Discord に絶対に送らない。**通知の経路を差し替え忘れたテストが本物の webhook に届いた
# (実測 2026-09-23: 当番の修正報告の見本 3 通が編集長の Discord に出た)。webhook の URL を最初に消し、
# さらに urlopen を塞ぐ(通知を検めるテストは、自分で偽の urlopen を差し込む)
pipelib.ENV.pop("DISCORD_WEBHOOK_URL", None)
os.environ.pop("DISCORD_WEBHOOK_URL", None)


def _no_network(*a, **k):
    raise AssertionError("テストから本物の通信を試みた(urlopen)。偽物に差し替えること")


pipelib.urllib.request.urlopen = _no_network


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
    check(C(good, fetched={"https://z.example/9"}) == [], f"new_facts 付きの出力が落ちた: {C(good, fetched={'https://z.example/9'})}")
    # 素材に無い URL(執筆が見つけた出典)は、fetch_page.py で本文を取れたページなら許す。中身の一致の確認は校閲(項目3)
    # (読めたかの検査は test_check_output_unread_pages)
    check(C(dict(OK, sources=OK["sources"] + [{"url": "https://q.example", "label": "q"}]), fetched=["https://q.example/"]) == [],
          "読めた素材の外の出典 URL を機械が落とした")
    check(C(dict(OK, sources=OK["sources"][:2] + [{"url": "https://c.example/3", "label": "#タグ_付き ID"}])) == [], "label の # や _ を落とした")
    check(any("Markdown" in p for p in C(dict(OK, sources=OK["sources"][:2] + [{"url": "https://c.example/3", "label": "[リンク](x)"}]))),
          "label の Markdown リンクが通った")
    # label の制御文字・不可視文字は差し戻さず、書き出しで取り除く(出典ページの題名のゼロ幅空白を執筆が写す。2026-09-19)
    check(C(dict(OK, sources=OK["sources"][:2] + [{"url": "https://c.example/3", "label": "お​知らせ ~ for\x044"}])) == [], "label の不可視文字で差し戻した")
    check(renderlib.clean_label("お​知らせ\n ~  for\x044  ") == "お知らせ ~ for4", f"clean_label: {renderlib.clean_label('お​知らせ ~ for\x044')!r}")
    # label の上限は執筆 schema と書き出しで同じ値(80 字だった頃、告知タイトルが切れて校閲 R16 が何巡もブロックし記事が落ちた。2026-09-26)
    schema_label = json.loads((pipelib.ROOT / "schema" / "article-out.schema.json").read_text(encoding="utf-8"))["properties"]["sources"]["items"]["properties"]["label"]
    check(schema_label.get("maxLength") == renderlib.LABEL_MAX >= 200, f"label の上限: schema {schema_label.get('maxLength')} / 書き出し {renderlib.LABEL_MAX}")
    check(len(renderlib.clean_label("あ" * 300)) == renderlib.LABEL_MAX, "clean_label の上限が LABEL_MAX でない")
    # 検査は取り除いたあとの値で(不可視文字で割った Markdown 記号・空になる label を通さない。監査指摘)
    check(any("Markdown" in p for p in C(dict(OK, sources=OK["sources"][:2] + [{"url": "https://c.example/3", "label": "[公式]​(https://evil)"}]))),
          "不可視文字で割った Markdown リンクの label が通った")
    check(any("空" in p for p in C(dict(OK, sources=OK["sources"][:2] + [{"url": "https://c.example/3", "label": "​\x04"}]))), "空になる label が通った")
    # 発行前の検査(lint)と同じ基準: `__` `**` も通さない(2026-10-08: 商品名の `__` が検算を通り、lint で赤になった)
    check(any("Markdown" in p for p in C(dict(OK, sources=OK["sources"][:2] + [{"url": "https://c.example/3", "label": "コレクション__ブロマイド228"}]))),
          "lint が赤にする label(__)が検算を通った")
    # 見出し・リードも、発行前の検査(lint.check_plain_text)が赤にするものは検算で差し戻す(2026-10-14 監査指摘: `**販売開始**` の見出しが
    # 検算を通って記事に採られ、lint が赤にして組み直し・書き直し・記事を外すへ進んだ)。基準は同じ定義(実際に両方を走らせて比べる)
    import contextlib, io
    import lint as _lint
    for v in ("**販売開始**", "__限定__", "[公式](https://a.example)", "# 見出し", "`code`", "  ## 前に空白", "#タグ_付き ID", "販売開始", "[A] 告知"):
        for key in ("title", "lede"):
            rep = _lint.Report()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                _lint.check_plain_text(rep, "-", key, v.strip())
            flagged = [p for p in C(dict(OK, **{key: v})) if "Markdown" in p]
            check(bool(flagged) == (rep.errors > 0), f"{key}={v!r}: 検算 {flagged} / lint {rep.errors} 件(基準が割れている)")
    check(any("見出しに Markdown" in p for p in C(dict(OK, title="**販売開始**"))) and any("リードに Markdown" in p for p in C(dict(OK, lede="**販売開始**"))),
          "見出し・リードの Markdown 記法が検算を通った")
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
    # tags の上限は作品の正式名称が丸ごと入る長さ(24字だった頃、28字の作品名『ACE COMBAT 8: WINGS OF THEVE』が
    # タグに入らず、書き手が24字で切って壊れたタグ(…WINGS OFゆ?)を作り、校閲 R16 が何巡もブロックして落ちた。2026-09-29)
    schema_tags = json.loads((pipelib.ROOT / "schema" / "article-out.schema.json").read_text(encoding="utf-8"))["properties"]["tags"]["items"]
    check(len("ACE COMBAT 8: WINGS OF THEVE") <= schema_tags.get("maxLength", 0),
          f"作品の正式名称(28字)が tags 上限 {schema_tags.get('maxLength')} 字に入らない")
    check(any("見出し" in p for p in C(dict(OK, title_fact_ids=[]))), "見出しの根拠無しが通った")
    check(len(C({"status": "decline", "decline_code": "", "decline_detail": ""})) == 2, "理由の無い decline が通った")
    check(C({"status": "decline", "decline_code": "NOT_NEWS", "decline_detail": "x"}) == [], "正当な decline が落ちた")
    check(any("decline_code" in p for p in C(dict(OK, decline_code="OTHER"))), "ok なのに decline_code ありが通った")


def test_check_output_unread_pages():
    """執筆の根拠の URL(new_facts・sources)は、素材の url か、fetch_page.py が本文を取れたページ(セッションの記録)だけ。2026-10-14 の rule_bench:
    執筆が fetch_page.py の取れなかった報道を Web 検索で読んで期間を断定し、綴りの誤った URL(news.denfamicogamer.jp)を new_facts と出典に
    書いた答えが検算を通った(校閲は fetch_page.py だけで読むので確かめられない)。素材・読んだページと書き方が違うだけ(末尾の /・scheme・
    共有用のクエリ・%エンコード・リダイレクトの行き先)の正しい答えは落とさない"""
    import copy
    _, fb = renderlib.materials_with_ids(MATS)
    C = lambda o, fetched=(): renderlib.check_output(copy.deepcopy(o), fb, MATS, fetched=fetched)
    unread = lambda ps: [p for p in ps if "fetch_page.py で本文を取れていない" in p]
    press, typo = "https://news.denfaminicogamer.jp/news/261009y", "https://news.denfamicogamer.jp/news/261009y"
    ans = dict(OK, new_facts=[{"id": "N1", "text": "10月15日3:59まで", "url": press}], blocks=[{"markdown": "x", "fact_ids": ["F1", "F3", "F4", "N1"]}],
               sources=OK["sources"] + [{"url": press, "label": "電ファミニコゲーマー"}])
    check(unread(C(ans)), "fetch_page.py で読めていない素材の外のページを根拠にした答えが検算を通った")
    check(C(ans, fetched={press}) == [], f"fetch_page.py で読めたページを根拠にした答えを落とした: {C(ans, fetched={press})}")
    # 綴りの誤った URL(実在しないページ)は、正しい綴りのページを読んでいても通さない。new_facts だけ・sources だけでも
    only_n = dict(ans, new_facts=[dict(ans["new_facts"][0], url=typo)])
    only_s = dict(ans, sources=OK["sources"] + [{"url": typo, "label": "電ファミニコゲーマー"}])
    check(unread(C(only_n, fetched={press})) and unread(C(only_s, fetched={press})), "綴りの誤った URL を根拠にした答えが検算を通った")
    check(typo in " ".join(unread(C(only_s, fetched={press}))), "差し戻しの理由に、読めていない URL が無い(書き手が直せない)")
    # 書き方が違うだけの同じページは落とさない(共有・計測用のクエリは、片側だけでも、両側で値・種類が違っても同じページ)
    for cited, pool in ((press + "/", {press}), ("http://news.denfaminicogamer.jp/news/261009y", {press}),
                        ("https://www.youtube.com/watch?v=abc", {"https://youtube.com/watch?v=abc&si=xyz"}),
                        ("https://www.youtube.com/watch?v=abc&si=AAA&t=30", {"https://youtube.com/watch?si=BBB&v=abc"}),
                        ("https://shop.example.jp/item?id=1&utm_source=cite", {"https://shop.example.jp/item?utm_source=fetch&id=1"}),
                        ("https://shop.example.jp/item?id=1&fbclid=x", {"https://shop.example.jp/item?gclid=y&id=1"}),
                        ("https://example.jp/商品/1", {"https://example.jp/%E5%95%86%E5%93%81/1"}),
                        ("https://www.lantis.jp/release_1.html", {"https://lnk.to/1", "https://www.lantis.jp/release_1.html"}),
                        ("https://twitter.com/imas/status/1", {"https://x.com/imas/status/1?s=20"}),
                        ("https://x.com/imas/status/1?s=46&t=abc", {"https://x.com/imas/status/1?s=20"}),
                        ("https://example.jp:443/a", {"https://example.jp/a"}), ("http://example.jp:80/a", {"https://example.jp/a"}),
                        ("https://example.jp/a%2fb", {"https://example.jp/a%2Fb"}), ("https://example.jp/%7Euser", {"https://example.jp/~user"}),
                        ("https://example.jp/s?q=商品", {"https://example.jp/s?q=%E5%95%86%E5%93%81"}),
                        ("https://example.jp/s?q=a+b", {"https://example.jp/s?q=a%20b"})):
        a = dict(ans, new_facts=[dict(ans["new_facts"][0], url=cited)], sources=OK["sources"] + [{"url": cited, "label": "l"}])
        check(C(a, fetched=pool) == [], f"同じページの書き方の違いで落とした: {cited} / {sorted(pool)}: {C(a, fetched=pool)}")
    # 素材の url は記録が無くても根拠にできる(FETCH_FAILED の素材・X の投稿も出典には書く)。書き方が違うだけでも同じ
    mat = dict(OK, sources=[{"url": "https://a.example/1/?utm_source=x", "label": "x"}, {"url": "http://b.example/2", "label": "y"}],
               new_facts=[{"id": "N1", "text": "t", "url": "https://c.example/3/"}], blocks=[{"markdown": "x", "fact_ids": ["F1", "N1"]}])
    check(C(mat) == [], f"素材の url(書き方違い)を根拠にした答えを落とした: {C(mat)}")
    # ページを選ぶクエリが違う・片方にしか無い・個数が違う、別のパス・ポート・%2F は別のページ(読んだ /item で、読んでいない別の商品
    # /item?id=2 を通さない。2026-10-14 監査指摘)。素材と同じパスの別のページも同じ
    for cited, pool in (("https://www.youtube.com/watch?v=B", {"https://www.youtube.com/watch?v=A"}),
                        ("https://shop.example.jp/item?id=2", {"https://shop.example.jp/item"}),
                        ("https://shop.example.jp/item", {"https://shop.example.jp/item?id=1"}),
                        ("https://shop.example.jp/item?id=1&id=2", {"https://shop.example.jp/item?id=1"}),
                        ("https://shop.example.jp/item?id=1&s=20", {"https://shop.example.jp/item?id=1"}),
                        ("https://a.example/1?id=2", set()), ("https://example.jp:8080/a", {"https://example.jp/a"}),
                        # 既定のポートは scheme ごと: http の 443・https の 80 は既定ではない(2026-10-14 監査指摘)
                        ("http://shop.example.jp:443/item", {"http://shop.example.jp:80/item"}),
                        ("https://shop.example.jp:80/item", {"https://shop.example.jp/item"}),
                        ("https://example.jp/a%2Fb", {"https://example.jp/a/b"}), ("https://example.jp/a/b", {"https://example.jp/a%2Fb"}),
                        # 二重の %エンコード(%252F は「%2F」という文字列)・不正な UTF-8 の別のバイトは別のページ(2026-10-14 監査指摘)
                        ("https://example.jp/a%252Fb", {"https://example.jp/a%2Fb"}), ("https://example.jp/a%2Fb", {"https://example.jp/a%252Fb"}),
                        ("https://example.jp/%FF", {"https://example.jp/%FE"}), ("https://example.jp/s?q=%FF", {"https://example.jp/s?q=%FE"}),
                        ("https://example.jp/s?q=a%26b", {"https://example.jp/s?q=a&b"}),
                        ("https://a.example/12", {"https://a.example/1"}), ("https://a.example.evil/1", set())):
        a = dict(ans, new_facts=[dict(ans["new_facts"][0], url=cited)], sources=OK["sources"])
        check(unread(C(a, fetched=pool)), f"別のページを読めたページ・素材として通した: {cited} / {sorted(pool)}")
    # 見送りは根拠の URL を持たない。記録の無い答え(記録を始める前の答えを読み直す道具が fetched=None を渡す)は読めたかを見ない
    check(C({"status": "decline", "decline_code": "NOT_NEWS", "decline_detail": "x"}) == [], "見送りを根拠の URL で落とした")
    check(C(ans, fetched=None) == [], "記録の無い答え(fetched=None)で、読めたかを見た")


def test_render_and_length():
    _, fb = renderlib.materials_with_ids(MATS)
    good = dict(OK, new_facts=[{"id": "N1", "text": "9月25日発売", "url": "https://a.example/1"}],
                blocks=[{"markdown": "本文", "fact_ids": ["F1", "N1"]}],
                sources=[{"url": "https://a.example/1", "label": "ポータル「お​知らせ」"}])
    # 書く先は docs/_posts の下(本番と同じ置き場。書式の検査と見張りが効く。監査指摘)
    p = Path(tempfile.mkdtemp()) / "docs" / "_posts" / "2026-09-12-x.md"
    p.parent.mkdir(parents=True)
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
    p = Path(tempfile.mkdtemp()) / "docs" / "_posts" / "2026-09-12-x.md"
    p.parent.mkdir(parents=True)
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
    # 中見出しと本文を空行で1 block に束ねた稿は、機械が見出しの block と本文の block に分けて通す
    # (2026-10-08: 書き直しが「## 店名\n\n- 在庫…」を1 block で返し、「複数段落」で2巡戻されて記事が落ちた)
    hb = dict(OK, blocks=[{"markdown": "## 会場\n\n" + OK["blocks"][0]["markdown"], "fact_ids": list(OK["blocks"][0]["fact_ids"])}] + OK["blocks"][1:])
    check(renderlib.check_output(hb, fb, MATS) == [], f"中見出しと本文を束ねた block が落ちた: {renderlib.check_output(dict(hb), fb, MATS)}")
    check(hb["blocks"][0] == {"markdown": "## 会場", "fact_ids": []} and hb["blocks"][1]["fact_ids"] == OK["blocks"][0]["fact_ids"],
          f"中見出しの分け方が違う: {hb['blocks'][:2]}")
    check(any("複数段落" in p for p in renderlib.check_output(dict(OK, blocks=[{"markdown": "## 会場\n\nA\n\nB", "fact_ids": ["F1", "F3", "F4"]}]), fb, MATS)),
          "中見出しの後の複数段落が通った")
    # 地の文と箇条書きを空行で1 block に束ねた稿も、空行なしと同じ境目で分けて通す(地の文が2つあれば戻す)
    # (2026-10-10: 書き直しが「品目は次の通り。」+空行+箇条書きを1 block で返し、「複数段落」で戻されて指摘が残った)
    pl = dict(OK, blocks=[{"markdown": "品目は次の通り。\n\n- A\n- B", "fact_ids": list(OK["blocks"][0]["fact_ids"])}] + OK["blocks"][1:])
    check(renderlib.check_output(pl, fb, MATS) == [], f"地の文と箇条書きを空行で束ねた block が落ちた: {renderlib.check_output(dict(pl), fb, MATS)}")
    check([b["markdown"] for b in pl["blocks"][:2]] == ["品目は次の通り。", "- A\n- B"]
          and pl["blocks"][1]["fact_ids"] == OK["blocks"][0]["fact_ids"], f"地の文と箇条書きの分け方が違う: {pl['blocks'][:2]}")
    hl = dict(OK, blocks=[{"markdown": "## DAY1\n- 曲A\n- 曲B\n\n未配信曲は含まない。", "fact_ids": ["F1", "F3", "F4"]}])
    check(renderlib.check_output(hl, fb, MATS) == [], f"見出し+箇条書きと注記を空行で束ねた block が落ちた: {hl['blocks']}")
    check(any("複数段落" in p for p in renderlib.check_output(dict(OK, blocks=[{"markdown": "A\n\n- x\n\nB", "fact_ids": ["F1", "F3", "F4"]}]), fb, MATS)),
          "箇条書きをはさんだ地の文2段落が通った")
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
    # 中見出し(根拠の控えが無い)と本文を1 block に束ねて返した稿も、検算が分けたあとは未指摘段落が同じと見なされる
    # (2026-10-08 のくじ在庫記事: 書き直し2巡目が「1 block に複数段落」で戻され、校閲ブロックで落ちた)
    hb_body = "## 店A\n\n- 在庫あり <!-- F3 -->\n\n価格は三千円である。 <!-- F1 -->"
    ah = dict(a3, blocks=[{"markdown": "## 店A\n\n- 在庫あり", "fact_ids": ["F3"]}, {"markdown": "価格は改めて三千円と告知された。", "fact_ids": ["F1"]}])
    _, fbh = renderlib.materials_with_ids(MATS)
    ph = renderlib.check_output(ah, fbh, MATS) + R(ah, b=hb_body)
    check(ph == [], f"中見出しと本文を束ねただけの書き直しが落ちた: {ph}")
    # 別々の段落から抜いた文を空白でつないだ quote は、両方の段落に掛かる(2026-10-10 million-14th-live-goods)
    two = "DAY1のグッズはステッカー400円。DAY1の対象は甲。 <!-- F1 -->\n\nDAY2のグッズはステッカー400円。DAY2の対象は乙。 <!-- F3 -->"
    iss2 = [{"issue_id": "I1", "rule_id": "R2", "repair": "drop_claim",
             "quote": "DAY1のグッズはステッカー400円。 DAY2のグッズはステッカー400円。"}]
    fix2 = dict(OK, addressed_issue_ids=["I1"], blocks=[{"markdown": "DAY1の対象は甲。", "fact_ids": ["F1"]},
                                                        {"markdown": "DAY2の対象は乙。", "fact_ids": ["F3"]}])
    check(compose.revise_check(fix2, iss2, old_fm, two) == [], f"段落をまたぐ quote の両段落を直した稿が落ちた: {compose.revise_check(fix2, iss2, old_fm, two)}")
    iss2b = [dict(iss2[0], quote="DAY1のグッズはステッカー400円。")]
    check(any("段落" in p for p in compose.revise_check(fix2, iss2b, old_fm, two)), "quote の無い段落の改変が通った")
    # R2(確かめられない N)の id は、指摘の外の段落からも字面を変えずに外してよい(2026-10-10 joint-dere-miri-fureaka)
    nb = "出演は甲と乙。 <!-- F3 N1 -->\n\nフォームは必須の1行入力欄。 <!-- F1 N1 -->"
    iss_n = [{"issue_id": "I1", "rule_id": "R2", "repair": "drop_claim", "quote": "フォームは必須の1行入力欄", "fact_ids": ["N1"]}]
    fix_n = dict(OK, addressed_issue_ids=["I1"], blocks=[{"markdown": "出演は甲と乙。", "fact_ids": ["F3"]},
                                                         {"markdown": "フォームから送る。", "fact_ids": ["F1"]}])
    check(compose.revise_check(fix_n, iss_n, old_fm, nb) == [], f"R2 の N を全段落から外した稿が落ちた: {compose.revise_check(fix_n, iss_n, old_fm, nb)}")
    fix_n2 = dict(fix_n, blocks=[{"markdown": "出演は甲と乙。", "fact_ids": ["F4"]}, fix_n["blocks"][1]])
    check(any("根拠 id" in p for p in compose.revise_check(fix_n2, iss_n, old_fm, nb)), "R2 の外の根拠 id の付け替えが通った")
    # 「直せなかった指摘」は同じ箇所への指摘だけ(同じ規則の別の記述は、前の巡の見落としで、書き手の失敗ではない)
    # (2026-10-10 shiny-ops-roundup: 1巡目と2巡目で R1 の対象が違うのに「2巡目まで残った」と通知した)
    prev = ["STEP3の最後の1枚はSSR以上確定、STEP4は限定SSR確定。"]
    check(not compose.same_issue_left({"quote": "投票企画で選ばれた「灯織＆夏葉＆はるき」がコミュに登場する"}, prev), "別の記述への指摘を残ったと数えた")
    check(compose.same_issue_left({"quote": "STEP4は限定SSR確定"}, prev), "同じ記述への指摘を残ったと数えない")
    check(compose.same_issue_left({"quote": ""}, prev) and not compose.same_issue_left({"quote": "x"}, None), "quote の無い指摘・前の巡に無い規則の扱い")
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
    check(any("出典を外した" in p for p in R(dict(a3, sources=OK["sources"][:2]))), "出典を黙って外したのが通った")
    # add_source の提案が無くても、既存の出典を全て残して素材裏付けの出典を「足す」のは通る(出典隠し R3 を執筆の
    # 判断で直せる。決めるのは執筆=revise-article.md。実在・一致は次の巡の校閲。実測 2026-09-30 joint-cg-million)
    added = dict(a3, sources=OK["sources"] + [{"url": "https://d.example/4", "label": "追加した一次情報"}])
    check(R(added) == [], f"出典を足しただけの稿が落ちた: {R(added)}")
    # ただし「差し替え」(1つ外して1つ足す)は、drop_source の指摘が無いかぎり止める(黙って弱い出典に付け替える手)
    swapped = dict(a3, sources=OK["sources"][:2] + [{"url": "https://d.example/4", "label": "差し替え"}])
    check(any("出典を外した" in p for p in R(swapped)), "出典の無断差し替えが通った")
    iss_add = [{"issue_id": "I1", "rule_id": "R2", "repair": "add_source", "quote": ""}]
    check(any("出典を外した" in p for p in R(dict(a3, addressed_issue_ids=["I1"], sources=OK["sources"][:2]), iss_add)),
          "add_source で出典を外したのが通った")
    iss_lint = [{"issue_id": "I1", "rule_id": "LINT", "repair": "rewrite_claim", "quote": ""}]
    check(R(dict(a4, addressed_issue_ids=["I1"], title="x"), iss_lint) == [], "LINT 修正に外の検査が掛かった")
    # 箇条書きを1 block にまとめた旧稿を、項目ごとに別 block で返し、指摘の項目だけ落とす(監査指摘)
    old_list = "見出し段落。 <!-- F1 -->\n\n- 項目アルファ千円\n- 項目ベータ二千円\n- 項目ガンマ三千円 <!-- F2 -->"
    iss_li = [{"issue_id": "I1", "rule_id": "R7", "repair": "drop_claim", "quote": "項目ベータ二千円"}]
    keep = ["- 項目アルファ千円", "- 項目ガンマ三千円"]
    li_ok = dict(OK, addressed_issue_ids=["I1"], blocks=[{"markdown": "見出し段落。", "fact_ids": ["F1"]}]
                 + [{"markdown": m, "fact_ids": ["F2"]} for m in keep])
    check(compose.revise_check(li_ok, iss_li, old_fm, old_list) == [],
          f"項目ごとに割って対象だけ消した稿が落ちた: {compose.revise_check(li_ok, iss_li, old_fm, old_list)}")
    li_bad = dict(li_ok, blocks=[{"markdown": "見出し段落。", "fact_ids": ["F1"]}, {"markdown": "- 項目ガンマ三千円", "fact_ids": ["F2"]}])
    check(any("消した" in p for p in compose.revise_check(li_bad, iss_li, old_fm, old_list)),
          "未指摘の箇条書き項目の削除が通った")
    # 校閲 quote が本文に無い外側の鉤括弧「…」で囲まれていても、指摘段落は「触った」と認めて局所修正を通す(監査指摘)
    old_q = "開催記念グッズは新ソロ曲デザインで受注する。 <!-- F1 -->\n\n配信は10月に始まる。 <!-- F3 -->"
    iss_q = [{"issue_id": "I1", "rule_id": "R7", "repair": "rewrite_claim", "quote": "「開催記念グッズは新ソロ曲デザインで受注する。」"}]
    q_ok = dict(OK, addressed_issue_ids=["I1"], blocks=[{"markdown": "開催記念グッズは11月に受注する。", "fact_ids": ["F1"]},
                                                        {"markdown": "配信は10月に始まる。", "fact_ids": ["F3"]}])
    check(compose.revise_check(q_ok, iss_q, old_fm, old_q) == [],
          f"鉤括弧付き quote で指摘段落だけ直した稿が落ちた: {compose.revise_check(q_ok, iss_q, old_fm, old_q)}")
    q_bad = dict(q_ok, blocks=[{"markdown": "開催記念グッズは11月に受注する。", "fact_ids": ["F1"]},
                               {"markdown": "配信は11月に始まる。", "fact_ids": ["F3"]}])
    check(any("段落" in p for p in compose.revise_check(q_bad, iss_q, old_fm, old_q)),
          "鉤括弧付き quote でも未指摘段落の改変は落ちない")
    # 校閲が該当箇所を複数まとめ、見出し・リード・本文を「…」で囲み（見出し）等の但し書きと読点でつないだ1つの
    # quote で返したとき、その該当箇所を全部直した稿を通す(実測 2026-10-02 gaku-kuramoto R1「日々、発見的ステップ！」。
    # 束ねた quote はどの一つの欄にも丸ごと収まらず、quote ⊂ 欄 だけ見ると見出し・リード・本文を全部「指摘の外」と
    # 誤認して、正しい書き直しを検算が戻し、同じ R1 が次の巡に残って契約の欠陥として当番に上がった)
    fm_multi = dict(old_fm, title="倉本千奈「日々、発見的ステップ！」フィギュア化",
                    lede="学マスの倉本千奈「日々、発見的ステップ！」が、EUSUN製のスケールフィギュアとして立体化される。")
    body_multi = "学マスの倉本千奈「日々、発見的ステップ！」のスケールフィギュア化が決定した。 <!-- F1 -->\n\n製品の仕様、予約期間、価格などは後日発表される。 <!-- F3 -->"
    iss_multi = [{"issue_id": "I1", "rule_id": "R1", "repair": "drop_claim",
                  "quote": "「倉本千奈「日々、発見的ステップ！」フィギュア化」（見出し）、"
                           "「学マスの倉本千奈「日々、発見的ステップ！」が、EUSUN製のスケールフィギュアとして立体化される。」（リード）、"
                           "「学マスの倉本千奈「日々、発見的ステップ！」のスケールフィギュア化が決定した。」（本文）"}]
    multi_ok = dict(OK, addressed_issue_ids=["I1"], title="倉本千奈のスケールフィギュア化",
                    lede="倉本千奈が、EUSUN製のスケールフィギュアとして立体化される。",
                    blocks=[{"markdown": "倉本千奈のスケールフィギュア化が決定した。", "fact_ids": ["F1"]},
                            {"markdown": "製品の仕様、予約期間、価格などは後日発表される。", "fact_ids": ["F3"]}])
    check(compose.revise_check(multi_ok, iss_multi, fm_multi, body_multi) == [],
          f"束ねた quote の該当箇所(見出し・リード・本文)を全部直した稿が落ちた: {compose.revise_check(multi_ok, iss_multi, fm_multi, body_multi)}")
    # 束ねた quote でも、該当箇所に無い段落の無断改変は従来どおり止める
    multi_bad = dict(multi_ok, blocks=[{"markdown": "倉本千奈のスケールフィギュア化が決定した。", "fact_ids": ["F1"]},
                                       {"markdown": "製品は限定生産で即日完売した。", "fact_ids": ["F3"]}])
    check(any("段落" in p for p in compose.revise_check(multi_bad, iss_multi, fm_multi, body_multi)),
          "束ねた quote で該当外段落の改変が通った")
    # 束ねた quote の表記は A「…」(欄) だけでなく、B 欄「…」(実測 review-2026-09-21)・C 欄：…(実測
    # review-2026-09-25。欄名も「見出し/タイトル」で揺れる)がある。どの表記でも該当3欄を直した稿を通す(監査指摘 MF-1)
    fm_b = dict(old_fm, title="SideMプロミ2027、会員先行は10月4日まで", lede="10月4日23時59分まで申し込める")
    body_b = "受付期間は2026年9月12日21時から10月4日23時59分まで。 <!-- F22 -->\n\n別の段落はそのまま。 <!-- F3 -->"
    iss_b = [{"issue_id": "I1", "rule_id": "R1", "repair": "rewrite_claim",
              "quote": "見出し「SideMプロミ2027、会員先行は10月4日まで」(根拠: F9, F10)、"
                       "リード「10月4日23時59分まで申し込める」(根拠: F42)、"
                       "本文「受付期間は2026年9月12日21時から10月4日23時59分まで。」(根拠: F22)"}]
    b_ok = dict(OK, addressed_issue_ids=["I1"], title="SideMプロミ2027の会員先行受付", lede="申込は10月4日まで受け付ける",
                blocks=[{"markdown": "受付は10月4日で締め切られる。", "fact_ids": ["F22"]},
                        {"markdown": "別の段落はそのまま。", "fact_ids": ["F3"]}])
    check(compose.revise_check(b_ok, iss_b, fm_b, body_b) == [],
          f"B 表記(欄「…」)の束ね quote の3欄を直した稿が落ちた: {compose.revise_check(b_ok, iss_b, fm_b, body_b)}")
    b_bad = dict(b_ok, blocks=[{"markdown": "受付は10月4日で締め切られる。", "fact_ids": ["F22"]},
                              {"markdown": "別の段落を無断で書き換えた。", "fact_ids": ["F3"]}])
    check(any("段落" in p for p in compose.revise_check(b_bad, iss_b, fm_b, body_b)),
          "B 表記の束ね quote で該当外段落の改変が通った")
    fm_c = dict(old_fm, title="ウィークエンドシトロン、3日間入店制限", lede="店舗では9月25日から27日まで終日入店制限を実施する")
    body_c = "店舗では9月25日から27日までの3日間、終日入店制限を実施する。 <!-- F1 -->\n\n別段落はそのまま。 <!-- F3 -->"
    iss_c = [{"issue_id": "I1", "rule_id": "R1", "repair": "rewrite_claim",
              "quote": "タイトル：ウィークエンドシトロン、3日間入店制限\n"
                       "リード：店舗では9月25日から27日まで終日入店制限を実施する\n"
                       "本文：店舗では9月25日から27日までの3日間、終日入店制限を実施する。"}]
    c_ok = dict(OK, addressed_issue_ids=["I1"], title="シトロン店舗、3日間の入店制限", lede="9月25日から27日まで入店を制限する",
                blocks=[{"markdown": "25日から27日まで終日の入店制限を実施する。", "fact_ids": ["F1"]},
                        {"markdown": "別段落はそのまま。", "fact_ids": ["F3"]}])
    check(compose.revise_check(c_ok, iss_c, fm_c, body_c) == [],
          f"C 表記(タイトル：…)の束ね quote の3欄を直した稿が落ちた: {compose.revise_check(c_ok, iss_c, fm_c, body_c)}")
    c_bad = dict(c_ok, blocks=[{"markdown": "25日から27日まで終日の入店制限を実施する。", "fact_ids": ["F1"]},
                              {"markdown": "別段落を無断で書き換えた。", "fact_ids": ["F3"]}])
    check(any("段落" in p for p in compose.revise_check(c_bad, iss_c, fm_c, body_c)),
          "C 表記の束ね quote で該当外段落の改変が通った")
    # 本文だけの長い quote が、リード全文を偶然内包していても、逆向き一致は欄名ラベルの区画に限る。
    # ラベルの無い本文 quote でリード(欄外)の改変を通してはいけない(監査指摘 MF-1。既存 docs/_posts で
    # リード全文が本文に含まれる記事を20/1301件実測。欄共通の逆向き包含だと指摘外のリード改変が検算を抜ける)
    fm_incl = dict(old_fm, lede="新商品の予約受付が始まった。")
    body_incl = "新商品の予約受付が始まった。価格は3000円。 <!-- F1 -->\n\n別の段落。 <!-- F3 -->"
    iss_incl = [{"issue_id": "I1", "rule_id": "R1", "repair": "drop_claim", "quote": "新商品の予約受付が始まった。価格は3000円。"}]
    incl_bad = dict(OK, addressed_issue_ids=["I1"], lede="全く無関係なリードへ改変した。",
                    blocks=[{"markdown": "価格は改めて3000円と告知された。", "fact_ids": ["F1"]},
                            {"markdown": "別の段落。", "fact_ids": ["F3"]}])
    check(any("リード" in p for p in compose.revise_check(incl_bad, iss_incl, fm_incl, body_incl)),
          "本文 quote がリード全文を含むとき、指摘外のリード改変が通った")
    # 同じ入力で、本文(指摘欄)だけ直した稿はそのまま通る(逆向きを欄名で絞っても局所修正は落とさない)
    incl_ok = dict(incl_bad, lede="新商品の予約受付が始まった。")
    check(compose.revise_check(incl_ok, iss_incl, fm_incl, body_incl) == [],
          f"本文 quote で本文だけ直した稿が落ちた: {compose.revise_check(incl_ok, iss_incl, fm_incl, body_incl)}")
    # 校閲が欄の**一部**を「…」で省略して引用したとき(実測 2026-10-09 joint-starmas R1:
    # リード「…事務所を越えたアイドルたちが事務所を掃除するゲーム内コミュが公開され…」、gaku-katamarion R2:
    # リード「花海咲季が歌う…ノミネートされ、…」 本文「Spotifyでは…掲載されている。」)、指摘どおり直した稿を通す。
    # 以前は区画 ⊂ 欄 を見ず、正しい書き直しを「指摘に無いリードを変えた」で2巡とも戻して記事が落ちた
    fm_ex = dict(old_fm, lede="5周年企画として、事務所を越えたアイドルたちが事務所を掃除するゲーム内コミュが公開され、Steamでは70％オフで販売中だ。")
    body_ex = ("公式は10月8日に告知した。Spotifyでは収録アルバムに入り、花海咲季名義で掲載されている。 <!-- F1 -->\n\n"
               "セールは10月22日まで。 <!-- F3 -->")
    iss_ex = [{"issue_id": "I1", "rule_id": "R1", "repair": "rewrite_claim",
               "quote": "リード「…事務所を越えたアイドルたちが事務所を掃除するゲーム内コミュが公開され…」"},
              {"issue_id": "I2", "rule_id": "R2", "repair": "drop_claim",
               "quote": "本文「Spotifyでは収録アルバムに入り、花海咲季名義で掲載されている。」"}]
    ex_ok = dict(OK, addressed_issue_ids=["I1", "I2"],
                 lede="5周年企画として、メンバーが事務所を掃除するゲーム内コミュが公開され、Steamでは70％オフで販売中だ。",
                 blocks=[{"markdown": "公式は10月8日に告知した。Spotifyでは収録アルバムに入っている。", "fact_ids": ["F1"]},
                         {"markdown": "セールは10月22日まで。", "fact_ids": ["F3"]}])
    check(compose.revise_check(ex_ok, iss_ex, fm_ex, body_ex) == [],
          f"欄の一部を引いた quote(リード「…」・本文「…」)どおりに直した稿が落ちた: {compose.revise_check(ex_ok, iss_ex, fm_ex, body_ex)}")
    # 途中を「…」で省いた抜き出し、欄名の無い省略付き quote でも同じ
    iss_ex2 = [{"issue_id": "I1", "rule_id": "R1", "repair": "rewrite_claim", "quote": "リード「5周年企画として、…事務所を越えたアイドルたち…」"},
               {"issue_id": "I2", "rule_id": "R2", "repair": "drop_claim", "quote": "…Spotifyでは収録アルバムに入り、花海咲季名義…"}]
    check(compose.revise_check(ex_ok, iss_ex2, fm_ex, body_ex) == [],
          f"途中を省いた quote・欄名の無い省略付き quote どおりに直した稿が落ちた: {compose.revise_check(ex_ok, iss_ex2, fm_ex, body_ex)}")
    # 抜き出しが欄に無い(別の欄の字面)ときは触っていない。リード指摘で本文の別段落を書き換えたら止める
    ex_bad = dict(ex_ok, blocks=[ex_ok["blocks"][0], {"markdown": "セールは無期限に延びた。", "fact_ids": ["F3"]}])
    check(any("段落" in p for p in compose.revise_check(ex_bad, iss_ex, fm_ex, body_ex)),
          "欄の一部を引いた quote で、指摘の外の段落の改変が通った")
    iss_ex3 = [{"issue_id": "I1", "rule_id": "R1", "repair": "rewrite_claim", "quote": "本文「…Spotifyでは収録アルバムに入り…」"}]
    check(any("リード" in p for p in compose.revise_check(dict(ex_ok, addressed_issue_ids=["I1"]), iss_ex3, fm_ex, body_ex)),
          "本文の一部だけを引いた quote で、指摘の外のリード改変が通った")
    # R4(出典の不一致): 誤 URL を外し、正しい一次情報に付け替える。quote は frontmatter の URL(本文段落を
    # 触らない)なので、根拠段落の fact_ids を正しい候補の facts に付け替えても、字面が同じなら通す。
    # 以前は差し替えを「出典を足した」で、fact_ids の変更を「未指摘段落を変えた」で二重に弾き、誤 URL の R4 が
    # 何巡もブロックして有効な記事が校閲上限後に脱落した(監査指摘 MF-1)
    wrong_url = "https://idolmaster-official.jp/news/01_19898.html"
    right_url = "https://idolmaster-official.jp/live_event/283_noctchill/"
    fm_r4 = dict(old_fm, sources=[{"url": wrong_url}, {"url": "https://b.example/2"}])
    old_r4 = "4公演の時刻と通し券8万円が発表された。 <!-- F19 -->\n\n仙台で3月に開催される。 <!-- F20 -->"
    iss_r4 = [{"issue_id": "I1", "rule_id": "R4", "repair": "drop_source", "quote": wrong_url}]
    ans_r4 = dict(OK, addressed_issue_ids=["I1"],
                  sources=[{"url": right_url, "label": "283 PRODUCTION noctchill"}, {"url": "https://b.example/2", "label": "y"}],
                  new_facts=[{"id": "N1", "text": "4公演の時刻と通し券8万円", "url": right_url},
                             {"id": "N2", "text": "仙台で3月に開催", "url": right_url}],
                  blocks=[{"markdown": "4公演の時刻と通し券8万円が発表された。", "fact_ids": ["N1"]},
                          {"markdown": "仙台で3月に開催される。", "fact_ids": ["N2"]}])
    check(compose.revise_check(ans_r4, iss_r4, fm_r4, old_r4) == [],
          f"R4 の出典付け替え(誤 URL→正しい一次情報)が落ちた: {compose.revise_check(ans_r4, iss_r4, fm_r4, old_r4)}")
    # 付け替え中でも、未指摘段落の**字面**の無断改変は止める(根拠 id だけを外し、字面の検査は残す)
    ans_r4_bad = dict(ans_r4, blocks=[{"markdown": "4公演の時刻と通し券8万円が発表された。", "fact_ids": ["N1"]},
                                      {"markdown": "仙台ではなく東京で開催される。", "fact_ids": ["N2"]}])
    check(any("段落" in p for p in compose.revise_check(ans_r4_bad, iss_r4, fm_r4, old_r4)),
          "出典付け替えでも未指摘段落の字面改変を通した")
    # drop_source が無ければ、差し替え(外す+足す)は従来どおり止める(黙って弱い出典に付け替える手)
    iss_r4_noflag = [{"issue_id": "I1", "rule_id": "R1", "repair": "drop_claim", "quote": wrong_url}]
    check(any("出典を外した" in p for p in compose.revise_check(dict(ans_r4, blocks=[{"markdown": "4公演の時刻と通し券8万円が発表された。", "fact_ids": ["F19"]},
                                                                                    {"markdown": "仙台で3月に開催される。", "fact_ids": ["F20"]}]),
                                                                iss_r4_noflag, fm_r4, old_r4)),
          "出典についての指摘が無いのに出典の差し替えが通った")
    # R4(誤出典)の指摘なら、repair が rewrite_claim でも差し替えは通る(repair は提案。監査指摘)
    iss_r4_rw = [{"issue_id": "I1", "rule_id": "R4", "repair": "rewrite_claim", "quote": "4公演の時刻と通し券8万円が発表された。"}]
    check(compose.revise_check(ans_r4, iss_r4_rw, fm_r4, old_r4) == [],
          f"R4 の差し替えが repair の種類で弾かれた: {compose.revise_check(ans_r4, iss_r4_rw, fm_r4, old_r4)}")


def test_rollback(tmp: Path):
    """既存話題から事実だけ剥がす場合もディスクへ保存する(監査指摘 R10-P0-1)。"""
    st = tmp / "stock"; (st / "scheduled").mkdir(parents=True)
    (tmp / "metrics").mkdir()
    stories = [{"story_id": "s1", "first_published": "2026-09-01", "published_facts": ["旧", "新"],
                "edition_facts": {"2026-09-01": ["旧"], "2026-09-12": ["新"]}}]
    assemble.dump_yaml(st / "stories.yml", stories)
    (st / "scheduled" / "2026-09-20.json").write_text(json.dumps([sched_row("a", "2026-09-12"), sched_row("b", "2026-09-01")]), encoding="utf-8")
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


def test_reservation_keeps_all_facts(tmp: Path):
    """続報予約は素材の facts を全部写す。予約の根拠の日付が13件目にあっても落ちない(2026-10-07 with glasses)。"""
    st = tmp / "stock"; (st / "scheduled").mkdir(parents=True)
    (tmp / "metrics").mkdir(); (tmp / "docs" / "_editions").mkdir(parents=True)
    assemble.dump_yaml(st / "stories.yml", [])
    assemble.dump_yaml(st / "pending.yml", [])
    facts = [f"関連投稿 {i}" for i in range(12)] + ["10/10（土）14:59まで"]
    mats = {"c1": {"id": "c1", "dedup_key": "glasses", "title": "ガシャ", "url": "https://x.com/imasml_theater/status/1", "facts": facts}}
    out = {"stories": [], "pending_add": [], "pending_remove": [], "digest": [],
           "reservations": [{"candidate_id": "c1", "date": "2026-10-07", "kind": "締切前", "slug": "x", "subject": "s", "note": "n"}]}
    saved = (assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS)
    try:
        assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS = \
            tmp, st / "stories.yml", st / "scheduled", st / "pending.yml", tmp / "docs" / "_editions"
        assemble.apply("2026-10-04", 1, out, [{"slug": "x", "brand": "million"}], mats, False, stories=[])
        text = (st / "scheduled" / "2026-10-07.json").read_text(encoding="utf-8")
        rows = json.loads(text)
        check(rows and rows[0]["facts"] == facts, f"予約が素材の facts を切り詰めた: {rows and rows[0]['facts'][-1:]}")
        # 書いた予約のファイル全体を書式(scheduled.schema)で検める(facts だけ見ていると、予約に書式の無い欄を足しても通る。監査指摘)
        errs = pipelib.schema_errors(st / "scheduled" / "2026-10-07.json", text)
        check(errs == [], f"組版が書いた予約が書式に合わない: {errs}")
    finally:
        assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS = saved


def test_reservation_keeps_article_sources(tmp: Path):
    """続報予約は、予約を付けた記事が出典にしたほかの素材(公式の投稿)も添えて写す。添えは素材として執筆に渡り、予約(トリガー)にはならない。
    予約の素材(日付の載った二次情報のページ)だけを写すと、締切当日の執筆が公式の出典に届かず見送る
    (実測 2026-10-20: シャニソン第17回テクニカルチャレンジ杯の最終日)。添えの無い既存の予約は compose の前に添え、
    保存した計画(--reuse-plan)にも足す。冪等。"""
    import yaml
    st = tmp / "stock"; (st / "scheduled").mkdir(parents=True)
    (tmp / "metrics").mkdir(); (tmp / "docs" / "_editions").mkdir(parents=True); (tmp / "docs" / "_posts").mkdir(parents=True)
    (tmp / "candidates").mkdir()
    assemble.dump_yaml(st / "stories.yml", [])
    assemble.dump_yaml(st / "pending.yml", [])
    sec = {"id": "202610181832-explore-1", "dedup_key": "tec17", "title": "第17回テクニカルチャレンジ杯 開催情報",
           "url": "https://melonpapapapa-n.com/sc-prism_ev123/", "facts": ["開催期間: 2026/10/17(土) 12:00 ~ 2026/10/20(火) 20:59"]}
    off = {"id": "202610190222-grok-34", "dedup_key": "tec17", "title": "第17回テクニカルチャレンジ杯",
           "url": "https://x.com/imassc_prism/status/2111291358019514716", "facts": ["課題曲は『Black Reverie(xRライブ ver.)』。"]}
    unused = {"id": "202610190222-grok-99", "dedup_key": "tec17", "title": "出典にしなかった投稿",
              "url": "https://x.com/imassc_prism/status/2111291358019514799", "facts": ["x"]}
    mats = {c["id"]: c for c in (sec, off, unused)}
    fm = {"slug": "shiny-tec17", "brand": "shiny", "candidate_ids": [sec["id"], off["id"], unused["id"]],
          "sources": [{"url": sec["url"], "label": "a"}, {"url": off["url"], "label": "b"}]}
    out = {"stories": [], "pending_add": [], "pending_remove": [], "digest": [],
           "reservations": [{"candidate_id": sec["id"], "date": "2026-10-20", "kind": "締切", "slug": "shiny-tec17",
                             "subject": "テクニカルチャレンジ杯 最終日", "note": "20時59分まで", "occurs_on": "2026-10-20"}]}
    saved = (assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS)
    croot = compose.ROOT
    try:
        assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS = \
            tmp, st / "stories.yml", st / "scheduled", st / "pending.yml", tmp / "docs" / "_editions"
        compose.ROOT = tmp
        assemble.apply("2026-10-19", 1, out, [fm], mats, False, stories=[])
        p = st / "scheduled" / "2026-10-20.json"
        text = p.read_text(encoding="utf-8")
        rows = json.loads(text)
        check(pipelib.schema_errors(p, text) == [], f"添えた予約が書式に合わない: {pipelib.schema_errors(p, text)}")
        head = rows[0]["id"]
        comp = [r for r in rows if r.get("companion_of") == head]
        check([r["url"] for r in comp] == [off["url"]] and comp[0]["source_type"] == "公式" and comp[0]["facts"] == off["facts"]
              and comp[0]["dedup_key"] == "tec17" and comp[0]["occurs_on"] == "2026-10-20",
              f"記事が出典にした公式の投稿を予約に添えない(出典にしなかった素材は添えない): {comp}")
        # 当日: 添えは素材(選定の主題・執筆の素材)に入り、予約(トリガー)にはならない
        window = compose.load_window_candidates("2026-10-20")
        check({head, comp[0]["id"]} <= set(window), "添えた素材が当日の素材に入らない")
        by_brand, _, _ = compose.write_plan_index("2026-10-20", window, {})
        subj = next(s for rows_ in by_brand.values() for s in rows_ if s["dedup_key"] == "tec17")
        check(comp[0]["id"] in subj["ids"], f"添えた素材が予約と同じ主題に束ならない: {subj['ids']}")
        src = Path(compose.__file__).read_text(encoding="utf-8")
        check('triggers = [t for t in load_scheduled(date) if not t.get("companion_of")]' in src, "添えた素材を予約(トリガー)として選定に渡す")
        plan = {"articles": [{"slug": "shiny-tec17", "candidate_ids": [head]}]}
        compose.attach_companions(plan, window); compose.attach_companions(plan, window)
        check(plan["articles"][0]["candidate_ids"] == [head, comp[0]["id"]], f"保存した計画に添えた素材が足されない: {plan}")
        # 添えの無い既存の予約(2026-10-20 より前の組版): 予約した号の記事と素材から添える。過去日は触らない。冪等
        (tmp / "candidates" / "2026-10-19.json").write_text(json.dumps([sec, off, unused], ensure_ascii=False), encoding="utf-8")
        (tmp / "docs" / "_posts" / "2026-10-19-shiny-tec17.md").write_text(
            "---\n" + yaml.safe_dump(fm, allow_unicode=True) + "---\n本文\n", encoding="utf-8")
        old = [r for r in rows if not r.get("companion_of")]
        p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        (st / "scheduled" / "2026-10-18.json").write_text(json.dumps([dict(old[0], id="past")], ensure_ascii=False), encoding="utf-8")
        log = assemble.restore_reservation_companions("2026-10-20")
        got = json.loads(p.read_text(encoding="utf-8"))
        check([r["url"] for r in got if r.get("companion_of") == head] == [off["url"]], f"既存の予約に公式の投稿を添えない: {log}")
        check(len(json.loads((st / "scheduled" / "2026-10-18.json").read_text(encoding="utf-8"))) == 1, "過去日の予約を書き換えた")
        check(assemble.restore_reservation_companions("2026-10-20") == [], "添えが冪等でない")
        # 記事が予約の素材(日付確認の二次情報)を出典にせず、公式の投稿だけを出典にした(実測 2026-10-11 学マスの運営情報まとめ)
        (tmp / "docs" / "_posts" / "2026-10-19-shiny-tec17.md").write_text(
            "---\n" + yaml.safe_dump(dict(fm, sources=[{"url": off["url"], "label": "b"}]), allow_unicode=True) + "---\n本文\n",
            encoding="utf-8")
        p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        assemble.restore_reservation_companions("2026-10-20")
        got = json.loads(p.read_text(encoding="utf-8"))
        check([r["url"] for r in got if r.get("companion_of") == head] == [off["url"]],
              "予約の素材を出典にしなかった記事から、公式の投稿を添えない")
        # 同じ素材を2本の記事が使い、どちらも予約の url を出典にしていなければ、どの記事か決まらないので添えない
        (tmp / "docs" / "_posts" / "2026-10-19-shiny-tec17-b.md").write_text(
            "---\n" + yaml.safe_dump(dict(fm, slug="shiny-tec17-b", sources=[{"url": unused["url"], "label": "c"}]),
                                     allow_unicode=True) + "---\n本文\n", encoding="utf-8")
        p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        check(assemble.restore_reservation_companions("2026-10-20") == [], "どの記事か決まらない予約に添えた")
    finally:
        assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS = saved
        compose.ROOT = croot


def test_companion_keeps_source_topic(tmp: Path):
    """複数の話題を扱うまとめ記事から付けた続報予約の添えは、素材の元の話題(src_dedup_key)を残す。執筆・書き直しには予約と違うときだけ
    other_topic として見せ、既報もその話題から引く。元の話題の無い既存の添えには compose の前に足す(過去日は触らない)。冪等。
    予約の話題キーで上書きすると、別の話題の素材が予約の話題の素材に見えた(当番の指摘 34fdc7491c: あさり先生のゼミ終了の予約に
    Agitato花海咲季ガシャの公式投稿が添わった)。"""
    import yaml
    import storylink
    st = tmp / "stock"; (st / "scheduled").mkdir(parents=True)
    (tmp / "metrics").mkdir(); (tmp / "docs" / "_editions").mkdir(parents=True); (tmp / "docs" / "_posts").mkdir(parents=True)
    (tmp / "candidates").mkdir()
    assemble.dump_yaml(st / "stories.yml", [])
    assemble.dump_yaml(st / "pending.yml", [])
    seminar = {"id": "202610101834-explore-9", "dedup_key": "asari-produce-seminar-2026", "title": "あさり先生のプロデュースゼミ",
               "url": "https://game8.jp/gakuen-idolmaster/610573", "facts": ["開催期間: 10/11〜10/20 11:59"]}
    same = {"id": "202610110238-grok-55", "dedup_key": "asari-produce-seminar-2026", "title": "あさり先生のプロデュースゼミ開催",
            "url": "https://x.com/gkmas_official/status/2108389738277183532", "facts": ["ゼミ開催"]}
    gacha = {"id": "202610110238-grok-56", "dedup_key": "gaku-agitato-saki-gacha-2026", "title": "Agitato花海咲季ガシャ",
             "url": "https://x.com/gkmas_official/status/2108384513445572998", "facts": ["咲季ガシャ開催"]}
    mats = {c["id"]: c for c in (seminar, same, gacha)}
    fm = {"slug": "gaku-unei-matome", "brand": "gaku", "candidate_ids": [seminar["id"], same["id"], gacha["id"]],
          "sources": [{"url": c["url"], "label": "a"} for c in (seminar, same, gacha)]}
    out = {"stories": [], "pending_add": [], "pending_remove": [], "digest": [],
           "reservations": [{"candidate_id": seminar["id"], "date": "2026-10-20", "kind": "終了", "slug": "gaku-unei-matome",
                             "subject": "あさり先生のプロデュースゼミ終了", "note": "11時59分まで", "occurs_on": "2026-10-20"}]}
    saved = (assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS)
    try:
        assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS = \
            tmp, st / "stories.yml", st / "scheduled", st / "pending.yml", tmp / "docs" / "_editions"
        assemble.apply("2026-10-19", 1, out, [fm], mats, False, stories=[])
        p = st / "scheduled" / "2026-10-20.json"
        text = p.read_text(encoding="utf-8")
        check(pipelib.schema_errors(p, text) == [], f"添えた予約が書式に合わない: {pipelib.schema_errors(p, text)}")
        rows = json.loads(text)
        comp = {r["url"]: r for r in rows if r.get("companion_of")}
        check(comp[gacha["url"]].get("src_dedup_key") == gacha["dedup_key"]
              and comp[same["url"]].get("src_dedup_key") == same["dedup_key"],
              f"添えた素材に元の話題が残らない: {[(r['url'], r.get('src_dedup_key')) for r in comp.values()]}")
        # 執筆・書き直しの素材: 予約と別の話題から来た添えだけに other_topic。既報はその話題からも引く
        shown, _ = renderlib.materials_with_ids(rows)
        tag = {r["url"]: r.get("other_topic") for r in shown}
        check(tag == {seminar["url"]: None, same["url"]: None, gacha["url"]: gacha["dedup_key"]},
              f"別の話題から来た添えを執筆に見せない: {tag}")
        check(gacha["dedup_key"] in storylink.prior_keys(rows), "添えの元の話題の既報を引かない")
        # 前の号の予約から候補へ戻す(carry_dropped_candidates)ときも元の話題を残す
        back = compose.scheduled_as_candidate(comp[gacha["url"]])
        check(back.get("src_dedup_key") == gacha["dedup_key"], f"予約の添えを候補へ戻すと元の話題が落ちる: {back}")
        ctext = json.dumps([back], ensure_ascii=False)
        check(pipelib.schema_errors(tmp / "candidates" / "2026-10-20.json", ctext) == [],
              f"元の話題を残した候補が書式に合わない: {pipelib.schema_errors(tmp / 'candidates' / '2026-10-20.json', ctext)}")
        # 元の話題の無い既存の添え(2026-10-20 の組版): 予約を付けた号の素材から足す。過去日は触らない。冪等
        (tmp / "candidates" / "2026-10-19.json").write_text(json.dumps(list(mats.values()), ensure_ascii=False), encoding="utf-8")
        (tmp / "docs" / "_posts" / "2026-10-19-gaku-unei-matome.md").write_text(
            "---\n" + yaml.safe_dump(fm, allow_unicode=True) + "---\n本文\n", encoding="utf-8")
        old = [{k: v for k, v in r.items() if k != "src_dedup_key"} for r in rows]
        p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        past = st / "scheduled" / "2026-10-18.json"
        past.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        log = assemble.restore_reservation_companions("2026-10-20")
        text = p.read_text(encoding="utf-8")
        got = {r["url"]: r.get("src_dedup_key") for r in json.loads(text) if r.get("companion_of")}
        check(got == {same["url"]: same["dedup_key"], gacha["url"]: gacha["dedup_key"]}, f"既存の添えに元の話題を足さない: {got} {log}")
        check(len(json.loads(text)) == len(old), "元の話題を足すときに添えを足し直した")
        check(pipelib.schema_errors(p, text) == [], f"足した予約が書式に合わない: {pipelib.schema_errors(p, text)}")
        check(all("src_dedup_key" not in r for r in json.loads(past.read_text(encoding="utf-8"))), "過去日の予約を書き換えた")
        check(assemble.restore_reservation_companions("2026-10-20") == [], "元の話題の足しが冪等でない")
        for name in ("write-article.md", "review-article.md"):
            body = (Path(compose.__file__).resolve().parent.parent / "prompts" / name).read_text(encoding="utf-8")
            check(("other_topic" if name.startswith("write") else "src_dedup_key") in body, f"{name} が別の話題の添えの扱いを指示しない")
            # キーの表記が違うだけの同じ話題(実データ: 杯の結果発表の予約に添えた杯の公式の投稿)を別の話題として避けさせない
            check("キーの表記が違うだけ" in body and "同じ出来事・商品" in body, f"{name} がキーの違うだけの同じ話題の素材を使わせない")
    finally:
        assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS = saved


def test_companion_topic_survives_second_reservation(tmp: Path):
    """添えを素材にして次の予約を付けても、添えに残した元の話題(src_dedup_key)を予約の話題で上書きしない(組版・既存の添えへの補完の両方)。
    添えそのものから付けた予約にも元の話題を残す。元の話題の無い古い添えは元の候補までたどる。
    以前は素材の dedup_key(添え先の予約の話題)を写した(監査指摘: 山梨の WALK & WANDER 開幕の予約に添えた歩数計の素材が、終了の予約で
    開幕の話題に変わり、執筆の other_topic が消えた)。"""
    import yaml
    st = tmp / "stock"; (st / "scheduled").mkdir(parents=True)
    (tmp / "metrics").mkdir(); (tmp / "docs" / "_editions").mkdir(parents=True); (tmp / "docs" / "_posts").mkdir(parents=True)
    (tmp / "candidates").mkdir()
    assemble.dump_yaml(st / "stories.yml", [])
    assemble.dump_yaml(st / "pending.yml", [])
    walk = {"id": "202608311234-explore-18", "dedup_key": "cg-yamanashi-walk-wander-2026", "title": "やまなし WALK & WANDER",
            "url": "https://example.com/walk", "facts": ["11/21〜12/27 開催"]}
    pedo = {"id": "202608312333-explore-1", "dedup_key": "cg-yamanashi-walk-wander-pedometer-2026", "title": "歩数計キャンペーン",
            "url": "https://example.com/pedometer", "facts": ["歩数計で特典"]}
    fm1 = {"slug": "yamanashi", "brand": "cg", "candidate_ids": [walk["id"], pedo["id"]],
           "sources": [{"url": c["url"], "label": "a"} for c in (walk, pedo)]}
    res = lambda cid, date, kind: {"stories": [], "pending_add": [], "pending_remove": [], "digest": [],
                                   "reservations": [{"candidate_id": cid, "date": date, "kind": kind, "slug": "yamanashi",
                                                     "subject": f"WALK & WANDER {kind}", "note": "", "occurs_on": date}]}
    saved = (assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS)
    try:
        assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS = \
            tmp, st / "stories.yml", st / "scheduled", st / "pending.yml", tmp / "docs" / "_editions"
        (tmp / "candidates" / "2026-09-01.json").write_text(json.dumps([walk, pedo], ensure_ascii=False), encoding="utf-8")
        assemble.apply("2026-09-01", 1, res(walk["id"], "2026-11-21", "開幕"), [fm1], {c["id"]: c for c in (walk, pedo)}, False, stories=[])
        first = json.loads((st / "scheduled" / "2026-11-21.json").read_text(encoding="utf-8"))
        head = next(r for r in first if not r.get("companion_of"))
        comp1 = next(r for r in first if r.get("companion_of"))
        check(comp1.get("src_dedup_key") == pedo["dedup_key"], f"1代目の添えに元の話題が無い: {comp1}")
        # 2代目: 開幕の予約から書いた記事が添えも出典にして、終了の予約を付ける
        mats2 = assemble.load_materials("2026-11-21")
        fm2 = {"slug": "yamanashi", "brand": "cg", "candidate_ids": [head["id"], comp1["id"]],
               "sources": [{"url": r["url"], "label": "a"} for r in (head, comp1)]}
        # 添えそのものから付けた予約(12/01 の締切)も同じ号で付ける(同じ号の apply は、その号が付けた予約を剥がして付け直す)
        out2 = res(head["id"], "2026-12-27", "終了")
        out2["reservations"] += res(comp1["id"], "2026-12-01", "締切")["reservations"]
        assemble.apply("2026-11-21", 1, out2, [fm2], mats2, False, stories=[])
        p = st / "scheduled" / "2026-12-27.json"
        text = p.read_text(encoding="utf-8")
        check(pipelib.schema_errors(p, text) == [], f"2代目の予約が書式に合わない: {pipelib.schema_errors(p, text)}")
        comp2 = next(r for r in json.loads(text) if r["url"] == pedo["url"])
        check(comp2.get("src_dedup_key") == pedo["dedup_key"], f"2代目の添えで元の話題を予約の話題で上書きした: {comp2.get('src_dedup_key')}")
        shown, _ = renderlib.materials_with_ids([comp2])
        check(shown[0].get("other_topic") == pedo["dedup_key"], f"2代目の添えの別の話題を執筆に見せない: {shown}")
        # 添えそのものから付けた予約: 話題は添え先の予約のまま、元の話題を残す
        q = st / "scheduled" / "2026-12-01.json"
        qtext = q.read_text(encoding="utf-8")
        entry = next(r for r in json.loads(qtext) if not r.get("companion_of"))
        check(entry.get("src_dedup_key") == pedo["dedup_key"] and entry["dedup_key"] == walk["dedup_key"],
              f"添えから付けた予約に元の話題が残らない: {entry}")
        check(pipelib.schema_errors(q, qtext) == [], f"添えから付けた予約が書式に合わない: {pipelib.schema_errors(q, qtext)}")
        # 前の号の予約から戻した候補(companion_of は落ちる)を素材にしても元の話題を残す
        back = {**compose.scheduled_as_candidate(comp1), "id": "carried-pedo"}
        got = assemble.companion_rows({"id": "sched-x", "url": walk["url"], "dedup_key": walk["dedup_key"]},
                                      {"candidate_ids": ["carried-pedo"], "sources": [{"url": pedo["url"]}]}, {"carried-pedo": back})
        check(got and got[0].get("src_dedup_key") == pedo["dedup_key"], f"戻した候補の添えで元の話題を上書きした: {got}")
        # 既存の添えへの補完: 1代目も2代目も元の話題が無い(2026-10-20 の組版)。元の候補までたどって足す。冪等
        strip = lambda path: path.write_text(json.dumps([{k: v for k, v in r.items() if k != "src_dedup_key"}
                                                         for r in json.loads(path.read_text(encoding="utf-8"))], ensure_ascii=False), encoding="utf-8")
        strip(st / "scheduled" / "2026-11-21.json"); strip(p)
        for on, f in (("2026-09-01", fm1), ("2026-11-21", fm2)):
            (tmp / "docs" / "_posts" / f"{on}-yamanashi.md").write_text("---\n" + yaml.safe_dump(f, allow_unicode=True) + "---\n本文\n", encoding="utf-8")
        log = assemble.restore_reservation_companions("2026-11-22")
        comp2 = next(r for r in json.loads(p.read_text(encoding="utf-8")) if r["url"] == pedo["url"])
        check(comp2.get("src_dedup_key") == pedo["dedup_key"], f"補完で2代目の添えに予約の話題を元の話題として書いた: {comp2.get('src_dedup_key')} {log}")
        check(assemble.restore_reservation_companions("2026-11-22") == [], "2代目の添えへの補完が冪等でない")
        # 元の候補までたどれない古い添えには、予約の話題を元の話題として書かない
        (tmp / "candidates" / "2026-09-01.json").unlink()
        strip(st / "scheduled" / "2026-11-21.json"); strip(p)
        assemble.restore_reservation_companions("2026-11-22")
        comp2 = next(r for r in json.loads(p.read_text(encoding="utf-8")) if r["url"] == pedo["url"])
        check(comp2.get("src_dedup_key") is None, f"たどれない添えに予約の話題を書いた: {comp2.get('src_dedup_key')}")
    finally:
        assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS = saved


def test_grok_verify_keeps_new_announcement_separate():
    """grok-verify は、前の投稿に無い新しい発表(セットリスト・結果など)を載せた投稿を、元の告知の候補へまとめない
    (実測 2026-10-19: 深海フェス2026 のセットリストの投稿が 10/17 の出演告知の候補へまとめられ、選定が既報として落とした)。"""
    text = (Path(compose.__file__).resolve().parent.parent / "prompts" / "grok-verify.md").read_text(encoding="utf-8")
    check("同じ施策の複数の投稿は1件にまとめ" not in text and "同じ施策でも別の候補にし、その投稿の url と投稿日" in text,
          "新しい発表を載せた投稿を元の告知の候補へまとめる依頼のまま")


def test_restore_reservation_facts(tmp: Path):
    """保存済みの予約で先頭12件に切れた facts を元素材から復元する(2026-10-10 with glasses の締切予約)。
    元と食い違う・元が無い・過去日の予約は触らない。2回目は何も変えない。"""
    st = tmp / "stock" / "scheduled"; st.mkdir(parents=True); (tmp / "candidates").mkdir()
    facts = [f"関連投稿 {i}" for i in range(12)] + ["10/10（土）14:59まで"]
    cand = {"id": "202610040227-grok-78", "facts": facts}
    real_s, real_c = pipelib.ROOT / "stock" / "scheduled" / "2026-10-10.json", pipelib.ROOT / "candidates" / "2026-10-04.json"
    if real_s.exists() and real_c.exists():   # 実データがあればその予約と元素材を使う
        rr = next((r for r in json.loads(real_s.read_text(encoding="utf-8")) if "with-glasses" in r.get("id", "")), None)
        rc = rr and next((c for c in json.loads(real_c.read_text(encoding="utf-8")) if c.get("id") == rr.get("src_candidate_id")), None)
        if rr and rc and len(rc.get("facts") or []) > len(rr.get("facts") or []):
            cand, facts = rc, rc["facts"]
    row = sched_row("sched-2026-10-10-glasses-締切", "2026-10-04", src_candidate_id=cand["id"], facts=facts[:12])
    other = sched_row("o", "2026-10-04", src_candidate_id=cand["id"], facts=["別の事実"])
    gone = sched_row("g", "2026-10-04", src_candidate_id="missing", facts=["x"])
    (st / "2026-10-10.json").write_text(json.dumps([row, other, gone], ensure_ascii=False), encoding="utf-8")
    (st / "2026-10-06.json").write_text(json.dumps([dict(row, id="past")], ensure_ascii=False), encoding="utf-8")
    (tmp / "candidates" / "2026-10-04.json").write_text(json.dumps([cand], ensure_ascii=False), encoding="utf-8")
    saved = (assemble.ROOT, assemble.SCHEDULED)
    try:
        assemble.ROOT, assemble.SCHEDULED = tmp, st
        log = assemble.restore_reservation_facts("2026-10-07")
        rows = {r["id"]: r for r in json.loads((st / "2026-10-10.json").read_text(encoding="utf-8"))}
        check(rows[row["id"]]["facts"] == facts, f"切れた予約が復元されない: {rows[row['id']]['facts'][-1:]}")
        check(rows["o"]["facts"] == ["別の事実"] and rows["g"]["facts"] == ["x"], "元と食い違う/元が無い予約を書き換えた")
        check(len(json.loads((st / "2026-10-06.json").read_text(encoding="utf-8"))[0]["facts"]) == 12, "過去日の予約を書き換えた")
        check(len(log) == 1 and assemble.restore_reservation_facts("2026-10-07") == [], f"冪等でない: {log}")
        compose_root, compose.ROOT = compose.ROOT, tmp   # 復元後の予約が素材として読まれる
        try:
            check(any(facts[-1] in s.get("facts", []) for s in compose.load_scheduled("2026-10-10")), "復元した facts が素材に載らない")
        finally:
            compose.ROOT = compose_root
    finally:
        assemble.ROOT, assemble.SCHEDULED = saved


def test_restore_chained_reservation_facts(tmp: Path):
    """予約を元に付けた予約(src_candidate_id が sched-*)も、元の予約をたどって候補の facts まで復元する(当番の指摘 f1a5050dcc。
    実測: 2027-01-12 のオケコン千秋楽の予約は 09-27 の予約 → 09-18 の候補 248件のうち12件だけを持っていた)。
    途中の予約が元と食い違えば、たどった先を採らない。過去日の予約(途中の段)は書き換えない。既報の照合も予約の素材を引く。"""
    import storylink as sl
    st = tmp / "stock" / "scheduled"; st.mkdir(parents=True); (tmp / "candidates").mkdir()
    facts = [f"事実 {i}" for i in range(20)] + ["A席全席指定料金: 12,000円（税込）"]
    (tmp / "candidates" / "2026-09-18.json").write_text(json.dumps([{"id": "c-1", "facts": facts}], ensure_ascii=False), encoding="utf-8")
    mid = sched_row("sched-2026-09-27-orch-締切", "2026-09-18", dedup_key="orch", url="https://idolmaster-official.jp/news/01_19894",
                    src_candidate_id="c-1", facts=facts[:12])
    bad = dict(mid, id="sched-2026-09-27-bad-締切", facts=["別の事実"] + facts[1:12])
    (st / "2026-09-27.json").write_text(json.dumps([mid, bad], ensure_ascii=False), encoding="utf-8")
    end = sched_row("sched-2027-01-12-orch-千秋楽", "2026-09-27", src_candidate_id=mid["id"], facts=facts[:12])
    end_bad = dict(end, id="e2", src_candidate_id=bad["id"], facts=bad["facts"][:12])
    (st / "2027-01-12.json").write_text(json.dumps([end, end_bad], ensure_ascii=False), encoding="utf-8")
    saved = (assemble.ROOT, assemble.SCHEDULED)
    try:
        assemble.ROOT, assemble.SCHEDULED = tmp, st
        log = assemble.restore_reservation_facts("2026-10-07")
        rows = {r["id"]: r for r in json.loads((st / "2027-01-12.json").read_text(encoding="utf-8"))}
        check(rows[end["id"]]["facts"] == facts, f"予約を元にした予約を復元しない: {len(rows[end['id']]['facts'])}件")
        check(rows["e2"]["facts"] == bad["facts"][:12], "途中の予約が元と食い違うのに、元の候補の facts にすり替えた")
        check(len(json.loads((st / "2026-09-27.json").read_text(encoding="utf-8"))[0]["facts"]) == 12, "過去日の予約(途中の段)を書き換えた")
        check(len(log) == 1 and assemble.restore_reservation_facts("2026-10-07") == [], f"冪等でない: {log}")
    finally:
        assemble.ROOT, assemble.SCHEDULED = saved
    # 既報の照合: 予約から書いた記事(candidate_ids が sched-*)の話題キーを、その号の予約から引く
    (tmp / "docs" / "_posts").mkdir(parents=True)
    (tmp / "docs" / "_posts" / "2026-09-27-orch.md").write_text(
        "---\nslug: orch\nbrand: cg\ntitle: オケコン締切\ncandidate_ids: [sched-2026-09-27-orch-締切]\n---\n本文\n", encoding="utf-8")
    arts = sl.past_articles("2026-09-28", tmp)
    check(arts and "orch" in arts[0]["keys"] and "idolmaster-official.jp/news/01_19894" in arts[0]["urls"],
          f"予約から書いた記事の話題キー・URL を既報の照合に載せない: {arts}")


def test_review_failed_changed_article(tmp: Path):
    """書き直した記事(前の判定から中身の指紋が変わった)の再校閲が2回とも動かなければ、前の巡の「校閲済み」で補わず載せない。
    補うと、変わった中身を誰も見ないまま承認になり、release も新しい指紋で通す(監査指摘)。中身が変わっていなければ引き継ぐ。"""
    import release
    d = "2099-01-02"
    posts = tmp / "docs" / "_posts"
    posts.mkdir(parents=True)
    (tmp / "metrics").mkdir()
    art = posts / f"{d}-x.md"
    art.write_text("---\nslug: x\ntitle: t\n---\n前の稿\n", encoding="utf-8")

    class FakeP:
        def __init__(self, ok_):
            self.ok, self.returncode, self.pid = ok_, (0 if ok_ else 1), 0

        def communicate(self, timeout=None):
            return ((json.dumps({"verdict": "approve", "blockers": [], "comments": []}), "") if self.ok else ("", "boom"))
    saved = (compose.ROOT, pipelib.ROOT, release.ROOT, compose.subprocess.Popen)
    try:
        compose.ROOT = pipelib.ROOT = release.ROOT = tmp
        old_hash = pipelib.article_hash(art)
        carry = {"verdict": "block", "blockers": [], "comments": [], "reviewed": [f"article:{d}-x.md", "paper"],
                 "hashes": {f"docs/_posts/{d}-x.md": old_hash}}

        first: list[str] = []

        def fake_popen(argv, **k):
            prompt = Path(argv[2]).read_text(encoding="utf-8") if Path(argv[2]).exists() else argv[2]
            first.append(first[0] if first else prompt)
            return FakeP(prompt != first[0])        # 最初に起動される記事の担当だけ、やり直しも含めて動かない
        compose.subprocess.Popen = fake_popen
        for changed in (True, False):
            first.clear()
            for p in (tmp / "metrics").glob("review-*.json"):
                p.unlink()
            art.write_text("---\nslug: x\ntitle: t\n---\n" + ("書き直した稿(指摘の外も変えた)\n" if changed else "前の稿\n"), encoding="utf-8")
            r = compose.claude_review(d, 2, targets=[f"{d}-x.md"], editorial=False, paper=True, carry=carry)
            v = release.review_verdict(d)[0]
            if changed:
                check(r.get("verdict") == "block" and v != "approve" and any(b.get("file") == f"docs/_posts/{d}-x.md" for b in r["blockers"]),
                      f"書き直した記事の再校閲が動かないのに承認した: {r.get('verdict')} / release {v}")
            else:
                check(r.get("verdict") == "approve", f"中身の変わらない記事の前の判定を引き継がない: {r.get('verdict')} {r.get('blockers')}")
    finally:
        compose.ROOT, pipelib.ROOT, release.ROOT, compose.subprocess.Popen = saved


def test_review_unchecked_scope_not_approved(tmp: Path):
    """紙面全体・社説の担当が2回とも動かなければ、その担当が**いまの中身を前に検めていた**ときだけ判定を引き継ぐ。初めて見る紙面・
    検めたあとに変わった紙面を、記事の担当の承認や巡の終わりの指紋(hashes)で補って approve にしない。release も承認として通さない
    (監査指摘 2026-10-14: 記事の担当が通り紙面担当だけ2回とも動かないと approve になった。紙面が変わった後の巡でも同じ)。"""
    import release
    d = "2099-01-03"
    posts, eds = tmp / "docs" / "_posts", tmp / "docs" / "_editions"
    for p in (posts, eds, tmp / "docs" / "_editorials", tmp / "metrics"):
        p.mkdir(parents=True, exist_ok=True)
    (posts / f"{d}-a.md").write_text("---\n" + article_fm("a", d, title="学マスの告知") + "---\n本文\n", encoding="utf-8")
    ranking = "".join(f"- n: {i}\n  name: 名{i}\n  brand: cg\n  delta: new\n" for i in range(1, 9))

    def set_paper(row: str) -> None:
        (eds / f"{d}.md").write_text(
            f"---\nnumber: 30\ndate: '{d}'\nweekday: 土\nissued_at: '{d}T06:00:00+09:00'\npages: 1\narticle_count: 1\ncorrected_count: 0\n"
            f"lead_slug: a\ndigest:\n- label: 本日\n  rows:\n  - k: 発\n    t: {row}\n    d: ''\n    brand: cg\n    slug: a\n"
            f"- label: 昨日\n  rows: []\n- label: 継続中\n  rows: []\n- label: 明日\n  rows: []\nranking:\n{ranking}birthdays: []\n---\n",
            encoding="utf-8")
    set_paper("学マスの告知")
    fail: set[str] = set()       # 2回とも動かない担当(article / paper / editorial)
    calls: list[str] = []

    class FakeP:
        def __init__(self, ok_):
            self.ok, self.returncode, self.pid = ok_, (0 if ok_ else 1), 0

        def communicate(self, timeout=None):
            return ((json.dumps({"verdict": "approve", "blockers": [], "comments": []}), "") if self.ok else ("", "boom"))

    def fake_popen(argv, **k):
        text = Path(re.search(r"`([^`]+)`", argv[2]).group(1)).read_text(encoding="utf-8")
        who = "paper" if "紙面全体**を見て" in text else "editorial" if "**社説**を校閲" in text else "article"
        calls.append(who)
        return FakeP(who not in fail)

    def run(round_no: int, failing: set[str], **kw) -> tuple[dict, str]:
        fail.clear(); fail.update(failing); calls.clear()
        r = compose.claude_review(d, round_no, **kw)
        return r, release.review_verdict(d)[0]

    saved = (compose.ROOT, pipelib.ROOT, release.ROOT, compose.subprocess.Popen)
    try:
        compose.ROOT = pipelib.ROOT = release.ROOT = tmp
        compose.subprocess.Popen = fake_popen
        a_only = dict(targets=[f"{d}-a.md"], editorial=False, paper=True)
        # 1巡目: 記事の担当は通り、紙面担当だけ2回とも動かない(一度も紙面を見ていない)
        r1, v = run(1, {"paper"}, **a_only)
        check(calls.count("paper") == 2, f"紙面担当をやり直していない: {calls}")
        check(r1.get("verdict") != "approve" and v != "approve" and "paper" not in r1.get("checked", {}),
              f"紙面担当が一度も動いていないのに承認した: {r1.get('verdict')} / release {v} / checked {sorted(r1.get('checked') or {})}")
        # 2巡目: 紙面担当が動いて承認 → 3巡目: 同じ紙面で紙面担当が動かなければ、前の判定を引き継ぐ
        r2, v = run(2, set(), **a_only, carry=r1)
        check(r2.get("verdict") == "approve" and v == "approve" and "paper" in r2.get("checked", {}), f"紙面担当が動いたのに承認しない: {r2.get('verdict')} / {v}")
        r3, v = run(3, {"paper"}, **a_only, carry=r2)
        check(r3.get("verdict") == "approve" and v == "approve", f"前に検めた紙面と同じなのに、紙面担当の前の判定を引き継がない: {r3.get('verdict')} / {v}")
        # 4巡目: 紙面(digest)が変わったあと紙面担当が動かない → 5巡目: 紙面はそのまま、また動かない(4巡目の hashes は変わった紙面を
        # 指すが、紙面担当はそれを検めていない)
        set_paper("学マスの告知(差し替え)")
        r4, v = run(4, {"paper"}, **a_only, carry=r3)
        check(r4.get("verdict") != "approve" and v != "approve", f"変わった紙面を、前の紙面の判定で承認した: {r4.get('verdict')} / release {v}")
        r5, v = run(5, {"paper"}, **a_only, carry=r4)
        check(r5.get("verdict") != "approve" and v != "approve",
              f"紙面担当が検めていない紙面を、前の巡の指紋(hashes)と同じだからと承認した: {r5.get('verdict')} / release {v}")
        # 社説も同じ: 一度も検めていない社説の担当が2回とも動かなければ承認しない
        (tmp / "docs" / "_editorials" / f"{d}.md").write_text("---\n" + article_fm("ed", d, title="社説") + "---\n社説の本文\n", encoding="utf-8")
        r6, v = run(6, {"editorial"}, targets=[f"{d}-a.md"], editorial=True, paper=False, carry=r3)
        check("editorial" in calls and r6.get("verdict") != "approve" and v != "approve",
              f"社説の担当が一度も動いていないのに承認した: {r6.get('verdict')} / release {v} / {calls}")
        # 古い記録(checked が無い)からの引き継ぎは、巡の終わりの指紋(hashes)で代える: 同じ紙面なら引き継ぐ
        (tmp / "docs" / "_editorials" / f"{d}.md").unlink()
        old = {"verdict": "approve", "blockers": [], "comments": [], "reviewed": [f"article:{d}-a.md", "paper"],
               "hashes": pipelib.review_manifest(d)}
        r7, v = run(7, {"paper"}, **a_only, carry=old)
        check(r7.get("verdict") == "approve" and v == "approve", f"古い記録の、同じ紙面への紙面担当の判定を引き継がない: {r7.get('verdict')} / {v}")
    finally:
        compose.ROOT, pipelib.ROOT, release.ROOT, compose.subprocess.Popen = saved


def test_leave_tree_clean_partial(tmp: Path):
    """途中終了の後始末は、compose の成果物のうち**実際に変わったファイル**(変更・削除・未追跡)だけを stage して commit/push する。
    号を作る前に止まると、まだ無い成果物(号スナップショット・社説・予約)のパスで git add が pathspec エラーになり、計画や記録まで
    残った(監査指摘 2026-10-14)。compose の成果物でない変更・無視するファイルは commit しない。"""
    d = "2099-01-04"
    branch = f"edition/{d}"
    work, origin = tmp / "work", tmp / "origin.git"
    g = lambda *a: subprocess.run(["git", *a], cwd=work, capture_output=True, text=True, check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(work)], check=True, capture_output=True)
    g("config", "user.email", "t@example"); g("config", "user.name", "t")
    for p in ("docs/_posts", "stock", "metrics", "scripts"):
        (work / p).mkdir(parents=True)
    (work / ".gitignore").write_text("metrics/work/\n", encoding="utf-8")
    (work / "docs" / "_posts" / f"{d}-old.md").write_text("---\n" + article_fm("old", d) + "---\n前の実行の記事\n", encoding="utf-8")
    (work / "stock" / "stories.yml").write_text("stories: []\n", encoding="utf-8")
    (work / "scripts" / "other.py").write_text("x = 1\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-q", "-m", "fixture"); g("remote", "add", "origin", str(origin)); g("push", "-q", "-u", "origin", branch)
    # 選定のあと、号を作る前に止まった: 計画(未追跡)・台帳(変更)・前の実行の記事の削除・作業ファイル(無視)・compose の成果物でない変更
    (work / "metrics" / f"plan-{d}.json").write_text("{}\n", encoding="utf-8")
    (work / "stock" / "stories.yml").write_text("stories: [x]\n", encoding="utf-8")
    (work / "docs" / "_posts" / f"{d}-old.md").unlink()
    (work / "metrics" / "work").mkdir()
    (work / "metrics" / "work" / "p.md").write_text("依頼文\n", encoding="utf-8")
    (work / "scripts" / "other.py").write_text("x = 2\n", encoding="utf-8")
    notes: list[str] = []
    saved = (compose.ROOT, pipelib.ROOT, compose.STARTED_ON, compose.STARTED_DATE, compose.notify, pipelib.notify)
    try:
        compose.ROOT = pipelib.ROOT = work
        compose.STARTED_ON, compose.STARTED_DATE = branch, d
        compose.notify = pipelib.notify = lambda job, msg, ok=True, **k: notes.append(msg) or True
        compose._leave_tree_clean("テスト: 号を作る前に停止")
    finally:
        compose.ROOT, pipelib.ROOT, compose.STARTED_ON, compose.STARTED_DATE, compose.notify, pipelib.notify = saved
        pipelib.STOPPING.clear()    # 途中終了の後始末(stop_groups)が立てた「終えるところ」を、後のテストのために戻す
    left = g("status", "--porcelain", "--untracked-files=all").stdout.splitlines()
    committed = set(g("show", "--name-only", "--format=", "HEAD").stdout.split())
    check(left == [" M scripts/other.py"], f"途中終了の後始末のあとに compose の成果物が残った/成果物でない変更まで commit した: {left} / {notes}")
    check({f"metrics/plan-{d}.json", "stock/stories.yml", f"docs/_posts/{d}-old.md"} <= committed
          and "scripts/other.py" not in committed and not any(c.startswith("metrics/work/") for c in committed),
          f"変わった成果物(未追跡・変更・削除)だけを commit していない: {sorted(committed)}")
    check(g("rev-parse", "HEAD").stdout == subprocess.run(["git", "rev-parse", branch], cwd=origin, capture_output=True, text=True).stdout,
          "途中終了の commit を push していない")
    check(not any("git add 失敗" in n or "commit/push できなかった" in n for n in notes), f"後始末の commit が失敗した: {notes}")
    check(any("成果物ではない変更" in n and "scripts/other.py" in n for n in notes), f"成果物でない変更を知らせない: {notes}")


def test_grok_deep_retry(tmp: Path):
    """深掘りで答えの無かった問いは、予算の範囲で1回だけ投げ直す。予算は1回目の配分と実際の検索回数の大きいほうを差し引いた残り。
    投げ直しが異常終了・書き出しが無いときは名指しで知らせる(監査指摘)。"""
    import collect

    def run(spent1: int, deep_max: int, deep2_fails: bool) -> tuple[list[str], list[str]]:
        root = tmp / f"{spent1}-{deep_max}-{deep2_fails}"
        (root / "candidates").mkdir(parents=True)
        notes, dives = [], []

        class P:
            pid = 0
            returncode = 0
            def wait(self, timeout=None): return 0
            def communicate(self, timeout=None): return None, None

        def fake_popen(args, cwd=None, **kw):
            wd = Path(cwd)
            urls = re.findall(r"(?m)^- url:\s*(\S+)", (wd / "x-posts.md").read_text(encoding="utf-8"))
            (wd / "items.json").write_text("[]", encoding="utf-8")
            (wd / "posts.json").write_text(json.dumps([{"url": u, "status": "none", "item": ""} for u in urls]), encoding="utf-8")
            if "-deep" not in wd.name:
                (wd / "deep.json").write_text(json.dumps([{"question": "元の告知", "why": "共有の文面だけ"}]), encoding="utf-8")
            return P()

        def basic(queries, outdir, errs):
            (outdir / "cg.md").write_text("### 1\n- url: https://x.com/a/status/9\n- 本文: 共有の文面\n", encoding="utf-8")
            return []

        def dive(queries, outdir, chosen, errs, suffix="-deep", retry=False):
            dives.append((suffix, retry, sum(len(v) for v in chosen.values())))
            if suffix == "-deep":
                (outdir / "cg-deep.md").write_text("見つからない\n", encoding="utf-8")
                return set()
            if deep2_fails:
                errs["cg"] = "時間切れ"
                return {"cg"}
            (outdir / "cg-deep2.md").write_text("### 1\n- url: https://x.com/off/status/1\n- 問い: 1\n- 本文: 公式の告知\n", encoding="utf-8")
            return set()
        counts = iter([{"cg": spent1}] + [{} for _ in range(10)])
        names = ("ROOT", "build_prompts", "grok_basic", "deep_dive_grok", "grok_week_usage", "grok_search_counts", "notify",
                 "prompt_file", "save_raw", "explore_workdir", "GROK_DEEP_MAX")
        saved = {n: getattr(collect, n) for n in names}
        saved_popen = collect.subprocess.Popen
        try:
            collect.ROOT = root
            collect.build_prompts = lambda: [{"key": "cg", "brand": "cg", "topic": "t"}]
            collect.grok_basic, collect.deep_dive_grok = basic, dive
            collect.grok_week_usage = lambda *a: 0
            collect.grok_search_counts = lambda since: next(counts)
            collect.notify = lambda job, msg, ok=True, require=False: notes.append(msg) or True
            collect.prompt_file = lambda date, name, prompt, base=None: "p"
            collect.save_raw = lambda *a, **kw: None
            collect.explore_workdir = lambda key: (root / "wd" / key).mkdir(parents=True) or root / "wd" / key
            collect.GROK_DEEP_MAX = deep_max
            collect.subprocess.Popen = fake_popen
            collect.run_explores(True, False)
        finally:
            for n, v in saved.items():
                setattr(collect, n, v)
            collect.subprocess.Popen = saved_popen
        return notes, dives
    # 予算4: 1回目に2回配って実際に2回 → 残り2で1問を投げ直す
    notes, dives = run(2, 4, False)
    check(dives == [("-deep", False, 1), ("-deep2", True, 1)], f"答えの無い問いを投げ直さない: {dives}")
    # 1回目が配分(2回)を超えて3回検索 → 残り1で投げ直さない(この回の上限 GROK_DEEP_MAX を超えない)
    notes, dives = run(3, 4, False)
    check(dives == [("-deep", False, 1)], f"1回目の超過を差し引かずに投げ直した: {dives}")
    # 投げ直しが時間切れで書き出しも無い → 名指しで知らせる
    notes, dives = run(2, 4, True)
    check(any("投げ直し" in m and "cg" in m for m in notes), f"投げ直しの失敗を知らせない: {notes}")


def test_lost_post_notified_once(tmp: Path):
    """X の投稿の喪失は、深掘りの確かめまで終えた最終結果で、投稿ごとに1度だけ通知する(当番の指摘 11fc3de74b。
    実測 2026-10-07: 通常の確かめで読めなかった投稿を深掘り前に通知し、深掘りでも読めずにもう一度通知した)。
    深掘りで済んだ投稿は失ったと言わない。深掘りの確かめの問い(使わない)が読めなくても通知しない。"""
    import collect
    def run(deep_status: str, first: str = "- url: https://x.com/a/status/9\n- 本文: 告知", tag: str = "") -> list[str]:
        root = tmp / (deep_status + tag)
        (root / "candidates").mkdir(parents=True)
        notes = []
        class P:
            pid = 0
            returncode = 0
            def wait(self, timeout=None): return 0
            def communicate(self, timeout=None): return None, None
        def fake_popen(args, cwd=None, **kw):
            wd = Path(cwd)
            urls = re.findall(r"(?m)^(?:- )?url:\s*(\S+)", (wd / "x-posts.md").read_text(encoding="utf-8"))
            deep_pass = "-deep" in wd.name
            st = (lambda u: deep_status if "status/9" in u else "none") if deep_pass else (lambda u: "unreadable" if "status/9" in u else "none")
            (wd / "items.json").write_text("[]", encoding="utf-8")
            (wd / "posts.json").write_text(json.dumps([{"url": u, "status": st(u), "item": ""} for u in urls]), encoding="utf-8")
            if not deep_pass:
                (wd / "deep.json").write_text(json.dumps([{"question": "元の投稿", "why": "引用だけ"}]), encoding="utf-8")
            return P()
        def basic(queries, outdir, errs):
            (outdir / "cg.md").write_text(f"### 1\n{first}\n\n### 2\n- url: https://x.com/a/status/2\n- 本文: 雑談\n", encoding="utf-8")
            return []
        def dive(queries, outdir, chosen, errs, suffix="-deep", retry=False):
            (outdir / f"cg{suffix}.md").write_text("### 1\n- url: https://x.com/a/status/9\n- 本文: 告知\n", encoding="utf-8")
            return set()
        names = ("ROOT", "build_prompts", "grok_basic", "deep_dive_grok", "grok_week_usage", "grok_search_counts", "notify",
                 "prompt_file", "save_raw", "explore_workdir")
        saved = {n: getattr(collect, n) for n in names}
        saved_popen = collect.subprocess.Popen
        try:
            collect.ROOT = root
            collect.build_prompts = lambda: [{"key": "cg", "brand": "cg", "topic": "t"}]
            collect.grok_basic, collect.deep_dive_grok = basic, dive
            collect.grok_week_usage, collect.grok_search_counts = (lambda *a: 0), (lambda *a: {})
            collect.notify = lambda job, msg, ok=True, require=False: notes.append(msg) or True
            collect.prompt_file = lambda date, name, prompt, base=None: "p"
            collect.save_raw = lambda *a, **kw: None
            collect.explore_workdir = lambda key: (root / "wd" / key).mkdir(parents=True) or root / "wd" / key
            collect.subprocess.Popen = fake_popen
            collect.run_explores(True, False)
        finally:
            for n, v in saved.items():
                setattr(collect, n, v)
            collect.subprocess.Popen = saved_popen
        return notes
    notes = run("unreadable")
    lost = [m for m in notes if "status/9" in m]
    check(len(lost) == 1 and lost[0].count("https://x.com/a/status/9") == 1 and "status/2" not in lost[0],
          f"深掘りでも読めない投稿の喪失を、最終結果で1度だけ通知しない: {notes}")
    check(not any("deep.json" in m for m in notes), f"使わない深掘りの確かめの問いが読めないことを通知した: {notes}")
    notes = run("none")
    check(not any("status/9" in m for m in notes), f"深掘りで済んだ投稿を失ったと通知した: {notes}")
    # 同じ投稿が段によって別の形の url で書かれる(2026-10-17: 返信先として x.com/i/status/<ID> で書き出され、深掘りでは
    # x.com/<アカウント>/status/<ID> で済んだのに、url の文字列が違うので「失った」と通知した)。投稿は status ID で見分ける
    notes = run("none", first="- url: https://x.com/a/status/1\n- 本文: 返信\n\n返信先:\nurl: https://x.com/i/status/9\n- リンク: なし", tag="-anon")
    check(not any("status/9" in m for m in notes), f"深掘りで原本の形の url で済んだ投稿を、別の形の url のまま失ったと通知した: {notes}")
    notes = run("unreadable", first="url: https://x.com/i/status/9\n- リンク: なし", tag="-anon")
    check(any("status/9" in m for m in notes), f"深掘りでも読めない投稿(別の形の url)の喪失を通知しない: {notes}")
    # 突き合わせ・重なりの除去も同じ鍵で: 結果が原本の形の url で返っても、その投稿は済み。同じ投稿の2つの形は1つにする
    anon = "### 1\nurl: https://x.com/i/status/9\n- 本文: x"
    check(collect.unaccounted_posts(anon, [{"url": "https://x.com/a/status/9", "status": "none", "item": ""}], []) == [],
          "結果が別の形の url で返った投稿を、未処理として残した")
    item = {"title": "#315ナイトの感想", "brand": "sidem", "kind": "fan", "url": "https://x.com/a/status/9", "event_date": "",
            "published_date": "2026-10-16", "deadline": "", "facts": ["ファン投稿は #315ナイト を付けている。"], "quote": "x",
            "dedup_key": "sidem-315night-fan", "engagement": "低", "mentioned_idols": []}
    check(collect.candidate_usable(item), "見本の候補が依頼した形でない(テストの前提)")
    check(collect.unaccounted_posts(anon, [{"url": "https://x.com/a/status/9", "status": "extracted", "item": "https://x.com/i/status/9"}],
                                    [item]) == [],
          "候補にした投稿(別の形の url)を、未処理として残した")
    check(len(collect.post_blocks(collect.dedupe_posts(anon + "\n\n### 2\n- url: https://x.com/a/status/9\n- 本文: x"))) == 1,
          "同じ投稿の別の形の url を1つにしない")
    # 重なりの除去は、本文なしの返信先(先)で、同じ投稿の本文付きの塊(後)を消さない(監査指摘 dedupe-discards-readable-post)。
    # 本文があれば候補に、無ければ読めないと答える確かめ役で、確かめまで通す
    reply = ("### 1\n- url: https://x.com/a/status/1\n- 本文: 返信\n\n返信先:\nurl: https://x.com/i/status/9\n- リンク: なし\n\n"
             "### 2\n- url: https://x.com/a/status/9\n- 本文: 告知の本文\n- リンク: なし")
    got = collect.post_blocks(collect.dedupe_posts(reply))
    check([collect.post_url(b) for b in got] == ["https://x.com/a/status/1", "https://x.com/a/status/9"] and "告知の本文" in got[1],
          f"本文なしの返信先が、同じ投稿の本文付きの塊を消した: {got}")
    vroot = tmp / "verify-order"
    (vroot / "cg.md").parent.mkdir(parents=True)
    (vroot / "cg.md").write_text(reply + "\n", encoding="utf-8")
    class VP:       # 起動は pipelib.start_group 越し(待ち・回収は finish_group が communicate・kill で行う)
        pid = 0
        returncode = 0
        def wait(self, timeout=None): return 0
        def poll(self): return 0
        def communicate(self, timeout=None): return "", ""
        def kill(self): pass
    def body_popen(args, cwd=None, **kw):
        wd = Path(cwd)
        res, its = [], []
        for b in collect.post_blocks((wd / "x-posts.md").read_text(encoding="utf-8")):
            u = collect.post_url(b)
            if "本文" in b:
                its.append({**item, "url": u, "title": f"投稿 {u}"})
                res.append({"url": u, "status": "extracted", "item": u})
            else:
                res.append({"url": u, "status": "unreadable", "item": ""})
        (wd / "items.json").write_text(json.dumps(its, ensure_ascii=False), encoding="utf-8")
        (wd / "posts.json").write_text(json.dumps(res), encoding="utf-8")
        return VP()
    vnames = ("prompt_file", "save_raw", "explore_workdir")
    vsaved = {n: getattr(collect, n) for n in vnames}
    vpopen = collect.subprocess.Popen
    try:
        collect.prompt_file = lambda date, name, prompt, base=None: "p"
        collect.save_raw = lambda *a, **kw: None
        collect.explore_workdir = lambda key: (vroot / "wd" / key).mkdir(parents=True, exist_ok=True) or vroot / "wd" / key
        collect.subprocess.Popen = body_popen
        ledger = collect.lost_posts_ledger()
        vitems, _ = collect.verify_grok_faces([{"key": "cg", "brand": "cg", "topic": "t"}], vroot, ledger=ledger)
    finally:
        for n, v in vsaved.items():
            setattr(collect, n, v)
        collect.subprocess.Popen = vpopen
    check(any(collect.post_key(x["url"]) == collect.post_key("https://x.com/a/status/9") for x in vitems) and not ledger["lost"],
          f"本文付きの塊を重なりの除去で落とし、候補にできなかった: {vitems} / {ledger['lost']}")
    check(collect.post_key("https://x.com/a/status/9?s=20") == collect.post_key("https://twitter.com/i/web/status/9")
          and collect.post_key("https://example.com/a") == "https://example.com/a", "投稿の鍵の取り方")
    # 面どうしで同じアカウントを検索するので、同じ投稿が複数の面で残る。面の数だけ通知しない(監査指摘 lost-post-cross-face)
    notes = []
    saved = collect.notify
    try:
        collect.notify = lambda job, msg, ok=True, require=False: notes.append(msg) or True
        u = "https://x.com/imas_official/status/1"
        ledger = collect.lost_posts_ledger()
        ledger["lost"]["765as"] = [(f"### 1\n- url: {u}\n- 本文: 告知", "時間切れ")]
        ledger["lost"]["joint-other"] = [(f"### 3\n- url: {u}\n- 本文: 告知", "時間切れ"),
                                         ("### 4\n- url: https://x.com/imas_official/status/7\n- 本文: 別", "時間切れ")]
        ledger["settled"].add(collect.post_key("https://x.com/imas_official/status/7"))
        collect.report_lost_posts(ledger)
    finally:
        collect.notify = saved
    check(len(notes) == 1 and "\n".join(notes).count(u) == 1 and "765as" in notes[0] and "joint-other" in notes[0]
          and "status/7" not in notes[0],
          f"複数の面で残った同じ投稿の喪失を、所属の面を併記して1度だけ通知しない: {notes}")


def test_official_topic_accepts_press():
    """公式が告知するはずの話題でも、報道があれば書く。執筆の出典の規則・見送りの理由コード・校閲の R5 が同じ基準(公式・準公式・報道)。
    2026-10-13: ゲーム内のお知らせだけのログボを、電ファミの報道があるのに見送った。編集長「信頼できるソースを無視すんな」。
    片方だけ直すと、報道で書いた記事を校閲が R5 で落とす。"""
    w = (pipelib.ROOT / "prompts" / "write-article.md").read_text(encoding="utf-8")
    r = (pipelib.ROOT / "prompts" / "review-article.md").read_text(encoding="utf-8")
    rule = next((l for l in w.splitlines() if "**公式が告知するはずの話題**" in l), "")
    code = next((l for l in w.splitlines() if l.startswith("- NO_PRIMARY_SOURCE:")), "")
    r5 = "\n".join(l for l in r.splitlines()[next((i for i, l in enumerate(r.splitlines()) if l.startswith("- R5 ")), 0):][:2])
    check("報道" in rule and "報道" in code and "報道" in r5, f"報道を認める基準が執筆・理由コード・校閲で揃っていない: {rule[:60]} / {code[:60]} / {r5[:80]}")
    # 報道は「公式の発表・ゲーム内のお知らせを報じたもの」に限る(ファンの投稿を集めた記事・転載は除く)。公式と食い違う値は公式を採る。両方とも
    # 執筆と校閲の両側にある(片側だけ旧状態に戻ると、まとめ記事や報道の誤った値で書いた記事が通る。監査指摘)。
    # 報道の範囲は執筆・校閲・書き直しで同じ書き方で、告知文の引用を条件にしない(2026-10-14: 「公式の告知を取材・引用した報道」を、校閲が
    # 「告知文を引用しておらず R5 は境界線上」と読んだ。規則が効くかは rule_bench の事例 review-article-press-only-no-r5 が見る)
    limit = "公式の発表・ゲーム内のお知らせを報じた報道"
    r5_full = "\n".join(r.splitlines()[next((i for i, l in enumerate(r.splitlines()) if l.startswith("- R5 ")), 0):][:3])
    rv = (pipelib.ROOT / "prompts" / "revise-article.md").read_text(encoding="utf-8")
    check(limit in w and limit in r5_full and limit in rv and "ファンの投稿を集めた記事・転載は除く" in w and "ファンの投稿を集めた記事・転載は除く" in r5_full,
          "報道の限定条件が執筆・校閲・書き直しに同じ書き方で無い")
    check(all("告知文を引用していなくてよい" in t for t in (w, r5_full, rv)) and not any("取材・引用" in t for t in (w, r, rv)),
          "報道の範囲に告知文の引用を条件と読める書き方が残っている")
    check("公式と食い違う値は、どの出典で書くときも公式を採る" in w and "食い違う値" in r5_full and "公式を採る" in r5_full,
          "公式と食い違う値は公式を採る、が執筆・校閲の両方に無い")
    # 報道で書く記事でも、報道の本文で確かめた具体を足せる(素材に無い事実の確かめの元に報道を含める。執筆・校閲・書き直し)
    check("3 で出典にできる報道" in w and "R5 の条件を満たす報道" in r and limit in rv,
          "報道の本文で確かめた事実を足せない(素材に無い事実の確かめの元が一次情報だけ)")


def test_review_paper_defines_same_subject():
    """紙面担当の P1 は「同じ知らせ」だけ。同じ催しの別の知らせを重複にしない(2026-10-07 MSP アンケート)。"""
    text = (pipelib.ROOT / "prompts" / "review-paper.md").read_text(encoding="utf-8")
    p1 = next((l for l in text.splitlines() if l.startswith("- P1 ")), "")
    check("同じ知らせ" in p1 and "別の主題" in p1, f"P1 に「同じ主題」の定義が無い: {p1}")


# 規則の事例(scripts/rule_bench.py)。上の2つの文言の検査は、逆の規則を書き足しても文言が残れば通る(監査指摘 2026-10-14)。
# 規則が効くかは rule_bench が実際のモデルに判断させて検める。ここでは事例の形と、答えを照らすコードの振る舞いだけを見る(モデルは呼ばない)
RULE_CASES = {
    "write-partial-mismatch-drops-one": "write", "write-press-only-writes": "write", "write-fan-matome-only-declines": "write",
    "write-press-price-conflict-uses-official": "write", "review-article-press-only-no-r5": "review_article",
    "review-article-fan-matome-only-r5": "review_article", "review-article-press-price-conflict-r5": "review_article",
    "review-paper-same-event-different-news-kept": "review_paper", "review-paper-same-news-twice-drops-one": "review_paper",
    "revise-press-price-conflict-uses-official": "revise",
    "write-linked-official-read-by-fetch-page": "write", "write-unreadable-link-not-cited": "write",
}


def test_rule_bench_cases():
    """事例ファイルが揃っていて形が正しい(素材・記事は書式どおり、期待の欄は事例の素材・記事を指す)。形の検査は崩れた事例を拒む。"""
    import copy
    import rule_bench
    cases = rule_bench.load_cases()
    check(set(RULE_CASES) <= set(cases), f"規則の事例が欠けている: {sorted(set(RULE_CASES) - set(cases))}")
    for n, kind in RULE_CASES.items():
        c = cases.get(n) or {}
        check(c.get("kind") == kind, f"{n} の kind: {c.get('kind')}")
        probs = rule_bench.case_problems(c) if c else ["無い"]
        check(not probs, f"{n} の形: {probs[:3]}")
    if not all(n in cases for n in RULE_CASES):
        return
    w = cases["write-partial-mismatch-drops-one"]
    # 期待の欄が読める: 外すべき素材の事実 id は2つ目の素材のもの(本番と同じ通し番号)、外すべき url は2つ目の素材の url
    ids = rule_bench.material_ids(w["materials"])
    check({ids[i] for i in w["expect"]["not_cite"]} == {w["materials"][1]["url"]} and w["expect"]["not_sources"] == [w["materials"][1]["url"]],
          f"一部の素材だけが食い違う事例の期待: {w['expect']}")
    check(cases["write-fan-matome-only-declines"]["expect"] == {"status": "decline", "decline_codes": ["NO_PRIMARY_SOURCE"]},
          "まとめ記事だけの事例は NO_PRIMARY_SOURCE の見送りを期待する")
    pair = cases["review-paper-same-news-twice-drops-one"]
    check(pair["expect"]["blockers"] == [{"rule": "P1", "files": [f"docs/_posts/{n}" for n in pair["posts"]], "n_files": 1}],
          f"同じ知らせの2本の事例の期待: {pair['expect']}")

    def broken(name, edit):
        c = copy.deepcopy(cases[name])
        edit(c)
        return rule_bench.case_problems(c)
    check(broken("write-partial-mismatch-drops-one", lambda c: c["expect"].update(not_cite=["F99"])), "素材に無い事実 id を期待にできる")
    check(broken("write-partial-mismatch-drops-one", lambda c: c["pages"].pop(c["materials"][0]["url"])), "素材の url の本文が無い事例を通す")
    check(broken("write-partial-mismatch-drops-one", lambda c: c["materials"][0].update(extra=1)), "書式に無い欄のある素材を通す")
    check(broken("write-fan-matome-only-declines", lambda c: c["expect"].pop("decline_codes")), "見送りの理由コードの無い期待を通す")
    check(broken("write-press-only-writes", lambda c: c["expect"].pop("sources")), "使う素材の期待が無い執筆の事例を通す")
    check(broken("review-paper-same-news-twice-drops-one", lambda c: c["expect"]["blockers"][0].update(files=["docs/_posts/x.md"])),
          "事例に無い記事を期待にできる")
    check(broken("review-article-press-only-no-r5", lambda c: c.update(candidates=[])), "記事の素材が無い校閲の事例を通す")
    check(broken("review-article-press-only-no-r5", lambda c: c["posts"].update({k: v.replace("rank: small", "rank: tiny") for k, v in c["posts"].items()})),
          "書式に合わない記事を通す")
    check(broken("review-paper-same-event-different-news-kept", lambda c: c.update(kind="review")), "知らない kind を通す")
    check(broken("write-press-price-conflict-uses-official", lambda c: c["expect"].update(cite_sources=["https://x.example/"])),
          "素材に無い出典を cite_sources にできる")
    check(broken("revise-press-price-conflict-uses-official", lambda c: c.update(issues=[])), "指摘の無い書き直しの事例を通す")
    check(broken("revise-press-price-conflict-uses-official", lambda c: c.update(slug="other")), "書き直す記事と slug が合わない事例を通す")
    # 素材の外のページの事例: 読めるリンク先(本文を持たせた素材の外のページ)を出典に期待する / 読めないリンク先(本文が無い)は見送りを期待する
    lo, ul = cases["write-linked-official-read-by-fetch-page"], cases["write-unreadable-link-not-cited"]
    off = (lo["expect"].get("sources") or [""])[0]
    check(off in lo["pages"] and off not in {m["url"] for m in lo["materials"]} and any(off in f for m in lo["materials"] for f in m["facts"]),
          f"読めるリンク先の事例: 期待の出典が、素材がリンクする素材の外のページ(本文あり)でない: {off}")
    link = re.findall(r"https://\S+", " ".join(f for m in ul["materials"] for f in m["facts"]))
    check(link and not any(u in ul["pages"] for u in link) and ul["expect"].get("status") == "decline",
          f"読めないリンク先の事例: リンク先の本文を持たせている・見送りを期待しない: {link}")
    check(broken("write-linked-official-read-by-fetch-page", lambda c: c["pages"].pop(off, None)), "期待の出典が素材にも pages にも無い事例を通す")


def test_rule_bench_judges_shape(tmp: Path):
    """答えの形の欄(執筆の status・decline_code・根拠の事実 id・出典の url / 校閲の blockers の rule_id と file)を期待と照らし、
    偽の答えで合格・不合格を正しく出す。本番の経路(執筆の依頼文・校閲の claude_review)を偽の答えで通しても同じ結果になる。"""
    import contextlib
    import copy
    import io
    import fetch_page
    import rule_bench
    import writer_bench
    cases = rule_bench.load_cases()
    if not all(n in cases for n in RULE_CASES):
        check(False, "規則の事例が欠けている")
        return
    w = cases["write-partial-mismatch-drops-one"]
    a_url, b_url = w["materials"][0]["url"], w["materials"][1]["url"]
    # 本番の検算に通る形の答え(形の不合格と規則の不合格を分けて見るため)
    good = {"status": "ok", "decline_code": "", "decline_detail": "", "title": "SideMぱしゃこれA-BOX予約", "title_fact_ids": ["F1"],
            "lede": "l", "lede_fact_ids": ["F1"], "blocks": [{"markdown": "m", "fact_ids": ["F2", "N1"]}], "tags": ["SideM", "予約"],
            "new_facts": [{"id": "N1", "text": "8,800円", "url": a_url}], "event_date": None, "addressed_issue_ids": [],
            "sources": [{"url": a_url.rstrip("/"), "label": "アニメイト通販「A-BOX」(2026年10月12日)"}]}
    jw = rule_bench.judge_write
    check(jw(w["expect"], good) == [], f"残りの素材で書いた答えを不合格にした: {jw(w['expect'], good)}")
    bad = copy.deepcopy(good)
    bad["blocks"][0]["fact_ids"].append("F6")
    check(jw(w["expect"], bad), "食い違う素材の事実を根拠にした答えを合格にした")
    bad = copy.deepcopy(good)
    bad["new_facts"].append({"id": "N2", "text": "販売終了", "url": b_url})
    check(jw(w["expect"], bad), "食い違う素材のページを読んで足した答えを合格にした")
    bad = copy.deepcopy(good)
    bad["sources"] = [{"url": b_url, "label": "y"}]
    check(len(jw(w["expect"], bad)) == 2, f"出典の取り違えを2点とも挙げない: {jw(w['expect'], bad)}")
    decl = {"status": "decline", "decline_code": "SOURCE_MISMATCH", "decline_detail": "B が販売終了"}
    check(jw(w["expect"], decl) and "decline" in jw(w["expect"], decl)[0], "素材ごと見送った答えを合格にした")
    check(jw(w["expect"], None), "答えが無いのを合格にした")
    m = cases["write-fan-matome-only-declines"]["expect"]
    check(jw(m, dict(decl, decline_code="NO_PRIMARY_SOURCE")) == [] and jw(m, decl) and jw(m, good),
          "まとめ記事だけの事例: NO_PRIMARY_SOURCE の見送りだけを合格にする")
    pc = cases["write-press-price-conflict-uses-official"]
    p, mats = pc["expect"], pc["materials"]
    off, press = mats[0]["url"], mats[1]["url"]
    ans = dict(good, title_fact_ids=["F1"], lede_fact_ids=["F3"], blocks=[{"markdown": "m", "fact_ids": ["F2"]}], new_facts=[],
               sources=[{"url": off, "label": "x"}])
    check(jw(p, ans, mats) == [], f"公式の価格で書いた答えを不合格にした: {jw(p, ans, mats)}")
    check(jw(p, dict(ans, lede_fact_ids=["F15"]), mats) and jw(p, dict(ans, lede_fact_ids=["F3", "F15"]), mats), "報道の食い違う価格を使った答えを合格にした")
    # 公式のページを読んで N に書いた価格を根拠にしても、規則どおり(F3 でなくてよい。監査指摘)
    via_n = dict(ans, lede_fact_ids=["N4"], title_fact_ids=["F12"], new_facts=[{"id": "N4", "text": "価格は19,800円(税込)", "url": off}])
    check(jw(p, via_n, mats) == [], f"公式を読んだ N で書いた答えを不合格にした: {jw(p, via_n, mats)}")
    only_press = dict(ans, lede_fact_ids=["F12"], title_fact_ids=["F16"], blocks=[], sources=[{"url": off, "label": "x"}, {"url": press, "label": "y"}])
    check(jw(p, only_press, mats), "公式の事実を根拠に使わない答えを合格にした")
    # 校閲: rule_id は頭の項目番号で読む(R15 は R1 ではない)
    check([rule_bench.rule_of(x) for x in ("P1", "P1 同じ主題", " R-5", "R15", "", None)] == ["P1", "P1", "R5", "R15", "", ""], "rule_id の読み方")
    pair = cases["review-paper-same-news-twice-drops-one"]
    fa, fb = (f"docs/_posts/{n}" for n in pair["posts"])
    blk = lambda rid, f: {"file": f, "issue": "i", "quote": "q", "rule_id": rid, "repair": "drop_article", "fact_ids": [], "expected": "e"}
    jr = rule_bench.judge_review
    check(jr(pair["expect"], {"verdict": "block", "blockers": [blk("P1", fb)]}) == [], "同じ知らせの1本を落とす答えを不合格にした")
    check(jr(pair["expect"], {"verdict": "block", "blockers": [blk("P1 同じ知らせ", fa), blk("P1", fa)]}) == [], "同じ1本への2つの P1 を不合格にした")
    check(jr(pair["expect"], {"verdict": "block", "blockers": [blk("P1", fa), blk("P1", fb)]}), "両方を落とす答えを合格にした")
    check(jr(pair["expect"], {"verdict": "approve", "blockers": []}), "同じ知らせを2本とも残す答えを合格にした")
    check(jr(pair["expect"], {"verdict": "block", "blockers": [blk("R1", fa)]}), "P1 でない指摘を合格にした")
    check(jr(pair["expect"], {"verdict": "error", "blockers": [], "failed": ["paper: 時間切れ"]}), "判定の無い校閲を合格にした")
    kept = cases["review-paper-same-event-different-news-kept"]
    k1 = f"docs/_posts/{next(iter(kept['posts']))}"
    check(jr(kept["expect"], {"verdict": "approve", "blockers": []}) == [] and jr(kept["expect"], {"verdict": "block", "blockers": [blk("R3", k1)]}) == [],
          "別の知らせを2本とも残す答えを不合格にした")
    check(jr(kept["expect"], {"verdict": "block", "blockers": [blk("P1", k1)]}), "別の知らせを P1 で落とす答えを合格にした")
    r5 = cases["review-article-press-price-conflict-r5"]
    f5 = f"docs/_posts/{next(iter(r5['posts']))}"
    check(jr(r5["expect"], {"verdict": "block", "blockers": [blk("R5", f5)]}) == [] and jr(r5["expect"], {"verdict": "block", "blockers": [blk("R1", f5)]}),
          "食い違う報道の価格の記事: R5 の指摘だけを合格にする")
    # 本番の経路を偽の答えで通す(モデルは呼ばない)。結果の置き場・作業フォルダ・依頼文のファイルは一時ディレクトリに作る
    saved = (rule_bench.OUT, pipelib.ROOT, compose.ROOT, writer_bench.write_one, compose.subprocess.Popen, os.environ.get(rule_bench.FIXED_PAGES_ENV))
    saved_stage = dict(compose.STAGE_MIN)      # 校閲は巡の実測を残す(ほかのテストの見積もりに混ぜない)
    seen = {}
    try:
        rule_bench.OUT = tmp / "out"
        pipelib.ROOT = compose.ROOT = tmp / "root"

        def fake_write(name, short, label, model):
            seen["write"] = (short, os.environ.get(rule_bench.FIXED_PAGES_ENV), model)
            return {"case": name, "answer": copy.deepcopy(good), "exit": 0}
        writer_bench.write_one = fake_write
        r = rule_bench.run_one("write-partial-mismatch-drops-one", "t1")
        short, pages, model = seen.get("write", ("", "", ""))
        prompt_path = re.search(r"`([^`]+\.md)`", short)
        prompt = Path(prompt_path.group(1)).read_text(encoding="utf-8") if prompt_path else ""
        check(r["pass"] and r["problems"] == [], f"偽の答え(残りの素材で書いた)が本番の執筆の経路で不合格: {r.get('problems')}")
        check(a_url in prompt and '"id": "F5"' in prompt and "## 素材" in prompt, "執筆の依頼文に素材が本番と同じ形(F 番号付き)で入っていない")
        check(model == pipelib.CODEX_WRITE_MODEL, f"執筆のモデルが本番と違う: {model}")
        check(pages and json.loads(Path(pages).read_text(encoding="utf-8")) == w["pages"], "執筆に事例の出典の本文が渡らない")
        writer_bench.write_one = lambda name, short, label, model: {"case": name, "answer": dict(decl), "exit": 0}
        r = rule_bench.run_one("write-partial-mismatch-drops-one", "t2")
        check(not r["pass"] and (rule_bench.OUT / "t2" / "write-partial-mismatch-drops-one.json").exists(), "見送った偽の答えを合格にした・結果が残らない")
        # 本番の検算に通らない答え(理由の無い見送り)は、規則の判断が期待どおりでも合格にしない(本番は差し戻す。監査指摘)
        writer_bench.write_one = lambda name, short, label, model: {
            "case": name, "answer": {**decl, "decline_code": "NO_PRIMARY_SOURCE", "decline_detail": ""}, "exit": 0}
        r = rule_bench.run_one("write-fan-matome-only-declines", "t2b")
        check(not r["pass"] and r["problems"] == [] and r["shape_problems"], f"本番の検算に通らない答えを合格にした: {r.get('shape_problems')}")
        # 答えの読み込みは本番と同じ(同じキーが2回ある答え・出力 schema に合わない答えは読めない答え)
        saved_run = writer_bench.run_group      # 執筆の起動は本番と同じ run_group(自前のグループ。時間切れで子孫まで止める)
        try:
            whole = json.dumps(good, ensure_ascii=False)
            off_schema = json.dumps(dict(good, addressed_issue_ids="I1"), ensure_ascii=False)
            for text, readable in (('{"status": "decline", ' + whole[1:], False), (off_schema, False), (whole, True)):
                def fake_run(cmd, timeout=None, **kw):
                    Path(cmd[cmd.index("--output-last-message") + 1]).write_text(text, encoding="utf-8")
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                writer_bench.run_group = fake_run
                got = saved[3]("x", "p", "t", "m")["answer"]
                check((got is not None) == readable, f"答えの読み込みが本番と違う(同じキーが2回ある答え・schema 違反・正しい答え): {text[:40]} → {got}")
        finally:
            writer_bench.run_group = saved_run
        # 書き直し: 本番と同じ依頼文(指摘に I1 を振る・現在の記事・素材)で書かせ、報道の食い違う価格を根拠に残さない答えだけを合格にする
        import yaml
        rv = rule_bench.load_cases(["revise-press-price-conflict-uses-official"])["revise-press-price-conflict-uses-official"]
        post_text = next(iter(rv["posts"].values()))
        fm = yaml.safe_load(post_text.split("---\n")[1])
        paras = [re.match(r"(.*) <!-- (.*) -->$", x, re.S) for x in post_text.split("\n---\n", 1)[1].strip().split("\n\n")]
        blocks = [{"markdown": m_.group(1), "fact_ids": m_.group(2).split()} for m_ in paras]
        rev_good = {"status": "ok", "decline_code": "", "decline_detail": "", "title": fm["title"], "title_fact_ids": fm["title_fact_ids"],
                    "lede": fm["lede"], "lede_fact_ids": fm["lede_fact_ids"], "tags": fm["tags"], "new_facts": [], "event_date": None,
                    "blocks": [{"markdown": blocks[0]["markdown"].replace("18,700", "19,800"), "fact_ids": ["F3", "F2", "F7"]}] + blocks[1:],
                    "sources": [{"url": s["url"], "label": s["label"]} for s in fm["sources"]], "addressed_issue_ids": ["I1"]}
        rev_keep = copy.deepcopy(rev_good)
        rev_keep["blocks"][0] = blocks[0]

        def fake_revise(answer):
            def f(name, short, label, model):
                seen["revise"] = Path(re.search(r"`([^`]+\.md)`", short).group(1)).read_text(encoding="utf-8")
                return {"case": name, "answer": copy.deepcopy(answer), "exit": 0}
            return f
        writer_bench.write_one = fake_revise(rev_good)
        r = rule_bench.run_one("revise-press-price-conflict-uses-official", "t6")
        rp = seen.get("revise", "")
        check(r["pass"], f"公式の価格に書き直した偽の答えを不合格にした: {r.get('problems')} {r.get('shape_problems')}")
        check('"issue_id": "I1"' in rp and '"rule_id": "R5"' in rp and fm["title"] in rp and '"id": "F15"' in rp,
              "書き直しの依頼文に、本番と同じ形の指摘(I1)・現在の記事・素材が入っていない")
        writer_bench.write_one = fake_revise(rev_keep)
        r = rule_bench.run_one("revise-press-price-conflict-uses-official", "t7")
        check(not r["pass"] and any("F15" in x for x in r["problems"]), f"報道の価格を残した書き直しを合格にした: {r.get('problems')}")
        writer_bench.write_one = fake_revise(dict(rev_good, addressed_issue_ids=[]))
        r = rule_bench.run_one("revise-press-price-conflict-uses-official", "t8")
        check(not r["pass"] and r["shape_problems"], f"本番の照合で戻る書き直し(対応していない指摘)を合格にした: {r.get('shape_problems')}")
        calls, said = [], [blk("P1", fb)]

        class FakeP:
            returncode, pid = 0, 0

            def __init__(self, argv, cwd=None, **k):
                calls.append({"argv": argv, "cwd": Path(cwd), "pages": os.environ.get(rule_bench.FIXED_PAGES_ENV),
                              "posts": sorted(p.name for p in (Path(cwd) / "docs" / "_posts").glob("*.md"))})

            def communicate(self, timeout=None):
                return (json.dumps({"verdict": "block", "comments": [], "blockers": said}), "")
        compose.subprocess.Popen = FakeP
        r = rule_bench.run_one("review-paper-same-news-twice-drops-one", "t3")
        c0 = calls[0] if calls else {}
        check(r["pass"], f"偽の答え(1本を落とす)が本番の校閲の経路で不合格: {r.get('problems')}")
        check(len(calls) == 1 and c0["posts"] == sorted(pair["posts"]) and c0["cwd"] == rule_bench.OUT / "t3" / "review-paper-same-news-twice-drops-one",
              f"校閲が事例の記事だけを置いた作業フォルダで動かない: {c0.get('cwd')} {c0.get('posts')}")
        check("--json-schema" in c0.get("argv", []) and pipelib.REVIEW_MODEL in c0.get("argv", []) and c0.get("pages"),
              "校閲の呼び出しが本番と違う(判定 schema・モデル)か、出典の本文が渡らない")
        check(pipelib.ROOT == compose.ROOT == tmp / "root", "校閲のあとに ROOT が戻らない")
        calls.clear()
        said[:] = [blk("P1", k1)]
        r = rule_bench.run_one("review-paper-same-event-different-news-kept", "t4")
        check(not r["pass"] and "P1" in " ".join(r["problems"]), f"別の知らせを落とす偽の答えを合格にした: {r.get('problems')}")
        # 事例の記事を指さない指摘は、本番の校閲がコメントに回す(落とす力を持たない)。照らすのは本番が動く形
        said[:] = [blk("P1", "docs/_posts/2026-10-07-not-in-case.md")]
        r = rule_bench.run_one("review-paper-same-event-different-news-kept", "t5")
        check(r["pass"] and r["answer"]["verdict"] == "approve", f"事例の記事を指さない指摘で不合格にした: {r.get('problems')}")
    finally:
        (rule_bench.OUT, pipelib.ROOT, compose.ROOT, writer_bench.write_one, compose.subprocess.Popen, env) = saved
        compose.STAGE_MIN.clear()
        compose.STAGE_MIN.update(saved_stage)
        if env is None:
            os.environ.pop(rule_bench.FIXED_PAGES_ENV, None)
        else:
            os.environ[rule_bench.FIXED_PAGES_ENV] = env
    # fetch_page.py は事例の本文を返す(取りに行かない・描画しない)。事例に無いページは取れない
    pages = tmp / "pages.json"
    pages.write_text(json.dumps({"https://a.example/x/": "告知\n\n\n\n受付期間：10月1日～10月12日\n本文"}, ensure_ascii=False), encoding="utf-8")
    saved_f = (fetch_page.fetch, fetch_page.needs_render, sys.argv, os.environ.get(fetch_page.FIXED_PAGES_ENV))

    def no_net(*a, **k):
        raise AssertionError("事例の本文を返すときに取りに行った")
    outs = []

    def run_fetch(u):
        buf = io.StringIO()
        sys.argv = ["fetch_page.py", u]
        with contextlib.redirect_stdout(buf):
            rc = fetch_page.main()
        return rc, buf.getvalue()
    wrong = []
    try:
        fetch_page.fetch = fetch_page.needs_render = no_net
        os.environ[fetch_page.FIXED_PAGES_ENV] = str(pages)
        for u in ("https://a.example/x", "https://a.example/other"):
            outs.append(run_fetch(u))
        # 全事例の固定の本文が、本番の出力の形(空行の連なりを詰める・上限で切る)以外は一字も変わらずに返る(監査指摘: 値の破損を見逃さない)
        for n, c in cases.items():
            pages.write_text(json.dumps(c["pages"], ensure_ascii=False), encoding="utf-8")
            for u, body in c["pages"].items():
                rc, out = run_fetch(u)
                want = re.sub(r"\n[ \t　]*(?:\n[ \t　]*)+", "\n\n", body).strip()[:40000]
                if rc != 0 or out.split("--- 本文(要約なし) ---\n", 1)[-1] != want + "\n":
                    wrong.append(f"{n}: {u}")
    finally:
        fetch_page.fetch, fetch_page.needs_render, sys.argv, env = saved_f
        if env is None:
            os.environ.pop(fetch_page.FIXED_PAGES_ENV, None)
        else:
            os.environ[fetch_page.FIXED_PAGES_ENV] = env
    check(outs and outs[0][0] == 0 and "--- 本文(要約なし) ---\n告知\n\n受付期間" in outs[0][1] and "受付期間" in outs[0][1].split("--- 本文")[0],
          f"事例の本文を本番と同じ形(期間・本文)で返さない: {outs[:1]}")
    check(len(outs) == 2 and outs[1][0] == 1 and outs[1][1].startswith("FETCH_FAILED"), f"事例に無いページを取れたことにした: {outs[1:]}")
    check(not wrong, f"事例の固定の本文が、そのままの中身で返らない: {wrong[:3]}")


def test_bench_tools_check_read_pages(tmp: Path):
    """執筆の品質の測定(writer_bench.metrics)と過去記事の書き直し(rewrite_past)も、本番と同じく根拠の URL を記録と照らす:
    空の記録なら素材の外のページを根拠にした答えを不合格にし(rewrite_past は記事を書き換えない)、読めた記録なら合格、記録の無い古い答え
    (None)は読めたかを見ない。rewrite_past は記事ごとの記録を検算に渡す(2026-10-14 監査指摘: この経路で記録を捨てても全テストが通った)"""
    import copy
    import rewrite_past
    import writer_bench
    unread_url = "https://unread.example.jp/news"
    mats, _ = renderlib.materials_with_ids(MATS)
    case = {"materials": mats, "rank": "small", "date": "2026-10-14"}
    ans = dict(OK, new_facts=[{"id": "N1", "text": "追加の事実", "url": unread_url}], blocks=[{"markdown": "x", "fact_ids": ["F1", "F3", "F4", "N1"]}],
               sources=OK["sources"] + [{"url": unread_url, "label": "追加の出典"}])
    unread = lambda ps: any("fetch_page.py で本文を取れていない" in p for p in ps)
    m = {str(f): writer_bench.metrics(case, copy.deepcopy(ans), fetched=f)["check_problems"] for f in ([], [unread_url], None)}
    check(unread(m["[]"]) and m[str([unread_url])] == [] and m["None"] == [],
          f"writer_bench の測定が根拠の URL を記録と照らさない(空・読めた・記録なし): {m}")
    p = tmp / "docs" / "_posts" / "2026-10-14-probe.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    got = {}
    for f in ([], [unread_url], None):
        write_checked_text = "---\n" + article_fm(slug="probe", edition="2026-10-14") + "---\n元の記事\n"
        pipelib.write_checked(p, write_checked_text)
        probs = rewrite_past.apply_answer(p, case, copy.deepcopy(ans), fetched=f)
        got[str(f)] = (unread(probs), p.read_text(encoding="utf-8") != write_checked_text)
    check(got == {"[]": (True, False), str([unread_url]): (False, True), "None": (False, True)},
          f"rewrite_past が根拠の URL を記録と照らさない(不合格か・記事を書き換えたか。空・読めた・記録なし): {got}")
    # 書かせた答えの記録は記事ごとに apply_answer へ渡る(生の出力から読み直した答えは記録が無いので None)
    seen = {}
    saved = (rewrite_past.ROOT, rewrite_past.apply_answer, compose.claude_review)
    pipelib.write_checked(p.with_name("2026-10-14-raw.md"), "---\n" + article_fm(slug="raw", edition="2026-10-14") + "---\n元の記事\n")

    def fake_apply(path, c, a, fetched=None):
        seen[path.name] = fetched
        return ["検算不合格(テスト)"]
    try:
        rewrite_past.ROOT, rewrite_past.apply_answer = tmp, fake_apply
        compose.claude_review = lambda *a, **k: {"verdict": "approve", "blockers": []}
        rewrite_past.replace_and_review({p.name: {"status": "ok"}, "2026-10-14-raw.md": {"status": "ok"}}, {p.name: case, "2026-10-14-raw.md": case},
                                        {}, {}, {}, set(), {p.name: [unread_url]})
    finally:
        rewrite_past.ROOT, rewrite_past.apply_answer, compose.claude_review = saved
    check(seen == {p.name: [unread_url], "2026-10-14-raw.md": None}, f"rewrite_past が記事ごとの記録を検算に渡さない: {seen}")


def test_rule_bench_checks_read_pages(tmp: Path):
    """規則の事例でも本番と同じく、根拠の URL をセッションの記録と照らす: writer_bench.write_one は子に記録の置き場を渡し、子の fetch_page.py が
    本文を取れたページを返す。rule_bench はそれで本番の検算を掛け、読んでいないページを根拠にした答えを「形」の不合格にする
    (2026-10-14: 素材の外の読めないページを出典にした答えが、本番の検算を通る形のまま合格・不合格を判定されていた)"""
    import copy
    import rule_bench
    import writer_bench
    cases = rule_bench.load_cases()
    names = ("write-linked-official-read-by-fetch-page", "write-unreadable-link-not-cited")
    if not all(n in cases for n in names):
        check(False, f"素材の外のページの事例が無い: {names}")
        return
    off = cases[names[0]]["expect"]["sources"][0]
    link = re.findall(r"https://\S+", " ".join(f for m in cases[names[1]]["materials"] for f in m["facts"]))[0]
    good = {"status": "ok", "decline_code": "", "decline_detail": "", "title": "ミリシタ、秋のしおりログボ", "title_fact_ids": ["N1"],
            "lede": "l", "lede_fact_ids": ["N1"], "blocks": [{"markdown": "m", "fact_ids": ["N1", "F2"]}], "tags": ["ミリシタ", "ログインボーナス"],
            "new_facts": [{"id": "N1", "text": "10月20日14:59まで", "url": off}], "event_date": "2026-10-12", "addressed_issue_ids": [],
            "sources": [{"url": off, "label": "アイドルマスター ポータル「秋のしおり集めログインボーナス」(2026年10月12日)"}]}
    saved = (rule_bench.OUT, pipelib.ROOT, compose.ROOT, writer_bench.write_one, writer_bench.run_group, os.environ.get(rule_bench.FIXED_PAGES_ENV))
    try:
        rule_bench.OUT = tmp / "out"
        pipelib.ROOT = compose.ROOT = tmp / "root"
        # 素材がリンクする告知を読んで書いた答え: 読めた記録があれば合格、記録が無ければ「形」の不合格(本番なら差し戻し)
        for fetched, ok in (([off], True), ([], False)):
            writer_bench.write_one = lambda name, short, label, model, f=fetched: {"case": name, "answer": copy.deepcopy(good), "exit": 0,
                                                                                   "fetched": list(f)}
            r = rule_bench.run_one(names[0], f"rp{len(fetched)}")
            check(r["pass"] == ok and r["problems"] == [] and bool(r["shape_problems"]) != ok and r.get("fetched") == fetched,
                  f"読めた記録 {fetched} で、告知を読んで書いた答えの判定が違う: {r.get('pass')} {r.get('problems')} {r.get('shape_problems')}")
        # 読めないリンク先を出典にして書いた答え: 規則(見送り)の不合格と、形(読めていないページ)の不合格の両方
        bad = dict(good, new_facts=[{"id": "N1", "text": "t", "url": link}], sources=[{"url": link, "label": "公式のお知らせ"}])
        writer_bench.write_one = lambda name, short, label, model: {"case": name, "answer": copy.deepcopy(bad), "exit": 0, "fetched": []}
        r = rule_bench.run_one(names[1], "ul")
        check(not r["pass"] and r["problems"] and any(link in x for x in r["shape_problems"]),
              f"読めないリンク先を出典にした答えを、形の不合格にしない: {r.get('problems')} {r.get('shape_problems')}")
        # 書き直し(run_revise)も本番の revise_apply と同じ: 素材の外のページは、このセッションで読めたか、現在の稿の根拠(verified_facts)に
        # あるときだけ根拠にできる(2026-10-14 監査指摘: run_revise の検算で記録を捨てても全テストが通った)
        import yaml
        rv_name = "revise-press-price-conflict-uses-official"
        rv = copy.deepcopy(rule_bench.load_cases([rv_name])[rv_name])
        (pn, post_text), = rv["posts"].items()
        mt = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", post_text, re.S)
        fm = yaml.safe_load(mt.group(1))
        draft_page, new_page = "https://shop.example.jp/figure/1", "https://unread.example.jp/figure"
        fm["verified_facts"] = [{"id": "N1", "text": "予約特典はアクリル麻雀牌", "url": draft_page}]
        rv["posts"] = {pn: "---\n" + compose.yaml_dump_keeping_strings(fm) + "---\n" + mt.group(2)}
        paras = [re.match(r"(.*) <!-- (.*) -->$", x, re.S) for x in mt.group(2).strip().split("\n\n")]
        blocks = [{"markdown": p_.group(1), "fact_ids": p_.group(2).split()} for p_ in paras]
        rev = {"status": "ok", "decline_code": "", "decline_detail": "", "title": fm["title"], "title_fact_ids": fm["title_fact_ids"],
               "lede": fm["lede"], "lede_fact_ids": fm["lede_fact_ids"], "tags": fm["tags"], "event_date": None, "addressed_issue_ids": ["I1"],
               "new_facts": [{"id": "N1", "text": "予約特典はアクリル麻雀牌", "url": draft_page}],
               "blocks": [{"markdown": blocks[0]["markdown"].replace("18,700", "19,800"), "fact_ids": ["F3", "F2", "F7", "N1"]}] + blocks[1:],
               "sources": [{"url": s["url"], "label": s["label"]} for s in fm["sources"]]}
        with_new = dict(rev, new_facts=rev["new_facts"] + [{"id": "N2", "text": "受注は11月まで", "url": new_page}],
                        blocks=[dict(rev["blocks"][0], fact_ids=rev["blocks"][0]["fact_ids"] + ["N2"])] + rev["blocks"][1:],
                        sources=rev["sources"] + [{"url": new_page, "label": "受注のページ"}])
        saved_load = rule_bench.load_cases
        try:
            rule_bench.load_cases = lambda names=None: {rv_name: copy.deepcopy(rv)}
            got = {}
            for key, answer, fetched in (("写した", rev, []), ("未読", with_new, []), ("読めた", with_new, [new_page])):
                writer_bench.write_one = lambda name, short, label, model, a=answer, f=fetched: {"case": name, "answer": copy.deepcopy(a), "exit": 0,
                                                                                                 "fetched": list(f)}
                r = rule_bench.run_one(rv_name, f"rv-{len(got)}")
                got[key] = (r["pass"], any("fetch_page.py で本文を取れていない" in x for x in r["shape_problems"]), r["shape_problems"])
        finally:
            rule_bench.load_cases = saved_load
        check({k: v[:2] for k, v in got.items()} == {"写した": (True, False), "未読": (False, True), "読めた": (True, False)},
              f"書き直しの事例の検算が、読めたページ・現在の稿の根拠を本番と同じに扱わない: {got}")
        # writer_bench.write_one(本物)は子に記録の置き場を渡し、子が書いたページを返して、記録を消す
        writer_bench.write_one = saved[3]
        seen = {}

        def fake_run(cmd, timeout=None, **kw):     # 執筆の起動は本番と同じ run_group(組 P)
            seen["rec"] = (kw.get("env") or {}).get(pipelib.FETCH_RECORD_ENV, "")
            if seen["rec"]:
                Path(seen["rec"]).write_text(json.dumps({"url": off, "final": None}) + "\n", encoding="utf-8")
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        writer_bench.run_group = fake_run
        got = writer_bench.write_one("x", "p", "t", "m")
        rec = Path(seen.get("rec") or "/nonexistent")
        check(got.get("fetched") == [off] and got.get("answer") == good and not rec.exists()
              and rec.parent == tmp / "root" / "metrics" / "work" / "bench" / "fetched",
              f"write_one が子に記録の置き場を渡さない・読めたページを返さない・記録を残す: {seen} {got.get('fetched')}")
    finally:
        (rule_bench.OUT, pipelib.ROOT, compose.ROOT, writer_bench.write_one, writer_bench.run_group, env) = saved
        if env is None:
            os.environ.pop(rule_bench.FIXED_PAGES_ENV, None)
        else:
            os.environ[rule_bench.FIXED_PAGES_ENV] = env


def test_job_lock():
    # 本物の作業ツリーの排他(metrics/jobs.lock)は使わない: 走っている工程やもう1本のテストとぶつかって、テストが揺れる
    # (2026-10-13: selfcheck を2本同時に流すと、片方が「他の工程が作業ツリーを使っている」で赤になった)
    td = Path(tempfile.mkdtemp())
    (td / "metrics").mkdir()
    code = ("import sys; from pathlib import Path; sys.path.insert(0, %r)\nimport pipelib\npipelib.ROOT = Path(%r)\n"
            "try:\n    pipelib.job_lock('t', wait_min=0); print('GOT')\n"
            "except pipelib.JobLockTimeout: print('TIMEOUT')") % (str(Path(__file__).resolve().parent), str(td))
    saved = pipelib.ROOT
    try:
        pipelib.ROOT = td
        fd = pipelib.job_lock("t")
    finally:
        pipelib.ROOT = saved
    check(fd is not None, "job_lock が取れない")
    run = lambda env=None: subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env).stdout.strip()
    check(run() == "TIMEOUT", "他プロセスが持っているのに取れた")
    check(run({**os.environ, "IMAS_JOB_LOCK": "held"}) == "GOT", "held の子が待ってしまう")
    os.close(fd)
    check(run() == "GOT", "離したのに取れない")


def test_job_lock_collect_oncall_handover():
    """03:00 の組版が待ち始めた時点で印が無く、03:02 に収集が当番を呼んだ(受け渡し)。組版は待つ分を見直し、
    収集が排他を離してから当番が取るまでの間も取らずに譲り、当番が取り直しを終えてから始まる(監査指摘)。
    時間は縮めて試す(分→0.1秒)。"""
    import threading
    import time
    import collect
    saved = (pipelib.COLLECT_ONCALL_MARK, pipelib.LOCK_POLL_SEC, collect.diagnose_anomalies, collect.collect_oncall_end, pipelib.ROOT)
    with tempfile.TemporaryDirectory() as td:
        try:
            pipelib.ROOT = Path(td)      # 本物の作業ツリーの排他は使わない(走っている工程・もう1本のテストとぶつかる)
            (Path(td) / "metrics").mkdir()
            pipelib.COLLECT_ONCALL_MARK = Path(td) / "mark.json"
            # 終える時刻は「いまから35分後」に固定(時刻表による計算は固定日時のテストで検める。走らせる時刻に依存させない。監査指摘)
            collect.collect_oncall_end = lambda now: time.time() + 35 * 60
            pipelib.LOCK_POLL_SEC = 0.02
            order = []
            collect_fd = pipelib.job_lock("collect")
            scaled = lambda t0: pipelib.compose_lock_wait_min(t0) / 600   # 10分→1秒、40分→4秒

            def compose_side():
                try:
                    fd = pipelib.job_lock("compose", wait_min=10 / 600, wait_fn=scaled,
                                          yield_fn=pipelib.collect_oncall_handover_pending)
                    order.append("compose")
                    os.close(fd)
                except pipelib.JobLockTimeout:
                    order.append("compose-timeout")

            def oncall_side():
                time.sleep(1.4)          # 当番の起動が遅く、収集が離してから(組版の最初の待ち 1秒も過ぎて)取りに来る
                fd = pipelib.job_lock("oncall", wait_min=1)
                pipelib.mark_collect_oncall(time.time() + 35 * 60, "2026-10-09", held=True)
                order.append("oncall")
                time.sleep(0.5)          # 取り直し
                pipelib.COLLECT_ONCALL_MARK.unlink()
                os.close(fd)

            tc = threading.Thread(target=compose_side)
            tc.start()
            time.sleep(0.2)              # 組版が印の無いまま待ち始めてから
            collect.diagnose_anomalies = lambda *a, **k: True
            collect.hand_to_oncall()     # 収集が当番を呼び、排他を持ったまま印を置く
            m = json.loads(pipelib.COLLECT_ONCALL_MARK.read_text(encoding="utf-8"))
            check(m["held"] is False and m["end_at"] > time.time(), f"収集が置く印: {m}")
            to = threading.Thread(target=oncall_side)
            to.start()
            time.sleep(0.1)
            os.close(collect_fd)         # 収集が終わって排他が空く(当番はまだ取りに来ていない)
            tc.join(10)
            to.join(10)
            check(order == ["oncall", "compose"], f"組版が収集→当番の受け渡しに割り込んだ・待ちを見直さない: {order}")
            # 当番を呼べなかったら印を置かない(組版を待たせない)
            collect.diagnose_anomalies = lambda *a, **k: False
            collect.hand_to_oncall()
            check(not pipelib.COLLECT_ONCALL_MARK.exists(), "当番を呼べないのに印を置いた")
            # 当番が起動できずに死んだときは、受け渡しの猶予で譲るのをやめる
            pipelib.COLLECT_ONCALL_MARK.write_text(json.dumps({"end_at": time.time() + 3600, "handover_at": time.time() - pipelib.COLLECT_HANDOVER_SEC - 1,
                                                               "held": False}), encoding="utf-8")
            check(not pipelib.collect_oncall_handover_pending(), "受け渡しの猶予を過ぎても組版が譲り続ける")
            # 当番が印を上書きしている最中に組版が読んでも、受け渡し中と見える(書きかけの空の印を読ませない。監査指摘)
            pipelib.mark_collect_oncall(time.time() + 33 * 60, "2026-10-09")
            seen, real_replace = [], os.replace

            def watch_replace(src, dst):
                seen.append(pipelib.collect_oncall_handover_pending())   # 差し替える直前に、組版が読む
                real_replace(src, dst)
            os.replace = watch_replace
            try:
                pipelib.mark_collect_oncall(time.time() + 33 * 60, "2026-10-09")
            finally:
                os.replace = real_replace
            check(seen == [True] and pipelib.collect_oncall_handover_pending(), f"印の上書きの最中に組版が受け渡しを見落とす: {seen}")
            check(not list(Path(td).glob(".mark.json.*")), "印の書きかけのファイルが残った")
        finally:
            pipelib.COLLECT_ONCALL_MARK, pipelib.LOCK_POLL_SEC, collect.diagnose_anomalies, collect.collect_oncall_end, pipelib.ROOT = saved
    # 本物の当番(oncall.main)が、親の収集がまだ排他を持っている間に起動しても、取り直せる時刻まで待つ
    # (02:00 の収集が 03:02 に呼ぶ: 終える時刻 03:35 → 取れた時点で30分残る 03:05 まで約3分待つ。以前は0分で即諦めた。監査指摘)
    import oncall
    saved_o = (sys.argv, oncall.job_lock, oncall.notify, oncall.COLLECT_ONCALL_MARK, pipelib.COLLECT_ONCALL_MARK,
               oncall.STAGE_END_AT, oncall.DEADLINE_AT)   # main が書き換える期限も戻す(後の当番のテストが時間切れ扱いになる)
    with tempfile.TemporaryDirectory() as td:
        waits = []

        def busy_lock(job, wait_min=0, **k):
            waits.append(wait_min)
            raise pipelib.JobLockTimeout("親の収集が持っている")
        try:
            oncall.COLLECT_ONCALL_MARK = pipelib.COLLECT_ONCALL_MARK = Path(td) / "mark.json"
            oncall.job_lock, oncall.notify = busy_lock, lambda *a, **k: True
            for left_min, lo, hi in ((33, 2.9, 3.1), (5 * 60, 90, 90), (20, 0, 0)):
                sys.argv = ["oncall.py", "--stage", "collect", "--date", "2026-10-09", "--reason", "x",
                            "--end-at", str(int(time.time() + left_min * 60))]
                oncall.main()
                check(lo <= waits[-1] <= hi, f"収集の当番が親の排他を待つ分(残り{left_min}分): {waits[-1]}")
        finally:
            (sys.argv, oncall.job_lock, oncall.notify, oncall.COLLECT_ONCALL_MARK, pipelib.COLLECT_ONCALL_MARK,
             oncall.STAGE_END_AT, oncall.DEADLINE_AT) = saved_o


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


def test_compose_carry_review():
    """承認済みの号に落とした記事を戻す組み直し(--reuse-plan --carry-review)は、承認済みの記事の判定を引き継ぎ、今回書いた記事
    だけを校閲する。全部を見直すと承認済みの記事に新しいブロックが付き、戻すために承認済みの記事を失う(2026-10-10 の救済)。
    承認でない結果・--reuse-plan でない実行では引き継がない(全部を校閲する)。"""
    import inspect
    src = inspect.getsource(compose.main)
    i_retarget, i_carry = src.find("retarget: list[str] | None = None"), src.find("retarget = carry_targets(date, carried)")
    i_loop = src.find("for rounds in range(1, args.max_rounds + 2):")
    check(0 <= i_retarget < i_carry < i_loop and "args.reuse_plan and carry_usable(c, date)" in src
          and "bool(retarget) or force_paper)" in src, "校閲の往復の前に、承認を引き継いで変わった記事に絞っていない・紙面担当を走らせない")
    d = "2099-01-01"
    ok = {"verdict": "approve", "blockers": [], "comments": [], "hashes": {f"docs/_editions/{d}.md": "e"}}
    check(compose.carry_usable(ok, d), "この号の承認を引き継げない")
    for bad, what in (({**ok, "verdict": "block"}, "承認でない"), ({**ok, "hashes": {"docs/_editions/2099-01-02.md": "e"}}, "別の号"),
                      ({k: v for k, v in ok.items() if k != "hashes"}, "指紋が無い"), ([], "形が違う")):
        check(not compose.carry_usable(bad, d), f"引き継げない校閲結果({what})を引き継ぐ")
    saved = (compose.ROOT, pipelib.ROOT)
    with tempfile.TemporaryDirectory() as td:
        try:
            compose.ROOT = pipelib.ROOT = Path(td)
            (Path(td) / "docs" / "_posts").mkdir(parents=True)
            (Path(td) / "metrics").mkdir()
            posts = Path(td) / "docs" / "_posts"
            for s, body in (("a", "承認済み"), ("b", "承認済み"), ("c", "今回書いた")):
                (posts / f"{d}-{s}.md").write_text(f"---\nslug: {s}\ntitle: {s}\n---\n{body}\n", encoding="utf-8")
            approved = {k: v for k, v in pipelib.review_manifest(d).items() if not k.endswith("-c.md")}
            (posts / f"{d}-b.md").write_text("---\nslug: b\ntitle: b\n---\n組み直しの途中で変わった\n", encoding="utf-8")
            got = compose.carry_targets(d, {**ok, "hashes": approved})
            check(got == [f"{d}-b.md", f"{d}-c.md"], f"承認時と中身が違う記事(今回書いた・途中で変わった)だけを校閲に回さない: {got}")
            # 承認を引き継いだ巡(strict)で、変わった記事の担当が2回とも動かず、紙面全体の担当だけ承認した → 承認にしない
            # (前の承認で補うと、変わった中身を見ないまま承認になり、release も新しい指紋で通す。監査指摘)
            first: list[str] = []

            class FakeP:
                def __init__(self, ok_):
                    self.ok, self.returncode, self.pid = ok_, (0 if ok_ else 1), 0

                def communicate(self, timeout=None):
                    return ((json.dumps({"verdict": "approve", "blockers": [], "comments": []}), "") if self.ok else ("", "boom"))

            def fake_popen(argv, **k):
                prompt = Path(argv[2]).read_text(encoding="utf-8") if Path(argv[2]).exists() else argv[2]
                first.append(first[0] if first else prompt)
                return FakeP(prompt != first[0])          # 最初の担当(変わった記事)だけ、やり直しも含めて失敗
            saved_popen = compose.subprocess.Popen
            compose.subprocess.Popen = fake_popen
            try:
                r = compose.claude_review(d, 1, targets=[f"{d}-b.md"], editorial=False, paper=True, strict=True,
                                          carry={**ok, "reviewed": [f"article:{d}-b.md"]})
            finally:
                compose.subprocess.Popen = saved_popen
            rec = json.loads((Path(td) / "metrics" / f"review-{d}-1.json").read_text(encoding="utf-8"))
            check(r.get("verdict") == "error" and rec.get("verdict") == "error",
                  f"引き継いだ巡で担当が動かなかったのに承認にした(記録 {rec.get('verdict')}): {r.get('verdict')} {r.get('failed')}")
            r = compose.claude_review("2099-01-01", 1, targets=[], editorial=False, paper=False,
                                      carry={"verdict": "approve", "blockers": [], "comments": [], "reviewed": ["article:x.md"]})
            check(r.get("verdict") == "approve" and "article:x.md" in (r.get("reviewed") or []),
                  f"今回書いた記事が無い組み直しで、引き継いだ承認が残らない: {r}")
        finally:
            compose.ROOT, pipelib.ROOT = saved


def test_compose_carry_kept_paper():
    """落とした記事を戻す組み直しで、紙面担当に承認済みのまま残す記事を名指しし、P1(重複)で落とすほうにさせない。
    名指しが無いと、戻した記事と承認済みの記事の重複で承認済みのほうを落とし、組み直しごと控えに戻された
    (2026-10-12: 宇宙誕生祭の上映会。同じ組み直しで戻ったレトラの記事も失った)。"""
    import inspect
    src = inspect.getsource(compose.main)
    check(src.count("kept=carry_kept(date, carried) if carried else None") == 3,
          "組み直しの紙面担当の呼び出し(往復・取り直し・ブロック記事の除外のあと)に、承認済みの記事を渡していない")
    check("承認済みの記事" in (compose.PROMPTS / "review-paper.md").read_text(encoding="utf-8"),
          "紙面担当の指示に、承認済みの記事を落とすほうにしない決まりが無い")
    d = "2099-01-01"
    ok = {"verdict": "approve", "blockers": [], "comments": [], "hashes": {f"docs/_editions/{d}.md": "e"}}
    saved = (compose.ROOT, pipelib.ROOT)
    with tempfile.TemporaryDirectory() as td:
        try:
            compose.ROOT = pipelib.ROOT = Path(td)
            posts = Path(td) / "docs" / "_posts"
            posts.mkdir(parents=True)
            (Path(td) / "metrics").mkdir()
            for s_, body in (("a", "承認済み"), ("b", "承認済み"), ("c", "今回書いた")):
                (posts / f"{d}-{s_}.md").write_text(f"---\nslug: {s_}\ntitle: {s_}\n---\n{body}\n", encoding="utf-8")
            approved = {k: v for k, v in pipelib.review_manifest(d).items() if not k.endswith("-c.md")}
            (posts / f"{d}-b.md").write_text("---\nslug: b\ntitle: b\n---\n組み直しの途中で変わった\n", encoding="utf-8")
            kept = compose.carry_kept(d, {**ok, "hashes": approved})
            check(kept == [f"{d}-a.md"], f"承認時から変わっていない記事だけを承認済みとしない: {kept}")

            class FakeP:
                returncode, pid = 0, 0

                def communicate(self, timeout=None):
                    return (json.dumps({"verdict": "approve", "blockers": [], "comments": []}), "")
            saved_popen = compose.subprocess.Popen
            compose.subprocess.Popen = lambda argv, **k: FakeP()
            try:
                compose.claude_review(d, 1, targets=[], editorial=False, paper=True, kept=kept)
            finally:
                compose.subprocess.Popen = saved_popen
            paper = [t for t in (f.read_text(encoding="utf-8") for f in (Path(td) / "metrics" / "work" / d).glob("*.md"))
                     if "紙面全体" in t and "P1" in t]
            sec = paper[0].split("## 承認済みの記事", 1)[-1] if paper and "## 承認済みの記事" in paper[0] else ""
            check(f"docs/_posts/{d}-a.md" in sec and f"{d}-b.md" not in sec and f"{d}-c.md" not in sec,
                  f"紙面担当の依頼に承認済みの記事が名指しされない: {sec!r}")
        finally:
            compose.ROOT, pipelib.ROOT = saved



def test_review_reads_sources_like_writer():
    """校閲は出典を執筆と同じ fetch_page.py で読む。WebFetch は JS を描画せず、公式ポータルの告知が見出しだけに見えて、
    執筆が読めた事実を「確認できない」と3巡ブロックし、スタマス5周年の IM＠S RADIO の記事が落ちた(2026-10-13)"""
    import contextlib, io
    import fetch_page
    d = "2099-01-01"
    argvs = []
    saved = (compose.ROOT, pipelib.ROOT)
    with tempfile.TemporaryDirectory() as td:
        try:
            compose.ROOT = pipelib.ROOT = Path(td)
            posts = Path(td) / "docs" / "_posts"
            posts.mkdir(parents=True)
            (Path(td) / "metrics").mkdir()
            (posts / f"{d}-a.md").write_text("---\nslug: a\ntitle: a\n---\n本文\n", encoding="utf-8")

            class FakeP:
                returncode, pid = 0, 0

                def communicate(self, timeout=None):
                    return (json.dumps({"verdict": "approve", "blockers": [], "comments": []}), "")
            saved_popen = compose.subprocess.Popen
            compose.subprocess.Popen = lambda argv, **k: argvs.append(argv) or FakeP()
            try:
                compose.claude_review(d, 1, targets=[f"{d}-a.md"], editorial=False, paper=False)
            finally:
                compose.subprocess.Popen = saved_popen
            prompts = [f.read_text(encoding="utf-8") for f in (Path(td) / "metrics" / "work" / d).glob("review-*.md")]
        finally:
            compose.ROOT, pipelib.ROOT = saved
    check(len(argvs) == 1, f"記事1本の校閲が1回起動しない: {len(argvs)}")
    a = argvs[0] if argvs else []
    denied = a[a.index("--disallowedTools") + 1] if "--disallowedTools" in a else ""
    check("WebFetch" in denied.split(","), f"校閲が WebFetch で出典を読める(JS を描画しない): {a}")
    check(prompts and "scripts/fetch_page.py" in prompts[0], "校閲の依頼に出典の読み方(fetch_page.py)が無い")
    # 描画したページの空行の連なりを詰める。行数で切って読んでも本文が見える
    page = "【生配信】IM＠S RADIO | 【公式】アイドルマスター ポータル" + "\n \n" * 150 + "パーソナリティ 平田 宏美(菊地 真役)\n" + "本文。" * 200
    saved_f = (fetch_page.fetch, sys.argv)
    buf = io.StringIO()
    try:
        fetch_page.fetch = lambda url: page
        sys.argv = ["fetch_page.py", "https://example.com/news/1"]
        with contextlib.redirect_stdout(buf):
            fetch_page.main()
    finally:
        fetch_page.fetch, sys.argv = saved_f
    head = "\n".join(buf.getvalue().splitlines()[:10])
    check("菊地 真役" in head, f"空行の連なりで本文が先頭10行に入らない: {head!r}")
    # 素材の一部だけが食い違う記事は、残りの素材で書く(2026-10-13: ぱしゃこれ B-BOX が販売終了で、A-BOX ごと見送った)
    w = (compose.PROMPTS / "write-article.md").read_text(encoding="utf-8")
    sec = w.split("## 6. 見送り", 1)[-1].split("## 7.", 1)[0]
    check("一部の素材だけがそうなら" in sec and "残りの素材で書く" in sec,
          "執筆の見送りに、一部の素材だけが食い違うときは残りで書く決まりが無い")

def test_compose_recover(tmp: Path):
    """承認まで進んでも記事を落とした号は、当番が直したあとこの号に戻す(2026-10-10 編集長「復帰保証までが Opus/Sol の仕事だ」)。
    組み直しは控えを取ってから --reuse-plan。承認済みだった記事を失う・承認されないなら控えに戻す。戻った・戻らない記事を名指しする。"""
    import datetime as _dt
    import time
    import oncall
    # 組版: 落とした記事があり、発行の締切まで時間が残るときだけ --recover と締切を渡す
    pub = _dt.datetime(2026, 10, 10, 6, 0, tzinfo=pipelib.JST).timestamp() - compose.HANDOFF_MIN * 60
    check(compose.recover_until("2026-10-10", pub - 3 * 3600) == pub, "03:00 台に戻す締切を渡さない")
    check(compose.recover_until("2026-10-10", pub - (compose.RECOVER_MIN_LEFT_MIN - 1) * 60) is None, "間に合わないのに戻しに行かせる")
    src = __import__("inspect").getsource(compose.main)
    check('extra_args=["--recover", "--end-at", str(int(until))] if until else None' in src and "recover_until(date) if recover_pending(date)" in src,
          "承認済みで記事を落とした号を、当番に戻させていない")
    text = (pipelib.ROOT / "prompts" / "oncall-whywhy.md").read_text(encoding="utf-8")
    check("紙面に戻るまでが仕事" in text and "落とした記事・確定した号は戻さない" not in text, "なぜなぜの依頼文が、落とした記事を戻させていない")
    # 当番: 控え → 校閲記録を外す → --reuse-plan → 結果で採るか控えに戻すか
    posts = tmp / "docs" / "_posts"
    posts.mkdir(parents=True)
    (tmp / "metrics").mkdir()
    (tmp / "metrics" / "plan-2026-10-10.json").write_text(json.dumps({"articles": [{"slug": s} for s in ("a", "b", "c", "d")]}), encoding="utf-8")
    saved = (oncall.ROOT, oncall.run_stage, oncall.commit_paths, oncall.backup_edition, oncall.restore_edition, oncall.notify,
             oncall.STAGE_END_AT, oncall.RECOVER_TRIED)
    try:
        for after, code, want_restore, want in ((["a", "b", "c"], 0, False, "戻った 1本: c"),          # c が戻った
                                                (["a", "c"], 0, True, "外れた 1本: b"),               # c は戻ったが b を失った → 控えへ
                                                (["a", "b"], 1, True, "戻った 0本")):                 # 承認されない → 控えへ
            for p in posts.glob("*.md"):
                p.unlink()
            for s in ("a", "b"):
                (posts / f"2026-10-10-{s}.md").write_text("x", encoding="utf-8")
            (tmp / "metrics" / "review-2026-10-10-1.json").write_text('{"verdict": "block"}', encoding="utf-8")
            (tmp / "metrics" / "review-2026-10-10-12.json").write_text('{"verdict": "approve"}', encoding="utf-8")   # 巡の番号は数で比べる
            calls, notes = [], []

            def fake_run(cmd, log, limit, after=after, code=code):
                carry = cmd[cmd.index("--carry-review") + 1] if "--carry-review" in cmd else ""
                calls.append(("run", "--reuse-plan" if "--reuse-plan" in cmd else "", limit,
                              carry and json.loads(Path(carry).read_text(encoding="utf-8"))))
                for p in posts.glob("*.md"):
                    p.unlink()
                for s in after:
                    (posts / f"2026-10-10-{s}.md").write_text("x", encoding="utf-8")
                return code
            oncall.ROOT, oncall.run_stage = tmp, fake_run
            oncall.commit_paths = lambda paths, msg, branch, push_timeout=120: calls.append(("commit", tuple(paths)))
            oncall.backup_edition = lambda d, e: calls.append(("backup",)) or "backup/x"
            oncall.restore_edition = lambda d, e, b, u=None: calls.append(("restore", b))
            oncall.notify = lambda job, msg, ok=True, require=False: notes.append((msg, ok)) or True
            oncall.STAGE_END_AT = time.time() + 1800
            got = oncall.rerun_stage("compose", "2026-10-10", "edition/2026-10-10", False, recover=True)
            check(calls[0] == ("backup",) and calls[1] == ("commit", ("metrics",)) and calls[2][:2] == ("run", "--reuse-plan")
                  and 1500 < calls[2][2] <= 1800, f"組み直しの手順・時間の上限: {calls}")
            check(calls[2][3] == {"verdict": "approve"}, f"承認済みの校閲結果(最新の巡)を引き継がせていない: {calls[2][3]}")
            check(not list((tmp / "metrics").glob("review-*.json")), "古い校閲記録を外していない")
            check((("restore", "backup/x") in calls) == want_restore and (got == 0) == (not want_restore),
                  f"{after} exit {code}: 控えに戻す判断が違う: {calls} exit {got}")
            check(any(want in m for m, _ in notes) and any("戻らない" in m and "d" in m for m, _ in notes), f"戻った・戻らない記事を名指ししない: {notes}")
            check(oncall.RECOVER_TRIED, "組み直しを試みたことを記録しない(後段がもう一度走る)")
    finally:
        (oncall.ROOT, oncall.run_stage, oncall.commit_paths, oncall.backup_edition, oncall.restore_edition, oncall.notify,
         oncall.STAGE_END_AT, oncall.RECOVER_TRIED) = saved
    # 時間切れの組み直しは、別セッションで起動された孫(執筆・校閲のセッション)まで落とす。落としている最中に生まれる孫も
    # 残さない(写しを1回取るだけだと、取ったあとに生まれた孫が残る。残ると控えへ戻した紙面に書く。監査指摘)
    pidf = tmp / "spawned.pids"
    spawner = ("import subprocess,sys,time\n"
               "while True:\n"
               f"    q=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)\n"
               f"    open({str(pidf)!r},'a').write(str(q.pid)+'\\n');time.sleep(0.02)\n")
    script = (f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{spawner!r}],start_new_session=True);time.sleep(60)")
    saved_root = oncall.ROOT
    try:
        oncall.ROOT = tmp
        code = oncall.run_stage([sys.executable, "-c", script], tmp / "rerun.log", 2)
    finally:
        oncall.ROOT = saved_root
    time.sleep(0.3)
    pids = [int(x) for x in pidf.read_text().split()] if pidf.exists() else []
    alive = [q for q in pids if Path(f"/proc/{q}").exists() and oncall._proc_state(q) not in ("Z", "X")]
    check(code == 124 and len(pids) > 5 and not alive, f"時間切れで別セッションの孫が残った({len(alive)}/{len(pids)}本, exit {code})")
    # 期限前に終わった(異常終了・正常終了)ときも、残った孫を落としてから返す(監査指摘)
    for want in (1, 0):
        pidf = tmp / f"left-{want}.pid"
        script = ("import subprocess,sys;"
                  "q=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True);"
                  f"open({str(pidf)!r},'w').write(str(q.pid));sys.exit({want})")
        saved_root = oncall.ROOT
        try:
            oncall.ROOT = tmp
            code = oncall.run_stage([sys.executable, "-c", script], tmp / "rerun.log", 30)
        finally:
            oncall.ROOT = saved_root
        gpid = int(pidf.read_text()) if pidf.exists() else 0
        alive = gpid and Path(f"/proc/{gpid}").exists() and oncall._proc_state(gpid) not in ("Z", "X")
        check(code == want and gpid and not alive, f"exit {want} で終わった子の孫が残った(pid {gpid}, exit {code})")
    # 控えへ戻すとき、組み直しの間に出来た未追跡ファイル(組版が作った先日付の予約 等)も消して clean にし、push まで行う
    # (残ると clean 判定で復元が止まり、06:00 の発行も未追跡ファイルで止まる。開始前からあったものは消さない。監査指摘)
    remote, work = tmp / "remote.git", tmp / "work"
    run = lambda *a, cwd=None: subprocess.run(list(a), cwd=cwd, capture_output=True, text=True, check=True)
    run("git", "init", "-q", "--bare", str(remote))
    run("git", "init", "-q", "-b", "edition/2026-10-10", str(work))
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        run("git", "config", k, v, cwd=work)
    (work / "docs" / "_posts").mkdir(parents=True)
    (work / "docs" / "_posts" / "2026-10-10-a.md").write_text("a", encoding="utf-8")
    run("git", "add", "-A", cwd=work)
    run("git", "commit", "-q", "-m", "approved", cwd=work)
    run("git", "remote", "add", "origin", str(remote), cwd=work)
    run("git", "push", "-q", "origin", "edition/2026-10-10", cwd=work)
    run("git", "branch", "backup/x", cwd=work)
    saved_root = oncall.ROOT
    try:
        oncall.ROOT = work
        before = oncall.untracked_files()
        (work / "stock" / "scheduled").mkdir(parents=True)
        (work / "stock" / "scheduled" / "2026-10-20.json").write_text("{}", encoding="utf-8")   # 組み直しが作った予約(未コミット)
        (work / "docs" / "_posts" / "2026-10-10-b.md").write_text("b", encoding="utf-8")       # 組み直しが書いた記事(未コミット)
        oncall.restore_edition("2026-10-10", "edition/2026-10-10", "backup/x", before)
        status = run("git", "status", "--porcelain", cwd=work).stdout
        check(before == set() and status == "" and not (work / "stock" / "scheduled" / "2026-10-20.json").exists(),
              f"組み直しの間に出来た未追跡ファイルを消さずに復元が止まる: {status!r}")
    except RuntimeError as e:
        check(False, f"組み直しの間に出来た未追跡ファイルで復元が止まった: {e}")
    finally:
        oncall.ROOT = saved_root
    # 往復を省いて組み直すときも、作業ツリーが汚れていれば何も変えない(控えへの復元で汚れを消さない。監査指摘)
    saved = (oncall.root_clean, oncall.ensure_edition, oncall.rerun_stage, oncall.notify, oncall.sh)
    try:
        ran, notes = [], []
        oncall.notify = lambda job, msg, ok=True, require=False: notes.append(msg) or True
        oncall.rerun_stage = lambda *a, **k: ran.append(a) or 0
        oncall.sh = lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
        oncall.root_clean, oncall.ensure_edition = (lambda: False), (lambda e, c: "")
        check(oncall.recover_rerun("2026-10-10", "edition/2026-10-10") is None and not ran, "汚れた作業ツリーで組み直しに入った")
        oncall.root_clean, oncall.ensure_edition = (lambda: True), (lambda e, c: "edition/2026-10-10 がリモートと食い違う")
        check(oncall.recover_rerun("2026-10-10", "edition/2026-10-10") is None and not ran, "リモートと食い違う号で組み直しに入った")
        oncall.ensure_edition = lambda e, c: ""
        check(oncall.recover_rerun("2026-10-10", "edition/2026-10-10") == 0 and ran, "前提を満たすのに組み直さない")
    finally:
        oncall.root_clean, oncall.ensure_edition, oncall.rerun_stage, oncall.notify, oncall.sh = saved
    # 当番の本体が例外で抜けても(往復の前の fetch の失敗 等)、掴んだままの排他を離して組み直しに進む(監査指摘)
    saved = (oncall._main, oncall.recover_rerun, oncall.notify, oncall.RECOVER, oncall.RECOVER_TARGET, oncall.RECOVER_TRIED,
             oncall.STAGE_END_AT, oncall.ROOT, pipelib.ROOT)
    try:
        (tmp / "lk" / "metrics").mkdir(parents=True)
        oncall.ROOT = pipelib.ROOT = tmp / "lk"
        held = []

        def boom():
            held.append(pipelib.job_lock("oncall"))      # 排他を掴んだまま例外で抜ける
            raise RuntimeError("fetch に失敗")
        tried = []
        # 後段は排他を取ってから組み直す。掴んだままの排他が離されていなければ、取れずに(2分待って)組み直しに進まない
        oncall._main, oncall.recover_rerun = boom, (lambda d, e: tried.append(d) or 0)
        oncall.notify = lambda job, msg, ok=True, require=False: True
        oncall.RECOVER, oncall.RECOVER_TARGET, oncall.RECOVER_TRIED = True, ("2026-10-10", "edition/2026-10-10"), False
        oncall.STAGE_END_AT = time.time() + 3600
        check(oncall.main() == 0 and tried == ["2026-10-10"], f"本体の例外で組み直しに進まない・排他が残る: {tried}")
    finally:
        oncall.release_held_job_locks()     # このテストで開いた排他(作業用の tmp のもの)を閉じる
        (oncall._main, oncall.recover_rerun, oncall.notify, oncall.RECOVER, oncall.RECOVER_TARGET, oncall.RECOVER_TRIED,
         oncall.STAGE_END_AT, oncall.ROOT, pipelib.ROOT) = saved


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
    # 収集(collect/watch)の resume は「続き」として扱い、release の続きと取り違えない(当番 2026-10-02)
    check(oncall.rerun_policy("watch", ["scripts/pipelib.py"], "resume") == (False, "続き(定点観測の繰り越しを拾い直す)"),
          "watch: resume の表示名が collect 用でない")
    check(oncall.rerun_policy("collect", ["scripts/pipelib.py"], "resume") == (False, "定点観測の取り直し(落とした新着をその号の素材へ)"),
          "collect: 取り直しの表示名")
    for stage in ("collect", "watch"):
        check(oncall.rerun_policy(stage, ["scripts/pipelib.py"], "none") == (False, "none"), f"{stage}: none が効かない")
    # 落とした記事を戻す当番(--recover)は、生成層を直しても・none と答えても、承認済みの紙面を作り直さず --reuse-plan で戻す
    # (2026-10-10: 「確定した号は戻さない」で発行前の2本を戻さなかった。編集長「復帰保証までが Opus/Sol の仕事だ」)
    for files, mode in ((["scripts/compose.py"], "rebuild"), (["scripts/assemble.py"], "none"), ([], "resume")):
        check(oncall.rerun_policy("compose", files, mode, recover=True) == (False, "落とした記事をこの号に戻す(--reuse-plan)"),
              f"recover: {files} {mode} で戻しに行かない・作り直しになる")
    # release 起点の作り直しは compose 先頭 → release の順に走る
    calls, cmds = [], []
    saved = (oncall.reset_edition, oncall.run_stage, oncall.commit_paths, oncall.ROOT, oncall.root_clean)
    oncall.root_clean = lambda: True
    tmp = Path(tempfile.mkdtemp()); (tmp / "metrics").mkdir()
    try:
        oncall.ROOT = tmp
        oncall.reset_edition = lambda d, e: calls.append("reset") or "backup/x"
        oncall.run_stage = lambda cmd, log, t: cmds.append(cmd) or calls.append(Path(cmd[1]).name + ("(reuse)" if "--reuse-plan" in cmd else "")) or 0
        oncall.commit_paths = lambda *a, **k: calls.append("commit")
        code = oncall.rerun_stage("release", "2026-09-12", "edition/2026-09-12", full=True)
        check(code == 0 and calls == ["reset", "compose.py", "release.py"], f"release 起点 full の順序: {calls}")
        calls.clear()
        oncall.rerun_stage("compose", "2026-09-12", "edition/2026-09-12", full=False)
        check(calls == ["commit", "compose.py(reuse)"], f"compose 続きの順序: {calls}")
        calls.clear()
        oncall.rerun_stage("release", "2026-09-12", "edition/2026-09-12", full=False)
        check(calls == ["release.py"], f"release 続きの順序: {calls}")
        # 収集の resume は no-op にせず、繰り越しを拾い直すため collect を実際に起動する(当番 2026-10-02:
        # 直す前は else 枝で code=0 を返すだけで collect.py を呼ばず、未処理6件が次の定時まで残った)
        for stage in ("collect", "watch"):
            calls.clear(); cmds.clear()
            code = oncall.rerun_stage(stage, "2026-09-12", "edition/2026-09-12", full=False)
            check(code == 0 and calls == ["collect.py"], f"{stage} 続きが collect を起動しない: {calls}")
            # 拾い直しは諦めずに繰り越す(--oncall-rerun)。原因未確定のまま再び読めなくても新着を失わない(監査指摘)
            check("--oncall-rerun" in cmds[0], f"{stage} 拾い直しに --oncall-rerun が無い: {cmds[0]}")
        # collect は壁時計でなく**取り込み先の号(edition)**を処理する。発行後 watch 等で異常発生日と
        # 取り込み先がずれても、--date で edition の号を渡す(渡さないと別号を checkout する。監査指摘)
        calls.clear(); cmds.clear()
        oncall.rerun_stage("watch", "2026-09-12", "edition/2026-09-13", full=False)
        i = cmds[0].index("--date")
        check(cmds[0][i + 1] == "2026-09-13", f"collect に取り込み先の号を渡していない: {cmds[0]}")
        # 取り直しが途中で終わって作業ツリーが汚れたら、保存できた素材と既読状態を確定して clean にする(監査指摘)
        states, committed = iter([False, True]), []
        oncall.root_clean = lambda: next(states)
        oncall.commit_paths = lambda paths, msg, branch, push_timeout=120: committed.append(paths)
        oncall.run_stage = lambda cmd, log, t: 124
        pushed = []
        saved_ep = oncall.ensure_pushed, oncall.settle_collect_outputs
        oncall.ensure_pushed = lambda branch, what, budget=300: pushed.append(branch) or True
        oncall.settle_collect_outputs = lambda root: []     # 出力の検めは test_oncall_collect_rerun_settles_broken_output が実際の git で検める
        try:
            oncall.rerun_stage("collect", "2026-09-13", "edition/2026-09-13", False)
        finally:
            oncall.ensure_pushed, oncall.settle_collect_outputs = saved_ep
        check(committed and "candidates" in committed[0] and "stock/watch-state.json" in committed[0],
              f"途中で終わった取り直しの素材を確定しない: {committed}")
        # clean でも、取り直しの commit がリモートに届いているかを確かめて送る(push の途中で時間切れ。監査指摘)
        check(pushed == ["edition/2026-09-13"], f"取り直しの素材をリモートに確定しない: {pushed}")
        # ensure_pushed: ローカルだけにある commit をリモートへ送る(実際の git で)
        gtmp = Path(tempfile.mkdtemp(prefix="ep-"))
        G = lambda *a, cwd=gtmp / "w": subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(["git", "init", "-q", "--bare", "-b", "edition/x", str(gtmp / "r.git")], check=True)
        subprocess.run(["git", "clone", "-q", str(gtmp / "r.git"), str(gtmp / "w")], check=True, capture_output=True)
        G("config", "user.email", "t@example.com"); G("config", "user.name", "t"); G("checkout", "-q", "-b", "edition/x")
        (gtmp / "w" / "a").write_text("1"); G("add", "a"); G("commit", "-q", "-m", "c1"); G("push", "-q", "origin", "edition/x")
        (gtmp / "w" / "a").write_text("2"); G("commit", "-q", "-am", "c2")
        saved_root2 = oncall.ROOT
        try:
            oncall.ROOT = gtmp / "w"
            ok_p = oncall.ensure_pushed("edition/x", "テスト")
        finally:
            oncall.ROOT = saved_root2
        check(ok_p and G("rev-parse", "edition/x", cwd=gtmp / "r.git") == G("rev-parse", "HEAD"), "ローカルだけの commit を送らない")
    finally:
        oncall.reset_edition, oncall.run_stage, oncall.commit_paths, oncall.ROOT, oncall.root_clean = saved


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
        # Discord には編集長向けの報告(差分・記録用の診断全文は記録ファイル)。項目は 起きたこと・なぜ・どう直したか・同じ型の箇所・紙面への影響。
        # 長さは制限しない(編集長 2026-10-04「短くして情報が0になったらゴミだ」「コード差分はゴミ」)
        check(len(sent) == 1 and "metrics/oncall-2026-09-12-compose-report.md" in sent[0][0]
              and "diff" not in sent[0][0] and "ddd" not in sent[0][0], f"編集長への報告が記録の場所つきでない・差分や診断全文を含む: {sent[:1]}")
        sent.clear()
        summ = {"what_happened": "まとめサイトの新着1件を、本文を読めないまま既読にして失っていた。" * 3,
                "why": "モデルの空の出力を『読んで何も無かった』と解釈して既読にしていた。" * 4,
                "what_changed": "新着ページごとに読めた・事実なし・読めなかったを返させ、読めなかったページは既読にしない。" * 3,
                "similar": "モデルの出力で状態を変える箇所を全部調べた。" * 4, "paper_impact": "失った1件は戻らない。10/6号から効く"}
        oncall.report_change("collect", "2026-10-05", {"status": "fixed", "editor_summary": summ, "diagnosis": "x" * 3000}, [{"review": {"verdict": "approve"}}],
                             "base", "headhash00", "", "repair/y", ["main"], "なし")
        m = sent[0][0]
        check(m.startswith("🛠 当番が直しました: 10/5号の収集") and all(v in m for v in summ.values()) and "承認(1往復)" in m and "xxx" not in m
              and all(f"■ {l}" in m for l in pipelib.INCIDENT_LABELS), f"編集長への報告の中身(5項目・切り詰めない): {m[:400]}")
        n = pipelib.editor_notice("🔧 題", [("なぜ起きたか", "長い説明" * 200)], footer="記録: x")
        check("長い説明" * 200 in n and n.startswith("🔧 題\n\n■ なぜ起きたか"), "報告の本文を切り詰めた・見出しの形でない")
        check(pipelib.internal_names("candidate_ids が無い。verified_facts を new_facts に。R1 と r77 と collect.py") ==
              ["R1", "candidate_ids", "collect.py", "new_facts", "r77", "verified_facts"], f"内部の名前の検出: {pipelib.internal_names('candidate_ids R1 r77 collect.py verified_facts new_facts')}")
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
        src = inspect.getsource(oncall._main)
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
    # 編集長向けの要約は最終の内容で置き換わる(訂正したのに初回の要約が届く、を防ぐ。監査指摘 r118)
    fix["editor_summary"] = {"what_happened": "a", "why": "w", "what_changed": "直さずにもう一度実行する", "similar": "s", "paper_impact": "c"}
    oncall.apply_integrate(fix, {"status": "fixed", "rerun_mode": "unchanged", "diagnosis": "", "root_cause": "", "recovery": "",
                                 "editor_summary": {"what_happened": "a2", "why": "w2", "what_changed": "読めなかった新着を既読にしないよう直した",
                                                    "similar": "s2", "paper_impact": "c2"}})
    check(fix["editor_summary"]["what_changed"] == "読めなかった新着を既読にしないよう直した", f"要約が最終の内容にならない: {fix['editor_summary']}")
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
    # event_date の無い稿(null)を書き直して日付を埋めても、指摘が event_date に掛かっていなければ元の値(無し)を写して通す
    # (2026-10-03: 「指摘に無い event_date を変えた」で2巡戻され、R1 が直らずブロックで落ちた)
    old_noev = ("---\n" + compose.yaml_dump_keeping_strings({"title": "t", "lede": "l", "tags": ["a", "b"],
                                                              "sources": [{"url": u["url"], "label": "x", "type": "公式"} for u in OK["sources"]]})
                + "---\n価格は三千円である。 <!-- F1 -->\n")
    p.write_text(old_noev, encoding="utf-8")
    ans = dict(OK, addressed_issue_ids=["I1"], blocks=[{"markdown": "価格は三千円。", "fact_ids": ["F1"]}], event_date="2026-09-13")
    outcome, msg = compose.revise_apply("2026-09-12", art, p, ans, fb, MATS, iss)
    check(outcome == "fixed" and "event_date" not in (compose.parse_front_matter(p) or {}),
          f"指摘外で埋めた event_date で書き直しが戻された/残った: {outcome} {msg}")
    # 元の稿の日付を書き手が変えても、指摘外なら元の値のまま
    p.write_text(old, encoding="utf-8")
    ans = dict(OK, addressed_issue_ids=["I1"], blocks=[{"markdown": "価格は三千円。", "fact_ids": ["F1"]}], event_date="2026-09-20")
    outcome, msg = compose.revise_apply("2026-09-12", art, p, ans, fb, MATS, iss)
    check(outcome == "fixed" and str((compose.parse_front_matter(p) or {}).get("event_date")) == "2026-09-13",
          f"指摘外の event_date が元の値に戻らない: {outcome} {msg}")
    # 指摘の外の段落も変えた稿は、形が正しければ採り、次の巡の校閲に「指摘の外の変更」を確かめさせる(範囲は中身の判断で校閲が見る。
    # 2026-10-10: DAY の区分の誤りを直す組み直しが「指摘に無い段落を変えた」で戻され、締切前日の記事が落ちた)
    old2 = ("---\n" + compose.yaml_dump_keeping_strings({"title": "t", "lede": "l", "tags": ["a", "b"], "event_date": "2026-09-13",
                                                          "sources": [{"url": u["url"], "label": "x", "type": "公式"} for u in OK["sources"]]})
            + "---\n価格は三千円である。 <!-- F1 -->\n\n発売は秋である。 <!-- F1 -->\n")
    p.write_text(old2, encoding="utf-8")
    compose.OFFSCOPE_NOTES.clear()
    ans_scope = dict(OK, addressed_issue_ids=["I1"], blocks=[{"markdown": "価格は三千円。", "fact_ids": ["F1"]},
                                                             {"markdown": "発売は秋の予定。", "fact_ids": ["F1"]}])
    outcome, msg = compose.revise_apply("2026-09-12", art, p, ans_scope, fb, MATS, iss)
    note = (compose.OFFSCOPE_NOTES.get("2026-09-12") or {}).get("x", "")
    hint = compose.review_hint("2026-09-12", "2026-09-12-x.md", {})
    check(outcome == "fixed" and "発売は秋の予定" in p.read_text(encoding="utf-8") and "指摘に無い段落" in note
          and "指摘の外の変更" in hint and "発売は秋" in hint, f"指摘の外も直した稿を戻した・校閲に確かめさせない: {outcome} {msg} / {hint[-200:]}")
    # 形の不合格(HTML のタグなど)は、範囲と関係なく従来どおり戻す
    p.write_text(old2, encoding="utf-8")
    ans_bad = dict(OK, addressed_issue_ids=["I1"], blocks=[{"markdown": "価格は<span>三千円</span>。", "fact_ids": ["F1"]},
                                                           {"markdown": "発売は秋の予定。", "fact_ids": ["F1"]}])
    outcome, msg = compose.revise_apply("2026-09-12", art, p, ans_bad, fb, MATS, iss)
    check(outcome == "kept" and p.read_text(encoding="utf-8") == old2, f"形の崩れた稿が範囲の扱いに紛れて通った: {outcome} {msg}")
    compose.OFFSCOPE_NOTES.clear()
    # 指摘が event_date を名指すなら書き手の値を使う
    p.write_text(old, encoding="utf-8")
    iss_ev = [dict(iss[0], issue="event_date 2026-09-13 は出典の開催日と違う")]
    outcome, msg = compose.revise_apply("2026-09-12", art, p, ans, fb, MATS, iss_ev)
    check(outcome == "fixed" and str((compose.parse_front_matter(p) or {}).get("event_date")) == "2026-09-20",
          f"event_date の指摘で直した日付が反映されない: {outcome} {msg}")
    # 前の稿の verified_facts(N1)を new_facts に写さずに指した稿は戻し、**原因を書いた**理由を返す(2026-09-25 に2回戻されて落ちた)
    p.write_text(old, encoding="utf-8")
    ans = dict(OK, addressed_issue_ids=["I1"], blocks=[{"markdown": "価格は三千円である。", "fact_ids": ["F1", "N1"]}], new_facts=[])
    outcome, msg = compose.revise_apply("2026-09-12", art, p, ans, fb, MATS, iss)
    check(outcome == "kept" and "new_facts に" in msg and "書き写す" in msg, f"N の id の戻し方に原因が無い: {outcome} {msg}")
    check("verified_facts" in (pipelib.PROMPTS / "revise-article.md").read_text(encoding="utf-8"), "書き直しの依頼文が verified_facts の写し方を言っていない")
    # 機械の検査が戻したときは、次の巡の校閲にそう伝える(校閲が「執筆が無視した」と読んで同じ指摘を繰り返さない)
    compose.REVISE_NOTES["2026-09-12"] = {"x": msg}
    try:
        h = compose.review_hint("2026-09-12", "2026-09-12-x.md", {})
        check("執筆が指摘を無視したのではない" in h and "N1" in h, f"戻した書き直しが校閲の依頼文に載らない: {h!r}")
        check(compose.review_hint("2026-09-12", "2026-09-12-y.md", {}) == "", "戻していない記事に注記が付いた")
    finally:
        compose.REVISE_NOTES.clear()
    # 注記は**その巡の結果**だけ: 巡の開始で消え、出力が読めなかった巡はその理由に更新される(古い巡の理由を引きずらない。監査指摘 r77)
    saved_rp, saved_popen, saved_root, saved_proot = compose.revise_prompt, compose.subprocess.Popen, compose.ROOT, pipelib.ROOT
    try:
        compose.ROOT = pipelib.ROOT = tmp
        (tmp / "schema").mkdir(exist_ok=True)
        (tmp / "schema" / "article-out.schema.json").write_text("{}", encoding="utf-8")
        (tmp / "docs" / "_posts").mkdir(parents=True, exist_ok=True)
        post = tmp / "docs" / "_posts" / "2026-09-12-x.md"
        post.write_text(old, encoding="utf-8")
        compose.revise_prompt = lambda *a, **k: "p"
        class P:
            returncode = 0
            def __init__(self, out_path): self.out_path = out_path
            def communicate(self, timeout=None):
                Path(self.out_path).write_text("これは JSON ではない", encoding="utf-8")
                return None, None
            def kill(self): pass
        def fake_popen(cmd, **kw):
            return P(cmd[cmd.index("--output-last-message") + 1])
        compose.subprocess.Popen = fake_popen
        compose.REVISE_NOTES["2026-09-12"] = {"x": "前々巡の古い理由"}
        plan = {"articles": [{"slug": "x", "brand": "765", "candidate_ids": ["c1"], "rank": "small"}]}
        n_before = len(pipelib.ANOMALIES)
        res = compose.revise_articles("2026-09-12", {"docs/_posts/2026-09-12-x.md": [{"rule_id": "R1", "quote": "価格は三千円である", "repair": "drop_claim"}]},
                                      plan, {"c1": MATS[0]}, None)
        note = compose.REVISE_NOTES["2026-09-12"].get("x", "")
        check("読めず" in note and "前々巡" not in note, f"出力が読めなかった巡の注記が更新されない: {note!r}")
        # 読めなかった書き直しは「書き手が書き直した」に数えない(次の巡に同じ指摘が残っても契約の欠陥と記録しない)。実行の失敗は異常の台帳へ
        check(res == {"x": "unreadable"}, f"書き直しの結果: {res}")
        check(len(pipelib.ANOMALIES) == n_before + 1 and "出力が読めない" in pipelib.ANOMALIES[-1], "読めなかった書き直しが台帳に無い")
    finally:
        compose.revise_prompt, compose.subprocess.Popen, compose.ROOT, pipelib.ROOT = saved_rp, saved_popen, saved_root, saved_proot
        compose.REVISE_NOTES.clear()
    # 書き直しを適用できなかった記事は、持ち越した指摘も含めてブロックを1件に絞る(監査指摘 r78)
    saved_root2, saved_proot2 = compose.ROOT, pipelib.ROOT
    try:
        compose.ROOT = pipelib.ROOT = tmp
        (tmp / "docs" / "_posts").mkdir(parents=True, exist_ok=True)
        (tmp / "docs" / "_posts" / "2026-09-12-x.md").write_text(old, encoding="utf-8")
        (tmp / "docs" / "_posts" / "2026-09-12-y.md").write_text(old, encoding="utf-8")
        compose.REVISE_NOTES["2026-09-12"] = {"x": "読めず"}
        pipelib.set_quiet(True)
        carry = {"verdict": "block", "reviewed": ["article:2026-09-12-x.md", "article:2026-09-12-y.md"], "comments": [],
                 "blockers": [{"scope": "article:2026-09-12-x.md", "file": "docs/_posts/2026-09-12-x.md", "issue": "a", "quote": "q", "rule_id": "R1", "repair": "drop_claim", "fact_ids": [], "expected": ""},
                              {"scope": "article:2026-09-12-x.md", "file": "docs/_posts/2026-09-12-x.md", "issue": "b", "quote": "q", "rule_id": "R2", "repair": "drop_claim", "fact_ids": [], "expected": ""},
                              {"scope": "article:2026-09-12-y.md", "file": "docs/_posts/2026-09-12-y.md", "issue": "c", "quote": "q", "rule_id": "R1", "repair": "drop_claim", "fact_ids": [], "expected": ""},
                              {"scope": "article:2026-09-12-y.md", "file": "docs/_posts/2026-09-12-y.md", "issue": "d", "quote": "q", "rule_id": "R2", "repair": "drop_claim", "fact_ids": [], "expected": ""}]}
        r = compose.claude_review("2026-09-12", 2, targets=[], editorial=False, paper=False, carry=carry)
        got = [(b["scope"][-4:-3], b["issue"]) for b in r["blockers"]]
        check(got == [("x", "a"), ("y", "c"), ("y", "d")], f"持ち越した指摘の絞り込み: {got}")
    finally:
        compose.ROOT, pipelib.ROOT = saved_root2, saved_proot2
        pipelib.set_quiet(False)
        compose.REVISE_NOTES.clear()
    # 校閲の依頼文: 続報予約の素材の在りか、「X の投稿 URL を特定しろ」と言わせない(2026-09-25: 執筆に直せない指摘で3本落ちた)
    rp = (pipelib.PROMPTS / "review-article.md").read_text(encoding="utf-8")
    check("続報予約の素材" in rp and "stock/scheduled/{DATE}.json" not in rp and "執筆には直せない" in rp and "執筆も X を開けない" in rp, "校閲の依頼文に、執筆に直せない指摘を避ける規則が無い")
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
    text = "短い行\n" + "x" * 5000 + "\n最後\n"
    chunks = pipelib.split_chunks(text, 1800)
    check("".join(chunks) == text and all(len(c) <= 1800 for c in chunks) and len(chunks) >= 3, "split_chunks が元に戻らない")
    # 長い通知は notify 自身が分割して全部送る(watch・compose の長い通知が webhook の上限で落ちない。監査指摘 r73)。
    # 途中の塊が落ちたら False
    sent = []
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_open(req, timeout=None):
        body = json.loads(req.data.decode())["content"]
        sent.append(body)
        if len(sent) == 2:
            raise OSError("boom")
        return FakeResp()
    saved = (pipelib.urllib.request.urlopen, pipelib._QUIET, dict(pipelib.ENV))
    try:
        pipelib._QUIET = False
        pipelib.ENV["DISCORD_WEBHOOK_URL"] = "https://discord.example/hook"
        pipelib.urllib.request.urlopen = fake_open
        ok = pipelib.notify("t", text, require=True)
        check(not ok and len(sent) >= 3 and all(len(c) <= 1800 + 12 for c in sent) and sent[0].startswith("(1/"), f"長い通知の分割: {ok} {len(sent)}")
        sent.clear()
        check(pipelib.notify("t", "短い") is True and len(sent) == 1 and not sent[0].startswith("(1/"), "短い通知に番号が付いた")
    finally:
        pipelib.urllib.request.urlopen, pipelib._QUIET = saved[0], saved[1]
        pipelib.ENV.clear(); pipelib.ENV.update(saved[2])


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
    # 以下は対応付けと議論の規則を見る(答えの欄の検査は最後に別に見る)ので、host と type だけを必須にする
    HT = ("host", "type")
    try:
        cs.ask = fake_ask
        agreed, split = cs.consensus("p", ["yuzu_yng", "onkyodav", "idolmaster_en", "jimushiny_oa", "x1", "x2"], need=HT)
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
        check(cs.consensus("p", ["h"], need=HT)[0].get("h", ("",))[0] == "報道" and len(calls) == 2, "一致しているのに2巡目を掛けた")
        # 答えの対象の書き方が依頼と違っても対応付ける(`youtube.com/@Foo` を頼んで `@foo` や URL で返る。
        # 2026-09-18: 答えているのに無回答扱いになり、議論しても決まらなかった)
        calls.clear()
        forms = iter([[{"host": "@TogawaNonoha", "type": "ファン", "why": "個人"}, {"host": "https://www.Example.com/", "type": "報道"}],
                      [{"host": "https://www.youtube.com/@togawanonoha/about", "type": "ファン", "why": "個人"}, {"host": "example.com", "type": "報道"},
                       {"host": "頼んでいない", "type": "報道"}]])
        cs.ask = lambda cmd, prompt, timeout=900: next(forms)
        ag, sp = cs.consensus("p", ["youtube.com/@TogawaNonoha", "example.com"], need=HT)
        check(ag.get("youtube.com/@TogawaNonoha", ("",))[0] == "ファン" and ag.get("example.com", ("",))[0] == "報道" and not sp,
              f"書き方の違う答えを対応付けられない: {ag} {sp}")
        # 末尾が同じ依頼が2つあるときは、末尾だけの答えを当てない(取り違えない)
        amb = cs._by_host([{"host": "@foo", "type": "ファン"}], ["youtube.com/@foo", "tiktok.com/@foo"], HT)
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
        ag, sp = cs.consensus(lambda ks: "対象: " + " ".join(ks), ["youtube.com/@bar", "youtube.com/@foo"], need=HT)
        check(ag.get("youtube.com/@foo", ("",))[0] == "ファン" and ag.get("youtube.com/@bar", ("",))[0] == "報道" and not sp,
              f"同じ host の2対象で、2巡目の host だけの答えを捨てた: {ag} {sp}")
        check(len(prompts) == 4 and "@bar" not in prompts[2].split("\n")[0] and "@foo" in prompts[2], f"2巡目の依頼文に割れていない対象が残っている: {prompts[2][:80]}")
        cs.ask = fake_ask
        # パス付きの対象を host だけで返した答え: その host の依頼が1つだけなら当てる。2つあれば当てない
        one = cs._by_host([{"host": "youtube.com", "type": "ファン"}], ["youtube.com/@foo", "example.com"], HT)
        two = cs._by_host([{"host": "youtube.com", "type": "ファン"}], ["youtube.com/@foo", "youtube.com/@bar"], HT)
        check(list(one) == ["youtube.com/@foo"] and two == {}, f"host だけの答えの対応付け: {one} {two}")
        # 答えの欄: サイトは運営主体と根拠が必須。欠けた答え・同じ対象への2つの答えは無回答(議論へ)
        site = {"host": "example.com", "operator": "出版社", "type": "報道", "why": "会社概要"}
        check(list(cs._by_host([site], ["example.com"], cs.ANSWER_SITE)) == ["example.com"]
              and cs._by_host([dict(site, operator="")], ["example.com"], cs.ANSWER_SITE) == {}
              and cs._by_host([site, dict(site, type="ファン")], ["example.com"], cs.ANSWER_SITE) == {},
              "運営主体の無い答え・矛盾する2つの答えを確定させた")
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


def test_request_parsers(tmp: Path):
    """パーサの無い URL は人に「決まらない」と言う前に当番へ回す(編集長 2026-09-26「申告前に診断しろ」)。
    依頼はホストごとに1つ、同じホストは PARSER_RETRY_DAYS のあいだ出し直さない。依頼の文は prompts/oncall-parser。"""
    import classify_sources as cs
    tmp.mkdir(parents=True, exist_ok=True)
    calls: list[tuple[str, str, str]] = []
    saved = (cs.PARSER_REQUESTS, dict(cs.USED_IN))
    try:
        cs.PARSER_REQUESTS = tmp / "parser-requests.json"
        cs.USED_IN.clear()
        cs.USED_IN["https://www.nicovideo.jp/user/1"] = ["記事A"]
        need = {"https://www.nicovideo.jp/user/1": "nicovideo.jp", "https://www.nicovideo.jp/user/2": "nicovideo.jp",
                "https://www.tiktok.com/tag/imas": "tiktok.com"}
        rows = cs.request_parsers("2026-09-26", need, escalate_fn=lambda s, d, r: calls.append((s, d, r)) or True)
        check(len(calls) == 1 and calls[0][0] == "classify" and calls[0][1] == "2026-09-26", f"当番の呼び出し: {[(c[0], c[1]) for c in calls]}")
        reason = calls[0][2]
        check("パーサ" in reason and "- nicovideo.jp" in reason and "- tiktok.com" in reason and "https://www.nicovideo.jp/user/2" in reason
              and "記事A" in reason, "依頼文に対象のホスト・URL・使われ方が無い")
        check(len(rows) == 3 and all("当番" in r and "依頼を 2026-09-26 に出した" in r for r in rows), f"記録の行: {rows}")
        rec = json.loads(cs.PARSER_REQUESTS.read_text(encoding="utf-8"))
        check(set(rec) == {"nicovideo.jp", "tiktok.com"} and rec["nicovideo.jp"]["date"] == "2026-09-26", f"依頼の記録: {rec}")
        # 同じ日・翌日の収集では出し直さない
        rows = cs.request_parsers("2026-09-27", need, escalate_fn=lambda s, d, r: calls.append((s, d, r)) or True)
        check(len(calls) == 1 and all("依頼済み(2026-09-26)" in r for r in rows), f"出し直し: {len(calls)}回 / {rows[:1]}")
        # 直らないまま PARSER_RETRY_DAYS を過ぎたら出し直す
        rows = cs.request_parsers("2026-09-30", need, escalate_fn=lambda s, d, r: calls.append((s, d, r)) or True)
        check(len(calls) == 2, f"{cs.PARSER_RETRY_DAYS} 日後の出し直し: {len(calls)}回")
        # 当番を呼べなかった(ONCALL=off)ときは、記録に残さず「人がパーサを書くこと」
        cs.PARSER_REQUESTS.unlink()
        rows = cs.request_parsers("2026-09-26", {"https://www.tiktok.com/tag/x": "tiktok.com"}, escalate_fn=lambda s, d, r: False)
        check(rows and "人がパーサを書くこと" in rows[0] and not json.loads(cs.PARSER_REQUESTS.read_text(encoding="utf-8")), f"呼べなかったとき: {rows}")
        # 当番の再実行(--require-parsers): パーサの無い URL が残っていたら exit 2 で、依頼の記録を消す(次の収集が出し直す)。
        # 残っていなければ通る(監査指摘 r85: 一部の形だけ直った再実行を成功扱いにしない)
        (tmp / "docs" / "_posts").mkdir(parents=True, exist_ok=True)
        (tmp / "candidates").mkdir(exist_ok=True)
        (tmp / "candidates" / "2026-09-26.json").write_text(json.dumps([{"url": "https://www.nicovideo.jp/user/9", "title": "t"}]), encoding="utf-8")
        cs.PARSER_REQUESTS.write_text(json.dumps({"nicovideo.jp": {"date": "2026-09-26"}, "tiktok.com": {"date": "2026-09-26"}}), encoding="utf-8")
        saved_root = (cs.ROOT, cs.POSTS_ONLY, sys.argv, cs.UNRESOLVED)
        try:
            cs.ROOT, cs.POSTS_ONLY, cs.UNRESOLVED = tmp, None, tmp / "classify-unresolved.json"
            sys.argv = ["classify_sources.py", "--date", "2026-09-26", "--apply", "--require-parsers"]
            code = cs.main()
            check(code == 2, f"パーサが残る再実行の exit: {code}")
            check(set(json.loads(cs.PARSER_REQUESTS.read_text(encoding="utf-8"))) == {"tiktok.com"}, "残ったホストの依頼の記録が消えていない")
            (tmp / "candidates" / "2026-09-26.json").write_text(json.dumps([{"url": "https://x.com/i/trending/1", "title": "t"}]), encoding="utf-8")
            code = cs.main()
            check(code == 0, f"パーサの残らない再実行の exit: {code}")
        finally:
            cs.ROOT, cs.POSTS_ONLY, sys.argv, cs.UNRESOLVED = saved_root
    finally:
        cs.PARSER_REQUESTS, used = saved
        cs.USED_IN.clear()
        cs.USED_IN.update(used)
    # 当番の再実行の決め方: classify は号を作り直さない(prompts/ を触っても)
    import oncall
    check(oncall.rerun_policy("classify", ["scripts/classify_sources.py", "prompts/classify-site.md"], "resume") == (False, "出典の判定のやり直し(classify_retag_lint)"),
          "classify の再実行が組版の作り直しになる")
    check(oncall.rerun_policy("classify", ["scripts/classify_sources.py"], "none") == (False, "none"), "classify の none")
    check("classify" in oncall.STAGES and oncall.UNIT_OF["classify"] == "collect", "oncall に classify 工程が無い")


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
    check(cs.platform_unit("https://docs.google.com/")[0] == "skip" and "文書 ID" in cs.platform_unit("https://docs.google.com/")[1], "文書 ID の無い URL の理由")
    # 持ち主はいるはずだが取る手段が無い URL は skip ではなく parser(当番にパーサを書かせる。編集長 2026-09-26「パースしろという判定をしろ」)
    check(cs.platform_unit("https://www.nicovideo.jp/user/12345678") == ("parser", "nicovideo.jp"), f"パーサの無い URL: {cs.platform_unit('https://www.nicovideo.jp/user/12345678')}")
    check(cs.display_base("https://www.nicovideo.jp/user/12345678").startswith("(持ち主を取るパーサが無い: nicovideo.jp"), "パーサの無い URL の表示")
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
    # ニコニコの番組 URL は、ホストと /watch/<ID> の形を検めて拾う(部分一致で拾わない。監査指摘 r82)
    ok_urls = ("https://live.nicovideo.jp/watch/lv351245267", "https://www.nicovideo.jp/watch/sm9", "https://nico.ms/sm9", "https://sp.live.nicovideo.jp/watch/lv1",
               "https://embed.nicovideo.jp/watch/sm9")
    ng_urls = ("https://notnicovideo.jp/watch/sm9", "https://example.test/?next=https://www.nicovideo.jp/watch/sm9", "https://ch.nicovideo.jp/sidem",
               "https://www.nicovideo.jp/user/1/watch/sm9", "https://live.nicovideo.jp/watch/xx1")
    check(all(cs.NICO_ID.search(u) for u in ok_urls) and not any(cs.NICO_ID.search(u) for u in ng_urls), "ニコニコの ID の拾い方")
    check(cs.NICO_ID.search("https://nico.ms/sm9").group(1) == "sm9", "ID の取り出し")
    # チャンネルは投稿主体の構造化フィールドからだけ取る。説明文に貼られた別チャンネルのリンクを拾わない(監査指摘 r82)
    import html as _html
    props_live = {"socialGroup": {"type": "channel", "id": "ch2606757", "socialGroupPageUrl": "https://ch.nicovideo.jp/channel/ch2606757"}}
    page_live = f'<title>315プロNight! - ニコニコ生放送</title><script id="embedded-data" data-props="{_html.escape(json.dumps(props_live))}"></script>'
    check(cs.nico_channel_fetch("lv1", page=page_live, fetch_alias=False) == ("ch.nicovideo.jp/channel/ch2606757", "ch2606757", "315プロNight!"),
          f"生放送のチャンネル: {cs.nico_channel_fetch('lv1', page=page_live, fetch_alias=False)}")
    props_user = {"socialGroup": {"type": "community", "id": "co1"}}
    page_user = (f'<title>ユーザー生放送</title><script id="embedded-data" data-props="{_html.escape(json.dumps(props_user))}"></script>'
                 '<p>関連: <a href="https://ch.nicovideo.jp/channel/ch2606757">公式チャンネル</a></p>')
    check(cs.nico_channel_fetch("lv2", page=page_user, fetch_alias=False) == ("", "", "ユーザー生放送"), "コミュニティ放送に、説明文のチャンネルを付けた")
    page_video = '<title>動画</title><div data-api-data="' + _html.escape(json.dumps({"channel": None, "owner": {"id": 5}})) + '"></div><a href="https://ch.nicovideo.jp/channel/ch2606757">x</a>'
    check(cs.nico_channel_fetch("sm3", page=page_video, fetch_alias=False)[0] == "nicovideo.jp/user/5", "ユーザー投稿の動画に、ページ内のチャンネルを付けた")
    # 動画ページの今の形(2026-10-09 実測): <meta name="server-response"> の data.response。ユーザー投稿は投稿者のユーザーページが単位
    # (単位が無いと合議に掛からず、未確認のまま紙面に残った: sm18526341)
    def page_sr(resp):
        return ('<title>iM@S 6th - ニコニコ動画</title><meta name="server-response" content="'
                + _html.escape(json.dumps({"meta": {"status": 200}, "data": {"metadata": {}, "response": resp}})) + '">'
                '<a href="https://ch.nicovideo.jp/channel/ch2606757">x</a>')
    got = cs.nico_channel_fetch("sm18526341", page=page_sr({"channel": None, "owner": {"id": 17835786, "nickname": "mingya"}}), fetch_alias=False)
    check(got == ("nicovideo.jp/user/17835786", "", "iM@S 6th"), f"今の動画ページからユーザー投稿の投稿者を引けない: {got}")
    got = cs.nico_channel_fetch("so5", page=page_sr({"channel": {"id": "ch2606757"}, "owner": None}), fetch_alias=False)
    check(got[:2] == ("ch.nicovideo.jp/channel/ch2606757", "ch2606757"), f"今の動画ページからチャンネルを引けない: {got}")
    props_ulive = {"socialGroup": {"type": "community", "id": "co1"}, "program": {"supplier": {"supplierType": "user", "programProviderId": "42"}}}
    page_ulive = f'<title>ユーザー生放送</title><script id="embedded-data" data-props="{_html.escape(json.dumps(props_ulive))}"></script>'
    check(cs.nico_channel_fetch("lv3", page=page_ulive, fetch_alias=False)[0] == "nicovideo.jp/user/42", "ユーザー生放送の放送者を引けない")
    page_video2 = '<title>動画</title><div data-api-data="' + _html.escape(json.dumps({"channel": {"id": "2606757", "url": "https://ch.nicovideo.jp/ch2606757"}})) + '"></div>'
    check(cs.nico_channel_fetch("sm4", page=page_video2, fetch_alias=False)[1] == "ch2606757", "チャンネル動画の channel フィールドを読めない")
    # 同じ番組 ID は1回しか取りに行かない(watch が出典行ごとに引く)
    calls = []
    saved_fetch = cs.nico_channel_fetch
    try:
        cs.nico_channel_fetch = lambda vid, page=None, fetch_alias=True: (calls.append(vid), ("ch.nicovideo.jp/x", "ch1", "t"))[1]
        cs._NICO_CACHE.clear()
        cs.nico_channel("lv9"); cs.nico_channel("lv9")
        check(calls == ["lv9"], f"番組ページを2回取りに行った: {calls}")
    finally:
        cs.nico_channel_fetch = saved_fetch
        cs._NICO_CACHE.clear()
    # ニコニコの生放送・動画も投稿者(チャンネル)で決まる(編集長 2026-09-26「これくらい curl して取れよ」)。
    # 表にあるチャンネル(ch.nicovideo.jp/sidem)の番組 lv… は機械で video_ids へ
    saved_nc, saved_root3, saved_st = cs.nico_channel, (cs.ROOT, pipelib.ROOT), (pipelib._ST_TABLE, pipelib._ST_KEY)
    try:
        cs.nico_channel = lambda vid: ("ch.nicovideo.jp/sidem", "ch2606757", "315プロNight!") if vid == "lv351245267" else ("", "", "")
        cs.ROOT = pipelib.ROOT = tmp
        pipelib._ST_TABLE, pipelib._ST_KEY = None, None
        (tmp / "source_types.yml").write_text("path_types:\n  ch.nicovideo.jp/sidem: 公式\nvideo_ids:\n  公式:\n    - AAAAAAAAAAA\n", encoding="utf-8")
        (tmp / "candidates" / "2026-09-19.json").write_text(json.dumps([{"url": "https://live.nicovideo.jp/watch/lv351245267"}, {"url": "https://www.nicovideo.jp/watch/sm999"}]), encoding="utf-8")
        cs.POSTS_ONLY = None
        check(list(cs.unknown_nico("2026-09-19")) == ["lv351245267", "sm999"], f"ニコニコの ID を拾わない: {cs.unknown_nico('2026-09-19')}")
        left, unk = cs.resolve_nico("2026-09-19", apply=True)
        check(pipelib.classify_source("https://live.nicovideo.jp/watch/lv351245267") == "公式", "チャンネルが表にある番組が公式にならない")
        check(left == ["nicovideo:sm999(?)"] and unk == {}, f"チャンネルを引けない動画の扱い: {left} {unk}")
        d, a, p = cs.unknown_targets("2026-09-19")
        check(not d and not p and "https://www.nicovideo.jp/watch/sm999" not in cs.SKIPPED, "ニコニコの動画をドメインや skip に回した")
        # ユーザー投稿: 投稿者のユーザーページが合議に回り、表に載れば動画 ID を機械で足す
        cs.nico_channel = lambda vid: ("nicovideo.jp/user/17835786", "", "キラメキラリ") if vid == "sm999" else ("", "", "")
        left, unk = cs.resolve_nico("2026-09-19", apply=False)
        check(left == ["nicovideo:sm999(nicovideo.jp/user/17835786)"] and list(unk) == ["nicovideo.jp/user/17835786"],
              f"ユーザー投稿の投稿者が合議に回らない: {left} {unk}")
        cs.add_paths({"nicovideo.jp/user/17835786": ("ファン", "個人の投稿")})
        pipelib._ST_TABLE, pipelib._ST_KEY = None, None
        left, unk = cs.resolve_nico("2026-09-19", apply=True)
        pipelib._ST_TABLE, pipelib._ST_KEY = None, None
        check(pipelib.classify_source("https://www.nicovideo.jp/watch/sm999?ref=nicoiphone_other") == "ファン" and not left,
              f"投稿者が表にあるユーザー投稿が決まらない: {left}")
    finally:
        cs.nico_channel, (cs.ROOT, pipelib.ROOT), (pipelib._ST_TABLE, pipelib._ST_KEY) = saved_nc, saved_root3, saved_st
        (tmp / "candidates" / "2026-09-19.json").write_text(json.dumps([{"url": "https://www.youtube.com/watch?v=CCCCCCCCCCC"}]), encoding="utf-8")
    # 「決まらなかった」記録のキーと、watch が表示に使うキーは同じ表記(display_base)。動画は投稿者のチャンネルに寄せる(監査指摘 r73/r74)
    saved_va, saved_unres = cs.video_author, cs.UNRESOLVED
    try:
        cs.video_author = lambda vid: ("imas-official", "題名") if vid == "AAAAAAAAAAA" else ("", "")
        cs.UNRESOLVED = tmp / "metrics" / "classify-unresolved.json"
        cs.save_unresolved(["enso-order.acecombat.jp: 当事者「a」 / 準公式「b」", "youtube.com/@Foo: ファン「c」 / —「」",
                            "somehandle: 演者「d」 / ファン「e」", "youtube:AAAAAAAAAAA(@imas-official)", "youtube:ZZZZZZZZZZZ(@?)",
                            f"{fkey}: 公式「f」 / 当事者「g」"])
        rec = cs.load_unresolved()
        pairs = [("https://enso-order.acecombat.jp/", "https://enso-order.acecombat.jp"),
                 ("https://www.youtube.com/@Foo/videos", "https://youtube.com/@Foo"),
                 ("https://x.com/somehandle/status/1", "https://x.com/somehandle"),
                 ("https://www.youtube.com/watch?v=AAAAAAAAAAA", "https://youtube.com/@imas-official"),
                 ("https://youtu.be/ZZZZZZZZZZZ", "https://youtube.com/watch?v=ZZZZZZZZZZZ"),
                 (form, f"https://{fkey}")]
        for url, want in pairs:
            got = cs.display_base(url)
            check(got == want and got in rec, f"表示キーと記録キーが合わない: {url} → {got}(記録: {sorted(rec)[:3]}…)")
    finally:
        cs.video_author, cs.UNRESOLVED = saved_va, saved_unres
    # 別の行が同じ記録キーに落ちても、先の理由(合議の言い分)を後の行で消さない(BOOTH の商品 → ショップ、動画 → チャンネル。保管指摘 9857d38eb3)
    saved_va, saved_unres, saved_bs = cs.video_author, cs.UNRESOLVED, dict(cs._BOOTH_SHOP)
    try:
        cs.video_author = lambda vid: ("", "")
        cs.UNRESOLVED = tmp / "metrics" / "classify-unresolved-merge.json"
        cs._BOOTH_SHOP["7563732"] = ("nagarestar.booth.pm", "流れ星")
        item = "https://booth.pm/ja/items/7563732"
        cs.save_unresolved(["nagarestar.booth.pm: 両モデルの意見が不一致 当事者「a」 / ファン「b」",
                            f"{item}: BOOTH の商品。ショップ nagarestar.booth.pm の種別が決まらない(ショップの合議を参照)",
                            "youtube:BBBBBBBBBBB(@Bar)", "youtube.com/@Bar: 演者「c」 / ファン「d」",
                            "nagarestar.booth.pm: 両モデルの意見が不一致 当事者「a」 / ファン「b」"])
        rec = cs.load_unresolved()
        shop = rec.get(cs.display_base(item), "")
        check(cs.display_base(item) == "https://nagarestar.booth.pm" and "両モデルの意見が不一致" in shop and "ショップの合議を参照" in shop
              and shop.count("両モデルの意見が不一致") == 1,
              f"BOOTH の商品の行がショップの合議の理由を上書きした: {rec}")
        chan = rec.get("https://youtube.com/@Bar", "")
        check("演者「c」" in chan and "チャンネルが判定表に無い" in chan and rec.get("https://youtube.com/watch?v=BBBBBBBBBBB"),
              f"動画の行とチャンネルの合議の理由が片方だけになった: {rec}")
    finally:
        cs.video_author, cs.UNRESOLVED = saved_va, saved_unres
        cs._BOOTH_SHOP.clear()
        cs._BOOTH_SHOP.update(saved_bs)
    # 取引: 失敗したら判定表と記事を**開始時の中身**へ戻す(未追跡の記事も。git の HEAD ではない。監査指摘)。
    # 組版前(lint=False)は lint を掛けない。戻せなければ理由に「戻せない」
    (tmp / "source_types.yml").write_text("official_domains:\n  - a.example\n", encoding="utf-8")
    post_a = tmp / "docs" / "_posts" / "2026-09-19-a.md"
    before = post_a.read_bytes()
    calls = []
    def fake_run(cmd, timeout):
        calls.append(cmd)
        if any("classify_sources.py" in str(c) for c in cmd):    # 合議が表と記事を書き換えたあと失敗
            (tmp / "source_types.yml").write_text("official_domains:\n  - a.example\n  - b.example\n", encoding="utf-8")
            post_a.write_text(post_a.read_text(encoding="utf-8").replace("未確認", "公式"), encoding="utf-8")
            (tmp / "docs" / "_posts" / "2026-09-19-new.md").write_text("x", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    # 取引の段は pipelib._txn_run(子を自分のプロセスグループで起動し、止めてから戻す)。本物の子プロセスでの中断は
    # test_classify_txn_interrupt が見る
    saved_run, saved_root = pipelib._txn_run, pipelib.ROOT
    journal = tmp / pipelib.TXN_JOURNAL
    try:
        pipelib._txn_run, pipelib.ROOT = fake_run, tmp
        ok, why = pipelib.classify_retag_lint("2026-09-19", posts_only=True, lint=False)
        check(not ok and "合議 exit 1" in why and "boom" in why and "戻せない" not in why, f"取引の失敗の扱い: {ok} {why}")
        check("--posts-only" in calls[0] and not any("lint.py" in str(c) for cmd in calls for c in cmd), f"組版前の旗と lint 無し: {calls}")
        check(post_a.read_bytes() == before and "b.example" not in (tmp / "source_types.yml").read_text(encoding="utf-8")
              and not (tmp / "docs" / "_posts" / "2026-09-19-new.md").exists(), "失敗した取引が開始時の中身へ戻っていない")
        check(not journal.exists(), "戻せた取引の控えが残った")
        # 成功の経路(組版前): 合議 → 付け直し で終わり、lint は掛けない
        calls.clear()
        pipelib._txn_run = lambda cmd, timeout: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1]
        ok, why = pipelib.classify_retag_lint("2026-09-19", posts_only=True, lint=False)
        check(ok and len(calls) == 2, f"組版前の成功の経路: {ok} {len(calls)}")
        ok, why = pipelib.classify_retag_lint("2026-09-19")
        check(ok and any("lint.py" in str(c) for c in calls[-1]), "収集の取引で lint が掛からない")
        check(not journal.exists(), "成功した取引の控えが残った")
        # 戻せないとき: 理由に「戻せない」。控えは残し、次の起動(recover_classify_txn)が戻し直す
        saved_restore = pipelib._restore
        pipelib._restore = lambda snap, new_under=None: "a.md: Permission denied"
        pipelib._txn_run = lambda cmd, timeout: ((tmp / "source_types.yml").write_text("official_domains:\n  - c.example\n", encoding="utf-8"),
                                                 subprocess.CompletedProcess(cmd, 1, "", "cannot"))[1]
        ok, why = pipelib.classify_retag_lint("2026-09-19")
        check(not ok and "戻せない" in why and journal.exists(), f"戻せないときの理由・控え: {why} {journal.exists()}")
        pipelib._restore = saved_restore
        back = pipelib.recover_classify_txn()
        check("開始時へ戻した" in back and "a.example" in (tmp / "source_types.yml").read_text(encoding="utf-8") and not journal.exists(),
              f"残った控えから次の起動が戻さない: {back}")
        # 控えを書けない(空き容量が無い等)ときは、子を起こさずに失敗で返す(戻せない取引を始めない。工程は落とさない)
        calls.clear()
        pipelib._txn_run = lambda cmd, timeout: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1]
        saved_journal = pipelib._write_txn_journal
        pipelib._write_txn_journal = lambda snap, posts: (_ for _ in ()).throw(OSError(28, "No space left on device"))
        import signal as _signal
        handlers = (_signal.getsignal(_signal.SIGTERM), _signal.getsignal(_signal.SIGINT))
        try:
            ok, why = pipelib.classify_retag_lint("2026-09-19")
        finally:
            pipelib._write_txn_journal = saved_journal
        check(not ok and "控えを書けない" in why and not calls and not journal.exists(), f"控えを書けないのに取引を始めた: {ok} {why} {calls}")
        check((_signal.getsignal(_signal.SIGTERM), _signal.getsignal(_signal.SIGINT)) == handlers, "取引が終わっても中断のハンドラが戻らない")
    finally:
        pipelib._txn_run, pipelib.ROOT = saved_run, saved_root


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
        check(p.read_text(encoding="utf-8") == before and not list(tmp.glob(".source_types.yml.*.tmp")),
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


def test_ledger_facts_are_cited(tmp: Path):
    """台帳に「掲載済み」として渡す事実は、記事が根拠として指した事実だけ(見出し・リードの根拠 id、本文の段落の控え、verified_facts)。
    2026-10-10: 学マス運営まとめはプロデュースゼミだけを書いたのに、素材にあったログボの事実まで台帳に載り、以後ログボが既報扱いになった。
    根拠 id の無い古い形の記事だけ、素材の事実で代える。"""
    posts = tmp / "docs" / "_posts"
    posts.mkdir(parents=True)
    (tmp / "metrics").mkdir()
    # 執筆のときの素材: c1=[ゼミ, ログボ], c2=[ガシャ] → F1=ゼミ, F2=ログボ, F3=ガシャ。その後、収集が c1 の先頭に事実を足した
    # (いまの素材から番号を振り直すと F1・F3 がずれる。監査指摘)。台帳は、稿を採ったときの記録(factmap)で引く
    at_write = {"c1": {"id": "c1", "facts": ["ゼミは10/20まで", "ログボは15日まで"]}, "c2": {"id": "c2", "facts": ["ガシャは11日まで"]}}
    _, fmap = renderlib.materials_with_ids([at_write["c1"], at_write["c2"]])
    mats_now = {"c1": {"id": "c1", "facts": ["後から足された事実", "ゼミは10/20まで", "ログボは15日まで"]}, "c2": at_write["c2"]}
    saved = (assemble.POSTS, assemble.ROOT)
    try:
        assemble.POSTS, assemble.ROOT = posts, tmp
        old_text = ("---\nslug: a\ncandidate_ids: [c1, c2]\ntitle_fact_ids: [F1]\nlede_fact_ids: [F1]\n"
                    "verified_facts:\n- id: N1\n  text: ゼミの報酬はSSR\n  url: https://x.example/1\n---\n"
                    "ゼミを開催中だ。 <!-- F1 N1 -->\n\nガシャも開催中。 <!-- F3 -->\n")
        (posts / "2026-10-10-a.md").write_text(old_text, encoding="utf-8")
        old_key = renderlib.draft_key(posts / "2026-10-10-a.md")
        drafts = {old_key: fmap}
        (tmp / "metrics" / "factmap-2026-10-10.json").write_text(json.dumps({"a": drafts}, ensure_ascii=False), encoding="utf-8")
        fm = {"slug": "a", "candidate_ids": ["c1", "c2"], "title_fact_ids": ["F1"], "lede_fact_ids": ["F1"],
              "verified_facts": [{"id": "N1", "text": "ゼミの報酬はSSR", "url": "https://x.example/1"}], "_name": "2026-10-10-a.md"}
        got = assemble.cited_facts(fm, drafts)
        check(got == ["ゼミは10/20まで", "ゼミの報酬はSSR", "ガシャは11日まで"], f"記事が根拠にした事実だけにならない: {got}")
        inp = assemble.build_input("2026-10-10", [fm], mats_now, [], [])
        texts = [f["text"] for f in inp["articles"][0]["facts"]]
        check(texts and set(texts) == {"ゼミは10/20まで", "ゼミの報酬はSSR", "ガシャは11日まで"},
              f"台帳への入力が、稿を採ったときの事実の記録でなく、いまの素材からずれた番号で引かれた: {texts}")
        # 記録にない根拠 id(素材の欠落・取り違え)は引かずに空(見出しだけが台帳に残る)、異常として申告する
        pipelib.ANOMALIES.clear()
        bad = dict(fm, title_fact_ids=["F9"])
        check(assemble.cited_facts(bad, drafts) == [] and any("F9" in a for a in pipelib.ANOMALIES),
              f"引けない根拠 id で、誤った事実を返す・黙って捨てる: {assemble.cited_facts(bad, drafts)} {pipelib.ANOMALIES}")
        pipelib.ANOMALIES.clear()
        # 改稿(素材が増えたあとの番号で F1=ログボ)を採ったあと、除外・組み直しの失敗で旧い稿に巻き戻った: 旧い稿の対応が選ばれる(監査指摘)
        (posts / "2026-10-10-a.md").write_text(old_text.replace("ゼミを開催中だ。", "ログボを開催中だ。"), encoding="utf-8")
        drafts[renderlib.draft_key(posts / "2026-10-10-a.md")] = {"F1": "ログボは15日まで", "F3": "ガシャは11日まで"}
        check(assemble.cited_facts(fm, drafts)[0] == "ログボは15日まで", "いまの稿(改稿)の対応が選ばれない")
        (posts / "2026-10-10-a.md").write_text(old_text, encoding="utf-8")          # 巻き戻し
        check(assemble.cited_facts(fm, drafts)[0] == "ゼミは10/20まで", f"旧い稿に巻き戻ったのに、改稿の対応で引いた: {assemble.cited_facts(fm, drafts)}")
        # 記録はあるのに、いまの稿の対応が無い(記録の無い書き換え): 空と申告
        (posts / "2026-10-10-a.md").write_text(old_text.replace("ゼミを開催中だ。", "記録の無い稿"), encoding="utf-8")
        check(assemble.cited_facts(fm, drafts) == [] and pipelib.ANOMALIES, "いまの稿の対応が無いのに、別の稿の対応で引いた")
        (posts / "2026-10-10-a.md").write_text(old_text, encoding="utf-8")
        pipelib.ANOMALIES.clear()
        # 字面・本文の控えは同じで、見出し・リードの根拠だけを付け替えた改稿(素材が増えて F2=ゼミ)は別の稿。巻き戻すと旧い稿の対応で引く
        re_cited = old_text.replace("title_fact_ids: [F1]\nlede_fact_ids: [F1]", "title_fact_ids: [F2]\nlede_fact_ids: [F2]")
        (posts / "2026-10-10-a.md").write_text(re_cited, encoding="utf-8")
        k_new = renderlib.draft_key(posts / "2026-10-10-a.md")
        check(k_new != old_key, "根拠だけを付け替えた改稿が、旧い稿と同じ指紋になる")
        d2 = {old_key: fmap, k_new: {"F1": "ログボは15日まで", "F2": "ゼミは10/20まで", "F3": "ガシャは11日まで"}}
        (posts / "2026-10-10-a.md").write_text(old_text, encoding="utf-8")       # 巻き戻し
        check(assemble.cited_facts(fm, d2)[0] == "ゼミは10/20まで", "根拠だけ付け替えた改稿から巻き戻したのに、改稿の対応で引いた")
        # 同じ指紋に違う対応が来たら、上書きせず食い違いとして残し、台帳は引かずに申告する
        dd = {}
        compose.merge_fact_map(dd, old_key, fmap)
        compose.merge_fact_map(dd, old_key, {"F1": "ログボは15日まで"})
        check("__conflict__" in dd[old_key] and assemble.cited_facts(fm, dd) == [] and pipelib.ANOMALIES,
              f"同じ稿の指紋に違う対応が来たのに、上書き・黙って引いた: {dd}")
        pipelib.ANOMALIES.clear()
        # 記録のファイルがある号で、記録の無い稿(書き出しの後・記録の前に止まって、--reuse-plan が再利用した 等)は、素材の事実で
        # 代えず見出しだけにして申告する(監査指摘)
        (posts / "2026-10-10-b.md").write_text("---\nslug: b\ncandidate_ids: [c1]\n---\n古い形の記事\n", encoding="utf-8")
        old = {"slug": "b", "candidate_ids": ["c1"], "_name": "2026-10-10-b.md"}
        pipelib.ANOMALIES.clear()
        facts = assemble.build_input("2026-10-10", [old], at_write, [], [])["articles"][0]["facts"]
        check(facts == [] and pipelib.ANOMALIES, f"記録のある号で、記録の無い稿を素材の事実に代えた: {facts}")
        pipelib.ANOMALIES.clear()
        # 記録のファイルが無い号(この仕組みより前)だけ、従来どおり素材の事実で代える
        (tmp / "metrics" / "factmap-2026-10-10.json").unlink()
        fallback = {f["text"] for f in assemble.build_input("2026-10-10", [old], at_write, [], [])["articles"][0]["facts"]}
        check(assemble.cited_facts(old, None) is None and assemble.cited_facts(fm, None) is None
              and fallback == {"ゼミは10/20まで", "ログボは15日まで"}, f"記録の無い号で、素材の事実に代えない: {fallback}")
        # 記録のファイルが壊れている(構文・形)・一度壊れた印がある号では、記録の無い稿を素材の事実で代えない(見出しだけ。申告する。監査指摘)
        for broken in ('{"a": {"k": {"F1": "ゼ', json.dumps({"__corrupted__": "読めなかった"}), "[]", json.dumps({"old": None})):
            (tmp / "metrics" / "factmap-2026-10-10.json").write_text(broken, encoding="utf-8")
            pipelib.ANOMALIES.clear()
            facts = assemble.build_input("2026-10-10", [old], at_write, [], [])["articles"][0]["facts"]
            check(facts == [] and pipelib.ANOMALIES, f"壊れた記録で、記録の無い稿を素材の事実に代えた: {broken[:20]} → {facts}")
        pipelib.ANOMALIES.clear()
    finally:
        assemble.POSTS, assemble.ROOT = saved
    # 組版: 稿を採ったら対応を記録し、台帳づくり(run_assemble)の前と各コミットの前にファイルへ残す(以前の記録と合わせる)
    saved_root = compose.ROOT
    try:
        compose.ROOT = tmp
        compose.FACT_MAP.clear()
        compose.FACT_MAP["2026-10-11"] = {"x": {"k1": {"F1": "一"}}}
        compose.persist_records("2026-10-11")
        compose.FACT_MAP["2026-10-11"] = {"x": {"k2": {"F1": "改"}}, "y": {"k3": {"F1": "二"}}}
        compose.persist_records("2026-10-11")
        rec = json.loads((tmp / "metrics" / "factmap-2026-10-11.json").read_text(encoding="utf-8"))
        check(rec == {"x": {"k1": {"F1": "一"}, "k2": {"F1": "改"}}, "y": {"k3": {"F1": "二"}}},
              f"稿ごとの事実の記録が、以前の記録(旧い稿の対応)と合わさらない: {rec}")
        # 既存の記録が壊れていたら(書きかけで止まった)、申告して「壊れた」印を付けて残す(--reuse-plan の再開で黙って上書きしない)。
        # 書き込みは一度に差し替えるので、一時ファイルは Git 管理外に残るだけ
        # 対応が空でも空の記録を書く(記録のファイルがあること=稿ごとの記録で書いた号、の印。初回の保存の前に止まった号を、
        # 仕組みより前の号と取り違えない。監査指摘)。執筆を始める前に作る
        compose.FACT_MAP.clear()
        compose.persist_records("2026-10-12")
        check(json.loads((tmp / "metrics" / "factmap-2026-10-12.json").read_text(encoding="utf-8")) == {},
              "対応が空のとき記録のファイルを作らない(仕組みより前の号と取り違える)")
        src_main = __import__("inspect").getsource(compose.main)
        check(0 <= src_main.find("persist_records(date)\n    with stage(\"執筆\"):"), "執筆を始める前に記録のファイルを作らない")
        for broken in ('{"x": {"k1": ', "[]", json.dumps({"old": None})):      # 構文が壊れた・形が崩れた
            (tmp / "metrics" / "factmap-2026-10-11.json").write_text(broken, encoding="utf-8")
            pipelib.ANOMALIES.clear()
            compose.FACT_MAP["2026-10-11"] = {"z": {"k9": {"F1": "再"}}}
            compose.persist_records("2026-10-11")
            rec = json.loads((tmp / "metrics" / "factmap-2026-10-11.json").read_text(encoding="utf-8"))
            check(rec.get("__corrupted__") and rec.get("z") == {"k9": {"F1": "再"}} and pipelib.ANOMALIES and renderlib.factmap_valid(rec),
                  f"壊れた記録({broken})を黙って上書きした・印を残さない: {rec}")
        check(not [p for p in (tmp / "metrics").glob(".*")], "一時ファイルが Git の管理する場所に残った")
        pipelib.ANOMALIES.clear()
        check("metrics/factmap-2026-10-11.json" in compose.own_artifacts("2026-10-11"), "途中終了の後始末が事実の記録をコミットしない")
    finally:
        compose.ROOT = saved_root
        compose.FACT_MAP.clear()
    import inspect
    src = inspect.getsource(compose.run_assemble)
    check(0 <= src.find("persist_records(date)") < src.find("_assemble.run("), "台帳づくりの前に、稿ごとの事実の記録を残さない")
    # 執筆・書き直しで稿を採ったときに対応を記録することは、実際に稿を採らせて検める(test_writer_children。ソースの文字列は見ない)


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
                  "collect-rules", "collect-item", "grok-collect", "grok-verify", "grok-deep", "explore", "watch-facts",
                  "classify-rules", "classify-site", "classify-x", "classify-debate",
                  "oncall-fix", "oncall-fix.objections", "oncall-review", "oncall-parser", "oncall-whywhy")


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
    # 見出しは主体と出来事で作り、出所(公式X告知)を入れない(編集長 2026-09-25「どれだよ」)。公式の投稿は発表そのもので、
    # 本文でも「〜と告知している」と断らず事実として書く(編集長「逆だろ」)。断定を避けるのはファン発・未確認だけ
    check("どの作品・ブランド・アイドル・催しの話か" in t and "事実として地の文で書く" in t and "断りは要らない" in t and "伝聞元がファン発・未確認のときだけ" in t,
          "執筆の依頼文: 公式の投稿を事実として書く規則、見出しに出所を入れない規則が無い")
    check("出所を示して書き" not in t and "投稿かが分かる出所を示して" not in t, "「公式 X の投稿では〜と告知している」と書かせる規則が残っている")
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
    check(since and f"since:{since.group(1)}" in gp.split("3. 角度を変えて検索する")[1] and "対象期間より前" in gp, "Grok の依頼文に対象期間の規則が無い")
    check("lead_slug" in compose.lead_prompt("2026-09-18", [{"slug": "s", "rank": "small"}]) and
          "社説" not in compose.lead_prompt("2026-09-18", []), "一面の依頼文(社説は選ばせない)")


def test_assemble_judge():
    """判断を分けて取り、従来と同じ形の1つの出力にまとめる。記事の並び順のまま、pending_remove は重複なし。
    1本でも答えが無ければ全体が上がる(半端な台帳を作らない)。"""
    inp = _assemble_input(19)
    seen = []
    # 模擬の答えも出力 schema の形にする(崩れた答えはまとめに使わない)
    srow = lambda s, subject=None: {"slug": s, "story_id": s, "subject": subject or s, "published_facts": ["F1"]}
    rres = lambda s, d: {"slug": s, "candidate_id": "c1", "date": d, "occurs_on": d, "kind": "締切", "subject": "x", "note": ""}
    padd = lambda k, subject="s", watch="w": {"dedup_key": k, "brand": "cg", "subject": subject, "watch": watch}
    def fake_session(text, date, name, schema, budget=None):
        seen.append((name, sorted(json.loads(schema)["required"])))
        if name == "assemble-digest":
            return {"digest": [{"label": "本日", "rows": []}]}
        slugs = [ln.split()[2] for ln in text.split("\n") if ln.startswith("- slug: ")]
        extra = [srow("どこにも無い")] + ([srow("slug-0")] if "slug-0" not in slugs else [])   # 余計な行(他の組の記事)
        return {"stories": [srow(s) for s in slugs] + extra,
                "reservations": [rres(slugs[0], "2026-10-01")], "pending_add": [padd("よその話題")], "pending_remove": ["p2", "p1"]}
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
            {"stories": [srow("b", "b1"), srow("a", "a1"), srow("a", "a2")],
             "reservations": [rres("b", "2026-10-02"), rres("a", "2026-10-09"), rres("a", "2026-10-01")],
             "pending_add": [padd("kb", "x"), padd("ka", "先"), padd("よそ")],
             "pending_remove": ["p3", "無い"]},
            {"stories": [srow("c", "c1")], "reservations": [],
             "pending_add": [padd("ka", "後")], "pending_remove": ["p1", "p3"]}])
        # 同じ組で同じ記事に2行(a1・a2)は契約違反で採らない(validate が見出しで補う)。他は記事の並び順
        check([s["subject"] for s in m["stories"]] == ["b1", "c1"], f"stories の順・同じ記事への2行: {m['stories']}")
        check([(r["slug"], r["date"]) for r in m["reservations"]] == [("a", "2026-10-01"), ("a", "2026-10-09"), ("b", "2026-10-02")], f"reservations の順: {m['reservations']}")
        check(m["pending_add"] == [padd("ka", "先"), padd("kb", "x")], f"pending_add の規則: {m['pending_add']}")
        check(m["pending_remove"] == ["p1", "p3"], f"pending_remove は入力にあるものを入力の順で: {m['pending_remove']}")
        # 同じ記事・同じ key に**中身の違う**行が重なっても、返ってきた順で採用が変わらない(監査指摘)
        rows = [{"stories": [{"slug": "a", "story_id": "ka", "subject": "甲", "published_facts": ["F1"]},
                             {"slug": "a", "story_id": "ka", "subject": "乙", "published_facts": ["F2"]}],
                 "pending_add": [padd("ka", watch="甲"), padd("ka", watch="乙")],
                 "reservations": [], "pending_remove": []},
                {"stories": [], "reservations": [], "pending_remove": [], "pending_add": [padd("ka", watch="丙")]}]
        flip = [{k: list(reversed(v)) for k, v in rows[0].items()}, rows[1]]
        m1 = assemble.merge_judgments(inp2, [arts[:2], arts[2:]], {"digest": []}, rows)
        m2 = assemble.merge_judgments(inp2, [arts[:2], arts[2:]], {"digest": []}, flip)
        check(m1 == m2 and len(m1["stories"]) == 0 and len(m1["pending_add"]) == 1, f"重複行の採用が返却順で変わる: {m1} / {m2}")
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
        # 時間切れでは子孫(道具が起動したプロセス)まで止める。残すと、やり直しのセッションと並走する(監査指摘)。
        # 回収は死んだのを確かめてから返すので、返った直後に見てよい(待ち時間に頼らない)。孫を起こす前に時間切れになったら、
        # 確かめられなかったとして落とす(例外で止めない)
        gp = tmp / "grandchild.pid"
        gpid = int(gp.read_text().strip() or 0) if gp.exists() else 0
        check(gpid and not _pid_alive(gpid), f"時間切れのあとに孫プロセス {gpid or '(起こす前に時間切れ)'} が残った")
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


def test_deadline_publish_boundary(tmp: Path):
    """「発行を過ぎたか」は実際の発行時刻(06:00)で見る。05:52〜06:00 に始まった組版の締切は 05:52(06:00 の HANDOFF_MIN 前)のままで、
    main は排他も取らず新しい作業を始めない。起動からの相対の締切は 06:00 以後の再実行だけ(監査指摘 2026-10-14: 05:53 の再起動で
    締切が 08:43 に延び、06:00 の発行を待たせうる)。排他を待つ間に締切を過ぎたときも始めない。
    始めずに終えるときは、なぜなぜを当番の保管へ回し、終わりの処理で当番を起こさない(当番が排他を持ったまま診断して 06:00 の発行を
    待たせる。監査指摘 2026-10-14)。締切の前に始まって止まったときは、従来どおり終わりの処理が当番を起こす。"""
    import datetime as _dt
    import oncall
    import runpy
    import time as _time
    import types
    tmp.mkdir(parents=True, exist_ok=True)
    d = "2026-10-14"
    at = lambda h, m: _dt.datetime(2026, 10, 14, h, m, tzinfo=pipelib.JST).timestamp()
    handoff = at(6, 0) - compose.HANDOFF_MIN * 60
    for h, m in ((5, 51), (5, 52), (5, 53), (5, 59)):
        got = compose.hard_deadline(at(h, m), d)
        check(got == handoff, f"{h:02d}:{m:02d} 起動の締切が 05:52 でない: {_dt.datetime.fromtimestamp(got, pipelib.JST):%H:%M}")
    check(compose.hard_deadline(at(6, 0), d) == at(6, 0) + compose.COMPOSE_LIMIT_MIN * 60, "06:00 以後の再実行の締切が起動からの相対でない")
    clock = {"now": 0.0}
    seen: list[str] = []

    def fake_lock(*a, **k):
        seen.append("lock")
        clock["now"] += clock.get("wait", 0)
        if clock.get("timeout"):
            raise pipelib.JobLockTimeout("工程の排他を取れない(テストの代役)")
        return object()

    def fake_checkout(*a, **k):
        seen.append("checkout")
        pipelib.notify("compose", "checkout に失敗(テストの代役)", ok=False)   # 締切の前に始まって止まった、の代役
        return False                     # ここまで来たら打ち切る(締切の検査を通ったことだけを見る)
    escalated: list[list] = []
    saved = (compose.time, compose.job_lock, compose.checkout_edition_branch, sys.argv, oncall.BACKLOG,
             pipelib.job_lock, pipelib.checkout_edition_branch, pipelib.escalate, pipelib._ESCALATED, list(pipelib.ANOMALIES), _time.time)
    backlog = lambda: [r for r in oncall.backlog_open() if r.get("stage") == "compose"]
    try:
        oncall.BACKLOG = tmp / "oncall-backlog.jsonl"
        compose.time = types.SimpleNamespace(time=lambda: clock["now"], sleep=lambda s: None)
        compose.job_lock, compose.checkout_edition_branch = fake_lock, fake_checkout
        sys.argv = ["compose.py", "--date", d, "--reuse-plan"]
        cases = ((at(5, 53), 0, [], "05:53 の再起動"),
                 (at(5, 40), 15 * 60, ["lock"], "05:40 に起動し、排他を待つ間に 05:55"),
                 (at(5, 0), 0, ["lock", "checkout"], "05:00 の起動"))
        for start, wait, want, what in cases:
            clock.update(now=start, wait=wait, timeout=False)
            seen.clear(); pipelib.ANOMALIES.clear()
            n_kept = len(backlog())
            code = compose.main()
            check(code == 1 and seen == want, f"{what}: 締切の前後で始める・始めないを取り違えた(通った工程 {seen})")
            late = want != ["lock", "checkout"]
            check(late == (len(backlog()) == n_kept + 1), f"{what}: 締切を過ぎて始めないときに、なぜなぜを当番の保管へ回さない/誤って回した")
            check(late == (not pipelib.ANOMALIES), f"{what}: 始めずに終えたあとの異常の台帳: {pipelib.ANOMALIES}")
        # 終わりの処理まで通す(compose.py を __main__ として走らせ、当番の起動を記録する)
        pipelib.job_lock, pipelib.checkout_edition_branch = fake_lock, fake_checkout
        pipelib.escalate = lambda stage, date, reason, **k: escalated.append([stage, date, k.get("rerun")]) or True
        _time.time = lambda: clock["now"]
        for start, wait, timeout, late, what in ((at(5, 55), 0, False, True, "05:55 の再起動"),
                                                 (at(5, 0), 0, False, False, "05:00 に起動して止まった"),
                                                 (at(5, 45), 10 * 60, True, True, "05:45 に起動し、排他を取れないまま 05:55"),
                                                 (at(5, 0), 10 * 60, True, False, "05:00 に起動し、排他を取れないまま 05:10")):
            clock.update(now=start, wait=wait, timeout=timeout)
            escalated.clear(); pipelib.ANOMALIES.clear(); pipelib._ESCALATED = False
            try:
                runpy.run_path(str(Path(compose.__file__)), run_name="__main__")
                code = None
            except SystemExit as e:
                code = e.code
            check(code == 1 and (escalated == []) == late,
                  f"{what}: 終わりの処理が当番を{'起こした(発行の排他を塞ぐ)' if late else '起こさない'}: exit {code} / {escalated}")
        check(len(backlog()) == 4, f"当番の保管に積んだなぜなぜの数が違う: {backlog()}")
    finally:
        (compose.time, compose.job_lock, compose.checkout_edition_branch, sys.argv, oncall.BACKLOG,
         pipelib.job_lock, pipelib.checkout_edition_branch, pipelib.escalate, pipelib._ESCALATED, anomalies, _time.time) = saved
        pipelib.ANOMALIES[:] = anomalies
        pipelib.STOPPING.clear()    # 途中終了の後始末(stop_groups)が立てた「終えるところ」を、後のテストのために戻す


def test_x_anonymous_post_url():
    """`x.com/i/status/<ID>` は投稿者を引いて `x.com/<投稿者>/status/<ID>` に直す。直さないと種別を判定する単位
    (X のアカウント)が無く、紙面に未確認の出典が残る(2026-10-03: trend 面の 16件)。引けなければ元のまま"""
    import collect
    asked = []
    def fake(url):
        asked.append(url)
        return json.dumps({"author_url": "https://x.com/ChuLo5738", "url": "https://x.com/ChuLo5738/status/2105957772651385193"})
    check(collect.x_post_url("https://x.com/i/status/2105957772651385193", fetch=fake)
          == "https://x.com/ChuLo5738/status/2105957772651385193", "i/status の投稿者を引いていない")
    check(asked and asked[0].startswith("https://publish.twitter.com/oembed?") and "2105957772651385193" in asked[0], f"oEmbed の問い合わせ: {asked}")
    check(collect.x_post_url("https://twitter.com/i/web/status/5?s=20", fetch=fake) == "https://x.com/ChuLo5738/status/5", "i/web/status の形")
    asked.clear()
    check(collect.x_post_url("https://x.com/imas_official/status/1", fetch=fake) == "https://x.com/imas_official/status/1" and not asked,
          "投稿者のある URL まで問い合わせた")
    check(collect.x_post_url("https://x.com/i/trending/123", fetch=fake) == "https://x.com/i/trending/123", "トレンドは投稿ではない")
    def broken(url):
        raise OSError("404")
    check(collect.x_post_url("https://x.com/i/status/7", fetch=broken) == "https://x.com/i/status/7", "引けないときは元のまま")
    check(collect.x_post_url("https://x.com/i/status/7", fetch=lambda u: json.dumps({"author_url": "https://evil.example/a"}))
          == "https://x.com/i/status/7", "ハンドルでない author_url を採った")
    # 収集の正規化を通すと、種別を判定できる形で候補に載る
    saved = collect.http_get
    try:
        collect.http_get = fake
        got = collect.normalize([{"url": "https://x.com/i/status/2105957772651385193", "title": "t", "brand": "general",
                                  "dedup_key": "k", "facts": ["f"], "_via": "grok"}])
    finally:
        collect.http_get = saved
    check([c["url"] for c in got] == ["https://x.com/ChuLo5738/status/2105957772651385193"], f"収集の正規化で直らない: {got}")
    import classify_sources as cs
    check(cs.platform_unit(got[0]["url"]) == ("x", "ChuLo5738"), "判定の単位が付かない")


def test_normalize_fixes_misnamed_brand():
    """collector の面に候補のどこにも根拠が無く、見出し・主題キーが別の作品を1つだけ名指しするときは、名指しの作品の面へ直す
    (2026-10-18号: トレンドの探索が vα-liv の告知に gaku を付け、学マス面と vα-liv 面に同じ話題の記事が立ち、校閲で2本を落とした)。
    根拠のある面・合同・作品が名指しされない候補はそのまま"""
    import collect
    def one(**kw):
        return collect.normalize([{"url": "https://example.com/" + kw.pop("u", "a"), "facts": kw.pop("facts", ["f"]),
                                   "_via": "grok", **kw}])[0]["brand"]
    check(one(title="ヴイアラ 1stLIVE全編の期間限定プレミア公開", brand="gaku", dedup_key="valiv-1st-live-full-premiere-2026",
              facts=["ヴイアラ 1stLIVE全編をプレミア公開。", "公開期間: 10/17(土)22:30-10/18(日)23:59まで。"]) == "dsva",
          "vα-liv の告知が学マス面のまま")
    check(one(title="レトラ BIRTHDAY ONLINE LIVE 現地ライブビューイング先行", brand="gaku",
              dedup_key="valiv-letora-birthday-live-lv-2nd-lottery-2026", facts=["先行抽選二次受付が開始。"]) == "dsva",
          "主題キーが vα-liv を名指しする候補が学マス面のまま")
    # ミリシタのガシャに 765 のアイドル(collector は million): 名鑑で million に当たらなくても、見出しが別の作品を名指ししないので直さない
    check(one(title="菊地真Birthdayガシャ", brand="million", dedup_key="makoto-birthday-gasha", facts=["菊地真のガシャ"]) == "million",
          "作品の名指しの無い候補の面を、アイドル名だけで動かした")
    # シャニソンの 765 コラボ(collector は shiny): 見出しが 765 側を名指ししても、shiny の根拠があるので直さない
    check(one(title="シャニソン 765プロコラボ第5弾", brand="shiny", dedup_key="shiny-765as-collab-5", facts=["765as とのコラボ"]) == "shiny",
          "collector の面に根拠のある候補を動かした")
    check(one(title="学マスとヴイアラの合同配信", brand="joint", dedup_key="valiv-gaku-joint") == "joint", "合同を動かした")
    check(one(title="ヴイアラ 配信", brand="other", dedup_key="valiv-stream") == "dsva", "面の無い候補の補正が効かない")


def test_x_anonymous_post_in_posts(tmp: Path):
    """紙面に**既に載った** `x.com/i/status/<ID>` も、投稿者を引いてアカウントを合議に掛け、決まった種別を
    投稿 ID で表(x_posts)に写し、付け直しで記事の種別まで届く。過去紙面の URL は書き換えない
    (2026-10-03: 記事2件の出典が判定の単位なしで未確認のまま残り、毎朝の watch が同じ異常を出す。監査指摘)。"""
    import yaml
    import classify_sources as cs
    import retag_sources as rt
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "docs" / "_posts").mkdir(parents=True)
    (tmp / "candidates").mkdir()
    a, b = "https://x.com/i/status/2105957772651385193", "https://x.com/i/status/2105254556238291021"
    post = tmp / "docs" / "_posts" / "2026-10-03-k.md"
    post.write_text("---\n" + article_fm(sources=[{"label": "l1", "url": a, "type": "未確認"}, {"label": "l2", "url": b, "type": "未確認"}])
                    + "---\n本文\n", encoding="utf-8")
    (tmp / "source_types.yml").write_text("x_accounts:\n  ファン:\n    - somebody\n", encoding="utf-8")
    authors = {"2105957772651385193": "ChuLo5738", "2105254556238291021": "mkzk_CRESCENT"}
    asked = []
    def fake_consensus(prompt, keys, need=None):
        asked.append(sorted(keys))
        return ({"ChuLo5738": ("ファン", "個人")} if "ChuLo5738" in keys else {}), \
               [f"{k}: ファン「個人」 / 当事者「店」" for k in keys if k == "mkzk_CRESCENT"]
    saved = (cs.ROOT, cs.POSTS_ONLY, cs.UNRESOLVED, cs.consensus, cs.x_post_author, rt.POSTS,
             pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY, sys.argv)
    try:
        cs.ROOT = pipelib.ROOT = tmp
        cs.POSTS_ONLY = None
        cs.UNRESOLVED = tmp / "metrics" / "classify-unresolved.json"
        cs.consensus = fake_consensus
        cs.x_post_author = lambda pid, fetch=None: authors.get(pid, "")
        cs._X_AUTHOR.clear()
        rt.POSTS = tmp / "docs" / "_posts"
        pipelib._ST_TABLE, pipelib._ST_KEY = None, None
        sys.argv = ["classify_sources.py", "--date", "2026-10-04", "--apply"]
        check(cs.main() == 0, "合議が落ちた")
        check(asked == [["ChuLo5738", "mkzk_CRESCENT"]], f"投稿者のアカウントが合議に掛からない: {asked}")
        t = pipelib.source_type_table()
        check(t["x_accounts"].get("ファン") and "ChuLo5738" in t["x_accounts"]["ファン"], f"アカウントが表に入らない: {t['x_accounts']}")
        check({str(i) for i in (t.get("x_posts") or {}).get("ファン") or []} == {"2105957772651385193"},
              f"決まった投稿 ID が x_posts に入らない(決まらなかったものは入れない): {t.get('x_posts')}")
        check(pipelib.classify_source(a) == "ファン" and pipelib.classify_source(b) == "未確認", "投稿 ID で種別を引けない")
        check(pipelib.classify_source("https://x.com/i/web/status/2105957772651385193") == "ファン", "i/web/status の形")
        # 付け直しで記事の種別が更新される。URL はそのまま(過去紙面は種別しか変えられない)
        sys.argv = ["retag_sources.py", "--apply"]
        rt.main()
        fm = yaml.safe_load(post.read_text(encoding="utf-8").split("---\n")[1])
        check([(x["url"], x["type"]) for x in fm["sources"]] == [(a, "ファン"), (b, "未確認")], f"記事の種別: {fm['sources']}")
        # 決まらなかったものは、watch がアカウントの単位で引ける(なぜ決まらなかったかが付く)
        check(cs.display_base(b) == "https://x.com/mkzk_CRESCENT", f"watch に出す単位: {cs.display_base(b)}")
        check("当事者" in cs.load_unresolved().get(cs.display_base(b), ""), f"決まらなかった理由を引けない: {cs.load_unresolved()}")
        # 2回目: アカウントが表にあれば合議に掛けず、投稿 ID を足すだけ(二重に足さない)
        asked.clear()
        sys.argv = ["classify_sources.py", "--date", "2026-10-04", "--apply"]
        check(cs.main() == 0 and asked == [["mkzk_CRESCENT"]], f"表にあるアカウントを合議に掛けた: {asked}")
        # 投稿者を引けない投稿は、理由付きで飛ばす
        cs._X_AUTHOR.clear()
        cs.x_post_author = lambda pid, fetch=None: ""
        cs.unknown_targets("2026-10-04")
        check(b in cs.SKIPPED and "投稿者を引けない" in cs.SKIPPED[b], f"投稿者を引けない投稿: {cs.SKIPPED}")
    finally:
        (cs.ROOT, cs.POSTS_ONLY, cs.UNRESOLVED, cs.consensus, cs.x_post_author, rt.POSTS,
         pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY, sys.argv) = saved
        cs._X_AUTHOR.clear()


def test_collect_classify_gate(tmp: Path):
    """収集が空振り(候補0件)でも、判定済みの候補ばかりでも、紙面に未確認の出典が残っていれば合議〜付け直しへ
    進む。候補の未確認だけで入口を閉じると、紙面の未確認が次の collect でも処理されない(監査指摘 2026-10-03)"""
    import collect
    import classify_sources as cs
    (tmp / "docs" / "_posts").mkdir(parents=True)
    post = tmp / "docs" / "_posts" / "2026-10-03-k.md"
    saved = cs.ROOT
    try:
        cs.ROOT = tmp
        post.write_text("---\ntitle: t\nsources:\n- label: l\n  url: https://x.com/i/status/1\n  type: ファン\n---\n本文\n",
                        encoding="utf-8")
        check(not collect.needs_classify([]), "紙面にも候補にも未確認が無いのに合議へ進む")
        check(collect.needs_classify([{"url": "https://a.example/", "source_type": "未確認"}]), "候補の未確認で進まない")
        post.write_text(post.read_text(encoding="utf-8").replace("type: ファン", "type: 未確認"), encoding="utf-8")
        check(collect.needs_classify([]), "空の収集で紙面の未確認を合議へ回さない")
        check(collect.needs_classify([{"url": "https://a.example/", "source_type": "公式"}]),
              "判定済みの候補ばかりの収集で紙面の未確認を合議へ回さない")
    finally:
        cs.ROOT = saved


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


def test_extract_json_array_strict():
    """定点観測 facts 化の読み取り(当番 2026-10-02「facts 化の出力が読めなかった(6件)」):
    モデルが配列のあとに `]` を含む地の文を添えても、正しく出ていた配列を落とさない。
    途中切れ・壊れ・配列無しのときだけ None(=読めなかった。0件の [] とは別)を返す。"""
    E = pipelib.extract_json_array_strict
    check(E('[{"a":1}]') == [{"a": 1}], "配列だけの出力が読めない")
    check(E('はい:\n[{"a":1}]') == [{"a": 1}], "前置きのある出力が読めない")
    # 後置き(配列のあとの注記)に `]` があっても、greedy に末尾まで取って壊さない ← 直した欠陥
    check(E('[{"a":1}]\n\n注: 2件は取得できず[次回WebFetchで再取得]。') == [{"a": 1}],
          "配列のあとに ] を含む注記があると読めなくなる(greedy 取り)")
    check(E('```json\n[{"a":1},{"b":2}]\n```\n以上[6件中4件]。') == [{"a": 1}, {"b": 2}], "コードフェンス+後置きが読めない")
    # 文字列リテラルの中の括弧・エスケープした引用符は配列の切れ目と取り違えない
    check(E('[{"facts":["受付[先行] 8/8〜8/24"]}]\n完了。') == [{"facts": ["受付[先行] 8/8〜8/24"]}], "本文中の [ ] で切れ目を誤る")
    check(E('[{"t":"ガシャ\\"SP\\"開催"}] 済') == [{"t": 'ガシャ"SP"開催'}], "エスケープした引用符で切れ目を誤る")
    # 「読めなかった」(None)と「0件だった」([])を取り違えない
    check(E('[{"a":1},{"b":2') is None, "途中切れ(閉じ ] が無い)を読めたことにした")
    check(E('[{"a": }]') is None, "壊れた JSON を読めたことにした")
    check(E("配列はありません") is None, "配列の無い出力を読めたことにした")
    check(E("[]") == [], "空配列(0件)を読めなかった(None)に落とした")


def test_fetch_page_renders_whitespace_only_page():
    """素の HTML がタイトルとメニューと大量の改行だけのページは、描画し直して読む。空白込みで数えると
    400字を超えて描画せず、執筆が本文を読めずに記事を見送った(2026-10-08: 公式ポータルの LP、空白込み 591字)"""
    import fetch_page
    thin = "【ミリシタ】動画フレーム配布キャンペーン | 【公式】アイドルマスター ポータル" + "\n" * 500 + "メニュー\n検索\nログイン" + "\n" * 100
    check(len(thin.strip()) >= 400 and pipelib.looks_unrendered(thin), "改行だらけの描画前ページを本文ありと判定した")
    check(not pipelib.looks_unrendered("本文。" * 200), "本文のあるページを描画前と判定した")
    calls = []
    class R:
        returncode, stdout = 0, "<html><body><p>（2026.10.7 変更）営利を目的とした制作・配信は行わないでください。</p></body></html>"
    saved = (fetch_page.fetch, pipelib.run_group, sys.argv)
    try:
        fetch_page.fetch = lambda url: thin
        pipelib.run_group = lambda *a, **k: calls.append(a) or R()
        sys.argv = ["fetch_page.py", "https://idolmaster-official.jp/lp/x"]
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fetch_page.main()
    finally:
        fetch_page.fetch, pipelib.run_group, sys.argv = saved
    check(calls and "2026.10.7 変更" in buf.getvalue(), f"改行だらけのページを描画し直さなかった: {buf.getvalue()[:120]!r}")
    # 描画必須と分かっているサイト(sources.yml の portal)は、メニューだけで非空白 400字を超えても描画して読む
    # (公式ポータルはナビだけで非空白 3千字を超える。監査指摘 render-detection-navigation)
    nav = "【公式】アイドルマスター ポータル メニュー スケジュール 検索 ブランド選択 " * 100
    check(len(re.sub(r"\s+", "", nav)) >= 3000 and not pipelib.looks_unrendered(nav), "長いナビの前提が崩れた")
    check("idolmaster-official.jp" in pipelib.render_hosts(), f"公式ポータルが描画必須のサイトに入っていない: {pipelib.render_hosts()}")
    check(not pipelib.needs_render("https://example.com/a", nav), "描画必須でないサイトの本文ありページを描画した")
    calls.clear()
    try:
        fetch_page.fetch = lambda url: nav
        pipelib.run_group = lambda *a, **k: calls.append(a) or R()
        sys.argv = ["fetch_page.py", "https://idolmaster-official.jp/news/01_99999"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fetch_page.main()
    finally:
        fetch_page.fetch, pipelib.run_group, sys.argv = saved
    check(calls and "2026.10.7 変更" in buf.getvalue(), f"公式ポータルの長いナビのページを描画し直さなかった: {buf.getvalue()[:120]!r}")
    # 収集の裏取りも同じ: 公式ポータルの長いナビのページは描画して期間を抜き出す
    import collect
    class U:
        status = 200
        headers = type("H", (), {"get_content_charset": lambda self: "utf-8"})()
        def read(self, n=None): return ("<p>" + nav + "</p>").encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False
    rcalls = []
    saved_c = (collect.urllib.request.urlopen, collect.fetch_rendered, collect.classify_source)
    try:
        collect.urllib.request.urlopen = lambda req, timeout=15: U()
        collect.fetch_rendered = lambda url: rcalls.append(url) or "<p>受付期間：2026年10月8日(木)12:00〜10月12日(日)23:59</p>"
        collect.classify_source = lambda u: "公式"
        cv = collect.normalize([{"url": "https://idolmaster-official.jp/news/01_99999", "title": "t", "facts": ["f"], "_via": "explore"}])
        collect.verify(cv)
    finally:
        collect.urllib.request.urlopen, collect.fetch_rendered, collect.classify_source = saved_c
    check(rcalls and cv[0].get("periods") == ["受付期間: 2026年10月8日(木)12:00〜10月12日(日)23:59"],
          f"裏取りが公式ポータルの長いナビのページを描画せず期間を落とした: {rcalls} {cv[0].get('periods')}")


def test_fetch_page_records_read_pages(tmp: Path):
    """fetch_page.py は本文を取れたページだけを、環境変数 IMAS_FETCH_RECORD の記録に残す(頼んだ URL とリダイレクトの行き先)。
    取れなかったページ(FETCH_FAILED)は残さない。記録を書けなくても読み取りは止めない。検算はこの記録で根拠の URL を照らす
    (2026-10-14 rule_bench: 執筆が fetch_page.py の取れなかった報道を Web 検索で読んで書いた答えが検算を通った)"""
    import contextlib, io
    import fetch_page
    tmp.mkdir(parents=True, exist_ok=True)
    rec = tmp / "rec" / "slug.jsonl"            # 置き場のフォルダが無くても作って書く
    pages = tmp / "pages.json"
    pages.write_text(json.dumps({"https://a.example/x": "告知の本文"}, ensure_ascii=False), encoding="utf-8")
    page = "<html><body><p>" + "公式の告知の本文。" * 80 + "</p></body></html>"

    class U:
        status = 200
        headers = type("H", (), {"get_content_charset": lambda self: "utf-8"})()
        def geturl(self): return "https://www.lantis.jp/release_1.html"
        def read(self, n=None): return page.encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def run(url, record=None, fixed=None):
        env_keys = (pipelib.FETCH_RECORD_ENV, fetch_page.FIXED_PAGES_ENV)
        saved_env = {k: os.environ.get(k) for k in env_keys}
        saved = (sys.argv, fetch_page.urllib.request.urlopen, fetch_page.needs_render)
        out, err = io.StringIO(), io.StringIO()
        try:
            for k, v in zip(env_keys, (record, fixed)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = str(v)
            fetch_page.urllib.request.urlopen = lambda req, timeout=25: U()
            fetch_page.needs_render = lambda u, t: False
            sys.argv = ["fetch_page.py", url]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = fetch_page.main()
        finally:
            sys.argv, fetch_page.urllib.request.urlopen, fetch_page.needs_render = saved
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return rc, out.getvalue(), err.getvalue()
    rc1, _, _ = run("https://a.example/x", rec, pages)              # 事例の本文(取れる)
    rc2, out2, _ = run("https://a.example/missing", rec, pages)     # 事例に無い(取れない)
    rc3, _, _ = run("https://lnk.to/1", rec)                         # 本物の取得の経路: リダイレクトの行き先も残す
    lines = [json.loads(l) for l in rec.read_text(encoding="utf-8").splitlines()] if rec.exists() else []
    check(rc1 == 0 and rc2 == 1 and out2.startswith("FETCH_FAILED") and rc3 == 0, f"fetch_page の終了: {rc1} {rc2} {rc3}")
    check(lines == [{"url": "https://a.example/x", "final": None}, {"url": "https://lnk.to/1", "final": "https://www.lantis.jp/release_1.html"}],
          f"本文を取れたページ(とリダイレクトの行き先)だけを記録しない: {lines}")
    check(pipelib.fetched_urls(rec) == {"https://a.example/x", "https://lnk.to/1", "https://www.lantis.jp/release_1.html"},
          f"記録から読めたページを読めない: {pipelib.fetched_urls(rec)}")
    # 記録を書けない(置き場がフォルダ)ときも本文は返し、書けなかったことを stderr に出す。記録の変数が無ければ何も書かない
    rc4, out4, err4 = run("https://a.example/x", tmp, pages)
    check(rc4 == 0 and "告知の本文" in out4 and "FETCH_RECORD_FAILED" in err4, f"記録を書けないと読み取りが止まる・黙る: {rc4} {err4[:80]}")
    before = rec.read_text(encoding="utf-8")
    rc5, _, _ = run("https://a.example/x", None, pages)
    check(rc5 == 0 and rec.read_text(encoding="utf-8") == before, "記録の変数が無いのに記録に書いた")
    # 壊れた行・無い記録は飛ばす(読めた分だけ返す)
    rec.write_text(before + "{壊れた行\n[1]\n", encoding="utf-8")
    check(pipelib.fetched_urls(rec) == {"https://a.example/x", "https://lnk.to/1", "https://www.lantis.jp/release_1.html"}
          and pipelib.fetched_urls(tmp / "none.jsonl") == set(), "壊れた行・無い記録で、読めたページの読み方が崩れる")


def test_fetch_rendered_skips_images():
    """描画取得は画像を読み込まない。画像の多い一覧(columbia.jp/idolmaster/)は画像待ちで読み込み完了に届かず、
    毎回時間切れで空を返していた(2026-10-07)。selenium の無い環境でも確かめられるよう、ブラウザは偽物に差し替える"""
    import importlib
    import types
    got = {}
    class Opts:
        def __init__(self): self.args, self.prefs = [], {}
        def add_argument(self, a): self.args.append(a)
        def add_experimental_option(self, k, v): self.prefs[k] = v
    fake = {n: types.ModuleType(n) for n in ("selenium", "selenium.webdriver", "selenium.webdriver.chrome",
                                             "selenium.webdriver.chrome.options", "selenium.webdriver.common",
                                             "selenium.webdriver.common.by", "selenium.webdriver.support",
                                             "selenium.webdriver.support.ui", "selenium.webdriver.support.expected_conditions")}
    fake["selenium.webdriver"].Chrome = lambda options: got.setdefault("opts", options)
    fake["selenium"].webdriver = fake["selenium.webdriver"]
    fake["selenium.webdriver.chrome.options"].Options = Opts
    fake["selenium.webdriver.common.by"].By = object
    fake["selenium.webdriver.support.ui"].WebDriverWait = object
    fake["selenium.webdriver.support"].expected_conditions = fake["selenium.webdriver.support.expected_conditions"]
    saved = {n: sys.modules.get(n) for n in list(fake) + ["fetch_rendered"]}
    try:
        sys.modules.update(fake)
        sys.modules.pop("fetch_rendered", None)
        importlib.import_module("fetch_rendered").make_driver()
    finally:
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m
    check(got["opts"].prefs.get("prefs", {}).get("profile.managed_default_content_settings.images") == 2,
          f"描画取得が画像を読み込む(画像の多いページで読み込み完了に届かず時間切れになる): {got['opts'].prefs}")


def test_render_failure_keeps_cause(tmp: Path):
    """描画取得の失敗は、終了コードと stderr の要点を記録に残す。空文字に変えると呼び出し側の記録は「空の応答」だけになり、
    当番が別に再現して TimeoutException を特定していた(2026-10-07 コロムビアの一覧。当番の指摘 dc23e62472)"""
    import contextlib, io, types, importlib
    import collect
    import fetch_page
    tb = ("Traceback (most recent call last):\n  File \"x.py\", line 1\n"
          "selenium.common.exceptions.TimeoutException: Message: \nStacktrace:\n#0 0x55 <unknown>\n#1 0x56 <unknown>\n"
          "RENDER_FAILED: TimeoutException: timeout: Timed out receiving message from renderer: 39.9\n")
    class Bad:
        returncode, stdout, stderr = 1, "", tb
    def timeout_run(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 90, output=b"", stderr=b"RENDER_FAILED: TimeoutException: page load\n")
    saved = pipelib.run_group
    errs = []
    try:
        for run in (lambda *a, **k: Bad(), timeout_run):
            pipelib.run_group = run
            try:
                pipelib.render_page("https://columbia.jp/idolmaster/")
                errs.append("")
            except pipelib.RenderFailed as e:
                errs.append(str(e))
        # 定点観測の一覧(portal): 失敗の原因が観測先の失敗の記録に載る
        pipelib.run_group = lambda *a, **k: Bad()
        try:
            collect.list_source({"id": "c", "type": "portal", "url": "https://columbia.jp/idolmaster/", "base": "https://columbia.jp",
                                 "list_regex": '<a href="([^"]+)">(.*?)</a>'}, set())
            errs.append("")
        except RuntimeError as e:
            errs.append(str(e))
        # 出典照合(fetch_page.py): 描画できず本文も無いとき、原因を FETCH_FAILED に添える
        saved_f, saved_argv = fetch_page.fetch, sys.argv
        buf, ebuf = io.StringIO(), io.StringIO()
        try:
            fetch_page.fetch = lambda url: ""
            sys.argv = ["fetch_page.py", "https://columbia.jp/idolmaster/"]
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(ebuf):
                rc = fetch_page.main()
        finally:
            fetch_page.fetch, sys.argv = saved_f, saved_argv
    finally:
        pipelib.run_group = saved
    check("終了コード 1" in errs[0] and "TimeoutException" in errs[0] and "#0" not in errs[0],
          f"描画の失敗に終了コード・例外の要点が無い(またはスタックの屑が入った): {errs[0]!r}")
    check("時間切れ" in errs[1] and "TimeoutException" in errs[1], f"描画の打ち切りに原因が無い: {errs[1]!r}")
    check("TimeoutException" in errs[2] and "終了コード 1" in errs[2], f"一覧の取得失敗の記録が原因を失った: {errs[2]!r}")
    check(rc == 1 and "TimeoutException" in buf.getvalue() and "終了コード 1" in buf.getvalue(),
          f"出典照合の取得失敗に描画の原因が無い: {buf.getvalue()!r}")
    # facts 化: 描画できなかった新着は、原因を添えて WebFetch へ回す
    prompts = []
    saved_c = (collect.fetch_rendered, collect.urllib.request.urlopen, collect.notify)
    def fail(url, timeout=30):
        raise pipelib.RenderFailed(f"{url} の描画に失敗: 終了コード 1: RENDER_FAILED: TimeoutException: x")
    try:
        collect.fetch_rendered = fail
        collect.notify = lambda *a, **k: None
        collect.facts_batch([{"url": "https://columbia.jp/a", "title": "t", "brand": "imas", "csr": True}],
                            lambda prompt, timeout=0: prompts.append(prompt) or None, {})
        # 裏取り: 出典を取れなかった候補は、取れなかった原因を verify_note に残す
        def boom(req, timeout=15):
            raise OSError("Connection reset by peer")
        collect.urllib.request.urlopen = boom
        cv = [{"url": "https://example.com/a", "title": "t", "facts": [], "source_type": "公式", "via": "explore"}]
        collect.verify(cv)
    finally:
        collect.fetch_rendered, collect.urllib.request.urlopen, collect.notify = saved_c
    check(prompts and "TimeoutException" in prompts[0], "facts 化の素材に描画の失敗の原因が無い")
    check(cv[0]["verify"] == "failed" and "Connection reset by peer" in str(cv[0].get("verify_note")),
          f"裏取りの取得失敗の原因を残さない: {cv[0]}")
    # fetch_rendered.py 自身: 例外は RENDER_FAILED の1行で出し、非0で終わる
    fake = {n: types.ModuleType(n) for n in ("selenium", "selenium.webdriver", "selenium.webdriver.chrome",
                                             "selenium.webdriver.chrome.options", "selenium.webdriver.common",
                                             "selenium.webdriver.common.by", "selenium.webdriver.support",
                                             "selenium.webdriver.support.ui", "selenium.webdriver.support.expected_conditions")}
    fake["selenium"].webdriver = fake["selenium.webdriver"]
    fake["selenium.webdriver.chrome.options"].Options = object
    fake["selenium.webdriver.common.by"].By = object
    fake["selenium.webdriver.support.ui"].WebDriverWait = object
    fake["selenium.webdriver.support"].expected_conditions = fake["selenium.webdriver.support.expected_conditions"]
    saved_m = {n: sys.modules.get(n) for n in list(fake) + ["fetch_rendered"]}
    saved_argv = sys.argv
    ebuf = io.StringIO()
    try:
        sys.modules.update(fake)
        sys.modules.pop("fetch_rendered", None)
        fr = importlib.import_module("fetch_rendered")
        class TimeoutException(Exception):
            pass
        def raise_timeout(*a):
            raise TimeoutException("Message: timeout: Timed out receiving message from renderer\nStacktrace:\n#0 0x55")
        fr.fetch = raise_timeout
        sys.argv = ["fetch_rendered.py", "https://columbia.jp/idolmaster/"]
        with contextlib.redirect_stderr(ebuf):
            rc = fr.main()
    finally:
        sys.argv = saved_argv
        for n, m in saved_m.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m
    check(rc == 1 and ebuf.getvalue().strip() == "RENDER_FAILED: TimeoutException: timeout: Timed out receiving message from renderer",
          f"描画取得が例外の要点を1行で出さない: {rc} {ebuf.getvalue()!r}")


def test_render_failure_reaches_classify_and_verify(tmp: Path):
    """描画の失敗は、素の本文で続ける経路でも原因を記録に残す。出典の分類(サイトの主体を掘る材料)は fetch_page.py の
    stdout だけを読み、描画の失敗(stderr に出て終了コード0)を捨てていた。公式ポータルでは素の本文がメニューだけになり、
    分類の材料がメニューだけでも、描画の時間切れのせいだと分からなかった(監査指摘 render-diagnostic-lost-in-classification)。
    裏取りも同じ型: 描画できず写しが見えなかったのを「URL の取り違え」とだけ記録していた"""
    import contextlib, io, json
    import classify_sources as cs
    import collect
    import fetch_page
    menu = "<html><body>" + "".join(f"<a href='/m{i}'>メニュー{i}</a> " for i in range(40)) + "</body></html>"
    def render_fail(url, timeout=30):
        raise pipelib.RenderFailed(f"{url} の描画に失敗: 終了コード 1: RENDER_FAILED: TimeoutException: page load")
    plain = {"body": pipelib.html_to_text(menu.encode())}
    def run_fetch_page(cmd, **k):
        # 分類から起動される fetch_page.py をその場で走らせる(素の本文=メニュー、描画=時間切れ)
        out, err = io.StringIO(), io.StringIO()
        saved_argv = sys.argv
        try:
            sys.argv = ["fetch_page.py"] + [str(x) for x in cmd[2:]]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = fetch_page.main()
        finally:
            sys.argv = saved_argv
        return subprocess.CompletedProcess(cmd, rc, out.getvalue(), err.getvalue())
    saved = (cs.run_group, fetch_page.fetch, fetch_page.render_page, cs.page_meta)
    try:
        fetch_page.render_page = render_fail
        cs.run_group = run_fetch_page
        fetch_page.fetch = lambda url: plain["body"]
        body, status = cs.rendered_excerpt("https://idolmaster-official.jp/")
        cs.page_meta = lambda url: ("t", "d", "")
        profile = cs.site_profile("idolmaster-official.jp", "https://idolmaster-official.jp/news/01_1")
        plain["body"] = ""
        body2, status2 = cs.rendered_excerpt("https://idolmaster-official.jp/")
    finally:
        cs.run_group, fetch_page.fetch, fetch_page.render_page, cs.page_meta = saved
    check("メニュー1" in body and "描画できず" in status and "TimeoutException" in status and "終了コード 0" in status,
          f"分類の材料が描画の失敗を捨てた: {body[:60]!r} / {status!r}")
    check(profile.count("取得状態: 描画できず") == 2 and "TimeoutException" in profile,
          f"サイトの材料(代表ページ・トップ)に取得状態が無い: {profile!r}")
    check(body2 == "" and "取得できず" in status2 and "終了コード 1" in status2 and "TimeoutException" in status2,
          f"分類の材料が取得失敗の原因を失った: {body2!r} / {status2!r}")
    # 正常なら取得状態は空(材料に余計な行を足さない)
    ok_run = lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "URL: x\n--- 本文(要約なし) ---\n会社概要 株式会社X\n", "")
    try:
        cs.run_group = ok_run
        body3, status3 = cs.rendered_excerpt("https://example.com/")
    finally:
        cs.run_group = saved[0]
    check(body3 == "会社概要 株式会社X" and status3 == "", f"正常な取得に取得状態が付いた: {body3!r} {status3!r}")

    # 裏取り: 描画できずに写しが見えなかった候補は、描画の失敗を記録に残す(URL の取り違えとだけ書かない)
    class Res:
        status = 200
        headers = type("H", (), {"get_content_charset": staticmethod(lambda: "utf-8")})()
        def read(self, n=-1):
            return menu.encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    saved_c = (collect.fetch_rendered, collect.urllib.request.urlopen)
    cv = [{"url": "https://idolmaster-official.jp/news/01_1", "title": "t", "facts": [], "source_type": "公式", "via": "explore",
           "quotes": ["本日より受付を開始いたしました。詳しくは特設ページをご確認ください。"]},
          {"url": "https://idolmaster-official.jp/news/01_2", "title": "t", "facts": [], "source_type": "公式", "via": "watch"}]
    try:
        collect.fetch_rendered = render_fail
        collect.urllib.request.urlopen = lambda req, timeout=15: Res()
        with contextlib.redirect_stdout(io.StringIO()):
            collect.verify(cv)
    finally:
        collect.fetch_rendered, collect.urllib.request.urlopen = saved_c
    check(cv[0]["verify"] == "failed" and "描画できず" in cv[0].get("verify_note", "") and "TimeoutException" in cv[0]["verify_note"],
          f"裏取りが描画の失敗を捨てて「URL の取り違え」とだけ記録した: {cv[0]}")
    check("TimeoutException" in cv[1].get("render_error", ""), f"裏取りが描画の失敗を候補に残さない: {cv[1]}")
    # 描画の失敗を残した候補は、候補ファイルの書式(candidates.schema)に丸ごと合う。必須の欄を満たした行で、誤りの全件が空であることを
    # 見る(エラー文に欄の名前が入るものだけを拾うと、render_error の型を壊しても通った。監査指摘)
    row = cand_row("c-render", cv[1]["url"], **{k: x for k, x in cv[1].items() if k != "url"})
    errs = pipelib.schema_errors(Path("candidates") / "2026-10-14.json", json.dumps([row], ensure_ascii=False))
    check(row.get("render_error") and errs == [], f"描画の失敗の記録が候補の書式に通らない: {errs}")


def test_watch_deadline_carryover(tmp: Path):
    """定時の収集の締切(組 P)で打ち切った定点観測の新着は、次の回に持ち越して候補にする(2026-10-21 取り込みの確かめ):
    - 一覧を読んだあとに締切を過ぎた観測先の新着は facts 化を始めず、既読にせず未処理の列(_pending)に残す
    - 締切を過ぎてから順番が来た観測先は一覧を読まず、既読も境界(_cut)も変えない
    - 次の回(締切なし)は、未処理の列と読まなかった観測先の新着を全部 facts 化して候補にし、列を空にする"""
    import time as _t
    import collect
    import yaml
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "sources.yml").write_text(yaml.safe_dump([
        {"id": "a", "brand": "general", "type": "html", "url": "https://a.jp/", "base": "https://a.jp", "list_regex": "x", "enabled": True},
        {"id": "b", "brand": "sidem", "type": "html", "url": "https://b.jp/", "base": "https://b.jp", "list_regex": "x", "enabled": True}]),
        encoding="utf-8")
    cut_b = [{"page": 3, "after": ["https://b.jp/old"]}]
    (tmp / "watch-state.json").write_text(json.dumps({"a": ["https://a.jp/seen"], "b": ["https://b.jp/seen"], "_cut": {"b": cut_b}}),
                                          encoding="utf-8")
    listings = {"a": [(f"https://a.jp/n{i}", "") for i in range(3)] + [("https://a.jp/seen", "")],
                "b": [("https://b.jp/n0", ""), ("https://b.jp/seen", "")]}
    listed, calls, notes = [], [], []

    def fake_list(s, known, fetch=None, resume=None):
        listed.append(s["id"])
        if state_deadline["arm"]:
            collect.DEADLINE = _t.time() - 1        # 最初の観測先の一覧を読んだところで締切を過ぎる
        return listings[s["id"]], 1, []
    state_deadline = {"arm": True}

    def extract(prompt, timeout=0):
        urls = re.findall(r"^### \d+\. (\S+)", prompt, re.M)
        calls.append(urls)
        return [{"page": i + 1, "status": "extracted", "items": [{"title": "t", "url": u, "facts": ["f"]}]} for i, u in enumerate(urls)]
    saved = (collect.ROOT, collect.STATE_PATH, collect.list_source, collect.notify, collect.render_prompt, collect.page_html_or_note,
             collect.DEADLINE)
    try:
        collect.ROOT, collect.STATE_PATH = tmp, tmp / "watch-state.json"
        collect.list_source = fake_list
        collect.page_html_or_note = lambda url, csr: ("", "")
        collect.notify = lambda job, msg, ok=True, require=False: notes.append(msg) or True
        collect.render_prompt = lambda name, **kw: kw.get("MATERIAL", "")
        collect.DEADLINE = _t.time() + 3600
        cands, info = collect.run_watch(extract)
        st = info["stats"]["_state"]
        pend = [it["url"] for it in st.get("_pending") or []]
        check(listed == ["a"] and not calls and not cands and pend == [f"https://a.jp/n{i}" for i in range(3)]
              and not any(u in st["a"] for u in pend) and st["b"] == ["https://b.jp/seen"] and st.get("_cut") == {"b": cut_b}
              and not st.get("_unreadable"),
              f"締切で打ち切った新着を持ち越さない(既読にした・読めなかった数に入れた・境界を変えた): 一覧 {listed} facts {calls} "
              f"pending {pend} 状態 {st}")
        (tmp / "watch-state.json").write_text(json.dumps(st), encoding="utf-8")      # main が候補の保存のあとに書くのと同じ
        state_deadline["arm"], collect.DEADLINE = False, None
        listed.clear()
        cands, info = collect.run_watch(extract)
        st = info["stats"]["_state"]
        got = sorted(c["url"] for c in cands)
        want = sorted([f"https://a.jp/n{i}" for i in range(3)] + ["https://b.jp/n0"])
        check(got == want and not st.get("_pending") and all(u in st["a"] + st["b"] for u in want),
              f"次の回に、持ち越した新着・読まなかった観測先の新着を候補にしない: {got} pending {st.get('_pending')}")
    finally:
        (collect.ROOT, collect.STATE_PATH, collect.list_source, collect.notify, collect.render_prompt, collect.page_html_or_note,
         collect.DEADLINE) = saved


def test_watch_pagination_and_batches(tmp: Path):
    """定点観測(編集長 2026-09-30「公式のニュース12件しか見ないとか設計不備過ぎる」):
    一覧は既に見た記事だけのページに行き当たるまで遡る。上限まで読んでも新着が続けば打ち切りを返す。
    初めて見る観測先は、いまの一覧を既読にするだけ(古い記事を新着として流さない)。facts 化は WATCH_BATCH 件ずつ全部を処理する。"""
    import collect
    import yaml
    pages = {1: ["/news/01_9", "/news/01_8", "/news/01_7"], 2: ["/news/01_6", "/news/01_5", "/news/01_4"],
             3: ["/news/01_3", "/news/01_2", "/news/01_1"], 4: []}
    fetched = []
    def fake_fetch(url):
        n = 1 if url.endswith("/news") else int(url.rsplit("=", 1)[1])
        fetched.append(n)
        return "<html>" + "".join(f'<a href="{p}">x</a>' for p in pages.get(n, [])) + "</html>"   # 項目の無いページ=一覧の終わり
    s = {"id": "p", "type": "portal", "url": "https://o.jp/news", "page_url": "https://o.jp/news?page_index={n}", "max_pages": 10,
         "base": "https://o.jp", "list_regex": '<a[^>]+href="(/news/[0-9_]+)"[^>]*>(?:.*?)</a>'}
    known = {f"https://o.jp/news/01_{i}" for i in (1, 2, 3, 4, 5, 6)}
    found, n, cut = collect.list_source(s, known, fetch=fake_fetch)
    check(fetched == [1, 2] and n == 2 and not cut and len(found) == 6, f"既読だけのページで止まらない: {fetched} {n} {cut}")
    fetched.clear()
    found, n, cut = collect.list_source(s, set(), fetch=fake_fetch)
    check(fetched == [1, 2, 3, 4] and not cut and len(found) == 9, f"空のページ(一覧の終わり)で止まらない: {fetched}")
    fetched.clear()
    found, n, cut = collect.list_source({**s, "max_pages": 2}, set(), fetch=fake_fetch)
    check(n == 2 and cut, "上限まで読んでも新着が続いたのに打ち切りを返さない")
    found, n, cut = collect.list_source({k: v for k, v in s.items() if k != "page_url"}, set(), fetch=fake_fetch)
    check(n == 1 and not cut and len(found) == 3, "page_url の無い観測先を遡った")
    # 範囲外のページが1ページ目と同じ中身を返すサイト(Pylon Port)では、既に集めた項目だけのページとして止まる(監査指摘 r104)
    fetched.clear()
    found, n, cut = collect.list_source(s, set(), fetch=lambda url: fake_fetch(url if not url.endswith("=4") else "https://o.jp/news"))
    check(n == 4 and not cut and len(found) == 9, f"1ページ目と同じ中身の範囲外ページで止まらない: {n} {cut} {len(found)}")
    # 取得の失敗(空の応答)を一覧の終わりと取り違えない(監査指摘 r102)
    try:
        collect.list_source(s, set(), fetch=lambda url: fake_fetch(url) if url.endswith("/news") else "")
        check(False, "2ページ目の取得失敗を一覧の終わりとして扱った")
    except RuntimeError:
        pass

    # run_watch: 初めて見る観測先は既読にするだけ。新着は WATCH_BATCH 件ずつ全部を facts 化。上限を超えた分だけ繰り越して異常を上げる
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "sources.yml").write_text(yaml.safe_dump([
        {"id": "old", "brand": "general", "type": "html", "url": "https://a.jp/", "base": "https://a.jp", "list_regex": "x", "enabled": True},
        {"id": "fresh", "brand": "sidem", "type": "html", "url": "https://b.jp/", "base": "https://b.jp", "list_regex": "x", "enabled": True}]),
        encoding="utf-8")
    old_known = [f"https://a.jp/k{i}" for i in range(3)]
    (tmp / "watch-state.json").write_text(json.dumps({"old": old_known}), encoding="utf-8")
    listings = {"old": [(f"https://a.jp/n{i}", "") for i in range(30)] + [(u, "") for u in old_known],
                "fresh": [(f"https://b.jp/f{i}", "") for i in range(20)]}
    calls, notes = [], []
    # facts 化の出力は新着ページごとの結果(全ページ「読んだが事実なし」)
    none_all = lambda prompt: [{"page": i + 1, "status": "none", "items": []} for i in range(prompt.count("### "))]
    saved = (collect.ROOT, collect.STATE_PATH, collect.list_source, collect.notify, collect.WATCH_BATCH, collect.render_prompt, collect.page_html_or_note)
    try:
        collect.ROOT, collect.STATE_PATH = tmp, tmp / "watch-state.json"
        collect.list_source = lambda s, known, fetch=None, resume=None: (listings[s["id"]], 1, [])
        collect.page_html_or_note = lambda url, csr: ("", "")   # 新着ページの本文は取らない(ネットに出ない)
        collect.notify = lambda job, msg, ok=True, require=False: notes.append((msg, ok)) or True
        collect.render_prompt = lambda name, **kw: kw.get("MATERIAL", "")
        collect.WATCH_BATCH = 12
        cands, info = collect.run_watch(lambda prompt, timeout=0: calls.append(prompt.count("### ")) or none_all(prompt))
        check(sorted(calls) == [6, 12, 12], f"facts 化のバッチ: {calls}(30件は 12・12・6 の3回で全部)")
        check(info["new"] == 30 and info["deferred"] == 0, f"新着・繰り越し: {info['new']} {info['deferred']}")
        check(info["stats"]["fresh"].get("baseline") and info["stats"]["fresh"]["new"] == 0, f"初めて見る観測先を新着にした: {info['stats']['fresh']}")
        st = info["stats"]["_state"]
        check(len(st["fresh"]) == 20 and all(f"https://a.jp/n{i}" in st["old"] for i in range(30)), "既読の記録が足りない")
        # 件数に上限は無い: 110件でも全部を処理して、何も繰り越さない(編集長 2026-09-30「全部処理してねーじゃん」)
        calls.clear()
        listings = {"old": [(f"https://a.jp/z{i}", "") for i in range(90)] + [(u, "") for u in old_known],
                    "fresh": [(f"https://b.jp/z{i}", "") for i in range(20)]}
        (tmp / "watch-state.json").write_text(json.dumps({"old": old_known, "fresh": []}), encoding="utf-8")
        cands, info = collect.run_watch(lambda prompt, timeout=0: calls.append(prompt.count("### ")) or none_all(prompt))
        check(sum(calls) == 110 and len(calls) == 10 and info["deferred"] == 0 and not info["stats"]["_state"].get("_pending"),
              f"新着を全部処理していない: {sum(calls)}件 / {len(calls)}回 / 繰り越し {info['deferred']}")
        # 出力が読めなかったバッチだけが未処理の列に残り、次の実行では一覧に既に見た記事しか出なくても、列から先に処理する(監査指摘 r102・r103)
        st = info["stats"]["_state"]
        (tmp / "watch-state.json").write_text(json.dumps(st), encoding="utf-8")
        listings = {"old": [("https://a.jp/P", "")] + [(u, "") for u in old_known], "fresh": []}
        cands, info = collect.run_watch(lambda prompt, timeout=0: None)          # 読めない出力
        st = info["stats"]["_state"]
        check([it["url"] for it in st.get("_pending") or []] == ["https://a.jp/P"], f"読めなかった新着が未処理の列に残らない: {st.get('_pending')}")
        (tmp / "watch-state.json").write_text(json.dumps(st), encoding="utf-8")
        listings = {"old": [(f"https://a.jp/m{i}", "") for i in range(30)] + [("https://a.jp/P", "")], "fresh": []}
        firsts = []
        cands, info = collect.run_watch(lambda prompt, timeout=0: firsts.append(prompt.split("\n", 1)[0]) or none_all(prompt))
        st = info["stats"]["_state"]
        check(any(f.startswith("### 1. https://a.jp/P") for f in firsts) and not st.get("_pending") and "https://a.jp/P" in st["old"],
              f"未処理の列の新着を先に処理して既読にしない: {firsts[:3]} / 残り {st.get('_pending')}")
        # 当番の拾い直し(oncall_rerun): 1度読めず _unreadable=1 で繰り越した新着を、同じバッチの再実行でまた
        # 読めなかったとき、通常は2度目で諦めて既読にする(原因未確定のまま新着を失う。監査指摘)。
        # oncall_rerun ではそれを諦めず、全件を未処理の列に残し、回数も進めない(rerun-second-unreadable-drops-pending)
        base = {"old": ["https://a.jp/seen"],
                "_pending": [{"source_id": "old", "brand": "general", "url": "https://a.jp/1", "title": "t", "source_type": "公式", "csr": False}],
                "_unreadable": {"https://a.jp/1": 1}}
        listings = {"old": [("https://a.jp/seen", "")], "fresh": []}
        (tmp / "watch-state.json").write_text(json.dumps(base), encoding="utf-8")
        cands, info = collect.run_watch(lambda prompt, timeout=0: None)                 # 通常(2度目)= 諦めて既読
        st = info["stats"]["_state"]
        check(info["deferred"] == 0 and not st.get("_pending") and "https://a.jp/1" in st["old"],
              f"通常の2度目で既読にならない(=この経路で新着を失う): deferred={info['deferred']} pending={st.get('_pending')} old={st['old']}")
        (tmp / "watch-state.json").write_text(json.dumps(base), encoding="utf-8")
        cands, info = collect.run_watch(lambda prompt, timeout=0: None, oncall_rerun=True)  # 当番の拾い直し = 諦めない
        st = info["stats"]["_state"]
        check(info["deferred"] == 1 and [it["url"] for it in st.get("_pending") or []] == ["https://a.jp/1"]
              and "https://a.jp/1" not in st.get("old", []) and st.get("_unreadable", {}).get("https://a.jp/1") == 1,
              f"拾い直しで諦めず繰り越さない(=失う): deferred={info['deferred']} pending={st.get('_pending')} unreadable={st.get('_unreadable')}")
        # 通常の収集が2度目で諦めた新着も、当番が直したあとの取り直しで拾う(諦めたら既読で、一覧にも未処理の列にも
        # 二度と出ない。捨てると当番が直しても選定リストに入らない。監査指摘)
        (tmp / "watch-state.json").write_text(json.dumps(base), encoding="utf-8")
        cands, info = collect.run_watch(lambda prompt, timeout=0: None)                 # 通常の2度目 = 諦める
        st = info["stats"]["_state"]
        check([g["url"] for g in st.get("_given_up") or []] == ["https://a.jp/1"] and "https://a.jp/1" in st["old"],
              f"諦めた新着を取り直しのために残さない: {st.get('_given_up')}")
        (tmp / "watch-state.json").write_text(json.dumps(st), encoding="utf-8")
        seen_calls = []
        cands, info = collect.run_watch(lambda prompt, timeout=0: seen_calls.append(prompt) or [])   # 次の通常の収集
        st = info["stats"]["_state"]
        check(not seen_calls and [g["url"] for g in st.get("_given_up") or []] == ["https://a.jp/1"],
              f"通常の収集が諦めた新着を読み直す(上限の外を後回しにする)か、諦めた列を消した: {len(seen_calls)} {st.get('_given_up')}")
        given_up_state = st
        hit = {"title": "t", "url": "https://src.jp/1", "facts": ["f"]}
        (tmp / "watch-state.json").write_text(json.dumps(given_up_state), encoding="utf-8")
        cands, info = collect.run_watch(lambda prompt, timeout=0: [{"page": 1, "status": "extracted", "items": [hit]}]
                                        if "### 1. https://a.jp/1" in prompt else None, oncall_rerun=True)   # 当番が直した後の取り直し
        st = info["stats"]["_state"]
        check(len(cands) == 1 and info["deferred"] == 0 and not st.get("_given_up") and not st.get("_pending"),
              f"当番の取り直しで諦めた新着を拾えない: cands={cands} given_up={st.get('_given_up')} pending={st.get('_pending')}")
        (tmp / "watch-state.json").write_text(json.dumps(given_up_state), encoding="utf-8")
        cands, info = collect.run_watch(lambda prompt, timeout=0: None, oncall_rerun=True)   # 直しが効いていない取り直し
        st = info["stats"]["_state"]
        check(info["deferred"] == 1 and [it["url"] for it in st.get("_pending") or []] == ["https://a.jp/1"] and not st.get("_given_up"),
              f"取り直しでまた読めない諦めた新着を、未処理の列に移して申告しない: deferred={info['deferred']} {st.get('_pending')} {st.get('_given_up')}")
        # ページごとの結果(監査指摘 watch-read-status-contract): 本文を読めなかったページ・応答に無いページ・形の崩れた
        # ページは既読にせず未処理の列に残す。旧い契約の [](候補なし)で全件を既読にしない(2026-10-04: ?p=32544 を失った)
        listings = {"old": [(f"https://a.jp/q{i}", "") for i in range(5)] + [("https://a.jp/seen", "")], "fresh": []}
        item = {"title": "t", "url": "https://src.jp/1", "facts": ["f"]}
        replies = {"[]": lambda prompt: [],
                   "pages": lambda prompt: [{"page": 1, "status": "extracted", "items": [item]},
                                            {"page": 2, "status": "none", "items": []},
                                            {"page": 3, "status": "unreadable", "items": []},
                                            {"page": 5, "status": "none", "items": [item]}]}   # 4 は応答に無い・5 は status と items が食い違う
        for name, reply in replies.items():
            (tmp / "watch-state.json").write_text(json.dumps({"old": ["https://a.jp/seen"]}), encoding="utf-8")
            cands, info = collect.run_watch(lambda prompt, timeout=0: reply(prompt))
            st = info["stats"]["_state"]
            pend = [it["url"] for it in st.get("_pending") or []]
            want = [f"https://a.jp/q{i}" for i in range(5)] if name == "[]" else ["https://a.jp/q2", "https://a.jp/q3", "https://a.jp/q4"]
            check(pend == want and not any(u in st["old"] for u in want) and all(st["_unreadable"].get(u) == 1 for u in want),
                  f"{name}: 読めなかった・欠けたページを既読にした(=新着を失う): pending={pend} old={st['old']}")
            check(len(cands) == (0 if name == "[]" else 1) and (name == "[]" or {"https://a.jp/q0", "https://a.jp/q1"} <= set(st["old"])),
                  f"{name}: 抽出済み・対象外のページの扱い: cands={cands} old={st['old']}")
    finally:
        (collect.ROOT, collect.STATE_PATH, collect.list_source, collect.notify, collect.WATCH_BATCH, collect.render_prompt, collect.page_html_or_note) = saved


def test_watch_resume_after_cut(tmp: Path):
    """定点観測の一覧を上限で打ち切ったら、読んだ位置(未完了の境界)を状態に残し、次の回(当番の取り直し・通常の収集)は
    手前の既読ページで巡回を終えずに、境界の先の新着を回収する(2026-10-14 監査指摘: 新着3ページ・上限2で打ち切った後、
    上限10で取り直しても1ページ目だけ読んで新着0件だった)。実際の list_source・run_watch に一覧のページだけを返す。"""
    import collect
    import yaml
    tmp.mkdir(parents=True, exist_ok=True)
    listing = {}
    reads = []

    def fetch(url):
        n = int(url.rsplit("=", 1)[1]) if "=" in url else 1
        reads.append(n)
        return "<ul>" + "".join(f'<a href="https://example.org/{u}">{u}</a>' for u in listing.get(n, [])) + "</ul>"
    src = {"id": "official", "brand": "shiny", "type": "html", "enabled": True, "url": "https://example.org/news",
           "page_url": "https://example.org/news?p={n}", "base": "https://example.org", "list_regex": r'<a href="([^"]+)">([^<]+)</a>'}
    none_all = lambda prompt, timeout=0: [{"page": i + 1, "status": "none", "items": []} for i in range(prompt.count("### "))]

    def run(max_pages, state, rerun=False):
        (tmp / "sources.yml").write_text(yaml.safe_dump([{**src, "max_pages": max_pages}]), encoding="utf-8")
        (tmp / "watch-state.json").write_text(json.dumps(state), encoding="utf-8")
        reads.clear()
        _, info = collect.run_watch(none_all, oncall_rerun=rerun)
        return list(reads), info, info["stats"].pop("_state")
    url = lambda u: f"https://example.org/{u}"
    saved = (collect.ROOT, collect.STATE_PATH, collect.http_get, collect.page_html_or_note, collect.notify, collect.render_prompt)
    notes = []
    try:
        collect.ROOT, collect.STATE_PATH = tmp, tmp / "watch-state.json"
        collect.http_get = fetch
        collect.page_html_or_note = lambda u, csr: ("", "")
        collect.notify = lambda job, msg, ok=True, require=False: notes.append(msg) or True
        collect.render_prompt = lambda name, **kw: kw.get("MATERIAL", "")
        listing.update({1: ["n1", "n2", "n3"], 2: ["n4", "n5", "n6"], 3: ["n7", "n8", "n9"], 4: ["o1", "o2", "o3"], 5: ["o4", "o5", "o6"]})
        base = {"official": [url(f"o{i}") for i in range(1, 7)]}
        first, info, st = run(2, base)
        check(first == [1, 2] and info["new"] == 6 and info["stats"]["official"].get("unfinished") and st.get("_cut", {}).get("official")
              and any("official" in m for m in notes), f"上限で打ち切ったことと読んだ位置を残さない: {first} {info['stats']} {st.get('_cut')}")
        # 当番の取り直し(上限を上げた): 先頭の既読ページで終えず、境界の先(3ページ目)の新着を回収して、境界を消す
        again, info2, st2 = run(10, st, rerun=True)
        check(again == [1, 2, 3, 4] and info2["new"] == 3 and all(url(f"n{i}") in st2["official"] for i in (7, 8, 9))
              and not st2.get("_cut") and not info2["stats"]["official"].get("unfinished"),
              f"取り直しが手前の既読ページで巡回を終え、奥の新着を回収しない: 読んだページ {again} 新着 {info2['new']} 境界 {st2.get('_cut')}")
        # 通常の収集(上限は同じ 2)も、次の回は境界のページへ飛んで先を読む(手前の既読ページで上限を使い切らない)
        nxt, info3, st3 = run(2, st)
        check(nxt == [1, 2, 3, 4] and info3["new"] == 3 and not st3.get("_cut"),
              f"上限が同じ次の回が、境界の先を読まない: 読んだページ {nxt} 新着 {info3['new']} 境界 {st3.get('_cut')}")
        # 境界が残ったまま、先頭にも上限を超える新着が来た: 両方の境界を残し、次の回で手前から順に両方の先を読む
        listing.clear()
        listing.update({1: ["m1", "m2", "m3"], 2: ["m4", "m5", "m6"], 3: ["m7", "n1", "n2"], 4: ["n3", "n4", "n5"],
                        5: ["n6", "n7", "n8"], 6: ["n9", "o1", "o2"], 7: ["o3", "o4", "o5"]})
        front, info4, st4 = run(2, st)
        check(front == [1, 2] and len(st4.get("_cut", {}).get("official", [])) == 2,
              f"先頭の打ち切りで、前の境界を失った: {front} {st4.get('_cut')}")
        both, info5, st5 = run(2, st4)
        got = {u for u in st5["official"]}
        check(all(url(u) in got for u in ("m7", "n7", "n8", "n9")) and not st5.get("_cut"),
              f"2つの境界の先を読まない: 読んだページ {both} 境界 {st5.get('_cut')} 既読 {sorted(got)[:12]}")
        # 読んでいる間に一覧がずれて、前に読んだ項目の一部だけのページが挟まった: 一覧の終わりと取り違えず、その先も読む
        listing.clear()
        listing.update({1: ["n1", "n2", "n3"], 2: ["n4", "n5", "n6"], 3: ["n5", "n6"], 4: ["n7", "n8", "n9"], 5: ["o1", "o2", "o3"]})
        ov, info6, st6 = run(2, st)
        check(info6["new"] == 3 and all(url(f"n{i}") in st6["official"] for i in (7, 8, 9)) and not st6.get("_cut"),
              f"重なっただけのページを一覧の終わりにして、境界の先を読まない: 読んだページ {ov} 新着 {info6['new']} 境界 {st6.get('_cut')}")
        # 境界より手前の記事が消えて、境界が記録したページより手前へずれた: 記録したページへ飛ばずに順に読み、境界の先を読む
        listing.clear()
        listing.update({i: [f"d{i}"] for i in range(1, 7)})
        listing[7] = ["o1"]
        _, _, sd = run(4, base)                       # d1〜d4 を読んで打ち切る(境界は4ページ目の d4)
        listing.clear()
        listing.update({1: ["d4"], 2: ["d5"], 3: ["d6"], 4: ["o1", "o2"]})      # d1〜d3 が消えた
        moved, info7, sd2 = run(4, sd, rerun=True)
        check(moved == [1, 2, 3, 4] and info7["new"] == 2 and url("d5") in sd2["official"] and not sd2.get("_cut"),
              f"手前へずれた境界の先を飛ばして失った: 読んだページ {moved} 新着 {info7['new']} 境界 {sd2.get('_cut')}")
    finally:
        (collect.ROOT, collect.STATE_PATH, collect.http_get, collect.page_html_or_note, collect.notify, collect.render_prompt) = saved


def test_watch_body_cp932(tmp: Path):
    """定点観測の facts 化に渡す本文: Shift_JIS と宣言して CP932 の拡張文字(①)を含むページを、置換文字だらけにせず読む
    (2026-10-14 監査指摘: 本文の取得だけが cp932 を試さず、置換文字 1,110 個の本文を「取得済み」として渡した)。"""
    import collect
    html = '<html><meta charset="Shift_JIS"><body><h2>①特典の受付を開始</h2><p>' + "受付の告知本文です。" * 100 + "</p></body></html>"
    raw = html.encode("cp932")

    class Res:
        headers = type("H", (), {"get_content_charset": lambda self: None})()
        def read(self, *a): return raw
        def __enter__(self): return self
        def __exit__(self, *a): return False
    prompts = []
    saved = (collect.urllib.request.urlopen, collect.fetch_rendered, collect.render_prompt, collect.notify)
    try:
        collect.urllib.request.urlopen = lambda req, timeout=20: Res()
        collect.fetch_rendered = lambda url, timeout=30: (_ for _ in ()).throw(AssertionError("素の HTML で読めるページを描画した"))
        collect.render_prompt = lambda name, **kw: kw.get("MATERIAL", "")
        collect.notify = lambda *a, **k: True
        got, why = collect.page_html_or_note("https://lantis.jp/sidem/extra/index.html", False)
        collect.facts_batch([{"url": "https://lantis.jp/sidem/extra/index.html", "title": "", "brand": "sidem", "csr": False}],
                            lambda prompt, timeout=0: prompts.append(prompt) or None, {})
    finally:
        collect.urllib.request.urlopen, collect.fetch_rendered, collect.render_prompt, collect.notify = saved
    check("①特典の受付を開始" in got and "�" not in got and why == "", f"CP932 の本文を読めない: {got[:80]!r} {why!r}")
    check(prompts and "①特典の受付を開始" in prompts[0] and "�" not in prompts[0], "facts 化に置換文字だらけの本文を渡した")


def test_watch_static_page_body(tmp: Path):
    """定点観測の facts 化: 描画の要らない観測先の新着も本文を取って渡す。`#id` で1ページの中の1件を指す URL は、その件の部分だけを渡す
    (2026-10-09: ランティスの SideM 特典告知2件。本文を渡さず WebFetch に任せたら要約しか返らず、写しが取れずに読めなかった扱いになった)"""
    import collect
    page = ('<html><head><meta charset="Shift_JIS"></head><body><nav>ナビ</nav>'
            '<div class="event_box"><div class="date" id="20261009_2">2026.10.9 update</div><h2>Beit の特典が決定</h2><p>品番 LACM-24794</p></div>'
            '<div class="event_box"><div class="date" id="20261009_1">2026.10.9 update</div><h2>Jupiter の特典デザインが決定</h2><p>品番 LACM-24793</p></div>'
            '<div class="event_box"><div class="date" id="20260925">2026.9.25 update</div><h2>古い告知</h2><p>' + "過去の告知の本文。" * 60 + '</p></div></body></html>')
    class Res:
        headers = type("H", (), {"get_content_charset": lambda self: None})()
        def __init__(self, body): self.body = body
        def read(self, *a): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False
    prompts = []
    saved = (collect.urllib.request.urlopen, collect.render_prompt, collect.notify, collect.fetch_rendered)
    try:
        collect.urllib.request.urlopen = lambda req, timeout=20: Res(page.encode("cp932"))
        collect.render_prompt = lambda name, **kw: kw.get("MATERIAL", "")
        collect.notify = lambda *a, **k: True
        collect.fetch_rendered = lambda url, timeout=30: (_ for _ in ()).throw(AssertionError("素の HTML で読めるページを描画した"))
        collect.facts_batch([{"url": "https://lantis.jp/sidem/extra/index.html#20261009_1", "title": "", "brand": "sidem", "csr": False},
                             {"url": "https://lantis.jp/sidem/extra/index.html#nowhere", "title": "", "brand": "sidem", "csr": False}],
                            lambda prompt, timeout=0: prompts.append(prompt) or None, {})
    finally:
        collect.urllib.request.urlopen, collect.render_prompt, collect.notify, collect.fetch_rendered = saved
    m = prompts[0] if prompts else ""
    first, second = (m.split("### 2.") + [""])[:2]
    check("Jupiter の特典デザインが決定" in first and "LACM-24793" in first and "WebFetch" not in first,
          f"素の HTML のページの本文を渡さず WebFetch に任せた(要約しか返らず写しが取れない): {first!r}")
    check("Beit" not in first and "古い告知" not in first and "ナビ" not in first, f"#id の件以外を混ぜた: {first!r}")
    check("古い告知" in second and "WebFetch" not in second, f"id がページに無いときページ全体を渡さない: {second!r}")


def test_collect_oncall_rerun_exit(tmp: Path):
    """当番の拾い直し(--oncall-rerun)で読めないバッチが残ったら、collect は成功で終えず非0を返す
    (繰り越しは既読にせず保持済み。直しが効いていないことを当番へ申告する。監査指摘)。
    候補は壁時計でなく --date で渡した取り込み先の号へ書く(発行後 watch 等で号がずれても別号を汚さない)。"""
    import collect
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "candidates").mkdir(exist_ok=True)
    saved = (collect.ROOT, collect.job_lock, collect.run_watch, collect.run_explores,
             collect.append_metric, collect.notify, sys.argv)
    try:
        collect.ROOT = tmp
        collect.job_lock = lambda *a, **k: None
        collect.run_explores = lambda *a, **k: ([], {})
        collect.append_metric = lambda *a, **k: None
        collect.notify = lambda *a, **k: True
        argv = ["collect", "--no-git", "--oncall-rerun", "--skip-explore", "--skip-grok", "--date", "2026-09-13"]
        collect.run_watch = lambda call, oncall_rerun=False: ([], {"deferred": 2, "new": 2, "facted": 0})
        sys.argv = list(argv)
        check(collect.main() == 1, "拾い直しで読めないバッチが残ったのに成功(0)で終えた")
        check((tmp / "candidates" / "2026-09-13.json").exists(), "候補を --date の号(取り込み先)へ書いていない")
        collect.run_watch = lambda call, oncall_rerun=False: ([], {"deferred": 0, "new": 2, "facted": 2})
        sys.argv = list(argv)
        check(collect.main() == 0, "拾い直しで全部読めたのに非0で終えた")
        # 観測先の一覧がまた取れない(新着を見つけられないので deferred は0)→ 取り直しは成功にしない(監査指摘)
        collect.run_watch = lambda call, oncall_rerun=False: ([], {"deferred": 0, "new": 0,
                                                                  "stats": {"columbia-imas": {"error": "空の応答"}}})
        sys.argv = list(argv)
        check(collect.main() == 1, "取り直しで観測先の一覧がまた取れないのに成功(0)で終えた")
        # 一覧の読んでいない区間(未完了の境界)が残った → 取り直しは成功にしない(残った区間の新着は素材に入っていない)
        collect.run_watch = lambda call, oncall_rerun=False: ([], {"deferred": 0, "new": 3,
                                                                  "stats": {"imas-portal": {"found": 30, "unfinished": 1}}})
        sys.argv = list(argv)
        check(collect.main() == 1, "取り直しで一覧を読み終えられないのに成功(0)で終えた")
    finally:
        (collect.ROOT, collect.job_lock, collect.run_watch, collect.run_explores,
         collect.append_metric, collect.notify, sys.argv) = saved


def _collect_repo(tmp: Path) -> Path:
    """収集の後始末を実際の git で検める下準備: 号のブランチ(edition/2026-10-14)を checkout した作業ツリーと、push 先の bare
    リモート。.gitignore と判定表は本物を写す(後始末が残す一時ファイル・正規化の種別判定を本番と同じにする)。"""
    root, remote = tmp / "w", tmp / "r.git"
    G = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    root.mkdir(parents=True)
    G("init", "-q", "-b", "main")
    G("config", "user.email", "t@example.com")
    G("config", "user.name", "t")
    repo = Path(__file__).resolve().parent.parent
    for d in ("candidates", "stock", "metrics"):
        (root / d).mkdir()
    (root / ".gitignore").write_text((repo / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "source_types.yml").write_text((repo / "source_types.yml").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "candidates" / "2026-10-14.json").write_text("[]\n", encoding="utf-8")
    (root / "stock" / "watch-state.json").write_text("{}\n", encoding="utf-8")
    G("add", "-A")
    G("commit", "-qm", "base")
    G("remote", "add", "origin", str(remote))
    G("push", "-q", "origin", "main")
    G("checkout", "-qb", "edition/2026-10-14")
    G("push", "-qu", "origin", "edition/2026-10-14")
    return root


def _remote_file(root: Path, path: str) -> str:
    """リモートの号のブランチにある path の中身(無ければ空)。"""
    r = subprocess.run(["git", "show", f"origin/edition/2026-10-14:{path}"], cwd=root, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _json_or_none(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def test_collect_settles_on_exception(tmp: Path):
    """収集が素材と既読状態を保存したあと(記録・出典の判定・commit の前)に例外で落ちても、保存した成果を検めて確定し、作業ツリーを
    clean にして終える。次の工程(組版)の checkout が汚れで止まらない(2026-10-14 監査指摘: 例外のあと candidates/ と stock/ が
    未コミットのまま残り、組版の checkout_edition_branch が拒否した)。実際の git・main で検める。"""
    import collect
    root = _collect_repo(tmp)
    row = {"title": "告知", "url": "https://shinycolors.lantis.jp/audit-exc", "brand": "shiny", "facts": ["受付を開始"],
           "_via": "watch", "dedup_key": "audit-exc"}
    notes = []
    names = ("ROOT", "STATE_PATH", "job_lock", "run_watch", "run_explores", "verify", "needs_classify", "append_metric", "notify")
    saved = {n: getattr(collect, n) for n in names}
    saved_p = (pipelib.ROOT, pipelib.notify, sys.argv)

    def broken_metric(*a, **k):
        raise OSError(28, "No space left on device")
    raised = None
    try:
        collect.ROOT = pipelib.ROOT = root
        collect.STATE_PATH = root / "stock" / "watch-state.json"
        collect.job_lock = lambda *a, **k: None
        collect.run_watch = lambda call, oncall_rerun=False: ([dict(row)], {"stats": {"_state": {"official": [row["url"]]}}, "new": 1, "deferred": 0})
        collect.run_explores = lambda *a, **k: ([], {})
        collect.verify = lambda cands: {"confirmed": 0, "unconfirmed": len(cands), "failed": 0}
        collect.needs_classify = lambda cands: False
        collect.append_metric = broken_metric
        collect.notify = pipelib.notify = lambda job, msg, ok=True, require=False: notes.append(msg) or True
        sys.argv = ["collect", "--date", "2026-10-14", "--skip-explore", "--skip-grok"]
        try:
            collect.main()
        except OSError as e:
            raised = e
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout.strip()
        next_ok = pipelib.checkout_edition_branch("2026-10-14", "compose")
    finally:
        for n, v in saved.items():
            setattr(collect, n, v)
        pipelib.ROOT, pipelib.notify, sys.argv = saved_p
    cands = _json_or_none(_remote_file(root, "candidates/2026-10-14.json")) or []
    state = _json_or_none(_remote_file(root, "stock/watch-state.json")) or {}
    check(raised is not None, "収集の例外を飲み込んだ(当番のなぜなぜに渡らない)")
    check(not dirty and next_ok, f"例外のあと作業ツリーが汚れたまま・次の工程の checkout が止まる: {dirty!r} {next_ok}")
    check([c.get("url") for c in cands] == [row["url"]] and state.get("official") == [row["url"]],
          f"保存した素材と既読状態をリモートに確定しない: {cands} {state}")


def test_collect_settles_on_sigterm(tmp: Path):
    """収集が素材と既読状態を保存したあとに SIGTERM(systemd の打ち切り)を受けても、保存した成果を検めて確定し、作業ツリーを
    clean にして終える(2026-10-14 監査指摘: 保存直後の SIGTERM で候補と既読状態が変更のまま残り、次の工程が拒否した)。
    実際の別プロセスに SIGTERM を送り、実際の git で検める。"""
    import signal as _signal
    import time as _time

    def run(case: str, pause_in: str, extra: list[str]):
        """収集の別プロセスを、pause_in(append_metric=保存したあと / notify=確定したあとの通知)で止めて SIGTERM を送る。"""
        root = _collect_repo(tmp / case)
        ready = tmp / case / "ready"
        driver = tmp / case / "driver.py"
        driver.write_text(f"""import sys, time
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
import collect, pipelib
def no_net(*a, **k):
    raise AssertionError("通信しない")
pipelib.urllib.request.urlopen = no_net
pipelib.ENV.pop("DISCORD_WEBHOOK_URL", None)
root = Path({str(root)!r})
collect.ROOT = pipelib.ROOT = root
collect.STATE_PATH = root / "stock" / "watch-state.json"
def pause(*a, **k):
    Path({str(ready)!r}).write_text("ここで止めた")
    time.sleep(60)
def note(job, msg, ok=True, require=False):
    print("NOTIFY", msg, flush=True)
    if {pause_in!r} == "notify" and "一覧が取れない" in msg:
        pause()
    return True
collect.notify = pipelib.notify = note
collect.job_lock = lambda *a, **k: None
row = {{"title": "告知", "url": "https://shinycolors.lantis.jp/audit-term", "brand": "shiny", "facts": ["受付を開始"],
       "_via": "watch", "dedup_key": "audit-term"}}
collect.run_watch = lambda call, oncall_rerun=False: ([dict(row)], {{"stats": {{"_state": {{"official": [row["url"]]}},
                                                                             "other": {{"error": "空の応答"}}}}, "new": 1, "deferred": 0}})
collect.run_explores = lambda *a, **k: ([], {{}})
collect.verify = lambda cands: {{"confirmed": 0, "unconfirmed": len(cands), "failed": 0}}
collect.needs_classify = lambda cands: False
if {pause_in!r} == "append_metric":
    collect.append_metric = pause
sys.argv = ["collect", "--date", "2026-10-14", "--skip-explore", "--skip-grok"] + {extra!r}
try:
    code = collect.main()
except collect.Terminated:
    code = 143
sys.exit(code)
""", encoding="utf-8")
        p = subprocess.Popen([sys.executable, str(driver)], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        end = _time.time() + 60
        while not ready.exists() and p.poll() is None and _time.time() < end:
            _time.sleep(0.05)
        if ready.exists():
            p.send_signal(_signal.SIGTERM)
        try:
            out, _ = p.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout.strip()
        saved = (pipelib.ROOT, pipelib.notify)
        try:
            pipelib.ROOT, pipelib.notify = root, (lambda *a, **k: True)
            next_ok = pipelib.checkout_edition_branch("2026-10-14", "compose")
        finally:
            pipelib.ROOT, pipelib.notify = saved
        cands = _json_or_none(_remote_file(root, "candidates/2026-10-14.json")) or []
        state = _json_or_none(_remote_file(root, "stock/watch-state.json")) or {}
        check(ready.exists() and p.returncode == 143, f"{case}: SIGTERM で後始末をして終えない(終了 {p.returncode}): {out[-600:]}")
        check(not dirty and next_ok, f"{case}: SIGTERM のあと作業ツリーが汚れたまま・次の工程の checkout が止まる: {dirty!r} {next_ok}")
        check([c.get("url") for c in cands] == ["https://shinycolors.lantis.jp/audit-term"]
              and state.get("official") == ["https://shinycolors.lantis.jp/audit-term"],
              f"{case}: 保存した素材と既読状態をリモートに確定しない: {cands} {state}")
        return out
    # 素材と既読状態を保存したあと(記録・判定・commit の前)
    out = run("saved", "append_metric", [])
    check("SIGTERM" in out, f"打ち切られたことを申告しない: {out[-400:]}")
    # 確定(commit・push)のあとの通知の最中: 飲み込まずに終える(監査指摘: 確定のあとも SIGTERM を遅らせ続けて 0 で終えた)
    run("committed", "notify", ["--oncall-rerun"])


def test_collect_state_write_is_atomic(tmp: Path):
    """既読状態の保存が途中で止まっても(ファイルの大きさの上限で書けない)、前の既読状態は壊れずに残る(2026-10-14 監査指摘:
    直接の上書きが途中で止まると切れた JSON が残り、当番の後始末がそれを確定した)。実際の main を別プロセスで走らせる。"""
    root = tmp / "w"
    (root / "candidates").mkdir(parents=True)
    (root / "stock").mkdir()
    (root / "source_types.yml").write_text((Path(__file__).resolve().parent.parent / "source_types.yml").read_text(encoding="utf-8"),
                                           encoding="utf-8")
    before = json.dumps({"official": ["https://example.org/old"]}) + "\n"
    (root / "stock" / "watch-state.json").write_text(before, encoding="utf-8")
    driver = tmp / "driver.py"
    driver.write_text(f"""import resource, sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
import collect, pipelib
pipelib.urllib.request.urlopen = None
root = Path({str(root)!r})
collect.ROOT = pipelib.ROOT = root
collect.STATE_PATH = root / "stock" / "watch-state.json"
collect.job_lock = lambda *a, **k: None
big = {{"official": [f"https://example.org/n{{i}}" for i in range(3000)]}}
row = {{"title": "告知", "url": "https://example.org/n1", "brand": "shiny", "facts": ["受付を開始"], "_via": "watch", "dedup_key": "a"}}
collect.run_watch = lambda call, oncall_rerun=False: ([dict(row)], {{"stats": {{"_state": big}}, "new": 1, "deferred": 0}})
collect.run_explores = lambda *a, **k: ([], {{}})
collect.verify = lambda cands: {{"confirmed": 0, "unconfirmed": len(cands), "failed": 0}}
collect.append_metric = lambda *a, **k: None
resource.setrlimit(resource.RLIMIT_FSIZE, (8192, 8192))     # 素材(小さい)は書けて、既読状態(大きい)は途中で書けない
sys.argv = ["collect", "--no-git", "--date", "2026-10-14", "--skip-explore", "--skip-grok"]
sys.exit(collect.main())
""", encoding="utf-8")
    r = subprocess.run([sys.executable, str(driver)], cwd=root, capture_output=True, text=True, timeout=120)
    after = (root / "stock" / "watch-state.json").read_text(encoding="utf-8")
    cands = _json_or_none((root / "candidates" / "2026-10-14.json").read_text(encoding="utf-8")) or []
    check(r.returncode != 0 and "File too large" in (r.stdout + r.stderr), f"既読状態の書き込みを途中で止められていない(試験の前提): {r.returncode} {r.stderr[-300:]}")
    check(after == before, f"既読状態の保存が途中で止まり、前の既読状態を壊した: {after[:80]!r}")
    check([c.get("url") for c in cands] == ["https://example.org/n1"], f"素材を書けていない(試験の前提): {cands}")
    check([p.name for p in (root / "stock").iterdir()] == ["watch-state.json"],
          f"書けなかった一時ファイルを既読状態の置き場に残した: {list((root / 'stock').iterdir())}")


def test_oncall_collect_rerun_settles_broken_output(tmp: Path):
    """当番の取り直し(収集)が時間切れ・異常終了で途中で終わったときの後始末: 確定する前に出力を検め、書きかけで切れた JSON・書式に
    合わない素材は確定せずに HEAD の中身へ戻す(素材を戻したら既読状態も戻す)。壊れていない成果は確定して作業ツリーを clean にする
    (2026-10-14 監査指摘: 保存の途中で打ち切られた JSON を当番の後始末が commit・push し、組版・発行前の lint が読めずに止まる)。
    実際の git と、実際の収集のプロセス(書き込みの途中で終わる)で検める。"""
    import oncall
    scripts = Path(__file__).resolve().parent
    names = ("ROOT", "run_stage", "notify", "STAGE_END_AT")
    saved = {n: getattr(oncall, n) for n in names}
    saved_root = pipelib.ROOT
    notes = []
    try:
        oncall.notify = lambda job, msg, ok=True, require=False: notes.append(msg) or True
        oncall.STAGE_END_AT = None
        # 1) 実際の収集(取り直し)が、素材を保存したあと既読状態の保存の途中(8文字目)でプロセスごと終わる
        root = _collect_repo(tmp / "cut")
        oncall.ROOT = pipelib.ROOT = root
        child = tmp / "cut-child.py"
        child.write_text(f"""import os, sys, pathlib
sys.path.insert(0, {str(scripts)!r})
import collect, pipelib
pipelib.urllib.request.urlopen = None
root = pathlib.Path({str(root)!r})
collect.ROOT = pipelib.ROOT = root
collect.STATE_PATH = root / "stock" / "watch-state.json"
collect.job_lock = lambda *a, **k: None
collect.notify = pipelib.notify = lambda *a, **k: True
orig = pathlib.Path.write_text
def cut(self, data, *a, **k):
    if self.parent == root / "stock":      # 既読状態の書き込み(一時ファイルを含む)を 8 文字目で打ち切ってプロセスごと終える
        with open(self, "w", encoding="utf-8") as f:
            f.write(data[:8])
        os._exit(124)
    return orig(self, data, *a, **k)
pathlib.Path.write_text = cut
row = {{"title": "告知", "url": "https://example.org/n1", "brand": "shiny", "facts": ["受付を開始"], "_via": "watch", "dedup_key": "a"}}
collect.run_watch = lambda call, oncall_rerun=False: ([dict(row)], {{"stats": {{"_state": {{"official": [row["url"]]}}}}, "new": 1, "deferred": 0}})
collect.run_explores = lambda *a, **k: ([], {{}})
collect.verify = lambda cands: {{"confirmed": 0, "unconfirmed": len(cands), "failed": 0}}
collect.needs_classify = lambda cands: False
sys.argv = ["collect", "--skip-explore", "--skip-grok", "--oncall-rerun", "--date", "2026-10-14"]
sys.exit(collect.main())
""", encoding="utf-8")
        oncall.run_stage = lambda cmd, log, timeout: subprocess.run([sys.executable, str(child)], cwd=root, capture_output=True,
                                                                     text=True, timeout=120).returncode
        code = oncall.rerun_stage("collect", "2026-10-14", "edition/2026-10-14", False)
        state = _json_or_none(_remote_file(root, "stock/watch-state.json"))
        cands = _json_or_none(_remote_file(root, "candidates/2026-10-14.json")) or []
        check(code == 124 and state == {} and [c.get("url") for c in cands] == ["https://example.org/n1"] and oncall.root_clean(),
              f"書き込みの途中で終わった取り直しの後始末: exit {code} / リモートの既読 {state!r} / 素材 {cands} / clean {oncall.root_clean()}")
        # 2) 一時ファイルを経ない書き手が素材の保存の途中で終わった(書きかけの JSON)。既読状態と記録は書けていた
        root2 = _collect_repo(tmp / "broken")
        oncall.ROOT = pipelib.ROOT = root2

        def broken_stage(cmd, log, timeout):
            (root2 / "candidates" / "2026-10-14.json").write_text('[{"id": "x", "ur', encoding="utf-8")
            (root2 / "stock" / "watch-state.json").write_text(json.dumps({"official": ["https://example.org/x"]}), encoding="utf-8")
            (root2 / "metrics" / "2026-10-14.json").write_text(json.dumps({"collect": [{"added": 1}]}), encoding="utf-8")
            return 124
        oncall.run_stage = broken_stage
        notes.clear()
        code = oncall.rerun_stage("collect", "2026-10-14", "edition/2026-10-14", False)
        check(_remote_file(root2, "candidates/2026-10-14.json") == "[]\n" and _remote_file(root2, "stock/watch-state.json") == "{}\n",
              f"書きかけの素材(と、それに合わせた既読状態)をリモートに確定した: {_remote_file(root2, 'candidates/2026-10-14.json')!r} "
              f"{_remote_file(root2, 'stock/watch-state.json')!r}")
        check(_json_or_none(_remote_file(root2, "metrics/2026-10-14.json")) == {"collect": [{"added": 1}]} and oncall.root_clean(),
              f"壊れていない記録を確定しない・作業ツリーが clean でない: {_remote_file(root2, 'metrics/2026-10-14.json')!r} {oncall.root_clean()}")
        check(any("candidates/2026-10-14.json" in m and "戻した" in m for m in notes), f"戻した出力を名指ししない: {notes}")
    finally:
        for n, v in saved.items():
            setattr(oncall, n, v)
        pipelib.ROOT = saved_root


def test_storylink(tmp: Path):
    """既報の照合(二度載せの防止): 同じ一次情報の候補を、過去の記事の話題へ**話題ごと**つなぎ直し、今日の候補どうしもまとめる。
    一覧・特設ページのように多くの記事に出る URL は照合に使わない。続報予約は触らない。選定の索引に既報と過去の記事が付く。"""
    import storylink as sl
    import yaml
    check(sl.url_key("https://x.com/a/status/123?s=20") == "x:123" and sl.url_key("https://x.com/a") == "", "X の URL キー")
    check(sl.url_key("https://www.youtube.com/watch?v=AAAAAAAAAAA&t=1") == "yt:AAAAAAAAAAA", "YouTube の URL キー")
    check(sl.url_key("https://idolmaster-official.jp/news/01_19948.html") == sl.url_key("https://idolmaster-official.jp/news/01_19948/")
          == "idolmaster-official.jp/news/01_19948", "記事ページの URL キー(.html・末尾の / を揃える)")
    check(sl.url_key("https://idolmaster-official.jp/news") == "" and sl.url_key("https://a.jp/") == "", "一覧・トップを記事扱いした")
    # 一覧の URL で別の知らせを同じ記事とみなさない。項目を見分ける見出し(#)・問い合わせ(?id=)は残す(監査指摘 r102)
    check(sl.url_key("https://lantis.jp/sidem/releaseinfo/index.html") == "", "発売情報の一覧を記事扱いした")
    check(sl.url_key("https://lantis.jp/sidem/event/index.html#20260912") != sl.url_key("https://lantis.jp/sidem/event/index.html#20260611") != "",
          "一覧の別の項目(#)を同じ記事とみなした")
    check(sl.url_key("https://a.jp/detail.php?id=1") != sl.url_key("https://a.jp/detail.php?id=2"), "?id= の違う記事を同じとみなした")
    check(sl.url_key("https://www.famitsu.com/article/202609/88518?page=1&utm_source=x") == sl.url_key("https://www.famitsu.com/article/202609/88518"),
          "ページ送り・追跡の問い合わせで同じ記事を別とみなした")
    check(sl.url_key("https://idolmaster-official.jp/news/01_1#sec2") == sl.url_key("https://idolmaster-official.jp/news/01_1"), "記事の節(#)で同じ記事を別とみなした")
    d = "2026-09-18"
    (tmp / "docs" / "_posts").mkdir(parents=True, exist_ok=True)
    (tmp / "metrics").mkdir(exist_ok=True)
    (tmp / "candidates").mkdir(exist_ok=True)
    (tmp / "stock").mkdir(exist_ok=True)
    post = lambda ed, slug, title, urls: (tmp / "docs" / "_posts" / f"{ed}-{slug}.md").write_text(
        "---\n" + yaml.safe_dump({"slug": slug, "brand": "cg", "title": title, "candidate_ids": [],
                                  "sources": [{"url": u, "label": "l"} for u in urls]}, allow_unicode=True) + "---\n本文\n", encoding="utf-8")
    post("2026-09-17", "dijimas", "那覇市でじますコラボ決定", ["https://idolmaster-official.jp/news/01_19889"])
    for i in range(5):     # 5本の記事に出る特設ページ(hub)
        post("2026-09-1" + str(i), f"live{i}", f"ライブ{i}", ["https://idolmaster-official.jp/live_event/x/"])
    (tmp / "metrics" / "plan-2026-09-17.json").write_text(json.dumps({"articles": [{"slug": "dijimas", "dedup_key": "dijimas-cg-2026"}]}), encoding="utf-8")
    for i in range(5):
        (tmp / "metrics" / f"plan-2026-09-1{i}.json").write_text(json.dumps({"articles": [{"slug": f"live{i}", "dedup_key": f"live-{i}"}]}), encoding="utf-8")
    (tmp / "stock" / "stories.yml").write_text(yaml.safe_dump([{"story_id": "dijimas-cg-2026", "first_published": "2026-09-17",
                                                                 "published_facts": ["開催地: 沖縄県那覇市"]}], allow_unicode=True), encoding="utf-8")
    cands = {
        "c1": {"id": "c1", "dedup_key": "cg-dejimas-naha-2026", "url": "https://idolmaster-official.jp/news/01_19889.html", "found_at": "2026-09-17T07:00"},
        "c2": {"id": "c2", "dedup_key": "cg-dejimas-naha-2026", "url": "https://www.famitsu.com/article/1", "found_at": "2026-09-17T08:00"},
        "c3": {"id": "c3", "dedup_key": "b-watch", "url": "https://x.com/imas/status/77", "found_at": "2026-09-17T07:00"},
        "c4": {"id": "c4", "dedup_key": "b-explore", "url": "https://twitter.com/imas/status/77", "found_at": "2026-09-17T09:00"},
        "c5": {"id": "c5", "dedup_key": "hub-topic", "url": "https://idolmaster-official.jp/live_event/x/", "found_at": "2026-09-17T07:00"},
        "sched-1": {"id": "sched-1", "dedup_key": "keep-me", "url": "https://idolmaster-official.jp/news/01_19889", "origin": "scheduled"},
    }
    changed = sl.link(cands, d, tmp)
    check(cands["c1"]["dedup_key"] == cands["c2"]["dedup_key"] == "dijimas-cg-2026", f"話題ごとつなぎ直していない: {cands['c1']['dedup_key']} {cands['c2']['dedup_key']}")
    check(cands["c3"]["dedup_key"] == cands["c4"]["dedup_key"] == "b-watch", f"同じ日の同じ一次情報をまとめない: {cands['c3']['dedup_key']} {cands['c4']['dedup_key']}")
    check(cands["c5"]["dedup_key"] == "hub-topic", "多くの記事に出る特設ページで話題をつないだ")
    check(cands["sched-1"]["dedup_key"] == "keep-me", "続報予約の話題を変えた")
    check(any(c[0] == "c2" and "那覇市でじますコラボ決定" in c[3] for c in changed), f"つなぎ直しの理由: {changed}")
    # 3つ以上の別の話題に使われたシリーズのページ(Memory Pict. の特設 cg_mp)ではつながない。2つまでならつなぐ(二度載せの典型)(監査指摘 r104)
    for i, k in enumerate(("mp-5th", "mp-6th", "mp-6th-b")):
        post(f"2026-09-0{i + 1}", f"mp{i}", f"Memory Pict.第{i + 5}弾", ["https://shop.asobistore.jp/feature/cg_mp"])
        (tmp / "metrics" / f"plan-2026-09-0{i + 1}.json").write_text(json.dumps({"articles": [{"slug": f"mp{i}", "dedup_key": k}]}), encoding="utf-8")
    c7 = {"c7": {"id": "c7", "dedup_key": "mp-7th", "url": "https://shop.asobistore.jp/feature/cg_mp", "found_at": "2026-09-17T07:00"}}
    sl.link(c7, d, tmp)
    check(c7["c7"]["dedup_key"] == "mp-7th", "3つの話題に使われたシリーズのページで、第7弾を過去の回につないだ")
    # 選定の索引に既報(known)と過去の記事(prior)が付く。直近の見出しは面ごと
    saved = compose.ROOT
    try:
        compose.ROOT = tmp
        by_brand, _, _ = compose.write_plan_index(d, {k: {**v, "brand": "cg", "facts": ["f"]} for k, v in cands.items() if k != "sched-1"}, {})
    finally:
        compose.ROOT = saved
    row = next(r for rs in by_brand.values() for r in rs if r["dedup_key"] == "dijimas-cg-2026")
    # 既報は台帳の要約ではなく、過去の記事が読者に出した本文で渡す(2026-10-01: 要約に無い価格を「新しい事実」として8番らーめんを再掲した)
    pr = (row.get("prior") or [{}])[0]
    check("known" not in row and pr.get("edition") == "2026-09-17" and pr.get("text") == "本文",
          f"索引の過去の記事(本文つき): {row.get('prior')}")
    (tmp / "docs" / "_posts" / "2026-09-17-dijimas.md").write_text(
        "---\nslug: dijimas\nbrand: cg\ntitle: 那覇市でじますコラボ決定\nlede: 第1弾は11月20日から。\ncandidate_ids: []\nsources:\n"
        "- url: https://idolmaster-official.jp/news/01_19889\n  label: l\n---\nAセットは税込2310円。 <!-- F1 F2 -->\n\n店舗は31店舗。 <!-- F3 -->\n",
        encoding="utf-8")
    pr = sl.prior_lookup(d, tmp)(["dijimas-cg-2026"])[0]
    check(pr["text"] == "第1弾は11月20日から。 Aセットは税込2310円。 店舗は31店舗。", f"読者に出した本文(リード+本文、注記なし): {pr['text']!r}")
    # 既報は照合の期間(45日)より古くても既報として渡す(監査指摘 r107)
    post("2026-06-01", "old-news", "6月の記事", ["https://a.jp/old/1"])
    (tmp / "metrics" / "plan-2026-06-01.json").write_text(json.dumps({"articles": [{"slug": "old-news", "dedup_key": "old-key"}]}), encoding="utf-8")
    check((sl.prior_lookup(d, tmp)(["old-key"]) or [{}])[0].get("edition") == "2026-06-01", "45日より古い既報が渡らない")
    # 計画が別の主題に統合した記事は、候補が持っていた話題キー(別名)でも既報が引ける。本文は切り詰めない(監査指摘 r108)
    long_tail = "末尾の事実: 発送は2027年2月、シリアル500枚。"
    (tmp / "docs" / "_posts" / "2026-09-05-alias.md").write_text(
        "---\nslug: alias\nbrand: joint\ntitle: Pステ松戸の交流会\ncandidate_ids:\n- c-al\nsources: []\n---\n" + "前段。" * 1200 + long_tail + "\n",
        encoding="utf-8")
    (tmp / "candidates" / "2026-09-05.json").write_text(json.dumps([{"id": "c-al", "dedup_key": "nagoya-meeting", "url": "https://z.jp/a/1"}]), encoding="utf-8")
    (tmp / "metrics" / "plan-2026-09-05.json").write_text(json.dumps({"articles": [{"slug": "alias", "dedup_key": "matsudo-meeting"}]}), encoding="utf-8")
    pb = sl.prior_lookup(d, tmp)
    check((pb(["nagoya-meeting"]) or [{}])[0].get("edition") == "2026-09-05" and (pb(["matsudo-meeting"]) or [{}])[0].get("edition") == "2026-09-05",
          "候補側の話題キー(別名)で既報が引けない")
    check(pb(["matsudo-meeting"])[0]["text"].endswith(long_tail), "長い記事の本文を切り詰めた(末尾の既報が渡らない)")
    check("2026-09-17 那覇市でじますコラボ決定" in sl.recent_titles(d, tmp).get("cg", []), "直近の見出しが面ごとに出ない")


def test_storylink_links_via_post_links(tmp: Path):
    """告知の投稿の候補を、投稿に貼られたリンク(facts の url)と過去の記事の本文のリンクで既報につなぐ(2026-10-17号: アイマスch の
    ラジオ。10/16 号は公式ニュースを出典に配信ページを本文でリンクし、10/17 は同じ配信を告知する X の投稿が新規として選ばれた)。
    facts の url・本文だけのリンクは、2つの話題の記事に出るなら話題を見分けない(催しの記事とインタビューの記事の両方が示すページ)。"""
    import storylink as sl
    import yaml
    d = "2026-10-17"
    (tmp / "docs" / "_posts").mkdir(parents=True, exist_ok=True)
    (tmp / "metrics").mkdir(exist_ok=True)
    post = lambda ed, slug, key, srcs, body: (
        (tmp / "docs" / "_posts" / f"{ed}-{slug}.md").write_text(
            "---\n" + yaml.safe_dump({"slug": slug, "brand": "million", "title": slug, "candidate_ids": [],
                                      "sources": [{"url": u, "label": "l"} for u in srcs]}, allow_unicode=True) + "---\n" + body + "\n",
            encoding="utf-8"),
        (tmp / "metrics" / f"plan-{ed}.json").write_text(json.dumps({"articles": [{"slug": slug, "dedup_key": key}]}), encoding="utf-8"))
    post("2026-10-16", "radio", "idolmaster-ch-radio-members-2026-10", ["https://idolmaster-official.jp/news/01_20103"],
         "配信は[YouTubeの配信ページ](https://youtube.com/live/E0d4UT59xM4)で視聴できます。")
    post("2026-09-02", "party", "retrose-party", ["https://atfes.com/contents/cho-retroseparty"], "催し")
    post("2026-09-04", "interview", "interview-2026", ["https://news.example.jp/interview/1", "https://atfes.com/contents/cho-retroseparty"],
         "インタビュー")
    cands = {
        "r": {"id": "r", "dedup_key": "imas-channel-member-radio-2026-10", "url": "https://x.com/imas_ch/status/2110658707336142874",
              "facts": ["冒頭15分は誰でも視聴できる。", "リンク: https://www.youtube.com/live/E0d4UT59xM4"]},
        "g": {"id": "g", "dedup_key": "retrose-acrylic", "url": "https://x.com/Atfes_portal/status/2111004241741709421",
              "facts": ["リンク: https://atfes.com/contents/cho-retroseparty"]},
    }
    sl.link(cands, d, tmp)
    check(cands["r"]["dedup_key"] == "idolmaster-ch-radio-members-2026-10",
          f"投稿に貼られた配信ページで、同じ配信ページを本文に載せた既報につながない: {cands['r']['dedup_key']}")
    check(cands["g"]["dedup_key"] == "retrose-acrylic",
          f"2つの話題の記事に出るページ(facts の url)で、新しいほうの別の話題につないだ: {cands['g']['dedup_key']}")
    # 本文だけのリンクは2つの話題に出れば見分けない(候補の url そのものでも)
    c2 = {"x": {"id": "x", "dedup_key": "other", "url": "https://youtube.com/live/E0d4UT59xM4", "facts": []}}
    post("2026-10-15", "radio-b", "another-topic", ["https://idolmaster-official.jp/news/01_1"],
         "[配信](https://www.youtube.com/live/E0d4UT59xM4)")
    sl.link(c2, d, tmp)
    check(c2["x"]["dedup_key"] == "other", f"本文のリンクとして2つの話題に出るページでつないだ: {c2['x']['dedup_key']}")
    # 本文の裸の url(「視聴URL：https://…」)も既報の一次情報(監査指摘 R1: 2026-10-14号の IM@S RADIO は配信 URL を裸で書き、
    # 10/15 に同じ配信 URL を facts に持つ公式ニュースの候補が既報につながらず、同じ放送をもう一度載せた)
    post("2026-10-14", "imas-radio", "imas-radio-2026-10-27", ["https://x.com/imas_ch/status/2110000115251605510"],
         "パーソナリティは平田宏美。 <!-- F3 -->\n\n視聴URL：https://www.youtube.com/live/RRByxc2z4eM <!-- F4 -->")
    c3 = {"n": {"id": "n", "dedup_key": "im-s-radio-2026-10-27", "url": "https://idolmaster-official.jp/news/01_20101",
                "facts": ["配信URL: https://youtube.com/live/RRByxc2z4eM"]}}
    sl.link(c3, d, tmp)
    check(c3["n"]["dedup_key"] == "imas-radio-2026-10-27", f"本文の裸の url で既報につながない: {c3['n']['dedup_key']}")
    check(sl.text_urls("視聴URL：https://www.youtube.com/live/RRByxc2z4eMで配信。<https://a.jp/b>、[x](https://c.jp/d).")
          == ["https://www.youtube.com/live/RRByxc2z4eM", "https://a.jp/b", "https://c.jp/d"], "文の url を和文・括弧で切らない")


def test_storylink_same_day_fact_urls(tmp: Path):
    """同じ日の候補どうしも facts の url で束ねる(監査指摘 R2: 2026-10-15 の公式ニュースの IM@S RADIO と、そのニュースを「詳細」に
    貼った公式 X の投稿が別の話題のまま選定に渡った)。facts の url が別の候補の url そのもののときだけ束ね、2つ以上の別の話題の
    候補が facts に持つページ(催しのページ)・2つ以上の別の話題の候補の url を facts に並べた候補(リンク集の投稿)では束ねない。"""
    import storylink as sl
    d = "2026-10-15"
    (tmp / "docs" / "_posts").mkdir(parents=True, exist_ok=True)
    cands = {
        "w": {"id": "w", "dedup_key": "im-s-radio-2026-10-27", "url": "https://idolmaster-official.jp/news/01_20101",
              "found_at": "2026-10-14T12:33", "facts": ["配信URL: https://youtube.com/live/RRByxc2z4eM"]},
        "g": {"id": "g", "dedup_key": "imas-radio-2026-10-27", "url": "https://x.com/imas_official/status/2110188856045969519",
              "found_at": "2026-10-15T02:25", "facts": ["詳細: https://idolmaster-official.jp/news/01_20101"]},
        # 催しのページ(候補 e の url)を2つの別の話題の候補が facts に持つ: どちらも束ねない
        "e": {"id": "e", "dedup_key": "live-2027", "url": "https://idolmaster-official.jp/live/abc", "facts": []},
        "goods": {"id": "goods", "dedup_key": "live-goods", "url": "https://x.com/a/status/1", "facts": ["https://idolmaster-official.jp/live/abc"]},
        "ticket": {"id": "ticket", "dedup_key": "live-ticket", "url": "https://x.com/a/status/2", "facts": ["https://idolmaster-official.jp/live/abc"]},
        # 2つの別の話題の候補の url を並べたリンク集の投稿: 橋渡ししない
        "v1": {"id": "v1", "dedup_key": "voice", "url": "https://va-liv.booth.pm/items/8830505", "facts": []},
        "v2": {"id": "v2", "dedup_key": "third-live", "url": "https://idolmaster-official.jp/va-liv/event/3rd_live", "facts": []},
        "r": {"id": "r", "dedup_key": "reply", "url": "https://x.com/valiv_official/status/3",
              "facts": ["ティザー: https://idolmaster-official.jp/va-liv/event/3rd_live", "ボイス: https://va-liv.booth.pm/items/8830505"]},
    }
    sl.link(cands, d, tmp)
    check(cands["w"]["dedup_key"] == cands["g"]["dedup_key"],
          f"告知ページを facts に貼った投稿を、告知ページの候補と束ねない: {cands['w']['dedup_key']} / {cands['g']['dedup_key']}")
    check([cands[i]["dedup_key"] for i in ("e", "goods", "ticket")] == ["live-2027", "live-goods", "live-ticket"],
          f"2つの話題の候補が facts に持つページで束ねた: {[cands[i]['dedup_key'] for i in ('e', 'goods', 'ticket')]}")
    check([cands[i]["dedup_key"] for i in ("v1", "v2", "r")] == ["voice", "third-live", "reply"],
          f"リンク集の投稿で別の話題を橋渡しした: {[cands[i]['dedup_key'] for i in ('v1', 'v2', 'r')]}")


def test_storylink_related_prior(tmp: Path):
    """同じ催しの別の発表の記事も既報として渡す(当番の指摘 0405461304。2026-10-16号: 765×961 ライブの 765側出演者は 9/30 号の
    記事にあったが、候補は URL で 10/14 号のゲスト発表の話題へつながり、選定には 10/14 号の本文しか渡らず、出演者を新情報として選んだ)。
    つなぎ直す前の話題キー(linked_from)・候補の URL・既報の記事と同じ一次情報/特設ページを持つ別の話題の記事まで広げる。
    特設ページでも、多くの記事・3つ以上の話題に出るもの(hub)ではつながない。選定の「この面の直近の記事」には合同面の見出しも添える。"""
    import storylink as sl
    import yaml
    d = "2026-10-16"
    for sub in ("docs/_posts", "metrics", "candidates", "stock"):
        (tmp / sub).mkdir(parents=True, exist_ok=True)

    def post(ed, slug, key, title, body, urls, ids=(), brand="joint"):
        (tmp / "docs" / "_posts" / f"{ed}-{slug}.md").write_text(
            "---\n" + yaml.safe_dump({"slug": slug, "brand": brand, "title": title, "candidate_ids": list(ids),
                                      "sources": [{"url": u, "label": "l"} for u in urls]}, allow_unicode=True) + "---\n" + body + "\n",
            encoding="utf-8")
        p = tmp / "metrics" / f"plan-{ed}.json"
        arts = json.loads(p.read_text(encoding="utf-8"))["articles"] if p.exists() else []
        p.write_text(json.dumps({"articles": arts + [{"slug": slug, "dedup_key": key}]}), encoding="utf-8")

    live = "https://idolmaster-official.jp/live_event/IUOAFA/"
    post("2026-09-07", "ticket", "ultimate-ticket", "765×961合同ライブ、チケット先行開始", "先行受付が始まった。", ["https://famitsu.com/a/1", live],
         ids=["c-0907"])
    (tmp / "candidates" / "2026-09-07.json").write_text(json.dumps([{"id": "c-0907", "dedup_key": "ultimate-2027", "url": "https://famitsu.com/a/1"}]),
                                                        encoding="utf-8")
    post("2026-09-30", "cast", "xr-live-2027", "765×961 xRライブ、7月に3公演", "第一公演の出演は星井美希、四条貴音、我那覇響、奥空心白。", [live])
    post("2026-10-14", "guests", "guest-idols", "765×961ライブに蘭子・未来・果穂", "ゲストは神崎蘭子、春日未来、小宮果穂。",
         ["https://t.co/abc", "https://x.com/imas/status/1"])
    # 3つ以上の話題に出るシリーズの特設ページ(hub)では、別の催しの記事を添えない
    for i in range(3):
        post(f"2026-08-0{i + 1}", f"mp{i}", f"mp-{i}", f"Memory Pict.第{i + 5}弾", f"第{i + 5}弾の本文", ["https://shop.asobistore.jp/feature/cg_mp"],
             brand="cg")
    post("2026-10-10", "other", "other-key", "765 単独の記事", "単独の本文", ["https://a.jp/n/1"], brand="765")
    cands = {"c1": {"id": "c1", "dedup_key": "ultimate-2027", "brand": "765", "url": "https://t.co/abc", "facts": ["765側の出演者4人"],
                    "found_at": "2026-10-16T02:29"}}
    sl.link(cands, d, tmp)
    c = cands["c1"]
    check(c["dedup_key"] == "guest-idols" and c.get("linked_from") == ["ultimate-2027"], f"つなぎ直す前の話題キーが残らない: {c}")
    rows = sl.prior_lookup(d, tmp)(sl.prior_keys([c]), [c["url"]])
    got = {r["edition"]: r.get("related", False) for r in rows}
    check(got == {"2026-10-14": False, "2026-09-07": False, "2026-09-30": True},
          f"同じ催しの別の発表の記事(出演者の 9/30 号)が既報に入らない、または関係の無い記事が入った: {got}")
    check([r["edition"] for r in rows] == ["2026-10-14", "2026-09-07", "2026-09-30"], f"既報の並び(直接の既報が先、新しい順): {rows}")
    mp = sl.prior_lookup(d, tmp)(["mp-0"])
    check([r["edition"] for r in mp] == ["2026-08-01"], f"シリーズの特設ページ(hub)で別の回の記事を添えた: {mp}")
    saved = compose.ROOT
    try:
        compose.ROOT = tmp
        by_brand, _, _ = compose.write_plan_index(d, cands, {})
        recent = compose.recent_for_brand(d, "765")
    finally:
        compose.ROOT = saved
    row = by_brand["765"][0]
    check(any(p.get("related") and "四条貴音" in p["text"] for p in row.get("prior") or []),
          f"選定の索引に、同じ催しの別の発表の記事(出演者)が渡らない: {row.get('prior')}")
    check("linked_from" not in row, "選定の索引に内部の印を出した")
    check("2026-10-14 765×961ライブに蘭子・未来・果穂(合同面)" in recent and "2026-10-10 765 単独の記事" in recent,
          f"この面の直近の記事に合同面の見出しが無い: {recent}")
    # 発表が重なり、公演ページが3つ目の話題に載って hub になっても、同じ催しの既報は渡る(監査指摘 event-page-becomes-hub)。
    # 新しい候補は過去のキーも hub でない URL も持たない(公演ページの下の別のページだけ)
    post("2026-10-15", "guest2", "second-guest", "765×961ライブ第二公演のゲスト", "第二公演のゲストは天海春香。", [live])
    check(sl.url_key(live) in sl.hub_urls(sl.past_articles("2026-10-16", tmp, sl.PRIOR_DAYS)), "前提: 公演ページが hub になっていない")
    for u in (live, live + "ticket"):
        c2 = {"id": "c2", "dedup_key": "ultimate-ticket-general", "url": u, "found_at": "2026-10-16T03:00"}
        sl.link({"c2": c2}, d, tmp)
        rows = sl.prior_lookup(d, tmp)(sl.prior_keys([c2]), [u])
        check(c2["dedup_key"] == "ultimate-ticket-general" and [r["edition"] for r in rows] == ["2026-10-15", "2026-09-30", "2026-09-07"]
              and all(r.get("related") for r in rows),
              f"公演ページが hub になった後、同じ催しの既報(出演者の 9/30 号)が渡らない({u}): {[(r['edition'], r.get('related')) for r in rows]}")
    # 同じ催しの発表がさらに重なっても(第2・第3公演のゲストが別の話題の記事になる)、古い発表の記事(出演者の 9/30 号)は本数で
    # 押し出されない。選定の索引では、催しの記事は催しごとに plan-events に全部書き、主題は催しのキーで指す(監査指摘 related-prior-evicts-known-cast)
    post("2026-10-15", "guest3", "third-guest", "765×961ライブ第三公演のゲスト", "第三公演のゲストは如月千早。", [live + "ticket"])
    c3 = {"id": "c3", "dedup_key": "cast-ticket-followup", "brand": "765", "url": live + "ticket", "facts": ["765側の出演者4人"],
          "found_at": "2026-10-16T03:30"}
    sl.link({"c3": c3}, d, tmp)
    rows = sl.prior_lookup(d, tmp)(sl.prior_keys([c3]), [c3["url"]])
    check(any("四条貴音" in r["text"] for r in rows) and [r["edition"] for r in rows] == ["2026-10-15", "2026-10-15", "2026-09-30", "2026-09-07"],
          f"同じ催しの発表が重なると、出演者の既報が本数の上限で押し出された: {[(r['edition'], r['title']) for r in rows]}")
    saved = compose.ROOT
    try:
        compose.ROOT = tmp
        by_brand, _, _ = compose.write_plan_index(d, {"c3": c3}, {})
        ev = json.loads((tmp / "metrics" / f"plan-events-{d}-765.json").read_text(encoding="utf-8"))
        # 判定から漏れた主題の拾い直しにも、同じ催しの記事を渡す
        saved_rp = (compose.subprocess.run, compose.prompt_file, compose.save_raw)
        compose.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "")
        compose.prompt_file, compose.save_raw = (lambda *a, **kw: "p"), (lambda *a, **kw: None)
        try:
            compose.replan_missing(d, {"articles": []}, by_brand, {"c3": c3}, [c3["dedup_key"]])
        finally:
            compose.subprocess.run, compose.prompt_file, compose.save_raw = saved_rp
        ev_m = json.loads((tmp / "metrics" / f"plan-events-{d}-missing.json").read_text(encoding="utf-8"))
    finally:
        compose.ROOT = saved
    check(ev_m == ev, f"拾い直しに同じ催しの記事が渡らない: {ev_m}")
    row = by_brand["765"][0]
    arts = [a for e in ev if e["event"] in row.get("events", []) for a in e["articles"]]
    check(row.get("events") == [sl.event_key(sl.url_key(live))] and any("四条貴音" in a["text"] for a in arts)
          and [a["edition"] for a in arts] == ["2026-10-15", "2026-10-15", "2026-09-30", "2026-09-07"],
          f"選定の索引で、同じ催しの記事が全部渡らない: events={row.get('events')} {[(a['edition'], a['title']) for a in arts]}")
    # 同じ話題の記事も本数で切らない(3本目以降の古い記事が書いた事実が新しく見えないように)
    for i, ed in enumerate(("2026-08-10", "2026-08-11", "2026-08-12")):
        post(ed, f"bd{i}", "letora-bd", f"誕生日第{i + 1}報", f"第{i + 1}報の事実{i}。", [f"https://b.jp/n/{i}"], brand="dsva")
    check([r["edition"] for r in sl.prior_lookup(d, tmp)(["letora-bd"])] == ["2026-08-12", "2026-08-11", "2026-08-10"],
          f"同じ話題の古い記事が本数の上限で押し出された: {sl.prior_lookup(d, tmp)(['letora-bd'])}")
    check(sl.event_key(sl.url_key("https://shop.asobistore.jp/feature/cg_mp")) == ""
          and sl.event_key(sl.url_key("https://idolmaster-official.jp/live_event/")) == "",
          "シリーズの特設ページ・公演の一覧を、1つの催しのページとみなした")


def test_grok_roles(tmp: Path):
    """X 調査の役割分担(編集長 2026-10-04): Grok は X の検索だけ → Luna が確かめて候補と「X の原本でしか確かめられない問い」を出す →
    週の検索予算の残りの範囲でだけ Grok が原本を深掘り → もう一度 Luna。"""
    import collect
    # 予算: 週の上限 × 深掘りの割合 − 直近7日の実使用。1回の上限も効く
    saved = (collect.GROK_WEEKLY_SEARCHES, collect.GROK_DEEP_SHARE, collect.GROK_DEEP_MAX)
    try:
        collect.GROK_WEEKLY_SEARCHES, collect.GROK_DEEP_SHARE, collect.GROK_DEEP_MAX = 1000, 0.8, 24
        check(collect.deep_budget(300) == 24 and collect.deep_budget(790) == 10 and collect.deep_budget(900) == 0,
              f"深掘りの予算: {collect.deep_budget(300)} {collect.deep_budget(790)} {collect.deep_budget(900)}")
    finally:
        collect.GROK_WEEKLY_SEARCHES, collect.GROK_DEEP_SHARE, collect.GROK_DEEP_MAX = saved
    # 問いの配分: 面を順に1問ずつ、問い1つに検索2回、1面3問まで
    deep = {"sidem": [{"question": f"s{i}"} for i in range(5)], "gakuen": [{"question": "g0"}]}
    ch = collect.plan_deep_dive(deep, 6)
    check({k: [a["question"] for a in v] for k, v in ch.items()} == {"gakuen": ["g0"], "sidem": ["s0", "s1"]}, f"問いの配分(予算6): {ch}")
    ch = collect.plan_deep_dive(deep, 100)
    check(len(ch["sidem"]) == 3, f"1面3問まで: {ch}")
    check(collect.plan_deep_dive(deep, 1) == {}, "予算が足りないのに深掘りする")
    # 確かめる段: 投稿の無い面・「なし」の面は Luna を起動しない。Luna が書いた候補と問いを読み、読めない面は異常にする
    tmp.mkdir(parents=True, exist_ok=True)
    out = tmp / "g"
    out.mkdir()
    (out / "sidem.md").write_text("### 1\n- url: https://x.com/a/status/1\n- 本文: 本文", encoding="utf-8")
    (out / "gakuen.md").write_text("なし", encoding="utf-8")
    (out / "shiny.md").write_text("### 1\n- url: https://x.com/b/status/2\n- 本文: 本文", encoding="utf-8")
    qs = [{"key": k, "brand": k, "topic": "t"} for k in ("sidem", "gakuen", "shiny", "dsva")]
    started, notes = [], []
    class P:
        pid = 0
        returncode = 0
        def wait(self, timeout=None): return 0
        def communicate(self, timeout=None): return None, None
    def fake_popen(args, cwd=None, **kw):
        started.append(Path(cwd).name)
        if "sidem" in Path(cwd).name:
            (Path(cwd) / "items.json").write_text(json.dumps([{"url": "https://x.com/a/status/1", "brand": "sidem", "facts": ["f"]}]), encoding="utf-8")
            (Path(cwd) / "posts.json").write_text(json.dumps([{"url": "https://x.com/a/status/1", "status": "extracted",
                                                              "item": "https://x.com/a/status/1"}]), encoding="utf-8")
            (Path(cwd) / "deep.json").write_text(json.dumps([{"question": "元の告知", "why": "引用だけ"}]), encoding="utf-8")
        return P()
    saved = (collect.subprocess.Popen, collect.notify, collect.prompt_file)
    try:
        collect.subprocess.Popen = fake_popen
        collect.notify = lambda job, msg, ok=True, require=False: notes.append((msg, ok)) or True
        collect.prompt_file = lambda date, name, prompt, base=None: "p"
        items, deep = collect.verify_grok_faces(qs, out)
    finally:
        collect.subprocess.Popen, collect.notify, collect.prompt_file = saved
    # 候補の読めない shiny だけ、もう1回確かめる
    check(sorted(re.sub(r"^explore-|-[^-]+$", "", s) for s in started) == ["verify-shiny", "verify-shiny-again", "verify-sidem"],
          f"確かめる段を起動した面: {started}")
    check([x["url"] for x in items] == ["https://x.com/a/status/1"] and deep == {"sidem": [{"question": "元の告知", "why": "引用だけ"}]},
          f"候補と問い: {items} {deep}")
    check(any(not ok and "shiny" in m and "items" not in m for m, ok in notes), f"結果の読めない面を異常にしない: {notes}")
    check(any(not ok and "shiny" in m and "deep.json" in m for m, ok in notes) and not any("sidem" in m for m, ok in notes),
          f"問いの書き出しが読めない面を異常にしない(読めた面は異常にしない): {notes}")
    # 週の使用は、利用者のすべての Grok セッション(別のクローン・手での利用を含む)から数える(監査指摘 r116)
    home = tmp / "home2"
    for repo, n in (("a", 3), ("b", 5)):
        sd = home / ".grok" / "sessions" / repo / "s"
        sd.mkdir(parents=True)
        (sd / "updates.jsonl").write_text('{"rawInput": {"variant": "XSearch"}}\n' * n, encoding="utf-8")
    saved_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = str(home)
        check(collect.grok_week_usage() == 8, f"週の使用を全セッションから数えない: {collect.grok_week_usage()}")
    finally:
        if saved_home is not None:
            os.environ["HOME"] = saved_home


def test_empty_answer_is_not_done(tmp: Path):
    """空・部分的なモデルの答えを『処理した・何も無い』と解釈しない(2026-10-06 の洗い出し)。
    入力1件ごとの答えをコードが入力と突き合わせ、答えの無いものだけやり直し、それでも無ければ名指しで異常にする。"""
    import collect, compose, planlib, assemble
    tmp.mkdir(parents=True, exist_ok=True)
    # 1. Grok の投稿: Luna が投稿ごとの結果を1件しか返さない → 残った1件だけで1回やり直し、それでも無ければ URL を名指し
    out = tmp / "g"
    out.mkdir()
    (out / "cg.md").write_text("### 1\n- url: https://x.com/a/status/1\n- 本文: 告知\n\n### 2\n- url: https://x.com/b/status/2\n- 本文: 続報\n",
                               encoding="utf-8")
    inputs, notes, raws = {}, [], []
    class P:
        pid = 0
        returncode = 0
        def wait(self, timeout=None): return 0
        def communicate(self, timeout=None): return None, None
    def fake_popen(args, cwd=None, **kw):
        inputs[re.sub(r"^explore-|-[^-]+$", "", Path(cwd).name)] = (Path(cwd) / "x-posts.md").read_text(encoding="utf-8")
        (Path(cwd) / "items.json").write_text(json.dumps([{"url": "https://x.com/a/status/1", "brand": "cg", "facts": ["f"]}]), encoding="utf-8")
        (Path(cwd) / "posts.json").write_text(json.dumps([{"url": "https://x.com/a/status/1", "status": "extracted", "item": "https://x.com/a/status/1"},
                                                          {"url": "https://x.com/b/status/2", "status": "unreadable", "item": ""}]), encoding="utf-8")
        (Path(cwd) / "deep.json").write_text("[]", encoding="utf-8")
        return P()
    saved = (collect.subprocess.Popen, collect.notify, collect.prompt_file, collect.save_raw)
    try:
        collect.subprocess.Popen = fake_popen
        collect.notify = lambda job, msg, ok=True, require=False: notes.append((msg, ok)) or True
        collect.prompt_file = lambda date, name, prompt, base=None: "p"
        collect.save_raw = lambda date, name, *a, **kw: raws.append((name, sorted((kw.get("files") or {}))))
        items, _ = collect.verify_grok_faces([{"key": "cg", "brand": "cg", "topic": "t"}], out)
    finally:
        collect.subprocess.Popen, collect.notify, collect.prompt_file, collect.save_raw = saved
    check(sorted(inputs) == ["verify-cg", "verify-cg-again"] and "status/2" in inputs["verify-cg-again"]
          and "status/1" not in inputs["verify-cg-again"], f"結果の無い投稿だけでやり直さない: {inputs}")
    check(any(not ok and "https://x.com/b/status/2" in m and "1件" in m for m, ok in notes), f"2回とも残った投稿を名指ししない: {notes}")
    check([n for n, _ in raws] == ["grok-verify-cg", "grok-verify-cg-again"] and "posts.json" in raws[0][1],
          f"作業ディレクトリを消す前に生の出力を残さない: {raws}")
    one = "- url: https://x.com/a/status/1\n"
    check(collect.unaccounted_posts(one, None, []) == ["- url: https://x.com/a/status/1"],
          "投稿ごとの結果が読めないのに、全部処理したことにした")
    check(collect.unaccounted_posts(one, [{"url": "https://x.com/a/status/1", "status": "extracted", "item": "https://x.com/a/status/1"}], []) != [],
          "「候補にした」と言って候補を書いていない投稿を済みにした")
    check(collect.unaccounted_posts(one, [{"url": "https://x.com/a/status/1", "status": "none", "item": ""}], []) == [], "事実なしの投稿を残した")
    check(collect.unaccounted_posts("告知 https://x.com/a/status/1 です", [], []) == ["告知 https://x.com/a/status/1 です"],
          "投稿に分けられない書き出しを0件にした")
    # 書式の崩れた投稿と正しい投稿が混ざっても、崩れた投稿を捨てない(監査指摘 r2)
    mixed = "### 1\nurl: https://x.com/a/status/1\n本文: A\n\n### 2\n- url: https://x.com/b/status/2\n- 本文: B\n"
    check([collect.post_url(b) for b in collect.unaccounted_posts(mixed, [{"url": "https://x.com/b/status/2", "status": "none", "item": ""}], [])]
          == ["https://x.com/a/status/1"], f"書式の崩れた投稿が消えた: {collect.post_blocks(mixed)}")
    mid = ("### 1\n- url: https://x.com/a/status/1\n- 本文: A\n\n### 2\n- 本文: B(url の行が抜けた)\n\n"
           "### 3\n- url: https://x.com/c/status/3\n- 本文: C https://x.com/q/status/9 を引用\n")
    none_ac = [{"url": "https://x.com/a/status/1", "status": "none", "item": ""}, {"url": "https://x.com/c/status/3", "status": "none", "item": ""}]
    left_mid = collect.unaccounted_posts(mid, none_ac, [])
    check(len(left_mid) == 1 and "本文: B" in left_mid[0] and "本文: A" not in left_mid[0],
          f"途中の url の抜けた投稿が前の投稿に吸われて消えた(引用投稿の URL は区切りの漏れにしない): {left_mid}")
    tail = "### 1\n- url: https://x.com/a/status/1\n- 本文: A\n\n### 2\n- 本文: B(末尾で url の行が抜けた)\n"
    check(len(collect.unaccounted_posts(tail, [{"url": "https://x.com/a/status/1", "status": "none", "item": ""}], [])) == 1,
          "末尾の url の抜けた投稿が消えた")
    check(len(collect.unaccounted_posts(tail, [{"url": "https://x.com/a/status/1", "status": "none", "item": ""}, {"url": "", "status": "none", "item": ""}], [])) == 1,
          "空の url の結果で url の無い投稿を済みにした")
    mixed2 = "https://x.com/a/status/1 の告知\n\n- url: https://x.com/b/status/2\n- 本文: B\n"
    check(len(collect.unaccounted_posts(mixed2, [{"url": "https://x.com/b/status/2", "status": "none", "item": ""}], [])) == 1,
          "区切りより前の投稿が消えた")
    # 後段で捨てられる url の候補を指す extracted は済みにしない
    check(collect.unaccounted_posts(one, [{"url": "https://x.com/a/status/1", "status": "extracted", "item": "not-a-url"}], ["not-a-url"]) != [],
          "形の不正な url の候補を指す投稿を済みにした")
    # 2. 校閲: 判定の形をしていない答え({}・verdict なし)を「指摘なし」にしない
    check(compose._parse_review("{}", "", "t").get("verdict") not in ("approve", "block"), "空の校閲の答えを判定にした")
    check(compose._parse_review('{"verdict":"approve","blockers":[],"comments":[]}', "", "t")["verdict"] == "approve", "形の正しい判定を読めない")
    check(compose._parse_review('{"verdict":"block","blockers":[],"comments":[]}', "", "t")["verdict"] == "error",
          "理由の無い block を判定にした(まとめると承認に化ける)")
    for bad in ('{"verdict":"block","blockers":[null],"comments":[]}', '{"verdict":"approve","blockers":[],"comments":["x"]}',
                '{"verdict":"block","blockers":[{"file":"a","issue":"i"}],"comments":[]}'):
        check(compose._parse_review(bad, "", "t")["verdict"] == "error", f"崩れた指摘の要素を判定にした: {bad}")
    # 生の出力は上書きしない(やり直しが成功しても1回目が残る)
    saved_root = pipelib.ROOT
    try:
        pipelib.ROOT = tmp / "repo"
        a, b = pipelib.save_raw("d", "watch-x", "1回目"), pipelib.save_raw("d", "watch-x", "2回目")
    finally:
        pipelib.ROOT = saved_root
    check(a != b and "1回目" in a.read_text(encoding="utf-8") and "2回目" in b.read_text(encoding="utf-8"), f"生の出力を上書きした: {a} {b}")
    # 時間切れの回も、それまでの出力を残す(判定には使わない)
    saved_rs = (compose.run_group, compose.save_raw, compose.prompt_file)
    kept = []
    def timeout_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1, output=b'{"partial', stderr=b"working")
    try:
        compose.run_group, compose.prompt_file = timeout_run, (lambda d, n, p, base=None: "p")
        compose.save_raw = lambda date, name, out="", err="", code=None, files=None: kept.append((name, out, err))
        try:
            compose.claude_run("x", timeout=5)
        except subprocess.TimeoutExpired:
            pass
        compose.codex_run("x", timeout=5)
    finally:
        compose.run_group, compose.save_raw, compose.prompt_file = saved_rs
    check(len(kept) == 2 and all('{"partial' in o and "working" in e for _, o, e in kept), f"時間切れの回の出力を残さない: {kept}")
    # 3. 面別選定: 判定の無い主題を「不採用」にしない(取りこぼしとして拾い直しに回る)
    rows = [{"dedup_key": "a", "ids": ["1"]}, {"dedup_key": "b", "ids": ["2"]}]
    p = planlib.decisions_to_plan("cg", rows, {"a": {"action": "drop", "reason": "既報"}}, set())
    check([d["dedup_key"] for d in p["dropped"]] == ["a"] and not p["articles"], f"判定の無い主題を不採用にした: {p}")
    p = planlib.decisions_to_plan("cg", rows, {"a": {"action": "defer"}, "b": {"action": "drop", "reason": "既報"}}, set())
    check([d["dedup_key"] for d in p["dropped"]] == ["b"], f"知らない判定の主題を不採用にした: {p}")
    p = planlib.decisions_to_plan("cg", rows, {"a": {"action": "drop"}, "b": {"action": "drop", "reason": " "}}, set())
    check(p["dropped"] == [], f"理由の無い不採用で主題を消した: {p}")
    # 探索の答えの崩れた要素([null] など)で収集を止めない。読めない出力として記録する
    of, ef = tempfile.NamedTemporaryFile("w", delete=False), tempfile.NamedTemporaryFile("w", delete=False)
    of.write('[null, {"url": "https://example.com/a", "facts": ["f"]}]')
    class EP:
        pid, returncode, args = 0, 0, []
        def wait(self, timeout=None): return 0
        def communicate(self, timeout=None): return None, None
    saved_sr = collect.save_raw
    try:
        collect.save_raw = lambda *a, **kw: None
        got_e, err_e = collect.collect_explore("k", EP(), of, ef, 10 ** 12)
    finally:
        collect.save_raw = saved_sr
    check([g["url"] for g in got_e] == ["https://example.com/a"] and "[出力が読めない]" in err_e, f"探索の崩れた要素: {got_e} {err_e!r}")
    # 正規化で捨てられる形の候補を、処理済みの根拠にしない(Grok の投稿・定点観測のページ)
    check(collect.normalize([{"url": "https://example.com/a", "title": "t", "facts": ["f"], "event_date": 20261006}])[0]["url"]
          == "https://example.com/a", "日付が数値の候補を捨てた")
    check(collect.unaccounted_posts(one, [{"url": "https://x.com/a/status/1", "status": "extracted", "item": "https://x.com/a/status/1"}],
                                    [{"url": "https://x.com/a/status/1", "facts": ["f"]}]) == [], "書かれた候補を指す投稿を残した")
    check(collect.watch_page_results([{"page": 1, "status": "extracted", "items": [{"url": "not-a-url"}]}], 1) == {},
          "正規化で捨てられる候補だけのページを既読にした")
    check(collect.watch_page_results([{"page": 1, "status": "extracted", "items": [{"url": "https://example.com/n"}]}], 1) == {},
          "事実の無い候補だけのページを既読にした")
    check(collect.unaccounted_posts(one, [{"url": "https://x.com/a/status/1", "status": "extracted", "item": "https://x.com/a/status/1"}],
                                    [{"url": "https://x.com/a/status/1"}]) != [], "事実の無い候補を指す投稿を済みにした")
    # 崩れた候補(url が配列・facts が数値)で付け直しが止まらない
    check(collect.repoint_to_post([s for s in (collect.shape_item(x) for x in [{"url": ["a"]}, {"url": "https://youtube.com/watch?v=x", "facts": 7}]) if s], one)
          [0]["facts"] == [], "崩れた候補で付け直しが止まる")
    # 候補ID修復: [null] は「直すものなし」ではなく読めない答え(2回聞いて異常)
    calls = []
    saved = (compose.claude_run, list(pipelib.ANOMALIES))
    try:
        compose.claude_run = lambda prompt, timeout=600, model=None: calls.append(1) or "[null]"
        pipelib.ANOMALIES.clear()
        compose.repair_invalid_ids("2026-10-06", {"articles": [{"slug": "s", "candidate_ids": ["bad"]}]}, {"good": {"id": "good"}})
        anomalies = list(pipelib.ANOMALIES)
    finally:
        compose.claude_run = saved[0]
        pipelib.ANOMALIES[:] = saved[1]
    check(len(calls) == 2 and anomalies and "s" in anomalies[0], f"崩れた修復の答えを「直すものなし」にした: {calls} {anomalies}")
    # 空の修復先・実在しない修復先も読めない答え。正しい答えは反映する
    for ans, want_calls in (('[{"slug": "s", "old": "bad", "new": ""}]', 2), ('[{"slug": "s", "old": "bad", "new": "good"}]', 1)):
        calls.clear()
        plan_r = {"articles": [{"slug": "s", "candidate_ids": ["bad"]}]}
        saved = (compose.claude_run, list(pipelib.ANOMALIES))
        try:
            compose.claude_run = lambda prompt, timeout=600, model=None, a=ans: calls.append(1) or a
            fixed = compose.repair_invalid_ids("2026-10-06", plan_r, {"good": {"id": "good"}})
        finally:
            compose.claude_run = saved[0]
            pipelib.ANOMALIES[:] = saved[1]
        check(len(calls) == want_calls and (want_calls == 2 or plan_r["articles"][0]["candidate_ids"] == ["good"]),
              f"候補ID修復の答えの検査: {ans} {calls} {plan_r}")
    # 時間切れの回収は子が出力のパイプを持っていても待ち続けない
    import time as _t
    pr = subprocess.Popen(["bash", "-c", "sleep 30 & echo partial; wait"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True)
    t0 = _t.time()
    try:
        pr.communicate(timeout=0.5)
    except subprocess.TimeoutExpired:
        so, se = pipelib.reap(pr, grace=3)
    check(_t.time() - t0 < 10 and "partial" in so and "時間切れ" in se, f"時間切れの回収が子を待ち続けた: {_t.time() - t0:.1f}s {so!r}")
    # 4. 組版の台帳: stories の無い記事だけで1回やり直し、それでも無ければ記事を名指しで異常にする
    arts = [{"slug": f"s{i}", "dedup_key": f"k{i}", "title": f"t{i}"} for i in range(3)]
    asked = []
    def fake_session(text, date, name, schema, budget=None):
        asked.append(name)
        if name == "assemble-digest":
            return {"digest": []}
        if name == "assemble-ledger-again":
            return {"stories": [{"slug": "s1", "story_id": "k1", "subject": "x", "published_facts": ["t1"]}],
                    "reservations": [], "pending_add": [], "pending_remove": []}
        return {"stories": [{"slug": "s0", "story_id": "k0", "subject": "x", "published_facts": ["t0"]}],
                "reservations": [], "pending_add": [], "pending_remove": []}
    saved = (assemble.run_session, assemble.prompt_ledger, assemble.prompt_digest, list(pipelib.ANOMALIES))
    try:
        assemble.run_session = fake_session
        assemble.prompt_ledger = lambda date, inp, ch: ",".join(a["slug"] for a in ch)
        assemble.prompt_digest = lambda date, inp: ""
        pipelib.ANOMALIES.clear()
        merged = assemble.judge("2026-10-06", {"articles": arts, "pending": []})
        anomalies = list(pipelib.ANOMALIES)
    finally:
        assemble.run_session, assemble.prompt_ledger, assemble.prompt_digest = saved[:3]
        pipelib.ANOMALIES[:] = saved[3]
    check("assemble-ledger-again" in asked and [s["slug"] for s in merged["stories"]] == ["s0", "s1"],
          f"台帳の答えの無い記事をやり直さない: {asked} {merged['stories']}")
    check(len(anomalies) == 1 and "s2" in anomalies[0] and "s1" not in anomalies[0], f"やり直しても無い記事を名指ししない: {anomalies}")
    check(assemble.uncovered_articles([arts[:1]], [{"stories": [{"slug": "s0"}]}]) == arts[:1], "slug だけの stories を判断済みにした")
    ok_row = {"slug": "s0", "story_id": "k0", "subject": "x", "published_facts": ["F1"]}
    check(assemble.uncovered_articles([arts[:1]], [{"stories": [ok_row]}]) == arts[:1], "予約・追跡の項目が欠けた答えを判断済みにした")
    check(assemble.uncovered_articles([arts[:1]], [{"stories": [ok_row], "reservations": [], "pending_add": [], "pending_remove": []}]) == [],
          "明示の [] を欠けと取り違えた")
    bad_row = {"slug": "s0", "story_id": "k0", "subject": "x", "published_facts": 1}
    good_row = {"slug": "s0", "story_id": "k0", "subject": "x", "published_facts": ["F1"]}
    empty = {"reservations": [], "pending_add": [], "pending_remove": []}
    m = assemble.merge_judgments({"articles": arts[:1], "pending": []}, [arts[:1], arts[:1]], {"digest": []},
                                 [dict(empty, stories=[bad_row]), dict(empty, stories=[good_row])])
    check(m["stories"] == [good_row], f"やり直しの正しい行より崩れた行を採った: {m['stories']}")
    # 形の崩れた台帳の答え(reservations が数値・pending_remove が辞書)は、まとめに使わない
    m = assemble.merge_judgments({"articles": arts[:1], "pending": [{"dedup_key": "gaku-event-2026"}]}, [arts[:1], arts[:1]], {"digest": []},
                                 [{"stories": [good_row], "reservations": 1, "pending_add": [], "pending_remove": {"gaku-event-2026": "x"}},
                                  dict(empty, stories=[good_row])])
    check(m["pending_remove"] == [] and m["stories"] == [good_row], f"崩れた台帳の答えをまとめに使った: {m}")
    check(collect.unaccounted_posts("### 1\n- url:\n- 投稿者: a\n- 本文: 告知\n", [{"url": "-", "status": "none"}], []) != [],
          "空の url の行の次の行を url と読んで済みにした")
    nohdr = "- url: https://x.com/a/status/1\n- 本文: A\n\n- url:\n- 投稿者: b\n- 本文: Bの告知\n"
    left_nh = collect.unaccounted_posts(nohdr, [{"url": "https://x.com/a/status/1", "status": "none", "item": ""}], [])
    check(len(left_nh) == 1 and "Bの告知" in left_nh[0], f"空の url の行の投稿が前の投稿に吸われた: {left_nh}")
    # 付け直しも同じ区切り: url の無い投稿のリンクを前の投稿へ帰属させない
    vid = "- url: https://x.com/a/status/1\n- 本文: A\n\n- url:\n- 本文: B\n- リンク:\n  - https://www.youtube.com/watch?v=b\n"
    saved_cs = collect.classify_source
    try:
        collect.classify_source = lambda u: "未確認" if "youtube" in u else saved_cs(u)
        rp = collect.repoint_to_post([{"url": "https://www.youtube.com/watch?v=b", "facts": []}], vid)
    finally:
        collect.classify_source = saved_cs
    check(rp[0]["url"] == "https://www.youtube.com/watch?v=b", f"url の無い投稿のリンクを前の投稿へ付け直した: {rp}")
    # 拾い直しの答えが null・崩れた要素でも号を止めない(取りこぼしのまま残す)
    # (テスト用のディレクトリで動かす。本体の metrics に書かない)
    saved_rp = (compose.run_group, compose.prompt_file, compose.save_raw, compose.missing_plan_prompt, compose.ROOT)
    def replan_with(ans: str, keys: list[str]) -> tuple[list, dict]:
        try:
            compose.ROOT = tmp / "rp"
            (compose.ROOT / "metrics").mkdir(parents=True, exist_ok=True)
            def fake_run(cmd, **kw):
                (compose.ROOT / "metrics" / "plan-2099-01-01-missing.json").write_text(ans, encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            compose.run_group, compose.prompt_file = fake_run, (lambda d, n, p, base=None: "p")
            compose.save_raw, compose.missing_plan_prompt = (lambda *a, **kw: None), (lambda *a, **kw: "")
            pl = {"articles": [{"slug": "cg-old", "brand": "cg", "candidate_ids": ["c0"]}]}
            cands_t = {f"c-{k}": {"id": f"c-{k}"} for k in keys} | {"c0": {"id": "c0"}}
            return compose.replan_missing("2099-01-01", pl, {"cg": [{"dedup_key": k, "ids": [f"c-{k}"]} for k in keys]}, cands_t, keys), pl
        finally:
            compose.run_group, compose.prompt_file, compose.save_raw, compose.missing_plan_prompt, compose.ROOT = saved_rp
    art = lambda k, **kw: dict({"slug": "cg_new", "brand": "cg", "dedup_key": k, "candidate_ids": ["c"], "rank": "small", "angle": "",
                                "purpose": "新情報", "lead_score": 0}, **kw)
    for ans in ("null", '{"articles": [null], "dropped": [{"dedup_key": ["x"]}]}', '{"dropped": [{"dedup_key": "k"}]}',
                '{"dropped": [{"dedup_key": "k", "brand": "cg", "reason": " "}]}', json.dumps({"articles": [art("k", rank=["small"])]})):
        res, pl = replan_with(ans, ["k"])
        check(isinstance(res, list) and len(pl["articles"]) == 1 and not pl.get("dropped"),
              f"拾い直しの崩れた答えで止まった・崩れた要素を反映した(主題は取りこぼしのまま残すべき): {ans} {pl}")
    # 同じ主題に崩れた記事化と正しい不採用 → 矛盾した答えとして反映しない
    for ans in (json.dumps({"articles": [art("k", rank=["small"])], "dropped": [{"dedup_key": "k", "brand": "cg", "reason": "既報"}]}),):
        res, pl = replan_with(ans, ["k"])
        check(len(pl["articles"]) == 1 and not pl.get("dropped"),
              f"拾い直しの崩れた答えで止まった・崩れた要素を反映した(主題は取りこぼしのまま残すべき): {ans} {pl}")
    # 見本どおりの要素は反映し、slug と candidate_ids はコードが付ける(モデルの写した id・崩れた slug は使わない)。
    # 崩れた要素・答えが2つある主題だけ反映しない
    res, pl = replan_with(json.dumps({"articles": [art("k", candidate_ids=["c-k3"]), art("k2", rank=["small"]), art("k4"), art("k4")],
                                      "merge_into": [{"slug": "cg-old", "dedup_key": "k5"}],
                                      "dropped": [{"dedup_key": "k3", "brand": "cg", "reason": "既報", "note": ""},
                                                  {"dedup_key": "k5", "brand": "cg", "reason": "既報"}]}), ["k", "k2", "k3", "k4", "k5"])
    new = [a for a in pl["articles"] if a["slug"] != "cg-old"]
    check([(a["dedup_key"], a["candidate_ids"]) for a in new] == [("k", ["c-k"])] and re.fullmatch(r"[a-z0-9-]+", new[0]["slug"])
          and [d["dedup_key"] for d in pl.get("dropped") or []] == ["k3"] and pl["articles"][0]["candidate_ids"] == ["c0"],
          f"拾い直しの反映(id はコードが展開・答えが2つの主題は反映しない): {pl}")
    # 台帳の答えは出力 schema で検める(要素の欄の型・日付の形が崩れていれば使わない)
    bad_res = dict(empty, stories=[good_row], reservations=[{"slug": "s0", "candidate_id": "c1", "date": 20261010,
                                                             "kind": "締切", "subject": "x", "note": ""}])
    check(not assemble.ledger_out_ok(bad_res) and assemble.ledger_out_ok(dict(empty, stories=[good_row])),
          "要素の崩れた台帳の答えを通した・正しい答えを落とした")
    # 書き直しの答えの欄の形が崩れていても(addressed_issue_ids が数値)、号を止めずに元の稿のまま
    saved = (renderlib.check_output, compose.parse_front_matter)
    try:
        renderlib.check_output = lambda *a, **kw: []
        compose.parse_front_matter = lambda p: {"sources": []}
        pth = tmp / "x.md"
        pth.write_text("---\nslug: s\n---\n本文\n", encoding="utf-8")
        res = compose.revise_apply("2026-10-06", {"slug": "s"}, pth, {"status": "ok", "addressed_issue_ids": 1, "blocks": [], "sources": []},
                                   {}, [], [{"issue_id": "I1", "rule_id": "R1", "repair": "rewrite_claim", "quote": "本文"}])
    finally:
        renderlib.check_output, compose.parse_front_matter = saved
    check(res[0] == "kept", f"欄の形が崩れた書き直しの答えで止まった・適用した: {res}")
    # 5. 探索の写し: 出典の本文に無ければ URL の取り違え(形の照合。言い換え・空白・全角半角は許す)
    page = "シャイニーカラーズ Master ShowPiece の公演グッズの事後通販が10月10日から始まります。"
    check(pipelib.quote_on_page("Master ShowPiece の公演グッズの事後通販が、10月10日から始まります", page) is True, "写しの揺れを許さない")
    check(pipelib.quote_on_page("ノクチル の新曲「いかないで」が配信開始されました", page) is False, "取り違えた URL の写しを通した")
    check(pipelib.quote_on_page("短い", page) is None, "短すぎる写しで判定した")
    # 共通の前置きで断片が一致しても、写し全体が無ければ「ある」にしない(confirmed にしない)
    pg2 = "アイドルマスターシャイニーカラーズのイベント開催情報について、Master ShowPieceの開催をお知らせします。"
    check(pipelib.quote_on_page("アイドルマスターシャイニーカラーズのイベント開催情報について、ノクチルの単独公演をお知らせします。", pg2) is not True,
          "共通の前置きだけの一致を「写しがある」とした")
    # 同じ URL に正しい候補と取り違えた候補が来ても、正しい候補は failed にならない(照合の前に混ぜない・日別ファイルで混ぜない)
    u = "https://idolmaster-official.jp/news/01_19898.html"
    got = collect.normalize([{"url": u, "title": "グッズ", "facts": ["事後通販"], "quote": "公演グッズの事後通販が10月10日から始まります", "_via": "explore"},
                             {"url": u, "title": "新曲", "facts": ["配信"], "quote": "ノクチル の新曲「いかないで」が配信開始されました", "_via": "explore"}])
    check(len(got) == 2 and [c.get("quotes") for c in got] == [["公演グッズの事後通販が10月10日から始まります"],
                                                               ["ノクチル の新曲「いかないで」が配信開始されました"]],
          f"写しのある同じ URL の候補を照合の前に混ぜた: {got}")
    saved = (collect.urllib.request.urlopen, collect.fetch_rendered, collect.ROOT)
    class R:
        status = 200
        headers = type("H", (), {"get_content_charset": lambda self: "utf-8"})()
        def read(self, n=None): return ("<p>" + page + "</p>" + "あ" * 500).encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False
    try:
        collect.urllib.request.urlopen = lambda req, timeout=15: R()
        collect.fetch_rendered = lambda url: None
        collect.ROOT = tmp / "repo"
        (tmp / "repo" / "candidates").mkdir(parents=True, exist_ok=True)
        collect.verify(got)
        collect.merge_into_day_file(list(reversed(got)), "2026-10-06")
        day = json.loads((tmp / "repo" / "candidates" / "2026-10-06.json").read_text(encoding="utf-8"))
    finally:
        collect.urllib.request.urlopen, collect.fetch_rendered, collect.ROOT = saved
    check([c["verify"] for c in got] != ["failed", "failed"] and got[1]["verify"] == "failed" and "取り違え" in got[1]["verify_note"],
          f"写しの照合: {[(c['verify'], c.get('verify_note')) for c in got]}")
    check(len(day) == 1 and day[0]["title"] == "グッズ" and day[0]["verify"] != "failed", f"取り違えた候補が正しい候補を潰した: {day}")
    # 写しを付けた候補が日別ファイルの schema を通る(欄を足して schema を直さず、lint で収集が止まった。2026-10-07)
    from jsonschema import Draft202012Validator
    cv = Draft202012Validator(json.loads((Path(__file__).resolve().parent.parent / "schema" / "candidates.schema.json").read_text(encoding="utf-8")))
    errs = [e.message for e in cv.iter_errors(day + got)]
    check(errs == [], f"収集した候補が candidates.schema.json に通らない: {errs[:3]}")


def test_accept_only_normal_and_schema(tmp: Path):
    """モデルの答えは、正常に終わったセッションのものを、渡した schema で検めてから使う(2026-10-06 の洗い出し 15巡目)。"""
    import collect, compose, assemble, oncall
    tmp.mkdir(parents=True, exist_ok=True)
    # 重複キー・schema
    try:
        pipelib.loads_strict('{"a": 1, "a": 2}')
        check(False, "重複キーの JSON を読んだ")
    except ValueError:
        pass
    # 途中切れの外側配列から内側の配列を拾わない
    check(pipelib.extract_json_array_strict('[{"url":"https://example.com/a","facts":[],"title":') is None, "途中切れの内側の [] を0件と読んだ")
    check(pipelib.extract_json_array_strict('前置き [{"a": 1}] 後置き') == [{"a": 1}], "前後に地の文のある配列を読めない")
    # 投稿ごとの結果の重複・矛盾は済みにしない / 見出しの番号が崩れた投稿・題だけの見出し
    one = "### 1\n- url: https://x.com/a/status/1\n- 本文: A\n"
    check(collect.unaccounted_posts(one, [{"url": "https://x.com/a/status/1", "status": "none", "item": ""},
                                          {"url": "https://x.com/a/status/1", "status": "unreadable"}], []) != [], "矛盾した結果で済みにした")
    odd = "# 765 面のまとめ\n\n" + one + "\n### 投稿2\n- 投稿者: b\n- 本文: B\n"
    left = collect.unaccounted_posts(odd, [{"url": "https://x.com/a/status/1", "status": "none", "item": ""}], [])
    check(len(left) == 1 and "本文: B" in left[0], f"番号で始まらない見出しの投稿が消えた・題の見出しを投稿にした: {left}")
    # 候補の要素が崩れていれば処理済みの根拠にしない(facts の要素が1つ崩れた)
    check(not collect.candidate_usable({"url": "https://example.com/a", "facts": ["残る", {"text": "失う"}]}), "崩れた facts の候補で処理済みにした")
    check(collect.candidate_usable({"url": "https://example.com/a", "facts": ["f"], "event_date": None}), "null の日付を崩れとみなした")
    # 写しの無い探索の候補は confirmed にしない
    page = "<p>" + "シャイニーカラーズの公演グッズの事後通販が10月10日から始まります。" + "あ" * 500 + "</p>"
    class R:
        status = 200
        headers = type("H", (), {"get_content_charset": lambda self: "utf-8"})()
        def read(self, n=None): return page.encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False
    saved = (collect.urllib.request.urlopen, collect.fetch_rendered, collect.classify_source)
    try:
        collect.urllib.request.urlopen = lambda req, timeout=15: R()
        collect.fetch_rendered = lambda url: None
        collect.classify_source = lambda u: "公式"
        cs = collect.normalize([{"url": "https://idolmaster-official.jp/news/a", "title": "t", "facts": ["f"], "_via": "explore"},
                                {"url": "https://idolmaster-official.jp/news/b", "title": "t", "facts": ["f"], "_via": "explore",
                                 "quote": "公演グッズの事後通販が10月10日から始まります"}])
        collect.verify(cs)
    finally:
        collect.urllib.request.urlopen, collect.fetch_rendered, collect.classify_source = saved
    check([c["verify"] for c in cs] == ["unconfirmed", "confirmed"], f"写しの無い候補を confirmed にした: {[(c['verify'], c.get('verify_note')) for c in cs]}")
    # 一面選定: schema どおりでない答えはフォールバックへ
    saved = (compose.run_group, compose.prompt_file, compose.save_raw, compose.ROOT)
    try:
        compose.ROOT = tmp / "lead"
        (compose.ROOT / "metrics").mkdir(parents=True, exist_ok=True)
        compose.run_group = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, '{"lead_slug": []}', "")
        compose.prompt_file, compose.save_raw = (lambda d, n, p, base=None: "p"), (lambda *a, **kw: None)
        pl = {"articles": [{"slug": "a", "rank": "small", "lead_score": 5}, {"slug": "b", "rank": "small", "lead_score": 9}]}
        compose.pick_lead("2026-10-06", pl)
    finally:
        compose.run_group, compose.prompt_file, compose.save_raw, compose.ROOT = saved
    check([a["slug"] for a in pl["articles"] if a["rank"] == "lead"] == ["b"], f"崩れた一面の答えで止まった・フォールバックしない: {pl}")
    # 当番・監査: schema どおりでない答えは使わない
    sf = tmp / "s.json"
    sf.write_text(json.dumps({"type": "object", "required": ["status", "editor_summary"]}), encoding="utf-8")
    saved = (oncall.run_group, oncall.prompt_file, oncall.save_raw)
    try:
        oncall.run_group = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, '{"status": "no_fix_needed"}', "")
        oncall.prompt_file, oncall.save_raw = (lambda d, n, p, base=None: "p"), (lambda *a, **kw: None)
        try:
            oncall.run_claude("x", sf, tmp)
            check(False, "schema の欠けた当番の答えを使った")
        except RuntimeError:
            pass
    finally:
        oncall.run_group, oncall.prompt_file, oncall.save_raw = saved
    # 16巡目: 重複キーは入口ごとに読めない答え / 真偽値の番号 / 日付の形 / item の契約 / 前置きの投稿 / Grok の書きかけ
    (tmp / "dup.json").write_text('[{"url": "u", "status": "unreadable", "status": "none", "item": ""}]', encoding="utf-8")
    check(collect.read_json_list(tmp / "dup.json") is None, "重複キーのファイルを読んだ")
    check(oncall.parse_json('{"verdict": "reject", "verdict": "approve"}') is None, "重複キーの監査の答えを読んだ")
    check(collect.watch_page_results([{"page": True, "status": "none", "items": []}], 1) == {}, "真偽値の番号でページを既読にした")
    check(collect.watch_page_results(pipelib.extract_json_array_strict('[{"page":1,"status":"unreadable","status":"none","items":[]}]'), 1) == {},
          "重複キーのページ結果で既読にした")
    check(not collect.candidate_usable({"url": "https://example.com/a", "facts": ["申込"], "deadline": "2026/10/10"}), "形の崩れた締切で処理済みにした")
    check(collect.unaccounted_posts(one, [{"url": "https://x.com/a/status/1", "status": "none", "item": ["x"]}], []) != [], "item の崩れた結果で済みにした")
    pre = "- 投稿者: a\n- 本文: A(見出しも url の行も無い)\n\n- url: https://x.com/b/status/2\n- 本文: B\n"
    lp = collect.unaccounted_posts(pre, [{"url": "https://x.com/b/status/2", "status": "none", "item": ""}], [])
    check(len(lp) == 1 and "本文: A" in lp[0], f"前置きの投稿が次の投稿に吸われた: {lp}")
    gd = tmp / "g"
    gd.mkdir(exist_ok=True)
    (gd / "prompt-cg.md").write_text("p", encoding="utf-8")
    class GP:
        pid, returncode = 0, 1
        def communicate(self, timeout=None):
            (gd / "cg.md").write_text("### 1\n- url: https://x.com/a/status/1\n", encoding="utf-8")
            return "", "boom"
    saved = (collect.subprocess.Popen, collect.save_raw)
    try:
        collect.subprocess.Popen, collect.save_raw = (lambda *a, **kw: GP()), (lambda *a, **kw: None)
        errs = {}
        collect.run_grok_prompts([({"key": "cg"}, gd / "prompt-cg.md")], errs)
    finally:
        collect.subprocess.Popen, collect.save_raw = saved
    check(not (gd / "cg.md").exists() and (gd / "cg.partial-1.md").exists() and "cg" in errs, "異常終了した Grok の書きかけを答えにした")
    # やり直しも異常終了: 初回の途中出力を上書きしない(試みごとに退ける)
    saved = (collect.subprocess.Popen, collect.save_raw)
    try:
        collect.subprocess.Popen, collect.save_raw = (lambda *a, **kw: GP()), (lambda *a, **kw: None)
        collect.run_grok_prompts([({"key": "cg"}, gd / "prompt-cg.md")], errs)
    finally:
        collect.subprocess.Popen, collect.save_raw = saved
    check((gd / "cg.partial-1.md").exists() and (gd / "cg.partial-2.md").exists(), "やり直しが初回の途中出力を上書きした")
    # 17巡目: 空白だけの事実 / 分類の崩れた2つ目の答え / 同じ追跡事項に2行 / ディレクトリになった出力 / 書き出す前の失敗
    check(not collect.candidate_usable({"url": "https://example.com/a", "facts": [" \t"]}), "空白だけの事実で処理済みにした")
    import classify_sources as csrc
    site = {"host": "example.com", "operator": "公式", "type": "公式", "why": "w"}
    check(csrc._by_host([site, {"host": "example.com", "type": "ファン", "why": "w"}], ["example.com"], csrc.ANSWER_SITE) == {},
          "崩れた2つ目の答えを捨てて矛盾を見逃した")
    art_p = [{"slug": "s0", "dedup_key": "k0", "title": "t"}]
    two_p = {"stories": [{"slug": "s0", "story_id": "k0", "subject": "x", "published_facts": ["F1"]}], "reservations": [], "pending_remove": [],
             "pending_add": [{"dedup_key": "k0", "brand": "cg", "subject": "s", "watch": "Aの日程"},
                             {"dedup_key": "k0", "brand": "cg", "subject": "s", "watch": "Bの日程"}]}
    check(assemble.uncovered_articles([art_p], [two_p]) == art_p
          and assemble.merge_judgments({"articles": art_p, "pending": []}, [art_p], {"digest": []}, [two_p])["pending_add"] == [],
          "同じ追跡事項の2行の片方を黙って採った")
    (tmp / "dirout").mkdir(exist_ok=True)
    check((pipelib.read_for_raw(tmp / "dirout") or "").startswith("(読めない"), "ディレクトリの出力で止まる")
    class GQ:
        pid, returncode = 0, 1
        def communicate(self, timeout=None): return "", "auth error"
    saved = (collect.subprocess.Popen, collect.save_raw)
    try:
        collect.subprocess.Popen, collect.save_raw = (lambda *a, **kw: GQ()), (lambda *a, **kw: None)
        (gd / "prompt-cg-deep.md").write_text("p", encoding="utf-8")
        failed = collect.run_grok_prompts([({"key": "cg"}, gd / "prompt-cg-deep.md")], {})
    finally:
        collect.subprocess.Popen, collect.save_raw = saved
    check(failed == {"cg"}, f"書き出す前に落ちた深掘りを失敗にしない: {failed}")
    # 18巡目: 議論の2巡目の無回答は1巡目の答えを残さない / 無効な追加の key の削除は採らない / ディレクトリの書き出し
    rounds = iter([[site], [dict(site, type="ファン")], [site, {"host": "example.com", "type": "ファン", "why": "w"}], [site]])
    saved_ask = csrc.ask
    try:
        csrc.ask = lambda cmd, prompt, timeout=900: next(rounds)
        ag, sp = csrc.consensus("p", ["example.com"])
    finally:
        csrc.ask = saved_ask
    check("example.com" not in ag, f"2巡目に崩れた答えを出した側の1巡目の答えで一致にした: {ag}")
    two_r = dict(two_p, pending_remove=["k0"])
    check(assemble.merge_judgments({"articles": art_p, "pending": [{"dedup_key": "k0"}]}, [art_p], {"digest": []}, [two_r])["pending_remove"] == [],
          "追加を捨てた追跡事項の削除だけを採った")
    (gd / "dirface.md").mkdir(exist_ok=True)
    check(not collect.grok_wrote(gd, {"key": "dirface"}), "ディレクトリの書き出しで止まる・書いたことにした")
    # 20巡目: 書きかけを退けられずに残った面(非空の <key>.md・異常終了)を完了とみなさない。やり直し、名指しで異常にする
    bd = tmp / "gb"
    bd.mkdir(exist_ok=True)
    calls_b, notes_b = [], []
    def fake_faces(queries, outdir, errs, retry=False):
        calls_b.append(([q["key"] for q in queries], retry))
        for q in queries:
            (outdir / f"{q['key']}.md").write_text("### 1\n- url: https://x.com/a/status/1\n", encoding="utf-8")
            errs[q["key"]] = "書きかけを退けられない(PermissionError)"
        return {q["key"] for q in queries}
    saved = (collect.run_grok_faces, collect.notify, collect.grok_session_error)
    try:
        collect.run_grok_faces = fake_faces
        collect.notify = lambda job, msg, ok=True, require=False: notes_b.append((msg, ok)) or True
        collect.grok_session_error = lambda since_s=7200: ""
        still_b = collect.grok_basic([{"key": "cg"}], bd, {})
    finally:
        collect.run_grok_faces, collect.notify, collect.grok_session_error = saved
    check(calls_b == [(["cg"], False), (["cg"], True)] and still_b == ["cg"]
          and any(not ok and "cg" in m and "退けられなかった書きかけ" in m for m, ok in notes_b),
          f"退けられなかった書きかけの面を完了とみなした: {calls_b} {still_b} {notes_b}")
    # 21巡目: 退けられなかった書きかけは、次の試みが新しく書かずに正常終了しても完了にしない。新しく書けば完了
    sd = tmp / "stuck"
    sd.mkdir(exist_ok=True)
    (sd / "prompt-cg.md").write_text("p", encoding="utf-8")
    seq = iter([(1, "書きかけ"), (0, None), (0, "### 1\n- url: https://x.com/b/status/2\n")])
    class SP:
        pid = 0
        def __init__(self):
            self.returncode, text = next(seq)
            if text is not None:
                (sd / "cg.md").write_text(text, encoding="utf-8")
        def communicate(self, timeout=None): return "", ""
    saved = (collect.subprocess.Popen, collect.save_raw, Path.replace)
    try:
        collect.subprocess.Popen, collect.save_raw = (lambda *a, **kw: SP()), (lambda *a, **kw: None)
        def deny(self, target):
            raise PermissionError("denied")
        Path.replace = deny
        collect.run_grok_prompts([({"key": "cg"}, sd / "prompt-cg.md")], {})       # 異常終了・退けられない
        Path.replace = saved[2]
        collect.run_grok_prompts([({"key": "cg"}, sd / "prompt-cg.md")], {})       # 正常終了・新しく書かない
        mid = collect.grok_wrote(sd, {"key": "cg"})
        collect.run_grok_prompts([({"key": "cg"}, sd / "prompt-cg.md")], {})       # 正常終了・新しく書いた
        after = collect.grok_wrote(sd, {"key": "cg"})
    finally:
        collect.subprocess.Popen, collect.save_raw, Path.replace = saved
        collect.STUCK_OUTPUTS.clear()
    check(mid is False and after is True, f"退けられなかった書きかけの扱い: 書かずに正常終了={mid} 新しく書いた={after}")
    # 台帳ファイル: 空のファイルは0件にしない
    (tmp / "empty.yml").write_text("", encoding="utf-8")
    try:
        assemble.load_yaml_list(tmp / "empty.yml")
        check(False, "空の台帳ファイルを0件として読んだ")
    except ValueError:
        pass
    assemble.dump_yaml(tmp / "zero.yml", [])
    check(assemble.load_yaml_list(tmp / "zero.yml") == [], "0件の台帳を書いて読めない")


def test_grok_item_url_repointed_to_post():
    """X の投稿に貼られた動画の url が候補の url になっていたら、貼った投稿(公式を先)の url に付け直す
    (2026-10-05: 公式 X のコラボ動画告知が YouTube の url で未確認の候補になり、執筆が出典不一致で見送った)。"""
    import collect
    posts = """## 16
- url: https://x.com/KamizuruCosmo/status/2106670674232123736
- 本文:
「クライヤ」コラボ
- リンク:
  - https://www.youtube.com/shorts/YvB9Lkq5aOs

## 5
- url: https://x.com/valiv_official/status/2106670673922064689
- 本文:
クライヤ 踊ってみた with 四条貴音さん
- リンク:
  - https://www.youtube.com/shorts/YvB9Lkq5aOs
  - https://www.tiktok.com/@valiv_official/video/7692091940719906055
"""
    saved = collect.classify_source
    try:
        # 本人のアカウントは公式でない扱いにして、公式の投稿が選ばれることを確かめる。
        # 動画の種別は表に載ると変わる(2026-10-05 の発行でこの動画が公式に載り赤になった)ので、未確認に固定する
        collect.classify_source = lambda u: ("当事者" if "KamizuruCosmo" in u
                                             else "未確認" if "youtube.com" in u else saved(u))
        items = collect.repoint_to_post([
            {"url": "https://www.youtube.com/shorts/YvB9Lkq5aOs", "facts": ["クライヤ"]},
            {"url": "https://www.youtube.com/shorts/other", "facts": []},
            {"url": "https://x.com/a/status/1", "facts": []},
        ], posts)
    finally:
        collect.classify_source = saved
    check(items[0]["url"] == "https://x.com/valiv_official/status/2106670673922064689"
          and items[0]["facts"] == ["クライヤ", "リンク: https://www.youtube.com/shorts/YvB9Lkq5aOs",
                                    "リンク: https://www.tiktok.com/@valiv_official/video/7692091940719906055"],
          f"動画の url を貼った公式の投稿に付け直さない: {items[0]}")
    check(items[1]["url"] == "https://www.youtube.com/shorts/other" and items[2]["url"] == "https://x.com/a/status/1",
          f"投稿に無い url・X の url を書き換えた: {items[1:]}")


def test_grok_post_links_copied_to_facts():
    """投稿を出典にする候補に、投稿に貼られたリンクを facts(「リンク: <url>」)へ写す(2026-10-17号: 写さなかったので、執筆は
    投稿のページの途中で切れた表示 youtube.com/live/E0d4UT59x… から別の動画の url を作って出典にし、既報の照合も配信ページで
    前日の記事に届かなかった)。同じ投稿は x.com/i/status/<ID> の形でも引く。既にあるリンクは重ねない。リンクの無い投稿は足さない。"""
    import collect
    posts = """### 16
- url: https://x.com/imas_ch/status/2110658707336142874
- 本文:
メンバー限定ラジオ
- リンク:
  - https://www.youtube.com/live/E0d4UT59xM4
  - https://www.youtube.com/@imas-official/join

### 17
- url: https://x.com/animatekasihara/status/2110958095530918280
- 本文:
フェア
- リンク: なし
"""
    items = collect.repoint_to_post([
        {"url": "https://x.com/i/status/2110658707336142874", "facts": ["配信", "リンク: https://www.youtube.com/@imas-official/join"]},
        {"url": "https://x.com/animatekasihara/status/2110958095530918280", "facts": ["フェア"]},
    ], posts)
    check(items[0]["facts"] == ["配信", "リンク: https://www.youtube.com/@imas-official/join", "リンク: https://www.youtube.com/live/E0d4UT59xM4"],
          f"投稿に貼られたリンクを facts に写さない・重ねた: {items[0]['facts']}")
    check(items[1]["facts"] == ["フェア"], f"リンクの無い投稿に facts を足した: {items[1]['facts']}")


def test_rebuild_backfills_post_links(tmp: Path):
    """号を作り直す前に、写す前に収集した素材の投稿の候補へ、保存済みの投稿の原本(生の出力)に貼られたリンクを写し、素材を commit に
    含める(監査指摘 R3: 2026-10-17号のラジオの候補は配信ページを持たないまま作り直しに渡り、前日の既報につながらなかった)。冪等。"""
    import collect
    import oncall
    d = "2026-10-17"
    (tmp / "candidates").mkdir(parents=True, exist_ok=True)
    raw = tmp / "metrics" / "work" / d / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    cands = [
        {"id": "r", "title": "ラジオ", "brand": "general", "source_type": "公式", "url": "https://x.com/imas_ch/status/2110658707336142874",
         "found_at": "2026-10-17T02:25:32+09:00", "dedup_key": "imas-channel-member-radio-2026-10", "verify": "confirmed", "facts": ["冒頭15分は誰でも視聴できる。"]},
        {"id": "f", "title": "フェア", "brand": "sidem", "source_type": "当事者", "url": "https://x.com/animatekasihara/status/2110958095530918280",
         "found_at": "2026-10-17T02:25:32+09:00", "dedup_key": "fair", "verify": "confirmed", "facts": ["フェア"]},
    ]
    (tmp / "candidates" / f"{d}.json").write_text(json.dumps(cands, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (raw / "grok-verify-joint-other.txt").write_text("""# at: 2026-10-17T02:18:57+09:00
# exit: 0
# stdout

# stderr

# file: x-posts.md
### 16
- url: https://x.com/imas_ch/status/2110658707336142874
- 本文:
メンバー限定ラジオ
- リンク:
  - https://www.youtube.com/live/E0d4UT59xM4

### 17
- url: https://x.com/animatekasihara/status/2110958095530918280
- 本文:
フェア
- リンク: なし
# file: items.json
[{"url": "https://x.com/other/status/1"}]
# file: posts.json
[]
""", encoding="utf-8")
    calls = []
    saved = (oncall.ROOT, oncall.backup_edition, oncall.rollback_in_subprocess, oncall.commit_paths)
    try:
        oncall.ROOT = tmp
        oncall.backup_edition = lambda date, edition: "backup/x"
        oncall.rollback_in_subprocess = lambda date: []
        oncall.commit_paths = lambda paths, msg, branch, **k: calls.append(paths)
        check(oncall.reset_edition(d, f"edition/{d}") == "backup/x", "作り直しの準備が控えを返さない")
    finally:
        oncall.ROOT, oncall.backup_edition, oncall.rollback_in_subprocess, oncall.commit_paths = saved
    got = {c["id"]: c["facts"] for c in json.loads((tmp / "candidates" / f"{d}.json").read_text(encoding="utf-8"))}
    check(got == {"r": ["冒頭15分は誰でも視聴できる。", "リンク: https://www.youtube.com/live/E0d4UT59xM4"], "f": ["フェア"]},
          f"作り直しの前に、保存済みの投稿の原本のリンクを素材へ写さない: {got}")
    check(calls and f"candidates/{d}.json" in calls[0], f"写した素材を作り直しの commit に含めない: {calls}")
    check(collect.backfill_post_links(d, tmp) == 0, "2回目にも素材を書き換えた(冪等でない)")
    check(collect.backfill_post_links("2026-10-18", tmp) == 0, "素材・原本の無い号で 0 を返さない")


def test_grok_repoint_uses_each_face_posts(tmp: Path):
    """付け直しには、その面の投稿を渡す(最後の面の投稿を全部の面に渡していた。監査指摘: 末尾に「なし」の面があると補正されない)。"""
    import collect
    tmp.mkdir(parents=True, exist_ok=True)
    out = tmp / "g"
    out.mkdir()
    (out / "dsva.md").write_text("""## 5
- url: https://x.com/valiv_official/status/2106670673922064689
- 本文:
クライヤ 踊ってみた with 四条貴音さん
- リンク:
  - https://www.youtube.com/shorts/YvB9Lkq5aOs
""", encoding="utf-8")
    (out / "joint.md").write_text("## 1\n- url: https://x.com/imas_official/status/1\n- 本文:\n告知\n", encoding="utf-8")
    (out / "trend.md").write_text("なし", encoding="utf-8")
    qs = [{"key": k, "brand": k, "topic": "t"} for k in ("dsva", "joint", "trend")]
    class P:
        pid = 0
        returncode = 0
        def wait(self, timeout=None): return 0
        def communicate(self, timeout=None): return None, None
    def fake_popen(args, cwd=None, **kw):
        items = ([{"url": "https://www.youtube.com/shorts/YvB9Lkq5aOs", "brand": "dsva", "facts": ["クライヤ"]}]
                 if "dsva" in Path(cwd).name else [])
        (Path(cwd) / "items.json").write_text(json.dumps(items), encoding="utf-8")
        (Path(cwd) / "deep.json").write_text("[]", encoding="utf-8")
        urls = re.findall(r"(?m)^- url:\s*(\S+)", (Path(cwd) / "x-posts.md").read_text(encoding="utf-8"))
        (Path(cwd) / "posts.json").write_text(json.dumps([{"url": u, "status": "extracted" if items else "none",
                                                          "item": items[0]["url"] if items else ""} for u in urls]), encoding="utf-8")
        return P()
    saved = (collect.subprocess.Popen, collect.notify, collect.prompt_file, collect.classify_source)
    try:
        collect.subprocess.Popen = fake_popen
        collect.notify = lambda job, msg, ok=True, require=False: True
        collect.prompt_file = lambda date, name, prompt, base=None: "p"
        # 動画の種別は表に載ると変わるので、未確認に固定する
        collect.classify_source = lambda u, c=collect.classify_source: "未確認" if "youtube.com" in u else c(u)
        items, _ = collect.verify_grok_faces(qs, out)
    finally:
        collect.subprocess.Popen, collect.notify, collect.prompt_file, collect.classify_source = saved
    check([x["url"] for x in items] == ["https://x.com/valiv_official/status/2106670673922064689"],
          f"末尾以外の面の動画 url を、その面の投稿に付け直さない: {items}")


def test_collect_health():
    """新規0件のとき、全系統の取得が正常なら異常にしない。取得に失敗した系統があれば内訳を返す(当番の指摘 3e0904d4fe)。"""
    import collect
    ok_info = {"stats": {"imas-portal": {"found": 12, "new": 0}}, "deferred": 0}
    check(collect.collect_health(ok_info, {"explore:765as": 0, "grok:765as": 0}) == [], "正常な空振りを異常にした")
    bad = collect.collect_health({"stats": {"imas-portal": {"error": "timeout"}}, "deferred": 2}, {"explore_failed:sidem": 1, "explore:sidem": 0})
    check(len(bad) == 3 and any("imas-portal" in b for b in bad) and any("2件" in b for b in bad) and any("sidem" in b for b in bad),
          f"取得の失敗の内訳: {bad}")


def test_grok_format_matches_parser():
    """Grok に指示する書き出しの形を、収集のコード(投稿への付け直し)がそのまま読めること。依頼文と読み取りがずれると、
    動画などのリンクで候補になったものを公式の投稿へ付け直せなくなる(当番の指摘 2b3c2a884e)。"""
    import collect
    p = collect.write_grok_prompt(Path(tempfile.mkdtemp()), {"key": "k", "brand": "765", "topic": "t", "accounts": ["a"]}).read_text(encoding="utf-8")
    sample = p.split("```")[1].replace("<アカウント>/status/<ID>", "a/status/1").replace(
        "  - 投稿に付いたリンクの url(1行に1つ。無ければ「- リンク: なし」)", "  - https://youtube.com/watch?v=x")
    got = collect.repoint_to_post([{"url": "https://youtube.com/watch?v=x", "facts": []}], sample)
    check(got[0]["url"] == "https://x.com/a/status/1", f"依頼文の見本の形を付け直しが読めない: {got}")
    # 公式アカウントは、道具の上限(10件)まで返ったら until_time で遡る。「1回だけ」と縛ると、忙しい日の古い投稿を落とす
    # (2026-10-10: 学マス公式の3件がどの回の収集にも入らなかった)。遡りは別枠で上限があり、やらないことと矛盾しない
    check(f"limit {collect.X_SEARCH_LIMIT}" in p and "until_time:" in p and "date -u -d" in p
          and f"最大 {collect.GROK_OFFICIAL_PAGES}回" in p and "を**1回だけ**渡す" not in p,
          "公式アカウントの検索が、上限まで返ったときに遡らない")
    check("(1 の公式アカウントの遡りは別)" in p and "遡りはこれとは別枠" in p, "検索の上限・やらないことが遡りと食い違う")
    check("`until_time:<その秒>` で進む" in p and "同じ url の投稿は1回だけ書く" in p,
          "遡りの境界が進まないとき・ページの重なりの扱いが無い")
    # ページの境目で重なった投稿は、確かめに渡す前にコードが1つにする(結果が同じ url に2つ返ると未処理になる。監査指摘)
    blk = lambda n, body: f"### {n}\n- url: https://x.com/a/status/{n}\n- 本文:\n{body}"
    dup = "\n\n".join([blk(1, "一"), blk(2, "二"), blk(2, "二"), blk(3, "三")])
    got = collect.dedupe_posts(dup)
    check([collect.post_url(b) for b in collect.post_blocks(got)] == [f"https://x.com/a/status/{n}" for n in (1, 2, 3)],
          f"重なった投稿を1つにしない: {got}")
    plain = "\n\n".join([blk(1, "一"), blk(2, "二")])
    check(collect.dedupe_posts(plain) == plain, "重なりの無い書き出しを書き換えた")
    check(collect.unaccounted_posts(got, [{"url": f"https://x.com/a/status/{n}", "status": "none", "item": ""} for n in (1, 2, 3)], []) == [],
          "重なりを除いた書き出しが、投稿ごとの結果で済みにならない")
    nof = collect.write_grok_prompt(Path(tempfile.mkdtemp()), {"key": "k", "brand": "765", "topic": "t", "accounts": []}).read_text(encoding="utf-8")
    check("until_time:" not in nof, "公式アカウントの無い面に遡りの指示がある")



def test_grok_official_paging_follows_prompt(tmp: Path):
    """公式アカウントの遡り(依頼文 grok-collect の 1): 返った件数が道具の上限(満杯)なら古い側へ遡り、上限未満になったら止める。
    依頼文に書かれた止める条件のとおりに動く検索役(Grok の代わり)へ、満杯 → 境界の重複を含む満杯 → 満杯未満、の検索結果を返すと、
    3回引いて止まり、境界で重なった投稿は1つにまとめて確かめ(Luna)に渡る。実際の grok_basic・verify_grok_faces を通す
    (監査指摘 2026-10-14: 止める条件を「件以上」に反転しても、語句の有無だけを見るテストが通った)。"""
    import collect
    tmp.mkdir(parents=True, exist_ok=True)
    out = tmp / "g"
    out.mkdir()
    (tmp / "source_types.yml").write_text((Path(__file__).resolve().parent.parent / "source_types.yml").read_text(encoding="utf-8"),
                                          encoding="utf-8")     # 投稿の付け直しが種別を引く(作業の置き場を一時ディレクトリにするため)
    lim = collect.X_SEARCH_LIMIT
    # 公式アカウントの投稿(新しい順・秒は別々)。until_time:<いちばん古い秒+1> で引き直すと、前の回のいちばん古い投稿がまた返る(境界の重複)
    posts = [(f"https://x.com/off/status/{900 + i}", 1_760_000_000 - 60 * i) for i in range(2 * lim + 4)]
    searches, given, notes = [], [], []

    class Session:
        pid, returncode = 0, 0

        def __init__(self, args, cwd=None, **kw):
            if args[0] == "grok":       # 基本の調べ: 依頼文の 1 に書かれた止める条件のとおりに、公式アカウントを遡る
                prompt = Path(args[args.index("--prompt-file") + 1]).read_text(encoding="utf-8")
                step1 = prompt.split("1. 公式アカウントをまとめて引く", 1)[-1].split("\n2. ", 1)[0]
                n = int(re.search(r"limit (\d+)", step1).group(1))
                more = int(re.search(r"追加で最大 (\d+)回", step1).group(1))
                stop = re.search(r"(\d+)件(未満|以上)になるか", step1)
                written, until = [], None
                while stop and len(searches) < 1 + more:
                    got = [p for p in posts if until is None or p[1] < until][:n]
                    searches.append(len(got))
                    written += got
                    k = int(stop.group(1))
                    if not got or (len(got) < k if stop.group(2) == "未満" else len(got) >= k):
                        break
                    until = got[-1][1] + 1
                sample = prompt.split("```")[1].strip("\n")
                dest = re.search(r"書き出すファイル: `([^`]+)`", prompt).group(1)
                Path(dest).write_text("\n\n".join(sample.replace("### 番号", f"### {i + 1}").replace("https://x.com/<アカウント>/status/<ID>", u)
                                                  for i, (u, _) in enumerate(written)) + "\n", encoding="utf-8")
            else:                       # 確かめ(Luna): 渡された投稿を全部「事実なし」で済ませる
                urls = re.findall(r"(?m)^- url:\s*(\S+)", (Path(cwd) / "x-posts.md").read_text(encoding="utf-8"))
                given.extend(urls)
                (Path(cwd) / "items.json").write_text("[]", encoding="utf-8")
                (Path(cwd) / "deep.json").write_text("[]", encoding="utf-8")
                (Path(cwd) / "posts.json").write_text(json.dumps([{"url": u, "status": "none", "item": ""} for u in urls]), encoding="utf-8")

        def communicate(self, timeout=None): return "", ""
        def wait(self, timeout=None): return 0
        def poll(self): return 0
    q = {"key": "gakuen", "brand": "gaku", "topic": "t", "accounts": ["off"]}
    saved = (collect.notify, collect.ROOT, pipelib.ROOT, collect.subprocess.Popen)
    try:
        collect.notify = lambda job, msg, ok=True, require=False: notes.append(msg) or True
        collect.ROOT = pipelib.ROOT = tmp
        collect.subprocess.Popen = Session
        collect.grok_basic([q], out, {})
        collect.verify_grok_faces([q], out)
    finally:
        collect.notify, collect.ROOT, pipelib.ROOT, collect.subprocess.Popen = saved
    check(searches == [lim, lim, len(posts) - 2 * lim + 2],
          f"満杯なら遡り、満杯未満で止める、のとおりに引かない(依頼文の止める条件): 1回ごとの件数 {searches}")
    check(given == [u for u, _ in posts], f"境界で重なった投稿を1つにして、全部の投稿を1回ずつ確かめに渡さない: {len(given)}件 {given[:3]}")
    check(not notes, f"全部済んだのに投稿の喪失を知らせた: {notes}")


def test_grok_deep_format_matches_parser():
    """深掘り(grok-deep)に指示する書き出しの形を、突き合わせ(post_blocks / post_url)がそのまま読めること。
    2026-10-09: 依頼文に形が無く、Grok が「## 問い1」+問いの文+「- 投稿の url:」で書いた。url を読めず、Luna が
    候補にした・事実なしとした投稿まで「深掘りまで確かめても失った」と7件通知した(当番の指摘 b88a0534)。"""
    import collect
    p = collect.render_prompt("grok-deep", BRAND="million", TODAY="2026-10-09", OUT="o.md", MAX_SEARCHES=2, RETRY="",
                              QUESTIONS="1. 投稿 https://x.com/b/status/2 の本文は何か。(なぜ X の原本が要るか: 記載なし)")
    check("```" in p, "深掘りの依頼文に書き出しの形が無い")
    if "```" not in p:
        return
    sample = p.split("```")[1].replace("<アカウント>/status/<ID>", "b/status/2")
    blocks = collect.post_blocks(sample)
    check([collect.post_url(b) for b in blocks] == ["https://x.com/b/status/2"], f"深掘りの見本を投稿に分けられない: {blocks}")
    check(collect.unaccounted_posts(sample, [{"url": "https://x.com/b/status/2", "status": "none", "item": ""}], []) == [],
          "深掘りの見本の投稿が、事実なしの結果で済みにならない")
    # どの問いでも見つからないときの書き出しは、確かめを飛ばす「見つからない」と同じ形
    check("「見つからない」とだけ書く" in p, "深掘りで何も見つからないときの書き方が無い")
    # 答えの投稿が無かった問いは、予算の範囲で1回だけ違う角度で投げ直す(「見つからない」で終えない。2026-10-09 のアスラン=BBⅡ世)
    asks = [{"question": f"問{i}", "why": ""} for i in (1, 2, 3)]
    ans = "### 1\n- url: https://x.com/b/status/2\n- 問い: 2\n- 本文:\nx\n\n### 2\n- 問い: 3\n- 本文:\nurl の無い塊は答えにしない"
    check(collect.unanswered_asks(ans, asks) == [asks[0], asks[2]], f"答えの無かった問いの拾い方: {collect.unanswered_asks(ans, asks)}")
    check(collect.unanswered_asks("見つからない", asks) == asks, "「見つからない」だけの書き出しで、全部の問いを投げ直しに回さない")
    nonum = "### 1\n- url: https://x.com/b/status/2\n- 本文:\nx"
    check(collect.unanswered_asks(nonum, asks[:1]) == [] and collect.unanswered_asks(nonum, asks) == [],
          "番号の無い答え: 問いが1つならその答え、複数なら投げ直さない(答えのある問いに予算を使わない)")
    # 問いの番号の契約: 依頼文の見本の形のとおりに、2つの問いのうち2つ目だけに答えた書き出しから、1つ目が投げ直しの対象になる
    # (見本から「- 問い:」の行が消えると番号の無い答えになり、どの問いの答えか分からず投げ直さない。監査指摘 2026-10-14)
    two = [{"question": "第一の問い", "why": ""}, {"question": "第二の問い", "why": ""}]
    p2 = collect.render_prompt("grok-deep", BRAND="million", TODAY="2026-10-09", OUT="o.md", MAX_SEARCHES=4, RETRY="",
                               QUESTIONS="1. 第一の問い\n2. 第二の問い")
    one = re.sub(r"(?m)^([ \t]*[-*・]?[ \t]*問い[ \t]*[:：]).*$", r"\g<1> 2", p2.split("```")[1]).replace("<アカウント>/status/<ID>", "b/status/2")
    check(collect.unanswered_asks(one, two) == [two[0]],
          f"見本の形で2つ目の問いに答えた書き出しから、答えの無い1つ目を投げ直しに回さない: {collect.unanswered_asks(one, two)} / {one!r}")
    with tempfile.TemporaryDirectory() as td:
        saved = collect.run_grok_prompts
        try:
            sent = []
            collect.run_grok_prompts = lambda targets, errs: sent.extend(pp.read_text(encoding="utf-8") for _, pp in targets) or set()
            qs = [{"key": "sidem", "brand": "sidem", "topic": "t", "accounts": ["SideM_official"]}]
            collect.deep_dive_grok(qs, Path(td), {"sidem": asks[:1]}, {}, suffix="-deep2", retry=True)
            collect.deep_dive_grok(qs, Path(td), {"sidem": asks[:1]}, {})
        finally:
            collect.run_grok_prompts = saved
        check(len(sent) == 2 and "投げ直し" in sent[0] and "@SideM_official" in sent[0] and "sidem-deep2.md" in sent[0]
              and "投げ直し" not in sent[1] and "sidem-deep.md" in sent[1], "投げ直しの依頼が、違う角度・別の書き出しにならない")
    # 収集の流れの中での投げ直し(予算・失敗の通知)は test_grok_deep_retry が run_explores 越しに検める

def test_grok_face_retry(tmp: Path):
    """Grok の面がまとめを残せなかったら、その面だけ「先に書く」順で1回やり直し、それでも残らなければ異常を上げる
    (実測 2026-10-03: 学マスの面が打ち切られて0件、公式Xの4コマを1日遅れで載せた)。手数(--max-turns)では縛らず、
    週次上限を消費する X 検索の回数だけを数える(編集長 2026-10-04「手数の概念が悪い」)。"""
    import collect
    import urllib.parse
    tmp.mkdir(parents=True, exist_ok=True)
    out = tmp / "g"
    out.mkdir()
    qs = [{"key": "gakuen", "brand": "gaku", "topic": "t"}, {"key": "shiny", "brand": "shiny", "topic": "t"},
          {"key": "dsva", "brand": "dsva", "topic": "t"}]
    runs, notes = [], []

    class Grok:
        """Grok のセッション(外部)の代わり: 依頼文の書き出し先に、面ごとの決まった結果を書いて正常に終わる。
        学マスはやり直しで書ける、dsva は書けない(打ち切られて何も書かずに終わる)"""
        pid, returncode = 0, 0

        def __init__(self, args, **kw):
            prompt = Path(args[args.index("--prompt-file") + 1]).read_text(encoding="utf-8")
            key = Path(args[args.index("--prompt-file") + 1]).stem.removeprefix("prompt-")
            retry = "**やり直し**" in prompt
            runs.append((key, retry))
            if key == "shiny" or (key == "gakuen" and retry):
                dest = re.search(r"書き出すファイル: `([^`]+)`", prompt).group(1)
                Path(dest).write_text("### 1\n- url: https://x.com/a/status/1\n- 本文:\nまとめ\n", encoding="utf-8")

        def communicate(self, timeout=None): return "", ""
        def wait(self, timeout=None): return 0
        def poll(self): return 0
    saved = (collect.notify, collect.ROOT, pipelib.ROOT, collect.subprocess.Popen)
    try:
        collect.notify = lambda job, msg, ok=True, require=False: notes.append((msg, ok)) or True
        collect.ROOT = pipelib.ROOT = tmp          # 生の出力・セッション記録の置き場も一時ディレクトリに
        collect.subprocess.Popen = Grok
        # 本番の基本の調べ(grok_basic)を走らせ、外部のセッションの実行だけを差し替える
        still = collect.grok_basic(qs, out, {})
    finally:
        collect.notify, collect.ROOT, pipelib.ROOT, collect.subprocess.Popen = saved
    first, second = runs[:3], runs[3:]
    check(sorted(first) == [("dsva", False), ("gakuen", False), ("shiny", False)] and sorted(second) == [("dsva", True), ("gakuen", True)]
          and still == ["dsva"], f"やり直しの対象と結果: {runs} {still}")
    check(any("dsva" in m and not ok for m, ok in notes) and not any("gakuen" in m for m, _ in notes),
          f"やり直しても書けなかった面だけを名指ししない: {notes}")
    # 依頼文: やり直しのときだけ「先に書く」行。数えるのは検索だけ、と常に書く
    p1 = collect.write_grok_prompt(out, {"key": "gakuen", "brand": "gaku", "topic": "t", "accounts": ["gkmas_official"]}).read_text(encoding="utf-8")
    p2 = collect.write_grok_prompt(out, {"key": "gakuen", "brand": "gaku", "topic": "t", "accounts": ["gkmas_official"]}, retry=True).read_text(encoding="utf-8")
    check("数えるのは X の検索だけ" in p1 and "1 の結果を、まずファイルに書く" in p1 and "リンク先のページを開かない" in p1
          and "やり直し" not in p1 and "やり直し" in p2, "Grok の依頼文の数え方・役割・やり直しの指示")
    src = (pipelib.ROOT / "scripts" / "collect.py").read_text(encoding="utf-8")
    check("run_grok_faces(missing, outdir, grok_errs, retry=True)" in src, "書けなかった面をやり直していない")
    # 起動引数に手数の上限を付けない
    argv = []
    saved_popen = collect.subprocess.Popen
    class P:
        returncode = 0
        def communicate(self, timeout=None): return "", ""
    try:
        collect.subprocess.Popen = lambda args, **kw: argv.append(args) or P()
        collect.run_grok_faces(qs[:1], out, {})
    finally:
        collect.subprocess.Popen = saved_popen
    check(argv and "--max-turns" not in argv[0], f"Grok を手数で縛っている: {argv[:1]}")
    # 面ごとの X 検索の回数をセッション記録から数える
    home = tmp / "home"
    for name, brand, n in (("s1", "gaku", 3), ("s2", "sidem", 7)):
        sd = home / ".grok" / "sessions" / urllib.parse.quote(str(tmp / "repo"), safe="") / name
        sd.mkdir(parents=True)
        (sd / "chat_history.jsonl").write_text('{"content":"担当する面: brand=\\"' + brand + '\\" …"}\n', encoding="utf-8")
        (sd / "updates.jsonl").write_text('{"rawInput": {"variant": "XSearch", "backend": true}}\n' * n + '{"rawInput": {"url": "x"}}\n', encoding="utf-8")
    saved_root, saved_home = collect.ROOT, os.environ.get("HOME")
    try:
        collect.ROOT, os.environ["HOME"] = tmp / "repo", str(home)
        got = collect.grok_search_counts(0)
        check(got == {"gaku": 3, "sidem": 7}, f"面ごとの X 検索の回数: {got}")
    finally:
        collect.ROOT = saved_root
        if saved_home is not None:
            os.environ["HOME"] = saved_home


def test_update_clis(tmp: Path):
    """道具の CLI の更新(編集長 2026-10-02「定期的に Codex/Grok/Claude のアップデートを仕掛けるようにしないと駄目だ」):
    版が変われば知らせ、更新後に答えが返らなければ(Grok は API に拒まれても終了コード 0)異常として上げる。
    収集は Grok が全面0件のとき、セッション記録の失敗理由を添えて異常を上げる。"""
    import subprocess as sp
    import urllib.parse
    import collect
    import update_clis as uc
    tmp.mkdir(parents=True, exist_ok=True)
    ver = {"claude": ["2.1.210", "2.1.220"], "codex": ["0.160.0", "0.160.0"], "grok": ["1.0.5", "1.0.5"]}
    def fake_run(args, timeout=600, cwd=None):
        tool = args[0]
        if args[1:] == ["--version"]:
            v = ver[tool].pop(0) if len(ver[tool]) > 1 else ver[tool][0]
            return sp.CompletedProcess(args, 0, f"{tool} {v}\n", "")
        if args[1:] == ["update"]:
            return sp.CompletedProcess(args, 0, "ok", "")
        if tool == "grok":   # 古い版: 答えずに終了コード 0
            return sp.CompletedProcess(args, 0, "", "")
        return sp.CompletedProcess(args, 0, "2\n", "")
    notes = []
    saved = (uc.run, uc.notify, uc.RECORD, uc.job_lock, uc.diagnose_anomalies)
    # 期限は INVOCATION_ID(systemd からの起動か)で変わる。当番の selfcheck は systemd の下で走るので、手で回した形に固定する
    # (2026-10-03: 当番の selfcheck だけ、期限 01:55 切れで記録を書かずに戻り、このテストが赤になった)
    saved_inv = os.environ.pop("INVOCATION_ID", None)
    try:
        uc.run, uc.RECORD = fake_run, tmp / "cli-versions.json"
        uc.notify = lambda job, msg, ok=True, require=False: notes.append((msg, ok)) or True
        uc.job_lock = lambda job, wait_min=0: None
        uc.diagnose_anomalies = lambda *a, **k: False
        saved_argv, sys.argv = sys.argv, ["update_clis.py"]
        try:
            code = uc.main()
        finally:
            sys.argv = saved_argv
        rec = json.loads(uc.RECORD.read_text(encoding="utf-8"))["tools"]
        check(code == 1 and rec["grok"]["works"] is False and rec["claude"]["works"] and rec["claude"]["after"] == "2.1.220",
              f"更新と動作確認の記録: {rec}")
        check(any(ok and "claude 2.1.210 → 2.1.220" in m for m, ok in notes), f"版の変化を知らせない: {notes}")
        check(any(not ok and "grok" in m and "答えを返さない" in m for m, ok in notes), f"答えを返さない Grok を異常にしない: {notes}")
        # 期限(02:00 の収集の前)までに終えられないなら、更新せずロックを手放し、未更新を異常にする(監査指摘 r110)
        import time as _t
        saved_sd = uc.set_deadline
        notes.clear()
        calls_after = []
        uc.set_deadline = lambda: setattr(uc, "_deadline", uc.now_jst().timestamp() + 30)
        uc.run = lambda args, timeout=600, cwd=None: calls_after.append(args) or sp.CompletedProcess(args, 0, "2\n", "")
        sys.argv = ["update_clis.py"]
        try:
            code = uc.main()
        finally:
            sys.argv, uc.set_deadline = saved_argv, saved_sd
        check(code == 1 and not calls_after and any(not ok and "期限" in m for m, ok in notes), f"期限切れで更新を止めない: {calls_after[:2]} {notes}")
        # 定時の起動が遅れて 01:55 を過ぎていたら、期限を延ばさず何もしない(ロックも取らない。監査指摘 r111)
        import datetime as _dt
        notes.clear()
        locks = []
        saved_now, saved_lock2 = uc.now_jst, uc.job_lock
        uc.now_jst = lambda: _dt.datetime(2026, 10, 3, 2, 14, tzinfo=pipelib.JST)
        uc.job_lock = lambda job, wait_min=0: locks.append(wait_min)
        os.environ["INVOCATION_ID"] = "test"
        sys.argv = ["update_clis.py"]
        try:
            code = uc.main()
        finally:
            sys.argv, uc.now_jst, uc.job_lock = saved_argv, saved_now, saved_lock2
            os.environ.pop("INVOCATION_ID", None)
        check(code == 1 and not locks and not calls_after and any(not ok and "期限" in m for m, ok in notes),
              f"遅れた定時起動で期限を延ばした: ロック {locks} / 呼び出し {calls_after[:2]} / {notes}")
    finally:
        uc.run, uc.notify, uc.RECORD, uc.job_lock, uc.diagnose_anomalies = saved
        if saved_inv is not None:
            os.environ["INVOCATION_ID"] = saved_inv
    # Grok のセッション記録から失敗理由を読む(終了コードもエラー出力も空だった 2026-10-02 の形)
    home = tmp / "home"
    sess = home / ".grok" / "sessions" / urllib.parse.quote(str(tmp / "repo"), safe="") / "s1"
    sess.mkdir(parents=True)
    (sess / "updates.jsonl").write_text('{"params":{"update":{"stop_reason":"error","agent_result":"API error (status 426 Upgrade Required): outdated"}}}\n',
                                        encoding="utf-8")
    saved_root, saved_home = collect.ROOT, os.environ.get("HOME")
    try:
        collect.ROOT, os.environ["HOME"] = tmp / "repo", str(home)
        check("426 Upgrade Required" in collect.grok_session_error(), f"Grok の失敗理由を読めない: {collect.grok_session_error()}")
    finally:
        collect.ROOT = saved_root
        if saved_home is not None:
            os.environ["HOME"] = saved_home


def test_update_clis_smoke_answer(tmp: Path):
    """道具の動作確認は、正常終了して、答え(標準出力の最後の行)が求めたとおりの「2」のときだけ動作 OK(監査指摘 2026-10-14 ar9:
    実の CLI が `2` を出して exit 1、`1+1=3 (attempt 2)` を出して exit 0、のどちらも動作 OK になっていた)。
    実の子プロセス(一時ディレクトリの偽の CLI)を起動して確かめる。"""
    import update_clis as uc
    bindir, wd = tmp / "bin", tmp / "wd"
    bindir.mkdir(parents=True, exist_ok=True)
    wd.mkdir(parents=True, exist_ok=True)
    saved = (uc.tool_path, uc._deadline)
    try:
        uc.tool_path, uc._deadline = (lambda: f"{bindir}:{os.environ.get('PATH', '')}"), 0
        for out, code, want in (("2", 0, True), ("前置き\n2\n", 0, True), ("2", 1, False), ("1+1=3 (attempt 2)", 0, False),
                                ("", 0, False), ("2 です", 0, False), ("12", 0, False)):
            for tool in ("claude", "codex", "grok"):
                exe, said = bindir / tool, bindir / f"{tool}.out"
                said.write_text(out, encoding="utf-8")
                exe.write_text(f"#!/bin/sh\ncat '{said}'\nexit {code}\n", encoding="utf-8")
                exe.chmod(0o755)
                ok, why = uc.smoke(tool, wd)
                check(ok is want and (ok or why), f"{tool} の動作確認: 出力 {out!r} / exit {code} → {(ok, why)}(期待 {want})")
    finally:
        uc.tool_path, uc._deadline = saved


def test_git_commit_push_checkout(tmp: Path):
    """収集・組版の Git 操作(pipelib.commit_and_push / checkout_edition_branch)を、実の Git(一時リポジトリとローカルの bare origin)で通す
    (監査指摘 2026-10-14 ar9: 既存のテストは commit/push と checkout を無効にしても全部通っていた)。
    push を拒まれた commit は、変更の無い再試行でも送ってから成功と返す。次の工程の checkout は、送れない commit を origin に合わせて
    捨てず(止まる)、送れるようになれば送ってから進む。分かれていれば origin の上に積み直す。main は衝突しなければ取り込んで送り、
    edition と main が同じファイルを変えていれば、取り込む前の clean な状態へ戻して止まり、当番へ渡す。"""
    tmp.mkdir(parents=True, exist_ok=True)
    remote, work, other = tmp / "remote.git", tmp / "work", tmp / "other"
    date, branch = "2026-10-15", "edition/2026-10-15"

    def g(*a, cwd=work, check_=True):
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=check_)

    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, capture_output=True)
    g("config", "core.hooksPath", str(remote / "hooks"), cwd=remote)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        g("config", k, v)
    (work / "source_types.yml").write_text("official_domains:\n  - a.example\n", encoding="utf-8")
    (work / "shared.txt").write_text("base\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-q", "-m", "base")
    g("remote", "add", "origin", str(remote)); g("push", "-q", "origin", "main")
    g("checkout", "-q", "-b", branch); g("push", "-q", "origin", branch)
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True, capture_output=True)
    for k, v in (("user.email", "o@example.com"), ("user.name", "o")):
        g("config", k, v, cwd=other)
    hook = remote / "hooks" / "pre-receive"
    hook.parent.mkdir(exist_ok=True)

    def reject(on: bool) -> None:
        if on:
            hook.write_text("#!/bin/sh\necho 'rejected by test' >&2\nexit 1\n", encoding="utf-8")
            hook.chmod(0o755)
        else:
            hook.unlink(missing_ok=True)

    def remote_tip() -> str:
        return g("rev-parse", "-q", "--verify", f"refs/heads/{branch}", cwd=remote, check_=False).stdout.strip()

    def remote_has(path: str) -> bool:
        return g("cat-file", "-e", f"refs/heads/{branch}:{path}", cwd=remote, check_=False).returncode == 0

    def call(fn, *a):
        try:
            return fn(*a)
        except Exception as e:      # noqa: BLE001 — 例外で抜けること自体が欠陥(衝突で abort に届かない 等)
            check(False, f"Git 操作が例外で抜けた({a}): {type(e).__name__}: {e}")
            return None

    notes, called = [], []
    saved = (pipelib.ROOT, pipelib.notify, pipelib.escalate)
    try:
        pipelib.ROOT = work
        pipelib.notify = lambda job, msg, ok=True, require=False: notes.append((msg, ok)) or True
        # 当番へは止まった工程の再実行つきで渡す(rerun の実効値まで控える。組版の終わりの診断は再実行なし)
        pipelib.escalate = lambda stage, d, reason, rerun=True, **k: called.append((stage, d, reason, rerun)) or True
        # 1. push を拒まれたら False(commit は手元に残る)
        reject(True)
        (work / "material-1.txt").write_text("1", encoding="utf-8")
        ok = call(pipelib.commit_and_push, branch, "collect 1", "collect")
        head1 = g("rev-parse", "HEAD").stdout.strip()
        check(ok is False and remote_tip() != head1, f"push を拒まれたのに成功と返した: {ok}")
        # 2. 変更の無い再試行でも、届いていない commit を送ってから成功と返す
        reject(False)
        ok = call(pipelib.commit_and_push, branch, "collect retry", "collect")
        check(ok is True and remote_tip() == head1, f"届いていない commit を送らずに成功と返した: {ok} / origin {remote_tip()[:10]} 手元 {head1[:10]}")
        # 2b. 書く先を限った後始末(paths)で変更が無くても、届いていない commit を送ってから成功と返す
        #     (監査指摘 2026-10-14 取り込みの監査: 限った確定の「変更なし」が送らずに成功と返した)
        reject(True)
        (work / "material-1b.txt").write_text("1b", encoding="utf-8")
        check(call(pipelib.commit_and_push, branch, "collect 1b", "collect", ["material-*"]) is False, "push を拒まれたのに成功と返した(限った確定)")
        head1b = g("rev-parse", "HEAD").stdout.strip()
        reject(False)
        ok = call(pipelib.commit_and_push, branch, "collect 1b retry", "collect", ["material-*"])
        check(ok is True and remote_tip() == head1b,
              f"限った確定で変更が無いとき、届いていない commit を送らずに成功と返した: {ok} / origin {remote_tip()[:10]} 手元 {head1b[:10]}")
        # 3. 次の工程の checkout は、送れない commit を origin に合わせて捨てない(止まる)。送れるようになれば送ってから進む
        reject(True)
        (work / "material-2.txt").write_text("2", encoding="utf-8")
        check(call(pipelib.commit_and_push, branch, "collect 2", "collect") is False, "push を拒まれたのに成功と返した(2回目)")
        head2 = g("rev-parse", "HEAD").stdout.strip()
        g("checkout", "-q", "main")      # 次の工程は別のブランチにいることもある
        called.clear()
        ok = call(pipelib.checkout_edition_branch, date, "compose")
        check(ok is False and g("rev-parse", branch).stdout.strip() == head2,
              f"送れない commit を checkout で捨てた / 止まらなかった: {ok} / {g('rev-parse', branch).stdout.strip()[:10]} ≠ {head2[:10]}")
        check([(c[0], c[1], c[3]) for c in called] == [("compose", date, True)],
              f"送れない commit で止めたことを、再実行つきで当番へ渡さない: {[(c[0], c[1], c[3]) for c in called]}")
        reject(False)
        ok = call(pipelib.checkout_edition_branch, date, "compose")
        check(ok is True and (work / "material-2.txt").exists() and remote_tip() == head2,
              f"checkout が手元だけの素材を消した・送っていない: {ok} / 素材 {(work / 'material-2.txt').exists()} / origin {remote_tip()[:10]}")
        # 4. 送れなかった間に他の工程が push して分かれていたら、origin の上に積み直して送る(どちらも消さない)
        reject(True)
        (work / "material-3.txt").write_text("3", encoding="utf-8")
        check(call(pipelib.commit_and_push, branch, "collect 3", "collect") is False, "push を拒まれたのに成功と返した(3回目)")
        reject(False)
        g("fetch", "-q", "origin", cwd=other); g("checkout", "-q", "-B", branch, f"origin/{branch}", cwd=other)
        (other / "other.txt").write_text("o", encoding="utf-8")
        g("add", "-A", cwd=other); g("commit", "-q", "-m", "other", cwd=other); g("push", "-q", "origin", branch, cwd=other)
        ok = call(pipelib.checkout_edition_branch, date, "collect")
        check(ok is True and remote_has("material-3.txt") and remote_has("other.txt") and (work / "material-3.txt").exists()
              and g("rev-parse", "HEAD").stdout.strip() == remote_tip(),
              f"分かれた手元の commit を積み直して送らない: {ok} / origin に素材 {remote_has('material-3.txt')} 他 {remote_has('other.txt')}")
        # 5. main は衝突しなければ取り込んで送る
        g("checkout", "-q", "main", cwd=other); g("pull", "-q", "--ff-only", "origin", "main", cwd=other)
        (other / "script.txt").write_text("fix", encoding="utf-8")
        g("add", "-A", cwd=other); g("commit", "-q", "-m", "main fix", cwd=other); g("push", "-q", "origin", "main", cwd=other)
        ok = call(pipelib.checkout_edition_branch, date, "collect")
        main_tip = g("rev-parse", "refs/heads/main", cwd=remote).stdout.strip()
        check(ok is True and remote_has("script.txt")
              and g("merge-base", "--is-ancestor", main_tip, remote_tip(), cwd=remote, check_=False).returncode == 0,
              f"main を取り込んで送らない: {ok}")
        # 6. edition と main が同じファイルを変えていたら、取り込む前の clean な状態へ戻して止まり、当番へ渡す
        (work / "shared.txt").write_text("edition\n", encoding="utf-8")
        check(call(pipelib.commit_and_push, branch, "edition edit", "collect") is True, "edition の変更を送れない")
        (other / "shared.txt").write_text("main\n", encoding="utf-8")
        g("add", "-A", cwd=other); g("commit", "-q", "-m", "main edit", cwd=other); g("push", "-q", "origin", "main", cwd=other)
        before = g("rev-parse", "HEAD").stdout.strip()
        notes.clear(); called.clear()
        for job in ("compose", "collect"):       # 次の工程も同じ衝突で止まるが、作業ツリーを汚さない
            ok = call(pipelib.checkout_edition_branch, date, job)
            merging = g("rev-parse", "-q", "--verify", "MERGE_HEAD", check_=False).returncode == 0
            status = g("status", "--porcelain").stdout
            check(ok is False and not merging and status == "" and g("rev-parse", "HEAD").stdout.strip() == before
                  and g("branch", "--show-current").stdout.strip() == branch,
                  f"{job}: main との衝突のあと戻さない・止まらない: {ok} / merge 中 {merging} / {status!r}")
        check(any(not ok_ and "shared.txt" in m for m, ok_ in notes), f"衝突したファイルを名指しで異常にしない: {notes}")
        check([(c[0], c[1], c[3]) for c in called] == [("compose", date, True), ("collect", date, True)]
              and all("shared.txt" in c[2] for c in called),
              f"衝突で止めたことを再実行つきで当番へ渡さない: {[(c[0], c[1], c[3]) for c in called]}")
        # merge --abort で戻らなくても、取り込む前の commit へ戻してから止まる
        real_git = pipelib.git
        pipelib.git = lambda *a, check=True: (subprocess.CompletedProcess(["git", *a], 128, "", "abort failed (test)")
                                              if a[:2] == ("merge", "--abort") else real_git(*a, check=check))
        try:
            ok = call(pipelib.checkout_edition_branch, date, "compose")
        finally:
            pipelib.git = real_git
        merging = g("rev-parse", "-q", "--verify", "MERGE_HEAD", check_=False).returncode == 0
        status = g("status", "--porcelain").stdout
        check(ok is False and not merging and status == "" and g("rev-parse", "HEAD").stdout.strip() == before,
              f"abort が効かないとき取り込む前へ戻さない: {ok} / merge 中 {merging} / {status!r}")
    finally:
        pipelib.ROOT, pipelib.notify, pipelib.escalate = saved


def test_article_purpose():
    """記事の目的(編集長 2026-10-03「目的の間違い」: 予約締切の前日の記事を、執筆が新しい事実が無いと見送った)。
    選定が purpose を返し、計画に載り、執筆の依頼文に出る。続報予約の消化は目的が期日の知らせ。"""
    import planlib
    sch = planlib.plan_schema(["k1"], "765")
    dec = sch["properties"]["decisions"]["properties"]["k1"]
    check("purpose" in dec["required"] and "期日の知らせ" in dec["properties"]["purpose"]["enum"], "選定の schema に目的が無い")
    plan = planlib.decisions_to_plan("765", [{"dedup_key": "k1", "ids": ["c1"], "title": "t"}, {"dedup_key": "k2", "ids": ["c2"], "title": "t2"}],
                                     {"k1": {"action": "article", "angle": "締切が明日", "purpose": "期日の知らせ", "rank": "small", "lead_score": 0},
                                      "k2": {"action": "article", "angle": "新発表", "purpose": "?", "rank": "small", "lead_score": 0}}, set())
    arts = {a["dedup_key"]: a for a in plan["articles"]}
    check(arts["k1"]["purpose"] == "期日の知らせ" and arts["k2"]["purpose"] == "新情報", f"計画の目的: {[(k, a.get('purpose')) for k, a in arts.items()]}")
    art = {"slug": "x", "rank": "small", "angle": "締切が明日"}
    p1 = compose.article_prompt("2026-10-03", {**art, "purpose": "期日の知らせ"}, [], [], None)
    p2 = compose.article_prompt("2026-10-03", {**art, "purpose": "新情報"}, [], [], {"kind": "締切", "note": "予約できる", "subject": "s"})
    p3 = compose.article_prompt("2026-10-03", {**art, "purpose": "新情報"}, [], [], None)
    check("- 目的: 期日の知らせ" in p1 and "- 目的: 期日の知らせ" in p2 and "- 目的: 新情報" in p3, "執筆の依頼文に目的が出ない(続報予約は期日の知らせ)")


def test_plan_merge_into_drop():
    """不採用(drop)の主題へ merge した主題は、単独の記事にせず相手の理由で不採用に並べる(2026-10-04: dsva 面で
    「1on1(10/3)」を drop した「1on1」へ merge し、それが単独の記事になって執筆が終了済みで見送った)。
    記事へ・連鎖・循環の扱いは変えない。校閲は名鑑で補った姓名を R1 にしない(同日: 「馬場このみ」が4巡ブロックされ落ちた)"""
    import planlib
    rows = [{"dedup_key": k, "ids": [f"c-{k}"], "title": k} for k in ("a", "b", "c", "d", "e", "f", "g")]
    dec = {"a": {"action": "drop", "reason": "既報", "note": "終了済み"},
           "b": {"action": "merge", "merge_into": "a"},
           "c": {"action": "merge", "merge_into": "b"},
           "d": {"action": "article", "angle": "x", "purpose": "新情報", "rank": "small", "lead_score": 0},
           "e": {"action": "merge", "merge_into": "d"},
           "f": {"action": "merge", "merge_into": "g"},
           "g": {"action": "merge", "merge_into": "f"}}
    plan = planlib.decisions_to_plan("dsva", rows, dec, set())
    arts = {a["dedup_key"]: a for a in plan["articles"]}
    drops = {d["dedup_key"]: d for d in plan["dropped"]}
    check(set(arts) == {"d", "f"} and arts["f"]["candidate_ids"] == ["c-f", "c-g"], f"drop へ merge した主題が記事になった: {sorted(arts)}")
    check(drops.get("b", {}).get("reason") == "既報" and drops.get("c", {}).get("reason") == "既報" and "a" in drops.get("b", {}).get("note", ""),
          f"drop へ merge した主題が相手の理由で不採用にならない: {drops}")
    check(arts["d"]["candidate_ids"] == ["c-d", "c-e"], f"記事への merge: {arts['d']['candidate_ids']}")
    rp = (pipelib.PROMPTS / "review-article.md").read_text(encoding="utf-8")
    r1 = rp[rp.index("- R1 "):rp.index("- R2 ")]
    check("docs/_data/idols.json" in r1 and "R1 にしない" in r1, "校閲の R1 に、名鑑で補った姓名の扱いが無い")
    check("敬称" in r1 and "765" in r1 and "根拠(F・N)で確かめる" in r1 and "0件なら R1" not in r1,
          "校閲の R1 の名鑑規則が、敬称・765 の出演・一人に決まらないときの通常確認を扱っていない")
    # 2026-10-06: 告知日時を過ぎたことを過去形で書くと R1、未来形に戻すと R7 で、どちらに書いても落ちた
    check("{DATE} 06:00 より前" in r1 and "過去として書いた" in r1 and "R7" in r1 and "延期・中止" in r1,
          "校閲の R1 が、告知の日時を過ぎたことを過去として書いた記述を R1 から外していない(R7 と矛盾する)")


def test_withdrawal(tmp: Path):
    """取り下げ(編集長 2026-09-29「記事ごと削除でいいのでは」): 道具(withdraw.py)が記事・記録・目次・一面・号の数・未来の予約を
    1回で直し、lint の判定(edition_withdrawal_ok / promotion_ok / withdrawal_record_errors / load_withdrawn)がそれを認め、
    取り下げに紛れた別の変更は認めない。一面の取り下げでは lead_score 最大(roundup を除く)を一面に立て直す。"""
    import withdraw
    d = "2026-09-29"
    posts, eds = tmp / "docs" / "_posts", tmp / "docs" / "_editions"
    for p in (posts, eds, tmp / "metrics", tmp / "stock" / "scheduled"):
        p.mkdir(parents=True, exist_ok=True)
    # 下準備も書式(article / edition)に合う形にする(本番の取り下げは公開済みの書式どおりの記事・号を書き換える。pipelib.write_checked)
    post = lambda slug, rank, brand, cid: ("---\n" + article_fm(slug, d, brand=brand, rank=rank, candidate_ids=[cid], title=slug)
                                         + f"---\n{slug} の本文。\n\n二段落目。\n")
    (posts / f"{d}-a.md").write_text(post("a", "lead", "cg", "c-a"), encoding="utf-8")
    (posts / f"{d}-b.md").write_text(post("b", "large", "million", "c-b"), encoding="utf-8")
    (posts / f"{d}-c.md").write_text(post("c", "roundup", "million", "c-c"), encoding="utf-8")
    ranking = "".join(f"- n: {i}\n  name: 名{i}\n  brand: cg\n  delta: new\n" for i in range(1, 9))
    ed_text = (f"---\nnumber: 15\ndate: '{d}'\nweekday: 月\nissued_at: '{d}T06:00:00+09:00'\npages: 2\narticle_count: 3\ncorrected_count: 0\n"
               "lead_slug: a\ndigest:\n"
               "- label: 本日\n  rows:\n  - k: 発\n    t: A\n    d: ''\n    brand: cg\n    slug: a\n  - k: 発\n    t: B\n    d: ''\n    brand: million\n    slug: b\n"
               "- label: 昨日\n  rows: []\n- label: 継続中\n  rows: []\n- label: 明日\n  rows: []\n"
               f"ranking:\n{ranking}birthdays: []\n"
               "---\n号の本文(そのまま残ること)\n")
    (eds / f"{d}.md").write_text(ed_text, encoding="utf-8")
    (tmp / "metrics" / f"plan-{d}.json").write_text(json.dumps({"articles": [
        {"slug": "a", "lead_score": 50}, {"slug": "b", "lead_score": 40}, {"slug": "c", "lead_score": 90}]}), encoding="utf-8")
    sched = [sched_row("s-a", d, src_candidate_id="c-a"), sched_row("s-b", d, src_candidate_id="c-b")]
    (tmp / "stock" / "scheduled" / "2026-10-02.json").write_text(json.dumps(sched, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (tmp / "stock" / "scheduled" / "2026-09-20.json").write_text(json.dumps(sched[:1], indent=1) + "\n", encoding="utf-8")
    b_before = (posts / f"{d}-b.md").read_text(encoding="utf-8")

    # 断る入力では何も変えない(記事だけ消えて号が古いまま、を残さない。監査指摘 r98)
    snapshot = lambda: {p: p.read_bytes() for p in sorted(tmp.rglob("*")) if p.is_file()}
    before = snapshot()
    for kw, what in (({"lead": "zz"}, "号に無い --lead"), ({"lead": "c"}, "roundup を --lead")):
        try:
            withdraw.withdraw(f"{d}-a", "編集長", "理由", d, root=tmp, **kw)
            check(False, f"{what} を断らない")
        except SystemExit:
            pass
        check(snapshot() == before, f"{what} で断ったのに作業ツリーが変わった")
    try:
        withdraw.withdraw(f"{d}-b", "編集長", "理由", d, lead="c", root=tmp)
        check(False, "一面でない記事の取り下げで --lead を断らない")
    except SystemExit:
        pass
    check(snapshot() == before, "一面でない取り下げの --lead で断ったのに作業ツリーが変わった")
    (tmp / "stock" / "scheduled" / "2026-10-05.json").write_text("{壊れた", encoding="utf-8")
    before = snapshot()
    try:
        withdraw.withdraw(f"{d}-a", "編集長", "理由", d, root=tmp)
        check(False, "読めない予約ファイルで止まらない")
    except ValueError:
        pass
    check(snapshot() == before, "読めない予約ファイルで止まったのに作業ツリーが変わった")
    (tmp / "stock" / "scheduled" / "2026-10-05.json").unlink()
    # 足せない形の記録(空配列・文書終端つき・壊れた記録)では何も変えない(監査指摘 r100)
    for text in ("[]\n", "- post: 2026-09-01-x\n  at: '2026-09-01'\n  by: a\n  reason: b\n...\n", "post: x\n"):
        (tmp / "withdrawn.yml").write_text(text, encoding="utf-8")
        before = snapshot()
        try:
            withdraw.withdraw(f"{d}-a", "編集長", "理由", d, root=tmp)
            check(False, f"足せない記録 {text!r} で止まらない")
        except SystemExit:
            pass
        check(snapshot() == before, f"足せない記録 {text!r} で止まったのに作業ツリーが変わった")
    (tmp / "withdrawn.yml").unlink()

    withdraw.withdraw(f"{d}-a", "編集長", "アイマスと関係しない情報だけの記事", d, root=tmp)
    check(not (posts / f"{d}-a.md").exists(), "取り下げた記事が残っている")
    b_after = (posts / f"{d}-b.md").read_text(encoding="utf-8")
    check(b_after == b_before.replace("rank: large", "rank: lead"), "一面の立て直しで rank 以外が変わった(roundup の c を立てた?)")
    new_ed_text = (eds / f"{d}.md").read_text(encoding="utf-8")
    old_ed, new_ed = withdraw.split_doc(ed_text)[0], withdraw.split_doc(new_ed_text)[0]
    check(new_ed["lead_slug"] == "b" and new_ed["article_count"] == 2 and new_ed["pages"] == 1, f"号の数・一面(残りは y 面だけ): {new_ed}")
    check([r["slug"] for g in new_ed["digest"] for r in g["rows"]] == ["b"], f"目次: {new_ed['digest']}")
    check(new_ed_text.partition("\n---")[2] == ed_text.partition("\n---")[2], "号の本文が変わった")
    check([r["id"] for r in json.loads((tmp / "stock" / "scheduled" / "2026-10-02.json").read_text(encoding="utf-8"))] == ["s-b"],
          "その記事が作った未来の予約が消えていない(か、別の予約まで消した)")
    check(len(json.loads((tmp / "stock" / "scheduled" / "2026-09-20.json").read_text(encoding="utf-8"))) == 1, "過去の予約ファイルを触った")
    # 一面に立てられる記事(roundup・culture 以外)が残らない取り下げは断る
    try:
        withdraw.withdraw(f"{d}-b", "編集長", "理由", d, root=tmp)
        check(False, "roundup しか残らない一面の取り下げを断らない")
    except SystemExit:
        pass
    check((posts / f"{d}-b.md").exists(), "断ったのに記事を消した")
    rows, errs = lint.load_withdrawn((tmp / "withdrawn.yml").read_text(encoding="utf-8"))
    check(not errs and [r["post"] for r in rows] == [f"{d}-a"], f"記録: {rows} {errs}")

    # lint の判定: 道具の結果は認める
    gone = {"a"}
    check(lint.edition_withdrawal_ok(old_ed, new_ed, ed_text, new_ed_text, gone) == "", lint.edition_withdrawal_ok(old_ed, new_ed, ed_text, new_ed_text, gone))
    ob, nb = withdraw.split_doc(b_before), withdraw.split_doc(b_after)
    check(lint.promotion_ok(ob[0], nb[0], ob[1], nb[1]), "一面の立て直しを認めない")
    check(lint.withdrawal_record_errors([], rows, {f"{d}-a"}) == [], "正しい追記を止めた")
    # 取り下げに紛れた別の変更は認めない
    bad = lambda **kw: lint.edition_withdrawal_ok(old_ed, {**new_ed, **kw}, ed_text, new_ed_text, gone)
    check(bad(digest=[{**g, "rows": []} for g in new_ed["digest"]]) != "", "取り下げていない記事の目次の行まで消せた")
    check(bad(ranking=[{"n": 1}]) != "", "取り下げで変えてよい項目の外(ranking)を変えられた")
    check(lint.edition_withdrawal_ok(old_ed, {**new_ed, "lead_slug": "b"}, ed_text, new_ed_text, {"c"}) != "", "一面でない記事の取り下げで一面を差し替えられた")
    check(lint.edition_withdrawal_ok(old_ed, new_ed, ed_text, new_ed_text + "\n", gone) != "", "号の本文(末尾の空行)を変えられた")
    check(not lint.promotion_ok(ob[0], {**nb[0], "title": "別"}, ob[1], nb[1]), "立て直しに紛れて見出しを変えられた")
    for r in ("roundup", "culture"):
        check(not lint.promotion_ok({**ob[0], "rank": r}, nb[0], ob[1], nb[1]), f"{r} を一面に立てられた")
    check(not lint.promotion_ok(ob[0], nb[0], ob[1], nb[1] + "追記"), "立て直しに紛れて本文を変えられた")
    check(lint.withdrawal_record_errors(rows, [{**rows[0], "reason": "書き換え"}], set()) != [], "記録の書き換えを止めない")
    check(lint.withdrawal_record_errors([], rows, set()) != [], "削除を伴わない記録の追記を止めない")
    for text, what in (("- post: 2026-09-29-a\n  at: true\n  by: a\n  reason: b\n", "型"),
                       ("- post: 2026-09-29-a\n  at: '2026-02-30'\n  by: a\n  reason: b\n", "実在しない日付"),
                       ("- post: 2026-09-29-a\n  at: '2026-09-29'\n  by: a\n  reason: b\n" * 2, "二重記録"),
                       ("post: x\n", "配列でない")):
        check(lint.load_withdrawn(text)[1] != [], f"取り下げの記録の形式の誤り({what})を通した")


def test_rewrites(tmp: Path):
    """書き直し(rewrites.yml)の例外は、編集長が認めた範囲の記事の、本文と内容の欄だけ(監査指摘: 記録を足せば何でも通った)。"""
    rec = lambda post, at="2026-10-08": {"post": post, "at": at, "by": "編集長", "reason": "r"}
    ok = rec("2026-10-05-a")
    check(lint.rewrite_record_errors([], [ok], {"2026-10-05-a"}) == [], "認めた範囲の書き直しの記録を止めた")
    check(lint.rewrite_record_errors([], [rec("2026-09-06-a")], {"2026-09-06-a"}) != [], "範囲外の号の書き直しを通した")
    check(lint.rewrite_record_errors([], [rec("2026-10-05-a", "2026-10-09")], {"2026-10-05-a"}) != [], "認めていない日の記録を通した")
    check(lint.rewrite_record_errors([], [ok], set()) != [], "記事の変更を伴わない記録を通した")
    check(lint.rewrite_record_errors([ok], [], set()) != [], "書き直しの記録の削除を通した")
    old = {"slug": "a", "edition": "2026-10-05", "brand": "gaku", "rank": "small", "candidate_ids": ["c1"], "title": "t",
           "corrections": [{"at": "x"}], "corrected": True}
    check(lint.rewrite_change_errors(old, {**old, "title": "新", "lede": "l", "rank": "medium"}) == [], "本文と内容の欄の書き直しを止めた")
    for k, v in (("candidate_ids", []), ("slug", "b"), ("edition", "2026-10-06"), ("brand", "cg"), ("corrected", False), ("extra", 1)):
        check(lint.rewrite_change_errors(old, {**old, k: v}) != [], f"書き直しで {k} を変えられた")
    check(lint.rewrite_change_errors(old, {**old, "corrections": []}) != [], "書き直しで既にある訂正を消せた")
    check(lint.rewrite_change_errors(old, {**old, "corrections": [{"at": "x"}, {"at": "y"}]}) == [], "訂正の追記を止めた")
    check(lint.rewrite_change_errors({**old, "rank": "lead"}, {**old, "rank": "large"}) != [], "書き直しで一面を外せた")
    check(lint.rewrite_change_errors(old, {**old, "rank": "culture"}) != [], "書き直しでファン面に動かせた")
    # 書き直しの道具: 校閲を通ったと確かめる前に止まったら、差し替えた記事を全部元に戻す(監査指摘)
    import rewrite_past
    import compose as _c
    posts = tmp / "docs" / "_posts"
    posts.mkdir(parents=True)
    (tmp / "metrics").mkdir()
    n = "2026-10-05-a.md"
    (posts / n).write_text("元の記事", encoding="utf-8")
    saved = (rewrite_past.ROOT, rewrite_past.apply_answer, _c.claude_review)

    def fake_apply(path, case, ans, fetched=None):
        path.write_text("差し替えた記事", encoding="utf-8")
        return []

    def boom(*a, **k):
        raise FileNotFoundError("claude")
    try:
        rewrite_past.ROOT, rewrite_past.apply_answer, _c.claude_review = tmp, fake_apply, boom
        try:
            rewrite_past.replace_and_review({n: {"status": "ok"}}, {n: {"date": "2026-10-05"}}, {}, {}, bk := {}, conf := set())
        except FileNotFoundError:
            pass
        check((posts / n).read_text(encoding="utf-8") == "差し替えた記事" and bk == {n: "元の記事"} and not conf,
              "校閲の前に控えを取っていない")
        # main の後始末(例外の経路)と同じ戻し方を、main 越しに確かめる
        saved_argv, saved_build = sys.argv, rewrite_past.build_cases
        rewrite_past.build_cases = lambda ops, dates: {n: {"date": "2026-10-05"}}
        saved_raw = rewrite_past.latest_raw_answer
        rewrite_past.latest_raw_answer = lambda label, stem: {"status": "ok"}
        sys.argv = ["rewrite_past.py", "2026-10-05", "--from-raw"]
        (posts / n).write_text("元の記事", encoding="utf-8")
        try:
            rewrite_past.main()
            check(False, "校閲の例外が握り潰された")
        except FileNotFoundError:
            pass
        finally:
            sys.argv, rewrite_past.build_cases, rewrite_past.latest_raw_answer = saved_argv, saved_build, saved_raw
        check((posts / n).read_text(encoding="utf-8") == "元の記事", "校閲の例外で未校閲の差し替えが残った")
        # 校閲を通った差し替えは、その号の紙面の出典を判定(合議 → 付け直し)に掛ける。掛けないと書き直しで新しく引いた
        # 出典が未確認のまま紙面に残る(2026-10-08: 10/3〜10/7 号の書き直しで 6件、watch が鳴った)
        (tmp / "metrics" / "work" / "bench").mkdir(parents=True, exist_ok=True)
        _c.claude_review = lambda *a, **k: {"verdict": "approve", "blockers": [], "failed": []}
        calls = []
        saved_cls, saved_left = rewrite_past.classify_retag_lint, rewrite_past.unresolved_post_sources
        try:
            # 合議が成功しても、決まらず未確認のまま残った出典が差し替えた記事にあれば失敗(監査指摘 R2 2026-10-08)
            left_row = [{"url": "https://www.nicovideo.jp/watch/sm1", "title": "a(記事の出典: ニコニコ動画)"}]
            for ok_cls, left, want_rc in ((True, [], 0), (False, [], 1), (True, left_row, 1)):
                calls.clear()
                asked = []
                rewrite_past.classify_retag_lint = lambda d, **k: (calls.append((d, k)), (ok_cls, "" if ok_cls else "合議 exit 1"))[1]
                rewrite_past.unresolved_post_sources = lambda d, only=None: (asked.append((d, only)), left)[1]
                rewrite_past.build_cases = lambda ops, dates: {n: {"date": "2026-10-05"}}
                rewrite_past.latest_raw_answer = lambda label, stem: {"status": "ok"}
                sys.argv = ["rewrite_past.py", "2026-10-05", "--from-raw"]
                (posts / n).write_text("元の記事", encoding="utf-8")
                rc = rewrite_past.main()
                check(calls == [("2026-10-05", {"posts_only": True, "lint": False})],
                      f"差し替えた号の出典を判定に掛けていない: {calls}")
                check(rc == want_rc, f"出典の判定の{'成功' if ok_cls else '失敗'}・未確認 {len(left)}件で終了コード {rc}")
                check(asked == ([("2026-10-05", {n})] if ok_cls else []), f"差し替えた記事の未確認を見ていない: {asked}")
        finally:
            sys.argv, rewrite_past.build_cases, rewrite_past.latest_raw_answer = saved_argv, saved_build, saved_raw
            rewrite_past.classify_retag_lint, rewrite_past.unresolved_post_sources = saved_cls, saved_left
    finally:
        rewrite_past.ROOT, rewrite_past.apply_answer, _c.claude_review = saved


def test_compose_classifies_after_revise(tmp: Path):
    """校閲往復の書き直しで書き手が足した出典(判定表に無い URL)も、発行前に判定へ掛ける。組版前の判定(1b')の
    あとなので、掛け直さないと未確認のまま 06:00 に出る(監査指摘 R1 2026-10-08)。未確認が無ければ合議を起こさない。
    書き直し(revise_articles)の最後の呼び出しより後、最終の commit より前で呼ぶ"""
    import inspect
    import classify_sources as cs
    import compose as _c
    posts = tmp / "docs" / "_posts"
    posts.mkdir(parents=True)
    head = "---\ntitle: a\nsources:\n- url: {u}\n  label: L\n  type: {t}\n---\n本文\n"
    (posts / "2026-10-08-a.md").write_text(head.format(u="https://example.com/x", t="公式"), encoding="utf-8")
    (posts / "2026-10-07-b.md").write_text(head.format(u="https://example.org/y", t="未確認"), encoding="utf-8")
    saved = (cs.ROOT, _c.classify_retag_lint, _c.afford, _c.remaining_seconds)
    calls = []
    try:
        cs.ROOT = tmp
        _c.classify_retag_lint = lambda d, **k: (calls.append((d, k.get("posts_only"))), (True, ""))[1]
        _c.afford = lambda *a, **k: True
        _c.remaining_seconds = lambda **k: 60
        check(_c.classify_revised_sources("2026-10-08", "edition/x", 0.0) and calls == [],
              f"未確認の無い号で合議を起こした: {calls}")
        (posts / "2026-10-08-a.md").write_text(head.format(u="https://lawson-print.com/p/1", t="未確認"), encoding="utf-8")
        check(_c.classify_revised_sources("2026-10-08", "edition/x", 0.0) and calls == [("2026-10-08", True)],
              f"書き直しで入った未確認の出典を判定に掛けていない: {calls}")
        check([r["url"] for r in cs.unresolved_post_sources("2026-10-07", only={"2026-10-07-z.md"})] == [],
              "only で絞った記事以外の未確認を数えた")
    finally:
        cs.ROOT, _c.classify_retag_lint, _c.afford, _c.remaining_seconds = saved
    src = inspect.getsource(_c.main)
    at = src.index("classify_revised_sources(")
    check(at > src.rindex("revise_articles(") and at < src.rindex("紙面生成(校閲"),
          "書き直し後の出典の判定が、最後の書き直しと最終の commit の間に無い")


def test_late_facts_reach_plan_index(tmp: Path):
    """前の号から引き継いだ未読の事実は、選定インデックス(主題ごと先頭 PLAN_FACTS_PER_SUBJECT 件)に先に並べる。
    引き継ぎは候補の末尾へ足すので、既に上限以上の事実を持つ候補ではインデックスで切れ、それでも選定記録に読んだと残って
    次の号へも渡らなかった(監査指摘 late-facts-hidden-by-plan-index: 実データ 10日号 100候補中32件が8事実超)。
    上限を超えて切れた分は読んだことにせず、次の号がまた引き継いで先に並べる"""
    cdir, mdir = tmp / "candidates", tmp / "metrics"
    cdir.mkdir(parents=True)
    mdir.mkdir(parents=True)
    w = lambda d, rows: (cdir / f"{d}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    r = lambda d: {x["id"]: x for x in json.loads((cdir / f"{d}.json").read_text(encoding="utf-8"))}
    url, n = "https://shop.example/premium", compose.PLAN_FACTS_PER_SUBJECT
    old = [f"既存{k}" for k in range(n + 2)]
    cand = lambda i, facts: cand_row(i, url, dedup_key="premium", brand="cg", verify="confirmed", facts=facts)
    saved = compose.ROOT
    try:
        compose.ROOT = tmp
        # 10日号の選定は既存の事実だけを読んだ。その後、取り直しで同じ候補に事実が1件・別の候補に上限+2件足された
        w("2026-10-10", [cand("p1", old), {**cand("p2", ["別0"]), "url": "https://shop.example/other", "dedup_key": "other"}])
        compose.record_candidates_read("2026-10-10", r("2026-10-10"))
        late1 = "プレミアム会員限定商品の購入にはプレミアム会員登録が必要"
        late2 = [f"追加{k}" for k in range(n + 2)]
        w("2026-10-10", [cand("p1", old + [late1]),
                         {**cand("p2", ["別0"] + late2), "url": "https://shop.example/other", "dedup_key": "other"}])
        # 11日号は同じページを拾い直しており、既に上限以上の事実を持つ
        w("2026-10-11", [cand("q1", old), {**cand("q2", ["別0"]), "url": "https://shop.example/other", "dedup_key": "other"}])
        compose.carry_late_candidates("2026-10-11")
        cands = r("2026-10-11")
        late = compose.late_fact_keys("2026-10-11")
        by_brand, _, hidden = compose.write_plan_index("2026-10-11", cands, {}, late)
        idx = {s["dedup_key"]: s["facts"] for s in by_brand["cg"]}
        check(late1 in idx["premium"] and len(idx["premium"]) == n,
              f"引き継いだ未読の事実が選定インデックスで切れた(末尾に足したまま上限で切った): {idx['premium']}")
        check(idx["other"] == late2[:n] and hidden == {compose.fact_key(f) for f in late2[n:]},
              f"上限を超えた引き継ぎ分を先に並べていない・切れた分を返していない: {idx['other']} {hidden}")
        compose.record_candidates_read("2026-10-11", cands, hidden)
        rec = json.loads(compose.read_record_path("2026-10-11").read_text(encoding="utf-8"))["read"]
        check(compose.fact_key(late1) in rec["q1"]["facts"]
              and not set(rec["q2"]["facts"]) & hidden and compose.fact_key(late2[0]) in rec["q2"]["facts"],
              f"選定記録が実際に選定へ渡した範囲と一致しない: {rec['q2']}")
        # 12日号: 切れた分だけが未読として引き継がれ、インデックスの先頭に並ぶ。選定に渡ったものは二度と引き継がない
        w("2026-10-12", [{**cand("s2", ["別0"] + late2[:n]), "url": "https://shop.example/other", "dedup_key": "other"}])
        compose.carry_late_candidates("2026-10-12")
        late = compose.late_fact_keys("2026-10-12")
        check(late == {compose.fact_key(f) for f in late2[n:]},
              f"12日号へ引き継ぐ未読が、11日号の選定に渡らなかった分と一致しない: {len(late)}件")
        by_brand, _, hidden = compose.write_plan_index("2026-10-12", r("2026-10-12"), {}, late)
        idx = {s["dedup_key"]: s["facts"] for s in by_brand["cg"]}
        check(idx["other"][:2] == late2[n:] and not hidden, f"切れた分を次の号の選定に渡していない: {idx['other']}")
        # 引き継いだ事実は文字数でも切らない(監査指摘 late-facts-hidden-by-char-limit。実データ 11日号の「DISC2の歌唱者」698字):
        # 10日号の選定は先頭 PLAN_FACT_CHARS 字までの事実を読み、その後、同じ書き出しの全文(後半に新しい歌唱者)が足された
        for d in ("2026-10-11", "2026-10-12"):
            compose.read_record_path(d).unlink(missing_ok=True)
        lim = compose.PLAN_FACT_CHARS
        head = ("DISC2の歌唱者：" + "、".join(f"曲{k}＝アイドル{k}" for k in range(60)))[:lim]
        full = head + "、" + "、".join(f"新曲{k}＝新アイドル{k}" for k in range(40))
        dcand = lambda i, facts: cand_row(i, "https://e.example/disc", dedup_key="disc", brand="cg", verify="confirmed", facts=facts)
        w("2026-10-10", [dcand("d1", ["発売日", head])])
        compose.record_candidates_read("2026-10-10", r("2026-10-10"))
        w("2026-10-10", [dcand("d1", ["発売日", head, full])])
        w("2026-10-11", [dcand("e1", ["発売日", head])])
        compose.carry_late_candidates("2026-10-11")
        cands = r("2026-10-11")
        late = compose.late_fact_keys("2026-10-11")
        by_brand, _, hidden = compose.write_plan_index("2026-10-11", cands, {}, late)
        facts = {s["dedup_key"]: s["facts"] for s in by_brand["cg"]}["disc"]
        check(late == {compose.fact_key(full)} and full in facts,
              f"引き継いだ長い事実の {lim}字以降が選定インデックスに無い(先頭だけ見せて全文を読了にする): {[len(f) for f in facts]}")
        compose.record_candidates_read("2026-10-11", cands, hidden)
        check(full in facts or compose.late_fact_keys("2026-10-12"),
              "選定に見せていない後半を読了と記録し、次の号へも引き継がない")
    finally:
        compose.ROOT = saved
    import inspect
    src = inspect.getsource(compose.main)
    check("write_plan_index(date, cands, blocklist, late_fact_keys(date))" in src
          and "record_candidates_read(date, cands, hidden_late, unjudged=" in src,
          "組版が引き継いだ事実を選定インデックスへ先に並べ、切れた分を選定記録から外していない")


# テスト用の偽の claude CLI。本物と同じ引数(-p の短い指示・--json-schema)で起動され、cwd(組版の ROOT)の fake-cli.json の
# 指示で答える。起動された依頼の名前を fake-cli.log に残す。モデルの答えから先(読み込み・検め・計画への反映)は本番のコードが動く
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, re, sys
from pathlib import Path
a = sys.argv[1:]
short = a[a.index("-p") + 1] if "-p" in a else ""
schema = json.loads(a[a.index("--json-schema") + 1]) if "--json-schema" in a else {}
m = re.search(r"metrics/work/([0-9-]+)/([^\s/]+?)\.md", short)
date, name = (m.group(1), m.group(2)) if m else ("", "")
ctl = json.loads(Path("fake-cli.json").read_text(encoding="utf-8"))
log = open("fake-cli.log", "a", encoding="utf-8")
log.write(name + "\n")
def answer(obj):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)
if name.startswith("plan-") and name != "plan-missing":
    b = name[len("plan-"):]
    c = ctl.get("plan", {}).get(b, {})
    if "exit" in c:
        sys.exit(c["exit"])
    keys = schema["properties"]["decisions"]["required"]
    dec = {k: {"action": "article", "angle": f"{b} 面の判定 {k}", "purpose": "新情報", "rank": "small", "lead_score": 10,
               "merge_into": "", "claimed_slug": "", "reason": "", "note": ""} for k in keys}
    dec.update(c.get("decisions", {}))
    answer({"decisions": dec})
if name == "lead":
    c = ctl.get("lead", {})
    if "exit" in c:
        sys.exit(c["exit"])
    answer({"lead_slug": schema["properties"]["lead_slug"]["enum"][0]})
if name == "plan-missing":
    c = ctl.get("missing", {})
    rows = json.loads(Path(f"metrics/plan-index-{date}-missing.json").read_text(encoding="utf-8"))
    log.write("missing-keys " + " ".join(sorted(r["dedup_key"] for r in rows)) + "\n")
    if "exit" in c:
        sys.exit(c["exit"])
    Path(f"metrics/plan-{date}-missing.json").write_text(json.dumps(c.get("answer", {}), ensure_ascii=False), encoding="utf-8")
    print("拾い直し: 記事1本")
    sys.exit(0)
c = ctl.get("review", {"verdict": "approve", "blockers": [], "comments": []})
if "exit" in c:
    sys.exit(c["exit"])
answer(c)
'''


def _fake_claude(root: Path, ctl: dict) -> str:
    """root/bin に偽の claude を置き、root/fake-cli.json に答え方を書く。PATH の先頭に足す値を返す。"""
    b = root / "bin"
    b.mkdir(parents=True, exist_ok=True)
    (b / "claude").write_text(FAKE_CLAUDE, encoding="utf-8")
    (b / "claude").chmod(0o755)
    (root / "fake-cli.json").write_text(json.dumps(ctl, ensure_ascii=False), encoding="utf-8")
    return str(b)


def test_release_reads_latest_run(tmp: Path):
    """発行は、いちばん新しい組版の実行の最後の巡の校閲を読む(監査指摘 2026-10-14)。巡の番号は実行ごとに1から数え直すので、
    番号の最大を採ると、組み直し(--reuse-plan --carry-review)が1巡目で承認しても前の実行の5巡目の承認を読み、戻した記事を
    「承認後の変更」として発行を止める。逆に新しい実行が承認しなかったときに、中身の同じ古い承認へ戻ってもいけない。
    当番が組み直しに引き継ぐ校閲記録(oncall.latest_review)も同じものを選ぶ。"""
    import oncall
    import release
    d = "2026-10-10"
    posts, metrics = tmp / "docs" / "_posts", tmp / "metrics"
    posts.mkdir(parents=True)
    metrics.mkdir()
    (tmp / "docs" / "_editions").mkdir()
    (tmp / "docs" / "_editions" / f"{d}.md").write_text("---\nnumber: 1\ndate: 2026-10-10\n---\n", encoding="utf-8")
    (posts / f"{d}-old.md").write_text("---\nslug: old\ntitle: 承認済み\n---\n承認済みの記事\n", encoding="utf-8")
    saved = (compose.ROOT, pipelib.ROOT, release.ROOT, oncall.ROOT, compose.RUN_ID, os.environ.get("PATH", ""))
    try:
        compose.ROOT = pipelib.ROOT = release.ROOT = oncall.ROOT = tmp
        os.environ["PATH"] = _fake_claude(tmp, {}) + os.pathsep + saved[-1]
        # 前の実行(印を付ける前の形)が5巡目で承認した。そのあと組み直しが落とした記事を戻し、新しい実行の1巡目で承認した
        old = {"verdict": "approve", "blockers": [], "comments": [], "reviewed": [f"article:{d}-old.md", "paper"],
               "hashes": pipelib.review_manifest(d)}
        (metrics / f"review-{d}-5.json").write_text(json.dumps(old), encoding="utf-8")
        (posts / f"{d}-new.md").write_text("---\nslug: new\ntitle: 戻した記事\n---\n戻した記事\n", encoding="utf-8")
        compose.RUN_ID = "20261010T050000.000000"
        r = compose.claude_review(d, 1, targets=compose.carry_targets(d, old), editorial=False, paper=True, carry=old, strict=True)
        v = release.review_verdict(d)
        check(r.get("verdict") == "approve" and v == ("approve", f"review-{d}-1.json"),
              f"組み直しの新しい承認より、前の実行の後ろの巡を読んだ: 新しい1巡目 {r.get('verdict')} / 発行の判定 {v}")
        check(oncall.latest_review(d) == metrics / f"review-{d}-1.json", f"当番が引き継ぐ校閲記録が新しい実行のものでない: {oncall.latest_review(d)}")
        # 新しい実行が承認しなかった(担当が動かない)。中身は前の実行の承認と同じでも、古い承認へ戻らない
        for p in metrics.glob("review-*.json"):
            p.unlink()
        (posts / f"{d}-new.md").unlink()
        old_run = dict(old, run="20261010T030000.000000", hashes=pipelib.review_manifest(d))
        (metrics / f"review-{d}-5.json").write_text(json.dumps(old_run), encoding="utf-8")
        (tmp / "fake-cli.json").write_text(json.dumps({"review": {"exit": 1}}), encoding="utf-8")
        compose.RUN_ID = "20261010T053000.000000"
        r = compose.claude_review(d, 1, targets=[], editorial=False, paper=True, carry=old_run, strict=True)
        v = release.review_verdict(d)
        check(r.get("verdict") == "error" and v[0] != "approve" and f"review-{d}-1.json" in v[1],
              f"新しい実行が承認しなかったのに、前の実行の承認で発行する: 新しい1巡目 {r.get('verdict')} / 発行の判定 {v}")
        # 印の無い記録だけなら、巡の番号を数として比べる(10 は 9 より後)。読めない記録があれば、読めないと判定する
        for p in metrics.glob("review-*.json"):
            p.unlink()
        cur = pipelib.review_manifest(d)
        (metrics / f"review-{d}-9.json").write_text(json.dumps({"verdict": "block", "blockers": [{"file": "x", "issue": "y"}], "hashes": cur}), encoding="utf-8")
        (metrics / f"review-{d}-10.json").write_text(json.dumps({"verdict": "approve", "blockers": [], "hashes": cur}), encoding="utf-8")
        check(release.review_verdict(d) == ("approve", f"review-{d}-10.json"), f"印の無い記録で巡の番号を数として比べない: {release.review_verdict(d)}")
        (metrics / f"review-{d}-3.json").write_text("{壊れた", encoding="utf-8")
        check(release.review_verdict(d)[0] == "unreadable", f"読めない記録を飛ばして承認を採った: {release.review_verdict(d)}")
    finally:
        compose.ROOT, pipelib.ROOT, release.ROOT, oncall.ROOT, compose.RUN_ID = saved[:5]
        os.environ["PATH"] = saved[-1]


class _StopBeforeWriting(Exception):
    """選定の検査で、組版を執筆の直前で止める印。"""


def _compose_selection(root: Path, date: str, path_head: str) -> tuple[object, str]:
    """compose.main を選定(面別の選定 → 一面 → 形の修復 → 検証 → 取りこぼしの拾い直し → 選定記録)まで実際に走らせ、執筆の直前で
    止める。モデルの CLI は PATH の先頭(path_head)にあるもの。Git・排他・通知は差し替える。戻りは (終わり方, 標準出力)。"""
    import contextlib
    import io
    import signal
    names = ("ROOT", "job_lock", "checkout_edition_branch", "next_number", "notify", "escalate", "commit_and_push", "write_articles",
             "DEADLINE", "STARTED_ON", "STARTED_DATE")
    saved = {n: getattr(compose, n) for n in names}
    saved_other = (pipelib.ROOT, assemble.ROOT, assemble.SCHEDULED, list(pipelib.ANOMALIES), sys.argv, os.environ.get("PATH", ""),
                   signal.getsignal(signal.SIGTERM))

    def stop(*a, **k):
        raise _StopBeforeWriting()
    buf = io.StringIO()
    try:
        compose.ROOT = pipelib.ROOT = assemble.ROOT = root
        assemble.SCHEDULED = root / "stock" / "scheduled"
        compose.job_lock = lambda *a, **k: None
        compose.checkout_edition_branch = lambda *a, **k: True
        compose.next_number = lambda *a, **k: 1
        compose.notify = lambda *a, **k: True
        compose.escalate = lambda *a, **k: None
        compose.commit_and_push = lambda *a, **k: True
        compose.write_articles = stop
        sys.argv = ["compose.py", "--date", date]
        os.environ["PATH"] = path_head
        try:
            with contextlib.redirect_stdout(buf):
                end = compose.main()
        except (_StopBeforeWriting, OSError) as e:
            end = e
        return end, buf.getvalue()
    finally:
        for n, v in saved.items():
            setattr(compose, n, v)
        pipelib.ROOT, assemble.ROOT, assemble.SCHEDULED = saved_other[:3]
        pipelib.ANOMALIES[:] = saved_other[3]
        sys.argv = saved_other[4]
        os.environ["PATH"] = saved_other[5]
        signal.signal(signal.SIGTERM, saved_other[6])


def test_compose_selection_from_cli(tmp: Path):
    """選定を CLI の答えから計画の検証・選定記録まで実際に通す(監査指摘 2026-10-14: run_plan を常に空の計画にしても回帰テストが通った)。
    - 正常に答えた面の判定が計画に載り、一面・形の修復・検証を通って執筆の直前まで進む
    - 非0終了の面・形の崩れた判定の主題は判断済みにせず、拾い直しへ回す(拾い直しに渡るのはその主題だけ)
    - 拾い直しで記事にした主題は目的(期日の知らせ)を保ち、執筆の依頼文まで届く(以前は目的を捨てて「新情報」になった)
    - 選定記録は判定を得た主題だけ。拾い直しも答えなかった主題の候補は次の号が引き継ぐ
    - 選定の CLI が起動できない号は、全候補を読んだと記録しない(以前は起動前に全候補を記録し、翌号の引き継ぎが 0件だった)"""
    d, nxt = "2026-10-10", "2026-10-11"
    rows = [cand_row("ca1", "https://e.example/cg-a1", dedup_key="cg-a", facts=["CG の公演A 開催決定"]),
            cand_row("ca2", "https://e.example/cg-a2", dedup_key="cg-a", facts=["CG の公演A チケット先行"]),
            cand_row("cb1", "https://e.example/cg-b", dedup_key="cg-b", facts=["CG の新曲B 配信開始"]),
            cand_row("cx1", "https://e.example/cg-x", dedup_key="cg-x", verify="failed", facts=["確かめられない話"]),
            cand_row("ma1", "https://e.example/ml-a", brand="million", dedup_key="ml-a", deadline="2026-10-11",
                     facts=["ミリオンの抽選申込は10月11日23:59締切"]),
            cand_row("sa1", "https://e.example/sh-a", brand="shiny", dedup_key="sh-a", facts=["シャニの企画A 開始"]),
            cand_row("sb1", "https://e.example/sh-b", brand="shiny", dedup_key="sh-b", facts=["シャニの企画B 告知"])]
    usable = {"ca1", "ca2", "cb1", "ma1", "sa1", "sb1"}

    def setup(root: Path) -> None:
        for sub in ("candidates", "metrics", "docs/_posts", "docs/_editions", "stock/scheduled"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        (root / "candidates" / f"{d}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    # 1. 正常: cg 面は答える、million 面は非0終了、shiny 面は sh-b の判定だけ形が崩れる。一面の選定は答えない(機械で立てる)。
    #    拾い直しは ml-a を期日の知らせで記事にし、sh-b には答えない
    ok = tmp / "ok"
    setup(ok)
    bad_rank = {"action": "article", "angle": "崩れた判定", "purpose": "新情報", "rank": "huge", "lead_score": 0,
                "merge_into": "", "claimed_slug": "", "reason": "", "note": ""}
    head = _fake_claude(ok, {"plan": {"million": {"exit": 1}, "shiny": {"decisions": {"sh-b": bad_rank}}}, "lead": {"exit": 1},
                             "missing": {"answer": {"articles": [{"brand": "million", "rank": "small", "angle": "抽選申込の締切は明日",
                                                                  "purpose": "期日の知らせ", "lead_score": 5, "dedup_key": "ml-a"}],
                                                    "merge_into": [], "dropped": []}}})
    end, out = _compose_selection(ok, d, head + os.pathsep + os.environ.get("PATH", ""))
    check(isinstance(end, _StopBeforeWriting), f"選定が執筆の直前まで進まない(計画の検証で止まった・例外): {end!r} {out[-600:]}")
    log = (ok / "fake-cli.log").read_text(encoding="utf-8").splitlines() if (ok / "fake-cli.log").exists() else []
    check({"plan-cg", "plan-million", "plan-shiny", "lead", "plan-missing"} <= set(log) and "missing-keys ml-a sh-b" in log,
          f"拾い直しに渡ったのが、答えの無かった主題(ml-a・sh-b)だけでない: {log}")
    plan = json.loads((ok / "metrics" / f"plan-{d}.json").read_text(encoding="utf-8")) if (ok / "metrics" / f"plan-{d}.json").exists() else {}
    arts = {a.get("dedup_key"): a for a in plan.get("articles") or []}
    check(set(arts) == {"cg-a", "cg-b", "sh-a", "ml-a"} and arts["cg-a"]["candidate_ids"] == ["ca1", "ca2"]
          and arts["cg-b"]["angle"] == "cg 面の判定 cg-b" and arts["sh-a"]["angle"] == "shiny 面の判定 sh-a",
          f"面別の選定の答えが計画に載らない(または判断の無い主題を載せた): {sorted(arts)}")
    cands = {r["id"]: r for r in rows}
    check(plan and compose.validate_plan(plan, cands, {}) == [] and sum(a.get("rank") == "lead" for a in arts.values()) == 1,
          f"計画が検証を通らない: {compose.validate_plan(plan, cands, {}) if plan else '計画なし'}")
    ml = arts.get("ml-a") or {}
    check(ml.get("purpose") == "期日の知らせ" and ml.get("brand") == "million"
          and "- 目的: 期日の知らせ" in compose.article_prompt(d, ml, [cands["ma1"]], [], None),
          f"拾い直しで記事にした期日の知らせの目的が、計画・執筆の依頼文に届かない: {ml}")
    rp = ok / "metrics" / f"candidates-read-{d}.json"
    rec = json.loads(rp.read_text(encoding="utf-8"))["read"] if rp.exists() else None
    check(rec is not None and set(rec) == {"ca1", "ca2", "cb1", "cx1", "ma1", "sa1"},
          f"選定記録が、判定を得た主題(と選定に掛けない候補)になっていない: {sorted(rec or [])}")
    # 引き継ぎは write_checked(一時ファイルは pipelib.ROOT の下)で書くので、両方を一時領域へ向ける(本体に書かない。監査指摘)
    saved_root = (compose.ROOT, pipelib.ROOT)
    try:
        compose.ROOT = pipelib.ROOT = ok
        carry = compose.carry_late_candidates(nxt)
        got = [r["id"] for r in json.loads((ok / "candidates" / f"{nxt}.json").read_text(encoding="utf-8"))] \
            if (ok / "candidates" / f"{nxt}.json").exists() else []
    finally:
        compose.ROOT, pipelib.ROOT = saved_root
    check(got == ["sb1"] and carry, f"判定を得られなかった主題の候補を次の号が引き継がない(または判定済みまで引き継いだ): {got}")

    # 2. 選定の CLI が起動できない(FileNotFoundError: claude)。計画は出来ず、再実行も成功しなかった
    dead = tmp / "dead"
    setup(dead)
    (dead / "empty-bin").mkdir()
    end, out = _compose_selection(dead, d, str(dead / "empty-bin"))
    check(not (dead / "metrics" / f"plan-{d}.json").exists() or not json.loads(
        (dead / "metrics" / f"plan-{d}.json").read_text(encoding="utf-8")).get("articles"),
          f"CLI が無いのに計画ができた: {end!r}")
    rec = json.loads((dead / "metrics" / f"candidates-read-{d}.json").read_text(encoding="utf-8"))["read"] \
        if (dead / "metrics" / f"candidates-read-{d}.json").exists() else None
    check(rec is not None and not set(rec) & usable,
          f"選定が起動できなかった号で、選定に掛けていない候補を読んだと記録した(または記録が無く、次の号が遡らない): {rec}")
    try:
        compose.ROOT = pipelib.ROOT = dead
        compose.carry_late_candidates(nxt)
        got = {r["id"] for r in json.loads((dead / "candidates" / f"{nxt}.json").read_text(encoding="utf-8"))} \
            if (dead / "candidates" / f"{nxt}.json").exists() else set()
    finally:
        compose.ROOT, pipelib.ROOT = saved_root
    check(usable <= got, f"選定が起動できなかった号の素材を、次の号が引き継がない: {sorted(got)}")


def test_settled_drops_not_rewritten(tmp: Path):
    """重複(P1)で外した記事は、重複の相手が紙面にあるあいだ、組み直し(--reuse-plan)で書き直さない・救済で戻させない・
    次の号の選定に戻さない(実測 2026-10-12: 救済の組み直しが重複で外した上映会記事を書き直し、承認済みの記事と同じ重複がまた出た)。
    重複の相手が紙面から消えたら書き直す。書き方の指摘(R1 等)と既報(R10)で外した記事は今までどおり書き直す
    (R10 は稿への判断で、期日の知らせとして書き直せば載りうる。実測 2026-10-09 の永吉昴ガシャ記事。監査指摘)。"""
    import oncall
    for sub in ("candidates", "metrics", "docs/_posts"):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
    saved = (compose.ROOT, oncall.ROOT, dict(compose.DROP_LOG), dict(compose.DROP_SETTLED), compose.article_prompt, compose.prompt_file)
    d0, d1 = "2026-10-12", "2026-10-13"
    posts = tmp / "docs" / "_posts"
    try:
        compose.ROOT = oncall.ROOT = tmp
        compose.DROP_LOG.clear()
        compose.DROP_SETTLED.clear()
        slugs = ["keep", "dup", "orphan", "old", "fix"]
        for sl in slugs:
            (posts / f"{d0}-{sl}.md").write_text("---\n" + article_fm(sl, d0, title=sl, rank="medium", brand="765") + "---\n本文\n",
                                                 encoding="utf-8")
        cands = {f"c-{sl}": cand_row(f"c-{sl}", f"https://www.example.com/{sl}", dedup_key=sl, brand="765", facts=[sl])
                 for sl in slugs}
        (tmp / "candidates" / f"{d0}.json").write_text(json.dumps(list(cands.values())), encoding="utf-8")
        plan = {"articles": [{"slug": sl, "rank": "medium", "brand": "765", "candidate_ids": [f"c-{sl}"], "dedup_key": sl} for sl in slugs]}
        (tmp / "metrics" / f"plan-{d0}.json").write_text(json.dumps(plan), encoding="utf-8")
        written = [dict(a) for a in plan["articles"]]
        review = {"blockers": [
            {"file": f"docs/_posts/{d0}-dup.md", "rule_id": "P1", "repair": "drop_article",
             "issue": "同じ知らせが2本ある。765記事(slug: keep)と同じ上映会を伝えている。落とすのはこちら"},
            {"file": f"docs/_posts/{d0}-orphan.md", "rule_id": "P1", "repair": "drop_article",
             "issue": f"同じ知らせが2本ある。docs/_posts/{d0}-gone.md と同じ。落とすのはこちら"},
            {"file": f"docs/_posts/{d0}-fix.md", "rule_id": "R1", "repair": "drop_claim", "issue": "出典に無い店舗名"},
        ]}
        dropped, _ = compose.drop_blocked_articles(d0, review, written)
        # 既報(R10)は書き直しで執筆が見送った経路(revise_apply)と同じく、指摘ごと控える。指摘は 2026-10-09 の実例の形
        compose.log_drop(d0, "old", "書き直しで見送り", "NOT_NEWS 既報と同じ", posts / f"{d0}-old.md",
                         issues=[{"rule_id": "R10", "repair": "drop_article",
                                  "issue": "記事の内容は既報と同じで、新しい事実も当日のトリガーも無い。3日以内の締切の告知を目的とする"
                                           "記事でもない(本文に10/10 14:59の終了が無い)。"}])
        (posts / f"{d0}-old.md").unlink()
        compose.persist_drops(d0)
        rec = json.loads((tmp / "metrics" / f"drops-{d0}.json").read_text(encoding="utf-8"))
        check(sorted(dropped) == ["dup", "fix", "orphan"] and rec["dup"].get("settled") == {"rule": "P1", "dup_of": ["keep"]}
              and "settled" not in rec["old"] and "settled" not in rec["fix"],
              f"外れた記事の記録に、外した理由が話題に掛かること(規則と重複の相手)を残さない: {rec}")
        got = pipelib.settled_drops(d0, tmp)
        check(sorted(got) == ["dup"],
              f"重複の相手が紙面にある記事だけを、書き直さない対象にしていない(既報・相手の消えた重複・書き方の指摘まで入る): {got}")
        # 組み直し(--reuse-plan): 書き直さないのは、相手が紙面にある重複だけ
        asked = []

        class Stop(Exception):
            pass

        def fake_prompt(date, art, *a, **k):
            asked.append(art["slug"])
            return "x"

        def stop(*a, **k):
            raise Stop

        compose.article_prompt, compose.prompt_file = fake_prompt, stop
        try:
            compose.write_articles(d0, plan, cands, [], {}, reuse=True)
        except Stop:
            pass
        check(sorted(asked) == ["fix", "old", "orphan"],
              f"組み直しが、重複で外した記事を書き直す・既報(期日の知らせとして書き直せる)を書き直さない: {sorted(asked)}")
        # 救済: 戻す対象(戻らない記事に数える計画の記事)から外す。重複だけが外れた号では、戻す組み直しを当番に頼まない
        check(oncall.planned_slugs(d0) == ["keep", "orphan", "old", "fix"],
              f"救済が、重複で外した記事を戻らない記事に数える・既報を数えない: {oncall.planned_slugs(d0)}")
        check(compose.recover_pending(d0), "書き直せば戻る記事があるのに、戻す組み直しを頼まない")
        compose.DROP_LOG[d0] = {k: v for k, v in compose.DROP_LOG[d0].items() if k in ("dup", "old")}
        check(compose.recover_pending(d0), "既報で外した記事があるのに、戻す組み直しを頼まない")
        compose.DROP_LOG[d0] = {k: v for k, v in compose.DROP_LOG[d0].items() if k == "dup"}
        check(not compose.recover_pending(d0), "重複で外した記事だけなのに、戻す組み直しを頼む")
        # 次の号の選定: 重複の素材は戻さない。既報・相手の消えた重複・書き方の指摘の素材は戻す
        (tmp / "candidates" / f"{d1}.json").write_text("[]", encoding="utf-8")
        compose.carry_dropped_candidates(d1)
        ids = sorted(x["id"] for x in json.loads((tmp / "candidates" / f"{d1}.json").read_text(encoding="utf-8")))
        check(ids == ["c-fix", "c-old", "c-orphan"], f"重複で外した記事の素材を戻す・既報の素材を戻さない: {ids}")
        # 既報の印が残った記録(この修正の前に書かれた形)でも、既報だけでは書き直さない対象にしない
        rec = json.loads((tmp / "metrics" / f"drops-{d0}.json").read_text(encoding="utf-8"))
        rec["old"]["settled"] = {"rule": "R10"}
        (tmp / "metrics" / f"drops-{d0}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        check(sorted(pipelib.settled_drops(d0, tmp)) == ["dup"], "既報の印だけで、記事を書き直さない対象にする")
        # 重複の相手が紙面から消えたら、重複で外した記事は書き直す対象に戻る
        (posts / f"{d0}-keep.md").unlink()
        check(pipelib.settled_drops(d0, tmp) == {}, "重複の相手が紙面から消えても、重複で外した記事を書き直さない")
        # 記録が壊れていたら、全部を書き直す対象のまま(理由の分からない記事を戻さない側に倒さない)
        (tmp / "metrics" / f"drops-{d0}.json").write_text("{", encoding="utf-8")
        check(pipelib.settled_drops(d0, tmp) == {}, "外れた記事の記録が読めないのに、書き直さない記事を決める")
    finally:
        compose.ROOT, oncall.ROOT = saved[0], saved[1]
        compose.DROP_LOG.clear(); compose.DROP_LOG.update(saved[2])
        compose.DROP_SETTLED.clear(); compose.DROP_SETTLED.update(saved[3])
        compose.article_prompt, compose.prompt_file = saved[4], saved[5]


def test_carry_dropped_candidates_schema(tmp: Path):
    """前の号から戻した素材を書いた候補ファイルが、候補のスキーマ(lint と同じ)に合う(2026-10-14: 戻した印 carried がスキーマに無く、
    lint 赤で号が止まった)。続報予約の素材は、予約の項目(kind・subject・reserved_on 等)を持ち込まず候補の形にして写す。"""
    from jsonschema import Draft202012Validator
    for sub in ("candidates", "metrics", "docs/_posts", "stock/scheduled"):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
    saved_root, saved_log = compose.ROOT, dict(compose.DROP_LOG)
    try:
        compose.ROOT = tmp
        d0, d1 = "2026-10-13", "2026-10-14"
        (tmp / "candidates" / f"{d0}.json").write_text(json.dumps([
            {"id": "c1", "title": "ログボ", "brand": "gaku", "source_type": "報道", "url": "https://press.example/1",
             "found_at": "2026-10-12T18:32:50+09:00", "dedup_key": "login", "facts": ["ログボ"], "via": "explore", "verify": "confirmed"},
        ], ensure_ascii=False), encoding="utf-8")
        (tmp / "stock" / "scheduled" / f"{d0}.json").write_text(json.dumps([
            {"id": "sched-2026-10-13-tour-締切前", "dedup_key": "tour", "brand": "gaku", "subject": "ツアー", "kind": "締切前",
             "note": "受付締切の3日前", "reserved_on": "2026-09-20", "title": "ツアー受付", "url": "https://s.example/9",
             "source_type": "公式", "facts": ["受付は16日まで"], "via": "watch", "verify": "confirmed", "deadline": "2026-10-16",
             "src_candidate_id": "x9"},
            {"id": "sched-2026-10-13-nourl-終了", "dedup_key": "nourl", "brand": "gaku", "subject": "URL無し", "kind": "終了",
             "reserved_on": "2026-09-20", "facts": ["終了"]},
        ], ensure_ascii=False), encoding="utf-8")
        (tmp / "candidates" / f"{d1}.json").write_text(json.dumps([
            {"id": "n1", "title": "新", "brand": "cg", "source_type": "公式", "url": "https://n.example/1",
             "found_at": "2026-10-14T02:26:00+09:00", "dedup_key": "new", "facts": ["新"], "verify": "confirmed"},
        ], ensure_ascii=False), encoding="utf-8")
        (tmp / "metrics" / f"plan-{d0}.json").write_text(json.dumps({"articles": [
            {"slug": "login", "candidate_ids": ["c1"]}, {"slug": "tour", "candidate_ids": ["sched-2026-10-13-tour-締切前"]},
            {"slug": "nourl", "candidate_ids": ["sched-2026-10-13-nourl-終了"]}]}), encoding="utf-8")
        compose.DROP_LOG.clear()
        compose.DROP_LOG[d0] = {"login": ("ログボ", "執筆で見送り", "NO_PRIMARY_SOURCE"), "tour": ("ツアー", "校閲ブロック", "R4"),
                                "nourl": ("URL無し", "執筆で見送り", "NO_PRIMARY_SOURCE")}
        compose.persist_drops(d0)
        compose.carry_dropped_candidates(d1)
        rows = json.loads((tmp / "candidates" / f"{d1}.json").read_text(encoding="utf-8"))
        schema = json.loads((pipelib.ROOT / "schema" / "candidates.schema.json").read_text(encoding="utf-8"))
        errs = [f"{e.path}: {e.message}" for e in Draft202012Validator(schema).iter_errors(rows)]
        ids = sorted(x["id"] for x in rows)
        tour = next((x for x in rows if x["id"].startswith("sched-2026-10-13-tour")), {})
        check(not errs and ids == ["c1", "n1", "sched-2026-10-13-tour-締切前"] and tour.get("carried", {}).get("count") == 1
              and tour.get("title") == "ツアー受付" and tour.get("facts") == ["受付は16日まで"] and tour.get("event_date") == "2026-10-16",
              f"前の号から戻した素材の候補ファイルが候補のスキーマに合わない(lint 赤で号が止まる): {errs[:5]} {ids} {tour}")
    finally:
        compose.ROOT = saved_root
        compose.DROP_LOG.clear()
        compose.DROP_LOG.update(saved_log)


def test_carry_reservation_deadline(tmp: Path):
    """続報予約の素材の締切は、予約が指す締切(締切前・締切は出来事の日)。予約に写した元の話題の deadline(受付締切など別の締切)で
    翌号への戻しが締切切れと判定して捨てない(2026-10-15: 10/18 の入金期限を指すレトラの予約2件が、受付締切 10/11 の deadline で
    10/16 号に戻らなかった)。出来事の日を決められない予約や、締切でない予約も、号より前の元の deadline は渡さない。"""
    for sub in ("candidates", "metrics", "docs/_posts", "stock/scheduled"):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
    saved_root = compose.ROOT
    try:
        compose.ROOT = tmp
        d0, d1 = "2026-10-15", "2026-10-16"
        # 実データ stock/scheduled/2026-10-15.json のレトラ入金案内の形(facts は日付の要るものだけ)
        pay = ["受付期間: 2026年10月1日(木)〜2026年10月11日(日)23:59", "当落発表: 2026年10月13日(火)(予定)",
               "入金期間: 2026年10月13日(火)～2026年10月18日(日)23:59", "公演日時: 2026年11月11日(水)"]
        rows = [sched_row("sched-2026-10-15-letora-締切前", "2026-10-12", kind="締切前", dedup_key="letora", brand="dsva",
                          url="https://idolmaster-official.jp/news/01_20086", source_type="公式", verify="confirmed",
                          note="入金期限の3日前", facts=pay, event_date="2026-11-11", deadline="2026-10-11"),
                # 出来事の日を決められない締切前(4日後の 10/19 も書いてある): 号より前の元の deadline は渡さない
                sched_row("sched-2026-10-15-amb-締切前", "2026-10-12", kind="締切前", dedup_key="amb", url="https://e.example/amb",
                          facts=["受付は10月11日まで", "10月18日と10月19日に追加受付"], deadline="2026-10-11"),
                # 締切でない予約の、号より後の元の deadline はそのまま
                sched_row("sched-2026-10-15-open-開始", "2026-10-12", kind="開始", dedup_key="open", url="https://e.example/open",
                          facts=["10月15日開始、10月20日まで"], deadline="2026-10-20")]
        (tmp / "stock" / "scheduled" / f"{d0}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        got = {r["dedup_key"]: r.get("deadline") for r in compose.load_scheduled(d0)}
        check(got == {"letora": "2026-10-18", "amb": None, "open": "2026-10-20"},
              f"続報予約の素材の締切が、予約が指す締切にならない(元の話題の deadline のまま): {got}")
        check(json.loads((tmp / "stock" / "scheduled" / f"{d0}.json").read_text(encoding="utf-8")) == rows, "予約のファイルを書き換えた")
        plan = {"articles": [{"slug": "letora", "candidate_ids": [r["id"] for r in rows]}]}
        (tmp / "metrics" / f"plan-{d0}.json").write_text(json.dumps(plan), encoding="utf-8")
        (tmp / "metrics" / f"drops-{d0}.json").write_text(json.dumps({"letora": {
            "title": "レトラ入金", "stage": "校閲ブロック", "why": "R1", "candidate_ids": [r["id"] for r in rows]}}, ensure_ascii=False),
            encoding="utf-8")
        compose.carry_dropped_candidates(d1)
        out = json.loads((tmp / "candidates" / f"{d1}.json").read_text(encoding="utf-8"))
        ids = sorted(x["id"] for x in out)
        let = next((x for x in out if x["id"] == rows[0]["id"]), {})
        check(ids == sorted(r["id"] for r in rows) and let.get("deadline") == "2026-10-18" and let.get("carried", {}).get("from") == d0,
              f"入金期限前の予約の素材が、元の話題の受付締切で締切切れとされ翌号に戻らない: {ids} {let.get('deadline')}")
        for x in out:
            check(not x.get("deadline") or x["deadline"] >= d1, f"戻した素材に号より前の締切が残る: {x['id']} {x.get('deadline')}")
        # 翌号に同じ URL の候補(収集の拾い直し。受付締切 10/11 の deadline)があり、その号の選定も選ばなかった: まとめた候補は
        # 予約の締切 10/18 を持ち、翌々号へも戻る(監査指摘 reservation-deadline-merge: 既存候補の 10/11 が残り 10/17 号で捨てた)
        for sub in ("candidates", "metrics"):
            for f in (tmp / sub).glob("*.json"):
                f.unlink()
        d2 = "2026-10-17"
        (tmp / "metrics" / f"plan-{d0}.json").write_text(json.dumps(plan), encoding="utf-8")
        (tmp / "metrics" / f"drops-{d0}.json").write_text(json.dumps({"letora": {
            "title": "レトラ入金", "stage": "校閲ブロック", "why": "R1", "candidate_ids": [rows[0]["id"]]}}, ensure_ascii=False),
            encoding="utf-8")
        pre = {"id": "202610111234-watch-1", "title": "レトラ", "brand": "dsva", "dedup_key": "letora", "url": rows[0]["url"],
               "source_type": "公式", "verify": "confirmed", "found_at": "2026-10-15T08:00:00+09:00", "facts": pay[:2],
               "deadline": "2026-10-11"}
        (tmp / "candidates" / f"{d1}.json").write_text(json.dumps([pre], ensure_ascii=False), encoding="utf-8")
        compose.carry_dropped_candidates(d1)
        out1 = json.loads((tmp / "candidates" / f"{d1}.json").read_text(encoding="utf-8"))
        check([(x["id"], x.get("deadline")) for x in out1] == [(pre["id"], "2026-10-18")],
              f"同じ URL の候補へまとめた予約の素材の締切が、既存候補の受付締切のまま: {[(x['id'], x.get('deadline')) for x in out1]}")
        compose.carry_dropped_candidates(d1)       # 冪等
        check(json.loads((tmp / "candidates" / f"{d1}.json").read_text(encoding="utf-8")) == out1, "同じ号へ二度戻すと結果が変わる")
        (tmp / "metrics" / f"plan-{d1}.json").write_text('{"articles": []}', encoding="utf-8")
        compose.carry_dropped_candidates(d2)
        p2 = tmp / "candidates" / f"{d2}.json"
        out2 = json.loads(p2.read_text(encoding="utf-8")) if p2.exists() else []
        check([(x["id"], x.get("deadline")) for x in out2] == [(pre["id"], "2026-10-18")],
              f"同じ URL の候補へまとめた予約の素材が、入金期限 10/18 の前の {d2} 号に戻らない: {out2}")
        # 収集の拾い直し・前の号の未読の引き継ぎ(同じ規則): facts が同じでも遅い締切を採り、早い締切では縮めない
        a = {**pre, "facts": pay[:2]}
        check(pipelib.merge_candidate_facts(a, {**pre, "deadline": "2026-10-18"}) and a["deadline"] == "2026-10-18",
              f"同じページの候補をまとめると、遅い締切が落ちる: {a.get('deadline')}")
        check(not pipelib.merge_candidate_facts(a, {**pre, "deadline": "2026-10-11"}) and a["deadline"] == "2026-10-18",
              f"同じページの候補をまとめると、締切が早いほうへ戻る: {a.get('deadline')}")
        b = {k: v for k, v in pre.items() if k != "deadline"}
        check(pipelib.merge_candidate_facts(b, pre) and b.get("deadline") == "2026-10-11", "締切の無い候補へ、まとめた素材の締切が入らない")
    finally:
        compose.ROOT = saved_root


def test_carry_dropped_candidates(tmp: Path):
    """前の号の計画に入ったのに紙面に載らなかった記事の素材を、次の号の選定に戻す(2026-10-13: 報道を出典にしてよいと決めたログボが、
    翌号の選定に戻る経路が無かった)。組み直しで戻った記事は写さない。同じ素材は CARRY_DROPPED_MAX 回まで。冪等。選定に理由が見える。"""
    for sub in ("candidates", "metrics", "docs/_posts"):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
    saved_root, saved_log = compose.ROOT, dict(compose.DROP_LOG)
    try:
        compose.ROOT = tmp
        d0, d1 = "2026-10-13", "2026-10-14"
        (tmp / "candidates" / f"{d0}.json").write_text(json.dumps([
            cand_row("c1", "https://press.example/1", dedup_key="login", brand="gaku", facts=["ログボ"]),
            cand_row("c2", "https://e.example/2", dedup_key="back", brand="gaku", facts=["戻った"]),
            cand_row("c3", "https://e.example/3", dedup_key="tour", brand="gaku", facts=["三回目"],
                     carried={"from": "2026-10-12", "count": 2, "why": "2026-10-12号で記事にならなかった(執筆で見送り: SOURCE_MISMATCH)"}),
        ], ensure_ascii=False), encoding="utf-8")
        compose.DROP_LOG.clear()
        compose.DROP_LOG[d0] = {"login": ("ログボ", "執筆で見送り", "NO_PRIMARY_SOURCE 公式の告知に届かない"),
                                "back": ("戻った", "校閲ブロック", "R2"), "tour": ("三回目", "執筆で見送り", "SOURCE_MISMATCH")}
        plan = {"articles": [{"slug": "login", "candidate_ids": ["c1"]}, {"slug": "back", "candidate_ids": ["c2"]},
                             {"slug": "tour", "candidate_ids": ["c3"]}, {"slug": "sch", "candidate_ids": ["sched-9"]}]}
        (tmp / "metrics" / f"plan-{d0}.json").write_text(json.dumps(plan), encoding="utf-8")
        compose.persist_drops(d0)
        # 同じ号の組み直し(--reuse-plan)で一部が戻り、別の記事が落ちた: 以前の記録と合わせ、戻った記事だけを除く(監査指摘)
        (tmp / "docs" / "_posts" / f"{d0}-back.md").write_text("---\ntitle: 戻った\n---\n本文\n", encoding="utf-8")   # 組み直しで戻った
        compose.DROP_LOG[d0] = {"sch": ("予約の記事", "校閲ブロック", "R4")}
        compose.persist_drops(d0)
        rec = json.loads((tmp / "metrics" / f"drops-{d0}.json").read_text(encoding="utf-8"))
        check(sorted(rec) == ["login", "sch", "tour"] and rec["login"]["candidate_ids"] == ["c1"],
              f"外れた記事の記録が、以前の記録と合わさらない・戻った記事を除かない: {sorted(rec)}")
        # 前の号の続報予約から選んだ記事の素材も戻す(日付は前の号で使った値のまま。監査指摘)
        (tmp / "stock" / "scheduled").mkdir(parents=True)
        (tmp / "stock" / "scheduled" / f"{d0}.json").write_text(json.dumps([sched_row("sched-9", "2026-10-10", url="https://s.example/9",
                                                                                     dedup_key="sch", brand="gaku", kind="締切前",
                                                                                     facts=["予約", "受付は10月16日まで"])]),
                                                               encoding="utf-8")
        (tmp / "candidates" / f"{d1}.json").write_text(json.dumps([cand_row("n1", "https://n.example/1", facts=["新"])]),
                                                       encoding="utf-8")
        log = compose.carry_dropped_candidates(d1)
        rows = json.loads((tmp / "candidates" / f"{d1}.json").read_text(encoding="utf-8"))
        ids = [x["id"] for x in rows]
        c1 = next((x for x in rows if x["id"] == "c1"), {})
        s9 = next((x for x in rows if x["id"] == "sched-9"), {})
        check(sorted(ids) == ["c1", "n1", "sched-9"] and c1.get("carried", {}).get("count") == 1 and "NO_PRIMARY_SOURCE" in c1["carried"]["why"]
              and s9.get("event_date") == "2026-10-16" and log,
              f"見送った記事の素材(予約を含む)を次の号に戻さない・戻った記事や上限を超えた素材まで写した: {ids} {c1.get('carried')} {s9} {log}")
        compose.carry_dropped_candidates(d1)
        check(sorted(x["id"] for x in json.loads((tmp / "candidates" / f"{d1}.json").read_text(encoding="utf-8"))) == ["c1", "n1", "sched-9"],
              "2回呼ぶと素材が重なる(冪等でない)")
        # 同じページを毎号拾い直す素材: この号の同じ URL の候補へ前の号の事実をマージし、回数を付け続ける(上限を超えても回数は残し、
        # 選定には見せない)。回数が 1→2→無し→1 と戻らない(監査指摘)
        chain = [("2026-10-20", None), ("2026-10-21", None), ("2026-10-22", None), ("2026-10-23", None)]
        prev_row = cand_row("p0", "https://press.example/same", dedup_key="same", brand="gaku", facts=["期限は15日"])
        (tmp / "candidates" / "2026-10-19.json").write_text(json.dumps([prev_row], ensure_ascii=False), encoding="utf-8")
        (tmp / "metrics" / "plan-2026-10-19.json").write_text(json.dumps({"articles": [{"slug": "same", "candidate_ids": ["p0"]}]}), encoding="utf-8")
        compose.DROP_LOG["2026-10-19"] = {"same": ("同じ", "執筆で見送り", "NO_PRIMARY_SOURCE")}
        compose.persist_drops("2026-10-19")
        counts, shown = [], []
        prev = "2026-10-19"
        for k, (day, _) in enumerate(chain):
            nid = f"q{k}"
            (tmp / "candidates" / f"{day}.json").write_text(json.dumps([cand_row(nid, "https://press.example/same", dedup_key="same",
                                                                                  brand="gaku", facts=[f"別の事実{k}"])], ensure_ascii=False),
                                                            encoding="utf-8")
            compose.carry_dropped_candidates(day)
            row = json.loads((tmp / "candidates" / f"{day}.json").read_text(encoding="utf-8"))[0]
            counts.append((row.get("carried") or {}).get("count"))
            b, _, _ = compose.write_plan_index(day, {row["id"]: row}, {})
            shown.append(bool(next((s for s in b.get("gaku", []) if s["dedup_key"] == "same"), {}).get("carried")))
            if k == 0:
                check("期限は15日" in row["facts"], f"同じページの候補に、前の号の事実をマージしない: {row['facts']}")
            (tmp / "metrics" / f"plan-{day}.json").write_text(json.dumps({"articles": [{"slug": "same", "candidate_ids": [nid]}]}), encoding="utf-8")
            compose.DROP_LOG[day] = {"same": ("同じ", "執筆で見送り", "NO_PRIMARY_SOURCE")}
            compose.persist_drops(day)
            prev = day
        check(counts == [1, 2, 3, 4] and shown == [True, True, False, False],
              f"毎号拾い直す素材の回数が戻る・上限を超えても選定に見せる: {counts} {shown}")
        check("metrics/drops-2026-10-13.json" in compose.own_artifacts("2026-10-13"), "途中終了の後始末が外れた記事の記録をコミットしない")
        # 同じ URL の素材が複数(通常の候補と予約など): 全部の事実をまとめ、回数は最大値から一度だけ増やす。順序に依らない(監査指摘)
        results = []
        for order in (["m1", "m2"], ["m2", "m1"]):
            d2, d3 = "2026-11-01", "2026-11-02"
            mats = {"m1": cand_row("m1", "https://p.example/same2", dedup_key="s2", brand="gaku", facts=["事実A"]),
                    "m2": cand_row("m2", "https://p.example/same2", dedup_key="s2", brand="gaku", facts=["事実B"],
                                   carried={"from": "2026-10-31", "count": 1, "why": "2026-10-31号で記事にならなかった(執筆で見送り)"})}
            (tmp / "candidates" / f"{d2}.json").write_text(json.dumps([mats[i] for i in order], ensure_ascii=False), encoding="utf-8")
            (tmp / "metrics" / f"plan-{d2}.json").write_text(json.dumps({"articles": [{"slug": "s2", "candidate_ids": order}]}), encoding="utf-8")
            (tmp / "metrics" / f"drops-{d2}.json").unlink(missing_ok=True)
            (tmp / "candidates" / f"{d3}.json").unlink(missing_ok=True)
            compose.DROP_LOG[d2] = {"s2": ("同じ2", "執筆で見送り", "NO_PRIMARY_SOURCE")}
            compose.persist_drops(d2)
            compose.carry_dropped_candidates(d3)
            got = json.loads((tmp / "candidates" / f"{d3}.json").read_text(encoding="utf-8"))
            results.append([(x["id"], sorted(x["facts"]), x["carried"]["count"]) for x in got])
        check(results[0] == results[1] == [("m1", ["事実A", "事実B"], 2)],
              f"同じ URL の2件目以降の事実が戻らない・回数が最大値から増えない・順序で結果が変わる: {results}")
        # 候補ファイルの無い号(予約だけで組んだ)でも、予約の素材は戻す。戻した予約は、校閲にも執筆と同じ素材として載る(監査指摘)
        d4, d5 = "2026-11-10", "2026-11-11"
        (tmp / "stock" / "scheduled" / f"{d4}.json").write_text(json.dumps([sched_row("sched-k", "2026-11-07", url="https://s.example/k",
                                                                                     dedup_key="k", brand="gaku", kind="締切前",
                                                                                     facts=["予約K", "申込は11月13日まで"])]),
                                                               encoding="utf-8")
        (tmp / "metrics" / f"plan-{d4}.json").write_text(json.dumps({"articles": [{"slug": "k", "candidate_ids": ["sched-k"]}]}), encoding="utf-8")
        compose.DROP_LOG[d4] = {"k": ("予約K", "校閲ブロック", "R4")}
        compose.persist_drops(d4)
        compose.carry_dropped_candidates(d5)
        got = json.loads((tmp / "candidates" / f"{d5}.json").read_text(encoding="utf-8"))
        check([x["id"] for x in got] == ["sched-k"] and got[0]["event_date"] == "2026-11-13",
              f"候補ファイルの無い号の予約の素材が戻らない(日付は前の号の値): {got}")
        (tmp / "docs" / "_posts" / f"{d5}-k.md").write_text("---\ntitle: k\ncandidate_ids:\n- sched-k\n---\n本文\n", encoding="utf-8")
        hint = compose.review_hint(d5, f"{d5}-k.md", {})
        check("予約K" in hint and "予約に見つからない" not in hint, f"戻した予約の素材が校閲に渡らない: {hint[-200:]}")
        # 戻した素材を前の号の選定がまた選ばなかった: 外れた記事の記録に載らなくても、締切までは上限の回数まで戻す
        # (2026-10-14: 戻したログボを選定が外し、締切の 10/15 号に戻らなかった)。選ばれた・締切を過ぎた・戻した印の無い素材は戻さない
        da, db, dc = "2026-11-20", "2026-11-21", "2026-11-22"
        tag = {"from": "2026-11-19", "count": 1, "why": "2026-11-19号で記事にならなかった(執筆で見送り)"}
        (tmp / "candidates" / f"{da}.json").write_text(json.dumps([
            cand_row("r1", "https://press.example/r1", dedup_key="r1", deadline=dc, carried=tag),
            cand_row("r2", "https://press.example/r2", dedup_key="r2", deadline=da, carried=tag),
            cand_row("r3", "https://press.example/r3", dedup_key="r3", carried=tag),
            cand_row("r4", "https://press.example/r4", dedup_key="r4"),
            cand_row("r5", "https://press.example/r5", dedup_key="r5", deadline=da),
        ], ensure_ascii=False), encoding="utf-8")
        (tmp / "metrics" / f"plan-{da}.json").write_text(json.dumps({"articles": [{"slug": "r3", "candidate_ids": ["r3"]},
                                                                                  {"slug": "r5", "candidate_ids": ["r5"]}]}), encoding="utf-8")
        # 外れた記事の経路でも、締切を過ぎた素材は戻さない(監査指摘)
        (tmp / "metrics" / f"drops-{da}.json").write_text(json.dumps({"r5": {"title": "r5", "stage": "執筆で見送り", "why": "NO_PRIMARY_SOURCE",
                                                                          "candidate_ids": ["r5"]}}), encoding="utf-8")
        compose.carry_dropped_candidates(db)
        got = json.loads((tmp / "candidates" / f"{db}.json").read_text(encoding="utf-8"))
        check([(x["id"], x["carried"]["from"], x["carried"]["count"]) for x in got] == [("r1", da, 2)] and "選定" in got[0]["carried"]["why"],
              f"選定が選ばなかった戻し素材を次の号に戻さない・選ばれた/締切を過ぎた/印の無い素材まで戻した: {got}")
        (tmp / "metrics" / f"plan-{db}.json").write_text(json.dumps({"articles": []}), encoding="utf-8")
        compose.carry_dropped_candidates(dc)
        check(not (tmp / "candidates" / f"{dc}.json").exists(), "選定が選ばなかった戻し素材を、上限を超えても戻し続ける")
        by_brand, _, _ = compose.write_plan_index(d1, {x["id"]: x for x in rows}, {})
        subj = next((s for s in by_brand.get("gaku", []) if s["dedup_key"] == "login"), {})
        # 選定には印だけを見せ、前の号の理由は見せない(選定が前の号の理由で外した。2026-10-14 のログボ)
        check("記事にならず" in str(subj.get("carried")) and "NO_PRIMARY_SOURCE" not in str(subj.get("carried"))
              and "公式の告知" not in str(subj.get("carried")),
              f"選定に、前の号で記事にならなかった印が見えない・前の号の理由まで見せている: {subj}")
        rules = compose.plan_rules()
        check("不採用の理由にしない" in rules and "同じ理由で記事にならない" not in rules and "ほかの主題と同じ基準" in rules,
              "選定の規則が、前の号で記事にならなかった主題を前の号の理由で外させる・既報などの基準を上書きして選ばせる")
    finally:
        compose.ROOT = saved_root
        compose.DROP_LOG.clear()
        compose.DROP_LOG.update(saved_log)
    rules = (pipelib.ROOT / "prompts" / "plan-rules.md").read_text(encoding="utf-8")
    check("`carried`" in rules, "選定の規則に、前の号で記事にならなかった主題の扱いが無い")


def test_carry_not_news_reason(tmp: Path):
    """前の号で「既報と同じ・新しい事実も当日のトリガーも無い」(執筆・書き直しの NOT_NEWS、校閲の R10)で外れた素材は、戻すときにその理由を
    選定に見せる(2026-10-17〜19: 既報のラジオ・SideM フェアを、別アカウントの投稿で拾い直したため既報につながらず、理由も隠したので
    3号続けて新情報として選び、毎号同じ理由で落とした)。出典など規則で変わる理由は見せないまま。選定が外した後の戻しでも理由を引き継ぐ。"""
    for sub in ("candidates", "metrics", "docs/_posts"):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
    saved_root, saved_log = compose.ROOT, dict(compose.DROP_LOG)
    try:
        compose.ROOT = tmp
        d0, d1, d2 = "2026-10-18", "2026-10-19", "2026-10-20"
        (tmp / "candidates" / f"{d0}.json").write_text(json.dumps([
            cand_row("radio", "https://x.com/imas_ch/status/1", dedup_key="radio", brand="general", facts=["10月21日配信"]),
            cand_row("fair", "https://x.com/shop/status/2", dedup_key="fair", brand="general", facts=["25日まで"]),
            cand_row("src", "https://fan.example/3", dedup_key="src", brand="general", facts=["二次情報"]),
        ], ensure_ascii=False), encoding="utf-8")
        compose.DROP_LOG.clear()
        compose.DROP_LOG[d0] = {"radio": ("ラジオ", "書き直しで見送り", "NOT_NEWS 10月16日号ですでに報じられており、新事実もトリガーも無い"),
                                "fair": ("フェア", "校閲ブロック", "R10 既報と同じ内容で、新しい事実も当日のトリガーも無い(10/3号)"),
                                "src": ("出典", "執筆で見送り", "NO_PRIMARY_SOURCE 公式の告知に届かない")}
        (tmp / "metrics" / f"plan-{d0}.json").write_text(json.dumps({"articles": [
            {"slug": k, "candidate_ids": [k]} for k in ("radio", "fair", "src")]}), encoding="utf-8")
        compose.persist_drops(d0)
        compose.carry_dropped_candidates(d1)
        rows = {x["id"]: x for x in json.loads((tmp / "candidates" / f"{d1}.json").read_text(encoding="utf-8"))}
        by_brand, _, _ = compose.write_plan_index(d1, rows, {})
        shown = {s["dedup_key"]: str(s.get("carried") or "") for s in by_brand.get("general", [])}
        check("10月16日号ですでに報じられており" in shown.get("radio", "") and "既報と同じ" in shown.get("radio", "")
              and "10/3号" in shown.get("fair", "") and "記事にならず" in shown.get("src", "")
              and "公式の告知" not in shown.get("src", "") and "NO_PRIMARY_SOURCE" not in shown.get("src", ""),
              f"既報と同じとして外れた理由を選定に見せない・出典の理由まで見せている: {shown}")
        # この号の選定が外した(計画に入らなかった)→ 次の号へ戻すときも、既報と同じという理由を引き継ぐ
        (tmp / "metrics" / f"plan-{d1}.json").write_text(json.dumps({"articles": [{"slug": "src", "candidate_ids": ["src"]}]}),
                                                         encoding="utf-8")
        compose.carry_dropped_candidates(d2)
        got = {x["id"]: x for x in json.loads((tmp / "candidates" / f"{d2}.json").read_text(encoding="utf-8"))}
        b2, _, _ = compose.write_plan_index(d2, got, {})
        s2 = {s["dedup_key"]: str(s.get("carried") or "") for s in b2.get("general", [])}
        check(sorted(got) == ["fair", "radio"] and "選定" in got["radio"]["carried"]["why"]
              and "10月16日号ですでに報じられており" in s2.get("radio", ""),
              f"選定が外した後の戻しで、既報と同じという理由を落とした: {s2} {[(k, v['carried']) for k, v in got.items()]}")
        rules = compose.plan_rules()
        check("既報と同じ内容で、新しい事実も当日のトリガーも無いとして外れた" in rules and "不採用の理由にしない" in rules,
              "選定の規則に、既報と同じとして外れた主題の扱いが無い")
    finally:
        compose.ROOT = saved_root
        compose.DROP_LOG.clear()
        compose.DROP_LOG.update(saved_log)


def test_carry_late_candidates(tmp: Path):
    """前の号の選定が候補を読んだ後に、その号の候補ファイルへ入った候補(当番の取り直しが組版の開始後〜06:00 に終わった)を、
    次の号の候補へ写す。写さないと、収集は 06:00 前の分を前の号へ入れ、組版は自分の号のファイルしか読まないので、
    どの号の選定にも掛からない(当番の指摘 b2a9e39466)。読んだ後に足された事実は、次の号が同じページを拾い直していても
    その候補へ増分マージする(次の号の候補にその事実があるとは限らない。監査指摘 late-facts-url-dedup)。冪等"""
    import inspect
    cdir, mdir = tmp / "candidates", tmp / "metrics"
    cdir.mkdir(parents=True)
    mdir.mkdir(parents=True)
    w = lambda d, rows: (cdir / f"{d}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    r = lambda d: json.loads((cdir / f"{d}.json").read_text(encoding="utf-8"))
    c = lambda i, u, n=1, v="confirmed", base=None: cand_row(i, f"https://e.example/{u}", verify=v,
                                                             facts=[f"{base or i}-{k}" for k in range(n)])
    saved = compose.ROOT
    try:
        compose.ROOT = tmp
        w("2026-10-10", [c("a1", "1"), c("a2", "2"), c("a5", "5", 3)])
        compose.record_candidates_read("2026-10-10", {"a1": c("a1", "1"), "a2": c("a2", "2"), "a5": c("a5", "5", 3),
                                                      "sched-x": {"id": "sched-x"}})
        rec = json.loads(compose.read_record_path("2026-10-10").read_text(encoding="utf-8"))["read"]
        check(sorted(rec) == ["a1", "a2", "a5"] and rec["a1"] == {"url": "https://e.example/1", "facts": [compose.fact_key("a1-0")]},
              f"選定記録が候補ファイルの id・URL・読んだ事実になっていない(予約を混ぜた・事実が無い): {rec}")
        # 選定が読んだ後: 新しい候補 a3、既存の a1 に事実が足された、10-11 で拾い直した URL の a4(事実は同じ)、
        # 10-11 で拾い直した URL の a5 に、10-11 の候補に無い事実が足された(10-11 の b5 は事実が少なく、裏取りは unconfirmed)
        w("2026-10-10", [c("a1", "1", 2), c("a2", "2"), c("a3", "3"), c("a4", "4", base="a4"),
                         c("a5", "5", 4, v="unconfirmed")])
        w("2026-10-11", [c("b1", "4", base="a4"), c("b2", "6"), c("b5", "5", 2, base="a5")])
        log = compose.carry_late_candidates("2026-10-11")
        got = {x["id"]: x["facts"] for x in r("2026-10-11")}
        check(got == {"b1": ["a4-0"], "b2": ["b2-0"], "b5": ["a5-0", "a5-1", "a5-3"], "a1": ["a1-0", "a1-1"], "a3": ["a3-0"]}
              and len(log) == 2,
              f"選定の後に入った候補・足された事実を次の号へ渡していない(または読んだ事実まで足した・拾い直した URL の候補を重ねた): {got} {log}")
        b5 = next(x for x in r("2026-10-11") if x["id"] == "b5")
        check(b5["verify"] == "unconfirmed", f"足した事実の裏取りを引き継いでいない(弱いほうを採っていない): {b5}")
        check(compose.carry_late_candidates("2026-10-11") == [] and len(r("2026-10-11")) == 5, "2回目で二重に写した")
        # 10-11 の選定より前に、写した a3 の元へさらに事実が足された: 同じ id の写しへ増分だけ足す
        w("2026-10-10", [c("a1", "1", 2), c("a2", "2"), c("a3", "3", 2), c("a4", "4", base="a4"),
                         c("a5", "5", 4, v="unconfirmed")])
        log = compose.carry_late_candidates("2026-10-11")
        a3 = [x for x in r("2026-10-11") if x["id"] == "a3"]
        check(len(a3) == 1 and a3[0]["facts"] == ["a3-0", "a3-1"] and log,
              f"写した候補の元に足された事実を、同じ id の写しへ足していない(または候補を重ねた): {a3}")
        # 次の号が選定したら、写した候補・足した事実は D+2 で二度と写らない。記録の無い号からは遡らない
        compose.record_candidates_read("2026-10-11", {x["id"]: x for x in r("2026-10-11")})
        w("2026-10-12", [c("d1", "9")])
        check(compose.carry_late_candidates("2026-10-12") == [] and len(r("2026-10-12")) == 1,
              f"選定の済んだ候補を次の次の号へまた写した: {r('2026-10-12')}")
        # 10-11 の選定の後に、10-10 の a5 へまた事実が足された: 10-12 の候補へ写す(10-11 の選定は読んでいない)
        w("2026-10-10", [c("a1", "1", 2), c("a2", "2"), c("a3", "3", 2), c("a4", "4", base="a4"),
                         c("a5", "5", 5, v="unconfirmed")])
        log = compose.carry_late_candidates("2026-10-12")
        got = {x["id"]: x["facts"] for x in r("2026-10-12")}
        check(set(got) == {"d1", "a5"} and log, f"2号前の候補に足された未読の事実を写していない: {got}")
        w("2026-10-13", [])
        compose.read_record_path("2026-10-11").unlink()
        compose.read_record_path("2026-10-12").unlink(missing_ok=True)
        compose.read_record_path("2026-10-10").unlink()
        check(compose.carry_late_candidates("2026-10-13") == [], "選定記録の無い号から候補を写した")
    finally:
        compose.ROOT = saved
    src = inspect.getsource(compose.main)
    check(src.index("carry_late_candidates(") < src.index("load_window_candidates("),
          "前の号の遅れた候補を、この号の候補を読む前に写していない")
    plan = src.index("run_plan(")
    check(src.index("args.reuse_plan and plan_saved.exists()") < src.index("record_candidates_read(") < plan,
          "選定記録(空の記録)を、選定をやり直すとき(--reuse-plan でない)の選定の直前に書いていない")
    own = compose.own_artifacts("2026-10-11")
    check("metrics/candidates-read-2026-10-11.json" in own and "candidates/2026-10-11.json" in own,
          "途中終了の commit に選定記録・写した候補が含まれない")


def test_oncall_ensure_edition(tmp: Path):
    """取り込み先の号が origin にだけある(新しい clone・ローカル branch を消したあと)なら、ローカルを作って揃える(監査指摘 r88)。
    origin に無ければ問題を返し、ローカルが origin と食い違えば問題を返す。"""
    import oncall
    tmp.mkdir(parents=True, exist_ok=True)
    remote, work = tmp / "remote.git", tmp / "work"
    g = lambda *a, cwd=work: subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, capture_output=True)
    g("config", "user.email", "t@example"); g("config", "user.name", "t")
    (work / "a").write_text("1", encoding="utf-8"); g("add", "a"); g("commit", "-q", "-m", "1")
    g("remote", "add", "origin", str(remote)); g("push", "-q", "origin", "main")
    g("branch", "edition/2026-09-27"); g("push", "-q", "origin", "edition/2026-09-27"); g("branch", "-D", "edition/2026-09-27")
    check(oncall.ensure_edition("edition/2026-09-27", work) == "", "origin にだけある号の branch を揃えられない")
    check(g("rev-parse", "edition/2026-09-27").stdout == g("rev-parse", "origin/edition/2026-09-27").stdout, "作ったローカルが origin と違う")
    check("origin に無い" in oncall.ensure_edition("edition/2026-09-28", work), "無い号を問題として返さない")
    (work / "a").write_text("2", encoding="utf-8"); g("checkout", "-q", "edition/2026-09-27"); g("commit", "-q", "-am", "2")
    check("食い違う" in oncall.ensure_edition("edition/2026-09-27", work), "ローカルが進んでいるのに揃ったことにした")
    # 遅れているだけ(取り込みの push が届いたか確かめられずに巻き戻した 等)なら、checkout 中でもそうでなくても早送りで揃える
    # (監査指摘 2026-10-14)
    g("push", "-q", "origin", "edition/2026-09-27")
    for checked_out in (True, False):
        if checked_out:
            g("reset", "-q", "--hard", "HEAD~1")
        else:
            g("checkout", "-q", "main"); g("branch", "-f", "edition/2026-09-27", "main")
        check(oncall.ensure_edition("edition/2026-09-27", work) == ""
              and g("rev-parse", "edition/2026-09-27").stdout == g("rev-parse", "origin/edition/2026-09-27").stdout
              and not g("status", "--porcelain").stdout and (work / "a").read_text(encoding="utf-8") == ("2" if checked_out else "1"),
              f"遅れているだけのローカルを早送りで揃えない(checkout 中 {checked_out})")
    # 通信の停滞(timeout)は traceback で落とさず「前提の確認に失敗」(RuntimeError)にする(監査指摘 r89)
    saved_sh = oncall.sh
    try:
        def slow(args, cwd, timeout=600):
            raise subprocess.TimeoutExpired(args, timeout)
        oncall.sh = slow
        try:
            oncall.ensure_edition("edition/2026-09-27", work)
            check(False, "通信の timeout が RuntimeError にならない")
        except RuntimeError as e:
            check("応答しない" in str(e), f"timeout の説明: {e}")
    finally:
        oncall.sh = saved_sh


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
        oncall.restore_edition = lambda d, e, b, u=None: calls.append(("restore", b))
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


def test_oncall_restore_to_backup(tmp: Path):
    """控えへの復元は、控えを作ったあとに commit・push された記事・台帳の変更も控えの中身へ戻し、ローカルとリモートの号を
    控えのハッシュに揃える(監査指摘 2026-10-14: 控えのあとに未追跡ファイルだけを足す試験では、控えでなく HEAD へ戻す誤りを見逃した)。"""
    import oncall
    date, edition = "2026-10-14", "edition/2026-10-14"
    tmp.mkdir(parents=True, exist_ok=True)
    remote, work = tmp / "remote.git", tmp / "work"
    g = lambda *a: subprocess.run(["git", *a], cwd=work, capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", edition, str(work)], check=True, capture_output=True)
    g("config", "user.email", "t@example"); g("config", "user.name", "t")
    post, ledger = work / "docs" / "_posts" / f"{date}-keep.md", work / "stock" / "stories.yml"
    post.parent.mkdir(parents=True); ledger.parent.mkdir(parents=True)
    post.write_text("APPROVED", encoding="utf-8"); ledger.write_text("stories: [APPROVED]\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-q", "-m", "approved")
    g("remote", "add", "origin", str(remote)); g("push", "-q", "-u", "origin", edition)
    saved = oncall.ROOT
    try:
        oncall.ROOT = work
        before = oncall.untracked_files()
        backup = oncall.backup_edition(date, edition)
        approved = g("rev-parse", backup)
        # 組み直しが記事と台帳を書き換えて commit・push し、未追跡の生成物も残した(控えと HEAD の中身が違う)
        post.write_text("BROKEN", encoding="utf-8"); ledger.write_text("stories: [BROKEN]\n", encoding="utf-8")
        g("commit", "-q", "-am", "bad rerun"); g("push", "-q", "origin", edition)
        (work / "docs" / "_posts" / f"{date}-new.md").write_text("new", encoding="utf-8")
        oncall.restore_edition(date, edition, backup, before)
        tip = subprocess.run(["git", "ls-remote", str(remote), f"refs/heads/{edition}"], capture_output=True, text=True).stdout.split()
        check(post.read_text(encoding="utf-8") == "APPROVED" and ledger.read_text(encoding="utf-8") == "stories: [APPROVED]\n"
              and not (work / "docs" / "_posts" / f"{date}-new.md").exists(),
              f"控えの中身(記事・台帳)に戻らない: {post.read_text(encoding='utf-8')!r} / {ledger.read_text(encoding='utf-8')!r}")
        check(g("rev-parse", edition) == approved and tip[:1] == [approved] and g("branch", "--show-current") == edition
              and not g("status", "--porcelain"),
              f"ローカル・リモートの号が控えのハッシュに揃わない(控え {approved[:10]} / ローカル {g('rev-parse', edition)[:10]} / リモート {tip[:1]})")
    except (RuntimeError, subprocess.CalledProcessError) as e:
        check(False, f"控えへの復元が失敗した: {e}")
    finally:
        oncall.ROOT = saved


def test_oncall_merge_push_settles(tmp: Path):
    """当番の取り込み(main・号への merge と push)は1つの取引。push が非0で終わっても、時間切れ・例外で抜けても、リモートを見て、
    届いていれば揃えて続け、届いていなければ巻き戻し、号のブランチで終える。確かめられなければ巻き戻し、届いていた分は次の当番が
    早送りで揃える(監査指摘 2026-10-14: main の push の時間切れで巻き戻しを通らず、ローカルの main だけが進んだまま残り、
    次の当番も前提の検査で止まって取り込めなかった)。本物の _main を一時リポジトリで走らせ、push と確かめる通信だけに障害を入れる。"""
    import oncall
    date, edition = "2026-10-14", "edition/2026-10-14"
    tmp.mkdir(parents=True, exist_ok=True)

    def g(root, *a):
        return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True).stdout.strip()

    def ok(root, *a):
        return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True).returncode == 0

    def repo(name):
        root, remote = tmp / name, tmp / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
        g(root, "config", "user.email", "t@example"); g(root, "config", "user.name", "t")
        (root / "scripts").mkdir(); (root / "metrics").mkdir()
        (root / ".gitignore").write_text("metrics/oncall-*\nmetrics/jobs.lock\nmetrics/work/\n", encoding="utf-8")
        (root / "scripts" / "x.py").write_text("x = 0\n", encoding="utf-8")
        g(root, "add", "-A"); g(root, "commit", "-q", "-m", "base")
        g(root, "remote", "add", "origin", str(remote)); g(root, "push", "-q", "-u", "origin", "main")
        g(root, "checkout", "-q", "-b", edition)
        (root / "paper.txt").write_text("approved", encoding="utf-8")      # 号の紙面(main には無い)
        g(root, "add", "-A"); g(root, "commit", "-q", "-m", "paper"); g(root, "push", "-q", "-u", "origin", edition)
        return root

    real_sh, n = oncall.sh, [0]

    def rounds(stage, d, context, wt, base, *a):
        n[0] += 1
        (wt / "scripts" / "x.py").write_text(f"x = {n[0]}\n", encoding="utf-8")
        head, diff, changed = oncall.freeze(wt, base, 1)
        return dict(approved=True, fix={"status": "fixed", "rerun_mode": "resume"}, head=head, diff=diff, changed=changed,
                    kept={}, later=[], error="")

    def faulty(target, how, delivered, blind):
        # how: push の終わり方(時間切れ・非0の終了・起動の失敗)/ delivered: そのまえにリモートへ届いたか /
        # blind: そのあと届いたかを確かめる通信も止まる
        def fake(args, cwd, timeout=600):
            if args[:2] == ["git", "push"] and args[-1] == target:
                if delivered:
                    real_sh(args, cwd, timeout)
                if how == "nonzero":
                    return subprocess.CompletedProcess(args, 1, "", "rejected")
                if how == "oserror":
                    raise OSError("起動できない")
                raise subprocess.TimeoutExpired(args, timeout)
            if blind and args[:2] == ["git", "ls-remote"]:
                raise subprocess.TimeoutExpired(args, timeout)
            return real_sh(args, cwd, timeout)
        return fake

    names = ("ROOT", "sh", "notify", "job_lock", "gather_context", "report_change", "env_fingerprint", "run_rounds", "keep_later",
             "DEADLINE_AT", "STAGE_END_AT", "RECOVER", "REMOTE_CHECK_PAUSE_SEC")
    saved, saved_argv, notes = {k: getattr(oncall, k, None) for k in names}, sys.argv, []
    try:
        oncall.notify = lambda job, msg, ok=True, require=False: notes.append(msg) or True
        oncall.job_lock = lambda *a, **k: os.open(os.devnull, os.O_RDONLY)
        oncall.gather_context = lambda *a: "ctx"
        oncall.report_change = lambda *a, **k: True
        oncall.env_fingerprint = lambda: ""
        oncall.run_rounds, oncall.keep_later = rounds, (lambda *a: 0)
        oncall.DEADLINE_AT = oncall.STAGE_END_AT = None
        oncall.RECOVER, oncall.REMOTE_CHECK_PAUSE_SEC = False, 0
        sys.argv = ["oncall", "--stage", "compose", "--date", date, "--no-rerun"]
        cases = (("timeout", False, False), ("timeout", True, False), ("timeout", False, True), ("timeout", True, True),
                 ("nonzero", False, False), ("nonzero", True, False), ("oserror", False, False))
        for target in ("main", edition):
            for how, delivered, blind in cases:
                kind = f"{how}{'-delivered' if delivered else ''}{'-blind' if blind else ''}"
                where = f"{target} の push が {kind}"
                root = oncall.ROOT = repo(f"{target.replace('/', '-')}-{kind}")
                base = g(root, "rev-parse", "main")
                notes.clear()
                oncall.sh = faulty(target, how, delivered, blind)
                try:
                    code = oncall._main()
                finally:
                    oncall.sh = real_sh
                g(root, "fetch", "-q", "origin")
                state = dict(exit=code, branch=g(root, "branch", "--show-current"), status=g(root, "status", "--porcelain"))
                settled = delivered and not blind      # 届いたと確かめられたときだけ取り込めたことにする
                check(code == (0 if settled else 1) and state["branch"] == edition and not state["status"],
                      f"{where}: 終わり方・終えるブランチが違う: {state} {notes[-1:]}")
                for b in ("main", edition):
                    check(ok(root, "merge-base", "--is-ancestor", b, f"origin/{b}"),
                          f"{where}: ローカルの {b} がリモートに無い commit を持ったまま残った(次の当番が前提の検査で止まる)")
                if settled:
                    check(g(root, "rev-parse", "main") == g(root, "rev-parse", "origin/main") != base
                          and g(root, "rev-parse", edition) == g(root, "rev-parse", f"origin/{edition}")
                          and ok(root, "merge-base", "--is-ancestor", "origin/main", f"origin/{edition}"),
                          f"{where}: 届いていたのに取り込めたことにしない")
                elif blind:
                    check(any("確かめられない" in m for m in notes), f"{where}: 届いたか確かめられないことを知らせない: {notes[-1:]}")
                # 次の当番は前提の検査を通り、新しい修正を main と号へ取り込める
                notes.clear()
                code = oncall._main()
                g(root, "fetch", "-q", "origin")
                check(code == 0 and g(root, "branch", "--show-current") == edition
                      and all(g(root, "rev-parse", b) == g(root, "rev-parse", f"origin/{b}") for b in ("main", edition))
                      and ok(root, "merge-base", "--is-ancestor", "origin/main", f"origin/{edition}"),
                      f"{where}: 次の当番が取り込めない(exit {code}): {notes[-1:]}")
    finally:
        for k, v in saved.items():
            setattr(oncall, k, v)
        sys.argv = saved_argv


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
    # 紙面への影響は読者が受け取る結果で書き、1号の遅れも影響。打ち消す言い方を監査が咎める(編集長 2026-10-07「影響あるじゃん」)
    check("1号でも遅れる・遅れうるなら影響がある" in fp and "影響を打ち消す言い方" in fp and "遅れを隠していないか" in rp,
          "紙面への影響の書き方の契約が当番・監査の依頼文に無い")
    check("指摘されたものは直す" in fp and "反論する" not in fp and "commit 済み" in fp and "範囲は広げない" in fp, "当番への依頼が「範囲の中で直す」になっていない")
    check("**must_fix**" in rp and "**later**" in rp and "later が残っていてもよい" in rp, "監査への依頼が must_fix と later を分けていない")
    # 起きる道筋(どういうときに・どのくらい)を言えない指摘は、指摘として扱わない(編集長 2026-09-18)
    check("`occurs`" in rp and "**書かない**: 起きる道筋を言えないもの" in rp and "schema 上は可能" in rp, "監査への依頼が、起きる道筋の無い指摘を捨てさせていない")
    check("起きる道筋が無い" in fp and "追いかけて直さない" in fp, "当番が、起きる道筋の無い指摘を弾けることになっていない")
    check(all(x in rp for x in oncall.POLICY_EXCLUDED) and all(x in fp for x in oncall.POLICY_EXCLUDED), "判定対象外(編集方針)が依頼文に無い")
    rs = json.loads((Path(oncall.__file__).resolve().parent.parent / "schema" / "oncall-review.schema.json").read_text(encoding="utf-8"))
    check("later" in rs["required"] and rs["properties"]["must_fix"]["items"]["properties"]["severity"]["enum"] == ["blocks_publish", "corrupts_data", "stopgap", "misreport"],
          "監査の schema: later が必須でない、または must_fix に品質・書き方の指摘を入れられる")
    check(all("occurs" in rs["properties"][k]["items"]["required"] for k in ("must_fix", "later")), "監査の schema: 指摘に occurs(起きる道筋)が必須でない")
    # 監査は実際に走らせる: 走らせたコマンドと結果(verification)が必須。テスト・selfcheck・lint を走らせた記録の無い承認は承認にしない
    # (2026-10-14: 読み取り専用の監査が走らせないまま承認し、素材ファイルの書式に無い欄で号が止まった。編集長「ずさんな承認は全部ゴミ」)
    check("verification" in rs["required"] and rs["properties"]["verification"].get("minItems") == 1,
          "監査の schema: 走らせたコマンドと結果(verification)が必須でない")
    check("## 0. 走らせること" in rp and "test_pipeline.py" in rp and "lint.py" in rp and "approve しない" in rp,
          "監査への依頼が、テスト・selfcheck・lint を走らせることを求めていない")
    # 走らせた = 終了コードがある。テストと selfcheck は 0。「走らせられなかった」の結果文では承認にならない(監査指摘)。lint は号の中身で
    # 赤になりうるので、走らせた記録があればよい(修正によるかは監査が判断する)
    cannot = [dict(v, exit_code=None, result="実行できなかった: permission denied") for v in AUDIT_RAN]
    check(oncall.audit_ran_checks({"verification": AUDIT_RAN})
          and oncall.audit_ran_checks({"verification": AUDIT_RAN[:2] + [dict(AUDIT_RAN[2], exit_code=1)]})
          and not oncall.audit_ran_checks({"verification": AUDIT_RAN[:2]})
          and not oncall.audit_ran_checks({"verification": cannot})
          and not oncall.audit_ran_checks({"verification": [{k: v for k, v in r.items() if k != "exit_code"} for r in AUDIT_RAN]})
          and not oncall.audit_ran_checks({"verification": [dict(AUDIT_RAN[0], exit_code=1)] + AUDIT_RAN[1:]})
          and not oncall.audit_ran_checks({"verification": AUDIT_RAN + [dict(AUDIT_RAN[1], exit_code=1)]})
          and not oncall.audit_ran_checks({"verification": [dict(v, exit_code=True) for v in AUDIT_RAN]})
          and not oncall.audit_ran_checks({"verification": cannot + [{"command": "python3 -m py_compile scripts/test_pipeline.py "
                                                                                 "scripts/selfcheck.py scripts/lint.py",
                                                                      "exit_code": 0, "result": "ok"}]})
          and not oncall.audit_ran_checks({}),
          "走らせた記録の検め方(3つ全部に終了コード・テストと selfcheck は最後の実行が 0・走らせられなかったもの・名前を含むだけの"
          "別のコマンドは数えない)")
    # スクリプトを実行したコマンドだけを数える(監査指摘: py_compile の1件で3つを走らせたことになった)
    for cmd, want in (("python3 scripts/test_pipeline.py", True), ("cd /w/x && timeout 900 python3 scripts/test_pipeline.py", True),
                      ("TMPDIR=/tmp/a python3 -u scripts/test_pipeline.py 2>&1 | tail -3", True),
                      ("timeout -k 5 900 /usr/bin/python3.14 /w/x/scripts/test_pipeline.py", True),
                      ("env A=1 python3 -X dev scripts/test_pipeline.py", True),
                      ("python3 -m py_compile scripts/test_pipeline.py", False), ("echo python3 scripts/test_pipeline.py", False),
                      ("cat scripts/test_pipeline.py", False), ("python3 -c 'import scripts/test_pipeline.py'", False),
                      ("python3 audit_test_pipeline.py", False), ("python3 scripts/test_pipeline.py.bak", False),
                      ("python3 --help scripts/test_pipeline.py", False), ("python3 -V scripts/test_pipeline.py", False),
                      ("true || python3 scripts/test_pipeline.py", False), ("true || x && python3 scripts/test_pipeline.py", False),
                      ("false; python3 scripts/test_pipeline.py", True), ("python3 -B -X dev scripts/test_pipeline.py", True)):
        check(oncall.runs_script(cmd, "test_pipeline") is want, f"スクリプトを実行したコマンドの見分けが違う: {cmd!r} → {not want}")
    vi = rs["properties"]["verification"]["items"]
    check(vi["required"] == ["command", "exit_code", "result"] and vi["properties"]["exit_code"]["type"] == ["integer", "null"],
          "監査の schema: 走らせた記録に終了コードが必須でない")
    src_rc = __import__("inspect").getsource(oncall.run_codex)
    check('"workspace-write"' in src_rc and '"read-only"' not in src_rc and '"worktree", "add", "--detach"' in src_rc
          and "remove_worktree(aw, cwd)" in src_rc.split("finally:")[-1], "監査を、使い捨ての作業ツリーで書き込みのできる環境として起動していない")
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
        answers = iter([{"verdict": "reject", "must_fix": [{"id": "p1", "claim": "c"}], "notes": ""}, {"verdict": "approve", "must_fix": [], "verification": AUDIT_RAN, "notes": ""}])
        oncall.run_codex = lambda prompt, schema, cwd, timeout=1800: next(answers)
        r = oncall.run_rounds("compose", "2026-09-18", "ctx", tmp, "B" * 40,
                              {"base": "B" * 40, "head": "B" * 40, "fix": {"status": "no_fix_needed"}, "open": [{"id": "old"}]}, "env", [])
        check(r["approved"] and r["kept"]["open"] == [], f"指摘が無くなったのに終わらない: {r}")
        # (3x) 走らせた記録の無い承認は承認にならず、「走らせていない」を指摘として次の往復へ回す
        answers = iter([{"verdict": "approve", "must_fix": [], "verification": [{"command": "読んだ", "result": "問題なし"}], "notes": ""},
                        {"verdict": "approve", "must_fix": [], "verification": AUDIT_RAN, "notes": ""}])
        oncall.run_codex = lambda prompt, schema, cwd, timeout=1800: next(answers)
        tr = []
        r = oncall.run_rounds("compose", "2026-09-18", "ctx", tmp, "B" * 40,
                              {"base": "B" * 40, "head": "B" * 40, "fix": {"status": "no_fix_needed"}, "open": [{"id": "old"}]}, "env", tr)
        reviews = [t["review"] for t in tr if "review" in t]
        check(r["approved"] and len(reviews) == 2, f"走らせた記録の無い承認を承認にした: {len(reviews)}回 {r.get('approved')}")
        # (3y) 3つの検査は当番のコードが自分で走らせ(selfcheck が赤なら監査に進まない)、lint の結果を監査の依頼に載せる
        #      (監査の自己申告だけを門にすると、走らせない記録で通る道が何度も見つかった。監査指摘)
        calls, prompts = [], []
        oncall.sh = lambda args, cwd, timeout=600: (calls.append([str(a) for a in args])
                                                    or subprocess.CompletedProcess(args, 1 if "scripts/lint.py" in args else 0,
                                                                                   "lint: 2 errors, 0 warnings\n", ""))
        answers = iter([{"verdict": "approve", "must_fix": [], "verification": AUDIT_RAN, "notes": ""}])
        oncall.run_codex = lambda prompt, schema, cwd, timeout=1800: (prompts.append(prompt) or next(answers))
        tr = []
        r = oncall.run_rounds("compose", "2026-09-18", "ctx", tmp, "B" * 40,
                              {"base": "B" * 40, "head": "B" * 40, "fix": {"status": "no_fix_needed"}, "open": [{"id": "old"}]}, "env", tr)
        order = [next((k for k in ("selfcheck.py", "lint.py") if any(a.endswith(k) for a in c)), None) for c in calls]
        check(r["approved"] and [o for o in order if o] == ["selfcheck.py", "lint.py"] and prompts
              and "当番のコードが走らせた検査" in prompts[0] and "lint.py --base origin/main: 終了 1: lint: 2 errors" in prompts[0]
              and any(t.get("code_checks") == {"selfcheck": 0, "lint": 1} for t in tr),
              f"当番のコードが3つの検査を走らせて結果を監査に渡していない: {order} {prompts[:1] and prompts[0][-300:]}")
        oncall.sh = lambda args, cwd, timeout=600: subprocess.CompletedProcess(args, 0, "", "")
        # (3a) 発行後でよい指摘(later)は approve を妨げず、往復と試行をまたいで重複なしで集まる
        #      起きる道筋(occurs)の無い later は集めない(「schema 上は可能」の類を後の仕事にしない)
        la = {"id": "l1", "claim": "堅牢化A", "evidence": "e", "occurs": "記事が60本を超えた号(月に1〜2回)"}
        answers = iter([{"verdict": "reject", "must_fix": [{"id": "p1", "claim": "c"}], "later": [la], "notes": ""},
                        {"verdict": "approve", "must_fix": [], "verification": AUDIT_RAN, "notes": "",
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
        # (3c) 保管していた指摘を直す昼の仕事(backlog)では、later も must_fix として直すまで approve にならず、保管にも積まない
        answers = iter([{"verdict": "approve", "must_fix": [], "verification": AUDIT_RAN, "notes": "", "later": [la]},
                        {"verdict": "approve", "must_fix": [], "verification": AUDIT_RAN, "notes": "", "later": []}])
        oncall.run_codex = lambda prompt, schema, cwd, timeout=1800: next(answers)
        tr = []
        r = oncall.run_rounds(oncall.BACKLOG_STAGE, "2026-10-07", "ctx", tmp, "B" * 40, None, "env", tr)
        check(r["approved"] and r["later"] == [] and sum(1 for t in tr if "integrate" in t) == 1,
              f"backlog で later を後回しにした: {r['later']} {[list(t) for t in tr]}")
        row1 = {"key": "k1", "claim": "c", "occurs": "o", "evidence": "e", "at": "2026-10-01T04:25", "stage": "compose"}
        rb = oncall.backlog_reason([row1])
        fpb = oncall.fix_prompt(oncall.BACKLOG_STAGE, "2026-10-07", rb, None)
        saved_rows = list(oncall.BACKLOG_ROWS)
        oncall.BACKLOG_ROWS[:] = [row1]
        rpb = oncall.review_prompt(oncall.BACKLOG_STAGE, "2026-10-07", {"status": "fixed"}, "diff", None)
        oncall.BACKLOG_ROWS[:] = saved_rows
        check("[k1]" in rb and "発行に必要な最小限に絞らない" in fpb and "欠陥の型" in fpb and "全件" in fpb
              and "[k1]" in rpb and "全件" in rpb and "発行するのに必要な最小限" not in fpb + rpb,
              "保管の指摘を直す依頼文(当番・監査の範囲が backlog 用になっていない)")
        check("この号を発行するのに必要な最小限" in oncall.fix_prompt("compose", "2026-10-07", "ctx", None), "通常の当番の範囲が変わった")
        # 消し込むのは、最終報告が1件ずつ扱った指摘だけ(扱っていない・2回書いた・知らない扱いは残す)
        fixb = {"backlog_items": [{"key": "k1", "result": "fixed", "why": "w"}, {"key": "k2", "result": "fixed", "why": "w"},
                                  {"key": "k2", "result": "invalid", "why": "w"}, {"key": "k4", "result": "later", "why": "w"}]}
        check(oncall.settled_backlog_keys(fixb, ["k1", "k2", "k3", "k4"]) == ["k1"], "扱っていない指摘まで消し込む")
        # 時間の枠: 次の定時工程の15分前まで。発行の時間帯・短い枠では始めない
        import datetime as _dtb
        J = lambda h, m: _dtb.datetime(2026, 10, 7, h, m, tzinfo=pipelib.JST)
        check(oncall.backlog_deadline(J(9, 0)) == J(12, 15).timestamp(), "09:00 の枠が 12:15 まででない")
        check(oncall.backlog_deadline(J(4, 0)) is None and oncall.backlog_deadline(J(12, 0)) is None, "発行の時間帯・短い枠で始める")
        check(oncall.backlog_deadline(J(0, 40)) is None and oncall.backlog_deadline(J(0, 0)) == J(1, 15).timestamp(),
              "深夜の枠が 01:30 の道具の更新を待たせる")
        # 収集の当番は、直したら取り直してその号の素材に入れる。終える時刻は呼び出し時に固定(23:33 の失敗 → 01:15。
        # 02:00 の収集の失敗 → 組版が待つ分を含めて 03:35。組版はその印を見て 03:40 まで待つ)
        check(pipelib.collect_oncall_end(J(23, 40)) == (J(1, 15) + _dtb.timedelta(days=1)).timestamp()
              and pipelib.collect_oncall_end(J(2, 20)) == J(3, 35).timestamp(), "収集の当番の終える時刻")
        saved_mark = pipelib.COLLECT_ONCALL_MARK
        try:
            pipelib.COLLECT_ONCALL_MARK = tmp / "mark.json"
            check(pipelib.compose_lock_wait_min(J(3, 0).timestamp()) == 10, "印が無いのに組版が長く待つ")
            pipelib.COLLECT_ONCALL_MARK.write_text(json.dumps({"end_at": J(3, 35).timestamp()}), encoding="utf-8")
            check(pipelib.compose_lock_wait_min(J(3, 0).timestamp()) == 40 and pipelib.compose_lock_wait_min(J(3, 40).timestamp()) == 10,
                  "組版が収集の当番を待つ分")
        finally:
            pipelib.COLLECT_ONCALL_MARK = saved_mark
        import collect
        calls = []
        saved_diag, saved_now = collect.diagnose_anomalies, collect.now_jst
        try:
            collect.diagnose_anomalies = lambda *a, **k: calls.append((a, k)) and False
            collect.now_jst = lambda: J(2, 20)
            collect.hand_to_oncall()
        finally:
            collect.diagnose_anomalies, collect.now_jst = saved_diag, saved_now
        check(len(calls) == 1 and calls[0][0][0] == "collect" and calls[0][1].get("rerun") is True
              and calls[0][1].get("extra_args") == ["--end-at", str(int(J(3, 35).timestamp()))],
              f"収集の当番が、直したあと取り直さない・終える時刻を固定して渡さない: {calls}")
        # 依頼文などの生成層を直しても、収集の当番は号を作り直さず取り直すだけ
        check(oncall.rerun_policy("collect", ["prompts/watch-facts.md"], "rebuild")[0] is False, "収集の当番が号の作り直しに入る")
        # 終わりに、残った指摘を名指しで知らせる(全経路。atexit)/ 保管を読めないのは「残件なし」にしない
        notes_l = []
        saved_l = (oncall.notify, oncall.backlog_open)
        try:
            oncall.notify = lambda job, msg, ok=True, require=False: notes_l.append((msg, ok)) or True
            oncall.backlog_open = lambda: [{"key": "k2", "claim": "残った"}]
            oncall.report_left_backlog(["k1", "k2"], "2026-10-07")
        finally:
            oncall.notify, oncall.backlog_open = saved_l
        check(len(notes_l) == 1 and not notes_l[0][1] and "k2" in notes_l[0][0] and "k1" not in notes_l[0][0], f"残った指摘を名指ししない: {notes_l}")
        # unit の停止(SIGTERM)でも残件の報告が走る(atexit は既定の SIGTERM では走らない)
        mark = tmp / "left-reported"
        code = ("import sys, time; sys.path.insert(0, %r); import oncall\n"
                "oncall.report_left_backlog = lambda keys, date: open(%r, 'w').write(','.join(keys))\n"
                "oncall.arm_backlog_exit(['k1'], '2026-10-07'); print('armed', flush=True); time.sleep(30)\n") % (
            str(Path(oncall.__file__).resolve().parent), str(mark))
        pr = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        pr.stdout.readline()
        pr.terminate()
        pr.wait(timeout=20)
        check(mark.exists() and mark.read_text() == "k1", "SIGTERM で終わったときに残った指摘を知らせない")
        # 最終巡の空の扱いは初稿の扱いを置き換える(扱っていない指摘を消し込まない)
        fx = {"backlog_items": [{"key": "k1", "result": "fixed", "why": "w"}]}
        oncall.apply_integrate(fx, {"backlog_items": []})
        check(oncall.settled_backlog_keys(fx, ["k1"]) == [], "最終巡で扱っていない指摘を、初稿の扱いで消し込む")
        # 保管を読めない(権限)ときは「残件なし」にしない
        saved_bk2 = oncall.BACKLOG
        locked = tmp / "locked"
        locked.mkdir(exist_ok=True)
        (locked / "b.jsonl").write_text("", encoding="utf-8")
        try:
            oncall.BACKLOG = locked / "b.jsonl"
            os.chmod(locked, 0)
            try:
                oncall.backlog_rows()
                check(os.geteuid() == 0, "権限エラーで保管を読めないのに残件なしにした")
            except PermissionError:
                pass
        finally:
            os.chmod(locked, 0o755)
            oncall.BACKLOG = saved_bk2
        import watch
        saved_bk = oncall.BACKLOG
        try:
            oncall.BACKLOG = tmp / "bkdir"
            oncall.BACKLOG.mkdir(exist_ok=True)       # 読めない(ディレクトリ)
            check(any("読めない" in p for p in watch.hand_backlog("2026-10-07")), "保管を読めないのに残件なしにした")
        finally:
            oncall.BACKLOG = saved_bk
        # 監視は、保管の指摘を当番に渡す(覚え書きの通知で終わらせない)。台帳は付けず、指摘の key を渡す。渡せなければ異常
        import watch
        calls_w = []
        saved_w = (watch.escalate, oncall.backlog_open)
        try:
            oncall.backlog_open = lambda: [{"key": "k1", "claim": "c"}, {"key": "k2", "claim": "d"}]
            watch.escalate = lambda *a, **kw: calls_w.append((a, kw)) or True
            ok_w = watch.hand_backlog("2026-10-07")
            watch.escalate = lambda *a, **kw: False
            ng_w = watch.hand_backlog("2026-10-07")
        finally:
            watch.escalate, oncall.backlog_open = saved_w
        check(ok_w == [] and calls_w and calls_w[0][0][0] == "backlog" and calls_w[0][1]["ledger"] is False
              and calls_w[0][1]["extra_args"] == ["--backlog-keys", "k1", "k2"] and calls_w[0][1]["rerun"] is False,
              f"監視が保管の指摘を当番に渡さない: {calls_w}")
        check(len(ng_w) == 1 and "k1" in ng_w[0] and "k2" in ng_w[0], f"当番に渡せなかったのに異常にしない: {ng_w}")
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
        check(len(keys) == 2 and oncall.BACKLOG_BROKEN == [3], f"壊れた行で一覧が止まる・壊れた行を記録しない: {keys} {oncall.BACKLOG_BROKEN}")
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


def test_anomaly_ledger_and_whywhy(tmp: Path):
    """人に「異常」として通知したものは台帳に積み、当番への依頼文に「なぜなぜ」として必ず付く
    (編集長 2026-09-26「すべてのエラーがなぜ起きたかなぜなぜしろ」「申告前に診断しろ」)。
    試験実行(quiet)では当番を呼ばない。同じ process で二度は呼ばない。"""
    saved = list(pipelib.ANOMALIES)
    saved_flag, saved_quiet = pipelib._ESCALATED, pipelib._QUIET
    try:
        pipelib.ANOMALIES.clear()
        pipelib._ESCALATED = False
        pipelib.set_quiet(False)
        check(pipelib.escalate_reason("理由") == "理由", "台帳が空なら理由だけ")
        pipelib.notify("compose", "記事Xを落とした: 校閲ブロック", ok=False)
        pipelib.notify("compose", "順調", ok=True)
        pipelib.notify("oncall", "当番の通知は台帳に積まない", ok=False)
        pipelib.anomaly("compose", "  ")
        pipelib.anomaly("watch", "未確認の出典 3件")
        check(pipelib.ANOMALIES == ["[compose] 記事Xを落とした: 校閲ブロック", "[watch] 未確認の出典 3件"], f"台帳: {pipelib.ANOMALIES}")
        r = pipelib.escalate_reason("止まった")
        check(r.startswith("止まった") and "なぜなぜ" in r and "- [compose] 記事Xを落とした" in r and "- [watch] 未確認の出典 3件" in r,
              "依頼文に台帳となぜなぜの指示が無い")
        # 試験実行では当番を呼ばない(呼ぶと本物の当番が起動する)
        pipelib.set_quiet(True)
        check(pipelib.escalate("compose", "2026-09-26", "x") is False and pipelib._ESCALATED is False, "quiet で当番を呼んだ")
        check(pipelib.diagnose_anomalies("compose", "2026-09-26") is False, "quiet で diagnose が当番を呼んだ")
        # 当番を起動できなかった(systemd-run 失敗・Popen 失敗)ときは _ESCALATED を立てない(工程末尾の diagnose が二度目を試せる。監査指摘 r87)
        pipelib.set_quiet(False)
        (tmp / "scripts").mkdir(parents=True, exist_ok=True)
        (tmp / "metrics").mkdir(exist_ok=True)
        (tmp / "scripts" / "oncall.py").write_text("", encoding="utf-8")
        saved_popen, saved_inv, saved_proot, saved_oncall = pipelib.subprocess.Popen, os.environ.get("INVOCATION_ID"), pipelib.ROOT, pipelib.ENV.get("ONCALL")
        try:
            os.environ.pop("INVOCATION_ID", None)      # systemd の外の経路(Popen)
            pipelib.ROOT = tmp                          # 本物の metrics/ に当番のログを作らない
            pipelib.ENV["ONCALL"] = "on"
            def boom(*a, **k):
                raise OSError("起動できない")
            pipelib.subprocess.Popen = boom
            check(pipelib.escalate("compose", "2026-09-26", "x") is False and pipelib._ESCALATED is False, "起動に失敗したのに _ESCALATED が立った")
        finally:
            pipelib.subprocess.Popen, pipelib.ROOT = saved_popen, saved_proot
            if saved_inv is not None:
                os.environ["INVOCATION_ID"] = saved_inv
            if saved_oncall is None:
                pipelib.ENV.pop("ONCALL", None)
            else:
                pipelib.ENV["ONCALL"] = saved_oncall
        # 既に当番を呼んだ process では、終わりの diagnose は重ねて呼ばない(当番は1日2回まで)
        pipelib._ESCALATED = True
        pipelib.set_quiet(False)
        check(pipelib.diagnose_anomalies("compose", "2026-09-26") is False, "二度目の当番を呼んだ")
        pipelib.ANOMALIES.clear()
        pipelib._ESCALATED = False
        check(pipelib.diagnose_anomalies("compose", "2026-09-26") is False, "台帳が空なのに当番を呼んだ")
    finally:
        pipelib.ANOMALIES.clear()
        pipelib.ANOMALIES.extend(saved)
        pipelib._ESCALATED = saved_flag
        pipelib.set_quiet(saved_quiet)
    # compose: 書き直しをはさんでも同じ記事に同じ規則の指摘が残った、を記録し、通知と台帳に載せる
    saved_stuck = dict(compose.STUCK)
    try:
        compose.STUCK.clear()
        check(compose.stuck_summary("2026-09-26") == "", "記録が無いのに要約が出る")
        compose.note_stuck("2026-09-26", "校閲", "docs/_posts/2026-09-26-a.md", "R16", "sources[0] のラベルが切れている", 3)
        s = compose.stuck_summary("2026-09-26")
        check("2026-09-26-a.md R16(3巡目まで残った)" in s and "契約" in s, f"要約: {s}")
    finally:
        compose.STUCK.clear()
        compose.STUCK.update(saved_stuck)
    # oncall: --no-rerun は「再実行しても通らない(none)」より先に見る(号が確定した工程の当番は再実行せず 0 で終わる)
    import oncall
    src = (pipelib.ROOT / "scripts" / "oncall.py").read_text(encoding="utf-8")
    check(src.index("if a.no_rerun:") < src.index('if rerun_mode == "none":\n            notify'), "no_rerun の判定が none の後にある")
    check(set(oncall.STAGES) >= {"compose", "release", "classify", "collect", "watch", "update"}, f"oncall の工程: {oncall.STAGES}")
    # compose の例外・SystemExit・終了時の当番は、対象の号(STARTED_DATE。--date で過去号を組み直したとき)を渡す(監査指摘 r87)
    csrc = (pipelib.ROOT / "scripts" / "compose.py").read_text(encoding="utf-8")
    check('escalate("compose", edition_date()' not in csrc and csrc.count("STARTED_DATE or edition_date()") >= 3,
          "compose の終了経路の当番が対象の号ではなく edition_date() を渡している")


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


def test_reminder_event_date(tmp: Path):
    """続報予約を素材として読むとき、event_date は予約が指す日(締切前は予約の日の3日後、ほかは予約の日)にする。元の話題の開始日を
    渡すと書き手が写し、1か月前の日付の記事として lint の鮮度検査で赤になった(実測 2026-10-12: 9/11 開始の香水予約の締切前)。
    元の話題の deadline は別の期限(入金期限など)のことがあるので使わない(監査指摘: 10/15 の予約で deadline 11/4)。
    出来事の日(occurs_on)の無い古い予約で、その日が予約に書いていないもの(x・noct)は日付を渡さない(執筆が決める。監査指摘)"""
    sdir = tmp / "stock" / "scheduled"
    sdir.mkdir(parents=True)
    rows = [{"id": "sched-2026-10-12-anideal-締切前", "dedup_key": "anideal", "kind": "締切前",
             "event_date": "2026-09-11", "deadline": "2026-10-15", "facts": ["予約期間は2026年9月11日〜10月15日"]},
            {"id": "sched-2026-10-12-x-締切前", "dedup_key": "x", "kind": "締切前", "event_date": "2026-09-01", "facts": []},
            {"id": "sched-2026-10-12-letora-発売", "dedup_key": "letora", "kind": "発売",
             "event_date": "2026-11-11", "facts": ["10月12日発売"]},
            {"id": "sched-2026-10-12-tours-終了", "dedup_key": "tours", "kind": "終了",
             "event_date": "2026-08-28", "deadline": "2026-10-12", "facts": []},
            {"id": "sched-2026-10-12-apple-締切前", "dedup_key": "apple", "kind": "締切前",
             "event_date": None, "deadline": "2026-11-04", "facts": ["受付締切: 10月15日(木) 23:59", "入金期限: 11月4日"]},
            {"id": "sched-2026-10-12-noct-締切前", "dedup_key": "noct", "kind": "締切前",
             "event_date": "2027-01-01", "deadline": "2026-09-27", "facts": []}]
    (sdir / "2026-10-12.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    saved = compose.ROOT
    try:
        compose.ROOT = tmp
        got = {r["dedup_key"]: r.get("event_date") for r in compose.load_scheduled("2026-10-12")}
        mats = compose.load_window_candidates("2026-10-12")
    finally:
        compose.ROOT = saved
    check(got == {"anideal": "2026-10-15", "x": None, "letora": "2026-10-12", "tours": "2026-10-12",
                  "apple": "2026-10-15", "noct": None},
          f"続報予約の event_date が予約の日になっていない: {got}")
    check(mats["sched-2026-10-12-anideal-締切前"]["event_date"] == "2026-10-15", "執筆の素材に元の話題の開始日が渡る")
    check(json.loads((sdir / "2026-10-12.json").read_text(encoding="utf-8"))[0]["event_date"] == "2026-09-11",
          "予約のファイルを書き換えた")
    # 校閲にも執筆と同じ補正済みの予約の素材を渡す(監査指摘: 校閲が予約のファイルを直接読み、元の話題の日付で執筆をブロックした)
    pdir = tmp / "docs" / "_posts"
    pdir.mkdir(parents=True)
    (pdir / "2026-10-12-letora.md").write_text(
        "---\ntitle: x\ncandidate_ids:\n- sched-2026-10-12-letora-発売\n- c-1\n---\n本文\n", encoding="utf-8")
    (pdir / "2026-10-12-plain.md").write_text("---\ntitle: y\ncandidate_ids:\n- c-1\n---\n本文\n", encoding="utf-8")
    try:
        compose.ROOT = tmp
        h = compose.review_hint("2026-10-12", "2026-10-12-letora.md", {})
        h_plain = compose.review_hint("2026-10-12", "2026-10-12-plain.md", {})
    finally:
        compose.ROOT = saved
    i = h.find("## 続報予約の素材")
    shown = json.loads(h[h.index("\n", i) + 1:]) if i >= 0 else []
    check([(r["id"], r["event_date"]) for r in shown] == [("sched-2026-10-12-letora-発売", "2026-10-12")],
          f"校閲の依頼文に補正済みの予約の素材が載らない: {h!r}")
    check(h_plain == "", f"予約の素材を使わない記事に予約の素材が載った: {h_plain!r}")


# 実データ stock/scheduled/2026-09-30.json の2件の写し(リポジトリにあればそちらを使う)。ミッションパスは 10/1 4:59 に終わるので、
# 紙面が届く 06:00 には終わっている → 前日(9/30)の号に予約した。デレステの周年施策は 9/30 23:59 に終わる → 当日の予約
REAL_SCHED_0930 = [
    {"id": "sched-2026-09-30-gaku-mission-pass-2026-09-終了", "dedup_key": "gaku-mission-pass-2026-09", "brand": "gaku",
     "subject": "学マス9月ミッションパス", "kind": "終了",
     "note": "実際の終了は10/1 4:59。読者に紙面が届く06:00には終わっているため前日に予約", "reserved_on": "2026-09-02",
     "title": "学マス、9月ミッションパス開催", "url": "https://x.com/gkmas_official/status/2094621274098307424", "source_type": "公式",
     "facts": ["期間は9月1日から10月1日4時59分までの予定", "無償ジュエルを最大5000個獲得できる", "プレミアム報酬は「おしのび用メガネ」"],
     "verify": "confirmed", "event_date": "2026-09-01", "src_candidate_id": "202609020238-grok-29"},
    {"id": "sched-2026-09-30-deresute-11th-anniversary-campaign-2026-終了", "dedup_key": "deresute-11th-anniversary-campaign-2026",
     "brand": "cg", "subject": "デレステ11周年記念キャンペーン", "kind": "終了", "note": "記念施策終了", "reserved_on": "2026-09-04",
     "title": "デレステ11周年記念キャンペーン", "url": "https://apis.game.starlight-stage.jp/information/detail/7864/1/10/1",
     "source_type": "当事者",
     "facts": ["2026年9月3日に11周年を迎え記念キャンペーンを実施",
               "プラチナスペシャルガシャセット(リミテッドver/フェスver)の販売期間は9月30日23:59まで",
               "育成強化キャンペーン等の周年施策も9月30日23:59まで"],
     "via": "claude", "verify": "confirmed", "src_candidate_id": "202609031835-explore-5", "deadline": "2026-09-30"}]
# 実データ stock/scheduled/2026-08-26.json の写し: 8/25 19時の配信の結果を翌日の号で伝える予約(予約に 8/26 は書いていない)
REAL_SCHED_0826_RESULT = {
    "id": "sched-2026-08-26-shiny-new-idol-teaser-stream-2026-08-25-結果", "dedup_key": "shiny-new-idol-teaser-stream-2026-08-25",
    "brand": "shiny", "subject": "シャニマス生配信で発表された近日登場アイドル", "kind": "結果", "note": "前日8/25 19時配信の発表内容を回収",
    "reserved_on": "2026-08-25",
    "facts": ["シャイニーカラーズ公式が8月24日付で近日登場予定のアイドルをチラ見せし、8月25日(火)19時からの生配信で発表予定と告知",
              "番組タイトルは「シャニマス生配信 ～8月も意外と暑い～ズSP～」、出演は峯田茉優さん(八宮めぐる役)・前川涼子さん(大崎甜花役)・プロデューサー高山祐介さん",
              "配信時点では正体は非公開、シャニソン側の最新情報・機能アップデートも予告されている"],
    "title": "近日登場アイドルをチラ見せ・8月25日生配信で発表予定", "url": "https://x.com/imassc_official/status/2091782560091394410",
    "source_type": "公式", "verify": "confirmed", "src_candidate_id": "202608242201-grok-22", "event_date": "2026-08-25"}


def test_reservation_occurs_on(tmp: Path):
    """続報予約は、予約の日(記事を出す号)とは別に、予約が指す出来事の日(occurs_on)を持つ。06:00 より前に終わる催しは前日の号に
    予約するので2つは一致しない(監査指摘 2026-10-14: 実データ sched-2026-09-30-gaku-mission-pass-2026-09-終了 は「10月1日4時59分まで」
    なのに、執筆・校閲に event_date 9/30 を渡し、記事の frontmatter に誤った日付が書かれても lint は通った)。
    組版が書いた予約は書式どおりで出来事の日を持ち、執筆・校閲にはそれが渡る。欄の無い古い予約も読め、決められなければ日付を渡さない。"""
    real_p = Path(__file__).resolve().parent.parent / "stock" / "scheduled" / "2026-09-30.json"
    real = {r["id"]: r for r in REAL_SCHED_0930}
    if real_p.exists():
        real.update({r["id"]: r for r in json.loads(real_p.read_text(encoding="utf-8")) if r.get("id") in real})
    mp, ds = (real[r["id"]] for r in REAL_SCHED_0930)
    check(renderlib.date_mentioned("2026-10-01", "10月1日4時59分まで", {2026}) and renderlib.date_mentioned("2026-10-01", "１０月１日まで", {2026})
          and not renderlib.date_mentioned("2026-09-01", "9/13開催", {2026}), "時刻の続く日付・全角の日付を素材の日付と見ない(9/1 が 9/13 に当たる)")
    # 1) 組版: 9/2 の号が、ミッションパスの記事の素材から予約する(モデルの答えは出来事の日 10/1 を付けて 9/30 の号に予約)
    st = tmp / "stock"; (st / "scheduled").mkdir(parents=True)
    (tmp / "metrics").mkdir(); (tmp / "docs" / "_editions").mkdir(parents=True); (tmp / "docs" / "_posts").mkdir(parents=True)
    assemble.dump_yaml(st / "stories.yml", [])
    assemble.dump_yaml(st / "pending.yml", [])
    cid = mp["src_candidate_id"]
    mats = {cid: cand_row(cid, mp["url"], title=mp["title"], brand="gaku", dedup_key=mp["dedup_key"], facts=list(mp["facts"]),
                          event_date=mp["event_date"])}
    posts = [{"slug": "gaku-mission-pass", "brand": "gaku", "candidate_ids": [cid], "title": "t", "lede": "l", "rank": "small"}]
    resv = lambda kind, occ, date="2026-09-30": {"slug": "gaku-mission-pass", "candidate_id": cid, "date": date, "occurs_on": occ,
                                                 "kind": kind, "subject": mp["subject"], "note": mp["note"]}
    out = {"digest": [], "stories": [], "pending_add": [], "pending_remove": [],
           "reservations": [resv("終了", "2026-10-01"),                  # 06:00 前に終わる → 前日の号・出来事は翌日
                            resv("開始", "2026-10-02", "2026-10-01"),    # 開始は予約の日とずれない → 出来事の日を付けずに予約する
                            resv("締切", "2026-10-02"),                  # 締切のずれは翌日まで(しかも素材に無い日)→ 同上
                            # 締切前(9/27)の答えの出来事の日(9/30)が素材に無い → 出来事の日だけ外す。予約は残す(締切 10/1 は
                            # 素材にあり、締切前の4日前にあたる。監査指摘: 出来事の日を外したあとの照合が3日後までしか見ず、予約ごと捨てた)
                            resv("締切前", "2026-09-30", "2026-09-27")]}
    ledger = {k: out[k] for k in assemble.LEDGER_KEYS}
    check(assemble.ledger_out_ok(ledger)
          and not assemble.ledger_out_ok(dict(ledger, reservations=[{k: v for k, v in resv("終了", "2026-10-01").items() if k != "occurs_on"}])),
          "組版の答えの書式が、予約の出来事の日を求めていない")
    inp = {"facts": [], "existing_story": {}, "tomorrow_reservations": [], "pending": [], "subjects": []}
    saved = (assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS, compose.ROOT, pipelib.ROOT,
             assemble.classify_source)
    try:
        assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS = \
            tmp, st / "stories.yml", st / "scheduled", st / "pending.yml", tmp / "docs" / "_editions"
        compose.ROOT = pipelib.ROOT = tmp
        assemble.classify_source = lambda u: "公式"
        clean, notes = assemble.validate("2026-09-02", out, posts, mats, inp)
        got = {r["kind"]: r.get("occurs_on") for r in clean["reservations"]}
        check(got == {"終了": "2026-10-01", "開始": None, "締切": None, "締切前": None} and sum("出来事の日" in n for n in notes) == 3
              and not any("捨てた" in n for n in notes), f"予約の出来事の日の検め(種別とのずれ・素材の日付)が違う: {got} {notes}")
        ok4, _ = assemble.validate("2026-09-02", dict(out, reservations=[resv("締切前", "2026-10-01", "2026-09-27")]), posts, mats, inp)
        check([r.get("occurs_on") for r in ok4["reservations"]] == ["2026-10-01"], f"06:00 前の締切の4日前の締切前の出来事の日を採らない: {ok4}")
        assemble.apply("2026-09-02", 1, clean, posts, mats, False, stories=[])
        written = {}
        for sp in sorted((st / "scheduled").glob("*.json")):
            text = sp.read_text(encoding="utf-8")
            check(pipelib.schema_errors(sp, text) == [], f"組版が書いた予約が書式(scheduled.schema)に合わない: {sp.name} {pipelib.schema_errors(sp, text)}")
            written.update({r["kind"]: r for r in json.loads(text)})
        end = written.get("終了") or {}
        check(sorted(written) == ["終了", "締切", "締切前", "開始"] and end.get("occurs_on") == "2026-10-01"
              and end.get("facts") == mp["facts"] and end.get("reserved_on") == "2026-09-02"
              and not any("occurs_on" in written.get(k, {}) for k in ("開始", "締切", "締切前")),
              f"予約が落ちた・予約に出来事の日が残らない・合わない日を残した: {written}")
        # 2) 9/30 の号: 執筆と校閲の素材の event_date は出来事の日(10/1)。予約の日(9/30)も元の話題の開始日(9/1)も渡さない
        loaded = {r["kind"]: r for r in compose.load_scheduled("2026-09-30")}
        check(loaded.get("終了", {}).get("event_date") == "2026-10-01",
              f"執筆に渡す event_date が出来事の日でない: {loaded.get('終了', {}).get('event_date')}")
        window = compose.load_window_candidates("2026-09-30")
        sid = end.get("id", "(予約が無い)")
        mats_in, _ = renderlib.materials_with_ids([window[sid]] if sid in window else [])
        art = {"slug": "gaku-mission-pass-end", "brand": "gaku", "rank": "small", "angle": "終了", "candidate_ids": [sid]}
        prompt = compose.article_prompt("2026-09-30", art, mats_in, [], loaded.get("終了"))
        check('"event_date": "2026-10-01"' in prompt and '"event_date": "2026-09-30"' not in prompt and '"event_date": "2026-09-01"' not in prompt,
              "執筆の素材の event_date が出来事の日でない")
        (tmp / "docs" / "_posts" / "2026-09-30-gaku-mission-pass-end.md").write_text(
            "---\n" + article_fm("gaku-mission-pass-end", "2026-09-30", candidate_ids=[sid]) + "---\n本文\n", encoding="utf-8")
        h = compose.review_hint("2026-09-30", "2026-09-30-gaku-mission-pass-end.md", {})
        i = h.find("## 続報予約の素材")
        shown = json.loads(h[h.index("\n", i) + 1:]) if i >= 0 else []
        check([(r["id"], r.get("event_date")) for r in shown] == [(sid, "2026-10-01")], f"校閲の素材の event_date が出来事の日でない: {shown}")
        # 3) 欄の無い古い予約(実データのまま): 読める。ミッションパスは 9/30 と 10/1 のどちらかを予約の字面から決められないので
        #    event_date を渡さず執筆が決める(予約の日 9/30・元の話題の開始日 9/1 は渡さない)。デレステ(9/30 23:59)は 9/30
        old_dir = tmp / "old"
        (old_dir / "stock" / "scheduled").mkdir(parents=True)
        (old_dir / "stock" / "scheduled" / "2026-09-30.json").write_text(json.dumps([mp, ds], ensure_ascii=False), encoding="utf-8")
        compose.ROOT = old_dir
        old = {r["id"]: r for r in compose.load_scheduled("2026-09-30")}
        check("event_date" not in old[mp["id"]] and old[ds["id"]].get("event_date") == "2026-09-30",
              f"古い予約の出来事の日: {[(k, v.get('event_date')) for k, v in old.items()]}")
        check(json.loads((old_dir / "stock" / "scheduled" / "2026-09-30.json").read_text(encoding="utf-8")) == [mp, ds], "古い予約のファイルを書き換えた")
        # 前日 19時の配信の結果を翌日の号で伝える古い予約: 予約の日(8/26)は予約に書いていない → 渡さない(監査指摘)
        (old_dir / "stock" / "scheduled" / "2026-08-26.json").write_text(json.dumps([REAL_SCHED_0826_RESULT], ensure_ascii=False), encoding="utf-8")
        res26 = compose.load_scheduled("2026-08-26")
        check(len(res26) == 1 and "event_date" not in res26[0], f"翌日に結果を伝える古い予約に予約の日・元の話題の日を渡した: {res26}")
    finally:
        (assemble.ROOT, assemble.STORIES, assemble.SCHEDULED, assemble.PENDING, assemble.EDITIONS, compose.ROOT, pipelib.ROOT,
         assemble.classify_source) = saved
    # 執筆の依頼文: 続報予約の記事の event_date は予約の出来事の日(素材に無いときも執筆が決められるように)
    check("続報予約の記事は、その予約の出来事" in (pipelib.PROMPTS / "write-article.md").read_text(encoding="utf-8")
          and "occurs_on" in (pipelib.PROMPTS / "assemble-ledger.md").read_text(encoding="utf-8"),
          "依頼文に、続報予約の出来事の日の決め方が無い")


FAKE_CODEX = r'''#!/usr/bin/env python3
# テスト用の偽の codex(本物の子プロセスとして起動される)。依頼の種類と記事(write-<slug> / revise-<slug>)ごとに、呼ばれた回数番目の
# 手順を FAKE_CODEX_SPEC から引く: touch(作業ツリーのファイルを直接書く・消す)→ fetch(本物の fetch_page.py で読む。FAKE_FETCH_PAGE)
# → answer(最終メッセージ)→ sleep → exit
import json, os, pathlib, re, subprocess, sys, time
spec_p = pathlib.Path(os.environ["FAKE_CODEX_SPEC"])
m = re.search(r"/metrics/work/[^/]+/(write|revise)-([a-z]+(?:-[a-z]+)*)(?:-\d+)?\.md", sys.argv[-1])
key = m.group(1) + "-" + m.group(2)
cnt = spec_p.with_name("count-" + key)
n = int(cnt.read_text()) if cnt.exists() else 0
cnt.write_text(str(n + 1))
steps = json.loads(spec_p.read_text(encoding="utf-8"))[key]
step = steps[min(n, len(steps) - 1)]
for rel, text in (step.get("touch") or {}).items():
    if text is None:
        pathlib.Path(rel).unlink()
    else:
        pathlib.Path(rel).write_text(text, encoding="utf-8")
for u in step.get("fetch") or []:
    subprocess.run([sys.executable, os.environ["FAKE_FETCH_PAGE"], u], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
if step.get("answer") is not None:
    pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text(json.dumps(step["answer"], ensure_ascii=False), encoding="utf-8")
if step.get("background"):      # 道具の実行を残して終わる(あとから書く。detach なら別のセッション・プロセスグループに抜け、
    import subprocess             # nodump なら /proc/<pid>/environ を読めなくする)
    code = ("import ctypes; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)\n" if step.get("nodump") else "") + step["background"]
    subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=bool(step.get("detach")))
if step.get("kill_guard"):      # 自分を包む child_guard を SIGKILL する(OOM などで監督だけが落ちた形)
    import signal
    os.kill(os.getppid(), signal.SIGKILL)
time.sleep(step.get("sleep", 0))
sys.exit(step.get("exit", 0))
'''


def test_writer_children(tmp: Path):
    """執筆・書き直しを本物の子プロセス(偽の codex)で走らせて、採った稿と作業ツリーを検める。
    - 見出し・リードの Markdown 記法は検算で差し戻す(2026-10-14 監査指摘: `**販売開始**` を採ってから lint が赤にした)
    - 稿を採ったら、その稿の指紋と F 番号 → 事実の対応を記録し、ファイルに残す(監査指摘: 記録の呼び出しを消してもテストが通った)
    - 書き直しの子が記事を直接書き換えても採らない。異常終了・時間切れ・適用の拒否のどれでも、記事は控えのまま
      (監査指摘: 直接書き換えて異常終了した子の変更が残った)"""
    import contextlib, io, shutil, time
    import lint as _lint
    date = "2026-10-14"
    posts = tmp / "docs" / "_posts"
    posts.mkdir(parents=True)
    (tmp / "metrics").mkdir()
    shutil.copytree(Path(__file__).resolve().parent.parent / "schema", tmp / "schema")
    bindir = tmp / "bin"
    bindir.mkdir()
    (bindir / "codex").write_text(FAKE_CODEX, encoding="utf-8")
    os.chmod(bindir / "codex", 0o755)
    spec_p = tmp / "spec" / "spec.json"
    spec_p.parent.mkdir()
    cand = cand_row("c1", "https://example.com/news", facts=["販売開始", "価格は3000円"], dedup_key="x")
    _, fmap = renderlib.materials_with_ids([cand])
    base = {"status": "ok", "decline_code": "", "decline_detail": "", "title": "シンデレラガールズ販売開始", "title_fact_ids": ["F1"],
            "lede": "販売を開始する。", "lede_fact_ids": ["F1"], "blocks": [{"markdown": "販売を開始する。価格は3000円。", "fact_ids": ["F1", "F2"]}],
            "tags": ["シンデレラガールズ", "販売"], "sources": [{"url": cand["url"], "label": "公式告知"}], "event_date": None,
            "new_facts": [], "addressed_issue_ids": []}
    slugs = ("probe", "alpha", "beta", "gamma", "delta")
    art = lambda s: {"slug": s, "brand": "cg", "rank": "small", "angle": "知らせ", "candidate_ids": ["c1"], "dedup_key": "x"}
    plan = {"articles": [art(s) for s in slugs]}
    path = lambda s: posts / f"{date}-{s}.md"
    issue = {"rule_id": "R1", "issue": "事実を直す", "quote": "販売を開始する。価格は3000円。", "repair": "rewrite_claim",
             "expected": "正しく書く", "fact_ids": ["F1"]}
    fixed = dict(base, addressed_issue_ids=["I1"], blocks=[{"markdown": "販売を開始した。価格は3000円。", "fact_ids": ["F1", "F2"]}])
    # 子の CLI が終わったあとも同じプロセスグループに残り、テストが合図(release-<段>)を置いてから記事を書き換える道具の実行
    # (書き換えが控えへ戻したあとに来るように。合図が来なければ 20 秒で書かずに終わる)
    late_write = lambda s, phase: (f"import time,pathlib\nfor _ in range(400):\n    if pathlib.Path({str(tmp / ('release-' + phase))!r}).exists():\n"
                                   f"        pathlib.Path('docs/_posts/{date}-{s}.md').write_text('残った道具の書き込み')\n        break\n"
                                   f"    time.sleep(0.05)\n")
    spec = {"write-probe": [{"answer": dict(base, title="**販売開始**", lede="**販売開始**する。")}, {"answer": base}],
            **{f"write-{s}": [{"answer": base}] for s in slugs[1:]},
            # 書かせる記事を直接書いて 96 で異常終了(CLI の普通の終了コード。起動の失敗ではない)→ やり直しで答えから書く(監査指摘)
            "write-alpha": [{"touch": {f"docs/_posts/{date}-alpha.md": "不採用の直書き"}, "exit": 96}, {"answer": base}],
            # 正常終了して、別のセッションに抜け、環境を読めなくした道具を残す
            "revise-probe": [{"answer": fixed, "background": late_write("delta", "revise"), "detach": True, "nodump": True}],
            # 記事を直接書き換え、ほかの記事も書き換え、新しい記事ファイルを作り、道具を残して異常終了
            "revise-alpha": [{"touch": {f"docs/_posts/{date}-alpha.md": "壊れた直書き", f"docs/_posts/{date}-delta.md": "よその記事も直書き",
                                        f"docs/_posts/{date}-zeta.md": "勝手に作った記事"}, "answer": fixed, "exit": 97,
                              "background": late_write("beta", "revise")}],
            # 書き換えて時間切れ
            "revise-beta": [{"touch": {f"docs/_posts/{date}-beta.md": "時間切れの直書き"}, "answer": fixed, "sleep": 20}],
            # 書き換え、検算で適用を拒まれる答え(tags 1個)を返す
            "revise-gamma": [{"touch": {f"docs/_posts/{date}-gamma.md": "拒まれた稿の直書き"}, "answer": dict(fixed, tags=["販売"])}]}
    spec_p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    saved = (compose.ROOT, pipelib.ROOT, compose.classify_source, compose.storylink.prior_lookup, compose.DEADLINE, os.environ.get("PATH"))
    try:
        compose.ROOT = pipelib.ROOT = tmp
        compose.classify_source = lambda u: "公式"
        compose.storylink.prior_lookup = lambda *a: (lambda *b: [])
        compose.DEADLINE = None
        os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["FAKE_CODEX_SPEC"] = str(spec_p)
        compose.FACT_MAP.clear()
        written, aborted = compose.write_articles(date, plan, {"c1": cand}, [], {})
        # 1) 見出し・リードの Markdown は差し戻され、2回目の稿が採られる。採った記事は lint の検査も通る
        fm = compose.parse_front_matter(path("probe")) or {}
        rep = _lint.Report()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            _lint.check_plain_text(rep, "-", "title", fm.get("title"))
            _lint.check_plain_text(rep, "-", "lede", fm.get("lede"))
        check((spec_p.parent / "count-write-alpha").read_text() == "2" and "不採用の直書き" not in path("alpha").read_text(encoding="utf-8")
              and (compose.parse_front_matter(path("alpha")) or {}).get("title") == base["title"],
              f"96 で終わった CLI を起動の失敗と取り違えた・やり直さない: {path('alpha').read_text(encoding='utf-8')[:40]!r}")
        check(sorted(a["slug"] for a in written) == sorted(slugs) and fm.get("title") == base["title"] and rep.errors == 0
              and (spec_p.parent / "count-write-probe").read_text() == "2",
              f"見出し・リードの Markdown 記法を差し戻さずに採った: {fm.get('title')!r} {fm.get('lede')!r} lint {rep.errors}件 {aborted}")
        # 2) 初稿の稿の指紋と事実の対応が記録され、ファイルに残る
        key0 = renderlib.draft_key(path("probe"))
        check((compose.FACT_MAP.get(date) or {}).get("probe", {}).get(key0) == fmap,
              f"初稿を採ったのに事実の対応を記録しない: {compose.FACT_MAP.get(date)}")
        before = {s: path(s).read_bytes() for s in slugs}
        by_file = {f"docs/_posts/{date}-{s}.md": [dict(issue)] for s in ("probe", "alpha", "beta", "gamma")}
        compose.DEADLINE = time.time() + 3      # beta の子だけが時間切れになる
        res = compose.revise_articles(date, by_file, plan, {"c1": cand}, [])
        compose.DEADLINE = None
        (tmp / "release-revise").write_text("", encoding="utf-8")    # 控えへ戻したあと: 残った道具の実行に書かせる合図
        time.sleep(0.6)
        # 3) 子の直接の書き込みは、異常終了・時間切れ・適用の拒否のどれでも控えへ戻る(よその記事・新しいファイルも)。
        #    子が残した道具の実行も止めてから戻すので、戻したあとに書き換えられない(監査指摘)
        check(res == {"probe": "fixed", "alpha": "unreadable", "beta": "unreadable", "gamma": "kept"}, f"書き直しの結果: {res}")
        for s in ("alpha", "beta", "gamma", "delta"):
            check(path(s).read_bytes() == before[s], f"書き直しの子が直接書き換えた {s} が控えに戻らない: {path(s).read_text(encoding='utf-8')[:40]!r}")
        check(not path("zeta").exists(), "書き直しの子が勝手に作った記事ファイルが残った")
        # 4) 改稿を採ると、改稿の指紋の対応も記録され、初稿の対応と合わせてファイルに残る
        key1 = renderlib.draft_key(path("probe"))
        check(key1 != key0 and "販売を開始した。" in path("probe").read_text(encoding="utf-8"), "改稿が記事に当たっていない")
        compose.persist_records(date)
        rec = json.loads((tmp / "metrics" / f"factmap-{date}.json").read_text(encoding="utf-8"))
        check(rec.get("probe") == {key0: fmap, key1: fmap} and renderlib.factmap_valid(rec),
              f"採った稿(初稿・改稿)の指紋と事実の対応がファイルに残らない: {rec.get('probe')}")
        # 5) 組み直し(--reuse-plan・--recover)の初稿の子が、この号のほかの記事(承認済み)を直接書き換え・消し・作っても採らない。
        #    正常終了・異常終了・時間切れのどれでも、書かせた記事以外は控えへ戻り、書かせた記事は答えから書く
        #    (監査: 書き直しと同じ型が初稿の執筆に残っていた)
        spec.update({
            "write-fresh": [{"touch": {f"docs/_posts/{date}-fresh.md": "子が直接書いた稿", f"docs/_posts/{date}-alpha.md": "承認済みを直書き",
                                       f"docs/_posts/{date}-delta.md": None, f"docs/_posts/{date}-ghost.md": "勝手に作った記事"},
                             "answer": base, "background": late_write("probe", "reuse")}],
            # 異常終了して、別のセッションに抜け環境を読めなくした道具を残す(やり直しは正常)
            "write-late": [{"touch": {f"docs/_posts/{date}-gamma.md": "異常終了の直書き"}, "exit": 1, "background": late_write("alpha", "reuse"),
                            "detach": True, "nodump": True}, {"answer": base}],
            # 時間切れになり、別のセッションに抜け環境を読めなくした道具を残す(やり直しは正常)
            "write-slow": [{"touch": {f"docs/_posts/{date}-beta.md": "時間切れの直書き"}, "answer": base, "sleep": 20,
                            "background": late_write("gamma", "reuse"), "detach": True, "nodump": True}, {"answer": base}]})
        spec_p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        before2 = {s: path(s).read_bytes() for s in slugs}
        plan2 = {"articles": [art(s) for s in slugs + ("fresh", "late", "slow")]}
        compose.DEADLINE = time.time() + 3      # slow の子の1回目だけが時間切れになる
        written2, aborted2 = compose.write_articles(date, plan2, {"c1": cand}, [], {}, reuse=True)
        compose.DEADLINE = None
        (tmp / "release-reuse").write_text("", encoding="utf-8")     # 控えへ戻したあと: 残った道具の実行に書かせる合図
        time.sleep(0.6)
        for s in slugs:
            check(path(s).exists() and path(s).read_bytes() == before2[s],
                  f"組み直しの初稿の子が直接書き換え・消した既存の記事 {s} が控えに戻らない: {path(s).read_text(encoding='utf-8')[:40] if path(s).exists() else '(消えた)'!r}")
        check(not path("ghost").exists(), "組み直しの初稿の子が勝手に作った記事ファイルが残った")
        # late・slow のやり直しは、slow の時間切れ(締切の間際。待ちは秒に切り捨てる)のあとに起動する。締切を過ぎていれば起動せず
        # 締切で落とした記事になる(組 P)、過ぎていなければ答えから書く。どちらでも子の直書きは残さない
        outs2 = compose.WRITE_OUTCOMES.get(date) or {}
        redo_ok = all(((compose.parse_front_matter(path(s)) or {}).get("title") == base["title"] and s not in aborted2) if path(s).exists()
                      else (s in aborted2 and "締切" in outs2.get(s, "")) for s in ("late", "slow"))
        check(sorted(a["slug"] for a in written2) == sorted(slugs + ("fresh",) + tuple(s for s in ("late", "slow") if s not in aborted2))
              and set(aborted2) <= {"late", "slow"} and redo_ok
              and (compose.parse_front_matter(path("fresh")) or {}).get("title") == base["title"]
              and "子が直接書いた稿" not in path("fresh").read_text(encoding="utf-8"),
              f"組み直しで書かせた記事が答えから書かれない・締切後にやり直した: {[a['slug'] for a in written2]} {aborted2} "
              f"{ {s: (compose.WRITE_OUTCOMES.get(date) or {}).get(s) for s in ('late', 'slow')} }")
        # 6) CLI を本当に起動できない(PATH に codex が無い): 直接起動したときと同じく FileNotFoundError で止まり、書かせる記事を残さず、
        #    ほかの記事は変えない(出力の読めない回としてやり直し・落とすことはしない)
        before3 = {p_.name: p_.read_bytes() for p_ in posts.glob(f"{date}-*.md")}
        empty = tmp / "nobin"
        empty.mkdir()
        os.environ["PATH"] = str(empty)
        try:
            compose.write_articles(date, {"articles": [art(s) for s in slugs + ("fresh", "late", "slow", "nocli")]}, {"c1": cand}, [], {},
                                   reuse=True)
            check(False, "CLI を起動できないのに、出力の読めない回として扱った(FileNotFoundError にしない)")
        except FileNotFoundError as e:
            check("起動できない" in str(e) and "nocli" in str(e), f"起動できない理由が違う: {e}")
        os.environ["PATH"] = f"{bindir}{os.pathsep}{saved[5] or ''}"
        check({p_.name: p_.read_bytes() for p_ in posts.glob(f"{date}-*.md")} == before3 and not path("nocli").exists(),
              "CLI を起動できないときに、記事が変わった・書かせる記事が残った")
        # 7) child_guard だけが SIGKILL で落ちた(OOM など): CLI が別のセッションに抜けて環境を読めなくした道具を残していても、
        #    compose が記録していた子孫として止めてから控えへ戻す(監査指摘)。初稿(組み直し)と書き直しの両方
        spec.update({"write-crash": [{"background": late_write("probe", "crash"), "detach": True, "nodump": True, "kill_guard": True,
                                      "sleep": 0.3}, {"answer": base}],
                     "revise-probe": [{"answer": fixed}, {"background": late_write("delta", "crash"), "detach": True, "nodump": True,
                                                          "kill_guard": True, "sleep": 0.3}]})
        spec_p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        before4 = {s: path(s).read_bytes() for s in ("probe", "delta")}
        w4, a4 = compose.write_articles(date, {"articles": [art(s) for s in slugs + ("crash",)]}, {"c1": cand}, [], {}, reuse=True)
        res4 = compose.revise_articles(date, {f"docs/_posts/{date}-probe.md": [dict(issue)]}, plan, {"c1": cand}, [])
        (tmp / "release-crash").write_text("", encoding="utf-8")      # 控えへ戻したあと: 残った道具の実行に書かせる合図
        time.sleep(0.6)
        check("crash" in [a["slug"] for a in w4] and res4 == {"probe": "unreadable"}
              and all(path(s).read_bytes() == before4[s] for s in ("probe", "delta")),
              f"child_guard が落ちたあと、残った道具が控えへ戻した記事を書き換えた: {[a['slug'] for a in w4]} {res4}")
        # 8) 校閲の往復で、社説の書き直し(子を起こす別の工程)と記事の書き直しを並べない(run_in_turn)。子が記録の間隔より先に
        #    child_guard を SIGKILL して、環境を読めなくした道具を残しても、付け替わった孤児として止めてから控えへ戻す(監査指摘:
        #    並べたとき、記録・グループ・印のどれでも拾えず、過去号の書き直しで記事が書き換わった)
        spec["revise-probe"] = [{"answer": fixed}, {"answer": fixed}, {"background": late_write("delta", "turn"), "detach": True,
                                                                        "nodump": True, "kill_guard": True}]
        spec_p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        before5 = path("delta").read_bytes()
        res5: dict = {}
        spans: list = []

        def editorial_like():           # 社説の書き直しのように、子を起こして待つ工程
            t = time.monotonic()
            subprocess.run([sys.executable, "-c", "import time; time.sleep(0.4)"])
            spans.append(("社説", t, time.monotonic()))

        def revise_job():
            t = time.monotonic()
            res5.update(compose.revise_articles(date, {f"docs/_posts/{date}-probe.md": [dict(issue)]}, plan, {"c1": cand}, []))
            spans.append(("書き直し", t, time.monotonic()))
        pipelib.ANOMALIES.clear()
        errs = compose.run_in_turn([("社説", editorial_like), ("記事の書き直し", revise_job)])
        (tmp / "release-turn").write_text("", encoding="utf-8")
        time.sleep(0.6)
        (_, a0, a1), (_, b0, b1) = sorted(spans, key=lambda x: x[1])
        check(not errs and res5 == {"probe": "unreadable"} and a1 <= b0 and path("delta").read_bytes() == before5
              and not any("止められない" in a for a in pipelib.ANOMALIES),
              f"社説と並べた・付け替わった孤児を止めない: {errs} {res5} {spans} {pipelib.ANOMALIES}")
        # ほかに子を起こしうるスレッドが動いているあいだは、片付けを確かめたとは言わない(異常として申告させる)
        hold = __import__("threading").Event()
        other = __import__("threading").Thread(target=hold.wait, daemon=True)
        other.start()
        check(compose.ChildTracker().start().sweep() is False, "ほかのスレッドが動いているのに、片付けを確かめたとした")
        hold.set()
        other.join()
        check(compose.ChildTracker().start().sweep() is True, "ほかのスレッドが無いのに、片付けを確かめられないとした")
    finally:
        compose.ROOT, pipelib.ROOT, compose.classify_source, compose.storylink.prior_lookup, compose.DEADLINE = saved[:5]
        os.environ["PATH"] = saved[5] or ""
        os.environ.pop("FAKE_CODEX_SPEC", None)
        compose.FACT_MAP.clear()
        pipelib.ANOMALIES.clear()


def test_child_guard(tmp: Path):
    """child_guard(執筆・書き直しの子の CLI を包む)を実際に走らせる: 終了コードは CLI のもの(96・97 などどの値も)をそのまま返し、
    起動できたか・片付けを確かめられたかは結果のファイルで返す(2026-10-14 監査: 96 を起動の失敗に使い、CLI が普通に返した 96 を取り違えた)。
    起動できなければ compose は直接起動したときと同じく FileNotFoundError にする。CLI が終わったら(SIGTERM で止められても)、CLI が残した
    道具の実行を、別のセッションに抜けて環境を読めなくしたものまで止めてから終わる(監査: 印で探す止め方では見落とした)"""
    import time
    tmp.mkdir(parents=True, exist_ok=True)

    def run(cmd, name):
        out_path = tmp / f"{name}.txt"
        r = subprocess.run(compose.guarded(cmd, out_path), capture_output=True, text=True)
        st = json.loads(compose.guard_status_of(out_path).read_text(encoding="utf-8")) if compose.guard_status_of(out_path).exists() else None
        return r, st, out_path
    for code in (0, 3, 96, 97, 127):
        r, st, out_path = run(["sh", "-c", f"exit {code}"], f"rc{code}")
        check(r.returncode == code and st == {"started": True, "cleaned": True, "note": ""} and compose.guard_result(out_path, "test") is True
              and not compose.guard_status_of(out_path).exists(), f"CLI の終了コード {code} の返し方・結果: {r.returncode} {st}")
    r, st, out_path = run(["no-such-cli-imas-test"], "missing")
    check(st and st["started"] is False and "起動できない" in r.stderr and compose.guard_result(out_path, "test") is False,
          f"起動できない CLI を結果で返さない: {r.returncode} {st}")
    # 別のセッションに抜け、環境を読めなくし、あとから書く道具を残して終わる CLI(正常終了・SIGTERM で止められた)
    for name, sleep in (("exit", 0), ("term", 30)):
        out = tmp / f"left-{name}.txt"
        tool = (f"import ctypes, time, pathlib; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); time.sleep(0.8); "
                f"pathlib.Path({str(out)!r}).write_text('残った道具の書き込み')")
        cli = (f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {tool!r}], start_new_session=True, "
               f"stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep({sleep})")
        p = subprocess.Popen(compose.guarded([sys.executable, "-c", cli], tmp / f"g-{name}.txt"), start_new_session=True)
        if sleep:
            time.sleep(0.3)
            compose.end_guarded(p)
        else:
            p.wait(timeout=10)
        time.sleep(1.2)
        st = json.loads(compose.guard_status_of(tmp / f"g-{name}.txt").read_text(encoding="utf-8"))
        check(not out.exists() and p.returncode is not None and st["cleaned"] is True,
              f"CLI が残した道具の実行が child_guard のあとも書いた({name}): rc {p.returncode} {st}")


def test_child_tracker_edges(tmp: Path):
    """ChildTracker と run_in_turn の境目(監査指摘 later):
    - 始める前からの子は、起動時刻が同じ時計刻みでも止めない(止めると終了状態を横取りし、Popen が 0 を返す)
    - 所属を確かめた、すでに終わった孤児も回収する(ゾンビを残さない)
    - run_in_turn は、前の工程が時間切れで動いているあいだ、あとの工程を始めない"""
    import time
    tmp.mkdir(parents=True, exist_ok=True)
    # 前のテストが回収せずに残したゾンビの子は「始める前からの子」で、束ねは回収しない(ここでは数えない)
    zombies_before = {pid for pid, (s, ppid, _, _) in compose.proc_table().items() if ppid == os.getpid() and s == "Z"}
    early = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
    # 始める前からの子が、始めたあとに別のセッションの道具を起こして終わる(道具はここへ付け替わる)。束ねの孤児として止めない
    go, tool_pid = tmp / "go", tmp / "tool.pid"
    pre = subprocess.Popen([sys.executable, "-c",
                            "import pathlib, subprocess, sys, time\n"
                            f"while not pathlib.Path({str(go)!r}).exists(): time.sleep(0.02)\n"
                            "q = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'], start_new_session=True)\n"
                            f"pathlib.Path({str(tool_pid)!r}).write_text(str(q.pid)); time.sleep(0.3)\n"])
    tr = compose.ChildTracker().start()
    tr.t0 = 0       # 起動時刻が始まりと同じ刻みになった場合を、いつでも起こす(時刻では見分けられない)
    go.write_text("", encoding="utf-8")
    pre.wait(timeout=10)
    # 親が先に終わり、付け替わった子も終わる(ここの子のゾンビになる)
    mid = subprocess.Popen([sys.executable, "-c", "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.2)'])"])
    mid.wait()
    time.sleep(0.6)
    ok = tr.sweep()
    zombies = [pid for pid, (s, ppid, _, _) in compose.proc_table().items()
               if ppid == os.getpid() and s == "Z" and pid not in zombies_before]
    check(ok and early.poll() is None and zombies == [], f"始める前からの子を止めた・終わった孤児を回収しない: {early.poll()} {zombies}")
    tool = int(tool_pid.read_text())
    tool_alive = compose.proc_table().get(tool, ("X",))[0] not in ("Z", "X")
    check(tool_alive, "始める前からの子が、始めたあとに起こした道具を、束ねの孤児として止めた")
    for q in (tool,):
        try:
            os.kill(q, 9)
            os.waitpid(q, 0)
        except (OSError, ChildProcessError):
            pass
    early.kill()
    early.wait()
    started: list = []
    errs = compose.run_in_turn([("前", lambda: time.sleep(0.4)), ("あと", lambda: started.append(1))], timeout=0.05)
    check(started == [] and any("始めない" in e for e in errs), f"前の工程が時間切れで動いているのに、あとの工程を始めた: {errs}")
    time.sleep(0.5)


def test_guarded_launch_seams(tmp: Path):
    """組 P(start_group・finish_group・stop_groups)と組 W(child_guard・ChildTracker・控え)の継ぎ目(2026-10-21 の取り込み):
    - 時間切れの子(child_guard 越し)には SIGTERM を先に送り、child_guard に CLI と残した道具の実行を片付けさせてから回収する
      (いきなりグループごと SIGKILL すると、child_guard が片付けも結果も残せない)
    - 束ねが例外・中断で抜けても、起動した子を止め、記録した子孫(別のセッションに抜けた道具)も止め、記事を控えへ戻してから上げる"""
    import time
    date = "2026-10-14"
    posts = tmp / "docs" / "_posts"
    posts.mkdir(parents=True)
    post = posts / f"{date}-kept.md"
    post.write_text("---\n" + article_fm("kept", date) + "---\n元の本文\n", encoding="utf-8")
    # CLI の代役: 別のセッションに抜けてあとから記事を書く道具を残し、自分は長く待つ
    tool_pid = tmp / "tool.pid"
    tool = (f"import os, time, pathlib; pathlib.Path({str(tool_pid)!r}).write_text(str(os.getpid())); time.sleep(1.5); "
            f"pathlib.Path({str(post)!r}).write_text('残った道具の書き込み')")
    cli = (f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {tool!r}], start_new_session=True, "
           f"stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(30)")
    saved = (compose.ROOT, pipelib.ROOT)
    try:
        compose.ROOT = pipelib.ROOT = tmp
        # 1) 時間切れ: SIGTERM を先に送るので、child_guard が片付けて結果のファイル(cleaned)を書く
        out = tmp / "t-timeout.txt"
        with open(tmp / "log1", "w", encoding="utf-8") as lf:
            p = pipelib.start_group(compose.guarded([sys.executable, "-c", cli], out), cwd=tmp, env=compose.child_env(out.name),
                                    stdout=lf, stderr=subprocess.STDOUT)
        _wait_files(tool_pid)
        se, timed_out = compose.finish_guarded(p, 0.5)
        st = compose.guard_status_of(out)
        got = json.loads(st.read_text(encoding="utf-8")) if st.exists() else None
        tool = int(tool_pid.read_text()) if tool_pid.exists() else 0
        check(timed_out and p.returncode is not None and got and got.get("cleaned") is True and not (tool and _pid_alive(tool)),
              f"時間切れの子に SIGTERM を先に送らず、child_guard が片付け・結果を残せない: {timed_out} {got} 道具 {tool}")
        st.unlink(missing_ok=True)
        tool_pid.unlink(missing_ok=True)
        # 2) 束ねが例外で抜ける: 起動した子・抜けた道具を止め、記事を控えへ戻してから例外を上げる
        snap = compose.posts_snapshot(date)
        out2 = tmp / "t-raise.txt"
        raised = None
        try:
            with compose.guarded_batch(date, snap, "テスト") as tr:
                with open(tmp / "log2", "w", encoding="utf-8") as lf:
                    p2 = pipelib.start_group(compose.guarded([sys.executable, "-c", cli], out2), cwd=tmp,
                                             env=compose.child_env(out2.name), stdout=lf, stderr=subprocess.STDOUT)
                tr.add(p2, out2.name)
                _wait_files(tool_pid)
                post.write_text("子の直書き", encoding="utf-8")
                raise RuntimeError("中断の代役")
        except RuntimeError as e:
            raised = e
        tool2 = int(tool_pid.read_text()) if tool_pid.exists() else 0
        time.sleep(2)       # 道具が生きていれば書く時刻を過ぎる
        check(raised is not None and p2.poll() is not None and not (tool2 and _pid_alive(tool2))
              and "元の本文" in post.read_text(encoding="utf-8"),
              f"束ねが例外で抜けたとき、子・抜けた道具を止めて記事を控えへ戻さない: {p2.poll()} 道具 {tool2} "
              f"{post.read_text(encoding='utf-8')[:20]!r}")
        compose.guard_status_of(out2).unlink(missing_ok=True)
    finally:
        compose.ROOT, pipelib.ROOT = saved
        pipelib.STOPPING.clear()


def test_restore_posts_skip(tmp: Path):
    """控えへ戻す(compose.restore_posts)は、skip に渡した記事(呼び出し側が答えから書く・消す記事)には触らない: 控えにある記事の書き換えも、
    控えに無い新しい記事も、そのまま残す。ほかの記事だけを戻し、子が作った記事を消す(監査指摘: skip を壊してもテストが通った)"""
    date = "2026-10-14"
    posts = tmp / "docs" / "_posts"
    posts.mkdir(parents=True)
    p = lambda s: posts / f"{date}-{s}.md"
    for s in ("kept", "target", "other"):
        p(s).write_text("---\n" + article_fm(s, date) + "---\n元の本文\n", encoding="utf-8")
    saved, saved_p = compose.ROOT, pipelib.ROOT
    try:
        compose.ROOT = pipelib.ROOT = tmp
        snap = compose.posts_snapshot(date)
        before = {s: p(s).read_bytes() for s in ("kept", "target", "other")}
        p("target").write_text("書かせる記事の書き換え", encoding="utf-8")     # 控えにある・skip
        p("newtarget").write_text("書かせる新しい記事", encoding="utf-8")       # 控えに無い・skip
        p("other").write_text("ほかの記事の書き換え", encoding="utf-8")
        p("ghost").write_text("子が作った記事", encoding="utf-8")
        p("kept").unlink()
        back = compose.restore_posts(date, snap, skip={p("target"), p("newtarget")})
    finally:
        compose.ROOT, pipelib.ROOT = saved, saved_p
    check(p("target").read_text(encoding="utf-8") == "書かせる記事の書き換え" and p("newtarget").exists()
          and p("other").read_bytes() == before["other"] and p("kept").read_bytes() == before["kept"] and not p("ghost").exists()
          and sorted(back) == sorted(f"{date}-{s}.md" for s in ("other", "kept", "ghost")),
          f"skip の記事に触った・ほかの記事を戻さない: {back}")


def test_writer_reads_only_fetched_pages(tmp: Path):
    """執筆・書き直しを本物の子プロセス(偽の codex が本物の fetch_page.py を走らせる)で通し、根拠の URL を検める。2026-10-14 の rule_bench:
    執筆が fetch_page.py の取れなかった報道を Web 検索で読んで期間を断定し、綴りの誤った URL を出典に書いた答えが検算を通った。
    - 子の fetch_page.py が本文を取れたページは、compose が渡した記事ごとの記録に残り、そのページを根拠にした稿は採る
    - 読んでいない・綴りの誤った・取れなかった(FETCH_FAILED)ページを根拠にした稿は差し戻し、読み直して出せば採る。2回とも通らなければ落とす
    - 書き直しは、現在の稿の根拠のページと書き直しのセッションで読めたページを使える。読んでいないページを足した稿は採らない(元の稿のまま)"""
    import shutil
    date = "2026-10-14"
    repo = Path(__file__).resolve().parent.parent
    posts = tmp / "docs" / "_posts"
    posts.mkdir(parents=True)
    (tmp / "metrics").mkdir()
    shutil.copytree(repo / "schema", tmp / "schema")
    bindir = tmp / "bin"
    bindir.mkdir()
    (bindir / "codex").write_text(FAKE_CODEX, encoding="utf-8")
    os.chmod(bindir / "codex", 0o755)
    spec_p = tmp / "spec" / "spec.json"
    spec_p.parent.mkdir()
    cand = cand_row("c1", "https://example.com/news", facts=["販売開始", "価格は3000円"], dedup_key="x")
    shop, other = "https://shop.example.jp/item/1", "https://other.example.jp/p"
    typo, gone = "https://shop.exampel.jp/item/1", "https://gone.example.jp/x"
    # 共有用のクエリだけが両側で違う同じページ(読んだのは utm_source=fetch、答えは utm_source=cite)と、読んだページと同じパスの別の商品
    shop_q, shop_sib = shop + "?utm_source=fetch", shop + "?id=2"
    pages = tmp / "spec" / "pages.json"         # 子の fetch_page.py が返す本文(取りに行かない)。gone は無い = FETCH_FAILED
    pages.write_text(json.dumps({cand["url"]: "告知の本文", shop: "商品ページの本文", other: "別の店舗の本文", shop_q: "商品ページの本文",
                                 shop_sib: "別の商品の本文"}, ensure_ascii=False), encoding="utf-8")
    base = {"status": "ok", "decline_code": "", "decline_detail": "", "title": "シンデレラガールズ販売開始", "title_fact_ids": ["F1"],
            "lede": "販売を開始する。", "lede_fact_ids": ["F1"], "tags": ["シンデレラガールズ", "販売"], "event_date": None, "addressed_issue_ids": []}
    para = "販売を開始する。価格は3000円。店舗で受け取れる。"
    n1 = lambda url: {"id": "N1", "text": "店舗で受け取れる", "url": url}
    with_page = lambda url: dict(base, new_facts=[n1(url)], blocks=[{"markdown": para, "fact_ids": ["F1", "F2", "N1"]}],
                                 sources=[{"url": cand["url"], "label": "公式告知"}, {"url": url, "label": "商品ページ"}])
    # 書き直し: 指摘の段落だけを直し、現在の稿の N1 を写して、別のページの N2 を足す
    revised = lambda: dict(with_page(shop), addressed_issue_ids=["I1"], new_facts=[n1(shop), {"id": "N2", "text": "別の店舗でも扱う", "url": other}],
                           blocks=[{"markdown": "販売を開始した。価格は3000円。店舗で受け取れる。別の店舗でも扱う。", "fact_ids": ["F1", "F2", "N1", "N2"]}],
                           sources=[{"url": cand["url"], "label": "公式告知"}, {"url": shop, "label": "商品ページ"}, {"url": other, "label": "別の店舗"}])
    spec = {"write-seen": [{"fetch": [cand["url"], shop], "answer": with_page(shop)}],
            "write-late": [{"answer": with_page(shop)}, {"fetch": [shop], "answer": with_page(shop)}],
            "write-typo": [{"fetch": [shop], "answer": with_page(typo)}],
            "write-gone": [{"fetch": [gone], "answer": with_page(gone)}],
            "write-utm": [{"fetch": [shop_q], "answer": with_page(shop + "?utm_source=cite")}],
            "write-sibling": [{"fetch": [shop], "answer": with_page(shop_sib)}],
            "revise-seen": [{"fetch": [other], "answer": revised()}],
            "revise-late": [{"answer": revised()}]}
    spec_p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    art = lambda s: {"slug": s, "brand": "cg", "rank": "small", "angle": "知らせ", "candidate_ids": ["c1"], "dedup_key": "x"}
    slugs = ("seen", "late", "typo", "gone", "utm", "sibling")
    plan = {"articles": [art(s) for s in slugs]}
    path = lambda s: posts / f"{date}-{s}.md"
    env_keys = ("FAKE_CODEX_SPEC", "FAKE_FETCH_PAGE", "IMAS_FETCH_PAGES", "PATH")
    saved = (compose.ROOT, pipelib.ROOT, compose.classify_source, compose.storylink.prior_lookup, compose.DEADLINE,
             {k: os.environ.get(k) for k in env_keys})
    try:
        compose.ROOT = pipelib.ROOT = tmp
        compose.classify_source = lambda u: "公式"
        compose.storylink.prior_lookup = lambda *a: (lambda *b: [])     # 既報の引き方(main で prior_by_key から変わった)
        compose.DEADLINE = None
        os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ.update(FAKE_CODEX_SPEC=str(spec_p), FAKE_FETCH_PAGE=str(repo / "scripts" / "fetch_page.py"), IMAS_FETCH_PAGES=str(pages))
        compose.FACT_MAP.clear()
        written, aborted = compose.write_articles(date, plan, {"c1": cand}, [], {})
        counts = {s: (spec_p.parent / f"count-write-{s}").read_text() for s in slugs}
        outcomes = compose.WRITE_OUTCOMES.get(date) or {}
        check(sorted(a["slug"] for a in written) == ["late", "seen", "utm"] and sorted(aborted) == ["gone", "sibling", "typo"],
              f"読めたページを根拠にした稿だけを採る、になっていない: 採った {[a['slug'] for a in written]} / 外した {aborted}")
        check(counts == {"seen": "1", "late": "2", "typo": "2", "gone": "2", "utm": "1", "sibling": "2"}, f"差し戻しの回数: {counts}")
        check(all("fetch_page.py で本文を取れていない" in outcomes.get(s, "") for s in ("typo", "gone", "sibling")),
              f"読めていないページで落とした理由が残らない: {outcomes}")
        ret = tmp / "metrics" / "work" / date / "write-late-1.md"
        check(ret.exists() and "fetch_page.py で本文を取れていない" in ret.read_text(encoding="utf-8") and shop in ret.read_text(encoding="utf-8"),
              "差し戻しの依頼に、読めていないページと直し方が無い")
        rec = compose.fetch_record(date, "seen")
        check(pipelib.fetched_urls(rec) == {cand["url"], shop} and not pipelib.fetched_urls(compose.fetch_record(date, "gone")),
              f"子の fetch_page.py が記事ごとの記録に、取れたページだけを残さない: {pipelib.fetched_urls(rec)}")
        fm = compose.parse_front_matter(path("seen")) or {}
        check([v.get("url") for v in fm.get("verified_facts") or []] == [shop], f"読めたページの事実が記事に残らない: {fm.get('verified_facts')}")
        # 書き直し: 初稿のセッションの記録が無くても、現在の稿の根拠のページ(shop)は写して使える。書き直しで読んだ other も使える。
        # 読んでいない other を足した late の稿は採らない
        rec.unlink(missing_ok=True)
        before_late = path("late").read_bytes() if path("late").exists() else b""
        issue = {"rule_id": "R1", "issue": "事実を直す", "quote": para, "repair": "rewrite_claim", "expected": "正しく書く", "fact_ids": ["F1"]}
        res = compose.revise_articles(date, {f"docs/_posts/{date}-{s}.md": [dict(issue)] for s in ("seen", "late")}, plan, {"c1": cand}, [])
        fm = compose.parse_front_matter(path("seen")) or {}
        check(res == {"seen": "fixed", "late": "kept"}, f"書き直しの結果: {res} {compose.REVISE_NOTES.get(date)}")
        check([v.get("url") for v in fm.get("verified_facts") or []] == [shop, other] and path("late").read_bytes() == before_late,
              f"書き直しの稿の当て方: {fm.get('verified_facts')}")
        check("fetch_page.py で本文を取れていない" in (compose.REVISE_NOTES.get(date) or {}).get("late", ""),
              f"採らなかった書き直しの理由が次の巡の校閲に渡らない: {compose.REVISE_NOTES.get(date)}")
    finally:
        compose.ROOT, pipelib.ROOT, compose.classify_source, compose.storylink.prior_lookup, compose.DEADLINE = saved[:5]
        for k, v in saved[5].items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        compose.FACT_MAP.clear()
        for d in (compose.WRITE_OUTCOMES, compose.REVISE_NOTES, compose.OFFSCOPE_NOTES, compose.DROP_LOG, compose.STUCK):
            d.pop(date, None)
        pipelib.ANOMALIES.clear()


def test_fetch_record_place_broken(tmp: Path):
    """fetch_page.py の記録の置き場を作れなくても(置き場の場所が通常のファイル)、執筆・書き直しは例外で止まらない(2026-10-14 監査指摘:
    フォルダの作成の失敗が write_articles / revise_articles の外へ抜け、組版が止まる経路だった)。記録は空のまま検算へ進み
    (素材だけを根拠にした稿は採る・素材の外のページを根拠にした稿は差し戻して落とす・書き直しは結果を返す)、障害は異常として1度だけ申告する"""
    import shutil
    date = "2026-10-14"
    repo = Path(__file__).resolve().parent.parent
    posts = tmp / "docs" / "_posts"
    posts.mkdir(parents=True)
    (tmp / "metrics" / "work" / date).mkdir(parents=True)
    (tmp / "metrics" / "work" / date / "fetched").write_text("置き場の場所にあるファイル", encoding="utf-8")
    shutil.copytree(repo / "schema", tmp / "schema")
    bindir = tmp / "bin"
    bindir.mkdir()
    (bindir / "codex").write_text(FAKE_CODEX, encoding="utf-8")
    os.chmod(bindir / "codex", 0o755)
    spec_p = tmp / "spec" / "spec.json"
    spec_p.parent.mkdir()
    cand = cand_row("c1", "https://example.com/news", facts=["販売開始", "価格は3000円"], dedup_key="x")
    shop = "https://shop.example.jp/item/1"
    pages = tmp / "spec" / "pages.json"
    pages.write_text(json.dumps({cand["url"]: "告知の本文", shop: "商品ページの本文"}, ensure_ascii=False), encoding="utf-8")
    base = {"status": "ok", "decline_code": "", "decline_detail": "", "title": "シンデレラガールズ販売開始", "title_fact_ids": ["F1"],
            "lede": "販売を開始する。", "lede_fact_ids": ["F1"], "tags": ["シンデレラガールズ", "販売"], "event_date": None, "addressed_issue_ids": [],
            "new_facts": [], "blocks": [{"markdown": "販売を開始する。価格は3000円。", "fact_ids": ["F1", "F2"]}],
            "sources": [{"url": cand["url"], "label": "公式告知"}]}
    outer = dict(base, new_facts=[{"id": "N1", "text": "店舗で受け取れる", "url": shop}],
                 blocks=[{"markdown": "販売を開始する。価格は3000円。店舗で受け取れる。", "fact_ids": ["F1", "F2", "N1"]}],
                 sources=base["sources"] + [{"url": shop, "label": "商品ページ"}])
    fixed = dict(base, addressed_issue_ids=["I1"], blocks=[{"markdown": "販売を開始した。価格は3000円。", "fact_ids": ["F1", "F2"]}])
    spec = {"write-plain": [{"fetch": [cand["url"]], "answer": base}], "write-outer": [{"fetch": [shop], "answer": outer}],
            "revise-plain": [{"fetch": [cand["url"]], "answer": fixed}]}
    spec_p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    art = lambda s: {"slug": s, "brand": "cg", "rank": "small", "angle": "知らせ", "candidate_ids": ["c1"], "dedup_key": "x"}
    plan = {"articles": [art("plain"), art("outer")]}
    env_keys = ("FAKE_CODEX_SPEC", "FAKE_FETCH_PAGE", "IMAS_FETCH_PAGES", "PATH")
    saved = (compose.ROOT, pipelib.ROOT, compose.classify_source, compose.storylink.prior_lookup, compose.DEADLINE,
             {k: os.environ.get(k) for k in env_keys})
    written = aborted = res = None
    errors = []
    try:
        compose.ROOT = pipelib.ROOT = tmp
        compose.classify_source = lambda u: "公式"
        compose.storylink.prior_lookup = lambda *a: (lambda *b: [])     # 既報の引き方(main で prior_by_key から変わった)
        compose.DEADLINE = None
        getattr(compose, "FETCH_RECORD_BROKEN", set()).clear()      # 同じ障害を重ねて申告しないための控え(前のテストの分を消す)
        pipelib.ANOMALIES.clear()
        os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ.update(FAKE_CODEX_SPEC=str(spec_p), FAKE_FETCH_PAGE=str(repo / "scripts" / "fetch_page.py"), IMAS_FETCH_PAGES=str(pages))
        compose.FACT_MAP.clear()
        try:
            written, aborted = compose.write_articles(date, plan, {"c1": cand}, [], {})
            issue = {"rule_id": "R1", "issue": "事実を直す", "quote": "販売を開始する。価格は3000円。", "repair": "rewrite_claim",
                     "expected": "正しく書く", "fact_ids": ["F1"]}
            res = compose.revise_articles(date, {f"docs/_posts/{date}-plain.md": [issue]}, plan, {"c1": cand}, [])
        except Exception as e:      # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")
        noted = [a for a in pipelib.ANOMALIES if "記録の置き場" in a]
    finally:
        compose.ROOT, pipelib.ROOT, compose.classify_source, compose.storylink.prior_lookup, compose.DEADLINE = saved[:5]
        for k, v in saved[5].items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        compose.FACT_MAP.clear()
        getattr(compose, "FETCH_RECORD_BROKEN", set()).clear()
        for d in (compose.WRITE_OUTCOMES, compose.REVISE_NOTES, compose.OFFSCOPE_NOTES, compose.DROP_LOG, compose.STUCK):
            d.pop(date, None)
        pipelib.ANOMALIES.clear()
    check(not errors, f"記録の置き場を作れないと執筆・書き直しが例外で止まる: {errors}")
    check(written is not None and [a["slug"] for a in written] == ["plain"] and aborted == ["outer"],
          f"記録が空のときの検算(素材だけの稿は採り、素材の外のページの稿は落とす): {written} {aborted}")
    check(res == {"plain": "fixed"}, f"記録の置き場を作れないときの書き直しの結果: {res}")
    check(len(noted) == 1, f"記録の置き場を作れない障害を1度だけ異常として申告しない: {noted}")


def test_restore_files_ledger(tmp: Path):
    """記事を落としたあとの組み直しが失敗したときの巻き戻し(compose.restore_files)を実際に走らせる: 記事・号・台帳・予約が組み直しの
    前の中身へバイト単位で戻り、増えた記事は消える。書式のファイルを素通しで書く経路なので、見張りの許可(WRITE_PASSTHROUGH)に理由を
    書いて載せてある(載せていなければ全テストの終わりの見張りが落ちる。監査指摘: この経路を走らせるテストが無かった)"""
    date = "2026-10-14"
    (tmp / "docs" / "_posts").mkdir(parents=True)
    (tmp / "docs" / "_editions").mkdir(parents=True)
    (tmp / "stock" / "scheduled").mkdir(parents=True)
    files = {"docs/_posts/2026-10-14-a.md": "---\n" + article_fm("a", date) + "---\n本文\n",
             "docs/_editions/2026-10-14.md": "---\nnumber: 30\n---\n",
             "stock/scheduled/2026-10-20.json": json.dumps([sched_row("s1", date)], ensure_ascii=False),
             "stock/stories.yml": "- story_id: k\n"}
    for rel, text in files.items():
        (tmp / rel).write_text(text, encoding="utf-8")
    saved = compose.ROOT
    try:
        compose.ROOT = tmp
        snap = compose.snapshot_files([f"docs/_posts/{date}-*.md", f"docs/_editions/{date}.md", "stock/**/*", "stock/*"])
        (tmp / "docs" / "_posts" / "2026-10-14-a.md").write_text("壊れた記事", encoding="utf-8")
        (tmp / "docs" / "_editions" / "2026-10-14.md").unlink()
        (tmp / "docs" / "_posts" / "2026-10-14-b.md").write_text("増えた記事", encoding="utf-8")
        (tmp / "stock" / "scheduled" / "2026-10-20.json").write_text("[]", encoding="utf-8")
        n = len(STRAY_WRITES)
        compose.restore_files(snap)
    finally:
        compose.ROOT = saved
    check(all((tmp / rel).read_text(encoding="utf-8") == text for rel, text in files.items())
          and not (tmp / "docs" / "_posts" / "2026-10-14-b.md").exists() and len(STRAY_WRITES) == n,
          f"組み直しの巻き戻しが戻らない・増えた記事が残る・許可の無い素通しの書き込み: {STRAY_WRITES[n:]}")


def test_stray_governed_write_is_caught(tmp: Path):
    """見張り(_watch_governed_writes)が、本番のコードの素通しの書き込みを控え、write_checked・テストの下準備・書式の無いファイルは控えない。"""
    (tmp / "candidates").mkdir(parents=True)
    scripts = Path(__file__).resolve().parent
    ns: dict = {}
    code = ("import os\n"
            "import pipelib\n"
            "def bad(p):\n    p.write_text('[]', encoding='utf-8')\n"
            "def bad_open(p):\n    with open(p, 'w', encoding='utf-8') as f:\n        f.write('[]')\n"
            "def bad_path_open(p):\n    with p.open('a', encoding='utf-8') as f:\n        f.write('')\n"
            "def bad_bytes(p):\n    p.write_bytes(b'[]')\n"
            "def bad_move(src, p):\n    os.replace(src, p)\n"
            "def bad_atomic(p):\n    pipelib.write_atomic(p, '[]')\n"
            "def read(p):\n    return open(p, encoding='utf-8').read()\n")
    exec(compile(code, str(scripts / "compose.py"), "exec"), ns)
    n = len(STRAY_WRITES)
    target = tmp / "candidates" / "2026-10-01.json"
    ns["bad"](target)
    ns["bad_open"](target)
    ns["bad_path_open"](target)
    ns["bad_bytes"](target)
    (tmp / "x.tmp").write_text("[]", encoding="utf-8")
    ns["bad_move"](tmp / "x.tmp", target)
    ns["bad_atomic"](target)      # 書式の検査を飛ばした原子的な書き込み(write_checked を通さない write_atomic)
    caught = [s.split(" →")[0] for s in STRAY_WRITES[n:]]
    del STRAY_WRITES[n:]
    ns["bad"](tmp / "candidates" / "notes.txt")
    ns["read"](target)
    pipelib.write_checked(tmp / "candidates" / "2026-10-02.json", "[]\n")
    (tmp / "candidates" / "2026-10-03.json").write_text("[]", encoding="utf-8")
    check(caught == ["compose.py:4 bad", "compose.py:6 bad_open", "compose.py:9 bad_path_open", "compose.py:12 bad_bytes",
                     "compose.py:14 bad_move", "compose.py:16 bad_atomic"] and len(STRAY_WRITES) == n,
          f"見張りが素通しの書き込み(write_text・open・Path.open・write_bytes・os.replace・write_atomic)を控えない・控えなくてよいもの"
          f"(書式の無いファイル・読むだけ・write_checked・テストの下準備)を控えた: {caught} {STRAY_WRITES[n:]}")
    del STRAY_WRITES[n:]


def test_oncall_collect_rerun_killed_classify(tmp: Path):
    """当番の収集の取り直しで、出典の判定の取引の途中で収集が SIGKILL されても、当番の後始末は確定(commit)の前に控えから判定表と
    記事を戻し、作業ツリーを clean にして次の工程の checkout を通す(2026-10-14 取り込みの監査: 先に収集の記録を commit して HEAD が
    控えと食い違い、判定表と記事が未コミットで残って次の checkout が止まった)。本物の子プロセスと SIGKILL・ローカルの bare origin で。"""
    import oncall
    import shutil
    repo = Path(__file__).resolve().parent.parent
    root, remote = tmp / "work", tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
    g = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    g("config", "user.name", "t"); g("config", "user.email", "t@example.com")
    for name in ("scripts", "schema", "prompts"):
        shutil.copytree(repo / name, root / name, dirs_exist_ok=True)
    for name in ("docs/_posts", "candidates", "metrics", "stock"):
        (root / name).mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo / ".gitignore", root / ".gitignore")
    shutil.copy2(repo / "source_types.yml", root / "source_types.yml")
    post = root / "docs" / "_posts" / "2026-10-14-a.md"
    post.write_text("---\n" + article_fm("a", "2026-10-14") + "---\n本文\n", encoding="utf-8")
    (root / "candidates" / "2026-10-15.json").write_text("[]\n", encoding="utf-8")
    (root / "stock" / "watch-state.json").write_text("{}\n", encoding="utf-8")
    # 判定表と記事を書き換えたところで、付け直しの子が親(収集)を SIGKILL する
    (root / "scripts" / "classify_sources.py").write_text(
        "from pathlib import Path\np=Path('source_types.yml');p.write_text(p.read_text()+'\\n# txn unfinished\\n')\n", encoding="utf-8")
    (root / "scripts" / "retag_sources.py").write_text(
        "from pathlib import Path\nimport os,signal\np=next(Path('docs/_posts').glob('*.md'));p.write_text(p.read_text()+'\\n<!-- txn unfinished -->\\n')\n"
        "os.kill(os.getppid(),signal.SIGKILL)\n", encoding="utf-8")
    (root / "scripts" / "collect.py").write_text(
        "import pipelib\nfrom pathlib import Path\nPath('metrics/2026-10-15.json').write_text('{\"collect\":[{\"added\":1}]}')\n"
        "pipelib.classify_retag_lint('2026-10-15')\n", encoding="utf-8")
    g("add", "."); g("commit", "-qm", "base"); g("remote", "add", "origin", str(remote)); g("push", "-qu", "origin", "main")
    branch = "edition/2026-10-15"
    g("checkout", "-qb", branch); g("push", "-qu", "origin", branch)
    table_before, post_before = (root / "source_types.yml").read_text(encoding="utf-8"), post.read_text(encoding="utf-8")
    saved = (pipelib.ROOT, oncall.ROOT, pipelib.notify, oncall.notify, pipelib.escalate)
    try:
        pipelib.ROOT = oncall.ROOT = root
        pipelib.notify = oncall.notify = lambda *a, **k: True
        pipelib.escalate = lambda *a, **k: True
        code = oncall.rerun_stage("collect", "2026-10-15", branch, False)
        dirty = g("status", "--porcelain")
        ok = pipelib.checkout_edition_branch("2026-10-15", "compose")
    finally:
        pipelib.ROOT, oncall.ROOT, pipelib.notify, oncall.notify, pipelib.escalate = saved
    kept_metric = '"added":1' in g("show", f"origin/{branch}:metrics/2026-10-15.json").replace(" ", "")
    check(code == -9 and not dirty and ok and (root / "source_types.yml").read_text(encoding="utf-8") == table_before
          and post.read_text(encoding="utf-8") == post_before and not (root / pipelib.TXN_JOURNAL).exists() and kept_metric,
          f"収集が判定の取引の途中で殺されたあと、当番の後始末が判定表と記事を戻してから確定しない: exit {code} / dirty {dirty!r} / "
          f"checkout {ok} / 記録を確定 {kept_metric}")


def test_failed_rollback_blocks_commit(tmp: Path):
    """出典の判定の取引を戻せなかった(書き込めない 等)まま、収集・組版の途中終了の後始末が確定(commit)しない。確定すると HEAD が
    控えと食い違い、書き込めるようになっても二度と戻せない(2026-10-14 取り込みの監査)。戻せるようになれば控えから戻せる。
    本物の子プロセス・SIGTERM・実 Git で、収集(settle_interrupted)と組版(_leave_tree_clean)の両方を通す。"""
    import shutil
    repo = Path(__file__).resolve().parent.parent
    root, remote = tmp / "work", tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
    g = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    g("config", "user.name", "t"); g("config", "user.email", "t@example.com")
    for name in ("scripts", "schema", "prompts"):
        shutil.copytree(repo / name, root / name, dirs_exist_ok=True)
    for name in ("docs/_posts", "candidates", "metrics", "stock"):
        (root / name).mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo / ".gitignore", root / ".gitignore")
    shutil.copy2(repo / "source_types.yml", root / "source_types.yml")
    post = root / "docs" / "_posts" / "2026-10-14-a.md"
    post.write_text("---\n" + article_fm("a", "2026-10-14") + "---\n本文\n", encoding="utf-8")
    (root / "candidates" / "2026-10-15.json").write_text("[]\n", encoding="utf-8")
    (root / "stock" / "watch-state.json").write_text("{}\n", encoding="utf-8")
    (root / "scripts" / "classify_sources.py").write_text(
        "def unresolved_post_sources(*a,**k): return []\nif __name__ == '__main__':\n from pathlib import Path\n"
        " p=Path('source_types.yml');p.write_text(p.read_text()+'\\n# txn unfinished\\n')\n", encoding="utf-8")
    # 記事を書き換えて書き込めなくし(戻しが失敗する)、親に SIGTERM を送る
    (root / "scripts" / "retag_sources.py").write_text(
        "from pathlib import Path\nimport os,signal,time\np=next(Path('docs/_posts').glob('*.md'));p.write_text(p.read_text()+'\\n<!-- txn unfinished -->\\n')\n"
        "p.chmod(0o444)\nos.kill(os.getppid(),signal.SIGTERM)\ntime.sleep(30)\n", encoding="utf-8")
    g("add", "."); g("commit", "-qm", "base"); g("remote", "add", "origin", str(remote)); g("push", "-qu", "origin", "main")
    branch = "edition/2026-10-15"
    g("checkout", "-qb", branch); g("push", "-qu", "origin", branch)
    original_post, original_table = post.read_bytes(), (root / "source_types.yml").read_bytes()
    common = ("import sys,signal,os;sys.path.insert(0,'scripts');import pipelib,collect,compose;from pathlib import Path\n"
              "pipelib.set_quiet(True);pipelib.notify=collect.notify=compose.notify=lambda *a,**k: True\n"
              "Path('metrics/2026-10-15.json').write_text('{\"collect\":[{\"added\":1}]}')\n")
    programs = {"collect": ("signal.signal(signal.SIGTERM,collect._on_sigterm)\ntry:\n pipelib.classify_retag_lint('2026-10-15')\n"
                            "except BaseException as e:\n collect.settle_interrupted('2026-10-15','edition/2026-10-15',e)\n sys.exit(143)\n"),
                "compose": ("compose.STARTED_ON='edition/2026-10-15';compose.STARTED_DATE='2026-10-15'\ndef handler(s,f):\n"
                            " compose._leave_tree_clean('SIGTERM')\n os._exit(143)\nsignal.signal(signal.SIGTERM,handler)\n"
                            "pipelib.classify_retag_lint('2026-10-15',lint=False)\n")}
    results = {}
    saved = (pipelib.ROOT, pipelib.notify)
    try:
        for name, program in programs.items():
            head = g("rev-parse", "HEAD")
            r = subprocess.run([sys.executable, "-c", common + program], cwd=root, capture_output=True, text=True, timeout=60)
            moved = head != g("rev-parse", "HEAD")
            post.chmod(0o644)          # 書き込めるようになった
            pipelib.ROOT, pipelib.notify = root, (lambda *a, **k: True)
            back = pipelib.recover_classify_txn()
            results[name] = (r.returncode, moved, back, post.read_bytes() == original_post,
                             (root / "source_types.yml").read_bytes() == original_table)
            g("checkout", "-q", "--", "."); g("clean", "-qfd", "--", "metrics")
    finally:
        pipelib.ROOT, pipelib.notify = saved
    check(all(code == 143 and not moved and "戻した" in back and post_ok and table_ok
              for code, moved, back, post_ok, table_ok in results.values()) and len(results) == 2,
          f"判定の取引を戻せないまま、途中終了の後始末が確定した・書き込めるようになっても戻せない: {results}")


def test_compose_run_derive(tmp: Path):
    """組版の派生(compose.run_derive): 成功は True、失敗(0 以外の終了・スクリプトが無い)は終了コードと最後の行を異常として申告して
    False。組版は derive を run_derive だけで呼ぶ(監査指摘 2026-10-14: 5か所とも終了コードを見ず、derive が落ちても黙って lint へ進んだ)。"""
    (tmp / "scripts").mkdir(parents=True)
    fake = tmp / "scripts" / "derive.py"
    saved, seen = (compose.ROOT, compose.anomaly), []
    try:
        compose.ROOT = tmp
        compose.anomaly = lambda job, text: seen.append(text)
        fake.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        ok = compose.run_derive("2026-10-05")
        fake.write_text("import sys\nprint('途中', file=sys.stderr)\nprint('ranking が作れない', file=sys.stderr)\nsys.exit(3)\n",
                        encoding="utf-8")
        bad = compose.run_derive("2026-10-05")
        fake.unlink()
        missing = compose.run_derive("2026-10-05")
    finally:
        compose.ROOT, compose.anomaly = saved
    check(ok is True and bad is False and missing is False and len(seen) == 2
          and "終了 3" in seen[0] and "ranking が作れない" in seen[0] and "終了 2" in seen[1],
          f"組版の派生の失敗を申告しない・成功を失敗にした: {ok} {bad} {missing} {seen}")
    src = Path(compose.__file__).read_text(encoding="utf-8")
    check(src.count('"derive.py"') == 1, "組版が派生(derive.py)を run_derive を通さずに呼んでいる(終了コードを見ない)")


def test_derive_write(tmp: Path):
    """derive --write(号の最後の形)を見張りの下で通す: 書式で検めて書き、素通しの書き込みが無い(監査指摘: derive の書き込みを
    素通しに変えてもテストが通った)。組み立て途中の号(ranking が空)から、最後の形(ranking 8件・数・誕生日)を書く。"""
    import derive
    import shutil
    import yaml
    d = "2026-10-03"
    posts, eds, data = tmp / "docs" / "_posts", tmp / "docs" / "_editions", tmp / "docs" / "_data"
    for p in (posts, eds, data, tmp / "candidates"):
        p.mkdir(parents=True, exist_ok=True)
    real = Path(__file__).resolve().parent.parent / "docs" / "_data" / "idols.json"
    shutil.copy(real, data / "idols.json")
    names = [x["name"] for x in json.loads(real.read_text(encoding="utf-8"))[:8]]
    (posts / f"{d}-a.md").write_text("---\n" + article_fm("a", d, rank="lead", title="a") + "---\n" + "、".join(names) + "が出演する。\n",
                                     encoding="utf-8")
    (eds / f"{d}.md").write_text(
        f"---\nnumber: 15\ndate: '{d}'\nweekday: 土\nissued_at: '{d}T06:00:00+09:00'\npages: 0\narticle_count: 0\ncorrected_count: 0\n"
        "lead_slug: a\ndigest:\n- label: 本日\n  rows:\n  - k: 発\n    t: A\n    d: ''\n    brand: cg\n    slug: a\n"
        "- label: 昨日\n  rows: []\n- label: 継続中\n  rows: []\n- label: 明日\n  rows: []\nranking: []\nbirthdays: []\n---\n", encoding="utf-8")
    saved, n = (derive.ROOT, sys.argv), len(STRAY_WRITES)
    try:
        derive.ROOT, sys.argv = tmp, ["derive.py", "--date", d, "--write"]
        rc = derive.main()
    finally:
        derive.ROOT, sys.argv = saved
    text = (eds / f"{d}.md").read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("\n---\n", 1)[0].lstrip("-\n"))
    check(rc == 0 and len(fm.get("ranking") or []) == 8 and fm.get("article_count") == 1
          and pipelib.schema_errors(eds / f"{d}.md", text) == [] and len(STRAY_WRITES) == n,
          f"derive の書き込みが最後の形を書式どおりに書かない・素通しで書いた: rc={rc} {fm.get('ranking')} {STRAY_WRITES[n:]}")


def test_canonicalize_and_assign_ranks(tmp: Path):
    """組版の記事の書き直し(計画への固定項目の写し・枠の割り当て)を見張りの下で通す。どちらも書式どおりに書き、素通しで書かない
    (監査指摘: この2つの書き込みを素通しに変えても全テストが通った)。"""
    import yaml
    d = "2026-10-03"
    posts = tmp / "docs" / "_posts"
    posts.mkdir(parents=True)
    a, b = posts / f"{d}-a.md", posts / f"{d}-b.md"
    # 執筆の転記が計画と食い違う(slug・号・面・候補)・出典の種別が判定表と違う・訂正欄が初期値でない
    a.write_text("---\n" + article_fm("wrong", "2026-01-01", brand="other", candidate_ids=["zz"], corrected=True, src="公式",
                                      sources=[{"label": "l1", "url": "https://a.example/1", "type": "公式"}])
                 + "---\n" + "あ" * 800 + "\n", encoding="utf-8")
    b.write_text("---\n" + article_fm("b", d, candidate_ids=["c2"]) + "---\n" + "い" * 100 + "\n", encoding="utf-8")
    art_a = {"slug": "a", "brand": "cg", "rank": "small", "candidate_ids": ["c1"]}
    art_b = {"slug": "b", "brand": "cg", "rank": "small", "candidate_ids": ["c2"]}
    saved, n = compose.ROOT, len(STRAY_WRITES)
    try:
        compose.ROOT = tmp
        compose.canonicalize_article(d, art_a, {"c1": cand_row("c1", "https://a.example/1")})
        compose.assign_ranks(d, {"articles": [{"slug": "a", "lead_score": 10}, {"slug": "b", "lead_score": 50}]}, [art_a, art_b])
    finally:
        compose.ROOT = saved
    fm = lambda p: yaml.safe_load(p.read_text(encoding="utf-8").split("\n---\n", 1)[0].lstrip("-\n"))
    fa, fb_ = fm(a), fm(b)
    want_type = compose.classify_source("https://a.example/1")
    check((fa["slug"], str(fa["edition"]), fa["brand"], fa["candidate_ids"], fa["corrected"]) == ("a", d, "cg", ["c1"], False)
          and fa["sources"][0]["type"] == want_type and fa["src"] == want_type and fa["rank"] == "large" and fb_["rank"] == "lead"
          and all(pipelib.schema_errors(p, p.read_text(encoding="utf-8")) == [] for p in (a, b)) and len(STRAY_WRITES) == n,
          f"固定項目の写し・枠の割り当てが書式どおりに書かない・素通しで書いた: {fa} {fb_.get('rank')} {STRAY_WRITES[n:]}")


def test_reassemble_reranks_rewritten(tmp: Path):
    """書き直しで本文が短くなった記事は、組版をやり直す前に枠を当て直す。当て直さずに組み直すと、組版のあとの lint が
    「本文と枠が不一致」で赤くなる(2026-10-15号: 校閲の書き直しで 394字・438字になった記事が medium のまま組版された)。
    一面は動かさない。main の組版のやり直しはすべて枠の当て直しを通す"""
    import assemble as _assemble
    import inspect
    d = "2026-10-15"
    posts = tmp / "docs" / "_posts"
    posts.mkdir(parents=True)
    a, b = posts / f"{d}-a.md", posts / f"{d}-b.md"
    a.write_text("---\n" + article_fm("a", d, rank="medium") + "---\n" + "あ" * 394 + "\n", encoding="utf-8")
    b.write_text("---\n" + article_fm("b", d, rank="lead") + "---\n" + "い" * 300 + "\n", encoding="utf-8")
    art_a, art_b = {"slug": "a", "brand": "cg", "rank": "medium"}, {"slug": "b", "brand": "cg", "rank": "lead"}
    plan = {"articles": [{"slug": "a", "lead_score": 90, "rank": "medium"}, {"slug": "b", "lead_score": 10, "rank": "lead"}]}
    seen = []
    saved = compose.ROOT, _assemble.run, compose.persist_records
    try:
        compose.ROOT = tmp
        compose.persist_records = lambda *x, **k: None
        _assemble.run = lambda date, number, **k: (seen.append(re.search(r"^rank: (\S+)$", a.read_text(encoding="utf-8"), re.M).group(1)), 0)[1]
        compose.reassemble(d, 1, plan, [art_a, art_b])
    finally:
        compose.ROOT, _assemble.run, compose.persist_records = saved
    check(seen == ["small"] and art_a["rank"] == "small" and plan["articles"][0]["rank"] == "small"
          and re.search(r"^rank: lead$", b.read_text(encoding="utf-8"), re.M),
          f"組版のやり直しの前に枠を当て直していない(または一面を動かした): 組版が見た枠 {seen} / {art_a} / {plan}")
    src = inspect.getsource(compose.main)
    calls = re.findall(r"\b(run_assemble|reassemble)\(", src)
    check(calls.count("run_assemble") == 1 and calls[0] == "run_assemble" and calls.count("reassemble") >= 4,
          f"main の組版のやり直しが枠の当て直しを通っていない: {calls}")
    at = src.index("revise_articles(date, by_file, plan, cands, aborted)\n")
    check(src.index("assign_ranks(date, plan, written, keep_lead=True)", at) < src.index("run_lint(date)", at),
          "lint 赤の書き直しのあと、枠を当て直す前に lint を見ている(枠の不一致で記事を落とす)")


def test_reassemble_restands_lead_after_revise_drop(tmp: Path):
    """書き直しで一面が見送られた(記事ファイルが消えた)号を組み直すときは、残りの記事から lead_score 最大を一面に立てる。
    立て直しが校閲ブロックの経路にしか無く、一面0本のまま組版されて lint が赤くなった(2026-10-16号: 一面の記事が書き直しで
    NOT_NEWS になった)。roundup は一面にしない"""
    import assemble as _assemble
    d = "2026-10-16"
    posts = tmp / "docs" / "_posts"
    posts.mkdir(parents=True)
    a, b, r = posts / f"{d}-a.md", posts / f"{d}-b.md", posts / f"{d}-r.md"
    a.write_text("---\n" + article_fm("a", d, rank="small") + "---\n" + "あ" * 200 + "\n", encoding="utf-8")
    b.write_text("---\n" + article_fm("b", d, rank="small") + "---\n" + "い" * 200 + "\n", encoding="utf-8")
    r.write_text("---\n" + article_fm("r", d, rank="roundup") + "---\n" + "う" * 200 + "\n", encoding="utf-8")
    plan = {"articles": [{"slug": "gone", "lead_score": 99, "rank": "lead"}, {"slug": "a", "lead_score": 20, "rank": "small"},
                         {"slug": "b", "lead_score": 60, "rank": "small"}, {"slug": "r", "lead_score": 90, "rank": "roundup"}]}
    written = [dict(x, brand="cg") for x in plan["articles"]]   # 一面 gone は書き直しで見送られ、ファイルが無い
    seen = []
    saved = compose.ROOT, _assemble.run, compose.persist_records
    try:
        compose.ROOT = tmp
        compose.persist_records = lambda *x, **k: None
        _assemble.run = lambda date, number, **k: (seen.append(re.search(r"^rank: (\S+)$", b.read_text(encoding="utf-8"), re.M).group(1)), 0)[1]
        compose.reassemble(d, 1, plan, written)
    finally:
        compose.ROOT, _assemble.run, compose.persist_records = saved
    ranks = {p.stem[len(d) + 1:]: re.search(r"^rank: (\S+)$", p.read_text(encoding="utf-8"), re.M).group(1) for p in (a, b, r)}
    check(seen == ["lead"] and ranks == {"a": "small", "b": "lead", "r": "roundup"} and plan["articles"][2]["rank"] == "lead",
          f"一面が見送られた号を一面0本のまま組み直した(または roundup を一面にした): 組版が見た枠 {seen} / {ranks} / {plan}")


def test_rewrite_past_apply_answer(tmp: Path):
    """過去記事の書き直しの差し替え(rewrite_past.apply_answer)と枠の付け直し(fix_rank)を見張りの下で通す: 元の記録(訂正・号・slug・
    候補)を残して内容を差し替え、短くなった記事の枠を付け直し、書式どおりに書く(監査指摘: 素通しに変えても全テストが通った)。"""
    import rewrite_past
    import yaml
    d = "2026-09-12"
    posts = tmp / "docs" / "_posts"
    posts.mkdir(parents=True)
    p = posts / f"{d}-x.md"
    p.write_text("---\n" + article_fm("x", d, rank="large", candidate_ids=["c1"], title="旧見出し") + "---\n旧本文\n", encoding="utf-8")
    mats, _ = renderlib.materials_with_ids(MATS)
    n = len(STRAY_WRITES)
    problems = rewrite_past.apply_answer(p, {"materials": mats, "rank": "small", "date": d}, OK)
    text = p.read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("\n---\n", 1)[0].lstrip("-\n"))
    check(problems == [] and fm["title"] == OK["title"] and fm["slug"] == "x" and fm["candidate_ids"] == ["c1"] and fm["rank"] == "small"
          and "旧本文" not in text and not p.with_suffix(".rewrite.tmp").exists()
          and pipelib.schema_errors(p, text) == [] and len(STRAY_WRITES) == n,
          f"過去記事の差し替え・枠の付け直しが書式どおりに書かない・素通しで書いた: {problems} {fm.get('rank')} {STRAY_WRITES[n:]}")


def test_restore_files(tmp: Path):
    """落とした後の組版が失敗したときの巻き戻し(compose.restore_files): 控えた記事・号・予約をバイト単位で戻し、後から増えた
    ファイルを消す。書式に合わない古い中身でも戻す(見張りの許可の理由。監査指摘: この経路をテストが通っていなかった)。"""
    d = "2026-10-05"
    for sub in ("docs/_posts", "docs/_editions", "stock/scheduled"):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
    a, b = tmp / "docs" / "_posts" / f"{d}-a.md", tmp / "docs" / "_posts" / f"{d}-b.md"
    ed, sc = tmp / "docs" / "_editions" / f"{d}.md", tmp / "stock" / "scheduled" / "2026-10-08.json"
    a.write_bytes(("---\n" + article_fm("a", d) + "---\n本文\n").encode("utf-8"))
    b.write_bytes(b"---\ntitle: old form\n---\n")          # 書式の古い記事も、控えたとおりに戻す
    ed.write_bytes(b"---\nnumber: 1\n---\n")
    sc.write_bytes(b"[]\n")
    before = {p: p.read_bytes() for p in (a, b, ed, sc)}
    saved = compose.ROOT
    try:
        compose.ROOT = tmp
        snap = compose.snapshot_files([f"docs/_posts/{d}-*.md", f"docs/_editions/{d}.md", "stock/scheduled/*.json"])
        a.write_text("changed", encoding="utf-8")
        b.unlink()
        ed.write_text("changed", encoding="utf-8")
        new = tmp / "docs" / "_posts" / f"{d}-new.md"
        new.write_text("new", encoding="utf-8")
        compose.restore_files(snap)
    finally:
        compose.ROOT = saved
    check(all(p.exists() and p.read_bytes() == v for p, v in before.items()) and not new.exists(),
          "落とした後の組版の巻き戻しが、控えた中身へ戻さない・後から増えた記事を残す")


def test_run_codex_isolation(tmp: Path):
    """監査(run_codex)は使い捨ての作業ツリーで動く: CLI の -C とプロセスの cwd がそこを指し、呼び出し元の作業ツリーに何も残さず、
    CLI が失敗しても片付ける(監査指摘: -C を呼び出し元に変えても全テストが通った)。偽の codex を PATH の先頭に置いて実際に起動する。"""
    import oncall
    repo, bindir = tmp / "repo", tmp / "bin"
    repo.mkdir(parents=True)
    bindir.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@example.com"); g("config", "user.name", "t")
    (repo / "a").write_text("1"); g("add", "a"); g("commit", "-q", "-m", "base")
    log, fail_flag = tmp / "codex-calls.jsonl", tmp / "fail"
    answer = json.dumps({"verdict": "approve", "verification": AUDIT_RAN, "must_fix": [], "later": [], "notes": ""}, ensure_ascii=False)
    fake = bindir / "codex"
    fake.write_text(f"#!{sys.executable}\n"
                    "import json, os, sys\n"
                    "a = sys.argv[1:]\n"
                    "c, o = a[a.index('-C') + 1], a[a.index('-o') + 1]\n"
                    f"open({str(log)!r}, 'a').write(json.dumps({{'C': c, 'cwd': os.getcwd()}}) + '\\n')\n"
                    "open(os.path.join(c, 'written-by-audit'), 'w').write('x')\n"
                    "open('cwd-written', 'w').write('x')\n"
                    f"if os.path.exists({str(fail_flag)!r}):\n    sys.exit(1)\n"
                    f"open(o, 'w', encoding='utf-8').write({answer!r})\n", encoding="utf-8")
    fake.chmod(0o755)
    schema = Path(__file__).resolve().parent.parent / "schema" / "oncall-review.schema.json"
    clean = lambda: (g("status", "--porcelain") == "" and not (repo / "written-by-audit").exists() and not (repo / "cwd-written").exists()
                     and sum(1 for ln in g("worktree", "list", "--porcelain").splitlines() if ln.startswith("worktree ")) == 1)
    saved_path, saved_root = os.environ.get("PATH", ""), pipelib.ROOT
    try:
        os.environ["PATH"] = f"{bindir}:{saved_path}"
        pipelib.ROOT = tmp           # 生の出力の控え(save_raw)も一時ディレクトリへ
        ans = oncall.run_codex("監査して", schema, repo)
        ok_run = ans.get("verdict") == "approve" and clean()
        fail_flag.write_text("1")
        failed = False
        try:
            oncall.run_codex("監査して", schema, repo)
        except RuntimeError:
            failed = True
        ok_fail = failed and clean()
    finally:
        os.environ["PATH"], pipelib.ROOT = saved_path, saved_root
    calls = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    real = os.path.realpath
    ok_place = len(calls) == 2 and all(real(c["C"]) == real(c["cwd"]) != real(str(repo)) and not Path(c["C"]).exists() for c in calls)
    check(ok_run and ok_fail and ok_place,
          f"監査が使い捨ての作業ツリーで動かない・呼び出し元に書き込みが残る・失敗で片付かない: {ok_run} {ok_fail} {ok_place} {calls}")


def test_remove_worktree(tmp: Path):
    """監査用の使い捨ての作業ツリーは、git で消せなくても片付け、片付けられなければ異常として申告する(黙って残さない。監査指摘)。"""
    import oncall
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    g = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@example.com"); g("config", "user.name", "t")
    (repo / "a").write_text("1"); g("add", "a"); g("commit", "-q", "-m", "base")
    listed = lambda aw: f"worktree {aw}" in g("worktree", "list", "--porcelain").splitlines()
    saved_sh, saved_rmtree, saved_anomaly, seen = oncall.sh, oncall.shutil.rmtree, oncall.anomaly, []
    try:
        oncall.anomaly = lambda job, text: seen.append(text)
        aw = tmp / "aw1"
        g("worktree", "add", "-q", "--detach", str(aw), "HEAD")
        oncall.remove_worktree(aw, repo)
        ok_normal = not aw.exists() and not listed(aw) and not seen
        # git worktree remove だけが失敗: ディレクトリを消して登録を掃除する
        aw = tmp / "aw2"
        g("worktree", "add", "-q", "--detach", str(aw), "HEAD")
        oncall.sh = lambda args, cwd, timeout=600: (subprocess.CompletedProcess(args, 1, "", "fatal: injected")
                                                    if args[:3] == ["git", "worktree", "remove"] else saved_sh(args, cwd, timeout))
        oncall.remove_worktree(aw, repo)
        ok_fallback = not aw.exists() and not listed(aw) and not seen
        # ディレクトリも消せない: 異常として申告する
        aw = tmp / "aw3"
        g("worktree", "add", "-q", "--detach", str(aw), "HEAD")
        oncall.shutil.rmtree = lambda *a, **k: None
        oncall.remove_worktree(aw, repo)
        ok_report = aw.exists() and len(seen) == 1 and "片付けられない" in seen[0]
    finally:
        oncall.sh, oncall.shutil.rmtree, oncall.anomaly = saved_sh, saved_rmtree, saved_anomaly
    check(ok_normal and ok_fallback and ok_report,
          f"監査用の作業ツリーの片付け(普通に消す・git で消せなければディレクトリを消す・それも無理なら申告): {ok_normal} {ok_fallback} {ok_report} {seen}")


def test_write_checked_contract(tmp: Path):
    """write_checked: テスト(strict)では書式に合わない中身で例外にして元のファイルを変えない。本番では書いて異常を申告する。
    差し替えに失敗しても元のファイルを保ち、一時ファイルを残さない。一時ファイルは書く先と同じディレクトリで、Git の対象外の名前
    (監査指摘: 検査を空にしても全テストが通った・別の置き場からの差し替えが EXDEV で止まり一時ファイルが残った)。"""
    d = tmp / "candidates"
    d.mkdir(parents=True)
    p = d / "2026-10-05.json"
    good = json.dumps([cand_row("c1", "https://a.example/1")], ensure_ascii=False) + "\n"
    bad = json.dumps([cand_row("c1", "https://a.example/1", unknown_field=1)], ensure_ascii=False) + "\n"
    pipelib.write_checked(p, good)
    raised = False
    try:
        pipelib.write_checked(p, bad)
    except pipelib.SchemaViolation as e:
        raised = "candidates.schema.json" in str(e)
    check(raised and p.read_text(encoding="utf-8") == good, "テストで書式に合わない中身を書かせても例外にならない・元のファイルを変えた")
    saved_anomaly, seen = pipelib.anomaly, []
    os.environ.pop("IMAS_STRICT_WRITES")
    try:
        pipelib.anomaly = lambda stage, msg, *a, **k: seen.append((stage, msg))
        pipelib.write_checked(p, bad)
    finally:
        os.environ["IMAS_STRICT_WRITES"] = "1"
        pipelib.anomaly = saved_anomaly
    check(p.read_text(encoding="utf-8") == bad and len(seen) == 1 and seen[0][0] == "schema" and "unknown_field" in seen[0][1],
          f"本番で書式に合わない中身を書かない・異常として申告しない: {seen}")
    # 差し替えの失敗(EXDEV 等): 元のファイルを保ち、一時ファイルを残さない
    pipelib.write_checked(p, good)
    saved_replace, used = pipelib.os.replace, []

    def failing_replace(src, dst):
        used.append(Path(src).parent)
        raise OSError(18, "Invalid cross-device link")
    pipelib.os.replace = failing_replace
    try:
        failed = False
        try:
            pipelib.write_checked(p, json.dumps([cand_row("c2", "https://a.example/2")]) + "\n")
        except OSError:
            failed = True
    finally:
        pipelib.os.replace = saved_replace
    left = [x.name for x in d.iterdir() if x.name != p.name]
    check(failed and p.read_text(encoding="utf-8") == good and not left and used == [d],
          f"差し替えに失敗したとき元のファイルを保たない・一時ファイルを残した・一時ファイルが書く先と別の置き場: {left} {used}")
    ign = subprocess.run(["git", "check-ignore", "-q", "--no-index", f"candidates/.{p.name}.123.tmp"],
                         cwd=Path(__file__).resolve().parent.parent, capture_output=True)
    check(ign.returncode == 0, "write_checked の一時ファイルの名前が .gitignore の対象でない(止まって残ると作業ツリーを汚す)")


RELEASE_DAY = "2026-10-14"


def _release_fixture(base: Path, conflict: bool = False, ahead: bool = False) -> tuple[Path, Path]:
    """発行の試験用の Git(本物の origin には触らない): 手元の bare を origin にした clone。main に1つ、
    edition/RELEASE_DAY に号の commit を1つ(origin へ push 済み)。conflict は main と edition で同じファイルを別々に変える。
    ahead は edition に origin へ出していない commit を1つ足す。戻りは (作業ツリー, bare)。"""
    bare, repo = base / "origin.git", base / "work"
    base.mkdir(parents=True)

    def g(*a, cwd=repo):
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True)
    g("init", "-q", "--bare", "-b", "main", str(bare), cwd=base)
    g("clone", "-q", str(bare), str(repo), cwd=base)
    g("symbolic-ref", "HEAD", "refs/heads/main")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    (repo / "x").write_text("base\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-qm", "base")
    g("push", "-q", "-u", "origin", "main")
    g("checkout", "-q", "-b", f"edition/{RELEASE_DAY}")
    ed = repo / "docs" / "_editions" / f"{RELEASE_DAY}.md"
    ed.parent.mkdir(parents=True)
    ed.write_text("---\nnumber: 58\narticle_count: 1\n---\n", encoding="utf-8")
    if conflict:
        (repo / "x").write_text("edition\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-qm", "edition")
    g("push", "-q", "-u", "origin", f"edition/{RELEASE_DAY}")
    if ahead:
        (repo / "local").write_text("組み直した commit(まだ push していない)\n", encoding="utf-8")
        g("add", "-A")
        g("commit", "-qm", "local")
    if conflict:
        g("checkout", "-q", "main")
        (repo / "x").write_text("main\n", encoding="utf-8")
        g("commit", "-qam", "main changed")
        g("push", "-q", "origin", "main")
        g("checkout", "-q", f"edition/{RELEASE_DAY}")
    return repo, bare


def _git_out(cwd: Path, *a) -> str:
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True).stdout.strip()


class _ReleaseEnv:
    """release.main を手元の Git だけで走らせる: 校閲・発行前の lint・配信・排他・当番・通知を偽物にする(この試験は Git の扱いを見る)。"""

    def __init__(self, repo: Path, dry: bool = False):
        import release
        self.r, self.repo, self.dry = release, repo, dry
        self.notes: list[tuple[bool, str]] = []
        self.escalated: list[str] = []
        self.deployed: list[bool] = []

    def __enter__(self):
        r = self.r
        real_run = subprocess.run

        def run(cmd, *a, **k):
            if len(cmd) > 1 and str(cmd[1]).endswith("lint.py"):
                return subprocess.CompletedProcess(cmd, 0, "lint: 0 errors\n", "")
            return real_run(cmd, *a, **k)
        self.saved = (r.ROOT, r.notify, r.review_verdict, r.deploy_site, r.DRY_RUN, subprocess.run, pipelib.job_lock,
                      pipelib.dedupe_source_table, pipelib.escalate, sys.argv)
        r.ROOT = self.repo
        r.notify = lambda msg, ok=True: self.notes.append((ok, msg))
        r.review_verdict = lambda d: ("approve", "試験")
        r.deploy_site = lambda: self.deployed.append(True) or "配信済み(試験)。"
        subprocess.run = run
        pipelib.job_lock = lambda *a, **k: None
        pipelib.dedupe_source_table = lambda *a, **k: []
        pipelib.escalate = lambda stage, date, reason, **k: self.escalated.append(f"{stage}: {reason}") or True
        sys.argv = ["release.py", "--date", RELEASE_DAY] + (["--dry-run"] if self.dry else [])
        return self

    def __exit__(self, *exc):
        r = self.r
        (r.ROOT, r.notify, r.review_verdict, r.deploy_site, r.DRY_RUN, subprocess.run, pipelib.job_lock,
         pipelib.dedupe_source_table, pipelib.escalate, sys.argv) = self.saved
        return False

    def main(self):
        """release.main の終了コード。例外で抜けたら「例外: …」(直す前の実装は失敗を例外で上げる)。"""
        try:
            return self.r.main()
        except Exception as e:      # noqa: BLE001
            return f"例外: {type(e).__name__}: {e}"


def test_release_push_retry(tmp: Path):
    """main の push だけが失敗したら、配信せず edition も消さずに止まる。再実行は squash が「差分なし」でも手元の号の commit を
    push し、origin/main に号が届いたことを確かめてから配信・edition の削除へ進む(監査指摘 ar7-1: 再実行が push を省き、
    origin/main に号が無いまま成功で終わって edition ブランチまで消した)。push の失敗は origin の pre-receive で本物に起こす。"""
    repo, bare = _release_fixture(tmp / "push")
    marker = tmp / "push" / "reject-main-once"
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nwhile read old new ref; do\n"
                    f"  if [ \"$ref\" = refs/heads/main ] && [ -e '{marker}' ]; then rm -f '{marker}'; echo 試験の拒否 >&2; exit 1; fi\n"
                    "done\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    marker.touch()
    on_origin = lambda: subprocess.run(["git", "cat-file", "-e", f"main:docs/_editions/{RELEASE_DAY}.md"], cwd=bare,
                                       capture_output=True).returncode == 0
    edition_on_origin = lambda: bool(_git_out(bare, "branch", "--list", f"edition/{RELEASE_DAY}"))
    with _ReleaseEnv(repo) as env:
        rc = env.main()
        check(rc == 1 and not env.deployed and edition_on_origin() and not on_origin() and env.escalated
              and _git_out(repo, "status", "--porcelain") == "",
              f"main の push の失敗で止まらない・配信した・edition を消した・当番を呼ばない: rc={rc!r} 配信={env.deployed} "
              f"edition={edition_on_origin()} 当番={env.escalated}")
        check(not marker.exists(), "試験の下準備: origin が main の push を1回拒否していない")
        rc = env.main()
        check(rc == 0 and on_origin() and _git_out(bare, "rev-parse", "main") == _git_out(repo, "rev-parse", "main")
              and env.deployed and not edition_on_origin(),
              f"push の失敗の後の再実行で origin/main に号が届かない(か、届く前に edition を消した): rc={rc!r} "
              f"origin に号={on_origin()} 配信={env.deployed} edition={edition_on_origin()}")
    # push が成功と答えても、origin から取り直して号が届いたと確かめられなければ進まない(届かない push・取り直しの失敗)
    import release
    real_git = release.git
    for what, fake in (("push が成功と答えたが origin に届いていない", ("push", "origin", "main")),
                       ("push の後の origin の取り直しに失敗", ("fetch", "origin", "main"))):
        repo, bare = _release_fixture(tmp / ("silent" if fake[0] == "push" else "nofetch"))
        edition_on_origin = lambda: bool(_git_out(bare, "branch", "--list", f"edition/{RELEASE_DAY}"))

        def faked(*a, check=True, _fake=fake):
            if a == _fake:
                return subprocess.CompletedProcess(["git", *a], 0 if a[0] == "push" else 128, "",
                                                   "" if a[0] == "push" else "fatal: unable to access(試験)")
            return real_git(*a, check=check)
        with _ReleaseEnv(repo) as env:
            release.git = faked
            try:
                rc = env.main()
            finally:
                release.git = real_git
            check(rc == 1 and not env.deployed and edition_on_origin() and env.escalated
                  and any(not ok and "届いたことを確かめられない" in m for ok, m in env.notes),
                  f"{what}のに、配信・edition の削除へ進んだ(か、当番を呼ばない): rc={rc!r} 配信={env.deployed} "
                  f"edition={edition_on_origin()} {env.notes}")
            rc = env.main()          # 取り直せる再実行は、届いたことを確かめて進む
            check(rc == 0 and env.deployed and not edition_on_origin()
                  and _git_out(bare, "rev-parse", "main") == _git_out(repo, "rev-parse", "main"),
                  f"{what}の後の再実行で発行を終えない: rc={rc!r} 配信={env.deployed} edition={edition_on_origin()}")


def test_release_squash_conflict(tmp: Path):
    """main と edition の同じファイルが衝突したら、作業ツリーを squash 前の main に戻して止まる(未解決の index と stage 済みの号を
    残さない)。edition の commit はそのまま。次の release は dirty で止まらない(監査指摘 ar7-2)。中断(Ctrl-C・systemctl stop の
    SIGTERM)でも同じに戻す。"""
    import signal
    repo, bare = _release_fixture(tmp / "cf", conflict=True)
    ed_before = _git_out(repo, "rev-parse", f"edition/{RELEASE_DAY}")
    with _ReleaseEnv(repo) as env:
        rc = env.main()
        status = _git_out(repo, "status", "--porcelain")
        check(rc == 1 and status == "" and _git_out(repo, "rev-parse", "main") == _git_out(bare, "rev-parse", "main")
              and _git_out(repo, "rev-parse", f"edition/{RELEASE_DAY}") == ed_before and not env.deployed,
              f"squash の衝突の後に作業ツリーが戻らない・edition が変わった: rc={rc!r} status={status!r}")
        check(any(not ok and "squash" in m and "衝突: x" in m for ok, m in env.notes) and env.escalated,
              f"squash の衝突を(衝突したファイルの名前つきで)知らせない・当番を呼ばない: {env.notes}")
        env.notes.clear()
        rc = env.main()
        check(rc == 1 and not any("未コミットの変更" in m for _, m in env.notes),
              f"衝突の後の再実行が dirty で止まる: {env.notes}")
    # 中断: commit の最中の Ctrl-C でも squash 前の main に戻し、中断はそのまま上げる
    repo, bare = _release_fixture(tmp / "intr")
    import release
    real_git = release.git

    def interrupted(*a, **k):
        if a and a[0] == "commit":
            raise KeyboardInterrupt("試験の中断")
        return real_git(*a, **k)
    with _ReleaseEnv(repo) as env:
        release.git = interrupted
        try:
            env.main()
            stopped = False
        except KeyboardInterrupt:
            stopped = True
        finally:
            release.git = real_git
        status = _git_out(repo, "status", "--porcelain")
        check(stopped and status == "" and _git_out(repo, "rev-parse", "main") == _git_out(bare, "rev-parse", "main"),
              f"commit の最中の中断で作業ツリーが戻らない: 中断={stopped} status={status!r}")
    # systemctl stop の SIGTERM も、squash の間は同じ後始末を通る例外にする(既定の SIGTERM は後始末なしに即死)
    import time
    stop = getattr(release, "stop_as_exception", None)
    got = None
    if stop:
        before = signal.getsignal(signal.SIGTERM)
        try:
            with stop():
                os.kill(os.getpid(), signal.SIGTERM)
                for _ in range(100):
                    time.sleep(0.01)
        except KeyboardInterrupt:
            got = signal.getsignal(signal.SIGTERM) == before
    check(got is True, "squash の間の SIGTERM が後始末を通る例外にならない(か、終わった後に元の扱いへ戻らない)")
    # 本物の SIGTERM: release を子プロセス(別のプロセス群)で走らせ、本物の git commit の最中に、systemctl stop と同じく
    # プロセス群ごと SIGTERM を送る(commit の pre-commit フックから1回だけ)。squash 前の main に戻り、再実行で発行できる(再監査)
    repo, bare = _release_fixture(tmp / "sigterm")
    marker = tmp / "sigterm" / "stop-once"
    marker.touch()
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nif [ -e '{marker}' ]; then rm -f '{marker}'; kill -TERM -$(ps -o pgid= $$ | tr -d ' '); sleep 5; fi\n"
                    "exit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    driver = tmp / "sigterm" / "drive.py"
    driver.write_text(
        "import subprocess, sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
        "import pipelib, release\n"
        "real_run = subprocess.run\n"
        "def run(cmd, *a, **k):\n"
        "    if len(cmd) > 1 and str(cmd[1]).endswith('lint.py'):\n"
        "        return subprocess.CompletedProcess(cmd, 0, 'lint: 0 errors\\n', '')\n"
        "    return real_run(cmd, *a, **k)\n"
        "subprocess.run = run\n"
        f"release.ROOT = __import__('pathlib').Path({str(repo)!r})\n"
        "release.notify = lambda msg, ok=True: print('NOTIFY', ok, msg, flush=True)\n"
        "release.review_verdict = lambda d: ('approve', '試験')\n"
        "release.deploy_site = lambda: '配信済み(試験)。'\n"
        "pipelib.job_lock = lambda *a, **k: None\n"
        "pipelib.dedupe_source_table = lambda *a, **k: []\n"
        "pipelib.escalate = lambda *a, **k: True\n"
        f"sys.argv = ['release.py', '--date', {RELEASE_DAY!r}]\n"
        "sys.exit(release.main())\n", encoding="utf-8")
    p = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True, timeout=120, start_new_session=True)
    status = _git_out(repo, "status", "--porcelain")
    check(not marker.exists() and p.returncode != 0 and status == ""
          and _git_out(repo, "rev-parse", "main") == _git_out(bare, "rev-parse", "main"),
          f"commit の最中の SIGTERM で作業ツリーが squash 前の main に戻らない: exit={p.returncode} status={status!r} "
          f"送った={not marker.exists()}")
    with _ReleaseEnv(repo) as env:
        rc = env.main()
    check(rc == 0 and env.deployed, f"SIGTERM で止まった後の再実行で発行できない: rc={rc!r} {env.notes}")


def test_release_dry_run_no_push(tmp: Path):
    """--dry-run は、edition に origin へ出していない commit があっても push しない・手元の commit を捨てない(監査指摘 ar7-5)。"""
    repo, bare = _release_fixture(tmp / "dry", ahead=True)
    refs = lambda: _git_out(bare, "for-each-ref", "--format=%(refname) %(objectname)")
    remote_before, local_before = refs(), _git_out(repo, "rev-parse", f"edition/{RELEASE_DAY}")
    with _ReleaseEnv(repo, dry=True) as env:
        rc = env.main()
    check(rc == 0 and refs() == remote_before, f"--dry-run が origin を変えた(push した): rc={rc!r}\n前 {remote_before}\n後 {refs()}")
    check(_git_out(repo, "rev-parse", f"edition/{RELEASE_DAY}") == local_before,
          "--dry-run が手元だけの commit を捨てた(origin へ引き戻した)")
    # origin だけが先(手元の edition が遅れている): 点検は origin の中身を見るが、手元のブランチ(edition・main)は動かさない(再監査)
    repo, bare = _release_fixture(tmp / "dry-behind")
    (repo / "later").write_text("origin にだけある commit\n", encoding="utf-8")
    for a in (["add", "-A"], ["commit", "-qm", "origin only"], ["push", "-q", "origin", f"edition/{RELEASE_DAY}"],
              ["reset", "-q", "--hard", "HEAD~1"]):
        subprocess.run(["git", *a], cwd=repo, capture_output=True, check=True)
    local = lambda: _git_out(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
    remote_before, local_before = refs(), local()
    tip, seen = _git_out(bare, "rev-parse", f"edition/{RELEASE_DAY}"), []
    with _ReleaseEnv(repo, dry=True) as env:
        # 点検(校閲の判定を読む時点)の中身が、発行なら出す origin の先端か(origin にだけある file が見えるか)
        env.r.review_verdict = lambda d: (seen.append((_git_out(repo, "rev-parse", "HEAD") == tip, (repo / "later").exists())),
                                          ("approve", "試験"))[1]
        rc = env.main()
    check(rc == 0 and refs() == remote_before and local() == local_before,
          f"--dry-run が手元のブランチを動かした(origin へ引き戻した)・origin を変えた: rc={rc!r}\n前 {local_before}\n後 {local()}")
    check(seen == [(True, True)], f"--dry-run が発行なら出す origin の中身でなく、遅れた手元の中身を点検した: {seen}")


def test_deploy_index_failure(tmp: Path):
    """current を切り替えた後に索引の更新が落ちたら、current を索引が取り込んだリリース(索引の run_id)へ戻してパージし、
    実態どおりに知らせる(監査指摘 ar7-4: 新しい静的と古い索引が残り、取り下げた記事が検索に残った)。前の配信が索引の状態を
    確かめられずに新しい版を残していても、戻す先は索引の記録で決める。記録が無ければ前の版へ戻し、揃っているとは言わない
    (再監査)。予算(--budget-sec)のうちに、戻す・知らせるまで終える(再監査: release の打ち切りを越えた)。
    本物の indexer を子プロセスで、本物の SQLite を別の接続で書き込みロックして落とす。ロックを外した再実行は新しい版を揃えて出す。"""
    import datetime
    import shutil
    import sqlite3
    import deploy
    import release
    here = Path(__file__).resolve().parent
    root = tmp / "root"
    (root / "scripts").mkdir(parents=True)
    for n in ("indexer.py", "tags.py"):
        shutil.copy(here / n, root / "scripts" / n)
    posts = root / "docs" / "_posts"
    posts.mkdir(parents=True)
    (root / "docs" / "_data").mkdir()
    (root / "docs" / "_data" / "tags.yml").write_text("alias: {}\ndrop: []\n", encoding="utf-8")
    d = "2026-10-13"
    for s in ("a", "b"):
        (posts / f"{d}-{s}.md").write_text(f"---\nslug: {s}\nedition: '{d}'\ntitle: 記事{s}\ntags: [t]\n---\n本文{s}\n", encoding="utf-8")
    srv = tmp / "srv"
    db = srv / "data" / "paper.db"
    real_run = subprocess.run
    subprocess.run(  # 前の版(リリース old)の索引(a・b)
        [sys.executable, str(root / "scripts" / "indexer.py"), "--db", str(db), "--run-id", "old", "--quiet"],
        check=True, capture_output=True)
    (posts / f"{d}-b.md").unlink()          # b を取り下げた版を配信する
    releases = srv / "releases"
    old = releases / "old"
    for s in ("a", "b"):
        (old / "articles" / f"{d}-{s}").mkdir(parents=True)
        (old / "articles" / f"{d}-{s}" / "index.html").write_text("前の版", encoding="utf-8")
    current = srv / "current"
    current.symlink_to(old)
    slow_build = []

    def fake_build(dest: Path, timeout=None):     # 今の記事から静的な記事ページを作る(slow_build があればその秒だけ掛ける)
        import time
        need = slow_build[-1] if slow_build else 0
        if timeout is not None and need > timeout:  # 本物の build と同じく、渡された上限で打ち切られる
            time.sleep(timeout)
            raise subprocess.TimeoutExpired("jekyll", timeout)
        time.sleep(need)
        for rel in deploy.REQUIRED + [f"articles/{p.stem}/index.html" for p in posts.glob("*.md")]:
            p = dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("新しい版", encoding="utf-8")

    def guarded_run(cmd, *a, **k):
        # 索引の子プロセスには試験の DB を必ず渡させる(渡さないと本番の既定の DB に書く)
        if len(cmd) > 1 and str(cmd[1]).endswith("indexer.py") and str(db) not in [str(c) for c in cmd]:
            raise AssertionError(f"indexer に試験の DB(--db)を渡していない: {cmd}")
        return real_run(cmd, *a, **k)
    def articles() -> list[str]:
        c = sqlite3.connect(db)
        try:
            return sorted(r[0] for r in c.execute("SELECT slug FROM articles"))
        finally:
            c.close()
    notes, purges = [], []
    names = [f"20261014T{6 + m:02d}0000" for m in range(12)]       # 配信ごとのリリースの名前(時刻順)
    stamps = iter(names)
    saved = {k: getattr(deploy, k) for k in ("ROOT", "SRV", "RELEASES", "CURRENT", "build", "purge_cache", "notify", "sync_app",
                                             "now_jst", "INDEX_ATTEMPTS", "INDEX_RETRY_SEC", "reindex", "indexed_run",
                                             "INDEX_TIMEOUT_SEC", "INDEX_MIN_SEC", "RESERVE_SEC") if hasattr(deploy, k)}
    saved_db, saved_argv = getattr(deploy, "DB", None), sys.argv
    lock = sqlite3.connect(db)
    try:
        deploy.ROOT, deploy.SRV, deploy.RELEASES, deploy.CURRENT = root, srv, releases, current
        deploy.DB = db
        deploy.build, deploy.sync_app = fake_build, (lambda: False)
        deploy.purge_cache = lambda: purges.append(True) or "パージ(試験)"
        deploy.notify = lambda job, msg, ok=True, **k: notes.append((ok, msg)) or True
        deploy.now_jst = lambda: datetime.datetime.strptime(next(stamps), "%Y%m%dT%H%M%S")
        deploy.INDEX_ATTEMPTS, deploy.INDEX_RETRY_SEC = 1, 0      # SQLite の待ち(5秒)を1回だけ
        sys.argv = ["deploy.py"]
        subprocess.run = guarded_run
        def run_main():
            try:
                return deploy.main()
            except (Exception, SystemExit) as e:       # noqa: BLE001  直す前の実装は索引の失敗を例外で上げる
                return f"例外: {type(e).__name__}: {str(e)[:200]}"
        cur = lambda: current.resolve().name
        state = lambda: getattr(deploy, "STATE", "")
        # S1 本物の書き込みロック: 索引が取り込んだ old へ戻す(静的 a・b / 索引 a・b で揃う)
        lock.execute("BEGIN IMMEDIATE")                           # 別の接続が書き込み中
        rc = run_main()
        lock.rollback()
        check(rc == 1 and cur() == "old" and articles() == ["a", "b"] and purges,
              f"索引の失敗で current を索引の版へ戻さない・パージしない(静的と索引が食い違う): rc={rc!r} current={cur()} "
              f"索引={articles()} パージ={purges}")
        check("索引が取り込んだ版(old)に戻した" in state() and any(not ok and "戻した" in m for ok, m in notes),
              f"索引の失敗の後の公開の状態を実態どおりに言わない: {state()!r} {notes}")
        # S2 ロックが外れた再実行: 新しい版を揃えて出す
        notes.clear()
        purges.clear()
        rc = run_main()
        check(rc == 0 and cur() == names[1] and articles() == ["a"] and purges and "新しい版" in state(),
              f"ロックが外れた再実行で新しい版を揃えて出さない: rc={rc!r} current={cur()} 索引={articles()}")
        # S3 索引の状態が読めない失敗: 新しい版(記事 c を足した版)を残し、確かめられないと言う(戻さない)
        (posts / f"{d}-c.md").write_text(f"---\nslug: c\nedition: '{d}'\ntitle: 記事c\ntags: [t]\n---\n本文c\n", encoding="utf-8")
        real_reindex, real_indexed_run = deploy.reindex, getattr(deploy, "indexed_run", None)

        def broken(run_id, timeout=None):
            raise RuntimeError("database is locked(試験)")

        def unreadable():
            raise sqlite3.OperationalError("unable to open database file(試験)")
        deploy.reindex, deploy.indexed_run = broken, unreadable
        rc = run_main()
        deploy.indexed_run = real_indexed_run
        check(rc == 1 and cur() == names[2] and "確かめられない" in state() and "静的と索引が揃っている" not in state(),
              f"索引の状態が読めないのに戻した・揃っていると言った: rc={rc!r} current={cur()} {state()!r}")
        # S4 続けて失敗: 戻す先は直前の current(names[2]。索引と食い違う)でなく、索引が取り込んだ names[1]
        rc = run_main()
        check(rc == 1 and cur() == names[1] and not (current / "articles" / f"{d}-c").exists() and articles() == ["a"]
              and f"索引が取り込んだ版({names[1]})に戻した(静的と索引が揃っている)" in state(),
              f"索引が取り込んでいない版へ戻して「揃っている」と言った: rc={rc!r} current={cur()} 索引={articles()} {state()!r}")
        # S5 取り込みの記録が無い索引(この直しより前の索引): 前の版へ戻すが、揃っているとは言わない
        c = sqlite3.connect(db)
        c.execute("DELETE FROM meta WHERE key = 'run_id'")
        c.commit()
        c.close()
        rc = run_main()
        check(rc == 1 and cur() == names[1] and "確かめられない" in state() and "静的と索引が揃っている" not in state(),
              f"取り込みの記録が無いのに揃っていると言った: rc={rc!r} current={cur()} {state()!r}")
        (posts / f"{d}-c.md").unlink()
        # S6 1回目だけ落ちる索引は、戻さずに再試行で通す
        calls = []

        def flaky(run_id, timeout=None):
            calls.append(run_id)
            if len(calls) == 1:
                raise RuntimeError("database is locked(試験)")
            return real_reindex(run_id)
        deploy.reindex, deploy.INDEX_ATTEMPTS = flaky, 2
        rc = run_main()
        check(rc == 0 and calls == [names[5]] * 2 and cur() == names[5],
              f"索引の一時的な失敗を再試行しない(か、通ったのに戻した): rc={rc!r} {calls} current={cur()}")
        # S7 取引は通った(索引はこの版まで進んだ)が indexer が後で落ちた: 静的と索引は揃っているので戻さない
        def late(run_id, timeout=None):
            real_reindex(run_id)
            raise RuntimeError("後始末で落ちた(試験)")
        deploy.reindex = late
        rc = run_main()
        c = sqlite3.connect(db)
        ran = (c.execute("SELECT value FROM meta WHERE key = 'run_id'").fetchone() or [None])[0]
        c.close()
        check(rc == 0 and cur() == names[6] and ran == names[6],
              f"索引が新しい版まで進んだのに current を戻した: rc={rc!r} current={cur()}")
        # S8 予算: ビルドが長引き、索引が毎回時間切れまで掛かっても、予算のうちに戻す・知らせるまで終える
        # (秒を縮めて試す。予算 3秒・後始末に残す 1秒・ビルド 0.5秒)
        import time

        def hang(run_id, timeout=None):
            time.sleep(timeout if timeout is not None else 60)
            raise subprocess.TimeoutExpired("indexer.py", timeout)
        deploy.reindex, deploy.INDEX_ATTEMPTS, deploy.INDEX_RETRY_SEC = hang, 3, 0.3
        deploy.INDEX_TIMEOUT_SEC, deploy.INDEX_MIN_SEC, deploy.RESERVE_SEC = 10, 0.2, 1.0
        slow_build.append(0.5)
        sys.argv = ["deploy.py", "--budget-sec", "3"]
        t0 = time.time()
        rc = run_main()
        took = time.time() - t0
        check(rc == 1 and took < 3.0 and cur() == names[6] and "戻した" in state(),
              f"予算のうちに戻す・知らせるまで終えない: rc={rc!r} {took:.1f}秒 current={cur()} {state()!r}")
        # S9 ビルドが予算を食い尽くす: 渡した上限で打ち切り、current は差し替えない(予算 2秒・ビルド 3秒掛かる)
        slow_build.append(3.0)
        sys.argv = ["deploy.py", "--budget-sec", "2"]
        t0 = time.time()
        rc = run_main()
        took = time.time() - t0
        check(rc == 1 and took < 2.0 and cur() == names[6] and "前の版のまま" in state(),
              f"ビルドを予算の残りで打ち切らない(か、差し替えた): rc={rc!r} {took:.1f}秒 current={cur()} {state()!r}")
        slow_build.clear()
        sys.argv = ["deploy.py"]
        # S10 索引の状態が読めずに新しい版(b2 を足した版)が残る → 手で索引を直す(--run-id なし) → 次の配信も失敗:
        # 手での更新は前の取り込みの記録を消すので、古い記録のリリース(names[6])へ戻して「揃っている」と言わない(再監査)
        (posts / f"{d}-b2.md").write_text(f"---\nslug: b2\nedition: '{d}'\ntitle: 記事b2\ntags: [t]\n---\n本文b2\n", encoding="utf-8")
        deploy.reindex, deploy.INDEX_ATTEMPTS, deploy.indexed_run = broken, 1, unreadable
        rc = run_main()
        deploy.indexed_run = real_indexed_run
        live = cur()
        subprocess.run([sys.executable, str(root / "scripts" / "indexer.py"), "--db", str(db), "--quiet"],
                       check=True, capture_output=True)
        rc2 = run_main()
        check(rc == 1 and live == names[9] and rc2 == 1 and cur() == names[9] and articles() == ["a", "b2"]
              and "静的と索引が揃っている" not in state() and "確かめられない" in state(),
              f"手で直した索引の後の失敗で、古い取り込みの記録のリリースへ戻した(か、揃っていると言った): rc={rc!r}/{rc2!r} "
              f"current={cur()} 索引={articles()} {state()!r}")
    finally:
        subprocess.run = real_run
        lock.close()
        for k, v in saved.items():
            setattr(deploy, k, v)
        deploy.DB, sys.argv = saved_db, saved_argv
    # release の通知は deploy が出す「配信の状態」をそのまま載せる(「current は前号のまま」と決めつけない)。時間切れも止めない
    sent, saved_run, saved_notify, asked = [], subprocess.run, release.notify, []
    try:
        release.notify = lambda msg, ok=True: sent.append(msg)
        subprocess.run = lambda cmd, *a, **k: asked.append((cmd, k.get("timeout"))) or subprocess.CompletedProcess(
            cmd, 1, "検証OK\n" + getattr(release, "DEPLOY_STATE_MARK", "配信の状態: ") + "current は新しい版(X)。索引はまだ前の版\n", "boom")
        release.deploy_site()

        def slow(cmd, *a, **k):
            raise subprocess.TimeoutExpired(cmd, 2400)
        subprocess.run = slow
        try:
            out = release.deploy_site()
        except subprocess.TimeoutExpired:
            out = "例外"
    finally:
        subprocess.run, release.notify = saved_run, saved_notify
    check(len(sent) == 2 and "current は新しい版(X)" in sent[0] and "前号のまま" not in sent[0] and out != "例外",
          f"配信の失敗の通知が deploy の状態を載せない・時間切れで止まる: {sent} {out}")
    check(getattr(release, "DEPLOY_STATE_MARK", None) == getattr(deploy, "STATE_MARK", ""), "release と deploy の「配信の状態」の印が違う")
    # release は deploy に、自分が打ち切る時間より短い予算を渡す(打ち切られる前に戻す・知らせるまで終えさせる)
    cmd, timeout = asked[0] if asked else ([], None)
    budget = float(cmd[cmd.index("--budget-sec") + 1]) if "--budget-sec" in cmd else None
    check(budget is not None and timeout is not None and budget < timeout,
          f"release が deploy に打ち切りより短い予算を渡さない: {cmd} timeout={timeout}")


def test_correction_append_only(tmp: Path):
    """訂正(第1号以降)は訂正の記録の末尾への追記だけ。既にある記録の書き換え・削除・並べ替えは、件数が増えていても lint が止める
    (監査指摘 ar7-3)。判定の関数と、一時の Git で走らせた本物の lint の両方で検める。"""
    import contextlib
    import io
    import shutil
    first = {"at": "2026-09-16T10:00:00+09:00", "by": "編集長", "initial": "初出の表記", "corrected_to": "正しい表記"}
    second = {"at": "2026-10-14T10:00:00+09:00", "by": "編集長", "initial": "a", "corrected_to": "b"}
    # 本物の lint(append-only の経路)
    d = "2026-09-15"
    root = tmp / "lintroot"
    posts = root / "docs" / "_posts"
    posts.mkdir(parents=True)
    shutil.copytree(Path(__file__).resolve().parent.parent / "schema", root / "schema")
    p = posts / f"{d}-k.md"
    write = lambda corr, body: p.write_text("---\n" + article_fm("k", d, corrected=True, corrections=corr) + "---\n" + body, encoding="utf-8")
    write([first], "本文。\n")
    for a in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
              ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(["git", *a], cwd=root, capture_output=True, check=True)
    keys = ("ROOT", "DOCS", "POSTS", "EDITIONS", "EDITORIALS", "CANDIDATES", "IDOLS", "WITHDRAWN", "REWRITES")
    saved, saved_argv = {k: getattr(lint, k) for k in keys}, sys.argv

    def run_lint() -> list[str]:
        out = io.StringIO()
        sys.argv = ["lint.py", "--base", "HEAD", "--no-net"]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            lint.main()
        return [l for l in out.getvalue().splitlines() if l.startswith(f"::error file=docs/_posts/{d}-k.md::append-only")]
    try:
        lint.ROOT = root
        lint.DOCS = root / "docs"
        lint.POSTS, lint.EDITIONS, lint.EDITORIALS = posts, root / "docs" / "_editions", root / "docs" / "_editorials"
        lint.CANDIDATES, lint.IDOLS = root / "candidates", root / "docs" / "_data" / "idols.json"
        lint.WITHDRAWN, lint.REWRITES = root / "withdrawn.yml", root / "rewrites.yml"
        write([first, second], "本文(訂正した)。\n")
        ok_lines = run_lint()
        write([{**first, "initial": "書き換え"}, second], "本文(訂正した)。\n")
        bad_lines = run_lint()
    finally:
        for k, v in saved.items():
            setattr(lint, k, v)
        sys.argv = saved_argv
    check(ok_lines == [], f"本物の lint が正しい訂正の追記を止めた: {ok_lines}")
    check(bad_lines != [], "本物の lint が既にある訂正の記録の書き換え(と1件の追記)を通した")
    # 判定の関数: 削除・並べ替え・追記なし・枠の変更も止める
    old = {"slug": "k", "edition": d, "brand": "cg", "rank": "small", "corrected": True, "corrections": [first]}
    judge = getattr(lint, "correction_change_errors", None)
    errs = (lambda new: judge(old, {**old, **new})) if judge else (lambda new: [])
    check(judge is not None and errs({"corrections": [first, second]}) == [], "正しい訂正の追記を止めた")
    for corr, what in (([{**first, "initial": "書き換え"}, second], "既にある記録の書き換え"),
                       ([second, second], "既にある記録の削除(件数は増えている)"),
                       ([second, first], "並べ替え"),
                       ([first], "追記なし")):
        check(errs({"corrections": corr}) != [], f"訂正で{what}を通した")
    check(errs({"corrections": [first, second], "rank": "lead"}) != [], "訂正で rank を変えられた")


def test_indexer_tag_dictionary(tmp: Path):
    """タグ辞書(alias・drop)だけを直しても、本文の変わらない記事のタグ・全文検索のタグを付け直す。全件の作り直しと差分の更新は
    同じ索引になる。辞書も記事も変わらなければ入れ直さない(監査指摘 ar7-6)。"""
    import indexer
    root = tmp / "ix"
    posts = root / "docs" / "_posts"
    posts.mkdir(parents=True)
    (posts / "2026-10-14-a.md").write_text("---\nslug: a\nedition: '2026-10-14'\ntitle: 記事\ntags: [旧タグ, 残す]\n---\n本文\n",
                                          encoding="utf-8")
    saved = (indexer.ROOT, indexer.DOCS)
    plain = {"alias": {}, "drop": set()}
    fixed = {"alias": {"旧タグ": "正規タグ"}, "drop": {"残す"}}
    try:
        indexer.ROOT, indexer.DOCS = root, root / "docs"
        conn = indexer.connect(root / "diff.db")
        with conn:
            indexer.index_articles(conn, plain, False)
        with conn:
            again = indexer.index_articles(conn, plain, False)
        with conn:
            indexer.index_articles(conn, fixed, False)
        got = ([r[0] for r in conn.execute("SELECT tag FROM article_tags")], [r[0] for r in conn.execute("SELECT tags FROM search")])
        conn.close()
        full = indexer.connect(root / "full.db")
        with full:
            indexer.index_articles(full, fixed, True)
        want = ([r[0] for r in full.execute("SELECT tag FROM article_tags")], [r[0] for r in full.execute("SELECT tags FROM search")])
        full.close()
    finally:
        indexer.ROOT, indexer.DOCS = saved
    check(again == (0, 0), f"辞書も記事も変わらないのに入れ直した: {again}")
    check(got == want == (["正規タグ"], ["正規タグ"]), f"タグ辞書だけを直したとき差分の索引に反映しない: 差分 {got} / 全件 {want}")


def test_shared_service_units(tmp: Path):
    """共有サービス(短縮 URL・フォーム・メールフォーム)はドメイン全体では判定しない(監査指摘 2026-10-14: t.co と
    forms.cloud.microsoft が丸ごと公式に登録され、どこへの短縮も・文書 ID の違うどのフォームも公式になった)。
    短縮は展開先の種別を短縮1つずつへ写し(合議には掛けない)、フォームは文書 ID ごとに判定する。"""
    import classify_sources as cs
    import email.message
    import io
    import urllib.error
    import urllib.request
    import yaml
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "candidates").mkdir(exist_ok=True)
    (tmp / "docs" / "_posts").mkdir(parents=True, exist_ok=True)
    table = tmp / "source_types.yml"
    short, dest = "https://t.co/YknRoOJtym", "https://asobistage.asobistore.jp/event/valiv_bd2026_cosmo/ticket"
    form1 = "https://forms.cloud.microsoft/pages/responsepage.aspx?id=officialDocument0123456789&route=shorturl"
    form2 = "https://forms.cloud.microsoft/pages/responsepage.aspx?id=unrelatedDocument0123456789"
    # 撤去する前の表の形(ドメイン全体の登録)。union merge で行が戻ってきても、判定には使わない
    table.write_text("official_domains:\n  - t.co\n  - forms.cloud.microsoft\nsemi_official_domains:\n  - asobistage.asobistore.jp\n"
                     "  - krs.bz\nparty_domains:\n  - bit.ly\n", encoding="utf-8")
    saved = (cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY, getattr(cs, "expand_short_fetch", None), cs.ask, cs.site_profile,
             cs.POSTS_ONLY, cs.UNRESOLVED, sys.argv)
    try:
        cs.ROOT = pipelib.ROOT = tmp
        pipelib._ST_TABLE = pipelib._ST_KEY = None
        getattr(cs, "_SHORT", {}).clear()
        check(pipelib.classify_source(short) == "未確認" and pipelib.classify_source("https://bit.ly/abc") == "未確認",
              f"短縮 URL がドメインの登録で種別を持った: {pipelib.classify_source(short)}")
        check(pipelib.classify_source(form2) == "未確認", "文書 ID の違うフォームがドメインの登録で公式になった")
        check(pipelib.classify_source("https://krs.bz/other/m/form") == "未確認", "別の会社のメールフォームがドメインの登録で準公式になった")
        # 判定の単位: 短縮は1つずつ、フォームは文書 ID まで(id 以外のクエリは落とす)、メールフォームはアカウント
        check(cs.platform_unit(short) == ("short", "t.co/YknRoOJtym"), f"短縮の単位: {cs.platform_unit(short)}")
        k1, k2 = cs.platform_unit(form1)[1], cs.platform_unit(form2)[1]
        check(k1 == "forms.cloud.microsoft/pages/responsepage.aspx?id=officialDocument0123456789" and k1 != k2 and cs.is_document(k1),
              f"フォームの単位: {k1} / {k2}")
        check(cs.platform_unit("https://forms.cloud.microsoft/pages/responsepage.aspx")[0] == "skip", "文書 ID の無いフォームを判定の対象にした")
        check(cs._key_forms(k1)[0] != cs._key_forms(k2)[0], "合議の答えの対応付けで、別のフォームが同じ対象になる")
        check(cs.platform_unit("https://krs.bz/columbia/m/morform") == ("path", "krs.bz/columbia"), "メールフォームの単位はアカウント")
        # 合議がドメインと答えても、共有サービスはドメインとしては足さない
        before = table.read_text(encoding="utf-8")
        cs.add_domains({"t.co": ("公式", "x"), "forms.cloud.microsoft": ("公式", "y"), "krs.bz": ("準公式", "z")})
        check(yaml.safe_load(table.read_text(encoding="utf-8")) == yaml.safe_load(before), "共有サービスをドメインとして表に足した")
        # フォームは、文書のキーで登録したものにだけ当たる
        cs.add_paths({k1: ("公式", "リンク元が公式")})
        check(pipelib.classify_source(form1) == "公式" and pipelib.classify_source(form2) == "未確認", "フォームが文書 ID ごとに決まらない")
        # 短縮: 候補の短縮 URL の展開先を判定の対象にし、決まった種別を短縮1つずつへ写す。短縮そのものは合議に掛けない
        unknown = "https://t.co/Unknown0001"
        (tmp / "candidates" / "2026-10-14.json").write_text(json.dumps([
            {"url": short, "title": "誕生日ライブのチケット"}, {"url": unknown, "title": "コラボ商品"},
            {"url": "https://t.co/Broken0001", "title": "展開できない"}], ensure_ascii=False), encoding="utf-8")
        fetched, asked = [], []
        dests = {short: dest, unknown: "https://shop.unknown.example/item/1"}
        cs.expand_short_fetch = lambda u, hops=5: (fetched.append(u), dests.get(u, ""))[1]
        cs.ask = lambda cmd, prompt, timeout=900: (asked.append(prompt), [
            {"host": "shop.unknown.example", "operator": "雑貨店", "type": "当事者", "why": "コラボ商品の販売元"}])[1]
        cs.site_profile = lambda *a, **k: "材料"
        cs.POSTS_ONLY, cs.UNRESOLVED = None, tmp / "metrics" / "classify-unresolved.json"
        sys.argv = ["classify_sources.py", "--date", "2026-10-14", "--apply"]
        code = cs.main()
        pipelib._ST_TABLE = None
        check(code == 0 and pipelib.classify_source(short) == "準公式", f"短縮へ展開先の種別が写らない: {pipelib.classify_source(short)}")
        check(pipelib.classify_source(unknown) == "当事者" and pipelib.classify_source("https://shop.unknown.example/") == "当事者",
              "展開先が表に無い短縮: 展開先を合議で決めてから短縮へ写していない")
        check(len(asked) == 2 and all("### shop.unknown.example" in p and "### t.co" not in p for p in asked),
              f"合議の対象が展開先でない: {[p[-200:] for p in asked]}")
        row = next((ln for ln in table.read_text(encoding="utf-8").splitlines() if "t.co/YknRoOJtym" in ln), "")
        check("機械で追加" in row and dest in row, f"短縮の行に決め方と展開先が無い: {row}")
        check(fetched.count(short) == 1, f"同じ短縮を何度も展開した: {fetched}")
        rec = cs.load_unresolved()
        check("展開先を引けない" in rec.get(cs.display_base("https://t.co/Broken0001"), ""), f"展開できない短縮の理由が watch のキーで残らない: {rec}")
        # lint: 展開先が準公式の短縮を公式と書いた記事は、過大表示で止まる
        rep = lint.Report()
        lint.check_source_types(rep, lint.ROOT / "x.md", {"sources": [{"url": short, "type": "公式", "label": "l"}], "src": "公式"})
        check(rep.errors == 2, f"展開先が準公式の短縮を公式と書いた記事が lint を通った: {rep.errors}")
    except (Exception, SystemExit) as e:      # noqa: BLE001  直す前の実装(関数が無い・表の検査で止まる等)でも、落ちたことを記録して先へ進む
        check(False, f"共有サービスの判定のテストが例外: {type(e).__name__}: {e}")
    finally:
        (cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY, cs.expand_short_fetch, cs.ask, cs.site_profile,
         cs.POSTS_ONLY, cs.UNRESOLVED, sys.argv) = saved
    # 展開: リダイレクト(Location)とページで飛ばす形(meta refresh)を、短縮サービスの外へ出るまで辿る。展開先そのものは取りに行かない
    opened = []

    class Opener:
        def open(self, req, timeout=None):
            opened.append(req.full_url)
            if req.full_url == "https://t.co/a1":
                h = email.message.Message()
                h["Location"] = "https://bit.ly/b2"
                raise urllib.error.HTTPError(req.full_url, 301, "Moved", h, None)
            if req.full_url == "https://bit.ly/b2":
                return io.BytesIO(b'<meta http-equiv="refresh" content="0;URL=https://example.jp/page?x=1&amp;y=2">')
            raise AssertionError(f"展開先まで取りに行った: {req.full_url}")
    saved_bo = urllib.request.build_opener
    try:
        urllib.request.build_opener = lambda *h: Opener()
        got = cs.expand_short_fetch("https://t.co/a1")
        check(got == "https://example.jp/page?x=1&y=2" and opened == ["https://t.co/a1", "https://bit.ly/b2"], f"短縮の展開: {got} {opened}")
    except (Exception, SystemExit) as e:      # noqa: BLE001
        check(False, f"短縮の展開のテストが例外: {type(e).__name__}: {e}")
    finally:
        urllib.request.build_opener = saved_bo
    # 本番の判定表: ドメインの一覧に、ドメイン単位で決まらないホスト(短縮・フォーム・プラットフォーム)が載っていない
    real = pipelib.parse_source_table((Path(__file__).resolve().parent.parent / "source_types.yml").read_text(encoding="utf-8"), "source_types.yml")
    bad = [f"{k}: {h}" for k, v in real.items() if k.endswith("_domains") for h in v or []
           if cs.platform_unit(f"https://{h}/") != ("domain", h)]
    check(not bad, f"判定表にドメイン単位で決まらないホストがドメインとして載っている: {bad}")


def test_document_id_case(tmp: Path):
    """文書・短縮・動画の ID は大小文字を区別する(監査指摘 2026-10-14: Microsoft Forms の文書 ID を大小文字で同一視し、合議が
    `abCd…` を公式と答えたのに、別の文書 `AbCd…` に公式が保存された)。合議の対応付け・表への追加・表の検査・merge 後の二重行の除去の
    どれも同じ比べ方(pipelib.path_type_norm)。"""
    import classify_sources as cs
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "candidates").mkdir(exist_ok=True)
    (tmp / "docs" / "_posts").mkdir(parents=True, exist_ok=True)
    f1 = "https://forms.cloud.microsoft/pages/responsepage.aspx?id=AbCdEfGhIjKlMnOpQr12&route=shorturl"
    f2 = "https://forms.cloud.microsoft/pages/responsepage.aspx?id=abCdEfGhIjKlMnOpQr12"
    g1 = "https://docs.google.com/forms/d/e/1FAIpQLSAbCdEfGhIjKlMnOpQrStUvWx/viewform"
    g2 = "https://docs.google.com/forms/d/e/1FAIpQLSabCdEfGhIjKlMnOpQrStUvWx/viewform"
    table = tmp / "source_types.yml"
    table.write_text("official_domains:\n  - a.example\nvideo_ids:\n  公式:\n    - AAAAAAAAAAA\n", encoding="utf-8")
    saved = (cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY, cs.ask, cs.site_profile, cs.POSTS_ONLY, cs.UNRESOLVED, sys.argv)
    try:
        cs.ROOT = pipelib.ROOT = tmp
        pipelib._ST_TABLE = pipelib._ST_KEY = None
        k1, k2 = cs.platform_unit(f1)[1], cs.platform_unit(f2)[1]
        check(k1 != k2 and cs._key_forms(k1)[0] != cs._key_forms(k2)[0] and cs._key_forms(cs.platform_unit(g1)[1])[0] != cs._key_forms(cs.platform_unit(g2)[1])[0],
              f"大小文字だけ違う文書が合議の同じ対象になる: {cs._key_forms(k1)} {cs._key_forms(k2)}")
        # 経路: 候補の大小文字だけ違うフォーム(文書 ID のクエリの形と、短縮の /r/<ID> の形)→ 合議(両モデルが後者だけを公式と
        # 答える)→ 表。前者は未確認のまま
        r1, r2 = "https://forms.office.com/r/AbCdEfGh12", "https://forms.office.com/r/abCdEfGh12"
        kr1, kr2 = cs.platform_unit(r1)[1], cs.platform_unit(r2)[1]
        check(kr1 == "forms.office.com/r/AbCdEfGh12" and cs._key_forms(kr1)[0] != cs._key_forms(kr2)[0], f"短縮の形のフォームの単位: {kr1} {kr2}")
        (tmp / "candidates" / "2026-10-14.json").write_text(json.dumps([{"url": u, "title": u[-4:]} for u in (f1, f2, r1, r2)]), encoding="utf-8")
        cs.ask = lambda cmd, prompt, timeout=900: [{"host": k, "operator": "公式", "type": "公式", "why": "リンク元が公式"} for k in (k2, kr2)]
        cs.site_profile = lambda *a, **k: "材料"
        cs.POSTS_ONLY, cs.UNRESOLVED = None, tmp / "metrics" / "classify-unresolved.json"
        sys.argv = ["classify_sources.py", "--date", "2026-10-14", "--apply"]
        cs.main()
        pipelib._ST_TABLE = None
        got = [pipelib.classify_source(u) for u in (f1, f2, r1, r2)]
        check(got == ["未確認", "公式", "未確認", "公式"], f"合議が答えた文書と違う文書に公式が付いた: {got}")
        # 大小文字だけ違う文書を別々に足せる(表の検査で重複にしない)。Google の文書も同じ
        cs.add_paths({k1: ("ファン", "x"), cs.platform_unit(g1)[1]: ("公式", "y"), cs.platform_unit(g2)[1]: ("ファン", "z")})
        pipelib._ST_TABLE = None
        check(pipelib.classify_source(f1) == "ファン" and pipelib.classify_source(f2) == "公式"
              and pipelib.classify_source(g1) == "公式" and pipelib.classify_source(g2) == "ファン", "大小文字だけ違う文書を別々に判定できない")
        # merge 後の二重行の除去も、大小文字だけ違う文書・動画 ID を消さない(同じ行は消す)
        text = table.read_text(encoding="utf-8").replace("    - AAAAAAAAAAA\n", "    - AAAAAAAAAAA\n    - aaaaaaaaaaa\n    - AAAAAAAAAAA\n")
        table.write_text(text, encoding="utf-8")
        removed = pipelib.dedupe_source_table(table)
        t = pipelib.parse_source_table(table.read_text(encoding="utf-8"), table)
        check(removed == ["    - AAAAAAAAAAA"] and t["video_ids"]["公式"] == ["AAAAAAAAAAA", "aaaaaaaaaaa"]
              and k1 in t["path_types"] and k2 in t["path_types"], f"二重行の除去が別の ID を消した: {removed} {t['video_ids']}")
    except (Exception, SystemExit) as e:      # noqa: BLE001
        check(False, f"文書 ID の大小文字のテストが例外: {type(e).__name__}: {e}")
    finally:
        cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY, cs.ask, cs.site_profile, cs.POSTS_ONLY, cs.UNRESOLVED, sys.argv = saved


def test_performer_domains(tmp: Path):
    """合議が答えてよい種別(ALLOWED)は、どれもドメインとして保存でき、判定と表の検査が同じ種別で読み戻す(監査指摘 2026-10-14:
    依頼文どおり所属事務所のサイトを演者と答えて2モデルが一致しても、保存先が無く KeyError('演者') で取引ごと落ちた)。"""
    import classify_sources as cs
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / "source_types.yml"
    p.write_text("official_domains:\n  - a.example\n", encoding="utf-8")      # 演者などの一覧がまだ無い表
    saved = (cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY, cs.ask)
    try:
        cs.ROOT = pipelib.ROOT = tmp
        pipelib._ST_TABLE = pipelib._ST_KEY = None
        check(set(cs.LIST_OF) == set(cs.ALLOWED), f"合議の種別とドメインの一覧が対応していない: {set(cs.ALLOWED) ^ set(cs.LIST_OF)}")
        check("所属事務所" in (pipelib.PROMPTS / "classify-rules.md").read_text(encoding="utf-8"), "依頼文の演者の定義が変わった(テストの前提)")
        answer = [{"host": "actor-agency.example", "operator": "声優事務所", "type": "演者", "why": "出演者の所属事務所"}]
        cs.ask = lambda *a, **k: answer
        agreed, split = cs.consensus("p", ["actor-agency.example"])
        check(agreed.get("actor-agency.example", ("",))[0] == "演者" and not split, f"演者の答えが一致しない: {agreed} {split}")
        try:
            cs.add_domains(agreed)
            cs.add_domains({f"{t}-{i}.example": (t, "w") for i, t in enumerate(cs.ALLOWED)})
        except (Exception, SystemExit) as e:      # noqa: BLE001
            check(False, f"合議で一致した種別を保存できない: {type(e).__name__}: {e}")
        pipelib._ST_TABLE = None
        t = pipelib.parse_source_table(p.read_text(encoding="utf-8"), p)
        check("actor-agency.example" in (t.get("performer_domains") or []), f"演者のドメインの保存先: {sorted(t)}")
        for i, typ in enumerate(cs.ALLOWED):
            got = pipelib.classify_source(f"https://{typ}-{i}.example/news")
            check(got == typ, f"{typ} として保存したドメインの判定: {got}")
        check(pipelib.classify_source("https://www.actor-agency.example/profile") == "演者"
              and pipelib.classify_source("https://fc.actor-agency.example/") == "演者", "演者のドメインの配下が演者にならない")
        rep = lint.Report()
        lint.check_source_types(rep, lint.ROOT / "x.md", {"sources": [{"url": "https://actor-agency.example/n", "type": "演者", "label": "l"}], "src": "演者"})
        check(rep.errors == 0, f"演者のドメインを演者と書いた記事が lint で止まる: {rep.errors}")
        # 表の検査: 同じドメインを演者と別の種別に載せたら止める
        try:
            pipelib.parse_source_table("performer_domains:\n  - x.example\nparty_domains:\n  - x.example\n", p)
            check(False, "演者と当事者に同じドメインが載った表を通した")
        except SystemExit as e:
            check("performer_domains" in str(e), f"重複の理由: {e}")
    except (Exception, SystemExit) as e:      # noqa: BLE001
        check(False, f"演者のドメインのテストが例外: {type(e).__name__}: {e}")
    finally:
        cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY, cs.ask = saved


def test_platform_owner_units(tmp: Path):
    """プラットフォーム上の持ち主の単位: ニコニコの `/channel/<ch ID>` は ID まで(監査指摘 2026-10-14: 別のチャンネルが同じ
    `ch.nicovideo.jp/channel` になり、1つを公式にすると別のチャンネルとその番組まで公式になった)。同じ型として、先頭の区切りが
    持ち主でない投稿の URL(instagram.com/p/<ID>)を1つの主体にしない。"""
    import classify_sources as cs
    import html as _html
    one, two = "https://ch.nicovideo.jp/channel/ch1111111", "https://ch.nicovideo.jp/channel/ch2222222"
    k1 = "ch.nicovideo.jp/channel/ch1111111"
    check(cs.path_key(one) == k1 and cs.path_key(two) == "ch.nicovideo.jp/channel/ch2222222", f"チャンネルの単位: {cs.path_key(one)} {cs.path_key(two)}")
    check(cs.path_key(one + "/video?page=2") == k1 and cs.path_key("https://ch.nicovideo.jp/sidem/video") == "ch.nicovideo.jp/sidem"
          and cs.path_key("https://ch.nicovideo.jp/channel") is None, "チャンネルの単位(配下・別名・ID 無し)")
    check(cs.display_base(one) == "https://" + k1, f"表示の単位: {cs.display_base(one)}")
    props = {"socialGroup": {"type": "channel", "id": "ch1111111", "socialGroupPageUrl": one}}
    page = f'<title>番組 - ニコニコ生放送</title><script id="embedded-data" data-props="{_html.escape(json.dumps(props))}"></script>'
    check(cs.nico_channel_fetch("lv1", page=page, fetch_alias=False)[0] == k1, "番組から引いたチャンネルとチャンネルの URL の単位が違う")
    check(cs.path_key("https://www.instagram.com/p/AbCdEf/") is None and cs.platform_unit("https://www.instagram.com/p/AbCdEf/")[0] == "parser"
          and cs.path_key("https://www.instagram.com/someone/") == "instagram.com/someone", "Instagram の投稿を1つの主体にした")
    # 経路: 候補のチャンネル URL → 合議の対象 → 表。別のチャンネルとその番組は公式にならない
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "candidates").mkdir(exist_ok=True)
    (tmp / "docs" / "_posts").mkdir(parents=True, exist_ok=True)
    (tmp / "source_types.yml").write_text("official_domains:\n  - a.example\n", encoding="utf-8")
    saved = (cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY, cs.POSTS_ONLY, cs.nico_channel)
    try:
        cs.ROOT = pipelib.ROOT = tmp
        pipelib._ST_TABLE = pipelib._ST_KEY = None
        cs.POSTS_ONLY = None
        (tmp / "candidates" / "2026-10-14.json").write_text(json.dumps([{"url": one, "title": "チャンネル告知"}]), encoding="utf-8")
        _d, _a, paths = cs.unknown_targets("2026-10-14")
        check(paths == {k1: one}, f"チャンネルの合議の対象: {paths}")
        cs.add_paths({k: ("公式", "対象チャンネル") for k in paths})
        check(pipelib.classify_source(one) == "公式" and pipelib.classify_source(two) == "未確認", "別のチャンネルまで公式になった")
        (tmp / "candidates" / "2026-10-14.json").write_text(json.dumps([{"url": "https://live.nicovideo.jp/watch/lv2222222"}]), encoding="utf-8")
        cs.nico_channel = lambda vid: ("ch.nicovideo.jp/channel/ch2222222", "ch2222222", "別番組")
        left, unk = cs.resolve_nico("2026-10-14", True)
        pipelib._ST_TABLE = None
        check(pipelib.classify_source("https://live.nicovideo.jp/watch/lv2222222") == "未確認" and list(unk) == ["ch.nicovideo.jp/channel/ch2222222"],
              f"別のチャンネルの番組が公式になった: {left} {unk}")
    except (Exception, SystemExit) as e:      # noqa: BLE001
        check(False, f"チャンネルの単位のテストが例外: {type(e).__name__}: {e}")
    finally:
        cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY, cs.POSTS_ONLY, cs.nico_channel = saved


def test_youtube_playlist_owner(tmp: Path):
    """YouTube の再生リストの持ち主(2026-10-17: `youtube.com/playlist?list=…` が出典に載り、持ち主を取るパーサが無かった)。
    単位は list= の ID(`&si=` は含めない)、種別は oEmbed の author_url(持ち主のチャンネル)の種別を写す。偽の取得で確かめる。"""
    import classify_sources as cs
    url = "https://www.youtube.com/playlist?list=PLVWdhK_WANzc&si=fdk5wzD9IgCw1N-r"
    key = "youtube.com/playlist?list=PLVWdhK_WANzc"
    check(cs.platform_unit(url) == ("path", key), f"再生リストの単位: {cs.platform_unit(url)}")
    check(cs.platform_unit("https://www.youtube.com/playlist")[0] == "skip", "list= の無い再生リストの URL を判定の対象にした")
    check(pipelib.path_type_norm(key) == key, "再生リストの ID の大小文字を潰した")
    calls: list[str] = []

    def fake(u: str) -> str:
        calls.append(u)
        return json.dumps({"title": "シャニラジHBTK", "author_name": "x", "author_url": "/@imas-official"})
    check(cs.playlist_author("PLVWdhK_WANzc", fetch=fake) == ("imas-official", "シャニラジHBTK")
          and "list%3DPLVWdhK_WANzc" in calls[0], f"oEmbed の持ち主: {calls}")
    check(cs.playlist_author("PLx0123456789", fetch=lambda u: json.dumps({"author_url": "https://www.youtube.com/channel/UCabc"}))[0] == "",
          "ハンドルでない author_url をハンドルにした")
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "candidates").mkdir(exist_ok=True)
    (tmp / "docs" / "_posts").mkdir(parents=True, exist_ok=True)
    (tmp / "source_types.yml").write_text("official_domains:\n  - a.example\nvideo_channels:\n  公式:\n    - imas-official\n", encoding="utf-8")
    saved = (cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY, cs.POSTS_ONLY, cs.playlist_author, dict(cs._PLAYLIST_CACHE))
    other = "https://www.youtube.com/playlist?list=PLother00000"
    try:
        cs.ROOT = pipelib.ROOT = tmp
        pipelib._ST_TABLE = pipelib._ST_KEY = None
        cs.POSTS_ONLY = None
        cs._PLAYLIST_CACHE.clear()
        owners = {"PLVWdhK_WANzc": ("imas-official", "シャニラジHBTK"), "PLother00000": ("somefan", "切り抜き")}
        cs.playlist_author = lambda lid, fetch=None: owners[lid]
        check(cs.display_base(url) == "https://youtube.com/@imas-official", f"表示の単位: {cs.display_base(url)}")
        (tmp / "candidates" / "2026-10-17.json").write_text(json.dumps([{"url": url, "title": "シャニソン"}, {"url": other, "title": "x"}]),
                                                            encoding="utf-8")
        d, a, paths = cs.unknown_targets("2026-10-17")
        check(not cs.NEED_PARSER and not cs.SKIPPED and not d and not paths, f"再生リストがパーサ待ち・合議に回った: {cs.NEED_PARSER} {paths}")
        left, unk = cs.resolve_playlists("2026-10-17", True)
        pipelib._ST_TABLE = None
        check(pipelib.classify_source(url) == "公式" and pipelib.classify_source(other) == "未確認", "持ち主の種別が再生リストに写らない")
        check(list(unk) == ["somefan"] and unk["somefan"][0] == "youtube.com/playlist?list=PLother00000"
              and len(left) == 1 and "(@somefan)" in left[0], f"表に無いチャンネルの再生リスト: {left} {unk}")
        cs.add_video_channels({"somefan": ("ファン", "個人")})
        left, unk = cs.resolve_playlists("2026-10-17", True)
        pipelib._ST_TABLE = None
        check(pipelib.classify_source(other) == "ファン" and not left and not unk, f"チャンネルが決まった後の再生リスト: {left} {unk}")
    except (Exception, SystemExit) as e:      # noqa: BLE001
        check(False, f"再生リストのテストが例外: {type(e).__name__}: {e}")
    finally:
        cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY, cs.POSTS_ONLY, cs.playlist_author, cache = saved
        cs._PLAYLIST_CACHE.clear()
        cs._PLAYLIST_CACHE.update(cache)


def test_booth_item_owner(tmp: Path):
    """BOOTH の商品の持ち主(2026-10-19: `booth.pm/ja/items/7563732` が出典に載り、持ち主を取るパーサが無かった)。
    単位はショップ `<subdomain>.booth.pm`(ドメイン)。商品の JSON の shop.subdomain から引き、ショップの種別を商品へ写す。偽の取得で確かめる。"""
    import classify_sources as cs
    url = "https://booth.pm/ja/items/7563732"
    key = "booth.pm/ja/items/7563732"
    check(getattr(cs, "booth_item_key", lambda u: None)(url) == key, "BOOTH の商品のキーが取れない")
    for u in ("https://booth.pm/", "https://booth.pm/ja/search/アイマス", "https://booth.pm/ja/browse/写真"):
        check(cs.platform_unit(u)[0] == "skip", f"商品 ID の無い BOOTH の URL: {u} {cs.platform_unit(u)}")
    calls: list[str] = []

    def fake(u: str) -> str:
        calls.append(u)
        return json.dumps({"id": 7563732, "description": "https://other.booth.pm/ も見てね",
                           "shop": {"name": "流れ星", "subdomain": "nagarestar", "url": "https://nagarestar.booth.pm/"}})
    try:
        check(cs.booth_item_shop("7563732", fetch=fake) == ("nagarestar.booth.pm", "流れ星")
              and calls == ["https://booth.pm/ja/items/7563732.json"], f"商品の JSON のショップ: {calls}")
        check(cs.booth_item_shop("1", fetch=lambda u: json.dumps({"shop": {"subdomain": "evil.example/x"}}))[0] == "",
              "サブドメインでない subdomain をショップにした")
        check(cs.booth_item_shop("1", fetch=lambda u: (_ for _ in ()).throw(OSError("404")))[0] == "", "取得の失敗でショップを返した")
    except (Exception, SystemExit) as e:      # noqa: BLE001
        check(False, f"BOOTH のショップを引けない: {type(e).__name__}: {e}")
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "candidates").mkdir(exist_ok=True)
    (tmp / "docs" / "_posts").mkdir(parents=True, exist_ok=True)
    # 実表(source_types.yml)と同じく va-liv.booth.pm は公式。ショップの種別がそのまま商品へ写ることを確かめる
    (tmp / "source_types.yml").write_text("official_domains:\n  - va-liv.booth.pm\n", encoding="utf-8")
    saved = (cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY, cs.POSTS_ONLY, getattr(cs, "booth_item_shop", None),
             dict(getattr(cs, "_BOOTH_SHOP", {})))
    other = "https://booth.pm/items/8830505"
    try:
        cs.ROOT = pipelib.ROOT = tmp
        pipelib._ST_TABLE = pipelib._ST_KEY = None
        cs.POSTS_ONLY = None
        cs._BOOTH_SHOP.clear()
        owners = {"7563732": ("nagarestar.booth.pm", "流れ星"), "8830505": ("va-liv.booth.pm", "vα-liv")}
        cs.booth_item_shop = lambda iid, fetch=None: owners[iid]
        check(cs.platform_unit(url) == ("domain", "nagarestar.booth.pm"), f"商品の単位: {cs.platform_unit(url)}")
        check(cs.display_base(url) == "https://nagarestar.booth.pm", f"表示の単位: {cs.display_base(url)}")
        (tmp / "candidates" / "2026-10-19.json").write_text(json.dumps([{"url": url, "title": "ボイス"}, {"url": other, "title": "x"}]),
                                                            encoding="utf-8")
        d, a, paths = cs.unknown_targets("2026-10-19")
        check(not cs.NEED_PARSER and not cs.SKIPPED and d == {"nagarestar.booth.pm": url} and not paths,
              f"BOOTH の商品がパーサ待ち・ショップ以外の単位で合議に回った: {cs.NEED_PARSER} {cs.SKIPPED} {d} {paths}")
        left = cs.resolve_booth_items(True)
        pipelib._ST_TABLE = None
        check(pipelib.classify_source(other) == "公式" and pipelib.classify_source(url) == "未確認"
              and len(left) == 1 and left[0].startswith(url), f"表にあるショップの種別が商品に写らない: {left}")
        cs.add_domains({"nagarestar.booth.pm": ("ファン", "個人のショップ")})
        pipelib._ST_TABLE = None
        left = cs.resolve_booth_items(True)
        pipelib._ST_TABLE = None
        check(pipelib.classify_source(url) == "ファン" and not left, f"ショップが決まった後の商品: {left}")
    except (Exception, SystemExit) as e:      # noqa: BLE001
        check(False, f"BOOTH の商品のテストが例外: {type(e).__name__}: {e}")
    finally:
        cs.ROOT, pipelib.ROOT, pipelib._ST_TABLE, pipelib._ST_KEY, cs.POSTS_ONLY, fn, cache = saved
        if fn is not None:
            cs.booth_item_shop = fn
            cs._BOOTH_SHOP.clear()
            cs._BOOTH_SHOP.update(cache)


def test_classify_txn_interrupt(tmp: Path):
    """出典の判定の取引の途中(表を書いた後・付け直しの最中)に親へ SIGTERM・SIGINT が来ても、子をプロセスグループごと止めてから
    判定表と記事を開始時へ戻し、中断はそのあとで元の扱いへ渡す(監査指摘 2026-10-14: 戻さずに終わって表だけが変わり、出典の lint が
    赤く、次の工程の checkout が「未コミットの変更」で止まった)。親が SIGKILL で死んだら、次の工程の checkout が控えから戻す。
    本物の親子のプロセスとシグナルで確かめる(合議・付け直しの子は、表と記事を書いて待つだけの代役)。"""
    import signal as _signal
    import time
    scripts_dir = Path(__file__).resolve().parent
    orig_table = "official_domains:\n  - a.example\n"
    orig_post = "---\n" + article_fm("a", "2026-10-14") + "---\n本文\n"
    # 合議の代役: 表と記事を書く。TXN_CLASSIFY で終わり方を変える(slow: 孫を起こして待つ / fail: 失敗で終わる /
    # orphan-fail・orphan-ok: 孫(go が置かれたら・無くても数秒後に表を書き直す)を残して、失敗・成功で先に終わる)
    classify = ("import os, subprocess, sys, time\nfrom pathlib import Path\n"
                "Path('source_types.yml').write_text('official_domains:\\n  - b.example\\n')\n"
                "Path('docs/_posts/2026-10-14-new.md').write_text('new')\n"
                "Path('.source_types.yml.999.tmp').write_text('half')\n"
                "mode = os.environ.get('TXN_CLASSIFY', '')\n"
                "if mode == 'slow':\n"
                "    g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "    Path('ready').write_text(f'{os.getpid()} {g.pid}')\n    time.sleep(60)\n"
                "if mode == 'fail':\n    sys.exit(1)\n"
                "if mode in ('orphan-fail', 'orphan-ok'):\n"
                "    g = subprocess.Popen([sys.executable, 'scripts/grandchild.py'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "    Path('ready').write_text(f'{os.getpid()} {g.pid}')\n"
                "    sys.exit(1 if mode == 'orphan-fail' else 0)\n")
    grandchild = ("import time\nfrom pathlib import Path\nfor _ in range(60):\n    if Path('go').exists():\n        break\n"
                  "    time.sleep(0.1)\nPath('source_types.yml').write_text('official_domains:\\n  - grandchild.example\\n')\n")
    retag = ("import os, subprocess, sys, time\nfrom pathlib import Path\n"
             "Path('docs/_posts/2026-10-14-a.md').write_text('retagged')\n"
             "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
             "Path('ready').write_text(f'{os.getpid()} {g.pid}')\ntime.sleep(60)\n")

    def git_(root, *a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a], cwd=root,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def setup(name):
        root = tmp / name
        (root / "scripts").mkdir(parents=True)
        (root / "docs" / "_posts").mkdir(parents=True)
        (root / "source_types.yml").write_text(orig_table, encoding="utf-8")
        (root / "docs" / "_posts" / "2026-10-14-a.md").write_text(orig_post, encoding="utf-8")
        (root / "scripts" / "classify_sources.py").write_text(classify, encoding="utf-8")
        (root / "scripts" / "retag_sources.py").write_text(retag, encoding="utf-8")
        (root / "scripts" / "grandchild.py").write_text(grandchild, encoding="utf-8")
        (root / ".gitignore").write_text("/metrics/\n/ready\n/out\n/go\n/flag\n/sweeping\n", encoding="utf-8")
        git_(root, "init", "-q", "-b", "edition/2026-10-14")
        git_(root, "add", "-A")
        git_(root, "commit", "-qm", "base")
        return root

    def alive(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False

    run = "print(pipelib.classify_retag_lint('2026-10-14', posts_only=True, lint=False), flush=True)\n"

    def start(root, handler="", body=run, wait_for="ready", env=None):
        prog = (f"import os, signal, sys, time\nsys.path.insert(0, {str(scripts_dir)!r})\nfrom pathlib import Path\nimport pipelib\n"
                f"pipelib.ROOT = Path({str(root)!r})\n{handler}\n{body}")
        proc = subprocess.Popen([sys.executable, "-c", prog], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                stdin=subprocess.DEVNULL, env={**os.environ, **(env or {})})
        for _ in range(400 if wait_for else 0):
            try:
                pids = [int(x) for x in (root / "ready").read_text().split()]
                if len(pids) == 2 and (wait_for == "ready" or (root / wait_for).exists()):
                    return proc, pids
            except (OSError, ValueError):
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        return proc, []

    def settled(root, pids, label, need_pids=True):
        (root / "go").write_text("go", encoding="utf-8")      # 残った孫がいれば、ここで表を書き直す
        for _ in range(50):          # 止めた孫が回収されるまで(残っていれば書き直すまで)
            if not any(alive(q) for q in pids):
                break
            time.sleep(0.1)
        time.sleep(0.5)
        for f in ("ready", "go"):
            (root / f).unlink(missing_ok=True)
        got = {"表": (root / "source_types.yml").read_text(encoding="utf-8") == orig_table,
               "記事": (root / "docs" / "_posts" / "2026-10-14-a.md").read_text(encoding="utf-8") == orig_post,
               "増えた記事": not (root / "docs" / "_posts" / "2026-10-14-new.md").exists(),
               "書きかけの表": not (root / ".source_types.yml.999.tmp").exists(),
               "子と孫": (bool(pids) or not need_pids) and not any(alive(q) for q in pids),
               "作業ツリー": git_(root, "status", "--porcelain").stdout.strip() == "",
               "控え": not (root / "metrics" / "work" / "classify-txn.json").exists()}
        check(all(got.values()), f"{label}: 開始時へ戻っていない・止まっていない {[k for k, v in got.items() if not v]}")

    def cleanup(pids):
        for q in pids:
            try:
                os.kill(q, _signal.SIGKILL)
            except OSError:
                pass

    tmp.mkdir(parents=True, exist_ok=True)
    # 1. SIGTERM(既定の扱い): 戻してから SIGTERM で終わる
    root = setup("term")
    proc, pids = start(root)
    try:
        check(pids, "付け直しの子が始まらない")
        os.kill(proc.pid, _signal.SIGTERM)
        proc.communicate(timeout=30)
        check(proc.returncode == -_signal.SIGTERM, f"SIGTERM の終わり方: {proc.returncode}")
        settled(root, pids, "SIGTERM")
    finally:
        cleanup(pids)
    # 2. SIGINT(Ctrl-C): 戻してから KeyboardInterrupt で終わる
    root = setup("int")
    proc, pids = start(root)
    try:
        os.kill(proc.pid, _signal.SIGINT)
        _out, err = proc.communicate(timeout=30)
        check(proc.returncode != 0 and "KeyboardInterrupt" in err, f"SIGINT の終わり方: {proc.returncode} {err[-200:]}")
        settled(root, pids, "SIGINT")
    finally:
        cleanup(pids)
    # 3. 組版のように SIGTERM の扱いを持つ親: 戻した**あと**で、その扱い(途中終了の後始末)が走る
    root = setup("handler")
    out = root / "out"
    handler = ("import os, signal\n"
               "def _h(s, f):\n"
               f"    Path({str(out)!r}).write_text(Path({str(root / 'source_types.yml')!r}).read_text())\n"
               "    os._exit(7)\n"
               "signal.signal(signal.SIGTERM, _h)\n")
    proc, pids = start(root, handler)
    try:
        os.kill(proc.pid, _signal.SIGTERM)
        proc.communicate(timeout=30)
        check(proc.returncode == 7 and out.exists() and out.read_text() == orig_table,
              f"元の SIGTERM の扱いが、戻した後に走らない: {proc.returncode} {out.read_text() if out.exists() else '(走っていない)'}")
        settled(root, pids, "SIGTERM(扱いを持つ親)")
    finally:
        cleanup(pids)
    # 4. 時間切れ: 子と孫を止めてから戻す
    def in_process(name, mode, label, call, need_pids=None):
        """この process の中で取引を走らせる(合議の代役の終わり方は mode)。call は取引を呼ぶ関数(戻りは検める側が見る)"""
        root = setup(name)
        saved_root, saved_env, pids = pipelib.ROOT, os.environ.get("TXN_CLASSIFY"), []
        try:
            pipelib.ROOT = root
            os.environ["TXN_CLASSIFY"] = mode
            call(root)
            pids = [int(x) for x in (root / "ready").read_text().split()] if (root / "ready").exists() else []
            settled(root, pids, label, need_pids=(mode != "") if need_pids is None else need_pids)
        except (Exception, SystemExit) as e:      # noqa: BLE001
            check(False, f"{label}のテストが例外: {type(e).__name__}: {e}")
        finally:
            pipelib.ROOT = saved_root
            if saved_env is None:
                os.environ.pop("TXN_CLASSIFY", None)
            else:
                os.environ["TXN_CLASSIFY"] = saved_env
            cleanup(pids)

    def timed_out(root):
        ok, why = pipelib.classify_retag_lint("2026-10-14", posts_only=True, timeout=3, lint=False)
        check(not ok and "TimeoutExpired" in why, f"時間切れの扱い: {ok} {why}")
    in_process("timeout", "slow", "時間切れ", timed_out)
    # 4'. 合議の子が先に終わり、孫が残る(失敗で終わる・成功して付け直しへ進む): 孫も止めてから戻す。戻した後で孫に書き直させない
    def failed_first(root):
        ok, why = pipelib.classify_retag_lint("2026-10-14", posts_only=True, lint=False)
        check(not ok and "合議 exit 1" in why, f"合議の失敗の扱い: {ok} {why}")
    in_process("orphan-fail", "orphan-fail", "子が先に失敗して孫が残る", failed_first)
    # 4'''. 子の記録を書けない(空き容量が無い等): 子を動かさずに止めて段を失敗にする(記録の無い子を残さない。親が SIGKILL で
    #       死んでも次の起動が止められない子を作らない)
    def no_record(root):
        saved_w = pipelib._write_txn_child
        pipelib._write_txn_child = lambda *a: (_ for _ in ()).throw(OSError(28, "No space left on device"))
        try:
            ok, why = pipelib.classify_retag_lint("2026-10-14", posts_only=True, lint=False)
        finally:
            pipelib._write_txn_child = saved_w
        check(not ok and "OSError" in why and not (root / "ready").exists()
              and (root / "source_types.yml").read_text(encoding="utf-8") == orig_table, f"子の記録を書けないのに子が動いた: {ok} {why}")
    in_process("no-record", "orphan-ok", "子の記録を書けない", no_record, need_pids=False)
    # 親が合図の前に死んだ(標準入力が閉じた)子は、本来のコマンドを動かさずに終わる
    gate_dir = tmp / "gate"
    gate_dir.mkdir(exist_ok=True)
    try:
        g = subprocess.Popen([sys.executable, "-c", pipelib._TXN_GATE, sys.executable, "-c", "open('ran', 'w').write('x')"],
                             cwd=gate_dir, stdin=subprocess.PIPE)
        g.stdin.close()
        g.wait(timeout=30)
        check(g.returncode == 125 and not (gate_dir / "ran").exists(), f"合図の無いまま取引の子が動いた: exit {g.returncode}")
    except (Exception, SystemExit) as e:      # noqa: BLE001
        check(False, f"取引の子の入口のテストが例外: {type(e).__name__}: {e}")
    # 4''. 工程の終了の例外(収集の SIGTERM を例外にした Terminated のような BaseException)・KeyboardInterrupt が取引の中で
    #      起きても、戻してから例外を通す
    class Terminated(BaseException):
        pass
    for exc in (Terminated, KeyboardInterrupt):
        def raised(root, exc=exc):
            real = pipelib._txn_run

            def after_classify(cmd, timeout):
                r = real(cmd, timeout)
                if "classify_sources.py" in str(cmd[1]):
                    raise exc()
                return r
            pipelib._txn_run = after_classify
            try:
                pipelib.classify_retag_lint("2026-10-14", posts_only=True, lint=False)
                check(False, f"{exc.__name__} が取引の外へ出ない")
            except exc:
                pass
            finally:
                pipelib._txn_run = real
        in_process(f"raise-{exc.__name__}", "", f"取引の中の {exc.__name__}", raised)
    # 5. 親が SIGKILL(後始末できない): 次の工程の checkout が、未コミットの変更を見る前に控えから戻し、残った子も止める
    root = setup("kill")
    proc, pids = start(root)
    saved = (pipelib.ROOT, pipelib.notify, pipelib.git_net, list(pipelib.ANOMALIES))
    try:
        os.kill(proc.pid, _signal.SIGKILL)
        proc.communicate(timeout=30)
        check((root / "source_types.yml").read_text(encoding="utf-8") != orig_table, "SIGKILL の前提: 表が書き換わっていない")
        notes = []
        pipelib.ROOT = root
        pipelib.notify = lambda job, msg, ok=True, require=False: notes.append(msg) or True
        pipelib.git_net = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "offline")
        pipelib.checkout_edition_branch("2026-10-14", "collect")
        check(notes and "未コミット" not in notes[0] and "fetch" in notes[0], f"checkout が控えから戻さずに止まった: {notes}")
        settled(root, pids, "SIGKILL の後の checkout")
    except (Exception, SystemExit) as e:      # noqa: BLE001
        check(False, f"SIGKILL の後の回復のテストが例外: {type(e).__name__}: {e}")
    finally:
        pipelib.ROOT, pipelib.notify, pipelib.git_net = saved[:3]
        pipelib.ANOMALIES[:] = saved[3]
        cleanup(pids)
    # 6. 合議の子が先に終わったあと・孫を止める前に親が SIGKILL: 次の工程の checkout が、直接の子が終わっていても孫を止めてから戻す
    root = setup("kill-orphan")
    slow_sweep = (f"_real = pipelib._stop_session\n"
                  f"def _slow(*a, **k):\n    Path({str(root / 'sweeping')!r}).write_text('x')\n    time.sleep(60)\n    return _real(*a, **k)\n"
                  "pipelib._stop_session = _slow\n")
    proc, pids = start(root, slow_sweep, wait_for="sweeping", env={"TXN_CLASSIFY": "orphan-ok"})
    saved = (pipelib.ROOT, pipelib.notify, pipelib.git_net, list(pipelib.ANOMALIES))
    try:
        check(len(pids) == 2 and not alive(pids[0]) and alive(pids[1]), f"前提: 合議の子は終わり、孫が残っている: {pids}")
        os.kill(proc.pid, _signal.SIGKILL)
        proc.communicate(timeout=30)
        pipelib.ROOT = root
        pipelib.notify = lambda job, msg, ok=True, require=False: True
        pipelib.git_net = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "offline")
        pipelib.checkout_edition_branch("2026-10-14", "collect")
        (root / "sweeping").unlink(missing_ok=True)
        settled(root, pids, "子が終わった後の SIGKILL の後の checkout")
    except (Exception, SystemExit) as e:      # noqa: BLE001
        check(False, f"子が終わった後の SIGKILL のテストが例外: {type(e).__name__}: {e}")
    finally:
        pipelib.ROOT, pipelib.notify, pipelib.git_net = saved[:3]
        pipelib.ANOMALIES[:] = saved[3]
        cleanup(pids)
    # 7. 戻している最中に届いた SIGTERM・SIGINT も落とさない: 戻し終えてから元の扱いへ届く(合議の失敗で戻す最中に自分へ送る)
    for sig, want in ((_signal.SIGTERM, "既定"), (_signal.SIGINT, "KeyboardInterrupt"), (_signal.SIGTERM, "扱いを持つ親")):
        root = setup(f"during-{sig.name}-{len(want)}")
        out = root / "out"
        handler = (f"_real = pipelib._restore\n"
                   f"def _r(*a, **k):\n    if not Path({str(root / 'flag')!r}).exists():\n"
                   f"        Path({str(root / 'flag')!r}).write_text('x')\n        os.kill(os.getpid(), {int(sig)})\n"
                   "    return _real(*a, **k)\npipelib._restore = _r\n")
        if want == "扱いを持つ親":
            handler += ("def _h(s, f):\n"
                        f"    Path({str(out)!r}).write_text(Path({str(root / 'source_types.yml')!r}).read_text())\n    os._exit(7)\n"
                        "signal.signal(signal.SIGTERM, _h)\n")
        proc, _p = start(root, handler, wait_for=None, env={"TXN_CLASSIFY": "fail"})
        try:
            _out, err = proc.communicate(timeout=60)
            if want == "既定":
                ok_end = proc.returncode == -_signal.SIGTERM
            elif want == "KeyboardInterrupt":
                ok_end = proc.returncode != 0 and "KeyboardInterrupt" in err
            else:
                ok_end = proc.returncode == 7 and out.exists() and out.read_text() == orig_table
            check(ok_end and (root / "flag").exists(), f"戻す最中の {sig.name}({want})を落とした: exit {proc.returncode} {err[-200:]}")
            (root / "flag").unlink(missing_ok=True)
            settled(root, [], f"戻す最中の {sig.name}({want})", need_pids=False)
        finally:
            if proc.poll() is None:
                proc.kill()
    # 8. SIGTERM を例外(収集の Terminated のような BaseException)に変える親: 戻してから、その例外が親へ届く
    root = setup("terminated")
    out = root / "out"
    handler = ("class Terminated(BaseException):\n    pass\n"
               "def _h(s, f):\n    raise Terminated()\nsignal.signal(signal.SIGTERM, _h)\n")
    body = ("try:\n    " + run + "except Terminated:\n"
            f"    Path({str(out)!r}).write_text(Path({str(root / 'source_types.yml')!r}).read_text())\n    sys.exit(5)\n")
    proc, pids = start(root, handler, body)
    try:
        os.kill(proc.pid, _signal.SIGTERM)
        proc.communicate(timeout=30)
        check(proc.returncode == 5 and out.exists() and out.read_text() == orig_table,
              f"SIGTERM を例外にする親へ、戻した後に例外が届かない: {proc.returncode} {out.read_text() if out.exists() else '(届いていない)'}")
        settled(root, pids, "SIGTERM を例外にする親")
    finally:
        cleanup(pids)


def test_tags_and_idols(tmp: Path):
    """タグの正規化と名鑑のブランドの写像を、結果で検める(監査指摘 2026-10-14: 正規化を全消去にしても、名鑑 367件を全部 other で
    作っても、既存のテストが 0 failures だった)。"""
    import collections
    import build_idols
    import tags
    spec = {"drop": {"公式", "未確認"}, "alias": {"shiny": "シャニマス", "765": "765AS", "シンデレラ": "シンデレラガールズ"}, "vocabulary": {}}
    got = tags.normalize([" shiny ", "シャニマス", "公式", 765, "", "新しい固有名", "シンデレラ", "シンデレラガールズ", "未確認"], spec)
    check(got == ["シャニマス", "765AS", "新しい固有名", "シンデレラガールズ"], f"タグの正規化(表記ゆれ・除外・重複・順序・自由な固有名): {got}")
    real = tags.load()
    check(real["alias"] and real["drop"], "語彙(docs/_data/tags.yml)の表記ゆれ・除外が読めない")
    for k, v in real["alias"].items():
        want = [] if v in real["drop"] else [v]
        check(tags.normalize([k], real) == want, f"語彙の表記ゆれ {k} → {v} が効かない: {tags.normalize([k], real)}")
    check(all(tags.normalize([d], real) == [] for d in real["drop"]), "語彙の除外が効かない")
    # 名鑑: 原本の brandId は、原本に書かれたブランド名を通して本紙のブランドへ写る(写像の表とは別に、ここに持つ対応で検める)
    name_of_id = {1: "765AS", 3: "DS", 4: "シンデレラ", 5: "ミリオン", 6: "SideM", 7: ".KR", 8: "シャニ", 9: "961プロ",
                  10: "ヴイアラ", 11: "学園", 255: "その他"}
    brand_of_name = {"765AS": "765", "DS": "dsva", "シンデレラ": "cg", "ミリオン": "million", "SideM": "sidem", ".KR": "other",
                     "シャニ": "shiny", "961プロ": "other", "ヴイアラ": "dsva", "学園": "gaku", "その他": "other"}
    repo = Path(__file__).resolve().parent.parent
    raw = json.loads((repo / "birthdays.json").read_text(encoding="utf-8"))
    tmp.mkdir(parents=True, exist_ok=True)
    saved = (build_idols.SRC, build_idols.DST, build_idols.ROOT)
    try:
        build_idols.SRC, build_idols.DST, build_idols.ROOT = repo / "birthdays.json", tmp / "idols.json", tmp
        rc = build_idols.main()
    finally:
        build_idols.SRC, build_idols.DST, build_idols.ROOT = saved
    built = json.loads((tmp / "idols.json").read_text(encoding="utf-8")) if (tmp / "idols.json").exists() else []
    by_id = {e["characterId"]: e for e in raw}
    check(rc == 0 and len(built) == len(raw) == len(by_id) > 300, f"名鑑の件数: {rc} {len(built)} / 原本 {len(raw)}")
    wrong = []
    for item in built:
        src = by_id.get(item["characterId"]) or {}
        name = name_of_id.get(src.get("brandId"))
        m, d = (int(x) for x in str(src.get("birthday", "0/0")).split("/"))
        if (name not in (src.get("brands") or []) or item["brand"] != brand_of_name.get(name) or item["name"] != src.get("name")
                or item["birthday"] != f"{m:02d}-{d:02d}"):
            wrong.append(f"{item['name']}({src.get('brandId')}→{item['brand']})")
    check(not wrong, f"名鑑のブランド・名前・誕生日が原本と合わない {len(wrong)}件: {wrong[:5]}")
    counts = collections.Counter(i["brand"] for i in built)
    check(counts["cg"] == sum(1 for e in raw if e["brandId"] == 4) and len(counts) == 8, f"名鑑のブランドの分布: {dict(counts)}")
    check((repo / "docs" / "_data" / "idols.json").read_text(encoding="utf-8") == (tmp / "idols.json").read_text(encoding="utf-8"),
          "公開している名鑑(docs/_data/idols.json)が原本から作り直したものと違う")


DIGEST_LABELS = ("本日", "昨日", "継続中", "明日")


class assemble_at:
    """assemble の置き場(記事・号・台帳・予約・pending・控え)を tmp に向ける。抜けるときに戻す。"""
    NAMES = ("ROOT", "POSTS", "EDITIONS", "STORIES", "SCHEDULED", "PENDING")

    def __init__(self, tmp: Path):
        self.tmp = tmp

    def __enter__(self):
        t = self.tmp
        for d in (t / "stock" / "scheduled", t / "metrics", t / "docs" / "_posts", t / "docs" / "_editions", t / "candidates"):
            d.mkdir(parents=True, exist_ok=True)
        self.saved = {n: getattr(assemble, n) for n in self.NAMES}
        assemble.ROOT, assemble.POSTS, assemble.EDITIONS = t, t / "docs" / "_posts", t / "docs" / "_editions"
        assemble.STORIES, assemble.SCHEDULED, assemble.PENDING = t / "stock" / "stories.yml", t / "stock" / "scheduled", t / "stock" / "pending.yml"
        return self

    def __exit__(self, *exc):
        for n, v in self.saved.items():
            setattr(assemble, n, v)
        return False


def test_derive_ranking(tmp: Path):
    """ランキング(derive --write が書いた号)を、算出の実装とは別に手で数えた順位と突き合わせる: 紙面(見出し・リード・本文・目次)の
    言及×1.0 + 当日の探索(explore)素材の言及×0.5、同点は前号順位→名鑑順(characterId。名鑑ファイルの並びではない)、delta は前号比。
    言及が8名に満たない号でもスコア0を同じ規則で並べて8件にする(2026-10-14 監査: 言及が8名未満の号でランキングが8件に届かず
    lint が止めた・重みを0にしても全テストが通った)。"""
    import contextlib
    import datetime
    import io as _io
    import derive
    import yaml
    for d in ("docs/_data", "docs/_posts", "docs/_editions", "candidates"):
        (tmp / d).mkdir(parents=True, exist_ok=True)
    idols = [("アカネ", "cg", 50), ("イズミ", "765", 10), ("ウタハ", "million", 30), ("エマ", "shiny", 20), ("オトハ", "sidem", 60),
             ("カナデ", "cg", 40), ("キララ", "gaku", 70), ("クルミ", "765", 80), ("ケイ", "million", 90), ("コハル", "shiny", 5),
             ("サクラ", "cg", 3)]
    (tmp / "docs/_data/idols.json").write_text(json.dumps([{"name": n, "brand": b, "birthday": "01-01", "characterId": c}
                                                           for n, b, c in idols], ensure_ascii=False), encoding="utf-8")
    brand = {n: b for n, b, _ in idols}

    def edition(date: str, rows=(), ranking=()):
        fm = {"number": 1, "date": date, "weekday": "月火水木金土日"[datetime.date.fromisoformat(date).weekday()],
              "issued_at": f"{date}T06:00:00+09:00", "pages": 0, "article_count": 0, "corrected_count": 0, "lead_slug": "a1",
              "digest": [{"label": l, "rows": list(rows) if l == "本日" else []} for l in DIGEST_LABELS],
              "ranking": [{"n": i + 1, "name": n, "brand": brand[n], "delta": "new"} for i, n in enumerate(ranking)], "birthdays": []}
        (tmp / "docs/_editions" / f"{date}.md").write_text("---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n",
                                                           encoding="utf-8")

    def post(date: str, slug: str, title: str, lede: str, body: str):
        (tmp / "docs/_posts" / f"{date}-{slug}.md").write_text("---\n" + article_fm(slug, date, title=title, lede=lede) + "---\n" + body + "\n",
                                                               encoding="utf-8")

    def ranking(date: str):
        saved_root, saved_argv = derive.ROOT, sys.argv
        derive.ROOT, sys.argv = tmp, ["derive.py", "--date", date, "--write"]
        try:
            with contextlib.redirect_stdout(_io.StringIO()):
                derive.main()
        except Exception as e:      # noqa: BLE001  8件に届かない号は書式違反(SchemaViolation)で上がる
            return f"{type(e).__name__}: {e}"
        finally:
            derive.ROOT, sys.argv = saved_root, saved_argv
        fm, _ = derive.parse_frontmatter(tmp / "docs/_editions" / f"{date}.md")
        return [(r["n"], r["name"], r["brand"], r["delta"]) for r in fm["ranking"]]

    edition("2026-10-19", ranking=["カナデ", "オトハ", "ウタハ", "イズミ", "エマ", "キララ", "クルミ", "ケイ"])
    # 10/20: 紙面 = 記事2本+目次の行。探索素材は当日の explore だけを数える(watch・前日の素材は数えない)
    post("2026-10-20", "a1", "アカネの新曲。", "アカネが歌う。", "アカネの声。")
    post("2026-10-20", "a2", "イズミ公演。", "オトハも登場。", "イズミが出演。")
    edition("2026-10-20", rows=[{"k": "発", "t": "ウタハ出演", "d": "", "brand": "million", "slug": "a2"}])
    (tmp / "candidates/2026-10-20.json").write_text(json.dumps([
        cand_row("e1", "https://x.example/1", origin="explore", title="ウタハ配信", facts=["ウタハ登場"]),
        cand_row("e2", "https://x.example/2", origin="explore", title="エマ特集", facts=["エマ写真", "サクラ告知"]),
        cand_row("e3", "https://x.example/3", origin="explore", title="コハル生誕", facts=[]),
        cand_row("w1", "https://x.example/4", origin="watch", title="ケイケイケイ", facts=["ケイ"])], ensure_ascii=False), encoding="utf-8")
    (tmp / "candidates/2026-10-19.json").write_text(json.dumps([
        cand_row("e9", "https://x.example/9", origin="explore", title="クルミクルミクルミ", facts=[])], ensure_ascii=False), encoding="utf-8")
    # 手で数えた点: アカネ 3 / ウタハ 1+2×0.5=2 / イズミ 2 / オトハ 1 / エマ 2×0.5=1 / サクラ 0.5 / コハル 0.5 / ほか 0(言及は7名)。
    # 同点: ウタハ(前号3位)>イズミ(4位)、オトハ(2位)>エマ(5位)、サクラ(id 3)>コハル(id 5)。8位はスコア0のうち前号最上位のカナデ
    want = [(1, "アカネ", "cg", "new"), (2, "ウタハ", "million", "up"), (3, "イズミ", "765", "up"), (4, "オトハ", "sidem", "down"),
            (5, "エマ", "shiny", "stay"), (6, "サクラ", "cg", "new"), (7, "コハル", "shiny", "new"), (8, "カナデ", "cg", "down")]
    got = ranking("2026-10-20")
    check(got == want, f"ランキング(言及7名・同点・探索の重み・前号比)が手で数えた順位と違う: {got}")
    # 10/21: 言及は1名だけ。残りはスコア0を前号(10/20 の derive の出力)の順位で並べて8件
    post("2026-10-21", "b1", "ケイの話。", "続報。", "本文。")
    edition("2026-10-21")
    want2 = [(1, "ケイ", "million", "new"), (2, "アカネ", "cg", "down"), (3, "ウタハ", "million", "down"), (4, "イズミ", "765", "down"),
             (5, "オトハ", "sidem", "down"), (6, "エマ", "shiny", "down"), (7, "サクラ", "cg", "down"), (8, "コハル", "shiny", "down")]
    got2 = ranking("2026-10-21")
    check(got2 == want2, f"言及が1名の号のランキングが8件にならない・スコア0の並びが前号順位でない: {got2}")


def test_assemble_digest_keeps_lead_within_cap():
    """一面を欠く 4群×3行の答えでも、一面を足したうえで目次は全体12行・群4行に収まり、一面の行は1行残る
    (2026-10-14 監査: 12行に縮めた後に一面を足して13行になり、lint と組版が止まった)。"""
    posts = [{"slug": "lead", "brand": "cg", "rank": "lead", "title": "一面の大きな知らせ", "lede": "一面のリード", "candidate_ids": []}]
    posts += [{"slug": f"o{i}", "brand": "765", "rank": "small", "title": f"知らせ{i}号の見出し", "lede": "リード", "candidate_ids": []}
              for i in range(16)]
    inp = {"articles": [{"slug": p["slug"], "title": p["title"], "lede": p["lede"], "facts": [], "dedup_key": p["slug"]} for p in posts],
           "pending": [], "tomorrow_reservations": []}
    row = lambda p: {"k": "発", "t": p["title"][:20], "d": "", "brand": p["brand"], "slug": p["slug"]}
    empty = {"stories": [], "reservations": [], "pending_add": [], "pending_remove": []}
    no_lead = dict(empty, digest=[{"label": l, "rows": [row(posts[1 + 3 * g + j]) for j in range(3)]} for g, l in enumerate(DIGEST_LABELS)])
    late_lead = dict(empty, digest=[{"label": l, "rows": [row(posts[1 + 4 * g + j]) for j in range(4)]} for g, l in enumerate(DIGEST_LABELS)])
    late_lead["digest"][3]["rows"][3] = row(posts[0])          # 一面が最後の群の最後の行(縮めるときに削られる位置)
    for name, out in (("一面を欠く4群×3行", no_lead), ("一面が末尾の4群×4行", late_lead)):
        res, _ = assemble.validate("2026-10-14", json.loads(json.dumps(out)), posts, {}, inp)
        groups = res["digest"]
        check([g["label"] for g in groups] == list(DIGEST_LABELS) and sum(len(g["rows"]) for g in groups) == 12
              and all(len(g["rows"]) <= 4 for g in groups) and sum(r.get("slug") == "lead" for g in groups for r in g["rows"]) == 1,
              f"目次が全体12行・群4行・一面1行に収まらない({name}): {[[r.get('slug') for r in g['rows']] for g in groups]}")


def test_assemble_interrupted_writes(tmp: Path):
    """台帳(YAML)・予約(JSON)・号スナップショットの書き直しが途中で止まっても(容量不足)、元のファイルを保ち、一時ファイルを残さず、同じ組版を
    もう一度走らせれば直る。一時ファイルは書く先と同じディレクトリの Git の対象外の名前(2026-10-14 監査: 予約の書き直しの中断で
    JSON が壊れ再実行も JSONDecodeError で止まった・stories.yml.tmp が残って作業ツリーを汚した)。"""
    import builtins
    import io as _io
    D = "2026-10-14"
    post = {"slug": "x", "brand": "cg", "rank": "lead", "candidate_ids": ["c1"], "title": "催し", "lede": "l"}
    mats = {"c1": cand_row("c1", "https://idolmaster-official.jp/news/01_1", dedup_key="event", title="催し",
                           facts=["2026年10月20日に催しを開催"])}

    def out(note: str) -> dict:
        return {"digest": [{"label": l, "rows": []} for l in DIGEST_LABELS],
                "stories": [{"slug": "x", "story_id": "event", "subject": "催し", "published_facts": [note]}],
                "reservations": [{"slug": "x", "candidate_id": "c1", "date": "2026-10-20", "kind": "開幕", "subject": "催し", "note": note}],
                "pending_add": [], "pending_remove": []}
    repo = Path(__file__).resolve().parent.parent
    res = {}
    with assemble_at(tmp):
        assemble.dump_yaml(assemble.STORIES, [])
        assemble.dump_yaml(assemble.PENDING, [])
        assemble.apply(D, 1, out("一回目"), [post], mats, False)
        sched, ed = assemble.SCHEDULED / "2026-10-20.json", assemble.EDITIONS / f"{D}.md"
        for target in (assemble.STORIES, sched, ed):
            before, seen, saved_open = target.read_bytes(), [], _io.open

            def full_disk(file, mode="r", *a, **k):
                try:
                    p = Path(os.fspath(file))
                except TypeError:
                    return saved_open(file, mode, *a, **k)
                if any(c in mode for c in "wax") and p.parent == target.parent and target.name in p.name:
                    seen.append(p)
                    with saved_open(file, mode, *a, **k) as f:
                        f.write(b'[{"partial' if "b" in mode else '[{"partial')      # 書きかけで容量が尽きる
                    raise OSError(28, "No space left on device(試験)")
                return saved_open(file, mode, *a, **k)
            _io.open = builtins.open = full_disk
            try:
                failed = False
                try:
                    assemble.apply(D, 1, out("二回目"), [post], mats, False)
                except OSError:
                    failed = True
            finally:
                _io.open = builtins.open = saved_open
            intact = target.read_bytes() == before
            left = sorted(x.name for x in target.parent.iterdir() if x.name.endswith(".tmp"))
            ignored = bool(seen) and all(subprocess.run(["git", "check-ignore", "-q", "--no-index", str(p.relative_to(tmp))],
                                                        cwd=repo, capture_output=True).returncode == 0 for p in seen)
            try:
                assemble.apply(D, 1, out("二回目"), [post], mats, False)
                again = ("二回目" in assemble.load_yaml_list(assemble.STORIES)[0]["published_facts"]
                         and json.loads(sched.read_text(encoding="utf-8"))[0]["note"] == "二回目"
                         and assemble.FM_RE.match(ed.read_text(encoding="utf-8")) is not None)
            except Exception as e:      # noqa: BLE001
                again = f"{type(e).__name__}: {e}"
            res[target.name] = (failed, intact, left, ignored, again)
    for name, (failed, intact, left, ignored, again) in res.items():
        check(failed and intact and not left and ignored and again is True,
              f"{name} の書き直しの中断: 元を保たない・一時ファイルを残した・一時ファイルが Git の対象・再実行で直らない: "
              f"intact={intact} left={left} ignored={ignored} again={again}")


def test_assemble_checks_exit_codes(tmp: Path):
    """組版は派生欄(derive)・lint を**終了コードで**判定する: 非0(signal を含む)は組版の失敗(2)にして stderr の言い分を知らせ、
    derive が落ちたら lint へ進まない(2026-10-14 監査: 別の号の書きかけの候補 JSON で lint が JSONDecodeError で落ちても、stdout に
    ::error が無いので組版は 0 を返した)。子の検査は替え玉のスクリプトを本当に子プロセスとして走らせる。"""
    import contextlib
    import io as _io
    D = "2026-10-20"
    res = {}
    with assemble_at(tmp):
        (assemble.POSTS / f"{D}-a1.md").write_text("---\n" + article_fm("a1", D, rank="lead", title="一面の見出し") + "---\n本文。\n",
                                                  encoding="utf-8")
        assemble.dump_yaml(assemble.STORIES, [])
        assemble.dump_yaml(assemble.PENDING, [])
        (tmp / "scripts").mkdir()
        for name in ("derive", "lint"):
            (tmp / "scripts" / f"{name}.py").write_text(
                "import os, pathlib, signal, sys\n"
                "here = pathlib.Path(__file__).resolve().parent.parent\n"
                f"(here / 'ran-{name}').write_text('1')\n"
                f"mode = (here / 'mode-{name}').read_text().strip()\n"
                "if mode == 'kill':\n    os.kill(os.getpid(), signal.SIGKILL)\n"
                "if mode == 'crash':\n    print('途中まで')\n"
                "    sys.stderr.write('Traceback (most recent call last):\\njson.decoder.JSONDecodeError: Expecting value: line 1 column 12\\n')\n"
                "    sys.exit(1)\n"
                "if mode == 'red':\n    print('::error file=docs/_editions/x.md::digest 全体が13行')\n    print('lint: 1 errors')\n    sys.exit(1)\n"
                "print('lint: 0 errors')\n", encoding="utf-8")
        answer = {"digest": [{"label": l, "rows": []} for l in DIGEST_LABELS], "stories": [], "reservations": [],
                  "pending_add": [], "pending_remove": []}
        saved, sent = (assemble.judge, assemble.notify), []
        assemble.judge = lambda date, inp, budget=None: json.loads(json.dumps(answer))
        assemble.notify = lambda job, msg, ok=True, **k: sent.append(msg)
        try:
            for modes in (("ok", "ok"), ("crash", "ok"), ("kill", "ok"), ("ok", "crash"), ("ok", "kill"), ("ok", "red")):
                (tmp / "mode-derive").write_text(modes[0])
                (tmp / "mode-lint").write_text(modes[1])
                for f in ("ran-derive", "ran-lint"):
                    (tmp / f).unlink(missing_ok=True)
                sent.clear()
                with contextlib.redirect_stdout(_io.StringIO()):
                    code = assemble.run(D, 1)
                res[modes] = (code, (tmp / "ran-lint").exists(), " ".join(sent))
        finally:
            assemble.judge, assemble.notify = saved
    check(res[("ok", "ok")][0] == 0 and not res[("ok", "ok")][2], f"検査が通った組版を失敗にした: {res[('ok', 'ok')]}")
    for k in (("crash", "ok"), ("kill", "ok")):
        code, lint_ran, msg = res[k]
        check(code == 2 and not lint_ran and "derive" in msg and ("JSONDecodeError" in msg or "exit -9" in msg),
              f"派生欄(derive)の異常終了({k[0]})を組版の失敗にしない・言い分を知らせない・lint へ進んだ: {res[k]}")
    for k in (("ok", "crash"), ("ok", "kill")):
        code, _, msg = res[k]
        check(code == 2 and "lint" in msg and ("JSONDecodeError" in msg or "exit -9" in msg),
              f"lint の異常終了({k[1]})を組版の失敗にしない・言い分を知らせない: {res[k]}")
    check(res[("ok", "red")][0] == 2 and "13行" in res[("ok", "red")][2], f"lint の赤を組版の失敗にしない: {res[('ok', 'red')]}")


def test_pending_keeps_later_editions(tmp: Path):
    """過去の号を組み直しても、後の号の pending の足し引き(追加・決着の削除)は残り、組み直した号の足し引きは後の号の組み直しでも
    残る。同じ判断の組み直しは同じ中身、巻き戻しはその号の分だけを外す。消した行は判断に渡さない(2026-10-14 監査: 10/12 の
    組み直しで 10/13 が足した追跡事項が黙って消えた。1巡目の監査: 印の記録を別のファイルに書くと書き込みの間で止まって消した行を
    戻せない・印の仕組みより前の号の組み直しで後の号の追加が消える・巻き戻した号の追加を後の号の組み直しがよみがえらせる)。"""
    import builtins
    import io as _io
    item = lambda k: {"dedup_key": k, "brand": "cg", "subject": f"{k} の件", "watch": "発表日"}

    def out(add=(), rm=()):
        return {"digest": [{"label": l, "rows": []} for l in DIGEST_LABELS], "stories": [], "reservations": [],
                "pending_add": [item(k) for k in add], "pending_remove": list(rm)}
    keys = lambda: [x["dedup_key"] for x in assemble.load_yaml_list(assemble.PENDING) if not x.get("removed_on")]   # 追跡中の行
    ap = lambda d, o: assemble.apply(d, 1, o, [], {}, False)
    # 1. 書き込みの中断: 一度組んだ号を組み直し、初めて k0 を消す回が pending.yml を書いた直後に止まる(容量不足)
    #    → もう一度組めば k0 は消えたまま、巻き戻せば k0 が戻る
    with assemble_at(tmp / "enospc"):
        assemble.dump_yaml(assemble.STORIES, [])
        assemble.dump_yaml(assemble.PENDING, [item("k0"), item("k9")])
        ap("2026-10-12", out())
        saved_replace, saved_open, stopped = os.replace, _io.open, []

        def replace_then_stop(src, dst, *a, **k):
            if stopped:
                raise OSError(28, "No space left on device(試験)")
            r = saved_replace(src, dst, *a, **k)
            if Path(os.fspath(dst)) == assemble.PENDING:
                stopped.append(dst)
            return r

        def stop_open(file, mode="r", *a, **k):
            if stopped and any(c in mode for c in "wax"):
                raise OSError(28, "No space left on device(試験)")
            return saved_open(file, mode, *a, **k)
        os.replace, _io.open, builtins.open = replace_then_stop, stop_open, stop_open
        try:
            try:
                ap("2026-10-12", out(rm=["k0"]))
            except OSError:
                pass
        finally:
            os.replace, _io.open, builtins.open = saved_replace, saved_open, saved_open
        ap("2026-10-12", out(rm=["k0"]))
        retried = keys()
        assemble.rollback("2026-10-12")
        restored = keys()
    check(bool(stopped) and retried == ["k9"] and sorted(restored) == ["k0", "k9"],
          f"pending.yml を書いた直後に止まった後、もう一度組む・巻き戻すで消した行を戻せない: {bool(stopped)} {retried} {restored}")
    # 2. 印の仕組みより前に組んだ号(控えに印が無い・行に印が無い)を、後の号(印の仕組み)が入った後で組み直す・巻き戻す
    with assemble_at(tmp / "legacy"):
        assemble.dump_yaml(assemble.STORIES, [])
        assemble.dump_yaml(assemble.PENDING, [item("k0"), item("old")])         # old は 10/12 が(印なしで)足した行
        assemble.dump_yaml(tmp / "legacy" / "metrics" / "pending-before-2026-10-12.yml", [item("k0")])
        ap("2026-10-13", out(add=["later"], rm=["k0"]))
        ap("2026-10-12", out())
        legacy_rebuilt = keys()
        assemble.rollback("2026-10-12")
        legacy_rolled = keys()
    check(legacy_rebuilt == ["later"] and legacy_rolled == ["later"],
          f"印の仕組みより前の号の組み直し・巻き戻しで、後の号の足し引きが消えた・前回の分が残った: {legacy_rebuilt} {legacy_rolled}")
    # 2b. 印の仕組みより前の号どうし: 9/14 が K、9/15 が X を(印なしで)足した。9/14 を組み直しても X は残り、9/14 の K は外れる。
    #     新しい仕組みの 10/15 が K を消した後、9/14 を巻き戻し、10/15 を K を消さない判断で組み直しても K はよみがえらない(2巡目の監査)
    with assemble_at(tmp / "legacy2"):
        assemble.dump_yaml(assemble.STORIES, [])
        assemble.dump_yaml(assemble.PENDING, [item("k0"), item("K"), item("X")])
        m = tmp / "legacy2" / "metrics"
        assemble.dump_yaml(m / "pending-before-2026-09-14.yml", [item("k0")])
        assemble.dump_yaml(m / "pending-before-2026-09-15.yml", [item("k0"), item("K")])
        assemble.dump_yaml(m / "pending-before-2026-09-16.yml", [item("k0"), item("K"), item("X")])
        ap("2026-10-15", out(rm=["K"]))
        assemble.rollback("2026-09-14")
        legacy_rolled2 = keys()
        ap("2026-10-15", out())
        legacy_void = keys()
    check(legacy_rolled2 == ["k0", "X"] and legacy_void == ["k0", "X"],
          f"印の仕組みより前の号の巻き戻しで後の号(9/15)の追加が消えた・巻き戻した号の追加を後の号の組み直しがよみがえらせた: "
          f"{legacy_rolled2} {legacy_void}")
    # 2c. 印の仕組みより前の号どうし: 9/13 が K を足し、9/14 が K を消した(pending に行は残っていない)。9/13 を巻き戻した後、
    #     9/14 を巻き戻しても・K を消さない判断で組み直しても K はよみがえらない(3巡目の監査。実データの sidem-mfes-2026-winter-wonderland)
    for how in ("rollback", "rebuild"):
        with assemble_at(tmp / f"legacy3-{how}"):
            assemble.dump_yaml(assemble.STORIES, [])
            assemble.dump_yaml(assemble.PENDING, [item("k0")])
            m = tmp / f"legacy3-{how}" / "metrics"
            assemble.dump_yaml(m / "pending-before-2026-09-13.yml", [item("k0")])
            assemble.dump_yaml(m / "pending-before-2026-09-14.yml", [item("k0"), item("K")])
            assemble.dump_yaml(m / "pending-before-2026-09-15.yml", [item("k0")])
            assemble.rollback("2026-09-13")
            if how == "rollback":
                assemble.rollback("2026-09-14")
            else:
                ap("2026-09-14", out())
            legacy3 = keys()
        check(legacy3 == ["k0"], f"巻き戻した号(9/13)の追加を、それを消した号(9/14)の{how}がよみがえらせた: {legacy3}")
    # 3. 巻き戻した号(10/12)の追加を、後の号(10/13)の組み直しがよみがえらせない
    with assemble_at(tmp / "void"):
        assemble.dump_yaml(assemble.STORIES, [])
        assemble.dump_yaml(assemble.PENDING, [])
        ap("2026-10-12", out(add=["k1"]))
        ap("2026-10-13", out(rm=["k1"]))
        assemble.rollback("2026-10-12")
        ap("2026-10-13", out())
        voided = keys()
    check(voided == [], f"巻き戻した号の追加を、後の号の組み直しがよみがえらせた: {voided}")
    # 4. 組み直し・巻き戻しの一連(過去の号・後の号の両方)
    with assemble_at(tmp / "seq"):
        assemble.dump_yaml(assemble.STORIES, [])
        assemble.dump_yaml(assemble.PENDING, [item("k0"), item("k9")])
        ap("2026-10-12", out(add=["k1"]))
        ap("2026-10-13", out(add=["k2"], rm=["k0", "k1"]))         # 10/13: k2 を足し、k0・k1 が決着
        after13 = keys()
        ap("2026-10-12", out(add=["k1"]))                          # 10/12 を同じ判断で組み直す
        same = keys()
        ap("2026-10-12", out(add=["k1", "k3"], rm=["k9"]))         # 10/12 を別の判断で組み直す(k3 を足し、k9 が決着)
        changed = keys()
        b = assemble.PENDING.read_bytes()
        ap("2026-10-12", out(add=["k1", "k3"], rm=["k9"]))
        stable = assemble.PENDING.read_bytes() == b
        ap("2026-10-13", out(add=["k2"], rm=["k0", "k1"]))         # そのあと 10/13 を組み直しても 10/12 の分は残る
        later_rerun = sorted(keys())
        log = assemble.rollback("2026-10-12")
        rolled = sorted(keys())
        again = assemble.rollback("2026-10-12")
        ap("2026-10-12", out(add=["k1"]))                          # 巻き戻した号を組み直す(k1 は 10/13 が決着済み)
        rebuilt = sorted(keys())
        inp_keys = [x["dedup_key"] for x in assemble.build_input("2026-10-14", [], {}, [], assemble.baseline_pending("2026-10-14", False))["pending"]]
    check(sorted(inp_keys) == ["k2", "k9"], f"決着済み(removed_on)の行を判断に渡した: {inp_keys}")
    check(after13 == ["k9", "k2"], f"前提: 10/13 の足し引き: {after13}")
    check(same == ["k9", "k2"], f"過去の号(10/12)を同じ判断で組み直すと、後の号(10/13)の足し引きが消えた: {same}")
    check(changed == ["k2", "k3"] and stable, f"過去の号を別の判断で組み直したときの pending・冪等: {changed} {stable}")
    check(later_rerun == ["k2", "k3"], f"後の号(10/13)を組み直すと、組み直した過去の号(10/12)の足し引きが消えた: {later_rerun}")
    check(rolled == ["k2", "k9"] and log and again == [], f"巻き戻しがその号の足し引きだけを外さない・冪等でない: {rolled} {log} {again}")
    check(rebuilt == ["k2", "k9"], f"巻き戻した号の組み直しで、後の号が決着させた追跡事項がよみがえった: {rebuilt}")


# --- モデル・道具のセッションの起動と回収(2026-10-14 の再監査) ------------------------------------------------------
# 子孫が死んだかは、関数が返った直後に /proc で見る(回収は死んだのを確かめてから返す。待ち時間に頼らない)

# 偽の CLI: 子を2つ起こす(同じグループの子と、自分でセッションを分けた子)。どちらも 30 秒後に書く。親は 60 秒待つ。
# 起動が速い sh で書き、子の pid は起こした直後に置き換えで書く(時間切れの前に子が揃う。読む側が書きかけを読まない)
_PROBE_CLI = ('d="$SESSION_PROBE"\n'
              '( sleep 30; echo x > "$d/group.wrote" ) &\n'
              'echo $! > "$d/group.pid.tmp" && mv "$d/group.pid.tmp" "$d/group.pid"\n'
              'setsid sh -c \'sleep 30; echo x > "$1/escaped.wrote"\' sh "$d" &\n'
              'echo $! > "$d/escaped.pid.tmp" && mv "$d/escaped.pid.tmp" "$d/escaped.pid"\n'
              'sleep 60\n')
_NO_PID = 4194311     # どのプロセスにも当たらない pid(pid_max の外。偽のプロセスの回収が本物のグループに触れない)


def _pid_alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (OSError, IndexError):
        return False


def _probe_bin(tmp: Path) -> Path:
    """偽の claude / codex / grok を置いたディレクトリ(PATH の先頭に足す)。"""
    b = tmp / "bin"
    b.mkdir(parents=True, exist_ok=True)
    for name in ("claude", "codex", "grok"):
        (b / name).write_text("#!/bin/sh\n" + _PROBE_CLI, encoding="utf-8")
        (b / name).chmod(0o755)
    return b


def _wait_files(*paths: Path, limit: float = 30) -> bool:
    """子の準備ができた(pid のファイルが揃った)のを待つ。時間切れ・中断を起こす前に、相手が揃ったことを確かめる。"""
    import time as _t
    end = _t.time() + limit
    while not all(p.exists() for p in paths):
        if _t.time() > end:
            return False
        _t.sleep(0.005)
    return True


def _left_after(tmp: Path, name: str, call) -> list[str]:
    """call(偽の CLI を時間切れ・中断まで走らせる)が返った直後に、生き残っている・時間切れのあとに書いた子孫。"""
    import signal
    d = tmp / name.replace(".", "-")
    d.mkdir(parents=True, exist_ok=True)
    saved = os.environ.get("SESSION_PROBE")
    os.environ["SESSION_PROBE"] = str(d)
    try:
        call()
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        pass
    finally:
        if saved is None:
            os.environ.pop("SESSION_PROBE", None)
        else:
            os.environ["SESSION_PROBE"] = saved
    left = []
    for n in ("group", "escaped"):
        pf = d / f"{n}.pid"
        if not pf.exists():
            left.append(f"{n}(子を起こす前に止まった)")
            continue
        pid = int(pf.read_text())
        if _pid_alive(pid):
            left.append(f"{n}(生きている)")
            try:
                os.kill(pid, signal.SIGKILL)    # 後片付け
            except OSError:
                pass
        if (d / f"{n}.wrote").exists():
            left.append(f"{n}(書いた)")
    return left


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def test_session_timeout_stops_descendants(tmp: Path):
    """モデル・道具を時間切れ・中断で止めたら、CLI が起動した道具(同じグループの子も、自分でセッションを分けた子も)まで止まり、
    関数が返った時点で死んでいる(監査指摘 2026-10-14: compose.claude_run・oncall.run_claude の時間切れのあとに孫がファイルを
    書いた。codex_run・run_codex・定点観測・分類・ベンチ・道具の更新・経過を残す実行も同じ形だった)。
    起動できない CLI は例外にせず、終了コード 127 と理由を返す"""
    import signal
    import threading
    import collect
    import classify_sources as cs
    import oncall
    import update_clis as uc
    import writer_bench as wb
    tmp.mkdir(parents=True, exist_ok=True)
    b = _probe_bin(tmp)
    sf = tmp / "answer.schema.json"
    sf.write_text('{"type": "object"}', encoding="utf-8")
    repo = tmp / "repo"     # 監査(run_codex)は commit から作業ツリーを切り出す
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "x"], check=True, capture_output=True)
    names = [(compose, "prompt_file"), (compose, "save_raw"), (oncall, "prompt_file"), (oncall, "save_raw"),
             (oncall, "session_env"), (collect, "save_raw"), (pipelib, "prompt_file"), (pipelib, "save_raw"),
             (wb, "save_raw"), (wb, "run_group")]
    saved = [(m, n, getattr(m, n, None)) for m, n in names]
    saved_path = os.environ.get("PATH", "")
    real_group = getattr(pipelib, "run_group", None)
    try:
        os.environ["PATH"] = f"{b}:{saved_path}"
        nop = lambda *a, **k: None
        compose.prompt_file = oncall.prompt_file = pipelib.prompt_file = lambda *a, **k: "p"
        compose.save_raw = oncall.save_raw = collect.save_raw = pipelib.save_raw = wb.save_raw = nop
        oncall.session_env = lambda: dict(os.environ)
        # ベンチの 1500 秒は縮める(run_group 経由で起動していなければ縮まらず、偽の CLI の親が 60 秒で終わったあとに子が書く)
        wb.run_group = lambda cmd, timeout=None, **k: real_group(cmd, timeout=2, **k)
        cases = {
            "compose.claude_run": lambda: compose.claude_run("x", timeout=2),
            "compose.codex_run": lambda: compose.codex_run("x", timeout=2),
            "oncall.run_claude": lambda: oncall.run_claude("x", sf, tmp, timeout=2),
            "oncall.run_codex": lambda: oncall.run_codex("x", sf, repo, timeout=2),
            "collect.claude_exec": lambda: collect.claude_exec("x", timeout=2),
            "classify_sources.ask": lambda: cs.ask(["claude"], "x", timeout=2),
            "writer_bench.write_one": lambda: wb.write_one("probe", "x", "probe", "m"),
            "update_clis.run": lambda: uc.run([str(b / "codex")], timeout=2, cwd=tmp),
            "pipelib.claude_traced": lambda: pipelib.claude_traced(["x"], tmp / "trace.jsonl", timeout=2, cwd=tmp),
        }
        for name, call in cases.items():
            left = _left_after(tmp, name, call)
            check(not left, f"{name}: 時間切れで止めたあとに子孫が残った・書いた({', '.join(left)})")

        # 中断(Ctrl-C・SIGTERM を変えた SystemExit など、待っている間の例外)でも、道具まで止めてから上げる。
        # 中断は子が揃ってから起こす(揃う前に止めると、確かめられない)
        def interrupted():
            d = tmp / "interrupted"

            def fire():
                if _wait_files(d / "group.pid", d / "escaped.pid"):
                    os.kill(os.getpid(), signal.SIGUSR1)
            old = signal.signal(signal.SIGUSR1, _raise_interrupt)
            t = threading.Thread(target=fire, daemon=True)
            t.start()
            try:
                compose.claude_run("x", timeout=60)
            finally:
                t.join()
                signal.signal(signal.SIGUSR1, old)
        left = _left_after(tmp, "interrupted", interrupted)
        check(not left, f"中断で止めたあとに子孫が残った・書いた({', '.join(left)})")
        # 起動できない CLI: 例外にせず、その担当だけの異常終了(127)と理由を返す
        if real_group is None:
            check(False, "モデル・道具の起動の関数(pipelib.run_group)が無い")
        else:
            r = real_group([str(tmp / "no-such-cli")], timeout=5, cwd=tmp)
            check(r.returncode == 127 and "起動できない" in r.stderr, f"起動できない CLI の結果: {r.returncode} {r.stderr!r}")
    finally:
        os.environ["PATH"] = saved_path
        for m, n, v in saved:
            setattr(m, n, v)


def test_parallel_launch_failure_reaps(tmp: Path):
    """並列に起動している途中で、2本目の起動が失敗(OSError)しても、例外で抜けずにその担当だけの失敗として扱い、
    先に起動した担当を止めて回収する(監査指摘 2026-10-14: 選定 run_plan・校閲 claude_review の2本目の起動の失敗で例外になり、
    1本目が回収されずに残って、確定後の記事に書いた)。起動以外の例外で抜けても、起動済みの担当は止めて回収してから上げる"""
    import time as _t
    real_popen = subprocess.Popen
    d = "2099-03-01"
    started: list = []

    def popen(fail_from: int):
        def fake(argv, **kw):
            if len(started) >= fail_from:
                raise OSError("injected: 起動できない")
            p = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kw)
            started.append(p)
            return p
        return fake

    def alive() -> list:
        return [p.pid for p in started if p.poll() is None]

    def cleanup():
        for p in started:
            if p.poll() is None:
                p.kill()
                p.wait()
        started.clear()

    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "docs" / "_posts").mkdir(parents=True, exist_ok=True)
    (tmp / "metrics").mkdir(exist_ok=True)
    (tmp / "docs" / "_posts" / f"{d}-a.md").write_text("---\nslug: a\ntitle: a\n---\n本文\n", encoding="utf-8")
    saved = (compose.ROOT, pipelib.ROOT, compose.DEADLINE, subprocess.Popen, compose.brand_plan_prompt, compose.prompt_file,
             compose.save_raw)
    try:
        compose.ROOT = pipelib.ROOT = tmp
        compose.save_raw = lambda *a, **k: None
        compose.brand_plan_prompt = lambda *a, **k: "x"
        # 選定: 2面を同じ波で起動し、2面目の起動が失敗する
        compose.DEADLINE = _t.time() + 2
        subprocess.Popen = popen(1)
        by_brand = {"cg": [{"dedup_key": "k1", "ids": ["c1"]}], "765": [{"dedup_key": "k2", "ids": ["c2"]}]}
        try:
            compose.run_plan(d, by_brand, [], wave=2)
            raised = ""
        except Exception as e:      # noqa: BLE001
            raised = f"{type(e).__name__}: {e}"
        check(not raised, f"選定: 2面目の起動の失敗で例外になった({raised})")
        check(started and not alive(), f"選定: 起動に失敗した波で、先に起動した面が回収されずに残った({alive()})")
        cleanup()
        # 校閲: 記事担当を起動したあと、紙面担当の起動が失敗する
        compose.DEADLINE = _t.time() + 2
        subprocess.Popen = popen(1)
        try:
            r = compose.claude_review(d, 1, targets=[f"{d}-a.md"], editorial=False, paper=True)
            raised = ""
        except Exception as e:      # noqa: BLE001
            r, raised = {}, f"{type(e).__name__}: {e}"
        check(not raised and r.get("verdict") == "error", f"校閲: 2件目の起動の失敗で例外になった・判定ありにした({raised} {r.get('verdict')})")
        check(started and not alive(), f"校閲: 起動に失敗した波で、先に起動した担当が回収されずに残った({alive()})")
        cleanup()
        # 起動以外の例外(2件目の依頼文が書けない)で抜けても、起動済みの担当を止めて回収してから上げる
        compose.DEADLINE = _t.time() + 30
        subprocess.Popen = popen(99)
        n_pf = []

        def pf(*a, **k):
            n_pf.append(1)
            if len(n_pf) >= 2:
                raise OSError("injected: 依頼文を書けない")
            return "p"
        compose.prompt_file = pf
        try:
            compose.claude_review(d, 1, targets=[f"{d}-a.md"], editorial=False, paper=True)
        except OSError:
            pass
        check(started and not alive(), f"校閲: 例外で抜けたとき、起動済みの担当が回収されずに残った({alive()})")
    finally:
        cleanup()
        (compose.ROOT, pipelib.ROOT, compose.DEADLINE, subprocess.Popen, compose.brand_plan_prompt, compose.prompt_file,
         compose.save_raw) = saved


def test_compose_no_launch_after_deadline(tmp: Path):
    """締切を過ぎたら、執筆・校閲・書き直し・修正の担当を(やり直し・差し戻しも)起動しない。未着手の記事は理由付きで記録して
    後始末へ進む(監査指摘 2026-10-14: 待ち時間を縮めるだけで起動は止まらず、締切後も後続の記事・紙面とその再試行を起動した)。
    締切前に起動した分は今どおり書く(打ち切りは締切だけ)"""
    import time as _t
    d = "2099-03-02"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "docs" / "_posts").mkdir(parents=True, exist_ok=True)
    (tmp / "metrics").mkdir(exist_ok=True)
    (tmp / "docs" / "_posts" / f"{d}-a.md").write_text("---\nslug: a\ntitle: a\n---\n本文\n", encoding="utf-8")
    launched: list = []

    class P:
        pid, returncode = _NO_PID, 0

        def communicate(self, timeout=None):
            compose.DEADLINE = _t.time() - 1        # 1本目の担当が動いている間に締切を過ぎる
            return None, None

        def kill(self):
            pass

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0
    cands = {"c1": dict(MATS[0], id="c1"), "c2": dict(MATS[1], id="c2")}
    plan = {"articles": [{"slug": s, "rank": "small", "brand": "765", "candidate_ids": [c], "dedup_key": s, "angle": s}
                         for s, c in (("w1", "c1"), ("w2", "c2"))]}
    saved = (compose.ROOT, pipelib.ROOT, compose.DEADLINE, subprocess.Popen, compose.article_prompt, compose.prompt_file,
             compose.save_raw)
    try:
        compose.ROOT = tmp
        compose.save_raw = lambda *a, **k: None
        compose.prompt_file = lambda *a, **k: "p"
        compose.article_prompt = lambda *a, **k: "x"
        subprocess.Popen = lambda argv, **k: launched.append(argv) or P()
        # 執筆: 1本目の執筆中に締切を過ぎる → 2本目と、1本目のやり直しを起動しない
        compose.DEADLINE = _t.time() + 600
        written, aborted = compose.write_articles(d, plan, cands, [], {}, wave=1)
        drops = compose.DROP_LOG.get(d) or {}
        check(len(launched) == 1 and not written and sorted(aborted) == ["w1", "w2"]
              and all((drops.get(s) or ("", ""))[1] == "締切で未着手" for s in ("w1", "w2")),
              f"執筆: 締切後に記事の担当を起動した・未着手を記録しない(起動 {len(launched)}本 / 外した {aborted} / 記録 {drops})")
        # 校閲・書き直し・修正: 締切を過ぎていれば1本も起動しない。校閲は担当が動かなかった巡(判定なし)になる
        compose.ROOT = pipelib.ROOT = tmp
        launched.clear()
        compose.DEADLINE = _t.time() - 60
        r = compose.claude_review(d, 1, targets=[f"{d}-a.md"], editorial=False, paper=True)
        check(not launched and r.get("verdict") == "error" and all("締切" in f for f in r.get("failed") or ["(なし)"]),
              f"校閲: 締切後に担当を起動した・判定ありにした(起動 {len(launched)}本 / {r.get('verdict')} {r.get('failed')})")
        arts = {"articles": [{"slug": "a", "rank": "small", "brand": "765", "candidate_ids": ["c1"], "dedup_key": "a"}]}
        res = compose.revise_articles(d, {f"docs/_posts/{d}-a.md": [{"rule_id": "R1", "issue": "x", "quote": "本文"}]},
                                      arts, cands, [])
        compose.fix_articles(d, {f"docs/_posts/{d}-a.md": [{"rule_id": "R1", "issue": "x"}]})
        check(not launched and res.get("a") == "not_started", f"書き直し・修正: 締切後に担当を起動した(起動 {len(launched)}本 / {res})")
        # 依頼文を書くあいだに締切を過ぎても起動しない(起動の直前にも確かめる)
        def slow_prompt(*a, **k):
            _t.sleep(0.1)
            return "p"
        compose.prompt_file = slow_prompt
        compose.DEADLINE = _t.time() + 0.05
        r = compose.claude_review(d, 2, targets=[f"{d}-a.md"], editorial=False, paper=False)
        compose.DEADLINE = _t.time() + 0.05
        compose.DROP_LOG.pop(d, None)
        pipelib.ROOT = saved[1]     # 出典の種別の判定表は本体のものを読む(書かない)
        written, aborted = compose.write_articles(d, {"articles": plan["articles"][:1]}, cands, [], {}, wave=1)
        check(not launched and r.get("verdict") == "error" and aborted == ["w1"],
              f"準備のあいだに締切を過ぎた担当を起動した(起動 {len(launched)}本 / 校閲 {r.get('verdict')} / 執筆で外した {aborted})")
    finally:
        compose.DROP_LOG.pop(d, None)
        compose.WRITE_OUTCOMES.pop(d, None)
        compose.REVISE_NOTES.pop(d, None)
        (compose.ROOT, pipelib.ROOT, compose.DEADLINE, subprocess.Popen, compose.article_prompt, compose.prompt_file,
         compose.save_raw) = saved


def test_leave_tree_clean_stops_sessions(tmp: Path):
    """組版が例外・SIGTERM で途中終了するときは、確定(commit)の前に、起動したまま回収していないセッションを子孫ごと止める
    (残すと確定のあとに記事を書き、作業ツリーがまた dirty になる。監査指摘 2026-10-14)"""
    start = getattr(pipelib, "start_group", None)
    if start is None:
        check(False, "セッションを起動して控える関数(pipelib.start_group)が無い")
        return
    tmp.mkdir(parents=True, exist_ok=True)
    p = start([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp)
    saved = compose.STARTED_ON
    try:
        compose.STARTED_ON = None       # commit はしない経路(開始前)。止めるのはその前
        compose._leave_tree_clean("test")
        # 後始末のあとは、別スレッドの担当が次の担当を起動しない(工程を終えるところ)
        later = start([sys.executable, "-c", "pass"], cwd=tmp)
        check(isinstance(later, pipelib.LaunchFailed), "途中終了の後始末のあとに、新しい担当を起動した")
        if not isinstance(later, pipelib.LaunchFailed):
            pipelib.finish_group(later, 10)
    finally:
        compose.STARTED_ON = saved
        if hasattr(pipelib, "STOPPING"):
            pipelib.STOPPING.clear()    # 途中終了の後始末が立てた「終えるところ」を、後のテストのために戻す
        if p.poll() is None:
            p.kill()
            p.wait()
            check(False, "途中終了の確定の前に、起動したままのセッションを止めない")


def test_session_escaped_and_term(tmp: Path):
    """セッションの親(CLI)が先に終わっても、自分でセッションを分けて残った道具を止める(正常終了でも、時間切れでも)。
    回収は、この process 自身・この process の子でない pid を起点にしない。工程の入口は、実際の SIGTERM でも起動した CLI を
    子孫ごと止めてから終わる(監査指摘 2026-10-14: 親が先に終わると別セッションの子が残った。既定の SIGTERM は後始末を走らせず、
    手で回した update_clis を止めると CLI が残った。自分を指す偽の対象の回収が無関係な子を止めた)"""
    import signal
    import time as _t
    tmp.mkdir(parents=True, exist_ok=True)
    run = getattr(pipelib, "run_group", None)
    check(run is not None and hasattr(pipelib, "stop_tree"), "モデル・道具の起動・回収の関数(pipelib.run_group・stop_tree)が無い")
    # 親は、自分でセッションを分けた子が揃うのを待ってから終わる(正常終了 / 子が出力のパイプを持ったまま、時間切れ)
    for case, closes, timeout in ((("normal", True, 30), ("pipe", False, 1)) if run else ()):
        pf = tmp / f"{case}.pid"
        code = ("import os, sys, time\n"
                "from pathlib import Path\n"
                "if os.fork() == 0:\n"
                "    os.setsid()\n"
                + ("    os.close(1); os.close(2)\n" if closes else "")
                + f"    Path({str(pf) + '.tmp'!r}).write_text(str(os.getpid())); os.replace({str(pf) + '.tmp'!r}, {str(pf)!r})\n"
                "    time.sleep(30)\n"
                "    os._exit(0)\n"
                f"while not Path({str(pf)!r}).exists(): time.sleep(0.005)\n")
        t0 = _t.time()
        try:
            r = run([sys.executable, "-c", code], timeout=timeout, cwd=tmp)
            how = f"exit {r.returncode}"
        except subprocess.TimeoutExpired:
            how = "時間切れ"
        took = _t.time() - t0
        pid = int(pf.read_text()) if pf.exists() else 0
        alive = bool(pid) and _pid_alive(pid)
        check(pid and not alive and took < 8,
              f"親が先に終わったセッション({case}: {how})で、自分でセッションを分けた子が残った・回収を待ち続けた"
              f"(pid {pid} 生存 {alive} / {took:.1f}秒)")
        if alive:
            os.kill(pid, signal.SIGKILL)
    # 自分を指す偽の対象・この process の子でない pid を起点に、無関係な子を止めない
    class Self:
        pid, returncode = os.getpid(), None

        def kill(self):
            pass

        def communicate(self, timeout=None):
            return "", ""
    # (回収の関数が無い実装では確かめない。上で落としてある)
    peers = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) for _ in range(2)] \
        if hasattr(pipelib, "stop_tree") else []
    try:
        if peers:
            pipelib.reap(Self(), grace=1)
            pipelib.stop_tree(peers[0].pid, root=peers[0].pid)     # 頭でない(自分のグループの)子: グループは無く、辿るのは子からだけ
            check(not _pid_alive(peers[0].pid) and _pid_alive(peers[1].pid),
                  "自分を指す偽の対象・グループの頭でない pid の回収で、無関係な子を止めた(または対象を止めない)")
            # 頭でない子が、兄弟(別のグループの頭)のグループに入っている: その子だけを止め、兄弟のグループには広げない
            sib = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], process_group=0)
            kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], process_group=sib.pid)
            peers += [sib, kid]
            pipelib.stop_tree(kid.pid, root=kid.pid)
            check(not _pid_alive(kid.pid) and _pid_alive(sib.pid),
                  "頭でない子の回収で、その子が入った兄弟のグループ(自分のものと分からないグループ)まで止めた")
    finally:
        for q in peers:
            if q.poll() is None:
                q.kill()
            q.wait()
    # 工程の入口: 実際の SIGTERM でも、起動した CLI を子孫ごと止めてから終わる(手で回したとき。systemd の外)
    b = _probe_bin(tmp / "term-bin")
    scripts = Path(__file__).resolve().parent
    harness = {
        "update_clis": ("import sys; sys.path.insert(0, sys.argv[1]); import update_clis as m\n"
                        "m.job_lock = lambda *a, **k: None; m.notify = lambda *a, **k: True\n"
                        "sys.argv = ['update_clis', '--no-update']; m.main()\n"),
        "collect": ("import sys; sys.path.insert(0, sys.argv[1]); import collect as m, pipelib\n"
                    "from pathlib import Path; m.ROOT = pipelib.ROOT = Path(sys.argv[2])     # 本体の作業ツリーに書かない\n"
                    "m.job_lock = lambda *a, **k: None; pipelib.prompt_file = lambda *a, **k: 'p'\n"
                    "m.run_watch = lambda call, oncall_rerun=False: (call('x', timeout=120), ([], {}))[1]\n"
                    "m.prompt_file = lambda *a, **k: 'p'; m.save_raw = lambda *a, **k: None\n"
                    "sys.argv = ['collect', '--no-git', '--skip-explore', '--skip-grok', '--date', '2099-01-01']\n"
                    "try:\n    m.main()\nexcept m.Terminated:\n    sys.exit(143)     # collect の入口(__main__)と同じ扱い\n"),
        "oncall": ("import os, sys; sys.path.insert(0, sys.argv[1]); import oncall as m\n"
                   "from pathlib import Path; d = Path(sys.argv[2]); sf = d / 's.json'; sf.write_text('{\"type\": \"object\"}')\n"
                   "m.prompt_file = lambda *a, **k: 'p'; m.save_raw = lambda *a, **k: None; m.session_env = lambda: dict(os.environ)\n"
                   "m._main = lambda: m.run_claude('x', sf, d, timeout=60)     # 通常の当番(どの工程でも入口は main)\n"
                   "m.main()\n"),
    }
    for name, code in harness.items():
        d = tmp / f"term-{name}"
        d.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k != "INVOCATION_ID"}
        env.update(PATH=f"{b}:{os.environ.get('PATH', '')}", SESSION_PROBE=str(d))
        h = subprocess.Popen([sys.executable, "-c", code, str(scripts), str(d)], env=env, cwd=tmp, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        ready = _wait_files(d / "group.pid", d / "escaped.pid")
        h.terminate()       # 実際の SIGTERM
        try:
            _, err = h.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            h.kill()
            _, err = h.communicate()
        left = []
        for n in ("group", "escaped"):
            pf = d / f"{n}.pid"
            pid = int(pf.read_text()) if pf.exists() else 0
            if pid and _pid_alive(pid):
                left.append(n)
                os.kill(pid, signal.SIGKILL)
        check(ready and h.returncode == 143 and not left,
              f"{name}: SIGTERM で、起動した CLI を止めずに終わった(準備 {ready} / 終了 {h.returncode} / 残った {left} / "
              f"{(err or '').strip()[-200:]})")
    # 工程が例外(Ctrl-C を含む)で終わるときも、回収していないセッション(別スレッドの担当も)を残さない
    d = tmp / "exit-int"
    d.mkdir(parents=True, exist_ok=True)
    code = ("import sys, threading; sys.path.insert(0, sys.argv[1]); import pipelib\n"
            "from pathlib import Path; d = Path(sys.argv[2])\n"
            "threading.Thread(target=lambda: pipelib.start_group([sys.argv[3]], cwd=d), daemon=True).start()\n"
            "import time\n"
            "while not ((d / 'group.pid').exists() and (d / 'escaped.pid').exists()): time.sleep(0.005)\n"
            "raise KeyboardInterrupt\n")
    env = dict(os.environ, SESSION_PROBE=str(d))
    h = subprocess.run([sys.executable, "-c", code, str(scripts), str(d), str(b / "claude")], env=env, cwd=tmp,
                       capture_output=True, text=True, timeout=60, start_new_session=True)
    left = []
    for n in ("group", "escaped"):
        pf = d / f"{n}.pid"
        pid = int(pf.read_text()) if pf.exists() else 0
        if pid and _pid_alive(pid):
            left.append(n)
            os.kill(pid, signal.SIGKILL)
    check(h.returncode != 0 and not left, f"例外で終わった工程が、別スレッドで起動した CLI を残した(終了 {h.returncode} / 残った {left})")


def test_session_signal_windows(tmp: Path):
    """中断の隙間を残さない(Sol 監査2巡目の指摘 2026-10-14):
    - 起動から控えるまでのあいだに届いた SIGINT・SIGTERM でも、起動した担当を見失わずに止める
    - 収集の SIGTERM のあと、待っていた定点観測のバッチが CLI を起動しない(工程を終えるところでは起動しない)
    - 依頼文を書くあいだに収集の締切を過ぎたら facts 化を起動せず、読めなかった回数にも数えない
    - 正常に終わったあとの後始末の途中の中断でも、残った道具を凍らせたまま忘れない
    - この process の子でない対象(ほかの親の独立したセッション)は、グループも含めて止めない"""
    import signal
    import time as _t
    import collect
    tmp.mkdir(parents=True, exist_ok=True)
    real_popen = subprocess.Popen
    started: list = []

    def popen_then(sig):
        def fake(argv, **kw):
            p = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kw)
            started.append(p)
            os.kill(os.getpid(), sig)       # 起動から控えるまでのあいだに届く信号
            return p
        return fake

    def cleanup():
        for p in started:
            if p.poll() is None:
                p.kill()
                p.wait()
        started.clear()
    start = getattr(pipelib, "start_group", None)
    check(start is not None, "セッションを起動して控える関数(pipelib.start_group)が無い")
    # 起動の隙間の SIGINT(既定の扱い: KeyboardInterrupt)・SIGTERM(exit_on_term)。モデル・道具の起動(start_group)と、
    # 当番が工程を再実行する起動(oncall.run_stage)の両方
    import oncall
    old_int = signal.signal(signal.SIGINT, signal.default_int_handler)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        for (sig, want), via in [(c, v) for v in ("start_group", "run_stage")
                                 for c in ((signal.SIGINT, "KeyboardInterrupt"), (signal.SIGTERM, "SystemExit 143"))]:
            if start is None:
                break
            if sig == signal.SIGTERM:
                pipelib.exit_on_term()
            subprocess.Popen = popen_then(sig)
            try:
                if via == "run_stage":
                    oncall.run_stage(["probe"], tmp / "stage.log", 30)
                else:
                    start(["probe"], cwd=tmp)
                got = "返った"
            except KeyboardInterrupt:
                got = "KeyboardInterrupt"
            except SystemExit as e:
                got = f"SystemExit {e.code}"
            finally:
                subprocess.Popen = real_popen
            check(got == want and started and started[-1].poll() is not None,
                  f"{via}: 起動から控えるまでのあいだの {signal.Signals(sig).name} で、起動した担当を見失った({got} / "
                  f"生きている {[p.pid for p in started if p.poll() is None]})")
            cleanup()
            if hasattr(pipelib, "STOPPING"):
                pipelib.STOPPING.clear()    # SIGTERM の後始末が立てた「終えるところ」を、次の確かめのために戻す
    finally:
        subprocess.Popen = real_popen
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term if old_term is not None else signal.SIG_DFL)
        if hasattr(pipelib, "STOPPING"):
            pipelib.STOPPING.clear()        # SIGTERM の後始末が立てた「終えるところ」を、後のテストのために戻す
        cleanup()
    # 別スレッドで起動してから控えるまでのあいだに、主スレッドが終了の後始末(stop_groups)をしても、その担当を止める
    # (後始末の一覧に載らないまま待ち続けない。Sol 監査3巡目の指摘)
    if start is not None:
        import threading
        launched, resume, res = threading.Event(), threading.Event(), {}

        def held(argv, **kw):
            p = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kw)
            started.append(p)
            launched.set()
            resume.wait(10)         # 主スレッドの後始末が済むまで、控える前で止まる
            return p
        subprocess.Popen = held
        try:
            t = threading.Thread(target=lambda: res.setdefault("r", start(["probe"], cwd=tmp)), daemon=True)
            t.start()
            launched.wait(10)
            pipelib.stop_groups()           # 終了の後始末(主スレッド。SIGTERM のハンドラと同じ)
            resume.set()
            t.join(30)
        finally:
            subprocess.Popen = real_popen
            if hasattr(pipelib, "STOPPING"):
                pipelib.STOPPING.clear()
        r = res.get("r")
        check(isinstance(r, getattr(pipelib, "LaunchFailed", ())) and started and started[-1].poll() is not None,
              f"別スレッドの起動のあいだに始まった終了の後始末から、その担当が漏れた({type(r).__name__} / "
              f"生きている {[p.pid for p in started if p.poll() is None]})")
        if r is not None and not isinstance(r, getattr(pipelib, "LaunchFailed", ())):
            pipelib.stop_groups([r])
        cleanup()
    # 収集の SIGTERM: 待っていたバッチが CLI を起動しない(実際の collect.main・run_watch・定点観測の並列)
    d = tmp / "watch-term"
    (d / "bin").mkdir(parents=True, exist_ok=True)
    (d / "bin" / "claude").write_text(f'#!/bin/sh\necho $$ >> "{d}/launches"\nexec sleep 30\n', encoding="utf-8")
    (d / "bin" / "claude").chmod(0o755)
    (d / "sources.yml").write_text("[]\n", encoding="utf-8")
    (d / "state.json").write_text(json.dumps({"_pending": [{"url": f"https://a.example/p{i}", "title": "t", "brand": "cg",
                                                            "source_id": "s", "csr": False} for i in range(3)]}), encoding="utf-8")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import collect as m, pipelib\n"
            "from pathlib import Path; m.ROOT = pipelib.ROOT = Path(sys.argv[2]); m.STATE_PATH = m.ROOT / 'state.json'\n"
            "m.job_lock = lambda *a, **k: None; pipelib.prompt_file = lambda *a, **k: 'p'; m.save_raw = lambda *a, **k: None\n"
            "m.page_html_or_note = lambda url, csr: ('<p>本文</p>', ''); m.WATCH_BATCH = m.WATCH_PARALLEL = 1\n"
            "sys.argv = ['collect', '--no-git', '--skip-explore', '--skip-grok', '--date', '2099-01-01']\n"
            "try:\n    m.main()\nexcept m.Terminated:\n    sys.exit(143)     # collect の入口(__main__)と同じ扱い\n")
    env = {k: v for k, v in os.environ.items() if k != "INVOCATION_ID"}
    env["PATH"] = f"{d / 'bin'}:{os.environ.get('PATH', '')}"
    h = subprocess.Popen([sys.executable, "-c", code, str(Path(__file__).resolve().parent), str(d)], env=env, cwd=d,
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    ready = _wait_files(d / "launches")     # 1本目が起動して待っているあいだに送る(待っている担当は時間切れまで返らない)
    h.terminate()
    try:
        _, err = h.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        h.kill()
        _, err = h.communicate()
    pids = [int(x) for x in (d / "launches").read_text().split()] if (d / "launches").exists() else []
    left = [q for q in pids if _pid_alive(q)]
    for q in left:
        os.kill(q, signal.SIGKILL)
    check(ready and h.returncode == 143 and len(pids) == 1 and not left,
          f"収集の SIGTERM のあと、待っていたバッチが CLI を起動した・止めずに終わった(起動 {len(pids)}本 / 残った {left} / "
          f"終了 {h.returncode} / {(err or '').strip()[-200:]})")
    # 依頼文を書くあいだに収集の締切を過ぎた facts 化: 起動せず、読めなかった回数にも数えない
    saved_c = (getattr(collect, "DEADLINE", None), pipelib.prompt_file, collect.save_raw, collect.notify, collect.page_html_or_note)
    launched: list = []

    def slow_prompt(*a, **k):
        _t.sleep(0.1)
        return "p"
    try:
        collect.DEADLINE = _t.time() + 0.05
        pipelib.prompt_file = slow_prompt
        collect.save_raw = collect.notify = lambda *a, **k: None
        collect.page_html_or_note = lambda url, csr: ("<p>本文</p>", "")
        subprocess.Popen = lambda argv, **kw: launched.append(argv) or real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kw)
        state = {"_unreadable": {"https://a.example/u1": 1}}
        _, tried = collect.facts_batch([{"url": "https://a.example/u1", "title": "t", "brand": "cg", "csr": False}],
                                       collect.claude_exec, state)
        check(not launched and not tried and state.get("_unreadable") == {"https://a.example/u1": 1} and not state.get("_given_up"),
              f"依頼文を書くあいだに収集の締切を過ぎた facts 化を起動した・読めなかった回数に数えた(起動 {len(launched)}本 / {state})")
    finally:
        subprocess.Popen = real_popen
        (collect.DEADLINE, pipelib.prompt_file, collect.save_raw, collect.notify, collect.page_html_or_note) = saved_c
    # 正常に終わったあとの後始末の途中の中断: 残った道具を忘れずに止めてから上げる
    if start is not None:
        pf = tmp / "after.pid"
        code = ("import os, sys, time\nfrom pathlib import Path\n"
                "if os.fork() == 0:\n    os.setsid(); os.close(1); os.close(2)\n"
                f"    Path({str(pf) + '.tmp'!r}).write_text(str(os.getpid())); os.replace({str(pf) + '.tmp'!r}, {str(pf)!r})\n"
                "    time.sleep(30); os._exit(0)\n"
                f"while not Path({str(pf)!r}).exists(): time.sleep(0.005)\n")
        p = start([sys.executable, "-c", code], cwd=tmp)
        real_stop, n = pipelib.stop_tree, []

        def interrupted_once(*a, **k):
            n.append(1)
            if len(n) == 1:
                raise KeyboardInterrupt
            return real_stop(*a, **k)
        pipelib.stop_tree = interrupted_once
        try:
            pipelib.finish_group(p, 30)
            got = "返った"
        except KeyboardInterrupt:
            got = "中断"
        finally:
            pipelib.stop_tree = real_stop
        pid = int(pf.read_text()) if pf.exists() else 0
        alive = bool(pid) and _pid_alive(pid)
        if alive:
            os.kill(pid, signal.SIGKILL)
        check(got == "中断" and pid and not alive, f"後始末の途中の中断で、残った道具を止めずに忘れた({got} / pid {pid} 生存 {alive})")
        # 時間切れの回収の途中の中断(Ctrl-C)でも、凍らせた子を残さず、一覧からも漏らさない
        d2 = tmp / "timeout-int"
        d2.mkdir(parents=True, exist_ok=True)
        b2 = _probe_bin(tmp / "timeout-int-bin")
        saved_probe = os.environ.get("SESSION_PROBE")
        os.environ["SESSION_PROBE"] = str(d2)
        try:
            p = start([str(b2 / "claude")], cwd=tmp)
            ready = _wait_files(d2 / "group.pid", d2 / "escaped.pid")
            n.clear()
            pipelib.stop_tree = interrupted_once
            try:
                pipelib.finish_group(p, 0.2)
                got = "返った"
            except KeyboardInterrupt:
                got = "中断"
            finally:
                pipelib.stop_tree = real_stop
        finally:
            if saved_probe is None:
                os.environ.pop("SESSION_PROBE", None)
            else:
                os.environ["SESSION_PROBE"] = saved_probe
        left = []
        for name in ("group", "escaped"):
            q = int((d2 / f"{name}.pid").read_text()) if (d2 / f"{name}.pid").exists() else 0
            if q and _pid_alive(q):
                left.append(name)
                os.kill(q, signal.SIGKILL)
        kept = p in getattr(pipelib, "_SESSIONS", set())
        if kept:
            pipelib.stop_groups([p])
        check(ready and got == "中断" and not left and p.poll() is not None and not kept,
              f"時間切れの回収の途中の中断で、子を残した・一覧から漏らさない(準備 {ready} / {got} / 残った {left} / 一覧に残る {kept})")
    # この process の子でない対象(ほかの親が起こした独立したセッション)は、グループも含めて止めない
    pf = tmp / "foreign.pid"
    helper = real_popen([sys.executable, "-c",
                         "import os, subprocess, sys, time\n"
                         "q = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)\n"
                         f"open({str(pf) + '.tmp'!r}, 'w').write(str(q.pid)); os.replace({str(pf) + '.tmp'!r}, {str(pf)!r})\n"
                         "time.sleep(30)\n"])
    try:
        fpid = int(pf.read_text()) if _wait_files(pf) else 0

        class Foreign:
            pid, returncode = fpid, None

            def kill(self):
                pass

            def communicate(self, timeout=None):
                return "", ""
        class Reaped(Foreign):          # 回収済みの頭(番号がほかの親のグループに使われている)・印が違う
            returncode, session_mark = 0, "0" * 32
        if fpid:
            pipelib.reap(Foreign(), grace=1)
            pipelib.reap(Reaped(), grace=1)
            pipelib.stop_tree(fpid, mark="0" * 32)       # 正常に終わったあとの後始末と同じ呼び方
        check(fpid and _pid_alive(fpid), f"この process の子でない対象・回収済みの頭の番号(pid {fpid})のグループを止めた")
    finally:
        if pf.exists():
            try:
                os.kill(int(pf.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        helper.kill()
        helper.wait()


def test_collect_handover_deadline(tmp: Path):
    """収集は組版へ引き渡す締切を持つ(監査指摘 2026-10-14: Grok の面を順に待つたびに GROK_TIMEOUT を与え直し、応答しない面が
    並ぶと1波で 3×GROK_TIMEOUT になって、02:00 の収集が排他を持ったまま 03:00 の組版を妨げた)。
    - Grok の1波は起動時に決めた絶対の締切で回収する(待ちが面の数だけ足し算にならない)
    - 定時の収集の締切は次の定時工程の15分前(02:00 の収集なら 02:45)。手で回したとき・当番の取り直しには付けない
    - 締切を越えて待たず、過ぎたら Grok・確かめ・探索・定点観測の facts 化を起動しない(定点観測の新着は読めなかった回数に数えない)"""
    import datetime as _dt
    import time as _t
    import collect
    tmp.mkdir(parents=True, exist_ok=True)
    waits: list = []
    launched: list = []

    class GP:      # 応答しない Grok(時間切れまで返らない。止められたら空で返る)
        pid, returncode = _NO_PID, None

        def __init__(self):
            self.killed = False

        def communicate(self, timeout=None):
            if self.killed:
                return "", ""
            waits.append(timeout)
            _t.sleep(min(timeout or 0, 1))
            raise subprocess.TimeoutExpired("grok", timeout)

        def kill(self):
            self.killed, self.returncode = True, -9

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode
    names = ("GROK_TIMEOUT", "GROK_WAVE", "save_raw", "notify", "prompt_file", "explore_workdir", "page_html_or_note", "DEADLINE",
             "list_source", "render_page")
    saved = {n: getattr(collect, n, None) for n in names}
    saved_popen, saved_inv = subprocess.Popen, os.environ.get("INVOCATION_ID")
    notes: list = []
    try:
        collect.GROK_TIMEOUT, collect.GROK_WAVE = 0.3, 3
        collect.save_raw = lambda *a, **k: None
        collect.notify = lambda job, msg, ok=True, require=False: notes.append(msg) or True
        collect.prompt_file = lambda *a, **k: "p"
        collect.explore_workdir = lambda key: (tmp / "wd" / key).mkdir(parents=True, exist_ok=True) or tmp / "wd" / key
        collect.page_html_or_note = lambda url, csr: ("<p>本文</p>", "")
        collect.DEADLINE = None
        subprocess.Popen = lambda *a, **k: launched.append(a) or GP()
        targets = [({"key": f"f{i}"}, tmp / f"prompt-f{i}.md") for i in range(3)]
        failed = collect.run_grok_prompts(targets, {})
        check(failed == {"f0", "f1", "f2"} and sum(waits) <= 0.45,
              f"Grok の1波の待ちが面ごとに足し算になった(待った秒 {waits}。上限は GROK_TIMEOUT 0.3 秒)")
        # 収集の締切が先なら、そこまでしか待たない
        waits.clear()
        collect.GROK_TIMEOUT = 100
        collect.DEADLINE = _t.time() + 0.2
        collect.run_grok_prompts(targets[:1], {})
        check(waits and waits[0] <= 0.25, f"収集の締切を越えて Grok を待った(待った秒 {waits})")
        # 締切を過ぎたら起動しない
        launched.clear()
        collect.DEADLINE = _t.time() - 1
        errs: dict = {}
        failed = collect.run_grok_prompts(targets, errs)
        check(not launched and failed == {"f0", "f1", "f2"} and all("締切" in errs.get(k, "") for k in failed),
              f"Grok: 締切後に面を起動した・理由を残さない(起動 {len(launched)}本 / {errs})")
        # 波の途中で締切を過ぎたら、同じ波の残りの面も起動しない(1本目の起動に時間が掛かった)
        def slow_first(*a, **k):
            launched.append(a)
            if len(launched) == 1:
                _t.sleep(0.2)
            return GP()
        subprocess.Popen = slow_first
        collect.DEADLINE = _t.time() + 0.15
        errs = {}
        failed = collect.run_grok_prompts(targets, errs)
        check(len(launched) == 1 and failed == {"f0", "f1", "f2"} and all("締切" in errs.get(k, "") for k in ("f1", "f2")),
              f"Grok: 波の途中で締切を過ぎたのに、同じ波の残りの面を起動した(起動 {len(launched)}本 / {errs})")
        launched.clear()
        subprocess.Popen = lambda *a, **k: launched.append(a) or GP()
        collect.DEADLINE = _t.time() - 1
        od = tmp / "grok"
        od.mkdir(exist_ok=True)
        (od / "cg.md").write_text("### 1\n- url: https://x.com/a/status/9\n- 本文: 告知\n", encoding="utf-8")
        items, _ = collect.verify_grok_faces([{"key": "cg", "brand": "cg", "topic": "t"}], od)
        check(not launched and not items and any("status/9" in m and "締切" in m for m in notes),
              f"確かめ: 締切後に起動した・失う投稿を理由付きで名指ししない(起動 {len(launched)}本 / {notes[-1:]})")
        items, per = collect.run_explores(False, True)
        check(not launched and per and all(k.startswith("explore_failed:") for k in per),
              f"探索: 締切後に起動した・取得の失敗として残さない(起動 {len(launched)}本 / {per})")
        called: list = []
        state: dict = {}
        got, tried = collect.facts_batch([{"url": "https://a.example/n1", "title": "t", "brand": "cg", "csr": False}],
                                         lambda prompt, timeout=0: called.append(1) or [], state)
        check(not called and not tried and not state.get("_unreadable"),
              f"定点観測: 締切後に facts 化を起動した・読めなかった回数に数えた({called} {tried} {state})")
        # 定点観測: 締切を過ぎたら、一覧も新着の本文も取らない(監査指摘 2026-10-14: 締切後に新着48件の描画を起動して、組版の排他を妨げた)
        listed, fetched = [], []
        collect.list_source = lambda s, known, fetch=None: listed.append(s["id"]) or ([("https://a.example/n2", "t")], 1, False)
        collect.page_html_or_note = lambda url, csr: fetched.append(url) or ("", "")
        _, info = collect.run_watch(lambda prompt, timeout=0: called.append(1) or [])
        errs_w = [v.get("error", "") for k, v in ((info or {}).get("stats") or {}).items() if k != "_state" and isinstance(v, dict)]
        check(not listed and not fetched and not called and errs_w and all("締切" in e for e in errs_w),
              f"定点観測: 締切後に一覧・本文を取った・facts 化を起動した(一覧 {listed} / 本文 {fetched} / 起動 {len(called)})")
        # 締切の前でも、新着の本文の描画は収集の締切までしか待たない
        limits: list = []
        collect.render_page = lambda url, timeout=30, limit=None: limits.append(limit) or "<p>本文</p>"
        collect.DEADLINE = _t.time() + 5
        saved["page_html_or_note"]("https://a.example/n3", True)
        check(limits and isinstance(limits[0], (int, float)) and limits[0] <= 5.1,
              f"定点観測: 新着の本文の描画が収集の締切を越えて待つ(待つ上限 {limits})")
        # 少しずつ届く応答(1回の受信は速いが、全部で 2.5 秒)も、収集の締切で打ち切る。socket の待ち時間は1回の受信ごとにしか効かない
        class Drip:
            status = 200
            headers = type("H", (), {"get_content_charset": lambda self: "utf-8"})()

            def __init__(self):
                self.n = 0

            def read1(self, n=-1):
                _t.sleep(0.1)
                self.n += 1
                return b"x" if self.n < 25 else b""

            def read(self, n=-1):
                out = b""
                while (b := self.read1(n)):
                    out += b
                return out

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        saved_open = collect.urllib.request.urlopen
        collect.urllib.request.urlopen = lambda req, timeout=None: Drip()
        try:
            collect.DEADLINE = _t.time() + 0.3
            t0 = _t.time()
            _, why = saved["page_html_or_note"]("https://a.example/drip", False)
            took1 = _t.time() - t0
            collect.DEADLINE = _t.time() + 0.3
            t0 = _t.time()
            try:
                collect.watch_fetch({"type": "html"})("https://a.example/list")
                err2 = ""
            except Exception as e:      # noqa: BLE001
                err2 = str(e)
            took2 = _t.time() - t0
        finally:
            collect.urllib.request.urlopen = saved_open
        check(took1 < 1.5 and "締切" in why and took2 < 1.5 and "締切" in err2,
              f"定点観測: 少しずつ届く応答を、収集の締切を越えて読み続けた(本文 {took1:.1f}秒 {why!r} / 一覧 {took2:.1f}秒 {err2!r})")
        # 応答の下の受信(socket から1回読む)の待ち時間: 応答の形を本物(http.client の応答 → BufferedReader → raw.readinto →
        # socket)に合わせた偽物で確かめる。plan は受信ごとの (届くまでの秒, 中身)。待ち時間を越えれば時間切れ
        class Sock:
            def __init__(self, t):
                self.timeout = t

            def settimeout(self, t):
                self.timeout = t

            def gettimeout(self):
                return self.timeout

        class Raw:
            def __init__(self, plan, cap):
                self._sock, self.plan = Sock(cap), list(plan)

            def readinto(self, b):
                delay, data = self.plan.pop(0) if self.plan else (0, b"")
                t = self._sock.timeout
                if t is not None and delay > t:
                    _t.sleep(t)
                    raise TimeoutError("timed out")
                _t.sleep(delay)
                b[:len(data)] = data
                return len(data)

        class Wire(Drip):       # read1 は下の受信を per 回まとめて呼ぶ(chunked の長さの行を1字ずつ読むのと同じ形)
            def __init__(self, plan, cap, per=1):
                super().__init__()
                self.fp = type("F", (), {})()
                self.fp.raw, self.per = Raw(plan, cap), per

            def read1(self, n=-1):
                out = b""
                for _ in range(self.per):
                    buf = bytearray(64)
                    k = self.fp.raw.readinto(buf)
                    if not k:
                        break
                    out += bytes(buf[:k])
                return out

        def fetch_took(resp, deadline_in):
            collect.urllib.request.urlopen = lambda req, timeout=None: resp
            try:
                collect.DEADLINE = _t.time() + deadline_in
                t0 = _t.time()
                _, why = saved["page_html_or_note"]("https://a.example/wire", False)
                return _t.time() - t0, why
            finally:
                collect.urllib.request.urlopen = saved_open
        # 締切の直前に1回受信してから応答が止まっても、最初に決めた受信の待ち時間(20 秒)まで待たない
        took3, why3 = fetch_took(Wire([(0.25, b"x"), (20, b"")], 20.0), 0.3)
        # 締切が遠いときは、1回の受信の上限(urlopen が決めた待ち時間。ここでは 0.15 秒)を残り時間で延ばさない
        took4, why4 = fetch_took(Wire([(2.0, b"")], 0.15), 5)
        # 1回の read1 の中で何度も受信する応答(chunked の長さの行を少しずつ送る)も、受信のたびに締切で切る
        took5, why5 = fetch_took(Wire([(0.1, b"1")] * 25, 20.0, per=30), 0.3)
        check(took3 < 1.0 and "締切" in why3,
              f"定点観測: 締切の直前に止まった応答を、最初の受信の待ち時間まで待った({took3:.1f}秒 {why3!r})")
        check(took4 < 1.0 and "TimeoutError" in why4,
              f"定点観測: 1回の受信の上限を、収集の締切までの残り時間で延ばした({took4:.1f}秒 {why4!r})")
        check(took5 < 1.0 and "締切" in why5,
              f"定点観測: 1回の読み込みの中で何度も受信する応答を、締切を越えて読み続けた({took5:.1f}秒 {why5!r})")
        # ヘッダーが少しずつ届く(urlopen はヘッダーを読み終えるまで返らない)ときも、接続・ヘッダーを含めて締切までしか待たない
        def slow_headers(req, timeout=None):
            _t.sleep(2.5)
            return Wire([(0, b"")], 20.0)
        collect.urllib.request.urlopen = slow_headers
        try:
            collect.DEADLINE = _t.time() + 0.3
            t0 = _t.time()
            _, why6 = saved["page_html_or_note"]("https://a.example/headers", False)
            took6 = _t.time() - t0
            collect.DEADLINE = _t.time() + 0.3
            t0 = _t.time()
            try:
                collect.watch_fetch({"type": "html"})("https://a.example/list")
                err7 = ""
            except Exception as e:      # noqa: BLE001
                err7 = str(e)
            took7 = _t.time() - t0
        finally:
            collect.urllib.request.urlopen = saved_open
        check(took6 < 1.0 and "締切" in why6 and took7 < 1.0 and "締切" in err7,
              f"定点観測: 少しずつ届くヘッダーを、収集の締切を越えて待った(本文 {took6:.1f}秒 {why6!r} / 一覧 {took7:.1f}秒 {err7!r})")
        # 締切の決め方
        deadline_of = getattr(collect, "collect_deadline", None)
        if deadline_of is None:
            check(False, "収集の締切を決める関数(collect.collect_deadline)が無い")
        else:
            now = _dt.datetime(2026, 10, 14, 2, 0, tzinfo=pipelib.JST)
            os.environ.pop("INVOCATION_ID", None)
            check(deadline_of(False, now) is None, "手で回した収集に締切を付けた")
            os.environ["INVOCATION_ID"] = "test"
            want = _dt.datetime(2026, 10, 14, 2, 45, tzinfo=pipelib.JST).timestamp()
            check(deadline_of(False, now) == want, f"02:00 の定時の収集の締切が 02:45 でない: {deadline_of(False, now)}")
            check(deadline_of(True, now) is None, "当番の取り直し(当番が時刻を決めて止める)に収集の締切を付けた")
    finally:
        for n, v in saved.items():
            setattr(collect, n, v)
        subprocess.Popen = saved_popen
        if saved_inv is None:
            os.environ.pop("INVOCATION_ID", None)
        else:
            os.environ["INVOCATION_ID"] = saved_inv


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="imas-test-"))
    test_stray_governed_write_is_caught(tmp / "sgw")
    test_write_checked_contract(tmp / "wcc")
    test_release_push_retry(tmp / "rpr")
    test_release_squash_conflict(tmp / "rsc")
    test_release_dry_run_no_push(tmp / "rdr")
    test_deploy_index_failure(tmp / "dif")
    test_correction_append_only(tmp / "cao")
    test_indexer_tag_dictionary(tmp / "itd")
    test_remove_worktree(tmp / "rmw")
    test_run_codex_isolation(tmp / "rci")
    test_restore_files(tmp / "rsf")
    test_derive_write(tmp / "dw")
    test_compose_run_derive(tmp / "crd")
    test_oncall_collect_rerun_killed_classify(tmp / "ock")
    test_failed_rollback_blocks_commit(tmp / "frb")
    test_canonicalize_and_assign_ranks(tmp / "car2")
    test_reassemble_reranks_rewritten(tmp / "rerank")
    test_reassemble_restands_lead_after_revise_drop(tmp / "restand")
    test_rewrite_past_apply_answer(tmp / "rpa")
    test_slugify_format()
    test_escalate_is_module_global()
    test_anomaly_ledger_and_whywhy(tmp / "al")
    test_url_alive_verdict()
    test_reminder_event_date(tmp / "red")
    test_reservation_occurs_on(tmp / "roo")
    test_writer_children(tmp / "wch")
    test_restore_posts_skip(tmp / "rps")
    test_child_guard(tmp / "cg")
    test_child_tracker_edges(tmp / "cte")
    test_guarded_launch_seams(tmp / "gls")
    test_writer_reads_only_fetched_pages(tmp / "wrf")
    test_fetch_record_place_broken(tmp / "frb")
    test_restore_files_ledger(tmp / "rsfl")
    test_check_output()
    test_check_output_unread_pages()
    test_render_and_length()
    test_earliest_date()
    test_schema_hash_dates()
    test_revise_check()
    test_rollback(tmp / "rb")
    test_reservation_keeps_all_facts(tmp / "rkf")
    test_restore_reservation_facts(tmp / "rrf")
    test_reservation_keeps_article_sources(tmp / "rkas")
    test_companion_keeps_source_topic(tmp / "ckst")
    test_companion_topic_survives_second_reservation(tmp / "ctsr")
    test_grok_verify_keeps_new_announcement_separate()
    test_restore_chained_reservation_facts(tmp / "rcf")
    test_lost_post_notified_once(tmp / "lpn")
    test_grok_deep_retry(tmp / "gdr")
    test_review_failed_changed_article(tmp / "rfc")
    test_review_unchecked_scope_not_approved(tmp / "rus")
    test_leave_tree_clean_partial(tmp / "ltc")
    test_review_paper_defines_same_subject()
    test_official_topic_accepts_press()
    test_job_lock()
    test_job_lock_collect_oncall_handover()
    test_notify_require()
    test_oncall_rerun_policy()
    test_compose_carry_review()
    test_compose_carry_kept_paper()
    test_compose_recover(tmp / "recover")
    test_revise_apply_decline(tmp / "ra")
    test_notify_long()
    test_oncall_state(tmp / "st")
    test_oncall_restore_cleans_untracked()
    test_classify_consensus(tmp / "cs")
    test_table_write_and_reload(tmp / "tw")
    test_classify_posts_only(tmp / "cp")
    test_shared_service_units(tmp / "ssu")
    test_document_id_case(tmp / "dic")
    test_performer_domains(tmp / "pd")
    test_platform_owner_units(tmp / "pou")
    test_youtube_playlist_owner(tmp / "ytpl")
    test_booth_item_owner(tmp / "booth")
    test_classify_txn_interrupt(tmp / "cti")
    test_tags_and_idols(tmp / "ti")
    test_request_parsers(tmp / "rq")
    test_assemble_prompt_shape()
    test_ledger_facts_are_cited(tmp / "lfc")
    test_prompts_are_instructions_only()
    test_assemble_judge()
    test_claude_traced(tmp / "ct")
    test_time_budget()
    test_deadline_publish_boundary(tmp / "dpb")
    test_clean_url_and_table()
    test_x_anonymous_post_url()
    test_normalize_fixes_misnamed_brand()
    test_x_anonymous_post_in_posts(tmp / "xap")
    test_collect_classify_gate(tmp / "ccg")
    test_no_prompt_in_argv()
    test_next_number(tmp / "nn")
    test_dedupe_source_table(tmp / "dd")
    test_tool_path()
    test_oncall_undo_merge()
    test_oncall_ensure_edition(tmp / "ee")
    test_withdrawal(tmp / "wd")
    test_rewrites(tmp / "rw")
    test_compose_classifies_after_revise(tmp / "car")
    test_carry_late_candidates(tmp / "clc")
    test_carry_dropped_candidates(tmp / "cdc")
    test_carry_not_news_reason(tmp / "cnn")
    test_carry_reservation_deadline(tmp / "crd")
    test_carry_dropped_candidates_schema(tmp / "cdcs")
    test_late_facts_reach_plan_index(tmp / "lfpi")
    test_release_reads_latest_run(tmp / "rlr")
    test_compose_selection_from_cli(tmp / "csc")
    test_article_purpose()
    test_plan_merge_into_drop()
    test_extract_json_array_strict()
    test_update_clis(tmp / "uc")
    test_update_clis_smoke_answer(tmp / "ucs")
    test_git_commit_push_checkout(tmp / "gcp")
    test_grok_face_retry(tmp / "gr")
    test_grok_format_matches_parser()
    test_grok_official_paging_follows_prompt(tmp / "gop")
    test_grok_deep_format_matches_parser()
    test_collect_health()
    test_grok_roles(tmp / "gro")
    test_grok_item_url_repointed_to_post()
    test_grok_post_links_copied_to_facts()
    test_rebuild_backfills_post_links(tmp / "rbl")
    test_empty_answer_is_not_done(tmp / "ea")
    test_accept_only_normal_and_schema(tmp / "an")
    test_grok_repoint_uses_each_face_posts(tmp / "grp")
    test_watch_pagination_and_batches(tmp / "wp")
    test_watch_deadline_carryover(tmp / "wdc")
    test_watch_resume_after_cut(tmp / "wr")
    test_watch_body_cp932(tmp / "wc")
    test_watch_static_page_body(tmp / "ws")
    test_fetch_page_renders_whitespace_only_page()
    test_fetch_page_records_read_pages(tmp / "fpr")
    test_review_reads_sources_like_writer()
    test_fetch_rendered_skips_images()
    test_render_failure_keeps_cause(tmp / "rf")
    test_render_failure_reaches_classify_and_verify(tmp / "rfc")
    test_collect_oncall_rerun_exit(tmp / "cre")
    test_collect_settles_on_exception(tmp / "cse")
    test_collect_settles_on_sigterm(tmp / "cst")
    test_collect_state_write_is_atomic(tmp / "csa")
    test_oncall_collect_rerun_settles_broken_output(tmp / "ocb")
    test_storylink(tmp / "sl")
    test_storylink_related_prior(tmp / "slr")
    test_storylink_links_via_post_links(tmp / "slp")
    test_storylink_same_day_fact_urls(tmp / "sls")
    test_oncall_rollback_subprocess(tmp / "rs")
    test_oncall_apply_integrate()
    test_oncall_report_text(tmp / "rp")
    test_oncall_restore_on_exception(tmp / "oc")
    test_oncall_restore_to_backup(tmp / "rtb")
    test_oncall_merge_push_settles(tmp / "mps")
    test_oncall_fix_until_clean(tmp / "of")
    test_settled_drops_not_rewritten(tmp / "sd")
    test_rule_bench_cases()
    test_rule_bench_judges_shape(tmp / "rb")
    test_derive_ranking(tmp / "drk")
    test_assemble_digest_keeps_lead_within_cap()
    test_assemble_interrupted_writes(tmp / "aiw")
    test_assemble_checks_exit_codes(tmp / "ace")
    test_pending_keeps_later_editions(tmp / "pkl")
    test_session_timeout_stops_descendants(tmp / "sts")
    test_parallel_launch_failure_reaps(tmp / "plf")
    test_compose_no_launch_after_deadline(tmp / "nld")
    test_leave_tree_clean_stops_sessions(tmp / "ltcs")
    test_session_escaped_and_term(tmp / "set")
    test_session_signal_windows(tmp / "ssw")
    test_collect_handover_deadline(tmp / "chd")
    test_rule_bench_checks_read_pages(tmp / "rbp")
    test_bench_tools_check_read_pages(tmp / "btr")
    check(not STRAY_WRITES, "書式で決めたファイルを write_checked を通さずに書いた本番のコード(書く前に書式で検めること): "
          + "; ".join(sorted(set(STRAY_WRITES))[:10]))
    for f in FAILS:
        print(f"  [FAIL] {f}")
    print(f"test_pipeline: {len(FAILS)} failures")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
