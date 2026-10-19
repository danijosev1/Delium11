import React, { useEffect, useState } from "react";
import { Check, Copy, X } from "lucide-react";
import type { Confidence, Verdict } from "../types";

export type Tone = "slate" | "emerald" | "sky" | "amber" | "rose" | "violet";

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

export function scoreTone(score: number | null | undefined): Tone {
  if (score == null) return "slate";
  if (score >= 75) return "emerald";
  if (score >= 60) return "sky";
  if (score >= 45) return "amber";
  return "rose";
}

const BADGE: Record<Tone, string> = {
  slate: "bg-slate-100 text-slate-700 ring-slate-200",
  emerald: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  sky: "bg-sky-50 text-sky-700 ring-sky-200",
  amber: "bg-amber-50 text-amber-800 ring-amber-200",
  rose: "bg-rose-50 text-rose-700 ring-rose-200",
  violet: "bg-violet-50 text-violet-700 ring-violet-200",
};

const BAR: Record<Tone, string> = {
  slate: "bg-slate-300",
  emerald: "bg-emerald-500",
  sky: "bg-sky-500",
  amber: "bg-amber-500",
  rose: "bg-rose-500",
  violet: "bg-violet-500",
};

export const TONE_HEX: Record<Tone, string> = {
  slate: "#64748b",
  emerald: "#059669",
  sky: "#0284c7",
  amber: "#d97706",
  rose: "#e11d48",
  violet: "#7c3aed",
};

/* ------------------------------------------------------------------ */

export function Badge({
  tone = "slate",
  className,
  children,
  title,
}: {
  tone?: Tone;
  className?: string;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cx(
        "inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset",
        BADGE[tone],
        className
      )}
    >
      {children}
    </span>
  );
}

export function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <div className={cx("rounded-xl border border-slate-200 bg-white shadow-sm", className)}>{children}</div>
  );
}

