# © 2026 Одинцов Валентин Николаевич. Все права защищены.
from unittest import mock

from django.contrib.auth import authenticate, get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from directory.ldap_client import DirectoryError

from . import ad_auth
from .ad_auth import ADAccount, ADAuthError
from .models import Employee
from .permissions import is_admin

GUID = "9e000587-41e4-4871-ba8c-4628ec34c90f"
OTHER_GUID = "11111111-2222-3333-4444-555555555555"


def account(**changes):
    data = {"guid": GUID, "login": "Ivanov", "first_name": "Иван", "last_name": "Иванов",
            "email": "ivanov@zs.int", "title": "Инженер"}
    return ADAccount(**{**data, **changes})


def fetch(result):
    """Подмена запроса к контроллеру домена: result — ADAccount или ADAuthError."""
    return mock.patch.object(ad_auth, "fetch_account", side_effect=[result] if isinstance(result, Exception) else None,
                             return_value=result)


@override_settings(AD_AUTH_DOMAIN="zs.int", AD_AUTH_NETBIOS_DOMAIN="", AD_AUTH_BASE_DN="", AD_AUTH_SERVERS="",
                   AD_AUTH_SECURITY="ssl", AD_AUTH_VERIFY_CERT=True)
class ParseLoginTests(TestCase):
    def test_accepted_forms(self):
        for raw in ("ivanov", " ivanov ", "ZS\\ivanov", "zs.int\\ivanov", "ivanov@ZS.INT"):
            self.assertEqual(ad_auth.parse_login(raw), "ivanov", raw)

    def test_foreign_domain_and_bad_characters_rejected(self):
        for raw in ("", "ivanov@yanao.ru", "OTHER\\ivanov", "iva nov", "ivanov)(cn=*", "a*", "x" * 65):
            self.assertIsNone(ad_auth.parse_login(raw), raw)

    @override_settings(AD_AUTH_ADMIN_USERS=" Ivanov@zs.int, ZS\\petrov,sidorov ,, ivanov@yanao.ru")
    def test_admin_users_list(self):
        with self.assertLogs("models_employee.ad_auth", "ERROR"):
            self.assertEqual(ad_auth.admin_users(), {"ivanov", "petrov", "sidorov"})

    def test_connection_settings_from_domain(self):
        cfg = ad_auth.connection_settings("ivanov@zs.int", "secret")
        self.assertEqual((cfg.host, cfg.base_dn, cfg.security, cfg.verify_certificate), ("zs.int", "DC=zs,DC=int", "ssl", True))


