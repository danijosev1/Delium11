import {
  canUndo,
  createGame,
  freeTileIds,
  getHint,
  pickTile,
  restartGame,
  shuffleBoard,
  undo,
  type GameState,
} from './game';
import { generateLevel } from './generator';
import { solve } from './solver';
import type { Level, Tile } from './types';

/** Hand-made single-layer level: everything is free. */
function flatLevel(types: number[]): Level {
  const tiles: Tile[] = types.map((type, i) => ({ id: i, type, layer: 0, x: (i % 5) * 2, y: Math.floor(i / 5) * 2 }));
  return {
    levelNumber: 1,
    seed: 1,
    attempts: 1,
    params: { levelNumber: 1, kind: 'tutorial', difficulty: 0, tileTypes: 3, totalTiles: tiles.length, layers: 1, trayPressure: 3 },
    tiles,
    cols: 5,
    rows: 7,
  };
}

const pick = (state: GameState, id: number) => pickTile(state, id).state;

describe('game state', () => {
  test('free tiles: a covered tile cannot be picked', () => {
    const level = flatLevel([0, 0, 0]);
    level.tiles.push({ id: 3, type: 0, layer: 1, x: 1, y: 1 }); // covers tile 0
    const game = createGame(level);
    expect(freeTileIds(game).has(0)).toBe(false);
    expect(pick(game, 0)).toBe(game);
    expect(pick(game, 3).board).toHaveLength(3);
  });

  test('tray groups matching tiles and clears triples', () => {
    let game = createGame(flatLevel([0, 1, 0, 1, 0, 1]));
    game = pick(game, 0); // A
    game = pick(game, 1); // A B
    game = pick(game, 2); // A A B
    expect(game.tray.map((t) => t.type)).toEqual([0, 0, 1]);
    const result = pickTile(game, 4);
    expect(result.matched).toBe(true);
    expect(result.state.tray.map((t) => t.type)).toEqual([1]);
  });

  test('clearing the board and tray wins', () => {
    let game = createGame(flatLevel([2, 2, 2]));
    for (const id of [0, 1, 2]) game = pick(game, id);
    expect(game.status).toBe('won');
  });

  test('seven tiles without a triple loses; retry and undo recover', () => {
    let game = createGame(flatLevel([0, 0, 1, 1, 2, 2, 3, 3, 0, 1, 2, 3]));
    for (const id of [0, 1, 2, 3, 4, 5, 6]) game = pick(game, id);
    expect(game.tray).toHaveLength(7);
    expect(game.status).toBe('lost');
    expect(pick(game, 8)).toBe(game); // no moves once lost

    const undone = undo(game);
    expect(undone.status).toBe('playing');
    expect(undone.tray).toHaveLength(6);

    const retried = restartGame(game);
    expect(retried.status).toBe('playing');
    expect(retried.board).toHaveLength(12);
    expect(retried.tray).toHaveLength(0);
  });

  test('undo restores a cleared triple', () => {
    let game = createGame(flatLevel([0, 0, 0]));
    game = pick(pick(game, 0), 1);
    const beforeMatch = game;
    game = pick(game, 2);
    expect(canUndo(game)).toBe(false); // won
    game = pick(pick(createGame(flatLevel([0, 0, 0, 1, 1, 1])), 0), 1);
    const matched = pick(game, 2);
    expect(matched.tray).toHaveLength(0);
    const back = undo(matched);
    expect(back.tray.map((t) => t.id)).toEqual([0, 1]);
    expect(back.moves).toBe(2);
    expect(beforeMatch.tray).toHaveLength(2);
  });

  test('replaying the solver solution wins a generated level', () => {
    for (const n of [1, 37, 250, 777, 1000]) {
      const level = generateLevel(n);
      let game = createGame(level);
      for (const id of solve(level.tiles).moves) game = pick(game, id);
      expect(game.status).toBe('won');
    }
  });

  test('shuffle keeps positions and picture counts, and is undoable', () => {
    const game = pick(createGame(generateLevel(300)), generateLevel(300).tiles.at(-1)!.id);
    const shuffled = shuffleBoard(game);
    expect(shuffled.board.map(({ id, x, y, layer }) => [id, x, y, layer])).toEqual(
      game.board.map(({ id, x, y, layer }) => [id, x, y, layer]),
    );
    const sortedTypes = (s: GameState) => s.board.map((t) => t.type).sort((a, b) => a - b);
    expect(sortedTypes(shuffled)).toEqual(sortedTypes(game));
    expect(shuffleBoard(game)).toEqual(shuffled); // deterministic
    expect(undo(shuffled).board).toBe(game.board);
  });

  test('following hints always wins', () => {
    for (const n of [5, 120, 640, 999]) {
      let game = createGame(generateLevel(n));
      while (game.status === 'playing') {
        const hint = getHint(game);
        expect(hint).not.toBeNull();
        expect(freeTileIds(game).has(hint!)).toBe(true);
        game = pick(game, hint!);
      }
      expect(game.status).toBe('won');
    }
  });
});
