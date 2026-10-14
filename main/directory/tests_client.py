"""Тесты разбора записей каталога, адресов серверов и ранжирования поиска. Без Django и без LDAP-сервера."""

import uuid
from unittest import TestCase, skipUnless

from directory.ldap_client import AD, LDAP, ConnectionSettings, DirectoryError, _tls, map_entry, parse_endpoints
from models_employee.matching import EXACT, PREFIX, WORDS, score_parts

try:
    import ldap3  # noqa: F401

    HAS_LDAP3 = True
except ImportError:
    HAS_LDAP3 = False


class EndpointTests(TestCase):
    def test_example_from_requisites(self):
        # Имя сервера: email.yanao.ru, порт 636 → LDAPS
        [e] = parse_endpoints(ConnectionSettings(host="email.yanao.ru", port=636))
        self.assertEqual((e.host, e.port, e.use_ssl, e.start_tls), ("email.yanao.ru", 636, True, False))

    def test_schemes_and_failover(self):
        eps = parse_endpoints(ConnectionSettings(host="ldaps://dc1.corp.local, ldap://dc2.corp.local:3268"))
        self.assertEqual([(e.host, e.port, e.use_ssl) for e in eps], [("dc1.corp.local", 636, True), ("dc2.corp.local", 3268, False)])
        [e] = parse_endpoints(ConnectionSettings(host="ldap.example.com", security="starttls"))
        self.assertEqual((e.port, e.use_ssl, e.start_tls), (389, False, True))
        with self.assertRaises(DirectoryError):
            parse_endpoints(ConnectionSettings(host=""))

    @skipUnless(HAS_LDAP3, "ldap3 не установлен")
    def test_server_name_is_sent_in_sni(self):
        # без SNI email.yanao.ru отдаёт сертификат по умолчанию, и проверка не проходит
        cfg = ConnectionSettings(host="email.yanao.ru")
        self.assertEqual(_tls(ldap3, cfg, "email.yanao.ru").sni, "email.yanao.ru")
        for address in ("10.10.161.111", "[2001:db8::1]"):
            self.assertIsNone(_tls(ldap3, cfg, address).sni)


class MappingTests(TestCase):
    def test_active_directory_entry(self):
        guid = uuid.uuid4()
        attrs = {
            "objectGUID": [guid.bytes_le],
            "displayName": ["Петров Иван Сергеевич".encode()],
            "givenName": [b"\xd0\x98\xd0\xb2\xd0\xb0\xd0\xbd"],
            "sn": ["Петров".encode()],
            "mail": [b"i.petrov@yanao.ru"],
            "telephoneNumber": [b"+7 (34922) 4-12-34"],
            "title": ["Начальник отдела".encode()],
            "department": ["Отдел ИТ".encode()],
        }
        m = ConnectionSettings(kind=AD).effective_mapping()
        c = map_entry("CN=Петров Иван,OU=Users,DC=yg,DC=loc", attrs, m)
        self.assertEqual(c["external_id"], str(guid))
        self.assertEqual(c["display_name"], "Петров Иван Сергеевич")
        self.assertEqual(c["first_name"], "Иван")
        self.assertEqual(c["email"], "i.petrov@yanao.ru")
        self.assertEqual(c["department"], "Отдел ИТ")
        self.assertEqual(c["mobile"], "")

    def test_ldap_entry_with_fallbacks_and_overrides(self):
        cfg = ConnectionSettings(kind=LDAP, mapping={"department": "departmentNumber", "id": ""})
        m = cfg.effective_mapping()
        self.assertEqual(m["department"], "departmentNumber")
        self.assertIn("departmentNumber", cfg.attributes())
        self.assertIn("cn", cfg.attributes())
        # нет displayName — имя собирается из фамилии и имени; нет id — используется DN
        c = map_entry("uid=anna,cn=yg.loc", {"SN": [b"Petrova"], "givenname": [b"Anna"], "MAIL": [b"anna@yanao.ru"],
                                               "departmentNumber": [b"42"]}, m)
        self.assertEqual(c["display_name"], "Petrova Anna")
        self.assertEqual(c["external_id"], "dn:uid=anna,cn=yg.loc")
        self.assertEqual(c["department"], "42")

    def test_cn_fallback_cp1251_and_skips(self):
        m = ConnectionSettings(kind=LDAP).effective_mapping()
        c = map_entry("cn=x", {"cn": ["Приёмная".encode("cp1251")], "entryUUID": [b"ABC-1"]}, m)
        self.assertEqual(c["display_name"], "Приёмная")
        self.assertEqual(c["external_id"], "abc-1")
        self.assertIsNone(map_entry("cn=svc", {"objectClass": [b"person"]}, m))  # ни имени, ни почты
        only_mail = map_entry("cn=y", {"mail": [b"info@yanao.ru"]}, m)
        self.assertEqual(only_mail["display_name"], "info@yanao.ru")
        bad_mail = map_entry("cn=z", {"cn": [b"Z"], "mail": [b"not-an-email"]}, m)
        self.assertEqual(bad_mail["email"], "")

    def test_filters(self):
        self.assertIn("userAccountControl", ConnectionSettings(kind=AD).effective_filter())
        self.assertEqual(ConnectionSettings(kind=LDAP, search_filter=" (mail=*) ").effective_filter(), "(mail=*)")


