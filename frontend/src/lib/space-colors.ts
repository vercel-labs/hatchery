export const ACCENT_FAMILIES = [
  "blue",
  "red",
  "amber",
  "green",
  "teal",
  "purple",
  "pink",
] as const;

export const ACCENT_SHADES = ["700", "900"] as const;

export const ACCENT_COLORS = ACCENT_FAMILIES.flatMap((family) =>
  ACCENT_SHADES.map((shade) => `${family}-${shade}` as AccentColor),
);

export type AccentFamily = (typeof ACCENT_FAMILIES)[number];
export type AccentShade = (typeof ACCENT_SHADES)[number];
export type AccentColor = `${AccentFamily}-${AccentShade}`;

export function isAccentColor(color: string): color is AccentColor {
  return ACCENT_COLORS.includes(color as AccentColor);
}

export function normalizeAccentColor(color: string): AccentColor | null {
  if (isAccentColor(color)) return color;
  return ACCENT_FAMILIES.includes(color as AccentFamily)
    ? `${color as AccentFamily}-700`
    : null;
}

export function splitAccentColor(
  color: AccentColor | null,
): { family: AccentFamily; shade: AccentShade } | null {
  if (!color) return null;
  const [family, shade] = color.split("-");
  return { family: family as AccentFamily, shade: shade as AccentShade };
}

export function accentColor(
  family: AccentFamily,
  shade: AccentShade,
): AccentColor {
  return `${family}-${shade}`;
}

export function resolveSpaceColor(color: string | undefined): string {
  if (!color) return "var(--muted-foreground)";
  const normalized = normalizeAccentColor(color);
  return normalized ? `var(--geist-${normalized})` : color;
}

export function resolveSpaceForeground(family: AccentFamily): string {
  return `var(--geist-${family}-1000)`;
}
