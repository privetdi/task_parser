import { api } from "./api";
import { ChartsPanel } from "./components/ChartsPanel";
import { JobsPanel } from "./components/JobsPanel";
import { RatesPanel } from "./components/RatesPanel";
import { RunsTable } from "./components/RunsTable";
import { Badge } from "./components/StatusBadge";
import { usePolling } from "./hooks/usePolling";

export default function App() {
  const jobs = usePolling(api.jobs, 3000);
  const runs = usePolling(() => api.runs(30), 3000);
  const rates = usePolling(api.rates, 10000);
  const backfill = usePolling(api.backfill, 5000);
  const charts = usePolling(api.charts, 10000);
  const config = usePolling(api.config, 60000);

  async function runJob(name: string) {
    await api.runJob(name);
    // Сразу показываем новую запись, не дожидаясь очередного опроса
    await Promise.all([jobs.refresh(), runs.refresh(), backfill.refresh()]);
  }

  const connectionError = [jobs.error, runs.error, rates.error, charts.error].find(
    (e) => e === "Нет связи с сервером",
  );

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Курсы НБ РБ и графики IEA</h1>
          <p className="muted">Сбор данных по расписанию и отправка результатов на почту</p>
        </div>
        {config.data && (
          <p className="mail-info">
            <Badge tone={config.data.mail_mode === "smtp" ? "ok" : "info"}>
              Почта: {config.data.mail_mode === "smtp" ? "SMTP" : "без реальной отправки"}
            </Badge>{" "}
            <span className="muted">→ {config.data.mail_to.join(", ")}</span>
          </p>
        )}
      </header>

      {connectionError && (
        <p className="banner" role="alert">
          {connectionError}. Данные ниже могут быть устаревшими.
        </p>
      )}

      <section>
        <h2>Задачи</h2>
        <JobsPanel jobs={jobs.data} onRun={runJob} />
      </section>

      <section>
        <h2>Курсы валют</h2>
        <RatesPanel
          rates={rates.data}
          backfill={backfill.data}
          onRunBackfill={() => runJob("rates_backfill")}
        />
      </section>

      <section>
        <h2>Графики IEA</h2>
        <ChartsPanel charts={charts.data} />
      </section>

      <section>
        <h2>Журнал запусков</h2>
        <RunsTable runs={runs.data} />
      </section>
    </div>
  );
}
