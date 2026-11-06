
## 6. レビューへの返事
この作業ツリーには、ここまでの修正が commit 済みで入っている(`git log --oneline -5`)。下は監査の blocking のコメント。
- 1件ずつ replies に返す
  - `fixed`: 直した。どう直したかを evidence に書く
  - `disagree`: 同意しない。当たらない根拠(コードの該当行・再現しない入力・実測)を evidence に書く
- blocking と関係ない箇所を変えない
- コードを変えたら、status を `fixed` に改める
- editor_summary は、直した内容が変わったときだけ書き直す

{ITEMS}
