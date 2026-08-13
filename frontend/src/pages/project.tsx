// Project view: memory, attached repos, and the project's chats.
import { useEffect, useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { api, type Chat } from "../api"

export function ProjectPage({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { data: project } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.project(projectId),
  })
  const [memory, setMemory] = useState("")
  const [repos, setRepos] = useState("")
  useEffect(() => {
    if (project) {
      setMemory(project.memory)
      setRepos(project.repos.join("\n"))
    }
  }, [project?.updated_at]) // eslint-disable-line react-hooks/exhaustive-deps

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["project", projectId] })
  const saveMemory = useMutation({
    mutationFn: () => api.setMemory(projectId, memory),
    onSuccess: invalidate,
  })
  const saveRepos = useMutation({
    mutationFn: () => api.setRepos(projectId, repos.split("\n").map((r) => r.trim()).filter(Boolean)),
    onSuccess: invalidate,
  })
  const newChat = useMutation({
    mutationFn: () => api.createChat(projectId, "new chat"),
    onSuccess: (chat) => void navigate(`/chats/${chat.id}`),
  })

  if (!project) return <div className="p-8 text-sm text-neutral-500">loading...</div>
  const active = project.chats.filter((chat) => chat.status === "active")
  const archived = project.chats.filter((chat) => chat.status === "archived")

  return (
    <div className="mx-auto max-w-3xl space-y-8 p-8">
      <header className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">{project.name}</h1>
        <button
          onClick={() => newChat.mutate()}
          className="rounded bg-neutral-900 px-3 py-1.5 text-sm text-white hover:bg-neutral-700"
        >
          new chat
        </button>
      </header>

      <section>
        <SectionTitle
          title="memory"
          hint="current state and direction; the agent reads this every turn"
        />
        <textarea
          value={memory}
          onChange={(event) => setMemory(event.target.value)}
          rows={8}
          placeholder="what is this project about, where is it heading..."
          className="w-full rounded border border-neutral-200 bg-white p-3 font-mono text-sm outline-none focus:border-neutral-400"
        />
        <SaveButton
          dirty={memory !== project.memory}
          saving={saveMemory.isPending}
          onClick={() => saveMemory.mutate()}
        />
      </section>

      <section>
        <SectionTitle title="repos" hint="one owner/repo per line; github chats route here" />
        <textarea
          value={repos}
          onChange={(event) => setRepos(event.target.value)}
          rows={3}
          placeholder={"vercel/workflow\nvercel/vercel-py"}
          className="w-full rounded border border-neutral-200 bg-white p-3 font-mono text-sm outline-none focus:border-neutral-400"
        />
        <SaveButton
          dirty={repos !== project.repos.join("\n")}
          saving={saveRepos.isPending}
          onClick={() => saveRepos.mutate()}
        />
      </section>

      <section>
        <SectionTitle title="chats" hint="pings from slack and github land here too" />
        <ChatList chats={active} empty="no active chats" />
        {archived.length > 0 && (
          <>
            <div className="mt-4 mb-1 text-xs font-medium text-neutral-400 uppercase">archived</div>
            <ChatList chats={archived} empty="" />
          </>
        )}
      </section>
    </div>
  )
}

function SectionTitle({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="mb-2 flex items-baseline gap-2">
      <h2 className="font-medium">{title}</h2>
      <span className="text-xs text-neutral-400">{hint}</span>
    </div>
  )
}

function SaveButton({ dirty, saving, onClick }: { dirty: boolean; saving: boolean; onClick: () => void }) {
  if (!dirty) return null
  return (
    <button
      onClick={onClick}
      disabled={saving}
      className="mt-1 rounded border border-neutral-300 px-2 py-1 text-xs hover:bg-neutral-100"
    >
      {saving ? "saving..." : "save"}
    </button>
  )
}

function ChatList({ chats, empty }: { chats: Chat[]; empty: string }) {
  if (chats.length === 0)
    return empty ? <div className="text-sm text-neutral-400">{empty}</div> : null
  return (
    <ul className="divide-y divide-neutral-100 rounded border border-neutral-200 bg-white">
      {chats.map((chat) => (
        <li key={chat.id}>
          <Link to={`/chats/${chat.id}`} className="flex items-baseline justify-between px-3 py-2 hover:bg-neutral-50">
            <span className="truncate text-sm">{chat.title}</span>
            <span className="ml-2 shrink-0 text-xs text-neutral-400">
              {new Date(chat.updated_at).toLocaleString()}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  )
}
