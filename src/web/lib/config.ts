/**
 * Mirror of config/config.toml (read-only in the browser). The source of truth stays
 * the TOML file. Keep these in sync when you change your assumptions.
 */
export const CONFIG = {
  marketplace: "US",
  capital: { max_launch_budget: 15000 },
  preferences: {
    min_price: 18,
    max_price: 60,
    avoid: ["oversized", "glass", "batteries", "topicals", "gated"],
  },
  assumptions: {
    default_cogs_pct: 0.25,
    cogs_optimistic_pct: 0.2,
    cogs_pessimistic_pct: 0.32,
    freight_per_unit: 0.9,
    duty_pct: 0.05,
    tacos_pct: 0.15,
    return_rate_default: 0.04,
    inventory_months: 2.5,
    ppc_ramp_usd: 2000,
    fixed_launch_usd: 1500,
    packaging_dims_allowance: 0.1,
  },
  stress: { price_delta: -0.1, cogs_delta: 0.15, tacos_delta_pts: 0.05 },
  gates: {
    min_margin: 0.3,
    min_roi: 1.0,
    max_payback_months: 6,
    risk_floor: 40,
    differentiation_floor: 45,
  },
  score_weights: { demand: 25, competition: 25, differentiation: 20, profitability: 20, risk: 10 },
  budgets: {
    max_data_usd_per_validate: 3.0,
    max_data_usd_per_discover: 0.5,
    max_llm_usd_per_validate: 1.0,
    monthly_spend_alarm_usd: 120,
  },
  verdicts: { buy_min: 75, test_min: 60 },
} as const;
