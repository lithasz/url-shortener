from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_shorten_and_redirect():
    response = client.post("/shorten", params={"long_url": "https://example.com"})
    assert response.status_code == 200
    data = response.json()
    assert "short_code" in data

    code = data["short_code"]
    redirect_response = client.get(f"/{code}", follow_redirects=False)
    # 307 is HTTP's status code meaning "temporary redirect."
    assert redirect_response.status_code == 307

def test_missing_code_returns_404():
    response = client.get("/this-code-does-not-exist")
    assert response.status_code == 404