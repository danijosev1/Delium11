import { isFree } from './geometry';
import { createRng, type Rng } from './rng';
import { solve } from './solver';
import { MATCH_SIZE, type Level, type LevelKind, type LevelParams, type Tile } from './types';

export const MIN_LEVEL = 1;
export const MAX_LEVEL = 1000;

/** Board footprint in whole tiles. Sized so 64pt tiles fit an iPhone in portrait. */
export const BOARD_COLS = 5;
export const BOARD_ROWS = 7;

const MIN_TYPES = 5;
const MAX_TYPES = 16;
const MIN_TILES = 21;
const MAX_TILES = 120;
const MIN_LAYERS = 1;
const MAX_LAYERS = 5;
const TUTORIAL_LEVELS = 20;
const MAX_ATTEMPTS = 60;
/** Large prime so retry seeds never collide with other levels' seeds. */
const SEED_OFFSET = 1_000_003;
const GENERATOR_SOLVER_BUDGET = 20000;

// ---------------------------------------------------------------------------
// Difficulty curve
// ---------------------------------------------------------------------------

/** Smooth 0..1 curve before the sawtooth is applied. */
function baseDifficulty(levelNumber: number): number {
  if (levelNumber <= TUTORIAL_LEVELS) {
    return (0.04 * (levelNumber - 1)) / (TUTORIAL_LEVELS - 1);
  }
  const progress = (levelNumber - TUTORIAL_LEVELS) / (MAX_LEVEL - TUTORIAL_LEVELS);
  return 0.04 + 0.96 * Math.pow(progress, 0.85);
}

export function levelKind(levelNumber: number): LevelKind {
  if (levelNumber <= TUTORIAL_LEVELS) return 'tutorial';
  if (levelNumber % 50 === 0) return 'celebration';
  if (levelNumber % 10 === 0) return 'breather';
  return 'normal';
}

/** Even-layer slots, odd-layer (half-offset) slots. */
function layerCapacity(layer: number): number {
  return layer % 2 === 0 ? BOARD_COLS * BOARD_ROWS : (BOARD_COLS - 1) * (BOARD_ROWS - 1);
}

function totalCapacity(layers: number): number {
  let sum = 0;
  for (let l = 0; l < layers; l++) sum += layerCapacity(l);
  return sum;
}

export function getLevelParams(levelNumber: number): LevelParams {
  assertLevelNumber(levelNumber);
  const kind = levelKind(levelNumber);
  const base = baseDifficulty(levelNumber);
  // Sawtooth: every 10th level is a breather, every 50th a medium "celebration".
  const difficulty = kind === 'breather' ? base * 0.55 : kind === 'celebration' ? base * 0.78 : base;

  const tileTypes = MIN_TYPES + Math.round((MAX_TYPES - MIN_TYPES) * difficulty);
  const totalTiles = MIN_TILES + MATCH_SIZE * Math.round(((MAX_TILES - MIN_TILES) / MATCH_SIZE) * difficulty);
  let layers = MIN_LAYERS + Math.round((MAX_LAYERS - MIN_LAYERS) * difficulty);
  // Leave some breathing room on the board so layouts are not packed solid.
  while (layers < MAX_LAYERS && totalTiles > 0.85 * totalCapacity(layers)) layers++;
  const trayPressure = 3 + Math.round(3 * difficulty);

  return { levelNumber, kind, difficulty, tileTypes, totalTiles, layers, trayPressure };
}

function assertLevelNumber(levelNumber: number): void {
  if (!Number.isInteger(levelNumber) || levelNumber < MIN_LEVEL || levelNumber > MAX_LEVEL) {
    throw new RangeError(`Level number must be an integer from ${MIN_LEVEL} to ${MAX_LEVEL}, got ${levelNumber}`);
  }
}

// ---------------------------------------------------------------------------
// Backwards construction
// ---------------------------------------------------------------------------

/**
 * Builds the order in which tile types will be picked by a guaranteed solution.
 * The tray never holds more than `trayPressure` tiles (always below 7), so this
 * sequence can always be played to the end.
 */
