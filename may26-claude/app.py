import streamlit as st
import anthropic
import base64
import os
import json
import re
from datetime import datetime
import pandas as pd
from streamlit_gsheets import GSheetsConnection
import requests
from bs4 import BeautifulSoup

conn = st.connection("gsheets", type=GSheetsConnection)

# --- Configuración de la página ---
st.set_page_config(page_title="Asistente del Curso", page_icon="🤖")

# --- Configuración de la API de Anthropic ---
try:
    api_key = st.secrets["ANTHROPIC_API_KEY"]
    client = anthropic.Anthropic(api_key=api_key)
except KeyError:
    st.error("API Key de Anthropic no encontrada. Configúrala en .streamlit/secrets.toml como ANTHROPIC_API_KEY.")
    st.stop()
except Exception as e:
    st.error(f"Error al configurar la API: {e}")
    st.stop()

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8096

LOG_FILENAME = "registro_accesos.csv"
CONVERSATIONS_DIR = "conversations"
os.makedirs(CONVERSATIONS_DIR, exist_ok=True)

REGLA_FORMATO = """
IMPORTANTE (REGLA DE FORMATO):
- NO utilices comandos de estructura de documento LaTeX (\\documentclass, \\begin{document}, \\section, etc.).
- Escribe tu respuesta en Markdown normal y limpio.
- Encierra las fórmulas matemáticas EXCLUSIVAMENTE entre signos de dólar ($...$ para línea y $$...$$ para bloques).
- No generes un archivo .tex, genera una respuesta de chat legible.
"""


# --- Google Sheets ---
def guardar_en_sheets(nombre, email, codigo, tipo="Acceso", pregunta="", respuesta=""):
    try:
        existing_data = conn.read(worksheet="Registro", ttl=0)
        new_row = pd.DataFrame([{
            "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Nombre": nombre,
            "Email": email,
            "Codigo_Asignatura": codigo,
            "Tipo_Entrada": tipo,
            "Pregunta": pregunta,
            "Respuesta": respuesta
        }])
        updated_df = pd.concat([existing_data, new_row], ignore_index=True)
        conn.update(worksheet="Registro", data=updated_df)
    except Exception:
        st.sidebar.warning("No se pudo sincronizar el registro.")


# --- Web scraping UMH ---
def extraer_texto_umh(url):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        return soup.get_text(separator=' ', strip=True)
    except Exception as e:
        return f"Error al leer la web: {e}"


# --- Utilidades de conversación ---
def get_user_filepath(email, mode):
    safe_email = re.sub(r'[^a-zA-Z0-9]', '_', email)
    safe_mode = re.sub(r'[^a-zA-Z0-9]', '_', mode).lower()
    return os.path.join(CONVERSATIONS_DIR, f"{safe_email}_{safe_mode}.json")

def load_conversation(email, mode):
    filepath = get_user_filepath(email, mode)
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_conversation(email, mode, messages):
    filepath = get_user_filepath(email, mode)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(messages, f, indent=4, ensure_ascii=False)

def guardar_registro(nombre, email, codigo):
    if not os.path.exists(LOG_FILENAME):
        with open(LOG_FILENAME, "w", encoding="utf-8") as f:
            f.write("Fecha_Hora,Nombre,Email,Codigo_Asignatura\n")
    with open(LOG_FILENAME, "a", encoding="utf-8") as f:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{timestamp},{nombre},{email},{codigo}\n")


# --- Utilidades de Claude ---
def load_pdf_block(pdf_path):
    """Lee un PDF y lo devuelve como bloque de contenido para Claude."""
    if not os.path.exists(pdf_path):
        st.error(f"Archivo no encontrado: '{pdf_path}'")
        st.stop()
    with open(pdf_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": data},
        "cache_control": {"type": "ephemeral"},
    }

def read_excel(uploaded_file):
    """Lee un Excel subido y devuelve su contenido como texto estructurado."""
    try:
        xls = pd.ExcelFile(uploaded_file)
        parts = []
        for sheet in xls.sheet_names:
            df = pd.read_excel(xls, sheet_name=sheet).dropna(how="all").fillna("")
            parts.append(f"**Hoja: {sheet}**\n{df.to_markdown(index=False)}")
        return "\n\n".join(parts)
    except Exception as e:
        st.error(f"Error al leer el Excel: {e}")
        return None

