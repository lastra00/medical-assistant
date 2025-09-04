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

# Algunos entornos (p. ej., cloud) requieren encabezados tipo navegador para evitar 403
DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    "Origin": "https://midas.minsal.cl",
    "Referer": "https://midas.minsal.cl/",
}


def _http_get(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 25) -> Dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, timeout=timeout, headers=DEFAULT_HEADERS)
            # Retry simple ante 403 en el primer intento
            if resp.status_code == 403 and attempt == 0:
                time.sleep(0.8)
                continue
            resp.raise_for_status()
            # Algunos endpoints MINSAL devuelven JSON con BOM o text/plain
            try:
                return resp.json()
            except ValueError:
                txt = resp.text.lstrip("\ufeff\n\r ")
                return json.loads(txt)
        except Exception as e:
            last_exc = e
            time.sleep(0.2)
            continue
    raise HttpError(f"HTTP GET error: {last_exc}")


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


