"""What an administrator sees of people: users, invitations and client applications."""
from fastapi.testclient import TestClient

from app.main import app
from tests.test_regressions import admin_client

PASSWORD = "a-long-enough-password"


def register(admin, email):
    """Invite and register someone; returns (their client, their user id)."""
    invited = admin.post("/v1/admin/invitations", json={"email": email}).json()
    person = TestClient(app)
    user_id = person.post("/v1/auth/accept-invitation", json={
        "token": invited["invitation_token"], "password": PASSWORD}).json()["user_id"]
    return person, user_id, invited


def sign_in(person, email):
    token = person.post("/v1/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    person.headers["Authorization"] = "Bearer " + token
    return token


def test_the_admin_sees_who_registered_and_what_they_use(fresh_db):
    admin = admin_client()
    before = {u["email"]: u for u in admin.get("/v1/admin/users").json()["items"]}
    assert before["admin@example.invalid"]["is_admin"] and before["admin@example.invalid"]["active_sessions"] == 1

    person, user_id, _ = register(admin, "Maja@Example.invalid")
    row = next(u for u in admin.get("/v1/admin/users").json()["items"] if u["id"] == user_id)
    assert row["email"] == "maja@example.invalid" and not row["is_admin"] and row["enabled"]
    assert (row["active_sessions"], row["monitors"], row["stores"], row["apps"]) == (0, 0, 0, 0)
    assert row["last_signed_in_at"] is None

    sign_in(person, "maja@example.invalid")
    store = admin.get("/v1/stores").json()["items"][0]["id"]
    person.put(f"/v1/me/stores/{store}")
    row = next(u for u in admin.get("/v1/admin/users").json()["items"] if u["id"] == user_id)
    assert (row["active_sessions"], row["stores"]) == (1, 1) and row["last_signed_in_at"]


def test_a_disabled_user_is_signed_out_and_kept_out_until_enabled(fresh_db):
    admin = admin_client()
    person, user_id, _ = register(admin, "olle@example.invalid")
    sign_in(person, "olle@example.invalid")
    assert person.get("/v1/me").status_code == 200

    assert admin.post(f"/v1/admin/users/{user_id}/disable").status_code == 204
    assert person.get("/v1/me").status_code == 401                       # the session ended at once
    assert TestClient(app).post("/v1/auth/login", json={
        "email": "olle@example.invalid", "password": PASSWORD}).status_code == 401
    shown = next(u for u in admin.get("/v1/admin/users").json()["items"] if u["id"] == user_id)
    assert shown["enabled"] is False and shown["active_sessions"] == 0

    assert admin.post(f"/v1/admin/users/{user_id}/enable").status_code == 204
    sign_in(person, "olle@example.invalid")
    assert person.get("/v1/me").status_code == 200


def test_you_cannot_disable_yourself_and_unknown_users_are_404(fresh_db):
    admin = admin_client()
    me = admin.get("/v1/me").json()["id"]
    assert admin.post(f"/v1/admin/users/{me}/disable").status_code == 409
    assert admin.post("/v1/admin/users/nobody/disable").status_code == 404
    assert admin.post("/v1/admin/users/nobody/enable").status_code == 404


def test_invitations_show_whether_they_are_waiting_used_or_withdrawn(fresh_db):
    admin = admin_client()
    _person, _uid, used = register(admin, "used@example.invalid")
    waiting = admin.post("/v1/admin/invitations", json={"email": "waiting@example.invalid"}).json()
    byemail = {i["email"]: i for i in admin.get("/v1/admin/invitations").json()["items"]}
    assert byemail["used@example.invalid"]["status"] == "used" and byemail["waiting@example.invalid"]["status"] == "pending"
    assert byemail["waiting@example.invalid"]["invited_by"] == "admin@example.invalid"

    assert admin.delete(f"/v1/admin/invitations/{used['id']}").status_code == 409
    assert admin.delete(f"/v1/admin/invitations/{waiting['id']}").status_code == 204
    assert admin.delete("/v1/admin/invitations/nope").status_code == 404
    status = {i["email"]: i["status"] for i in admin.get("/v1/admin/invitations").json()["items"]}
    assert status["waiting@example.invalid"] == "expired"
    assert TestClient(app).post("/v1/auth/accept-invitation", json={
        "token": waiting["invitation_token"], "password": PASSWORD}).status_code == 400    # the token stopped working


def test_client_applications_are_listed_with_how_many_users_let_them_in(fresh_db):
    admin = admin_client()
    created = admin.post("/v1/clients", json={"name": "Phone app", "scopes": ["catalogue:read"]}).json()
    (row,) = [c for c in admin.get("/v1/admin/clients").json()["items"] if c["name"] == "Phone app"]
    assert row["id"] == created["client_id"] and row["users"] == 0 and row["scopes"] == ["catalogue:read"]
    assert row["revoked_at"] is None
    assert admin.delete(f"/v1/clients/{created['client_id']}").status_code == 204
    (row,) = [c for c in admin.get("/v1/admin/clients").json()["items"] if c["name"] == "Phone app"]
    assert row["revoked_at"] is not None


def test_only_an_administrator_can_see_any_of_it(fresh_db):
    admin = admin_client()
    person, _uid, _ = register(admin, "plain@example.invalid")
    sign_in(person, "plain@example.invalid")
    for path in ("/v1/admin/users", "/v1/admin/invitations", "/v1/admin/clients"):
        assert person.get(path).status_code == 403
        assert TestClient(app).get(path).status_code == 401
