import type { Product, ResearchProfile } from "../types";

export interface CommandSpec {
  id: string;
  group: "Discovery" | "Validation" | "Signals" | "Maintenance" | "UI";
  title: string;
  description: string;
  command: string;
  tokens?: number;
  usd?: number;
  llmUsd?: number;
  confirm: boolean;
  notes?: string[];
}

export interface ScanOptions {
  enrichLimit?: number;
  full?: boolean;
  top?: number;
  marketplaces?: string[];
  zombies?: boolean;
}

/** Worst-case per-finalist costs from README §Daily Scan cost model. */
const DATA_PER_FINALIST_LIGHT = 0.33;
const DATA_PER_FINALIST_FULL = 3.03;
const LLM_PER_FINALIST = 1.0;

const fmt = (n: number) => (n === 0 ? "$0.00" : `$${n.toFixed(2)}`);

/**
 * Projected worst case for one marketplace. Each extra marketplace adds its own
 * sweep + hydrate; the real CLI prints the exact figure in preflight.
 */
export function scanProjection(profile: ResearchProfile, o: ScanOptions = {}) {
  const finalists = o.full ? o.top ?? 10 : o.enrichLimit ?? 5;
  const sweepTokens = Math.round(profile.sweepSize * 0.15) + 11;
  const hydrateTokens = profile.sweepSize * 2;
  const competitorTokens = profile.competitorSets * 20;
  const serpUsd = profile.competitorSets * 0.03;
  const finalistUsd = finalists * (o.full ? DATA_PER_FINALIST_FULL : DATA_PER_FINALIST_LIGHT);
  const llmUsd = finalists * LLM_PER_FINALIST;
  return {
    finalists,
    sweepTokens,
    hydrateTokens,
    competitorTokens,
    totalTokens: sweepTokens + hydrateTokens + competitorTokens,
    serpUsd,
    finalistUsd,
    llmUsd,
    totalUsd: serpUsd + finalistUsd + llmUsd,
  };
}

/** Largest light-mode enrich limit (5 → 1) whose projection fits the profile's USD cap. */
export function fittingEnrichLimit(profile: ResearchProfile): number {
  for (let e = 5; e >= 1; e--) {
    if (scanProjection(profile, { enrichLimit: e }).totalUsd <= profile.scanUsdCap) return e;
  }
  return 1;
}

export function scanCommand(profile: ResearchProfile, o: ScanOptions = {}): string {
  const mps = (o.marketplaces ?? profile.marketplaces).join(",");
  const parts = ["uv run delium scan", `--marketplaces ${mps}`];
  if (o.top) parts.push(`--top ${o.top}`);
  parts.push(
    `--budget-cap ${profile.scanTokenCap}`,
    `--max-spend ${profile.scanUsdCap}`,
    `--sweep-size ${profile.sweepSize}`,
    `--competitor-sets ${profile.competitorSets}`
  );
  parts.push(o.full ? "--full" : `--light --enrich-limit ${o.enrichLimit ?? 5}`);
  if (o.zombies) parts.push("--zombies");
  return parts.join(" ");
}

