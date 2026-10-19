import React, { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Bookmark,
  BookmarkCheck,
  BookmarkPlus,
  Calculator,
  Coins,
  ExternalLink,
  Ghost,
  Info,
  Layers,
  MessageSquare,
  Package,
  Percent,
  RefreshCw,
  Search as SearchIcon,
  Shield,
  TrendingDown,
  TrendingUp,
  Users,
} from "lucide-react";
import { useStore } from "../state/store";
import { analyse, profitOptsFor } from "../lib/analyse";
import { formatUsd } from "../lib/engine";
import {
  ESTABLISHED_BRANDS,
  PILLAR_KEYS,
  PILLAR_LABELS,
  computeProfit,
  computeSellability,
  launchability,
  scoreProduct,
  stressedProfit,
  type ProfitOptions,
  type Scored,
} from "../lib/engine";
import { curveFor, curveUnitsForBsr, monthlySoldBucket } from "../lib/curves";
import { topCategory } from "../lib/categories";
import { buildCommands } from "../lib/commands";
import { CONFIG } from "../lib/config";
import { ageLabel, fmtScore, pct, usd } from "../lib/format";
import type { Product } from "../types";
import {
  Badge,
  Button,
  Card,
  ConfidenceBadge,
  EmptyState,
  LinkButton,
  LineChart,
  NumField,
  PageHeader,
  ProductThumb,
  ScoreRing,
  SectionTitle,
  SelectField,
  Sparkline,
  Stat,
  Tabs,
  Toggle,
  VerdictBadge,
  cx,
  scoreTone,
} from "../components/ui";
import { Find } from "./_stub";

export default function Workspace({ asin }: { asin?: string }) {
  const store = useStore();

  if (!asin) {
    return <Picker />;
  }
  const product = store.product(asin);
  if (!product) {
    return (
      <div className="mx-auto max-w-3xl p-8">
        <EmptyState
          icon={Package}
          title={`No stored product for ${asin}`}
          body="The preview only carries the eight demo products. Pick one from the list, or run a real validate in your terminal."
          action={
            <LinkButton href="#/product" variant="primary">
              Back to the picker
            </LinkButton>
          }
        />
      </div>
    );
  }
  return <WorkspaceBody key={product.asin} product={product} />;
}

