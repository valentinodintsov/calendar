# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""Настройки проекта. Значения, зависящие от машины, берутся из переменных окружения (.env) и local_settings.py."""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Переменные окружения из файла .env рядом с manage.py (образец — .env.example).
# Уже заданные в окружении переменные не перезаписываются.
load_dotenv(BASE_DIR / ".env")


# В рабочей установке задайте свой ключ в DJANGO_SECRET_KEY
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-&wbm$uhw22j75s#nz%*dxw+01*4qr*$_k2e(rv$exfv)mk(ofb")

# В рабочей установке: DJANGO_DEBUG=0
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

# Адреса сайта со схемой через запятую (https://calendar.example.ru) — нужны, когда сайт открывают по HTTPS через прокси
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

# 1 — приложение стоит за обратным прокси (nginx и т. п.), который передаёт X-Forwarded-Proto и X-Forwarded-Host.
# Включайте, только если прокси сам выставляет эти заголовки: иначе их может подделать клиент.
if os.environ.get("DJANGO_BEHIND_PROXY", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True

# Каталог данных: база и файлы событий. По умолчанию — рядом с manage.py; в Docker — том /data
DATA_DIR = Path(os.environ.get("DJANGO_DATA_DIR") or BASE_DIR)


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "events",
    "models_employee",
    "integrations",
    "directory",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
# 1 — статику отдаёт само приложение (whitenoise), отдельный веб-сервер для /static/ не нужен. Так работает образ Docker
if os.environ.get("DJANGO_SERVE_STATIC", "0") == "1":
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "main.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "main.wsgi.application"


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
    }
}


# Требования к паролю локального суперпользователя (createsuperuser)

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


LANGUAGE_CODE = "ru"

TIME_ZONE = "Asia/Yekaterinburg"

USE_I18N = True

USE_TZ = True


STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Загруженные вложения событий (отдаются через /api/attachments/<id>/download/)
MEDIA_ROOT = DATA_DIR / "media"
MEDIA_URL = "media/"

# Сколько файлов можно загрузить одним запросом (лимит 20 МБ на файл — в events/views.py)
DATA_UPLOAD_MAX_NUMBER_FILES = 20

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------- Вход и роли ----------
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "events:calendar"
LOGOUT_REDIRECT_URL = "login"

# Вход по учётной записи Active Directory (models_employee/ad_auth.py). Пароль проверяет контроллер домена;
# служебная учётная запись не нужна. Второй способ — резерв: только локальные суперпользователи (createsuperuser).
AUTHENTICATION_BACKENDS = [
    "models_employee.ad_auth.LocalSuperuserBackend",
    "models_employee.ad_auth.ActiveDirectoryBackend",
]
# Домен yanao.ru: пароли проверяет LDAP-сервер email.yanao.ru (за ним каталог Active Directory yg.loc, корень — cn=yg.loc).
AD_AUTH_DOMAIN = os.environ.get("AD_AUTH_DOMAIN", "yanao.ru")
# Серверы домена через запятую (второй — резерв). Пусто — имя домена (DNS сам отдаёт адреса контроллеров).
AD_AUTH_SERVERS = os.environ.get("AD_AUTH_SERVERS", "email.yanao.ru")
AD_AUTH_SECURITY = os.environ.get("AD_AUTH_SECURITY", "ssl")  # ssl (LDAPS, 636) | starttls (389) | none — пароль открытым текстом
AD_AUTH_PORT = int(os.environ.get("AD_AUTH_PORT", "0")) or None  # пусто — 636 для ssl, иначе 389
AD_AUTH_VERIFY_CERT = os.environ.get("AD_AUTH_VERIFY_CERT", "1") == "1"
AD_AUTH_CA_CERT_FILE = os.environ.get("AD_AUTH_CA_CERT_FILE", "")  # корневой сертификат центра сертификации домена (.pem)
AD_AUTH_BASE_DN = os.environ.get("AD_AUTH_BASE_DN", "cn=yg.loc")  # пусто — из имени домена: DC=yanao,DC=ru
AD_AUTH_TIMEOUT = 10  # секунд на подключение к контроллеру домена
# Группы — полным DN или именем; вложенные группы учитываются.
AD_AUTH_REQUIRE_GROUP = os.environ.get("AD_AUTH_REQUIRE_GROUP", "")  # пусто — войти может любой пользователь домена
AD_AUTH_ADMIN_GROUP = os.environ.get("AD_AUTH_ADMIN_GROUP", "")  # члены группы получают роль «Администратор» (при каждом входе)
# Логины через запятую (ivanov или ivanov@yanao.ru) — роль «Администратор» при каждом входе, независимо от группы
AD_AUTH_ADMIN_USERS = os.environ.get("AD_AUTH_ADMIN_USERS", "")

