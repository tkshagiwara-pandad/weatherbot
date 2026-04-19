import logging
import time
from datetime import date, datetime, timedelta

import requests

from bot.market import CLOB_URL, BookQuote, PolymarketClient, WeatherMarket
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
        max_days_ahead: int = 5,
    ):
        self._weather = weather
        self._polymarket = polymarket
        self._strategy = strategy
        self._signer = signer
        self._dry_run = dry_run
        self._max_days_ahead = max_days_ahead

    def scan_and_trade(self):
        logger.info("Scanning markets...")
        markets = self._polymarket.get_weather_markets()
        traded = 0
        skip_no_city = skip_date = skip_no_edge = skip_error = skip_no_liq = 0

        today = date.today()

        # スキャン前に unique な (都市, 日付) を一括フェッチ（1ペア=1リクエスト）
        city_dates: set[tuple[str, date]] = set()
        for m in markets:
            if m.city and m.active:
                dt = self._parse_date(m.end_date)
                if today < dt <= today + timedelta(days=self._max_days_ahead):
                    city_dates.add((m.city, dt))
        self._weather.prefetch(city_dates)

        # (abs_edge, edge, forecast_prob, is_temp, volume, question)
        candidates: list[tuple[float, float, float, bool, float, str]] = []

        for market in markets:
            if not market.city or not market.active:
                skip_no_city += 1
                continue
            try:
                target_date = self._parse_date(market.end_date)
                # 当日・過去 or 14日超先のマーケットはスキップ（予報精度外 / 当日は市場がリアルタイムデータを反映）
                if target_date <= today or target_date > today + timedelta(days=self._max_days_ahead):
                    skip_date += 1
                    continue
                forecast = self._weather.get_forecast(market.city, target_date)
                if forecast is None:
                    skip_error += 1
                    continue
                result = self._strategy.top_candidates(market, forecast)
                if result is None:
                    _q = market.question.lower()
                    if any(kw in _q for kw in ("rain", "precipitation", "snow", "storm")):
                        logger.info("SKIP unknown_precip: %s", market.question[:100])
                    skip_no_edge += 1
                    continue
                fp, edge, is_temp = result
                candidates.append((abs(edge), edge, fp, is_temp, market.volume, market.question))
                if abs(edge) >= (self._strategy._min_edge + self._strategy._edge_adjustment):
                    signal = self._strategy.evaluate(market, forecast)
                    if signal:
                        token_id = (
                            market.yes_token_id if signal.side == "yes" else market.no_token_id
                        )
                        quote = self._polymarket.get_book_quote(token_id)
                        if quote is None or quote.best_ask is None:
                            logger.info("SKIP no_ask  mid=%.0f%%  %s",
                                        market.yes_price * 100, market.question[:70])
                            skip_no_liq += 1
                            continue
                        # Require at least $1 of ask-side depth to avoid ghost quotes
                        if quote.ask_size * quote.best_ask < 1.0:
                            logger.info("SKIP thin_ask  depth=%.2f USDC  ask=%.0f%%  %s",
                                        quote.ask_size * quote.best_ask, quote.best_ask * 100, market.question[:70])
                            skip_no_liq += 1
                            continue
                        # Require two-sided book with tight spread
                        spread_rt = (quote.best_ask - (quote.best_bid or 0))
                        if quote.best_bid is None or spread_rt > 0.20:
                            logger.info("SKIP wide_spread  bid=%.0f%%  ask=%.0f%%  spread=%.0f%%  %s",
                                        (quote.best_bid or 0) * 100, quote.best_ask * 100,
                                        spread_rt * 100, market.question[:70])
                            skip_no_liq += 1
                            continue
                        # Recompute edge against real ask price (must be strictly positive)
                        real_edge = (
                            signal.forecast_prob - quote.best_ask
                            if signal.side == "yes"
                            else (1.0 - signal.forecast_prob) - quote.best_ask
                        )
                        if real_edge < (self._strategy._min_edge + self._strategy._edge_adjustment):
                            logger.info("SKIP low_edge  side=%s  fp=%.0f%%  ask=%.0f%%  real_edge=%+.3f  mid_edge=%+.3f  %s",
                                        signal.side, signal.forecast_prob * 100, quote.best_ask * 100,
                                        real_edge, edge, market.question[:60])
                            skip_no_edge += 1
                            continue
                        signal.market_price = quote.best_ask
                        signal.edge = real_edge
                        self._execute(signal, quote)
                        traded += 1
                        if not self._dry_run:
                            time.sleep(1)
                        continue
                skip_no_edge += 1
            except SecurityError as exc:
                logger.warning("Blocked by signer: %s", exc)
                skip_error += 1
            except Exception as exc:
                logger.warning("Error on market %s: %s", market.market_id[:8], exc)
                skip_error += 1

        mode = "[DRY RUN] " if self._dry_run else ""
        logger.info(
            "%sDone: %d traded | skipped: no_city=%d date=%d no_edge=%d no_liq=%d error=%d | remaining=%.2f USDC",
            mode, traded, skip_no_city, skip_date, skip_no_edge, skip_no_liq, skip_error,
            self._signer.daily_remaining(),
        )

        if candidates:
            candidates.sort(reverse=True)
            temp_count = sum(1 for _, _, _, is_temp, _, _ in candidates if is_temp)
            logger.info(
                "Top candidates (%d temp / %d precip out of %d evaluated):",
                temp_count, len(candidates) - temp_count, len(candidates),
            )
            for abs_edge, edge, fp, is_temp, volume, question in candidates[:5]:
                kind = "TEMP" if is_temp else "PRCP"
                logger.info("  [%s] edge=%+.3f  fp=%.0f%%  vol=$%.0f  %s", kind, edge, fp * 100, volume, question[:60])
        else:
            logger.info("No candidates evaluated (all markets filtered before edge check)")

        self._strategy.resolve_open_trades(self._polymarket)
        self._strategy.self_learn()

    # ------------------------------------------------------------------
    def _execute(self, signal: TradeSignal, quote: BookQuote):
        remaining = self._signer.daily_remaining()
        amount = min(self._strategy._trade_amount, remaining)
        if amount < 1.0:
            logger.info("Daily limit reached, stopping trades")
            return

        if self._dry_run:
            bid_str = f"{quote.best_bid:.3f}" if quote.best_bid is not None else "—"
            logger.info(
                "[DRY RUN] Would buy %s  amount=%.2f USDC  ask=%.3f  edge=%+.3f\n"
                "          market : %s\n"
                "          forecast_prob=%.0f%%  bid=%s  ask=%.0f%%  spread=%.0f%%",
                signal.side, amount, signal.market_price, signal.edge,
                signal.market.question,
                signal.forecast_prob * 100, bid_str,
                signal.market_price * 100,
                ((quote.best_ask or 0) - (quote.best_bid or 0)) * 100,
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
            return date.min  # empty/malformed end_date → filtered by target_date < today
