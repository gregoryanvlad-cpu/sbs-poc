from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


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
        timeout_seconds: int = 6,
    ) -> None:
        self._merchant_id = merchant_id
        self._secret = secret
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(
            total=timeout_seconds,
            connect=min(3, timeout_seconds),
            sock_connect=min(3, timeout_seconds),
            sock_read=timeout_seconds,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "X-MerchantId": self._merchant_id,
            "X-Secret": self._secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "sbsconnect-bot/1.0",
        }

    async def create_transaction(
        self,
        *,
        payment_method: int | None,
        amount: int,
        currency: str = "RUB",
        description: str,
        return_url: str,
        failed_url: str,
        payload: str,
    ) -> PlategaCreateResult:
        """Create a Platega payment link.

        Platega has two create endpoints:
        - /v2/transaction/process: generic link, user chooses an available method.
        - /transaction/process: fixed payment method.

        The production logs show that the fixed-method endpoint hangs on creation.
        Therefore the generic v2 endpoint is tried first.
        """
        body: dict[str, Any] = {
            "paymentDetails": {
                "amount": int(amount),
                "currency": currency,
            },
            "description": description,
            "return": return_url,
            "failedUrl": failed_url,
            "payload": payload,
        }

        errors: list[str] = []

        try:
            data = await self._post_json("/v2/transaction/process", body)
            return self._parse_create_result(data)
        except PlategaError as exc:
            errors.append(f"v2: {exc}")
            log.warning("platega_v2_create_failed", extra={"error": str(exc)})

        try:
            method = int(payment_method) if payment_method is not None else 0
        except Exception:
            method = 0

        if method > 0:
            fixed_body = dict(body)
            fixed_body["paymentMethod"] = method
            try:
                data = await self._post_json("/transaction/process", fixed_body)
                return self._parse_create_result(data)
            except PlategaError as exc:
                errors.append(f"fixed method {method}: {exc}")
                log.warning(
                    "platega_fixed_method_create_failed",
                    extra={"payment_method": method, "error": str(exc)},
                )

        raise PlategaError("; ".join(errors) if errors else "Platega create_transaction failed")

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        safe_body = dict(body)
        safe_body["payload"] = str(safe_body.get("payload") or "")[:200]
        log.info("platega_request", extra={"path": path, "body": safe_body})
        try:
            async with aiohttp.ClientSession(timeout=self._timeout, trust_env=True) as session:
                async with session.post(url, json=body, headers=self._headers()) as resp:
                    data = await _read_json_best_effort(resp)
                    if resp.status >= 400:
                        raise PlategaError(f"HTTP {resp.status}: {data}")
                    return data
        except PlategaError:
            raise
        except asyncio.TimeoutError as exc:
            raise PlategaError(f"timeout POST {path}") from exc
        except aiohttp.ClientError as exc:
            raise PlategaError(f"request failed POST {path}: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _parse_create_result(data: dict[str, Any]) -> PlategaCreateResult:
        tx_id = str(data.get("transactionId") or data.get("id") or data.get("externalId") or "").strip()
        redirect = str(
            data.get("redirect")
            or data.get("url")
            or data.get("payUrl")
            or data.get("paymentUrl")
            or data.get("payformUrl")
            or ""
        ).strip()
        status = str(data.get("status") or "").strip()
        if not tx_id or not redirect:
            raise PlategaError(f"unexpected response: {data}")
        return PlategaCreateResult(transaction_id=tx_id, redirect_url=redirect, status=status or "PENDING")

    async def get_transaction_status(self, *, transaction_id: str) -> PlategaStatusResult:
        url = f"{self._base_url}/transaction/{transaction_id}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout, trust_env=True) as session:
                async with session.get(url, headers=self._headers()) as resp:
                    data = await _read_json_best_effort(resp)
                    if resp.status >= 400:
                        raise PlategaError(f"Platega get_transaction_status failed: HTTP {resp.status}: {data}")
        except PlategaError:
            raise
        except asyncio.TimeoutError as exc:
            raise PlategaError(f"timeout GET /transaction/{transaction_id}") from exc
        except aiohttp.ClientError as exc:
            raise PlategaError(f"request failed GET /transaction/{transaction_id}: {type(exc).__name__}: {exc}") from exc

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
        tx_id = str(data.get("id") or data.get("transactionId") or transaction_id).strip()
        return PlategaStatusResult(transaction_id=tx_id, status=status or "UNKNOWN", amount=amount, currency=currency, payload=payload)


async def _read_json_best_effort(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    """Read JSON while staying resilient to broken/missing content-type."""
    try:
        data = await resp.json(content_type=None)
        if isinstance(data, dict):
            return data
        return {"_json": data}
    except Exception:
        try:
            txt = await resp.text()
        except Exception:
            txt = ""
        return {"_raw": txt}
