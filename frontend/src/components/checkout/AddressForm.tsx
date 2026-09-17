"use client";

import { useState } from "react";
import {
  forgetShipping,
  hasStoredShipping,
  type ShippingDetails,
} from "@/lib/shipping-store";

const inputClass =
  "w-full border border-line bg-surface px-3 py-2 text-sm placeholder:text-ink-muted/70 focus:outline-none";

type Errors = Partial<Record<keyof ShippingDetails, string>>;

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
}: {
  initial: ShippingDetails | null;
  pending: boolean;
  serverError: string | null;
  submitLabel: string;
  onSubmit: (details: ShippingDetails) => void;
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
            <label htmlFor={`addr-${f.key}`} className="mb-1 block text-sm">
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
              className={inputClass}
              required
            />
            {errors[f.key] && (
              <p
                id={`addr-${f.key}-err`}
                role="alert"
                className="mt-1 text-sm text-bronze"
              >
                {errors[f.key]}
              </p>
            )}
          </div>
        ))}
      </div>

      {serverError && (
        <p role="alert" className="text-sm text-bronze" aria-live="polite">
          {serverError}
        </p>
      )}

      {/* PII disclosure — visible at the point of prefill and save, with a
          real forget control. Not a code comment: a customer-facing notice. */}
      <div
        data-testid="pii-disclosure"
        className="border border-line bg-surface p-4 text-sm text-ink-muted"
      >
        {prefilledAtMount && !forgotten ? (
          <p className="text-ink">
            Prefilled from details saved on this device.
          </p>
        ) : null}
        <p>
          On checkout, these details are saved on this device only — to
          prefill your next order. They are never shared beyond the order
          itself.
        </p>
        {!forgotten && (prefilledAtMount || hasStoredShipping()) ? (
          <button
            type="button"
            data-testid="forget-saved-details"
            onClick={() => {
              forgetShipping();
              setForgotten(true);
            }}
            className="mt-2 text-ink underline underline-offset-4 transition-colors hover:text-bronze"
          >
            Forget saved details
          </button>
        ) : null}
        {forgotten ? (
          <p className="mt-1 text-ink">Saved details were removed.</p>
        ) : null}
      </div>

      <button
        type="submit"
        data-testid="address-submit"
        disabled={pending}
        className="w-full bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze disabled:opacity-60 sm:w-auto"
      >
        {pending ? "Working…" : submitLabel}
      </button>
    </form>
  );
}
