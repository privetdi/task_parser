import type { Chart } from "../api";
import { formatDateTime } from "../format";
import { ChartStatusBadge } from "./StatusBadge";

export function ChartsPanel({ charts }: { charts: Chart[] | null }) {
  if (!charts) return <p className="muted">Загрузка…</p>;
  if (charts.length === 0) {
    return (
      <p className="muted">
        Графиков пока нет. Задача «Графики IEA» запускается по расписанию или кнопкой выше; если
        сайт IEA отдаёт проверку на робота, причина видна в журнале запусков.
      </p>
    );
  }

  return (
    <div className="charts-grid">
      {charts.map((chart) => (
        <article className="card chart" key={chart.id}>
          {chart.image_url ? (
            <img src={chart.image_url} alt={chart.title} loading="lazy" />
          ) : (
            <div className="no-image">нет изображения</div>
          )}
          <h3>
            <a href={chart.url} target="_blank" rel="noreferrer">
              {chart.title}
            </a>
          </h3>
          <div>
            <ChartStatusBadge delivery={chart.delivery_status} chart={chart.status} />
          </div>
          {chart.sent_at && <p className="muted small">Отправлено {formatDateTime(chart.sent_at)}</p>}
          {(chart.last_error ?? chart.error) && (
            <p className="inline-error small">{chart.last_error ?? chart.error}</p>
          )}
        </article>
      ))}
    </div>
  );
}
