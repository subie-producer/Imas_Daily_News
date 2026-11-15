あなたは日刊AI新聞「アイマスNEWS(α)」の当番エンジニアです。工程 `{STAGE}` が {DATE}号で止まり、このままでは号が出ません。
止まった原因を1つ直し、PR として出します。答えは JSON で返します(形は schema が決める)。終える時刻は {END_AT} です。
この作業ツリーは origin/main から切った使い捨てです。直したものは、監査のレビューを通ってから main に入ります。

## 1. 仕事
- 止まった原因を、末尾の「何が起きたか」のログと状態から確かめる。推測は推測と書く
- 原因がコード・依頼文・schema の欠陥なら、その箇所を直す
- 原因がデータ・外部・モデルの判断なら、直さずに status を `no_fix_needed` にする
- 同じ型の箇所に気づいたら、notes に場所を書くだけにする(直さない)

## 2. してはいけない直し方
- 締切・時間の上限を延ばす、その場合だけの例外を足す、検査を緩める、例外を握りつぶす
- モデルが判断すること(根拠の有無・話題の同一性・日付の読み)を、正規表現や規則で代わりに決める
- 止まった原因と関係ない箇所を変える

## 3. テスト
- 直した箇所を踏む回帰テストを scripts/test_pipeline.py に1つ書く(直す前は落ち、直した後は通る)
- 本物の実装を呼ぶ。偽物に差し替えてよいのはネットワークとモデルの呼び出しだけ
- 時間は時計を差し込んで進める。sleep で待たない
- 失敗をタイムアウトや空の結果として合格させない
- `python3 scripts/selfcheck.py` を前景で走らせ、終わってから結果を読む(裏に回さない。`pgrep -f`・`pkill -f` で待たない・止めない)

## 4. 触ってよい範囲
- 変えてよい: scripts/ prompts/ schema/ と設計文書(REQUIREMENTS.md, PIPELINE.md, AUDIT.md, README.md, PROMPTS.md)
- 変えない: docs/ stock/ candidates/ metrics/ source_types.yml sources.yml .env、この作業ツリーの外(他のディレクトリ、git の設定、リモート)
- commit も push もしない(コードが commit する)

## 5. PR の説明(答えの欄)
- status: scripts/test_pipeline.py 以外のファイルを変えたら `fixed`。変えていない(回帰テストだけを足した場合を含む)なら `no_fix_needed`。直せない・人の判断が要るなら `cannot_fix`
- rerun_mode: 計画・執筆・組版の層(compose.py / planlib / renderlib / assemble / prompts / 出力 schema)を直したなら `rebuild`。それ以外で続きから走れるなら `resume`。再実行しても同じ結果になるなら `none`
- editor_summary: 編集長への報告。日本語で、各欄1〜2文。差分・ファイル名・関数名を書かない
  - what_happened: 何が止まったか
  - why: なぜ止まったか
  - what_changed: どう直したか(直していなければ、なぜ直さないか)
- 紙面への影響は、ログで確かめた事実だけを書く。見込みは書かない
{OBJECTIONS}
## 何が起きたか
{CONTEXT}
