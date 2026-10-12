import { useEffect, useMemo, useState } from 'react';
import { AccessibilityInfo, ScrollView, StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { BigButton } from '../components/BigButton';
import { Board, LAYER_LIFT } from '../components/Board';
import { Tray } from '../components/Tray';
import {
  canUndo,
  createGame,
  freeTileIds,
  generateLevel,
  getHint,
  MAX_LEVEL,
  MIN_LEVEL,
  pickTile,
  restartGame,
  shuffleBoard,
  TRAY_SIZE,
  undo,
  type GameState,
  type LevelKind,
} from '../engine';
import { COLORS, MIN_TEXT, MIN_TILE } from '../theme/tiles';

const GUTTER = 16;
const KIND_LABEL: Record<LevelKind, string> = {
  tutorial: 'Tutorial',
  normal: '',
  breather: 'Breather level',
  celebration: '🎉 Celebration level',
};

const clampLevel = (n: number) => Math.min(MAX_LEVEL, Math.max(MIN_LEVEL, n));

export function GameScreen() {
  const { width, height } = useWindowDimensions();
  const [game, setGame] = useState<GameState>(() => createGame(generateLevel(MIN_LEVEL)));
  const [hintId, setHintId] = useState<number | null>(null);
  const { level } = game;
  const levelNumber = level.levelNumber;

  useEffect(() => {
    if (game.status === 'won') AccessibilityInfo.announceForAccessibility('Well done! Level complete.');
    if (game.status === 'lost') AccessibilityInfo.announceForAccessibility('The tray is full. You can undo or try again.');
  }, [game.status]);

  const freeIds = useMemo(() => freeTileIds(game), [game]);

  // Board tiles: as large as the screen allows, never below 64pt.
  const lift = LAYER_LIFT * Math.max(0, level.params.layers - 1);
  const byWidth = (width - 2 * GUTTER - lift) / level.cols;
  const byHeight = (height * 0.6) / level.rows;
  const tileSize = Math.floor(Math.max(MIN_TILE, Math.min(byWidth, byHeight, 120)));
  const traySlot = Math.floor(Math.min(tileSize, (width - 2 * GUTTER - (TRAY_SIZE - 1) * 4) / TRAY_SIZE));

  const update = (next: GameState) => {
    setGame(next);
    setHintId(null);
  };
  const goToLevel = (n: number) => update(createGame(generateLevel(clampLevel(n))));

  const kindLabel = KIND_LABEL[level.params.kind];
  const playing = game.status === 'playing';

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'left', 'right', 'bottom']}>
      <ScrollView contentContainerStyle={styles.content}>
        <Text style={styles.title} accessibilityRole="header">
          Level {levelNumber}
        </Text>
        <Text style={styles.subtitle}>
          {[kindLabel, `${level.tiles.length} tiles`, `${level.params.tileTypes} pictures`, `${level.params.layers} layer${level.params.layers > 1 ? 's' : ''}`]
            .filter(Boolean)
            .join(' · ')}
        </Text>

        <View style={styles.row}>
          <BigButton label="−10" variant="secondary" onPress={() => goToLevel(levelNumber - 10)} disabled={levelNumber <= MIN_LEVEL} accessibilityHint="Go back ten levels" flex />
          <BigButton label="−1" variant="secondary" onPress={() => goToLevel(levelNumber - 1)} disabled={levelNumber <= MIN_LEVEL} accessibilityHint="Previous level" flex />
          <BigButton label="+1" variant="secondary" onPress={() => goToLevel(levelNumber + 1)} disabled={levelNumber >= MAX_LEVEL} accessibilityHint="Next level" flex />
          <BigButton label="+10" variant="secondary" onPress={() => goToLevel(levelNumber + 10)} disabled={levelNumber >= MAX_LEVEL} accessibilityHint="Skip ahead ten levels" flex />
        </View>

        <View style={styles.boardWrap}>
          <Board
            board={game.board}
            freeIds={freeIds}
            hintId={hintId}
            cols={level.cols}
            rows={level.rows}
            maxLayers={level.params.layers}
            tileSize={tileSize}
            onTilePress={(id) => playing && update(pickTile(game, id).state)}
          />
          {game.status !== 'playing' && (
            <View style={styles.overlay}>
              <Text style={[styles.banner, { color: game.status === 'won' ? COLORS.win : COLORS.lose }]} accessibilityRole="header">
                {game.status === 'won' ? 'Well done!' : 'The tray is full'}
              </Text>
              {game.status === 'won' ? (
                <BigButton label="Next level" onPress={() => goToLevel(levelNumber + 1)} disabled={levelNumber >= MAX_LEVEL} />
              ) : (
                <>
                  <BigButton label="Try again (free)" onPress={() => update(restartGame(game))} />
                  <BigButton label="Undo last tile" variant="secondary" onPress={() => update(undo(game))} />
                </>
              )}
            </View>
          )}
        </View>

        <Text style={styles.label}>Tray</Text>
        <Tray tray={game.tray} slotSize={traySlot} />

        <View style={styles.row}>
          <BigButton label="Undo" variant="secondary" onPress={() => update(undo(game))} disabled={!canUndo(game)} accessibilityHint="Put the last tile back" flex />
          <BigButton label="Shuffle" variant="secondary" onPress={() => update(shuffleBoard(game))} disabled={!playing} accessibilityHint="Mix up the pictures left on the board" flex />
          <BigButton label="Hint" variant="secondary" onPress={() => setHintId(getHint(game))} disabled={!playing} accessibilityHint="Highlights a good tile to tap" flex />
        </View>

        <BigButton label="Next ▶" onPress={() => goToLevel(levelNumber + 1)} disabled={levelNumber >= MAX_LEVEL} accessibilityHint="Go to the next level" />
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.background },
  content: { paddingHorizontal: GUTTER, paddingVertical: 12, gap: 12, alignItems: 'stretch' },
  title: { fontSize: 30, fontWeight: '800', color: COLORS.text, textAlign: 'center' },
  subtitle: { fontSize: MIN_TEXT, color: COLORS.mutedText, textAlign: 'center' },
  label: { fontSize: MIN_TEXT, fontWeight: '700', color: COLORS.text },
  row: { flexDirection: 'row', gap: 8 },
  boardWrap: { alignItems: 'center', justifyContent: 'center', paddingVertical: 8 },
  overlay: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: 'rgba(251, 247, 240, 0.92)',
    alignItems: 'stretch',
    justifyContent: 'center',
    gap: 16,
    paddingHorizontal: 24,
    borderRadius: 16,
  },
  banner: { fontSize: 34, fontWeight: '800', textAlign: 'center' },
});
