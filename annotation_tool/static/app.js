"use strict";
// 装卸动作标注前端 v1.6
// v1.4: 人声打标（v/w 双模式、voice_segments 模型、Enter 弹窗+ASR 预填、s 切分、人声轨开关）
// v1.5: ↑/↓ 切换 ←/→ 作用的进度轴（上轴粗调±5s / 下轴微调单帧），免鼠标点击
// v1.6: 标注单元改为 ASR 命令词切分单元（/api/units）：命令词单元+响段空隙+asr_only；
//       数字键 1~8 直接打标（6=无关段 7=自定义并聚焦文本框 8=无人声段）、←/→ 跳未标单元、Delete 删单元、
//       文本自动预填可编辑；结果存 speech_units；actions 与 voice_segments 一律不动
// v2.0: M 键将当前单元与下一相邻单元手动合并（任意间隔；label 去重 + 拼接、原 id 记入 removed 防重）
const $ = (id) => document.getElementById(id);
const video = $("video"), timeline = $("timeline"), loudLayer = $("loudLayer"),
      asrLayer = $("asrLayer"),
      actionLayer = $("actionLayer"), markerLayer = $("markerLayer"), playhead = $("playhead"),
      audio = $("vocalAudio");

const FRAME = 0.04; // 25fps 单帧

const VOICE_LABELS = { up: "上升", up_small: "上升一点", down: "下降", down_small: "下降一点", stop: "停", non_cmd: "无关段", custom: "自定义", no_vocal: "无人声段" };
function voiceCn(t) { return VOICE_LABELS[t] || t || ""; }

const state = {
  folder: null, video: null, duration: 0,
  loudByVideo: {}, loud: [],      // 当前视频响段 [{start_sec,end_sec,start,end}]
  asr: [],                        // 当前视频 ASR 命令词 [{type, word, start, end}]
  videos: [],
  actions: [],                    // [{type:"up"|"down", start, end}] 单位秒
  undoStack: [],
  inPoint: null, outPoint: null,
  open: { up: null, down: null }, // 快速打标中的动作起点
  selected: null,
  dirty: false, saveTimer: null,
  // ---- v1.4 人声打标 ----
  mode: "video",                  // "video" 视频标注 | "voice" 单元打标（v/w 切换，仅改变 ←/→ 语义）
  voice: [],                      // [{start,end,label,status:null|"labeled"|"ignored",srcIdx}] 初始 seed 自响段
  curVoice: null,                 // 当前段索引 = 最近跳转选中的段 ∪ 播放头所在段
  vocalOn: false,                 // 人声轨开关（视频静音 + 独立 audio 播 vocals.wav）
  // ---- v1.6 命令词切分单元 ----
  units: [],                      // /api/units + 本地编辑合并后 [{id,kind,label,word,text,start_sec,end_sec,dur_sec,source,sentence,status}]
  curUnit: null,                  // 当前单元在 state.units 的下标
  removedUnitIds: [],             // Delete 删掉的生成单元 id（持久化，重建时过滤）
  unitLegacy: false,              // asr_v2 缺失、回退旧版 ASR（无空隙预填文本）
  panelTab: "actions",            // 右侧面板当前页签 actions | units
};

function clamp(t) { return Math.max(0, Math.min(state.duration || 1e9, t)); }
function fmt(t) {
  t = Math.max(0, t || 0);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const p = (n, w) => String(n).padStart(w, "0");
  return (h ? p(h, 2) + ":" : "") + p(m, 2) + ":" + s.toFixed(3).padStart(6, "0");
}
function setStatus(text) { $("saveStatus").textContent = text; }
function flash(text) { setStatus(text); clearTimeout(flash._t); flash._t = setTimeout(() => setStatus(state.dirty ? "有未保存修改" : ""), 2500); }
function cn(type) { return type === "up" ? "上升" : "下降"; }

// ---------- 数据加载 ----------
async function loadFolders() {
  const r = await fetch("/api/folders").then(r => r.json());
  const sel = $("folderSel");
  sel.innerHTML = "";
  if (!r.folders.length) { sel.innerHTML = "<option value=''>result/loud_segments 下暂无响段 JSON</option>"; return; }
  for (const f of r.folders) {
    const o = document.createElement("option");
    o.value = o.textContent = f;
    sel.appendChild(o);
  }
  selectFolder(r.folders[0]);
}

async function selectFolder(f) {
  flushSave();
  state.folder = f; state.video = null;
  $("folderSel").value = f;
  const r = await fetch("/api/videos?folder=" + encodeURIComponent(f)).then(r => r.json());
  const loud = await fetch("/api/loud?folder=" + encodeURIComponent(f)).then(r => r.json());
  state.loudByVideo = {};
  for (const it of (loud.files || [])) {
    state.loudByVideo[it.file.replace(/\.[^.]+$/, "")] = it.segments || [];
  }
  renderVideoList(r.videos || []);
  if ((r.videos || []).length) selectVideo(r.videos[0]);
}

function renderVideoList(videos) {
  state.videos = videos;
  const box = $("videoList");
  box.innerHTML = "";
  for (const v of videos) {
    const d = document.createElement("div");
    d.className = "vitem"; d.textContent = v; d.title = v; d.dataset.name = v;
    d.onclick = () => selectVideo(v);
    box.appendChild(d);
  }
}

function seedVoice() {
  return state.loud.map((s, i) => ({ start: s.start_sec, end: s.end_sec, label: null, status: null, srcIdx: i }));
}

