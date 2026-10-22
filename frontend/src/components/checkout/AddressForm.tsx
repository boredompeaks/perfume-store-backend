"use client";

import { useState, type ReactNode } from "react";
import {
  forgetShipping,
  hasStoredShipping,
  type ShippingDetails,
} from "@/lib/shipping-store";

type Errors = Partial<Record<keyof ShippingDetails, string>>;

/**
 * D1 screen (U-W12-T8): OPEN-HAIRLINE fields — no box, label above in the
 * small tracked eyebrow step, bottom rule only. The rule keeps
 * `--color-field-border` (#857C72, 3.90:1 on paper): the screen draws its
 * underline in a decorative outline shade (#d0c4be), which is
 * barred from being a control boundary (§2.1.5 E2 — this underline IS the
 * field's 3:1 affordance). Preflight zeroes the other three sides, so `border-b`
 * alone is the whole box model here; the shared boxed field class every other
 * form uses stays untouched.
 */
const hairlineInputClass =
  "border-b border-field-border bg-transparent px-0 py-2 text-body placeholder:text-ink-secondary focus:outline-none";

function validate(d: ShippingDetails): Errors {
  const errors: Errors = {};
  if (!d.full_name.trim()) errors.full_name = "Full name is required.";
  if (!d.email.trim()) errors.email = "Email is required — we send your receipt here.";
  else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(d.email.trim()))
    errors.email = "Enter a valid email address.";
  const phone = d.phone.replace(/[\s-]/g, "");
  if (!phone) errors.phone = "Phone is required.";
  else if (!/^\d{10,15}$/.test(phone))
    errors.phone = "Enter 10–15 digits.";
  if (!d.address.trim()) errors.address = "Address is required.";
  if (!d.city.trim()) errors.city = "City is required.";
  if (!d.state.trim()) errors.state = "State is required.";
  const pincode = d.pincode.trim();
  if (!pincode) errors.pincode = "Pincode is required.";
  else if (!/^\d{6}$/.test(pincode))
    errors.pincode = "Enter a 6-digit pincode.";
  return errors;
}

