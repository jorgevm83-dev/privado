# agent/llamadas.py — Recados del agente de voz (Retell) por correo

"""
Recibe el aviso de Retell cuando termina y se analiza una llamada, y le manda al
despacho un correo con el recado (nombre, empresa, telefono, motivo, para quien,
cita solicitada, urgencia).

Reusa el mismo envio por Resend que ya usan las solicitudes de cita de WhatsApp.

Seguridad: la URL del webhook lleva un token secreto (?token=...). Si el token no
coincide con RETELL_WEBHOOK_TOKEN, se rechaza. Asi nadie puede mandarte correos falsos
adivinando la direccion.
"""

import hmac
import logging
import os
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

from agent.tools import _enviar_correo

logger = logging.getLogger("agentkit")

router = APIRouter()

# Retell reintenta si no recibe un 2xx. Se recuerdan las ultimas llamadas ya avisadas
# para no mandar el mismo recado dos veces.
_llamadas_avisadas: deque = deque(maxlen=200)

# Retell manda varios eventos por llamada (call_started, call_ended, call_analyzed).
# Solo call_analyzed trae los campos que se definieron en "Extraccion posterior".
EVENTO_QUE_AVISA = "call_analyzed"


def _token_valido(recibido: str) -> bool:
    esperado = os.getenv("RETELL_WEBHOOK_TOKEN", "").strip()
    if not esperado:
        # Sin token configurado se rechaza todo: es mejor no avisar que dejar la puerta abierta.
        logger.error("RETELL_WEBHOOK_TOKEN no esta configurado en Railway: se rechaza el webhook de Retell")
        return False
    return hmac.compare_digest(recibido.encode(), esperado.encode())


def _dato(datos: dict, *claves: str) -> str:
    """Busca un campo por varias grafias (con o sin acento) y devuelve texto limpio."""
    for clave in claves:
        valor = datos.get(clave)
        if valor not in (None, "", []):
            return str(valor).strip()
    return ""


def _es_urgente(valor) -> bool:
    if isinstance(valor, bool):
        return valor
    return str(valor or "").strip().lower() in {"true", "si", "sí", "yes", "1"}


def _hora_monterrey(call: dict) -> str:
    ms = call.get("start_timestamp")
    try:
        momento = datetime.fromtimestamp(ms / 1000, ZoneInfo("America/Monterrey"))
    except (TypeError, ValueError, OSError):
        momento = datetime.now(ZoneInfo("America/Monterrey"))
    return momento.strftime("%d/%m/%Y %H:%M")


def construir_correo(call: dict) -> tuple[str, str]:
    """Arma (asunto, texto) del correo a partir de la llamada analizada por Retell."""
    analisis = call.get("call_analysis") or {}
    datos = analisis.get("custom_analysis_data") or {}

    nombre = _dato(datos, "nombre")
    empresa = _dato(datos, "empresa")
    telefono = _dato(datos, "telefono", "teléfono")
    motivo = _dato(datos, "motivo")
    para_quien = _dato(datos, "para_quien")
    cita = _dato(datos, "cita_solicitada")
    urgente = _es_urgente(datos.get("urgente"))
    resumen = (analisis.get("call_summary") or "").strip()
    desde = call.get("from_number") or "no disponible"
    duracion_s = round((call.get("duration_ms") or 0) / 1000)

    hay_recado = bool(nombre or motivo or telefono)
    if hay_recado:
        asunto = f"Recado telefonico: {nombre or 'sin nombre'}" + (f" ({empresa})" if empresa else "")
    else:
        asunto = "Llamada sin recado"
    if urgente:
        asunto = "URGENTE - " + asunto

    lineas = [
        "Llamada atendida por el asistente telefonico del despacho",
        f"({_hora_monterrey(call)}, hora de Monterrey; duracion {duracion_s} s)",
        "",
    ]
    if urgente:
        lineas += ["*** MARCADA COMO URGENTE ***", ""]

    if hay_recado:
        lineas += [
            f"Nombre: {nombre or 'no indicado'}",
            f"Empresa: {empresa or 'no indicada'}",
            f"Telefono de regreso: {telefono or 'no indicado'}",
            f"Numero desde el que llamo: {desde}",
            f"Busca a: {para_quien or 'no indicado'}",
            f"Motivo: {motivo or 'no indicado'}",
            f"Cita solicitada: {cita or 'no pidio'}",
        ]
    else:
        lineas += [
            "La persona colgo o no dejo recado.",
            f"Numero desde el que llamo: {desde}",
        ]

    if resumen:
        lineas += ["", "Resumen de la llamada:", resumen]

    if cita:
        lineas += ["", "Ojo: el agente NO confirmo la cita. Falta confirmarsela a la persona."]

    lineas += ["", f"Motivo de cierre de la llamada: {call.get('disconnection_reason') or 'no indicado'}"]
    return asunto, "\n".join(lineas)


@router.post("/retell-webhook")
async def retell_webhook(request: Request):
    """Retell avisa aqui de cada evento de llamada. Solo call_analyzed manda correo."""
    if not _token_valido(request.query_params.get("token", "")):
        raise HTTPException(status_code=401, detail="Token invalido")

    try:
        cuerpo = await request.json()
    except Exception:  # noqa: BLE001
        # Un payload raro no debe hacer que Retell reintente para siempre
        logger.error("Webhook de Retell con cuerpo ilegible")
        return {"status": "ignorado"}

    evento = cuerpo.get("event")
    call = cuerpo.get("call") or {}
    call_id = call.get("call_id")

    if evento != EVENTO_QUE_AVISA:
        return {"status": "ignorado", "evento": evento}

    if call_id and call_id in _llamadas_avisadas:
        logger.info(f"Llamada ya avisada, se ignora: {call_id}")
        return {"status": "repetido"}

    asunto, texto = construir_correo(call)
    ok, detalle = await _enviar_correo(asunto, texto)
    if not ok:
        # Se contesta 500 para que Retell reintente el aviso; no se marca como avisada.
        logger.error(f"No se pudo mandar el recado de la llamada {call_id}: {detalle}")
        raise HTTPException(status_code=500, detail="No se pudo enviar el correo")

    if call_id:
        _llamadas_avisadas.append(call_id)
    logger.info(f"Recado de la llamada {call_id} enviado por correo")
    return {"status": "ok"}
