"use client";

import { useEffect, useState } from "react";

import type { Chat, Space } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
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
const statusVariant = {
  queued: "outline",
  running: "default",
  done: "secondary",
  failed: "destructive",
} as const;

type Selection =
  | { kind: "space"; id: string }
  | { kind: "chat"; id: string }
  | null;

function Dot({ color }: { color: string | undefined }) {
  return (
    <span
      className="size-2 shrink-0 rounded-full"
      style={{ backgroundColor: color ?? "var(--muted-foreground)" }}
    />
  );
}

export default function Home() {
  const [spaces, setSpaces] = useState<Space[] | null>(null);
  const [chats, setChats] = useState<Chat[] | null>(null);
  const [selection, setSelection] = useState<Selection>(null);

  useEffect(() => {
    fetch("/api/spaces")
      .then((r) => r.json())
      .then(setSpaces);
    fetch("/api/chats")
      .then((r) => r.json())
      .then(setChats);
  }, []);

  const colorOf = (spaceId: string) =>
    spaces?.find((s) => s.id === spaceId)?.color;

  const selectedSpace =
    selection?.kind === "space"
      ? (spaces?.find((s) => s.id === selection.id) ?? null)
      : null;
  const selectedChat =
    selection?.kind === "chat"
      ? (chats?.find((c) => c.id === selection.id) ?? null)
      : null;

  return (
    <SidebarProvider>
      <Sidebar>
        <SidebarHeader className="px-4 py-3">
          <span className="text-sm font-semibold">fabricator</span>
          <span className="text-xs text-muted-foreground">
            a software factory
          </span>
        </SidebarHeader>
        <SidebarSeparator />

        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupLabel>Spaces</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {spaces === null
                  ? Array.from({ length: 2 }).map((_, i) => (
                      <SidebarMenuItem key={i}>
                        <SidebarMenuSkeleton />
                      </SidebarMenuItem>
                    ))
                  : spaces.map((space) => (
                      <SidebarMenuItem key={space.id}>
                        <SidebarMenuButton
                          isActive={selectedSpace?.id === space.id}
                          onClick={() =>
                            setSelection({ kind: "space", id: space.id })
                          }
                          tooltip={space.name}
                        >
                          <Dot color={space.color} />
                          <span className="truncate">{space.name}</span>
                        </SidebarMenuButton>
                      </SidebarMenuItem>
                    ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>

          <SidebarGroup>
            <SidebarGroupLabel>Chats</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {chats === null
                  ? Array.from({ length: 4 }).map((_, i) => (
                      <SidebarMenuItem key={i}>
                        <SidebarMenuSkeleton />
                      </SidebarMenuItem>
                    ))
                  : chats.map((chat) => (
                      <SidebarMenuItem key={chat.id}>
                        <SidebarMenuButton
                          isActive={selectedChat?.id === chat.id}
                          onClick={() =>
                            setSelection({ kind: "chat", id: chat.id })
                          }
                          tooltip={chat.title}
                        >
                          <Dot color={colorOf(chat.space_id)} />
                          <span className="truncate">{chat.title}</span>
                        </SidebarMenuButton>
                      </SidebarMenuItem>
                    ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
      </Sidebar>

      <SidebarInset>
        <header className="flex h-14 items-center gap-2 border-b px-4">
          <SidebarTrigger />
          <Separator orientation="vertical" className="h-4" />
          {selection && (
            <Dot
              color={
                selectedSpace?.color ??
                (selectedChat ? colorOf(selectedChat.space_id) : undefined)
              }
            />
          )}
          <span className="text-sm font-medium">
            {selectedSpace?.name ?? selectedChat?.title ?? "fabricator"}
          </span>
        </header>
        <div className="flex-1 overflow-y-auto p-6">
          {selectedSpace ? (
            <SpacePane space={selectedSpace} />
          ) : selectedChat ? (
            <ChatPane chat={selectedChat} space={
              spaces?.find((s) => s.id === selectedChat.space_id) ?? null
            } />
          ) : (
            <Empty>
              <EmptyHeader>
                <EmptyTitle>Nothing selected</EmptyTitle>
                <EmptyDescription>
                  Pick a space or a chat from the sidebar.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}

function SpacePane({ space }: { space: Space }) {
  return (
    <Card className="max-w-2xl">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Dot color={space.color} />
          {space.name}
        </CardTitle>
        <CardDescription>{space.goal}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4 text-sm">
        <div className="flex flex-wrap gap-2">
          {space.repos.map((repo) => (
            <Badge key={repo} variant="outline">
              {repo}
            </Badge>
          ))}
        </div>
        <p className="text-muted-foreground">
          created {new Date(space.created_at).toLocaleDateString()}
        </p>
      </CardContent>
    </Card>
  );
}

function ChatPane({ chat, space }: { chat: Chat; space: Space | null }) {
  return (
    <div className="flex max-w-2xl flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        {space && (
          <Badge variant="outline">
            <Dot color={space.color} />
            {space.name}
          </Badge>
        )}
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