export default function AddressForm({
  initial,
  pending,
  serverError,
  submitLabel,
  onSubmit,
  onDestinationBlur,
  afterFields,
}: {
  initial: ShippingDetails | null;
  pending: boolean;
  serverError: string | null;
  submitLabel: string;
  onSubmit: (details: ShippingDetails) => void;
  /**
   * Spec 5.4 CHANGE-12: the delivery estimate fires on blur of pincode AND
   * state, so the destination fields report their own blur instead of the
   * parent polling form state it does not own. Everything else about this
   * form (fields, validation, the PII disclosure) is untouched.
   */
  onDestinationBlur?: (details: ShippingDetails) => void;
  /**
   * Spec 5.4: the delivery estimate renders "below the fields" — a slot
   * inserted directly after the field grid. The PII disclosure and the submit
   * button below keep their own order and styling.
   */
  afterFields?: ReactNode;
}) {
  const [details, setDetails] = useState<ShippingDetails>(
    initial ?? {
      full_name: "",
      email: "",
      phone: "",
      address: "",
      city: "",
      state: "",
      pincode: "",
    },
  );
  const [errors, setErrors] = useState<Errors>({});
  // Whether a prefill actually happened this mount — the disclosure is tied
  // to it, per the PII-handling requirement (visible note + forget control).
  const [prefilledAtMount] = useState<boolean>(() => initial !== null);
  const [forgotten, setForgotten] = useState(false);

  const set = (key: keyof ShippingDetails) => (value: string) => {
    setDetails((d) => ({ ...d, [key]: value }));
    setErrors((e) => ({ ...e, [key]: undefined }));
  };

  const fields: {
    key: keyof ShippingDetails;
    label: string;
    type?: string;
    autoComplete: string;
    wide?: boolean;
  }[] = [
    { key: "full_name", label: "Full name", autoComplete: "name" },
    { key: "email", label: "Email", type: "email", autoComplete: "email" },
    { key: "phone", label: "Phone", type: "tel", autoComplete: "tel" },
    {
      key: "address",
      label: "Address",
      autoComplete: "street-address",
      wide: true,
    },
    { key: "city", label: "City", autoComplete: "address-level2" },
    { key: "state", label: "State", autoComplete: "address-level1" },
    { key: "pincode", label: "Pincode", autoComplete: "postal-code" },
  ];

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        const next = validate(details);
        setErrors(next);
        if (Object.keys(next).length === 0) onSubmit(details);
      }}
      className="space-y-4"
      noValidate
    >
      <div className="grid gap-4 sm:grid-cols-2">
        {fields.map((f) => (
          <div key={f.key} className={f.wide ? "sm:col-span-2" : ""}>
            <label
              htmlFor={`addr-${f.key}`}
              className="mb-1 block text-eyebrow text-bronze"
            >
              {f.label}
            </label>
            <input
              id={`addr-${f.key}`}
              data-testid={`addr-${f.key}`}
              type={f.type ?? "text"}
              autoComplete={f.autoComplete}
              value={details[f.key]}
              onChange={(e) => set(f.key)(e.target.value)}
              aria-invalid={errors[f.key] ? true : undefined}
              aria-describedby={errors[f.key] ? `addr-${f.key}-err` : undefined}
              onBlur={
                f.key === "state" || f.key === "pincode"
                  ? () => onDestinationBlur?.(details)
                  : undefined
              }
              className={`${hairlineInputClass} w-full`}
              required
            />
            {errors[f.key] && (
              <p
                id={`addr-${f.key}-err`}
                role="alert"
                className="mt-1 text-small text-danger"
              >
                {errors[f.key]}
              </p>
            )}
          </div>
        ))}
      </div>

      {afterFields && <div className="mt-6">{afterFields}</div>}

      {serverError && (
        <p role="alert" className="text-small text-danger" aria-live="polite">
          {serverError}
        </p>
      )}

      {/* PII disclosure — visible at the point of prefill and save, with a
          real forget control. Not a code comment: a customer-facing notice.
          D1 screen: no box, the disclaimer set italic with the forget control
          on its own line and a hairline rule under it. The two pinned
          sentences (the prefill notice and the forget label) stay verbatim —
          spec 5.4 keeps this block and checkout/e2e both read that copy. The
          screen's "held solely on your current device" is folded onto the
          e2e-pinned "saved on this device only" wording. */}
      <div
        data-testid="pii-disclosure"
        className="text-small text-ink-secondary"
      >
        {prefilledAtMount && !forgotten ? (
          <p className="text-ink">
            Prefilled from details saved on this device.
          </p>
        ) : null}
        <div className="mt-2 flex flex-col gap-2 border-b border-hairline pb-4 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
          <p className="max-w-2xl italic">
            These carriage records are saved on this device only and remain
            uncommitted until order transmission.
          </p>
          {!forgotten && (prefilledAtMount || hasStoredShipping()) ? (
            <button
              type="button"
              data-testid="forget-saved-details"
              onClick={() => {
                forgetShipping();
                setForgotten(true);
              }}
              className="shrink-0 self-start text-ink underline underline-offset-4 transition-colors hover:text-bronze"
            >
              Forget saved details
            </button>
          ) : null}
        </div>
        {forgotten ? (
          <p className="mt-1 text-ink">Saved details were removed.</p>
        ) : null}
      </div>

      {/* D1 screen: the solid ink square CTA (radius stays 0 — §2.3's
          square-radius rule outranks the screen's rounded-lg). The label
          keeps its pending copy — uupm row 61, submit feedback: loading state
          on press, then the server's success (step change) or error. */}
      <button
        type="submit"
        data-testid="address-submit"
        disabled={pending}
        className="w-full bg-ink px-8 py-4 text-label text-paper transition-colors hover:bg-bronze disabled:opacity-60 sm:w-auto"
      >
        {pending ? "Working…" : submitLabel}
      </button>
    </form>
  );
}