class ScoreTests(TestCase):
    def test_display_name_and_extra_fields(self):
        args = dict(first="Иван", last="Петров", email="i.petrov@yanao.ru", full_names=["Петров Иван Сергеевич"],
                    extra=["Отдел ИТ", "Начальник отдела", "+7 (34922) 4-12-34"])
        self.assertEqual(score_parts("петров иван сергеевич", **args), EXACT)
        self.assertEqual(score_parts("петр", **args), PREFIX)
        self.assertEqual(score_parts("иван серг", **args), WORDS)
        self.assertEqual(score_parts("отдел ит", **args), WORDS)
        self.assertIsNone(score_parts("бухгалтерия", **args))


@skipUnless(HAS_LDAP3, "ldap3 не установлена")
class Ldap3MockTests(TestCase):
    """Проверка клиента на встроенном в ldap3 «фальшивом» сервере (MOCK_SYNC)."""

    def test_paged_search_with_mock_server(self):
        from unittest import mock

        import ldap3

        from directory.ldap_client import DirectoryClient

        server = ldap3.Server("fake")
        conn = ldap3.Connection(server, user="cn=admin,cn=yg.loc", password="secret", client_strategy=ldap3.MOCK_SYNC)
        conn.strategy.add_entry("cn=admin,cn=yg.loc", {"userPassword": "secret", "objectClass": "person", "sn": "admin"})
        for i in range(3):
            conn.strategy.add_entry(
                f"cn=user{i},cn=yg.loc",
                {"objectClass": ["person", "inetOrgPerson"], "cn": f"User {i}", "sn": f"Фамилия{i}",
                 "givenName": "Имя", "mail": f"user{i}@yanao.ru"},
            )
        conn.bind()
        cfg = ConnectionSettings(kind=LDAP, host="fake", bind_dn="cn=admin,cn=yg.loc", password="secret",
                                 base_dn="cn=yg.loc", search_filter="(mail=*)")
        client = DirectoryClient(cfg)
        with mock.patch.object(DirectoryClient, "_connect", return_value=conn):
            entries = list(client.iter_entries())
        mapped = [map_entry(dn, a, cfg.effective_mapping()) for dn, a in entries]
        self.assertEqual(sorted(c["email"] for c in mapped), ["user0@yanao.ru", "user1@yanao.ru", "user2@yanao.ru"])


class DiagnoseTests(TestCase):
    """Сетевая диагностика на локальных портах: открытый, закрытый, неизвестное имя."""

    def setUp(self):
        import socket

        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.open_port = self.listener.getsockname()[1]
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        self.closed_port = closed.getsockname()[1]
        closed.close()

    def tearDown(self):
        self.listener.close()

    def test_closed_configured_port(self):
        from directory.ldap_client import diagnose

        lines = diagnose(ConnectionSettings(host="127.0.0.1", port=self.closed_port), timeout=1)
        self.assertIn("127.0.0.1 → 127.0.0.1", lines[0])
        self.assertIn(f"порт {self.closed_port} (указан в настройках): соединение отклонено", lines[1])
        self.assertTrue(any("Ни один LDAP-порт не отвечает" in l or "Совет" in l for l in lines))

    def test_open_configured_port(self):
        from directory.ldap_client import diagnose

        lines = diagnose(ConnectionSettings(host="127.0.0.1", port=self.open_port), timeout=1)
        self.assertIn("открыт", lines[1])
        self.assertIn("сервер ответил", lines[-1])

    def test_unknown_host(self):
        from directory.ldap_client import diagnose

        lines = diagnose(ConnectionSettings(host="no-such-host.invalid", port=636), timeout=1)
        self.assertIn("не найдено в DNS", lines[0])

    def test_test_attaches_diagnostics_on_network_error(self):
        from unittest import mock

        from directory.ldap_client import DirectoryClient

        client = DirectoryClient(ConnectionSettings(host="127.0.0.1", port=self.closed_port, base_dn="cn=yg.loc"))
        err = DirectoryError("Сервер каталога недоступен: timed out", network=True)
        with mock.patch.object(DirectoryClient, "iter_entries", side_effect=err):
            with self.assertRaises(DirectoryError) as ctx:
                client.test()
        self.assertTrue(ctx.exception.details)
        self.assertIn("127.0.0.1", ctx.exception.details[0])