// ---------- v1.6 单元合并：后端生成 ⊕ 已存标注（文本/标签/边界编辑、手动切分单元、删除记录） ----------
function mergeUnits(genUnits, savedUnits, removedIds) {
  const saved = {}, removed = new Set(removedIds || []);
  for (const u of (savedUnits || [])) saved[u.id] = u;
  const out = [];
  const consumed = new Set();
  for (const g of genUnits) {
    if (removed.has(g.id)) continue;
    const s = saved[g.id];
    if (s) consumed.add(s.id);
    out.push(s ? {
      ...g,
      text: s.text != null ? s.text : g.text,
      label: s.label != null ? s.label : g.label,
      status: s.status || null,
      start_sec: typeof s.start_sec === "number" ? s.start_sec : g.start_sec,
      end_sec: typeof s.end_sec === "number" ? s.end_sec : g.end_sec,
      dur_sec: +((typeof s.end_sec === "number" ? s.end_sec : g.end_sec)
                 - (typeof s.start_sec === "number" ? s.start_sec : g.start_sec)).toFixed(3),
    } : g);
  }
  // v1.8 后端 <0.5s 合并后旧 id 失配：cmd:/gap: 已存条目落在新单元来源或区间内 → 继承标签/状态，不再变手工重复条目
  const manualSaved = (savedUnits || []).filter(s => /^manual:/.test(s.id));
  const byMerged = {};
  for (const g of out) for (const mid of (g.merged_from || [])) byMerged[mid] = g;
  for (const s of (savedUnits || [])) {
    if (consumed.has(s.id) || removed.has(s.id) || !/^(cmd|gap):/.test(s.id)) continue;
    if (manualSaved.some(mi => (mi.start_sec ?? 1e18) <= (s.start_sec ?? 0) + 1e-6
        && (mi.end_sec ?? -1) >= (s.end_sec ?? 1e18) - 1e-6)) { consumed.add(s.id); continue; } // 已被切分子块覆盖 → 静默消化，避免鬼影行
    const g = byMerged[s.id]
      || out.find(u => u.start_sec <= (s.start_sec ?? -1) + 1e-6
                 && u.end_sec >= (s.end_sec ?? 1e18) - 1e-6);
    if (!g) continue; // 无法归属 → 仍按手工条目带回
    consumed.add(s.id);
    if (!g.label && s.label) g.label = s.label;
    if (!g.status && s.status) g.status = s.status;
  }
  const genIds = new Set(genUnits.map(u => u.id));
  for (const s of (savedUnits || [])) {
    if (!genIds.has(s.id) && !consumed.has(s.id) && !removed.has(s.id)) { // 切分/新增的手工单元原样带回
      out.push({ kind: "manual", label: "", word: "", text: "", sentence: "",
                 source: "manual_edit", status: null, ...s });
    }
  }
  out.sort((a, b) => a.start_sec - b.start_sec || a.end_sec - b.end_sec);
  return out;
}

async function selectVideo(name) {
  flushSave();
  const keepVocal = state.vocalOn || pendingVocal;
  setVocal(false);
  state.video = name;
  state.actions = []; state.undoStack = []; state.inPoint = state.outPoint = null;
  state.open = { up: null, down: null }; state.selected = null; state.duration = 0;
  state.asr = [];
  state.voice = []; state.curVoice = null; closeVoiceModal();
  state.units = []; state.curUnit = null; state.removedUnitIds = []; state.unitLegacy = false;
  document.querySelectorAll(".vitem").forEach(el => el.classList.toggle("active", el.dataset.name === name));
  state.loud = state.loudByVideo[name.replace(/\.[^.]+$/, "")] || [];
  state.voice = seedVoice();
  renderTimeline(); renderActionList();
  $("transcoding").textContent = "加载 / 转码中，请稍候…";
  $("transcoding").classList.remove("hidden");
  unitStopAt = null; // v1.7: 切视频清除段播放约束
  video.src = "/media/" + encodeURIComponent(state.folder) + "/" + encodeURIComponent(name);
  if (keepVocal) setVocal(true);
  const [ann, asr, unitsR] = await Promise.all([
    fetch("/api/annotation?folder=" + encodeURIComponent(state.folder)
          + "&video=" + encodeURIComponent(name)).then(r => r.json()),
    fetch("/api/asr?folder=" + encodeURIComponent(state.folder)
          + "&video=" + encodeURIComponent(name)).then(r => r.json()).catch(() => ({})),
    fetch("/api/units?folder=" + encodeURIComponent(state.folder)
          + "&video=" + encodeURIComponent(name)).then(r => r.json()).catch(() => ({ units: [] })),
  ]);
  state.actions = (ann.actions || []).map(a => ({ type: a.type, start: a.start_sec, end: a.end_sec }));
  state.asr = (asr.commands || []).map(c => ({
    type: c.label, word: c.word, start: +c.start_sec || 0, end: +c.end_sec || 0,
  }));
  if (Array.isArray(ann.voice_segments) && ann.voice_segments.length) {
    state.voice = ann.voice_segments.map(v => ({
      start: +v.start_sec || 0, end: +v.end_sec || 0,
      label: v.label || null,
      status: (v.status === "labeled" || v.status === "ignored") ? v.status : (v.label ? "labeled" : null),
      srcIdx: typeof v.source_segment_idx === "number" ? v.source_segment_idx : null,
    }));
  } else {
    state.voice = seedVoice();
  }
  state.curVoice = null;
  // v1.6: 命令词切分单元（/api/units 生成 ⊕ speech_units 已存编辑）
  state.unitLegacy = !!unitsR.legacy;
  state.unitStats = unitsR.stats || {};
  state.removedUnitIds = ann.removed_unit_ids || [];
  state.units = mergeUnits(unitsR.units || [], ann.speech_units || [], state.removedUnitIds);
  state.curUnit = null;
  renderTimeline(); renderActionList(); renderUnitList();
}

// ---------- 时间轴渲染 ----------
function pct(t) { return (t / (state.duration || 1)) * 100; }

function renderTimeline() {
  loudLayer.innerHTML = ""; asrLayer.innerHTML = ""; actionLayer.innerHTML = ""; markerLayer.innerHTML = "";
  if (state.mode === "voice") { // v1.6: 单元层替换响段层（响段已被命令词切成单元）
    state.units.forEach((u, i) => {
      const d = document.createElement("div");
      let cls = "unitseg u-" + (u.kind || "cmd");
      if (u.status === "labeled" && u.label) cls += " lv-" + u.label;
      else if (u.status === "ignored") cls += " ignored";
      if (i === state.curUnit) cls += " cur";
      d.className = cls;
      d.style.left = pct(u.start_sec) + "%";
      d.style.width = Math.max(0.15, pct(u.end_sec) - pct(u.start_sec)) + "%";
      d.title = unitTitle(u, i) + "（点击选中；拖动两端改边界）";
      d.onclick = (e) => { e.stopPropagation(); selectUnit(i); };
      const l = document.createElement("div"); l.className = "handle hl";
      const r = document.createElement("div"); r.className = "handle hr";
      d.appendChild(l); d.appendChild(r);
      bindUnitDrag(l, i, "start"); bindUnitDrag(r, i, "end");
      loudLayer.appendChild(d);
    });
  } else {
    state.voice.forEach((s, i) => { // 视频模式维持 v1.4 响段/人声段显示
      const d = document.createElement("div");
      let cls = "loudseg";
      if (s.status === "ignored") cls += " ignored";
      else if (s.status === "labeled" && s.label) cls += " lv-" + s.label;
      if (i === state.curVoice) cls += " cur";
      d.className = cls;
      d.style.left = pct(s.start) + "%";
      d.style.width = Math.max(0.15, pct(s.end) - pct(s.start)) + "%";
      d.title = `人声段 ${i + 1}/${state.voice.length} ${fmt(s.start)} - ${fmt(s.end)}`
        + (s.status === "labeled" ? `【${voiceCn(s.label)}】`
          : s.status === "ignored" ? "【忽略】" : "（未处理，点击跳到段起点）");
      d.onclick = (e) => { e.stopPropagation(); seek(s.start); };
      loudLayer.appendChild(d);
    });
  }
  state.actions.forEach((a, i) => actionLayer.appendChild(actionEl(a, i)));
  renderAsrLayer();
  renderOpenBlocks();
  renderPoints();
  updateTimeLabel();
}

