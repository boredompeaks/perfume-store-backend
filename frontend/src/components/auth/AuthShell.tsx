import type { ReactNode } from "react";
import { site } from "@/lib/site";

export default function AuthShell({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="mx-auto max-w-md px-4 py-16 sm:px-6">
      <p className="text-center text-eyebrow text-bronze">
        Haute Parfumerie · Archives
      </p>
      <p className="mt-3 text-center font-display text-h3 text-ink">
        {site.name}
      </p>
      <div className="mt-10 border border-hairline bg-surface p-8 sm:p-10">
        <h1 className="font-display text-h1 italic">{title}</h1>
        <div className="mt-8">{children}</div>
      </div>
      <p className="mt-10 text-center text-eyebrow uppercase text-ink-secondary">
        No. 04 · Formulation registers
      </p>
    </div>
  );
}
