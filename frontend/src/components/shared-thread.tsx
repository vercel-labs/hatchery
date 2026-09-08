import { TextPart } from "@/components/parts/text-part";
import { Marker, MarkerContent } from "@/components/ui/marker";
import type { SharedThread } from "@/lib/sharing";

export function SharedThreadLink({ sharing }: { sharing: SharedThread }) {
  const label = `${sharing.provider === "slack" ? "Slack" : "GitHub"}: ${sharing.label}`;
  return /^https?:\/\//i.test(sharing.url) ? (
    <a href={sharing.url} target="_blank" rel="noopener noreferrer">
      {label}
    </a>
  ) : (
    <span>{label}</span>
  );
}

export function SharedThreadNotification({
  sharing,
  fallback = false,
}: {
  sharing: SharedThread;
  fallback?: boolean;
}) {
  return (
    <>
      <Marker variant="separator" data-sharing-id={sharing.id}>
        <MarkerContent>
          Shared thread started · <SharedThreadLink sharing={sharing} />
          {fallback && (
            <span className="block">
              Saved notification · original tool position unavailable
            </span>
          )}
        </MarkerContent>
      </Marker>
      <TextPart text={sharing.text} role="assistant" preserveLineBreaks />
    </>
  );
}
