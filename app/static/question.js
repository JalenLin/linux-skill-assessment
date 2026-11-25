// 作答頁 / 終端機獨立視窗：啟動環境、web shell、檢查、重置、比例調整、彈出視窗
(function () {
  const qid = window.QID, sid = window.SID;
  const qbase = `/s/${sid}/q/${encodeURIComponent(qid)}`;
  const termOnly = !!window.TERM_ONLY;
  const T = window.TIMING || { timed: false };
  const $ = (id) => document.getElementById(id);
  const btnCheck = $("btn-check"), btnReset = $("btn-reset"), btnRe = $("btn-reconnect"), btnCon = $("btn-console");
  // 同一題的作答頁與終端機視窗互相通知
  const chan = "BroadcastChannel" in window ? new BroadcastChannel(`lsa-term-${sid}-${qid}`) : null;

  // ---------------- 終端機 ----------------
  // 每個分頁（多節點題的每台機器）各有一個 xterm 與連線；切換分頁不會中斷其他台的連線。單機題只有一個、不顯示分頁。
  const TABS = window.TERM_TABS || [{ name: "", default: "ssh", switch: true }];
  const enc = new TextEncoder();
  let popup = null;   // 作答頁開出的終端機視窗
  let envReady = false;
  const sessions = TABS.map(makeSession);
  let cur = sessions[0];

  function makeSession(tab) {
    const el = document.createElement("div");
    el.className = "term-pane";
    el.hidden = true;
    $("term").appendChild(el);
    const term = new Terminal({
      cursorBlink: true, fontSize: 15, scrollback: 5000,
      fontFamily: 'ui-monospace, "Cascadia Mono", Menlo, Consolas, monospace',
      theme: { background: "#1e1e1e" },
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(el);
    // useConsole：作答者按了「改用主控台」（自動重連也維持主控台）；inConsole：目前連的是主控台
    const S = { tab, el, term, fit, ws: null, pingTimer: null, wanted: false, retries: 0, retryTimer: null,
                consoleDefault: tab.default === "console", useConsole: tab.default === "console", inConsole: false,
                started: false, statusText: "", statusOn: false, tabBtn: null };
    term.onData((d) => { if (S.ws && S.ws.readyState === 1) S.ws.send(enc.encode(d)); });
    term.onResize(({ cols, rows }) => {
      if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ type: "resize", cols, rows }));
    });
    return S;
  }

  const setStatus = (S, text, on) => {
    S.statusText = text; S.statusOn = !!on;
    if (S.tabBtn) S.tabBtn.classList.toggle("on", !!on);
    if (S === cur) { $("term-status").textContent = text; $("dot").classList.toggle("on", !!on); }
  };
  const status = (text, on) => sessions.forEach((S) => setStatus(S, text, on));
  const conLabel = () => {
    if (!btnCon) return;
    btnCon.hidden = !cur.tab.switch;
    btnCon.textContent = cur.inConsole ? t("改用 SSH") : t("改用主控台");
  };
  const refit = () => { if (!$("term").hidden && !cur.el.hidden) { try { cur.fit.fit(); } catch (e) {} } };
  new ResizeObserver(refit).observe($("term"));

  function show(S) {
    cur = S;
    sessions.forEach((x) => { x.el.hidden = x !== S; if (x.tabBtn) x.tabBtn.classList.toggle("active", x === S); });
    setStatus(S, S.statusText, S.statusOn);
    conLabel();
    refit();
    if (envReady && !S.started && !(popup && !popup.closed)) connect(S);
    S.term.focus();
  }
  if (TABS.length > 1 || TABS[0].name) {
    const bar = $("term-tabs");
    bar.hidden = false;
    sessions.forEach((S) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "term-tab";
      b.innerHTML = '<span class="dot"></span>';
      b.append(S.tab.name);
      b.onclick = () => show(S);
      S.tabBtn = b;
      bar.appendChild(b);
    });
  }
  show(cur);

  async function api(path, method = "POST") {
    const r = await fetch(`/api${qbase}/${path}`, { method });
    if (r.redirected) { location.href = r.url; return { ok: false, error: t("請重新登入") }; }
    try { return await r.json(); } catch (e) { return { ok: false, error: `HTTP ${r.status}` }; }
  }

  function disconnect(S) {
    S.wanted = false;
    S.started = false;
    clearTimeout(S.retryTimer);
    clearInterval(S.pingTimer);
    if (S.ws) { try { S.ws.close(); } catch (e) {} S.ws = null; }
  }
  const disconnectAll = () => sessions.forEach(disconnect);

  function connect(S) {
    disconnect(S);
    S.wanted = true;
    S.started = true;
    S.inConsole = false;
    if (S === cur) conLabel();
    if (S === cur) refit();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const node = S.tab.name ? `&node=${encodeURIComponent(S.tab.name)}` : "";
    const sock = new WebSocket(`${proto}://${location.host}/ws/shell/${sid}/${encodeURIComponent(qid)}?cols=${S.term.cols}&rows=${S.term.rows}&console=${S.useConsole ? 1 : 0}${node}`);
    S.ws = sock;
    sock.binaryType = "arraybuffer";
    sock.onopen = () => { setStatus(S, t("已連線"), true); if (S === cur) S.term.focus(); S.retries = 0; };
    sock.onmessage = (ev) => {
      if (typeof ev.data === "string") {   // 伺服器的控制訊息
        try {
          const m = JSON.parse(ev.data);
          if (m.type === "notice") flash(m.text);
          if (m.type === "console") {  // 主控台連線（作答者選的，或 SSH 連不上自動改用）
            S.inConsole = true;
            if (S === cur) conLabel();
            setStatus(S, m.forced ? t("主控台模式") : t("主控台模式（SSH 連不上）"), true);
            if (btnRe) btnRe.disabled = false;
          }
          if (m.type === "paused") {   // 開了別題，本題環境被暫停：不要自動重連
            S.wanted = false;
            setStatus(S, t("本題環境已暫停（開啟了其他題目）"), false);
            if (btnRe) btnRe.disabled = false;
          }
        } catch (e) {}
        return;
      }
      S.term.write(new Uint8Array(ev.data));
    };
    sock.onclose = () => {
      if (S.ws !== sock || !S.wanted || timeUp) return;
      // 自動重連（例如題目檢查時重開機、短暫斷網）
      if (S.retries < 30) {
        S.retries += 1;
        setStatus(S, t("連線中斷，自動重新連線中…"), false);
        S.retryTimer = setTimeout(() => { if (S.wanted && !timeUp) connect(S); }, 4000);
      } else {
        setStatus(S, t("連線已中斷，可按「重新連線」"), false);
      }
    };
    S.pingTimer = setInterval(() => {
      if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ type: "ping" }));
    }, 60000);
  }

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
    sessions.forEach((S) => S.term.attachCustomKeyEventHandler((e) => {
      const k = (e.key || "").toLowerCase();
      if (e.type === "keydown" && (((e.ctrlKey || e.metaKey) && k === "v") || (e.shiftKey && k === "insert"))) {
        flash(t("本次測驗禁止貼上"));
        e.preventDefault();
        return false;
      }
      return true;
    }));
  }

  const actionBtns = () => [btnCheck, btnReset, btnRe, btnCon].filter(Boolean);

  // opts.all：整組重新連線（第一次開啟、重置後）；否則只重連目前這個分頁（其他台的連線不受影響）
  // opts.console：目前分頁改用主控台／SSH
  async function start(opts) {
    opts = opts || {};
    const targets = opts.all ? sessions : [cur];
    cur.useConsole = "console" in opts ? !!opts.console : cur.consoleDefault;
    if (opts.all) sessions.forEach((S) => { S.useConsole = S.consoleDefault; });
    targets.forEach((S) => { disconnect(S); S.term.reset(); });
    actionBtns().forEach((b) => (b.disabled = true));
    targets.forEach((S) => setStatus(S, t("準備環境中…第一次開啟約需 10~30 秒"), false));
    const r = await api("start");
    if (!r.ok) {
      targets.forEach((S) => setStatus(S, t("環境啟動失敗"), false));
      cur.term.write(`\r\n\x1b[31m${r.error}\x1b[0m\r\n`);
      if (btnReset) btnReset.disabled = false;
      if (btnRe) btnRe.disabled = false;
      if (btnCon) btnCon.disabled = false;
      return;
    }
    envReady = true;
    actionBtns().forEach((b) => (b.disabled = false));
    if (popup && !popup.closed) { status(t("終端機在新視窗"), false); return; }
    targets.forEach((S) => { if (S !== cur) setStatus(S, "", false); });
    connect(cur);
  }
  if (btnRe) btnRe.onclick = () => start();
  if (btnCon) btnCon.onclick = () => start({ console: !cur.inConsole });

  // ---------------- 單題限時 ----------------
  let timeUp = false;
  function onTimeUp() {
    if (timeUp) return;
    timeUp = true;
    disconnectAll();
    status(t("本題作答時間到"), false);
    cur.term.write(`\r\n\x1b[1;33m${t("本題作答時間到，連線已關閉，環境將重置。")}\x1b[0m\r\n`);
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
      chan.onmessage = (ev) => { if (ev.data === "restart") start({ all: true }); };
      chan.postMessage("popup-open");
      window.addEventListener("beforeunload", () => chan.postMessage("popup-closed"));
    }
    if (T.timed && !T.active) {
      status(t("尚未開始作答"), false);
      cur.term.write(t("本題限時作答，請回到題目頁按「開始作答」。") + "\r\n");
      return;
    }
    start({ all: true });
    return;
  }

  // ---------------- 作答頁：彈出 / 收回 ----------------
  function showPopped(on) {
    $("term").hidden = on;
    $("popped").hidden = !on;
    $("btn-popout").hidden = on;
    if (on) { disconnectAll(); status(t("終端機在新視窗"), false); }
    else { refit(); sessions.forEach((S) => { if (S !== cur) setStatus(S, "", false); }); connect(cur); }
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
    disconnectAll();
    const r = await api("reset");
    if (!r.ok) { alert(r.error); btnReset.disabled = false; return; }
    await start({ all: true });
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

  start({ all: true });
  poll();
})();
