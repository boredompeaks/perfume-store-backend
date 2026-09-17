import type { ReactNode } from "react";

export default function AuthShell({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="mx-auto max-w-md px-4 py-16 sm:px-6">
      <h1 className="font-display text-4xl tracking-tight">{title}</h1>
      <div className="mt-8">{children}</div>
    </div>
  );
}
