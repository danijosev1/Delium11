import { StyleSheet, View } from 'react-native';

import type { Tile } from '../engine';
import { lookFor } from '../theme/tiles';
import { TileView } from './TileView';

/** Upper layers shift slightly up-left so the stacks read as 3D. */
export const LAYER_LIFT = 4;

interface Props {
  board: readonly Tile[];
  freeIds: Set<number>;
  hintId: number | null;
  cols: number;
  rows: number;
  maxLayers: number;
  tileSize: number;
  onTilePress: (id: number) => void;
}

export function Board({ board, freeIds, hintId, cols, rows, maxLayers, tileSize, onTilePress }: Props) {
  const half = tileSize / 2;
  const lift = LAYER_LIFT * Math.max(0, maxLayers - 1);
  // Draw lower layers first so higher tiles appear on top.
  const ordered = [...board].sort((a, b) => a.layer - b.layer || a.y - b.y || a.x - b.x);
  return (
    <View style={{ width: cols * tileSize + lift, height: rows * tileSize + lift }}>
      {ordered.map((tile) => {
        const free = freeIds.has(tile.id);
        return (
          <View
            key={tile.id}
            style={[styles.slot, { left: tile.x * half + lift - tile.layer * LAYER_LIFT, top: tile.y * half + lift - tile.layer * LAYER_LIFT }]}
          >
            <TileView
              type={tile.type}
              size={tileSize}
              free={free}
              highlighted={tile.id === hintId}
              onPress={() => onTilePress(tile.id)}
              accessibilityLabel={`${lookFor(tile.type).name}${free ? '' : ', covered'}`}
            />
          </View>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  slot: { position: 'absolute' },
});
