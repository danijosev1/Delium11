import { covers } from './geometry';
import { MATCH_SIZE, TRAY_SIZE, type Tile } from './types';

export interface SolveOptions {
  /** Tiles already sitting in the tray (used for hints/shuffle mid-game). */
  tray?: readonly Tile[];
  /** Search budget. The solver gives up (reports not solvable) after this many states. */
  maxNodes?: number;
}

export interface SolveResult {
  solvable: boolean;
  /** Tile ids in the order they were picked, when solvable. */
  moves: number[];
  /** States explored. */
  nodes: number;
  /** True when the search stopped because it ran out of budget (result is "unknown"). */
  hitLimit: boolean;
}

export const DEFAULT_MAX_NODES = 20000;

/**
 * Plays the level by the normal rules (no undo, shuffle or hints) using a
 * depth-first search with good move ordering and memoisation of dead states.
 *
 * Tray contents are fully determined by which board tiles have been removed
 * (each type's tray count is its removed count mod 3), so the set of tiles
 * still on the board is a complete description of the game state.
 */
export function solve(board: readonly Tile[], options: SolveOptions = {}): SolveResult {
  const maxNodes = options.maxNodes ?? DEFAULT_MAX_NODES;
  const n = board.length;

  let maxType = 0;
  for (const t of board) maxType = Math.max(maxType, t.type);
  for (const t of options.tray ?? []) maxType = Math.max(maxType, t.type);

  const trayCount = new Array<number>(maxType + 1).fill(0);
  let trayTotal = 0;
  for (const t of options.tray ?? []) {
    trayCount[t.type]++;
    trayTotal++;
  }
  if (trayTotal >= TRAY_SIZE) return { solvable: false, moves: [], nodes: 0, hitLimit: false };

  // coveredBy[i]: how many present tiles cover tile i. covering[i]: tiles that tile i covers.
  const coveredBy = new Array<number>(n).fill(0);
  const covering: number[][] = board.map(() => []);
  for (let i = 0; i < n; i++) {
    for (let j = 0; j < n; j++) {
      if (i !== j && covers(board[i], board[j])) {
        covering[i].push(j);
        coveredBy[j]++;
      }
    }
  }

  const present = new Array<boolean>(n).fill(true);
  const words = new Uint32Array(Math.ceil(n / 32) || 1);
  for (let i = 0; i < n; i++) words[i >>> 5] |= 1 << (i & 31);
  const keyOf = () => Array.from(words).join(',');

  const dead = new Set<string>();
  const path: number[] = [];
  let remaining = n;
  let nodes = 0;
  let hitLimit = false;

  const remove = (i: number) => {
    present[i] = false;
    words[i >>> 5] &= ~(1 << (i & 31));
    remaining--;
    for (const j of covering[i]) coveredBy[j]--;
    const type = board[i].type;
    if (trayCount[type] === MATCH_SIZE - 1) {
      trayCount[type] = 0;
      trayTotal -= MATCH_SIZE - 1;
      return true; // completed a triple
    }
    trayCount[type]++;
    trayTotal++;
    return false;
  };

  const restore = (i: number, completedTriple: boolean) => {
    present[i] = true;
    words[i >>> 5] |= 1 << (i & 31);
    remaining++;
    for (const j of covering[i]) coveredBy[j]++;
    const type = board[i].type;
    if (completedTriple) {
      trayCount[type] = MATCH_SIZE - 1;
      trayTotal += MATCH_SIZE - 1;
    } else {
      trayCount[type]--;
      trayTotal--;
    }
  };

  /** Number of tiles that would become free if tile i were removed. */
  const unlocks = (i: number) => {
    let count = 0;
    for (const j of covering[i]) if (coveredBy[j] === 1) count++;
    return count;
  };

  const orderedMoves = (): number[] => {
    const free: number[] = [];
    const freeOfType = new Map<number, number>();
    for (let i = 0; i < n; i++) {
      if (present[i] && coveredBy[i] === 0) {
        free.push(i);
        freeOfType.set(board[i].type, (freeOfType.get(board[i].type) ?? 0) + 1);
      }
    }

    // Completing a triple never hurts: it shrinks the tray and can only uncover tiles.
    let best = -1;
    for (const i of free) {
      if (trayCount[board[i].type] === MATCH_SIZE - 1 && (best < 0 || unlocks(i) > unlocks(best))) best = i;
    }
    if (best >= 0) return [best];

    // Any other move adds a tile to the tray; a full tray without a triple loses.
    if (trayTotal + 1 >= TRAY_SIZE) return [];

    const scored = free.map((i) => {
      const type = board[i].type;
      const inTray = trayCount[type];
      const reachable = inTray + (freeOfType.get(type) ?? 0);
      let score = 10 * unlocks(i);
      if (inTray > 0) score += 100;
      if (reachable >= MATCH_SIZE) score += 60;
      // Opening a new type is risky when the tray is already busy.
      if (inTray === 0) score -= 15 * trayTotal;
      return { i, score };
    });
    scored.sort((a, b) => b.score - a.score || a.i - b.i);
    return scored.map((s) => s.i);
  };

  const search = (): boolean => {
    if (remaining === 0) return trayTotal === 0;
    if (nodes >= maxNodes) {
      hitLimit = true;
      return false;
    }
    nodes++;
    const key = keyOf();
    if (dead.has(key)) return false;

    for (const i of orderedMoves()) {
      const completed = remove(i);
      path.push(board[i].id);
      if (search()) return true;
      path.pop();
      restore(i, completed);
      if (hitLimit) return false;
    }
    dead.add(key);
    return false;
  };

  const solvable = search();
  return { solvable, moves: solvable ? path.slice() : [], nodes, hitLimit: solvable ? false : hitLimit };
}
