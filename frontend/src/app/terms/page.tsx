import type { Metadata } from "next";
import { LegalSection, LegalShell } from "@/components/legal/LegalShell";

export const metadata: Metadata = {
  title: "Terms & Conditions",
};

export default function TermsPage() {
  return (
    <LegalShell title="Terms & Conditions">
      <LegalSection title="Using this store">
        <p>
          By creating an account or placing an order you agree to these
          terms. Provide accurate details — delivery failures caused by
          incorrect addresses or phone numbers are not refundable shipping
          costs on our side.
        </p>
      </LegalSection>

      <LegalSection title="Pricing and orders">
        <p>
          All prices are in Indian Rupees (INR). The total shown when your
          order is created is computed on our servers from current prices and
          is the amount you are charged. If a price changes between your
          cart and checkout, the checkout total is the binding one.
        </p>
        <p>
          An order is created as &ldquo;pending&rdquo; and becomes confirmed
          only after your payment is verified. Stock is not reserved before
          that verification: in the rare case where an item becomes
          unavailable after your payment is captured, we contact you and
          resolve it manually (refund or replacement) — this is handled by
          our team, never silently dropped.
        </p>
      </LegalSection>

      <LegalSection title="Payments">
        <p>
          Payments are collected via Razorpay in INR. We never see or store
          your payment credentials. Payment disputes should first be raised
          with us at the support email below so we can resolve them directly.
        </p>
      </LegalSection>

      <LegalSection title="Delivery">
        <p>
          We deliver across India to the pincodes served by our courier
          partners. Delivery timelines and any shipping charges, if
          introduced, are shown before payment. See the Shipping page for
          current dispatch timelines.
        </p>
      </LegalSection>

      <LegalSection title="Returns and refunds">
        <p>
          Our return and refund process is described on the Returns page.
          In short: unopened items within 7 days of delivery, refunds issued
          manually to the original payment method.
        </p>
      </LegalSection>

      <LegalSection title="Accounts">
        <p>
          You are responsible for activity under your account. Keep your
          password private. We may suspend accounts used for fraud, abuse,
          or payment chargebacks.
        </p>
      </LegalSection>

      <LegalSection title="Liability and law">
        <p>
          The storefront is provided as-is; nothing in these terms limits
          your statutory consumer rights under Indian law. These terms are
          governed by the laws of India. We may update these terms as the
          store evolves — the version on this page applies to your order.
        </p>
      </LegalSection>
    </LegalShell>
  );
}
