# services/telnyx_client.py
from __future__ import annotations
import httpx
from typing import Any, Dict

async def create_call_raw(call_payload: Dict[str, Any], base_url: str, headers: Dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.post(f"{base_url}/calls", json=call_payload, headers=headers)

async def send_dtmf(call_control_id: str, digits: str, base_url: str, headers: Dict[str, str]) -> None:
    url = f"{base_url}/calls/{call_control_id}/actions/send_dtmf"
    payload = {"digits": digits}
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload, headers=headers)

async def hangup(call_control_id: str, base_url: str, headers: Dict[str, str]) -> None:
    url = f"{base_url}/calls/{call_control_id}/actions/hangup"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={}, headers=headers)
