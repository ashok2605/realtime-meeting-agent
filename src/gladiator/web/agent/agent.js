// Agent page: runs inside the Recall bot's headless Chrome.
// - "Microphone" = the meeting's mixed audio (Recall injects it).
// - Anything we play on this page is heard in the meeting.
// - All intelligence is server-side; this page is a speaker + microphone + face.
(async () => {
  const $ = (id) => document.getElementById(id);
  const token = new URLSearchParams(location.search).get('token');
  const debug = { buffer: 0, epoch: 0, seq: 0, ws: 'connecting', cap: 'off' };
  const renderDebug = () => {
    $('debug').textContent =
      `ws:${debug.ws} cap:${debug.cap} epoch:${debug.epoch} seq:${debug.seq} ` +
      `buf:${debug.buffer.toFixed(0)}ms`;
  };
  const setFace = (state) => {
    $('orb').className = 'orb ' + state;
    $('state').textContent = state;
  };

  // --- playback path (24 kHz) -------------------------------------------
  const playCtx = new AudioContext({ sampleRate: 24000 });
  await playCtx.audioWorklet.addModule('/static/agent/player-worklet.js');
  const player = new AudioWorkletNode(playCtx, 'player', {
    numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
  });
  player.connect(playCtx.destination);
  const keepAlive = setInterval(() => {
    if (playCtx.state !== 'running') playCtx.resume();
  }, 1000);
  void keepAlive;

  // --- websocket to the gladiator server ---------------------------------
  let ws;
  const connect = () => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';  // ws for local mic testing
    ws = new WebSocket(`${proto}://${location.host}/ws/agent?token=${token}`);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      debug.ws = 'open'; renderDebug();
      ws.send(JSON.stringify({ type: 'hello', user_agent: navigator.userAgent }));
    };
    ws.onclose = () => {
      debug.ws = 'closed'; renderDebug();
      setTimeout(connect, 1000);  // the server may restart; the bot page persists
    };
    ws.onmessage = (e) => {
      if (typeof e.data === 'string') {
        const msg = JSON.parse(e.data);
        if (msg.type === 'flush') {
          player.port.postMessage({ type: 'flush', epoch: msg.epoch });
        } else if (msg.type === 'face') {
          setFace(msg.state);
        } else if (msg.type === 'pong') {
          const t3 = Date.now();
          const rtt = t3 - msg.t0;
          const offset = (msg.t0 + t3) / 2 - msg.t1;  // page wall - server wall
          ws.send(JSON.stringify({ type: 'clock', offset_ms: offset, rtt_ms: rtt }));
        }
        return;
      }
      // binary: [u32 epoch][u32 seq][int16 pcm @24k]
      const dv = new DataView(e.data);
      const epoch = dv.getUint32(0, true);
      const seq = dv.getUint32(4, true);
      debug.epoch = epoch; debug.seq = seq; renderDebug();
      const pcmBuf = e.data.slice(8);
      player.port.postMessage({ type: 'chunk', epoch, pcm: pcmBuf }, [pcmBuf]);
      if (playCtx.state !== 'running') playCtx.resume();
    };
  };
  connect();

  player.port.onmessage = (e) => {
    if (e.data.type === 'started' && ws && ws.readyState === 1) {
      // First sample of this turn actually rendering -> latency waterfall mark.
      ws.send(JSON.stringify({ type: 'play_ack', epoch: e.data.epoch,
                               client_play_ts_ms: Date.now() }));
    } else if (e.data.type === 'buffer') {
      debug.buffer = e.data.ms; renderDebug();
    }
  };

  setInterval(() => {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'ping', t0: Date.now() }));
  }, 4000);

  // --- capture path (16 kHz) ---------------------------------------------
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        // The "mic" is already a clean conference mix; Chrome's AEC/NS/AGC
        // would mangle it (and AEC would fight our own output).
        echoCancellation: false, noiseSuppression: false, autoGainControl: false,
        channelCount: 1,
      },
    });
    const capCtx = new AudioContext({ sampleRate: 16000 });
    await capCtx.audioWorklet.addModule('/static/agent/capture-worklet.js');
    const src = capCtx.createMediaStreamSource(stream);
    const cap = new AudioWorkletNode(capCtx, 'capture', {
      numberOfInputs: 1, numberOfOutputs: 0,
    });
    src.connect(cap);
    cap.port.onmessage = (e) => {
      if (!ws || ws.readyState !== 1) return;
      const pcm = e.data;  // ArrayBuffer of Int16 (20ms / 320 samples)
      const out = new ArrayBuffer(8 + pcm.byteLength);
      new DataView(out).setFloat64(0, Date.now(), true);
      new Uint8Array(out, 8).set(new Uint8Array(pcm));
      ws.send(out);
    };
    debug.cap = 'on'; renderDebug();
    const sendCaptureState = () => {
      if (ws && ws.readyState === 1) {
        ws.send(JSON.stringify({ type: 'capture_state', ok: true, detail: 'getUserMedia 16k' }));
      } else { setTimeout(sendCaptureState, 300); }
    };
    sendCaptureState();
    setFace('listening');
  } catch (err) {
    debug.cap = 'FAILED'; renderDebug();
    setFace('idle');
    const sendFail = () => {
      if (ws && ws.readyState === 1) {
        ws.send(JSON.stringify({ type: 'capture_state', ok: false, detail: String(err) }));
      } else { setTimeout(sendFail, 300); }
    };
    sendFail();
  }
})();
