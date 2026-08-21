# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente.
Funciona con cualquier proveedor (Zernio, Meta) gracias a la capa de providers.
"""

import asyncio
import logging
import os
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from agent.brain import generar_respuesta, obtener_mensaje_error
from agent.memory import (
    esta_pausada,
    guardar_mensaje,
    inicializar_db,
    liberar_evento,
    limpiar_eventos_viejos,
    marcar_evento_procesado,
    obtener_historial,
    pausar_conversacion,
)
from agent.providers import obtener_proveedor
from agent.providers.base import MensajeEntrante

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agentkit")
# En desarrollo queremos el detalle de NUESTRO agente, no el de las librerias.
# Poner el nivel raiz en DEBUG llena la terminal de ruido de aiosqlite y httpx
# y hace imposible leer lo que hizo el agente.
logger.setLevel(logging.DEBUG if ENVIRONMENT == "development" else logging.INFO)

PORT = int(os.getenv("PORT", "8000"))

# Cuando alguien de recepcion contesta manual desde la app de WhatsApp Business
# (numero en modo Coexistence), Ana se queda callada en esa conversacion por este
# tiempo, para no pisarle la respuesta a quien ya esta atendiendo al huesped.
PAUSA_HUMANO_HORAS = float(os.getenv("PAUSA_HUMANO_HORAS") or "2")

# En modo Coexistence, WhatsApp sincroniza el propio mensaje de Ana hacia la app
# nativa de WhatsApp Business, y Zernio manda ESE eco como si fuera un message.sent
# con source="whatsapp_business_app" -- indistinguible de un humano contestando de
# verdad. Confirmado en produccion: Ana se pausaba sola segundos despues de cada
# respuesta propia. Si la senal de "un humano tomo la conversacion" llega dentro de
# este margen desde que Ana misma le mando un mensaje a esa conversacion, se descarta
# por ser casi seguro el eco, no una persona.
MARGEN_ECO_SEGUNDOS = 20

# conversation_id -> hora (UTC) del ultimo mensaje que Ana misma mando ahi
_ultimo_envio_ana: dict[str, datetime] = {}

def _solo_digitos(numero: str) -> str:
    return "".join(c for c in numero if c.isdigit())


# Numeros del equipo que a veces escriben directo (1 a 1) al numero del hotel para
# temas internos. Se comparan por los ultimos 10 digitos para no depender de si
# WhatsApp antepone el "1" de larga distancia movil de Mexico (+52 1 XXX...) o no.
# Ojo: hay que normalizar los DOS lados igual (extraer solo digitos antes de
# recortar) -- si esta lista se arma con un simple [-10:] sobre el string crudo del
# .env y alguien carga un numero con formato ("985-104-5092", "+52 985..."), el
# filtro se rompe en silencio porque deja de coincidir con los digitos limpios que
# usa _es_numero_equipo().
NUMEROS_EQUIPO = {
    _solo_digitos(n)[-10:]
    for n in (os.getenv("NUMEROS_EQUIPO") or "").split(",")
    if n.strip()
}


def _es_numero_equipo(telefono: str) -> bool:
    """True si el mensaje viene de un numero del equipo, no de un huesped."""
    if not NUMEROS_EQUIPO:
        return False
    return _solo_digitos(telefono)[-10:] in NUMEROS_EQUIPO

# Un candado por numero de telefono. En WhatsApp es normal que alguien mande "hola" y
# medio segundo despues la pregunta de verdad: sin esto los dos mensajes se procesarian
# en paralelo, los dos leerian el mismo historial y las escrituras quedarian intercaladas.
_candados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Si la configuracion esta mal, guardamos el error y lo mostramos en el health check,
# en vez de reventar en el import y dejar a Railway reiniciando el contenedor a ciegas.
proveedor = None
error_configuracion: str | None = None
try:
    proveedor = obtener_proveedor()
except Exception as e:  # noqa: BLE001 — cualquier problema de configuracion
    error_configuracion = str(e)

# Resultado del chequeo de credenciales que se hace al arrancar. Se expone en el health
# check: que el servidor conteste no significa que el agente pueda responder por WhatsApp.
estado_proveedor: dict = {"ok": None, "detalle": "sin verificar"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepara la base de datos y chequea el proveedor al arrancar."""
    await inicializar_db()
    await limpiar_eventos_viejos()
    logger.info("Base de datos lista")
    logger.info(f"Servidor AgentKit escuchando en el puerto {PORT}")

    global estado_proveedor
    if proveedor is not None:
        logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
        ok, detalle = await proveedor.verificar_conexion()
        estado_proveedor = {"ok": ok, "detalle": detalle}
        logger.info(f"Conexion con el proveedor: {'OK' if ok else 'ERROR'} — {detalle}")
    else:
        logger.error(f"Proveedor de WhatsApp NO configurado: {error_configuracion}")

    yield


