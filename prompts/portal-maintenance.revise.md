- 公式ポータル(idolmaster-official.jp)の url で fetch_page.py が `FETCH_FAILED: メンテナンス中` を返すときは、読めないことを理由に decline しない。
  公式ポータルの素材は公式の出典として sources に残し(verify が `confirmed` なら facts をそのまま使う)、ほかの事実は読める出典で直す。
  公式ポータルが読めないことの断り書きは書かない(コードが本文の末尾に付ける)
