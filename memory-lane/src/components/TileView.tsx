import { memo } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { COLORS, lookFor } from '../theme/tiles';

interface Props {
  type: number;
  size: number;
  free?: boolean;
  highlighted?: boolean;
  onPress?: () => void;
  accessibilityLabel?: string;
}

/** A coloured square with a large emoji. Blocked tiles are dimmed and not tappable. */
export const TileView = memo(function TileView({ type, size, free = true, highlighted, onPress, accessibilityLabel }: Props) {
  const look = lookFor(type);
  return (
    <Pressable
      onPress={onPress}
      disabled={!onPress || !free}
      accessible={!!onPress}
      accessibilityRole={onPress ? 'button' : 'image'}
      accessibilityLabel={accessibilityLabel ?? look.name}
      accessibilityState={{ disabled: !free }}
      style={[
        styles.tile,
        {
          width: size,
          height: size,
          borderRadius: size * 0.16,
          backgroundColor: look.color,
          borderWidth: highlighted ? 5 : 2,
          borderColor: highlighted ? COLORS.hint : COLORS.tileBorder,
        },
      ]}
    >
      <Text style={{ fontSize: size * 0.55 }} allowFontScaling={false}>
        {look.emoji}
      </Text>
      {!free && <View pointerEvents="none" style={[StyleSheet.absoluteFill, styles.blocked, { borderRadius: size * 0.14 }]} />}
    </Pressable>
  );
});

const styles = StyleSheet.create({
  tile: {
    alignItems: 'center',
    justifyContent: 'center',
    shadowColor: '#000',
    shadowOpacity: 0.25,
    shadowRadius: 2,
    shadowOffset: { width: 1, height: 2 },
  },
  blocked: { backgroundColor: COLORS.blockedOverlay },
});