app = FastAPI(title="AgentKit — WhatsApp AI Agent", version="2.0.0", lifespan=lifespan)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway y monitoreo."""
    if error_configuracion:
        return {"status": "error", "service": "agentkit", "detalle": error_configuracion}

    # Se responde 200 aunque las credenciales esten mal, para que Railway no marque el
    # deploy como caido y puedas leer el diagnostico. El detalle esta en el cuerpo.
    return {
        "status": "ok" if estado_proveedor["ok"] else "degradado",
        "service": "agentkit",
        "proveedor": proveedor.__class__.__name__ if proveedor else None,
        "conexion": estado_proveedor,
    }


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificacion GET del webhook. La pide Meta; para Zernio no hace nada."""
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    respuesta = await proveedor.validar_webhook(request)
    if respuesta is not None:
        return PlainTextResponse(respuesta)

    # Meta pide un 403 cuando manda hub.mode=subscribe y el verify_token no coincide.
    # Devolverle 200 le hace creer que la URL quedo verificada cuando no es cierto.
    if request.query_params.get("hub.mode") == "subscribe":
        raise HTTPException(status_code=403, detail="Verify token incorrecto")

    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request, tareas: BackgroundTasks):
    """
    Recibe los mensajes de WhatsApp.

    Contesta 200 de inmediato y procesa el mensaje en segundo plano.

    Esto NO es un detalle de estilo. Los proveedores esperan un 2xx en unos 5 segundos y,
    si no lo reciben, reintentan el mismo evento hasta 7 veces. Como llamar a Claude tarda
    mas que eso, procesar antes de contestar hace que el cliente reciba la misma respuesta
    repetida. Por eso: responder primero, trabajar despues.
    """
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    if not await proveedor.verificar_firma(request):
        raise HTTPException(status_code=401, detail="Firma del webhook invalida")

    try:
        conversation_id_pausar = await proveedor.detectar_pausa_humana(request)
    except Exception as e:  # noqa: BLE001
        # Un payload raro (no-JSON, vacio, un array en vez de un objeto) no debe
        # escapar como 500: eso le confirmaria a Zernio que reintente el evento.
        logger.error(f"No se pudo leer el webhook (deteccion de pausa humana): {e}")
        return {"status": "ignorado"}

    if conversation_id_pausar:
        ultimo_envio = _ultimo_envio_ana.get(conversation_id_pausar)
        segundos_desde_envio = (
            (datetime.now(timezone.utc) - ultimo_envio).total_seconds()
            if ultimo_envio
            else None
        )
        if segundos_desde_envio is not None and segundos_desde_envio < MARGEN_ECO_SEGUNDOS:
            logger.info(
                f"Eco del propio mensaje de Ana en {conversation_id_pausar} "
                f"({segundos_desde_envio:.1f}s despues de enviarlo); no se pausa"
            )
        else:
            await pausar_conversacion(conversation_id_pausar, PAUSA_HUMANO_HORAS)
            logger.info(
                f"Recepcion tomo la conversacion {conversation_id_pausar}; "
                f"Ana se pausa ahi por {PAUSA_HUMANO_HORAS}h"
            )
        return {"status": "ok", "pausado": True}

    try:
        mensajes = await proveedor.parsear_webhook(request)
    except Exception as e:  # noqa: BLE001
        # Un payload raro no debe hacer que el proveedor reintente para siempre
        logger.error(f"No se pudo leer el webhook: {e}")
        return {"status": "ignorado"}

    encolados = 0
    for msg in mensajes:
        if msg.es_propio:
            continue

        if not msg.telefono:
            # Sin telefono, businessScopedUserId NI id no hay forma de saber quien
            # escribio. Nunca tratar esto como una conversacion mas: memory.py indexa
            # todo el historial por telefono, y dos huespedes sin identificador
            # terminarian compartiendo la MISMA clave (""), viendo el historial y el
            # candado el uno del otro.
            logger.error(f"Mensaje sin telefono identificable, se descarta: {msg.contexto}")
            continue

        if not msg.texto.strip():
            # Notas de voz, imagenes, ubicaciones, stickers: parsear_webhook los deja
            # con texto="". Se loguea (a diferencia de antes) para poder ver en los
            # logs que el huesped mando algo y Ana no lo pudo procesar, en vez de que
            # parezca que el mensaje nunca llego.
            logger.info(f"Mensaje sin texto de {msg.telefono} (adjunto no soportado aun), se ignora")
            continue

        if _es_numero_equipo(msg.telefono):
            logger.info(f"Mensaje de un numero del equipo ({msg.telefono}), se ignora")
            continue

        conversation_id = msg.contexto.get("conversation_id")
        if await esta_pausada(conversation_id):
            # Se guarda igual en el historial (aunque Ana no conteste ahora): si no,
            # cuando la pausa venza Ana retoma sin ningun rastro de lo que el huesped
            # dijo mientras recepcion lo atendia.
            logger.info(f"Conversacion {conversation_id} pausada (recepcion la esta atendiendo), se ignora")
            await guardar_mensaje(msg.telefono, "user", msg.texto)
            continue

        # La entrega es "al menos una vez": el mismo evento puede llegar dos veces
        evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
        if evento_id and not await marcar_evento_procesado(evento_id):
            logger.info(f"Evento repetido, se ignora: {evento_id}")
            continue

        logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")
        tareas.add_task(procesar_mensaje, msg)
        encolados += 1

    return {"status": "ok", "encolados": encolados}


