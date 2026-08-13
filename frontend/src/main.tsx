import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { createBrowserRouter, RouterProvider } from "react-router-dom"
import { App, ProjectRoute, ChatRoute, IndexRoute } from "./app"
import "./index.css"

const queryClient = new QueryClient()

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <IndexRoute /> },
      { path: "projects/:projectId", element: <ProjectRoute /> },
      { path: "chats/:chatId", element: <ChatRoute /> },
    ],
  },
])

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
