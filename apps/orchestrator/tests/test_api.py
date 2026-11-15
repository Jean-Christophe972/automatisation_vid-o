from fastapi.testclient import TestClient

from contentmachine.api import app

client = TestClient(app)


def test_health_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["ready_for_jobs"] is False
    assert "GEMINI_API_KEY" in body["missing_required"]


def test_health_with_key_never_leaks_it(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-value-123")
    for path in ("/health", "/config"):
        r = client.get(path)
        assert r.status_code == 200
        assert "secret-value-123" not in r.text
    assert client.get("/health").json()["ready_for_jobs"] is True
    assert client.get("/config").json()["GEMINI_API_KEY"]["set"] is True
