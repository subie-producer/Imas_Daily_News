- fetch_page.py が `FETCH_FAILED: メンテナンス中` を返す url は、原本を読めないことを理由に decline しない。
  その url の素材は素材の種別どおりの出典として sources に残し(verify が `confirmed` なら facts をそのまま使う)、ほかの事実は読める出典で直す。
  原本を確認できていないことの断り書きは書かない(コードが本文の末尾に付ける)
