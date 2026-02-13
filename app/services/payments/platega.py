from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import aiohttp


class PlategaError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlategaCreateResult:
    transaction_id: str
    redirect_url: str
    status: str


@dataclass(frozen=True)
class PlategaStatusResult:
    transaction_id: str
    status: str
    amount: int | None = None
    currency: str | None = None
    payload: str | None = None


class PlategaClient:
    """Minimal Platega API client.

    Docs: https://docs.platega.io/
    Base URL: https://app.platega.io/
    """

    def __init__(
        self,
        *,
        merchant_id: str,
        secret: str,
        base_url: str = "https://app.platega.io",
        timeout_seconds: int = 20,
    ) -> None:
        self._merchant_id = merchant_id
        self._secret = secret
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    def _headers(self) -> dict[str, str]:
        return {
            "X-MerchantId": self._merchant_id,
            "X-Secret": self._secret,
            "Content-Type": "application/json",
        }

    async def create_transaction(
        self,
        *,
        payment_method: int,
        amount: int,
        currency: str = "RUB",
        description: str,
        return_url: str,
        failed_url: str,
        payload: str,
    ) -> PlategaCreateResult:
        url = f"{self._base_url}/transaction/process"
        body: dict[str, Any] = {
            "paymentMethod": int(payment_method),
            "paymentDetails": {
                "amount": int(amount),
                "currency": currency,
            },
            "description": description,
            "return": return_url,
            "failedUrl": failed_url,
            "payload": payload,
        }

        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(url, json=body, headers=self._headers()) as resp:
                data = await _read_json_best_effort(resp)
                if resp.status >= 400:
                    raise PlategaError(f"Platega create_transaction failed: HTTP {resp.status}: {data}")

        tx_id = str(data.get("transactionId") or data.get("id") or "").strip()
        redirect = str(data.get("redirect") or "").strip()
        status = str(data.get("status") or "").strip()
        if not tx_id or not redirect:
            raise PlategaError(f"Platega create_transaction: unexpected response: {data}")
        return PlategaCreateResult(transaction_id=tx_id, redirect_url=redirect, status=status or "PENDING")

    async def get_transaction_status(self, *, transaction_id: str) -> PlategaStatusResult:
        url = f"{self._base_url}/transaction/{transaction_id}"
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(url, headers=self._headers()) as resp:
                data = await _read_json_best_effort(resp)
                if resp.status >= 400:
                    raise PlategaError(f"Platega get_transaction_status failed: HTTP {resp.status}: {data}")

        status = str(data.get("status") or "").strip()
        pd = data.get("paymentDetails") or {}
        amount = None
        currency = None
        if isinstance(pd, dict):
            try:
                amount = int(pd.get("amount")) if pd.get("amount") is not None else None
            except Exception:
                amount = None
            currency = str(pd.get("currency") or "").strip() or None
        payload = str(data.get("payload") or "").strip() or None
        tx_id = str(data.get("id") or transaction_id).strip()
        return PlategaStatusResult(transaction_id=tx_id, status=status or "UNKNOWN", amount=amount, currency=currency, payload=payload)


async def _read_json_best_effort(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    """Read JSON while staying resilient to broken/missing content-type."""
    try:
        return await resp.json(content_type=None)
    except Exception:
        try:
            txt = await resp.text()
        except Exception:
            txt = ""
        return {"_raw": txt}
