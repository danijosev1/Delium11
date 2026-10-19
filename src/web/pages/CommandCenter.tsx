import React, { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  BookmarkCheck,
  BookmarkPlus,
  CalendarClock,
  Check,
  Coins,
  Inbox,
  Loader2,
  Play,
  RotateCcw,
  Sparkles,
  Wallet,
  X,
} from "lucide-react";
import { useStore } from "../state/store";
import { useAnalysed } from "../lib/analyse";
import type { Analysed } from "../lib/analyse";
import { useLocalState } from "../lib/useLocalState";
import { fittingEnrichLimit, scanCommand, scanProjection } from "../lib/commands";
import { topCategory } from "../lib/categories";
import { EMERGING_CATEGORIES } from "../data/signals";
import { CONFIG } from "../lib/config";
import { fmtDateTime, fmtScore, nextRunAt, pct, relativeFromNow, usd } from "../lib/format";
import {
  Badge,
  Button,
  Card,
  CommandBlock,
  ConfidenceBadge,
  EmptyState,
  LinkButton,
  Modal,
  PageHeader,
  ProductThumb,
  ScoreBar,
  SectionTitle,
  SelectField,
  Stat,
  Toggle,
  VerdictBadge,
  cx,
  scoreTone,
} from "../components/ui";
import type { ScanRecord } from "../types";

const STAGES = [
  "Preflight — token check + cost projection",
  "Sweep — Product Finder across BSR sub-bands",
  "Hydrate — batched, cache-first /product",
  "Normalize + hard kill",
  "Cheap scoring",
  "Competitor sets — SERP + hydration",
  "Rank — sellability",
  "Finalists — reviews + differentiation",
  "Report — scan inbox",
];

type Filters = {
  confidence: string;
  category: string;
  price: string;
  source: string;
  verdict: string;
  onlyFit: boolean;
};
const DEFAULT_FILTERS: Filters = {
  confidence: "all",
  category: "all",
  price: "all",
  source: "all",
  verdict: "all",
  onlyFit: false,
};
type SortKey = "sellability" | "score" | "margin" | "emergence";

