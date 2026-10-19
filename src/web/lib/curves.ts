import { resolveKey } from "./categories";

type Anchor = { bsr: number; units: number };

/** Mirrors analysis/curves_data/bsr_velocity/us.toml. These values are ILLUSTRATIVE. */
const CURVES: Record<string, Anchor[]> = {
  default: [
    { bsr: 100, units: 4000 },
    { bsr: 1000, units: 800 },
    { bsr: 5000, units: 250 },
    { bsr: 20000, units: 70 },
    { bsr: 100000, units: 10 },
  ],
  "Home & Kitchen": [
    { bsr: 100, units: 4500 },
    { bsr: 1000, units: 900 },
    { bsr: 5000, units: 300 },
    { bsr: 20000, units: 90 },
    { bsr: 100000, units: 12 },
  ],
  "Sports & Outdoors": [
    { bsr: 100, units: 3000 },
    { bsr: 1000, units: 600 },
    { bsr: 5000, units: 190 },
    { bsr: 20000, units: 55 },
    { bsr: 100000, units: 8 },
  ],
  "Toys & Games": [
    { bsr: 100, units: 5000 },
    { bsr: 1000, units: 1000 },
    { bsr: 5000, units: 330 },
    { bsr: 20000, units: 100 },
    { bsr: 100000, units: 14 },
  ],
};

export function curveFor(category: string) {
  const key = resolveKey(
    category,
    Object.keys(CURVES).filter((k) => k !== "default")
  );
  return { key: key ?? "default", known: key != null, anchors: CURVES[key ?? "default"] };
}

/** Log-log interpolation, extrapolating past the end anchors (no artificial ceiling). */
export function curveUnitsForBsr(bsr: number, category: string): number {
  const a = curveFor(category).anchors;
  let i = a.findIndex((_, idx) => idx < a.length - 1 && bsr <= a[idx + 1].bsr);
  if (i === -1) i = a.length - 2;
  const p0 = a[i];
  const p1 = a[i + 1];
  const t = (Math.log10(bsr) - Math.log10(p0.bsr)) / (Math.log10(p1.bsr) - Math.log10(p0.bsr));
  const ly = Math.log10(p0.units) + t * (Math.log10(p1.units) - Math.log10(p0.units));
  return Math.max(1, Math.round(10 ** ly));
}

/** Amazon's "bought in past month" badge ladder. Keepa returns the bucket floor. */
export const MONTHLY_SOLD_LADDER = [
  50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000,
  9000, 10000, 20000, 30000, 40000, 50000, 100000,
];

export function monthlySoldBucket(value: number): { low: number; high: number | null } {
  if (value < MONTHLY_SOLD_LADDER[0]) return { low: value, high: MONTHLY_SOLD_LADDER[0] };
  let idx = 0;
  for (let i = 0; i < MONTHLY_SOLD_LADDER.length; i++) if (MONTHLY_SOLD_LADDER[i] <= value) idx = i;
  return { low: MONTHLY_SOLD_LADDER[idx], high: MONTHLY_SOLD_LADDER[idx + 1] ?? null };
}
