import React, { useMemo } from "react";
import { Bookmark, ExternalLink, Trash2 } from "lucide-react";
import { useStore } from "../state/store";
import { analyse } from "../lib/analyse";
import { topCategory } from "../lib/categories";
import { fmtDate, fmtScore, pct, usd } from "../lib/format";
import {
  Badge,
  Button,
  Card,
  ConfidenceBadge,
  EmptyState,
  LinkButton,
  PageHeader,
  ProductThumb,
  SelectField,
  Stat,
  VerdictBadge,
  cx,
  scoreTone,
} from "../components/ui";
import type { ShortlistStatus } from "../types";

const STATUS_OPTIONS: { value: ShortlistStatus; label: string }[] = [
  { value: "shortlist", label: "Shortlist" },
  { value: "watching", label: "Watching" },
  { value: "validated", label: "Validated" },
  { value: "rejected", label: "Rejected" },
];

export default function Shortlist() {
  const store = useStore();
  const { shortlist, activeProfile, setShortlistStatus, setShortlistNotes, removeFromShortlist } = store;

  const rows = useMemo(
    () =>
      shortlist
        .map((entry) => {
          const product = store.product(entry.asin);
          if (!product) return null;
          return { entry, product, analysed: analyse(product, activeProfile) };
        })
        .filter((r): r is NonNullable<typeof r> => r != null),
    [shortlist, activeProfile, store]
  );

  const watching = rows.filter((r) => r.entry.status === "watching").length;
  const validated = rows.filter((r) => r.entry.status === "validated").length;
  const capital = rows
    .filter((r) => r.entry.status !== "rejected")
    .reduce((a, r) => a + r.analysed.scored.profit.launchCapital, 0);

  return (
    <div className="mx-auto max-w-7xl space-y-6 p-4 md:p-8">
      <PageHeader
        title="Shortlist"
        subtitle={
          <>
            Products you've chosen to keep an eye on. Separate from the Scan Inbox — nothing lands here until you
            promote it. Notes and status are stored locally in your browser.
          </>
        }
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat icon={Bookmark} label="On the list" value={rows.length} hint={`${shortlist.length} stored entries`} />
        <Stat label="Watching" value={watching} hint="re-checked weekly by delium watch" />
        <Stat label="Validated" value={validated} hint="deep dive complete" />
        <Stat label="Capital required" value={usd(capital, 0)} hint="launch capital at low-end demand, non-rejected" />
      </div>

      {rows.length === 0 ? (
        <EmptyState
          icon={Bookmark}
          title="Nothing shortlisted yet"
          body="Open a product from the Scan Inbox or Find and hit Shortlist. Add a note about what would change your mind."
          action={
            <LinkButton href="#/" variant="primary">
              Back to Command Center
            </LinkButton>
          }
        />
      ) : (
        <div className="space-y-4">
          {rows.map(({ entry, product, analysed }) => {
            const s = analysed.scored;
            const sl = analysed.sellability;
            return (
              <Card key={entry.asin} className="p-4">
                <div className="flex flex-wrap items-start gap-4">
                  <ProductThumb emoji={product.emoji} hue={product.hue} />

                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 text-xs text-slate-500">
                      <span className="font-mono">{product.asin}</span>
                      <span>·</span>
                      <span>{topCategory(product.category)}</span>
                      <span>·</span>
                      <span>added {fmtDate(entry.addedAt)}</span>
                    </div>
                    <a
                      href={`#/product/${product.asin}`}
                      className="mt-1 block text-sm font-semibold leading-snug text-slate-900 hover:text-emerald-700"
                    >
                      {product.title}
                    </a>
                    <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      <VerdictBadge verdict={s.verdict} />
                      <ConfidenceBadge confidence={s.confidence} />
                      <Badge tone="slate">opportunity {fmtScore(s.composite)}</Badge>
                      <Badge tone={scoreTone(sl.score)}>sellability {fmtScore(sl.score)}</Badge>
                    </div>
                  </div>

                  <div className="flex w-full items-center gap-2 sm:w-auto">
                    <span className="text-2xl font-semibold tabular text-slate-900">{fmtScore(s.composite)}</span>
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={() => removeFromShortlist(entry.asin)}
                      aria-label={`Remove ${product.asin}`}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>

                <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2.5 text-xs sm:grid-cols-4">
                  <Fig label="Price" value={usd(product.price)} />
                  <Fig label="Sales / mo" value={`${product.monthlySold.toLocaleString()}+`} note={product.salesSource} />
                  <Fig label="Margin (base)" value={pct(s.profit.marginPct)} bad={!s.profit.gates.margin} />
                  <Fig
                    label="Payback"
                    value={s.profit.paybackMonths && isFinite(s.profit.paybackMonths) ? `${s.profit.paybackMonths.toFixed(1)} mo` : "—"}
                  />
                </dl>

                <div className="mt-4 grid gap-3 sm:grid-cols-[200px_1fr]">
                  <SelectField
                    label="Status"
                    value={entry.status}
                    onChange={(v) => setShortlistStatus(entry.asin, v as ShortlistStatus)}
                    options={STATUS_OPTIONS}
                  />
                  <label className="block">
                    <span className="text-xs font-medium text-slate-600">Notes</span>
                    <textarea
                      value={entry.notes}
                      onChange={(e) => setShortlistNotes(entry.asin, e.target.value)}
                      rows={2}
                      placeholder="Supplier conversations, sample results, what would change my mind…"
                      className="mt-1 w-full resize-y rounded-lg border border-slate-200 px-2.5 py-1.5 text-sm outline-none focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/20"
                    />
                  </label>
                </div>

                <div className="mt-3 flex justify-end">
                  <LinkButton href={`#/product/${product.asin}`} size="sm" variant="secondary">
                    Open workspace <ExternalLink className="h-3.5 w-3.5" />
                  </LinkButton>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}

function Fig({
  label,
  value,
  note,
  bad,
}: {
  label: string;
  value: string;
  note?: string;
  bad?: boolean;
}) {
  return (
    <div>
      <dt className="text-slate-500">{label}</dt>
      <dd className={cx("mt-0.5 font-semibold tabular", bad ? "text-rose-700" : "text-slate-900")}>
        {value}
        {note && <span className="ml-1 text-[10px] font-normal text-slate-400">{note}</span>}
      </dd>
    </div>
  );
}
