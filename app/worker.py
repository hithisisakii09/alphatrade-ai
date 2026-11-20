"""Background autonomous market scheduler and tick worker daemon for AlphaTrade AI."""

import logging
import signal
import sys
import time

from app.core.config import load_app_config
from app.core.execution_guard import assert_safe_execution_mode
from app.services.daily_runner import ScheduledDailyRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("AlphaTradeWorker")


def run_worker():
  """Starts the autonomous trading engine runner in daemon mode."""
  assert_safe_execution_mode()
  cfg = load_app_config()
  logger.info("Initializing AlphaTrade AI background autonomous worker...")

  runner = ScheduledDailyRunner(config=cfg, auto_start=True)

  def shutdown(sig, frame):
    logger.info("Termination signal received. Shutting down worker gracefully...")
    runner.stop()
    sys.exit(0)

  signal.signal(signal.SIGINT, shutdown)
  signal.signal(signal.SIGTERM, shutdown)

  logger.info("Worker active. Entering autonomous scheduling loop.")
  try:
    while True:
      time.sleep(1)
  except (KeyboardInterrupt, SystemExit):
    runner.stop()


if __name__ == "__main__":
  run_worker()
