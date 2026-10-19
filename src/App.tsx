import React from "react";
import { StoreProvider, useStore } from "./web/state/store";
import { useHashRoute } from "./web/lib/router";
import Sidebar, { MobileNav } from "./web/components/Sidebar";
import CommandCenter from "./web/pages/CommandCenter";
import Find from "./web/pages/Find";
import Workspace from "./web/pages/Workspace";
import Shortlist from "./web/pages/Shortlist";
import Commands from "./web/pages/Commands";
import Settings from "./web/pages/Settings";
import { Badge } from "./web/components/ui";
import { Info } from "lucide-react";

export default function App() {
  return (
    <StoreProvider>
      <Shell />
    </StoreProvider>
  );
}

function Shell() {
  const path = useHashRoute();
  const seg = path.split("/").filter(Boolean);
  const { activeProfile } = useStore();

  let page: React.ReactNode;
  switch (seg[0]) {
    case undefined:
      page = <CommandCenter />;
      break;
    case "find":
      page = <Find />;
      break;
    case "product":
      page = <Workspace key={seg[1] ?? "picker"} asin={seg[1]} />;
      break;
    case "shortlist":
      page = <Shortlist />;
      break;
    case "commands":
      page = <Commands />;
      break;
    case "settings":
      page = <Settings />;
      break;
    default:
      page = (
        <div className="p-10 text-slate-600">
          Page not found.{" "}
          <a href="#/" className="text-emerald-700 underline">
            Back to Command Center
          </a>
        </div>
      );
  }

  return (
    <div className="flex h-full">
      <Sidebar path={path} />
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/90 backdrop-blur">
          <MobileNav path={path} />
          <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 md:px-8">
            <div className="flex items-center gap-2 text-sm">
              <span className="text-slate-500">Profile:</span>
              <a href="#/settings" className="font-medium text-slate-900 hover:text-emerald-700">
                {activeProfile.name}
              </a>
              <span className="hidden text-slate-300 sm:inline">|</span>
              <span className="hidden text-slate-500 sm:inline">
                {activeProfile.marketplaces.join(" · ")}
              </span>
            </div>
            <Badge tone="amber" className="gap-1">
              <Info className="h-3 w-3" /> Demo data, not connected to data/delium.db
            </Badge>
          </div>
        </header>
        <main className="flex-1 overflow-y-auto">{page}</main>
      </div>
    </div>
  );
}