function unitTitle(u, i) {
  return `单元 ${i + 1}/${state.units.length} ${u.kind === "cmd" ? "命令词" : u.kind === "gap" ? "空隙" : "手工"} `
    + `${fmt(u.start_sec)} - ${fmt(u.end_sec)}（${(u.dur_sec || u.end_sec - u.start_sec).toFixed(2)}s）`
    + (u.text ? ` “${u.text}”` : "")
    + (u.status === "labeled" ? `【${voiceCn(u.label)}】` : u.status === "ignored" ? "【无关段】" : "【未标】");
}

function bindUnitDrag(handle, i, which) {
  handle.addEventListener("mousedown", (e) => {
    e.stopPropagation(); e.preventDefault();
    const move = (ev) => {
      const t = clamp(xToTime(ev.clientX));
      const u = state.units[i];
      if (!u) return;
      if (which === "start") u.start_sec = Math.min(t, u.end_sec - 0.05);
      else u.end_sec = Math.max(t, u.start_sec + 0.05);
      u.dur_sec = +(u.end_sec - u.start_sec).toFixed(3);
      renderUnitList();
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      renderTimeline(); scheduleSave();
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  });
}

function refreshVoiceCur() {
  loudLayer.querySelectorAll(".loudseg").forEach((el, i) => el.classList.toggle("cur", i === state.curVoice));
}

function renderAsrLayer() {
  asrLayer.innerHTML = "";
  if (!state.duration) return;
  for (const c of state.asr) {
    const m = document.createElement("div"); // 参考刻度：立在命令词音频终止点，只读
    m.className = "asrmark " + c.type;
    m.style.left = pct(c.end) + "%";
    m.title = `ASR ${c.word} 参考点 ${fmt(c.end)}（点击跳转，落点提前0.5s并暂停）`;
    m.onclick = (e) => { e.stopPropagation(); jumpAsrTo(c.end); };
    asrLayer.appendChild(m);
  }
}

function actionEl(a, i) {
  const d = document.createElement("div");
  d.className = "action " + a.type + (state.selected === i ? " selected" : "");
  d.style.left = pct(a.start) + "%";
  d.style.width = Math.max(0.3, pct(a.end) - pct(a.start)) + "%";
  d.title = `${cn(a.type)} ${fmt(a.start)} - ${fmt(a.end)}（拖动两端微调，点击选中）`;
  const l = document.createElement("div"); l.className = "handle hl";
  const r = document.createElement("div"); r.className = "handle hr";
  d.appendChild(l); d.appendChild(r);
  d.onclick = (e) => { e.stopPropagation(); seek(a.start); state.selected = i; renderTimeline(); renderActionList(); };
  bindDrag(l, d, i, "start"); bindDrag(r, d, i, "end");
  return d;
}

function bindDrag(handle, block, i, which) {
  handle.addEventListener("mousedown", (e) => {
    e.stopPropagation(); e.preventDefault();
    const move = (ev) => {
      const t = clamp(xToTime(ev.clientX));
      const a = state.actions[i];
      if (!a) return;
      if (which === "start") a.start = Math.min(t, a.end - 0.05);
      else a.end = Math.max(t, a.start + 0.05);
      block.style.left = pct(a.start) + "%";
      block.style.width = Math.max(0.3, pct(a.end) - pct(a.start)) + "%";
      block.title = `${cn(a.type)} ${fmt(a.start)} - ${fmt(a.end)}（拖动两端微调，点击选中）`;
      renderActionList();
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      pushUndo(); scheduleSave();
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  });
}

function renderOpenBlocks() {
  actionLayer.querySelectorAll(".open").forEach(el => el.remove());
  if (!state.duration) return;
  for (const type of ["up", "down"]) {
    if (state.open[type] == null) continue;
    const d = document.createElement("div");
    d.className = `action open ${type}`;
    d.style.left = pct(state.open[type]) + "%";
    d.style.width = Math.max(0.2, pct(clamp(video.currentTime)) - pct(state.open[type])) + "%";
    actionLayer.appendChild(d);
  }
}

function renderPoints() {
  markerLayer.innerHTML = "";
  for (const [cls, t] of [["in", state.inPoint], ["out", state.outPoint]]) {
    if (t == null) continue;
    const d = document.createElement("div");
    d.className = "marker " + cls;
    d.style.left = pct(t) + "%";
    d.title = (cls === "in" ? "入点 " : "出点 ") + fmt(t);
    markerLayer.appendChild(d);
  }
}

// ---------- 动作操作 ----------
function pushUndo() {
  state.undoStack.push(JSON.stringify(state.actions));
  if (state.undoStack.length > 100) state.undoStack.shift();
}
function undo() {
  if (!state.undoStack.length) return flash("没有可撤销的操作");
  state.actions = JSON.parse(state.undoStack.pop());
  state.selected = null;
  renderTimeline(); renderActionList(); scheduleSave();
  flash("已撤销");
}

function addAction(type, s, e) {
  if (s > e) [s, e] = [e, s];
  s = clamp(s); e = clamp(e);
  if (e - s < 0.05) return flash("起止太近，未保存");
  pushUndo();
  state.actions.push({ type, start: s, end: e });
  state.selected = state.actions.length - 1;
  renderTimeline(); renderActionList(); scheduleSave();
}
function deleteAction(i) {
  pushUndo();
  state.actions.splice(i, 1);
  state.selected = null;
  renderTimeline(); renderActionList(); scheduleSave();
}

function quickType(type) {
  const t = clamp(video.currentTime);
  // 1) 有完整入出点 -> 直接成段
  if (state.inPoint != null && state.outPoint != null) {
    addAction(type, state.inPoint, state.outPoint);
    state.inPoint = state.outPoint = null;
    renderPoints();
    return;
  }
  // 2) 只有入点或出点 -> 与当前位置成段
  if (state.inPoint != null || state.outPoint != null) {
    const p = state.inPoint != null ? state.inPoint : state.outPoint;
    addAction(type, p, t);
    state.inPoint = state.outPoint = null;
    renderPoints();
    return;
  }
  // 3) 快速打标: 无未闭合则开新段, 已有则闭合
  if (state.open[type] == null) {
    state.open[type] = t;
    flash(`${cn(type)} 起点 ${fmt(t)}，再按 ${type === "up" ? "1" : "2"} 闭合`);
  } else {
    addAction(type, state.open[type], t);
    state.open[type] = null;
    flash(`${cn(type)} 已闭合`);
  }
  renderOpenBlocks();
}

function cancelPending() {
  state.open = { up: null, down: null };
  state.inPoint = state.outPoint = null;
  renderOpenBlocks(); renderPoints();
  flash("已取消未闭合动作与入/出点");
}

// ---------- ASR 参考点跳转 ----------
function jumpAsrTo(t) { // 落点 = 参考点 − 0.5s 并暂停，听指令尾音再人工判动作起点
  seek(t - 0.5);
  video.pause();
}
function jumpAsr(dir, onlyStop) {
  const pts = state.asr.filter(c => !onlyStop || c.type === "stop")
    .map(c => c.end).sort((a, b) => a - b);
  if (!pts.length) return flash(onlyStop ? "该视频没有“停”参考点" : "该视频没有 ASR 参考点");
  const t = video.currentTime;
  let target = null;
  if (dir > 0) target = pts.find(x => x - 0.5 > t + 0.01);
  else target = [...pts].reverse().find(x => x - 0.5 < t - 0.01);
  if (target == null) target = dir > 0 ? pts[0] : pts[pts.length - 1]; // 循环
  jumpAsrTo(target);
  const w = state.asr.find(c => c.end === target);
  flash("跳到 ASR 参考点 " + (w ? w.word + " " : "") + fmt(target));
}

// ---------- 响段跳转 ----------
function loudBoundaries() {
  const b = new Set();
  for (const s of state.loud) { b.add(+s.start_sec.toFixed(3)); b.add(+s.end_sec.toFixed(3)); }
  return [...b].sort((a, c) => a - c);
}
function jumpLoud(dir, from) {
  const bs = loudBoundaries();
  if (!bs.length) return flash("该视频没有响段标记");
  const t = from != null ? from : video.currentTime;
  let target = null;
  if (dir > 0) target = bs.find(x => x > t + 0.01);
  else target = [...bs].reverse().find(x => x < t - 0.01);
  if (target == null) target = dir > 0 ? bs[0] : bs[bs.length - 1]; // 循环
  seek(target);
  flash("跳到响段边界 " + fmt(target));
}

// ---------- v1.6 单元打标 ----------
function refreshVoiceCur() {
  if (state.mode === "voice") {
    loudLayer.querySelectorAll(".unitseg").forEach((el, i) => el.classList.toggle("cur", i === state.curUnit));
  } else {
    loudLayer.querySelectorAll(".loudseg").forEach((el, i) => el.classList.toggle("cur", i === state.curVoice));
  }
}

function selectUnit(i) {
  if (i == null || !state.units[i]) return;
  state.curUnit = i;
  const u = state.units[i];
  seek(u.start_sec);
  renderTimeline(); renderUnitList();
  flash(unitTitle(u, i) + (u.sentence ? `　整句：${u.sentence}` : ""));
  playUnitSpan(u); // v1.7: 选中/跳到单元 = 自动播那一段，播完即停
}

function nextUnlabeledUnit(fromIdx) { // 环形查找下一个未打标单元
  const n = state.units.length;
  const from = fromIdx == null ? (state.curUnit == null ? 0 : state.curUnit + 1) : fromIdx + 1;
  for (let k = 0; k < n; k++) {
    const i = (from + k) % n;
    if (!state.units[i].status) return i;
  }
  return null;
}

function jumpUnit(dir) { // w 模式下 ←/→ = 上/下一个未打标单元
  if (!state.units.length) return flash("该视频没有标注单元（检查 result/asr_v2 是否已生成）");
  const t = video.currentTime;
  let idx = null;
  if (dir > 0) {
    for (let i = 0; i < state.units.length; i++) {
      if (state.units[i].start_sec > t + 0.05 && !state.units[i].status) { idx = i; break; }
    }
    if (idx == null) idx = nextUnlabeledUnit(); // 全跳完则环形找下一个未标
  } else {
    if (state.curUnit != null && state.curUnit > 0) {
      for (let i = state.curUnit - 1; i >= 0; i--) {
        if (!state.units[i].status) { idx = i; break; }
      }
    }
    if (idx == null) {
      for (let i = state.units.length - 1; i >= 0; i--) {
        if (state.units[i].start_sec < t - 0.05 && !state.units[i].status) { idx = i; break; }
      }
    }
    if (idx == null && state.curUnit != null) idx = state.curUnit; // 无更前的未标：留在当前
  }
  if (idx == null) { renderUnitList(); return flash("全部单元已打标"); }
  selectUnit(idx);
}

function labelUnit(label) { // 数字键 1~8 / 弹窗按钮共用；label=custom 时转弹窗聚焦文本框
  if (state.curUnit == null) {
    const t = video.currentTime;
    let idx = state.units.findIndex(u => t >= u.start_sec - 0.01 && t <= u.end_sec + 0.01);
    if (idx === -1) idx = nextUnlabeledUnit();
    if (idx == null && state.units.length) idx = 0;
    if (idx == null) return flash("该视频没有标注单元");
    state.curUnit = idx;
  }
  const u = state.units[state.curUnit];
  if (!u) return;
  if (label === "custom") { openVoiceModal(true); return; }
  if (label === "non_cmd") { u.label = "non_cmd"; u.status = "ignored"; }
  else { u.label = label; u.status = "labeled"; }
  afterUnitLabel();
}

function afterUnitLabel() {
  closeVoiceModal();
  scheduleSave();
  const nxt = nextUnlabeledUnit();
  if (nxt == null) { renderTimeline(); renderUnitList(); flash("本视频单元已全部打标"); return; }
  selectUnit(nxt);
}

function removeCurrentUnit() { // Delete：删掉当前单元（生成单元记入 removed_unit_ids，手工单元直接移除）
  if (state.curUnit == null) return flash("先选中一个单元再删除");
  const u = state.units[state.curUnit];
  if (u.kind !== "manual" && u.source !== "manual_edit" && u.id) state.removedUnitIds.push(u.id);
  state.units.splice(state.curUnit, 1);
  state.curUnit = null;
  renderTimeline(); renderUnitList(); scheduleSave();
  flash(`已删除单元 ${fmt(u.start_sec)} - ${fmt(u.end_sec)}（可 Ctrl+Z 恢复前请先保存？改动即时生效）`);
}

function splitUnitAtPlayhead() { // S：播放头处把当前单元切成两半，两半回到未标
  if (state.curUnit == null) return flash("先用 ←/→ 选中一个单元");
  const i = state.curUnit, u = state.units[i];
  const t = clamp(video.currentTime);
  if (t <= u.start_sec + 0.05 || t >= u.end_sec - 0.05) return flash("播放头不在单元内（离边界太近），无法切分");
  const mid = +t.toFixed(3);
  const a = { ...u, id: `manual:${u.start_sec.toFixed(2)}-${mid.toFixed(2)}`,
              kind: "manual", start_sec: u.start_sec, end_sec: mid, dur_sec: +(mid - u.start_sec).toFixed(3),
              label: "", status: null, source: "manual_edit", word: "", text: "" };
  const b = { ...u, id: `manual:${mid.toFixed(2)}-${u.end_sec.toFixed(2)}`,
              kind: "manual", start_sec: mid, end_sec: u.end_sec, dur_sec: +(u.end_sec - mid).toFixed(3),
              label: "", status: null, source: "manual_edit", word: "", text: "" };
  const wasGen = u.kind !== "manual" && u.source !== "manual_edit";
  if (wasGen) state.removedUnitIds.push(u.id); // 原生成单元不再出现
  state.units.splice(i, 1, a, b);
  state.curUnit = i + 1;
  renderTimeline(); renderUnitList(); scheduleSave();
  flash(`已在 ${fmt(mid)} 切分，两半均回到未标`);
}

function mergeNextUnit() { // M：当前单元与下一个相邻单元手动合并（不受间隔 <0.5s 自动合并限制）
  if (state.curUnit == null) return flash("先用 ←/→ 选中一个单元");
  const i = state.curUnit, a = state.units[i], b = state.units[i + 1];
  if (!b) return flash("已是最后一个单元，没有可合并的下一段");
  const labels = [...new Set([a.label, b.label].filter(Boolean))]; // 与 v1.8 自动合并同语义：非空去重 + 拼接
  const m = { ...a, id: `manual:${a.start_sec.toFixed(2)}-${b.end_sec.toFixed(2)}`,
              kind: "manual", start_sec: a.start_sec, end_sec: b.end_sec,
              dur_sec: +(b.end_sec - a.start_sec).toFixed(3),
              label: labels.join("+"),
              status: (a.status === "labeled" || b.status === "labeled") ? "labeled"
                    : (a.status === "ignored" || b.status === "ignored") ? "ignored" : null,
              source: "manual_edit",
              word: [a.word, b.word].filter(Boolean).join(" "),
              text: [a.text, b.text].filter(Boolean).join(" "),
              sentence: a.sentence || b.sentence || "" };
  for (const u of [a, b]) if (!state.removedUnitIds.includes(u.id)) state.removedUnitIds.push(u.id); // 原 id 不再由生成/带回路径出现
  state.units.splice(i, 2, m);
  state.curUnit = i;
  renderTimeline(); renderUnitList(); scheduleSave();
  const gap = (b.start_sec - a.end_sec).toFixed(1);
  flash(`已合并 ${fmt(m.start_sec)}~${fmt(m.end_sec)}（间隔 ${gap}s）`);
}

// ---------- 单元列表（右侧面板） ----------
function renderUnitList() {
  const box = $("unitList");
  if (!box) return;
  box.innerHTML = "";
  const done = state.units.filter(u => u.status).length;
  $("unitCount").textContent = state.units.length ? `(${done}/${state.units.length})` : "";
  state.units.forEach((u, i) => {
    const row = document.createElement("div");
    row.className = "urow" + (i === state.curUnit ? " selected" : "") + (u.status ? " done" : "");
    const badge = document.createElement("span");
    badge.className = "ubadge " + (u.kind || "cmd");
    badge.textContent = u.kind === "cmd" ? voiceCn(u.label || u.word) || "令"
                      : u.kind === "gap" ? "隙" : u.kind === "manual" ? "手" : "令";
    if (u.source === "asr_only") badge.title = "响段外的命令词";
    const span = document.createElement("span");
    span.className = "utime";
    span.textContent = `${u.start_sec.toFixed(1)}-${u.end_sec.toFixed(1)}`;
    const inp = document.createElement("input");
    inp.className = "utext";
    inp.value = u.text || "";
    inp.placeholder = u.kind === "cmd" ? (u.word || "") : "空隙文本（可输入）";
    inp.onchange = () => { u.text = inp.value.trim(); scheduleSave(); };
    inp.onclick = (e) => { e.stopPropagation(); state.curUnit = i; refreshVoiceCur(); renderUnitList._skipFocus = true; };
    inp.onkeydown = (e) => { // v1.6 fix：焦点在行内文本框时 Enter 不再被全局拦截吞掉——保存文本并打开打标弹窗
      if (e.key === "Enter") {
        e.preventDefault(); e.stopPropagation();
        inp.blur(); // 触发 onchange 保存
        if (state.mode === "voice") openVoiceModal();
      } else if (e.key === "Escape") {
        e.preventDefault(); e.stopPropagation();
        inp.blur();
      }
    };
    const st = document.createElement("span");
    st.className = "ustat" + (u.status === "labeled" ? " lv-" + u.label : u.status === "ignored" ? " ign" : "");
    st.textContent = u.status === "labeled" ? voiceCn(u.label) : u.status === "ignored" ? "无关" : "·";
    row.appendChild(badge); row.appendChild(span); row.appendChild(inp); row.appendChild(st);
    row.onclick = () => selectUnit(i);
    box.appendChild(row);
  });
}

// ---------- 打标弹窗（v1.6：面向当前单元，文本预填可编辑） ----------
const UNIT_KEY_LABELS = { "1": "up", "2": "up_small", "3": "down", "4": "down_small", "5": "stop", "6": "non_cmd", "7": "custom", "8": "no_vocal" };
let modalPrefill = null;

function currentUnit() { // 无选中时按播放头/环形未标定位一个单元
  if (state.curUnit != null && state.units[state.curUnit]) return state.units[state.curUnit];
  const t = video.currentTime;
  let idx = state.units.findIndex(u => t >= u.start_sec - 0.01 && t <= u.end_sec + 0.01);
  if (idx === -1) idx = nextUnlabeledUnit(0);
  if (idx == null) idx = state.units.length ? 0 : null;
  if (idx == null) return null;
  state.curUnit = idx;
  return state.units[idx];
}

function openVoiceModal(forceText) { // forceText：按 7 进入时聚焦文本框
  if (!state.units.length) return flash("该视频没有标注单元（检查 result/asr_v2 是否已生成）");
  const u = currentUnit();
  if (!u) return;
  $("vmInfo").textContent = unitTitle(u, state.curUnit);
  $("vmSentence").textContent = u.sentence ? `整句：${u.sentence}`
    : (u.word ? `命令词：${u.word}` : "（无整句上下文）");
  const inp = $("vmText");
  inp.value = u.text || ""; // v1.6: ASR 内容自动预填（空隙=非命令片段，命令词=词本身）
  const box = $("vmAsr"); box.innerHTML = "";
  const words = state.asr.filter(c => c.end > u.start_sec + 0.001 && c.start < u.end_sec - 0.001);
  modalPrefill = words.length ? words[0].type : null;
  if (words.length) {
    const lab = document.createElement("span");
    lab.className = "muted";
    lab.textContent = "单元内 ASR（点色块=采用并打标）：";
    box.appendChild(lab);
    for (const w of words) {
      const b = document.createElement("button");
      b.className = "asrchip " + w.type;
      b.textContent = `${w.word}·${voiceCn(w.type)}`;
      b.onclick = () => { inp.value = w.word; applyVoiceLabel(w.type); };
      box.appendChild(b);
    }
  }
  document.querySelectorAll("#vmOptions .vm-opt[data-label]").forEach(b => {
    b.classList.toggle("prefill", b.dataset.label === modalPrefill);
    b.classList.toggle("current", u.status === "labeled" && b.dataset.label === u.label);
  });
  $("voiceModal").classList.remove("hidden");
  if (forceText) inp.focus();
}

function closeVoiceModal() {
  $("voiceModal").classList.add("hidden");
  const a = document.activeElement;
  if (a && a.id === "vmText") a.blur();
}

function applyVoiceLabel(label) { // 弹窗按钮/数字键共用：up/up_small/down/down_small/stop/non_cmd/custom/no_vocal
  const u = currentUnit();
  if (!u) { closeVoiceModal(); return; }
  const el = $("vmText");
  const text = el ? el.value.trim() : "";
  if (label === "non_cmd") { u.label = "non_cmd"; u.status = "ignored"; }
  else if (label === "custom") {
    if (!text) { flash("请先输入语音文本，再按 Enter 提交"); if (el) el.focus(); return; }
    u.label = "custom"; u.status = "labeled";
  } else { u.label = label; u.status = "labeled"; }
  if (text) u.text = text;
  afterUnitLabel(); // 关弹窗 + scheduleSave + 跳下一个未标单元
}

// ---------- v1.6 模式 / 右侧面板页签 ----------
function setPanelTab(tab) {
  state.panelTab = tab;
  $("tabActionsBtn").classList.toggle("active", tab === "actions");
  $("tabUnitsBtn").classList.toggle("active", tab === "units");
  $("actionList").classList.toggle("hidden", tab !== "actions");
  $("unitList").classList.toggle("hidden", tab !== "units");
}

function setMode(m) {
  if (state.mode === m) return;
  state.mode = m;
  $("modeVideoBtn").classList.toggle("active", m === "video");
  $("modeVoiceBtn").classList.toggle("active", m === "voice");
  if (m === "voice") state.curVoice = null; else { state.curUnit = null; unitStopAt = null; }
  setPanelTab(m === "voice" ? "units" : "actions");
  renderTimeline(); renderUnitList();
  flash(m === "voice"
    ? "单元打标：←/→ 跳未标（自动播该段） · 空格播当前段 · 1~5 标签 · 6 无关段 · 7 自定义 · 8 无人声段 · Enter 弹窗 · S 切分 · M 与下一段合并 · Delete 删单元"
    : "视频标注模式（动作 actions 与单元互不影响）");
}

// ---------- 人声轨 ----------
let audioReady = false, pendingVocal = false;

function setVocal(on) {
  if (on) {
    if (!state.folder || !state.video) { flash("尚未选择视频"); return; }
    pendingVocal = true; audioReady = false;
    audio.src = "/api/vocals?folder=" + encodeURIComponent(state.folder)
      + "&video=" + encodeURIComponent(state.video);
    audio.load();
    $("vocalBtn").classList.add("pending");
  } else {
    state.vocalOn = false; audioReady = false; pendingVocal = false;
    audio.pause(); audio.removeAttribute("src");
    video.muted = false;
    $("vocalBtn").classList.remove("active"); $("vocalBtn").classList.remove("pending");
  }
}

$("vocalBtn").onclick = () => setVocal(!state.vocalOn && !pendingVocal ? true : false);

audio.addEventListener("canplay", () => {
  if (!pendingVocal) return;
  pendingVocal = false; audioReady = true;
  state.vocalOn = true;
  $("vocalBtn").classList.remove("pending"); $("vocalBtn").classList.add("active");
  audio.currentTime = video.currentTime; audio.playbackRate = video.playbackRate;
  video.muted = true;
  if (!video.paused) audio.play().catch(() => {});
  flash("人声轨已开启（视频已静音）");
});
audio.addEventListener("error", () => {
  const wasPending = pendingVocal;
  pendingVocal = false; audioReady = false;
  $("vocalBtn").classList.remove("pending");
  if (state.vocalOn || wasPending) {
    if (wasPending) flash("该视频无人声分离文件，使用原音");
    state.vocalOn = false;
    video.muted = false;
    $("vocalBtn").classList.remove("active");
  }
});

function vocalFollow() { // 人声轨跟随视频：seek/播放/暂停/倍速
  if (!state.vocalOn || !audioReady) return;
  audio.playbackRate = video.playbackRate;
  if (Math.abs(audio.currentTime - video.currentTime) > 0.06) audio.currentTime = video.currentTime;
  if (video.paused && !audio.paused) audio.pause();
  if (!video.paused && audio.paused) audio.play().catch(() => {});
}
video.addEventListener("play", () => { if (state.vocalOn && audioReady) { audio.currentTime = video.currentTime; audio.playbackRate = video.playbackRate; audio.play().catch(() => {}); } });
video.addEventListener("pause", () => { unitStopAt = null; if (state.vocalOn && audioReady) audio.pause(); });
video.addEventListener("seeked", vocalFollow);

// ---------- 播放控制 ----------
let unitStopAt = null; // v1.7: voice 模式只播当前单元的段——播放头到 end_sec 自动暂停

function playUnitSpan(u) { // 从单元起点播到终点，到点自动停（音源跟随 🎤：video play/pause 事件已同步 audio）
  unitStopAt = u.end_sec;
  requestAnimationFrame(unitStopTick);
  if (video.paused) { const p = video.play(); if (p && p.catch) p.catch(() => {}); }
}
function unitStopTick() {
  if (unitStopAt == null || video.paused) return;
  if (video.currentTime >= unitStopAt) { unitStopAt = null; video.pause(); return; }
  requestAnimationFrame(unitStopTick);
}

function togglePlay() {
  if (state.mode === "voice" && state.units.length) { // v1.7: W 模式空格=只播当前单元那一段
    if (!video.paused) { video.pause(); return; }
    const u = currentUnit(); // 无选中时按播放头/未标兜底定位（会写 state.curUnit）
    if (!u) { video.play(); return; }
    renderTimeline(); renderUnitList();
    seek(u.start_sec);
    playUnitSpan(u);
    return;
  }
  if (video.paused) video.play(); else video.pause();
}
function step(dt) {
  video.pause();
  video.currentTime = clamp(video.currentTime + dt);
}
function seek(t) { video.currentTime = clamp(t); }
function xToTime(clientX) {
  const r = timeline.getBoundingClientRect();
  return (clientX - r.left) / r.width * (state.duration || 1);
}

function updateTimeLabel() {
  $("timeLabel").textContent = `${fmt(video.currentTime)} / ${fmt(state.duration)}`;
  playhead.style.left = pct(clamp(video.currentTime)) + "%";
  if (state.open.up != null || state.open.down != null) renderOpenBlocks();
  const pend = [];
  if (state.open.up != null) pend.push(`上升(未闭合, 起 ${fmt(state.open.up)})`);
  if (state.open.down != null) pend.push(`下降(未闭合, 起 ${fmt(state.open.down)})`);
  $("pendingLabel").textContent = pend.join("  ");
  if (state.mode === "voice" && state.units.length) { // v1.6: 播放头落在某单元内也视为选中
    const t = video.currentTime;
    const idx = state.units.findIndex(u => t >= u.start_sec - 0.01 && t <= u.end_sec + 0.01);
    if (idx !== -1 && idx !== state.curUnit) { state.curUnit = idx; refreshVoiceCur(); renderUnitList(); }
  }
  vocalFollow();
}

// ---------- 动作列表 ----------
function renderActionList() {
  const box = $("actionList");
  box.innerHTML = "";
  $("actionCount").textContent = state.actions.length ? `(${state.actions.length})` : "";
  const order = state.actions.map((a, i) => i).sort((x, y) => state.actions[x].start - state.actions[y].start);
  for (const i of order) {
    const a = state.actions[i];
    const row = document.createElement("div");
    row.className = "arow" + (state.selected === i ? " selected" : "");
    const badge = document.createElement("span");
    badge.className = "badge " + a.type; badge.textContent = cn(a.type);
    const span = document.createElement("span");
    span.textContent = `${fmt(a.start)} - ${fmt(a.end)}`;
    const del = document.createElement("button");
    del.className = "del"; del.textContent = "删除";
    del.onclick = (e) => { e.stopPropagation(); deleteAction(i); };
    row.appendChild(badge); row.appendChild(span); row.appendChild(del);
    row.onclick = () => { seek(a.start); state.selected = i; renderTimeline(); renderActionList(); };
    box.appendChild(row);
  }
}

// ---------- 保存 ----------
function segIndexAt(t) {
  for (let i = 0; i < state.loud.length; i++) {
    if (t >= state.loud[i].start_sec && t <= state.loud[i].end_sec) return i;
  }
  return null;
}
function buildPayload() {
  return {
    folder: state.folder, video: state.video,
    updated_at: new Date().toISOString(),
    actions: state.actions.map(a => ({
      type: a.type,
      start_sec: +a.start.toFixed(3), end_sec: +a.end.toFixed(3),
      start: fmt(a.start), end: fmt(a.end),
      source_segment_idx: segIndexAt(a.start),
    })),
    // v1.6: 单元打标结果。不再提交 voice_segments（服务端沿用旧值，动作/旧人声结果一律不动）
    speech_units: state.units.map(u => ({
      id: u.id, kind: u.kind || "cmd",
      word: u.word || "", text: u.text || "",
      start_sec: +u.start_sec.toFixed(3), end_sec: +u.end_sec.toFixed(3),
      label: u.label || null, status: u.status || null,
      source: u.source || "", sentence: u.sentence || "",
    })),
    removed_unit_ids: state.removedUnitIds,
  };
}
function scheduleSave() {
  state.dirty = true;
  setStatus("保存中…");
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveNow, 600);
}
async function saveNow() {
  if (!state.dirty || !state.folder || !state.video) return;
  state.dirty = false;
  try {
    await fetch("/api/annotation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildPayload()),
    });
    setStatus("已保存 " + new Date().toLocaleTimeString());
  } catch {
    state.dirty = true;
    setStatus("保存失败!");
  }
}
function flushSave() {
  if (state.dirty) { clearTimeout(state.saveTimer); saveNow(); }
}

