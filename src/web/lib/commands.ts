import type { Product, ResearchProfile } from "../types";

export interface CommandSpec {
  id: string;
  group: "Discovery" | "Validation" | "Signals" | "Maintenance" | "UI";
  title: string;
  description: string;
  command: string;
  keepsTokens?: number;
  usd?: number;
  llmUsd?: number;
  confirm: boolean;
  notes?: string[];
}

const fmt = (n: number) => (n === 0 ? "$0.00" : `$${n.toFixed(2)}`);

export function buildCommands(profile: ResearchProfile, product?: Product): CommandSpec[] {
  const mp = profile.marketplaces.join(",");
  const asin = product?.asin ?? "B0EXAMPLE1";

  return [
    {
      id: "discover",
      group: "Discovery",
      title: "Discover a niche",
      description: "Related keywords → SERP top-10 → Keepa quick stats → Scout agent clusters candidates.",
      command: `uv run delium discover "silicone baby food tray"`,
      usd: 0.3,
      tokens: undefined,
      confirm: true,
      notes: ["Cheap and wide. No review fetching at this tier."],
    },
    {
      id: "emerging",
      group: "Discovery",
      title: "Emerging products",
      description: "Keepa Product Finder sweep across BSR sub-bands, then emerging→scoring.",
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
      command: `uv run delium scan --marketplaces ${mp} --budget-cap ${profile.scanTokenCap} --max-spend ${profile.scanUsdCap} --sweep-size ${profile.sweepSize} --competitor-sets ${profile.competitorSets} --light`,
      tokens: profile.scanTokenCap,
      usd: 6.65,
      llmUsd: 5,
      confirm: true,
      notes: ["Light mode finalists ≈ $6.65 worst case, typically ~$1–2 with a warm cache."],
    },
    {
      id: "validate",
      group: "Validation",
      title: `Validate ${asin}`,
      description:
        "The deep dive: fetch (target + 20 competitors + ~400 reviews) → deterministic compute → Analyst + Review Miner + Strategist → report.",
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
      notes: ["Diagnostic only — it never changes a curve, fee, weight, kill or gate."],
    },
    {
      id: "zombies",
      group: "Signals",
      title: "Zombie listings",
      description: "Out-of-stock long-term, strongly reviewed, proven past demand — verified demand proof.",
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
      id: "schedule stats",
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

export function scanProjection(profile: ResearchProfile) {
  const sweepTokens = Math.round(profile.sweepSize * 0.15) + 11;
  const hydrateTokens = profile.sweepSize * 2;
  const competitorTokens = profile.competitorSets * 20;
  const serpUsd = profile.competitorSets * 0.03;
  const lightFinalists = 5;
  const finalistUsd = lightFinalists * 0.33;
  const llmUsd = lightFinalists * 1.0;
  return {
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

export function formatCostLine(c: CommandSpec) {
  const parts: string[] = [];
  if (c.tokens) parts.push(`${c.tokens.toLocaleString()} Keepa tokens`);
  if (c.usd != null) parts.push(`${fmt(c.usd)} data`);
  if (c.llmUsd) parts.push(`${fmt(c.llmUsd)} LLM`);
  return parts.length ? parts.join(" · ") : "free (cached / DB only)";
}
