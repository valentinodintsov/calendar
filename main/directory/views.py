from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from models_employee.permissions import api_login_required, forbidden, is_admin

from .ldap_client import MIN_QUERY, DirectoryError
from .models import DirectorySource
from .search import get_client, search_directory


def _int(value, default, lo, hi):
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- адресная книга: всем пользователям


@api_login_required
@require_GET
def contacts(request):
    """
    GET /api/contacts/?q=текст&limit=20 — поиск на серверах каталогов (лучшие совпадения первыми).
    Доступно любому вошедшему пользователю, независимо от роли. Запрос — от 2 символов.
    """
    admin = is_admin(request.user)
    sources = DirectorySource.objects.all() if admin else DirectorySource.objects.filter(enabled=True)
    q = request.GET.get("q", "").strip()
    found = search_directory(q, _int(request.GET.get("limit"), 20, 1, 100)) if len(q) >= MIN_QUERY else {"results": [], "errors": []}
    errors = found["errors"]
    if not admin:
        # подробности ошибок подключения — только администратору
        errors = [{"source": e["source"], "message": "Каталог временно недоступен."} for e in errors]
    return JsonResponse(
        {
            "configured": DirectorySource.objects.filter(enabled=True).exists(),
            "can_manage": admin,
            "min_query": MIN_QUERY,
            "sources": [s.status_dict(admin=admin) for s in sources],
            "query": q,
            "total": len(found["results"]),
            "results": found["results"],
            "errors": errors,
        }
    )


# ---------------------------------------------------------------- настройка каталогов: только администратору


def _admin_only(request):
    if not is_admin(request.user):
        return forbidden("Настройка адресной книги доступна только администратору.")
    return None


@api_login_required
@require_GET
def sources(request):
    """GET /api/directory/sources/ — каталоги и их состояние (администратор)."""
    denied = _admin_only(request)
    if denied:
        return denied
    return JsonResponse({"sources": [s.status_dict(admin=True) for s in DirectorySource.objects.all()]})


@api_login_required
@require_POST
def source_test(request, pk):
    """POST /api/directory/sources/<id>/test/ — проверить подключение (администратор)."""
    denied = _admin_only(request)
    if denied:
        return denied
    source = get_object_or_404(DirectorySource, pk=pk)
    try:
        info = get_client(source).test()
    except DirectoryError as exc:
        return JsonResponse({"ok": False, "message": str(exc), "network": exc.network, "details": exc.details})
    msg = f"Подключение успешно. Найдено записей (первые): {info['found']}." if info["found"] else (
        "Подключение успешно, но по фильтру ничего не найдено. Проверьте Base DN и фильтр."
    )
    return JsonResponse({"ok": True, "message": msg, **info})
