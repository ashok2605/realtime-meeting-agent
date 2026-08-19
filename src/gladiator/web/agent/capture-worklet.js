// Capture worklet: meeting audio (this page's "microphone", supplied by the
// Recall bot) -> 20ms Int16 mono frames posted to the main thread.
// Context sample rate is 16000, so 320 samples per frame.
const FRAME_SAMPLES = 320;

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Float32Array(FRAME_SAMPLES);
    this.filled = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    let i = 0;
    while (i < ch.length) {
      const take = Math.min(FRAME_SAMPLES - this.filled, ch.length - i);
      this.buf.set(ch.subarray(i, i + take), this.filled);
      this.filled += take;
      i += take;
      if (this.filled === FRAME_SAMPLES) {
        const out = new Int16Array(FRAME_SAMPLES);
        for (let j = 0; j < FRAME_SAMPLES; j++) {
          const s = Math.max(-1, Math.min(1, this.buf[j]));
          out[j] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        this.port.postMessage(out.buffer, [out.buffer]);
        this.filled = 0;
      }
    }
    return true;
  }
}
registerProcessor('capture', CaptureProcessor);
