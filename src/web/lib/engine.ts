import { CONFIG } from "./config";
import { clamp, logNorm, median, norm } from "./math";
import { resolveKey } from "./categories";
import { curveUnitsForBsr } from "./curves";
import type { Competitor, Confidence, Keyword, Pillars, Product, Verdict } from "../types";

/* ------------------------------------------------------------------ *
 * Fees — mirrors analysis/fee_tables/us-2026.toml.
 * Values are ILLUSTRATIVE until calibrated (docs/calibration.md).
 * ------------------------------------------------------------------ */

export type SizeTier = "small_standard" | "large_standard" | "large_bulky";

const FULFILLMENT_BANDS: Record<SizeTier, { max: number; fee: number }[]> = {
  small_standard: [
    { max: 113, fee: 306 },
    { max: 227, fee: 315 },
    { max: 340, fee: 334 },
    { max: 454, fee: 343 },
  ],
  large_standard: [
    { max: 113, fee: 368 },
    { max: 227, fee: 379 },
    { max: 340, fee: 398 },
    { max: 454, fee: 445 },
    { max: 680, fee: 497 },
    { max: 907, fee: 539 },
    { max: 1134, fee: 578 },
    { max: 1361, fee: 609 },
  ],
  large_bulky: [{ max: 22680, fee: 901 }],
};

const REFERRAL: Record<string, number> = {
  "Home & Kitchen": 0.15,
  Kitchen: 0.15,
  Electronics: 0.08,
  "Sports & Outdoors": 0.15,
  "Toys & Games": 0.15,
  "Health & Household": 0.15,
  "Beauty & Personal Care": 0.15,
  "Pet Supplies": 0.15,
  "Office Products": 0.15,
  "Grocery & Gourmet Food": 0.08,
  Baby: 0.15,
  "Tools & Home Improvement": 0.15,
};

const STORAGE_AVG_CENTS_PER_CF = (87 * 9 + 240 * 3) / 12; // standard 9mo + Q4 peak 3mo
const MM3_PER_CF = 28316846.6;

export function sizeTier(weightG: number, dims: [number, number, number]): SizeTier {
  const [longest, mid, shortest] = [...dims].sort((a, b) => b - a);
  if (weightG <= 454 && longest <= 381 && mid <= 305 && shortest <= 19) return "small_standard";
  if (weightG <= 9072 && longest <= 457 && mid <= 356 && shortest <= 203) return "large_standard";
  return "large_bulky";
}

export function fulfillmentFee(tier: SizeTier, weightG: number): number {
  const bands = FULFILLMENT_BANDS[tier];
  for (const b of bands) if (weightG <= b.max) return b.fee / 100;
  const last = bands[bands.length - 1];
  const base = tier === "large_standard" ? 640 : 901;
  const perKg = tier === "large_standard" ? 16 : 8;
  return (base + perKg * Math.ceil((weightG - last.max) / 1000)) / 100;
}

export function referralFor(category: string): { pct: number; key: string | null } {
  const key = resolveKey(category, Object.keys(REFERRAL));
  return { pct: key ? REFERRAL[key] : 0.15, key };
}

export function cubicFeet(dims: [number, number, number]): number {
  const allowance = 1 + CONFIG.assumptions.packaging_dims_allowance;
  const [a, b, c] = dims.map((d) => d * allowance);
  return (a * b * c) / MM3_PER_CF;
}

/* ------------------------------------------------------------------ *
 * Profit waterfall — mirrors analysis/profit.py
 * ------------------------------------------------------------------ */

export interface ProfitOptions {
  price?: number;
  cogsPct?: number;
  cogsUsd?: number;
  freight?: number;
  tacosPct?: number;
  returnRate?: number;
  unitsLow?: number;
  quotedCogs?: boolean;
}

export interface ProfitLine {
  label: string;
  amount: number;
  kind: "revenue" | "cost" | "net";
}

