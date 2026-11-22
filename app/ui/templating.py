from __future__ import annotations

import json
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import get_settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["cfg"] = get_settings
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else {}
templates.env.filters["dt"] = lambda d: d.strftime("%d %b %Y %H:%M") if d else ""
