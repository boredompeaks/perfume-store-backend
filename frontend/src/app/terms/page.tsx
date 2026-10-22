import type { Metadata } from "next";
import Link from "next/link";
import { LegalProse, type LegalProseSection } from "@/components/legal/LegalProse";
import { LegalShell } from "@/components/legal/LegalShell";
import { getReturnWindow } from "@/lib/policies";

export const metadata: Metadata = {
  title: "Terms & Conditions",
};

const policyLink = "underline underline-offset-4 hover:text-bronze";

export default async function TermsPage() {
  // A-12.D.2, same binding as /returns: number only when the API publishes
  // one, otherwise the sentence ships without its window claim.
  const { days } = await getReturnWindow();

  // A5.11: the section list drives both the ToC anchors and the headings, so
  // the two can never drift apart. The copy is the shipped policy text — no
  // new commercial or legal claim is introduced here (P5, §0b.1).
  const sections: LegalProseSection[] = [
    {
      id: "using-this-store",
      title: "Using this store",
      body: (
        <p>
          By creating an account or placing an order you agree to these terms.
          Provide accurate details — delivery failures caused by incorrect
          addresses or phone numbers are not refundable shipping costs on our
          side.
        </p>
      ),
    },
    {
      id: "pricing-and-orders",
      title: "Pricing and orders",
      body: (
        <>
          <p>
            All prices are in Indian Rupees (INR). The total shown when your
            order is created is computed on our servers from current prices and
            is the amount you are charged. If a price changes between your cart
            and checkout, the checkout total is the binding one.
          </p>
          <p>
            An order is created as &ldquo;pending&rdquo; and becomes confirmed
            only after your payment is verified. Stock is not reserved before
            that verification: in the rare case where an item becomes
            unavailable after your payment is captured, we contact you and
            resolve it manually (refund or replacement) — this is handled by
            our team, never silently dropped.
          </p>
        </>
      ),
    },
    {
      id: "payments",
      title: "Payments",
      body: (
        <p>
          Payments are collected via Razorpay in INR. We never see or store your
          payment credentials. Payment disputes should first be raised with us
          at the support email below so we can resolve them directly.
        </p>
      ),
    },
    {
      id: "delivery",
      title: "Delivery",
      body: (
        <p>
          We deliver across India to the pincodes served by our courier
          partners. Delivery timelines and any shipping charges, if introduced,
          are shown before payment. See the{" "}
          <Link href="/shipping" className={policyLink}>
            Shipping
          </Link>{" "}
          page for current dispatch timelines.
        </p>
      ),
    },
    {
      id: "returns-and-refunds",
      title: "Returns and refunds",
      body: (
        <p>
          Our return and refund process is described on the{" "}
          <Link href="/returns" className={policyLink}>
            Returns
          </Link>{" "}
          page. In short: unopened items
          {days ? ` within ${days} days of delivery` : ""}, refunds issued
          manually to the original payment method.
        </p>
      ),
    },
    {
      id: "accounts",
      title: "Accounts",
      body: (
        <p>
          You are responsible for activity under your account. Keep your
          password private. We may suspend accounts used for fraud, abuse, or
          payment chargebacks.
        </p>
      ),
    },
    {
      id: "liability-and-law",
      title: "Liability and law",
      body: (
        <p>
          The storefront is provided as-is; nothing in these terms limits your
          statutory consumer rights under Indian law. These terms are governed
          by the laws of India. We may update these terms as the store evolves —
          the version on this page applies to your order.
        </p>
      ),
    },
  ];

  return (
    <LegalShell title="Terms & Conditions">
      <LegalProse sections={sections} />
    </LegalShell>
  );
}