export interface ProfitResult {
  price: number;
  cogs: number;
  freight: number;
  duty: number;
  landed: number;
  fulfillment: number;
  referral: number;
  storage: number;
  returns: number;
  ppc: number;
  feesTotal: number;
  marginUnit: number;
  marginPct: number;
  roiPct: number;
  tier: SizeTier;
  referralPct: number;
  referralKey: string | null;
  volumeCf: number;
  unitsLow: number;
  launchCapital: number;
  paybackMonths: number;
  lines: ProfitLine[];
  gates: { margin: boolean; roi: boolean; payback: boolean };
  allGatesPass: boolean;
  quotedCogs: boolean;
}

export function computeProfit(
  product: Pick<Product, "price" | "category" | "weightG" | "dimsMm" | "bsr" | "monthlySold">,
  opts: ProfitOptions = {}
): ProfitResult {
  const a = CONFIG.assumptions;
  const price = opts.price ?? product.price;
  const cogsPct = opts.cogsPct ?? a.default_cogs_pct;
  const cogs = opts.cogsUsd != null ? opts.cogsUsd : cogsPct * price;
  const freight = opts.freight ?? a.freight_per_unit;
  const duty = a.duty_pct * cogs;
  const landed = cogs + freight + duty;

  const tier = sizeTier(product.weightG, product.dimsMm);
  const fulfillment = fulfillmentFee(tier, product.weightG);
  const { pct: referralPct, key: referralKey } = referralFor(product.category);
  const referral = referralPct * price;
  const volumeCf = cubicFeet(product.dimsMm);
  const storage = (volumeCf * STORAGE_AVG_CENTS_PER_CF) / 100;

  const returnRate = opts.returnRate ?? a.return_rate_default;
  const returns = returnRate * (price * 0.5 + fulfillment);
  const tacosPct = opts.tacosPct ?? a.tacos_pct;
  const ppc = tacosPct * price;

  const feesTotal = fulfillment + referral + storage + returns + ppc;
  const marginUnit = price - landed - feesTotal;
  const marginPct = price > 0 ? marginUnit / price : 0;
  const roiPct = landed > 0 ? marginUnit / landed : 0;

  const unitsLow = opts.unitsLow ?? Math.max(1, Math.round(product.monthlySold * 0.7));
  const launchCapital = landed * unitsLow * a.inventory_months + a.ppc_ramp_usd + a.fixed_launch_usd;
  const paybackMonths = marginUnit > 0 ? launchCapital / (marginUnit * unitsLow) : Infinity;

  const lines: ProfitLine[] = [
    { label: "Sell price", amount: price, kind: "revenue" },
    { label: "Landed cost (COGS + freight + duty)", amount: -(cogs + freight + duty), kind: "cost" },
    { label: `FBA fulfillment (${tier.replace("_", " ")})`, amount: -fulfillment, kind: "cost" },
    { label: `Referral fee (${(referralPct * 100).toFixed(1)}%)`, amount: -referral, kind: "cost" },
    { label: "Monthly storage (amortised)", amount: -storage, kind: "cost" },
    { label: `Returns (${(returnRate * 100).toFixed(0)}%)`, amount: -returns, kind: "cost" },
    { label: `PPC drag / TACOS (${(tacosPct * 100).toFixed(0)}%)`, amount: -ppc, kind: "cost" },
    { label: "Net margin / unit", amount: marginUnit, kind: "net" },
  ];

  return {
    price,
    cogs,
    freight,
    duty,
    landed,
    fulfillment,
    referral,
    storage,
    returns,
    ppc,
    feesTotal,
    marginUnit,
    marginPct,
    roiPct,
    tier,
    referralPct,
    referralKey,
    volumeCf,
    unitsLow,
    launchCapital,
    paybackMonths,
    lines,
    gates: {
      margin: marginPct >= CONFIG.gates.min_margin,
      roi: roiPct >= CONFIG.gates.min_roi,
      payback: paybackMonths <= CONFIG.gates.max_payback_months,
    },
    allGatesPass:
      marginPct >= CONFIG.gates.min_margin &&
      roiPct >= CONFIG.gates.min_roi &&
      paybackMonths <= CONFIG.gates.max_payback_months,
    quotedCogs: opts.quotedCogs ?? false,
  };
}

