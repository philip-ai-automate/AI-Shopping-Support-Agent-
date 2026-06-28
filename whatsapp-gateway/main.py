from dotenv import load_dotenv

load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from wa_db import init_wa_tables, auto_close_stale_handoffs
from meta_webhook import router as meta_router
from wa_proactive import router as proactive_router
from wa_daily_report import (
    router as daily_report_router,
    run_daily_reports,
    ensure_templates_submitted,
    poll_and_activate_templates,
)
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

# Auto-close stale handoffs: every hour
# Resolves handoffs where the customer has not messaged in 24 hours so the
# merchant inbox stays clean and AI resumes automatically.
_scheduler.add_job(
    auto_close_stale_handoffs,
    CronTrigger(minute=0, timezone="UTC"),
    id="auto_close_handoffs",
    replace_existing=True,
    misfire_grace_time=600,
)

# Template approval polling: every 2 hours
# Checks Meta API for any pending daily-report templates and activates them
# in the DB as soon as Meta approves them — no manual intervention needed.
_scheduler.add_job(
    poll_and_activate_templates,
    CronTrigger(minute=30, hour="*/2", timezone="UTC"),
    id="template_approval_poll",
    replace_existing=True,
    misfire_grace_time=3600,
)

# Template submission sweep: every 4 hours
# Submits the daily report template to any tenant that connected their
# WhatsApp Business number since the last sweep — no manual steps needed.
_scheduler.add_job(
    ensure_templates_submitted,
    CronTrigger(minute=0, hour="*/4", timezone="UTC"),
    id="template_submission_sweep",
    replace_existing=True,
    misfire_grace_time=3600,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    init_wa_tables()
    _scheduler.start()
    print("✅ [SCHEDULER] Daily reports: 07:00 UTC | Plan resets: 00:05 UTC | Auto-close handoffs: every hour | Template poll: every 2 h", flush=True)
    asyncio.create_task(ensure_templates_submitted())
    yield
    _scheduler.shutdown(wait=False)
    print("🛑 [SCHEDULER] Scheduler stopped", flush=True)


app = FastAPI(title="Phixtra WhatsApp Gateway", lifespan=lifespan)

app.include_router(meta_router)
app.include_router(proactive_router)
app.include_router(daily_report_router)
app.include_router(plan_reset_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "phixtra-whatsapp-gateway"}
