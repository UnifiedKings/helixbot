from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import aiohttp


class HelixError(RuntimeError):
    pass


@dataclass(frozen=True)
class JoinedLobby:
    lobby_id: str
    lobby_code: str
    guest_token: str
    member_id: str
    lobby_name: str


class HelixClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise RuntimeError("HelixClient.start() must be called first")
        return self._session

    async def join_lobby(self, code: str, nickname: str, password: str | None = None) -> JoinedLobby:
        payload: dict[str, Any] = {
            "invite_code": code.strip().upper(),
            "nickname": nickname.strip(),
        }
        if password:
            payload["password"] = password

        data = await self._request_json("POST", "/api/lobbies/join", json=payload)
        lobby = data.get("lobby") or {}
        member = data.get("member") or {}
        guest_token = str(data.get("guest_token") or "")
        lobby_id = str(lobby.get("id") or "")
        member_id = str(member.get("id") or "")
        if not guest_token or not lobby_id or not member_id:
            raise HelixError("Helix returned an incomplete lobby join response")

        return JoinedLobby(
            lobby_id=lobby_id,
            lobby_code=code.strip().upper(),
            guest_token=guest_token,
            member_id=member_id,
            lobby_name=str(lobby.get("name") or "Shared Lobby"),
        )

    async def get_lobby_state(self, lobby_id: str, guest_token: str) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            f"/api/lobbies/{quote(lobby_id, safe='')}/state",
            token=guest_token,
        )

    async def leave_lobby(self, lobby_id: str, guest_token: str) -> None:
        await self._request_json(
            "POST",
            f"/api/lobbies/{quote(lobby_id, safe='')}/leave",
            token=guest_token,
        )

    async def report_lobby_ended(
        self,
        lobby_id: str,
        guest_token: str,
        item_id: str,
    ) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            f"/api/lobbies/{quote(lobby_id, safe='')}/ended",
            token=guest_token,
            json={"item_id": item_id},
        )

    def lobby_websocket(
        self,
        lobby_id: str,
        guest_token: str,
    ) -> aiohttp.client._WSRequestContextManager:
        return self.session.ws_connect(
            self.websocket_url(lobby_id, guest_token),
            heartbeat=20,
            autoping=True,
            autoclose=True,
        )

    def stream_url(self, lobby_id: str, item_id: str) -> str:
        return (
            f"{self.base_url}/api/lobbies/{quote(lobby_id, safe='')}/stream/"
            f"{quote(item_id, safe='')}"
        )

    def websocket_url(self, lobby_id: str, guest_token: str) -> str:
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        base_path = parsed.path.rstrip("/")
        path = f"{base_path}/ws/lobbies/{quote(lobby_id, safe='')}"
        return urlunparse((scheme, parsed.netloc, path, "", f"token={quote(guest_token, safe='')}", ""))

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {}
        if token:
            headers["x-helix-lobby-token"] = token

        try:
            async with self.session.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=json,
            ) as response:
                payload: Any = None
                try:
                    payload = await response.json(content_type=None)
                except Exception:
                    payload = None

                if response.status >= 400:
                    detail = payload.get("detail") if isinstance(payload, dict) else None
                    raise HelixError(str(detail or f"Helix returned HTTP {response.status}"))

                if not isinstance(payload, dict):
                    raise HelixError("Helix returned an unexpected response")
                return payload
        except aiohttp.ClientError as exc:
            raise HelixError(f"Could not reach Helix: {exc}") from exc