/** The stressed case used for P1/P2: price −10%, COGS +15%, TACOS +5pts. */
export function stressedProfit(
  product: Pick<Product, "price" | "category" | "weightG" | "dimsMm" | "bsr" | "monthlySold">,
  opts: ProfitOptions = {}
): ProfitResult {
  const s = CONFIG.stress;
  const basePrice = opts.price ?? product.price;
  const baseCogsPct = opts.cogsPct ?? CONFIG.assumptions.default_cogs_pct;
  return computeProfit(product, {
    ...opts,
    price: basePrice * (1 + s.price_delta),
    cogsPct: opts.cogsUsd != null ? undefined : baseCogsPct * (1 + s.cogs_delta),
    cogsUsd: opts.cogsUsd != null ? opts.cogsUsd * (1 + s.cogs_delta) : undefined,
    tacosPct: (opts.tacosPct ?? CONFIG.assumptions.tacos_pct) + s.tacos_delta_pts,
  });
}

/* ------------------------------------------------------------------ *
 * Small numeric helpers
 * ------------------------------------------------------------------ */

const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : NaN);
const stddev = (xs: number[]) => {
  if (xs.length < 2) return 0;
  const m = mean(xs);
  return Math.sqrt(mean(xs.map((x) => (x - m) ** 2)));
};
const weighted = (parts: { value: number | null; weight: number }[]): number | null => {
  const avail = parts.filter((p) => p.value != null) as { value: number; weight: number }[];
  if (!avail.length) return null;
  const wsum = avail.reduce((a, p) => a + p.weight, 0);
  return clamp(avail.reduce((a, p) => a + p.value * p.weight, 0) / wsum);
};

/** Demo approximation: a competitor's BSR from its rank slot. */
export function impliedCompetitorBsr(baseBsr: number, i: number): number {
  return Math.max(50, Math.round(baseBsr * (0.45 + i * 0.32)));
}

/* ------------------------------------------------------------------ *
 * Pillar 1 — Demand
 * ------------------------------------------------------------------ */

export function clusterVolume(keywords: Keyword[]): number {
  const sorted = [...keywords].sort((a, b) => b.volume - a.volume);
  return sorted.reduce((total, k, _i) => {
    const contained = sorted.some(
      (o) => o.volume > k.volume && o.phrase.toLowerCase().includes(k.phrase.toLowerCase())
    );
    return total + k.volume * (contained ? 0.3 : 1);
  }, 0);
}

function sweetSpotUnits(u: number): number {
  if (u <= 150) return 0;
  if (u < 300) return norm(u, 150, 300) * 0.6;
  if (u <= 1200) return 60 + norm(u, 300, 1200) * 0.4;
  if (u <= 2500) return 100 - norm(u, 1200, 2500) * 0.3;
  return 70;
}

function logSlope(hist: number[]): number | null {
  if (hist.length < 4) return null;
  const y = hist.map((v) => Math.log10(Math.max(1, v)));
  const n = y.length;
  const xm = (n - 1) / 2;
  const ym = mean(y);
  let num = 0;
  let den = 0;
  for (let i = 0; i < n; i++) {
    num += (i - xm) * (y[i] - ym);
    den += (i - xm) ** 2;
  }
  return num / den;
}

function trendScore(hist: number[]): number | null {
  const s = logSlope(hist);
  if (s == null) return null;
  const annual = s * (hist.length - 1) * 4;
  return norm(-annual, -0.2, 0.4);
}

function growthScore(hist: number[]): number | null {
  if (hist.length < 4) return null;
  const first = hist[0];
  const last = hist[hist.length - 1];
  if (!first) return null;
  return norm((last - first) / first, -0.1, 0.4);
}

function seasonalityScore(hist: number[]): number | null {
  if (hist.length < 6) return null;
  const cv = stddev(hist) / mean(hist);
  const peakPct = clamp(cv * 150, 5, 80);
  return 100 - norm(peakPct, 20, 60);
}