// ---------- v1.5 双进度轴切换 ----------
let fastAxis = false; // false=下轴(自绘时间轴, 微调) | true=上轴(视频进度条, 粗调)
const axisStep = (shift) => (shift ? 30 : 5); // 上轴步长：±5s，Shift ±30s

function applyAxis() {
  $("playerWrap").classList.toggle("axis-active", fastAxis);
  timeline.classList.toggle("axis-active", !fastAxis);
  $("axisTag").textContent = fastAxis ? "轴:上 粗调±5s" : "轴:下 微调1帧";
}
function setAxis(top) {
  fastAxis = top;
  if (document.activeElement === video) video.blur(); // 焦点离开视频，屏蔽原生 ±5s 叠加
  applyAxis();
  flash(top ? "上轴（粗调）：←/→ ±5s，Shift ±30s"
            : "下轴（微调）：←/→ 单帧，Shift 0.5s" + (state.mode === "voice" ? " · 单元模式恢复未标单元跳转" : ""));
}
applyAxis();

// ---------- 事件绑定 ----------
$("folderSel").onchange = (e) => selectFolder(e.target.value);
timeline.onclick = (e) => {
  unitStopAt = null; // v1.7: 手动点时间轴 seek，不再受旧段终点约束
  const t = xToTime(e.clientX);
  if (tabHeld) jumpLoud(-1, t); // Tab+左键：跳到点击位置之前的响段边界
  else seek(t);
};

