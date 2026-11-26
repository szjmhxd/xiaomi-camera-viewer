/* 摄像头回放台 — 前端
 * 把"一天"当成一条连续时间轴，把碎片 mp4 串成可拖动、可连播的回放。
 */
(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const DAY = 86400;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const el = {
    video: $("#video"), player: $("#player"),
    cover: $("#playerCover"), bigPlay: $("#playerBigPlay"), hint: $("#playerHint"),
    msg: $("#playerMsg"), msgText: $("#msgText"), msgSpin: $("#msgSpin"),
    badge: $("#playerBadge"), osd: $("#osd"),
    days: $("#days"), daysEmpty: $("#daysEmpty"), sideStat: $("#sideStat"),
    camName: $("#camName"),
    dayLabel: $("#dayLabel"), dayMeta: $("#dayMeta"),
    daySearch: $("#daySearch"), daySearchClear: $("#daySearchClear"),
    dayChips: $("#dayChips"), filterCount: $("#filterCount"),
    folderName: $("#folderName"),
    tl: $("#timeline"), canvas: $("#tlCanvas"),
    cursor: $("#tlCursor"), hover: $("#tlHover"),
    tlClock: $("#tlClock"), tlGap: $("#tlGap"), tlScale: $("#tlScale"),
    jump: $("#jumpTime"),
    play: $("#btnPlay"), prev: $("#btnPrev"), next: $("#btnNext"),
    back10: $("#btnBack10"), fwd10: $("#btnFwd10"),
    rateSel: $("#rateSel"),
    mute: $("#btnMute"), shot: $("#btnShot"),
    full: $("#btnFull"), exportBtn: $("#btnExport"),
    refresh: $("#btnRefresh"), folder2: $("#btnFolder2"), folder: $("#btnFolder"),
    side: $("#side"), sideOpen: $("#btnSideOpen"), sideClose: $("#btnSideClose"),
    // 文件夹选择器
    fsModal: $("#fsModal"), fsDrives: $("#fsDrives"), fsPath: $("#fsPath"),
    fsList: $("#fsList"), fsUp: $("#fsUp"), fsRecent: $("#fsRecent"),
    fsHint: $("#fsHint"), fsOk: $("#fsOk"), fsCancel: $("#fsCancel"), fsClose: $("#fsClose"),
    fsUploadBtn: $("#fsUploadBtn"), fsUploadInput: $("#fsUploadInput"),
    fsUploadState: $("#fsUploadState"), fsNet: $("#fsNet"),
    // 配色
    themeBtn: $("#btnTheme"), themePop: $("#themePop"), themeGrid: $("#themeGrid"),
    // 手机端信息面板
    miDate: $("#miDate"), miFacts: $("#miFacts"),
  };

  const S = {
    cfg: null,
    days: [],
    date: null,
    segs: [],
    idx: -1,
    origin: 0,        // 当前 src 的第 0 秒对应片段内偏移
    mode: "stream",   // stream | file
    playing: false,
    rate: 1,
    retries: 0,
    blocks: [],
    filter: { text: "", quick: "all" },
    folder: null,     // 当前录像目录名，用于 localStorage 分桶
    pending: null,    // 已定位但还没起播：{ i, off }，点播放时才真正加载
    fs: { path: "", dirs: [], selected: -1 },
    busy: false,
  };

  /* ----------------------------- 小工具 ----------------------------- */
  const pad = (n) => String(n).padStart(2, "0");
  const hms = (sec) => {
    sec = Math.max(0, Math.round(sec));
    return `${pad(Math.floor(sec / 3600))}:${pad(Math.floor(sec / 60) % 60)}:${pad(sec % 60)}`;
  };
  const hm = (sec) => {
    sec = Math.max(0, Math.round(sec));
    return `${pad(Math.floor(sec / 3600))}:${pad(Math.floor(sec / 60) % 60)}`;
  };
  const humanSize = (n) => {
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i >= 2 ? 1 : 0)} ${u[i]}`;
  };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const api = async (path, opts) => {
    const r = await fetch(path, opts);
    if (!r.ok) {
      let msg = `${r.status} ${r.statusText}`;
      try { const j = await r.json(); if (j.error) msg = j.error; } catch (_) {}
      throw new Error(msg);
    }
    return r.json();
  };

  let toastTimer = null;
  function toast(text, ms = 2200) {
    el.msgText.textContent = text;
    el.msgSpin.classList.add("off");
    el.msg.classList.remove("hide");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.msg.classList.add("hide"), ms);
  }
  function loading(text) {
    el.msgText.textContent = text || "加载中…";
    el.msgSpin.classList.remove("off");
    el.msg.classList.remove("hide");
  }
  function idleHint(text) {
    el.msgSpin.classList.add("off");
    el.msgText.textContent = text || "点击 ▶ 开始回放";
    el.msg.classList.remove("hide");
  }
  function ready() { el.msg.classList.add("hide"); }

  /* ----------------------------- 初始化 ----------------------------- */
  async function boot() {
    try {
      S.cfg = await api("/api/config");
      renderStats();
      if (!S.cfg.encoder.ffmpeg) {
        toast("未检测到 ffmpeg，无法转码播放", 6000);
        return;
      }
      if (S.cfg.scanning) return pollScan();
      if (!S.cfg.root) {
        idleHint("先选择一个存放录像的文件夹");
        return openFolderPicker();
      }
      if (S.cfg.stats && S.cfg.stats.files === 0) {
        idleHint("这个文件夹里没有找到录像");
        return openFolderPicker("这个目录里没有可播放的片段，换一个试试");
      }
      await loadDays();
    } catch (e) {
      el.msgSpin.classList.add("off");
      el.msgText.textContent = `初始化失败：${e.message}`;
    }
  }

  function renderStats() {
    const st = (S.cfg && S.cfg.stats) || {};
    const cams = st.cameras || [];
    el.camName.textContent = cams.length ? cams[0] : (S.cfg && S.cfg.name) || "未选择目录";
    el.camName.title = cams.join("\n");
    const cacheMb = S.cfg && S.cfg.cache ? (S.cfg.cache.bytes / 1024 / 1024).toFixed(0) : 0;
    el.sideStat.innerHTML = `
      <div><i>片段</i><br><b>${(st.files || 0).toLocaleString()}</b></div>
      <div><i>容量</i><br><b>${humanSize(st.bytes || 0)}</b></div>
      <div><i>天数</i><br><b>${st.days || 0}</b></div>
      <div><i>已缓存</i><br><b>${cacheMb} MB</b></div>`;

    const name = (S.cfg && S.cfg.name) || "";
    el.folderName.textContent = name || "选择文件夹";
    el.folderName.title = (S.cfg && S.cfg.root) || "";
    el.folder.classList.toggle("empty", !name);
    S.folder = name;
  }

  /* --------------------------- 扫描与轮询 --------------------------- */
  async function pollScan() {
    let last = -1;
    while (true) {
      let st;
      try {
        st = await api("/api/scan");
      } catch (e) {
        await sleep(800);
        continue;
      }
      if (st.scanning) {
        const n = (st.stats && st.stats.progress) || 0;
        if (n !== last) {
          last = n;
          loading(`正在扫描目录… 已读取 ${n.toLocaleString()} 项`);
        } else {
          loading("正在扫描目录…");
        }
        await sleep(500);
        continue;
      }
      // 扫描结束
      S.cfg = await api("/api/config");
      renderStats();
      if (st.error) {
        toast(`扫描出错：${st.error}`, 6000);
        return;
      }
      if (!st.root) {
        return openFolderPicker();
      }
      if (!st.stats || st.stats.files === 0) {
        idleHint("这个文件夹里没有找到录像");
        return openFolderPicker("这个目录里没有可播放的片段，换一个试试");
      }
      return loadDays();
    }
  }

  async function loadDays() {
    S.days = await api("/api/days");
    if (!S.days.length) {
      el.msgSpin.classList.add("off");
      el.msgText.textContent = "没有找到可回放的日期";
      return;
    }
    applyFilter();

    const hash = decodeURIComponent(location.hash.slice(1));
    const key = `cam.last.${S.folder || "default"}`;
    const last = localStorage.getItem(key);
    const pick = S.days.find((d) => d.date === hash)
      || S.days.find((d) => d.date === last)
      || S.days[0];
    // 扫描完只定位到最近一天的第一段并显示首帧，不自动播放、不触发转码
    await selectDay(pick.date, { autoplay: false });
  }

  /* --------------------------- 日期筛选 ---------------------------- */
  function withinDays(dateStr, n) {
    if (!S.days.length) return true;
    const base = new Date(S.days[0].date + "T00:00:00");
    const d = new Date(dateStr + "T00:00:00");
    return (base - d) / 86400000 < n;
  }

  function filteredDays() {
    const q = S.filter.text.trim().toLowerCase();
    const quick = S.filter.quick;
    return S.days.filter((d) => {
      if (q) {
        const short = d.date.slice(5);
        const bare = d.date.replace(/-/g, "");
        if (!d.date.includes(q) && !short.includes(q) && !bare.includes(q.replace(/-/g, ""))) {
          return false;
        }
      }
      if (quick === "7" || quick === "30") return withinDays(d.date, Number(quick));
      if (quick === "long") return d.seconds >= 7200;
      return true;
    });
  }

  function applyFilter() {
    const list = filteredDays();
    renderDays(list);

    const hidden = S.days.length - list.length;
    el.filterCount.textContent = hidden > 0
      ? `显示 ${list.length} / ${S.days.length} 天`
      : `共 ${S.days.length} 天`;
    el.daysEmpty.hidden = list.length > 0;
    el.daySearchClear.hidden = !S.filter.text;
  }

  const WEEK = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  const weekdayOf = (dateStr) => {
    const d = new Date(dateStr + "T00:00:00");
    return Number.isNaN(d.getTime()) ? "" : WEEK[d.getDay()];
  };

  function renderDays(list) {
    const maxSec = Math.max.apply(null, S.days.map((d) => d.seconds).concat([1]));
    el.days.innerHTML = "";
    let lastMonth = "";
    for (const d of list) {
      // 跨月时插一条月份分隔，长列表更适合按月份扫
      const month = d.date.slice(0, 7);
      if (month !== lastMonth) {
        lastMonth = month;
        const h = document.createElement("div");
        h.className = "day-month";
        h.textContent = month.replace("-", " 年 ") + " 月";
        el.days.appendChild(h);
      }
      const b = document.createElement("button");
      b.className = "day" + (d.date === S.date ? " on" : "");
      b.dataset.date = d.date;
      const pct = Math.max(3, (d.seconds / maxSec) * 100);
      b.innerHTML =
        `<span class="day-d">${d.date.slice(5)}<em>${weekdayOf(d.date)}</em></span>` +
        `<span class="day-bar"><i style="width:${pct.toFixed(1)}%"></i></span>` +
        `<span class="day-c">${(d.seconds / 3600).toFixed(1)}h</span>`;
      b.title = `${d.date} ${weekdayOf(d.date)} · ${d.count} 个片段 · ${humanSize(d.bytes)}`;
      b.onclick = () => selectDay(d.date);
      el.days.appendChild(b);
    }
  }

  function bindFilter() {
    let t = null;
    el.daySearch.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => {
        S.filter.text = el.daySearch.value;
        applyFilter();
      }, 120);
    });
    el.daySearchClear.onclick = () => {
      el.daySearch.value = "";
      S.filter.text = "";
      applyFilter();
      el.daySearch.focus();
    };
    el.dayChips.addEventListener("click", (e) => {
      const btn = e.target.closest(".chip");
      if (!btn) return;
      S.filter.quick = btn.dataset.f;
      el.dayChips.querySelectorAll(".chip").forEach((c) =>
        c.classList.toggle("on", c === btn));
      applyFilter();
    });
  }

  /* ------------------------------ 选日期 ---------------------------- */
  /* 选日期只定位到当天第一段（或上次位置）并显示封面：
   * 不加载视频、不触发转码，必须再点时间点或播放键才起播。 */
  async function selectDay(date, opts) {
    const autoplay = !!(opts && opts.autoplay === true);
    if (S.date === date && S.segs.length) return;
    S.date = date;
    location.hash = date;
    localStorage.setItem(`cam.last.${S.folder || "default"}`, date);
    document.querySelectorAll(".day").forEach((b) =>
      b.classList.toggle("on", b.dataset.date === date));

    loading("读取当天索引…");
    let data;
    try {
      data = await api(`/api/day?date=${date}`);
    } catch (e) {
      return toast(`读取失败：${e.message}`, 4000);
    }
    S.segs = data.segments;
    S.idx = -1;
    S.origin = 0;
    S.retries = 0;

    const total = data.segments.reduce((a, s) => a + s.sz, 0);
    const hours = (data.seconds / 3600).toFixed(1);
    el.dayLabel.textContent = date;
    el.dayMeta.textContent =
      `${data.count} 个片段 · 共 ${hours} 小时录像 · ${humanSize(total)}`;
    el.miDate.textContent = `${date} ${weekdayOf(date)}`;
    el.miFacts.innerHTML =
      `<li>${data.count} 段</li><li>${hours} 小时</li><li>${humanSize(total)}</li>`;

    drawTimeline();
    if (!S.segs.length) {
      el.msgSpin.classList.add("off");
      el.msgText.textContent = "这一天没有可播放的片段";
      el.tlClock.textContent = "--:--:--";
      el.tlGap.textContent = "";
      return;
    }
    const posKey = `cam.pos.${S.folder || "default"}.${date}`;
    const lastPos = Number(localStorage.getItem(posKey) || 0);
    const start = lastPos > 0 && lastPos < DAY ? lastPos : S.segs[0].s;
    await seekTo(start, autoplay);
  }

  /* --------------------------- 时间段查找 --------------------------- */
  function indexAt(T) {
    let lo = 0, hi = S.segs.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const s = S.segs[mid];
      if (T < s.s) hi = mid - 1;
      else if (T >= s.s + s.t) lo = mid + 1;
      else return mid;
    }
    return -1;
  }

  function nextAfter(T) {
    let lo = 0, hi = S.segs.length - 1, res = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (S.segs[mid].s + S.segs[mid].t > T) { res = mid; hi = mid - 1; }
      else lo = mid + 1;
    }
    return res;
  }

  async function seekTo(T, autoplay) {
    if (!S.segs.length) return;
    T = Math.max(0, Math.min(DAY - 1, T));
    const i = indexAt(T);
    if (i >= 0) return startSeg(i, T - S.segs[i].s, autoplay !== false);
    const j = nextAfter(T);
    if (j < 0) return toast("此后没有录像了");
    const gap = S.segs[j].s - T;
    if (gap > 30) toast(`跳过 ${Math.round(gap / 60)} 分钟空档`, 1500);
    return startSeg(j, 0, autoplay !== false);
  }

  /* 冻结当前帧，避免切段时黑屏 */
  function freezeFrame() {
    const v = el.video;
    if (!v.videoWidth || v.readyState < 2) return;
    try {
      const c = document.createElement("canvas");
      c.className = "freeze";
      c.width = v.videoWidth;
      c.height = v.videoHeight;
      c.getContext("2d").drawImage(v, 0, 0);
      el.player.appendChild(c);
      setTimeout(() => c.remove(), 1200);
    } catch (_) { /* 解码异常时忽略 */ }
  }

  /* 未起播时的封面：用服务端抽帧缩略图，成本远低于转码一整段 */
  let coverToken = 0;
  let coverTimer = null;
  function showCover(seg, off, text) {
    const tok = ++coverToken;
    el.bigPlay.classList.remove("hide");
    if (text) {
      el.hint.textContent = text;
      el.hint.classList.remove("hide");
    } else {
      el.hint.classList.add("hide");
    }
    clearTimeout(coverTimer);
    // 拖动时间轴时连续触发，稍等一下再抽帧，别把 ffmpeg 打满
    coverTimer = setTimeout(() => {
      if (tok !== coverToken) return;
      el.cover.onload = () => { if (tok === coverToken) el.cover.classList.remove("hide"); };
      el.cover.onerror = () => { if (tok === coverToken) el.cover.classList.add("hide"); };
      el.cover.src = `/api/thumb?id=${encodeURIComponent(seg.id)}&t=${(off + 0.6).toFixed(2)}`;
    }, 180);
  }
  function hideCover() {
    coverToken++;
    clearTimeout(coverTimer);
    el.cover.classList.add("hide");
    el.bigPlay.classList.add("hide");
    el.hint.classList.add("hide");
  }

  async function startSeg(i, off, autoplay) {
    if (i < 0 || i >= S.segs.length) return;
    const seg = S.segs[i];
    off = Math.max(0, Math.min(off || 0, Math.max(0, seg.t - 0.3)));

    // 只定位、不播放：不请求 /api/stream，避免为了显示一帧就启动一次转码
    if (!autoplay) {
      // 已缓存的同一段：直接 seek 到目标帧，拖动时间轴时能立刻看到画面
      const sameFile = seg.cached && i === S.idx && S.mode === "file" && el.video.currentSrc;
      S.pending = { i, off };
      S.idx = i;
      S.playing = false;
      if (sameFile) {
        S.origin = 0;
        hideCover();
        try { el.video.pause(); el.video.currentTime = off; } catch (_) {}
        idleHint(`已定位到 ${hms(seg.s + off)}，点 ▶ 继续`);
        setTimeout(updateClock, 120);
      } else {
        S.origin = off;
        S.mode = seg.cached ? "file" : "stream";
        try { el.video.pause(); el.video.removeAttribute("src"); el.video.load(); } catch (_) {}
        showCover(seg, off, `点击播放 · 从 ${hms(seg.s + off)} 开始`);
        ready();
      }
      updatePlayBtn();
      updateBadge();
      updateClock();
      return;
    }

    S.pending = null;
    hideCover();

    const cached = await checkCached(seg);

    // 已缓存且同段：直接 seek，不需要重载
    if (cached && i === S.idx && S.mode === "file" && el.video.currentSrc && !el.video.ended) {
      try {
        el.video.currentTime = off;
        S.origin = 0;
        if (autoplay) el.video.play().catch(() => {});
        return;
      } catch (_) { /* 落到重载流程 */ }
    }

    if (S.idx !== i) freezeFrame();
    S.idx = i;
    S.origin = cached ? 0 : off;
    S.mode = cached ? "file" : "stream";
    if (!cached) {
      if (autoplay) loading("转码中…");
      else idleHint();
    }

    const id = encodeURIComponent(seg.id);
    if (cached) {
      el.video.src = `/api/media?id=${id}`;
      const onMeta = () => {
        el.video.removeEventListener("loadedmetadata", onMeta);
        try { el.video.currentTime = off; } catch (_) {}
      };
      el.video.addEventListener("loadedmetadata", onMeta);
      el.video.load();
    } else {
      el.video.src = `/api/stream?id=${id}&ss=${off.toFixed(2)}`;
      el.video.load();
      api(`/api/prepare?id=${id}`).then(() => { seg.cached = true; }).catch(() => {});
    }

    el.video.playbackRate = S.rate;
    if (autoplay) {
      S.playing = true;
      el.video.play().catch(() => {
        S.playing = false;
        updatePlayBtn();
        idleHint("浏览器拦截了自动播放，点 ▶ 开始");
      });
    } else {
      S.playing = false;
      idleHint();
    }
    updatePlayBtn();
    updateBadge();
    updateClock();
  }

  async function checkCached(seg) {
    if (seg.cached) return true;
    try {
      const st = await api(`/api/status?id=${encodeURIComponent(seg.id)}`);
      seg.cached = st.cached;
      if (st.dur && Math.abs(st.dur - seg.t) > 0.5) {
        seg.t = st.dur;
        drawTimeline();
      }
      return st.cached;
    } catch (_) {
      return false;
    }
  }

  function currentAbs() {
    if (S.idx < 0) return 0;
    const seg = S.segs[S.idx];
    const local = S.origin + (el.video.currentTime || 0);
    return seg.s + Math.max(0, Math.min(local, seg.t));
  }

  async function goNext() {
    if (S.idx < 0) return;
    if (S.idx + 1 < S.segs.length) return startSeg(S.idx + 1, 0, true);
    toast("已经是最后一段");
  }

  async function goPrev() {
    if (S.idx < 0) return;
    const inSeg = (el.video.currentTime || 0) - S.origin;
    if (inSeg > 3 || S.idx === 0) return startSeg(S.idx, 0, true);
    return startSeg(S.idx - 1, 0, true);
  }

  /* ----------------------------- 播放控制 --------------------------- */
  function togglePlay() {
    if (S.idx < 0) return;
    if (S.pending) {                    // 停在封面：第一次点播放才真正加载
      const p = S.pending;
      S.pending = null;
      startSeg(p.i, p.off, true);
      return;
    }
    if (el.video.paused) {
      el.video.play().then(() => { S.playing = true; updatePlayBtn(); })
        .catch(() => toast("浏览器阻止了播放，点一下画面", 2500));
    } else {
      el.video.pause();
    }
  }

  function updatePlayBtn() {
    el.play.textContent = S.playing ? "⏸" : "▶";
    el.play.title = S.playing ? "暂停 (空格)" : "播放 (空格)";
  }

  function updateBadge() {
    const seg = S.segs[S.idx];
    if (!seg) { el.badge.classList.remove("show"); return; }
    el.badge.classList.add("show");
    if (S.pending) {
      el.badge.innerHTML = `<span class="dot idle"></span>已定位 · 待播放`;
    } else if (S.mode === "file") {
      el.badge.innerHTML = `<span class="dot"></span>本地缓存 · 秒开`;
    } else {
      el.badge.innerHTML = `<span class="dot trans"></span>实时转码 720p`;
    }
  }

  function updateClock() {
    if (S.idx < 0) return;
    const t = currentAbs();
    el.tlClock.textContent = hms(t);
    el.osd.textContent = `${S.date} ${hms(t)}`;
    paintCursor(t);

    const seg = S.segs[S.idx];
    const played = S.origin + (el.video.currentTime || 0);
    const left = Math.max(0, seg.t - played);
    let txt = `本段剩 ${Math.round(left)}s`;
    const nxt = S.segs[S.idx + 1];
    if (nxt) {
      const gap = nxt.s - (seg.s + seg.t);
      if (gap >= 60) txt += ` · 之后空 ${Math.round(gap / 60)} 分钟`;
      else if (gap > 5) txt += ` · 之后空 ${Math.round(gap)} 秒`;
    }
    el.tlGap.textContent = txt;
  }

  /* ------------------------------ 时间轴 ---------------------------- */
  /* 颜色全部从 CSS 变量读，切换主题时重画即可跟着变 */
  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function drawTimeline() {
    const cv = el.canvas;
    const rect = el.tl.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(rect.width * dpr));
    const h = Math.max(1, Math.round(rect.height * dpr));
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    const g = cv.getContext("2d");
    g.clearRect(0, 0, w, h);

    const cTrack = cssVar("--track", "#0f1216");
    const cLine = cssVar("--track-line", "#1c2129");
    const cTop = cssVar("--accent-2", "#ff9a4d");
    const cBot = cssVar("--accent", "#ff6900");

    g.fillStyle = cTrack;
    g.fillRect(0, 0, w, h);
    g.strokeStyle = cLine;
    g.lineWidth = 1;
    for (let hr = 0; hr <= 24; hr += 2) {
      const x = Math.round((hr / 24) * w) + 0.5;
      g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
    }
    if (!S.segs.length) { S.blocks = []; return; }

    // 合并相邻片段（间隔 ≤2 秒视为连续），避免 1 分钟碎片画成 0.75px。
    // 已缓存与未缓存的块分开画：缓存过的更亮，一眼能看出哪些能秒开。
    const blocks = [];
    let cur = null;
    for (const s of S.segs) {
      const e = s.s + s.t;
      const cached = !!s.cached;
      if (cur && cur.cached === cached && s.s - cur.e <= 2) cur.e = Math.max(cur.e, e);
      else { cur = { b: s.s, e, cached }; blocks.push(cur); }
    }
    S.blocks = blocks;

    const per = w / DAY;
    const y = Math.round(h * 0.2);
    const bh = Math.round(h * 0.6);
    for (const blk of blocks) {
      const x = blk.b * per;
      const bw = Math.max(1.5 * dpr, (blk.e - blk.b) * per);
      const grad = g.createLinearGradient(0, y, 0, y + bh);
      grad.addColorStop(0, cTop);
      grad.addColorStop(1, cBot);
      g.globalAlpha = blk.cached ? 1 : 0.45;
      g.fillStyle = grad;
      g.beginPath();
      if (g.roundRect) g.roundRect(x, y, bw, bh, Math.min(3 * dpr, bw / 2));
      else g.rect(x, y, bw, bh);
      g.fill();
    }
    g.globalAlpha = 1;
  }

  function paintCursor(absSec) {
    const w = el.tl.clientWidth;
    el.cursor.style.transform = `translateX(${(absSec / DAY) * w}px)`;
  }

  function tlXToSec(clientX) {
    const r = el.tl.getBoundingClientRect();
    return Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * DAY;
  }

  /* 空档不接受定位：点击落到没有录像的位置时不跳转，拖动经过空档时游标停住，
   * 避免"点了个空白处却跳到半小时后"的错觉。连播跨段仍由 timeupdate 负责。 */
  function initTimeline() {
    let dragging = false;
    let lastEmptyToast = -9999;

    function rejected(T) {
      if (Math.abs(T - lastEmptyToast) > 40) {
        lastEmptyToast = T;
        toast("这个时间点没有录像", 1400);
      }
    }

    el.tl.addEventListener("pointerdown", (e) => {
      if (!S.segs.length) return;
      const T = tlXToSec(e.clientX);
      if (indexAt(T) < 0) { rejected(T); return; }
      dragging = true;
      el.tl.setPointerCapture && el.tl.setPointerCapture(e.pointerId);
      seekTo(T, true);
    });

    el.tl.addEventListener("pointermove", (e) => {
      const r = el.tl.getBoundingClientRect();
      const T = tlXToSec(e.clientX);
      const has = indexAt(T) >= 0;
      el.hover.style.left = `${e.clientX - r.left}px`;
      el.hover.classList.add("show");
      el.hover.innerHTML =
        `<b>${hms(T)}</b><span class="tl-hover-tag${has ? "" : " off"}">${has ? "有录像" : "无录像"}</span>`;
      el.tl.classList.toggle("no-hit", !has);
      if (dragging && has) seekTo(T, S.playing);
    });

    const stop = () => { dragging = false; };
    el.tl.addEventListener("pointerup", stop);
    el.tl.addEventListener("pointercancel", stop);
    el.tl.addEventListener("pointerleave", () => {
      dragging = false;
      el.hover.classList.remove("show");
      el.tl.classList.remove("no-hit");
    });
  }

  function renderScale() {
    const marks = [];
    for (let hr = 0; hr <= 24; hr += 3) marks.push(`${pad(hr)}:00`);
    el.tlScale.innerHTML = marks.map((m) => `<span>${m}</span>`).join("");
  }

  /* --------------------------- 文件夹选择器 -------------------------- */
  function openFolderPicker(hint) {
    S.fs.path = (S.cfg && S.cfg.root) || "";
    S.fs.selected = -1;
    el.fsModal.hidden = false;
    if (hint) el.fsHint.textContent = hint;
    else el.fsHint.textContent = "选中包含录像的目录，点右下角确认";

    el.fsRecent.innerHTML = "";
    const recent = (S.cfg && S.cfg.recent) || [];
    for (const p of recent.slice(0, 5)) {
      const b = document.createElement("button");
      b.textContent = p;
      b.title = p;
      b.onclick = () => fsGoto(p);
      el.fsRecent.appendChild(b);
    }
    el.fsRecent.hidden = recent.length === 0;

    fsLoadDrives();
    fsGoto(S.fs.path);
    renderLanAddresses();
    el.fsUploadState.textContent = "";
  }

  /* 手机访问地址：只在服务监听 0.0.0.0 时才有 */
  function renderLanAddresses() {
    const lan = (S.cfg && S.cfg.lan) || [];
    if (!lan.length) {
      el.fsNet.innerHTML = `<span class="net-tip">当前仅本机可访问。想让手机连，启动时不要加 <code>--host 127.0.0.1</code>。</span>`;
      return;
    }
    el.fsNet.innerHTML = "手机访问："
      + lan.map((u) => `<code data-url="${esc(u)}">${esc(u)}</code>`).join(" ")
      + `<span class="net-tip">同一 Wi-Fi 下用手机浏览器打开即可，点地址可复制。</span>`;
    el.fsNet.querySelectorAll("code").forEach((c) => {
      c.onclick = async () => {
        try {
          await navigator.clipboard.writeText(c.dataset.url);
          toast("地址已复制");
        } catch (_) {
          toast(c.dataset.url, 4000);
        }
      };
    });
  }

  /* ------------------------ 上传当前设备上的视频 ---------------------- */
  function batchId() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-`
      + `${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  }

  function uploadOne(file, batch, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/upload?batch=${batch}&name=${encodeURIComponent(file.name)}`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) return resolve();
        let msg = `HTTP ${xhr.status}`;
        try { msg = JSON.parse(xhr.responseText).error || msg; } catch (_) {}
        reject(new Error(msg));
      };
      xhr.onerror = () => reject(new Error("网络中断"));
      xhr.ontimeout = () => reject(new Error("上传超时"));
      xhr.send(file);
    });
  }

  async function uploadFiles(fileList) {
    const files = Array.from(fileList || []).filter((f) => f.size > 0);
    if (!files.length) return;
    const batch = batchId();
    el.fsUploadBtn.disabled = true;
    el.fsOk.disabled = true;
    let done = 0;
    try {
      for (const f of files) {
        el.fsUploadState.textContent =
          `上传中 ${done + 1}/${files.length} · ${(f.size / 1048576).toFixed(0)}MB · 0%`;
        await uploadOne(f, batch, (pct) => {
          el.fsUploadState.textContent =
            `上传中 ${done + 1}/${files.length} · ${(f.size / 1048576).toFixed(0)}MB · ${pct}%`;
        });
        done++;
      }
      el.fsUploadState.textContent = "读取时长并建立时间轴…";
      const res = await api(`/api/upload/finish?batch=${batch}`, { method: "POST" });
      el.fsUploadState.textContent = `已上传 ${res.files} 个，约 ${Math.round(res.seconds / 60)} 分钟录像`;
      await api(`/api/fs/open?path=${encodeURIComponent(res.dir)}`);
      closeFolderPicker();
      resetPlayer();
      await pollScan();
    } catch (e) {
      el.fsUploadState.textContent = `上传失败：${e.message}`;
      toast(`上传失败：${e.message}`, 5000);
    } finally {
      el.fsUploadBtn.disabled = false;
      el.fsOk.disabled = false;
      el.fsUploadInput.value = "";
    }
  }

  function closeFolderPicker() {
    el.fsModal.hidden = true;
  }

  async function fsLoadDrives() {
    let drives = [];
    try {
      drives = await api("/api/fs/drives");
    } catch (_) {}
    el.fsDrives.innerHTML = "";
    for (const d of drives) {
      const b = document.createElement("button");
      b.className = "fs-drive";
      const kindTxt = d.kind === "network" ? "网络" : d.kind === "removable" ? "移动" : "";
      b.innerHTML = `${d.name}${kindTxt ? `<span class="k">${kindTxt}</span>` : ""}`;
      b.title = d.path;
      if (S.fs.path && S.fs.path.toUpperCase().startsWith(d.path.toUpperCase())) {
        b.classList.add("on");
      }
      b.onclick = () => fsGoto(d.path);
      el.fsDrives.appendChild(b);
    }
  }

  async function fsGoto(path) {
    S.fs.selected = -1;
    if (!path) {
      el.fsPath.value = "";
      el.fsList.innerHTML = `<div class="fs-empty">先点上方选择一个磁盘</div>`;
      return;
    }
    el.fsPath.value = path;
    el.fsList.innerHTML = `<div class="fs-empty">读取中…</div>`;
    let data;
    try {
      data = await api(`/api/fs/list?path=${encodeURIComponent(path)}`);
    } catch (e) {
      el.fsList.innerHTML = `<div class="fs-empty">${esc(e.message)}</div>`;
      return;
    }
    S.fs.path = data.path;
    el.fsPath.value = data.path;
    S.fs.parent = data.parent;
    S.fs.dirs = data.dirs;

    if (!data.dirs.length) {
      el.fsList.innerHTML =
        `<div class="fs-empty">这个目录下没有子文件夹${data.dirs.length === 0 ? "，可以直接用当前目录" : ""}</div>`;
    } else {
      el.fsList.innerHTML = data.dirs.map((d, i) => `
        <button class="fs-item" data-i="${i}">
          <span class="ico">📁</span>
          <span class="nm">${esc(d.name)}</span>
          ${d.video ? '<span class="tag">含视频</span>' : ""}
        </button>`).join("");
      el.fsList.querySelectorAll(".fs-item").forEach((btn) => {
        btn.onclick = () => {
          const d = S.fs.dirs[Number(btn.dataset.i)];
          if (d) fsGoto(d.path);
        };
      });
    }
    // 盘符高亮跟随
    el.fsDrives.querySelectorAll(".fs-drive").forEach((b) => {
      b.classList.toggle("on", !!b.title && data.path.toUpperCase().startsWith(b.title.toUpperCase()));
    });
    el.fsUp.disabled = !data.parent;
  }

  async function fsConfirm() {
    const path = S.fs.path;
    if (!path) return toast("先选一个文件夹");
    el.fsOk.textContent = "打开中…";
    el.fsOk.disabled = true;
    try {
      await api(`/api/fs/open?path=${encodeURIComponent(path)}`);
      closeFolderPicker();
      resetPlayer();
      await pollScan();
    } catch (e) {
      toast(`打开失败：${e.message}`, 4000);
    } finally {
      el.fsOk.textContent = "用这个文件夹";
      el.fsOk.disabled = false;
    }
  }

  function resetPlayer() {
    hideCover();
    S.pending = null;
    try {
      el.video.pause();
      el.video.removeAttribute("src");
      el.video.load();
    } catch (_) {}
    S.segs = [];
    S.idx = -1;
    S.origin = 0;
    S.playing = false;
    S.date = null;
    el.dayLabel.textContent = "—";
    el.dayMeta.textContent = "";
    el.miDate.textContent = "—";
    el.miFacts.innerHTML = "";
    el.days.innerHTML = "";
    el.tlClock.textContent = "--:--:--";
    el.tlGap.textContent = "";
    el.badge.classList.remove("show");
    drawTimeline();
    updatePlayBtn();
  }

  function bindFolder() {
    el.folder.onclick = () => openFolderPicker();
    el.folder2.onclick = () => openFolderPicker();
    el.fsCancel.onclick = closeFolderPicker;
    el.fsClose.onclick = closeFolderPicker;
    el.fsOk.onclick = fsConfirm;
    el.fsUploadBtn.onclick = () => el.fsUploadInput.click();
    el.fsUploadInput.onchange = () => uploadFiles(el.fsUploadInput.files);
    el.fsUp.onclick = () => { if (S.fs.parent) fsGoto(S.fs.parent); };
    el.fsModal.addEventListener("click", (e) => {
      if (e.target === el.fsModal) closeFolderPicker();
    });
    el.fsPath.addEventListener("keydown", (e) => {
      if (e.key === "Enter") fsGoto(el.fsPath.value.trim());
    });
  }

  /* ------------------------------- 配色 ----------------------------- */
  const THEMES = [
    { id: "amber",  name: "琥珀", dot: "#ff6900" },
    { id: "aurora", name: "冰川", dot: "#2f9bff" },
    { id: "jade",   name: "翡翠", dot: "#12b981" },
    { id: "violet", name: "霓虹", dot: "#a259ff" },
    { id: "rose",   name: "玫瑰", dot: "#ff3d71" },
    { id: "paper",  name: "素白", dot: "#eef1f5" },
  ];

  function applyTheme(id, persist) {
    const t = THEMES.find((x) => x.id === id) || THEMES[0];
    document.documentElement.dataset.theme = t.id;
    if (persist !== false) {
      try { localStorage.setItem("cam.theme", t.id); } catch (_) {}
    }
    el.themeGrid.querySelectorAll(".theme-item").forEach((b) =>
      b.classList.toggle("on", b.dataset.id === t.id));
    // 时间轴是 canvas 画的，颜色得重画一遍
    drawTimeline();
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = cssVar("--bg", "#0a0c0f");
  }

  function bindTheme() {
    el.themeGrid.innerHTML = THEMES.map((t) => `
      <button class="theme-item" data-id="${t.id}">
        <span class="theme-dot" style="background:${t.dot}"></span>${t.name}
      </button>`).join("");

    el.themeGrid.onclick = (e) => {
      const b = e.target.closest(".theme-item");
      if (b) applyTheme(b.dataset.id);
    };
    el.themeBtn.onclick = (e) => {
      e.stopPropagation();
      el.themePop.hidden = !el.themePop.hidden;
    };
    document.addEventListener("click", (e) => {
      if (el.themePop.hidden) return;
      if (e.target instanceof Element && e.target.closest(".theme-wrap")) return;
      el.themePop.hidden = true;
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !el.themePop.hidden) el.themePop.hidden = true;
    });

    let saved = "";
    try { saved = localStorage.getItem("cam.theme") || ""; } catch (_) {}
    applyTheme(saved || "amber", false);
  }

  /* ------------------------------- 事件 ----------------------------- */
  function bindVideo() {
    const v = el.video;

    el.bigPlay.onclick = (e) => { e.stopPropagation(); togglePlay(); };
    el.cover.onclick = (e) => { e.stopPropagation(); togglePlay(); };

    v.addEventListener("timeupdate", () => {
      if (S.idx < 0) return;
      updateClock();
      const seg = S.segs[S.idx];
      if (S.playing && S.origin + v.currentTime >= seg.t - 0.4) {
        if (S.idx + 1 < S.segs.length) {
          S.retries = 0;
          startSeg(S.idx + 1, 0, true);
        } else {
          v.pause();
        }
      }
    });

    v.addEventListener("play", () => { S.playing = true; updatePlayBtn(); });
    v.addEventListener("pause", () => { S.playing = false; updatePlayBtn(); });
    v.addEventListener("waiting", () => {
      if (S.playing) loading(S.mode === "stream" ? "转码中…" : "缓冲中…");
    });
    v.addEventListener("playing", () => { ready(); S.retries = 0; });

    v.addEventListener("error", () => {
      if (!v.currentSrc) return;
      const seg = S.segs[S.idx];
      if (seg && S.retries < 1 && S.mode === "file") {
        seg.cached = false;      // 缓存文件有问题，回退实时转码重试一次
        S.retries++;
        return startSeg(S.idx, 0, S.playing);
      }
      if (S.idx + 1 < S.segs.length) {
        S.retries = 0;
        toast("片段无法播放，跳到下一段", 1500);
        startSeg(S.idx + 1, 0, S.playing);
      } else {
        el.msgSpin.classList.add("off");
        el.msgText.textContent = "该片段无法播放";
      }
    });

    v.addEventListener("ended", () => { S.playing = false; updatePlayBtn(); });
  }

  function applyRate(r) {
    S.rate = r;
    el.video.playbackRate = r;
    el.rateSel.value = String(r);
  }

  function bindControls() {
    el.play.onclick = togglePlay;
    el.prev.onclick = goPrev;
    el.next.onclick = goNext;
    el.back10.onclick = () => seekTo(currentAbs() - 10, S.playing);
    el.fwd10.onclick = () => seekTo(currentAbs() + 10, S.playing);

    el.rateSel.addEventListener("change", () => {
      applyRate(Number(el.rateSel.value) || 1);
    });

    el.mute.onclick = () => {
      el.video.muted = !el.video.muted;
      el.mute.textContent = el.video.muted ? "🔇" : "🔊";
      el.mute.classList.toggle("on", el.video.muted);
      el.mute.title = el.video.muted ? "已静音，点击开启声音 (M)" : "有声音，点击静音 (M)";
    };

    el.shot.onclick = () => {
      const v = el.video;
      if (!v.videoWidth) return toast("画面还没准备好");
      const c = document.createElement("canvas");
      c.width = v.videoWidth;
      c.height = v.videoHeight;
      c.getContext("2d").drawImage(v, 0, 0);
      const a = document.createElement("a");
      a.download = `${S.date}_${hms(currentAbs()).replace(/:/g, "")}.png`;
      a.href = c.toDataURL("image/png");
      a.click();
      toast("截图已保存");
    };

    el.full.onclick = () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else if (el.player.requestFullscreen) el.player.requestFullscreen();
    };

    el.jump.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const t = parseTime(el.jump.value);
      if (t == null) return toast("格式：16:20 或 16:20:30");
      if (!S.segs.length) return toast("先选一个有录像的日期");
      if (indexAt(t) < 0) {
        const j = nextAfter(t);
        toast(j < 0
          ? "这个时间点之后没有录像了"
          : `这个时间点没有录像，下一段从 ${hms(S.segs[j].s)} 开始`, 2600);
        return;
      }
      seekTo(t, true);
      el.jump.blur();
    });

    el.refresh.onclick = async () => {
      if (!S.cfg || !S.cfg.root) return openFolderPicker();
      loading("重新扫描…");
      try {
        await api("/api/reindex");
        resetPlayer();
        await pollScan();
        toast("扫描完成");
      } catch (e) {
        toast(`扫描失败：${e.message}`, 4000);
      }
    };

    el.exportBtn.onclick = openExport;

    el.sideOpen.onclick = () => el.side.classList.add("open");
    el.sideClose.onclick = () => el.side.classList.remove("open");
    el.side.addEventListener("click", (e) => {
      if (e.target.closest(".day") && window.innerWidth <= 900) {
        el.side.classList.remove("open");
      }
    });

    document.addEventListener("keydown", (e) => {
      if (el.fsModal.hidden === false) {
        if (e.key === "Escape") closeFolderPicker();
        return;
      }
      if (e.target && e.target.closest && e.target.closest("input, textarea, select")) return;
      const k = e.key;
      if (k === " ") { e.preventDefault(); togglePlay(); }
      else if (k === "ArrowLeft") { e.preventDefault(); goPrev(); }
      else if (k === "ArrowRight") { e.preventDefault(); goNext(); }
      else if (k === "j" || k === "J") seekTo(currentAbs() - 10, S.playing);
      else if (k === "l" || k === "L") seekTo(currentAbs() + 10, S.playing);
      else if (k === "f" || k === "F") el.full.click();
      else if (k === "m" || k === "M") el.mute.click();
      else if (/^[1-9]$/.test(k)) {
        const r = Number(k);
        if ([0.5, 1, 2, 4, 8].includes(r)) applyRate(r);
      }
    });

    let rsTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(rsTimer);
      rsTimer = setTimeout(() => { drawTimeline(); paintCursor(currentAbs()); }, 120);
    });

    window.addEventListener("beforeunload", () => {
      if (S.date) {
        localStorage.setItem(`cam.pos.${S.folder || "default"}.${S.date}`,
          String(Math.round(currentAbs())));
      }
    });
  }

  function parseTime(text) {
    const m = String(text).trim().match(/^(\d{1,2}):?(\d{0,2}):?(\d{0,2})$/);
    if (!m) return null;
    const h = Number(m[1] || 0), mi = Number(m[2] || 0), s = Number(m[3] || 0);
    if (h > 23 || mi > 59 || s > 59) return null;
    return h * 3600 + mi * 60 + s;
  }

  /* ------------------------------- 导出 ----------------------------- */
  function openExport() {
    if (!S.segs.length) return toast("先选一个有录像的日期");
    const cur = currentAbs();
    const mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML = `
      <div class="modal">
        <h3>导出为单个 mp4</h3>
        <p class="modal-sub">把 ${esc(S.date)} 指定区间内的所有片段拼接、转码成一个文件。
        区间越大耗时越久，建议一次不超过 1 小时。</p>
        <label>开始 <input id="exFrom" value="${hms(cur)}"></label>
        <label>结束 <input id="exTo" value="${hms(Math.min(DAY - 1, cur + 600))}"></label>
        <div class="modal-act">
          <button class="pill" id="exCancel">取消</button>
          <button class="pill on" id="exGo">开始导出</button>
        </div>
      </div>`;
    document.body.appendChild(mask);

    const close = () => mask.remove();
    mask.addEventListener("click", (e) => { if (e.target === mask) close(); });
    mask.querySelector("#exCancel").onclick = close;
    mask.querySelector("#exGo").onclick = () => {
      const f = parseTime(mask.querySelector("#exFrom").value);
      const t = parseTime(mask.querySelector("#exTo").value);
      if (f == null || t == null) return toast("时间格式不对");
      if (t <= f) return toast("结束时间要晚于开始时间");
      close();
      const a = document.createElement("a");
      a.href = `/api/export?date=${S.date}&from=${hm(f)}&to=${hm(t)}`;
      a.download = "";
      document.body.appendChild(a);
      a.click();
      a.remove();
      toast("已开始导出，大区间需要几分钟", 4000);
    };
  }

  /* ------------------------------- 启动 ----------------------------- */
  renderScale();
  bindTheme();
  bindVideo();
  bindControls();
  bindFilter();
  bindFolder();
  initTimeline();
  boot();

  // 后台转码完成后刷新时间轴（缓存状态变了）
  setInterval(() => { if (S.segs.length) drawTimeline(); }, 20000);

  // 调试出口：控制台可查状态、可跳转，也方便自动化验证
  window.__cam = {
    S, seekTo, startSeg, currentAbs, selectDay, applyTheme,
    openFolderPicker, fsGoto, applyFilter, pollScan,
  };
})();
