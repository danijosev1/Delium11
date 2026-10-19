const SEP = /\s*(?:>|\/|›|»|\|)\s*/;

/** Breadcrumb → normalized segments (casefold + whitespace collapse), like analysis/categories.py. */
export function segments(path: string): string[] {
  return path
    .split(SEP)
    .map((s) => s.trim().replace(/\s+/g, " ").toLowerCase())
    .filter(Boolean);
}

/** Segment equality, never substring. First key in table order wins. */
export function resolveKey(path: string, keys: string[]): string | null {
  const segs = segments(path);
  for (const k of keys) if (segs.includes(k.trim().toLowerCase())) return k;
  return null;
}

export const topCategory = (path: string) => path.split(">")[0].trim();
