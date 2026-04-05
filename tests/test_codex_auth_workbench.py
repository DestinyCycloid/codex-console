import asyncio
import io
import json
import zipfile
from contextlib import contextmanager
from pathlib import Path

from src.core.openai import codex_auth_workbench as workbench
from fastapi.routing import APIRoute

from src.core.openai.codex_auth_workbench import (
    CODEX_AUTH_HEALTHY,
    CODEX_AUTH_REPAIRABLE,
    build_codex_auth_zip_entries,
    build_managed_auth_json,
    resolve_email_service_for_account,
    resolve_codex_auth_status,
)
from src.database.models import Account, Base, EmailService
from src.database.session import DatabaseSessionManager
from src.web.app import create_app
from src.web.routes import accounts as accounts_routes


def _find_route(app, path: str, method: str) -> APIRoute:
    wanted_method = method.upper()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path == path and wanted_method in route.methods:
            return route
    raise AssertionError(f"Route not found: {method} {path}")


def test_resolve_codex_auth_status_marks_complete_account_healthy():
    account = Account(
        id=7,
        email="healthy@example.com",
        password="secret",
        access_token="access-token",
        refresh_token="refresh-token",
        id_token="id-token",
        account_id="acct_123",
        email_service="cloudmail",
        session_token="session-token",
        status="active",
        extra_data={"codex_auth": {"generated": True, "artifact_path": "data/codex_auth/7/auth.json"}},
    )

    status = resolve_codex_auth_status(account)

    assert status.health == CODEX_AUTH_HEALTHY
    assert status.complete is True
    assert status.generated is True
    assert status.export_ready is True


def test_resolve_codex_auth_status_marks_incomplete_account_repairable():
    account = Account(
        id=8,
        email="repairable@example.com",
        password="secret",
        access_token="access-token",
        refresh_token="",
        id_token="",
        account_id="acct_456",
        email_service="cloudmail",
        session_token="session-token",
        status="active",
        extra_data={},
    )

    status = resolve_codex_auth_status(account)

    assert status.health == CODEX_AUTH_REPAIRABLE
    assert status.complete is False
    assert status.export_ready is False


def test_build_managed_auth_json_matches_expected_shape():
    account = Account(
        id=9,
        email="export@example.com",
        password="secret",
        access_token="access-token",
        refresh_token="refresh-token",
        id_token="id-token",
        account_id="acct_789",
        email_service="cloudmail",
        status="active",
    )

    auth_json = build_managed_auth_json(account)

    assert auth_json["auth_mode"] == "chatgpt"
    assert auth_json["OPENAI_API_KEY"] is None
    assert auth_json["tokens"]["id_token"] == "id-token"
    assert auth_json["tokens"]["access_token"] == "access-token"
    assert auth_json["tokens"]["refresh_token"] == "refresh-token"
    assert auth_json["tokens"]["account_id"] == "acct_789"
    assert "last_refresh" in auth_json


def test_resolve_email_service_for_account_prefers_matching_mailbox(monkeypatch):
    captured = {}

    def fake_create_email_service(service_type, config, name):
        captured["service_type"] = service_type
        captured["config"] = dict(config)
        captured["name"] = name
        return {"name": name, "config": dict(config)}

    monkeypatch.setattr(workbench, "create_email_service", fake_create_email_service)

    account = Account(
        id=13,
        email="target@example.com",
        password="secret",
        email_service="outlook",
        status="active",
    )
    rows = [
        EmailService(
            id=1,
            service_type="outlook",
            name="higher-priority",
            enabled=True,
            priority=0,
            config={"email": "other@example.com"},
        ),
        EmailService(
            id=2,
            service_type="outlook",
            name="matching-mailbox",
            enabled=True,
            priority=10,
            config={"email": "target@example.com"},
        ),
    ]

    service, error = resolve_email_service_for_account(account, rows)

    assert error == ""
    assert service["name"] == "matching-mailbox"
    assert captured["config"]["email"] == "target@example.com"


def test_account_to_response_includes_codex_auth_state():
    account = Account(
        id=10,
        email="response@example.com",
        password="secret",
        access_token="access-token",
        refresh_token="refresh-token",
        id_token="id-token",
        account_id="acct_response",
        email_service="cloudmail",
        session_token="session-token",
        status="active",
    )

    response = accounts_routes.account_to_response(account)

    assert response.codex_auth["health"] == CODEX_AUTH_HEALTHY
    assert response.codex_auth["complete"] is True


def test_app_exposes_codex_auth_routes():
    app = create_app()

    _find_route(app, "/api/accounts/codex-auth/audit/async", "POST")
    _find_route(app, "/api/accounts/codex-auth/generate/async", "POST")
    _find_route(app, "/api/accounts/codex-auth/repair/async", "POST")
    _find_route(app, "/api/accounts/codex-auth/export", "POST")
    _find_route(app, "/api/accounts/codex-auth/tasks/{task_id}", "GET")


def test_export_codex_auth_artifacts_streams_zip(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "codex_auth_export.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        account = Account(
            email="zip@example.com",
            password="secret",
            access_token="access-token",
            refresh_token="refresh-token",
            id_token="id-token",
            account_id="acct_zip",
            email_service="cloudmail",
            status="active",
        )
        session.add(account)
        session.flush()
        account_id = account.id

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(accounts_routes, "get_db", fake_get_db)

    response = asyncio.run(
        accounts_routes.export_codex_auth_artifacts(
            accounts_routes.BatchCodexAuthRequest(ids=[account_id])
        )
    )

    async def collect_body():
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)

    body = asyncio.run(collect_body())
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = sorted(zf.namelist())
        assert names == ["zip-example.com/auth.json"]
        payload = json.loads(zf.read("zip-example.com/auth.json").decode("utf-8"))
    assert payload["auth_mode"] == "chatgpt"
    assert payload["tokens"]["refresh_token"] == "refresh-token"


def test_build_codex_auth_zip_entries_skips_incomplete_accounts():
    healthy = Account(
        id=11,
        email="healthy@example.com",
        password="secret",
        access_token="access-token",
        refresh_token="refresh-token",
        id_token="id-token",
        account_id="acct_healthy",
        email_service="cloudmail",
        status="active",
    )
    incomplete = Account(
        id=12,
        email="incomplete@example.com",
        password="secret",
        access_token="access-token",
        refresh_token="",
        id_token="",
        account_id="acct_incomplete",
        email_service="cloudmail",
        status="active",
    )

    entries = build_codex_auth_zip_entries([healthy, incomplete])

    assert len(entries) == 1
    assert entries[0][0] == "healthy-example.com/auth.json"
    payload = json.loads(entries[0][1].decode("utf-8"))
    assert payload["tokens"]["refresh_token"] == "refresh-token"
