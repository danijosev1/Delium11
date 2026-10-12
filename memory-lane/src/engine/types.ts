/**
 * Board geometry uses "half-cell" units: a tile is 2 units wide and 2 units tall,
 * so tiles on upper layers can sit offset by half a tile (brick/pyramid style).
 */
export interface Tile {
  /** Unique within a level. */
  id: number;
  /** Picture type, 0-based. Three tiles of the same type make a match. */
  type: number;
  /** 0 = bottom layer. Higher layers cover lower ones. */
  layer: number;
  /** Left edge in half-cell units. */
  x: number;
  /** Top edge in half-cell units. */
  y: number;
}

export type LevelKind = 'tutorial' | 'normal' | 'breather' | 'celebration';

export interface LevelParams {
  levelNumber: number;
  kind: LevelKind;
  /** 0 (easiest) to 1 (hardest) after applying the sawtooth. */
  difficulty: number;
  tileTypes: number;
  /** Always a multiple of 3. */
  totalTiles: number;
  layers: number;
  /**
   * The most tiles the generator's built-in solution ever holds in the tray at once (3-6).
   * Higher means the intended solution needs more juggling.
   */
  trayPressure: number;
}

export interface Level {
  levelNumber: number;
  /** Seed actually used (levelNumber plus any retry offset). */
  seed: number;
  /** How many generation attempts were needed (1 = first try succeeded). */
  attempts: number;
  params: LevelParams;
  tiles: Tile[];
  /** Board width/height in whole tiles. */
  cols: number;
  rows: number;
}

export const TRAY_SIZE = 7;
export const MATCH_SIZE = 3;
