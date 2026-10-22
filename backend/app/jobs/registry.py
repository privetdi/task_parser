"""Реестр задач: единственное место, где они перечислены.

Чтобы добавить задачу, достаточно функции в tasks.py, записи здесь и (если нужно расписание)
настройки с cron-выражением в config.py.
"""
from app.jobs import tasks
from app.jobs.runner import JobDefinition

JOBS = [
    JobDefinition(
        name="rates_backfill",
        title="Догрузка истории курсов",
        description=(
            "Загружает исторические курсы НБ РБ порциями и запоминает позицию. "
            "Запускается при старте, пока история не загружена полностью."
        ),
        run=tasks.rates_backfill,
    ),
    JobDefinition(
        name="rates_sync",
        title="Проверка курсов НБ РБ",
        description="Ежедневно: добавляет новые и обновляет изменённые курсы без дублей.",
        run=tasks.rates_sync,
        cron_setting="cron_rates_sync",
    ),
    JobDefinition(
        name="rates_report",
        title="Excel-отчёт на почту",
        description="Ежедневно формирует Excel с актуальными курсами и отправляет его на почту.",
        run=tasks.rates_report,
        cron_setting="cron_rates_report",
    ),
    JobDefinition(
        name="iea_charts",
        title="Графики IEA",
        description="Ежедневно: ищет новые графики на первой странице каталога IEA, "
        "скачивает и отправляет каждый отдельным письмом.",
        run=tasks.iea_charts,
        cron_setting="cron_iea_charts",
    ),
]
