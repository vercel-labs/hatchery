import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function TextPart({ text, role }: { text: string; role: string }) {
  if (!text.trim()) return null;

  return (
    <div
      data-message-role={role}
      className="typeset typeset-docs min-w-0 px-1.5"
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}
