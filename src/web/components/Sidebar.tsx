import React from "react";
import {
  Bookmark,
  Coins,
  KeyRound,
  LayoutDashboard,
  Package,
  Search,
  Settings,
  Terminal,
  Wallet,
} from "lucide-react";
import { useStore } from "../state/store";
import { CONFIG } from "../lib/config";
import { cx } from "./ui";

type NavItem = {
  href: string;
  seg: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
};

export const NAV: NavItem[] = [
  { href: "#/", seg: "", label: "Command Center", icon: LayoutDashboard },
  { href: "#/find", seg: "find", label: "Find", icon: Search },
  { href: "#/product", seg: "product", label: "Product", icon: Package },
  { href: "#/shortlist", seg: "shortlist", label: "Shortlist", icon: Bookmark },
  { href: "#/commands", seg: "commands", label: "Commands", icon: Terminal },
  { href: "#/settings", seg: "settings", label: "Settings", icon: Settings },
];

const activeSeg = (path: string) => path.split("/").filter(Boolean)[0] ?? "";

export default function Sidebar({ path }: { path: string }) {
  const { shortlist, keepaTokens, spendThisMonth } = useStore();
  const seg = activeSeg(path);
  const alarm = CONFIG.budgets.monthly_spend_alarm_usd;
  const spendPct = Math.min(100, (spendThisMonth / alarm) * 100);

  return (
    <aside className="hidden w-60 shrink-0 flex-col border-r border-slate-200 bg-white md:flex">
      <a href="#/" className="flex items-center gap-2.5 px-5 py-4">
        <div className="grid h-8 w-8 place-items-center rounded-lg bg-emerald-600 text-sm font-bold text-white">
          D
        </div>
        <div>
          <div className="text-sm font-semibold text-slate-900">Delium</div>
          <div className="text-[11px] text-slate-500">Research console</div>
        </div>
      </a>

      <nav className="flex-1 space-y-0.5 px-3 py-2" aria-label="Main">
        {NAV.map((item) => {
          const active = item.seg === seg;
          const Icon = item.icon;
          return (
            <a
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              className={cx(
                "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition",
                active
                  ? "bg-emerald-50 text-emerald-800"
                  : "text-slate-600 hover:bg-slate-50 hover:text-slate-900"
              )}
            >
              <Icon className={cx("h-4 w-4", active ? "text-emerald-600" : "text-slate-400")} />
              <span className="flex-1">{item.label}</span>
              {item.seg === "shortlist" && shortlist.length > 0 && (
                <span className="rounded-full bg-slate-100 px-1.5 text-[11px] text-slate-600">
                  {shortlist.length}
                </span>
              )}
            </a>
          );
        })}
      </nav>

      <div className="space-y-3 border-t border-slate-100 p-4 text-xs">
        <div className="flex items-center justify-between">
          <span className="flex items-center gap-1.5 text-slate-500">
            <Coins className="h-3.5 w-3.5" /> Keepa tokens
          </span>
          <span className="font-medium tabular text-slate-800">{keepaTokens.toLocaleString()}</span>
        </div>
        <div>
          <div className="flex items-center justify-between">
            <span className="flex items-center gap-1.5 text-slate-500">
              <Wallet className="h-3.5 w-3.5" /> This month
            </span>
            <span className="font-medium tabular text-slate-800">
              ${spendThisMonth.toFixed(2)} / ${alarm}
            </span>
          </div>
          <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-100">
            <div
              className={cx("h-full rounded-full", spendPct > 85 ? "bg-rose-500" : "bg-emerald-500")}
              style={{ width: `${spendPct}%` }}
            />
          </div>
        </div>
        <a
          href="#/settings"
          className="flex items-center gap-1.5 text-slate-500 hover:text-emerald-700"
        >
          <KeyRound className="h-3.5 w-3.5" /> Credentials live in .env
        </a>
      </div>
    </aside>
  );
}

export function MobileNav({ path }: { path: string }) {
  const seg = activeSeg(path);
  return (
    <nav className="flex gap-1 overflow-x-auto border-b border-slate-100 px-2 py-1.5 md:hidden" aria-label="Main">
      {NAV.map((item) => {
        const active = item.seg === seg;
        const Icon = item.icon;
        return (
          <a
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={cx(
              "flex shrink-0 items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium",
              active ? "bg-emerald-50 text-emerald-800" : "text-slate-600"
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {item.label}
          </a>
        );
      })}
    </nav>
  );
}
