from dotenv import load_dotenv

load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from wa_db import init_wa_tables, auto_close_stale_handoffs
from meta_webhook import router as meta_router
from wa_proactive import router as proactive_router
# Daily report scrapped 2026-11-06 (user): businesses build their own reports in Reports.
from wa_plan_reset import router as plan_reset_router, run_plan_resets

from push_notify import router as push_router
from keyland_alerts_gw import router as keyland_alerts_router
from staff_alerts import (run_alert_reminders, init_staff_alert_tables,
                          process_web_chat_alerts, email_unseen_web_replies)
from apscheduler.triggers.interval import IntervalTrigger

# ── Scheduler ─────────────────────────────────────────────────────────────────
_scheduler = AsyncIOScheduler()

# Demo bookings (2026-10-22): booked/moved/cancelled, 30-min reminder, "how did it go?",
# 8am digest and Monday summary — checked every minute (demo_alerts.py)
from demo_alerts import run_demo_alerts
_scheduler.add_job(
    run_demo_alerts,
    CronTrigger(minute="*", timezone="UTC"),
    id="demo_alerts",
    replace_existing=True,
    misfire_grace_time=120,
    max_instances=1,
)

# Follow-up reminders (2026-10-23): 8am on working days, each business's own time
# zone: salespeople get today's + overdue, owner + line manager get overdue (followup_alerts.py)
from followup_alerts import run_followup_alerts
_scheduler.add_job(
    run_followup_alerts,
    CronTrigger(minute="*/5", timezone="UTC"),
    id="followup_alerts",
    replace_existing=True,
    misfire_grace_time=240,
    max_instances=1,
)

# Staff chat alerts: one reminder 10 min after an unanswered alert — checked every minute
_scheduler.add_job(
    run_alert_reminders,
    CronTrigger(minute="*", timezone="UTC"),
    id="staff_alert_reminders",
    replace_existing=True,
    misfire_grace_time=120,
    max_instances=1,
)

# WhatsApp calls (2026-10-20): missed calls past their call-back deadline
# alert the managers — checked every minute
from wa_calls import run_call_deadlines
_scheduler.add_job(
    run_call_deadlines,
    CronTrigger(minute="*", timezone="UTC"),
    id="call_back_deadlines",
    replace_existing=True,
    misfire_grace_time=120,
    max_instances=1,
)

# Website chats (2026-10-06): staff alerts within seconds, and replies the
# visitor never saw in the chat box go by email after 2 minutes
_scheduler.add_job(
    process_web_chat_alerts,
    IntervalTrigger(seconds=8),
    id="web_chat_alerts",
    replace_existing=True,
    misfire_grace_time=30,
    max_instances=1,
)
_scheduler.add_job(
    email_unseen_web_replies,
    CronTrigger(minute="*", timezone="UTC"),
    id="web_chat_unseen_replies",
    replace_existing=True,
    misfire_grace_time=120,
    max_instances=1,
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




@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    init_wa_tables()
    init_staff_alert_tables()
    __import__("data_crypto").ready()   # loud error at start if the data key is missing
    _scheduler.start()
    print("✅ [SCHEDULER] Plan resets: 00:05 UTC | Auto-close handoffs: every hour", flush=True)
    yield
    _scheduler.shutdown(wait=False)
    print("🛑 [SCHEDULER] Scheduler stopped", flush=True)


app = FastAPI(title="Phixtra WhatsApp Gateway", lifespan=lifespan)

app.include_router(meta_router)
app.include_router(proactive_router)
app.include_router(plan_reset_router)
app.include_router(push_router)   # phone notifications test (2026-10-11)
app.include_router(keyland_alerts_router)   # KeyLand staff alerts: WhatsApp + phone notifications (2026-10-22)


@app.get("/health")
def health():
    return {"status": "ok", "service": "phixtra-whatsapp-gateway"}
