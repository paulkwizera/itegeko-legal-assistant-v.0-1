"""
Pro plan checkout via Flutterwave.

Flutterwave is used (rather than Stripe) because it can actually settle in
Rwanda and offers card AND Rwandan mobile money (MTN MoMo / Airtel Money) in
one hosted checkout page -- so a single integration covers both payment
methods instead of running two separate providers.

Charges are made in RWF because Rwandan mobile money on Flutterwave requires
RWF, and Flutterwave's hosted checkout converts for international card
payers automatically. PRO_PRICE_RWF is the RWF amount that corresponds to
the advertised $10/month -- update it if the exchange rate moves.

Requires FLUTTERWAVE_SECRET_KEY in the environment. Until it's set,
is_configured() returns False and the app shows an "upgrades aren't
available yet" message instead of crashing.
"""
import os
import uuid
import logging

import requests

logger = logging.getLogger("itegeko.payments")

FLW_SECRET_KEY = os.environ.get("FLUTTERWAVE_SECRET_KEY")
FLW_BASE_URL = "https://api.flutterwave.com/v3"

PRO_PRICE_RWF = int(os.environ.get("PRO_PRICE_RWF", "13500"))  # ~$10/month, adjust to current FX rate
PRO_PRICE_LABEL = os.environ.get("PRO_PRICE_LABEL", "$10")
SUBSCRIPTION_DAYS = 30


def is_configured():
    return bool(FLW_SECRET_KEY)


def create_checkout(user, redirect_url):
    """Creates a Flutterwave payment link. The hosted page itself lets the
    payer choose card or Mobile Money -- we don't need to branch on that here."""
    tx_ref = f"itegeko-{user['_id']}-{uuid.uuid4().hex[:10]}"
    payload = {
        "tx_ref": tx_ref,
        "amount": PRO_PRICE_RWF,
        "currency": "RWF",
        "redirect_url": redirect_url,
        "payment_options": "card,mobilemoneyrwanda",
        "customer": {
            "email": user["email"],
            "name": user.get("name", ""),
        },
        "customizations": {
            "title": "Itegeko Pro",
            "description": "Itegeko Pro subscription -- 1 month",
        },
    }
    resp = requests.post(
        f"{FLW_BASE_URL}/payments",
        json=payload,
        headers={"Authorization": f"Bearer {FLW_SECRET_KEY}"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Flutterwave payment init failed: {data}")
    return {"tx_ref": tx_ref, "checkout_url": data["data"]["link"]}


def verify_transaction(transaction_id):
    """Re-checks a completed transaction directly with Flutterwave (never
    trust the redirect query params alone) before granting Pro access."""
    resp = requests.get(
        f"{FLW_BASE_URL}/transactions/{transaction_id}/verify",
        headers={"Authorization": f"Bearer {FLW_SECRET_KEY}"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    tx = data.get("data", {})
    ok = (
        data.get("status") == "success"
        and tx.get("status") == "successful"
        and tx.get("currency") == "RWF"
        and float(tx.get("amount", 0)) >= PRO_PRICE_RWF
    )
    if not ok:
        logger.warning("Flutterwave verification failed or amount mismatch: %s", data)
    return ok, tx
