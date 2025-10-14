from __future__ import annotations

import sys
from importlib import util
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_MODULE_SPEC = util.spec_from_file_location("vpn_gpt_morune", _ROOT / "api" / "integrations" / "morune.py")
assert _MODULE_SPEC and _MODULE_SPEC.loader  # for type checkers
morune = util.module_from_spec(_MODULE_SPEC)
sys.modules[_MODULE_SPEC.name] = morune
_MODULE_SPEC.loader.exec_module(morune)


@pytest.fixture()
def morune_client():
    client = morune.MoruneClient(
        base_url="https://payments.example", api_key="token", project_id="proj"
    )
    yield client
    client.close()


def test_create_invoice_handles_nested_attributes(monkeypatch, morune_client):
    payload = {
        "data": {
            "attributes": {
                "invoiceId": "inv-001",
                "paymentUrl": " https://pay.example/checkout ",
                "status": "WAITING_PAYMENT",
                "total": "199.5",
                "currency_code": "eur",
            }
        }
    }

    monkeypatch.setattr(morune_client, "_request", lambda *args, **kwargs: payload)

    invoice = morune_client.create_invoice(
        payment_id="order-1",
        amount=200,
        currency="rub",
        description="Test",
        metadata=None,
        success_url=None,
        fail_url=None,
    )

    assert invoice.provider_payment_id == "inv-001"
    assert invoice.payment_url == "https://pay.example/checkout"
    assert invoice.status == "waiting_payment"
    assert invoice.amount == 200
    assert invoice.currency == "EUR"


def test_create_invoice_falls_back_to_links(monkeypatch, morune_client):
    payload = {
        "data": {
            "id": "inv-002",
            "links": {"checkout": {"href": "https://pay.example/from-links"}},
            "attributes": {
                "status": "pending",
                "amount": 450,
                "currency": "usd",
            },
        }
    }

    monkeypatch.setattr(morune_client, "_request", lambda *args, **kwargs: payload)

    invoice = morune_client.create_invoice(
        payment_id="order-2",
        amount=450,
        currency="rub",
        description="Test",
        metadata=None,
        success_url=None,
        fail_url=None,
    )

    assert invoice.provider_payment_id == "inv-002"
    assert invoice.payment_url == "https://pay.example/from-links"
    assert invoice.status == "pending"
    assert invoice.amount == 450
    assert invoice.currency == "USD"


def test_create_invoice_handles_new_invoice_url(monkeypatch, morune_client):
    payload = {
        "data": {
            "invoice": {
                "invoiceUrl": "https://pay.example/from-invoice",
                "cashier_url": "https://pay.example/cashier",
            },
            "attributes": {
                "invoice_id": "inv-003",
                "status": "processing",
                "amount": "600",
                "currency": "gbp",
            },
        }
    }

    monkeypatch.setattr(morune_client, "_request", lambda *args, **kwargs: payload)

    invoice = morune_client.create_invoice(
        payment_id="order-3",
        amount=600,
        currency="rub",
        description="Test",
        metadata=None,
        success_url=None,
        fail_url=None,
    )

    assert invoice.provider_payment_id == "inv-003"
    assert invoice.payment_url == "https://pay.example/from-invoice"
    assert invoice.status == "processing"
    assert invoice.amount == 600
    assert invoice.currency == "GBP"


def test_create_invoice_falls_back_to_scanning(monkeypatch, morune_client):
    payload = {
        "data": {
            "id": "inv-004",
            "attributes": {
                "status": "pending",
                "amount": "500",
                "currency": "usd",
                "buttons": [
                    {"title": "card", "value": "https://pay.example/fallback"},
                    {"title": "support", "value": "https://help.example/faq"},
                ],
            },
        }
    }

    monkeypatch.setattr(morune_client, "_request", lambda *args, **kwargs: payload)

    invoice = morune_client.create_invoice(
        payment_id="order-4",
        amount=500,
        currency="rub",
        description="Test",
        metadata=None,
        success_url=None,
        fail_url=None,
    )

    assert invoice.provider_payment_id == "inv-004"
    assert invoice.payment_url == "https://pay.example/fallback"
    assert invoice.status == "pending"
    assert invoice.amount == 500
    assert invoice.currency == "USD"


def test_create_invoice_handles_html_links(monkeypatch, morune_client):
    payload = {
        "data": {
            "attributes": {
                "hash": "hash-005",
                "status": "created",
                "payment_page": {
                    "html": '<iframe src="//pay.example/iframe/hash-005"></iframe>',
                },
                "links": [
                    {"rel": "backup", "href": "//pay.example/cashier/hash-005"},
                    {"rel": "alt", "href": "/cashier/hash-005"},
                ],
            },
            "result": {
                "description": "Оплатите по ссылке pay.example/cashier/hash-005",
            },
        }
    }

    monkeypatch.setattr(morune_client, "_request", lambda *args, **kwargs: payload)

    invoice = morune_client.create_invoice(
        payment_id="order-5",
        amount=700,
        currency="rub",
        description="Test",
        metadata=None,
        success_url=None,
        fail_url=None,
    )

    assert invoice.provider_payment_id == "hash-005"
    assert invoice.payment_url == "https://pay.example/cashier/hash-005"
    assert invoice.status == "created"
    assert invoice.amount is None
    assert invoice.currency == "RUB"


def test_create_invoice_fetches_details_when_missing_url(monkeypatch, morune_client):
    responses = iter(
        [
            {
                "data": {
                    "id": "inv-006",
                    "attributes": {
                        "status": "pending",
                        "amount": 300,
                        "currency": "usd",
                    },
                }
            },
            {
                "data": {
                    "attributes": {
                        "paymentLink": "https://pay.example/from-detail",
                        "status": "waiting_payment",
                        "amount": 305,
                        "currency": "eur",
                    }
                }
            },
        ]
    )

    def fake_request(method, path, *, json_payload=None):
        payload = next(responses)
        if method == "GET":
            assert path == "/e/api/invoices/inv-006"
            assert json_payload is None
        else:
            assert method == "POST"
            assert path == "/e/api/invoices"
        return payload

    monkeypatch.setattr(morune_client, "_request", fake_request)

    invoice = morune_client.create_invoice(
        payment_id="order-6",
        amount=300,
        currency="rub",
        description="Test",
        metadata=None,
        success_url=None,
        fail_url=None,
    )

    assert invoice.provider_payment_id == "inv-006"
    assert invoice.payment_url == "https://pay.example/from-detail"
    assert invoice.status == "waiting_payment"
    assert invoice.amount == 305
    assert invoice.currency == "EUR"
    assert invoice.raw["create"]["data"]["id"] == "inv-006"
    assert invoice.raw["detail"]["data"]["attributes"]["paymentLink"] == "https://pay.example/from-detail"
