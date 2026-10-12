# agent/tools.py — Herramientas del agente
# Generado por AgentKit

"""
Herramientas especificas del negocio.

La informacion del negocio le llega al agente por el system prompt (config/prompts.yaml),
asi que para CONTESTAR preguntas no hace falta nada de aca. Este archivo es el lugar para
las ACCIONES. Hoy hay una conectada al ciclo de tool use: registrar_solicitud_cita.
(obtener_horario y buscar_en_knowledge siguen sin estar conectadas.)

Claude Code genera las funciones segun los casos de uso elegidos en la entrevista.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import yaml

logger = logging.getLogger("agentkit")

CARPETA_KNOWLEDGE = Path("knowledge")


def cargar_info_negocio() -> dict:
    """Carga la informacion del negocio desde config/business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atencion del negocio."""
    info = cargar_info_negocio()
    return {
        "horario": info.get("negocio", {}).get("horario", "No disponible"),
        "esta_abierto": True,  # TODO: calcular segun la hora actual y el horario
    }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca informacion en los archivos de /knowledge.
    Retorna los fragmentos que coinciden con la consulta.
    """
    if not CARPETA_KNOWLEDGE.is_dir():
        return "No hay archivos de conocimiento disponibles."

    resultados = []
    for ruta in sorted(CARPETA_KNOWLEDGE.iterdir()):
        if ruta.name.startswith(".") or not ruta.is_file():
            continue
        try:
            contenido = ruta.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binarios y archivos ilegibles se saltean
        if consulta.lower() in contenido.lower():
            resultados.append(f"[{ruta.name}]: {contenido[:500]}")

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontre informacion especifica sobre eso en mis archivos."


# ── Solicitud de cita: aviso por correo al contador ──────────
# Esta SI es una accion conectada al ciclo de tool use (ver brain.py): cuando el cliente
# ya dio todos sus datos, Claude llama a registrar_solicitud_cita y al contador le llega
# un correo. El envio se hace con Resend (https://resend.com), via HTTPS.

RESEND_URL = "https://api.resend.com/emails"
TIPOS_PERSONA = ["fisica", "moral", "no_indicado"]

HERRAMIENTAS = [
    {
        "name": "registrar_solicitud_cita",
        "description": (
            "Registra una solicitud de cita con el contador y le avisa por correo al despacho. "
            "Llamala UNA sola vez por solicitud, y solo cuando el cliente ya te dio nombre, tema, "
            "tipo de persona, horario preferido y modalidad. No inventes ni completes datos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre completo del cliente"},
                "tema": {"type": "string", "description": "Tema a tratar en la cita"},
                "tipo_persona": {
                    "type": "string",
                    "enum": TIPOS_PERSONA,
                    "description": "fisica, moral, o no_indicado si no lo dijo",
                },
                "horario_preferido": {"type": "string", "description": "Dia y horario que le convienen, tal como lo dijo"},
                "modalidad": {"type": "string", "description": "presencial o videollamada"},
            },
            "required": ["nombre", "tema", "tipo_persona", "horario_preferido", "modalidad"],
        },
    }
]


def _destinatario() -> str:
    return os.getenv("NOTIFY_EMAIL") or "jorge@vera-contadores.com"


async def _enviar_correo(asunto: str, texto: str) -> tuple[bool, str]:
    """Manda un correo con Resend. Devuelve (ok, detalle). Nunca lanza excepcion."""
    llave = os.getenv("RESEND_API_KEY", "").strip()
    if not llave:
        return False, "RESEND_API_KEY no configurada"

    remitente = os.getenv("NOTIFY_FROM") or "Agente Vera Contadores <onboarding@resend.dev>"
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.post(
                RESEND_URL,
                headers={"Authorization": f"Bearer {llave}"},
                json={"from": remitente, "to": [_destinatario()], "subject": asunto, "text": texto},
            )
    except Exception as e:  # noqa: BLE001
        return False, f"error de red: {e}"

    if r.status_code // 100 == 2:
        return True, "enviado"
    return False, f"Resend respondio {r.status_code}: {r.text[:300]}"


async def registrar_solicitud_cita(
    telefono: str,
    nombre: str,
    tema: str,
    tipo_persona: str,
    horario_preferido: str,
    modalidad: str,
) -> dict:
    """Avisa por correo al despacho de una solicitud de cita."""
    personas = {"fisica": "Persona fisica", "moral": "Persona moral"}
    ahora = datetime.now(ZoneInfo("America/Monterrey")).strftime("%d/%m/%Y %H:%M")
    texto = (
        "Nueva solicitud de cita recibida por WhatsApp\n"
        f"({ahora}, hora de Monterrey)\n\n"
        f"Nombre: {nombre}\n"
        f"WhatsApp del cliente: {telefono or 'no disponible'}\n"
        f"Tipo de persona: {personas.get(tipo_persona, 'No indicado')}\n"
        f"Tema: {tema}\n"
        f"Horario preferido: {horario_preferido}\n"
        f"Modalidad: {modalidad}\n\n"
        "El agente le dijo al cliente que el despacho le confirmara la cita. "
        "Falta confirmarsela por WhatsApp."
    )
    ok, detalle = await _enviar_correo(f"Solicitud de cita: {nombre}", texto)
    if ok:
        logger.info(f"Solicitud de cita enviada por correo ({nombre}, {telefono})")
        return {"ok": True}
    logger.error(f"No se pudo avisar de la solicitud de cita de {nombre} ({telefono}): {detalle}")
    return {"ok": False, "error": "No se pudo registrar la solicitud"}


async def ejecutar_herramienta(nombre: str, argumentos: dict, telefono: str) -> dict:
    """Ejecuta la herramienta que pidio Claude. Un error aqui no debe tumbar la conversacion."""
    try:
        if nombre == "registrar_solicitud_cita":
            return await registrar_solicitud_cita(
                telefono=telefono,
                nombre=str(argumentos.get("nombre", "")).strip(),
                tema=str(argumentos.get("tema", "")).strip(),
                tipo_persona=str(argumentos.get("tipo_persona", "no_indicado")),
                horario_preferido=str(argumentos.get("horario_preferido", "")).strip(),
                modalidad=str(argumentos.get("modalidad", "")).strip(),
            )
        return {"ok": False, "error": f"herramienta desconocida: {nombre}"}
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Error ejecutando la herramienta {nombre}: {e}")
        return {"ok": False, "error": "error interno"}


# ════════════════════════════════════════════════════════════
# Claude Code: agrega aqui las funciones especificas segun
# el caso de uso elegido por el usuario. Ejemplos:
#
# Si FAQ -> buscar_en_knowledge() ya esta listo arriba
#
# Si AGENDAR CITAS:
# def obtener_slots_disponibles(fecha: str) -> list[dict]: ...
# def reservar_cita(telefono, fecha, hora, servicio): ...
# def cancelar_cita(telefono, cita_id): ...
#
# Si TOMAR PEDIDOS:
# def agregar_al_carrito(telefono, producto, cantidad): ...
# def ver_carrito(telefono) -> list[dict]: ...
# def confirmar_pedido(telefono) -> dict: ...
#
# Si VENTAS / LEADS:
# def registrar_lead(telefono, nombre, interes): ...
# def calificar_lead(telefono) -> str: ...
# def escalar_a_vendedor(telefono, contexto): ...
#
# Si SOPORTE:
# def crear_ticket(telefono, problema) -> str: ...
# def consultar_ticket(ticket_id) -> dict: ...
# def escalar_ticket(ticket_id, razon): ...
# ════════════════════════════════════════════════════════════
