"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowLeftIcon, BookOpenIcon, MessageSquareIcon } from "lucide-react";

import type { Chat, Project } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty";
import { Separator } from "@/components/ui/separator";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSkeleton,
  SidebarProvider,
  SidebarSeparator,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import { Skeleton } from "@/components/ui/skeleton";

const statusVariant = {
  queued: "outline",
  running: "default",
  done: "secondary",
  failed: "destructive",
} as const;

export function ProjectView({ projectId }: { projectId: string }) {
  const [project, setProject] = useState<Project | null>(null);
  const [chats, setChats] = useState<Chat[] | null>(null);
  // null = project overview
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    fetch(`/api/projects/${projectId}`)
      .then((r) => (r.ok ? r.json() : null))
      .then(setProject);
    fetch(`/api/projects/${projectId}/chats`)
      .then((r) => r.json())
      .then(setChats);
  }, [projectId]);

  const selected = chats?.find((chat) => chat.id === selectedId) ?? null;

  return (
    <SidebarProvider>
      <Sidebar>
        <SidebarHeader>
          <Button
            variant="ghost"
            className="w-full justify-start gap-2"
            nativeButton={false}
            render={<Link href="/" />}
          >
            <ArrowLeftIcon data-icon="inline-start" />
            {project?.name ?? "…"}
          </Button>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton
                isActive={selectedId === null}
                onClick={() => setSelectedId(null)}
              >
                <BookOpenIcon />
                <span>Project overview</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarHeader>
        <SidebarSeparator />

        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupLabel>Chats</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {chats === null ? (
                  Array.from({ length: 3 }).map((_, i) => (
                    <SidebarMenuItem key={i}>
                      <SidebarMenuSkeleton />
                    </SidebarMenuItem>
                  ))
                ) : chats.length === 0 ? (
                  <div className="px-2 py-4 text-center text-sm text-muted-foreground">
                    No chats yet
                  </div>
                ) : (
                  chats.map((chat) => (
                    <SidebarMenuItem key={chat.id}>
                      <SidebarMenuButton
                        isActive={chat.id === selectedId}
                        onClick={() => setSelectedId(chat.id)}
                        tooltip={chat.title}
                      >
                        <MessageSquareIcon />
                        <span className="truncate">{chat.title}</span>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  ))
                )}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
      </Sidebar>

      <SidebarInset>
        <header className="flex h-14 items-center gap-2 border-b px-4">
          <SidebarTrigger />
          <Separator orientation="vertical" className="h-4" />
          <span className="text-sm font-medium">
            {selected ? selected.title : "Project overview"}
          </span>
        </header>
        <div className="flex-1 overflow-y-auto p-6">
          {selected ? (
            <ChatPane chat={selected} />
          ) : (
            <OverviewPane project={project} />
          )}
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}

function OverviewPane({ project }: { project: Project | null }) {
  if (project === null) {
    return <Skeleton className="h-48 w-full max-w-2xl rounded-xl" />;
  }
  return (
    <Card className="max-w-2xl">
      <CardHeader>
        <CardTitle>{project.name}</CardTitle>
        <CardDescription>{project.goal}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4 text-sm">
        <div className="flex flex-wrap gap-2">
          {project.repos.map((repo) => (
            <Badge key={repo} variant="outline">
              {repo}
            </Badge>
          ))}
        </div>
        <p className="text-muted-foreground">
          created {new Date(project.created_at).toLocaleDateString()}
        </p>
      </CardContent>
    </Card>
  );
}

function ChatPane({ chat }: { chat: Chat }) {
  return (
    <div className="flex max-w-2xl flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={statusVariant[chat.status]}>{chat.status}</Badge>
        <Badge variant="outline">{chat.trigger}</Badge>
        {chat.sandbox_id && <Badge variant="outline">{chat.sandbox_id}</Badge>}
        <span className="text-sm text-muted-foreground">
          {new Date(chat.created_at).toLocaleString()}
        </span>
      </div>
      {chat.artifact ? (
        <Card>
          <CardHeader>
            <CardTitle>Artifact</CardTitle>
          </CardHeader>
          <CardContent className="text-sm">
            {chat.artifact.startsWith("http") ? (
              <a
                href={chat.artifact}
                target="_blank"
                rel="noreferrer"
                className="underline underline-offset-4"
              >
                {chat.artifact}
              </a>
            ) : (
              <p className="text-muted-foreground">{chat.artifact}</p>
            )}
          </CardContent>
        </Card>
      ) : (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>No artifact yet</EmptyTitle>
            <EmptyDescription>
              This chat has not produced a report, issue, or PR.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}
    </div>
  );
}
