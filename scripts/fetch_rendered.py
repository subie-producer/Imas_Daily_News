#!/usr/bin/env python3
"""ヘッドレスブラウザで JS レンダリング後のページを取得する(CSR サイトの定点観測用)。

Selenium Manager が Chrome for Testing と chromedriver を自動取得するため、
ブラウザの手動インストールは不要(初回のみダウンロードが走る)。

usage:
  .venv/bin/python scripts/fetch_rendered.py <URL> [--wait-css SELECTOR] [--timeout 20] [--out FILE]

--wait-css を指定すると、そのCSSセレクタが現れるまで待ってから page_source を出力する。
未指定時は document.readyState=complete 後さらに2秒待つ。
"""
import argparse
import json
import sys
import time
from pathlib import Path

# selenium は描画するときだけ読む(429 の見分け document_status を、selenium の無い環境のテストからも動かせるように)

UA =("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# ページ(文書そのもの)が HTTP 429 を返したときの終了コード(pipelib.RENDER_RATE_LIMITED と同じ値。selenium を読み込む前に pipelib を読まない)
RATE_LIMITED = 75


class RateLimited(Exception):
    """文書の応答が HTTP 429(2026-10-26: 描画した 429 の画面を本文として返していた。呼び出し側が Retry-After を守って描画し直す)。
    url は 429 を返した文書の URL(転送されたなら行き先。呼び出し側は、そのホストに控えを付ける)"""
    def __init__(self, retry_after: str, url: str = ""):
        super().__init__(f"HTTP 429 url={url or '-'} retry-after={retry_after or '-'}")
        self.retry_after = retry_after


def _events(entries):
    """性能ログ(performance)の1件ずつの (method, params)。読めない行は飛ばす。"""
    for e in entries or []:
        try:
            msg = json.loads(e.get("message") or "{}").get("message") or {}
        except (ValueError, AttributeError):
            continue
        yield msg.get("method"), msg.get("params") or {}


def _main_frame(entries) -> str | None:
    """主フレームの id(最初の文書の要求のフレーム)。分からなければ None。"""
    for method, p in _events(entries):
        if method == "Network.requestWillBeSent" and p.get("type") == "Document":
            return p.get("frameId")
    return None


def document_status(entries) -> tuple[int | None, str, str]:
    """性能ログから、主フレームの最後の文書(Document)の応答の (状態コード, Retry-After, 応答の URL)。見つからなければ (None, "", "")。
    最後の文書を見る(最初の 200 の文書が JS で別の文書へ移り、そこで 429 になるのを見落とさない)。iframe の文書は見ない(Sol 監査 R429 r5)"""
    main = _main_frame(entries)
    last = (None, "", "")
    for method, p in _events(entries):
        if method != "Network.responseReceived" or p.get("type") != "Document":
            continue
        if main is not None and p.get("frameId") not in (None, main):
            continue
        res = p.get("response") or {}
        heads = {str(k).lower(): str(v) for k, v in (res.get("headers") or {}).items()}
        status = res.get("status")
        last = ((int(status) if isinstance(status, (int, float)) else None), heads.get("retry-after", "").strip(), str(res.get("url") or ""))
    return last


def held_match(host: str, holds) -> str | None:
    """host が控えているホスト(holds のどれか、またはその下のホスト)なら、その控えのホスト名。"""
    host = (host or "").lower()
    for h in holds or ():
        if host == h or host.endswith("." + h):
            return h
    return None


def blocked_document(entries, holds) -> str:
    """主フレームの文書の要求が、控えているホスト(holds。中継 HoldProxy が差し止めたホスト)として差し止められたときの、その URL
    (転送の行き先を含む)。HTTPS は接続の失敗(Network.loadingFailed)、平文の HTTP は中継の印の付いた応答で見える。無ければ ""。"""
    import urllib.parse
    main = _main_frame(entries)
    urls: dict[str, tuple[str, str | None]] = {}
    for method, p in _events(entries):
        if p.get("type") != "Document":
            continue
        if method == "Network.requestWillBeSent":
            urls[p.get("requestId")] = (str((p.get("request") or {}).get("url") or ""), p.get("frameId"))   # 転送のたびに行き先で上書き
            continue
        if method == "Network.loadingFailed":
            u, frame = urls.get(p.get("requestId"), ("", None))
        elif method == "Network.responseReceived":
            res = p.get("response") or {}
            if not any(str(k).lower() == "x-imas-held" for k in (res.get("headers") or {})):
                continue
            u, frame = str(res.get("url") or ""), p.get("frameId")
        else:
            continue
        if u and (main is None or frame in (None, main)) and held_match(urllib.parse.urlsplit(u).hostname, holds):
            return u
    return ""


HELD_HEADER = b"X-Imas-Held: 429"      # 中継が控えているホストへの要求を差し止めた印(平文の HTTP の応答に付ける)


def proxy_request(head: bytes, held) -> tuple[str, int, bytes | None, float]:
    """中継が受けた要求の頭(要求行と見出し)から (ホスト, ポート, 送る頭, 控えの時刻)。held(host) は控えの時刻(無ければ 0)。
    CONNECT(HTTPS)は送る頭が None(中継は 200 を返して中身をそのまま通す)。平文の HTTP は要求行を origin-form にし、接続は1回の
    要求で閉じる(1つの接続で別のホストへの要求を続けて送らせない)。控えの時刻が今より後なら、中継は相手へ接続しない"""
    import urllib.parse
    line, _, rest = head.partition(b"\r\n")
    method, target, version = line.decode("latin-1").split(" ", 2)
    if method.upper() == "CONNECT":
        host, _, port = target.rpartition(":")
        host, port, out = host, int(port or 443), None
    else:
        u = urllib.parse.urlsplit(target)
        host, port = u.hostname or "", u.port or 80
        keep = [h for h in rest.split(b"\r\n") if h and h.split(b":", 1)[0].strip().lower()
                not in (b"connection", b"proxy-connection", b"keep-alive")]
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        out = f"{method} {path} {version}".encode("latin-1") + b"\r\n" + b"\r\n".join(keep + [b"Connection: close"]) + b"\r\n\r\n"
    host = host.strip("[]").lower()
    until = held(host) or 0.0
    return host, port, out, until if until > time.time() else 0.0


class HoldProxy:
    """ブラウザの接続をすべて通す中継(127.0.0.1)。接続のたびに、行き先のホストがいま 429 で控えているか(共有の控え。
    pipelib.held_until)を読み、控えていれば相手へ接続しない(要求を送らない)。描画を始めたあとでほかのプロセスが受けた 429 も、
    その接続の時点で守る(Sol 監査 R429 r6。起動時の一覧で名前解決を差し止めるだけでは、描画中の 429 を素通りした)。
    blocked は差し止めたホスト → 控えの時刻。
    確立したトンネルの中でも、ブラウザから相手へ送るたびに控えを読み直し、控えていれば接続を切る(TLS の接続の使い回しで、描画中に
    ほかのプロセスが受けた 429 を素通りしない。Sol 監査 R429 r7)。turn(host) は新しい接続の前に呼ぶ、ホストの順番(HOST_GAP の間隔)の
    待ち。暗号化されたトンネルの中の要求の区切りは中継から見えないので、間隔は接続ごとに取る"""
    def __init__(self, held, turn=None, listen: bool = True, connect=None, select_fn=None):
        """listen=False は待ち受けを開かない(テストが _handle を偽のソケットで直接呼ぶ。connect・select_fn も差し替える)"""
        import select
        import socket
        import threading
        self.held, self.turn, self.blocked = held, turn, {}
        self.connect = connect or socket.create_connection
        self.select = select_fn or select.select
        self.sock, self.port = None, 0
        if not listen:
            return
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        import threading
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, c) -> None:
        up = None
        try:
            c.settimeout(60)
            head = b""
            while b"\r\n\r\n" not in head:
                b = c.recv(65536)
                if not b or len(head) > 1 << 20:
                    return
                head += b
            head, _, extra = head.partition(b"\r\n\r\n")
            host, port, out, until = proxy_request(head + b"\r\n\r\n", self.held)
            if until:
                self.blocked[host] = until
                c.sendall(b"HTTP/1.1 403 Forbidden\r\n" + HELD_HEADER + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            if self.turn is not None:
                try:
                    self.turn(host)         # 同じホストへの接続の間隔(共有の予約)。締切を越えるなら接続しない
                except OSError:
                    c.sendall(b"HTTP/1.1 504 Gateway Timeout\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    return
                until = self.held(host) or 0.0      # 順番を待つあいだに受けた 429
                if until > time.time():
                    self.blocked[host] = until
                    c.sendall(b"HTTP/1.1 403 Forbidden\r\n" + HELD_HEADER + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    return
            up = self.connect((host, port), timeout=30)
            if out is None:
                c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                up.sendall(out)
            if extra:
                up.sendall(extra)
            socks = [c, up]
            while True:
                r, _, _ = self.select(socks, [], [], 60)
                if not r:
                    return
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    if s is c:
                        # ブラウザから相手へ送る前に控えを読み直す(使い回しのトンネルで、描画中に受けた 429 を素通りしない)
                        until = self.held(host) or 0.0
                        if until > time.time():
                            self.blocked[host] = until
                            return
                    (up if s is c else c).sendall(data)
        except Exception:       # noqa: BLE001  1つの接続の失敗は、その接続だけ閉じる(ブラウザには読み込みの失敗として見える)
            return
        finally:
            for s in (c, up):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass


def make_driver(proxy_port: int | None = None):
    """proxy_port があれば、ブラウザの接続(転送の行き先・ページの中の要求を含む)をすべてその中継(HoldProxy)に通す。
    127.0.0.1 の宛先も中継に通す(<-loopback>)"""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    if proxy_port:
        opts.add_argument(f"--proxy-server=http://127.0.0.1:{proxy_port}")
        opts.add_argument("--proxy-bypass-list=<-loopback>")
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,2400")
    opts.add_argument("--lang=ja-JP")
    opts.add_argument(f"--user-agent={UA}")
    # 自動操作の印を出さない。出すと Anime Store.JP などは Google のトップへ転送し、本文の代わりに検索ページを返す(2026-10-26)
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    # 画像は読み込まない。読むのは文字だけで、画像の多い一覧ページは画像の読み込みが終わらず読み込み完了を待つ所で
    # 時間切れになっていた(実測 2026-10-07: columbia.jp/idolmaster/ が 700件超の画像待ちで毎回 40 秒で落ちた)
    opts.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
    # 文書の応答の状態コードを読む(429 を見分ける)ため、性能ログを取る
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    return webdriver.Chrome(options=opts)


def check_documents(entries, holds: dict[str, float]) -> None:
    """ここまでの性能ログで、主フレームの文書が 429 か、控えているホストとして差し止められたなら RateLimited を上げる。"""
    import urllib.parse
    blocked = blocked_document(entries, holds)
    if blocked:
        until = holds.get(held_match(urllib.parse.urlsplit(blocked).hostname, holds) or "", time.time())   # 控えの残りを Retry-After にする
        raise RateLimited(f"{max(0.0, until - time.time()):.1f}", blocked)
    status, after, where = document_status(entries)
    if status == 429:
        raise RateLimited(after, where)


def fetch(url: str, wait_css: str | None, timeout: int, holds: dict[str, float] | None = None, deadline: float | None = None) -> str:
    """ブラウザの接続はすべて中継(HoldProxy)を通し、接続のたびに共有の控え(pipelib.held_until)と holds(起動時に渡された控え:
    ホスト → 控えの時刻)を読んで、控えているホストへは要求を送らない(描画の中の転送・描画中にほかのプロセスが受けた 429 も。
    Sol 監査 R429 r5・r6)。主フレームの文書を差し止めた・文書が 429 なら RateLimited"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pipelib import _host_reserve, held_until, site_host
    holds = dict(holds or {})
    deadline = deadline if deadline is not None else time.time() + timeout + 10     # 締切が無くても、読み込みの上限を越えて待たない

    def held(host: str) -> float:
        h = held_match(host, holds)
        return max(held_until(host), holds[h] if h else 0.0)

    def turn(host: str) -> None:
        """新しい接続の前の、ホストの順番(HOST_GAP の間隔。並列の取得と共有の予約)。deadline(描画の締切)を越える待ちは
        RateLimitWait(OSError)で、中継は接続しない"""
        gap = _host_reserve(site_host(f"//{host}"), 0.0, None if deadline is None else deadline - time.time())
        if gap > 0:
            time.sleep(gap)
    proxy = HoldProxy(held, turn)
    try:
        driver = make_driver(proxy.port)
    except Exception:
        proxy.close()
        raise
    logs: list = []

    def drain() -> None:
        try:
            logs.extend(driver.get_log("performance"))
        except Exception:       # noqa: BLE001  ログを取れないブラウザでは見分けない(従来どおり描画を返す)
            pass

    def check() -> None:
        check_documents(logs, {**holds, **proxy.blocked})
    try:
        driver.set_page_load_timeout(timeout + 10)
        try:
            driver.get(url)
        except Exception:
            # 読み込みが失敗・時間切れでも、差し止めた・429 だったならそちらを返す
            drain()
            check()
            raise
        drain()
        check()
        if wait_css:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_css))
            )
        else:
            WebDriverWait(driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(2)  # CSR の描画待ち
        # 描画を待つあいだに JS で別の文書へ移って 429 になった・差し止めたなら、その画面を本文にしない
        drain()
        check()
        return driver.page_source
    finally:
        driver.quit()
        proxy.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--wait-css", default=None)
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--out", default=None)
    ap.add_argument("--hold", action="append", default=[], help="429 で控えているホスト=控えの時刻(この描画では要求を送らない)")
    ap.add_argument("--deadline", type=float, default=None, help="描画の締切(時刻)。接続の順番の待ちはこれを越えない")
    args = ap.parse_args()
    holds = {}
    for h in args.hold:
        name, _, t = h.partition("=")
        try:
            holds[name.strip().lower()] = float(t)
        except ValueError:
            continue
    try:
        html = fetch(args.url, args.wait_css, args.timeout, holds, args.deadline)
    except RateLimited as e:
        print(f"RENDER_FAILED: {e}", file=sys.stderr)
        return RATE_LIMITED
    except Exception as e:
        # 呼び出し側(pipelib.render_page)が原因を記録できるよう、例外の要点を1行で出す。Selenium の例外は
        # 後ろに Chrome のスタックが続き、トレースバックの末尾だけでは何が起きたか読めない
        msg = (str(e).strip().splitlines() or [""])[0].removeprefix("Message:").strip()
        print(f"RENDER_FAILED: {type(e).__name__}: {msg}"[:300], file=sys.stderr)
        return 1
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"{len(html)} bytes -> {args.out}", file=sys.stderr)
    else:
        print(html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