export function demandScore(p: Product): number | null {
  const vol = clusterVolume(p.keywords);
  const d1 = vol > 0 ? logNorm(vol, 2000, 40000) : null;

  const units = p.competitors.map((c, i) =>
    curveUnitsForBsr(impliedCompetitorBsr(p.bsr, i), p.category)
  );
  const d2 = units.length ? sweetSpotUnits(median(units)) : null;

  const d3 = trendScore(p.bsrHistory);
  const d4 = growthScore(p.soldHistory.length > 1 ? p.soldHistory : p.bsrHistory.map((b) => 1e7 / b));
  const d5 = seasonalityScore(p.bsrHistory);

  return weighted([
    { value: d1, weight: 30 },
    { value: d2, weight: 30 },
    { value: d3, weight: 20 },
    { value: d4, weight: 10 },
    { value: d5, weight: 10 },
  ]);
}

/* ------------------------------------------------------------------ *
 * Pillar 2 — Competition
 * ------------------------------------------------------------------ */

export const ESTABLISHED_BRANDS = [
  "amazonbasics",
  "amazon basics",
  "anker",
  "logitech",
  "sony",
  "samsung",
  "philips",
  "bosch",
  "3m",
  "oxo",
  "simplehuman",
];

function listingQuality(c: Competitor): number {
  const images = clamp(c.imagesCount / 7, 0, 1);
  const rating = clamp((c.rating - 3.5) / (4.8 - 3.5), 0, 1);
  const titleLen = clamp((c.title.length - 40) / (180 - 40), 0, 1);
  return ((images + rating + titleLen) / 3) * 10;
}

export function competitionScore(p: Product): number | null {
  if (!p.competitors.length) return null;

  const reviews = p.competitors.map((c) => c.reviews);
  const c1 = 100 - logNorm(median(reviews), 200, 3000);
  const c2 = Math.min(p.competitors.filter((c) => c.reviews < 150).length, 4) * 25;

  const velocity = median(
    p.competitors.slice(0, 3).map((c) => c.reviews / Math.max(1, c.ageDays / 30))
  );
  const c3 = 100 - logNorm(Math.max(1, velocity), 20, 300);

  const brands = p.competitors.map((c) => c.brand.toLowerCase());
  const counts = new Map<string, number>();
  for (const b of brands) counts.set(b, (counts.get(b) ?? 0) + 1);
  const share = Math.max(...counts.values()) / p.competitors.length;
  const bigBrand = brands.some((b) => ESTABLISHED_BRANDS.includes(b));
  const c4 = clamp(100 - share * 100 - (bigBrand ? 25 : 0));

  const quality = mean(p.competitors.map(listingQuality));
  const c5 = clamp(100 - quality * 10);

  const prices = p.competitors.map((c) => c.price);
  const cv = stddev(prices) / mean(prices);
  const c6 = 100 - norm(cv, 0.05, 0.25);

  return weighted([
    { value: c1, weight: 30 },
    { value: c2, weight: 20 },
    { value: c3, weight: 15 },
    { value: c4, weight: 15 },
    { value: c5, weight: 15 },
    { value: c6, weight: 5 },
  ]);
}

/* ------------------------------------------------------------------ *
 * Pillar 3 — Differentiation
 * ------------------------------------------------------------------ */

const PACKAGING_WORDS = ["packag", "leak", "damag", "crack", "lid", "seal"];
const USAGE_WORDS = ["instruct", "confus", "manual", "how to", "unclear"];