class LiveSearchFilterTests(TestCase):
    """Фильтр для поиска на сервере каталога."""

    def test_injection_is_escaped(self):
        from directory.ldap_client import build_search_filter, escape_filter

        self.assertEqual(escape_filter("*)(uid=*"), "\\2a\\29\\28uid=\\2a")
        f = build_search_filter(ConnectionSettings(kind=LDAP, search_filter="(mail=*)"), "*)(objectClass=*")
        self.assertNotIn("(objectClass=*)", f.replace("(mail=*)", ""))
        self.assertTrue(f.startswith("(&(mail=*)"))
        self.assertEqual(f.count("(") , f.count(")"))

    def test_tokens_prefix_substring_and_yo(self):
        from directory.ldap_client import build_search_filter

        f = build_search_filter(ConnectionSettings(kind=LDAP), "Пётр сид")
        self.assertIn("(sn=Пётр*)", f)
        self.assertIn("(sn=Петр*)", f)  # оба написания
        self.assertIn("(displayName=*сид*)", f)
        self.assertIn("(mail=сид*)", f)
        self.assertNotIn("anr=", f)
        self.assertIn("(anr=сид)", build_search_filter(ConnectionSettings(kind=AD), "сид"))

    def test_phone_query(self):
        from directory.ldap_client import build_search_filter, phone_digits

        self.assertEqual(phone_digits("+7 (912) 000-11-22"), "79120001122")
        self.assertEqual(phone_digits("ab12"), "")
        f = build_search_filter(ConnectionSettings(kind=LDAP), "4-12-34")
        self.assertIn("(telephoneNumber=*4*1*2*3*4*)", f)
        self.assertIn("(mobile=*4*1*2*3*4*)", f)

    def test_rank_entries(self):
        from directory.ldap_client import rank_entries

        m = ConnectionSettings(kind=LDAP).effective_mapping()
        entries = [
            ("cn=a", {"sn": ["Петрова".encode()], "givenName": ["Анна".encode()], "mail": [b"a@x.ru"], "entryUUID": [b"1"]}),
            ("cn=b", {"sn": ["Петров".encode()], "givenName": ["Иван".encode()], "mail": [b"i@x.ru"], "entryUUID": [b"2"],
                      "telephoneNumber": [b"+7 (34922) 4-12-34"]}),
            ("cn=b", {"sn": ["Петров".encode()], "entryUUID": [b"2"]}),  # дубликат
            ("cn=svc", {"objectClass": [b"x"]}),
        ]
        ranked = rank_entries(entries, "петров", m, source="YG")
        self.assertEqual([c["email"] for _, c in ranked], ["i@x.ru", "a@x.ru"])
        self.assertEqual(ranked[0][1]["source"], "YG")
        phone = rank_entries(entries, "4-12-34", m)
        self.assertEqual([c["email"] for _, c in phone], ["i@x.ru"])


class PoolTests(TestCase):
    """Переиспользование соединения и переподключение без настоящего сервера."""

    def setUp(self):
        from directory.ldap_client import reset_pool

        reset_pool()
        self.addCleanup(reset_pool)

    def _fake_conn(self, responses):
        from unittest import mock

        conn = mock.Mock(closed=False)
        conn.response = []

        def search(**kw):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            conn.result, conn.response = item
            return True

        conn.search.side_effect = search
        return conn

    @skipUnless(HAS_LDAP3, "ldap3 не установлена")
    def test_reuses_connection_and_reconnects_once(self):
        from unittest import mock

        from ldap3.core.exceptions import LDAPSocketSendError

        from directory.ldap_client import DirectoryClient

        ok = ({"result": 0}, [{"type": "searchResEntry", "dn": "cn=a", "raw_attributes": {"cn": [b"A"]}}])
        broken = self._fake_conn([ok, LDAPSocketSendError("closed by server")])
        fresh = self._fake_conn([ok])
        cfg = ConnectionSettings(kind=LDAP, host="h", base_dn="cn=yg.loc")
        with mock.patch.object(DirectoryClient, "_connect", side_effect=[broken, fresh]) as connect:
            self.assertEqual(len(DirectoryClient(cfg).search("ab")), 1)
            self.assertEqual(len(DirectoryClient(cfg).search("ab")), 1)  # первое соединение разорвано → новое
        self.assertEqual(connect.call_count, 2)

    @skipUnless(HAS_LDAP3, "ldap3 не установлена")
    def test_result_codes(self):
        from unittest import mock

        from directory.ldap_client import DirectoryClient

        cfg = ConnectionSettings(kind=LDAP, host="h", base_dn="cn=nope")
        conn = self._fake_conn([({"result": 32, "description": "noSuchObject"}, [])])
        with mock.patch.object(DirectoryClient, "_connect", return_value=conn):
            with self.assertRaises(DirectoryError) as ctx:
                DirectoryClient(cfg).search("ab")
        self.assertIn("Base DN", str(ctx.exception))
        conn = self._fake_conn([({"result": 4}, [{"type": "searchResEntry", "dn": "cn=a", "raw_attributes": {}}])])
        with mock.patch.object(DirectoryClient, "_connect", return_value=conn):
            self.assertEqual(len(DirectoryClient(cfg).search("zz")), 1)  # sizeLimitExceeded — частичный ответ
