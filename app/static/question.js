// 作答頁 / 終端機獨立視窗：啟動環境、web shell、檢查、重置、比例調整、彈出視窗
(function () {
  const qid = window.QID, sid = window.SID;
  const qbase = `/s/${sid}/q/${encodeURIComponent(qid)}`;
  const termOnly = !!window.TERM_ONLY;
  const T = window.TIMING || { timed: false };
  const $ = (id) => document.getElementById(id);
  const btnCheck = $("btn-check"), btnReset = $("btn-reset"), btnRe = $("btn-reconnect");
  const status = (text, on) => { $("term-status").textContent = text; $("dot").classList.toggle("on", !!on); };
  // 同一題的作答頁與終端機視窗互相通知
  const chan = "BroadcastChannel" in window ? new BroadcastChannel(`lsa-term-${sid}-${qid}`) : null;

  // ---------------- 終端機 ----------------
  const term = new Terminal({
    cursorBlink: true, fontSize: 15, scrollback: 5000,
    fontFamily: 'ui-monospace, "Cascadia Mono", Menlo, Consolas, monospace',
    theme: { background: "#1e1e1e" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("term"));
  const refit = () => { if (!$("term").hidden) { try { fit.fit(); } catch (e) {} } };
  new ResizeObserver(refit).observe($("term"));
  refit();

  let ws = null, pingTimer = null, wanted = true, retries = 0, retryTimer = null;
  let popup = null;   // 作答頁開出的終端機視窗
  const enc = new TextEncoder();

  async function api(path, method = "POST") {
    const r = await fetch(`/api${qbase}/${path}`, { method });
    if (r.redirected) { location.href = r.url; return { ok: false, error: t("請重新登入") }; }
    try { return await r.json(); } catch (e) { return { ok: false, error: `HTTP ${r.status}` }; }
  }

  function disconnect() {
    wanted = false;
    clearTimeout(retryTimer);
    clearInterval(pingTimer);
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
  }

  function connect() {
    disconnect();
    wanted = true;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const sock = new WebSocket(`${proto}://${location.host}/ws/shell/${sid}/${encodeURIComponent(qid)}?cols=${term.cols}&rows=${term.rows}`);
    ws = sock;
    sock.binaryType = "arraybuffer";
    sock.onopen = () => { status(t("已連線"), true); term.focus(); retries = 0; };
    sock.onmessage = (ev) => {
      if (typeof ev.data === "string") {   // 伺服器的控制訊息
        try {
          const m = JSON.parse(ev.data);
          if (m.type === "notice") flash(m.text);
          if (m.type === "console") {  // SSH 連不上，改用主控台
            status(t("主控台模式（SSH 連不上）"), true);
            if (btnRe) btnRe.disabled = false;
          }
          if (m.type === "paused") {   // 開了別題，本題環境被暫停：不要自動重連
            wanted = false;
            status(t("本題環境已暫停（開啟了其他題目）"), false);
            if (btnRe) btnRe.disabled = false;
          }
        } catch (e) {}
        return;
      }
      term.write(new Uint8Array(ev.data));
    };
    sock.onclose = () => {
      if (ws !== sock || !wanted || timeUp) return;
      // 自動重連（例如題目檢查時重開機、短暫斷網）
      if (retries < 30) {
        retries += 1;
        status(t("連線中斷，自動重新連線中…"), false);
        retryTimer = setTimeout(() => { if (wanted && !timeUp) connect(); }, 4000);
      } else {
        status(t("連線已中斷，可按「重新連線」"), false);
      }
    };
    pingTimer = setInterval(() => {
      if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "ping" }));
    }, 60000);
  }
  term.onData((d) => { if (ws && ws.readyState === 1) ws.send(enc.encode(d)); });
  term.onResize(({ cols, rows }) => {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "resize", cols, rows }));
  });

  // ---------------- 禁止貼上（後台可選）----------------
  let flashTimer = null;
  function flash(text) {
    const el = $("term-status");
    const prev = el.dataset.prev || el.textContent;
    el.dataset.prev = prev;
    el.textContent = `⚠ ${text}`;
    el.classList.add("bad");
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { el.textContent = el.dataset.prev; delete el.dataset.prev; el.classList.remove("bad"); }, 2500);
  }
  if (window.BLOCK_PASTE) {
    const block = (e) => { e.preventDefault(); e.stopPropagation(); flash(t("本次測驗禁止貼上")); };
    // capture 階段攔截，事件不會到 xterm 的 textarea
    ["paste", "drop"].forEach((t) => $("term").addEventListener(t, block, true));
    term.attachCustomKeyEventHandler((e) => {
      const k = (e.key || "").toLowerCase();
      if (e.type === "keydown" && (((e.ctrlKey || e.metaKey) && k === "v") || (e.shiftKey && k === "insert"))) {
        flash(t("本次測驗禁止貼上"));
        e.preventDefault();
        return false;
      }
      return true;
    });
  }

  const actionBtns = () => [btnCheck, btnReset, btnRe].filter(Boolean);

  async function start() {
    actionBtns().forEach((b) => (b.disabled = true));
    status(t("準備環境中…第一次開啟約需 10~30 秒"), false);
    term.reset();
    const r = await api("start");
    if (!r.ok) {
      status(t("環境啟動失敗"), false);
      term.write(`\r\n\x1b[31m${r.error}\x1b[0m\r\n`);
      if (btnReset) btnReset.disabled = false;
      if (btnRe) btnRe.disabled = false;
      return;
    }
    actionBtns().forEach((b) => (b.disabled = false));
    if (popup && !popup.closed) { status(t("終端機在新視窗"), false); return; }
    connect();
  }
  if (btnRe) btnRe.onclick = () => start();

  // ---------------- 單題限時 ----------------
  let timeUp = false;
  function onTimeUp() {
    if (timeUp) return;
    timeUp = true;
    disconnect();
    status(t("本題作答時間到"), false);
    term.write(`\r\n\x1b[1;33m${t("本題作答時間到，連線已關閉，環境將重置。")}\x1b[0m\r\n`);
    actionBtns().forEach((b) => (b.disabled = true));
    if (!termOnly) setTimeout(() => location.reload(), 6000);
  }
  function startAttemptTimer() {
    const el = $("attempt-left");
    if (!T.timed || !T.active || !el) return;
    const end = Date.now() + T.seconds_left * 1000;
    const tick = () => {
      const left = Math.max(0, Math.round((end - Date.now()) / 1000));
      el.textContent = `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}`;
      el.classList.toggle("urgent", left < 120);
      if (left <= 0) { onTimeUp(); return; }
      setTimeout(tick, 1000);
    };
    tick();
  }
  startAttemptTimer();
  // 限時作答中離開頁面會中斷本題，先提醒
  window.addEventListener("beforeunload", (e) => {
    if (T.timed && T.active && !timeUp && !termOnly && !(popup && !popup.closed)) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  // ---------------- 終端機獨立視窗 ----------------
  if (termOnly) {
    // 標題由樣板產生
    if (chan) {
      chan.onmessage = (ev) => { if (ev.data === "restart") start(); };
      chan.postMessage("popup-open");
      window.addEventListener("beforeunload", () => chan.postMessage("popup-closed"));
    }
    if (T.timed && !T.active) {
      status(t("尚未開始作答"), false);
      term.write(t("本題限時作答，請回到題目頁按「開始作答」。") + "\r\n");
      return;
    }
    start();
    return;
  }

  // ---------------- 作答頁：彈出 / 收回 ----------------
  function showPopped(on) {
    $("term").hidden = on;
    $("popped").hidden = !on;
    $("btn-popout").hidden = on;
    if (on) { disconnect(); status(t("終端機在新視窗"), false); }
    else { refit(); connect(); }
  }
  $("btn-popout").onclick = () => {
    popup = window.open(`${qbase}/term`, `lsa-term-${sid}-${qid}`,
      "width=1100,height=720,menubar=no,toolbar=no,location=no");
    if (!popup) { alert(t("瀏覽器擋下了彈出視窗，請允許此網站開啟彈出視窗。")); return; }
    showPopped(true);
  };
  $("btn-focus-pop").onclick = () => { if (popup && !popup.closed) popup.focus(); else showPopped(false); };
  $("btn-pull-back").onclick = () => { if (popup && !popup.closed) popup.close(); popup = null; showPopped(false); };
  if (chan) {
    chan.onmessage = (ev) => {
      if (ev.data === "popup-closed" && !$("popped").hidden) { popup = null; showPopped(false); }
    };
  }
  // 沒有 BroadcastChannel 時，定期檢查視窗是否被關掉
  setInterval(() => { if (popup && popup.closed && !$("popped").hidden) { popup = null; showPopped(false); } }, 1500);

  // ---------------- 左右比例調整 ----------------
  const wrap = $("wrap"), splitter = $("splitter");
  const KEY = "lsa-split";
  const setSide = (pct) => wrap.style.setProperty("--side", `${pct}%`);
  try { const v = parseFloat(localStorage.getItem(KEY)); if (v >= 15 && v <= 80) setSide(v); } catch (e) {}
  splitter.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    splitter.setPointerCapture(e.pointerId);
    splitter.classList.add("dragging");
    document.body.classList.add("resizing");
    const rect = wrap.getBoundingClientRect();
    const move = (ev) => {
      const pct = Math.min(80, Math.max(15, ((ev.clientX - rect.left) / rect.width) * 100));
      setSide(pct.toFixed(1));
    };
    const up = () => {
      splitter.removeEventListener("pointermove", move);
      splitter.classList.remove("dragging");
      document.body.classList.remove("resizing");
      try { localStorage.setItem(KEY, parseFloat(wrap.style.getPropertyValue("--side"))); } catch (e) {}
      refit();
    };
    splitter.addEventListener("pointermove", move);
    splitter.addEventListener("pointerup", up, { once: true });
  });
  splitter.addEventListener("dblclick", () => {
    wrap.style.removeProperty("--side");
    try { localStorage.removeItem(KEY); } catch (e) {}
    refit();
  });

  // ---------------- 檢查 ----------------
  function escapeHtml(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
  function renderRuns(d) {
    $("qscore").textContent = d.score === null ? "—" : d.score;
    $("policy").textContent = { best: t("（取最高分）"), latest: t("（取最後一次）"), final: t("（以測驗結束後統一檢查為準）") }[d.policy] || "";
    $("runs").innerHTML = d.runs.map((r) => {
      const head = r.status === "done"
        ? `<b>${r.score} / ${r.max_score}</b> <span class="muted">${escapeHtml(r.message || "")}</span>`
        : r.status === "error" ? `<span class="bad">${escapeHtml(r.message || t("錯誤"))}</span>`
        : `<span class="warn">${r.status === "queued" ? t("排隊中…") : t("檢查中…")}</span>`;
      const items = (r.items || []).map((i) =>
        `<div class="result-item"><span>${i.passed ? "✅" : "❌"} ${escapeHtml(i.desc)}</span><span class="muted">${i.points}</span></div>`).join("");
      return `<div class="run"><div class="muted">${r.started_at.replace("T", " ")}</div>${head}${items}</div>`;
    }).join("");
    return d.runs.length && ["queued", "running"].includes(d.runs[0].status);
  }
  async function poll() {
    const d = await api("runs", "GET");
    if (!d.ok) return;
    if (renderRuns(d)) setTimeout(poll, 2000);
    else if (!(T.timed && !T.active) && !timeUp) btnCheck.disabled = false;
  }
  btnCheck.onclick = async () => {
    btnCheck.disabled = true;
    $("check-msg").textContent = "";
    const r = await api("check");
    if (!r.ok) { $("check-msg").textContent = r.error; btnCheck.disabled = false; return; }
    poll();
  };

  // ---------------- 重置 ----------------
  btnReset.onclick = async () => {
    if (!confirm(t("確定要重置本題？你在此環境做的所有修改都會消失，回到題目初始狀態。"))) return;
    btnReset.disabled = true;
    disconnect();
    const r = await api("reset");
    if (!r.ok) { alert(r.error); btnReset.disabled = false; return; }
    await start();
    if (chan) chan.postMessage("restart");   // 讓終端機視窗重新連到新環境
  };

  // 限時題目尚未開始：顯示「開始作答」面板，不自動建立環境
  if (T.timed && !T.active) {
    $("term").hidden = true;
    $("btn-popout").hidden = true;
    $("start-panel").hidden = false;
    actionBtns().forEach((b) => (b.disabled = true));
    status(t("尚未開始作答"), false);
    $("start-info").textContent = T.used
      ? t("本題限時 {n} 分鐘。上一次作答已結束。", { n: T.limit_min }) + (T.max_attempts ? " " + t("（已用 {used}/{max} 次）", { used: T.used, max: T.max_attempts }) : "")
      : t("本題限時 {n} 分鐘，按下後環境準備好就開始計時。", { n: T.limit_min });
    const btn = $("btn-begin");
    btn.textContent = T.used ? t("再作答一次（重新開始，環境會重置）") : t("開始作答（限時 {n} 分）", { n: T.limit_min });
    if (!T.can_start) { btn.disabled = true; $("start-reason").textContent = T.reason; }
    btn.onclick = async () => {
      if (T.used && !confirm(t("重新開始會建立全新的環境，並重新計時。確定？"))) return;
      btn.disabled = true;
      btn.textContent = t("準備環境中…約 10~30 秒");
      const r = await api("start?begin=1");
      if (!r.ok) { alert(r.error); btn.disabled = false; btn.textContent = t("開始作答"); return; }
      location.reload();
    };
    poll();
    return;
  }

  start();
  poll();
})();
