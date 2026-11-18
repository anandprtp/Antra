/**
 * Antra notification sounds (v1.1.8 FEAT-13).
 *
 * Fully synthesised with WebAudio — no audio assets. That keeps the bundle size
 * unchanged, works with no network, and lets the sound be shaped precisely
 * rather than shipping someone else's sample.
 *
 * Design note: the obvious implementation is three sine oscillators playing a
 * major triad. It reads as a "beep" because a
 * bare sine has no partials and no space. These voices instead use 2-operator
 * FM — a modulator driving the carrier's frequency, with the modulation index
 * decaying over the note — which is the classic way to get a struck-metal /
 * glass timbre. Add a short generated plate reverb, a sub-bass "body" transient
 * and a little stereo spread, and it stops sounding like a test tone.
 */

type Ctx = AudioContext;

export type SfxName = "crystal" | "pluck" | "blip";

let ctx: Ctx | null = null;
let master: GainNode | null = null;
let verb: ConvolverNode | null = null;
let verbSend: GainNode | null = null;
let lastPlayed = 0;

/** Minimum gap between per-track sounds. A 200-track album completing quickly
 *  must not machine-gun the speaker; extra triggers inside the window are
 *  dropped rather than queued, so the sound always tracks *recent* activity. */
const THROTTLE_MS = 900;

function ensure(): Ctx | null {
  try {
    if (!ctx) {
      const AC = (window as any).AudioContext || (window as any).webkitAudioContext;
      if (!AC) return null;
      ctx = new AC() as Ctx;

      // A compressor on the bus keeps overlapping voices from clipping without
      // having to hand-tune every gain.
      const comp = ctx.createDynamicsCompressor();
      comp.threshold.value = -12;
      comp.ratio.value = 12;
      comp.attack.value = 0.002;
      comp.release.value = 0.15;

      master = ctx.createGain();
      master.gain.value = 0.7;

      verb = ctx.createConvolver();
      verb.buffer = makeImpulse(ctx, 1.1, 3.2);
      verbSend = ctx.createGain();
      verbSend.gain.value = 0.28;

      verbSend.connect(verb);
      // The wet path must go through `master`, not straight to the compressor.
      // Routing it to the compressor bypassed the volume control, so the reverb
      // tail stayed audible at volume 0 (caught by the offline render test:
      // "volume 0 is silent" failed with peak=0.0199).
      verb.connect(master);
      master.connect(comp);
      comp.connect(ctx.destination);
    }
    // Webviews start the context suspended until a gesture; resume is cheap and
    // idempotent, and downloads are always user-initiated so a gesture has
    // happened by the time anything plays.
    if (ctx.state === "suspended") void ctx.resume();
    return ctx;
  } catch {
    return null;
  }
}

/** Exponentially-decaying noise burst = a serviceable small plate reverb. */
function makeImpulse(c: Ctx, seconds: number, decay: number): AudioBuffer {
  const rate = c.sampleRate;
  const len = Math.max(1, Math.floor(rate * seconds));
  const buf = c.createBuffer(2, len, rate);
  for (let ch = 0; ch < 2; ch++) {
    const d = buf.getChannelData(ch);
    for (let i = 0; i < len; i++) {
      d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, decay);
    }
  }
  return buf;
}

function out(c: Ctx, pan: number): AudioNode {
  if (typeof c.createStereoPanner !== "function") return master!;
  const p = c.createStereoPanner();
  p.pan.value = pan;
  p.connect(master!);
  if (verbSend) p.connect(verbSend);
  return p;
}

/**
 * One 2-operator FM voice.
 * `ratio` sets the modulator:carrier frequency relationship — non-integer
 * ratios give inharmonic (bell/metal) partials, which is what makes this read
 * as "struck glass" instead of "tone". `index` is the starting modulation
 * depth; decaying it over the note is what produces the bright strike settling
 * into a pure tail.
 */
function fmVoice(
  c: Ctx,
  freq: number,
  when: number,
  dur: number,
  gain: number,
  ratio: number,
  index: number,
  pan = 0,
) {
  const carrier = c.createOscillator();
  const mod = c.createOscillator();
  const modGain = c.createGain();
  const amp = c.createGain();

  carrier.type = "sine";
  mod.type = "sine";
  carrier.frequency.value = freq;
  mod.frequency.value = freq * ratio;

  modGain.gain.setValueAtTime(freq * index, when);
  modGain.gain.exponentialRampToValueAtTime(freq * index * 0.02, when + dur * 0.7);

  amp.gain.setValueAtTime(0.0001, when);
  amp.gain.exponentialRampToValueAtTime(gain, when + 0.006);   // fast strike
  amp.gain.exponentialRampToValueAtTime(0.0001, when + dur);   // natural ring-out

  mod.connect(modGain);
  modGain.connect(carrier.frequency);
  carrier.connect(amp);
  amp.connect(out(c, pan));

  mod.start(when);
  carrier.start(when);
  mod.stop(when + dur + 0.02);
  carrier.stop(when + dur + 0.02);
}

/** Very short low sine — felt more than heard. Gives the hit some body so it
 *  doesn't sound thin on laptop speakers. */
function body(c: Ctx, when: number, freq = 120, gain = 0.16, dur = 0.13) {
  const o = c.createOscillator();
  const g = c.createGain();
  o.type = "sine";
  o.frequency.setValueAtTime(freq * 1.6, when);
  o.frequency.exponentialRampToValueAtTime(freq * 0.7, when + dur);
  g.gain.setValueAtTime(0.0001, when);
  g.gain.exponentialRampToValueAtTime(gain, when + 0.004);
  g.gain.exponentialRampToValueAtTime(0.0001, when + dur);
  o.connect(g);
  g.connect(master!);
  o.start(when);
  o.stop(when + dur + 0.02);
}

