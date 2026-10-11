"use strict";
// 装卸动作标注前端 v1.4
// v1.4: 人声打标（v/w 双模式、voice_segments 模型、Enter 弹窗+ASR 预填、s 切分、人声轨开关）
// v1.5: ↑/↓ 切换 ←/→ 作用的进度轴（上轴粗调±5s / 下轴微调单帧），免鼠标点击
const $ = (id) => document.getElementById(id);
const video = $("video"), timeline = $("timeline"), loudLayer = $("loudLayer"),
      asrLayer = $("asrLayer"),
      actionLayer = $("actionLayer"), markerLayer = $("markerLayer"), playhead = $("playhead"),
      audio = $("vocalAudio");

const FRAME = 0.04; // 25fps 单帧

const VOICE_LABELS = { up: "上升", up_small: "上升一点", down: "下降", down_small: "下降一点", stop: "停", non_cmd: "非命令" };
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
  mode: "video",                  // "video" 视频标注 | "voice" 人声打标（v/w 切换，仅改变 ←/→ 语义）
  voice: [],                      // [{start,end,label,status:null|"labeled"|"ignored",srcIdx}] 初始 seed 自响段
  curVoice: null,                 // 当前段索引 = 最近跳转选中的段 ∪ 播放头所在段
  vocalOn: false,                 // 人声轨开关（视频静音 + 独立 audio 播 vocals.wav）
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