export function buildCommands(profile: ResearchProfile, product?: Product): CommandSpec[] {
  const mp = profile.marketplaces.join(",");
  const asin = product?.asin ?? "B0EXAMPLE1";
  const enrich = fittingEnrichLimit(profile);
  const scan = scanProjection(profile, { enrichLimit: enrich });

  return [
    {
      id: "discover",
      group: "Discovery",
      title: "Discover a niche",
      description:
        "Related keywords → SERP top-10 → Keepa quick stats → Scout agent clusters candidates.",
      command: `uv run delium discover "silicone baby food tray"`,
      usd: 0.3,
      confirm: true,
      notes: ["Cheap and wide. No review fetching at this tier."],
    },
    {
      id: "emerging",
      group: "Discovery",
      title: "Emerging products",
      description: "Keepa Product Finder sweep across BSR sub-bands, then the existing kills + scoring.",
      command: `uv run delium emerging --marketplaces ${mp}`,
      tokens: 210,
      usd: 0,
      confirm: true,
    },
    {
      id: "scan",
      group: "Discovery",
      title: "Daily Scan (light)",
      description:
        "Resumable, budget-gated funnel: sweep → hydrate → hard kill → cheap score → competitor sets → rank → finalists.",
      command: scanCommand(profile, { enrichLimit: enrich }),
      tokens: scan.totalTokens,
      usd: Math.round((scan.serpUsd + scan.finalistUsd) * 100) / 100,
      llmUsd: scan.llmUsd,
      confirm: true,
      notes: [
        `Enrich limit ${enrich} is the largest that fits your ${fmt(profile.scanUsdCap)} cap on the worst-case projection.`,
        "Typically ~$1–2 per scan with a warm cache.",
      ],
    },
    {
      id: "validate",
      group: "Validation",
      title: `Validate ${asin}`,
      description:
        "The deep dive: target + 20 competitors + ~400 reviews → deterministic compute → Analyst + Review Miner + Strategist → report.",
      command: `uv run delium validate ${asin}`,
      usd: 2.5,
      llmUsd: 1.0,
      confirm: true,
      notes: ["Caps: data ≤ $3.00, LLM ≤ $1.00 per run (config [budgets])."],
    },
    {
      id: "pains",
      group: "Validation",
      title: `Customer pain deep-dive for ${asin}`,
      description: "~100 reviews each for the target + up to 5 competitors; larger Review Miner budget.",
      command: `uv run delium pains ${asin}`,
      usd: 1.8,
      confirm: true,
    },
    {
      id: "calibrate",
      group: "Validation",
      title: "Calibrate estimates vs Keepa",
      description:
        "Compares the illustrative BSR→units curve and fee table against Keepa monthlySold / pickAndPackFee.",
      command: `uv run delium calibrate --refresh`,
      tokens: 2,
      confirm: true,
      notes: ["~2 tokens per product. Diagnostic only — never changes a curve, fee, weight, kill or gate."],
    },
    {
      id: "zombies",
      group: "Signals",
      title: "Zombie listings",
      description: "Out of stock long-term, strongly reviewed, proven past demand — verified demand proof.",
      command: `uv run delium zombies --marketplaces ${mp} --check-demand`,
      tokens: 420,
      usd: 0.6,
      confirm: true,
    },
    {
      id: "watch",
      group: "Signals",
      title: "Watchlist refresh",
      description: "Weekly Keepa + volume refresh for shortlisted ASINs; flags deltas worth attention.",
      command: `uv run delium watch`,
      usd: 0.2,
      confirm: false,
    },
    {
      id: "portfolio",
      group: "Signals",
      title: "Portfolio view",
      description: "Ranks everything validated to date — pure DB query + render, zero API cost.",
      command: `uv run delium portfolio`,
      usd: 0,
      confirm: false,
    },
    {
      id: "schedule-status",
      group: "Maintenance",
      title: "Scheduler status",
      description: "macOS launchd agent state, run times, last run and log tail.",
      command: `uv run delium schedule status`,
      usd: 0,
      confirm: false,
    },
    {
      id: "diagnose",
      group: "Maintenance",
      title: "Diagnose stored candidates",
      description: "Read-only: rebuilds the deterministic score per pillar with the input that drove it.",
      command: `uv run delium diagnose`,
      usd: 0,
      confirm: false,
    },
    {
      id: "ui",
      group: "UI",
      title: "Command Center (Streamlit)",
      description: "The local Streamlit app that wraps the same internal functions as the CLI.",
      command: `uv run delium ui`,
      usd: 0,
      confirm: false,
    },
  ];
}

export function formatCostLine(c: CommandSpec) {
  const parts: string[] = [];
  if (c.tokens) parts.push(`${c.tokens.toLocaleString()} Keepa tokens`);
  if (c.usd) parts.push(`${fmt(c.usd)} data`);
  if (c.llmUsd) parts.push(`${fmt(c.llmUsd)} LLM`);
  return parts.length ? parts.join(" · ") : "free (cached / DB only)";
}