# Reintentos de ENVIO (no del webhook): webhook_handler ya le devolvio 200 a Zernio
# antes de que este BackgroundTask corra, asi que desde la perspectiva de Zernio el
# evento ya se entrego con exito -- no hay ningun reintento del lado del proveedor
# que pueda salvar un envio fallido. Si Ana no reintenta ella misma aca, un fallo de
# red pasajero hablando con Zernio deja al huesped sin respuesta para siempre.
REINTENTOS_ENVIO = 3
ESPERA_ENTRE_REINTENTOS_SEGUNDOS = 2.0


async def _enviar_con_reintentos(telefono: str, mensaje: str, contexto: dict) -> bool:
    for intento in range(1, REINTENTOS_ENVIO + 1):
        if await proveedor.enviar_mensaje(telefono, mensaje, contexto):
            return True
        if intento < REINTENTOS_ENVIO:
            logger.warning(f"Reintentando envio a {telefono} (intento {intento + 1}/{REINTENTOS_ENVIO})")
            await asyncio.sleep(ESPERA_ENTRE_REINTENTOS_SEGUNDOS)
    return False


def _registrar_envio_ana(contexto: dict):
    conversation_id = contexto.get("conversation_id")
    if conversation_id:
        _ultimo_envio_ana[conversation_id] = datetime.now(timezone.utc)


async def procesar_mensaje(msg: MensajeEntrante):
    """
    Genera la respuesta y la manda de vuelta. Corre fuera del ciclo del webhook.

    Se toma un candado por telefono: dos mensajes seguidos del mismo cliente se
    atienden en orden, no en paralelo, para que el historial no se mezcle.
    """
    evento_id = msg.contexto.get("evento_id") or msg.mensaje_id

    async with _candados[msg.telefono]:
        try:
            # El historial se lee ANTES de guardar el mensaje actual: brain.py agrega
            # el mensaje nuevo al final, y asi no queda duplicado.
            historial = await obtener_historial(msg.telefono)
            respuesta, es_respuesta_real = await generar_respuesta(msg.texto, historial)

            enviado = await _enviar_con_reintentos(msg.telefono, respuesta, msg.contexto)

            if enviado:
                _registrar_envio_ana(msg.contexto)
            else:
                # Ya se reintento varias veces: esto es una falla real de red/Zernio,
                # no algo que un reintento futuro del proveedor vaya a resolver (el
                # webhook original ya se respondio 200). Se libera el evento igual,
                # por si el huesped reenvia el mismo mensaje mas tarde y Zernio lo
                # entrega con el mismo evento_id -- eso si tiene que procesarse.
                logger.error(f"No se pudo enviar la respuesta a {msg.telefono} tras reintentos; se libera el evento")
                await liberar_evento(evento_id)
                return

            # Solo se guarda en el historial lo que de verdad es conversacion. Los avisos
            # tecnicos ("estoy teniendo problemas") no son un turno del agente: guardarlos
            # los deja contaminando el contexto de todos los mensajes que vengan despues.
            if es_respuesta_real:
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await guardar_mensaje(msg.telefono, "assistant", respuesta)

            logger.info(f"Respuesta enviada a {msg.telefono}: {respuesta}")

        except Exception as e:  # noqa: BLE001
            logger.exception(f"Error procesando el mensaje de {msg.telefono}: {e}")
            await liberar_evento(evento_id)
            try:
                if await _enviar_con_reintentos(msg.telefono, obtener_mensaje_error(), msg.contexto):
                    _registrar_envio_ana(msg.contexto)
            except Exception:  # noqa: BLE001
                logger.error("Tampoco se pudo avisarle al cliente del error")
