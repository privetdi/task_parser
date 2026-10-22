// Типы повторяют схемы ответов backend (backend/app/api/schemas.py).

export type JobStatus = "running" | "success" | "partial" | "error" | "skipped" | "interrupted";
export type JobTrigger = "schedule" | "manual" | "startup";
export type DeliveryStatus = "pending" | "sent" | "failed";
export type ChartStatus = "discovered" | "downloaded" | "download_error";

export interface Run {
  id: number;
  job: string;
  job_title: string;
  trigger: JobTrigger;
  status: JobStatus;
  started_at: string;
  finished_at: string | null;
  duration_seconds: number | null;
  summary: string;
  error: string | null;
}

export interface Job {
  name: string;
  title: string;
  description: string;
  cron: string | null;
  next_run: string | null;
  is_running: boolean;
  last_run: Run | null;
}

export interface Rate {
  code: string;
  name: string;
  scale: number;
  rate: number;
  rate_date: string; // YYYY-MM-DD
  periodicity: 0 | 1; // 0 — ежедневно, 1 — ежемесячно
  change: number | null;
}

export interface LatestRates {
  report_date: string;
  count: number;
  rates: Rate[];
}

export interface Backfill {
  total: number;
  completed: number;
  remaining: number;
  target_start: string;
  loaded_from: string | null;
  is_complete: boolean;
  is_running: boolean;
}

export interface Chart {
  id: number;
  slug: string;
  title: string;
  url: string;
  status: ChartStatus;
  error: string | null;
  discovered_at: string;
  image_url: string | null;
  delivery_status: DeliveryStatus | null;
  sent_at: string | null;
  failed_attempts: number;
  last_error: string | null;
}

export interface Config {
  mail_mode: "dry-run" | "smtp";
  mail_from: string;
  mail_to: string[];
  timezone: string;
  schedules: Record<string, string>;
}

/** Ошибка ответа API; `detail` — понятный текст от backend. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(0, "Нет связи с сервером");
  }
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // тело не JSON — оставляем статус
    }
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}

export const api = {
  jobs: () => request<Job[]>("/api/jobs"),
  runJob: (name: string) =>
    request<{ run_id: number }>(`/api/jobs/${name}/run`, { method: "POST" }),
  runs: (limit = 30) => request<Run[]>(`/api/runs?limit=${limit}`),
  rates: () => request<LatestRates>("/api/rates/latest"),
  backfill: () => request<Backfill>("/api/rates/backfill"),
  charts: () => request<Chart[]>("/api/charts"),
  config: () => request<Config>("/api/config"),
};

export const REPORT_DOWNLOAD_URL = "/api/reports/rates.xlsx";