@override_settings(AD_AUTH_DOMAIN="zs.int")
class BackendTests(TestCase):
    def login(self, username="ivanov", password="secret", request=None):
        return authenticate(request, username=username, password=password)

    def test_first_login_creates_user_with_profile(self):
        with fetch(account()) as fetch_account:
            user = self.login("ZS\\Ivanov")
        fetch_account.assert_called_once_with("Ivanov", "secret")
        self.assertEqual((user.username, user.first_name, user.last_name, user.email), ("ivanov", "Иван", "Иванов", "ivanov@zs.int"))
        self.assertFalse(user.has_usable_password())
        self.assertEqual((user.employee.ad_guid, user.employee.position, user.employee.is_owner), (GUID, "Инженер", True))
        self.assertFalse(is_admin(user))

    def test_next_login_updates_same_user(self):
        with fetch(account()):
            first = self.login()
        with fetch(account(last_name="Петров", email="", title="")):
            second = self.login()
        self.assertEqual(first.pk, second.pk)
        # пустые значения из домена не затирают заполненное
        self.assertEqual((second.last_name, second.email, second.employee.position), ("Петров", "ivanov@zs.int", "Инженер"))
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_renamed_login_keeps_calendar(self):
        with fetch(account()):
            first = self.login()
        with fetch(account(login="petrov")):
            second = self.login("petrov")
        self.assertEqual((second.pk, second.username), (first.pk, "petrov"))

    def test_existing_local_user_is_linked(self):
        local = get_user_model().objects.create_user("IVANOV", "old@example.com", "x")
        with fetch(account()):
            user = self.login()
        self.assertEqual(user.pk, local.pk)
        self.assertEqual(user.employee.ad_guid, GUID)

    def test_reused_login_does_not_get_someone_elses_calendar(self):
        with fetch(account()):
            self.login()
        request = RequestFactory().post("/accounts/login/")
        with fetch(account(guid=OTHER_GUID)):
            self.assertIsNone(self.login(request=request))
        self.assertIn("занят", request.ad_auth_error)
        self.assertEqual(Employee.objects.get().ad_guid, GUID)

    def test_local_superuser_is_never_taken_over(self):
        get_user_model().objects.create_superuser("ivanov", "root@example.com", "local-password")
        with fetch(account()):
            self.assertIsNone(self.login())
        self.assertIsNone(Employee.objects.get().ad_guid)

    def test_rejected_by_domain(self):
        request = RequestFactory().post("/accounts/login/")
        with fetch(ADAuthError("")):
            self.assertIsNone(self.login(request=request))
        self.assertFalse(hasattr(request, "ad_auth_error"))
        self.assertFalse(get_user_model().objects.exists())

    def test_empty_password_and_foreign_domain_never_reach_domain(self):
        with fetch(account()) as fetch_account:
            self.assertIsNone(self.login(password=""))
            self.assertIsNone(self.login("ivanov@yanao.ru"))
        fetch_account.assert_not_called()
        with self.assertRaises(ADAuthError):
            ad_auth.fetch_account("ivanov", "")  # пустой пароль = анонимный вход LDAP

    def test_user_disabled_in_calendar_cannot_login(self):
        with fetch(account()):
            user = self.login()
        user.is_active = False
        user.save()
        with fetch(account()):
            self.assertIsNone(self.login())

    def test_admin_group_grants_and_revokes_role(self):
        with fetch(account(is_admin=True)):
            user = self.login()
        self.assertTrue(is_admin(user) and user.is_staff)
        with fetch(account(is_admin=None)):  # группа не настроена — роль не трогаем
            self.assertTrue(is_admin(self.login()))
        with fetch(account(is_admin=False)):
            user = self.login()
        self.assertFalse(is_admin(user) or user.is_staff)

    def test_local_password_works_only_for_superuser(self):
        User = get_user_model()
        User.objects.create_user("local", "local@example.com", "local-password")
        root = User.objects.create_superuser("root", "root@example.com", "root-password")
        with fetch(ADAuthError("")) as fetch_account:
            self.assertIsNone(self.login("local", "local-password"))
            self.assertEqual(self.login("root", "root-password"), root)
        fetch_account.assert_called_once()  # суперпользователь вошёл без обращения к домену


@override_settings(AD_AUTH_DOMAIN="zs.int")
class BindErrorTests(TestCase):
    def bind_error(self, text, network=False):
        exc = DirectoryError("Ошибка каталога", network=network)
        exc.__cause__ = Exception(text)
        with mock.patch.object(ad_auth.DirectoryClient, "connect", side_effect=exc), self.assertLogs(ad_auth.log, "ERROR") if network else mock.patch.object(ad_auth, "log"):
            with self.assertRaises(ADAuthError) as ctx:
                ad_auth.fetch_account("ivanov", "secret")
        return ctx.exception.message

    def test_messages(self):
        ad = "80090308: LdapErr: DSID-0C09044E, comment: AcceptSecurityContext error, data {}, v2580"
        self.assertEqual(self.bind_error(ad.format("52e")), "")
        self.assertEqual(self.bind_error(ad.format("525")), "")
        self.assertIn("заблокирована", self.bind_error(ad.format("775")))
        self.assertIn("Срок действия пароля", self.bind_error(ad.format("532")))
        self.assertIn("отключена", self.bind_error(ad.format("533")))
        self.assertEqual(self.bind_error("socket connection error", network=True), ad_auth.UNAVAILABLE)
        self.assertEqual(self.bind_error("что-то непонятное"), ad_auth.FAILED)


