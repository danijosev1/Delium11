import type { ResearchProfile } from "../types";

export const CONSERVATIVE: ResearchProfile = {
  id: "conservative-5k",
  name: "Conservative $5k",
  budget: 5000,
  priceMin: 18,
  priceMax: 40,
  minMonthlySales: 200,
  maxReviews: 500,
  preferredCategories: ["Home & Kitchen", "Pet Supplies"],
  excludedCategories: ["Electronics", "Clothing, Shoes & Jewelry"],
  marketplaces: ["US"],
  maxWeightG: 1500,
  cogsMode: "pct",
  cogsValue: 0.25,
  freightPerUnit: 0.9,
  targetMargin: 0.32,
  targetRoi: 1.2,
  riskTolerance: "low",
  scanUsdCap: 3,
  scanTokenCap: 800,
  sweepSize: 150,
  competitorSets: 15,
};

export const GROWTH: ResearchProfile = {
  id: "growth-20k",
  name: "Growth $20k",
  budget: 20000,
  priceMin: 25,
  priceMax: 70,
  minMonthlySales: 300,
  maxReviews: 1200,
  preferredCategories: ["Home & Kitchen", "Sports & Outdoors", "Toys & Games", "Pet Supplies"],
  excludedCategories: ["Clothing, Shoes & Jewelry"],
  marketplaces: ["US", "CA", "UK"],
  maxWeightG: 4000,
  cogsMode: "pct",
  cogsValue: 0.22,
  freightPerUnit: 1.1,
  targetMargin: 0.3,
  targetRoi: 1.0,
  riskTolerance: "medium",
  scanUsdCap: 5,
  scanTokenCap: 1500,
  sweepSize: 300,
  competitorSets: 40,
};

export const PROFILES: ResearchProfile[] = [CONSERVATIVE, GROWTH];
export const DEFAULT_PROFILE_ID = CONSERVATIVE.id;
