#!/bin/sh
# © 2026 Одинцов Валентин Николаевич. Все права защищены.
# Точка входа контейнера. Перед запуском сервера обновляет базу и запускает плановую синхронизацию
# внешних календарей. Любая другая команда выполняется как есть, например:
#   docker compose run --rm calendar python manage.py createsuperuser
set -e

if [ "$1" = "gunicorn" ]; then
    python manage.py migrate --noinput

    # SYNC_INTERVAL_MINUTES — как часто выполнять sync_calendars; 0 — не выполнять (запускаете сами по расписанию)
    interval="${SYNC_INTERVAL_MINUTES:-10}"
    if [ "$interval" -gt 0 ] 2>/dev/null; then
        (
            while true; do
                sleep "$((interval * 60))"
                python manage.py sync_calendars || echo "sync_calendars: ошибка, повторим через $interval мин."
            done
        ) &
    fi
fi

exec "$@"
