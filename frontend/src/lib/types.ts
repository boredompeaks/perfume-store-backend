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
