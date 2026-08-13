import { ProjectView } from "./project-view";

export default async function Page(props: PageProps<"/projects/[id]">) {
  const { id } = await props.params;
  return <ProjectView projectId={id} />;
}
