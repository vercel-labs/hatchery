// Shell: project sidebar + routed content. Pages live in pages/.
import { useState } from "react"
import { Link, Outlet, useNavigate, useParams } from "react-router-dom"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import clsx from "clsx"
import { api } from "./api"
import { ProjectPage } from "./pages/project"
import { ChatPage } from "./pages/chat"

export function App() {
  return (
    <div className="flex h-screen">
      <Sidebar />
      <main className="min-w-0 flex-1 overflow-y-auto">
        <Outlet />
      </main>
    </div>
  )
}

export function IndexRoute() {
  const { data: projects } = useQuery({ queryKey: ["projects"], queryFn: api.projects })
  return (
    <div className="p-8 text-sm text-neutral-500">
      {projects?.length ? "pick a project" : "create a project to get started"}
    </div>
  )
}

export function ProjectRoute() {
  const { projectId } = useParams()
  return <ProjectPage key={projectId} projectId={projectId!} />
}

export function ChatRoute() {
  const { chatId } = useParams()
  return <ChatPage key={chatId} chatId={chatId!} />
}

function Sidebar() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { projectId } = useParams()
  const [name, setName] = useState("")
  const { data: projects } = useQuery({ queryKey: ["projects"], queryFn: api.projects })
  const create = useMutation({
    mutationFn: api.createProject,
    onSuccess: (project) => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] })
      setName("")
      void navigate(`/projects/${project.id}`)
    },
  })

  return (
    <aside className="flex w-56 shrink-0 flex-col border-r border-neutral-200 bg-white">
      <Link to="/" className="border-b border-neutral-200 px-4 py-3 font-semibold">
        factory
      </Link>
      <nav className="flex-1 overflow-y-auto p-2">
        {(projects ?? []).map((project) => (
          <Link
            key={project.id}
            to={`/projects/${project.id}`}
            className={clsx(
              "block rounded px-2 py-1.5 text-sm hover:bg-neutral-100",
              project.id === projectId && "bg-neutral-100 font-medium",
            )}
          >
            {project.name}
          </Link>
        ))}
      </nav>
      <form
        className="border-t border-neutral-200 p-2"
        onSubmit={(event) => {
          event.preventDefault()
          if (name.trim()) create.mutate(name.trim())
        }}
      >
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="new project..."
          className="w-full rounded border border-neutral-200 px-2 py-1 text-sm outline-none focus:border-neutral-400"
        />
      </form>
    </aside>
  )
}
