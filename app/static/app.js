// 共用：倒數計時
(function () {
  const fmt = (s) => {
    s = Math.max(0, Math.floor(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return [h, m, x].map((v) => String(v).padStart(2, "0")).join(":");
  };
  const cd = document.getElementById("countdown");
  if (cd && cd.dataset.left !== "None") {
    const end = Date.now() + Number(cd.dataset.left) * 1000;
    const tick = () => {
      const left = (end - Date.now()) / 1000;
      cd.textContent = fmt(left);
      cd.classList.toggle("urgent", left < 600);
      if (left <= 0) { setTimeout(() => (location.href = cd.dataset.home || "/"), 1500); return; }
      setTimeout(tick, 1000);
    };
    tick();
  }
  const ts = document.getElementById("tostart");
  if (ts) {
    const start = Date.now() + Number(ts.dataset.left) * 1000;  // 用伺服器時間計算，避免電腦時鐘不準
    const tick = () => {
      const left = (start - Date.now()) / 1000;
      ts.textContent = fmt(left);
      if (left <= 0) { location.reload(); return; }
      setTimeout(tick, 1000);
    };
    tick();
  } else if (document.getElementById("waiting")) {
    setTimeout(() => location.reload(), 20000);
  }
})();
