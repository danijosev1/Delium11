import { StyleSheet, View } from 'react-native';

import { TRAY_SIZE, type Tile } from '../engine';
import { COLORS } from '../theme/tiles';
import { TileView } from './TileView';

interface Props {
  tray: readonly Tile[];
  slotSize: number;
}

/** Seven slots. Tray tiles are display-only, so they may be a little smaller than board tiles. */
export function Tray({ tray, slotSize }: Props) {
  const label = tray.length === 0 ? 'Tray is empty' : `Tray holds ${tray.length} of ${TRAY_SIZE} tiles`;
  return (
    <View style={styles.row} accessible accessibilityLabel={label}>
      {Array.from({ length: TRAY_SIZE }, (_, i) => (
        <View key={i} style={[styles.slot, { width: slotSize, height: slotSize, borderRadius: slotSize * 0.16 }]}>
          {tray[i] && <TileView type={tray[i].type} size={slotSize} />}
        </View>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', gap: 4, justifyContent: 'center' },
  slot: { backgroundColor: COLORS.traySlot, alignItems: 'center', justifyContent: 'center' },
});
