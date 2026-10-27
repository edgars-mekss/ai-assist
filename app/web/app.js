// Окно помощника: разговор потоком, ответы голосом (/api/tts), ввод голосом (/api/stt), напоминания, привычки, память.
'use strict';

const $ = (id) => document.getElementById(id);
const list = $('list'), input = $('input'), form = $('form'), send = $('send');

let messages = [];      // {role: 'user'|'assistant', content}
let settings = {};
let busy = null;        // AbortController текущего ответа

// ---------- оформление текста (простая разметка Markdown, всё экранируется) ----------
function esc(s) {
  return s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<i>$2</i>');
}
function render(md) {
  const out = [];
  const parts = md.split(/```/);
  parts.forEach((part, i) => {
    if (i % 2) { out.push('<pre><code>' + esc(part.replace(/^[^\n]*\n/, '')) + '</code></pre>'); return; }
    let listTag = null;
    let para = [];
    const flush = () => { if (para.length) { out.push('<p>' + para.map(inline).join('<br>') + '</p>'); para = []; } };
    const closeList = () => { if (listTag) { out.push('</' + listTag + '>'); listTag = null; } };
    for (const line of part.split('\n')) {
      const ul = line.match(/^\s*[-*•]\s+(.*)/), ol = line.match(/^\s*\d+[.)]\s+(.*)/), h = line.match(/^#{1,6}\s+(.*)/);
      if (ul || ol) {
        flush();
        const tag = ul ? 'ul' : 'ol';
        if (listTag !== tag) { closeList(); out.push('<' + tag + '>'); listTag = tag; }
        out.push('<li>' + inline((ul || ol)[1]) + '</li>');
      } else if (h) {
        flush(); closeList(); out.push('<h3>' + inline(h[1]) + '</h3>');
      } else if (!line.trim()) {
        flush(); closeList();
      } else {
        closeList(); para.push(line);
      }
    }
    flush(); closeList();
  });
  return out.join('');
}

// ---------- сообщения ----------
function addBubble(role, text) {
  $('empty').hidden = true;
  const row = document.createElement('div');
  row.className = 'msg ' + role;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  if (role === 'user') bubble.textContent = text;
  else bubble.innerHTML = render(text);
  row.appendChild(bubble);
  if (role === 'assistant') {
    const say = document.createElement('button');
    say.className = 'say';
    say.title = 'Прочитать вслух';
    say.textContent = '🔊';
    say.addEventListener('click', () => { voice.stop(); voice.push(bubble.dataset.text || text, true); });
    row.appendChild(say);
  }
  bubble.dataset.text = text;
  list.appendChild(row);
  list.scrollTop = list.scrollHeight;
  return bubble;
}

function redraw() {
  list.querySelectorAll('.msg').forEach((m) => m.remove());
  $('empty').hidden = messages.length > 0;
  for (const m of messages) addBubble(m.role, m.content);
}

function save() {
  fetch('/api/history', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ messages }) });
}

// ---------- голос: по предложениям, пока ответ ещё пишется ----------
const voice = {
  queue: [],       // промисы WAV по порядку
  buffer: '',      // недоговорённый хвост
  playing: false,
  audio: null,
  epoch: 0,        // «Стоп» сбрасывает очередь

  clean(text) {
    return text
      .replace(/```[\s\S]*?```/g, ' ')
      .replace(/[*_`#>|]/g, '')
      .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/^\s*[-•]\s+/gm, '')
      .replace(/\s+/g, ' ')
      .trim();
  },
  // force — озвучить даже при выключенном «Голосе» (кнопка 🔊 у сообщения)
  push(text, force) {
    if (!force && !settings.speak) return;
    this.buffer += text;
    const parts = this.buffer.split(/(?<=[.!?…:;])\s+|\n+/);
    this.buffer = force ? '' : parts.pop();
    for (const p of (force ? parts.concat(this.buffer) : parts)) this.say(p);
    if (force) this.flush(true);
  },
  flush(force) {
    if ((force || settings.speak) && this.buffer.trim()) this.say(this.buffer);
    this.buffer = '';
  },
  say(sentence) {
    const text = this.clean(sentence);
    if (!/[\p{L}\p{N}]/u.test(text)) return;
    const epoch = this.epoch;
    this.queue.push(
      fetch('/api/tts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) })
        .then((r) => (r.ok ? r.blob() : null))
        .catch(() => null)
        .then((blob) => (epoch === this.epoch ? blob : null))
    );
    this.next();
  },
  async next() {
    if (this.playing || !this.queue.length) return;
    this.playing = true;
    const epoch = this.epoch;
    const blob = await this.queue.shift();
    if (blob && epoch === this.epoch) {
      const url = URL.createObjectURL(blob);
      this.audio = new Audio(url);
      await new Promise((done) => {
        this.audio.onended = this.audio.onerror = done;
        this.audio.play().catch(done);
      });
      URL.revokeObjectURL(url);
    }
    this.playing = false;
    if (epoch === this.epoch) this.next();
  },
  stop() {
    this.epoch++;
    this.queue = [];
    this.buffer = '';
    if (this.audio) this.audio.pause();
    this.playing = false;
  },
};

