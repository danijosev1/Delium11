export const pct = (x: number, digits = 0) => `${(x * 100).toFixed(digits)}%`;

export function usd(x: number, digits = 2) {
  if (!Number.isFinite(x)) return "—";
  const s = Math.abs(x).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  return `${x < 0 ? "−" : ""}$${s}`;
}

export const fmtScore = (x: number | null | undefined) => (x == null ? "—" : Math.round(x).toString());

export function fmtDateTime(iso: string) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function fmtDate(iso: string) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

export function ageLabel(days: number) {
  if (days < 60) return `${days} d`;
  if (days < 730) return `${Math.round(days / 30.4)} mo`;
  return `${(days / 365).toFixed(1)} yr`;
}

/** Next occurrence of any "HH:MM" time (the launchd schedule). */
export function nextRunAt(times: string[], now = new Date()): Date {
  const candidates: Date[] = [];
  for (const offset of [0, 1]) {
    for (const t of times) {
      const [h, m] = t.split(":").map(Number);
      const d = new Date(now);
      d.setDate(d.getDate() + offset);
      d.setHours(h, m, 0, 0);
      if (d > now) candidates.push(d);
    }
  }
  candidates.sort((a, b) => a.getTime() - b.getTime());
  return candidates[0];
}

export function relativeFromNow(d: Date, now = new Date()) {
  const mins = Math.max(0, Math.round((d.getTime() - now.getTime()) / 60000));
  if (mins < 60) return `in ${mins} min`;
  const h = Math.floor(mins / 60);
  return `in ${h}h ${mins % 60}m`;
}
