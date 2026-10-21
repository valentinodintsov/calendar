# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""
Ролевая модель календаря. Роли у пользователя не взаимоисключающие — права складываются.

Действия:
  * Запись    — создание новых событий (в календаре владельца);
  * Чтение    — просмотр событий со всеми подробностями: описание, место, ссылка, участники, файлы;
  * Изменение — редактирование, перенос и удаление событий и их файлов.
    Изменять можно только то, что можно прочитать: Изменение ⇒ Чтение.

Роли:
  * Владелец      (Employee.is_owner, по умолчанию у всех) — запись, чтение и изменение событий
                  своего календаря; делегирует другим сотрудникам права на каждое действие;
  * Менеджер      (не флаг, а наличие Delegation) — запись, чтение и изменение в календаре
                  владельца по делегированным правам; можно быть менеджером нескольких календарей;
                  пользователи из настройки CALENDAR_MANAGERS — менеджеры всех календарей с правом чтения;
  * Наблюдатель   (Employee.is_observer) — чтение и изменение событий всех календарей;
  * Администратор (Employee.is_admin; суперпользователь — всегда) — запись, чтение и изменение
                  всех событий, управление ролями.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q


class Role(models.TextChoices):
    OWNER = "owner", "Владелец"
    MANAGER = "manager", "Менеджер"
    OBSERVER = "observer", "Наблюдатель"
    ADMIN = "admin", "Администратор"


def default_role():
    """Оставлено для миграции 0001 (там у Employee было поле role)."""
    return Role.OBSERVER


class Employee(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="employee",
        verbose_name="Пользователь",
    )
    is_owner = models.BooleanField(
        "Владелец",
        default=True,
        help_text="Ведёт свой календарь: создаёт, читает и изменяет свои события, делегирует права.",
    )
    is_observer = models.BooleanField(
        "Наблюдатель",
        default=False,
        help_text="Читает и изменяет события всех календарей.",
    )
    is_admin = models.BooleanField(
        "Администратор",
        default=False,
        help_text="Полный доступ ко всем событиям и управление ролями (даёт вход в админку).",
    )
    position = models.CharField("Должность", max_length=150, blank=True)
    ad_guid = models.CharField(
        "Учётная запись домена (objectGUID)", max_length=36, null=True, blank=True, unique=True, editable=False,
        help_text="Заполняется при входе через Active Directory.",
    )
    preferences = models.JSONField(
        "Личные настройки", default=dict, blank=True,
        help_text="Оформление и вид календаря этого пользователя (окно «Настройки»).",
    )

    class Meta:
        ordering = ["user__username"]
        verbose_name = "Сотрудник"
        verbose_name_plural = "Сотрудники"

    def __str__(self):
        name = self.user.get_full_name() or self.user.get_username()
        roles = ", ".join(Role(r).label.lower() for r in self.roles()) or "без ролей"
        return f"{name} — {roles}"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._initial_admin = self.is_admin

    def roles(self):
        """Все роли сотрудника, включая «Менеджер», если ему делегированы права."""
        from .permissions import reads_all_calendars

        result = []
        if self.is_owner:
            result.append(Role.OWNER)
        if self.pk and self.user.received_delegations.exists() or reads_all_calendars(self.user):
            result.append(Role.MANAGER)
        if self.is_observer:
            result.append(Role.OBSERVER)
        if self.is_admin or self.user.is_superuser:
            result.append(Role.ADMIN)
        return result

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self._sync_staff_flag()
        self._initial_admin = self.is_admin

    def _sync_staff_flag(self):
        """Администратору нужен вход в админку; при снятии роли доступ забираем."""
        user = self.user
        if self.is_admin and not user.is_staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])
        elif self._initial_admin and not self.is_admin and user.is_staff and not user.is_superuser:
            user.is_staff = False
            user.save(update_fields=["is_staff"])


class Delegation(models.Model):
    """
    Права, которые владелец выдал менеджеру (любому другому сотруднику).
    Получив права, сотрудник становится менеджером этого календаря.
    event = NULL — на все события владельца, иначе — только на одно событие.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="granted_delegations",
        verbose_name="Владелец",
    )
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="received_delegations",
        verbose_name="Менеджер",
    )
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="delegations",
        verbose_name="Событие",
        help_text="Пусто — права на все события владельца.",
    )
    can_create = models.BooleanField("Запись", default=False, help_text="Создание новых событий в календаре владельца.")
    can_read = models.BooleanField("Чтение", default=False)
    can_change = models.BooleanField("Изменение", default=False, help_text="Включает чтение.")
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Изменено", auto_now=True)

    class Meta:
        ordering = ["owner_id", "manager_id", "event_id"]
        verbose_name = "Делегирование прав"
        verbose_name_plural = "Делегирование прав"
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "manager"],
                condition=Q(event__isnull=True),
                name="delegation_unique_owner_level",
            ),
            models.UniqueConstraint(
                fields=["owner", "manager", "event"],
                name="delegation_unique_event_level",
            ),
            models.CheckConstraint(
                condition=~Q(owner=F("manager")),
                name="delegation_owner_not_manager",
            ),
        ]

    def __str__(self):
        scope = f"«{self.event.title}»" if self.event_id else "все события"
        return f"{self.owner} → {self.manager}: {', '.join(self.actions_display()) or 'нет прав'} ({scope})"

    def actions_display(self):
        return [
            label
            for flag, label in (
                (self.can_create, "запись"),
                (self.can_read, "чтение"),
                (self.can_change, "изменение"),
            )
            if flag
        ]

    def normalize(self):
        """Изменять можно только то, что можно прочитать: изменение ⇒ чтение."""
        if self.can_change:
            self.can_read = True

    def clean(self):
        from .permissions import get_roles

        errors = {}
        if self.owner_id and not ({Role.OWNER, Role.ADMIN} & get_roles(self.owner)):
            errors["owner"] = "Делегировать права может только владелец календаря или администратор."
        if self.owner_id and self.owner_id == self.manager_id:
            errors["manager"] = "Нельзя делегировать права самому себе."
        if self.event_id and self.owner_id and self.event.owner_id != self.owner_id:
            errors["event"] = "Событие принадлежит другому владельцу."
        if self.event_id and self.can_create:
            errors["can_create"] = "Право записи (создания событий) выдаётся на весь календарь, а не на одно событие."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.normalize()
        super().save(*args, **kwargs)
