from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from wa_db import init_wa_tables
from meta_webhook import router as meta_router
from wa_proactive import router as proactive_router

app = FastAPI(title="Phixtra WhatsApp Gateway")

app.include_router(meta_router)
app.include_router(proactive_router)

init_wa_tables()


@app.get("/health")
def health():
    return {"status": "ok", "service": "phixtra-whatsapp-gateway"}
