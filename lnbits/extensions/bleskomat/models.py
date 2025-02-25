import json
import time
from typing import Dict

from fastapi.params import Query
from pydantic import BaseModel, validator
from starlette.requests import Request

from lnbits import bolt11
from lnbits.core.services import pay_invoice, PaymentFailure

from . import db
from .exchange_rates import exchange_rate_providers, fiat_currencies
from .helpers import LnurlValidationError, get_callback_url


class CreateBleskomat(BaseModel):
    name: str = Query(...)
    fiat_currency: str = Query(...)
    exchange_rate_provider: str = Query(...)
    fee: str = Query(...)

    @validator("fiat_currency")
    def allowed_fiat_currencies(cls, v):
        if v not in fiat_currencies.keys():
            raise ValueError("Not allowed currency")
        return v

    @validator("exchange_rate_provider")
    def allowed_providers(cls, v):
        if v not in exchange_rate_providers.keys():
            raise ValueError("Not allowed provider")
        return v

    @validator("fee")
    def fee_type(cls, v):
        if not isinstance(v, (str, float, int)):
            raise ValueError("Fee type not allowed")
        return v


class Bleskomat(BaseModel):
    id: str
    wallet: str
    api_key_id: str
    api_key_secret: str
    api_key_encoding: str
    name: str
    fiat_currency: str
    exchange_rate_provider: str
    fee: str


class BleskomatLnurl(BaseModel):
    id: str
    bleskomat: str
    wallet: str
    hash: str
    tag: str
    params: str
    api_key_id: str
    initial_uses: int
    remaining_uses: int
    created_time: int
    updated_time: int

    def has_uses_remaining(self) -> bool:
        # When initial uses is 0 then the LNURL has unlimited uses.
        return self.initial_uses == 0 or self.remaining_uses > 0

    def get_info_response_object(self, secret: str, req: Request) -> Dict[str, str]:
        tag = self.tag
        params = json.loads(self.params)
        response = {"tag": tag}
        if tag == "withdrawRequest":
            for key in ["minWithdrawable", "maxWithdrawable", "defaultDescription"]:
                response[key] = params[key]
            response["callback"] = get_callback_url(req)
            response["k1"] = secret
        return response

    def validate_action(self, query: Dict[str, str]) -> None:
        tag = self.tag
        params = json.loads(self.params)
        # Perform tag-specific checks.
        if tag == "withdrawRequest":
            for field in ["pr"]:
                if not field in query:
                    raise LnurlValidationError(f'Missing required parameter: "{field}"')
            # Check the bolt11 invoice(s) provided.
            pr = query["pr"]
            if "," in pr:
                raise LnurlValidationError("Multiple payment requests not supported")
            try:
                invoice = bolt11.decode(pr)
            except ValueError:
                raise LnurlValidationError(
                    'Invalid parameter ("pr"): Lightning payment request expected'
                )
            if invoice.amount_msat < params["minWithdrawable"]:
                raise LnurlValidationError(
                    'Amount in invoice must be greater than or equal to "minWithdrawable"'
                )
            if invoice.amount_msat > params["maxWithdrawable"]:
                raise LnurlValidationError(
                    'Amount in invoice must be less than or equal to "maxWithdrawable"'
                )
        else:
            raise LnurlValidationError(f'Unknown subprotocol: "{tag}"')

    async def execute_action(self, query: Dict[str, str]):
        self.validate_action(query)
        used = False
        async with db.connect() as conn:
            if self.initial_uses > 0:
                used = await self.use(conn)
                if not used:
                    raise LnurlValidationError("Maximum number of uses already reached")
            tag = self.tag
            if tag == "withdrawRequest":
                try:
                    await pay_invoice(
                        wallet_id=self.wallet, payment_request=query["pr"]
                    )
                except (ValueError, PermissionError, PaymentFailure) as e:
                    raise LnurlValidationError("Failed to pay invoice: " + str(e))
                except Exception as e:
                    print(str(e))
                    raise LnurlValidationError("Unexpected error")

    async def use(self, conn) -> bool:
        now = int(time.time())
        result = await conn.execute(
            """
            UPDATE bleskomat.bleskomat_lnurls
            SET remaining_uses = remaining_uses - 1, updated_time = ?
            WHERE id = ?
                AND remaining_uses > 0
            """,
            (now, self.id),
        )
        return result.rowcount > 0
