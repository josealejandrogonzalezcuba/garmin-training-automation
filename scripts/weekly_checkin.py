"""
Check-in semanal automático de la preparación del Ironman 70.3.

Se ejecuta desde un GitHub Action (ver .github/workflows/weekly-checkin.yml).
Pasos: lee el contexto del repo -> descarga datos de intervals.icu -> le pide a
Claude un análisis estructurado -> actualiza data/registro-entrenamiento.md (y
data/plan-macro.md si toca) -> envía un email de resumen por Resend.
"""

import base64
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from typing import List, Literal, Optional

import anthropic
import pydantic
import requests
from pydantic import BaseModel

from intervals_push import subir_semana
from pdf_semana import build_week_pdf

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(REPO_ROOT, "data")

PERFIL_PATH = os.path.join(DATA_DIR, "perfil-atleta.md")
PLAN_PATH = os.path.join(DATA_DIR, "plan-macro.md")
REGISTRO_PATH = os.path.join(DATA_DIR, "registro-entrenamiento.md")
SKILL_PATH = os.path.join(DATA_DIR, "coaching-instructions.md")

INTERVALS_API_KEY = os.environ["INTERVALS_API_KEY"]
INTERVALS_ATHLETE_ID = os.environ["INTERVALS_ATHLETE_ID"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"]

TODAY = date.today().isoformat()


def proximo_lunes(hoy: Optional[date] = None) -> date:
    """Lunes de la semana que se va a planificar.

    El check-in corre los domingos por la noche, así que "la próxima semana" empieza
    mañana. Se calcula el próximo lunes ESTRICTAMENTE futuro para que, si el workflow
    se lanza a mano un día cualquiera, siga planificando la semana siguiente completa
    en vez de una semana ya empezada.
    """
    hoy = hoy or date.today()
    dias_hasta_lunes = (0 - hoy.weekday() + 7) % 7 or 7
    return hoy + timedelta(days=dias_hasta_lunes)


def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def last_checkin_date(registro_text: str) -> str:
    """Encuentra la fecha más reciente mencionada en las cabeceras '## YYYY-MM-DD ...'."""
    dates = re.findall(r"^## (\d{4}-\d{2}-\d{2})", registro_text, flags=re.MULTILINE)
    if not dates:
        raise ValueError("No se encontró ninguna fecha de check-in en registro-entrenamiento.md")
    return max(dates)


def fetch_intervals_data(oldest: str, newest: str) -> tuple[str, str]:
    auth = ("API_KEY", INTERVALS_API_KEY)
    base = f"https://intervals.icu/api/v1/athlete/{INTERVALS_ATHLETE_ID}"

    activities = requests.get(
        f"{base}/activities", params={"oldest": oldest, "newest": newest}, auth=auth, timeout=30
    )
    wellness = requests.get(
        f"{base}/wellness", params={"oldest": oldest, "newest": newest}, auth=auth, timeout=30
    )
    activities.raise_for_status()
    wellness.raise_for_status()
    return activities.text, wellness.text


TipoSesion = Literal["Natación", "Ciclismo", "Gimnasio", "Carrera", "Transición", "Descanso"]


class SesionDia(BaseModel):
    tipo: TipoSesion
    texto: str  # texto breve de la tarjeta del PDF (distancia, ritmo objetivo)
    hecho: bool = False  # true solo si la sesión ya está completada (tarjeta verde)

    # Campos para subir la sesión al calendario de intervals.icu -> Garmin.
    # Solo aplican a Carrera/Ciclismo/Natación: el gimnasio se lleva en Hevy y el
    # descanso no es un entrenamiento, así que ahí van a None.
    workout_description: Optional[str] = None
    workout_moving_time: Optional[int] = None  # duración estimada en SEGUNDOS


DiaSemana = Literal["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


class PlanDia(BaseModel):
    dia: DiaSemana  # EXACTAMENTE uno de estos 7 valores, sin fecha ni variaciones — el generador
    # de PDF hace coincidencia exacta contra esta lista. Un valor distinto (p.ej. "Lunes 10/8")
    # rompía el PDF entero (todos los días salían "Sin sesión") porque no había ningún tipo que
    # forzara al modelo a devolver el string exacto. Literal[...] lo convierte en un enum a nivel
    # de schema: la API no puede devolver otra cosa.
    sesiones: List[SesionDia]


class CheckinResult(BaseModel):
    registro_entry_markdown: str
    plan_macro_changed: bool
    plan_macro_full_markdown: Optional[str]
    email_subject: str
    email_body_html: str
    semana_titulo: str  # p.ej. "Ironman 70.3 — Semana 3"
    semana_subtitulo: str  # p.ej. "Lunes 17 → Domingo 23 de agosto · Fase 0, Bloque 2 (semana 1 de 3)"
    semana_pie: str  # nota breve al pie del PDF (fase, progresión aplicada, avisos)
    semana_dias: List[PlanDia]  # las sesiones de la PRÓXIMA semana, para el PDF adjunto


def ask_claude(
    perfil: str,
    plan: str,
    registro: str,
    skill: str,
    oldest: str,
    newest: str,
    activities_json: str,
    wellness_json: str,
) -> CheckinResult:
    client = anthropic.Anthropic()

    prompt = f"""Eres el coach de entrenamiento de un triatleta que se prepara para un Ironman 70.3.
Hoy es {newest}. Analiza el rango desde el último check-in ({oldest}) hasta hoy.

=== PERFIL DEL ATLETA (data/perfil-atleta.md) ===
{perfil}

=== PLAN MACRO ACTUAL (data/plan-macro.md) ===
{plan}

=== REGISTRO HISTÓRICO DE CHECK-INS (data/registro-entrenamiento.md) ===
{registro}

=== INSTRUCCIONES DE LA SKILL DE ENTRENAMIENTO ===
Sigue específicamente los Pasos 3 a 7 de este documento (los pasos 1-2 son solo
para la conversación inicial con el atleta, ignóralos):
{skill}

=== DATOS DE INTERVALS.ICU ===
Actividades (JSON):
{activities_json}

Wellness / forma (JSON):
{wellness_json}

Tu tarea:
1. Analiza el progreso real frente a lo planificado, siguiendo el proceso descrito en la skill
   (recalibración de zonas/ritmos incluida).
2. Decide si plan-macro.md necesita cambios reales (solo si hay una razón clara y justificada:
   desviación sostenida, fatiga, lesión, progreso más rápido de lo previsto, o una fase que ya
   no encaja). Un check-in normal con adherencia razonable NO debe generar cambios.
3. Devuelve el resultado con estos campos exactos:
   - registro_entry_markdown: la nueva sección completa a añadir a registro-entrenamiento.md,
     empezando por "## {newest} — <título breve>", con el análisis, las métricas calculadas y
     si se cambió el plan o no y por qué.
   - plan_macro_changed: true/false.
   - plan_macro_full_markdown: si plan_macro_changed es true, el contenido COMPLETO y actualizado
     de plan-macro.md (el archivo entero, no solo la parte que cambia). Si es false, deja este
     campo vacío.
   - email_subject: asunto del correo de resumen semanal.
   - email_body_html: cuerpo del correo en HTML simple (resumen de la semana, horas por
     disciplina, comparación con la semana anterior, si se cambió el plan o no, y un
     recordatorio de que el gimnasio y las sensaciones se comentan directamente en un chat
     con Claude, no respondiendo a este correo). Menciona que el detalle día a día de la
     próxima semana va en el PDF adjunto, no lo repitas entero en el cuerpo del correo.
   - semana_titulo: título del PDF adjunto, p.ej. "Ironman 70.3 — Semana X".
   - semana_subtitulo: fechas exactas (lunes → domingo) y fase/bloque de la próxima semana.
   - semana_pie: nota breve para el pie del PDF (fase, progresión aplicada esta semana, avisos
     importantes de carga/lesión si los hay).
   - semana_dias: la planificación día a día de la PRÓXIMA semana — SIEMPRE los 7 días (Lunes a
     Domingo), incluso si esta ejecución no tiene datos nuevos que analizar (en ese caso, usa la
     semana ya prevista en plan-macro.md sin cambios). Nunca lo dejes vacío. El campo "dia" de
     cada entrada debe ser EXACTAMENTE uno de: "Lunes", "Martes", "Miércoles", "Jueves", "Viernes",
     "Sábado", "Domingo" — sin la fecha ni ningún otro texto añadido (la fecha concreta va en
     "semana_subtitulo", no en cada día). Cada día lleva su lista de sesiones (tipo: "Natación",
     "Ciclismo", "Gimnasio", "Carrera", "Transición" o "Descanso").

     Formato del campo "texto" según el tipo (esto es solo para las tarjetas del PDF, que deben
     ser breves — el detalle completo sigue yendo en registro_entry_markdown si hace falta).
     NUNCA incluyas la hora ni el momento del día ("de noche", "muy temprano", "por la mañana")
     en ninguna tarjeta: el atleta ya tiene sus horarios fijos y solo ensucia el PDF.
     - "Gimnasio": SOLO el grupo muscular. Lunes es siempre "Pierna". El resto de sesiones de
       gimnasio alternan "Espalda-Bíceps" y "Pecho-Hombro-Tríceps" en el orden cronológico real
       de las sesiones, continuando la secuencia de la semana anterior sin reiniciar nunca
       (mira en registro-entrenamiento.md cuál fue el último grupo no-Pierna confirmado y aplica
       el siguiente de la alternancia). Nada más — sin hora, sin "enfoque hipertrofia".
     - "Natación": SOLO la duración o distancia (p.ej. "30-40 min", "600 m"). Sin detalle técnico.
     - "Ciclismo" y "Carrera": distancia y ritmo/zona objetivo concreto (p.ej. "40 km, Zona 2"
       o "8 km, 6:20-6:30/km"), porque eso sí cambia semana a semana.
     - "Descanso": "Descanso total. Sin entrenamiento."

     Campo "hecho": ponlo a true SOLO si esa sesión ya está completada. Para la planificación de
     la próxima semana será false en todas.

   Además, para "Carrera", "Ciclismo" y "Natación" (NUNCA para "Gimnasio" ni "Descanso") rellena:
   - workout_description: el entrenamiento estructurado que se subirá al reloj Garmin, con la
     sintaxis EXACTA de intervals.icu. Reglas verificadas contra la API real, respétalas al pie
     de la letra o el objetivo se pierde en silencio:
       * La palabra "Pace" al final es OBLIGATORIA para que se guarde el ritmo objetivo.
       * "m" SIEMPRE significa minutos, nunca metros: las distancias de natación van en km
         decimales ("0.6km" = 600 m). "600m" se interpretaría como 600 minutos.
       * En ciclismo "kph" NO se parsea: expresa la velocidad como min/km (2:44/km = 22 km/h).
       * Los rangos de ritmo se escriben del más lento al más rápido: "6:30-6:20/km".
     Ejemplos válidos (una línea por paso, "\\n" entre líneas):
       Carrera:  "Calentamiento\\n- 10m Z1 HR\\n\\nPrincipal\\n- 8km 6:30-6:20/km Pace\\n\\nVuelta a la calma\\n- 5m Z1 HR"
       Ciclismo: "- 40km 2:44-2:24/km Pace"
       Natación: "Calentamiento\\n- 0.2km 2:50-2:30/100m Pace\\n\\nPrincipal\\n8x\\n- 0.05km 2:40-2:20/100m Pace\\n- 30s Z1 HR"
   - workout_moving_time: duración total estimada de esa sesión en SEGUNDOS (entero).
"""

    # Causa raíz real (confirmada probando en la Action): con max_tokens=16000 dos de
    # cada tres intentos terminaban con stop_reason="max_tokens" y CERO bloques de
    # texto — es decir, el modelo se gastaba todo el presupuesto de tokens en
    # "thinking" (adaptive thinking, activo por defecto si no se especifica el
    # parámetro `thinking`) antes de llegar a escribir el JSON de salida. No era un
    # problema de presupuesto insuficiente para el JSON en sí, sino de que el
    # razonamiento interno se comía el presupuesto entero. Se desactiva explícitamente
    # para que los 16000 tokens se dediquen íntegros a la respuesta estructurada.
    # Se mantiene además un pequeño bucle de reintentos por si la generación de JSON
    # sale puntualmente mal formada.
    intentos = 3
    ultimo_error: Optional[Exception] = None
    for intento in range(1, intentos + 1):
        try:
            response = client.messages.parse(
                model="claude-sonnet-5",
                max_tokens=16000,
                thinking={"type": "disabled"},
                output_format=CheckinResult,
                messages=[{"role": "user", "content": prompt}],
            )
        except pydantic.ValidationError as exc:
            ultimo_error = exc
            print(f"Intento {intento}/{intentos}: JSON estructurado mal formado ({exc}).")
            continue

        if response.parsed_output is None:
            ultimo_error = RuntimeError(
                f"Claude no devolvió ningún bloque de texto parseable (stop_reason={response.stop_reason!r})."
            )
            print(f"Intento {intento}/{intentos}: {ultimo_error}")
            continue

        return response.parsed_output

    raise RuntimeError(
        f"Claude no devolvió una respuesta estructurada válida tras {intentos} intentos. "
        f"Último error: {ultimo_error!r}"
    ) from ultimo_error


def send_email(subject: str, html_body: str, pdf_path: Optional[str] = None) -> None:
    payload = {
        "from": "Ironman 70.3 Coach <onboarding@resend.dev>",
        "to": [RECIPIENT_EMAIL],
        "subject": subject,
        "html": html_body,
    }
    if pdf_path:
        with open(pdf_path, "rb") as f:
            pdf_b64 = base64.b64encode(f.read()).decode("ascii")
        payload["attachments"] = [
            {"filename": os.path.basename(pdf_path), "content": pdf_b64}
        ]

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()


def main() -> None:
    perfil = read_file(PERFIL_PATH)
    plan = read_file(PLAN_PATH)
    registro = read_file(REGISTRO_PATH)
    skill = read_file(SKILL_PATH)

    oldest = last_checkin_date(registro)
    newest = TODAY
    print(f"Analizando rango: {oldest} -> {newest}")

    activities_json, wellness_json = fetch_intervals_data(oldest, newest)

    result = ask_claude(perfil, plan, registro, skill, oldest, newest, activities_json, wellness_json)

    dias_recibidos = [d.dia for d in result.semana_dias]
    total_sesiones = sum(len(d.sesiones) for d in result.semana_dias)
    print(f"Días recibidos para el PDF: {dias_recibidos} (total sesiones: {total_sesiones})")

    dias_esperados = set(DiaSemana.__args__)
    dias_faltantes = dias_esperados - set(dias_recibidos)
    if dias_faltantes or total_sesiones == 0:
        raise RuntimeError(
            "Respuesta de Claude incompleta para el PDF semanal: "
            f"faltan días {sorted(dias_faltantes)}, total de sesiones = {total_sesiones}. "
            "Se aborta ANTES de escribir archivos o enviar el email, para no entregar un PDF "
            "vacío o una planificación rota. Revisa los logs de esta ejecución."
        )

    with open(REGISTRO_PATH, "a", encoding="utf-8") as f:
        f.write("\n" + result.registro_entry_markdown.strip() + "\n")
    print("registro-entrenamiento.md actualizado.")

    if result.plan_macro_changed and result.plan_macro_full_markdown:
        with open(PLAN_PATH, "w", encoding="utf-8") as f:
            f.write(result.plan_macro_full_markdown)
        print("plan-macro.md actualizado (cambios justificados).")
    else:
        print("plan-macro.md sin cambios (sin motivo justificado esta semana).")

    pdf_path = os.path.join(REPO_ROOT, "semana-actual.pdf")
    sesiones_por_dia = {
        dia_plan.dia: [
            {"tipo": s.tipo, "texto": s.texto, "hecho": s.hecho} for s in dia_plan.sesiones
        ]
        for dia_plan in result.semana_dias
    }
    build_week_pdf(
        pdf_path,
        result.semana_titulo,
        result.semana_subtitulo,
        sesiones_por_dia,
        pie_texto=result.semana_pie,
    )
    print("PDF de la semana generado.")

    # Sube la semana al calendario de intervals.icu, que la empuja sola al Garmin.
    # Si esto falla NO se aborta el check-in: el análisis y el correo siguen teniendo
    # valor por sí solos, así que se reporta el fallo dentro del propio correo (y en
    # los logs) en vez de quedarse sin resumen semanal.
    push_html = ""
    try:
        resumen = subir_semana(
            INTERVALS_API_KEY, INTERVALS_ATHLETE_ID, result.semana_dias, proximo_lunes()
        )
        print(f"intervals.icu: {resumen['creados']} sesiones subidas ({resumen['rango']}), "
              f"{resumen['borrados']} previas reemplazadas.")
        for linea in resumen["detalle"]:
            print(f"  - {linea}")
        if resumen["push_errors"]:
            print(f"AVISO: errores al empujar a Garmin: {resumen['push_errors']}")
            push_html = (
                "<p><b>Aviso:</b> las sesiones se crearon en intervals.icu pero Garmin "
                f"devolvió errores al recibirlas: {'; '.join(resumen['push_errors'])}. "
                "Revisa el calendario del reloj.</p>"
            )
        else:
            push_html = (
                f"<p>✅ Las {resumen['creados']} sesiones de carrera, ciclismo y natación de esta "
                "semana ya están cargadas en tu Garmin (vía intervals.icu). El gimnasio no se sube: "
                "ese lo llevas en Hevy.</p>"
            )
    except Exception as exc:  # noqa: BLE001 - se reporta, no se propaga
        print(f"ERROR subiendo las sesiones a intervals.icu: {exc}")
        push_html = (
            "<p><b>⚠️ No se pudieron cargar las sesiones en el Garmin esta semana</b> "
            f"(error: {exc}). El plan de abajo sigue siendo válido, pero tendrás que seguirlo "
            "desde el PDF hasta que se resuelva.</p>"
        )

    send_email(result.email_subject, push_html + result.email_body_html, pdf_path=pdf_path)
    print(f"Correo enviado a {RECIPIENT_EMAIL} (con PDF adjunto).")
    # No se borra pdf_path aquí a propósito: el workflow lo sube como artifact para poder
    # inspeccionar el PDF real de cada ejecución sin depender de abrir el email. El runner de
    # GitHub Actions es efímero, así que no hace falta limpiarlo manualmente.


if __name__ == "__main__":
    main()
