import importlib

from fastapi.testclient import TestClient


def test_site_pricing_reflects_environment(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "VLESS_HOST=test.example",
                "VLESS_PORT=2053",
                "BOT_PAYMENT_URL=https://vpn-gpt.store/pay",
                "TRIAL_DAYS=0",
                "PLANS=1m:80,3m:200,1y:700",
                "ADMIN_TOKEN=secret",
                "INTERNAL_TOKEN=service",
                "ADMIN_PANEL_PASSWORD=panelpass",
                "REFERRAL_BONUS_DAYS=30",
                "STARS_ENABLED=true",
                "STARS_PRICE_TEST=20",
                "STARS_PRICE_MONTH=80",
                "STARS_PRICE_3M=200",
                "STARS_PRICE_6M=0",
                "STARS_PRICE_YEAR=700",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("ENV_PATH", str(env_path))
    monkeypatch.setenv("DATABASE", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("GPT_API_KEY", "test-key")

    config_module = importlib.import_module("api.config")
    importlib.reload(config_module)

    site_module = importlib.import_module("api.endpoints.site")
    importlib.reload(site_module)

    db_module = importlib.import_module("api.utils.db")
    importlib.reload(db_module)
    db_module.init_db()

    api_main = importlib.import_module("api.main")
    importlib.reload(api_main)

    with TestClient(api_main.app) as client:
        response = client.get("/api/site/pricing")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["test"]["code"] == "test_1d"
    assert payload["test"]["price_stars"] == 20

    prices = {plan["code"]: plan["price_stars"] for plan in payload["plans"]}
    assert set(prices) == {"1m", "3m", "1y"}
    assert prices["1m"] == 80
    assert prices["3m"] == 200
    assert prices["1y"] == 700
