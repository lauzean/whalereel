"""运镜库 API：33 种预设运镜（提示词模板）。"""

import json
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api/camera-moves", tags=["camera"])

_MOVES_FILE = Path(__file__).resolve().parent.parent / "data" / "camera_moves.json"


@router.get("")
def list_camera_moves():
    try:
        data = json.loads(_MOVES_FILE.read_text(encoding="utf-8"))
        return data.get("moves", [])
    except (OSError, json.JSONDecodeError):
        return []