class FakeConnection:
    """Соединение с LDAP-шлюзом: правило вложенных групп не поддерживается, прямое членство — по списку."""

    def __init__(self, member_of=(), missing=()):
        self.member_of, self.missing, self.filters, self.response = member_of, missing, [], []

    def search(self, base, search_filter, **kwargs):
        from ldap3.core.exceptions import LDAPNoSuchObjectResult, LDAPProtocolErrorResult

        self.filters.append(search_filter)
        if ad_auth.IN_CHAIN in search_filter:
            raise LDAPProtocolErrorResult(result=2, description="protocolError", message="BER: record is too short")
        group = search_filter[len("(memberOf="):-1]
        if group in self.missing:
            raise LDAPNoSuchObjectResult(result=32, description="noSuchObject")
        self.response = [{"type": "searchResEntry", "dn": base}] if group in self.member_of else []


class GroupTests(TestCase):
    USER, GROUP = "CN=Иванов,cn=yg.loc", "CN=Calendar,OU=Groups,DC=yg,DC=loc"

    def test_direct_membership_when_nested_rule_is_not_supported(self):
        conn = FakeConnection(member_of=[self.GROUP])
        self.assertTrue(ad_auth._in_group(conn, self.USER, self.GROUP))
        self.assertEqual(len(conn.filters), 2)
        self.assertFalse(ad_auth._in_group(FakeConnection(), self.USER, self.GROUP))

    def test_unknown_group_does_not_block_login(self):
        with self.assertLogs(ad_auth.log, "ERROR"):
            self.assertFalse(ad_auth._in_group(FakeConnection(missing=[self.GROUP]), self.USER, self.GROUP))


@override_settings(AD_AUTH_DOMAIN="yanao.ru", AD_AUTH_NETBIOS_DOMAIN="", AD_AUTH_BASE_DN="cn=yg.loc",
                   AD_AUTH_SERVERS="email.yanao.ru", AD_AUTH_SECURITY="ssl", AD_AUTH_VERIFY_CERT=True)
class YanaoDomainTests(TestCase):
    def test_login_forms_and_connection(self):
        for raw in ("vnodintsov", "VNOdintsov@yanao.ru", "vnodintsov@YANAO.RU"):
            self.assertEqual((ad_auth.parse_login(raw) or "").lower(), "vnodintsov", raw)
        for raw in ("odintsovvn@zs.int", "ddodincov@noyabrsk.yanao.ru"):
            self.assertIsNone(ad_auth.parse_login(raw), raw)
        cfg = ad_auth.connection_settings("vnodintsov@yanao.ru", "secret")
        self.assertEqual((cfg.host, cfg.base_dn, cfg.security, cfg.verify_certificate, cfg.bind_dn),
                         ("email.yanao.ru", "cn=yg.loc", "ssl", True, "vnodintsov@yanao.ru"))


@override_settings(AD_AUTH_DOMAIN="zs.int")
class LoginPageTests(TestCase):
    def post(self, **data):
        return self.client.post(reverse("login"), {"username": "ivanov", "password": "secret", **data})

    def test_login_with_domain_account(self):
        with fetch(account()):
            resp = self.post()
        self.assertRedirects(resp, reverse("events:calendar"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("events:calendar")).status_code, 200)

    def test_wrong_password(self):
        with fetch(ADAuthError("")):
            self.assertContains(self.post(), "Неверное имя пользователя или пароль.")

    def test_reason_from_domain_is_shown(self):
        with fetch(ADAuthError(ad_auth.BIND_MESSAGES["775"])):
            self.assertContains(self.post(), "заблокирована в домене")

    def test_no_password_change_or_reset_pages(self):
        for path in ("/accounts/password_change/", "/accounts/password_reset/"):
            self.assertEqual(self.client.get(path).status_code, 404)
