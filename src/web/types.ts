export type Marketplace = "US" | "CA" | "UK" | "AU" | "IN";
export const MARKETPLACES: Marketplace[] = ["US", "CA", "UK", "AU", "IN"];

export type Verdict = "BUY" | "TEST" | "AVOID";
export type Confidence = "high" | "medium" | "low";
export type SalesSource = "amazon" | "estimated";
export type ShortlistStatus = "shortlist" | "watching" | "validated" | "rejected";

export interface Competitor {
  asin: string;
  title: string;
  brand: string;
  price: number;
  reviews: number;
  rating: number;
  ageDays: number;
  imagesCount: number;
}

export interface Complaint {
  theme: string;
  freqPct: number;
  severity: 1 | 2 | 3;
  quotes: string[];
}

export interface RiskFlag {
  flag: string;
  deduction: number;
  evidence: string;
}

export interface Keyword {
  phrase: string;
  volume: number;
}

export interface Pillars {
  demand: number | null;
  competition: number | null;
  differentiation: number | null;
  profitability: number | null;
  risk: number | null;
}

export interface Product {
  asin: string;
  marketplace: Marketplace;
  title: string;
  brand: string;
  category: string;
  emoji: string;
  hue: number;
  price: number;
  monthlySold: number;
  salesSource: SalesSource;
  bsr: number;
  reviews: number;
  rating: number;
  reviewsPerMonth: number;
  ageDays: number;
  weightG: number;
  dimsMm: [number, number, number];
  fbaFeeCents: number | null;
  confidence: Confidence;
  why: string;
  emergence: number;
  pillars: Pillars;
  bsrHistory: number[];
  soldHistory: number[];
  competitors: Competitor[];
  complaints: Complaint[];
  missingFeatures: string[];
  risks: RiskFlag[];
  keywords: Keyword[];
  reviewSample: number;
}

export interface ResearchProfile {
  id: string;
  name: string;
  budget: number;
  priceMin: number;
  priceMax: number;
  minMonthlySales: number;
  maxReviews: number;
  preferredCategories: string[];
  excludedCategories: string[];
  marketplaces: Marketplace[];
  maxWeightG: number;
  cogsMode: "pct" | "usd";
  cogsValue: number;
  freightPerUnit: number;
  targetMargin: number;
  targetRoi: number;
  riskTolerance: "low" | "medium" | "high";
  scanUsdCap: number;
  scanTokenCap: number;
  sweepSize: number;
  competitorSets: number;
}

export interface ShortlistEntry {
  asin: string;
  status: ShortlistStatus;
  notes: string;
  addedAt: string;
}

export interface ScanRecord {
  id: string;
  startedAt: string;
  status: "complete" | "failed" | "aborted";
  marketplaces: Marketplace[];
  finalists: number;
  tokens: number;
  usd: number;
  funnel: { stage: string; count: number | null }[];
}

export interface ProfitOverrides {
  price?: number;
  cogs?: number;
  freight?: number;
  tacosPct?: number;
  unitsLow?: number;
}

export interface CrossMarketRow {
  asin: string;
  title: string;
  source: Marketplace;
  target: Marketplace;
  /** null when the verdict is INSUFFICIENT_DATA (no target evidence to score). */
  score: number | null;
  verdict:
    | "STRONG_OPPORTUNITY"
    | "OPPORTUNITY_TO_VALIDATE"
    | "MATURE_MARKET"
    | "WEAK_TRANSFER"
    | "INSUFFICIENT_DATA";
  confidence: Confidence;
  match: "EXACT" | "STRONG" | "PROBABLE" | "WEAK" | "UNMATCHED";
  note: string;
}

export interface ZombieRow {
  asin: string;
  marketplace: Marketplace;
  title: string;
  brand: string;
  generic: boolean;
  rating: number;
  reviews: number;
  monthsDead: number;
  inStockBsr: number;
  restockGaps: number;
  score: number;
  verdict: "Verified zombie" | "Possibly temporary" | "Not a zombie";
  reason: string;
}
