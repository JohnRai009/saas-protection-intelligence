"""Optional separate scheduler: keep this process running for unattended refresh.

Start with: python collection_scheduler.py
Live calls require both Settings > Automatic refresh and DATTO_ENABLE_LIVE_COLLECTION=1.
"""
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from refresh_manager import get_settings, is_due, live_enabled, run_refresh
from storage_manager import DOMAINS_FILE
import json

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("datto_scheduler")


def domain_snapshot():
    try:
        with DOMAINS_FILE.open(encoding="utf-8") as handle:
            return json.load(handle).get("last_updated")
    except (OSError, ValueError, TypeError):
        return None


def scheduled_check():
    if not get_settings()["automatic_refresh"] or not live_enabled():
        log.info("Automatic collection disabled; no API request made")
        return
    if not is_due(domain_snapshot=domain_snapshot()):
        return
    result = run_refresh("scheduled")
    log.info("Scheduled refresh result: %s", result.get("status"))


def main():
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(scheduled_check, "interval", minutes=5, id="datto_refresh_due_check",
                      max_instances=1, coalesce=True, misfire_grace_time=120, replace_existing=True)
    log.info("Checking every five minutes for the eight-hour refresh due time")
    log.info("This process must remain running for unattended refreshes")
    scheduled_check()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped")


if __name__ == "__main__":
    main()