export function differentiationScore(p: Product): number | null {
  if (!p.complaints.length && !p.missingFeatures.length) return null;

  const intensity = p.complaints.reduce((a, c) => a + c.freqPct * c.severity, 0);
  const f1 = norm(intensity, 10, 60);

  const f2 = Math.min(p.missingFeatures.length, 4) * 25;

  const totalMass = p.complaints.reduce((a, c) => a + c.freqPct, 0);
  const addressableMass = p.complaints
    .filter((c) => c.severity >= 2)
    .reduce((a, c) => a + c.freqPct, 0);
  const f3 = totalMass > 0 ? clamp((addressableMass / totalMass) * 100) : null;

  const lower = p.complaints.map((c) => c.theme.toLowerCase());
  const hasPackaging = lower.some((t) => PACKAGING_WORDS.some((w) => t.includes(w)));
  const hasUsage = lower.some((t) => USAGE_WORDS.some((w) => t.includes(w)));
  let f4 = 0;
  if (hasPackaging) f4 += 50;
  f4 += 25; // no competitor in this demo set bundles the complement
  if (hasUsage) f4 += 25;

  return weighted([
    { value: f1, weight: 40 },
    { value: f2, weight: 25 },
    { value: f3, weight: 20 },
    { value: f4, weight: 15 },
  ]);
}

/* ------------------------------------------------------------------ *
 * Pillar 4 — Profitability (stressed case)
 * ------------------------------------------------------------------ */

export function profitabilityScore(stressed: ProfitResult): number | null {
  const p1 = norm(stressed.marginPct, 0.25, 0.45);
  const p2 = norm(stressed.roiPct, 1.0, 3.0);

  const cap = stressed.launchCapital;
  let p3: number;
  if (cap <= 14000 && cap >= 5000) p3 = 100;
  else if (cap < 5000 && cap >= 3000) p3 = 70;
  else if (cap > 14000 && cap <= 17000) p3 = 100 + norm(cap, 14000, 17000) * -40;
  else if (cap > 17000 && cap <= 20000) p3 = 60 - norm(cap, 17000, 20000) * 60;
  else if (cap > 20000) p3 = 0;
  else p3 = 40;

  const months = stressed.paybackMonths;
  const p4 = !isFinite(months) ? 0 : months <= 4 ? 100 : months >= 9 ? 0 : 100 - norm(months, 4, 9);

  let score = weighted([
    { value: p1, weight: 35 },
    { value: p2, weight: 25 },
    { value: p3, weight: 20 },
    { value: p4, weight: 20 },
  ]);
  if (score == null) return null;
  if (!stressed.allGatesPass) score = Math.min(score, 40);
  return score;
}

/* ------------------------------------------------------------------ *
 * Pillar 5 — Risk (deduction ledger)
 * ------------------------------------------------------------------ */

export function riskScore(p: Product): number {
  const deductions = p.risks.reduce((a, r) => a + r.deduction, 0);
  return clamp(100 - deductions);
}

/* ------------------------------------------------------------------ *
 * Composite, gates and verdict
 * ------------------------------------------------------------------ */

export interface GateResult {
  id: string;
  label: string;
  pass: boolean;
  detail: string;
}

export interface Scored {
  pillars: Pillars;
  composite: number | null;
  verdict: Verdict;
  verdictBasis: string;
  gates: GateResult[];
  profit: ProfitResult;
  stressed: ProfitResult;
  confidence: Confidence;
}

