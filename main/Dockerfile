#################################################
#
#   Программа для ЭВМ «Календарь»
#   (агрегатор событий в календарях пользователей)
#
#   © 2026 Одинцов Валентин Николаевич
#   Все права защищены. All rights reserved.
#
#   Автор и правообладатель:
#       Одинцов Валентин Николаевич
#       e-mail: valentinka_santa@mail.ru
#
#   Год создания и первого выпуска: 2026
#
#   Программа охраняется законодательством
#   Российской Федерации об авторском праве
#   (часть четвёртая ГК РФ, ст. 1225, 1259, 1261).
#   Знак охраны авторского права проставлен
#   в соответствии со ст. 1271 ГК РФ.
#
#   Копирование, распространение, модификация,
#   декомпиляция и иное использование программы
#   или её частей без письменного разрешения
#   правообладателя запрещены, если иное
#   не предусмотрено законом.
#
#################################################
# Образ приложения «Календарь»: Django под gunicorn, статику отдаёт само приложение (whitenoise).
# Сборка (из каталога main):  docker build -t calendar .
# Запуск — см. docker-compose.yml и ADMIN_GUIDE.md, раздел «Запуск в Docker».
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_DEBUG=0 \
    DJANGO_SERVE_STATIC=1 \
    DJANGO_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt requirements-docker.txt ./
RUN pip install -r requirements-docker.txt

COPY . .

# Статика собирается в образ; база и файлы событий живут в томе /data
RUN python manage.py collectstatic --noinput \
    && chmod +x docker-entrypoint.sh \
    && useradd --uid 1000 --no-create-home --home-dir /app calendar \
    && mkdir -p /data \
    && chown calendar:calendar /data

USER calendar
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "docker-healthcheck.py"]

ENTRYPOINT ["./docker-entrypoint.sh"]
# Один процесс и несколько потоков: блокировки отправки событий и кэш адресной книги живут в памяти процесса
CMD ["gunicorn", "main.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "120", "--access-logfile", "-", "--no-control-socket"]