async function selectVideo(name) {
  flushSave();
  const keepVocal = state.vocalOn || pendingVocal;
  setVocal(false);
  state.video = name;
  state.actions = []; state.undoStack = []; state.inPoint = state.outPoint = null;
  state.open = { up: null, down: null }; state.selected = null; state.duration = 0;
  state.asr = [];
  state.voice = []; state.curVoice = null; closeVoiceModal();
  document.querySelectorAll(".vitem").forEach(el => el.classList.toggle("active", el.dataset.name === name));
  state.loud = state.loudByVideo[name.replace(/\.[^.]+$/, "")] || [];
  state.voice = seedVoice();
  renderTimeline(); renderActionList();
  $("transcoding").textContent = "加载 / 转码中，请稍候…";
  $("transcoding").classList.remove("hidden");
  video.src = "/media/" + encodeURIComponent(state.folder) + "/" + encodeURIComponent(name);
  if (keepVocal) setVocal(true);
  const [ann, asr] = await Promise.all([
    fetch("/api/annotation?folder=" + encodeURIComponent(state.folder)
          + "&video=" + encodeURIComponent(name)).then(r => r.json()),
    fetch("/api/asr?folder=" + encodeURIComponent(state.folder)
          + "&video=" + encodeURIComponent(name)).then(r => r.json()).catch(() => ({})),
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
  renderTimeline(); renderActionList();
}

// ---------- 时间轴渲染 ----------
function pct(t) { return (t / (state.duration || 1)) * 100; }

function renderTimeline() {
  loudLayer.innerHTML = ""; asrLayer.innerHTML = ""; actionLayer.innerHTML = ""; markerLayer.innerHTML = "";
  state.voice.forEach((s, i) => { // v1.4: 响段层即人声段层，未处理=原淡蓝，贴标后按标签上色
    const d = document.createElement("div");
    let cls = "loudseg";
    if (s.status === "ignored") cls += " ignored";
    else if (s.status === "labeled" && s.label) cls += " lv-" + s.label;
    if (i === state.curVoice) cls += " cur";
    d.className = cls;
    d.style.left = pct(s.start) + "%";
    d.style.width = Math.max(0.15, pct(s.end) - pct(s.start)) + "%";
    d.title = `人声段 ${i + 1}/${state.voice.length} ${fmt(s.start)} - ${fmt(s.end)}`
      + (s.status ? `【${voiceStatusText(s)}】` : "（未处理，点击跳到段起点）");
    d.onclick = (e) => {
      e.stopPropagation();
      if (state.mode === "voice") selectVoice(i); else seek(s.start);
    };
    loudLayer.appendChild(d);
  });
  state.actions.forEach((a, i) => actionLayer.appendChild(actionEl(a, i)));
  renderAsrLayer();
  renderOpenBlocks();
  renderPoints();
  updateTimeLabel();
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

// ---------- v1.4 人声打标 ----------
function voiceStatusText(s) {
  if (s.status === "ignored") return "已忽略";
  if (s.status === "labeled") return voiceCn(s.label);
  return "未处理";
}

function setMode(m) {
  state.mode = m;
  $("modeVideoBtn").classList.toggle("active", m === "video");
  $("modeVoiceBtn").classList.toggle("active", m === "voice");
  $("hint").textContent = m === "video"
    ? "空格播放 · ←/→ 步进（↑/↓ 换轴） · I/O 入出点 · 1 上升 · 2 下降 · Tab 响段跳转 · n/b ASR参考点 · W 人声打标"
    : "人声模式：←/→ 上/下一个音频段（↑ 粗调±5s） · Enter 打标 · S 切分 · 空格播放 · Tab 响段跳转 · V 返回视频标注";
  flash(m === "voice" ? "音频打标模式（←/→ 跳音频段，Enter 弹窗）" : "视频标注模式");
}

function selectVoice(i) {
  if (i == null || !state.voice[i]) return;
  state.curVoice = i;
  const s = state.voice[i];
  seek(s.start); video.pause();
  renderTimeline();
  flash(`人声段 ${i + 1}/${state.voice.length} ${fmt(s.start)} - ${fmt(s.end)} · ${voiceStatusText(s)}`);
}

function jumpVoice(dir) { // w 模式下 ←/→ = 上/下一个音频段，落点=段起点+暂停
  const segs = state.voice;
  if (!segs.length) return flash("该视频没有音频段");
  const t = video.currentTime;
  let idx = null;
  if (dir > 0) {
    idx = segs.findIndex(s => s.start > t + 0.05);
    if (idx === -1) idx = 0; // 循环
  } else {
    const cur = segs.findIndex(s => t >= s.start - 0.01 && t <= s.end + 0.01);
    if (cur !== -1 && t - segs[cur].start > 0.5) idx = cur; // 重听当前段起点
    else {
      for (let i = segs.length - 1; i >= 0; i--) if (segs[i].start < t - 0.05) { idx = i; break; }
      if (idx === null) idx = segs.length - 1; // 循环
    }
  }
  selectVoice(idx);
}

function nextUnprocessed() { // 从当前段之后环形查找未处理段
  const n = state.voice.length;
  const from = state.curVoice == null ? 0 : state.curVoice + 1;
  for (let k = 0; k < n; k++) {
    const i = (from + k) % n;
    if (!state.voice[i].status) return i;
  }
  return null;
}

function splitVoiceAtPlayhead() { // s：播放头处把当前段切成两半，两半回到未处理态
  if (state.curVoice == null) return flash("先用 ←/→ 选中一个音频段");
  const i = state.curVoice, s = state.voice[i];
  const t = clamp(video.currentTime);
  if (t <= s.start + 0.05 || t >= s.end - 0.05) return flash("播放头不在段内（离边界太近），无法切分");
  state.voice.splice(i, 1,
    { start: s.start, end: t, label: null, status: null, srcIdx: s.srcIdx },
    { start: t, end: s.end, label: null, status: null, srcIdx: s.srcIdx });
  state.curVoice = i + 1; // 播放头即右半段起点
  renderTimeline(); scheduleSave();
  flash(`已在 ${fmt(t)} 切分，两半均回到未处理`);
}

// ---------- 打标弹窗 ----------
let modalPrefill = null;

function openVoiceModal() {
  if (!state.voice.length) return flash("该视频没有音频段");
  if (state.curVoice == null) {
    const t = video.currentTime;
    let idx = state.voice.findIndex(s => t >= s.start - 0.01 && t <= s.end + 0.01);
    if (idx === -1) idx = nextUnprocessed();
    if (idx == null) idx = 0;
    selectVoice(idx);
  }
  const s = state.voice[state.curVoice];
  $("vmInfo").textContent = `人声段 ${state.curVoice + 1}/${state.voice.length}　`
    + `${fmt(s.start)} - ${fmt(s.end)}　时长 ${(s.end - s.start).toFixed(2)}s`
    + (s.status ? `　当前：${voiceStatusText(s)}` : "");
  const box = $("vmAsr"); box.innerHTML = "";
  const words = state.asr.filter(c => c.end > s.start + 0.001 && c.start < s.end - 0.001);
  modalPrefill = words.length ? words[0].type : null; // 预填=段内第一个 ASR 刻度词
  const lab = document.createElement("span");
  lab.className = "muted";
  lab.textContent = words.length ? "段内 ASR（已预填首项，点击色块可直接采用）：" : "段内 ASR：无";
  box.appendChild(lab);
  for (const w of words) {
    const b = document.createElement("button");
    b.className = "asrchip " + w.type;
    b.textContent = `${w.word}·${voiceCn(w.type)}`;
    b.onclick = () => applyVoiceLabel(w.type);
    box.appendChild(b);
  }
  document.querySelectorAll("#vmOptions .vm-opt[data-label]").forEach(b => {
    b.classList.toggle("prefill", b.dataset.label === modalPrefill);
    b.classList.toggle("current", b.dataset.label === s.label);
  });
  $("voiceModal").classList.remove("hidden");
}

function closeVoiceModal() { $("voiceModal").classList.add("hidden"); }

function applyVoiceLabel(label) { // label ∈ up/up_small/down/down_small/stop/non_cmd 或 "__ignore"
  const i = state.curVoice;
  if (i == null) { closeVoiceModal(); return; }
  const s = state.voice[i];
  if (label === "__ignore") { s.label = null; s.status = "ignored"; }
  else { s.label = label; s.status = "labeled"; }
  closeVoiceModal();
  scheduleSave();
  const nxt = nextUnprocessed();
  if (nxt == null) { renderTimeline(); flash("本视频音频段已全部处理"); return; } // 原地停住
  selectVoice(nxt); // 自动跳下一个未处理段（内部已 renderTimeline）
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
video.addEventListener("pause", () => { if (state.vocalOn && audioReady) audio.pause(); });
video.addEventListener("seeked", vocalFollow);

// ---------- 播放控制 ----------
function togglePlay() {
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
  if (state.mode === "voice" && state.voice.length) { // 播放头落在某段内也视为选中
    const t = video.currentTime;
    const idx = state.voice.findIndex(s => t >= s.start - 0.01 && t <= s.end + 0.01);
    if (idx !== -1 && idx !== state.curVoice) { state.curVoice = idx; refreshVoiceCur(); }
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
    voice_segments: state.voice.map(s => ({ // v1.4: 人声打标结果（ignored 仅扫描跳过，不进训练集）
      start_sec: +s.start.toFixed(3), end_sec: +s.end.toFixed(3),
      start: fmt(s.start), end: fmt(s.end),
      label: s.label, status: s.status,
      source_segment_idx: s.srcIdx,
    })),
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
            : "下轴（微调）：←/→ 单帧，Shift 0.5s" + (state.mode === "voice" ? " · 人声模式恢复段跳转" : ""));
}
applyAxis();

// ---------- 事件绑定 ----------
$("folderSel").onchange = (e) => selectFolder(e.target.value);
timeline.onclick = (e) => {
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
    const map = { "1": "up", "2": "up_small", "3": "down", "4": "down_small", "5": "stop", "6": "non_cmd", "0": "__ignore" };
    if (map[e.key]) { e.preventDefault(); applyVoiceLabel(map[e.key]); return; }
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
      if (state.mode === "voice") { if (fastAxis) step(-axisStep(e.shiftKey)); else jumpVoice(-1); }
      else step(fastAxis ? -axisStep(e.shiftKey) : (e.shiftKey ? -0.5 : -FRAME));
      break;
    case "ArrowRight":
      e.preventDefault();
      if (document.activeElement === video) video.blur();
      if (state.mode === "voice") { if (fastAxis) step(axisStep(e.shiftKey)); else jumpVoice(1); }
      else step(fastAxis ? axisStep(e.shiftKey) : (e.shiftKey ? 0.5 : FRAME));
      break;
    case "v": case "V":
      if (e.ctrlKey || e.metaKey) break; // 不劫持 Ctrl+V
      setMode("video"); break;                     // 视频标注模式
    case "w": case "W":
      if (e.ctrlKey || e.metaKey) break; // 不劫持 Ctrl+W
      setMode("voice"); break;                     // 音频打标模式
    case "Enter":
      if (state.mode === "voice") { e.preventDefault(); openVoiceModal(); } break; // 打标弹窗
    case "s": case "S":
      if (e.ctrlKey || e.metaKey) break; // 保留 Ctrl+S 浏览器行为
      if (state.mode === "voice") { e.preventDefault(); splitVoiceAtPlayhead(); } break; // 切分当前段
    case "i": case "I":
      state.inPoint = clamp(video.currentTime);
      renderPoints(); flash("入点 " + fmt(state.inPoint)); break;
    case "o": case "O":
      state.outPoint = clamp(video.currentTime);
      renderPoints(); flash("出点 " + fmt(state.outPoint)); break;
    case "1": quickType("up"); break;
    case "2": quickType("down"); break;
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
    case "Delete": if (state.selected != null) deleteAction(state.selected); break;
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

// ---------- v1.4 模式按钮 / 弹窗按钮绑定 ----------
$("modeVideoBtn").onclick = () => setMode("video");
$("modeVoiceBtn").onclick = () => setMode("voice");
document.querySelectorAll("#vmOptions .vm-opt[data-label]").forEach(b => {
  b.onclick = () => applyVoiceLabel(b.dataset.label);
});
$("vmCancel").onclick = closeVoiceModal;
$("voiceModal").onclick = (e) => { if (e.target.id === "voiceModal") closeVoiceModal(); };

loadFolders();