export function SectionTitle({
  title,
  subtitle,
  actions,
  className,
}: {
  title: string;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cx("mb-4 flex flex-wrap items-start justify-between gap-3", className)}>
      <div className="min-w-0">
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
        {subtitle && <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight text-slate-900 md:text-2xl">{title}</h1>
        {subtitle && <p className="mt-1 max-w-3xl text-sm text-slate-500">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ */

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";

const BTN_VARIANT: Record<Variant, string> = {
  primary: "bg-emerald-600 text-white shadow-sm hover:bg-emerald-700",
  secondary: "border border-slate-200 bg-white text-slate-700 shadow-sm hover:bg-slate-50",
  ghost: "text-slate-600 hover:bg-slate-100",
  danger: "border border-rose-200 bg-white text-rose-700 hover:bg-rose-50",
};
const BTN_SIZE: Record<Size, string> = {
  sm: "h-8 gap-1.5 px-2.5 text-xs",
  md: "h-9 gap-2 px-3.5 text-sm",
};

export function buttonClass(variant: Variant = "secondary", size: Size = "md", className?: string) {
  return cx(
    "inline-flex items-center justify-center rounded-lg font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 focus-visible:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50",
    BTN_VARIANT[variant],
    BTN_SIZE[size],
    className
  );
}

export function Button({
  variant = "secondary",
  size = "md",
  className,
  type = "button",
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: Size }) {
  return <button type={type} className={buttonClass(variant, size, className)} {...rest} />;
}

export function LinkButton({
  href,
  variant = "secondary",
  size = "md",
  className,
  children,
}: {
  href: string;
  variant?: Variant;
  size?: Size;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <a href={href} className={buttonClass(variant, size, className)}>
      {children}
    </a>
  );
}

/* ------------------------------------------------------------------ */

export function Stat({
  label,
  value,
  hint,
  icon: Icon,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
}) {
  return (
    <Card className="p-4">
      <div className="flex items-center gap-2 text-xs font-medium text-slate-500">
        {Icon && <Icon className="h-3.5 w-3.5 text-slate-400" />}
        {label}
      </div>
      <div className="mt-1.5 truncate text-lg font-semibold tabular text-slate-900">{value}</div>
      {hint && <div className="mt-0.5 truncate text-xs text-slate-500">{hint}</div>}
    </Card>
  );
}

export function ScoreBar({
  value,
  showValue = true,
  tone,
  className,
}: {
  value: number | null | undefined;
  showValue?: boolean;
  tone?: Tone;
  className?: string;
}) {
  const t = tone ?? scoreTone(value);
  const width = value == null ? 0 : Math.max(0, Math.min(100, value));
  return (
    <div className={cx("flex items-center gap-2", className)}>
      <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
        <div className={cx("h-full rounded-full transition-all", BAR[t])} style={{ width: `${width}%` }} />
      </div>
      {showValue && (
        <span className="w-8 text-right text-xs font-medium tabular text-slate-700">
          {value == null ? "—" : Math.round(value)}
        </span>
      )}
    </div>
  );
}

export function ScoreRing({ value, size = 76, label }: { value: number | null; size?: number; label?: string }) {
  const stroke = 7;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const v = value == null ? 0 : Math.max(0, Math.min(100, value));
  const color = TONE_HEX[scoreTone(value)];
  return (
    <div className="relative inline-grid shrink-0 place-items-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90" aria-hidden="true">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#e2e8f0" strokeWidth={stroke} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - v / 100)}
        />
      </svg>
      <div className="absolute text-center">
        <div className="text-xl font-semibold leading-none tabular text-slate-900">
          {value == null ? "—" : Math.round(value)}
        </div>
        {label && <div className="mt-0.5 text-[10px] uppercase tracking-wide text-slate-500">{label}</div>}
      </div>
    </div>
  );
}

export function Sparkline({
  data,
  width = 96,
  height = 28,
  tone = "emerald",
  invert = false,
}: {
  data: number[];
  width?: number;
  height?: number;
  tone?: Tone;
  invert?: boolean;
}) {
  if (data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const pts = data
    .map((v, i) => {
      const x = (i / (data.length - 1)) * width;
      const n = (v - min) / range;
      const y = invert ? 2 + n * (height - 4) : height - 2 - n * (height - 4);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={width} height={height} aria-hidden="true">
      <polyline points={pts} fill="none" stroke={TONE_HEX[tone]} strokeWidth={1.75} strokeLinejoin="round" />
    </svg>
  );
}

export function LineChart({
  data,
  height = 150,
  tone = "emerald",
  invert = false,
  format = (n: number) => String(n),
  startLabel,
  endLabel,
}: {
  data: number[];
  height?: number;
  tone?: Tone;
  invert?: boolean;
  format?: (n: number) => string;
  startLabel?: string;
  endLabel?: string;
}) {
  if (data.length < 2) {
    return <div className="grid h-32 place-items-center text-sm text-slate-400">Not enough history</div>;
  }
  const W = 600;
  const H = height;
  const pad = 8;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const pts = data.map((v, i) => {
    const x = (i / (data.length - 1)) * W;
    const n = (v - min) / range;
    const y = invert ? pad + n * (H - 2 * pad) : H - pad - n * (H - 2 * pad);
    return [x, y] as const;
  });
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${line} L${W},${H} L0,${H} Z`;
  const color = TONE_HEX[tone];
  return (
    <div>
      <div className="flex gap-2">
        <div
          className="flex w-12 shrink-0 flex-col justify-between text-right text-[10px] tabular text-slate-400"
          style={{ height }}
        >
          <span>{format(invert ? min : max)}</span>
          <span>{format(invert ? max : min)}</span>
        </div>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          className="min-w-0 flex-1"
          style={{ height }}
          role="img"
          aria-label="Trend chart"
        >
          <line x1={0} y1={H - 0.5} x2={W} y2={H - 0.5} stroke="#e2e8f0" vectorEffect="non-scaling-stroke" />
          <path d={area} fill={color} fillOpacity={0.08} />
          <path
            d={line}
            fill="none"
            stroke={color}
            strokeWidth={2}
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />
        </svg>
      </div>
      {(startLabel || endLabel) && (
        <div className="mt-1 flex justify-between pl-14 text-[10px] text-slate-400">
          <span>{startLabel}</span>
          <span>{endLabel}</span>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */

export function Tabs({
  tabs,
  value,
  onChange,
}: {
  tabs: { id: string; label: string; count?: number }[];
  value: string;
  onChange: (id: string) => void;
}) {
  return (
    <div className="-mx-1 overflow-x-auto border-b border-slate-200" role="tablist">
      <div className="flex min-w-max gap-1 px-1">
        {tabs.map((t) => {
          const active = t.id === value;
          return (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => onChange(t.id)}
              className={cx(
                "-mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition",
                active
                  ? "border-emerald-600 text-emerald-700"
                  : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-800"
              )}
            >
              {t.label}
              {t.count != null && (
                <span
                  className={cx(
                    "rounded-full px-1.5 text-[11px]",
                    active ? "bg-emerald-100 text-emerald-700" : "bg-slate-100 text-slate-500"
                  )}
                >
                  {t.count}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-slate-900/40 p-3 sm:items-center"
      onMouseDown={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(e) => e.stopPropagation()}
        className="flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl bg-white shadow-xl"
      >
        <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3.5">
          <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-md p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="overflow-y-auto px-5 py-4">{children}</div>
        {footer && (
          <div className="flex flex-wrap justify-end gap-2 border-t border-slate-100 bg-slate-50 px-5 py-3">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */

export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the textarea fallback */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

export function CopyButton({ text, dark = false }: { text: string; dark?: boolean }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const t = window.setTimeout(() => setState("idle"), 1600);
    return () => window.clearTimeout(t);
  }, [state]);
  return (
    <button
      type="button"
      onClick={async () => setState((await copyText(text)) ? "copied" : "failed")}
      aria-label="Copy command"
      className={cx(
        "inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition",
        dark ? "text-slate-300 hover:bg-white/10 hover:text-white" : "text-slate-500 hover:bg-slate-100"
      )}
    >
      {state === "copied" ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      {state === "copied" ? "Copied" : state === "failed" ? "Select & copy" : "Copy"}
    </button>
  );
}

export function CommandBlock({ command, cost }: { command: string; cost?: string }) {
  return (
    <div className="overflow-hidden rounded-lg bg-slate-900">
      <div className="flex items-start gap-2 px-3 py-2.5">
        <code className="min-w-0 flex-1 select-all whitespace-pre-wrap break-all font-mono text-[12.5px] leading-relaxed text-emerald-200">
          <span className="select-none text-slate-500">$ </span>
          {command}
        </code>
        <CopyButton text={command} dark />
      </div>
      {cost && (
        <div className="border-t border-white/10 px-3 py-1.5 text-[11px] text-slate-400">Projected: {cost}</div>
      )}
    </div>
  );
}

export function EmptyState({
  icon: Icon,
  title,
  body,
  action,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  title: string;
  body?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center rounded-xl border border-dashed border-slate-300 bg-white px-6 py-12 text-center">
      {Icon && (
        <div className="mb-3 grid h-10 w-10 place-items-center rounded-full bg-slate-100">
          <Icon className="h-5 w-5 text-slate-400" />
        </div>
      )}
      <div className="text-sm font-semibold text-slate-800">{title}</div>
      {body && <div className="mt-1 max-w-md text-sm text-slate-500">{body}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ProductThumb({ emoji, hue, size = "md" }: { emoji: string; hue: number; size?: "sm" | "md" | "lg" }) {
  const dims = size === "sm" ? "h-9 w-9 text-lg" : size === "lg" ? "h-16 w-16 text-3xl" : "h-12 w-12 text-2xl";
  return (
    <div
      className={cx("grid shrink-0 place-items-center rounded-lg border", dims)}
      style={{ background: `hsl(${hue} 70% 95%)`, borderColor: `hsl(${hue} 50% 86%)` }}
      aria-hidden="true"
    >
      {emoji}
    </div>
  );
}

export function VerdictBadge({ verdict }: { verdict: Verdict }) {
  const t: Tone = verdict === "BUY" ? "emerald" : verdict === "TEST" ? "amber" : "rose";
  return (
    <Badge tone={t} className="font-semibold tracking-wide">
      {verdict}
    </Badge>
  );
}

export function ConfidenceBadge({ confidence }: { confidence: Confidence }) {
  const t: Tone = confidence === "high" ? "emerald" : confidence === "medium" ? "sky" : "amber";
  return <Badge tone={t}>{confidence} confidence</Badge>;
}

/* ------------------------------------------------------------------ */

export const inputCls =
  "w-full rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-sm text-slate-900 outline-none focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/20";

export function NumField({
  label,
  value,
  onChange,
  step = 1,
  min,
  max,
  prefix,
  suffix,
  scale = 1,
  hint,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  step?: number;
  min?: number;
  max?: number;
  prefix?: string;
  suffix?: string;
  scale?: number;
  hint?: string;
}) {
  const shown = +(value * scale).toFixed(4);
  const [text, setText] = useState(String(shown));
  useEffect(() => {
    setText((prev) => (parseFloat(prev) === shown ? prev : String(shown)));
  }, [shown]);

  return (
    <label className="block">
      <span className="text-xs font-medium text-slate-600">{label}</span>
      <div className="mt-1 flex items-center rounded-lg border border-slate-200 bg-white focus-within:border-emerald-500 focus-within:ring-2 focus-within:ring-emerald-500/20">
        {prefix && <span className="pl-2.5 text-sm text-slate-400">{prefix}</span>}
        <input
          type="number"
          inputMode="decimal"
          step={step}
          min={min}
          max={max}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            const v = parseFloat(e.target.value);
            if (!Number.isNaN(v) && (min == null || v >= min) && (max == null || v <= max)) onChange(v / scale);
          }}
          onBlur={() => setText(String(shown))}
          className="w-full min-w-0 bg-transparent px-2.5 py-1.5 text-sm tabular outline-none"
        />
        {suffix && <span className="pr-2.5 text-sm text-slate-400">{suffix}</span>}
      </div>
      {hint && <span className="mt-1 block text-[11px] text-slate-500">{hint}</span>}
    </label>
  );
}

export function SelectField({
  label,
  value,
  onChange,
  options,
  className,
}: {
  label?: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  className?: string;
}) {
  return (
    <label className={cx("block", className)}>
      {label && <span className="text-xs font-medium text-slate-600">{label}</span>}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={cx(inputCls, label ? "mt-1" : "", "pr-8")}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function TextField({
  label,
  value,
  onChange,
  placeholder,
  hint,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  hint?: string;
}) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-slate-600">{label}</span>
      <input
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className={cx(inputCls, "mt-1")}
      />
      {hint && <span className="mt-1 block text-[11px] text-slate-500">{hint}</span>}
    </label>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <label className="inline-flex cursor-pointer select-none items-center gap-2 text-xs font-medium text-slate-600">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={cx(
          "relative h-5 w-9 rounded-full transition",
          checked ? "bg-emerald-600" : "bg-slate-300"
        )}
      >
        <span
          className={cx(
            "absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all",
            checked ? "left-[18px]" : "left-0.5"
          )}
        />
      </button>
      {label}
    </label>
  );
}
