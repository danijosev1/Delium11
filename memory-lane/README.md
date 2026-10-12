# Memory Lane

A calm triple-tile matching puzzle for iPhone and iPad, built with Expo (React Native) and TypeScript.

**Status:** step 1 — the puzzle engine plus one test screen with placeholder tiles.

## How to play

- Tiles sit in stacked layers. A tile is **free** when nothing on a higher layer overlaps it; covered tiles are dimmed.
- Tap a free tile to move it into the 7-slot tray. Matching tiles group together.
- Three of the same picture in the tray disappear.
- Clear the board and the tray to win. If the tray fills with 7 tiles and no triple, you can **Undo** or **Try again** (free).
- Helpers: **Undo**, **Shuffle** (mixes the pictures left on the board), **Hint** (outlines a good tile in orange).

## Project layout

```
src/engine/       Pure TypeScript, no React — testable on its own
  types.ts        Tile, Level, LevelParams, tray constants
  rng.ts          Seeded RNG (mulberry32)
  geometry.ts     Overlap / free-tile rules
  generator.ts    Difficulty curve + generateLevel(levelNumber)
  solver.ts       Depth-first solver with a node budget (never hangs)
  game.ts         Game state: pick, undo, shuffle, hint, restart
src/components/   Board, Tray, TileView, BigButton
src/screens/      GameScreen (level picker, board, tray, helpers)
src/theme/        Placeholder tile looks, colours, accessibility minimums
```

### Level generation

`generateLevel(n)` is deterministic (seed = `n`) and covers levels 1–1000:

| | Level 1 | Level 1000 |
|---|---|---|
| Picture types | 5 | 16 |
| Tiles (multiple of 3) | 21 | 120 |
| Layers | 1 | 5 |

- Levels 1–20 are tutorials (21–24 tiles, 1 layer, 5 pictures).
- Sawtooth curve: every 10th level is a breather (55% of the normal difficulty); every 50th is a celebration level at medium difficulty (78%).
- Levels are built **backwards**. The generator first writes a pick order that never lets the tray reach 7, then chooses a pyramid layout, digs it out in a random valid order (always taking a free tile), and labels each tile with the next picture in that pick order. Replaying the dig-out order wins, so every level can be solved.
- The solver then plays the level with the normal rules. If it can't clear the level within its budget, the generator rebuilds it with the seed offset (`n + attempt × 1,000,003`).

## Commands

```bash
npm install
npm test            # Jest: all 1000 levels solvable, deterministic, tile counts multiple of 3
npm run typecheck
npm run lint
npx expo start      # dev server, then scan the QR code with your iPhone
```

## Accessibility

- Board tiles are at least 64pt, and they grow on larger screens (up to 120pt on iPad).
- Buttons are at least 56pt tall, and all text is 18pt or larger.
- Every action is a single tap. There are no timers, drags or long-presses.
- Tiles and buttons have VoiceOver labels. Win and loss are announced.
- Tray slots are smaller than board tiles so all 7 fit across a phone screen. You can't tap them.
