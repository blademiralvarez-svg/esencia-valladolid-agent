# agent/brain.py — Cerebro del agente: conexion con Claude
# Generado por AgentKit

"""
Logica de IA del agente. Lee el system prompt de config/prompts.yaml y genera las
respuestas con la API de Anthropic.
"""

import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from agent.cloudbeds import consultar_disponibilidad, generar_link_reserva

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Herramientas que Ana puede llamar de verdad durante la conversacion. A diferencia de
# agent/tools.py (que son funciones sueltas sin conectar), estas SI se pasan a la API
# de Claude y SI se ejecutan cuando el modelo decide usarlas.

# El modelo no tiene forma de saber que dia es "hoy": sin este dato, una fecha
# relativa como "el 10 de septiembre" se presta a que adivine mal el anio (paso de
# verdad: penso 2025 en vez de 2026 y la consulta a Cloudbeds salio con fechas ya
# pasadas). Yucatan no tiene horario de verano, asi que la zona es fija todo el anio.
ZONA_HORARIA_HOTEL = ZoneInfo("America/Merida")

TOOLS = [
    {
        "name": "consultar_disponibilidad_cloudbeds",
        "description": (
            "Consulta disponibilidad y tarifa real de habitaciones en Esencia Valladolid "
            "Hotel para un rango de fechas, contra el PMS (Cloudbeds). Usala siempre que "
            "el huesped pregunte por disponibilidad o precio de una habitacion con fechas "
            "concretas, en vez de decir que no tenes esa informacion."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_entrada": {
                    "type": "string",
                    "description": "Fecha de check-in, formato YYYY-MM-DD",
                },
                "fecha_salida": {
                    "type": "string",
                    "description": "Fecha de check-out, formato YYYY-MM-DD",
                },
                "adultos": {"type": "integer", "description": "Numero de adultos"},
                "ninos": {"type": "integer", "description": "Numero de ninos (0 si no hay)"},
            },
            "required": ["fecha_entrada", "fecha_salida", "adultos"],
        },
    },
    {
        "name": "generar_link_reserva_cloudbeds",
        "description": (
            "Genera el link del motor de reservas de Cloudbeds con las fechas y huespedes "
            "ya cargados, para que el huesped complete la reserva y el pago el mismo. "
            "Usala despues de confirmar disponibilidad, cuando el huesped quiera reservar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_entrada": {
                    "type": "string",
                    "description": "Fecha de check-in, formato YYYY-MM-DD",
                },
                "fecha_salida": {
                    "type": "string",
                    "description": "Fecha de check-out, formato YYYY-MM-DD",
                },
                "adultos": {"type": "integer", "description": "Numero de adultos"},
                "ninos": {"type": "integer", "description": "Numero de ninos (0 si no hay)"},
            },
            "required": ["fecha_entrada", "fecha_salida", "adultos"],
        },
    },
]

# Un mensaje con varias preguntas no deberia disparar una cadena larga de llamadas.
# Esto es un techo de seguridad, no un comportamiento esperado.
MAX_ITERACIONES_HERRAMIENTAS = 4


