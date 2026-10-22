import type { ReactNode } from "react";

import type { ChartStatus, DeliveryStatus, JobStatus } from "../api";

type Tone = "ok" | "warn" | "bad" | "info" | "muted";

export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

const JOB_STATUS: Record<JobStatus, [string, Tone]> = {
  running: ["Выполняется", "info"],
  success: ["Успешно", "ok"],
  partial: ["Частично", "warn"],
  error: ["Ошибка", "bad"],
  skipped: ["Пропущено", "muted"],
  interrupted: ["Прервано", "muted"],
};

export function JobStatusBadge({ status }: { status: JobStatus }) {
  const [label, tone] = JOB_STATUS[status];
  return <Badge tone={tone}>{label}</Badge>;
}

/** Статус графика: что с отправкой письма, а если письма ещё нет — что со скачиванием. */
export function ChartStatusBadge(props: {
  delivery: DeliveryStatus | null;
  chart: ChartStatus;
}) {
  if (props.delivery === "sent") return <Badge tone="ok">Отправлен</Badge>;
  if (props.delivery === "failed") return <Badge tone="bad">Ошибка отправки, будет повтор</Badge>;
  if (props.chart === "download_error") return <Badge tone="bad">Ошибка скачивания</Badge>;
  return <Badge tone="muted">Ожидает отправки</Badge>;
}
