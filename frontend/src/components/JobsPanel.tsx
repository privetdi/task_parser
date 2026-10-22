import { useState } from "react";

import type { Job } from "../api";
import { formatDateTime, formatDuration } from "../format";
import { JobStatusBadge } from "./StatusBadge";

interface Props {
  jobs: Job[] | null;
  onRun: (name: string) => Promise<void>;
}

export function JobsPanel({ jobs, onRun }: Props) {
  const [pending, setPending] = useState<string | null>(null);
  const [messages, setMessages] = useState<Record<string, string>>({});

  async function handleRun(name: string) {
    setPending(name);
    setMessages((m) => ({ ...m, [name]: "" }));
    try {
      await onRun(name);
    } catch (e) {
      // Например, 409: задача уже выполняется — показываем текст от сервера
      setMessages((m) => ({ ...m, [name]: e instanceof Error ? e.message : String(e) }));
    } finally {
      setPending(null);
    }
  }

  if (!jobs) return <p className="muted">Загрузка…</p>;

  return (
    <div className="cards">
      {jobs.map((job) => {
        const busy = job.is_running || pending === job.name;
        const last = job.last_run;
        return (
          <article className="card" key={job.name}>
            <header className="card-head">
              <h3>{job.title}</h3>
              {last && <JobStatusBadge status={last.status} />}
            </header>
            <p className="muted">{job.description}</p>
            <dl className="facts">
              <dt>Расписание</dt>
              <dd>
                {job.cron ? <code>{job.cron}</code> : "при старте и вручную"}
              </dd>
              {job.cron && (
                <>
                  <dt>Следующий запуск</dt>
                  <dd>{formatDateTime(job.next_run)}</dd>
                </>
              )}
              <dt>Последний запуск</dt>
              <dd>
                {last
                  ? `${formatDateTime(last.started_at)} · ${formatDuration(last.duration_seconds)}`
                  : "ещё не запускалась"}
              </dd>
            </dl>
            {last?.summary && <p className="summary">{last.summary}</p>}
            {last?.error && (
              <details>
                <summary>Ошибки</summary>
                <pre>{last.error}</pre>
              </details>
            )}
            <div className="card-actions">
              <button onClick={() => void handleRun(job.name)} disabled={busy}>
                {busy ? "Выполняется…" : "Запустить"}
              </button>
              {messages[job.name] && (
                <span className="inline-error" role="alert">
                  {messages[job.name]}
                </span>
              )}
            </div>
          </article>
        );
      })}
    </div>
  );
}