def iniciar_modo(mode_key, files_to_load, system_prompt, extra_text=""):
    """Carga los PDFs y prepara el modo. Equivalente a iniciar_chat() de Gemini."""
    system = system_prompt
    if extra_text:
        system += f"\n\nINFORMACIÓN ADICIONAL DE LA WEB OFICIAL:\n{extra_text}"

    with st.spinner("Cargando documentos del curso..."):
        pdf_blocks = [load_pdf_block(f) for f in files_to_load]

    st.session_state.pdf_blocks[mode_key] = pdf_blocks
    st.session_state.system_prompts[mode_key] = system
    st.session_state.api_messages[mode_key] = []
    st.session_state.chat = True

def enviar_a_claude(mode_key, user_text):
    """Envía un mensaje a Claude y devuelve la respuesta."""
    api_msgs = st.session_state.api_messages[mode_key]
    pdf_blocks = st.session_state.pdf_blocks[mode_key]
    system_prompt = st.session_state.system_prompts[mode_key]

    if not api_msgs:
        user_content = pdf_blocks + [{"type": "text", "text": user_text}]
    else:
        user_content = [{"type": "text", "text": user_text}]

    api_msgs.append({"role": "user", "content": user_content})

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=api_msgs,
        betas=["pdfs-2024-09-25"],
    )

    result = response.content[0].text
    api_msgs.append({"role": "assistant", "content": result})
    return result


