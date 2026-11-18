/* ═══════════════════════════════════════════════════════════════════════════
   ANTRA — command palette matcher  (v2.0.0 PORT-1)

   Ported from Antra-Web's `palette.js`. Only the MATCHER and the REGISTRY live
   here: everything DOM-shaped stays in App.svelte, because the desktop renders
   the overlay with Svelte rather than by hand-building nodes. Keeping this file
   DOM-free is what makes it executable under plain `node` in the test.

   The scoring is deliberately carried over unchanged rather than re-derived —
   two of its rules were paid for by measurement on the web build and would be
   re-broken by a naive rewrite:

     * separators fold to spaces, so `auto save` matches `Auto-save when ready`
       as a plain substring. Without it the hyphen forced the query down the
       subsequence path, where a long DESCRIPTION out-scored the short label the
       user actually typed;
     * subsequence hits are penalised by target length for the same reason.

   No Fuse.js: ~50 lines of subsequence scoring instead of a dependency.
   ═══════════════════════════════════════════════════════════════════════════ */

export interface Command {
  id: string;
  label: string;
  group?: string;
  hint?: string;
  keywords?: string[];
  kbd?: string;
  when?: () => boolean;
  run: () => void | Promise<void>;
}

export interface Scored {
  cmd: Command;
  score: number;
  i: number;
}

/** Lower-case, fold separators to spaces, collapse runs. */
export function norm(v: unknown): string {
  return String(v ?? '')
    .toLowerCase()
    .replace(/[-_/.,:]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Score one field. Returns 0 when the query is not even a subsequence, so a
 * non-match can never be rendered just because some other field scored.
 */
export function scoreField(q: string, text: unknown): number {
  if (!text) return 0;
  const t = norm(text);
  if (!q) return 1;
  if (!t) return 0;

  const idx = t.indexOf(q);
  if (idx === 0) return 1000;                     // prefix
  if (idx > 0) return 800 - Math.min(idx, 200);   // substring, earlier is better

  // Subsequence. Long text is trivially easy to subsequence-match, so it is
  // penalised by length — otherwise a 200-character description outranks the
  // short label that was typed.
  let ti = 0, hits = 0, gaps = 0, streak = 0, best = 0, boundary = 0;
  for (let qi = 0; qi < q.length; qi++) {
    const found = t.indexOf(q[qi], ti);
    if (found === -1) return 0;
    if (found === ti && qi > 0) { streak++; best = Math.max(best, streak); }
    else { gaps += found - ti; streak = 0; }
    if (found === 0 || t[found - 1] === ' ') boundary++;
    ti = found + 1;
    hits++;
  }
  const lenPenalty = Math.min(180, Math.max(0, t.length - q.length) * 1.5);
  return Math.max(1, 300 + hits * 8 + best * 12 + boundary * 10
                     - Math.min(gaps, 120) - lenPenalty);
}

/** Best field score for a command, with per-field weights. */
export function scoreCmd(q: string, cmd: Command): number {
  const kw = (cmd.keywords || []).join(' ');
  return Math.max(
    scoreField(q, cmd.label) * 1.0,
    scoreField(q, kw)        * 0.55,
    scoreField(q, cmd.group) * 0.35,
    scoreField(q, cmd.hint)  * 0.30,
  );
}

/** Drop commands whose `when()` is false or throws. A throwing predicate hides
 *  the command rather than taking the whole palette down with it. */
export function usable(cmds: Command[]): Command[] {
  const out: Command[] = [];
  for (const c of cmds) {
    if (!c || !c.label || typeof c.run !== 'function') continue;
    if (typeof c.when === 'function') {
      let ok = false;
      try { ok = !!c.when(); } catch { ok = false; }
      if (!ok) continue;
    }
    out.push(c);
  }
  return out;
}

/**
 * Rank commands against a query.
 *
 * With no query the order is REGISTRATION order and the cap is 80, not 40:
 * settings register last, and a 40 cap hid every one of them until you typed —
 * which makes the palette look like it does not index settings at all.
 */
export function searchCommands(query: string, cmds: Command[], limit = 80): Scored[] {
  const q = norm(query);
  const all = usable(cmds);
  const scored: Scored[] = [];
  all.forEach((c, i) => {
    const s = q ? scoreCmd(q, c) : (10000 - i);
    if (s > 0) scored.push({ cmd: c, score: s, i });
  });
  scored.sort((a, b) => b.score - a.score || a.i - b.i);
  return scored.slice(0, limit);
}

/** Collect from providers, tolerating one that throws. */
export function collect(providers: Array<() => Command[]>): Command[] {
  const out: Command[] = [];
  for (const p of providers) {
    let got: Command[] | null = null;
    try { got = typeof p === 'function' ? p() : null; } catch { got = null; }
    if (got) out.push(...got);
  }
  return out;
}
