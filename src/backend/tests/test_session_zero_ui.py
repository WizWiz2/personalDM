from fastapi.testclient import TestClient


def test_session_zero_wizard_is_served(client: TestClient):
    response = client.get("/api/session-zero-ui")
    assert response.status_code == 200
    assert "Нулевая сессия" in response.text
    assert "Мир и договорённости" in response.text
    assert "Персонаж игрока" in response.text
    assert "Начальная точка" in response.text
    assert "Завершить нулевую сессию" in response.text
    assert "/session-zero/complete" in response.text
    assert "/card" in response.text
