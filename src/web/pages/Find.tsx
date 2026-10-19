import React, { useMemo, useState } from "react";
import {
  Compass,
  Ghost,
  Globe2,
  History as HistoryIcon,
  Search as SearchIcon,
  Sparkles,
  TrendingUp,
} from "lucide-react";
import { useStore } from "../state/store";
import { useAnalysed } from "../lib/analyse";
import type { Analysed } from "../lib/analyse";
import { topCategory } from "../lib/categories";
import { buildCommands, scanCommand, scanProjection } from "../lib/commands";
import { CONFIG } from "../lib/config";
import { ageLabel, fmtDate, fmtDateTime, fmtScore, pct, usd } from "../lib/format";
import {
  Badge,
  Button,
  Card,
  CommandBlock,
  ConfidenceBadge,
  EmptyState,
  LinkButton,
  PageHeader,
  ProductThumb,
  ScoreBar,
  SectionTitle,
  SelectField,
  Stat,
  Tabs,
  TextField,
  VerdictBadge,
  cx,
  scoreTone,
} from "../components/ui";

type Mode = "emerging" | "keyword" | "discover" | "cross-market" | "zombies" | "history";

const MODES: { id: Mode; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
  { id: "emerging", label: "Emerging", icon: Sparkles },
  { id: "keyword", label: "Keyword", icon: SearchIcon },
  { id: "discover", label: "Black Box", icon: Compass },
  { id: "cross-market", label: "Cross-market", icon: Globe2 },
  { id: "zombies", label: "Zombies", icon: Ghost },
  { id: "history", label: "History", icon: HistoryIcon },
];

export default function Find() {
  const [mode, setMode] = useState<Mode>("emerging");
  const { activeProfile } = useStore();

  return (
    <div className="mx-auto max-w-7xl space-y-6 p-4 md:p-8">
      <PageHeader
        title="Find"
        subtitle={
          <>
            Five discovery sources pouring into one funnel. Every result computes against{" "}
            <span className="font-medium text-slate-700">{activeProfile.name}</span> and links into the workspace.
            Nothing here spends money inside the preview.
          </>
        }
      />

      <Tabs
        tabs={MODES.map((m) => ({ id: m.id, label: m.label }))}
        value={mode}
        onChange={(id) => setMode(id as Mode)}
      />

      {mode === "emerging" && <EmergingMode />}
      {mode === "keyword" && <KeywordMode />}
      {mode === "discover" && <DiscoverMode />}
      {mode === "cross-market" && <CrossMarketMode />}
      {mode === "zombies" && <ZombieMode />}
      {mode === "history" && <HistoryMode />}
    </div>
  );
}

/* ------------------------------------------------------------------ */