document.querySelectorAll("#speedCtrl button").forEach(b => {
  b.onclick = () => {
    video.playbackRate = +b.dataset.speed;
    audio.playbackRate = +b.dataset.speed; // 人声轨同步倍速
    document.querySelectorAll("#speedCtrl button").forEach(x => x.classList.toggle("active", x === b));
  };
});

video.addEventListener("loadedmetadata", () => {
  state.duration = video.duration || 0;
  renderTimeline();
});
video.addEventListener("canplay", () => $("transcoding").classList.add("hidden"));
video.addEventListener("waiting", () => $("transcoding").classList.remove("hidden"));
video.addEventListener("playing", () => $("transcoding").classList.add("hidden"));
video.addEventListener("timeupdate", updateTimeLabel);
video.addEventListener("error", () => {
  if (!video.src) return;
  $("transcoding").textContent = "视频加载失败，请查看服务器窗口日志";
  $("transcoding").classList.remove("hidden");
});

let tabHeld = false, tabShift = false, tabConsumed = false; // Tab 按住状态（Tab+← / Tab+点击 组合键用）

window.addEventListener("keydown", (e) => {
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "select" || tag === "textarea") return;
  const vm = $("voiceModal");
  if (vm && !vm.classList.contains("hidden")) { // 弹窗打开时只放行打标选键，其余快捷键全部拦截
    if (UNIT_KEY_LABELS[e.key]) { e.preventDefault(); applyVoiceLabel(UNIT_KEY_LABELS[e.key]); return; }
    if (e.key === "Enter") { e.preventDefault(); applyVoiceLabel("custom"); return; } // 提交文本=自定义
    if (e.key === "Escape") { e.preventDefault(); closeVoiceModal(); return; }
    e.preventDefault();
    return;
  }
  switch (e.key) {
    case " ": case "Spacebar": e.preventDefault(); togglePlay(); break;
    case "ArrowUp":   e.preventDefault(); setAxis(true); break;  // v1.5 切到上轴（粗调）
    case "ArrowDown": e.preventDefault(); setAxis(false); break; // v1.5 切到下轴（微调）
    case "ArrowLeft":
      e.preventDefault();
      if (document.activeElement === video) video.blur(); // 屏蔽视频原生 ±5s，步进只由脚本控制
      if (tabHeld) { tabConsumed = true; jumpLoud(-1); break; } // Tab+←：上一个响段边界
      if (state.mode === "voice") { if (fastAxis) step(-axisStep(e.shiftKey)); else jumpUnit(-1); }
      else step(fastAxis ? -axisStep(e.shiftKey) : (e.shiftKey ? -0.5 : -FRAME));
      break;
    case "ArrowRight":
      e.preventDefault();
      if (document.activeElement === video) video.blur();
      if (state.mode === "voice") { if (fastAxis) step(axisStep(e.shiftKey)); else jumpUnit(1); }
      else step(fastAxis ? axisStep(e.shiftKey) : (e.shiftKey ? 0.5 : FRAME));
      break;
    case "v": case "V":
      if (e.ctrlKey || e.metaKey) break; // 不劫持 Ctrl+V
      setMode("video"); break;                     // 视频标注模式
    case "w": case "W":
      if (e.ctrlKey || e.metaKey) break; // 不劫持 Ctrl+W
      setMode("voice"); break;                     // 单元打标模式
    case "Enter":
      if (state.mode === "voice") { e.preventDefault(); openVoiceModal(); } break; // 打标弹窗
    case "s": case "S":
      if (e.ctrlKey || e.metaKey) break; // 保留 Ctrl+S 浏览器行为
      if (state.mode === "voice") { e.preventDefault(); splitUnitAtPlayhead(); } break; // 切分当前单元
    case "m": case "M":
      if (state.mode === "voice") { e.preventDefault(); mergeNextUnit(); } break; // 当前单元与下一相邻单元合并
    case "i": case "I":
      state.inPoint = clamp(video.currentTime);
      renderPoints(); flash("入点 " + fmt(state.inPoint)); break;
    case "o": case "O":
      state.outPoint = clamp(video.currentTime);
      renderPoints(); flash("出点 " + fmt(state.outPoint)); break;
    case "1": case "2": case "3": case "4": case "5": case "6": case "7": case "8":
      if (state.mode === "voice") { // v1.6: 选中单元直接按数字键打标
        e.preventDefault();
        labelUnit(UNIT_KEY_LABELS[e.key]);
      } else if (e.key === "1") quickType("up");
      else if (e.key === "2") quickType("down");
      break;
    case "n": case "N":
      if (e.ctrlKey || e.metaKey) break; // 不劫持 Ctrl+N
      e.preventDefault(); jumpAsr(1, e.shiftKey); break;  // 下一个参考点（Shift 限“停”）
    case "b": case "B":
      if (e.ctrlKey || e.metaKey) break; // 不劫持 Ctrl+B
      jumpAsr(-1, e.shiftKey); break;                     // 上一个参考点
    case "Tab":
      e.preventDefault();
      tabHeld = true; tabShift = e.shiftKey; tabConsumed = false; break; // 松开时才跳段，方便 Tab+← 组合
    case "z": case "Z": if (e.ctrlKey || e.metaKey) { e.preventDefault(); undo(); } break;
    case "Delete": // v1.6: 单元模式删当前单元，视频模式删选中动作
      if (state.mode === "voice" && state.curUnit != null) { e.preventDefault(); removeCurrentUnit(); }
      else if (state.selected != null) deleteAction(state.selected);
      break;
    case "Escape": cancelPending(); break;
    case "?": case "h": case "H": toggleHelp(); break;
  }
});

