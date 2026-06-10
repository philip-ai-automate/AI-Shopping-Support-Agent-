from dotenv import load_dotenv

load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from wa_db import init_wa_tables
from meta_webhook import router as meta_router
from wa_proactive import router as proactive_router
from wa_daily_report import router as daily_report_router, run_daily_reports
from wa_plan_reset import router as plan_reset_router, run_plan_resets

# ── Scheduler ─────────────────────────────────────────────────────────────────
_scheduler = AsyncIOScheduler()

# Daily reports: 07:00 UTC = 08:00 WAT
_scheduler.add_job(
    run_daily_reports,
    CronTrigger(hour=7, minute=0, timezone="UTC"),
    id="daily_reports",
    replace_existing=True,
    misfire_grace_time=3600,
)

# Billing period reset: 00:05 UTC daily (just after midnight)
_scheduler.add_job(
    run_plan_resets,
    CronTrigger(hour=0, minute=5, timezone="UTC"),
    id="plan_period_reset",
    replace_existing=True,
    misfire_grace_time=3600,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_wa_tables()
    _scheduler.start()
    print("✅ [SCHEDULER] Daily reports: 07:00 UTC | Plan resets: 00:05 UTC")
    yield
    _scheduler.shutdown(wait=False)
    print("🛑 [SCHEDULER] Scheduler stopped")


app = FastAPI(title="Phixtra WhatsApp Gateway", lifespan=lifespan)

app.include_router(meta_router)
app.include_router(proactive_router)
app.include_router(daily_report_router)
app.include_router(plan_reset_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "phixtra-whatsapp-gateway"}