// ---------- разговор ----------
async function ask(text) {
  messages.push({ role: 'user', content: text });
  addBubble('user', text);
  const bubble = addBubble('assistant', '');
  bubble.classList.add('typing');
  let answer = '', usedTools = false;
  busy = new AbortController();
  send.textContent = 'Стоп';
  send.classList.add('stop');
  voice.stop();

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages }),
      signal: busy.signal,
    });
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let tail = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      tail += dec.decode(value, { stream: true });
      const lines = tail.split('\n');
      tail = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const part = JSON.parse(line);
        if (part.t) {
          answer += part.t;
          voice.push(part.t);
          bubble.innerHTML = render(answer);
          bubble.dataset.text = answer;
          list.scrollTop = list.scrollHeight;
        }
        if (part.thinking && !answer) bubble.innerHTML = '<p class="thinking">💭 думает…</p>';
        if (part.tool) { note(part.tool, bubble.parentNode); usedTools = true; }
        if (part.confirm) confirmCard(part.confirm, bubble.parentNode);
        if (part.confirmed) settleCard(part.confirmed);
        if (part.image) shot(part.image, bubble.parentNode);
        if (part.error) bubble.innerHTML = render(answer) + '<p class="err">' + esc(part.error) + '</p>';
      }
    }
    voice.flush();
  } catch (e) {
    if (e.name !== 'AbortError') bubble.innerHTML = render(answer) + '<p class="err">Нет связи с моделью.</p>';
  }
  bubble.classList.remove('typing');
  for (const id of Object.keys(openCards)) settleCard({ id, ok: false }, true);   // «Стоп» — открытые карточки = «нет»
  // ответа нет — не оставлять «думает…» навсегда
  if (!answer.trim() && !bubble.querySelector('.err')) bubble.innerHTML = '<p class="err">Ответа нет (остановлено или модель промолчала).</p>';
  busy = null;
  send.textContent = 'Отправить';
  send.classList.remove('stop');
  if (answer.trim()) {
    messages.push({ role: 'assistant', content: answer });
    save();
  } else {
    messages.pop();  // ответа не было — вопрос не сохраняем, его видно в окне
  }
  if (usedTools) refreshPanels();
  input.focus();
}

form.addEventListener('submit', (e) => {
  e.preventDefault();
  if (busy) { busy.abort(); voice.stop(); return; }
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  autosize();
  ask(text);
});
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});
function autosize() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 200) + 'px'; }
input.addEventListener('input', autosize);

