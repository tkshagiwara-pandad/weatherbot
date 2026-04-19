import argparse
import json
import logging
import os
import time

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Weather trading bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan markets and log signals without placing any orders or signing transactions",
    )
    args = parser.parse_args()

    with open("config.json") as f:
        cfg = json.load(f)

    # 秘密鍵はドライランでは不要
    private_key = os.environ.get("WALLET_PRIVATE_KEY")
    if not args.dry_run and not private_key:
        raise RuntimeError("WALLET_PRIVATE_KEY not set (required for live trading)")

    # Imports are here so env vars are loaded before any web3 init
    from bot.market import PolymarketClient
    from bot.notifier import TelegramNotifier
    from bot.strategy import Strategy
    from bot.trader import Trader
    from bot.weather import WeatherClient
    from signer.signer import SigningService

    signer = SigningService(
        private_key="0x" + "0" * 64 if args.dry_run else private_key,
        allowed_contracts=cfg["allowed_contracts"],
        max_trade_usdc=cfg["max_trade_usdc"],
        daily_limit_usdc=cfg["daily_limit_usdc"],
    )
    weather = WeatherClient()
    polymarket = PolymarketClient()
    strategy = Strategy(
        min_edge=cfg["min_edge"],
        trade_amount_usdc=cfg["trade_amount_usdc"],
        log_path=cfg.get("trade_log", "trades.jsonl"),
    )
    tg_token = cfg.get("telegram_token")
    tg_chat_id = cfg.get("telegram_chat_id")
    notifier = TelegramNotifier(tg_token, tg_chat_id) if tg_token and tg_chat_id else None

    trader = Trader(weather, polymarket, strategy, signer, dry_run=args.dry_run,
                    max_days_ahead=cfg.get("max_days_ahead", 5), notifier=notifier,
                    focus_cities=cfg.get("focus_cities"),
                    watch_cities=cfg.get("watch_cities"))

    interval = cfg.get("scan_interval_minutes", 60) * 60
    mode_label = " [DRY RUN - no orders will be placed]" if args.dry_run else ""
    logger.info(
        "Bot started%s | address=%s | scan every %d min",
        mode_label, signer.address, interval // 60,
    )
    if notifier:
        notifier.send(
            f"🤖 <b>Bot started</b>{'  [DRY RUN]' if args.dry_run else ''}\n"
            f"Address: <code>{signer.address}</code>\n"
            f"Scan every {interval // 60} min  |  Daily limit: {cfg['daily_limit_usdc']} USDC"
        )

    while True:
        try:
            trader.scan_and_trade()
        except Exception as exc:
            logger.error("Scan failed: %s", exc, exc_info=True)
        logger.info("Sleeping %d minutes...", interval // 60)
        time.sleep(interval)


if __name__ == "__main__":
    main()
