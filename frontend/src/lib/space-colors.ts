export const ACCENT_COLORS = [
  "blue",
  "red",
  "amber",
  "green",
  "teal",
  "purple",
  "pink",
] as const;

export type AccentColor = (typeof ACCENT_COLORS)[number];

export function isAccentColor(color: string): color is AccentColor {
  return ACCENT_COLORS.includes(color as AccentColor);
}

export function resolveSpaceColor(color: string | undefined): string {
  if (!color) return "var(--muted-foreground)";
  return isAccentColor(color) ? `var(--space-accent-${color})` : color;
}
