import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export default function Home() {
  return (
    <main className="flex min-h-screen items-center justify-center p-8">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>fabricator</CardTitle>
          <CardDescription>a software factory. nothing here yet.</CardDescription>
        </CardHeader>
        <CardContent>
          <Button variant="outline" render={<a href="/api/health" />}>
            backend health
          </Button>
        </CardContent>
      </Card>
    </main>
  );
}
