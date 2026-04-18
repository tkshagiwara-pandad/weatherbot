import logging
import time
from datetime import date, datetime, timedelta

import requests

from bot.market import CLOB_URL, PolymarketClient, WeatherMarket
from bot.strategy import Strategy, TradeSignal
from bot.weather import WeatherClient
from signer.signer import SecurityError, SigningService, TradeRequest

logger = logging.getLogger(__name__)

# Verify this address at https://docs.polymarket.com before going live
CLOB_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"


class Trader:
    def __init__(
        self,
        weather: WeatherClient,
        polymarket: PolymarketClient,
        strategy: Strategy,
        signer: SigningService,
        dry_run: bool = False,
    ):
        self._weather = weather
        self._polymarket = polymarket
        self._strategy = strategy
        self._signer = signer
        self._dry_run = dry_run

    def scan_and_trade(self):
        logger.info("Scanning markets...")
        markets = self._polymarket.get_weather_markets()
        traded = 0
        skip_no_city = skip_date = skip_no_edge = skip_error = 0

        today = date.today()

        # スキャン前に unique な (都市, 日付) を一括フェッチ（1ペア=1リクエスト）
        city_dates: set[tuple[str, date]] = set()
        for m in markets:
            if m.city and m.active:
                dt = self._parse_date(m.end_date)
                if today <= dt <= today + timedelta(days=14):
                    city_dates.add((m.city, dt))
        self._weather.prefetch(city_dates)

        for market in markets:
            if not market.city or not market.active:
                skip_no_city += 1
                continue
            try:
                target_date = self._parse_date(market.end_date)
                # 過去 or 14日超先のマーケットはスキップ（予報精度外）
                if target_date < today or target_date > today + timedelta(days=14):
                    skip_date += 1
                    continue
                forecast = self._weather.get_forecast(market.city, target_date)
                if forecast is None:
                    skip_error += 1
                    continue
                signal = self._strategy.evaluate(market, forecast)
                if not signal:
                    skip_no_edge += 1
                    continue
                self._execute(signal)
                traded += 1
                time.sleep(1)
            except SecurityError as exc:
                logger.warning("Blocked by signer: %s", exc)
                skip_error += 1
            except Exception as exc:
                logger.warning("Error on market %s: %s", market.market_id[:8], exc)
                skip_error += 1

        mode = "[DRY RUN] " if self._dry_run else ""
        logger.info(
            "%sDone: %d traded | skipped: no_city=%d date=%d no_edge=%d error=%d | remaining=%.2f USDC",
            mode, traded, skip_no_city, skip_date, skip_no_edge, skip_error,
            self._signer.daily_remaining(),
        )
        self._strategy.self_learn()

    # ------------------------------------------------------------------
    def _execute(self, signal: TradeSignal):
        remaining = self._signer.daily_remaining()
        amount = min(self._strategy._trade_amount, remaining)
        if amount < 1.0:
            logger.info("Daily limit reached, stopping trades")
            return

        if self._dry_run:
            logger.info(
                "[DRY RUN] Would buy %s  amount=%.2f USDC  price=%.3f  edge=%+.3f\n"
                "          market : %s\n"
                "          forecast_prob=%.0f%%  market_price=%.0f%%",
                signal.side, amount, signal.market_price, signal.edge,
                signal.market.question,
                signal.forecast_prob * 100, signal.market_price * 100,
            )
            return

        token_id = (
            signal.market.yes_token_id if signal.side == "yes" else signal.market.no_token_id
        )
        now = int(datetime.utcnow().timestamp())
        request = TradeRequest(
            market_id=signal.market.market_id,
            token_id=token_id,
            side="buy",
            amount_usdc=amount,
            price=signal.market_price,
            contract=CLOB_EXCHANGE,
        )
        signed = self._signer.sign_order(request, nonce=now * 1000, expiration=now + 3600)

        resp = requests.post(
            f"{CLOB_URL}/order",
            json={"order": signed.order_data, "owner": signed.maker, "orderType": "GTC"},
            timeout=15,
        )
        resp.raise_for_status()

        logger.info(
            "Order placed: %s %.2f USDC @ %.3f  [edge=%+.3f] %s",
            signal.side, amount, signal.market_price, signal.edge,
            signal.market.question[:60],
        )
        self._strategy.log_trade(signal, amount)

    @staticmethod
    def _parse_date(end_date_iso: str) -> date:
        try:
            return datetime.fromisoformat(end_date_iso.replace("Z", "+00:00")).date()
        except Exception:
            return date.today() + timedelta(days=1)
