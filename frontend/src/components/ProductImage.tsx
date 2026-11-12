"use client";

import Image from "next/image";
import { useState } from "react";
import { mediaUrl } from "@/lib/config";

type Props = {
  src: string | null | undefined;
  alt: string;
  sizes?: string;
  priority?: boolean;
  className?: string;
};

/**
 * Falls back to a monogram for a null path or a /media/ URL that 404s
 * (media is unserved in prod), never a broken image.
 */
export default function ProductImage({
  src,
  alt,
  sizes,
  priority,
  className,
}: Props) {
  const [failed, setFailed] = useState(false);
  const resolved = mediaUrl(src);
  const monogram = alt.charAt(0).toUpperCase();

  if (!resolved || failed) {
    return (
      <div
        aria-hidden="true"
        className={`absolute inset-0 flex items-center justify-center bg-hairline ${className ?? ""}`}
      >
        <span className="font-display text-4xl text-bronze/60">
          {monogram}
        </span>
      </div>
    );
  }

  return (
    <Image
      src={resolved}
      alt={alt}
      fill
      sizes={sizes ?? "100vw"}
      priority={priority}
      onError={() => setFailed(true)}
      className={`object-cover ${className ?? ""}`}
    />
  );
}
