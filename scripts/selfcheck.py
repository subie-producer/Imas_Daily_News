#!/usr/bin/env python3
"""selfcheck: パイプラインのスクリプトが「呼べる状態か」を静的に検査する。

  python3 scripts/selfcheck.py

編集を重ねるうちに、関数の定義と呼び出しが食い違ったまま commit される事故が
続いた(いずれも実行時にしか露見せず、定時実行を1回まるごと潰した):

- assign_ranks を定義したまま main() から呼ぶ行を落とし、rank が付かないまま
  release が lint エラー11件で停止(2026-08-28)
- write_articles(reuse=...) の呼び出しだけ残し、定義側の引数を落として
  compose が TypeError で停止。未コミットのまま残ったツリーが release・collect を
  連鎖的に止め、その日の号が発行されなかった(2026-08-29)

lint.py は紙面を検査するもので、スクリプト自身は見ていない。ここで埋める。
検査するのは次の3点:

1. 構文(ast.parse できるか)
2. 呼び出しの引数がその関数の定義と合っているか(キーワード名・位置引数の数)
3. 同一モジュール内で定義もインポートもされていない関数を呼んでいないか
"""
import argparse
import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ["collect.py", "compose.py", "pipelib.py", "release.py", "lint.py",
           "derive.py", "watch.py", "deploy.py", "indexer.py", "tags.py",
           "fetch_page.py", "cost.py"]


def check(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"構文エラー: {e.lineno}行目 {e.msg}"]

    errs: list[str] = []
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    names = {a.asname or a.name.split(".")[0]
             for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in n.names}
    names |= {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name)}
    names |= {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    # 引数として渡された関数(コールバック)・ローカル束縛・内包表記の変数は
    # 呼び出せて当然なので、未定義の判定から外す
    for f in funcs.values():
        names |= {a.arg for a in f.args.args} | {a.arg for a in f.args.kwonlyargs}
    names |= {t.id for n in ast.walk(tree) if isinstance(n, (ast.For, ast.comprehension))
              for t in ast.walk(n.target) if isinstance(t, ast.Name)}
    names |= {n.name for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name}

    for call in [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]:
        fname = call.func.id
        f = funcs.get(fname)
        if f is None:
            if fname not in names and not hasattr(builtins, fname):
                errs.append(f"{call.lineno}行目: {fname}() が定義もインポートもされていない")
            continue
        allowed = {a.arg for a in f.args.args} | {a.arg for a in f.args.kwonlyargs}
        has_kwargs = f.args.kwarg is not None
        for kw in call.keywords:
            if kw.arg and not has_kwargs and kw.arg not in allowed:
                errs.append(f"{call.lineno}行目: {fname}({kw.arg}=...) は定義に無い引数")
        if f.args.vararg is None and len(call.args) > len(f.args.args):
            errs.append(f"{call.lineno}行目: {fname}() に位置引数{len(call.args)}個"
                        f"(定義は{len(f.args.args)}個)")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    total = 0
    for name in TARGETS:
        p = ROOT / "scripts" / name
        if not p.exists():
            continue
        errs = check(p)
        total += len(errs)
        for e in errs:
            print(f"::error file=scripts/{name}::{e}")
            print(f"  [ERROR] scripts/{name}: {e}", file=sys.stderr)
    if not args.quiet:
        print(f"selfcheck: {total} errors ({len(TARGETS)} scripts)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