// ---------- настройки ----------
function saveSettings(patch) {
  Object.assign(settings, patch);
  return fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });
}
$('speak').addEventListener('change', (e) => { saveSettings({ speak: e.target.checked }); if (!e.target.checked) voice.stop(); });
$('openSettings').addEventListener('click', () => { $('settings').hidden = false; });
$('closeSettings').addEventListener('click', () => {
  saveSettings({ system: $('system').value.trim() || settings.system });
  $('settings').hidden = true;
});
$('settings').addEventListener('click', (e) => { if (e.target === $('settings')) $('closeSettings').click(); });
$('voice').addEventListener('change', (e) => saveSettings({ voice: e.target.value }));
$('modelSwitch').addEventListener('change', async (e) => {
  await saveSettings({ model: e.target.value });
  note('Отвечает ' + e.target.selectedOptions[0].textContent + ' — первый ответ после смены чуть дольше: модель загружается.');
  modelsOp();
});
$('speed').addEventListener('input', (e) => { $('speedValue').textContent = '×' + Number(e.target.value).toFixed(1); });
$('speed').addEventListener('change', (e) => saveSettings({ speed: Number(e.target.value) }));
$('testVoice').addEventListener('click', () => { voice.stop(); voice.push('Привет! Вот так я звучу. Если нравится — оставляем.', true); });
$('newChat').addEventListener('click', () => {
  if (busy || !messages.length) return;
  if (!confirm('Начать новый разговор? Старый сотрётся.')) return;
  voice.stop(); messages = []; save(); redraw();
});
$('clearChat').addEventListener('click', () => {
  if (!confirm('Стереть весь разговор?')) return;
  voice.stop(); messages = []; save(); redraw(); $('settings').hidden = true;
});

async function start() {
  try {
    const state = await (await fetch('/api/state')).json();
    settings = state.settings;
    messages = state.history || [];
    drawModels(state);
    $('speak').checked = !!settings.speak;
    $('system').value = settings.system;
    $('speed').value = settings.speed;
    $('speedValue').textContent = '×' + Number(settings.speed).toFixed(1);
    $('voice').innerHTML = state.voices.map((v) => `<option value="${esc(v.id)}">${esc(v.name)}</option>`).join('');
    $('voice').value = settings.voice;
    if ($('voice').selectedIndex < 0 && state.voices.length) saveSettings({ voice: $('voice').value = state.voices[0].id });
    if (!state.voices.length) $('speak').disabled = true;
    $('dot').className = 'dot ok';
    redraw();
  } catch {
    $('dot').className = 'dot err';
  }
  input.focus();
  memoryPoll(true);
  eventsPoll(true);
  // адрес …/#habits открывает панель сразу (#reminders, #habits, #memory, #settings)
  const open = { '#reminders': 'openReminders', '#habits': 'openHabits', '#memory': 'openMemory', '#settings': 'openSettings', '#access': 'openAccess' }[location.hash];
  if (open) $(open).click();
}

// ---------- память «Что я о тебе знаю» ----------
// факты выписывает сервер сам (в паузах разговора); здесь — список с правкой и подсказки «Запомнил: …» в чате
let memoryN = null;   // номер последнего показанного изменения

function drawFacts(facts) {
  const ul = $('facts');
  ul.innerHTML = '';
  $('factsEmpty').hidden = facts.length > 0;
  for (const f of facts) {
    const li = document.createElement('li');
    const inp = document.createElement('input');
    inp.value = f.text;
    inp.maxLength = 200;
    inp.addEventListener('change', () => {
      const text = inp.value.trim();
      memoryOp(text ? { op: 'update', id: f.id, text } : { op: 'remove', id: f.id });
    });
    const del = document.createElement('button');
    del.type = 'button';
    del.title = 'Забыть';
    del.textContent = '×';
    del.addEventListener('click', () => memoryOp({ op: 'remove', id: f.id }));
    li.append(inp, del);
    ul.appendChild(li);
  }
}

async function memoryOp(op) {
  const r = await (await fetch('/api/memory', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(op) })).json();
  memoryN = r.n;
  drawFacts(r.facts);
}

// строка-заметка в чате (что сделал помощник: запомнил, завёл напоминание…); before — вставить перед этим элементом
function note(text, before) {
  const div = document.createElement('div');
  div.className = 'note';
  div.textContent = text;
  list.insertBefore(div, before || null);
  list.scrollTop = list.scrollHeight;
}