# Роли новых пользователей (роли складываются; «Менеджер» появляется через делегирование прав,
# «Администратор» — у суперпользователя или по флагу в админке)
EMPLOYEE_DEFAULT_IS_OWNER = True
EMPLOYEE_DEFAULT_IS_OBSERVER = False
# Менеджеры всех календарей с правом чтения: логины через запятую (ivanov или ivanov@yanao.ru)
CALENDAR_MANAGERS = os.environ.get("CALENDAR_MANAGERS", "")

# ---------- Внешние календари (приложение integrations) ----------
# Google Calendar: OAuth-клиент типа «Веб-приложение» из Google Cloud Console.
# Разрешённый URI перенаправления: <адрес сайта>/integrations/google/callback/
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")

# Ключ шифрования паролей приложений и токенов (Fernet.generate_key()). Пусто — выводится из SECRET_KEY.
INTEGRATIONS_ENCRYPTION_KEY = os.environ.get("INTEGRATIONS_ENCRYPTION_KEY", "")

# Окно синхронизации: сколько дней назад и вперёд от сегодняшнего дня
INTEGRATIONS_SYNC_PAST_DAYS = 30
INTEGRATIONS_SYNC_FUTURE_DAYS = 365

# Microsoft Exchange (EWS): адрес по умолчанию при подключении календаря Outlook
INTEGRATIONS_EXCHANGE_URL = os.environ.get("INTEGRATIONS_EXCHANGE_URL", "https://email.yanao.ru/EWS/Exchange.asmx")

# Мгновенная отправка событий во внешние календари при создании, изменении и удалении
INTEGRATIONS_PUSH_ON_SAVE = True
INTEGRATIONS_PUSH_ASYNC = True  # в фоне, не задерживая ответ пользователю

# Домен в UID событий, которые наш календарь создаёт во внешних календарях
INTEGRATIONS_UID_DOMAIN = os.environ.get("INTEGRATIONS_UID_DOMAIN", "calendar.local")

# ---------- Почта: приглашения на события с файлом .ics ----------
# По умолчанию письма выводятся в консоль. Для реальной отправки настройте почтовый сервер в админке
# («Почтовый сервер (отправка приглашений)») или задайте EMAIL_HOST и т. д.
if os.environ.get("EMAIL_HOST"):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = os.environ["EMAIL_HOST"]
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "465"))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "1") == "1"
    EMAIL_USE_TLS = not EMAIL_USE_SSL and os.environ.get("EMAIL_USE_TLS", "0") == "1"
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "calendar@localhost")
# 1 — приглашения уходят от адреса пользователя, создавшего событие (вошедшего в календарь). Включайте, только если
# учётной записи почтового сервера разрешено отправлять от адресов сотрудников («Отправить как»): иначе сервер
# принимает письмо, а потом возвращает его с ошибкой «Not allowed address» (так ведёт себя email.yanao.ru).
# 0 — от общего адреса почтового сервера, ответы приходят владельцу события.
INTEGRATIONS_MAIL_FROM_USER = os.environ.get("INTEGRATIONS_MAIL_FROM_USER", "0") == "1"
# Файлы события вкладываются в письмо-приглашение, пока их общий размер не превысит этот предел (МБ)
INTEGRATIONS_MAIL_MAX_ATTACHMENTS_MB = 20

# ---------- Адресная книга: Active Directory / LDAP (приложение directory) ----------
# Подключение настраивает администратор в админке: «Адресная книга (AD / LDAP)» → «Каталоги».
# Поиск контактов выполняется на сервере каталога при каждом запросе (локальной копии нет).
DIRECTORY_TIMEOUT = 15  # секунд на подключение к серверу каталога
DIRECTORY_MAX_ENTRIES = 50000  # не больше стольких записей при проверке подключения
DIRECTORY_CACHE_SECONDS = 60  # одинаковый запрос повторно не отправляется на сервер в течение этого времени

# ---------- Настройки этой машины (файл не хранится в git) ----------
try:
    from .local_settings import *  # noqa: F401,F403
except ImportError:
    pass
