"""
Sube las sesiones planificadas de la semana al calendario de intervals.icu, que a
su vez las empuja solo al reloj Garmin del atleta (requiere que en intervals.icu
esté activo "upload workouts to Garmin", campo icu_garmin_upload_workouts).

Sintaxis de la descripción de un entrenamiento (verificada contra la API real el
2026-08-08 — el parser tiene trampas que costó descubrir, no cambiar a ojo):

  - Carrera:   "- 8km 6:30-6:20/km Pace"        -> distancia + ritmo objetivo
  - Ciclismo:  "- 40km 2:44-2:24/km Pace"       -> distancia + ritmo (2:44/km = 22 km/h)
  - Natación:  "- 0.6km 2:40-2:20/100m Pace"    -> distancia + ritmo por 100 m

Tres reglas que hay que respetar sí o sí:

  1. La palabra "Pace" al final es OBLIGATORIA para que se guarde el objetivo de
     ritmo. Sin ella ("- 8km 6:25/km") la API acepta la petición igualmente pero
     descarta el ritmo en silencio: el reloj recibiría la distancia sin objetivo.
  2. "m" SIEMPRE significa minutos, nunca metros. Por eso las distancias de
     natación van en km decimales ("0.6km" = 600 m); "600m" se interpretaría
     como 600 minutos.
  3. En ciclismo "kph" no se parsea: la velocidad hay que expresarla como min/km.
     Alternativa válida por pulso: "- 40km Z2 HR".

Los rangos de ritmo se escriben del más lento al más rápido ("6:30-6:20/km").
"""

import requests

BASE_URL = "https://intervals.icu/api/v1"

# Marca los eventos creados por esta automatización. Al regenerar una semana solo se
# borran los eventos con este prefijo, para no tocar nunca nada que el atleta haya
# puesto a mano en su calendario.
EXTERNAL_ID_PREFIX = "ironman703-auto"

# El gimnasio se lleva en Hevy y no aporta nada como entrenamiento guiado en el reloj,
# así que deliberadamente no se sube. Descanso tampoco es un entrenamiento.
TIPO_A_INTERVALS = {
    "Carrera": "Run",
    "Ciclismo": "Ride",
    "Natación": "Swim",
}


def _auth(api_key: str):
    return ("API_KEY", api_key)


def borrar_eventos_previos(api_key: str, athlete_id: str, oldest: str, newest: str) -> int:
    """Borra los eventos de ese rango creados por esta automatización (no los del atleta)."""
    resp = requests.get(
        f"{BASE_URL}/athlete/{athlete_id}/events",
        params={"oldest": oldest, "newest": newest},
        auth=_auth(api_key),
        timeout=30,
    )
    resp.raise_for_status()

    borrados = 0
    for event in resp.json():
        external_id = event.get("external_id") or ""
        if not external_id.startswith(EXTERNAL_ID_PREFIX):
            continue
        borrar = requests.delete(
            f"{BASE_URL}/athlete/{athlete_id}/events/{event['id']}",
            auth=_auth(api_key),
            timeout=30,
        )
        borrar.raise_for_status()
        borrados += 1
    return borrados


def construir_eventos(semana_dias, fecha_lunes) -> list:
    """semana_dias: lista de PlanDia (dia + sesiones). Devuelve la lista de eventos a crear."""
    from datetime import timedelta

    dias_orden = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    eventos = []

    for dia_plan in semana_dias:
        try:
            offset = dias_orden.index(dia_plan.dia)
        except ValueError:
            continue
        fecha = (fecha_lunes + timedelta(days=offset)).isoformat()

        for idx, sesion in enumerate(dia_plan.sesiones):
            tipo_intervals = TIPO_A_INTERVALS.get(sesion.tipo)
            if not tipo_intervals or not sesion.workout_description:
                continue
            eventos.append(
                {
                    "category": "WORKOUT",
                    "start_date_local": f"{fecha}T00:00:00",
                    "type": tipo_intervals,
                    "name": f"{sesion.tipo} — {sesion.texto}"[:100],
                    "description": sesion.workout_description,
                    "moving_time": sesion.workout_moving_time or None,
                    "external_id": f"{EXTERNAL_ID_PREFIX}-{fecha}-{idx}",
                }
            )
    return eventos


def subir_semana(api_key: str, athlete_id: str, semana_dias, fecha_lunes) -> dict:
    """Borra lo subido antes para esa semana y sube la planificación nueva.

    Devuelve un resumen con lo creado y los errores de push a Garmin, si los hay.
    """
    from datetime import timedelta

    oldest = fecha_lunes.isoformat()
    newest = (fecha_lunes + timedelta(days=6)).isoformat()

    borrados = borrar_eventos_previos(api_key, athlete_id, oldest, newest)
    eventos = construir_eventos(semana_dias, fecha_lunes)

    if not eventos:
        return {
            "creados": 0,
            "borrados": borrados,
            "rango": f"{oldest} → {newest}",
            "push_errors": [],
            "detalle": [],
        }

    resp = requests.post(
        f"{BASE_URL}/athlete/{athlete_id}/events/bulk",
        params={"upsertOnUid": "false"},
        json=eventos,
        auth=_auth(api_key),
        timeout=60,
    )
    resp.raise_for_status()
    creados = resp.json()

    push_errors = [
        f"{e.get('name')}: {e.get('push_errors')}" for e in creados if e.get("push_errors")
    ]
    detalle = [
        f"{e.get('start_date_local', '')[:10]} {e.get('type')} — {e.get('name')}" for e in creados
    ]

    return {
        "creados": len(creados),
        "borrados": borrados,
        "rango": f"{oldest} → {newest}",
        "push_errors": push_errors,
        "detalle": detalle,
    }
