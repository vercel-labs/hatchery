import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

export function TextPart({
  text,
  role,
  preserveLineBreaks = false,
  className,
}: {
  text: string;
  role: string;
  preserveLineBreaks?: boolean;
  className?: string;
}) {
  if (!text.trim()) return null;

  return (
    <div
      data-message-role={role}
      className={cn("typeset typeset-docs min-w-0 px-1.5", className)}
    >
      <ReactMarkdown
        remarkPlugins={
          preserveLineBreaks ? [remarkGfm, remarkBreaks] : [remarkGfm]
        }
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
