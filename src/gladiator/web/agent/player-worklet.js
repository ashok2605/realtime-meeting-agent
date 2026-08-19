// Player worklet: queued 24kHz Int16 chunks rendered sample-accurately on the
// audio thread (jitter-free). 'flush' drops the queue instantly (barge-in) and
// advances the epoch; stale-epoch chunks are ignored. 'started' fires when a
// new epoch's first sample actually renders (true playback timestamp).

class PlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];          // {samples: Float32Array, offset, epoch}
    this.epoch = 0;
    this.playingEpoch = -1;
    this.queuedSamples = 0;
    this.port.onmessage = (e) => {
      const m = e.data;
      if (m.type === 'chunk') {
        if (m.epoch < this.epoch) return;      // stale: arrived after a flush
        if (m.epoch > this.epoch) this.epoch = m.epoch;
        const int16 = new Int16Array(m.pcm);
        const f32 = new Float32Array(int16.length);
        for (let i = 0; i < int16.length; i++) f32[i] = int16[i] / 0x8000;
        this.queue.push({ samples: f32, offset: 0, epoch: m.epoch });
        this.queuedSamples += f32.length;
      } else if (m.type === 'flush') {
        this.queue = [];
        this.queuedSamples = 0;
        this.epoch = Math.max(this.epoch, m.epoch);
        this.playingEpoch = -1;
      }
    };
  }
  process(inputs, outputs) {
    const out = outputs[0][0];
    let i = 0;
    while (i < out.length && this.queue.length) {
      const head = this.queue[0];
      if (head.epoch !== this.playingEpoch) {
        this.playingEpoch = head.epoch;
        this.port.postMessage({ type: 'started', epoch: head.epoch });
      }
      const n = Math.min(out.length - i, head.samples.length - head.offset);
      out.set(head.samples.subarray(head.offset, head.offset + n), i);
      head.offset += n;
      i += n;
      this.queuedSamples -= n;
      if (head.offset >= head.samples.length) this.queue.shift();
    }
    for (; i < out.length; i++) out[i] = 0;  // underrun or idle: silence
    if ((currentFrame % 12800) < 128) {       // ~2x/sec telemetry
      this.port.postMessage({ type: 'buffer', ms: (this.queuedSamples / 24000) * 1000 });
    }
    return true;
  }
}
registerProcessor('player', PlayerProcessor);
