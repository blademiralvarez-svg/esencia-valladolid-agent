# agent/tools.py — Herramientas del agente
# Generado por AgentKit

"""
Herramientas especificas del negocio.

OJO: estas funciones NO se ejecutan solas todavia. La informacion del negocio le llega
al agente por el system prompt (config/prompts.yaml), asi que para CONTESTAR preguntas
no hace falta nada de aca. Este archivo es el lugar para las ACCIONES —reservar, cobrar,
abrir un ticket— y conectarlas al ciclo de tool use de Claude es un paso aparte.

Casos de uso de Esencia Valladolid Hotel: FAQ, reservaciones, leads/ventas,
pedidos (servicios adicionales) y soporte post-venta.
"""

import logging
from pathlib import Path

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
        "esta_abierto": True,  # Esencia atiende 24 horas
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


# ════════════════════════════════════════════════════════════
# Reservaciones
# ════════════════════════════════════════════════════════════

TIPOS_HABITACION = ("alberca_privada", "boutique")


def obtener_tipos_habitacion() -> list[dict]:
    """Lista los tipos de habitacion disponibles con sus caracteristicas basicas."""
    return [
        {
            "tipo": "alberca_privada",
            "nombre": "Habitacion con Alberca Privada",
            "ubicacion": "Planta baja",
            "cama": "King",
            "unidades": 4,
        },
        {
            "tipo": "boutique",
            "nombre": "Habitacion Boutique",
            "ubicacion": "Segundo piso",
            "cama": "Matrimonial",
            "unidades": 4,
        },
    ]


def registrar_solicitud_reserva(
    telefono: str,
    fecha_entrada: str,
    fecha_salida: str,
    tipo_habitacion: str,
    num_huespedes: int,
    notas: str = "",
) -> dict:
    """
    Registra una solicitud de reserva para que el equipo del hotel la confirme
    con disponibilidad y tarifa reales.

    El hotel no expone un sistema de reservas en linea, asi que esto NO confirma
    la reserva: deja constancia de la solicitud para que un humano la cierre.
    """
    logger.info(
        f"Solicitud de reserva — telefono={telefono} entrada={fecha_entrada} "
        f"salida={fecha_salida} tipo={tipo_habitacion} huespedes={num_huespedes} notas={notas}"
    )
    return {
        "registrada": True,
        "mensaje": (
            "Solicitud registrada. El equipo del hotel va a confirmar disponibilidad "
            "y tarifa para esas fechas."
        ),
    }


# ════════════════════════════════════════════════════════════
# Leads / ventas
# ════════════════════════════════════════════════════════════

def registrar_lead(telefono: str, nombre: str, interes: str) -> dict:
    """Registra un lead nuevo: quien es, y que le interesa (fechas, ocasion, tipo de habitacion)."""
    logger.info(f"Lead registrado — telefono={telefono} nombre={nombre} interes={interes}")
    return {"registrado": True}


def escalar_a_equipo(telefono: str, contexto: str) -> dict:
    """Marca una conversacion para que el equipo del hotel la atienda directamente."""
    logger.info(f"Conversacion escalada al equipo — telefono={telefono} contexto={contexto}")
    return {"escalado": True}


# ════════════════════════════════════════════════════════════
# Pedidos (servicios adicionales: spa en la habitacion, tours)
# ════════════════════════════════════════════════════════════

def registrar_pedido_servicio(telefono: str, servicio: str, fecha: str, notas: str = "") -> dict:
    """
    Registra un pedido de servicio adicional (spa en la habitacion, tour personalizado)
    para que el equipo del hotel lo confirme y coordine horario.
    """
    logger.info(
        f"Pedido de servicio — telefono={telefono} servicio={servicio} fecha={fecha} notas={notas}"
    )
    return {
        "registrado": True,
        "mensaje": "Pedido registrado. El equipo del hotel va a coordinar el horario contigo.",
    }


# ════════════════════════════════════════════════════════════
# Soporte post-venta
# ════════════════════════════════════════════════════════════

def crear_ticket_soporte(telefono: str, problema: str) -> dict:
    """Abre un ticket de soporte para un huesped que ya reservo o esta hospedado."""
    logger.info(f"Ticket de soporte — telefono={telefono} problema={problema}")
    return {"ticket_creado": True}