function EmergingMode() {
  const { activeProfile } = useStore();
  const analysed = useAnalysed();
  const [minAgeDays] = useState(180);

  const ranked = useMemo(
    () => [...analysed].sort((a, b) => b.product.emergence - a.product.emergence),
    [analysed]
  );

  const cmd = buildCommands(activeProfile).find((c) => c.id === "emerging");
  const emerging = ranked.filter((a) => a.product.emergence >= 40);
  const young = ranked.filter((a) => a.product.ageDays <= minAgeDays);

  return (
    <div className="space-y-5">
      <Card className="flex flex-wrap items-center gap-4 p-4">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-violet-50">
          <Sparkles className="h-5 w-5 text-violet-600" />
        </div>
        <div className="min-w-0 flex-1 text-sm text-slate-600">
          <span className="font-semibold text-slate-900">Keepa Product Finder</span> — recently tracked, selling,
          still low-review. 4 log-spaced BSR sub-bands, then the existing hard kills and{" "}
          <code className="font-mono text-xs">scoring.py</code>. The emergence score explains <em>why</em> a
          product reads as emerging; it never feeds the opportunity score.
        </div>
      </Card>

      {cmd && (
        <Card className="p-5">
          <SectionTitle
            title="Run an emerging sweep"
            subtitle="Keepa only — no DataForSEO fallback. The CLI shows this projection and confirms before spending."
          />
          <CommandBlock command={cmd.command} cost={formatCostLine(cmd)} />
        </Card>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Swept band" value="BSR 200 – 40,000" hint="$15–$60 price · ≤100 reviews" />
        <Stat label="Sub-bands" value="4" hint="log-spaced, merged then ranked locally" />
        <Stat label="Emerging (≥40)" value={emerging.length} hint={`of ${ranked.length} in the demo set`} />
        <Stat label="Tracked ≤180 d" value={young.length} hint="younger products, thinner history" />
      </div>

      <ResultList
        rows={ranked}
        empty="No candidates. Run the sweep in your terminal to populate the demo."
        caption="Ranked by emergence — the age/traction/momentum/competition signal, separate from the opportunity score."
      />
    </div>
  );
}

function KeywordMode() {
  const { activeProfile } = useStore();
  const analysed = useAnalysed();
  const [seed, setSeed] = useState("baby food freezer tray");

  const matches = useMemo(() => {
    const q = seed.trim().toLowerCase();
    if (!q) return [] as { a: Analysed; phrase: string; volume: number }[];
    const out: { a: Analysed; phrase: string; volume: number }[] = [];
    for (const a of analysed) {
      for (const k of a.product.keywords) {
        if (k.phrase.toLowerCase().includes(q) || q.includes(k.phrase.toLowerCase())) {
          out.push({ a, phrase: k.phrase, volume: k.volume });
        }
      }
    }
    return out.sort((x, y) => y.volume - x.volume);
  }, [analysed, seed]);

  const cmd = buildCommands(activeProfile).find((c) => c.id === "discover");

  return (
    <div className="space-y-5">
      <Card className="p-5">
        <SectionTitle
          title="Keyword research"
          subtitle="DataForSEO Amazon bulk volume + related-keyword expansion, then SERP page-one for the survivors."
        />
        <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
          <TextField
            label="Seed phrase"
            value={seed}
            onChange={setSeed}
            placeholder="e.g. collapsible dog bowl"
            hint="Volumes are what the cluster sums: a phrase contained in a bigger one counts at 30%."
          />
          <div className="pb-5">
            {cmd && (
              <CommandBlock
                command={`uv run delium discover "${seed}"`}
                cost={formatCostLine(cmd)}
              />
            )}
          </div>
        </div>
      </Card>

      {matches.length === 0 ? (
        <EmptyState
          icon={SearchIcon}
          title="No stored keyword data matches"
          body="The preview only searches the demo dataset's stored keyword clusters. Run a discover in your terminal for real volume."
        />
      ) : (
        <Card className="overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs font-medium text-slate-500">
              <tr>
                <th className="px-4 py-2.5">Phrase</th>
                <th className="px-4 py-2.5 text-right">Monthly volume</th>
                <th className="px-4 py-2.5">Product</th>
                <th className="px-4 py-2.5 text-right">Score</th>
                <th className="px-4 py-2.5" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {matches.map(({ a, phrase, volume }) => (
                <tr key={`${a.product.asin}-${phrase}`} className="hover:bg-slate-50/60">
                  <td className="px-4 py-2.5 font-medium text-slate-800">{phrase}</td>
                  <td className="px-4 py-2.5 text-right tabular text-slate-700">
                    {volume.toLocaleString()}
                  </td>
                  <td className="max-w-[220px] truncate px-4 py-2.5 text-slate-600">{a.product.title}</td>
                  <td className="px-4 py-2.5 text-right tabular text-slate-700">
                    {fmtScore(a.scored.composite)}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <LinkButton href={`#/product/${a.product.asin}`} size="sm" variant="ghost">
                      Open →
                    </LinkButton>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}

function DiscoverMode() {
  const { activeProfile } = useStore();
  const [seed, setSeed] = useState("silicone baby food tray");
  const cmd = buildCommands(activeProfile).find((c) => c.id === "discover");

  return (
    <div className="space-y-5">
      <Card className="p-5">
        <SectionTitle
          title="Black Box (Discover)"
          subtitle="A seed niche in, a ranked candidate shortlist out — cheap and wide. No reviews fetched at this tier."
        />
        <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
          <TextField label="Niche seed" value={seed} onChange={setSeed} placeholder="e.g. sous vide accessories" />
          <div className="pb-5">
            {cmd && <CommandBlock command={`uv run delium discover "${seed}"`} cost={formatCostLine(cmd)} />}
          </div>
        </div>
      </Card>

      <Card className="p-5">
        <SectionTitle title="What the pipeline does" subtitle="From docs/data-layer.md §3.2" />
        <ol className="space-y-3 text-sm">
          {[
            ["Seed → related keywords", "1–2 DataForSEO calls, a few hundred phrases."],
            ["Filter + bulk volume", "One call ≤1,000 keywords, config bands applied."],
            ["SERP page-one", "Top ~15 surviving keywords → candidate ASIN pool (~100–150)."],
            ["Keepa quick stats", "No offers param — price/BSR/reviews/age only, cheaper tokens."],
            ["Hard kills K1–K6, K10–K12", "All computable from the above, logged with rule name."],
            ["Triage + Scout agent", "Fast tier clusters candidates into niches with a thesis."],
            ["Report", "reports/discover-<slug>-<date>.md — the ranked shortlist."],
          ].map(([t, d], i) => (
            <li key={t} className="flex gap-3">
              <span className="mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-full bg-slate-100 text-xs font-semibold text-slate-600">
                {i + 1}
              </span>
              <div>
                <div className="font-medium text-slate-800">{t}</div>
                <div className="text-slate-500">{d}</div>
              </div>
            </li>
          ))}
        </ol>
      </Card>
    </div>
  );
}

function CrossMarketMode() {
  const { crossMarket } = useStore();

  const verdictTone: Record<string, "emerald" | "sky" | "amber" | "rose" | "slate"> = {
    STRONG_OPPORTUNITY: "emerald",
    OPPORTUNITY_TO_VALIDATE: "sky",
    MATURE_MARKET: "amber",
    WEAK_TRANSFER: "rose",
    INSUFFICIENT_DATA: "slate",
  };

  return (
    <div className="space-y-5">
      <Card className="flex items-start gap-4 p-4">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-sky-50">
          <Globe2 className="h-5 w-5 text-sky-600" />
        </div>
        <div className="text-sm text-slate-600">
          <span className="font-semibold text-slate-900">Directional, evidence-only.</span> A discovery signal, not
          a sixth pillar — <code className="font-mono text-xs">scoring.py</code> still owns the verdict. No target
          demand ≠ opportunity, and no listings ≠ opportunity.
        </div>
      </Card>

      {crossMarket.length === 0 ? (
        <EmptyState icon={Globe2} title="No cross-market runs stored" />
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {crossMarket.map((row) => (
            <Card key={`${row.asin}-${row.target}`} className="flex flex-col p-4">
              <div className="flex items-center gap-2 text-xs font-medium text-slate-500">
                <span className="rounded bg-slate-100 px-1.5 py-0.5">{row.source}</span>
                <span>→</span>
                <span className="rounded bg-slate-100 px-1.5 py-0.5">{row.target}</span>
                <span className="ml-auto font-mono text-slate-400">{row.match}</span>
              </div>
              <a
                href={`#/product/${row.asin}`}
                className="mt-2 line-clamp-2 text-sm font-semibold text-slate-900 hover:text-emerald-700"
              >
                {row.title}
              </a>
              <div className="mt-3 flex items-center gap-3">
                <span className="text-2xl font-semibold tabular text-slate-900">
                  {row.score == null ? "—" : Math.round(row.score)}
                </span>
                <Badge tone={verdictTone[row.verdict]}>{row.verdict.replace(/_/g, " ").toLowerCase()}</Badge>
                <Badge tone="slate">{row.confidence}</Badge>
              </div>
              <p className="mt-2 text-xs leading-relaxed text-slate-600">{row.note}</p>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

function ZombieMode() {
  const { activeProfile, zombies } = useStore();
  const cmd = buildCommands(activeProfile).find((c) => c.id === "zombies");

  const verdictTone: Record<string, "emerald" | "amber" | "rose"> = {
    "Verified zombie": "emerald",
    "Possibly temporary": "amber",
    "Not a zombie": "rose",
  };

  return (
    <div className="space-y-5">
      <Card className="flex items-start gap-4 p-4">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-slate-100">
          <Ghost className="h-5 w-5 text-slate-600" />
        </div>
        <div className="text-sm text-slate-600">
          <span className="font-semibold text-slate-900">Demand proof.</span> Listings out of stock long-term but
          still strongly reviewed — launch your own improved version on a <em>new</em> listing. The verdict is a
          research signal, not legal or policy clearance.
        </div>
      </Card>

      {cmd && (
        <Card className="p-5">
          <SectionTitle title="Sweep the dead listings" subtitle="Keepa-only; verify each is truly dead after hydration." />
          <CommandBlock command={cmd.command} cost={formatCostLine(cmd)} />
        </Card>
      )}

      <div className="space-y-3">
        {zombies.map((z) => (
          <Card key={z.asin} className="p-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={verdictTone[z.verdict]}>{z.verdict}</Badge>
              <Badge tone="slate">{z.marketplace}</Badge>
              <Badge tone={z.generic ? "violet" : "slate"}>
                {z.generic ? "generic / unbranded" : z.brand}
              </Badge>
              <span className="ml-auto text-lg font-semibold tabular text-slate-900">
                {Math.round(z.score)}
              </span>
            </div>
            <div className="mt-2 text-sm font-semibold text-slate-900">{z.title}</div>
            <div className="mt-1 font-mono text-xs text-slate-400">{z.asin}</div>
            <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-5">
              <ZFig label="Rating" value={`${z.rating.toFixed(1)} ★`} />
              <ZFig label="Reviews" value={z.reviews.toLocaleString()} />
              <ZFig label="Dead" value={`${z.monthsDead} mo`} />
              <ZFig label="In-stock BSR" value={z.inStockBsr.toLocaleString()} />
              <ZFig label="Restock gaps" value={String(z.restockGaps)} />
            </dl>
            <p className="mt-3 text-xs leading-relaxed text-slate-600">{z.reason}</p>
            <div className="mt-3 flex flex-wrap gap-2">
              <LinkButton href={`#/product/${z.asin}`} size="sm" variant="secondary">
                Open in workspace
              </LinkButton>
              <Badge tone="amber">manual trademark + policy check required</Badge>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}

function ZFig({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-slate-500">{label}</dt>
      <dd className="mt-0.5 font-semibold tabular text-slate-800">{value}</dd>
    </div>
  );
}

function HistoryMode() {
  const { scans, activeProfile } = useStore();
  const projection = scanProjection(activeProfile);

  return (
    <div className="space-y-5">
      <Card className="p-5">
        <SectionTitle
          title="Daily Scan history"
          subtitle="Each scan persists its funnel, per-stage cost and finalists to SQLite so a crash resumes from the last stage."
        />
        <CommandBlock
          command={scanCommand(activeProfile)}
          cost={`${projection.totalTokens.toLocaleString()} Keepa tokens · ${usd(projection.totalUsd)} worst case`}
        />
      </Card>

      {scans.length === 0 ? (
        <EmptyState icon={HistoryIcon} title="No scans recorded" body="Run a Daily Scan to populate history." />
      ) : (
        scans.map((s) => (
          <Card key={s.id} className="p-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge
                tone={s.status === "complete" ? "emerald" : s.status === "aborted" ? "amber" : "rose"}
              >
                {s.status}
              </Badge>
              <span className="text-sm font-medium text-slate-800">{fmtDateTime(s.startedAt)}</span>
              <span className="text-xs text-slate-500">{s.marketplaces.join(" · ")}</span>
              <span className="ml-auto text-xs tabular text-slate-500">
                {s.tokens.toLocaleString()} tokens · {usd(s.usd)}
              </span>
            </div>
            <div className="mt-3 grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-3">
              {s.funnel.map((f) => (
                <div key={f.stage} className="flex items-center justify-between gap-3 rounded-lg bg-slate-50 px-3 py-2">
                  <span className="min-w-0 truncate text-slate-600" title={f.stage}>
                    {f.stage}
                  </span>
                  <span className="font-medium tabular text-slate-800">
                    {f.count == null ? "—" : f.count.toLocaleString()}
                  </span>
                </div>
              ))}
            </div>
          </Card>
        ))
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */

function ResultList({
  rows,
  empty,
  caption,
}: {
  rows: Analysed[];
  empty: string;
  caption?: string;
}) {
  const store = useStore();
  if (rows.length === 0) return <EmptyState icon={Sparkles} title="No results" body={empty} />;

  return (
    <div>
      {caption && <p className="mb-3 text-xs text-slate-500">{caption}</p>}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {rows.map((a) => {
          const p = a.product;
          const shortlisted = store.isShortlisted(p.asin);
          return (
            <Card key={p.asin} className="flex flex-col p-4">
              <div className="flex gap-3">
                <ProductThumb emoji={p.emoji} hue={p.hue} />
                <div className="min-w-0 flex-1">
                  <a
                    href={`#/product/${p.asin}`}
                    className="line-clamp-2 text-sm font-semibold leading-snug text-slate-900 hover:text-emerald-700"
                  >
                    {p.title}
                  </a>
                  <div className="mt-1 text-xs text-slate-500">
                    {topCategory(p.category)} · {ageLabel(p.ageDays)} old
                  </div>
                </div>
                <VerdictBadge verdict={a.scored.verdict} />
              </div>

              <div className="mt-3 flex flex-wrap gap-1.5">
                <Badge tone="violet" className="gap-1">
                  <Sparkles className="h-3 w-3" /> emergence {p.emergence}
                </Badge>
                <ConfidenceBadge confidence={a.scored.confidence} />
                {p.salesSource === "amazon" ? (
                  <Badge tone="emerald">amazon monthlySold</Badge>
                ) : (
                  <Badge tone="amber">estimated curve</Badge>
                )}
              </div>

              <dl className="mt-3 grid grid-cols-3 gap-x-3 gap-y-2 text-xs">
                <Fig label="Price" value={usd(p.price)} />
                <Fig label="Sales/mo" value={`${p.monthlySold.toLocaleString()}+`} />
                <Fig label="Margin" value={pct(a.scored.profit.marginPct)} bad={!a.scored.profit.gates.margin} />
              </dl>

              <div className="mt-3">
                <ScoreBar value={a.scored.composite} />
              </div>

              <div className="mt-auto flex items-center gap-2 pt-4">
                <LinkButton href={`#/product/${p.asin}`} size="sm" variant="primary" className="flex-1">
                  Open
                </LinkButton>
                <Button size="sm" onClick={() => store.toggleShortlist(p.asin)} aria-pressed={shortlisted}>
                  {shortlisted ? "Shortlisted" : "Shortlist"}
                </Button>
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}

function Fig({ label, value, bad }: { label: string; value: string; bad?: boolean }) {
  return (
    <div>
      <dt className="text-slate-500">{label}</dt>
      <dd className={cx("mt-0.5 font-semibold tabular", bad ? "text-rose-700" : "text-slate-900")}>{value}</dd>
    </div>
  );
}