window.addEventListener("keyup", (e) => {
  if (e.key !== "Tab") return;
  tabHeld = false;
  if (!tabConsumed) jumpLoud(tabShift ? -1 : 1); // 单独 Tab：下一个 / Shift+Tab：上一个
});
window.addEventListener("blur", () => { tabHeld = false; });

window.addEventListener("beforeunload", () => {
  if (!state.dirty) return;
  navigator.sendBeacon("/api/annotation",
    new Blob([JSON.stringify(buildPayload())], { type: "application/json" }));
});

function toggleHelp() { $("helpModal").classList.toggle("hidden"); }
$("helpBtn").onclick = toggleHelp;
$("helpModal").onclick = (e) => { if (e.target.id === "helpModal") toggleHelp(); };

// ---------- v1.4/v1.6 模式按钮 / 页签 / 弹窗按钮绑定 ----------
$("modeVideoBtn").onclick = () => setMode("video");
$("modeVoiceBtn").onclick = () => setMode("voice");
$("tabActionsBtn").onclick = () => setPanelTab("actions");
$("tabUnitsBtn").onclick = () => setPanelTab("units");
document.querySelectorAll("#vmOptions .vm-opt[data-label]").forEach(b => {
  b.onclick = () => {
    if (b.dataset.label === "custom") { $("vmText").focus(); return; } // 7=聚焦输入框
    applyVoiceLabel(b.dataset.label);
  };
});
$("vmCancel").onclick = closeVoiceModal;
$("voiceModal").onclick = (e) => { if (e.target.id === "voiceModal") closeVoiceModal(); };
$("vmText").addEventListener("keydown", (e) => { // 文本框内：Enter 提交自定义，Esc 取消
  if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); applyVoiceLabel("custom"); }
  else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeVoiceModal(); }
});

loadFolders();
