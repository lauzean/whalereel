"""WebSocket 进度推送总线。"""

import asyncio
import json
from typing import Any


class WsBus:
    """按项目订阅的进度广播。单进程内存实现，够用。"""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, project_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.setdefault(project_id, set()).add(q)
        return q

    def unsubscribe(self, project_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(project_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(project_id, None)

    async def publish(self, project_id: str, event: str, data: dict[str, Any]) -> None:
        message = json.dumps({"event": event, "data": data}, ensure_ascii=False)
        for q in list(self._subs.get(project_id, ())):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass  # 慢消费者丢消息，前端以 REST 兜底


bus = WsBus()
