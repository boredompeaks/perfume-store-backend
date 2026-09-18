import { site } from "@/lib/site";
import { fallbackContact, type ContactSettings } from "@/lib/server-settings";

const iconClass = "h-5 w-5";

export function WhatsAppIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={iconClass} aria-hidden="true">
      <path d="M12.04 2C6.58 2 2.13 6.45 2.13 11.91c0 1.75.46 3.45 1.32 4.95L2 22l5.25-1.38a9.9 9.9 0 0 0 4.79 1.22h.01c5.46 0 9.9-4.45 9.9-9.91 0-2.65-1.03-5.14-2.9-7.01A9.82 9.82 0 0 0 12.04 2Zm0 18.15h-.01a8.2 8.2 0 0 1-4.19-1.15l-.3-.18-3.12.82.83-3.04-.2-.31a8.2 8.2 0 0 1-1.26-4.38c0-4.54 3.7-8.24 8.25-8.24 2.2 0 4.27.86 5.82 2.42a8.18 8.18 0 0 1 2.41 5.83c0 4.54-3.7 8.23-8.23 8.23Zm4.52-6.16c-.25-.12-1.47-.72-1.69-.81-.23-.08-.39-.12-.56.13-.16.24-.64.8-.78.97-.14.16-.29.18-.54.06-.25-.12-1.05-.39-2-1.23-.74-.66-1.23-1.47-1.38-1.72-.14-.25-.01-.38.11-.51.11-.11.25-.29.37-.43.13-.15.17-.25.25-.41.08-.17.04-.31-.02-.43-.06-.12-.56-1.34-.76-1.84-.2-.48-.41-.42-.56-.43h-.48c-.17 0-.43.06-.66.31-.22.25-.86.85-.86 2.07 0 1.22.89 2.4 1.01 2.56.12.17 1.75 2.67 4.23 3.74.59.26 1.05.41 1.41.52.59.19 1.13.16 1.56.1.48-.07 1.47-.6 1.67-1.18.21-.58.21-1.07.15-1.18-.06-.1-.23-.16-.48-.29Z" />
    </svg>
  );
}

export function PhoneIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={iconClass}
      aria-hidden="true"
    >
      <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92Z" />
    </svg>
  );
}

export function MailIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={iconClass}
      aria-hidden="true"
    >
      <rect x="2" y="4" width="20" height="16" rx="2" />
      <path d="m22 7-10 6L2 7" />
    </svg>
  );
}

export function InstagramIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={iconClass}
      aria-hidden="true"
    >
      <rect x="2" y="2" width="20" height="20" rx="5" />
      <circle cx="12" cy="12" r="4" />
      <circle cx="17.5" cy="6.5" r="0.5" fill="currentColor" stroke="none" />
    </svg>
  );
}

type ContactItem = {
  href: string;
  label: string;
  icon: React.ReactNode;
  external?: boolean;
};

/**
 * The one contact-row used in the footer and the contact page. Values come
 * from admin-configured settings (fallback: env/site.ts); unconfigured
 * channels render nothing instead of dead links.
 */
export default function ContactLinks({
  settings = fallbackContact,
  className,
}: {
  settings?: ContactSettings;
  className?: string;
}) {
  const items: ContactItem[] = [];

  if (settings.whatsappNumber) {
    items.push({
      href: `https://wa.me/${settings.whatsappNumber}?text=${encodeURIComponent(settings.whatsappMessage)}`,
      label: "Chat with us on WhatsApp",
      icon: <WhatsAppIcon />,
      external: true,
    });
  }
  if (settings.supportPhone) {
    items.push({
      href: `tel:${settings.supportPhone.replace(/[^+\d]/g, "")}`,
      label: `Call ${settings.supportPhone}`,
      icon: <PhoneIcon />,
    });
  }
  items.push({
    href: `mailto:${settings.supportEmail}?subject=${encodeURIComponent("Maison Aurel — support")}`,
    label: `Email ${settings.supportEmail}`,
    icon: <MailIcon />,
  });
  if (settings.instagramUrl) {
    items.push({
      href: settings.instagramUrl,
      label: "Follow us on Instagram",
      icon: <InstagramIcon />,
      external: true,
    });
  }

  return (
    <ul className={`flex flex-wrap items-center gap-3 ${className ?? ""}`}>
      {items.map((item) => (
        <li key={item.label}>
          <a
            href={item.href}
            aria-label={item.label}
            title={item.label}
            {...(item.external ? { target: "_blank", rel: "noreferrer" } : {})}
            className="flex h-10 w-10 items-center justify-center border border-line bg-surface text-ink transition-all hover:border-bronze hover:text-bronze active:scale-95"
          >
            {item.icon}
          </a>
        </li>
      ))}
    </ul>
  );
}
