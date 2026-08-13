// Typed client for the backend's /api routes (backend/app/api.py).

export type Project = {
  id: string
  name: string
  memory: string
  repos: string[]
  created_at: string
  updated_at: string
}

export type Chat = {
  id: string
  project_id: string
  title: string
  status: "active" | "archived"
  created_at: string
  updated_at: string
}

export type ChatEvent = {
  index: number
  type: string
  data: Record<string, unknown>
  meta: { id: string; at: string }
}

export type ProjectDetail = Project & { chats: Chat[] }
export type ChatDetail = Chat & { events: ChatEvent[] }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...init,
  })
  if (!response.ok) throw new Error(`${init?.method ?? "GET"} ${path} failed: ${response.status}`)
  return (await response.json()) as T
}

export const api = {
  projects: () => request<Project[]>("/api/projects"),
  createProject: (name: string) =>
    request<Project>("/api/projects", { method: "POST", body: JSON.stringify({ name }) }),
  project: (id: string) => request<ProjectDetail>(`/api/projects/${id}`),
  setMemory: (id: string, memory: string) =>
    request<Project>(`/api/projects/${id}/memory`, { method: "PUT", body: JSON.stringify({ memory }) }),
  setRepos: (id: string, repos: string[]) =>
    request<Project>(`/api/projects/${id}/repos`, { method: "PUT", body: JSON.stringify({ repos }) }),
  createChat: (projectId: string, title: string) =>
    request<Chat>("/api/chats", { method: "POST", body: JSON.stringify({ project_id: projectId, title }) }),
  chat: (id: string) => request<ChatDetail>(`/api/chats/${id}`),
  send: (id: string, message: string) =>
    request<{ ok: boolean }>(`/api/chats/${id}/messages`, { method: "POST", body: JSON.stringify({ message }) }),
  setStatus: (id: string, status: Chat["status"]) =>
    request<Chat>(`/api/chats/${id}/status`, { method: "POST", body: JSON.stringify({ status }) }),
}

// Tail a chat's event stream: ndjson over plain fetch, reconnecting with
// ?start=<next index> whenever the response ends (the backend caps each
// response; see app/api.py). Returns a stop function.
export function tailChat(chatId: string, startIndex: number, onEvent: (event: ChatEvent) => void): () => void {
  const controller = new AbortController()
  let next = startIndex

  const run = async () => {
    while (!controller.signal.aborted) {
      try {
        const response = await fetch(`/api/chats/${chatId}/stream?start=${next}`, {
          signal: controller.signal,
        })
        if (!response.ok || response.body === null) throw new Error(`stream ${response.status}`)
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ""
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split("\n")
          buffer = lines.pop() ?? ""
          for (const line of lines) {
            if (!line.trim()) continue
            const event = JSON.parse(line) as ChatEvent
            next = event.index + 1
            onEvent(event)
          }
        }
      } catch {
        if (controller.signal.aborted) return
      }
      await new Promise((resolve) => setTimeout(resolve, 1000))
    }
  }
  void run()
  return () => controller.abort()
}