async def _ejecutar_herramienta(nombre: str, entrada: dict) -> str:
    """Corre la herramienta que Claude pidio y devuelve el resultado como JSON."""
    try:
        if nombre == "consultar_disponibilidad_cloudbeds":
            resultado = await consultar_disponibilidad(
                fecha_entrada=entrada.get("fecha_entrada", ""),
                fecha_salida=entrada.get("fecha_salida", ""),
                adultos=int(entrada.get("adultos") or 1),
                ninos=int(entrada.get("ninos") or 0),
            )
            return json.dumps(resultado, ensure_ascii=False)

        if nombre == "generar_link_reserva_cloudbeds":
            link = generar_link_reserva(
                fecha_entrada=entrada.get("fecha_entrada", ""),
                fecha_salida=entrada.get("fecha_salida", ""),
                adultos=int(entrada.get("adultos") or 1),
                ninos=int(entrada.get("ninos") or 0),
            )
            return json.dumps({"link": link}, ensure_ascii=False)

        return json.dumps({"error": f"Herramienta desconocida: {nombre}"}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — una herramienta rota no debe tirar la conversacion
        logger.error(f"Error ejecutando la herramienta {nombre}: {e}")
        return json.dumps({"error": "La herramienta fallo al ejecutarse"}, ensure_ascii=False)

# El modelo se cambia desde .env, sin tocar el codigo.
#   claude-opus-5     el mas capaz             $5 / $25 por millon de tokens
#   claude-sonnet-5   el balanceado (default)  $3 / $15
#   claude-haiku-4-5  el mas barato y rapido   $1 / $5
# El "or" y no el default de os.getenv: una variable declarada vacia en el .env
# devuelve "" y dejaria al agente sin modelo.
MODELO = os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5"

# Es un bot de respuestas cortas: con esfuerzo bajo contesta mas rapido y mas barato.
# Dejalo vacio en el .env para no mandar el parametro.
ESFUERZO = os.getenv("ANTHROPIC_EFFORT", "low").strip()

# WhatsApp son mensajes cortos, pero este tope NO es solo la respuesta: en los modelos
# actuales el razonamiento interno tambien cuenta contra el. Con el margen justo, una
# pregunta que exija pensar un poco deja al agente sin espacio para contestar.
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS") or "4096")

# Los modelos mas viejos no aceptan output_config. Si la primera llamada falla por eso,
# se reintenta sin el parametro y se recuerda para las siguientes.
_soporta_esfuerzo = True


def cargar_config_prompts() -> dict:
    """Lee toda la configuracion desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


DIAS_ES = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
MESES_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def cargar_system_prompt(es_primer_mensaje: bool = False) -> str:
    """
    El system prompt: quien es el agente, que sabe del negocio, y que fecha es hoy.

    La fecha se calcula en cada llamada (no se guarda en prompts.yaml) para que
    nunca quede vieja, y es lo que le permite al modelo resolver fechas relativas
    ("el proximo fin de semana", "el 10 de septiembre") sin adivinar el anio.
    """
    base = cargar_config_prompts().get(
        "system_prompt", "Eres un asistente util. Responde siempre en espanol."
    )
    ahora = datetime.now(ZONA_HORARIA_HOTEL)
    fecha_legible = f"{DIAS_ES[ahora.weekday()]} {ahora.day} de {MESES_ES[ahora.month - 1]} de {ahora.year}"
    contexto_fecha = (
        f"\n\n## Fecha y hora actual\n"
        f"Hoy es {fecha_legible}, {ahora.strftime('%H:%M')} hora de Valladolid, Yucatan "
        f"(UTC-6). Usa esta fecha como referencia real para calcular cualquier fecha "
        f"relativa que mencione el huesped (\"manana\", \"el proximo fin de semana\", "
        f"\"el 10 de septiembre\") antes de llamar a cualquier herramienta. Las fechas "
        f"que le pases a las herramientas van en formato YYYY-MM-DD."
    )

    if not es_primer_mensaje:
        return base + contexto_fecha

    contexto_primer_mensaje = (
        f"\n\n## Este es el primer mensaje de la conversacion\n"
        f"Antes de responder lo que te haya preguntado, aclara brevemente y de forma "
        f"natural (no como un aviso legal ni una lista) que eres una asistente virtual "
        f"con inteligencia artificial, y que puedes hablar en el idioma que el huesped "
        f"prefiera. Hazlo en una frase corta, en el mismo idioma en el que te escribio, "
        f"y sigue con la respuesta a lo que haya preguntado en el mismo mensaje."
    )
    return base + contexto_fecha + contexto_primer_mensaje


def obtener_mensaje_error() -> str:
    """Que decirle al cliente cuando algo falla de nuestro lado."""
    return cargar_config_prompts().get(
        "error_message",
        "Lo siento, estoy teniendo problemas tecnicos. Por favor intenta de nuevo en unos minutos.",
    )


def obtener_mensaje_fallback() -> str:
    """Que decirle al cliente cuando no se entendio el mensaje."""
    return cargar_config_prompts().get(
        "fallback_message", "Disculpa, no entendi tu mensaje. Podrias reformularlo?"
    )


def _extraer_texto(respuesta) -> str:
    """
    Junta el texto de la respuesta de Claude.

    Ojo: NO se puede hacer respuesta.content[0].text. La respuesta es una lista de
    bloques y el primero no siempre es texto (los modelos que razonan devuelven
    primero un bloque de pensamiento). Hay que filtrar por tipo.
    """
    partes = [bloque.text for bloque in respuesta.content if bloque.type == "text"]
    return "\n".join(p for p in partes if p).strip()


def _es_error_de_esfuerzo(error: Exception) -> bool:
    """
    True solo si el modelo rechazo la llamada POR el parametro output_config/effort.

    Se exige que sea un 400 de peticion invalida y no cualquier error que mencione la
    palabra: un 529 de sobrecarga que la nombre de paso no debe apagar el parametro
    para todo el proceso.
    """
    if getattr(error, "status_code", None) != 400:
        return False
    texto = str(error).lower()
    return "output_config" in texto or "effort" in texto


async def generar_respuesta(mensaje: str, historial: list[dict]) -> tuple[str, bool]:
    """
    Genera una respuesta con Claude.

    Args:
        mensaje: el mensaje nuevo del cliente
        historial: los mensajes anteriores, [{"role": "user"|"assistant", "content": "..."}]

    Returns:
        (texto, es_respuesta_real)

        "es_respuesta_real" es False cuando lo que se devuelve es un aviso tecnico
        (error o fallback) y no una respuesta del agente. main.py lo usa para no
        guardar esos avisos en el historial: si se guardaran, quedarian contaminando
        el contexto de todos los mensajes siguientes.
    """
    global _soporta_esfuerzo

    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(), False

    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": mensaje})

    system_prompt = cargar_system_prompt(es_primer_mensaje=not historial)
    extras = {"output_config": {"effort": ESFUERZO}} if (_soporta_esfuerzo and ESFUERZO) else {}

    async def _llamar(parametros_extra: dict):
        return await client.messages.create(
            model=MODELO,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=mensajes,
            tools=TOOLS,
            **parametros_extra,
        )

    try:
        respuesta = await _llamar(extras)
    except Exception as e:  # noqa: BLE001
        if extras and _es_error_de_esfuerzo(e):
            logger.warning(
                f"El modelo {MODELO} no acepta output_config.effort; se reintenta sin ese parametro."
            )
            _soporta_esfuerzo = False
            try:
                respuesta = await _llamar({})
            except Exception as e2:  # noqa: BLE001
                logger.error(f"Error llamando a Claude: {e2}")
                return obtener_mensaje_error(), False
        else:
            logger.error(f"Error llamando a Claude: {e}")
            return obtener_mensaje_error(), False

    # Mientras Claude pida usar una herramienta, se la corremos y le devolvemos el
    # resultado, hasta que decida que ya tiene lo necesario para contestarle al huesped.
    iteraciones = 0
    while respuesta.stop_reason == "tool_use" and iteraciones < MAX_ITERACIONES_HERRAMIENTAS:
        iteraciones += 1
        mensajes.append({"role": "assistant", "content": respuesta.content})

        resultados = []
        for bloque in respuesta.content:
            if bloque.type != "tool_use":
                continue
            resultado = await _ejecutar_herramienta(bloque.name, bloque.input)
            resultados.append(
                {"type": "tool_result", "tool_use_id": bloque.id, "content": resultado}
            )
        mensajes.append({"role": "user", "content": resultados})

        try:
            respuesta = await _llamar(extras if _soporta_esfuerzo and ESFUERZO else {})
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error llamando a Claude durante el uso de herramientas: {e}")
            return obtener_mensaje_error(), False

    if getattr(respuesta, "stop_reason", None) == "max_tokens":
        logger.warning(
            f"La respuesta se corto por llegar al tope de {MAX_TOKENS} tokens. "
            "Si pasa seguido, sube ANTHROPIC_MAX_TOKENS o acorta el system prompt."
        )

    texto = _extraer_texto(respuesta)
    if not texto:
        logger.warning("Claude devolvio una respuesta sin texto")
        return obtener_mensaje_fallback(), False

    logger.info(
        f"Respuesta generada con {MODELO} "
        f"({respuesta.usage.input_tokens} in / {respuesta.usage.output_tokens} out)"
    )
    return texto, True