function buildPickSequence(params: LevelParams, rng: Rng): number[] {
  const triples = params.totalTiles / MATCH_SIZE;
  // Every type appears at least once; the rest are spread at random.
  const tripleTypes: number[] = [];
  for (let i = 0; i < triples; i++) {
    tripleTypes.push(i < params.tileTypes ? i : rng.int(params.tileTypes));
  }
  const unopened = rng.shuffle(tripleTypes);
  const remainingInOpen = new Map<number, number>(); // type -> tiles still to pick for open triples
  const tray = new Array<number>(params.tileTypes).fill(0);
  let trayTotal = 0;
  const sequence: number[] = [];
  const openChance = 0.1 + 0.6 * params.difficulty;
  const cap = params.trayPressure;

  // A move is safe if it completes a triple, or leaves room to finish one later.
  const isSafe = (type: number): boolean => {
    if (tray[type] === MATCH_SIZE - 1) return true;
    const after = trayTotal + 1;
    if (after > cap) return false;
    const makesPair = tray[type] === 1;
    const hasPair = makesPair || tray.some((c) => c === MATCH_SIZE - 1);
    return after < cap || hasPair;
  };

  const add = (type: number) => {
    sequence.push(type);
    tray[type]++;
    trayTotal++;
    if (tray[type] === MATCH_SIZE) {
      tray[type] = 0;
      trayTotal -= MATCH_SIZE;
    }
    const left = (remainingInOpen.get(type) ?? 0) - 1;
    if (left > 0) remainingInOpen.set(type, left);
    else remainingInOpen.delete(type);
  };

  while (sequence.length < params.totalTiles) {
    const continuing = [...remainingInOpen.keys()].filter(isSafe);
    const canOpen = unopened.length > 0 && isSafe(unopened[unopened.length - 1]);
    if (canOpen && (continuing.length === 0 || rng.next() < openChance)) {
      const type = unopened.pop()!;
      remainingInOpen.set(type, (remainingInOpen.get(type) ?? 0) + MATCH_SIZE);
      add(type);
    } else if (continuing.length > 0) {
      add(rng.pick(continuing));
    } else {
      throw new Error('pick sequence stalled'); // cannot happen; guarded by isSafe
    }
  }
  return sequence;
}

interface Slot {
  layer: number;
  x: number;
  y: number;
}

/**
 * Every position a tile may occupy. Even layers sit on the whole-tile grid,
 * odd layers on half offsets, so upper tiles straddle the ones beneath
 * (like a brick wall or a mahjong pyramid).
 */
function templateSlots(layer: number): Slot[] {
  const offset = layer % 2;
  const slots: Slot[] = [];
  for (let y = offset; y <= 2 * (BOARD_ROWS - 1) - offset; y += 2) {
    for (let x = offset; x <= 2 * (BOARD_COLS - 1) - offset; x += 2) slots.push({ layer, x, y });
  }
  return slots;
}

function slotOverlaps(a: Slot, b: Slot): boolean {
  return Math.abs(a.x - b.x) < 2 && Math.abs(a.y - b.y) < 2;
}

/** How many tiles each layer should get, weighted toward the bottom. */
function layerQuotas(params: LevelParams): number[] {
  const weights: number[] = [];
  for (let l = 0; l < params.layers; l++) weights.push(layerCapacity(l) * (1 - 0.15 * l));
  const weightSum = weights.reduce((a, b) => a + b, 0);
  const quotas = weights.map((w, l) => Math.min(layerCapacity(l), Math.floor((params.totalTiles * w) / weightSum)));
  let assigned = quotas.reduce((a, b) => a + b, 0);
  for (let l = 0; assigned < params.totalTiles; l = (l + 1) % params.layers) {
    if (quotas[l] < layerCapacity(l)) {
      quotas[l]++;
      assigned++;
    }
  }
  return quotas;
}

/**
 * Chooses which template slots hold tiles. Every tile above the bottom layer
 * rests fully on tiles beneath it, and slots near the middle are preferred so
 * the board forms a compact pyramid.
 */
