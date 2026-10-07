"""
Genera el PDF semanal de entrenamiento.

Formato:
tarjetas de color colocadas en una cuadrícula con FILAS FIJAS POR DISCIPLINA
(1 Gimnasio, 2 Carrera, 3 Ciclismo, 4 Natación) y una columna por día. Cada
disciplina cae siempre en la misma fila para poder ver de un vistazo qué días
toca cada una; una celda vacía significa que ese día no hay sesión de esa
disciplina. Las sesiones ya completadas van en verde, no en el color de su
disciplina.

No depende de un navegador (a diferencia del método con Chrome headless que se
usa en las conversaciones): en el entorno de GitHub Actions no hay Chrome y
añadirlo complicaría el workflow sin necesidad.
"""

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

# Un color propio por disciplina — nunca compartido entre dos (natación y ciclismo
# compartieron azul por error hasta 2026-08-08).
DISCIPLINA_COLOR = {
    "Gimnasio": ("#fff6d6", "#8a6d00"),
    "Carrera": ("#fde4cf", "#b34700"),
    "Ciclismo": ("#ede0fa", "#6a3fa0"),
    "Natación": ("#dcecfb", "#1565c0"),
    "Transición": ("#fde4cf", "#b34700"),
    "Descanso": ("#f0f0f0", "#666666"),
}

HECHO_COLOR = ("#d9f2d9", "#1b7a1b")

# El orden de estas filas es parte del formato acordado con el atleta, no un detalle
# cosmético: siempre Gimnasio, Carrera, Ciclismo, Natación.
FILAS_DISCIPLINA = ["Gimnasio", "Carrera", "Ciclismo", "Natación"]

# Transición/Brick no tiene fila propia: se reparte entre Carrera y Ciclismo.
FILA_DE_TIPO = {
    "Gimnasio": "Gimnasio",
    "Carrera": "Carrera",
    "Transición": "Carrera",
    "Ciclismo": "Ciclismo",
    "Natación": "Natación",
}

DIAS_ORDEN = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def _card(tipo: str, texto: str, ancho: float, hecho: bool = False) -> Table:
    bg, fg = HECHO_COLOR if hecho else DISCIPLINA_COLOR.get(tipo, ("#eeeeee", "#333333"))
    base_style = ParagraphStyle(
        "card", fontName="Helvetica", fontSize=8.5, leading=11, textColor=HexColor("#222222")
    )
    if tipo == "Descanso":
        style = ParagraphStyle("rest", parent=base_style, alignment=TA_CENTER, textColor=HexColor(fg))
        p = Paragraph(f"<i>{texto}</i>", style)
    else:
        p = Paragraph(f'<b><font color="{fg}">{tipo.upper()}</font></b><br/>{texto}', base_style)

    t = Table([[p]], colWidths=[ancho])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), HexColor(bg)),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return t


def build_week_pdf(
    output_path: str,
    titulo: str,
    subtitulo: str,
    sesiones_por_dia: dict,
    pie_texto: str = "",
) -> None:
    """sesiones_por_dia: {"Lunes": [{"tipo": "Natación", "texto": "...", "hecho": False}], ...}"""
    doc = BaseDocTemplate(
        output_path,
        pagesize=landscape(A4),
        topMargin=14 * mm,
        bottomMargin=10 * mm,
        leftMargin=10 * mm,
        rightMargin=10 * mm,
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame])])

    story = [
        Paragraph(
            titulo,
            ParagraphStyle(
                "h1",
                fontName="Helvetica-Bold",
                fontSize=17,
                leading=21,
                spaceAfter=4,
                textColor=HexColor("#1a56c4"),
            ),
        ),
        Paragraph(
            subtitulo,
            ParagraphStyle(
                "sub",
                fontName="Helvetica",
                fontSize=9.5,
                leading=13,
                spaceAfter=10,
                textColor=HexColor("#555555"),
            ),
        ),
    ]

    # Normaliza las claves recibidas (quita espacios sobrantes) para que un día no se pierda por
    # un detalle de formato — la validación fuerte de verdad vive en weekly_checkin.py (Literal +
    # comprobación de los 7 días antes de llegar aquí); esto es solo una red de seguridad extra.
    sesiones_por_dia_normalizado = {dia.strip(): sesiones for dia, sesiones in sesiones_por_dia.items()}

    if not any(sesiones_por_dia_normalizado.get(dia) for dia in DIAS_ORDEN):
        raise RuntimeError(
            "build_week_pdf: ningún día de sesiones_por_dia coincide con "
            f"{DIAS_ORDEN} (claves recibidas: {sorted(sesiones_por_dia.keys())}). "
            "Esto generaría un PDF con todos los días en blanco — se aborta en vez de crearlo."
        )

    label_width = doc.width * 0.09
    col_width = (doc.width - label_width) / 7.0 - 2

    header_row = [""]
    for dia in DIAS_ORDEN:
        header_row.append(
            Paragraph(
                f"<b>{dia.upper()}</b>",
                ParagraphStyle("dh", fontName="Helvetica-Bold", fontSize=9, leading=11),
            )
        )

    # Un día marcado como descanso ocupa las 4 filas de disciplina con una sola tarjeta.
    dias_descanso = {
        dia
        for dia in DIAS_ORDEN
        if any(s["tipo"] == "Descanso" for s in sesiones_por_dia_normalizado.get(dia, []))
    }

    filas = [header_row]
    span_commands = []
    for idx_fila, disciplina in enumerate(FILAS_DISCIPLINA, start=1):
        bg, fg = DISCIPLINA_COLOR[disciplina]
        fila = [
            Paragraph(
                f'<b><font color="{fg}">{disciplina.upper()}</font></b>',
                ParagraphStyle("rl", fontName="Helvetica-Bold", fontSize=8, leading=10),
            )
        ]
        for idx_col, dia in enumerate(DIAS_ORDEN, start=1):
            if dia in dias_descanso:
                if idx_fila == 1:
                    fila.append(_card("Descanso", "Descanso total.<br/>Sin entrenamiento.", col_width))
                    span_commands.append(("SPAN", (idx_col, 1), (idx_col, len(FILAS_DISCIPLINA))))
                else:
                    fila.append("")
                continue

            del_dia = [
                s
                for s in sesiones_por_dia_normalizado.get(dia, [])
                if FILA_DE_TIPO.get(s["tipo"]) == disciplina
            ]
            if not del_dia:
                fila.append("")
                continue

            cell = []
            for s in del_dia:
                cell.append(_card(s["tipo"], s["texto"], col_width, hecho=bool(s.get("hecho"))))
                cell.append(Spacer(1, 4))
            fila.append(cell)
        filas.append(fila)

    grid = Table(filas, colWidths=[label_width] + [col_width] * 7)
    grid.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("VALIGN", (0, 1), (0, -1), "MIDDLE"),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("TOPPADDING", (0, 1), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
            ]
            + span_commands
        )
    )
    story.append(grid)

    if pie_texto:
        story.append(Spacer(1, 10))
        story.append(
            Paragraph(
                pie_texto,
                ParagraphStyle(
                    "foot", fontName="Helvetica", fontSize=7, leading=9.5, textColor=HexColor("#777777")
                ),
            )
        )

    doc.build(story)
