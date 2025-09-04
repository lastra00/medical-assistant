import json
import time
from typing import Any, Dict, Optional

import requests

from .config import (
    MINSAL_GET_LOCALES,
    MINSAL_GET_TURNOS,
)


class HttpError(Exception):
    pass


def _http_get(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 20) -> Dict[str, Any]:
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        # Algunos endpoints MINSAL devuelven texto JSON
        return resp.json()
    except Exception as e:
        raise HttpError(f"HTTP GET error: {e}")


def tool_minsal_locales(comuna: Optional[str] = None, region: Optional[str] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if comuna:
        params["comuna_nombre"] = comuna
    if region:
        params["fk_region"] = region
    return _http_get(MINSAL_GET_LOCALES, params)


def tool_minsal_turnos(comuna: Optional[str] = None, region: Optional[str] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if comuna:
        params["comuna_nombre"] = comuna
    if region:
        params["fk_region"] = region
    return _http_get(MINSAL_GET_TURNOS, params)


