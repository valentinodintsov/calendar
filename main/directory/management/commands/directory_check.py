# © 2026 Одинцов Валентин Николаевич. Все права защищены.
from django.core.management.base import BaseCommand

from directory.ldap_client import DirectoryError, diagnose, rank_entries
from directory.models import DirectorySource
from directory.search import get_client


class Command(BaseCommand):
    help = (
        "Проверка каталогов адресной книги (Active Directory / LDAP): подключение, поиск, сетевая диагностика. "
        "Контакты не загружаются в базу — поиск всегда идёт на сервере каталога."
    )

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, action="append", help="Только каталог с этим id (можно несколько)")
        parser.add_argument("--search", metavar="ЗАПРОС", help="Выполнить поиск на сервере, как в адресной книге")
        parser.add_argument("--diagnose", action="store_true", help="Проверить DNS и доступность LDAP-портов (без входа)")

    def handle(self, *args, **opts):
        qs = DirectorySource.objects.all()
        if opts.get("id"):
            qs = qs.filter(pk__in=opts["id"])
        if not qs.exists():
            self.stdout.write("Каталоги не настроены.")
            return
        for source in qs:
            title = f"[{source.pk}] {source}{'' if source.enabled else ' (выключен)'}"
            if opts.get("diagnose"):
                self.stdout.write(title + ":")
                for line in diagnose(source.connection_settings()):
                    self.stdout.write("    " + line)
                continue
            try:
                if opts.get("search"):
                    q = opts["search"]
                    entries = get_client(source).search(q, limit=source.max_results or 50)
                    ranked = rank_entries(entries, q, source.connection_settings().effective_mapping(), limit=20)
                    self.stdout.write(self.style.SUCCESS(f"{title}: «{q}» — найдено {len(ranked)}"))
                    for _, c in ranked:
                        extra = " · ".join(x for x in (c["title"], c["department"], c["phone"]) if x)
                        self.stdout.write(f"    {c['display_name']} <{c['email']}>  {extra}")
                else:
                    info = get_client(source).test()
                    self.stdout.write(self.style.SUCCESS(f"{title}: подключение успешно, записей (первые): {info['found']}"))
                    for s in info["samples"]:
                        self.stdout.write(f"    {s['name']} <{s['email']}>  {s['dn']}")
                    self.stdout.write(f"    атрибуты: {', '.join(info['attributes'])}")
            except DirectoryError as exc:
                self.stderr.write(self.style.ERROR(f"{title}: {exc}"))
                for line in exc.details:
                    self.stderr.write("    " + line)