export default function CommandCenter() {
  const store = useStore();
  const { activeProfile: profile, scans, keepaTokens, spendThisMonth } = store;
  const analysed = useAnalysed();

  const [rejected, setRejected] = useLocalState<string[]>("delium.rejected.v1", []);
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [sort, setSort] = useState<SortKey>("sellability");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [enrich, setEnrich] = useState(() => fittingEnrichLimit(profile));
  const [stage, setStage] = useState<number | null>(null);
  const [lastRun, setLastRun] = useState<ScanRecord | null>(null);

  useEffect(() => {
    setEnrich(fittingEnrichLimit(profile));
  }, [profile]);

  const projection = scanProjection(profile, { enrichLimit: enrich });
  const overUsd = projection.totalUsd > profile.scanUsdCap;
  const overTokens = projection.totalTokens > profile.scanTokenCap;
  const paceMinutes =
    projection.totalTokens > keepaTokens ? Math.ceil((projection.totalTokens - keepaTokens) / 20) : 0;
  const command = scanCommand(profile, { enrichLimit: enrich });

  // Simulated stage-by-stage progress (the real scan runs detached via the CLI).
  useEffect(() => {
    if (stage == null) return;
    const t = window.setTimeout(() => {
      if (stage + 1 < STAGES.length) {
        setStage(stage + 1);
        return;
      }
      const tokens = Math.round(projection.totalTokens * 0.62);
      const spent =
        Math.round((projection.serpUsd * 0.7 + projection.finalistUsd * 0.5 + projection.llmUsd * 0.35) * 100) /
        100;
      const finalists = Math.min(analysed.length, Math.max(enrich, 3));
      const record: ScanRecord = {
        id: `scan-sim-${Date.now()}`,
        startedAt: new Date().toISOString(),
        status: "complete",
        marketplaces: profile.marketplaces,
        finalists,
        tokens,
        usd: spent,
        funnel: [
          { stage: "Sweep (Product Finder)", count: profile.sweepSize },
          { stage: "Hydrated", count: profile.sweepSize },
          { stage: "After variation merge + hard kill", count: Math.round(profile.sweepSize * 0.48) },
          { stage: "Cheap score survived", count: Math.round(profile.sweepSize * 0.2) },
          { stage: "Competitor sets built", count: profile.competitorSets },
          { stage: "Finalists (differentiated)", count: finalists },
        ],
      };
      store.addScan(record);
      store.setKeepaTokens(Math.max(0, keepaTokens - tokens));
      store.addSpend(spent);
      setLastRun(record);
      setStage(null);
    }, 650);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage]);

  const startScan = () => {
    setConfirmOpen(false);
    setLastRun(null);
    setStage(0);
  };

  const categories = useMemo(
    () => Array.from(new Set(analysed.map((a) => topCategory(a.product.category)))).sort(),
    [analysed]
  );

  const visible = useMemo(() => {
    const list = analysed
      .filter((a) => !rejected.includes(a.product.asin))
      .filter((a) => {
        const p = a.product;
        if (filters.confidence !== "all" && a.scored.confidence !== filters.confidence) return false;
        if (filters.category !== "all" && topCategory(p.category) !== filters.category) return false;
        if (filters.source !== "all" && p.salesSource !== filters.source) return false;
        if (filters.verdict !== "all" && a.scored.verdict !== filters.verdict) return false;
        if (filters.price === "lt25" && p.price >= 25) return false;
        if (filters.price === "25to40" && (p.price < 25 || p.price > 40)) return false;
        if (filters.price === "gt40" && p.price <= 40) return false;
        if (filters.onlyFit && !a.fit.ok) return false;
        return true;
      });
    const key = (a: Analysed) =>
      sort === "score"
        ? a.scored.composite ?? -1
        : sort === "margin"
          ? a.scored.profit.marginPct
          : sort === "emergence"
            ? a.product.emergence
            : a.sellability.score ?? -1;
    return [...list].sort((x, y) => key(y) - key(x));
  }, [analysed, rejected, filters, sort]);

  const rejectedHere = rejected.filter((r) => analysed.some((a) => a.product.asin === r));
  const inboxCount = analysed.length - rejectedHere.length;
  const g1Pass = analysed.filter((a) => a.scored.profit.allGatesPass).length;
  const lastScan = scans[0];
  const next = nextRunAt(["06:00"]);
  const filtersActive = JSON.stringify(filters) !== JSON.stringify(DEFAULT_FILTERS);

  return (
    <div className="mx-auto max-w-7xl space-y-6 p-4 md:p-8">
      <PageHeader
        title="Command Center"
        subtitle="Your scan inbox, ranked by sellability against the active Research Profile. Every number is computed by the deterministic engine; nothing here spends money."
        actions={
          <Button variant="primary" onClick={() => setConfirmOpen(true)} disabled={stage != null}>
            {stage != null ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            Run Daily Scan
          </Button>
        }
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Stat
          icon={RotateCcw}
          label="Last scan"
          value={lastScan ? fmtDateTime(lastScan.startedAt) : "never"}
          hint={lastScan ? `${lastScan.status} · ${lastScan.finalists} finalists` : "—"}
        />
        <Stat icon={Inbox} label="Scan inbox" value={inboxCount} hint={`${rejectedHere.length} rejected`} />
        <Stat icon={Coins} label="Keepa tokens" value={keepaTokens.toLocaleString()} hint="refills ~20/min" />
        <Stat
          icon={CalendarClock}
          label="Next scheduled run"
          value={next.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })}
          hint={relativeFromNow(next)}
        />
        <Stat
          icon={Wallet}
          label="Spend this month"
          value={usd(spendThisMonth)}
          hint={`alarm at ${usd(CONFIG.budgets.monthly_spend_alarm_usd, 0)}`}
        />
      </div>

      {stage != null && <ScanProgress stage={stage} />}

      {lastRun && (
        <Card className="flex flex-wrap items-center gap-3 border-emerald-200 bg-emerald-50/60 p-4">
          <Check className="h-5 w-5 text-emerald-600" />
          <div className="min-w-0 flex-1 text-sm text-emerald-900">
            <span className="font-semibold">Simulated scan complete.</span> {lastRun.finalists} finalists ·{" "}
            {lastRun.tokens.toLocaleString()} tokens · {usd(lastRun.usd)}. Run the command below to do it for real.
          </div>
          <Button size="sm" variant="ghost" onClick={() => setLastRun(null)} aria-label="Dismiss">
            <X className="h-4 w-4" />
          </Button>
        </Card>
      )}

      {g1Pass === 0 && (
        <Card className="flex gap-3 border-amber-200 bg-amber-50/70 p-4">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
          <div className="text-sm text-amber-900">
            <div className="font-semibold">No inbox product clears the G1 profit gate at your current assumptions.</div>
            <p className="mt-1 text-amber-800">
              With COGS at{" "}
              {profile.cogsMode === "pct" ? pct(profile.cogsValue) : usd(profile.cogsValue)} and TACOS at{" "}
              {pct(CONFIG.assumptions.tacos_pct)}, referral + PPC alone take ~30% of price, so a{" "}
              {pct(CONFIG.gates.min_margin)} net margin needs a high price or a real supplier quote. Open a product's{" "}
              <strong>Profit</strong> tab to see what price or COGS clears it.
            </p>
          </div>
        </Card>
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="p-5 lg:col-span-2">
          <SectionTitle
            title="Next Daily Scan"
            subtitle={`Light mode · enrich limit ${enrich} · caps ${profile.scanTokenCap.toLocaleString()} tokens / ${usd(profile.scanUsdCap)}`}
          />
          <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <MiniFigure label="Sweep" value={`${projection.sweepTokens} tok`} />
            <MiniFigure label="Hydrate" value={`${projection.hydrateTokens} tok`} />
            <MiniFigure label="Competitor sets" value={`${projection.competitorTokens} tok + ${usd(projection.serpUsd)}`} />
            <MiniFigure label="Finalists" value={`${usd(projection.finalistUsd + projection.llmUsd)}`} />
          </div>
          <div className="mt-4">
            <CommandBlock
              command={command}
              cost={`${projection.totalTokens.toLocaleString()} Keepa tokens · ${usd(projection.totalUsd)} worst case (typically ~$1–2 with a warm cache)`}
            />
          </div>
        </Card>

        <Card className="p-5">
          <SectionTitle title="Emerging categories" subtitle="From the latest scan's momentum signal" />
          <ul className="space-y-3">
            {EMERGING_CATEGORIES.map((c) => (
              <li key={c.name}>
                <div className="flex items-center justify-between gap-2 text-sm">
                  <span className="truncate font-medium text-slate-800" title={c.name}>
                    {c.name.split(">").slice(-1)[0].trim()}
                  </span>
                  <Badge tone="violet" className="gap-1">
                    <Sparkles className="h-3 w-3" /> {c.emerging}
                  </Badge>
                </div>
                <ScoreBar value={c.momentum} className="mt-1.5" />
                <p className="mt-1 text-[11px] text-slate-500">{c.note}</p>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <section>
        <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-slate-900">Scan Inbox</h2>
            <p className="text-xs text-slate-500">
              Separate from your shortlist — promote what's worth keeping. {visible.length} shown.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {rejectedHere.length > 0 && (
              <Button size="sm" variant="ghost" onClick={() => setRejected([])}>
                <RotateCcw className="h-3.5 w-3.5" /> Restore {rejectedHere.length} rejected
              </Button>
            )}
            <SelectField
              value={sort}
              onChange={(v) => setSort(v as SortKey)}
              options={[
                { value: "sellability", label: "Sort: sellability" },
                { value: "score", label: "Sort: opportunity score" },
                { value: "margin", label: "Sort: margin" },
                { value: "emergence", label: "Sort: emergence" },
              ]}
            />
          </div>
        </div>

        <Card className="mb-4 flex flex-wrap items-end gap-3 p-3">
          <SelectField
            label="Confidence"
            value={filters.confidence}
            onChange={(v) => setFilters({ ...filters, confidence: v })}
            options={[
              { value: "all", label: "Any" },
              { value: "high", label: "High" },
              { value: "medium", label: "Medium" },
              { value: "low", label: "Low" },
            ]}
          />
          <SelectField
            label="Category"
            value={filters.category}
            onChange={(v) => setFilters({ ...filters, category: v })}
            options={[{ value: "all", label: "All" }, ...categories.map((c) => ({ value: c, label: c }))]}
          />
          <SelectField
            label="Price band"
            value={filters.price}
            onChange={(v) => setFilters({ ...filters, price: v })}
            options={[
              { value: "all", label: "Any" },
              { value: "lt25", label: "Under $25" },
              { value: "25to40", label: "$25–$40" },
              { value: "gt40", label: "Over $40" },
            ]}
          />
          <SelectField
            label="Sales source"
            value={filters.source}
            onChange={(v) => setFilters({ ...filters, source: v })}
            options={[
              { value: "all", label: "Any" },
              { value: "amazon", label: "Amazon (Keepa monthlySold)" },
              { value: "estimated", label: "Estimated (curve)" },
            ]}
          />
          <SelectField
            label="Verdict"
            value={filters.verdict}
            onChange={(v) => setFilters({ ...filters, verdict: v })}
            options={[
              { value: "all", label: "Any" },
              { value: "BUY", label: "BUY" },
              { value: "TEST", label: "TEST" },
              { value: "AVOID", label: "AVOID" },
            ]}
          />
          <div className="pb-1.5">
            <Toggle
              checked={filters.onlyFit}
              onChange={(v) => setFilters({ ...filters, onlyFit: v })}
              label="Profile matches only"
            />
          </div>
          {filtersActive && (
            <Button size="sm" variant="ghost" onClick={() => setFilters(DEFAULT_FILTERS)} className="mb-0.5">
              Clear filters
            </Button>
          )}
        </Card>

        {visible.length === 0 ? (
          <EmptyState
            icon={Inbox}
            title="Nothing matches"
            body={
              inboxCount === 0
                ? "You've rejected everything in the inbox. Restore them or run the next Daily Scan."
                : "No inbox product matches these filters."
            }
            action={
              <Button
                onClick={() => {
                  setFilters(DEFAULT_FILTERS);
                  if (inboxCount === 0) setRejected([]);
                }}
              >
                Reset
              </Button>
            }
          />
        ) : (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {visible.map((a) => (
              <InboxCard
                key={a.product.asin}
                a={a}
                shortlisted={store.isShortlisted(a.product.asin)}
                onShortlist={() => store.toggleShortlist(a.product.asin)}
                onReject={() => setRejected([...rejected, a.product.asin])}
              />
            ))}
          </div>
        )}
      </section>

      <Modal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title="Run Daily Scan"
        footer={
          <>
            <Button onClick={() => setConfirmOpen(false)}>Cancel</Button>
            <Button variant="primary" onClick={startScan} disabled={overUsd || overTokens}>
              <Play className="h-4 w-4" /> Simulate run
            </Button>
          </>
        }
      >
        <div className="space-y-4 text-sm">
          <p className="text-slate-600">
            This preview can't reach Keepa or DataForSEO, so the button runs a <strong>simulation</strong> of the
            stages. Copy the command to run the real scan in your terminal.
          </p>
          <table className="w-full text-sm">
            <tbody className="divide-y divide-slate-100">
              <CostRow label="Sweep (Product Finder)" value={`${projection.sweepTokens} tokens`} />
              <CostRow label="Hydrate (batched /product)" value={`${projection.hydrateTokens} tokens`} />
              <CostRow
                label={`Competitor sets (${profile.competitorSets})`}
                value={`${projection.competitorTokens} tokens + ${usd(projection.serpUsd)}`}
              />
              <CostRow
                label={`Finalists enriched (${enrich})`}
                value={`${usd(projection.finalistUsd)} data + ${usd(projection.llmUsd)} LLM`}
              />
              <CostRow
                label="Total worst case"
                value={`${projection.totalTokens.toLocaleString()} tokens · ${usd(projection.totalUsd)}`}
                strong
              />
            </tbody>
          </table>
          <div className="flex items-center gap-3">
            <span className="text-xs font-medium text-slate-600">Enrich limit</span>
            <input
              type="range"
              min={1}
              max={10}
              value={enrich}
              onChange={(e) => setEnrich(Number(e.target.value))}
              className="flex-1 accent-emerald-600"
              aria-label="Enrich limit"
            />
            <span className="w-6 text-right text-sm font-medium tabular">{enrich}</span>
          </div>
          {(overUsd || overTokens) && (
            <div className="rounded-lg bg-rose-50 p-3 text-xs text-rose-800">
              The projection exceeds your profile cap ({overUsd ? `${usd(profile.scanUsdCap)} USD` : ""}
              {overUsd && overTokens ? " and " : ""}
              {overTokens ? `${profile.scanTokenCap} tokens` : ""}). The real scan aborts in preflight before spending —
              lower the enrich limit or raise the cap in Settings.
            </div>
          )}
          {paceMinutes > 0 && (
            <div className="rounded-lg bg-amber-50 p-3 text-xs text-amber-800">
              Will pace: ~{paceMinutes} min waiting for Keepa refills. Your balance isn't a hard limit.
            </div>
          )}
          <CommandBlock command={command} />
        </div>
      </Modal>
    </div>
  );
}

function MiniFigure({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-slate-50 px-3 py-2">
      <div className="text-[11px] text-slate-500">{label}</div>
      <div className="mt-0.5 text-sm font-medium tabular text-slate-800">{value}</div>
    </div>
  );
}

function CostRow({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return (
    <tr>
      <td className={cx("py-1.5 pr-3", strong ? "font-semibold text-slate-900" : "text-slate-600")}>{label}</td>
      <td className={cx("py-1.5 text-right tabular", strong ? "font-semibold text-slate-900" : "text-slate-700")}>
        {value}
      </td>
    </tr>
  );
}

function ScanProgress({ stage }: { stage: number }) {
  return (
    <Card className="p-5">
      <SectionTitle title="Daily Scan running (simulated)" subtitle={`Stage ${stage + 1} of ${STAGES.length}`} />
      <div className="mb-4 h-2 overflow-hidden rounded-full bg-slate-100">
        <div
          className="h-full rounded-full bg-emerald-500 transition-all"
          style={{ width: `${((stage + 1) / STAGES.length) * 100}%` }}
        />
      </div>
      <ol className="grid gap-1.5 text-sm sm:grid-cols-2">
        {STAGES.map((s, i) => (
          <li key={s} className="flex items-center gap-2">
            {i < stage ? (
              <Check className="h-4 w-4 text-emerald-600" />
            ) : i === stage ? (
              <Loader2 className="h-4 w-4 animate-spin text-emerald-600" />
            ) : (
              <span className="mx-1 h-2 w-2 rounded-full bg-slate-300" />
            )}
            <span className={i <= stage ? "text-slate-800" : "text-slate-400"}>{s}</span>
          </li>
        ))}
      </ol>
    </Card>
  );
}

function InboxCard({
  a,
  shortlisted,
  onShortlist,
  onReject,
}: {
  a: Analysed;
  shortlisted: boolean;
  onShortlist: () => void;
  onReject: () => void;
}) {
  const p = a.product;
  const s = a.scored;
  return (
    <Card className="flex flex-col p-4">
      <div className="flex gap-3">
        <ProductThumb emoji={p.emoji} hue={p.hue} />
        <div className="min-w-0 flex-1">
          <a
            href={`#/product/${p.asin}`}
            className="line-clamp-2 text-sm font-semibold leading-snug text-slate-900 hover:text-emerald-700"
          >
            {p.title}
          </a>
          <div className="mt-1 flex flex-wrap items-center gap-x-1.5 text-xs text-slate-500">
            <span className="font-mono">{p.asin}</span>
            <span>·</span>
            <span>{topCategory(p.category)}</span>
          </div>
        </div>
        <VerdictBadge verdict={s.verdict} />
      </div>

      <dl className="mt-4 grid grid-cols-3 gap-x-3 gap-y-2.5 text-xs">
        <Fig label="Price" value={usd(p.price)} />
        <Fig
          label="Sales / mo"
          value={`${p.monthlySold.toLocaleString()}+`}
          note={p.salesSource === "amazon" ? "amazon" : "estimated"}
        />
        <Fig label="Profit / unit" value={usd(s.profit.marginUnit)} />
        <Fig label="Margin" value={pct(s.profit.marginPct)} bad={!s.profit.gates.margin} />
        <Fig label="Opportunity" value={fmtScore(s.composite)} tone={scoreTone(s.composite)} />
        <Fig label="Sellability" value={fmtScore(a.sellability.score)} tone={scoreTone(a.sellability.score)} />
      </dl>

      <p className="mt-3 text-xs leading-relaxed text-slate-600">{p.why}</p>

      <div className="mt-3 flex flex-wrap gap-1.5">
        <ConfidenceBadge confidence={s.confidence} />
        {a.fit.preferred && <Badge tone="emerald">preferred category</Badge>}
        {!a.fit.ok && (
          <Badge tone="amber" title={a.fit.misses.join("; ")}>
            outside profile ({a.fit.misses.length})
          </Badge>
        )}
      </div>

      <div className="mt-auto flex items-center gap-2 pt-4">
        <LinkButton href={`#/product/${p.asin}`} size="sm" variant="primary" className="flex-1">
          Open
        </LinkButton>
        <Button size="sm" onClick={onShortlist} aria-pressed={shortlisted}>
          {shortlisted ? (
            <BookmarkCheck className="h-3.5 w-3.5 text-emerald-600" />
          ) : (
            <BookmarkPlus className="h-3.5 w-3.5" />
          )}
          {shortlisted ? "Shortlisted" : "Shortlist"}
        </Button>
        <Button size="sm" variant="ghost" onClick={onReject} aria-label={`Reject ${p.asin}`}>
          <X className="h-3.5 w-3.5" /> Reject
        </Button>
      </div>
    </Card>
  );
}

function Fig({
  label,
  value,
  note,
  tone,
  bad,
}: {
  label: string;
  value: string;
  note?: string;
  tone?: ReturnType<typeof scoreTone>;
  bad?: boolean;
}) {
  const toneText: Record<string, string> = {
    emerald: "text-emerald-700",
    sky: "text-sky-700",
    amber: "text-amber-700",
    rose: "text-rose-700",
    slate: "text-slate-500",
    violet: "text-violet-700",
  };
  return (
    <div>
      <dt className="text-slate-500">{label}</dt>
      <dd
        className={cx(
          "mt-0.5 font-semibold tabular",
          bad ? "text-rose-700" : tone ? toneText[tone] : "text-slate-900"
        )}
      >
        {value}
        {note && <span className="ml-1 text-[10px] font-normal text-slate-400">{note}</span>}
      </dd>
    </div>
  );
}
