import logging
from dataclasses import dataclass
from datetime import date

from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

logger = logging.getLogger(__name__)

POLYMARKET_ORDER_TYPES = {
    "Order": [
        {"name": "maker", "type": "address"},
        {"name": "taker", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "expiration", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "feeRateBps", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
    ]
}


@dataclass
class TradeRequest:
    market_id: str
    token_id: str
    side: str           # "buy" or "sell"
    amount_usdc: float
    price: float        # limit price 0.01-0.99
    contract: str       # must be in whitelist


@dataclass
class SignedOrder:
    order_data: dict
    signature: str
    maker: str


class SecurityError(Exception):
    pass


class SigningService:
    """
    Isolated signing layer. Private key never leaves this object.
    All requests are validated against contract whitelist and spending limits
    before signing. The LLM/bot layer only receives TradeRequest objects —
    it never touches the key directly.
    """

    def __init__(
        self,
        private_key: str,
        allowed_contracts: list[str],
        max_trade_usdc: float,
        daily_limit_usdc: float,
        chain_id: int = 137,
    ):
        self._account = Account.from_key(private_key)
        self._allowed = {Web3.to_checksum_address(c) for c in allowed_contracts}
        self._max_trade_usdc = max_trade_usdc
        self._daily_limit_usdc = daily_limit_usdc
        self._chain_id = chain_id
        self._daily_spent = 0.0
        self._reset_day = date.today()
        logger.info("SigningService ready. Address: %s", self._account.address)

    @property
    def address(self) -> str:
        return self._account.address

    def daily_remaining(self) -> float:
        self._tick_day()
        return self._daily_limit_usdc - self._daily_spent

    def sign_order(self, request: TradeRequest, nonce: int, expiration: int) -> SignedOrder:
        self._validate(request)

        contract = Web3.to_checksum_address(request.contract)
        maker_amount = int(request.amount_usdc * 1_000_000)   # USDC = 6 decimals
        taker_amount = int(maker_amount / request.price)

        domain = {
            "name": "Polymarket CTF Exchange",
            "version": "1",
            "chainId": self._chain_id,
            "verifyingContract": contract,
        }
        message = {
            "maker": self._account.address,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": int(request.token_id),
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": expiration,
            "nonce": nonce,
            "feeRateBps": 0,
            "side": 0 if request.side == "buy" else 1,
            "signatureType": 0,  # EOA
        }

        encoded = encode_typed_data(
            domain_data=domain,
            message_types=POLYMARKET_ORDER_TYPES,
            message_data=message,
        )
        signed = self._account.sign_message(encoded)
        sig = signed.signature.hex()

        self._daily_spent += request.amount_usdc
        logger.info(
            "Signed: %s %.2f USDC @ %.3f  edge market=%s  daily=%.2f/%.2f",
            request.side, request.amount_usdc, request.price,
            request.market_id[:8], self._daily_spent, self._daily_limit_usdc,
        )

        return SignedOrder(
            order_data={**message, "signature": sig},
            signature=sig,
            maker=self._account.address,
        )

    # ------------------------------------------------------------------
    def _tick_day(self):
        today = date.today()
        if today != self._reset_day:
            self._daily_spent = 0.0
            self._reset_day = today

    def _validate(self, req: TradeRequest):
        self._tick_day()

        contract = Web3.to_checksum_address(req.contract)
        if contract not in self._allowed:
            raise SecurityError(f"Contract not whitelisted: {contract}")

        if req.amount_usdc <= 0:
            raise SecurityError("Amount must be positive")

        if req.amount_usdc > self._max_trade_usdc:
            raise SecurityError(
                f"Trade {req.amount_usdc} USDC > per-trade limit {self._max_trade_usdc}"
            )

        remaining = self._daily_limit_usdc - self._daily_spent
        if req.amount_usdc > remaining:
            raise SecurityError(
                f"Trade would exceed daily limit. Remaining: {remaining:.2f} USDC"
            )

        if not (0.01 <= req.price <= 0.99):
            raise SecurityError(f"Price {req.price} out of valid range [0.01, 0.99]")
