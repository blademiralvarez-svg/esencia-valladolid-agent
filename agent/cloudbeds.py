# agent/cloudbeds.py — Integracion con el PMS Cloudbeds
# Generado por AgentKit

"""
Consulta disponibilidad y tarifas reales de Esencia Valladolid Hotel en Cloudbeds,
y arma el link del motor de reservas con las fechas ya cargadas.

Usa una API Key estatica (no OAuth): se genera una vez en el panel de Cloudbeds
con el scope read:room y no expira mientras se use al menos una vez cada 30 dias.
Documentacion: https://developers.cloudbeds.com/docs/quickstart-guide-api-authentication-for-property-level-users
"""

import logging
import os

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

BASE_URL = "https://hotels.cloudbeds.com/api/v1.2"

# Cloudbeds identifica las habitaciones con sus nombres tecnicos del PMS, que no son
# los que usa el hotel de cara al huesped. Se traduce aca para que Ana hable siempre
# en los mismos terminos que la web y WhatsApp, sin depender de que el modelo adivine
# la equivalencia.
CATEGORIA_POR_TIPO_CLOUDBEDS = {
    "Suite Junior queensize bed": "Habitación con Alberca Privada",
    "Suite Junior kingsize bed": "Habitación con Alberca Privada",
    "Deluxe Queen": "Habitación Boutique",
    "Deluxe Doble Individual": "Habitación Boutique",
    "Deluxe Family": "Habitación Boutique",
}


def _configurado() -> bool:
    return bool(os.getenv("CLOUDBEDS_API_KEY") and os.getenv("CLOUDBEDS_PROPERTY_ID"))


async def consultar_disponibilidad(
    fecha_entrada: str, fecha_salida: str, adultos: int, ninos: int = 0
) -> dict:
    """
    Consulta disponibilidad y tarifa real contra Cloudbeds para un rango de fechas.

    Args:
        fecha_entrada: formato YYYY-MM-DD (check-in)
        fecha_salida: formato YYYY-MM-DD (check-out)
        adultos: numero de adultos
        ninos: numero de ninos

    Returns:
        {"disponible": bool, "habitaciones": [...], "error": str | None}
    """
    api_key = os.getenv("CLOUDBEDS_API_KEY", "")
    property_id = os.getenv("CLOUDBEDS_PROPERTY_ID", "")

    if not api_key or not property_id:
        return {
            "disponible": False,
            "habitaciones": [],
            "error": "CLOUDBEDS_API_KEY o CLOUDBEDS_PROPERTY_ID no configurados",
        }

    params = {
        "propertyIDs": property_id,
        "startDate": fecha_entrada,
        "endDate": fecha_salida,
        "rooms": 1,
        "adults": adultos,
        "children": ninos,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as cliente:
            r = await cliente.get(
                f"{BASE_URL}/getAvailableRoomTypes",
                params=params,
                headers={"x-api-key": api_key},
            )
    except httpx.HTTPError as e:
        logger.error(f"Error de red consultando disponibilidad en Cloudbeds: {e}")
        return {"disponible": False, "habitaciones": [], "error": "No se pudo contactar a Cloudbeds"}

    if r.status_code != 200:
        logger.error(f"Cloudbeds respondio {r.status_code} en getAvailableRoomTypes: {r.text[:300]}")
        return {
            "disponible": False,
            "habitaciones": [],
            "error": f"Cloudbeds respondio {r.status_code}",
        }

    cuerpo = r.json()
    if not cuerpo.get("success"):
        return {"disponible": False, "habitaciones": [], "error": "Cloudbeds no pudo procesar la consulta"}

    habitaciones = []
    for propiedad in cuerpo.get("data", []):
        for room in propiedad.get("propertyRooms", []):
            tipo_cloudbeds = room.get("roomTypeName")
            habitaciones.append(
                {
                    "categoria": CATEGORIA_POR_TIPO_CLOUDBEDS.get(tipo_cloudbeds, tipo_cloudbeds),
                    "tipo_especifico": tipo_cloudbeds,
                    "tarifa_por_noche": room.get("roomRate"),
                    "habitaciones_disponibles": room.get("roomsAvailable"),
                    "max_huespedes": room.get("maxGuests"),
                    "costo_huesped_adicional": room.get("adultsExtraCharge"),
                }
            )

    return {"disponible": len(habitaciones) > 0, "habitaciones": habitaciones, "error": None}


def generar_link_reserva(fecha_entrada: str, fecha_salida: str, adultos: int, ninos: int = 0) -> str:
    """
    Arma el link del motor de reservas de Cloudbeds con fechas y huespedes prellenados,
    para que el huesped complete el pago el mismo sin que Ana maneje tarjetas.
    """
    base = os.getenv("CLOUDBEDS_BOOKING_ENGINE_URL", "")
    if not base:
        return ""
    return f"{base}?checkin={fecha_entrada}&checkout={fecha_salida}&adults={adultos}&kids={ninos}"
