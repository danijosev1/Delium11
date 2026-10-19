import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type {
  Product,
  ResearchProfile,
  ScanRecord,
  ShortlistEntry,
  ShortlistStatus,
  CrossMarketRow,
  ZombieRow,
} from "../types";
import { PROFILES, DEFAULT_PROFILE_ID } from "../data/profiles";
import { PRODUCTS } from "../data/products";
import { SCANS, CROSS_MARKET, ZOMBIES } from "../data/signals";

const LS_PROFILES = "delium.profiles.v1";
const LS_ACTIVE = "delium.activeProfile.v1";
const LS_SHORTLIST = "delium.shortlist.v1";

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable — in-memory only */
  }
}

interface StoreValue {
  products: Product[];
  product: (asin?: string) => Product | undefined;

  profiles: ResearchProfile[];
  activeProfile: ResearchProfile;
  setActiveProfile: (id: string) => void;
  upsertProfile: (profile: ResearchProfile) => void;
  resetProfiles: () => void;

  shortlist: ShortlistEntry[];
  isShortlisted: (asin: string) => boolean;
  shortlistEntry: (asin: string) => ShortlistEntry | undefined;
  toggleShortlist: (asin: string) => void;
  setShortlistStatus: (asin: string, status: ShortlistStatus) => void;
  setShortlistNotes: (asin: string, notes: string) => void;
  removeFromShortlist: (asin: string) => void;

  scans: ScanRecord[];
  addScan: (scan: ScanRecord) => void;
  crossMarket: CrossMarketRow[];
  zombies: ZombieRow[];

  keepaTokens: number;
  setKeepaTokens: (n: number) => void;
  spendThisMonth: number;
  addSpend: (usd: number) => void;
}

const StoreContext = createContext<StoreValue | null>(null);

export function StoreProvider({ children }: { children: React.ReactNode }) {
  const [profiles, setProfiles] = useState<ResearchProfile[]>(() =>
    readJson<ResearchProfile[]>(LS_PROFILES, PROFILES)
  );
  const [activeProfileId, setActiveProfileId] = useState<string>(() =>
    readJson<string>(LS_ACTIVE, DEFAULT_PROFILE_ID)
  );
  const [shortlist, setShortlist] = useState<ShortlistEntry[]>(() =>
    readJson<ShortlistEntry[]>(LS_SHORTLIST, [
      {
        asin: "B0DLM002",
        status: "watching",
        notes: "Waiting on a UK cross-market check before I spend review money.",
        addedAt: "2026-02-26T09:12:00.000Z",
      },
      {
        asin: "B0DLM005",
        status: "shortlist",
        notes: "Compartment gaskets are the whole wedge. Quote the 3-compartment mould first.",
        addedAt: "2026-02-27T18:44:00.000Z",
      },
    ])
  );
  const [scans, setScans] = useState<ScanRecord[]>(SCANS);
  const [keepaTokens, setKeepaTokens] = useState<number>(1180);
  const [spendThisMonth, setSpendThisMonth] = useState<number>(64.2);

  useEffect(() => writeJson(LS_PROFILES, profiles), [profiles]);
  useEffect(() => writeJson(LS_ACTIVE, activeProfileId), [activeProfileId]);
  useEffect(() => writeJson(LS_SHORTLIST, shortlist), [shortlist]);

  const activeProfile = useMemo(
    () => profiles.find((p) => p.id === activeProfileId) ?? profiles[0] ?? PROFILES[0],
    [profiles, activeProfileId]
  );

  const product = useCallback((asin?: string) => PRODUCTS.find((p) => p.asin === asin), []);

  const setActiveProfile = useCallback((id: string) => setActiveProfileId(id), []);

  const upsertProfile = useCallback((profile: ResearchProfile) => {
    setProfiles((prev) => {
      const idx = prev.findIndex((p) => p.id === profile.id);
      if (idx === -1) return [...prev, profile];
      const next = [...prev];
      next[idx] = profile;
      return next;
    });
  }, []);

  const resetProfiles = useCallback(() => {
    setProfiles(PROFILES);
    setActiveProfileId(DEFAULT_PROFILE_ID);
  }, []);

  const isShortlisted = useCallback(
    (asin: string) => shortlist.some((s) => s.asin === asin),
    [shortlist]
  );

  const shortlistEntry = useCallback(
    (asin: string) => shortlist.find((s) => s.asin === asin),
    [shortlist]
  );

  const toggleShortlist = useCallback((asin: string) => {
    setShortlist((prev) =>
      prev.some((s) => s.asin === asin)
        ? prev.filter((s) => s.asin !== asin)
        : [...prev, { asin, status: "shortlist", notes: "", addedAt: new Date().toISOString() }]
    );
  }, []);

  const setShortlistStatus = useCallback((asin: string, status: ShortlistStatus) => {
    setShortlist((prev) =>
      prev.map((s) => (s.asin === asin ? { ...s, status } : s))
    );
  }, []);

  const setShortlistNotes = useCallback((asin: string, notes: string) => {
    setShortlist((prev) => prev.map((s) => (s.asin === asin ? { ...s, notes } : s)));
  }, []);

  const removeFromShortlist = useCallback((asin: string) => {
    setShortlist((prev) => prev.filter((s) => s.asin !== asin));
  }, []);

  const addScan = useCallback((scan: ScanRecord) => setScans((prev) => [scan, ...prev]), []);
  const addSpend = useCallback((usd: number) => setSpendThisMonth((v) => v + usd), []);

  const value: StoreValue = {
    products: PRODUCTS,
    product,
    profiles,
    activeProfile,
    setActiveProfile,
    upsertProfile,
    resetProfiles,
    shortlist,
    isShortlisted,
    shortlistEntry,
    toggleShortlist,
    setShortlistStatus,
    setShortlistNotes,
    removeFromShortlist,
    scans,
    addScan,
    crossMarket: CROSS_MARKET,
    zombies: ZOMBIES,
    keepaTokens,
    setKeepaTokens,
    spendThisMonth,
    addSpend,
  };

  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
}

export function useStore(): StoreValue {
  const ctx = useContext(StoreContext);
  if (!ctx) throw new Error("useStore must be used inside <StoreProvider>");
  return ctx;
}