# --- Inicialización del estado de sesión ---
_defaults = {
    "autenticado": False,
    "email": "",
    "nombre": "",
    "codigo": "",
    "mode": None,
    "sub_mode": None,
    "chat": None,
    "histories": {},
    "api_messages": {},
    "system_prompts": {},
    "pdf_blocks": {},
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ============================================================
# PASO 1: AUTENTICACIÓN
# ============================================================
if not st.session_state.autenticado:
    st.sidebar.title("Identificación")
    nombre_usuario = st.sidebar.text_input("Tu nombre")
    email_usuario = st.sidebar.text_input("Tu correo electrónico")
    codigo_asignatura = st.sidebar.text_input("Código de la Asignatura")

    if st.sidebar.button("Entrar"):
        if nombre_usuario and email_usuario and codigo_asignatura:
            if codigo_asignatura in ["1500", "2165", "0000"]:
                st.session_state.nombre = nombre_usuario
                st.session_state.email = email_usuario
                st.session_state.codigo = codigo_asignatura
                st.session_state.histories = {
                    "Guía Docente": load_conversation(email_usuario, "Guía Docente"),
                    "Temario del Curso": load_conversation(email_usuario, "Temario del Curso"),
                    "A Mano": load_conversation(email_usuario, "A Mano"),
                    "Con Excel": load_conversation(email_usuario, "Con Excel"),
                }
                st.session_state.autenticado = True
                guardar_en_sheets(nombre_usuario, email_usuario, codigo_asignatura, tipo="Login")
                st.rerun()
            else:
                st.sidebar.error("El código de la asignatura es incorrecto.")
        else:
            st.sidebar.warning("Por favor, completa todos los campos.")

# ============================================================
# PASO 2: APLICACIÓN PRINCIPAL
# ============================================================
else:
    def change_mode():
        st.session_state.chat = None
        st.session_state.sub_mode = None

    st.sidebar.title("Modo de Consulta")
    st.session_state.mode = st.sidebar.radio(
        "Elige sobre qué quieres preguntar:",
        ("Guía Docente", "Temario del Curso", "Resolver Ejercicios"),
        index=None,
        on_change=change_mode,
    )

    st.title("🤖 Asistente de la Asignatura")

    info_examen = ""
    if st.session_state.codigo == "2165":
        info_examen = "El examen de la asignatura 2165 es el Lunes, 1 de junio de 09:00 a 12:00."
    elif st.session_state.codigo == "1500":
        info_examen = "El examen de la asignatura 1500 es el Viernes, 5 de junio de 09:30 a 12:30."

    current_mode_key = st.session_state.mode

    # ── GUÍA DOCENTE ──────────────────────────────────────────────────────────
    if st.session_state.mode == "Guía Docente":
        st.write("Preguntas sobre la **Guía Docente (PDF)** y la **Web Oficial de la UMH**.")

        if st.session_state.chat is None:
            url_umh = "https://www.umh.es/contenido/Estudios/:asi_g_2165_R1/datos_es.html"
            with st.spinner("Sincronizando con la web de la UMH..."):
                contenido_web = extraer_texto_umh(url_umh)

            iniciar_modo(
                "Guía Docente",
                files_to_load=["guia_docente.pdf"],
                system_prompt=(
                    f"Eres un asistente académico.\n"
                    f"DATOS IMPORTANTES DE EXAMEN: {info_examen}\n"
                    f"Utiliza el PDF adjunto y la información adicional de la web de la UMH para responder.\n"
                    f"Si el alumno pregunta por la fecha del examen, DEBES dar la fecha que aparece en 'DATOS IMPORTANTES'.\n"
                    f"Formatea todas las fórmulas matemáticas en LaTeX ($...$ o $$...$$)."
                ),
                extra_text=contenido_web,
            )

            if not st.session_state.histories["Guía Docente"]:
                st.session_state.histories["Guía Docente"].append({
                    "role": "assistant",
                    "content": "Hola. He analizado la guía docente y la página de la UMH. ¿Quieres saber algo sobre el profesorado, la evaluación o el temario?"
                })

    # ── TEMARIO DEL CURSO ─────────────────────────────────────────────────────
    elif st.session_state.mode == "Temario del Curso":
        current_mode_key = "Temario del Curso"
        st.write("Haz preguntas **sobre los temas de la asignatura**.")
        st.warning("📄 **Importante:** Los documentos cargados son resúmenes.")

        if st.session_state.chat is None:
            iniciar_modo(
                "Temario del Curso",
                files_to_load=["tema_1.pdf", "tema_2.pdf", "tema_3.pdf", "tema_4.pdf", "tema_5.pdf"],
                system_prompt=(
                    "Tu tarea sigue un proceso estricto en dos pasos.\n"
                    "Instrucción clave: Cuando escribas fórmulas matemáticas, utiliza siempre LaTeX "
                    "($...$ para fórmulas en línea y $$...$$ para bloques).\n\n"
                    "1. **Prioridad Máxima:** Busca la respuesta ÚNICA Y EXCLUSIVAMENTE en los 5 temas adjuntos.\n"
                    "2. **Plan B:** Si la respuesta NO está en los temas, puedes usar conocimiento general, pero DEBES "
                    "comenzar con: '**⚠️ Aviso: Esta información no se encuentra en los apuntes oficiales. "
                    "La siguiente respuesta se basa en conocimiento general y debes corroborarla.**'\n"
                    "3. Si no encuentras la información, indícalo claramente."
                ),
            )
            if not st.session_state.histories["Temario del Curso"]:
                st.session_state.histories["Temario del Curso"].append({
                    "role": "assistant",
                    "content": "Hola, he estudiado los resúmenes de los temas. ¿Sobre qué tienes dudas?"
                })

    # ── RESOLVER EJERCICIOS ───────────────────────────────────────────────────
    elif st.session_state.mode == "Resolver Ejercicios":
        st.write("Introduce los datos del ejercicio y especifica qué necesitas resolver.")

        tipo_ejercicio = st.radio(
            "Selecciona el tipo de ejercicio:",
            ["Proyecto de Inversión", "Bono Financiero"],
            horizontal=True
        )

        st.subheader("1. Introduce los Datos del Ejercicio")

        if tipo_ejercicio == "Proyecto de Inversión":
            excel_file = st.file_uploader(
                "📂 Sube un Excel con los datos (opcional — si lo subes no hace falta rellenar la tabla)",
                type=["xlsx", "xls"],
                help="El asistente leerá el Excel directamente."
            )
            if excel_file:
                excel_context = read_excel(excel_file)
                if excel_context:
                    st.success("✅ Excel leído correctamente.")
                    with st.expander("Ver datos importados"):
                        st.markdown(excel_context)
                else:
                    excel_context = None
                investment = discount_rate = edited_df = None
            else:
                excel_context = None
                col1, col2 = st.columns(2)
                investment = col1.number_input("Inversión Inicial (€)", value=100000, step=1000)
                discount_rate = col2.number_input("Tasa de Descuento (%)", value=17.0, step=0.5)
                st.text("Flujos de Caja Anuales (€)")
                df_default = pd.DataFrame([
                    {"Año": 1, "Proyecto A": 60000, "Proyecto B": 121000},
                    {"Año": 2, "Proyecto A": 72000, "Proyecto B": 0},
                ])
                edited_df = st.data_editor(df_default, num_rows="dynamic", key="cashflow_data")

        else:
            excel_context = None
            col_b1, col_b2 = st.columns(2)
            nominal = col_b1.number_input("Valor Nominal (€)", value=1000)
            cupon_p = col_b2.number_input("Cupón Anual (%)", value=5.0)
            precio_c = st.number_input("Precio de Adquisición (€)", value=950)
            plazo_b = st.slider("Años hasta vencimiento", 1, 30, 5)
            investment = discount_rate = edited_df = None

        st.subheader("2. ¿Qué necesitas que resuelva?")
        if tipo_ejercicio == "Proyecto de Inversión":
            default_instr = "Calcula el VAN, la TIR y el Payback Descontado de ambos proyectos y razona cuál es la mejor opción."
        else:
            default_instr = "Calcula la TIR del bono desglosando la rentabilidad explícita e implícita."
        user_instruction = st.text_area("Instrucciones para el asistente:", value=default_instr)

        st.subheader("3. Elige un Método de Resolución")
        col_mano, col_excel_btn = st.columns(2)

        if tipo_ejercicio == "Proyecto de Inversión":
            solve_mano_button = col_mano.button("✍️ Resolver a Mano")
            solve_excel_button = col_excel_btn.button("📊 Resolver con Excel")
        else:
            solve_mano_button = col_mano.button("✍️ Resolver a Mano")
            col_excel_btn.info("📊 Opción Excel no disponible para bonos temporalmente.")
            solve_excel_button = False

        def build_prompt(method_name, instruction):
            prompt = (
                f"Por favor, usando la metodología '{method_name}' y basándote en los siguientes datos, "
                f"realiza esta tarea: '{instruction}'.\n\n"
            )
            if tipo_ejercicio == "Proyecto de Inversión":
                if excel_context:
                    prompt += f"Datos importados del Excel:\n\n{excel_context}"
                else:
                    cash_flows = edited_df.loc[:, (edited_df != 0).any(axis=0)].to_string(index=False)
                    prompt += (
                        f"Datos del ejercicio:\n"
                        f"- Inversión Inicial: {investment}€\n"
                        f"- Tasa de Descuento: {discount_rate}%\n"
                        f"- Flujos de Caja:\n{cash_flows}"
                    )
            else:
                prompt += (
                    f"Datos del Bono:\n"
                    f"- Valor Nominal: {nominal}€\n"
                    f"- Cupón: {cupon_p}%\n"
                    f"- Precio de Adquisición: {precio_c}€\n"
                    f"- Plazo: {plazo_b} años\n"
                )
            return prompt

        prompt_text = None

        if solve_mano_button:
            current_mode_key = "A Mano"
            st.session_state.sub_mode = "A Mano"
            if tipo_ejercicio == "Proyecto de Inversión":
                archivos = ["tema_1.pdf", "tema_2.pdf", "ejercicos_resueltos_a_mano_tema_2.pdf"]
                sys_msg = (
                    "Eres un tutor experto que resuelve ejercicios de dirección financiera a mano. "
                    "Tu referencia principal es el documento de ejercicios resueltos adjunto. "
                    "DEBES imitar su metodología, estilo y formato de manera estricta.\n"
                    "**Reglas:**\n"
                    "1. Sigue los pasos exactos del documento para cada cálculo.\n"
                    "2. **Payback:** El resultado es el período entero en que el saldo se vuelve positivo. No calcules decimales.\n"
                    "3. **Decisión final:** Usa siempre Fase 1 (Viabilidad) y Fase 2 (Comparación).\n"
                    "4. **Conflicto VAN/TIR:** El VAN es el criterio decisivo en proyectos mutuamente excluyentes."
                )
            else:
                archivos = ["tema_3.pdf", "ejercicos_resueltos_a_mano_tema_3.pdf"]
                sys_msg = (
                    "Eres experto en bonos financieros. "
                    "Desglosa SIEMPRE la Rentabilidad EXPLÍCITA e IMPLÍCITA siguiendo el documento de referencia adjunto."
                )
            iniciar_modo("A Mano", archivos, sys_msg + REGLA_FORMATO)
            prompt_text = build_prompt("A Mano", user_instruction)

        if solve_excel_button:
            current_mode_key = "Con Excel"
            st.session_state.sub_mode = "Con Excel"
            archivos = ["tema_1.pdf", "tema_2.pdf", "ejercicos_resueltos_excel_tema_2.pdf"]
            sys_msg = (
                "Eres un tutor experto que enseña a resolver ejercicios con Excel. "
                "Tu referencia es el documento adjunto.\n"
                "**Reglas:**\n"
                "1. Explica la solución estructurando datos en celdas con fórmulas de Excel (=VNA, =TIR, etc.).\n"
                "2. **Decisión final:** Usa siempre Fase 1 (Viabilidad) y Fase 2 (Comparación).\n"
                "3. **Conflicto VAN/TIR:** El VAN es el criterio decisivo."
            )
            iniciar_modo("Con Excel", archivos, sys_msg + REGLA_FORMATO)
            prompt_text = build_prompt("Con Excel", user_instruction)

        if prompt_text:
            history = st.session_state.histories[current_mode_key]
            if excel_context:
                datos_h = "(datos importados desde Excel)"
            elif tipo_ejercicio == "Proyecto de Inversión":
                datos_h = edited_df.to_string(index=False)
            else:
                datos_h = f"Nominal: {nominal}€, Precio: {precio_c}€, Cupón: {cupon_p}%, Plazo: {plazo_b} años"

            user_question = f"**Tarea:** {user_instruction}\n\n**Datos:**\n{datos_h}"
            history.append({"role": "user", "content": user_question})

            with st.spinner("Generando resolución paso a paso..."):
                try:
                    assistant_response = enviar_a_claude(current_mode_key, prompt_text)
                    guardar_en_sheets(
                        st.session_state.nombre, st.session_state.email, st.session_state.codigo,
                        tipo=f"Ejercicio: {current_mode_key}", pregunta=user_instruction, respuesta=assistant_response
                    )
                    history.append({"role": "assistant", "content": assistant_response})
                    save_conversation(st.session_state.email, current_mode_key, history)
                except Exception as e:
                    st.error(f"Error al procesar la petición: {e}")
                    history.pop()
            st.rerun()

        st.markdown("---")
        st.subheader("📁 Archivo de Ejercicios Guardados")

        if st.session_state.sub_mode:
            current_mode_key = st.session_state.sub_mode
            historial = st.session_state.histories.get(current_mode_key, [])
            for i in range(len(historial) - 2, -1, -2):
                pregunta = historial[i]["content"]
                respuesta = historial[i + 1]["content"]
                titulo = pregunta.split('\n')[0].replace('**Tarea:**', '').strip()
                with st.expander(f"📋 Ejercicio: {titulo[:60]}..."):
                    st.info("**Tu consulta original:**")
                    st.write(pregunta)
                    st.markdown("---")
                    st.success("**Resolución del Asistente:**")
                    st.markdown(respuesta)

    # ── CHAT UNIFICADO (Guía Docente y Temario) ───────────────────────────────
    if current_mode_key and st.session_state.mode in ["Guía Docente", "Temario del Curso"]:
        st.info("💡 **Aviso General:** Soy un asistente virtual y puedo cometer errores. Verifica siempre la información importante.")
        history = st.session_state.histories[current_mode_key]

        for message in history:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

        if prompt := st.chat_input(f"Pregunta sobre {current_mode_key}..."):
            history.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)

            with st.spinner("Pensando..."):
                try:
                    assistant_response = enviar_a_claude(current_mode_key, prompt)
                    guardar_en_sheets(
                        st.session_state.nombre, st.session_state.email, st.session_state.codigo,
                        tipo=f"Consulta: {current_mode_key}", pregunta=prompt, respuesta=assistant_response
                    )
                    history.append({"role": "assistant", "content": assistant_response})
                    save_conversation(st.session_state.email, current_mode_key, history)
                except Exception as e:
                    st.error(f"Error al procesar tu pregunta: {e}")
                    history.pop()
            st.rerun()

    if not st.session_state.mode:
        st.info("Por favor, selecciona un modo de consulta en la barra lateral para comenzar.")
