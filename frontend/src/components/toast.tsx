"use client";

import Link from "next/link";
import {
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
} from "react";

export type ToastAction = { label: string; href: string };
type ToastItem = { id: number; message: string; action?: ToastAction };

const ToastContext = createContext<(message: string, action?: ToastAction) => void>(
  () => {},
);

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(0);

  const toast = useCallback((message: string, action?: ToastAction) => {
    const id = ++nextId.current;
    // Cap at 3 visible toasts — drop the oldest.
    setItems((prev) => [...prev.slice(-2), { id, message, action }]);
    setTimeout(() => {
      setItems((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  }, []);

  return (
    <ToastContext.Provider value={toast}>
      {children}
      <div
        aria-live="polite"
        className="pointer-events-none fixed inset-x-4 bottom-4 z-50 flex flex-col gap-2 sm:left-auto sm:right-6 sm:w-80"
      >
        {items.map((item) => (
          <div
            key={item.id}
            role="status"
            className="toast-enter pointer-events-auto flex items-center justify-between gap-3 bg-ink px-4 py-3 text-sm text-paper"
          >
            <span>{item.message}</span>
            {item.action && (
              <Link
                href={item.action.href}
                className="shrink-0 underline underline-offset-4 text-bronze-soft transition-colors hover:text-white"
              >
                {item.action.label}
              </Link>
            )}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}
