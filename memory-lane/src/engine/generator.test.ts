import { overlaps } from './geometry';
import { generateLevel, getLevelParams, MAX_LEVEL } from './generator';
import { solve } from './solver';
import type { Level } from './types';

const ALL_LEVELS = Array.from({ length: MAX_LEVEL }, (_, i) => i + 1);

describe('generateLevel for every level 1-1000', () => {
  const levels: Level[] = [];
  let generationMs = 0;

  beforeAll(() => {
    const start = Date.now();
    for (const n of ALL_LEVELS) levels.push(generateLevel(n));
    generationMs = Date.now() - start;
  });

  afterAll(() => {
    const lines = [`Generated ${levels.length} levels in ${(generationMs / 1000).toFixed(1)}s`];
    for (let from = 1; from <= MAX_LEVEL; from += 100) {
      const bucket = levels.slice(from - 1, from + 99);
      const avg = bucket.reduce((sum, l) => sum + l.tiles.length, 0) / bucket.length;
      const retries = bucket.filter((l) => l.attempts > 1).length;
      lines.push(`  levels ${String(from).padStart(4)}-${String(from + 99).padEnd(4)} avg tiles ${avg.toFixed(1).padStart(5)}  (retried: ${retries})`);
    }
    console.log(lines.join('\n'));
  });

  test('every level is solvable by the solver', () => {
    const start = Date.now();
    const unsolved = levels.filter((l) => !solve(l.tiles, { maxNodes: 50000 }).solvable).map((l) => l.levelNumber);
    console.log(`Solved ${levels.length} levels in ${((Date.now() - start) / 1000).toFixed(1)}s`);
    expect(unsolved).toEqual([]);
  });

  test('generation is deterministic', () => {
    const mismatched = ALL_LEVELS.filter((n) => JSON.stringify(generateLevel(n)) !== JSON.stringify(levels[n - 1]));
    expect(mismatched).toEqual([]);
  });

  test('tile counts are multiples of 3, overall and per picture type', () => {
    const problems: string[] = [];
    for (const level of levels) {
      if (level.tiles.length % 3 !== 0 || level.tiles.length !== level.params.totalTiles) {
        problems.push(`${level.levelNumber}: ${level.tiles.length} tiles`);
      }
      const counts = new Map<number, number>();
      for (const t of level.tiles) counts.set(t.type, (counts.get(t.type) ?? 0) + 1);
      if ([...counts.values()].some((c) => c % 3 !== 0)) problems.push(`${level.levelNumber}: unmatched type`);
      if (counts.size !== level.params.tileTypes) problems.push(`${level.levelNumber}: ${counts.size} types`);
    }
    expect(problems).toEqual([]);
  });

  test('layouts are well formed', () => {
    const problems: string[] = [];
    for (const level of levels) {
      const { tiles, cols, rows, params, levelNumber } = level;
      if (new Set(tiles.map((t) => t.id)).size !== tiles.length) problems.push(`${levelNumber}: duplicate ids`);
      for (const t of tiles) {
        const inBounds = t.x >= 0 && t.x <= 2 * (cols - 1) && t.y >= 0 && t.y <= 2 * (rows - 1);
        if (!inBounds || t.layer >= params.layers) problems.push(`${levelNumber}: tile ${t.id} out of bounds`);
      }
      // No two tiles on the same layer overlap.
      for (let i = 0; i < tiles.length; i++) {
        for (let j = i + 1; j < tiles.length; j++) {
          if (tiles[i].layer === tiles[j].layer && overlaps(tiles[i], tiles[j])) {
            problems.push(`${levelNumber}: tiles ${tiles[i].id} and ${tiles[j].id} overlap`);
          }
        }
      }
    }
    expect(problems).toEqual([]);
  });
});

describe('difficulty curve', () => {
  test('parameters stay within the requested ranges', () => {
    for (const n of ALL_LEVELS) {
      const p = getLevelParams(n);
      expect(p.tileTypes).toBeGreaterThanOrEqual(5);
      expect(p.tileTypes).toBeLessThanOrEqual(16);
      expect(p.totalTiles).toBeGreaterThanOrEqual(21);
      expect(p.totalTiles).toBeLessThanOrEqual(120);
      expect(p.totalTiles % 3).toBe(0);
      expect(p.layers).toBeGreaterThanOrEqual(1);
      expect(p.layers).toBeLessThanOrEqual(5);
    }
    expect(getLevelParams(1)).toMatchObject({ tileTypes: 5, totalTiles: 21, layers: 1 });
    expect(getLevelParams(999)).toMatchObject({ tileTypes: 16, totalTiles: 120, layers: 5 });
  });

  test('levels 1-20 are very easy tutorials', () => {
    for (let n = 1; n <= 20; n++) {
      const p = getLevelParams(n);
      expect(p.kind).toBe('tutorial');
      expect(p.totalTiles).toBeLessThanOrEqual(24);
      expect(p.tileTypes).toBe(5);
      expect(p.layers).toBe(1);
    }
  });

  test('every 10th level is easier than its neighbours, every 50th is a medium celebration', () => {
    for (let n = 30; n <= MAX_LEVEL; n += 10) {
      const p = getLevelParams(n);
      const before = getLevelParams(n - 1);
      expect(p.kind).toBe(n % 50 === 0 ? 'celebration' : 'breather');
      expect(p.difficulty).toBeLessThan(before.difficulty);
      if (n < MAX_LEVEL) expect(p.difficulty).toBeLessThan(getLevelParams(n + 1).difficulty);
    }
    for (let n = 100; n <= MAX_LEVEL; n += 50) {
      // Celebration sits between the breather dip and the normal curve.
      expect(getLevelParams(n).difficulty).toBeGreaterThan(getLevelParams(n - 10).difficulty);
    }
  });

  test('difficulty rises overall', () => {
    expect(getLevelParams(101).totalTiles).toBeLessThan(getLevelParams(501).totalTiles);
    expect(getLevelParams(501).totalTiles).toBeLessThan(getLevelParams(991).totalTiles);
  });

  test('rejects invalid level numbers', () => {
    expect(() => generateLevel(0)).toThrow(RangeError);
    expect(() => generateLevel(1001)).toThrow(RangeError);
    expect(() => generateLevel(1.5)).toThrow(RangeError);
  });
});
