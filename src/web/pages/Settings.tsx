import React, { useEffect, useState } from "react";
import { Check, KeyRound, RotateCcw, Save, Wallet, X } from "lucide-react";
import { useStore } from "../state/store";
import { MARKETPLACES } from "../types";
import type { Marketplace, ResearchProfile } from "../types";
import {
  Badge,
  Button,
  Card,
  NumField,
  PageHeader,
  SectionTitle,
  SelectField,
  Stat,
  TextField,
  Toggle,
  cx,
} from "../components/ui";

const CREDENTIALS = [
  { key: "DELIUM_KEEPA_API_KEY", label: "Keepa", configured: true },
  { key: "DELIUM_DATAFORSEO_LOGIN", label: "DataForSEO login", configured: true },
  { key: "DELIUM_DATAFORSEO_PASSWORD", label: "DataForSEO password", configured: true },
  { key: "DELIUM_UNWRANGLE_API_KEY", label: "Unwrangle (reviews)", configured: true },
  { key: "DELIUM_APIFY_API_TOKEN", label: "Apify (reviews fallback)", configured: false },
  { key: "DELIUM_LLM_API_KEY", label: "LLM vendor", configured: false },
];

export default function Settings() {
  const store = useStore();
  const { profiles, activeProfile, setActiveProfile, upsertProfile, resetProfiles, keepaTokens, spendThisMonth, scans } =
    store;

  const [draft, setDraft] = useState<ResearchProfile>(activeProfile);
  const dirty = JSON.stringify(draft) !== JSON.stringify(activeProfile);

  useEffect(() => {
    setDraft(activeProfile);
  }, [activeProfile]);

  const set = <K extends keyof ResearchProfile>(key: K, value: ResearchProfile[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));

  const toggleMarketplace = (m: Marketplace) =>
    set(
      "marketplaces",
      draft.marketplaces.includes(m)
        ? draft.marketplaces.filter((x) => x !== m)
        : [...draft.marketplaces, m]
    );

  return (
    <div className="mx-auto max-w-4xl space-y-6 p-4 md:p-8">
      <PageHeader
        title="Settings"
        subtitle={
          <>
            The Research Profile drives every finder default, every profit calculation, and how results are sorted.
            It is a <strong>preferences layer only</strong> — it never relaxes a hard kill, gate or scoring weight.
          </>
        }
        actions={
          <>
            <Button
              size="sm"
              onClick={resetProfiles}
              title="Restore the shipped preset list"
            >
              <RotateCcw className="h-3.5 w-3.5" /> Reset presets
            </Button>
            <Button
              size="sm"
              variant="primary"
              disabled={!dirty}
              onClick={() => upsertProfile(draft)}
            >
              <Save className="h-3.5 w-3.5" /> {dirty ? "Save profile" : "Saved"}
            </Button>
          </>
        }
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Active preset" value={activeProfile.name} hint={`$${activeProfile.budget.toLocaleString()} budget`} />
        <Stat label="Profiles" value={profiles.length} hint="edit or add your own" />
        <Stat label="Runs logged" value={scans.length} hint="in this browser session" />
        <Stat label="Spend this month" value={`$${spendThisMonth.toFixed(2)}`} hint={`${keepaTokens.toLocaleString()} tokens left`} />
      </div>

      {profiles.length > 0 && (
        <Card className="p-4">
          <SectionTitle title="Preset" subtitle="Switch the active profile, or edit it below." />
          <div className="flex flex-wrap gap-2">
            {profiles.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => setActiveProfile(p.id)}
                className={cx(
                  "rounded-lg border px-3 py-1.5 text-sm font-medium transition",
                  p.id === activeProfile.id
                    ? "border-emerald-300 bg-emerald-50 text-emerald-800"
                    : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                )}
              >
                {p.name}
              </button>
            ))}
          </div>
        </Card>
      )}

      <Card className="p-5">
        <SectionTitle
          title="Research Profile"
          subtitle="Preferences only. Hard kills (K1–K12), gates (G1–G5) and score weights stay in config.toml + scoring.py."
        />

        <div className="space-y-5">
          <TextField
            label="Profile name"
            value={draft.name}
            onChange={(v) => set("name", v)}
            placeholder="e.g. Conservative $5k"
          />

          <div className="grid gap-4 sm:grid-cols-3">
            <NumField
              label="Total budget"
              value={draft.budget}
              onChange={(v) => set("budget", v)}
              prefix="$"
              step={500}
              min={0}
              hint="max launch budget (inventory + PPC ramp)"
            />
            <NumField
              label="Min sell price"
              value={draft.priceMin}
              onChange={(v) => set("priceMin", v)}
              prefix="$"
              step={1}
              min={0}
            />
            <NumField
              label="Max sell price"
              value={draft.priceMax}
              onChange={(v) => set("priceMax", v)}
              prefix="$"
              step={1}
              min={0}
            />
          </div>

          <div className="grid gap-4 sm:grid-cols-3">
            <NumField
              label="Min monthly sales"
              value={draft.minMonthlySales}
              onChange={(v) => set("minMonthlySales", v)}
              step={50}
              min={0}
              hint="units / month floor"
            />
            <NumField
              label="Max reviews"
              value={draft.maxReviews}
              onChange={(v) => set("maxReviews", v)}
              step={50}
              min={0}
              hint="review-count ceiling"
            />
            <NumField
              label="Max unit weight"
              value={draft.maxWeightG}
              onChange={(v) => set("maxWeightG", v)}
              suffix="g"
              step={100}
              min={0}
            />
          </div>

          <div className="grid gap-4 sm:grid-cols-3">
            <SelectField
              label="COGS mode"
              value={draft.cogsMode}
              onChange={(v) => set("cogsMode", v as ResearchProfile["cogsMode"])}
              options={[
                { value: "pct", label: "% of sell price" },
                { value: "usd", label: "$ per unit" },
              ]}
            />
            {draft.cogsMode === "pct" ? (
              <NumField
                label="COGS"
                value={draft.cogsValue}
                onChange={(v) => set("cogsValue", v)}
                scale={100}
                suffix="%"
                step={1}
                min={0}
              />
            ) : (
              <NumField
                label="COGS"
                value={draft.cogsValue}
                onChange={(v) => set("cogsValue", v)}
                prefix="$"
                step={0.1}
                min={0}
              />
            )}
            <NumField
              label="Freight / unit"
              value={draft.freightPerUnit}
              onChange={(v) => set("freightPerUnit", v)}
              prefix="$"
              step={0.1}
              min={0}
            />
          </div>

          <div className="grid gap-4 sm:grid-cols-3">
            <NumField
              label="Target net margin"
              value={draft.targetMargin}
              onChange={(v) => set("targetMargin", v)}
              scale={100}
              suffix="%"
              step={1}
              min={0}
            />
            <NumField
              label="Target ROI / turn"
              value={draft.targetRoi}
              onChange={(v) => set("targetRoi", v)}
              scale={100}
              suffix="%"
              step={10}
              min={0}
            />
            <SelectField
              label="Risk tolerance"
              value={draft.riskTolerance}
              onChange={(v) => set("riskTolerance", v as ResearchProfile["riskTolerance"])}
              options={[
                { value: "low", label: "Low" },
                { value: "medium", label: "Medium" },
                { value: "high", label: "High" },
              ]}
            />
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <TextField
              label="Preferred categories"
              value={draft.preferredCategories.join(", ")}
              onChange={(v) =>
                set(
                  "preferredCategories",
                  v.split(",").map((s) => s.trim()).filter(Boolean)
                )
              }
              hint="comma separated — highlights and boosts, never a gate"
            />
            <TextField
              label="Excluded categories"
              value={draft.excludedCategories.join(", ")}
              onChange={(v) =>
                set(
                  "excludedCategories",
                  v.split(",").map((s) => s.trim()).filter(Boolean)
                )
              }
              hint="comma separated — matched on breadcrumb segments, not substrings"
            />
          </div>

          <div>
            <span className="text-xs font-medium text-slate-600">Marketplaces</span>
            <div className="mt-1.5 flex flex-wrap gap-2">
              {MARKETPLACES.map((m) => {
                const on = draft.marketplaces.includes(m);
                const noCoverage = m === "AU";
                return (
                  <button
                    key={m}
                    type="button"
                    onClick={() => toggleMarketplace(m)}
                    title={noCoverage ? "Keepa has no Australia coverage" : undefined}
                    className={cx(
                      "rounded-lg border px-3 py-1.5 text-sm font-medium transition",
                      on
                        ? "border-emerald-300 bg-emerald-50 text-emerald-800"
                        : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                    )}
                  >
                    {m}
                    {noCoverage && <span className="ml-1 text-[10px] text-amber-600">no data</span>}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <NumField
              label="Scan USD cap"
              value={draft.scanUsdCap}
              onChange={(v) => set("scanUsdCap", v)}
              prefix="$"
              step={1}
              min={0}
            />
            <NumField
              label="Scan token cap"
              value={draft.scanTokenCap}
              onChange={(v) => set("scanTokenCap", v)}
              step={100}
              min={0}
            />
            <NumField
              label="Sweep size"
              value={draft.sweepSize}
              onChange={(v) => set("sweepSize", v)}
              step={50}
              min={0}
              hint="raw ASINs swept"
            />
            <NumField
              label="Competitor sets"
              value={draft.competitorSets}
              onChange={(v) => set("competitorSets", v)}
              step={5}
              min={0}
              hint="top products getting a page-one set"
            />
          </div>

          {dirty && (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-200 bg-amber-50/70 px-3 py-2 text-xs text-amber-900">
              <span>You have unsaved changes.</span>
              <div className="flex gap-2">
                <Button size="sm" variant="ghost" onClick={() => setDraft(activeProfile)}>
                  Discard
                </Button>
                <Button size="sm" variant="primary" onClick={() => upsertProfile(draft)}>
                  <Save className="h-3.5 w-3.5" /> Save
                </Button>
              </div>
            </div>
          )}
        </div>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card className="p-5">
          <SectionTitle
            title="API credentials"
            subtitle="Read from .env — never committed, never shown here. Status only."
          />
          <ul className="space-y-2">
            {CREDENTIALS.map((c) => (
              <li key={c.key} className="flex items-center justify-between gap-3 text-sm">
                <span className="flex min-w-0 items-center gap-2">
                  <KeyRound className="h-3.5 w-3.5 shrink-0 text-slate-400" />
                  <span className="min-w-0">
                    <span className="block truncate font-medium text-slate-700">{c.label}</span>
                    <span className="block truncate font-mono text-[10px] text-slate-400">{c.key}</span>
                  </span>
                </span>
                {c.configured ? (
                  <Badge tone="emerald" className="gap-1">
                    <Check className="h-3 w-3" /> set
                  </Badge>
                ) : (
                  <Badge tone="slate" className="gap-1">
                    <X className="h-3 w-3" /> not set
                  </Badge>
                )}
              </li>
            ))}
          </ul>
          <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
            This preview cannot read <code className="font-mono">.env</code>, so the statuses above are illustrative —
            check <code className="font-mono">DELIUM_*</code> in your shell for the real picture.
          </p>
        </Card>

        <Card className="p-5">
          <SectionTitle title="Usage" subtitle="Spend guards from config [budgets] — enforced before any paid call." />
          <div className="space-y-4">
            <div>
              <div className="flex items-center justify-between text-xs text-slate-500">
                <span className="flex items-center gap-1.5">
                  <Wallet className="h-3.5 w-3.5" /> Monthly spend
                </span>
                <span className="tabular font-medium text-slate-800">${spendThisMonth.toFixed(2)} / $120</span>
              </div>
              <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-100">
                <div
                  className={cx("h-full rounded-full", spendThisMonth / 120 > 0.85 ? "bg-rose-500" : "bg-emerald-500")}
                  style={{ width: `${Math.min(100, (spendThisMonth / 120) * 100)}%` }}
                />
              </div>
            </div>

            <ul className="space-y-2 text-sm">
              <BudgetRow label="Max data / validate" value="$3.00" />
              <BudgetRow label="Max LLM / validate" value="$1.00" />
              <BudgetRow label="Max data / discover" value="$0.50" />
              <BudgetRow label="Monthly alarm" value="$120.00" />
            </ul>

            <Toggle
              checked
              onChange={() => undefined}
              label="Confirm before every paid provider call"
            />
          </div>
        </Card>
      </div>
    </div>
  );
}

function BudgetRow({ label, value }: { label: string; value: string }) {
  return (
    <li className="flex items-center justify-between gap-3">
      <span className="text-slate-600">{label}</span>
      <span className="font-medium tabular text-slate-800">{value}</span>
    </li>
  );
}
