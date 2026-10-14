"""
Чтение адресной книги из Active Directory / LDAP.

Модуль не зависит от Django: настройки передаются объектом ConnectionSettings,
библиотека ldap3 импортируется только при реальном подключении. Разбор записей
(map_entry) работает с «сырыми» значениями атрибутов (bytes) и тестируется без сервера.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import ssl
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from models_employee.matching import normalize, score_parts

AD = "ad"
LDAP = "ldap"

# Поля контакта → атрибуты каталога. Администратор может переопределить любое поле.
DEFAULT_MAPPING = {
    AD: {
        "id": "objectGUID",
        "display_name": "displayName",
        "first_name": "givenName",
        "last_name": "sn",
        "middle_name": "middleName",
        "email": "mail",
        "phone": "telephoneNumber",
        "mobile": "mobile",
        "title": "title",
        "department": "department",
        "company": "company",
        "office": "physicalDeliveryOfficeName",
    },
    LDAP: {
        "id": "entryUUID",
        "display_name": "displayName",
        "first_name": "givenName",
        "last_name": "sn",
        "middle_name": "",
        "email": "mail",
        "phone": "telephoneNumber",
        "mobile": "mobile",
        "title": "title",
        "department": "ou",
        "company": "o",
        "office": "physicalDeliveryOfficeName",
    },
}
CONTACT_FIELDS = [k for k in DEFAULT_MAPPING[AD] if k != "id"]

DEFAULT_FILTER = {
    # люди (без отключённых учётных записей) и объекты-контакты
    AD: "(&(objectCategory=person)(|(&(objectClass=user)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))(objectClass=contact)))",
    # широкий фильтр: OpenLDAP, 389 DS, FreeIPA, адресные книги почтовых серверов
    LDAP: "(|(objectClass=person)(objectClass=inetOrgPerson)(objectClass=organizationalPerson)(objectClass=user)(objectClass=contact))",
}

# Атрибуты, которые всегда пробуем запросить для имени, если основные пусты
NAME_FALLBACK_ATTRS = ["cn", "name"]


class DirectoryError(Exception):
    """Ошибка подключения или чтения каталога; текст можно показать администратору."""

    def __init__(self, message, network=False, details=None):
        super().__init__(message)
        self.network = network  # сервер недоступен по сети (а не ошибка входа / поиска)
        self.details = details or []  # строки сетевой диагностики


NETWORK_ERRORS = ("LDAPSocketOpenError", "LDAPServerPoolExhaustedError", "LDAPSocketReceiveError", "LDAPSocketCloseError")


@dataclass
class ConnectionSettings:
    kind: str = LDAP
    host: str = ""  # одно или несколько имён через запятую/пробел; можно ldap:// или ldaps://
    port: int | None = None
    security: str = "auto"  # auto | ssl | starttls | none
    verify_certificate: bool = True
    ca_cert_file: str = ""
    bind_dn: str = ""
    password: str = ""
    base_dn: str = ""
    search_filter: str = ""
    mapping: dict = field(default_factory=dict)
    page_size: int = 500
    timeout: int = 15
    max_entries: int = 50000

    def effective_mapping(self) -> dict:
        m = dict(DEFAULT_MAPPING.get(self.kind, DEFAULT_MAPPING[LDAP]))
        m.update({k: (v or "").strip() for k, v in (self.mapping or {}).items() if k in m})
        return m

    def effective_filter(self) -> str:
        return (self.search_filter or "").strip() or DEFAULT_FILTER.get(self.kind, DEFAULT_FILTER[LDAP])

    def attributes(self) -> list[str]:
        attrs = {a for a in self.effective_mapping().values() if a}
        attrs.update(NAME_FALLBACK_ATTRS)
        return sorted(attrs, key=str.lower)


# ---------------------------------------------------------------- адреса серверов


@dataclass
class Endpoint:
    host: str
    port: int
    use_ssl: bool
    start_tls: bool


def parse_endpoints(cfg: ConnectionSettings) -> list[Endpoint]:
    """
    'email.yanao.ru' + порт 636 → LDAPS; 'ldap://dc1, ldap://dc2' → два сервера (резерв).
    security=auto: SSL для ldaps:// и порта 636, иначе без шифрования.
    """
    raw = [h for h in re.split(r"[\s,;]+", cfg.host or "") if h]
    if not raw:
        raise DirectoryError("Не указано имя сервера.")
    result = []
    for item in raw:
        m = re.fullmatch(r"(?:(ldaps?)://)?(\[[^\]]+\]|[^:/]+)(?::(\d+))?/?", item, re.IGNORECASE)
        if not m:
            raise DirectoryError(f"Некорректное имя сервера: {item}")
        scheme, host, port = (m.group(1) or "").lower(), m.group(2), m.group(3)
        port = int(port) if port else (cfg.port or None)
        security = cfg.security
        if security == "auto":
            security = "ssl" if scheme == "ldaps" or port == 636 else "none"
        if port is None:
            port = 636 if security == "ssl" else 389
        result.append(Endpoint(host=host, port=port, use_ssl=security == "ssl", start_tls=security == "starttls"))
    return result


# ---------------------------------------------------------------- разбор записей


def _values(attrs: dict, name: str) -> list:
    if not name:
        return []
    return attrs.get(name.lower()) or []


def _text(value) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8").strip()
        except UnicodeDecodeError:
            return value.decode("cp1251", errors="replace").strip()
    return str(value).strip()


def _first(attrs, name) -> str:
    for v in _values(attrs, name):
        t = _text(v)
        if t:
            return t
    return ""


def _external_id(dn: str, attrs: dict, id_attr: str) -> str:
    vals = _values(attrs, id_attr)
    if vals:
        raw = vals[0]
        if id_attr.lower() == "objectguid" and isinstance(raw, bytes) and len(raw) == 16:
            return str(uuid.UUID(bytes_le=raw))
        t = _text(raw)
        if t:
            return t.lower()[:255]
    return ("dn:" + dn.strip().lower())[:255]


def map_entry(dn: str, attrs: dict, mapping: dict) -> dict | None:
    """
    Запись каталога → поля контакта. attrs: {имя атрибута (любой регистр): [bytes|str, ...]}.
    Записи без имени и без e-mail пропускаются (служебные объекты).
    """
    attrs = {k.lower(): (v if isinstance(v, (list, tuple)) else [v]) for k, v in (attrs or {}).items()}
    data = {f: _first(attrs, mapping.get(f, "")) for f in CONTACT_FIELDS}
    if not data["display_name"]:
        parts = [data["last_name"], data["first_name"], data["middle_name"]]
        data["display_name"] = " ".join(p for p in parts if p) or next(
            (_first(attrs, a) for a in NAME_FALLBACK_ATTRS if _first(attrs, a)), ""
        )
    if not data["display_name"] and not data["email"]:
        return None
    if not data["display_name"]:
        data["display_name"] = data["email"]
    if data["email"] and "@" not in data["email"]:
        data["email"] = ""
    limits = {"display_name": 255, "first_name": 150, "last_name": 150, "middle_name": 150, "email": 254,
              "phone": 64, "mobile": 64, "title": 255, "department": 255, "company": 255, "office": 255}
    data = {k: v[: limits[k]] for k, v in data.items()}
    data["external_id"] = _external_id(dn, attrs, mapping.get("id", ""))
    data["dn"] = dn[:1000]
    return data


# ---------------------------------------------------------------- поиск на сервере каталога

MIN_QUERY = 2  # символов
# Поля, по которым ищем: по началу значения (быстро, по индексу) или по вхождению где угодно
PREFIX_FIELDS = ["last_name", "first_name", "middle_name", "email"]
SUBSTRING_FIELDS = ["display_name", "title", "department", "office"]
PHONE_FIELDS = ["phone", "mobile"]


def escape_filter(value: str) -> str:
    """Экранирование значения для LDAP-фильтра (RFC 4515): \\ * ( ) и NUL."""
    return (
        value.replace("\\", "\\5c").replace("*", "\\2a").replace("(", "\\28").replace(")", "\\29").replace("\x00", "\\00")
    )


def phone_digits(query: str) -> str:
    """Цифры запроса, если он похож на телефон (иначе пусто)."""
    q = query.strip()
    digits = re.sub(r"\D", "", q)
    return digits if len(digits) >= 3 and re.fullmatch(r"[\d\s()+\-.]+", q) else ""


def build_search_filter(cfg: ConnectionSettings, query: str) -> str:
    """
    LDAP-фильтр для поиска по запросу пользователя: (&<фильтр каталога><условие для каждого слова>).
    Каждое слово должно найтись в каком-нибудь поле: по началу — в фамилии, имени, отчестве, e-mail;
    по вхождению — в полном имени, должности, подразделении, кабинете. Для AD добавляется ANR.
    Телефон ищется по цифрам в любом формате: «41234» → (telephoneNumber=*4*1*2*3*4*).
    """
    m = cfg.effective_mapping()
    base = cfg.effective_filter()
    digits = phone_digits(query)
    if digits:
        pattern = "*" + "*".join(digits) + "*"
        attrs = [m[f] for f in PHONE_FIELDS if m.get(f)] or ["telephoneNumber"]
        return f"(&{base}(|{''.join(f'({a}={pattern})' for a in attrs)}))"
    tokens = [t for t in re.split(r"\s+", query.strip()) if t][:5]
    clauses = []
    for t in tokens:
        parts = []
        # сервер различает «е» и «ё» — ищем оба написания
        variants = dict.fromkeys([t, t.replace("ё", "е").replace("Ё", "Е"), t.replace("е", "ё").replace("Е", "Ё")])
        for variant in variants:
            v = escape_filter(variant)
            parts += [f"({m[f]}={v}*)" for f in PREFIX_FIELDS if m.get(f)]
            parts += [f"({m[f]}=*{v}*)" for f in SUBSTRING_FIELDS if m.get(f)]
            parts.append(f"(cn=*{v}*)")
            if cfg.kind == AD:
                parts.append(f"(anr={v})")  # Ambiguous Name Resolution: displayName, sn, givenName, sAMAccountName, …
        clauses.append("(|" + "".join(dict.fromkeys(parts)) + ")")
    return f"(&{base}{''.join(clauses)})"


def rank_entries(entries, query: str, mapping: dict, limit: int = 20, source: str = ""):
    """Записи каталога → контакты, отсортированные по степени совпадения (лучшие первыми)."""
    digits = phone_digits(query)
    ranked, seen = [], set()
    for dn, attrs in entries:
        c = map_entry(dn, attrs, mapping)
        if c is None or c["external_id"] in seen:
            continue
        seen.add(c["external_id"])
        if digits:
            phones = [re.sub(r"\D", "", c[f]) for f in PHONE_FIELDS]
            if not any(digits in p for p in phones):
                continue  # «*4*1*2*3*4*» шире, чем нужно, — оставляем только настоящие совпадения
            score = 4
        else:
            score = score_parts(
                query, c["first_name"], c["last_name"], c["email"], "",
                full_names=[c["display_name"], f"{c['last_name']} {c['first_name']} {c['middle_name']}"],
                extra=[c["title"], c["department"], c["company"], c["office"], c["phone"], c["mobile"]],
            )
            if score is None:
                score = 5  # сервер нашёл (например, через ANR или cn), но слова не совпали явно — в конец
        c["source"] = source
        ranked.append((score, normalize(c["display_name"]), c))
    ranked.sort(key=lambda r: r[:2])
    return [(score, c) for score, _, c in ranked[:limit]]


# Повторно используемые соединения: открывать LDAPS и входить на каждое нажатие клавиши слишком долго
_POOL: dict = {}
_POOL_LOCK = threading.Lock()
POOL_IDLE_SECONDS = 300


class _PoolEntry:
    def __init__(self):
        self.lock = threading.Lock()
        self.conn = None
        self.used_at = 0.0

    def drop(self):
        if self.conn is not None:
            try:
                self.conn.unbind()
            except Exception:  # noqa: BLE001
                pass
        self.conn = None


def _pool_key(cfg: ConnectionSettings) -> str:
    raw = repr((cfg.host, cfg.port, cfg.security, cfg.verify_certificate, cfg.ca_cert_file, cfg.bind_dn, cfg.password))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def reset_pool():
    with _POOL_LOCK:
        for entry in _POOL.values():
            with entry.lock:
                entry.drop()
        _POOL.clear()


# коды результата LDAP → сообщения
RESULT_MESSAGES = {
    1: "Сервер требует входа: укажите имя пользователя (Bind DN) и пароль.",
    8: "Сервер требует шифрования: используйте порт 636 (LDAPS) или StartTLS.",
    13: "Сервер требует шифрования: используйте порт 636 (LDAPS) или StartTLS.",
    32: "Корневой элемент (Base DN) не найден на сервере.",
    49: "Неверное имя пользователя (Bind DN) или пароль.",
    50: "У учётной записи нет прав на поиск в каталоге.",
    87: "Некорректный фильтр поиска (проверьте фильтр в настройках каталога).",
}
OK_RESULTS = {0, 3, 4, 10}  # успех, превышен лимит времени/размера (частичный ответ), ссылка


# ---------------------------------------------------------------- подключение (ldap3)


def _is_network(exc) -> bool:
    return type(exc).__name__ in NETWORK_ERRORS or "timed out" in str(exc).lower()


def _friendly(exc) -> str:
    name = type(exc).__name__
    text = str(exc)
    low = text.lower()
    if "invalidcredentials" in low.replace(" ", "") or "52e" in low or name == "LDAPInvalidCredentialsResult":
        return "Неверное имя пользователя (Bind DN) или пароль."
    if "data 775" in low:
        return "Учётная запись для подключения заблокирована."
    if "data 532" in low or "data 773" in low:
        return "Срок действия пароля учётной записи для подключения истёк."
    if "certificate" in low or "ssl" in name.lower() and "verify" in low:
        return f"Ошибка проверки сертификата сервера: {text}. Укажите файл корневого сертификата или отключите проверку."
    if name in ("LDAPSocketOpenError", "LDAPServerPoolExhaustedError", "LDAPSocketReceiveError") or "timed out" in low:
        return f"Сервер каталога недоступен: {text}"
    if name in ("LDAPStrongerAuthRequiredResult", "LDAPConfidentialityRequiredResult") or "strongerauth" in low:
        return "Сервер требует шифрования: используйте порт 636 (LDAPS) или StartTLS."
    if name == "LDAPNoSuchObjectResult" or "nosuchobject" in low:
        return "Корневой элемент (Base DN) не найден на сервере."
    if name == "LDAPInvalidFilterError" or "filter" in low and "invalid" in low:
        return "Некорректный фильтр поиска."
    if name == "LDAPStartTLSError":
        return f"Не удалось включить StartTLS: {text}"
    if name == "LDAPOperationsErrorResult" and "bind" in low:
        return "Сервер требует входа: укажите имя пользователя (Bind DN) и пароль."
    return f"Ошибка каталога ({name}): {text}"


def _tls(ldap3, cfg: ConnectionSettings, host: str):
    """
    Параметры TLS для сервера. Имя сервера передаётся в SNI: без него сервер с несколькими сертификатами
    (например, почтовый) отдаёт сертификат по умолчанию, и проверка не проходит. Для IP-адреса SNI не бывает.
    """
    name = host.strip("[]")
    try:
        ipaddress.ip_address(name)
        name = None
    except ValueError:
        pass
    return ldap3.Tls(
        validate=ssl.CERT_REQUIRED if cfg.verify_certificate else ssl.CERT_NONE,
        ca_certs_file=cfg.ca_cert_file or None,
        sni=name,
    )


class DirectoryClient:
    """Подключение к каталогу через ldap3 (чистый Python, работает и на Windows)."""

    def __init__(self, cfg: ConnectionSettings):
        self.cfg = cfg

    def connect(self, raise_exceptions=True):
        """Открытое соединение со входом под cfg.bind_dn; закрывает (unbind) вызывающий."""
        return self._connect(raise_exceptions)

    def _connect(self, raise_exceptions=True):
        try:
            import ldap3
            from ldap3.core.exceptions import LDAPException
        except ImportError as exc:
            raise DirectoryError("Не установлена библиотека ldap3: pip install ldap3") from exc

        cfg = self.cfg
        endpoints = parse_endpoints(cfg)
        servers = [
            ldap3.Server(e.host, port=e.port, use_ssl=e.use_ssl, tls=_tls(ldap3, cfg, e.host), get_info=ldap3.NONE,
                         connect_timeout=cfg.timeout)
            for e in endpoints
        ]
        target = servers[0] if len(servers) == 1 else ldap3.ServerPool(servers, ldap3.FIRST, active=1, exhaust=True)
        user = cfg.bind_dn.strip() or None
        auth = ldap3.ANONYMOUS
        if user:
            auth = ldap3.NTLM if "\\" in user else ldap3.SIMPLE
        auto_bind = ldap3.AUTO_BIND_TLS_BEFORE_BIND if endpoints[0].start_tls else ldap3.AUTO_BIND_NO_TLS
        try:
            return ldap3.Connection(
                target,
                user=user,
                password=cfg.password or None,
                authentication=auth,
                auto_bind=auto_bind,
                read_only=True,
                raise_exceptions=raise_exceptions,
                auto_referrals=False,  # AD отдаёт ссылки на DomainDnsZones и т. п. — не ходим по ним
                receive_timeout=max(cfg.timeout, 30),
            )
        except LDAPException as exc:
            raise DirectoryError(_friendly(exc), network=_is_network(exc)) from exc

    def iter_entries(self, limit: int | None = None):
        """Генератор (dn, {атрибут: [bytes, ...]}) — постраничный поиск по всему поддереву Base DN."""
        from ldap3 import SUBTREE
        from ldap3.core.exceptions import LDAPException

        cfg = self.cfg
        if not cfg.base_dn.strip():
            raise DirectoryError("Не указан корневой элемент (Base DN).")
        conn = self._connect()
        count = 0
        try:
            gen = conn.extend.standard.paged_search(
                search_base=cfg.base_dn.strip(),
                search_filter=cfg.effective_filter(),
                search_scope=SUBTREE,
                attributes=cfg.attributes(),
                paged_size=max(10, min(cfg.page_size, 1000)),
                generator=True,
            )
            for item in gen:
                if item.get("type") != "searchResEntry":
                    continue  # ссылки (referrals) пропускаем
                yield item.get("dn", ""), item.get("raw_attributes") or {}
                count += 1
                if limit and count >= limit:
                    break
        except LDAPException as exc:
            if type(exc).__name__ in ("LDAPSizeLimitExceededResult",):
                return
            raise DirectoryError(_friendly(exc), network=_is_network(exc)) from exc
        finally:
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001
                pass

    def search(self, query: str, limit: int = 20, time_limit: int = 5):
        """
        Поиск на сервере каталога: [(dn, {атрибут: [bytes, ...]}), ...], не больше limit записей.
        Соединение берётся из пула и переиспользуется; разорванное — переоткрывается один раз.
        """
        from ldap3 import SUBTREE
        from ldap3.core.exceptions import LDAPException

        cfg = self.cfg
        if not cfg.base_dn.strip():
            raise DirectoryError("Не указан корневой элемент (Base DN).")
        search_filter = build_search_filter(cfg, query)
        key = _pool_key(cfg)
        with _POOL_LOCK:
            entry = _POOL.setdefault(key, _PoolEntry())
        with entry.lock:
            for attempt in (0, 1):
                if entry.conn is not None and (entry.conn.closed or time.monotonic() - entry.used_at > POOL_IDLE_SECONDS):
                    entry.drop()  # сервер мог закрыть простаивающее соединение
                if entry.conn is None:
                    entry.conn = self._connect(raise_exceptions=False)
                try:
                    entry.conn.search(
                        search_base=cfg.base_dn.strip(),
                        search_filter=search_filter,
                        search_scope=SUBTREE,
                        attributes=cfg.attributes(),
                        size_limit=max(1, min(limit, 500)),
                        time_limit=time_limit,
                    )
                except LDAPException as exc:
                    entry.drop()
                    if attempt == 0:
                        continue  # соединение разорвано — пробуем ещё раз с новым
                    raise DirectoryError(_friendly(exc), network=_is_network(exc)) from exc
                entry.used_at = time.monotonic()
                result = entry.conn.result or {}
                code = result.get("result", 0)
                if code in OK_RESULTS:
                    response = list(entry.conn.response or [])
                    break
                if code in (51, 52, 80, 81) and attempt == 0:  # busy / unavailable / server down
                    entry.drop()
                    continue
                entry.drop()
                raise DirectoryError(RESULT_MESSAGES.get(code) or f"Ошибка каталога: {result.get('description', code)} {result.get('message', '')}".strip())
        return [
            (item.get("dn", ""), item.get("raw_attributes") or {})
            for item in response
            if item.get("type") == "searchResEntry"
        ]

    def test(self, sample_size: int = 5) -> dict:
        """Проверка подключения: вход, поиск нескольких записей, какие атрибуты пришли."""
        mapping = self.cfg.effective_mapping()
        samples, seen_attrs, total = [], set(), 0
        try:
            entries = list(self.iter_entries(limit=sample_size))
        except DirectoryError as exc:
            if exc.network and not exc.details:
                exc.details = diagnose(self.cfg)
            raise
        for dn, attrs in entries:
            total += 1
            seen_attrs.update(attrs.keys())
            mapped = map_entry(dn, attrs, mapping)
            samples.append({"dn": dn, "name": mapped["display_name"] if mapped else "", "email": mapped["email"] if mapped else ""})
        return {"found": total, "samples": samples, "attributes": sorted(seen_attrs, key=str.lower)}


# ---------------------------------------------------------------- сетевая диагностика

LDAP_PORTS = [636, 389, 3269, 3268]
PORT_NAMES = {636: "LDAPS", 389: "LDAP / StartTLS", 3269: "глобальный каталог AD, SSL", 3268: "глобальный каталог AD"}


def _tcp_check(host: str, port: int, timeout: float):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "открыт"
    except socket.timeout:
        return False, "нет ответа — вероятно, закрыт файрволом или сервер недоступен из этой сети"
    except ConnectionRefusedError:
        return False, "соединение отклонено — на этом порту служба не запущена"
    except OSError as exc:
        return False, exc.strerror or str(exc)


def diagnose(cfg: ConnectionSettings, timeout: float = 4) -> list[str]:
    """
    Где обрывается связь с каталогом: DNS → TCP-порты LDAP. Возвращает строки для показа администратору.
    Проверяется с той машины, где работает календарь.
    """
    try:
        endpoints = parse_endpoints(cfg)
    except DirectoryError as exc:
        return [str(exc)]
    lines = []
    for ep in endpoints:
        try:
            addrs = sorted({i[4][0] for i in socket.getaddrinfo(ep.host, None, type=socket.SOCK_STREAM)})
        except socket.gaierror as exc:
            lines.append(f"{ep.host}: имя не найдено в DNS ({exc.strerror or exc}). Проверьте имя сервера и настройки DNS.")
            continue
        lines.append(f"{ep.host} → {', '.join(addrs)}")
        ports = [ep.port] + [p for p in LDAP_PORTS if p != ep.port]
        with ThreadPoolExecutor(max_workers=len(ports)) as pool:
            results = dict(zip(ports, pool.map(lambda p: _tcp_check(ep.host, p, timeout), ports)))
        for p in ports:
            ok, why = results[p]
            label = PORT_NAMES.get(p, "")
            mark = " (указан в настройках)" if p == ep.port else ""
            lines.append(f"порт {p}{' — ' + label if label else ''}{mark}: {why}")
        if results[ep.port][0]:
            lines.append("Порт открыт — значит, сервер ответил, но слишком медленно: увеличьте DIRECTORY_TIMEOUT или проверьте шифрование.")
            continue
        alive = [p for p in ports[1:] if results[p][0]]
        if 389 in alive:
            lines.append("Совет: укажите порт 389 и шифрование «StartTLS» (если сервер его не поддерживает — «Без шифрования»; "
                         "тогда пароль передаётся открытым текстом, используйте только во внутренней сети).")
        elif alive:
            lines.append(f"Совет: попробуйте порт {alive[0]} ({PORT_NAMES.get(alive[0], '')}).")
        else:
            lines.append("Ни один LDAP-порт не отвечает. Скорее всего, каталог доступен только из внутренней сети организации "
                         "(или через VPN), либо адрес LDAP-сервера другой. Уточните у администратора сети адрес и порт "
                         "и откройте доступ с сервера календаря.")
    return lines
