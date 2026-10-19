import React, { useMemo, useState } from "react";
import { Info, Terminal } from "lucide-react";
import { useStore } from "../state/store";
import { buildCommands, formatCostLine } from "../lib/commands";
import type { CommandSpec } from "../lib/commands";
import {
  Badge,
  Card,
  CommandBlock,
  PageHeader,
  SectionTitle,
  SelectField,
  Stat,
} from "../components/ui";

const GROUP_ORDER: CommandSpec["group"][] = [
  "Discovery",
  "Validation",
  "Signals",
  "Maintenance",
  "UI",
];

const GROUP_BLURB: Record<CommandSpec["group"], string> = {
  Discovery: "Cheap and wide — find candidates worth validating.",
  Validation: "The deep dives that spend real money and produce a verdict.",
  Signals: "Cross-cutting evidence: watchlist deltas, dead listings, portfolio ranking.",
  Maintenance: "Scheduler, diagnosis and housekeeping — zero API cost.",
  UI: "The local Streamlit app (this console is a browser-side companion to it).",
};

export default function Commands() {
  const { activeProfile, products, keepaTokens, spendThisMonth } = useStore();
  const [asin, setAsin] = useState(products[0]?.asin ?? "");

  const product = useMemo(() => products.find((p) => p.asin === asin), [products, asin]);
  const commands = useMemo(() => buildCommands(activeProfile, product), [activeProfile, product]);

  const paid = commands.filter((c) => c.confirm);
  const grouped = GROUP_ORDER.map((group) => ({
    group,
    items: commands.filter((c) => c.group === group),
  })).filter((g) => g.items.length > 0);

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-4 md:p-8">
      <PageHeader
        title="Commands"
        subtitle={
          <>
            Every CLI entry point with its projected worst-case cost. This preview cannot reach Keepa, DataForSEO or
            a review provider — copy a command into your terminal to run it for real against the profile{" "}
            <span className="font-medium text-slate-700">{activeProfile.name}</span>.
          </>
        }
        actions={
          <SelectField
            value={asin}
            onChange={setAsin}
            options={products.map((p) => ({ value: p.asin, label: `${p.asin} — ${p.title.slice(0, 34)}…` }))}
          />
        }
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat icon={Terminal} label="Commands" value={commands.length} hint={`${paid.length} require a confirm`} />
        <Stat label="Keepa tokens left" value={keepaTokens.toLocaleString()} hint="refills ~20/min" />
        <Stat label="Spend this month" value={`$${spendThisMonth.toFixed(2)}`} hint="alarm at $120" />
        <Stat
          label="Data budget"
          value={`$${activeProfile.scanUsdCap}/scan`}
          hint={`${activeProfile.scanTokenCap.toLocaleString()} token cap`}
        />
      </div>

      <Card className="flex gap-3 border-slate-200 bg-slate-50/70 p-4">
        <Info className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
        <p className="text-xs leading-relaxed text-slate-600">
          Costs are the <strong>worst case</strong> from <code className="font-mono">config/config.toml</code> and the
          README cost model. Everything is cache-first, so a repeated lookup is free and real costs run well below
          these caps. The caps bind on the projection, before any spend.
        </p>
      </Card>

      {grouped.map(({ group, items }) => (
        <section key={group} className="space-y-4">
          <SectionTitle title={group} subtitle={GROUP_BLURB[group]} />
          {items.map((cmd) => (
            <Card key={cmd.id} className="p-4">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold text-slate-900">{cmd.title}</h3>
                  <p className="mt-0.5 max-w-2xl text-xs leading-relaxed text-slate-500">{cmd.description}</p>
                </div>
                <Badge tone={cmd.confirm ? "amber" : "slate"}>
                  {cmd.confirm ? "confirm required" : "no confirm"}
                </Badge>
              </div>

              <div className="mt-3">
                <CommandBlock command={cmd.command} cost={formatCostLine(cmd)} />
              </div>

              {cmd.notes && cmd.notes.length > 0 && (
                <ul className="mt-3 space-y-1">
                  {cmd.notes.map((n) => (
                    <li key={n} className="flex gap-2 text-[11px] leading-relaxed text-slate-500">
                      <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-slate-300" />
                      {n}
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          ))}
        </section>
      ))}
    </div>
  );
}
