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

# ── Scheduler: 07:00 UTC daily = 08:00 WAT ───────────────────────────────────
_scheduler = AsyncIOScheduler()
_scheduler.add_job(
    run_daily_reports,
    CronTrigger(hour=7, minute=0, timezone="UTC"),
    id="daily_reports",
    replace_existing=True,
    misfire_grace_time=3600,   # run even if up to 1 hour late
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_wa_tables()
    _scheduler.start()
    print("✅ [SCHEDULER] Daily reports scheduled at 07:00 UTC")
    yield
    _scheduler.shutdown(wait=False)
    print("🛑 [SCHEDULER] Scheduler stopped")


app = FastAPI(title="Phixtra WhatsApp Gateway", lifespan=lifespan)

app.include_router(meta_router)
app.include_router(proactive_router)
app.include_router(daily_report_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "phixtra-whatsapp-gateway"}
