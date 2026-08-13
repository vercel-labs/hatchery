"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import type { Project } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

export default function Home() {
  const [projects, setProjects] = useState<Project[] | null>(null);

  useEffect(() => {
    fetch("/api/projects")
      .then((r) => r.json())
      .then(setProjects);
  }, []);

  return (
    <main className="mx-auto flex w-full max-w-xl flex-col gap-4 p-8">
      <header className="flex flex-col gap-1 pb-2">
        <h1 className="text-xl font-semibold">fabricator</h1>
        <p className="text-sm text-muted-foreground">a software factory</p>
      </header>

      {projects === null
        ? Array.from({ length: 2 }).map((_, i) => (
            <Skeleton key={i} className="h-32 w-full rounded-xl" />
          ))
        : projects.map((project) => (
            <Link key={project.id} href={`/projects/${project.id}`}>
              <Card className="transition-colors hover:bg-accent/50">
                <CardHeader>
                  <CardTitle>{project.name}</CardTitle>
                  <CardDescription>{project.goal}</CardDescription>
                </CardHeader>
                <CardContent className="flex flex-wrap gap-2">
                  {project.repos.map((repo) => (
                    <Badge key={repo} variant="outline">
                      {repo}
                    </Badge>
                  ))}
                </CardContent>
              </Card>
            </Link>
          ))}
    </main>
  );
}