export function scoreProduct(p: Product, opts: ProfitOptions = {}): Scored {
  const profit = computeProfit(p, opts);
  const stressed = stressedProfit(p, opts);

  const pillars: Pillars = {
    demand: demandScore(p),
    competition: competitionScore(p),
    differentiation: differentiationScore(p),
    profitability: profitabilityScore(stressed),
    risk: riskScore(p),
  };

  const w = CONFIG.score_weights;
  const composite = weighted([
    { value: pillars.demand, weight: w.demand },
    { value: pillars.competition, weight: w.competition },
    { value: pillars.differentiation, weight: w.differentiation },
    { value: pillars.profitability, weight: w.profitability },
    { value: pillars.risk, weight: w.risk },
  ]);

  const presentCount = Object.values(pillars).filter((v) => v != null).length;
  const confidence: Confidence =
    p.confidence === "low" || presentCount <= 3
      ? "low"
      : p.confidence === "high" && presentCount === 5
        ? "high"
        : "medium";

  const gates: GateResult[] = [
    {
      id: "G1",
      label: "Profit gates",
      pass: profit.allGatesPass,
      detail: `margin ${(profit.marginPct * 100).toFixed(0)}% / ROI ${(profit.roiPct * 100).toFixed(0)}% / payback ${profit.paybackMonths.toFixed(1)} mo`,
    },
    {
      id: "G2",
      label: "Data quality",
      pass: confidence !== "low",
      detail: confidence === "low" ? `${presentCount}/5 pillars present` : `${presentCount}/5 pillars, no partial state`,
    },
    {
      id: "G3",
      label: "Risk floor",
      pass: (pillars.risk ?? 0) >= CONFIG.gates.risk_floor,
      detail: `risk ${Math.round(pillars.risk ?? 0)} ≥ ${CONFIG.gates.risk_floor}`,
    },
    {
      id: "G4",
      label: "Differentiation floor",
      pass: (pillars.differentiation ?? 0) >= CONFIG.gates.differentiation_floor,
      detail: `differentiation ${Math.round(pillars.differentiation ?? 0)} ≥ ${CONFIG.gates.differentiation_floor}`,
    },
    {
      id: "G5",
      label: "Strategist concurrence",
      pass: false,
      detail: "pending — no LLM layer runs in this preview",
    },
  ];

  const hardFail = !gates[0].pass || !gates[2].pass;
  const softFail = !gates[1].pass || !gates[3].pass;

  let verdict: Verdict = "AVOID";
  let verdictBasis = "";
  if (hardFail) {
    verdict = "AVOID";
    verdictBasis = `Hard gate failure (${gates.filter((g) => !g.pass && (g.id === "G1" || g.id === "G3")).map((g) => g.id).join(", ")})`;
  } else if (composite != null && composite >= CONFIG.verdicts.buy_min && !softFail) {
    verdict = "BUY";
    verdictBasis = `Score ${composite.toFixed(0)} ≥ ${CONFIG.verdicts.buy_min} with all soft gates passing`;
  } else if (composite != null && composite >= CONFIG.verdicts.test_min) {
    verdict = "TEST";
    verdictBasis = softFail
      ? `Score ${composite.toFixed(0)} with soft-gate failure (${gates.filter((g) => !g.pass && (g.id === "G2" || g.id === "G4")).map((g) => g.id).join(", ")})`
      : `Score ${composite.toFixed(0)} in the ${CONFIG.verdicts.test_min}–${CONFIG.verdicts.buy_min} band`;
  } else {
    verdict = "AVOID";
    verdictBasis = `Score ${composite?.toFixed(0) ?? "—"} below ${CONFIG.verdicts.test_min}`;
  }

  return { pillars, composite, verdict, verdictBasis, gates, profit, stressed, confidence };
}

/* ------------------------------------------------------------------ *
 * Launchability (docs/launchability.md)
 * ------------------------------------------------------------------ */

export interface LaunchabilityResult {
  score: number | null;
  components: { label: string; value: number | null; weight: number }[];
  missing: string[];
  confidence: Confidence;
}

export function launchability(p: Product, targetPrice: number): LaunchabilityResult {
  if (!p.competitors.length) {
    return { score: null, components: [], missing: ["no competitor set"], confidence: "low" };
  }
  const reviews = p.competitors.map((c) => c.reviews);
  const medianReviews = 100 - logNorm(median(reviews), 100, 3000);
  const heavy = clamp(norm(p.competitors.filter((c) => c.reviews > 1000).length, 0, 5));
  const heavyScore = 100 - heavy;

  const brands = p.competitors.map((c) => c.brand.toLowerCase());
  let brandScore = 100;
  if (brands.some((b) => b.startsWith("amazon"))) brandScore -= 60;
  if (brands.some((b) => ESTABLISHED_BRANDS.includes(b))) brandScore -= 30;
  brandScore = clamp(brandScore);

  const medianAge = median(p.competitors.map((c) => c.ageDays));
  const ageScore = 100 - norm(medianAge, 180, 1825);

  const qualityGap = clamp(100 - mean(p.competitors.map(listingQuality)) * 10);

  const inBand = p.competitors.filter(
    (c) => Math.abs(c.price - targetPrice) / targetPrice <= 0.15
  ).length;
  const priceScore = 100 - norm(inBand, 0, 5);

  const components = [
    { label: "Median reviews of top-10", value: medianReviews, weight: 25 },
    { label: "Deeply-reviewed incumbents", value: heavyScore, weight: 15 },
    { label: "Brand wall (Amazon / established)", value: brandScore, weight: 15 },
    { label: "Incumbent listing age", value: ageScore, weight: 15 },
    { label: "Listing-quality gap", value: qualityGap, weight: 15 },
    { label: "Crowding at your target price", value: priceScore, weight: 15 },
  ];

  return {
    score: weighted(components),
    components,
    missing: [],
    confidence: "medium",
  };
}

