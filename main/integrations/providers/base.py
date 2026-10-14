"""Общий интерфейс внешних календарей."""

from dataclasses import dataclass

from ..ics import EventData

TIMEOUT = 20  # секунд на запрос к внешнему сервису


class ProviderError(Exception):
    """Ошибка внешнего календаря; текст можно показать пользователю."""


class AuthError(ProviderError):
    """Неверный логин/пароль или отозван доступ."""


class ConflictError(ProviderError):
    """Событие на сервере изменилось с момента последней синхронизации (412)."""


@dataclass
class RemoteItem:
    remote_id: str  # CalDAV: href ресурса; Google: id события
    etag: str
    data: EventData
    # Exchange: событие записано старой версией календаря — ссылки нет в тексте, нужно перезаписать
    needs_rewrite: bool = False


class Provider:
    """Интерфейс адаптера. Все даты — aware datetime."""

    def list_calendars(self) -> list[dict]:
        """[{"id": ..., "name": ..., "color": ..., "read_only": bool}]"""
        raise NotImplementedError

    def list_events(self, start, end) -> list[RemoteItem]:
        raise NotImplementedError

    def create_event(self, data: EventData) -> RemoteItem:
        raise NotImplementedError

    def update_event(self, remote_id: str, etag: str, data: EventData) -> str:
        """Возвращает новый ETag."""
        raise NotImplementedError

    def delete_event(self, remote_id: str, etag: str) -> None:
        raise NotImplementedError
