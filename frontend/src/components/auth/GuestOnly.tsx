"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/auth";

/**
 * Gates guest-only routes (/login, /register): a signed-in user is bounced
 * to their orders instead of seeing the forms again.
 */
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
      <div className="py-24 text-center text-sm text-ink-muted" role="status">
        {status === "loading"
          ? "Loading…"
          : "You're already signed in — taking you to your orders…"}
      </div>
    );
  }

  return <>{children}</>;
}
