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
