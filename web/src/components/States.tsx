import type { ReactNode } from "react";

export const Skeleton = ({ lines = 3, label }: { lines?: number; label: string }) => (
  <div className="skeleton-block" role="status" aria-live="polite" aria-label={label}>
    {Array.from({ length: lines }, (_, i) => (
      <span key={i} className={`skeleton skeleton--w${i % 4}`} />
    ))}
  </div>
);

export const ErrorState = ({ error, onRetry }: { error: Error | null; onRetry: () => void }) => (
  <div className="state state--error" role="alert">
    <h2>This view's data did not load</h2>
    <p>{error?.message ?? "Unknown error."}</p>
    <button type="button" className="btn" onClick={onRetry}>
      Load again
    </button>
  </div>
);

export const EmptyState = ({ title, children }: { title: string; children: ReactNode }) => (
  <div className="state">
    <h2>{title}</h2>
    <p>{children}</p>
  </div>
);

/** Loading, error and empty handled once, so every view gets all three. */
export function Loaded<T>({ q, label, empty, children }: {
  q: { data: T | undefined; isPending: boolean; isError: boolean; error: Error | null; refetch: () => unknown };
  label: string;
  empty?: (data: T) => ReactNode | null;
  children: (data: T) => ReactNode;
}) {
  if (q.isPending) return <Skeleton label={`Loading ${label}`} lines={5} />;
  if (q.isError || q.data === undefined) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  const e = empty?.(q.data);
  return <>{e ?? children(q.data)}</>;
}
