import { Pressable, StyleSheet, Text } from 'react-native';

import { COLORS, MIN_BUTTON_HEIGHT, MIN_TEXT } from '../theme/tiles';

interface Props {
  label: string;
  onPress: () => void;
  disabled?: boolean;
  variant?: 'primary' | 'secondary';
  accessibilityHint?: string;
  flex?: boolean;
}

export function BigButton({ label, onPress, disabled, variant = 'primary', accessibilityHint, flex }: Props) {
  const primary = variant === 'primary';
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityHint={accessibilityHint}
      accessibilityState={{ disabled: !!disabled }}
      style={({ pressed }) => [
        styles.button,
        flex && styles.flex,
        { backgroundColor: disabled ? COLORS.disabled : primary ? COLORS.button : COLORS.buttonSecondary },
        pressed && !disabled && styles.pressed,
      ]}
    >
      <Text style={[styles.label, { color: primary || disabled ? COLORS.buttonText : COLORS.text }]}>{label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  button: {
    minHeight: MIN_BUTTON_HEIGHT,
    minWidth: MIN_BUTTON_HEIGHT,
    paddingHorizontal: 16,
    borderRadius: 14,
    alignItems: 'center',
    justifyContent: 'center',
  },
  flex: { flex: 1 },
  pressed: { opacity: 0.75 },
  label: { fontSize: MIN_TEXT + 2, fontWeight: '700' },
});
