/**
 * One note in a pyramid tier. `name` is the material; `detail` is an optional
 * one-line provenance copy the server may publish beside it.
 */
export type OlfactoryNote = {
  name: string;
  detail?: string;
};

/**
 * Structured olfactory pyramid (§12-G10, BACKEND_REQUESTS.md "Structured
 * olfactory notes on Product"). GET /api/products/{slug}/ does not publish
 * this yet; the field is optional by design so the PDP can render the
 * pyramid section only when the server supplies tiers and never parse or
 * invent notes from the free-text `description` (§5.2 item 5, P5).
 */
export type ProductNotes = {
  top?: OlfactoryNote[];
  heart?: OlfactoryNote[];
  base?: OlfactoryNote[];
};

export type Product = {
  id: number;
  name: string;
  slug: string;
  description: string;
  /** "1234.00" — format for display with formatINR; never do money math client-side. */
  price: string;
  /** Volume in ml. */
  size: number;
  stock: number;
  category: string;
  /** Relative /media/… path or null — resolve with mediaUrl(). */
  image: string | null;
  created_at: string;
  /** Absent from the API today — see ProductNotes; drives the pyramid section. */
  notes?: ProductNotes;
};

export type CartItem = {
  id: number;
  product: Product;
  quantity: number;
  created_at: string;
};

export type Cart = {
  id: number;
  /** Exposed by the backend (V-09) — deliberately ignored by the frontend. */
  session_id: string;
  items: CartItem[];
  created_at: string;
  updated_at: string;
};

export type ProductPage = {
  count: number;
  total_pages: number;
  current_page: number;
  next_page: boolean;
  previous_page: boolean;
  results: Product[];
};

export type OrderItem = {
  id: number;
  product: number | null;
  product_name: string;
  price: string;
  quantity: number;
  subtotal: string;
};

export type OrderStatus =
  | "pending"
  | "confirmed"
  | "shipped"
  | "delivered"
  | "cancelled";

export type Order = {
  id: number;
  /** Customer-facing reference (ORD-YYYY-XXXXXXXX); tracking keys on this. */
  order_number: string;
  user: number;
  full_name: string;
  phone: string;
  address: string;
  city: string;
  state: string;
  pincode: string;
  status: OrderStatus;
  /** Coupon code string or null. */
  coupon: string | null;
  discount_amount: string;
  total_amount: string;
  items: OrderItem[];
  created_at: string;
  updated_at: string;
};

export type ShipmentEvent = {
  status: string;
  occurred_at: string;
};

export type Shipment = {
  order_number: string;
  status: string;
  carrier: string;
  /** Provider slug code, or null when the parcel is unlinked. */
  provider: string | null;
  tracking_number: string;
  /** Operator-configured URL — render only through safeExternalUrl(). */
  tracking_url: string | null;
  estimated_delivery: string | null;
  events: ShipmentEvent[];
};

export type CouponPreview = {
  coupon: string;
  subtotal: string;
  discount: string;
  final_total: string;
};

export type PaymentSession = {
  order_id: number;
  razorpay_order_id: string;
  /** Paise. */
  amount: number;
  amount_in_rupees: string;
  currency: string;
  key_id: string;
};
