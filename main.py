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
    with open("config.json") as f:
        cfg = json.load(f)

    private_key = os.environ.get("WALLET_PRIVATE_KEY")
    if not private_key:
        raise RuntimeError("WALLET_PRIVATE_KEY not set")

    weather_key = os.environ.get("VISUAL_CROSSING_API_KEY")
    if not weather_key:
        raise RuntimeError("VISUAL_CROSSING_API_KEY not set")

    # Imports are here so env vars are loaded before any web3 init
    from bot.market import PolymarketClient
    from bot.strategy import Strategy
    from bot.trader import Trader
    from bot.weather import WeatherClient
    from signer.signer import SigningService

    signer = SigningService(
        private_key=private_key,
        allowed_contracts=cfg["allowed_contracts"],
        max_trade_usdc=cfg["max_trade_usdc"],
        daily_limit_usdc=cfg["daily_limit_usdc"],
    )
    weather = WeatherClient(api_key=weather_key)
    polymarket = PolymarketClient()
    strategy = Strategy(
        min_edge=cfg["min_edge"],
        trade_amount_usdc=cfg["trade_amount_usdc"],
        min_volume_usdc=cfg["min_volume_usdc"],
        log_path=cfg.get("trade_log", "trades.jsonl"),
    )
    trader = Trader(weather, polymarket, strategy, signer)

    interval = cfg.get("scan_interval_minutes", 60) * 60
    logger.info("Bot started | address=%s | scan every %d min", signer.address, interval // 60)

    while True:
        try:
            trader.scan_and_trade()
        except Exception as exc:
            logger.error("Scan failed: %s", exc, exc_info=True)
        logger.info("Sleeping %d minutes...", interval // 60)
        time.sleep(interval)


if __name__ == "__main__":
    main()
