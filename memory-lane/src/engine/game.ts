import { isFree } from './geometry';
import { createRng } from './rng';
import { solve } from './solver';
import { MATCH_SIZE, TRAY_SIZE, type Level, type Tile } from './types';

export type GameStatus = 'playing' | 'won' | 'lost';

interface Snapshot {
  board: Tile[];
  tray: Tile[];
}

/** Immutable game state. Every action returns a new state object. */
export interface GameState {
  level: Level;
  /** Tiles still on the board. */
  board: Tile[];
  /** Tiles in the tray, grouped so identical tiles sit together. */
  tray: Tile[];
  status: GameStatus;
  /** Number of tiles picked so far (undo reduces it). */
  moves: number;
  /** Count of shuffles used, also feeds the shuffle seed. */
  shuffles: number;
  history: Snapshot[];
}

export function createGame(level: Level): GameState {
  return {
    level,
    board: level.tiles.slice(),
    tray: [],
    status: 'playing',
    moves: 0,
    shuffles: 0,
    history: [],
  };
}

/** Starts the same level over from scratch (free retry after a loss). */
export function restartGame(state: GameState): GameState {
  return createGame(state.level);
}

export function isTileFree(state: GameState, tileId: number): boolean {
  const tile = state.board.find((t) => t.id === tileId);
  return tile !== undefined && isFree(tile, state.board);
}

export function freeTileIds(state: GameState): Set<number> {
  const ids = new Set<number>();
  for (const t of state.board) if (isFree(t, state.board)) ids.add(t.id);
  return ids;
}

/** Inserts next to tiles of the same type, so matches group together. */
function insertIntoTray(tray: readonly Tile[], tile: Tile): Tile[] {
  let lastSame = -1;
  tray.forEach((t, i) => {
    if (t.type === tile.type) lastSame = i;
  });
  const out = tray.slice();
  if (lastSame >= 0) out.splice(lastSame + 1, 0, tile);
  else out.push(tile);
  return out;
}

function removeTriples(tray: Tile[]): { tray: Tile[]; matchedType: number | null } {
  const counts = new Map<number, number>();
  for (const t of tray) counts.set(t.type, (counts.get(t.type) ?? 0) + 1);
  for (const [type, count] of counts) {
    if (count >= MATCH_SIZE) {
      let toRemove = MATCH_SIZE;
      const kept = tray.filter((t) => {
        if (t.type === type && toRemove > 0) {
          toRemove--;
          return false;
        }
        return true;
      });
      return { tray: kept, matchedType: type };
    }
  }
  return { tray, matchedType: null };
}

function statusOf(board: readonly Tile[], tray: readonly Tile[]): GameStatus {
  if (board.length === 0 && tray.length === 0) return 'won';
  if (tray.length >= TRAY_SIZE) return 'lost';
  // Board empty but leftovers in the tray can never be matched.
  if (board.length === 0) return 'lost';
  return 'playing';
}

export interface PickResult {
  state: GameState;
  /** True when this pick completed a triple. */
  matched: boolean;
}

/** Moves a free tile into the tray. Returns the unchanged state if the tap is not allowed. */
export function pickTile(state: GameState, tileId: number): PickResult {
  if (state.status !== 'playing') return { state, matched: false };
  const tile = state.board.find((t) => t.id === tileId);
  if (!tile || !isFree(tile, state.board)) return { state, matched: false };

  const board = state.board.filter((t) => t.id !== tileId);
  const { tray, matchedType } = removeTriples(insertIntoTray(state.tray, tile));
  return {
    state: {
      ...state,
      board,
      tray,
      status: statusOf(board, tray),
      moves: state.moves + 1,
      history: [...state.history, { board: state.board, tray: state.tray }],
    },
    matched: matchedType !== null,
  };
}

export function canUndo(state: GameState): boolean {
  return state.history.length > 0 && state.status !== 'won';
}

/** Reverts the last pick or shuffle. Also works after a loss. */
export function undo(state: GameState): GameState {
  if (!canUndo(state)) return state;
  const previous = state.history[state.history.length - 1];
  const wasPick = previous.board.length !== state.board.length;
  return {
    ...state,
    board: previous.board,
    tray: previous.tray,
    status: statusOf(previous.board, previous.tray),
    moves: wasPick ? state.moves - 1 : state.moves,
    history: state.history.slice(0, -1),
  };
}

const SHUFFLE_TRIES = 12;
const HELPER_SOLVER_BUDGET = 4000;

/**
 * Rearranges which picture sits on each remaining board tile (positions stay).
 * Tries several arrangements and keeps the first one the solver can finish
 * from the current tray, so a shuffle never strands the player if avoidable.
 */
export function shuffleBoard(state: GameState): GameState {
  if (state.status !== 'playing' || state.board.length < 2) return state;
  const rng = createRng(state.level.seed * 31 + state.moves * 7919 + state.shuffles + 1);
  const types = state.board.map((t) => t.type);

  let board = state.board;
  for (let attempt = 0; attempt < SHUFFLE_TRIES; attempt++) {
    const shuffled = rng.shuffle(types);
    board = state.board.map((t, i) => ({ ...t, type: shuffled[i] }));
    if (solve(board, { tray: state.tray, maxNodes: HELPER_SOLVER_BUDGET }).solvable) break;
  }
  return {
    ...state,
    board,
    shuffles: state.shuffles + 1,
    history: [...state.history, { board: state.board, tray: state.tray }],
  };
}

/**
 * Suggests a good tile to tap next, or null when the game is over.
 * Uses the solver's first move when it can find a full solution, otherwise a
 * simple "build towards a triple" heuristic.
 */
export function getHint(state: GameState): number | null {
  if (state.status !== 'playing') return null;
  const result = solve(state.board, { tray: state.tray, maxNodes: HELPER_SOLVER_BUDGET });
  if (result.solvable && result.moves.length > 0) return result.moves[0];

  const free = state.board.filter((t) => isFree(t, state.board));
  if (free.length === 0) return null;
  const inTray = new Map<number, number>();
  for (const t of state.tray) inTray.set(t.type, (inTray.get(t.type) ?? 0) + 1);
  const freeOfType = new Map<number, number>();
  for (const t of free) freeOfType.set(t.type, (freeOfType.get(t.type) ?? 0) + 1);

  const score = (t: Tile) =>
    (inTray.get(t.type) ?? 0) * 10 + Math.min(freeOfType.get(t.type) ?? 0, MATCH_SIZE) * 3;
  return free.reduce((best, t) => (score(t) > score(best) ? t : best), free[0]).id;
}
