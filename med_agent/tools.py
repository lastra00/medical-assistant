import json
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode, quote, urlsplit

import requests

from .config import (
    MINSAL_GET_LOCALES,
    MINSAL_GET_TURNOS,
)


class HttpError(Exception):
    pass


# Encabezados tipo navegador para evitar 403 en entornos cloud
DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    "Origin": "https://midas.minsal.cl",
    "Referer": "https://midas.minsal.cl/",
    "X-Requested-With": "XMLHttpRequest",
}


def _http_get(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 25) -> Dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, timeout=timeout, headers=DEFAULT_HEADERS)
            # Retry simple ante 403/429 en el primer intento
            if resp.status_code in (403, 429) and attempt == 0:
                time.sleep(0.8)
                continue
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError:
                txt = (resp.text or "").lstrip("\ufeff\n\r ")
                return json.loads(txt)
        except Exception as e:
            last_exc = e
            time.sleep(0.2)
            continue
    raise HttpError(f"HTTP GET error: {last_exc}")


def _http_get_with_fallback(primary_url: str, alt_url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        return _http_get(primary_url, params=params)
    except HttpError:
        # Intento alternativo (farmanet)
        try:
            return _http_get(alt_url, params=params)
        except HttpError:
            # Proxys públicos como último recurso (DNS/403 en cloud)
            def _proxy_try(url: str) -> Dict[str, Any]:
                full = url
                if params:
                    qs = urlencode(params)
                    sep = '&' if ('?' in full) else '?'
                    full = f"{full}{sep}{qs}"
                # 1) allorigins
                try:
                    wrapped = f"https://api.allorigins.win/raw?url={quote(full, safe='')}"
                    return _http_get(wrapped, params=None)
                except HttpError:
                    pass
                # 2) r.jina.ai
                parts = urlsplit(full)
                pathq = parts.path + (f"?{parts.query}" if parts.query else "")
                wrapped = f"https://r.jina.ai/http://{parts.netloc}{pathq}"
                return _http_get(wrapped, params=None)

            try:
                return _proxy_try(primary_url)
            except HttpError:
                return _proxy_try(alt_url)


def tool_minsal_locales(comuna: Optional[str] = None, region: Optional[str] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if comuna:
        params["comuna_nombre"] = comuna
    if region:
        params["fk_region"] = region
    return _http_get_with_fallback(
        MINSAL_GET_LOCALES,
        "https://farmanet.minsal.cl/index.php/ws/getLocales",
        params,
    )


def tool_minsal_turnos(comuna: Optional[str] = None, region: Optional[str] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if comuna:
        params["comuna_nombre"] = comuna
    if region:
        params["fk_region"] = region
    return _http_get_with_fallback(
        MINSAL_GET_TURNOS,
        "https://farmanet.minsal.cl/index.php/ws/getLocalesTurnos",
        params,
    )


