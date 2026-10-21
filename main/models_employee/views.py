# © 2026 Одинцов Валентин Николаевич. Все права защищены.
import json

from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from .models import Delegation
from .permissions import Access, api_login_required, forbidden
from .preferences import update_preferences
from .search import search_users, user_card


def _delegation_dict(d):
    return {
        "id": d.pk,
        "manager": d.manager_id,
        "user": user_card(d.manager),
        "event": d.event_id,
        "can_create": d.can_create,
        "can_read": d.can_read,
        "can_change": d.can_change,
    }


@api_login_required
@require_http_methods(["GET", "POST"])
def delegations(request):
    """
    Права, которые текущий владелец выдал менеджерам на все свои события.

    GET  /api/delegations/ → {"delegations": [...]} — кому уже выданы права (с данными сотрудника)
    POST /api/delegations/ {"manager": id, "can_create": bool, "can_read": bool, "can_change": bool}
         Все флаги false — права отзываются.
    """
    access = Access(request.user)
    if not access.can_delegate:
        return forbidden("Делегировать права может только владелец календаря или администратор.")

    User = get_user_model()

    if request.method == "GET":
        rows = (
            Delegation.objects.filter(owner=request.user, event__isnull=True)
            .select_related("manager")
            .order_by("manager__last_name", "manager__first_name", "manager__username")
        )
        return JsonResponse({"delegations": [_delegation_dict(d) for d in rows]})

    try:
        data = json.loads(request.body or b"{}")
        manager_id = int(data.get("manager"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"errors": {"manager": ["Укажите менеджера."]}}, status=400)

    manager = User.objects.filter(pk=manager_id, is_active=True).exclude(pk=request.user.pk).first()
    if manager is None:
        return JsonResponse({"errors": {"manager": ["Сотрудник не найден."]}}, status=400)

    flags = {k: bool(data.get(k)) for k in ("can_create", "can_read", "can_change")}
    if not any(flags.values()):
        Delegation.objects.filter(owner=request.user, manager=manager, event__isnull=True).delete()
        return JsonResponse({"manager": manager.pk, "can_create": False, "can_read": False, "can_change": False})

    delegation, _ = Delegation.objects.update_or_create(
        owner=request.user, manager=manager, event=None, defaults=flags
    )
    return JsonResponse(_delegation_dict(delegation))


@api_login_required
@require_http_methods(["GET"])
def delegation_candidates(request):
    """
    GET /api/delegations/candidates/?q=текст — подсказки для поиска сотрудника
    по фамилии, имени (в любом порядке), e-mail или логину. Не больше 10, лучшие совпадения первыми;
    у полного совпадения "exact": true.
    """
    if not Access(request.user).can_delegate:
        return forbidden("Делегировать права может только владелец календаря или администратор.")
    query = request.GET.get("q", "")
    try:
        limit = max(1, min(int(request.GET.get("limit", 10)), 50))
    except ValueError:
        limit = 10
    # менеджером может стать любой другой активный сотрудник
    users = get_user_model().objects.filter(is_active=True).exclude(pk=request.user.pk)
    results = search_users(users, query, limit=limit)
    return JsonResponse({"query": query, "results": [user_card(u, score) for score, u in results]})


@api_login_required
@require_http_methods(["GET", "PATCH"])
def my_settings(request):
    """
    Личные настройки текущего пользователя — у каждого свои.

    GET   /api/me/settings/ → {"settings": {"theme", "start_view", "last_view"}}
    PATCH /api/me/settings/ {"theme": "dark"} — меняются только переданные значения
    """
    if request.method == "GET":
        return JsonResponse({"settings": Access(request.user).me()["settings"]})
    try:
        data = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict):
        return JsonResponse({"errors": {"__all__": ["Некорректный JSON."]}}, status=400)
    prefs, errors = update_preferences(request.user, data)
    if errors:
        return JsonResponse({"errors": errors}, status=400)
    return JsonResponse({"settings": prefs})
