import type { ReactNode } from "react";

export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <div className="page-header">
      <h1>{title}</h1>
      {description && <p>{description}</p>}
      <div style={{ flex: 1 }} />
      {actions}
    </div>
  );
}
