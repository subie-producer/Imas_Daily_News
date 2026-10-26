#!/usr/bin/env python3
"""fetch_page: 出典ページの本文を**要約せずに**取り出す(執筆時の出典照合用)。

  python3 scripts/fetch_page.py <URL> [--chars 40000]

執筆セッション(codex)がこれを実行して出典を読み直す。LLM の要約器を通さないことが
このスクリプトの存在理由:

- WebFetch のような「小さいモデルで要約してから渡す」経路だと、ラベルが落ちる。
  実際 2026-08-26号の出典ページには「受付期間 8/8〜8/24」「当落発表 8/26」
  「入金期間 8/26〜8/30」が並んでいたのに、要約経由では期間が1つも残らなかった。
- 当選者向けの入金期間が「販売期間」に化け、誰でも買える期間として報じられた。

先頭に、機械抽出したラベル付き期間(編集規程15)を出す。ここが本文と食い違うことは
ないので、期間はこのブロックを正とする。

**既定の出力上限は大きく取る。**6000字にしていたとき、本文8,219字のページが
末尾(出演者一覧・注意事項)ごと切れていた。切るくらいなら全部渡す。
1ページの本文はたいてい数千字で、読ませても執筆1回ぶんの負担は小さい。

本文を取れたページは、環境変数 IMAS_FETCH_RECORD の記録に残す(pipelib.note_fetched)。執筆が根拠にできるのは
素材のページと、この記録にあるページだけ(検算 renderlib.check_output が見る。校閲もこのスクリプトだけで出典を読む)。

読んだ時点の原本を残す(page_archive。編集長 2026-10-26「収集するタイミングでこういう記事だった(HTMLぺたー)すりゃいいんだよ」)。
保存のあるページも必ず相手のサイトから読み直す(誤記載の訂正・出演者の追加・締切の延長を捉える):
- 読めて同じ → 「確認済み」と読み直した時刻を添える
- 読めて変わった → いまの版を残し、収集時点の版との違いを本文の前に出す(記録 .changed にも残す)
- 読めない(メンテナンス・5xx・429・時間切れ) → 保存の原本を取得時刻つきで返す(読めたページに数える。記録に archived)
- 404・410 → 取り下げ・移動の可能性として FETCH_FAILED にし、保存の原本は参考として見せる(読めたページに数えない)
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import (PAGE_READ_BYTES, RENDER_FINAL, RENDER_STATUS, RenderFailed, browser_only, extract_periods, html_to_text, http_status,
                     maintenance_http_error, maintenance_redirect, maintenance_screen, needs_render, note_fetched, polite_urlopen, render_page)
import page_archive

UA = "Mozilla/5.0 (compatible; ImasNewsCollect/1.0)"
FIXED_PAGES_ENV = "IMAS_FETCH_PAGES"
FINAL_URL: dict[str, str] = {}     # 頼んだ URL → リダイレクトの行き先(本文を取れたページの記録に両方を残す。執筆は行き先を出典に書くことがある)
FETCH_HTML: dict[str, bytes] = {}  # 頼んだ URL → 取った HTML(原本の保存に使う)
FETCH_STATUS: dict[str, int] = {}  # 頼んだ URL → 素の取得の状態コード(原本の保存に使う)


class Maintenance(Exception):
    """サイトが転送せずに 5xx でメンテナンスの画面を返した(2026-10-23: ランティスの 503「現在メンテナンス中です」)"""
    def __init__(self, url: str, code: int):
        super().__init__(f"{url} が HTTP {code} でメンテナンスの画面を返した")
        self.url = url


def fetch(url: str) -> str:
    # 素の取得に 429 を返すサイトは、素の取得をせずに最初からブラウザで読む(2026-10-26: Anime Store.JP が数回の取得で 429)
    if browser_only(url):
        return ""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        # 同じホストへは間隔をあけ、429 は Retry-After を守って取り直す(上限を超えたら「429 で読めなかった」の HTTPError)
        res = polite_urlopen(req, timeout=25)
    except urllib.error.HTTPError as e:
        if maintenance_http_error(e):
            raise Maintenance(e.geturl() or url, e.code) from None
        raise
    with res:
        if res.status >= 400:
            return ""
        FINAL_URL[url] = res.geturl()
        FETCH_STATUS[url] = res.status      # 原本に残す状態コード
        raw = res.read(PAGE_READ_BYTES)
        FETCH_HTML[url] = raw       # 原本の保存(page_archive)に HTML も残す
        return html_to_text(raw, res.headers.get_content_charset())


def fixed_page(pages_file: str, url: str) -> str:
    """規則の事例(scripts/rule_bench.py)が固定で持たせた出典の本文({url: 本文} の JSON)。事例に無いページは空(= 取れない)。
    事例は毎回ページを取りに行かず、事例の外のページも読ませない(2026-10-14 監査指摘: 依頼文の規則を固定の事例でモデルに判断させる)"""
    pages = json.loads(Path(pages_file).read_text(encoding="utf-8"))
    for u in (url, url.rstrip("/"), url.rstrip("/") + "/"):
        if u in pages:
            return pages[u]
    return ""


def main() -> int:
    from pipelib import exit_on_term
    exit_on_term()      # SIGTERM(道具の時間切れ)でも、描画のブラウザを子孫ごと止めてから終わる
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--chars", type=int, default=40000, help="本文の出力上限")
    args = ap.parse_args()

    fixed = os.environ.get(FIXED_PAGES_ENV, "")
    # 保存の原本(固定の本文で走る事例は使わない)。置き場を読めなくても読み直しは続ける
    base = None if fixed else archived(page_archive.baseline, args.url)
    live = read_live(args.url, fixed)
    if live["kind"] == "ok" and base and live.get("render_error") and page_archive.text_sha(live["text"]) != base.get("sha"):
        # いまの読み直しは描画が要るのに描画できなかった(素の本文はナビだけのことがある)。原本の描画の有無にかかわらず、
        # 素の本文で原本を置き換えず・変化として見せず、読み直せなかったとして原本を返す(Sol 監査 r7)
        why = f"描画できず、素の本文だけでは原本と照らせない({live['render_error'][:200]})"
        live = {"kind": "failed", "final": live.get("final"), "why": why, "msg": f"FETCH_FAILED: {why}"}
    if (live["kind"] == "ok" and base and base.get("rendered") and not live.get("rendered")
            and page_archive.text_sha(live["text"]) != base.get("sha")):
        # 原本は描画して読んだ本文で、いまは素の本文だけ。違いは描画の有無かもしれないので、描画して照らし直す
        # (描画しないと、素の本文に無い価格・日程を「消えた」と見せる)
        if not live.get("render_error"):
            live = rendered_live(args.url, live)
        if live["kind"] == "ok" and not live.get("rendered"):
            # 描画できなかった: 素の本文だけでは原本と照らせない。読み直せなかったとして原本を返す(Sol 監査 r2)
            why = f"描画できず、素の本文だけでは原本と照らせない({live.get('render_error', '')[:200]})"
            live = {"kind": "failed", "final": live.get("final"), "why": why, "msg": f"FETCH_FAILED: {why}"}
    # 読めなかったことは隣の記録に残す(組版が、原本を読めないまま書いた記事に断り書きを付けるのに使う)。
    # メンテナンスでない失敗(404・表示の無い 5xx・時間切れ 等)も記録する。同じホストの別のページがメンテナンスでも、
    # このページに断り書きを付けない(Sol 監査 Z2 r1)。転送先で失敗したら(/old → /gone → 404)転送先も残す(Sol 監査 Z2 r5)
    if live["kind"] == "maintenance":
        note_fetched(args.url, live.get("final"), maintenance=True)
    elif live["kind"] != "ok":
        note_fetched(args.url, live.get("final"), failed=True, http=live.get("http"))
    if live["kind"] != "ok":
        if base and live["kind"] != "gone":
            # 読み直せない(メンテナンス・5xx・429・時間切れ)。保存の原本で書ける(読めたページに数え、組版は断り書きを付ける)
            note_fetched(args.url, base.get("final"), archived=page_archive.fetched_at(base), via=base.get("via"),
                         why="maintenance" if live["kind"] == "maintenance" else "failed")
            emit(args.url, base.get("final"), base["text"], args.chars,
                 f"原本の照合: いまは読み直せない({live['why']})。下の本文は{when(base)}に読んだ原本。出典として使ってよい")
            return 0
        print(live["msg"])
        if base:
            # 前はあったページが無い: 取り下げ・移動の可能性。保存の原本は参考に見せるが、読めたページには数えない
            print(f"収集時点の原本の照合: {when(base)}にはこのページの本文があった。いまは無い(取り下げ・移動の可能性)。"
                  "下の原本だけを根拠に書かない")
            print(f"--- {when(base)}の原本(参考。出典にしない) ---")
            print(page_archive.norm_text(base["text"])[:args.chars])
        return 1
    text, final = live["text"], live.get("final")
    # 本文を取れたページを、執筆・書き直しのセッションの記録に残す(検算は、根拠の URL が素材かこの記録にあるかを見る)
    note_fetched(args.url, final)
    note = None
    if not fixed:
        saved = keep(args.url, text, live.get("html"), final, bool(live.get("rendered")), live.get("status") or 200)
        if base and page_archive.text_sha(text) == base.get("sha"):
            note = f"原本の照合: {when(base)}の原本と同じ内容を、いま({page_archive.now_iso()[:16].replace('T', ' ')})読み直して確かめた"
        elif base:
            # 内容が変わった(誤記載の訂正・出演者の追加・締切の延長 など)。いまの版を正として、違いを見せる
            note = (f"原本の照合: {when(base)}の原本から内容が変わっている(いま {page_archive.now_iso()[:16].replace('T', ' ')} に読み直した)。"
                    f"下の本文はいまの版。違いは「--- 変わったところ ---」(- が{when(base)}の版、+ がいまの版)\n"
                    "--- 変わったところ ---\n" + page_archive.diff(base["text"], text))
            new = saved or {"at": page_archive.now_iso(), "sha": page_archive.text_sha(text)}
            note_fetched(args.url, final, changed={"from_at": page_archive.fetched_at(base), "to_at": new.get("at")})
            try:
                page_archive.note_change(args.url, final, base, new)
            except Exception as e:      # noqa: BLE001  台帳を書けなくても読み取りは止めない
                print(f"PAGE_ARCHIVE_FAILED: {type(e).__name__}: {e}", file=sys.stderr)
    emit(args.url, final, text, args.chars, note)
    return 0


GONE_CODES = (404, 410)       # ページが無い(取り下げ・移動の可能性)


def read_live(url: str, fixed: str = "") -> dict:
    """相手のサイトから読み直す。戻りは {"kind": ok / maintenance / gone / failed, "text", "final", "html", "why", "msg"}。
    msg は読めなかったときに出す FETCH_FAILED の行(保存の原本が無いときはこれまでどおりの文面)。"""
    try:
        text = fixed_page(fixed, url) if fixed else fetch(url)
    except Maintenance as e:
        return {"kind": "maintenance", "final": e.url, "why": f"メンテナンス中({e})",
                "msg": f"FETCH_FAILED: メンテナンス中({e})。このページの本文は今は読めない"}
    except Exception as e:
        http = isinstance(e, urllib.error.HTTPError)
        # 状態コードも記録に残す(429 は取り直しの上限を超えても読めなかった印。相手の停止ではない)
        return {"kind": "gone" if http and e.code in GONE_CODES else "failed", "final": e.geturl() if http else None,
                "why": f"{type(e).__name__}: {e}"[:300], "msg": f"FETCH_FAILED: {type(e).__name__}: {e}", "http": http_status(e)}
    final = FINAL_URL.get(url)
    # メンテナンス画面へ転送されたら、その案内はこのページの本文ではない。読めたことにしない
    if maintenance_redirect(url, final):
        return {"kind": "maintenance", "final": final, "why": f"メンテナンス中({final} へ転送された)",
                "msg": f"FETCH_FAILED: メンテナンス中({final} へ転送された)。このページの本文は今は読めない"}

    # JS で描画するページは本文が空同然になる。その場合と、描画必須と分かっているサイトは描画してから読み直す
    # (固定の本文は描画したあとのもの。取りに行かない)
    live = {"kind": "ok", "text": text, "final": final, "html": FETCH_HTML.get(url), "rendered": False,
            "status": FETCH_STATUS.get(url) or 200}
    if not fixed and needs_render(url, text):
        live = rendered_live(url, live)
    if live["kind"] == "ok" and not live["text"].strip():
        why = "本文を取得できませんでした" + (f"({live['render_error']})" if live.get("render_error") else "")
        return {"kind": "failed", "final": final, "why": why, "msg": f"FETCH_FAILED: {why}", "http": live.get("render_status")}
    return live


def rendered_live(url: str, live: dict) -> dict:
    """素の本文で読めたページ(live)を描画して読み直す。描画できなければ素の本文のまま(原因は render_error と stderr)。
    描画で JS がメンテナンス画面へ転送したら、読めなかった(maintenance)にする"""
    try:
        rendered = render_page(url, 30)
    except RenderFailed as e:
        # 素の本文で続けるが、描画できなかった原因(終了コードと stderr の要点)は読み手に見せる
        print(f"RENDER_FAILED: {e}", file=sys.stderr)
        return {**live, "render_error": str(e), "render_status": e.status}
    final = RENDER_FINAL.get(url)
    if maintenance_redirect(url, final):
        return {"kind": "maintenance", "final": final, "why": f"メンテナンス中({final} へ転送された)",
                "msg": f"FETCH_FAILED: メンテナンス中({final} へ転送された)。このページの本文は今は読めない"}
    # 描画した文書が HTTP のエラー(404・410・5xx・429)なら、その画面は本文ではない(素の取得と同じ扱い。Sol 監査 r8)
    st = RENDER_STATUS.get(url) or 0
    if st >= 400:
        if maintenance_screen(st, rendered.encode("utf-8", "replace")):
            return {"kind": "maintenance", "final": final, "why": f"メンテナンス中(描画したページが HTTP {st} でメンテナンスの画面)",
                    "msg": f"FETCH_FAILED: メンテナンス中(描画したページが HTTP {st} でメンテナンスの画面)。このページの本文は今は読めない"}
        why = f"描画したページが HTTP {st}"
        return {"kind": "gone" if st in GONE_CODES else "failed", "final": final, "why": why, "msg": f"FETCH_FAILED: {why}"}
    rtext = html_to_text(rendered.encode("utf-8", "replace"))
    if not rtext.strip():
        # 枠だけで本文の無い描画は、描画できなかったのと同じ(原本があれば原本を返す。Sol 監査 r9)
        print("RENDER_FAILED: 描画した本文が空", file=sys.stderr)
        return {**live, "render_error": "描画した本文が空"}
    # 描画で JS が別のページへ移ったら、その行き先を転送先として出す・記録する・原本を引けるようにする(Sol 監査 r3)
    return {**live, "text": rtext, "html": rendered, "rendered": True,
            "final": final if final and final != url else live.get("final"), "status": st or 200}


def archived(get, url: str) -> dict | None:
    """保存の原本(page_archive の get で引く)。置き場を読めなければ None(読み直しは続ける)。"""
    try:
        return get(url)
    except Exception as e:      # noqa: BLE001
        print(f"PAGE_ARCHIVE_FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def keep(url: str, text: str, html, final: str | None, rendered: bool = False, status: int = 200) -> dict | None:
    """読めたページを原本として残す(転送先の URL でも引けるようにする)。書けない置き場(読み取り専用のセッション)でも止めない。"""
    try:
        got = page_archive.save(url, text, html=html, final=final, status=status, via="read", rendered=rendered)
        if final and final != url and not maintenance_redirect(url, final):
            page_archive.save(final, text, html=html, final=final, status=status, via="read", rendered=rendered)
        return got
    except Exception as e:      # noqa: BLE001
        print(f"PAGE_ARCHIVE_FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def when(v: dict) -> str:
    """版をいつ・誰が読んだか(「収集時点(2026-10-25 21:03)」/「2026-10-26 04:30」)。"""
    at = page_archive.fetched_at(v)[:16].replace("T", " ")
    return f"収集時点({at})" if v.get("via") == "collect" else at


def emit(url: str, final: str | None, text: str, chars: int, note: str | None = None) -> None:
    """読んだ本文を出す(URL・転送先・原本の照合・期間・本文)。"""
    periods = extract_periods(text)
    print(f"URL: {url}")
    # 転送された(短縮 URL など)ときは行き先も見せる。見せないと、執筆は出典に書く行き先の url を本文から推して組み立て、
    # 読んでいない url を書く(2026-10-23: t.co の行き先 …_whanm_1023 を …_whanm_10231023 と書き、差し戻しても直らず記事が落ちた)
    if final and final != url:
        print(f"転送先 URL: {final}")
    if note:
        print(note)
    if periods:
        print("--- 期間(原文ラベルのまま。本文と食い違う場合はこちらが正) ---")
        for p in periods:
            print(f"  {p}")
    else:
        print("--- 期間: ラベル付きの期間は見つかりませんでした ---")
    print("--- 本文(要約なし) ---")
    # 空行の連なりは1つに詰める。描画した公式ポータルの告知はタイトルと本文の間に百行を超える空行があり、
    # `| head` のように行数で切って読むと本文が見えずタイトルだけになる(2026-10-13 当番の再現: IM＠S RADIO 告知)
    print(page_archive.norm_text(text)[:chars])


if __name__ == "__main__":
    sys.exit(main())
