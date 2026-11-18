"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/auth";

/** A signed-in user is bounced to their orders instead of seeing the forms. */
export default function GuestOnly({
  next,
  children,
}: {
  next?: string;
  children: React.ReactNode;
}) {
  const router = useRouter();
  const { status } = useAuth();

  useEffect(() => {
    if (status === "authenticated") {
      router.replace(next ?? "/orders");
    }
  }, [status, next, router]);

  if (status !== "anonymous") {
    return (
      <div className="py-24 text-center text-sm text-ink-secondary" role="status">
        {status === "loading"
          ? "Loading…"
          : "You're already signed in — taking you to your orders…"}
      </div>
    );
  }

  return <>{children}</>;
}
