// Dashboard: a pure event-stream reducer. Live mode and `gladiator replay`
// speak the same protocol (hello snapshot -> backlog -> events), so this file
// has exactly one rendering path.
(() => {
  const $ = (id) => document.getElementById(id);
  const token = new URLSearchParams(location.search).get('token');
  const state = {
    events: [],           // every event, for the log + download
    latTurns: [],         // latency.turn events (waterfalls)
    tokens: 0,
    muted: false,
    replay: false,
  };

  // ---------- header chips ------------------------------------------------
  const setChip = (id, value, cls) => {
    const el = $(id); el.textContent = value;
    const chip = el.closest('.chip');
    if (chip && cls !== undefined) chip.className = 'chip ' + cls;
  };
  const setFace = (s) => { $('face').textContent = s; $('orbmini').className = 'orbmini ' + s; };

  // ---------- questions ----------------------------------------------------
  const questions = new Map();  // id -> {el refs...}
  function initQuestions(list) {
    const root = $('questions'); root.innerHTML = '';
    questions.clear();
    for (const q of list) {
      const div = document.createElement('div');
      div.className = 'q';
      div.innerHTML = `
        <div class="qhead">
          <span class="qstatus ${q.status}">${q.status}</span>
          <span class="qlabel">${q.label}</span>
          <button class="qskip">skip</button>
        </div>
        <div class="qtext" style="font-size:12px;color:var(--dim)">${q.text}</div>
        <div class="qanswer"></div>`;
      div.querySelector('.qskip').onclick = () =>
        send({ type: 'control', action: 'skip_question', args: { question_id: q.id } });
      root.appendChild(div);
      questions.set(q.id, { el: div, data: q });
      if (q.answer_summary) renderAnswer(q.id, q);
    }
    renderProgress();
  }
  function renderAnswer(qid, data) {
    const q = questions.get(qid); if (!q) return;
    q.el.querySelector('.qstatus').className = 'qstatus ' + data.status;
    q.el.querySelector('.qstatus').textContent = data.status;
    const parts = [];
    if (data.answer_summary) parts.push(`<b>${esc(data.answer_summary)}</b>`);
    if (data.verbatim_quote) parts.push(`<span class="qquote">“${esc(data.verbatim_quote)}”</span>`);
    if (data.confidence) parts.push(`<span style="font:10px var(--mono)">conf: ${data.confidence}` +
      (data.revisions > 1 ? ` · rev ${data.revisions}` : '') + `</span>`);
    q.el.querySelector('.qanswer').innerHTML = parts.join('<br>');
    renderProgress();
  }
  function setQStatus(qid, status) {
    const q = questions.get(qid); if (!q) return;
    const el = q.el.querySelector('.qstatus');
    // Don't regress an answered question to "asked" on a re-ask.
    if ((status === 'asked') && ['answered', 'partial', 'skipped'].includes(el.textContent)) return;
    el.className = 'qstatus ' + status; el.textContent = status;
    renderProgress();
  }
  function renderProgress() {
    let done = 0;
    for (const { el } of questions.values()) {
      const s = el.querySelector('.qstatus').textContent;
      if (s === 'answered' || s === 'skipped') done++;
    }
    $('qprogress').textContent = `${done}/${questions.size}`;
  }

  // ---------- transcript ---------------------------------------------------
  let openLine = null;  // {who, el}
  function transcriptAppend(who, text) {
    const box = $('transcript');
    if (!openLine || openLine.who !== who) {
      const el = document.createElement('div');
      el.className = 'line ' + who;
      el.innerHTML = `<span class="who">${who === 'agent' ? 'GLADIATOR' : 'PARTICIPANTS'}</span><span class="tx"></span>`;
      box.appendChild(el);
      openLine = { who, el };
    }
    openLine.el.querySelector('.tx').textContent += text;
    box.scrollTop = box.scrollHeight;
  }
  const transcriptBreak = () => { openLine = null; };

  // ---------- latency ------------------------------------------------------
  function renderWaterfall(turn) {
    state.latTurns.push(turn);
    if (state.latTurns.length > 8) state.latTurns.shift();
    const scale = Math.max(2000, ...state.latTurns.map((t) => t.e2e || 0));
    $('wfscale').textContent = scale.toFixed(0);
    const root = $('waterfalls'); root.innerHTML = '';
    for (const t of state.latTurns.slice().reverse()) {
      const div = document.createElement('div');
      div.className = 'wf';
      const seg = (v, cls) => v ? `<div class="wfseg ${cls}" style="width:${(v / scale) * 100}%"></div>` : '';
      div.innerHTML =
        `turn ${t.epoch} — e2e <b>${t.e2e ?? '?'}ms</b>` +
        ` (detect ${t.detect_think ?? '?'} / serve ${t.serve ?? '?'} / transit ${t.transit_play ?? '?'}` +
        `${t.gate_hold_ms ? ` / gate ${t.gate_hold_ms}` : ''})` +
        `<div class="wfbar">${seg(t.detect_think, 'detect')}${seg(t.serve, 'serve')}${seg(t.transit_play, 'transit')}</div>`;
      root.appendChild(div);
    }
    renderLatStats();
  }
  function renderLatStats() {
    const segs = ['detect_think', 'serve', 'transit_play', 'e2e'];
    const rows = segs.map((s) => {
      const vals = state.latTurns.map((t) => t[s]).filter((v) => v != null).sort((a, b) => a - b);
      if (!vals.length) return `<tr><td>${s}</td><td>–</td><td>–</td><td>–</td></tr>`;
      const p = (q) => vals[Math.min(vals.length - 1, Math.floor(vals.length * q))].toFixed(0);
      return `<tr><td>${s}</td><td>${vals[vals.length - 1].toFixed(0)}</td><td>${p(0.5)}</td><td>${p(0.95)}</td></tr>`;
    });
    $('latstats').innerHTML =
      '<tr><th>segment</th><th>last</th><th>p50</th><th>p95</th></tr>' + rows.join('');
  }

  // ---------- event log ----------------------------------------------------
  const AUDIO_NOISE = new Set(['bridge.stats']);
  function logEvent(ev) {
    state.events.push(ev);
    $('evcount').textContent = state.events.length;
    const filter = $('evfilter').value.trim();
    if (filter && !ev.type.includes(filter)) return;
    const box = $('eventlog');
    const div = document.createElement('div');
    const cls = ev.type.includes('error') || ev.type.includes('fatal') ? 'err'
      : (ev.type.includes('drop') || ev.type.includes('hard_stop') || ev.type.includes('stale')) ? 'warn' : '';
    div.className = 'ev ' + cls;
    const t = new Date(ev.ts * 1000).toISOString().slice(11, 23);
    div.innerHTML = `<span class="t">${t}</span><span class="ty">${ev.type}</span>` +
      `<span class="d">${esc(JSON.stringify(ev.data))}</span>`;
    box.appendChild(div);
    while (box.children.length > 600) box.removeChild(box.firstChild);
    if ($('evfollow').checked) box.scrollTop = box.scrollHeight;
  }
  $('evfilter').oninput = () => {
    const box = $('eventlog'); box.innerHTML = '';
    const filter = $('evfilter').value.trim();
    for (const ev of state.events.slice(-600)) {
      if (!filter || ev.type.includes(filter)) {
        const keep = state.events; state.events = [];  // avoid re-push in logEvent
        logEvent(ev); state.events = keep;
      }
    }
    $('evcount').textContent = state.events.length;
  };

  // ---------- reducer ------------------------------------------------------
  function reduce(ev) {
    const d = ev.data || {};
    switch (ev.type) {
      case 'face.state': setFace(d.state); break;
      case 'recall.bot_status':
        setChip('bot-status', d.status, d.status === 'in_call_recording' ? 'ok'
          : d.status === 'fatal' ? 'bad' : 'warn'); break;
      case 'gemini.connected': setChip('gemini-status', d.resumed ? 'resumed' : 'connected', 'ok'); break;
      case 'gemini.disconnected': case 'gemini.error': setChip('gemini-status', 'reconnecting', 'warn'); break;
      case 'gemini.go_away': setChip('gemini-status', 'go_away', 'warn'); break;
      case 'page.connected': setChip('page-status', 'connected', 'ok'); break;
      case 'page.disconnected': setChip('page-status', 'lost', 'bad'); break;
      case 'page.capture_state': setChip('page-status', d.ok ? 'audio ok' : 'capture FAILED', d.ok ? 'ok' : 'bad'); break;
      case 'recall.speech_on': setChip('speaking', d.participant, 'ok'); break;
      case 'recall.speech_off': setChip('speaking', '–', ''); break;
      case 'transcript.user': transcriptAppend('user', d.text); break;
      case 'transcript.agent': transcriptAppend('agent', d.text); break;
      case 'turn.complete': case 'turn.interrupted': case 'turn.force_drop': transcriptBreak(); break;
      case 'discovery.answer':
        renderAnswer(d.question_id, { status: d.status, answer_summary: d.answer_summary,
          verbatim_quote: d.verbatim_quote, confidence: d.confidence, revisions: d.revision });
        break;
      case 'discovery.asked': setQStatus(d.question_id, 'asked'); break;
      case 'discovery.skipped': setQStatus(d.question_id, 'skipped'); break;
      case 'discovery.phase': $('phase').textContent = d.phase; break;
      case 'latency.turn': renderWaterfall(d); break;
      case 'gemini.usage': if (d.total_tokens) { state.tokens = d.total_tokens; $('tokens').textContent = d.total_tokens; } break;
    }
    if (!AUDIO_NOISE.has(ev.type)) logEvent(ev);
  }

  // ---------- transport ----------------------------------------------------
  let ws;
  function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws/dashboard?token=${token}`);
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'hello') {
        const s = msg.snapshot || {};
        state.replay = !!s.replay;
        if (s.replay) document.title = 'Gladiator — REPLAY';
        initQuestions((s.discovery && s.discovery.questions) || []);
        if (s.discovery) $('phase').textContent = s.discovery.phase;
        setFace(s.face || 'idle');
        setChip('bot-status', s.bot_status || '–');
        setChip('page-status', s.page_connected ? 'connected' : '–', s.page_connected ? 'ok' : '');
      } else if (msg.type === 'backlog') {
        for (const ev of msg.events) reduce(ev);
      } else if (msg.type === 'event') {
        reduce(msg.event);
      } else if (msg.type === 'control_result') {
        logEvent({ ts: Date.now() / 1000, type: 'control.result', data: msg });
        if (msg.action === 'mute') { state.muted = true; $('btn-mute').textContent = 'Unmute agent'; }
        if (msg.action === 'unmute') { state.muted = false; $('btn-mute').textContent = 'Mute agent'; }
      }
    };
    ws.onclose = () => setTimeout(connect, 1500);
  }
  connect();

  // ---------- controls ------------------------------------------------------
  $('btn-leave').onclick = () => confirm('Send the bot out of the meeting?') &&
    send({ type: 'control', action: 'leave' });
  $('btn-mute').onclick = () =>
    send({ type: 'control', action: state.muted ? 'unmute' : 'mute' });
  $('btn-greet').onclick = () => send({ type: 'control', action: 'greet' });
  $('btn-nudge').onclick = () => send({ type: 'control', action: 'nudge' });
  $('btn-download').onclick = () => {
    const blob = new Blob(state.events.map((e) => JSON.stringify(e) + '\n'),
                          { type: 'application/jsonl' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'gladiator-events.jsonl';
    a.click();
  };

  function esc(s) {
    return String(s ?? '').replace(/[&<>"]/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
})();
