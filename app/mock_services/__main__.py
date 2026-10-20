"""Arranca los servicios simulados:  python -m app.mock_services"""
from urllib.parse import urlparse

import uvicorn

from app.config import load_settings

url = urlparse(load_settings().services_base_url)
uvicorn.run("app.mock_services.main:app", host=url.hostname or "127.0.0.1", port=url.port or 8001, log_level="warning")
