# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""
Поиск контактов на серверах каталогов (Active Directory / LDAP) по запросу пользователя.
Контакты не хранятся в базе календаря: каждый запрос уходит на сервер (одинаковые — 60 секунд из кэша).
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from models_employee.matching import EXACT, normalize

from .ldap_client import MIN_QUERY, DirectoryClient, DirectoryError, rank_entries
from .models import DirectorySource

log = logging.getLogger(__name__)
CONTACT_FIELDS = ["display_name", "last_name", "first_name", "middle_name", "email", "phone", "mobile",
                  "title", "department", "company", "office"]


def get_client(source: DirectorySource) -> DirectoryClient:
    """Точка подмены в тестах."""
    return DirectoryClient(source.connection_settings())


def _cache_key(source: DirectorySource, query: str) -> str:
    raw = f"{source.pk}:{source.connection_settings().__dict__!r}:{normalize(query)}"
    return "directory:search:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _search_source(source: DirectorySource, query: str):
    """[(score, contact_dict)] из одного каталога; ошибки — DirectoryError."""
    key = _cache_key(source, query)
    cached = cache.get(key)
    if cached is not None:
        return cached
    client = get_client(source)
    cfg = source.connection_settings()
    entries = client.search(query, limit=source.max_results or 50)
    ranked = rank_entries(entries, query, cfg.effective_mapping(), limit=source.max_results or 50, source=source.name)
    for _, c in ranked:
        c["id"] = f"{source.pk}:{c['external_id']}"
        c["source_id"] = source.pk
    cache.set(key, ranked, getattr(settings, "DIRECTORY_CACHE_SECONDS", 60))
    return ranked


def _record(source: DirectorySource, error: str = ""):
    """Состояние каталога для администратора; пишем в базу, только когда оно меняется."""
    now = timezone.now()
    if error:
        if error != source.last_error or not source.last_error_at or (now - source.last_error_at).total_seconds() > 300:
            DirectorySource.objects.filter(pk=source.pk).update(last_error=error[:2000], last_error_at=now)
    elif source.last_error or not source.last_ok_at or (now - source.last_ok_at).total_seconds() > 300:
        DirectorySource.objects.filter(pk=source.pk).update(last_error="", last_ok_at=now)


def search_directory(query: str, limit: int = 20) -> dict:
    """
    Ищет во всех включённых каталогах параллельно.
    Возвращает {"results": [контакты, лучшие первыми], "errors": [{"source", "message"}]}.
    """
    query = (query or "").strip()
    sources = list(DirectorySource.objects.filter(enabled=True))
    if len(query) < MIN_QUERY or not sources:
        return {"results": [], "errors": []}

    def run(source):
        # в потоке — только запрос к серверу каталога; база данных — в основном потоке
        try:
            return source, _search_source(source, query), None
        except DirectoryError as exc:
            return source, [], str(exc)
        except Exception as exc:  # noqa: BLE001 — один каталог не должен ломать поиск в остальных
            log.exception("Ошибка поиска в каталоге %s", source.pk)
            return source, [], f"Внутренняя ошибка: {exc.__class__.__name__}"

    if len(sources) == 1:
        outcomes = [run(sources[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(len(sources), 4)) as pool:
            outcomes = list(pool.map(run, sources))
    for source, _, error in outcomes:
        _record(source, error or "")

    merged, errors, seen_emails = [], [], set()
    for source, ranked, error in outcomes:
        if error:
            errors.append({"source": source.name, "message": error})
        for score, c in ranked:
            email = c["email"].lower()
            if email and email in seen_emails:
                continue  # один человек в двух каталогах — показываем один раз
            if email:
                seen_emails.add(email)
            merged.append((score, normalize(c["display_name"]), c))
    merged.sort(key=lambda r: r[:2])
    merged = merged[:limit]

    # пользователь календаря с тем же e-mail — его можно добавить в событие участником
    emails = {c["email"].lower() for _, _, c in merged if c["email"]}
    users = {}
    if emails:
        cond = Q()
        for e in emails:
            cond |= Q(email__iexact=e)
        for pk, email in get_user_model().objects.filter(cond, is_active=True).values_list("pk", "email"):
            users.setdefault(email.lower(), pk)

    results = []
    for score, _, c in merged:
        item = {k: c.get(k, "") for k in CONTACT_FIELDS}
        item.update(
            id=c["id"],
            name=c["display_name"],
            source=c["source"],
            user_id=users.get(c["email"].lower()) if c["email"] else None,
            exact=score == EXACT,
        )
        item.pop("display_name")
        results.append(item)
    return {"results": results, "errors": errors}


def emails_in_directory(emails, max_lookups: int = 10) -> set[str]:
    """
    Какие из адресов есть в адресной книге (точное совпадение e-mail), в нижнем регистре.
    Нужна, чтобы гость, вписанный вручную, но найденный в AD/LDAP, тоже получил событие в свой календарь.
    Ошибки каталога не мешают сохранению события — такие адреса просто не отмечаются.
    """
    found = set()
    if not DirectorySource.objects.filter(enabled=True).exists():
        return found
    for email in [e.strip().lower() for e in emails if e and "@" in e][:max_lookups]:
        try:
            res = search_directory(email, limit=5)
        except Exception:  # noqa: BLE001
            log.exception("Проверка адреса %s в каталоге", email)
            continue
        if any((c.get("email") or "").lower() == email for c in res["results"]):
            found.add(email)
    return found
