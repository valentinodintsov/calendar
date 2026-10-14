from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Employee


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_employee_profile(sender, instance, created, raw=False, **kwargs):
    """
    У каждого пользователя есть профиль сотрудника. По умолчанию он владелец
    своего календаря; суперпользователь сразу ещё и администратор.
    """
    if raw or not created:
        return
    Employee.objects.get_or_create(
        user=instance,
        defaults={
            "is_owner": getattr(settings, "EMPLOYEE_DEFAULT_IS_OWNER", True),
            "is_observer": getattr(settings, "EMPLOYEE_DEFAULT_IS_OBSERVER", False),
            "is_admin": instance.is_superuser,
        },
    )
