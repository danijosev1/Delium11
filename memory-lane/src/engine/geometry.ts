import type { Tile } from './types';

/** Tiles are 2x2 half-cells, so they overlap when both offsets are under 2. */
export function overlaps(a: Tile, b: Tile): boolean {
  return Math.abs(a.x - b.x) < 2 && Math.abs(a.y - b.y) < 2;
}

/** True when `upper` sits on a higher layer and overlaps `lower`. */
export function covers(upper: Tile, lower: Tile): boolean {
  return upper.layer > lower.layer && overlaps(upper, lower);
}

/** A tile is free when no tile on a higher layer overlaps it. */
export function isFree(tile: Tile, board: readonly Tile[]): boolean {
  for (const other of board) {
    if (covers(other, tile)) return false;
  }
  return true;
}

export function freeTiles(board: readonly Tile[]): Tile[] {
  return board.filter((t) => isFree(t, board));
}
