// Chat view: the event stream reduced to messages, live-tailed from the
// backend. Messages from slack/github arrive wrapped in attribution tags
// (<slack_message ...>, <github_context ...>); we show the inner text with a
// channel badge.
import { useEffect, useRef, useState } from "react"
import { Link } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import clsx from "clsx"
import { api, tailChat, type ChatEvent } from "../api"

type Message = { role: "user" | "assistant"; text: string; channel?: string; error?: boolean }

function reduce(events: ChatEvent[]): { messages: Message[]; status: string | null } {
  const messages: Message[] = []
  let status: string | null = null
  for (const event of events) {
    if (event.type === "message.received") {
      messages.push({ role: "user", ...unwrap(String(event.data.message ?? "")), channel: channelOf(event) })
      status = "working..."
    } else if (event.type === "message.completed") {
      messages.push({ role: "assistant", text: String(event.data.message ?? "") })
      status = null
    } else if (event.type === "status.updated") {
      status = String(event.data.status ?? "")
    } else if (event.type === "turn.failed") {
      messages.push({ role: "assistant", text: `something went wrong: ${String(event.data.error ?? "")}`, error: true })
      status = null
    } else if (event.type === "turn.completed") {
      status = null
    }
  }
  return { messages, status }
}

function channelOf(event: ChatEvent): string | undefined {
  const channel = event.data.channel
  return typeof channel === "string" && channel !== "ui" ? channel : undefined
}

function unwrap(text: string): { text: string } {
  const match = /^<(slack_message|github_context)[^>]*>\n([\s\S]*)\n<\/\1>$/.exec(text.trim())
  return { text: match ? match[2] : text }
}

export function ChatPage({ chatId }: { chatId: string }) {
  const queryClient = useQueryClient()
  const { data: chat } = useQuery({ queryKey: ["chat", chatId], queryFn: () => api.chat(chatId) })
  const [live, setLive] = useState<ChatEvent[]>([])
  const [draft, setDraft] = useState("")
  const bottom = useRef<HTMLDivElement>(null)

  // tail from the end of the fetched log; dedupe on index against it
  const loadedCount = chat?.events.length
  useEffect(() => {
    if (loadedCount === undefined) return
    return tailChat(chatId, loadedCount, (event) => {
      setLive((current) => (current.some((e) => e.index === event.index) ? current : [...current, event]))
    })
  }, [chatId, loadedCount])

  const events = [...(chat?.events ?? []), ...live.filter((e) => e.index >= (loadedCount ?? 0))]
  const { messages, status } = reduce(events)

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages.length, status])

  const send = useMutation({
    mutationFn: (message: string) => api.send(chatId, message),
    onSuccess: () => setDraft(""),
  })
  const toggleArchive = useMutation({
    mutationFn: () => api.setStatus(chatId, chat?.status === "archived" ? "active" : "archived"),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["chat", chatId] }),
  })

  if (!chat) return <div className="p-8 text-sm text-neutral-500">loading...</div>

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-neutral-200 bg-white px-4 py-2">
        <div className="min-w-0">
          <h1 className="truncate font-medium">{chat.title}</h1>
          <Link to={`/projects/${chat.project_id}`} className="text-xs text-neutral-400 hover:underline">
            back to project
          </Link>
        </div>
        <button
          onClick={() => toggleArchive.mutate()}
          className="shrink-0 rounded border border-neutral-300 px-2 py-1 text-xs hover:bg-neutral-100"
        >
          {chat.status === "archived" ? "reactivate" : "archive"}
        </button>
      </header>

      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {messages.length === 0 && <div className="text-sm text-neutral-400">no messages yet</div>}
        {messages.map((message, index) => (
          <div key={index} className={clsx("flex", message.role === "user" ? "justify-end" : "justify-start")}>
            <div
              className={clsx(
                "max-w-[80%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap",
                message.role === "user" ? "bg-neutral-900 text-white" : "border border-neutral-200 bg-white",
                message.error && "border-red-300 bg-red-50 text-red-800",
              )}
            >
              {message.channel && (
                <div className="mb-1 text-[10px] font-medium tracking-wide uppercase opacity-60">
                  via {message.channel}
                </div>
              )}
              {message.text}
            </div>
          </div>
        ))}
        {status && <div className="text-xs text-neutral-400 italic">{status}</div>}
        <div ref={bottom} />
      </div>

      <form
        className="border-t border-neutral-200 bg-white p-3"
        onSubmit={(event) => {
          event.preventDefault()
          if (draft.trim() && !send.isPending) send.mutate(draft.trim())
        }}
      >
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={chat.status === "archived" ? "archived chat" : "message the factory..."}
          disabled={chat.status === "archived"}
          className="w-full rounded border border-neutral-200 px-3 py-2 text-sm outline-none focus:border-neutral-400 disabled:bg-neutral-50"
        />
      </form>
    </div>
  )
}
