import type { Metadata } from "next";
import { LegalProse, type LegalProseSection } from "@/components/legal/LegalProse";
import { LegalShell } from "@/components/legal/LegalShell";

export const metadata: Metadata = {
  title: "Privacy Policy",
};

// A5.11: identical construction to /terms — same ToC, same measure, same
// rhythm (§5.12.2: the two must be visually indistinguishable in structure).
// The copy is the shipped policy text; no new claim about data handling,
// retention or third parties is introduced (P5, §0b.1).
const sections: LegalProseSection[] = [
  {
    id: "what-we-collect",
    title: "What we collect",
    body: (
      <>
        <p>
          <strong className="text-ink">Account data</strong> — when you
          register: your username, email address, and a securely hashed
          password. We never store your password in readable form.
        </p>
        <p>
          <strong className="text-ink">Order data</strong> — delivery name,
          phone, address, pincode, the items ordered, and the amounts charged,
          retained for fulfilment, accounting, and support.
        </p>
        <p>
          <strong className="text-ink">Device data</strong> — a session cookie
          that keeps your cart between visits, and — if you opt in by completing
          a checkout — a copy of your delivery details stored{" "}
          <em>on your own device only</em> (see &ldquo;Saved details&rdquo;
          below).
        </p>
      </>
    ),
  },
  {
    id: "payments",
    title: "Payments",
    body: (
      <p>
        Payments are processed by Razorpay. Your card, UPI, or banking
        credentials go directly to Razorpay over their secure channels — they
        never touch our servers. Razorpay handles that data under its own
        privacy policy, available on their website.
      </p>
    ),
  },
  {
    id: "saved-details-on-your-device",
    title: "Saved details on your device",
    body: (
      <p>
        When you complete a checkout, your delivery details are saved in your
        browser&apos;s local storage to prefill your next order. This never
        leaves your device, is visibly disclosed at the point of saving, can be
        erased anytime with the &ldquo;Forget saved details&rdquo; control on
        the checkout page, and is automatically cleared when you sign out.
      </p>
    ),
  },
  {
    id: "what-we-dont-do",
    title: "What we don't do",
    body: (
      <p>
        No third-party analytics, no advertising trackers, no social-media
        pixels, no selling or sharing of your data. Emails are limited to what
        the service requires: account verification, password resets, and order
        correspondence.
      </p>
    ),
  },
  {
    id: "your-choices",
    title: "Your choices",
    body: (
      <p>
        You can sign out at any time (which erases locally saved checkout
        details on that device). For account deletion or a copy of your order
        data, write to us — requests are handled manually.
      </p>
    ),
  },
];

export default function PrivacyPage() {
  return (
    <LegalShell title="Privacy Policy">
      <LegalProse sections={sections} />
    </LegalShell>
  );
}
