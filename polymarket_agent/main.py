from __future__ import annotations

import time
from datetime import datetime, timezone

from .agents import AgentSystem
from .db import Database
from .settings import Settings


def run() -> None:
    settings = Settings()
    db = Database(settings.database_path)
    db.ensure_portfolio_seed(settings.starting_capital_usdc)
    system = AgentSystem(db, settings)
    system.run_wallet_curator()

    print("Polymarket multi-agent system started.")
    print(f"Dry-run mode: {settings.dry_run}")
    print(f"Paper cash tracking: {settings.track_paper_cash} (starting {settings.starting_capital_usdc} USDC)")

    last_scout_hour = None
    last_monitor_hour = None
    last_report_block = None
    last_curator = datetime.now(timezone.utc)

    while True:
        now = datetime.now(timezone.utc)

        if (now - last_curator).total_seconds() >= settings.wallet_curator_interval_hours * 3600:
            system.run_wallet_curator()
            last_curator = now

        # Stündlich
        if last_scout_hour != (now.date(), now.hour):
            system.run_scout()
            system.run_analyst()
            system.run_devils_advocate()
            system.run_risk_officer()
            last_scout_hour = (now.date(), now.hour)

        # Alle 2h
        monitor_key = (now.date(), now.hour // 2)
        if last_monitor_hour != monitor_key:
            system.run_position_monitor()
            last_monitor_hour = monitor_key

        # Alle 6h
        report_key = (now.date(), now.hour // 6)
        if last_report_block != report_key:
            system.run_portfolio_manager()
            last_report_block = report_key

        time.sleep(30)


if __name__ == "__main__":
    run()
