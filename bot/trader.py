import logging
import time
from datetime import date, datetime, timedelta

import requests

from bot.market import CLOB_URL, BookQuote, PolymarketClient, WeatherMarket
from bot.notifier import TelegramNotifier
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
        notifier: TelegramNotifier = None,
        focus_cities: list = None,
        watch_cities: list = None,
    ):
        self._weather = weather
        self._polymarket = polymarket
        self._strategy = strategy
        self._signer = signer
        self._dry_run = dry_run
        self._max_days_ahead = max_days_ahead
        self._notifier = notifier
        self._focus_cities: set[str] = set(focus_cities) if focus_cities else set()
        self._watch_cities: set[str] = set(watch_cities) if watch_cities else set()
        self._last_forecast_date: date = date.min

    def scan_and_trade(self):
        today = date.today()
        if self._notifier and self._watch_cities and today != self._last_forecast_date:
            self._send_forecast_summary(today)
            self._last_forecast_date = today

        logger.info("Scanning markets...")
        markets = self._polymarket.get_weather_markets()
        traded = 0
        skip_no_city = skip_focus = skip_date = skip_no_edge = skip_error = skip_no_liq = 0

        # スキャン前に unique な (都市, 日付) を一括フェッチ（1ペア=1リクエスト）
        city_dates: set[tuple[str, date]] = set()
        past_city_dates: set[tuple[str, date]] = set()
        for m in markets:
            if not m.city:
                continue
            if self._focus_cities and m.city not in self._focus_cities:
                continue
            dt = self._parse_date(m.end_date)
            if today < dt <= today + timedelta(days=self._max_days_ahead):
                city_dates.add((m.city, dt))
            elif today - timedelta(days=7) <= dt <= today:
                past_city_dates.add((m.city, dt))
        self._weather.prefetch(city_dates)
        self._weather.record_actuals(past_city_dates)

        # (abs_edge, edge, forecast_prob, is_temp, volume, question, target_date, temp_max_c, temp_min_c)
        candidates: list[tuple[float, float, float, bool, float, str, date, float, float]] = []

        for market in markets:
            if not market.city or not market.active:
                skip_no_city += 1
                continue
            if self._focus_cities and market.city not in self._focus_cities:
                skip_focus += 1
                continue
            try:
                target_date = self._parse_date(market.end_date)
                if target_date <= today or target_date > today + timedelta(days=self._max_days_ahead):
                    skip_date += 1
                    continue
                forecast = self._weather.get_forecast(market.city, target_date)
                if forecast is None:
                    skip_error += 1
                    continue
                result = self._strategy.top_candidates(market, forecast)
                if result is None:
                    skip_no_edge += 1
                    continue
                fp, edge, is_temp = result
                candidates.append((abs(edge), edge, fp, is_temp, market.volume, market.question,
                                   target_date, forecast.temp_max_c, forecast.temp_min_c))
                if abs(edge) >= (self._strategy._min_edge + self._strategy._edge_adjustment):
                    signal = self._strategy.evaluate(market, forecast)
                    if signal:
                        token_id = (
                            market.yes_token_id if signal.side == "yes" else market.no_token_id
                        )
                        quote = self._polymarket.get_book_quote(token_id)
                        if quote is None or quote.best_ask is None:
                            logger.debug("SKIP no_ask  mid=%.0f%%  %s",
                                         market.yes_price * 100, market.question[:70])
                            skip_no_liq += 1
                            continue
                        # Require at least $1 of ask-side depth to avoid ghost quotes
                        if quote.ask_size * quote.best_ask < 1.0:
                            logger.debug("SKIP thin_ask  depth=%.2f USDC  ask=%.0f%%  %s",
                                         quote.ask_size * quote.best_ask, quote.best_ask * 100, market.question[:70])
                            skip_no_liq += 1
                            continue
                        # Require two-sided book with tight spread
                        spread_rt = (quote.best_ask - (quote.best_bid or 0))
                        if quote.best_bid is None or spread_rt > 0.20:
                            logger.debug("SKIP wide_spread  bid=%.0f%%  ask=%.0f%%  spread=%.0f%%  %s",
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
                            logger.debug("SKIP low_edge  side=%s  fp=%.0f%%  ask=%.0f%%  real_edge=%+.3f  mid_edge=%+.3f  %s",
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
            "%sDone: %d traded | skipped: no_city=%d focus=%d date=%d no_edge=%d no_liq=%d error=%d | remaining=%.2f USDC",
            mode, traded, skip_no_city, skip_focus, skip_date, skip_no_edge, skip_no_liq, skip_error,
            self._signer.daily_remaining(),
        )

        if candidates:
            min_edge_thresh = self._strategy._min_edge + self._strategy._edge_adjustment
            tradeable = [c for c in candidates if c[0] >= min_edge_thresh]
            tradeable.sort(key=lambda x: (x[4], x[0]), reverse=True)  # volume desc, then abs_edge desc
            temp_count = sum(1 for _, _, _, is_temp, *_ in candidates if is_temp)
            logger.info(
                "Top candidates (%d tradeable / %d temp / %d precip out of %d evaluated):",
                len(tradeable), temp_count, len(candidates) - temp_count, len(candidates),
            )
            for abs_edge, edge, fp, is_temp, volume, question, tgt_date, tmax, tmin in tradeable[:5]:
                kind = "TEMP" if is_temp else "PRCP"
                if is_temp:
                    logger.info("  [%s] edge=%+.3f  fp=%.0f%%  forecast=max%.1f°C/min%.1f°C  date=%s  vol=$%.0f  %s",
                                kind, edge, fp * 100, tmax, tmin, tgt_date, volume, question[:60])
                else:
                    logger.info("  [%s] edge=%+.3f  fp=%.0f%%  date=%s  vol=$%.0f  %s",
                                kind, edge, fp * 100, tgt_date, volume, question[:60])
            if not tradeable:
                logger.info("  (no candidates above min_edge threshold)")
        else:
            logger.info("No candidates evaluated (all markets filtered before edge check)")

        if self._watch_cities and self._notifier and candidates:
            min_edge_thresh = self._strategy._min_edge + self._strategy._edge_adjustment
            watched = [
                (abs_edge, edge, fp, is_temp, volume, question, tgt_date, tmax, tmin)
                for abs_edge, edge, fp, is_temp, volume, question, tgt_date, tmax, tmin in candidates
                if abs_edge >= min_edge_thresh
                and any(city.lower() in question.lower() for city in self._watch_cities)
            ]
            if watched:
                lines = []
                for _, edge, fp, is_temp, volume, question, tgt_date, tmax, tmin in watched[:5]:
                    kind = "🌡" if is_temp else "🌧"
                    lines.append(
                        f"{kind} edge={edge:+.3f}  fp={fp:.0%}  vol=${volume:.0f}\n"
                        f"   {question[:70]}\n"
                        f"   forecast={tmax:.1f}°C/{tmin:.1f}°C  date={tgt_date}"
                    )
                self._notifier.send(
                    "👀 <b>Watch city alert</b>\n\n" + "\n\n".join(lines)
                )

        self._strategy.resolve_open_trades(self._polymarket)
        self._strategy.self_learn()

    # ------------------------------------------------------------------
    def _execute(self, signal: TradeSignal, quote: BookQuote):
        remaining = self._signer.daily_remaining()
        amount = min(self._strategy._trade_amount, remaining)
        if amount < 1.0:
            logger.info("Daily limit reached, stopping trades")
            if self._notifier:
                self._notifier.send("⚠️ デイリーリミット到達 — 本日の取引を停止しました")
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
        if self._notifier:
            self._notifier.send(
                f"✅ <b>Order placed</b>\n"
                f"Side: {signal.side.upper()}  Amount: {amount:.2f} USDC\n"
                f"Ask: {signal.market_price:.3f}  Edge: {signal.edge:+.3f}\n"
                f"fp: {signal.forecast_prob:.0%}\n"
                f"{signal.market.question[:80]}"
            )

    def _send_forecast_summary(self, today: date):
        tomorrow = today + timedelta(days=1)
        lines = [f"🌤 <b>Daily Forecast  {today.strftime('%m/%d')}–{tomorrow.strftime('%m/%d')}</b>\n"]
        for city in sorted(self._watch_cities):
            f0 = self._weather.get_forecast(city, today)
            f1 = self._weather.get_forecast(city, tomorrow)
            city_lines = [f"<b>{city}</b>"]
            for label, f in [("今日", f0), ("明日", f1)]:
                if f:
                    city_lines.append(
                        f"  {label}: {f.temp_max_c:.1f}°C/{f.temp_min_c:.1f}°C  "
                        f"雨{f.precip_prob:.0%}  {f.conditions}"
                    )
                else:
                    city_lines.append(f"  {label}: N/A")
            lines.append("\n".join(city_lines))
        self._notifier.send("\n\n".join(lines))

    @staticmethod
    def _parse_date(end_date_iso: str) -> date:
        try:
            return datetime.fromisoformat(end_date_iso.replace("Z", "+00:00")).date()
        except Exception:
            return date.min  # empty/malformed end_date → filtered by target_date < today
