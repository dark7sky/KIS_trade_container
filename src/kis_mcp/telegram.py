import asyncio

import httpx


class Notifications:
    def __init__(self, settings, store, client=None):
        self.settings, self.store = settings, store
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def flush(self):
        for row in self.store.pending_messages():
            retry_after = 0
            try:
                response = await self.client.post(
                    "https://api.telegram.org/bot" + self.settings.telegram_token + "/sendMessage",
                    json={
                        "chat_id": self.settings.telegram_chat,
                        "text": row["message"],
                        "protect_content": True,
                    },
                )
                body = response.json()
                retry_after = min(
                    86400, max(0, int(body.get("parameters", {}).get("retry_after", 0)))
                )
                if response.status_code != 200 or body.get("ok") is not True:
                    raise ValueError
                self.store.delivered(row["id"])
            except Exception:
                self.store.defer(row["id"], row["attempts"], retry_after)

    async def run(self):
        while True:
            try:
                await self.flush()
            except Exception:
                pass  # No external exception text: Telegram URLs contain the secret.
            await asyncio.sleep(2)

    async def close(self):
        await self.client.aclose()
