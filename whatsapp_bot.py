"""WhatsApp Business Cloud API auto-reply bot.

Run: uvicorn whatsapp_bot:app --host 0.0.0.0 --port 8000
Manual handoff release: python whatsapp_bot.py resume 919876543210
"""

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import sys
from contextlib import contextmanager

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("whatsapp-bot")

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
APP_SECRET = os.getenv("META_APP_SECRET", "")
ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.getenv("GRAPH_API_VERSION", "v25.0")
DB_PATH = os.getenv("BOT_DB_PATH", "whatsapp_bot.sqlite3")
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "our business")

app = FastAPI(title="WhatsApp Business Automation")


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS seen_messages (id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE IF NOT EXISTS staff_handoffs (phone TEXT PRIMARY KEY)")
        connection.commit()
        yield connection
        connection.commit()
    finally:
        connection.close()


with db():
    pass


def validate_signature(body: bytes, supplied: str) -> bool:
    if not APP_SECRET:
        log.error("META_APP_SECRET is missing; webhook rejected")
        return False
    expected = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied)


def send_message(to: str, text: str) -> None:
    if not ACCESS_TOKEN or not PHONE_NUMBER_ID:
        raise RuntimeError("Missing WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID")
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},
        json={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        },
        timeout=15,
    )
    response.raise_for_status()
    log.info("Replied to customer ending in %s", to[-4:])


def is_staff_handoff(phone: str) -> bool:
    with db() as connection:
        return connection.execute("SELECT 1 FROM staff_handoffs WHERE phone=?", (phone,)).fetchone() is not None


def pause_bot(phone: str) -> None:
    with db() as connection:
        connection.execute("INSERT OR IGNORE INTO staff_handoffs(phone) VALUES (?)", (phone,))


def resume_bot(phone: str) -> None:
    with db() as connection:
        connection.execute("DELETE FROM staff_handoffs WHERE phone=?", (phone,))


def already_processed(message_id: str) -> bool:
    with db() as connection:
        return connection.execute("SELECT 1 FROM seen_messages WHERE id=?", (message_id,)).fetchone() is not None


def mark_processed(message_id: str) -> None:
    with db() as connection:
        connection.execute("INSERT OR IGNORE INTO seen_messages(id) VALUES (?)", (message_id,))


def get_reply(text: str) -> tuple[str, bool]:
    t = text.lower().strip()
    if t in {"3", "human", "agent", "staff", "talk to staff", "speak to staff"}:
        return ("I'll leave this conversation for our team to handle. "
                "Automated replies are now paused for this conversation.", True)
    if t in {"hi", "hello", "hey", "start", "menu", "help"}:
        return (f"Hello! Welcome to {BUSINESS_NAME}. 👋\n\n"
                "How can we help you today?\n"
                "1️⃣ Business hours\n"
                "2️⃣ Services and appointments\n"
                "3️⃣ Speak to our team\n\n"
                "Reply with 1, 2, or 3.", False)
    if t in {"1", "hours", "timings", "opening hours"}:
        return ("Please share which branch or location you're asking about. "
                "Our team will confirm the current timings. Reply 3 for assistance.", False)
    if t in {"2", "service", "services", "appointment", "book", "booking"}:
        return ("For information about services or to request an appointment, "
                "please reply with the service you're interested in. "
                "Reply 3 to speak directly to our team.", False)
    return ("Thanks for your message! Reply *menu* for the options "
            "or *3* to speak with our team.", False)


@app.get("/")
def health():
    return {"status": "running", "service": "WhatsApp bot"}


@app.get("/webhook", response_class=PlainTextResponse)
def verify_webhook(
    mode: str = Query(default="", alias="hub.mode"),
    verify_token: str = Query(default="", alias="hub.verify_token"),
    challenge: str = Query(default="", alias="hub.challenge"),
):
    if mode == "subscribe" and VERIFY_TOKEN and hmac.compare_digest(verify_token, VERIFY_TOKEN):
        return challenge
    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request):
    raw = await request.body()
    if not validate_signature(raw, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if payload.get("object") != "whatsapp_business_account":
        return {"status": "ignored"}

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value", {})
            if value.get("metadata", {}).get("phone_number_id") != PHONE_NUMBER_ID:
                continue
            # Normal inbound messages only: no status updates, history, or app-sent echoes.
            for message in value.get("messages", []):
                if message.get("type") != "text":
                    continue
                if message.get("is_echo") or message.get("from_me"):
                    continue
                message_id = message.get("id")
                sender = message.get("from")
                text = message.get("text", {}).get("body", "")
                if not message_id or not sender or already_processed(message_id):
                    continue
                if is_staff_handoff(sender):
                    mark_processed(message_id)
                    continue
                reply, handoff = get_reply(text)
                # A reply to an inbound customer message is within the service window.
                try:
                    if handoff:
                        pause_bot(sender)  # avoid bot/staff racing on this conversation
                    send_message(sender, reply)
                    mark_processed(message_id)
                except requests.RequestException:
                    log.exception("Cloud API send failed")
                    # Webhook retry may redeliver this event; message IDs prevent
                    # duplicate replies once a message has been successfully handled.
                    raise HTTPException(status_code=503, detail="Message send temporarily failed")
    return {"status": "ok"}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in {"resume", "pause"}:
        phone = sys.argv[2].lstrip("+")
        if not phone.isdigit():
            raise SystemExit("Phone must contain digits only, including country code")
        (resume_bot if sys.argv[1] == "resume" else pause_bot)(phone)
        print(f"Bot {sys.argv[1]} updated for number ending in {phone[-4:]}")
    else:
        print("Run server: uvicorn whatsapp_bot:app --host 0.0.0.0 --port 8000")
        print("Staff controls: python whatsapp_bot.py pause|resume 919876543210")
