"use client";

import { CheckIcon } from "lucide-react";

import {
  ACCENT_FAMILIES,
  ACCENT_SHADES,
  accentColor,
  type AccentColor,
  type AccentFamily,
  type AccentShade,
  resolveSpaceColor,
  resolveSpaceForeground,
  splitAccentColor,
} from "@/lib/space-colors";
import { cn } from "@/lib/utils";
import {
  ToggleGroup,
  ToggleGroupItem,
} from "@/components/ui/toggle-group";

const SHADE_NAMES: Record<AccentShade, string> = {
  "700": "Solid",
  "900": "Adaptive",
};

export function SpaceColorPicker({
  value,
  onValueChange,
  label = "Accent color",
  allowUnselected = false,
}: {
  value: AccentColor | null;
  onValueChange: (value: AccentColor | null) => void;
  label?: string;
  allowUnselected?: boolean;
}) {
  const selected = splitAccentColor(value);

  return (
    <div className="flex flex-col gap-2">
      <ToggleGroup
        aria-label={`${label} hue`}
        value={selected ? [selected.family] : []}
        onValueChange={(families) => {
          const family = families[0] as AccentFamily | undefined;
          if (family) {
            onValueChange(accentColor(family, selected?.shade ?? "700"));
          } else if (allowUnselected) {
            onValueChange(null);
          }
        }}
        className="flex-wrap"
      >
        {ACCENT_FAMILIES.map((family) => {
          const isSelected = selected?.family === family;
          const name = family[0].toUpperCase() + family.slice(1);
          const preview = accentColor(family, selected?.shade ?? "700");
          return (
            <ToggleGroupItem
              key={family}
              value={family}
              aria-label={`${name} hue`}
              title={name}
              className={cn(
                "size-8 rounded-full p-0",
                isSelected &&
                  "ring-2 ring-ring ring-offset-2 ring-offset-background",
              )}
            >
              <span
                className="flex size-5 items-center justify-center rounded-full"
                style={{
                  backgroundColor: resolveSpaceColor(preview),
                  color: resolveSpaceForeground(family),
                }}
              >
                {isSelected && <CheckIcon aria-hidden="true" />}
              </span>
              <span className="sr-only">{name}</span>
            </ToggleGroupItem>
          );
        })}
      </ToggleGroup>

      <ToggleGroup
        aria-label={`${label} shade`}
        value={selected ? [selected.shade] : []}
        onValueChange={(shades) => {
          const shade = shades[0] as AccentShade | undefined;
          if (selected && shade) {
            onValueChange(accentColor(selected.family, shade));
          }
        }}
        variant="outline"
        size="sm"
        spacing={0}
      >
        {ACCENT_SHADES.map((shade) => (
          <ToggleGroupItem
            key={shade}
            value={shade}
            disabled={!selected}
            aria-label={`${SHADE_NAMES[shade]} shade, ${shade}`}
            title={`${SHADE_NAMES[shade]} (${shade})`}
          >
            {SHADE_NAMES[shade]} ({shade})
          </ToggleGroupItem>
        ))}
      </ToggleGroup>

      <span className="flex items-center gap-2 text-xs text-muted-foreground" aria-live="polite">
        <span
          className="size-3 shrink-0 rounded-full"
          style={{
            backgroundColor: value
              ? resolveSpaceColor(value)
              : "var(--muted-foreground)",
          }}
        />
        {selected
          ? `Preview: ${selected.family[0].toUpperCase() + selected.family.slice(1)} ${SHADE_NAMES[selected.shade]} (${selected.shade})`
          : "No accent selected — a color will be chosen automatically"}
      </span>
    </div>
  );
}
