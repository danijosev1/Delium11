import { useMemo } from "react";
import { useStore } from "../state/store";
import { computeSellability, scoreProduct } from "./engine";
import type { ProfitOptions, Scored, SellabilityResult } from "./engine";
import { resolveKey } from "./categories";
import type { Product, ResearchProfile } from "../types";

/** The Research Profile's COGS/freight feed every profit calculation. */
export function profitOptsFor(profile: ResearchProfile): ProfitOptions {
  return profile.cogsMode === "usd"
    ? { cogsUsd: profile.cogsValue, freight: profile.freightPerUnit }
    : { cogsPct: profile.cogsValue, freight: profile.freightPerUnit };
}

export interface ProfileFit {
  ok: boolean;
  misses: string[];
  preferred: boolean;
}

/** Preferences layer only: it highlights and sorts, it never relaxes a kill or gate. */
export function profileFit(p: Product, profile: ResearchProfile): ProfileFit {
  const misses: string[] = [];
  if (p.price < profile.priceMin || p.price > profile.priceMax)
    misses.push(`price $${p.price.toFixed(2)} outside $${profile.priceMin}–$${profile.priceMax}`);
  if (p.monthlySold < profile.minMonthlySales)
    misses.push(`${p.monthlySold}/mo below your ${profile.minMonthlySales}/mo minimum`);
  if (p.reviews > profile.maxReviews)
    misses.push(`${p.reviews.toLocaleString()} reviews above your ${profile.maxReviews.toLocaleString()} cap`);
  if (p.weightG > profile.maxWeightG) misses.push(`${p.weightG} g over your ${profile.maxWeightG} g limit`);
  if (!profile.marketplaces.includes(p.marketplace)) misses.push(`${p.marketplace} not in your marketplaces`);
  const excluded = resolveKey(p.category, profile.excludedCategories);
  if (excluded) misses.push(`excluded category "${excluded}"`);
  const preferred = resolveKey(p.category, profile.preferredCategories) != null;
  return { ok: misses.length === 0, misses, preferred };
}

export interface Analysed {
  product: Product;
  scored: Scored;
  sellability: SellabilityResult;
  fit: ProfileFit;
}

export function analyse(p: Product, profile: ResearchProfile): Analysed {
  const scored = scoreProduct(p, profitOptsFor(profile));
  const sellability = computeSellability(p, scored.composite, p.price);
  return { product: p, scored, sellability, fit: profileFit(p, profile) };
}

export function useAnalysed(): Analysed[] {
  const { products, activeProfile } = useStore();
  return useMemo(() => products.map((p) => analyse(p, activeProfile)), [products, activeProfile]);
}
