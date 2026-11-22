// 題組頁：未儲存提醒、改過的列標色、「顯示中的題目全部納入／不納入」
(function () {
  var forms = Array.prototype.slice.call(document.querySelectorAll("form[data-dirty-label]"));
  var dirty = {};          // form id → true
  var submitting = false;

  function snapshot(el) { return el.type === "checkbox" ? String(el.checked) : el.value; }

  forms.forEach(function (form) {
    var fields = Array.prototype.slice.call(form.querySelectorAll("input, select, textarea"))
      .filter(function (el) { return el.type !== "hidden" && el.name; });
    fields.forEach(function (el) { el.dataset.orig = snapshot(el); });

    function refresh() {
      var any = false;
      form.querySelectorAll("tr[data-q]").forEach(function (tr) {
        var changed = Array.prototype.some.call(tr.querySelectorAll("input[name]:not([type=hidden])"),
          function (el) { return snapshot(el) !== el.dataset.orig; });
        tr.classList.toggle("dirty", changed);
      });
      any = fields.some(function (el) { return snapshot(el) !== el.dataset.orig; });
      dirty[form.id] = any;
      var note = form.querySelector(".dirty-note");
      if (note) note.hidden = !any;
      var btn = form.querySelector("button[type=submit]");
      if (btn) btn.classList.toggle("pulse", any);
    }
    form.addEventListener("input", refresh);
    form.addEventListener("change", refresh);
    form._refresh = refresh;

    form.addEventListener("submit", function (e) {
      // 送出這一段時，如果別段還有沒存的修改，先提醒（那些修改不會一起存）
      var others = forms.filter(function (f) { return f !== form && dirty[f.id]; })
        .map(function (f) { return f.dataset.dirtyLabel; });
      if (others.length && !window.confirm(window.LSA_UNSAVED_OTHER.replace("{what}", others.join("、")))) {
        e.preventDefault();
        return;
      }
      submitting = true;
    });
  });

  window.addEventListener("beforeunload", function (e) {
    if (submitting) return;
    if (Object.keys(dirty).some(function (k) { return dirty[k]; })) { e.preventDefault(); e.returnValue = ""; }
  });

  // 納入勾選框改變時同步 data-in，讓「已納入／未納入」篩選跟著目前的勾選
  document.querySelectorAll("#set-questions tr[data-q] input.inc").forEach(function (cb) {
    cb.addEventListener("change", function () { cb.closest("tr").dataset.in = cb.checked ? "1" : "0"; });
  });

  function setShown(on) {
    var qform = document.getElementById("questions-form");
    document.querySelectorAll("#set-questions tr[data-q]").forEach(function (tr) {
      if (tr.hidden) return;
      var cb = tr.querySelector("input.inc");
      cb.checked = on;
      tr.dataset.in = on ? "1" : "0";
    });
    if (qform && qform._refresh) qform._refresh();
  }
  var inc = document.getElementById("inc-shown"), exc = document.getElementById("exc-shown");
  if (inc) inc.addEventListener("click", function () { setShown(true); });
  if (exc) exc.addEventListener("click", function () { setShown(false); });
})();