function Picker() {
  const { products } = useStore();
  return (
    <div className="mx-auto max-w-7xl space-y-6 p-4 md:p-8">
      <PageHeader
        title="Product workspace"
        subtitle="Everything about ONE product across seven tabs. Pick a product below, or open one from the Scan Inbox or Find."
      />
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {products.map((p) => {
          const a = analyse(p, useStore().activeProfile);
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
                  <div className="mt-1 text-xs text-slate-500">{topCategory(p.category)}</div>
                </div>
                <VerdictBadge verdict={a.scored.verdict} />
              </div>
              <div className="mt-3 flex items-center gap-3">
                <ScoreRing value={a.scored.composite} size={56} />
                <div className="text-xs text-slate-500">
                  Opportunity score
                  <div className="mt-0.5 font-mono text-slate-400">{p.asin}</div>
                </div>
              </div>
              <p className="mt-3 line-clamp-2 text-xs leading-relaxed text-slate-600">{p.why}</p>
              <div className="mt-3 flex items-center gap-2">
                <LinkButton href={`#/product/${p.asin}`} size="sm" variant="primary" className="flex-1">
                  Open →
                </LinkButton>
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */

type TabId =
  | "overview"
  | "sales"
  | "keywords"
  | "competitors"
  | "reviews"
  | "profit"
  | "risk"
  | "zombie";

const TABS: { id: TabId; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "sales", label: "Sales & momentum" },
  { id: "keywords", label: "Keywords" },
  { id: "competitors", label: "Competitors" },
  { id: "reviews", label: "Reviews" },
  { id: "profit", label: "Profit" },
  { id: "risk", label: "Risk & verdict" },
  { id: "zombie", label: "Zombie check" },
];

function WorkspaceBody({ product }: { product: Product }) {
  const store = useStore();
  const profile = store.activeProfile;
  const [tab, setTab] = useState<TabId>("overview");

  const [opts, setOpts] = useState<ProfitOptions>(() => profitOptsFor(profile));
  const [quoted, setQuoted] = useState(false);
  useEffect(() => {
    setOpts(profitOptsFor(profile));
    setQuoted(false);
  }, [profile]);

  const scored: Scored = useMemo(
    () => scoreProduct(product, quoted ? { ...opts, quotedCogs: true } : opts),
    [product, opts, quoted]
  );
  const stressed = useMemo(
    () => stressedProfit(product, quoted ? { ...opts, quotedCogs: true } : opts),
    [product, opts, quoted]
  );
  const sellability = useMemo(
    () => computeSellability(product, scored.composite, opts.price ?? product.price),
    [product, scored.composite, opts.price]
  );
  const launch = useMemo(() => launchability(product, opts.price ?? product.price), [product, opts.price]);

  const shortlisted = store.isShortlisted(product.asin);
  const entry = store.shortlistEntry(product.asin);
  const entryNotes = entry?.notes ?? "";

  const cmdValidate = buildCommands(profile, product).find((c) => c.id === "validate");

  return (
    <div className="mx-auto max-w-7xl space-y-5 p-4 md:p-8">
      {/* header */}
      <div className="flex flex-wrap items-start gap-4">
        <ProductThumb emoji={product.emoji} hue={product.hue} size="lg" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-xs text-slate-500">
            <span className="font-mono">{product.asin}</span>
            <span>·</span>
            <Badge tone="slate">{product.marketplace}</Badge>
            <Badge tone="slate">{topCategory(product.category)}</Badge>
          </div>
          <h1 className="mt-1 text-lg font-semibold leading-snug text-slate-900 md:text-xl">
            {product.title}
          </h1>
          <div className="mt-1 text-sm text-slate-500">
            {product.brand} · {ageLabel(product.ageDays)} on listing · {product.category}
          </div>
        </div>
        <div className="flex flex-col items-end gap-2">
          <VerdictBadge verdict={scored.verdict} />
          <ConfidenceBadge confidence={scored.confidence} />
          <div className="flex gap-2">
            <Button size="sm" onClick={() => store.toggleShortlist(product.asin)} aria-pressed={shortlisted}>
              {shortlisted ? (
                <BookmarkCheck className="h-3.5 w-3.5 text-emerald-600" />
              ) : (
                <BookmarkPlus className="h-3.5 w-3.5" />
              )}
              {shortlisted ? "Shortlisted" : "Shortlist"}
            </Button>
          </div>
        </div>
      </div>

      <Card className="border-slate-200 bg-slate-50/60 p-3 text-xs text-slate-600">
        <div className="flex items-start gap-2">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" />
          <span>
            <strong className="text-slate-800">Deep dive</strong> fetches whatever is missing — competitors,
            keywords, reviews, fees — after showing a combined cost/token estimate. It never runs inside this
            preview; copy the command:
          </span>
        </div>
        {cmdValidate && (
          <div className="mt-2">
            <code className="block select-all break-all rounded bg-slate-900 px-3 py-2 font-mono text-[12px] text-emerald-200">
              <span className="select-none text-slate-500">$ </span>
              {cmdValidate.command}
            </code>
          </div>
        )}
      </Card>

      <Tabs tabs={TABS.map((t) => ({ id: t.id, label: t.label }))} value={tab} onChange={(v) => setTab(v as TabId)} />

      {tab === "overview" && <Overview product={product} scored={scored} sellability={sellability} />}
      {tab === "sales" && <Sales product={product} />}
      {tab === "keywords" && <Keywords product={product} />}
      {tab === "competitors" && <Competitors product={product} />}
      {tab === "reviews" && <Reviews product={product} />}
      {tab === "profit" && (
        <Profit
          product={product}
          opts={opts}
          setOpts={setOpts}
          quoted={quoted}
          setQuoted={setQuoted}
        />
      )}
      {tab === "risk" && <Risk product={product} scored={scored} stressed={stressed} />}
      {tab === "zombie" && <ZombieCheck product={product} />}

      {shortlisted && entry && (
        <Card className="p-4">
          <SectionTitle title="Shortlist notes" subtitle={`Status: ${entry.status}`} />
          <div className="flex flex-wrap items-center gap-2">
            <SelectField
              value={entry.status}
              onChange={(v) => store.setShortlistStatus(product.asin, v as typeof entry.status)}
              options={[
                { value: "shortlist", label: "Shortlist" },
                { value: "watching", label: "Watching" },
                { value: "validated", label: "Validated" },
                { value: "rejected", label: "Rejected" },
              ]}
            />
            <Button size="sm" variant="danger" onClick={() => store.removeFromShortlist(product.asin)}>
              Remove
            </Button>
          </div>
          <textarea
            value={entryNotes}
            onChange={(e) => store.setShortlistNotes(product.asin, e.target.value)}
            rows={3}
            placeholder="Notes — supplier conversations, sample results, what would change my mind…"
            className="mt-3 w-full resize-y rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/20"
          />
        </Card>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */

function Overview({
  product,
  scored,
  sellability,
}: {
  product: Product;
  scored: Scored;
  sellability: ReturnType<typeof computeSellability>;
}) {
  return (
    <div className="space-y-5">
      <Card className="p-5">
        <div className="flex flex-wrap items-center gap-6">
          <ScoreRing value={scored.composite} size={92} label="Opportunity" />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold text-slate-900">{scored.verdictBasis}</div>
            <p className="mt-1 max-w-2xl text-sm leading-relaxed text-slate-600">{product.why}</p>
            <div className="mt-3 flex flex-wrap gap-1.5">
              {product.salesSource === "amazon" ? (
                <Badge tone="emerald">amazon monthlySold</Badge>
              ) : (
                <Badge tone="amber">estimated (illustrative curve)</Badge>
              )}
              <ConfidenceBadge confidence={scored.confidence} />
              <Badge tone={scored.profit.allGatesPass ? "emerald" : "rose"}>
                {scored.profit.allGatesPass ? "G1 profit gates pass" : "G1 profit gates fail"}
              </Badge>
            </div>
          </div>
        </div>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="p-5">
          <SectionTitle title="Pillar breakdown" subtitle="Score = Σ weight × sub-score, from config [score_weights]" />
          <ul className="space-y-3">
            {PILLAR_KEYS.map((k) => {
              const v = scored.pillars[k];
              const w = CONFIG.score_weights[k];
              return (
                <li key={k}>
                  <div className="flex items-center justify-between text-sm">
                    <span className="font-medium text-slate-700">{PILLAR_LABELS[k]}</span>
                    <span className="tabular text-slate-500">
                      {v == null ? "unknown" : Math.round(v)} <span className="text-slate-400">· w {w}</span>
                    </span>
                  </div>
                  <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-slate-100">
                    <div
                      className={cx("h-full rounded-full", `bg-${scoreTone(v)}-500`)}
                      style={{ width: `${v == null ? 0 : Math.min(100, v)}%` }}
                    />
                  </div>
                </li>
              );
            })}
          </ul>
          <p className="mt-3 text-[11px] text-slate-500">
            An absent pillar is excluded from the composite — <em>unknown</em>, never a 0 that would drag the score
            down.
          </p>
        </Card>

        <Card className="p-5">
          <SectionTitle title="Sellability" subtitle="Opportunity + the parts no pillar can see" />
          <div className="flex items-center gap-4">
            <ScoreRing value={sellability.score} size={72} label="Sellability" />
            <div className="text-sm text-slate-600">{sellability.reason}</div>
          </div>
          <ul className="mt-4 space-y-2 text-xs">
            {sellability.components.map((c) => (
              <li key={c.label} className="flex items-center justify-between gap-3">
                <span className="min-w-0 truncate text-slate-600">{c.label}</span>
                <span className="tabular font-medium text-slate-800">
                  {c.value == null ? "—" : Math.round(c.value)} <span className="text-slate-400">·{c.weight}</span>
                </span>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <Card className="p-5">
        <SectionTitle title="Gate results" subtitle="A high score never buys its way past a gate" />
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {scored.gates.map((g) => (
            <div
              key={g.id}
              className={cx(
                "rounded-lg border px-3 py-2 text-xs",
                g.pass ? "border-emerald-200 bg-emerald-50/60" : "border-slate-200 bg-slate-50"
              )}
            >
              <div className="flex items-center gap-2">
                <span className="font-mono font-semibold text-slate-700">{g.id}</span>
                <span className={cx("font-medium", g.pass ? "text-emerald-700" : "text-rose-700")}>
                  {g.pass ? "PASS" : g.id === "G5" ? "PENDING" : "FAIL"}
                </span>
              </div>
              <div className="mt-0.5 font-medium text-slate-800">{g.label}</div>
              <div className="mt-0.5 text-slate-500">{g.detail}</div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function Sales({ product }: { product: Product }) {
  const bucket = monthlySoldBucket(product.monthlySold);
  const curve = curveFor(product.category);
  const curveUnits = curveUnitsForBsr(product.bsr, product.category);
  const bsrInverted = product.bsrHistory.map((b) => b);
  const soldLabel = `${product.monthlySold}+ / mo`;

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat
          label="Monthly sold"
          value={soldLabel}
          hint={
            product.salesSource === "amazon"
              ? `amazon bucket ${bucket.low}${bucket.high ? `–${bucket.high}` : "+"}`
              : "estimated from the BSR curve"
          }
        />
        <Stat label="Current BSR" value={product.bsr.toLocaleString()} hint={`peak ${Math.min(...product.bsrHistory).toLocaleString()}`} />
        <Stat label="Reviews / mo" value={product.reviewsPerMonth} hint={`${product.reviews.toLocaleString()} total`} />
        <Stat label="Listing age" value={ageLabel(product.ageDays)} hint={`first tracked ${product.ageDays} d ago`} />
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="p-5">
          <SectionTitle title="BSR history (90 d)" subtitle="Improving rank = falling line — theil-sen slope is the demand trend." />
          <LineChart
            data={bsrInverted}
            invert
            tone="sky"
            format={(n) => Math.round(n).toLocaleString()}
            startLabel="90 d ago"
            endLabel="today"
          />
          <p className="mt-2 text-xs text-slate-500">
            Median top-10 BSR slope feeds <code className="font-mono">D3</code>. A cluster whose incumbents are all
            improving is a growing tide.
          </p>
        </Card>

        <Card className="p-5">
          <SectionTitle title="Unit estimate vs source" subtitle="Priority: Keepa monthlySold → rank-drop → illustrative curve." />
          <ul className="space-y-3 text-sm">
            <li className="flex items-center justify-between gap-3 rounded-lg bg-emerald-50/70 px-3 py-2">
              <div>
                <div className="font-medium text-emerald-900">Keepa `monthlySold`</div>
                <div className="text-xs text-emerald-700">Amazon's own bucketed figure, HIGH confidence</div>
              </div>
              <div className="text-right">
                <div className="font-semibold tabular text-emerald-800">{soldLabel}</div>
                <div className="text-xs text-emerald-700">{product.salesSource === "amazon" ? "in use" : "not reported"}</div>
              </div>
            </li>
            <li className="flex items-center justify-between gap-3 rounded-lg bg-slate-50 px-3 py-2">
              <div>
                <div className="font-medium text-slate-700">Illustrative BSR curve</div>
                <div className="text-xs text-slate-500">
                  {curve.known ? `category curve: ${curve.key}` : "default curve — category_known = false"}
                </div>
              </div>
              <div className="text-right">
                <div className="font-semibold tabular text-slate-800">{curveUnits.toLocaleString()}</div>
                <div className="text-xs text-slate-500">{product.salesSource === "estimated" ? "in use" : "fallback"}</div>
              </div>
            </li>
          </ul>
          <p className="mt-3 text-[11px] text-slate-500">
            ⚠️ Curve values are ILLUSTRATIVE until <code className="font-mono">delium calibrate</code> reconciles
            them against real Keepa data.
          </p>
        </Card>
      </div>

      <Card className="p-5">
        <SectionTitle title="Momentum signals" subtitle="What the demand pillar already prices, plus the emergence signal" />
        <div className="grid gap-4 sm:grid-cols-3">
          <MiniChart label="Rank trend (inverted)" data={bsrInverted} invert tone="emerald" />
          <MiniChart label="Sales trend" data={product.soldHistory} tone="sky" />
          <MiniChart label="Review accrual" data={monthlySeries(product.reviewsPerMonth)} tone="violet" />
        </div>
      </Card>
    </div>
  );
}

function monthlySeries(perMonth: number): number[] {
  const out: number[] = [];
  for (let i = 0; i < 13; i++) out.push(Math.max(1, Math.round(perMonth * (0.6 + i * 0.05))));
  return out;
}

function MiniChart({
  label,
  data,
  tone,
  invert,
}: {
  label: string;
  data: number[];
  tone: "emerald" | "sky" | "violet";
  invert?: boolean;
}) {
  return (
    <div className="rounded-lg bg-slate-50 p-3">
      <div className="text-xs font-medium text-slate-600">{label}</div>
      <div className="mt-2">
        <LineChart data={data} height={90} tone={tone} invert={invert} format={(n) => Math.round(n).toLocaleString()} />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function Keywords({ product }: { product: Product }) {
  const total = product.keywords.reduce((a, k) => a + k.volume, 0);
  const sorted = [...product.keywords].sort((a, b) => b.volume - a.volume);
  const top = sorted[0];
  const topShare = top ? top.volume / total : 0;
  const concentrationFlag = topShare > 0.6;

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Cluster phrases" value={product.keywords.length} hint="≥5 volumed phrases = full demand data" />
        <Stat label="Cluster volume" value={total.toLocaleString()} hint="dedupe by phrase containment (30% weight)" />
        <Stat label="Top phrase" value={top ? top.phrase : "—"} hint={top ? `${top.volume.toLocaleString()} / mo` : ""} />
        <Stat
          label="Top share"
          value={pct(topShare)}
          hint={concentrationFlag ? "> 60% → K15 keyword-concentration risk" : "spread across the cluster"}
        />
      </div>

      {concentrationFlag && (
        <Card className="flex gap-3 border-amber-200 bg-amber-50/70 p-4">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
          <p className="text-sm text-amber-900">
            Over 60% of the cluster volume sits in a single phrase — the risk engine applies a{" "}
            <code className="font-mono">keyword_concentration</code> deduction of −
            {CONFIG.pillars_placeholder ?? 15}.
          </p>
        </Card>
      )}

      <Card className="overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs font-medium text-slate-500">
            <tr>
              <th className="px-4 py-2.5">Phrase</th>
              <th className="px-4 py-2.5 text-right">Monthly volume</th>
              <th className="px-4 py-2.5 text-right">Share</th>
              <th className="px-4 py-2.5" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {sorted.map((k) => (
              <tr key={k.phrase}>
                <td className="px-4 py-2.5 font-medium text-slate-800">{k.phrase}</td>
                <td className="px-4 py-2.5 text-right tabular text-slate-700">{k.volume.toLocaleString()}</td>
                <td className="px-4 py-2.5 text-right tabular text-slate-500">{pct(k.volume / total)}</td>
                <td className="px-4 py-2.5">
                  <div className="h-1.5 w-24 overflow-hidden rounded-full bg-slate-100 sm:w-40">
                    <div className="h-full rounded-full bg-sky-500" style={{ width: `${(k.volume / (top?.volume || 1)) * 100}%` }} />
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function Competitors({ product }: { product: Product }) {
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Top-10 set" value={product.competitors.length} hint="frozen per run — competitor_sets row" />
        <Stat
          label="Median reviews"
          value={median(product.competitors.map((c) => c.reviews)).toLocaleString()}
          hint="review moat"
        />
        <Stat
          label="Beatable slots (<150)"
          value={product.competitors.filter((c) => c.reviews < 150).length}
          hint="proof the algorithm will seat newcomers"
        />
        <Stat
          label="Median price"
          value={usd(median(product.competitors.map((c) => c.price)))}
          hint={`market ${usd(product.price)}`}
        />
      </div>

      <Card className="overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs font-medium text-slate-500">
            <tr>
              <th className="px-4 py-2.5">Listing</th>
              <th className="px-4 py-2.5 text-right">Price</th>
              <th className="px-4 py-2.5 text-right">Reviews</th>
              <th className="px-4 py-2.5 text-right">Rating</th>
              <th className="px-4 py-2.5 text-right">Age</th>
              <th className="px-4 py-2.5 text-right">Images</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {product.competitors.map((c) => {
              const isBig = ESTABLISHED_BRANDS.includes(c.brand.toLowerCase());
              const weak = c.reviews < 150;
              return (
                <tr key={c.asin} className="hover:bg-slate-50/60">
                  <td className="px-4 py-2.5">
                    <div className="flex items-center gap-2">
                      <span className="min-w-0 truncate font-medium text-slate-800" title={c.title}>
                        {c.title}
                      </span>
                      {isBig && <Badge tone="rose">big brand</Badge>}
                      {weak && <Badge tone="emerald">beatable</Badge>}
                    </div>
                    <div className="mt-0.5 font-mono text-[11px] text-slate-400">{c.asin}</div>
                  </td>
                  <td className="px-4 py-2.5 text-right tabular text-slate-700">{usd(c.price)}</td>
                  <td className="px-4 py-2.5 text-right tabular text-slate-700">{c.reviews.toLocaleString()}</td>
                  <td className="px-4 py-2.5 text-right tabular text-slate-700">{c.rating.toFixed(1)}</td>
                  <td className="px-4 py-2.5 text-right tabular text-slate-500">{ageLabel(c.ageDays)}</td>
                  <td className="px-4 py-2.5 text-right tabular text-slate-500">{c.imagesCount}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function Reviews({ product }: { product: Product }) {
  const totalFreq = product.complaints.reduce((a, c) => a + c.freqPct, 0);
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Review sample" value={product.reviewSample} hint="≥150 across target + top 3 = full" />
        <Stat label="Listed reviews" value={product.reviews.toLocaleString()} hint={`${product.rating.toFixed(1)} ★ average`} />
        <Stat label="Complaint mass" value={pct(totalFreq)} hint="Σ frequency across themes" />
        <Stat label="Missing features" value={product.missingFeatures.length} hint="requested but absent page-one" />
      </div>

      <Card className="p-5">
        <SectionTitle
          title="Complaint themes"
          subtitle="Frequencies are recomputed from the sample; the Miner's own percentages are advisory only."
        />
        <ul className="space-y-4">
          {product.complaints.map((c) => (
            <li key={c.theme}>
              <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                <span className="font-medium text-slate-800">{c.theme}</span>
                <span className="flex items-center gap-2 text-xs text-slate-500">
                  <SeverityPips severity={c.severity} />
                  <span className="tabular">{c.freqPct}% of reviews</span>
                </span>
              </div>
              <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-100">
                <div className="h-full rounded-full bg-rose-400" style={{ width: `${c.freqPct}%` }} />
              </div>
              {c.quotes[0] && (
                <blockquote className="mt-2 border-l-2 border-slate-200 pl-3 text-xs italic text-slate-500">
                  “{c.quotes[0]}”
                </blockquote>
              )}
            </li>
          ))}
        </ul>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="p-5">
          <SectionTitle title="Missing features" subtitle="Requested in ≥3 reviews, absent from every page-one listing" />
          {product.missingFeatures.length === 0 ? (
            <EmptyState icon={MessageSquare} title="No missing features surfaced" />
          ) : (
            <ul className="space-y-2 text-sm">
              {product.missingFeatures.map((f) => (
                <li key={f} className="flex items-start gap-2">
                  <Layers className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
                  <span className="text-slate-700">{f}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card className="p-5">
          <SectionTitle title="Improvement brief" subtitle="The version of this product I would launch" />
          <p className="text-sm leading-relaxed text-slate-600">
            The Strategist reads every prior output and argues like a capital allocator. Its differentiation plan
            links each change to a cited complaint theme — never a generic upgrade. Run{" "}
            <code className="font-mono text-xs">delium validate</code> to generate the real plan, or{" "}
            <code className="font-mono text-xs">delium pains {product.asin}</code> for the full review-mining pass.
          </p>
        </Card>
      </div>
    </div>
  );
}

function SeverityPips({ severity }: { severity: 1 | 2 | 3 }) {
  return (
    <span className="flex items-center gap-0.5" title={`severity ${severity}/3`}>
      {[1, 2, 3].map((n) => (
        <span
          key={n}
          className={cx("h-1.5 w-1.5 rounded-full", n <= severity ? "bg-rose-500" : "bg-slate-200")}
        />
      ))}
    </span>
  );
}

/* ------------------------------------------------------------------ */

function Profit({
  product,
  opts,
  setOpts,
  quoted,
  setQuoted,
}: {
  product: Product;
  opts: ProfitOptions;
  setOpts: (o: ProfitOptions) => void;
  quoted: boolean;
  setQuoted: (b: boolean) => void;
}) {
  const profit = computeProfit(product, quoted ? { ...opts, quotedCogs: true } : opts);
  const stressed = stressedProfit(product, quoted ? { ...opts, quotedCogs: true } : opts);

  const price = opts.price ?? product.price;
  const cogsPct = opts.cogsPct ?? CONFIG.assumptions.default_cogs_pct;
  const rowMargin = (p: number, c: number, t: number) =>
    computeProfit(product, { ...opts, price: p, cogsPct: c, tacosPct: t }).marginPct;

  const prices = [-0.15, 0, 0.15].map((d) => price * (1 + d));
  const cogsDeltas = [-0.2, 0, 0.2];

  const override = Math.abs(price - product.price) > 0.01 || Math.abs(cogsPct - CONFIG.assumptions.default_cogs_pct) > 0.001;

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat icon={Percent} label="Net margin (base)" value={pct(profit.marginPct)} hint={`stressed ${pct(stressed.marginPct)}`} />
        <Stat icon={Coins} label="Net per unit" value={usd(profit.marginUnit)} hint={`stressed ${usd(stressed.marginUnit)}`} />
        <Stat label="ROI / turn" value={pct(profit.roiPct)} hint={`stressed ${pct(stressed.roiPct)}`} />
        <Stat
          label="Payback"
          value={isFinite(profit.paybackMonths) ? `${profit.paybackMonths.toFixed(1)} mo` : "—"}
          hint={`capital ${usd(profit.launchCapital)}`}
        />
      </div>

      <Card className="p-5">
        <SectionTitle
          title="Assumption overrides"
          subtitle="Session-only — this never writes to config.toml. Real overrides go on the CLI: --cogs 4.20 --freight 1.10"
          actions={
            override ? (
              <Button size="sm" variant="ghost" onClick={() => setOpts({ cogsPct: CONFIG.assumptions.default_cogs_pct, freight: CONFIG.assumptions.freight_per_unit })}>
                Reset to config
              </Button>
            ) : undefined
          }
        />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <NumField label="Sell price" value={price} onChange={(v) => setOpts({ ...opts, price: v })} prefix="$" step={0.5} />
          <NumField
            label="COGS (% of price)"
            value={cogsPct}
            onChange={(v) => setOpts({ ...opts, cogsPct: v / 100, cogsUsd: undefined })}
            scale={100}
            suffix="%"
            step={1}
          />
          <NumField
            label="Freight / unit"
            value={opts.freight ?? CONFIG.assumptions.freight_per_unit}
            onChange={(v) => setOpts({ ...opts, freight: v })}
            prefix="$"
            step={0.1}
          />
          <NumField
            label="TACOS"
            value={(opts.tacosPct ?? CONFIG.assumptions.tacos_pct) * 100}
            onChange={(v) => setOpts({ ...opts, tacosPct: v / 100 })}
            suffix="%"
            step={1}
          />
        </div>
        <div className="mt-3">
          <Toggle checked={quoted} onChange={setQuoted} label="Real supplier quote present (raises confidence to high)" />
        </div>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="p-5">
          <SectionTitle title="Unit economics" subtitle={`Fee table: us-2026 · size tier ${profit.tier.replace("_", " ")}`} />
          <table className="w-full text-sm">
            <tbody className="divide-y divide-slate-100">
              {profit.lines.map((l) => (
                <tr key={l.label}>
                  <td
                    className={cx(
                      "py-2 pr-3",
                      l.kind === "net" ? "font-semibold text-slate-900" : "text-slate-600"
                    )}
                  >
                    {l.label}
                  </td>
                  <td
                    className={cx(
                      "py-2 text-right tabular",
                      l.kind === "revenue"
                        ? "text-slate-900"
                        : l.kind === "net"
                          ? l.amount >= 0
                            ? "font-semibold text-emerald-700"
                            : "font-semibold text-rose-700"
                          : "text-slate-700"
                    )}
                  >
                    {usd(l.amount)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-3 flex flex-wrap gap-2 text-xs">
            <Badge tone={profit.gates.margin ? "emerald" : "rose"}>margin ≥ {pct(CONFIG.gates.min_margin)}</Badge>
            <Badge tone={profit.gates.roi ? "emerald" : "rose"}>ROI ≥ {pct(CONFIG.gates.min_roi)}</Badge>
            <Badge tone={profit.gates.payback ? "emerald" : "rose"}>payback ≤ {CONFIG.gates.max_payback_months} mo</Badge>
          </div>
        </Card>

        <Card className="p-5">
          <SectionTitle title="Sensitivity" subtitle="Margin across price ±15% × COGS ±20% · TACOS fixed at 15%" />
          <table className="w-full text-xs">
            <thead>
              <tr className="text-slate-500">
                <th className="py-2 text-left font-medium">COGS ↓ / Price →</th>
                {prices.map((p) => (
                  <th key={p} className="py-2 text-right font-medium">
                    {usd(p)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {cogsDeltas.map((d) => (
                <tr key={d}>
                  <td className="py-2 text-slate-600">
                    {cogsPct > 0 ? `${((cogsPct * (1 + d)) * 100).toFixed(0)}%` : "—"}
                  </td>
                  {prices.map((p) => {
                    const m = rowMargin(p, cogsPct * (1 + d), CONFIG.assumptions.tacos_pct);
                    const tone = m >= CONFIG.gates.min_margin ? "text-emerald-700" : m >= 0.2 ? "text-amber-700" : "text-rose-700";
                    return (
                      <td key={`${p}-${d}`} className={cx("py-2 text-right tabular font-medium", tone)}>
                        {pct(m)}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-3 text-[11px] text-slate-500">
            The honest answer is a surface, not a point. Cells below {pct(CONFIG.gates.min_margin)} fail G1.
          </p>
        </Card>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function Risk({
  product,
  scored,
  stressed,
}: {
  product: Product;
  scored: Scored;
  stressed: ReturnType<typeof stressedProfit>;
}) {
  return (
    <div className="space-y-5">
      <Card className="p-5">
        <div className="flex flex-wrap items-center gap-6">
          <ScoreRing value={scored.pillars.risk} size={84} label="Risk" />
          <div className="min-w-0 flex-1">
            <div className="text-sm text-slate-700">
              Starts at 100 and subtracts per confirmed flag. The report renders this ledger verbatim — flags with
              evidence, not just a number.
            </div>
            <div className="mt-2 flex flex-wrap gap-2">
              <Badge tone={(scored.pillars.risk ?? 0) >= CONFIG.gates.risk_floor ? "emerald" : "rose"}>
                G3 risk floor ≥ {CONFIG.gates.risk_floor}
              </Badge>
            </div>
          </div>
        </div>
      </Card>

      <Card className="overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs font-medium text-slate-500">
            <tr>
              <th className="px-4 py-2.5">Flag</th>
              <th className="px-4 py-2.5 text-right">Deduction</th>
              <th className="px-4 py-2.5">Evidence</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {product.risks.map((r) => (
              <tr key={r.flag}>
                <td className="px-4 py-2.5 font-mono text-xs text-rose-700">{r.flag}</td>
                <td className="px-4 py-2.5 text-right tabular font-medium text-rose-700">−{r.deduction}</td>
                <td className="px-4 py-2.5 text-slate-600">{r.evidence}</td>
              </tr>
            ))}
            <tr className="bg-slate-50/70">
              <td className="px-4 py-2.5 font-semibold text-slate-800">Total</td>
              <td className="px-4 py-2.5 text-right tabular font-semibold text-slate-900">
                {Math.round(scored.pillars.risk ?? 0)}
              </td>
              <td className="px-4 py-2.5" />
            </tr>
          </tbody>
        </table>
      </Card>

      <Card className="p-5">
        <SectionTitle title="Verdict" subtitle="Score ranks · Strategist decides · I approve" />
        <div className="flex flex-wrap items-center gap-4">
          <VerdictBadge verdict={scored.verdict} />
          <div className="text-sm text-slate-700">{scored.verdictBasis}</div>
        </div>
        <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50/70 p-3 text-xs text-amber-900">
          <div className="flex gap-2">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" />
            <span>
              <strong>G5 pending.</strong> No LLM layer runs in this preview, so the deterministic verdict stands
              alone — it can never manufacture a BUY. A missing or dissenting Strategist caps a would-be Buy at
              TEST.
            </span>
          </div>
        </div>
        <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Base margin" value={pct(scored.profit.marginPct)} />
          <Stat label="Stressed margin" value={pct(stressed.marginPct)} />
          <Stat label="Stressed ROI" value={pct(stressed.roiPct)} />
          <Stat
            label="Payback"
            value={isFinite(scored.profit.paybackMonths) ? `${scored.profit.paybackMonths.toFixed(1)} mo` : "—"}
          />
        </div>
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function ZombieCheck({ product }: { product: Product }) {
  const { zombies } = useStore();
  const match = zombies.find((z) => z.asin === product.asin);

  if (!match) {
    return (
      <EmptyState
        icon={Ghost}
        title="No zombie check cached for this ASIN"
        body="Run delium zombies --marketplaces US --check-demand, then it appears here (cache-first — a repeat run is free)."
        action={
          <LinkButton href="#/find" variant="secondary">
            Go to Find → Zombies
          </LinkButton>
        }
      />
    );
  }

  const verdictToneMap: Record<string, "emerald" | "amber" | "rose"> = {
    "Verified zombie": "emerald",
    "Possibly temporary": "amber",
    "Not a zombie": "rose",
  };

  return (
    <div className="space-y-5">
      <Card className="p-5">
        <div className="flex flex-wrap items-center gap-3">
          <Badge tone={verdictToneMap[match.verdict]}>{match.verdict}</Badge>
          <Badge tone="slate">{match.marketplace}</Badge>
          <Badge tone={match.generic ? "violet" : "slate"}>
            {match.generic ? "generic / unbranded" : match.brand}
          </Badge>
          <span className="text-lg font-semibold tabular text-slate-900">{Math.round(match.score)}</span>
        </div>
        <p className="mt-3 text-sm leading-relaxed text-slate-600">{match.reason}</p>
      </Card>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Stat label="Rating" value={`${match.rating.toFixed(1)} ★`} />
        <Stat label="Reviews" value={match.reviews.toLocaleString()} />
        <Stat label="Months dead" value={match.monthsDead} />
        <Stat label="In-stock BSR" value={match.inStockBsr.toLocaleString()} />
        <Stat label="Restock gaps" value={match.restockGaps} />
      </div>

      <Card className="p-5">
        <SectionTitle title="Compliance flags" subtitle="A research signal, not legal or policy clearance" />
        <ul className="space-y-2 text-sm text-slate-700">
          <li className="flex items-start gap-2">
            <Shield className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
            Launch your own improved version on a <strong>NEW</strong> listing (the zombie is demand proof).
          </li>
          {match.generic && (
            <li className="flex items-start gap-2">
              <Shield className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
              Possible revival — <strong>only</strong> offered because the brand looks generic, and only if you can
              source the identical product or hold the brand rights.
            </li>
          )}
          {!match.generic && (
            <li className="flex items-start gap-2">
              <Shield className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
              Named brand — <strong>no revival</strong>. The tool never suggests a different product on an existing
              ASIN.
            </li>
          )}
          <li className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
            Manual check required: a trademark search (USPTO / UK IPO / CIPO) and an Amazon anti-counterfeit policy
            review before acting.
          </li>
        </ul>
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function median(xs: number[]): number {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
