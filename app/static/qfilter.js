// 題目篩選（題組頁、題庫頁共用）：搜尋文字＋標籤（多選＝全部符合）＋納入狀態；勾選框配合批次操作。
// 標記方式：
//   容器 [data-qfilter]，裡面有 .qf-search、.qf-tag[data-tag]、.qf-state（選用）、.qf-count、.qf-all（選用）
//   每題 [data-q]：data-search（小寫的搜尋文字）、data-tags（JSON 小寫標籤）、data-in（1/0，選用）
//   每題的勾選框 .qf-pick
(function () {
  document.querySelectorAll("[data-qfilter]").forEach(function (box) {
    var scope = document.querySelector(box.dataset.qfilter) || document;
    var items = Array.prototype.slice.call(scope.querySelectorAll("[data-q]"));
    var search = box.querySelector(".qf-search");
    var state = box.querySelector(".qf-state");
    var count = box.querySelector(".qf-count");
    var all = box.querySelector(".qf-all") || scope.querySelector(".qf-all");
    var tagBtns = Array.prototype.slice.call(box.querySelectorAll(".qf-tag"));
    var picked = {};

    function visible() { return items.filter(function (el) { return !el.hidden; }); }

    function apply() {
      var words = (search && search.value || "").toLowerCase().split(/\s+/).filter(Boolean);
      var tags = Object.keys(picked);
      var st = state ? state.value : "";
      items.forEach(function (el) {
        var text = el.dataset.search || "";
        var mine = JSON.parse(el.dataset.tags || "[]");
        var ok = words.every(function (w) { return text.indexOf(w) >= 0; }) &&
                 tags.every(function (t) { return mine.indexOf(t) >= 0; }) &&
                 (!st || el.dataset.in === st);
        el.hidden = !ok;
        if (!ok) el.querySelectorAll(".qf-pick").forEach(function (c) { c.checked = false; });
      });
      if (count) count.textContent = count.dataset.fmt.replace("{n}", visible().length).replace("{total}", items.length);
      if (all) all.checked = false;
    }

    tagBtns.forEach(function (b) {
      b.addEventListener("click", function () {
        var t = b.dataset.tag;
        if (picked[t]) { delete picked[t]; b.classList.remove("on"); } else { picked[t] = 1; b.classList.add("on"); }
        apply();
      });
    });
    if (search) search.addEventListener("input", apply);
    if (state) state.addEventListener("change", apply);
    if (all) all.addEventListener("change", function () {
      visible().forEach(function (el) { el.querySelectorAll(".qf-pick").forEach(function (c) { c.checked = all.checked; }); });
    });
    // 點題目上的標籤 = 用這個標籤篩選
    scope.querySelectorAll("[data-q] .qtag[data-tag]").forEach(function (t) {
      t.addEventListener("click", function (e) {
        var b = tagBtns.filter(function (x) { return x.dataset.tag === t.dataset.tag; })[0];
        if (b && !picked[b.dataset.tag]) { e.preventDefault(); b.click(); box.scrollIntoView({block: "nearest"}); }
      });
    });
    apply();
  });
})();
