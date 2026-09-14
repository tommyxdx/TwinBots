from __future__ import annotations
import logging
import os
import time

LOG = logging.getLogger(__name__)


class Telegram:
    def __init__(self, cfg, store, http):
        self.cfg, self.store, self.http = cfg["telegram"], store, http
        # A crash during HTTP delivery has an unknown outcome; don't duplicate it.
        with store.transaction() as db:
            db.execute("UPDATE outbox SET status='unknown' WHERE status='sending'")

    def enqueue(self, dedupe, message):
        if not self.cfg["enabled"]:
            return
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO outbox(dedupe,ts,message) VALUES(?,?,?)",
                       (dedupe, time.time(), message[:4000]))

    def flush(self):
        if not self.cfg["enabled"]:
            return
        token, chat = os.getenv(self.cfg["token_env"]), os.getenv(self.cfg["chat_id_env"])
        if not token or not chat:
            LOG.warning("Telegram enabled but token/chat ID are missing; queued locally")
            return
        for row in self.store.rows("SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 5"):
            with self.store.transaction() as db:
                db.execute("UPDATE outbox SET status='sending',attempts=attempts+1 WHERE id=?", (row["id"],))
            try:
                result = self.http.json(f"https://api.telegram.org/bot{token}/sendMessage",
                                        payload={"chat_id": chat, "text": row["message"],
                                                 "link_preview_options": {"is_disabled": True}})
                status = "sent" if result.get("ok") else "rejected"
            except Exception:
                status = "unknown"
                LOG.warning("Telegram delivery uncertain; inspect outbox/report, no automatic resend")
            with self.store.transaction() as db:
                db.execute("UPDATE outbox SET status=? WHERE id=?", (status, row["id"]))