async function memoryPoll(first) {
  try {
    const r = await (await fetch('/api/memory')).json();
    if (!first && memoryN !== null) for (const x of r.news) if (x.n > memoryN) note('📝 ' + x.text);
    memoryN = r.n;
    if ($('memory').hidden) drawFacts(r.facts);
  } catch { /* сервер перезапускается — в следующий раз */ }
  setTimeout(memoryPoll, 8000);
}

$('openMemory').addEventListener('click', async () => {
  const r = await (await fetch('/api/memory')).json();
  memoryN = r.n;
  drawFacts(r.facts);
  $('memory').hidden = false;
});
$('closeMemory').addEventListener('click', () => { $('memory').hidden = true; });
$('memory').addEventListener('click', (e) => { if (e.target === $('memory')) $('memory').hidden = true; });
$('factForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const text = $('factInput').value.trim();
  if (!text) return;
  $('factInput').value = '';
  memoryOp({ op: 'add', text });
});
$('forgetAll').addEventListener('click', () => {
  if (confirm('Забыть всё, что я о тебе знаю? Разговор останется.')) memoryOp({ op: 'clear' });
});


// ---------- ввод голосом: запись 16 кГц, после паузы сама останавливается, текст распознаёт сервер (GigaAM) ----------
function wav16(samples, rate) {
  const buf = new ArrayBuffer(44 + samples.length * 2), v = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, 'data');
  v.setUint32(40, samples.length * 2, true);
  samples.forEach((x, i) => v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, x)) * 0x7fff, true));
  return new Blob([buf], { type: 'audio/wav' });
}

const blobBase64 = (blob) => new Promise((done) => {
  const r = new FileReader();
  r.onload = () => done(String(r.result).split(',')[1] || '');
  r.readAsDataURL(blob);
});

const mic = {
  on: false, ctx: null, stream: null, node: null, chunks: [],
  heard: false, quiet: 0, floor: 1, started: 0,
  placeholder: input.placeholder,

  async start() {
    if (this.on) return;
    if (busy) busy.abort();
    voice.stop();
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    } catch {
      note('🎤 Нет доступа к микрофону — разреши его (значок слева в строке окна) или в «Параметры → Конфиденциальность → Микрофон».');
      return;
    }
    this.ctx = new AudioContext({ sampleRate: 16000 });
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.node = this.ctx.createScriptProcessor(4096, 1, 1);
    Object.assign(this, { chunks: [], heard: false, quiet: 0, floor: 1, started: performance.now() });
    this.node.onaudioprocess = (e) => {
      if (!this.on) return;
      const d = e.inputBuffer.getChannelData(0);
      this.chunks.push(new Float32Array(d));
      let sum = 0;
      for (const x of d) sum += x * x;
      const rms = Math.sqrt(sum / d.length);
      this.floor = Math.min(this.floor, rms);
      // речь — заметно громче тишины в комнате; после речи пауза 1,3 с — конец фразы
      if (rms > Math.max(0.015, this.floor * 4)) { this.heard = true; this.quiet = 0; } else this.quiet += d.length / this.ctx.sampleRate;
      const long = (performance.now() - this.started) / 1000;
      if ((this.heard && this.quiet > 1.3) || (!this.heard && long > 8) || long > 60) this.stop(true);
    };
    src.connect(this.node);
    this.node.connect(this.ctx.destination);
    this.on = true;
    $('mic').classList.add('rec');
    input.placeholder = 'Слушаю… (замолчишь — отправлю; 🎤 или Ctrl+Пробел — стоп)';
  },

  async stop(send) {
    if (!this.on) return;
    this.on = false;
    $('mic').classList.remove('rec');
    this.node.disconnect();
    this.stream.getTracks().forEach((t) => t.stop());
    const rate = this.ctx.sampleRate;
    this.ctx.close();
    if (!send || !this.heard) { input.placeholder = this.placeholder; return; }
    const total = this.chunks.reduce((n, c) => n + c.length, 0);
    const samples = new Float32Array(total);
    let at = 0;
    for (const c of this.chunks) { samples.set(c, at); at += c.length; }
    input.placeholder = 'Распознаю…';
    $('mic').disabled = true;
    try {
      const r = await postJson('/api/stt', { wav: await blobBase64(wav16(samples, rate)) });
      const text = (r.text || '').trim();
      if (r.error) note('🎤 ' + r.error);
      else if (!text) note('🎤 Не расслышал — попробуй ещё раз.');
      else {
        const typed = input.value.trim();
        input.value = '';
        autosize();
        ask(typed ? typed + ' ' + text : text);
      }
    } catch {
      note('🎤 Нет связи с сервером.');
    }
    $('mic').disabled = false;
    input.placeholder = this.placeholder;
  },

  toggle() { if (this.on) this.stop(true); else this.start(); },
};
$('mic').addEventListener('click', () => mic.toggle());
document.addEventListener('keydown', (e) => {
  if (e.ctrlKey && e.code === 'Space') { e.preventDefault(); mic.toggle(); }
});

