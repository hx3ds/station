import asyncio
import time

class TokenState:
    def __init__(self, token: str):
        self.token = (token or "").strip()
        self.previous_token: str | None = None
        self.previous_token_expires_at: float | None = None
        self.lock = asyncio.Lock()

    def is_authorized(self, header_token: str) -> bool:
        header_token = (header_token or "").strip()
        if self.token and header_token == self.token:
            return True
        if not header_token:
            return False
        if not self.previous_token:
            return False
        expires_at = self.previous_token_expires_at
        if expires_at is None:
            return False
        if time.time() >= expires_at:
            return False
        return header_token == self.previous_token

    async def rotate_token(self, token: str, *, grace_seconds: int = 0) -> None:
        token = (token or "").strip()
        if not token:
            raise ValueError("token must be a non-empty string")
        if grace_seconds < 0:
            raise ValueError("grace_seconds must be >= 0")
        async with self.lock:
            old = self.token
            self.token = token
            if old and grace_seconds > 0 and old != token:
                self.previous_token = old
                self.previous_token_expires_at = time.time() + grace_seconds
            else:
                self.previous_token = None
                self.previous_token_expires_at = None
