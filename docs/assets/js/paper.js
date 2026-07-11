// アイマスNEWS(α) — JS は推しフィルタと SP ダイジェスト開閉のみ(REQUIREMENTS 2.3)
(function () {
  "use strict";

  // ---- 推しフィルタ ----
  var chips = document.querySelectorAll("[data-chip]");
  var items = document.querySelectorAll("[data-brand]");

  function applyFilter(brand) {
    items.forEach(function (el) {
      var hit = brand === "all" || el.getAttribute("data-brand") === brand;
      el.classList.toggle("brand-hidden", !hit);
    });
    chips.forEach(function (c) {
      var on = c.getAttribute("data-chip") === brand;
      c.style.fontWeight = on ? "700" : "500";
      c.style.color = on ? "#1c1b18" : "#8a857a";
      c.style.borderBottom = on
        ? "3px solid " + c.getAttribute("data-color")
        : "3px solid transparent";
    });
    // ダイジェスト群の件数を出し分け後に更新
    document.querySelectorAll("[data-digest-group]").forEach(function (g) {
      var visible = g.querySelectorAll("[data-brand]:not(.brand-hidden)").length;
      var cnt = g.querySelector("[data-group-count]");
      if (cnt) cnt.textContent = visible;
      g.classList.toggle("brand-hidden", visible === 0);
    });
  }

  chips.forEach(function (c) {
    c.addEventListener("click", function () {
      applyFilter(c.getAttribute("data-chip"));
    });
  });

  // /?brand=cg で面トップ相当(フィルタ済みトップ)を開く
  var q = new URLSearchParams(location.search).get("brand");
  if (q) applyFilter(q);

  // ---- SP ダイジェスト開閉 ----
  var overlay = document.getElementById("digest-overlay");
  var openBtn = document.getElementById("digest-open");
  var closeBtn = document.getElementById("digest-close");
  if (openBtn && overlay) openBtn.addEventListener("click", function () { overlay.classList.add("open"); });
  if (closeBtn && overlay) closeBtn.addEventListener("click", function () { overlay.classList.remove("open"); });
})();
