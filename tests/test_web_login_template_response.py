import importlib

from fastapi.testclient import TestClient

from src.config import settings as settings_module
from src.database import session as session_module


def _load_fresh_web_app(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.setenv("APP_DATA_DIR", str(data_dir))

    session_module._db_manager = None
    settings_module._settings = None
    session_module.init_database()

    web_app = importlib.import_module("src.web.app")
    return importlib.reload(web_app)


def test_login_page_renders_successfully(monkeypatch, tmp_path):
    web_app = _load_fresh_web_app(monkeypatch, tmp_path)
    client = TestClient(web_app.app)

    response = client.get("/login")

    assert response.status_code == 200
    assert "访问验证" in response.text
    assert "请输入访问密码继续" in response.text
