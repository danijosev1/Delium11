/** Placeholder art: a big emoji on a coloured square, one per picture type. */
export interface TileLook {
  emoji: string;
  name: string;
  color: string;
}

export const TILE_LOOKS: TileLook[] = [
  { emoji: '🍎', name: 'Apple', color: '#FFD6D6' },
  { emoji: '🌻', name: 'Sunflower', color: '#FFF1B8' },
  { emoji: '🐟', name: 'Fish', color: '#CDE8FF' },
  { emoji: '⭐', name: 'Star', color: '#FFE7A8' },
  { emoji: '🎈', name: 'Balloon', color: '#FFCFE3' },
  { emoji: '🍋', name: 'Lemon', color: '#F4FFB8' },
  { emoji: '🐞', name: 'Ladybird', color: '#FFDCCF' },
  { emoji: '🌙', name: 'Moon', color: '#DCD6FF' },
  { emoji: '🍇', name: 'Grapes', color: '#E8D4FF' },
  { emoji: '🐢', name: 'Turtle', color: '#D2F5D2' },
  { emoji: '☕', name: 'Tea cup', color: '#EADBCB' },
  { emoji: '🌈', name: 'Rainbow', color: '#D4F1F4' },
  { emoji: '🦋', name: 'Butterfly', color: '#D6E4FF' },
  { emoji: '🍀', name: 'Clover', color: '#C9F2D9' },
  { emoji: '🎵', name: 'Music', color: '#F0D9F7' },
  { emoji: '🐝', name: 'Bee', color: '#FFEFC2' },
];

export const lookFor = (type: number): TileLook => TILE_LOOKS[type % TILE_LOOKS.length];

export const COLORS = {
  background: '#FBF7F0',
  text: '#2B2622',
  mutedText: '#5C534B',
  tileBorder: '#8A7B6C',
  blockedOverlay: 'rgba(60, 50, 40, 0.45)',
  hint: '#E07A00',
  button: '#3D6B9E',
  buttonText: '#FFFFFF',
  buttonSecondary: '#E7E0D5',
  disabled: '#B9B1A6',
  traySlot: '#EFE8DC',
  win: '#2E7D4F',
  lose: '#B23B3B',
};

/** Accessibility minimums (points). */
export const MIN_TILE = 64;
export const MIN_BUTTON_HEIGHT = 56;
export const MIN_TEXT = 18;
