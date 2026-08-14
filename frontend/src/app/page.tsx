"use client";

import { useCallback, useEffect, useState } from "react";
import {
  BookMarkedIcon,
  FolderGitIcon,
  LinkIcon,
  TerminalIcon,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { Chat, Resource, Space } from "@/lib/api";
import type { ChatUIMessage } from "@/lib/messages";
import { ChatView } from "@/components/chat";
import { TerminalPane } from "@/components/terminal-pane";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
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
  const [failed, setFailed] = useState(false);
  const [selection, setSelection] = useState<Selection>(null);
  // set by space clicks only; chat clicks leave the order alone
  const [sortSpaceId, setSortSpaceId] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      const [s, c] = await Promise.all([
        fetch("/api/spaces"),
        fetch("/api/chats"),
      ]);
      if (!s.ok || !c.ok) throw new Error("backend unreachable");
      setSpaces(await s.json());
      setChats(await c.json());
    };
    load().catch(() => setFailed(true));
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

  const sortedChats =
    chats && sortSpaceId
      ? [...chats].sort(
          (a, b) =>
            Number(b.space_id === sortSpaceId) -
            Number(a.space_id === sortSpaceId),
        )
      : chats;

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
                  ? Array.from({ length: failed ? 0 : 2 }).map((_, i) => (
                      <SidebarMenuItem key={i}>
                        <SidebarMenuSkeleton />
                      </SidebarMenuItem>
                    ))
                  : spaces.map((space) => (
                      <SidebarMenuItem key={space.id}>
                        <SidebarMenuButton
                          isActive={selectedSpace?.id === space.id}
                          onClick={() => {
                            setSelection({ kind: "space", id: space.id });
                            setSortSpaceId(space.id);
                          }}
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
                {sortedChats === null
                  ? Array.from({ length: failed ? 0 : 4 }).map((_, i) => (
                      <SidebarMenuItem key={i}>
                        <SidebarMenuSkeleton />
                      </SidebarMenuItem>
                    ))
                  : sortedChats.map((chat) => (
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
        {selectedChat && !failed ? (
          <LiveChat key={selectedChat.id} chat={selectedChat} />
        ) : (
          <div className="flex-1 overflow-y-auto p-6 md:p-10">
            {failed ? (
              <Empty>
                <EmptyHeader>
                  <EmptyTitle>Backend unreachable</EmptyTitle>
                  <EmptyDescription>
                    Could not load spaces and chats. Locally: run `uv run
                    dev.py` in backend/ and reload.
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            ) : selectedSpace ? (
              <SpacePane space={selectedSpace} />
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
        )}
      </SidebarInset>
    </SidebarProvider>
  );
}

const resourceIcon = {
  repo: FolderGitIcon,
  reference: BookMarkedIcon,
  link: LinkIcon,
} as const;

function ResourceCard({ resource }: { resource: Resource }) {
  const Icon =
    resourceIcon[resource.kind as keyof typeof resourceIcon] ?? LinkIcon;
  return (
    <a href={resource.url} target="_blank" rel="noreferrer">
      <Card className="flex-row items-center gap-3 p-3 transition-colors hover:bg-accent/50">
        <Icon className="size-4 shrink-0 text-muted-foreground" />
        <div className="flex min-w-0 flex-col">
          <span className="truncate text-sm font-medium">
            {resource.title}
          </span>
          <span className="truncate text-xs text-muted-foreground">
            {new URL(resource.url).hostname}
          </span>
        </div>
      </Card>
    </a>
  );
}

function SpacePane({ space }: { space: Space }) {
  const resources = [
    ...space.repos.map((repo) => ({
      title: repo,
      url: `https://github.com/${repo}`,
      kind: "repo",
    })),
    ...space.resources,
  ];
  return (
    <div className="mx-auto grid w-full max-w-6xl gap-10 lg:grid-cols-[minmax(0,1fr)_18rem]">
      <article className="typeset typeset-docs mx-auto w-full max-w-2xl min-w-0">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{space.about}</ReactMarkdown>
      </article>
      <aside className="mx-auto flex w-full max-w-2xl flex-col gap-2 lg:mx-0 lg:max-w-none">
        <span className="px-1 text-xs font-medium text-muted-foreground">
          Resources
        </span>
        {resources.map((resource) => (
          <ResourceCard key={resource.url} resource={resource} />
        ))}
      </aside>
    </div>
  );
}

// The chat pane, with the coder terminal splitting in on the right once the
// dispatcher launches work. Keyed by chat.id at the call site so useChat
// remounts per chat.
function LiveChat({ chat }: { chat: Chat }) {
  const [coderActive, setCoderActive] = useState(false);
  const [showTerminal, setShowTerminal] = useState(false);

  const onMessagesChange = useCallback((messages: ChatUIMessage[]) => {
    const launched = messages.some((message) =>
      message.parts.some((part) => part.type === "tool-launch_coder"),
    );
    if (launched) setCoderActive(true);
  }, []);

  useEffect(() => {
    if (coderActive) setShowTerminal(true);
  }, [coderActive]);

  return (
    <div className="relative flex min-h-0 flex-1">
      <div className="flex min-w-0 flex-1 basis-[28rem] flex-col">
        <ChatView chatId={chat.id} onMessagesChange={onMessagesChange} />
      </div>
      {coderActive && !showTerminal && (
        <Button
          variant="outline"
          size="sm"
          className="absolute top-2 right-2 z-10"
          onClick={() => setShowTerminal(true)}
        >
          <TerminalIcon className="size-4" />
          terminal
        </Button>
      )}
      {showTerminal && (
        <TerminalPane chatId={chat.id} onClose={() => setShowTerminal(false)} />
      )}
    </div>
  );
}
