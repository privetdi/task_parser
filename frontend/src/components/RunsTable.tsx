import type { JobTrigger, Run } from "../api";
import { formatDateTime, formatDuration } from "../format";
import { JobStatusBadge } from "./StatusBadge";

const TRIGGERS: Record<JobTrigger, string> = {
  schedule: "по расписанию",
  manual: "вручную",
  startup: "при старте",
};

export function RunsTable({ runs }: { runs: Run[] | null }) {
  if (!runs) return <p className="muted">Загрузка…</p>;
  if (runs.length === 0) return <p className="muted">Запусков пока не было.</p>;

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Задача</th>
            <th>Запуск</th>
            <th>Статус</th>
            <th>Начало</th>
            <th>Длительность</th>
            <th>Итог</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id}>
              <td>{run.id}</td>
              <td>{run.job_title}</td>
              <td>{TRIGGERS[run.trigger]}</td>
              <td>
                <JobStatusBadge status={run.status} />
              </td>
              <td>{formatDateTime(run.started_at)}</td>
              <td>{formatDuration(run.duration_seconds)}</td>
              <td className="wrap">
                {run.summary}
                {run.error && (
                  <details>
                    <summary>Ошибки</summary>
                    <pre>{run.error}</pre>
                  </details>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