function chooseLayout(params: LevelParams, rng: Rng): Slot[] | null {
  const quotas = layerQuotas(params);
  const chosen: Slot[][] = [];
  const centerX = BOARD_COLS - 1;
  const centerY = BOARD_ROWS - 1;
  // Small random jitter keeps layouts varied while staying roughly symmetric.
  const score = (s: Slot) => (s.x - centerX) ** 2 + 0.7 * (s.y - centerY) ** 2 + rng.next() * 6;

  const supported = (s: Slot): boolean => {
    if (s.layer === 0) return true;
    return templateSlots(s.layer - 1)
      .filter((below) => slotOverlaps(below, s))
      .every((below) => chosen[s.layer - 1].some((c) => c.x === below.x && c.y === below.y));
  };

  let deficit = 0;
  for (let l = 0; l < params.layers; l++) {
    chosen.push([]);
    const want = quotas[l] + deficit;
    const eligible = templateSlots(l)
      .filter(supported)
      .map((s) => ({ s, score: score(s) }))
      .sort((a, b) => a.score - b.score);
    const take = Math.min(want, eligible.length);
    chosen[l] = eligible.slice(0, take).map((e) => e.s);
    deficit = want - take;
  }

  // Top up any shortfall wherever a supported slot is still empty, bottom first.
  for (let l = 0; deficit > 0 && l < params.layers; l++) {
    for (const s of templateSlots(l)) {
      if (deficit === 0) break;
      const taken = chosen[l].some((c) => c.x === s.x && c.y === s.y);
      const blocked = chosen.slice(l + 1).some((layer) => layer.some((c) => slotOverlaps(c, s)));
      if (!taken && !blocked && supported(s)) {
        chosen[l].push(s);
        deficit--;
      }
    }
  }
  return deficit > 0 ? null : chosen.flat();
}

/**
 * Builds a guaranteed-solvable level: choose the layout, then dig it out in a
 * random valid order (always taking a currently free tile) and stamp each
 * tile with the next type from the pick sequence. Replaying that order is a
 * winning game, so the level is solvable by construction.
 */
function placeTiles(sequence: number[], params: LevelParams, rng: Rng): Tile[] | null {
  const layout = chooseLayout(params, rng);
  if (!layout) return null;

  const tiles: Tile[] = layout.map((s, i) => ({ id: i, type: -1, layer: s.layer, x: s.x, y: s.y }));
  const remaining = new Set(tiles);
  for (let k = 0; k < sequence.length; k++) {
    const free = [...remaining].filter((t) => isFree(t, [...remaining]));
    // Harder levels tend to clear the top first, hiding matching partners deeper down.
    const weights = free.map((t) => 1 + params.difficulty * 3 * t.layer);
    let r = rng.next() * weights.reduce((a, b) => a + b, 0);
    let pick = free[free.length - 1];
    for (let i = 0; i < free.length; i++) {
      r -= weights[i];
      if (r < 0) {
        pick = free[i];
        break;
      }
    }
    pick.type = sequence[k];
    remaining.delete(pick);
  }

  // Stable, readable order: bottom layer first, then top-to-bottom, left-to-right.
  tiles.sort((a, b) => a.layer - b.layer || a.y - b.y || a.x - b.x);
  return tiles.map((t, i) => ({ ...t, id: i }));
}

function buildLevel(params: LevelParams, seed: number): Tile[] | null {
  const rng = createRng(seed);
  const sequence = buildPickSequence(params, rng);
  return placeTiles(sequence, params, rng);
}

/**
 * Deterministically generates a level. The layout is built backwards from a
 * known solution, then checked by the solver. If the solver cannot clear it
 * (e.g. the layout is too tricky for its search budget) the seed is offset and
 * the level rebuilt.
 */
export function generateLevel(levelNumber: number): Level {
  const params = getLevelParams(levelNumber);
  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
    const seed = levelNumber + attempt * SEED_OFFSET;
    const tiles = buildLevel(params, seed);
    if (!tiles) continue;
    if (!solve(tiles, { maxNodes: GENERATOR_SOLVER_BUDGET }).solvable) continue;
    return { levelNumber, seed, attempts: attempt + 1, params, tiles, cols: BOARD_COLS, rows: BOARD_ROWS };
  }
  throw new Error(`Could not generate a solvable layout for level ${levelNumber}`);
}
