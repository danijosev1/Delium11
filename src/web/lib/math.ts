export const clamp = (x: number, lo = 0, hi = 100) => Math.min(hi, Math.max(lo, x));

/** Linear map to 0–100, clipped (docs/analysis-engine.md conventions). */
export const norm = (x: number, lo: number, hi: number) => clamp(((x - lo) / (hi - lo)) * 100);

/** Same as norm, in log10 space. */
export const logNorm = (x: number, lo: number, hi: number) =>
  x <= 0 ? 0 : clamp(((Math.log10(x) - Math.log10(lo)) / (Math.log10(hi) - Math.log10(lo))) * 100);

export function median(xs: number[]): number {
  if (!xs.length) return NaN;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