/* ------------------------------------------------------------------ *
 * Sellability (docs/sellability.md) — opportunity + non-overlapping parts
 * ------------------------------------------------------------------ */

export interface SellabilityResult {
  score: number | null;
  components: { label: string; value: number | null; weight: number }[];
  missing: string[];
  confidence: Confidence;
  reason: string;
}

export function computeSellability(
  p: Product,
  composite: number | null,
  targetPrice: number
): SellabilityResult {
  if (!p.competitors.length) {
    return {
      score: composite,
      components: [{ label: "Opportunity score", value: composite, weight: 55 }],
      missing: ["price headroom", "incumbent freshness", "product momentum"],
      confidence: "low",
      reason: "No competitor set — sellability falls back to the opportunity score alone.",
    };
  }

  const inBand = p.competitors.filter(
    (c) => Math.abs(c.price - targetPrice) / targetPrice <= 0.15
  ).length;
  const priceHeadroom = 100 - norm(inBand, 0, 5);
  const medianAge = median(p.competitors.map((c) => c.ageDays));
  const freshness = 100 - norm(medianAge, 180, 1825);
  const momentum = logNorm(Math.max(0.5, p.reviewsPerMonth), 1, 120);

  const components = [
    { label: "Opportunity score", value: composite, weight: 55 },
    { label: "Price headroom at your target", value: priceHeadroom, weight: 22 },
    { label: "Incumbent freshness", value: freshness, weight: 13 },
    { label: "Product momentum (own review velocity)", value: momentum, weight: 10 },
  ];

  const score = weighted(components);
  return {
    score,
    components,
    missing: [],
    confidence: "high",
    reason:
      score == null
        ? "Not enough data."
        : `Sellability ${score.toFixed(0)}/100: opportunity ${composite?.toFixed(0) ?? "—"}/100; price headroom ${priceHeadroom.toFixed(0)}/100; incumbents ${freshness > 60 ? "movable" : "entrenched"} (${freshness.toFixed(0)}/100).`,
  };
}

/* ------------------------------------------------------------------ *
 * Display helpers
 * ------------------------------------------------------------------ */

export const PILLAR_KEYS = [
  "demand",
  "competition",
  "differentiation",
  "profitability",
  "risk",
] as const;

export const PILLAR_LABELS: Record<(typeof PILLAR_KEYS)[number], string> = {
  demand: "Demand",
  competition: "Competition",
  differentiation: "Differentiation",
  profitability: "Profitability",
  risk: "Risk",
};

export function tone(score: number | null | undefined) {
  if (score == null) return "slate" as const;
  if (score >= 75) return "emerald" as const;
  if (score >= 60) return "sky" as const;
  if (score >= 45) return "amber" as const;
  return "rose" as const;
}

export function verdictTone(v: Verdict) {
  return v === "BUY" ? ("emerald" as const) : v === "TEST" ? ("amber" as const) : ("rose" as const);
}

export function formatUsd(x: number) {
  return `$${x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function formatCompact(x: number) {
  if (x >= 1_000_000) return `${(x / 1_000_000).toFixed(1)}M`;
  if (x >= 1_000) return `${(x / 1_000).toFixed(x >= 10_000 ? 0 : 1)}k`;
  return `${Math.round(x)}`;
}