/** Filtered noise transient — the "air" on the strike. */
function air(c: Ctx, when: number, gain = 0.05, dur = 0.09) {
  const len = Math.max(1, Math.floor(c.sampleRate * dur));
  const buf = c.createBuffer(1, len, c.sampleRate);
  const d = buf.getChannelData(0);
  for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / len);
  const src = c.createBufferSource();
  src.buffer = buf;
  const hp = c.createBiquadFilter();
  hp.type = "highpass";
  hp.frequency.value = 4200;
  const g = c.createGain();
  g.gain.value = gain;
  src.connect(hp);
  hp.connect(g);
  g.connect(out(c, 0));
  src.start(when);
}

// ── The sounds ───────────────────────────────────────────────────────────────

/** Default. Sub thump + two detuned FM bells a perfect fifth apart + a high
 *  shimmer, into the plate. Reads as a confident, expensive "saved". */
function crystal(c: Ctx, t: number) {
  body(c, t, 130, 0.15, 0.14);
  air(c, t, 0.045, 0.08);
  // A5 -> E6 : a rising fifth resolves without sounding like a jingle.
  fmVoice(c, 880.0, t, 0.55, 0.20, 2.01, 2.6, -0.18);
  fmVoice(c, 882.6, t, 0.55, 0.12, 2.01, 2.6, 0.18);   // detuned twin = width
  fmVoice(c, 1318.5, t + 0.075, 0.75, 0.19, 1.41, 2.2, 0.12);
  fmVoice(c, 2637.0, t + 0.15, 0.55, 0.055, 3.01, 1.4, -0.1); // shimmer
}

/** Soft plucked string — subtle, good for long album runs. */
function pluck(c: Ctx, t: number) {
  body(c, t, 110, 0.10, 0.10);
  const o = c.createOscillator();
  const g = c.createGain();
  const lp = c.createBiquadFilter();
  o.type = "triangle";
  o.frequency.setValueAtTime(660, t);
  lp.type = "lowpass";
  lp.frequency.setValueAtTime(5200, t);
  lp.frequency.exponentialRampToValueAtTime(700, t + 0.35);
  lp.Q.value = 2;
  g.gain.setValueAtTime(0.0001, t);
  g.gain.exponentialRampToValueAtTime(0.26, t + 0.005);
  g.gain.exponentialRampToValueAtTime(0.0001, t + 0.4);
  o.connect(lp);
  lp.connect(g);
  g.connect(out(c, 0));
  o.start(t);
  o.stop(t + 0.42);
  fmVoice(c, 1320, t + 0.02, 0.3, 0.05, 2.0, 1.2, 0.15);
}

/** Minimal two-tick blip for people who find chimes intrusive. */
function blip(c: Ctx, t: number) {
  fmVoice(c, 1046.5, t, 0.09, 0.13, 3.0, 0.9, -0.08);
  fmVoice(c, 1568.0, t + 0.055, 0.12, 0.11, 3.0, 0.9, 0.08);
}

/** Richer resolve for "the whole batch finished". Deliberately distinct from
 *  the per-track sound so the two are never confused. */
function finishFlourish(c: Ctx, t: number) {
  body(c, t, 100, 0.18, 0.2);
  air(c, t, 0.05, 0.1);
  const notes = [659.25, 987.77, 1318.5];  // E5 - B5 - E6
  notes.forEach((f, i) => {
    fmVoice(c, f, t + i * 0.085, 0.9, 0.17 - i * 0.02, 2.01, 2.4, i === 1 ? 0.2 : -0.15);
  });
  fmVoice(c, 2637.0, t + 0.3, 0.9, 0.06, 3.01, 1.6, 0);
}

const TABLE: Record<SfxName, (c: Ctx, t: number) => void> = {
  crystal,
  pluck,
  blip,
};

// ── Public API ───────────────────────────────────────────────────────────────

export function setVolume(vol0to100: number) {
  const c = ensure();
  if (!c || !master) return;
  const v = Math.max(0, Math.min(100, vol0to100)) / 100;
  // Perceptual rather than linear: a 50% slider should sound like half.
  // The 1.6 headroom multiplier puts the default (70%) around -12 dBFS peak —
  // clearly audible over music without being startling. Measured, not guessed.
  master.gain.value = Math.pow(v, 1.6) * 1.6;
}

/** Play immediately, ignoring the throttle. Used by the Settings preview. */
export function preview(name: SfxName, vol0to100: number) {
  const c = ensure();
  if (!c) return;
  setVolume(vol0to100);
  (TABLE[name] || crystal)(c, c.currentTime + 0.02);
}

/** Per-track "saved" sound. Throttled — returns false if suppressed. */
export function playSaved(name: SfxName, vol0to100: number): boolean {
  if (!name || (name as string) === "off") return false;
  const now = Date.now();
  if (now - lastPlayed < THROTTLE_MS) return false;
  lastPlayed = now;
  const c = ensure();
  if (!c) return false;
  setVolume(vol0to100);
  (TABLE[name] || crystal)(c, c.currentTime + 0.02);
  return true;
}

/** Batch-complete flourish. Never throttled — it fires once per job. */
export function playFinished(vol0to100: number) {
  const c = ensure();
  if (!c) return;
  setVolume(vol0to100);
  lastPlayed = Date.now();
  finishFlourish(c, c.currentTime + 0.02);
}
