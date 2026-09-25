export const ACCENT_FAMILIES = [
  "blue",
  "red",
  "amber",
  "green",
  "teal",
  "purple",
  "pink",
] as const;

export const ACCENT_SHADES = ["600", "700", "800", "900"] as const;

export const ACCENT_COLORS = ACCENT_FAMILIES.flatMap((family) =>
  ACCENT_SHADES.map((shade) => `${family}-${shade}` as AccentColor),
);

export type AccentFamily = (typeof ACCENT_FAMILIES)[number];
export type AccentShade = (typeof ACCENT_SHADES)[number];
export type AccentColor = `${AccentFamily}-${AccentShade}`;

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

export function resolveAgentColor(color: AccentColor | undefined): string {
  return color ? `var(--geist-${color})` : "var(--muted-foreground)";
}

export function resolveAgentForeground(family: AccentFamily): string {
  return `var(--geist-${family}-1000)`;
}
