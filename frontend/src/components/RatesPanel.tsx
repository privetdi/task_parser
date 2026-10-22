import { useState } from "react";

import { type Backfill, type LatestRates, REPORT_DOWNLOAD_URL } from "../api";
import { formatChange, formatDate, formatRate } from "../format";

interface Props {
  rates: LatestRates | null;
  backfill: Backfill | null;
  onRunBackfill: () => Promise<void>;
}

function BackfillStatus({ backfill, onRun }: { backfill: Backfill; onRun: () => Promise<void> }) {
  const [error, setError] = useState<string | null>(null);

  async function handleRun() {
    setError(null);
    try {
      await onRun();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (backfill.total === 0) {
    return (
      <div className="backfill">
        <p>Справочник валют ещё не загружен.</p>
        <button onClick={() => void handleRun()} disabled={backfill.is_running}>
          {backfill.is_running ? "Загрузка…" : "Начать загрузку истории"}
        </button>
        {error && <span className="inline-error">{error}</span>}
      </div>
    );
  }

  return (
    <div className="backfill">
      <p>
        {backfill.is_complete
          ? `История загружена полностью — с ${formatDate(backfill.target_start)}.`
          : `Загрузка истории: ${backfill.completed} из ${backfill.total} версий валют` +
            (backfill.loaded_from
              ? `, данные есть с ${formatDate(backfill.loaded_from)} (цель — ${formatDate(backfill.target_start)})`
              : ` (цель — с ${formatDate(backfill.target_start)})`) +
            "."}
      </p>
      <progress value={backfill.completed} max={backfill.total} />
      {!backfill.is_complete && (
        <>
          <button onClick={() => void handleRun()} disabled={backfill.is_running}>
            {backfill.is_running ? "Идёт загрузка…" : "Продолжить загрузку"}
          </button>
          <p className="muted small">
            Источник отдаёт не больше 365 дней за запрос, поэтому история грузится порциями; позиция
            сохраняется, и загрузка продолжается с того же места после сбоя или перезапуска.
          </p>
        </>
      )}
      {error && <span className="inline-error">{error}</span>}
    </div>
  );
}

export function RatesPanel({ rates, backfill, onRunBackfill }: Props) {
  return (
    <>
      {backfill && <BackfillStatus backfill={backfill} onRun={onRunBackfill} />}

      {!rates ? (
        <p className="muted">Загрузка…</p>
      ) : rates.count === 0 ? (
        <p className="muted">Курсов в базе пока нет.</p>
      ) : (
        <>
          <div className="toolbar">
            <p>
              Актуальные курсы на <strong>{formatDate(rates.report_date)}</strong>, валют:{" "}
              {rates.count}. У каждой валюты указана дата, к которой относится её курс.
            </p>
            <a className="button secondary" href={REPORT_DOWNLOAD_URL} download>
              Скачать Excel
            </a>
          </div>
          <div className="table-wrap scroll">
            <table>
              <thead>
                <tr>
                  <th>Код</th>
                  <th>Валюта</th>
                  <th className="num">Номинал</th>
                  <th className="num">Курс, BYN</th>
                  <th>Дата курса</th>
                  <th>Периодичность</th>
                  <th className="num">Изменение</th>
                </tr>
              </thead>
              <tbody>
                {rates.rates.map((rate) => (
                  <tr key={rate.code}>
                    <td>
                      <strong>{rate.code}</strong>
                    </td>
                    <td>{rate.name}</td>
                    <td className="num">{rate.scale}</td>
                    <td className="num">{formatRate(rate.rate)}</td>
                    <td>{formatDate(rate.rate_date)}</td>
                    <td>{rate.periodicity === 0 ? "ежедневно" : "ежемесячно"}</td>
                    <td
                      className={`num ${
                        rate.change === null ? "" : rate.change > 0 ? "up" : rate.change < 0 ? "down" : ""
                      }`}
                    >
                      {formatChange(rate.change)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}