// ---------- общие мелочи панелей ----------
const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
const WEEK = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
const pad = (n) => String(n).padStart(2, '0');
const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
function humanWhen(when) {   // «2026-10-27T09:00» → «завтра в 09:00»
  const d = new Date(when);
  const day = isoDay(d), now = new Date(), tomorrow = new Date(now.getTime() + 864e5);
  const name = day === isoDay(now) ? 'сегодня' : day === isoDay(tomorrow) ? 'завтра' : `${d.getDate()} ${MONTHS[d.getMonth()]}`;
  return `${name} в ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
async function postJson(url, body) {
  return (await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
}

// боковая панель: кнопка открыть, «Готово» (close + Имя), щелчок мимо — закрыть
function panel(id, open, onOpen) {
  $(open).addEventListener('click', () => { onOpen(); $(id).hidden = false; });
  $('close' + id[0].toUpperCase() + id.slice(1)).addEventListener('click', () => { $(id).hidden = true; });
  $(id).addEventListener('click', (e) => { if (e.target === $(id)) $(id).hidden = true; });
}

function refreshPanels() {
  if (!$('reminders').hidden) remOp();
  if (!$('habits').hidden) habitsOp();
}

// ---------- напоминания ----------
function drawReminders(items) {
  const ul = $('remList');
  ul.innerHTML = '';
  $('remEmpty').hidden = items.length > 0;
  for (const r of items) {
    const li = document.createElement('li');
    const when = document.createElement('span');
    when.className = 'when';
    when.textContent = humanWhen(r.when);
    const text = document.createElement('span');
    text.className = 'text';
    text.textContent = r.text;
    const del = document.createElement('button');
    del.type = 'button';
    del.title = 'Отменить';
    del.textContent = '×';
    del.addEventListener('click', () => remOp({ op: 'remove', id: r.id }));
    li.append(when, text, del);
    ul.appendChild(li);
  }
}

async function remOp(op) {
  const r = op ? await postJson('/api/reminders', op) : await (await fetch('/api/reminders')).json();
  drawReminders(r.items);
  return r;
}

panel('reminders', 'openReminders', () => {
  const d = new Date(Date.now() + 3600e3);
  $('remWhen').value = `${isoDay(d)}T${pad(d.getHours())}:00`;
  remOp();
});
$('remForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = $('remText').value.trim();
  if (!text || !$('remWhen').value) return;
  const r = await remOp({ op: 'add', text, when: $('remWhen').value });
  if (r.msg) alert(r.msg);
  else $('remText').value = '';
});

// ---------- привычки: 14 последних дней, щелчок по дню — отметить / снять ----------
function drawHabits(h) {
  const grid = $('habitGrid');
  grid.innerHTML = '';
  $('habitsEmpty').hidden = h.habits.length > 0;
  $('checkTime').value = h.check || '';
  if (!h.habits.length) return;
  const today = new Date(h.today + 'T12:00');
  const days = [];
  for (let i = 13; i >= 0; i--) days.push(new Date(today.getTime() - i * 864e5));
  const cell = (cls, text) => { const el = document.createElement('div'); el.className = cls; el.textContent = text; return el; };
  grid.append(cell('hname', ''));
  for (const d of days) {
    const head = cell('dhead' + (d.getDay() % 6 === 0 ? ' weekend' : ''), d.getDate());
    head.title = WEEK[d.getDay()];
    grid.append(head);
  }
  grid.append(cell('dhead', 'серия'), cell('dhead', ''));
  for (const x of h.habits) {
    grid.append(cell('hname', x.name));
    for (const d of days) {
      const day = isoDay(d);
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'day' + (x.days.includes(day) ? ' done' : '') + (day === h.today ? ' today' : '');
      b.title = `${WEEK[d.getDay()]}, ${d.getDate()} ${MONTHS[d.getMonth()]}`;
      b.addEventListener('click', () => habitsOp({ op: 'toggle', id: x.id, day }));
      grid.append(b);
    }
    grid.append(cell('streak', x.streak ? '🔥 ' + x.streak : '—'));
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'del';
    del.title = 'Удалить привычку';
    del.textContent = '×';
    del.addEventListener('click', () => { if (confirm(`Удалить привычку «${x.name}» со всеми отметками?`)) habitsOp({ op: 'remove', id: x.id }); });
    grid.append(del);
  }
}

async function habitsOp(op) {
  drawHabits(op ? await postJson('/api/habits', op) : await (await fetch('/api/habits')).json());
}

panel('habits', 'openHabits', () => habitsOp());
$('habitForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const name = $('habitName').value.trim();
  if (!name) return;
  $('habitName').value = '';
  habitsOp({ op: 'add', name });
});
$('checkTime').addEventListener('change', (e) => habitsOp({ op: 'check', time: e.target.value }));

// ---------- события сервера: сработало напоминание, вечерний вопрос о привычках ----------
let eventsN = null;

function incoming(x) {
  if (busy) { setTimeout(() => incoming(x), 2000); return; }   // дождаться конца ответа, чтобы не смешать
  const text = x.kind === 'reminder' ? '⏰ Напоминание: ' + x.text : '📅 ' + x.text;
  addBubble('assistant', text);
  messages.push({ role: 'assistant', content: text });
  save();
  if (settings.speak) voice.push(x.kind === 'reminder' ? 'Напоминаю: ' + x.text : x.text, true);
  refreshPanels();
}

async function eventsPoll(first) {
  try {
    const r = await (await fetch('/api/events')).json();
    if (eventsN !== null && r.n < eventsN) eventsN = 0;   // сервер перезапустился — счёт заново
    if (!first && eventsN !== null) for (const x of r.items) if (x.n > eventsN) incoming(x);
    eventsN = r.n;
  } catch { /* сервер перезапускается — в следующий раз */ }
  setTimeout(eventsPoll, 5000);
}

// ---------- модели: переключатель в шапке, список и добавление в настройках ----------
let pullTimer = null;

function pullText(p) {
  if (!p) return '';
  if (p.error) return 'ошибка: ' + p.error;
  if (p.done) return '';
  if (p.total && p.total < 5e7) return 'завершается…';   // после основного файла Ollama докачивает мелкие
  if (p.total) return `качается ${Math.floor(((p.completed || 0) / p.total) * 100)}% из ${(p.total / 1e9).toFixed(1)} ГБ`;
  return 'качается…';
}

function drawModels(st) {
  const sw = $('modelSwitch');
  sw.innerHTML = '';
  for (const m of st.models) {
    const o = document.createElement('option');
    o.value = m.id;
    o.textContent = m.installed ? m.place : `${m.place} — ${pullText(m.pull) || 'не скачана'}`;
    o.disabled = !m.installed;
    sw.append(o);
  }
  sw.value = st.current;
  settings.model = st.current;

  const ul = $('modelList');
  ul.innerHTML = '';
  for (const m of st.models) {
    const li = document.createElement('li');
    const text = document.createElement('span');
    text.className = 'text';
    text.textContent = m.place;
    const small = document.createElement('small');
    small.textContent = m.name;
    text.append(document.createElement('br'), small);
    const state = document.createElement('span');
    state.className = 'when';
    state.textContent = m.id === st.current ? '✓ сейчас' : !m.installed ? (pullText(m.pull) || 'не скачана') : !m.tools ? 'без инструментов' : '';
    if (m.installed && !m.tools) state.title = 'Модель не умеет вызывать инструменты: разговор есть, напоминаний и привычек из чата нет';
    li.append(text, state);
    if (m.installed && m.thinking) {
      const lab = document.createElement('label');
      lab.className = 'check';
      lab.title = 'Рассуждать перед ответом: умнее и надёжнее с напоминаниями и привычками, но ответ на несколько секунд позже';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = m.think;
      box.addEventListener('change', () => modelsOp({ op: 'think', id: m.id, on: box.checked }));
      lab.append(box, ' рассуждать');
      li.append(lab);
    }
    if (!m.installed && (!m.pull || m.pull.done)) {
      const get = document.createElement('button');
      get.type = 'button';
      get.className = 'wide';
      get.textContent = 'Скачать';
      get.addEventListener('click', () => modelsOp({ op: 'pull', id: m.id }));
      li.append(get);
    }
    if (!m.builtin) {
      const del = document.createElement('button');
      del.type = 'button';
      del.title = 'Убрать из списка';
      del.textContent = '×';
      del.addEventListener('click', () => {
        if (!confirm(`Убрать модель «${m.label}» из списка?`)) return;
        const files = m.installed && confirm('Удалить и её файлы с диска? (Отмена — оставить файлы)');
        modelsOp({ op: 'remove', id: m.id, files });
      });
      li.append(del);
    }
    ul.appendChild(li);
  }

  clearTimeout(pullTimer);
  if (st.models.some((m) => m.pull && !m.pull.done)) pullTimer = setTimeout(() => modelsOp(), 2000);
}

async function modelsOp(op) {
  const r = op ? await postJson('/api/models', op) : await (await fetch('/api/models')).json();
  if (r.msg) alert(r.msg);
  drawModels(r);
  return r;
}

$('openSettings').addEventListener('click', () => modelsOp());
$('modelForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = $('modelName').value.trim();
  if (!name) return;
  const r = await modelsOp({ op: 'add', name, label: $('modelLabel').value.trim(), gpu: $('modelGpu').checked });
  if (!r.msg) { $('modelName').value = ''; $('modelLabel').value = ''; }
});

// ---------- действия на ПК: карточка «Разрешить / Отклонить», снимок экрана в чате ----------
// подтвердить можно только здесь: номер карточки приходит в потоке ответа, сама модель его не знает
const openCards = {};   // номер → элемент карточки
const TITLE = document.title;

function confirmCard(c, before) {
  const card = document.createElement('div');
  card.className = 'confirm';
  const head = document.createElement('div');
  head.className = 'cTitle';
  head.textContent = '🔐 ' + c.title + '?';
  card.append(head);
  if (c.fields) {
    const dl = document.createElement('dl');
    for (const [k, v] of c.fields) {
      const dt = document.createElement('dt');
      dt.textContent = k;
      const dd = document.createElement('dd');
      dd.textContent = v;
      dl.append(dt, dd);
    }
    card.append(dl);
  }
  if (c.text) {
    const pre = document.createElement('div');
    pre.className = 'cText';
    pre.textContent = c.text;
    card.append(pre);
  }
  const row = document.createElement('div');
  row.className = 'cButtons';
  const ok = document.createElement('button');
  ok.type = 'button';
  ok.className = 'ok';
  ok.textContent = 'Разрешить';
  const no = document.createElement('button');
  no.type = 'button';
  no.textContent = 'Отклонить';
  ok.addEventListener('click', () => settleCard({ id: c.id, ok: true }, true));
  no.addEventListener('click', () => settleCard({ id: c.id, ok: false }, true));
  row.append(ok, no);
  card.append(row);
  list.insertBefore(card, before || null);
  list.scrollTop = list.scrollHeight;
  openCards[c.id] = card;
  document.title = '❗ Помощник ждёт разрешения';
  ok.focus();
}

// send — ответить серверу (щелчок или «Стоп»); без send — сервер сам сообщил итог
function settleCard(r, send) {
  const card = openCards[r.id];
  if (!card) return;
  delete openCards[r.id];
  if (send) postJson('/api/confirm', { id: r.id, ok: r.ok }).catch(() => {});
  card.classList.add(r.ok ? 'allowed' : 'denied');
  card.querySelector('.cButtons').textContent = r.ok ? '✓ Разрешено' : '✕ Отклонено';
  if (!Object.keys(openCards).length) document.title = TITLE;
}

function shot(src, before) {
  const img = document.createElement('img');
  img.className = 'shot';
  img.src = src;
  img.title = 'Что помощник увидел на экране (снимок не сохраняется)';
  img.addEventListener('click', () => img.classList.toggle('big'));
  list.insertBefore(img, before || null);
  list.scrollTop = list.scrollHeight;
}

// ---------- окно 🔐 «Доступ»: выключатель, папки, режимы, почта, журнал ----------
function drawAccess(a) {
  $('accessOn').checked = a.enabled;
  $('accessBody').classList.toggle('off', !a.enabled);

  const fl = $('folderList');
  fl.innerHTML = '';
  for (const f of a.folders) {
    const li = document.createElement('li');
    const t = document.createElement('span');
    t.className = 'text';
    t.textContent = f;
    const del = document.createElement('button');
    del.type = 'button';
    del.title = 'Закрыть доступ к папке';
    del.textContent = '×';
    del.addEventListener('click', () => accessOp({ op: 'folder_remove', path: f }));
    li.append(t, del);
    fl.append(li);
  }
  $('foldersEmpty').hidden = a.folders.length > 0;

  const tl = $('toolList');
  tl.innerHTML = '';
  for (const t of a.tools) {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'text';
    name.textContent = t.title;
    const sel = document.createElement('select');
    for (const [v, label] of t.modes) {
      const o = document.createElement('option');
      o.value = v;
      o.textContent = label;
      sel.append(o);
    }
    sel.value = t.mode;
    sel.className = 'mode ' + t.mode;
    sel.addEventListener('change', () => accessOp({ op: 'mode', name: t.name, mode: sel.value }));
    li.append(name, sel);
    tl.append(li);
  }

  $('mailAddr').value = a.mail.address || '';
  $('mailPass').value = '';
  $('mailPass').placeholder = a.mail.has_password ? 'пароль сохранён (зашифрован)' : 'пароль приложения';
  $('mailServer').value = a.mail.server || '';
  $('mailPort').value = a.mail.port || '';
  $('mailState').textContent = a.mail.has_password ? '✓ подключена' : 'не подключена';

  const lg = $('actionLog');
  lg.innerHTML = '';
  for (const x of a.log) {
    const li = document.createElement('li');
    li.textContent = `${x.at.slice(5, 16)} · ${x.tool} · ${x.result}`;
    li.title = x.what;
    lg.append(li);
  }
  $('logEmpty').hidden = a.log.length > 0;
}

async function accessOp(op) {
  const r = op ? await postJson('/api/access', op) : await (await fetch('/api/access')).json();
  drawAccess(r);
  if (r.msg) alert(r.msg);
  return r;
}

panel('access', 'openAccess', () => accessOp());
$('accessOn').addEventListener('change', (e) => accessOp({ op: 'enabled', on: e.target.checked }));
$('folderForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const path = $('folderPath').value.trim();
  if (!path) return;
  const r = await accessOp({ op: 'folder_add', path });
  if (!r.msg) $('folderPath').value = '';
});
$('mailForm').addEventListener('submit', (e) => {
  e.preventDefault();
  accessOp({ op: 'mail', address: $('mailAddr').value.trim(), password: $('mailPass').value,
             server: $('mailServer').value.trim(), port: $('mailPort').value.trim() });
});
$('mailTest').addEventListener('click', () => accessOp({ op: 'mail_test' }));
$('mailForget').addEventListener('click', () => {
  if (confirm('Забыть адрес и пароль почты?')) accessOp({ op: 'mail_forget' });
});

start();
