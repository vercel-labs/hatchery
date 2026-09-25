import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";
import { fencePreformattedBlocks } from "./preformatted";

const markdownComponents: Components = {
  a: ({ children, href, title }) => (
    <a
      href={href}
      title={title}
      className="font-medium text-blue-600 underline underline-offset-4 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
      target="_blank"
      rel="noreferrer"
    >
      {children}
    </a>
  ),
};
export function MarkdownText({
  text,
  compact = false,
  streaming = false,
  messageText = false,
}: {
  text: string;
  compact?: boolean;
  streaming?: boolean;
  messageText?: boolean;
}) {
  return (
    <div
      aria-label={streaming ? "Streaming response" : undefined}
      data-message-text={messageText ? true : undefined}
      className={cn(
        "wrap-anywhere [&>*:first-child]:mt-0 [&>*:last-child]:mb-0 [&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-4 [&_blockquote]:text-muted-foreground [&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-[0.875em] [&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-semibold [&_hr]:border-border [&_ol]:list-decimal [&_ol]:pl-6 [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:bg-muted [&_pre]:leading-5 [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_table]:w-full [&_table]:border-collapse [&_td]:border [&_td]:border-border [&_th]:border [&_th]:border-border [&_th]:bg-muted [&_th]:text-left [&_ul]:list-disc [&_ul]:pl-6",
        compact
          ? "text-xs leading-5 [&_blockquote]:my-2 [&_h1]:mb-2 [&_h1]:mt-3 [&_h1]:text-base [&_h2]:mb-1.5 [&_h2]:mt-3 [&_h2]:text-sm [&_h3]:mb-1.5 [&_h3]:mt-2 [&_hr]:my-3 [&_li]:my-0.5 [&_ol]:my-2 [&_p]:my-2 [&_pre]:my-2 [&_pre]:p-2.5 [&_table]:my-2 [&_td]:px-2 [&_td]:py-1 [&_th]:px-2 [&_th]:py-1 [&_ul]:my-2"
          : "text-sm leading-7 [&_blockquote]:my-3 [&_h1]:mb-3 [&_h1]:mt-5 [&_h1]:text-xl [&_h2]:mb-2 [&_h2]:mt-5 [&_h2]:text-lg [&_h3]:mb-2 [&_h3]:mt-4 [&_hr]:my-5 [&_li]:my-1 [&_ol]:my-3 [&_p]:my-3 [&_pre]:my-3 [&_pre]:p-3 [&_table]:my-3 [&_td]:px-3 [&_td]:py-1.5 [&_th]:px-3 [&_th]:py-1.5 [&_ul]:my-3",
      )}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={markdownComponents}
      >
        {fencePreformattedBlocks(text)}
      </ReactMarkdown>
    </div>
  );
}
