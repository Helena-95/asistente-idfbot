import streamlit as st
import google.generativeai as genai
import os
import json
import re
from datetime import datetime
import pandas as pd
from streamlit_gsheets import GSheetsConnection
import requests
from bs4 import BeautifulSoup

conn = st.connection("gsheets", type=GSheetsConnection)

# --- Configuración de la página de Streamlit ---
st.set_page_config(
    page_title="Asistente del Curso",
    page_icon="🤖"
)

# --- Configuración de la API de Gemini ---
try:
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
except KeyError:
    st.error("API Key de Gemini no encontrada. Asegúrate de configurarla en los secretos.")
    st.stop()
except Exception as e:
    st.error(f"Error al configurar la API: {e}")
    st.stop()

# --- Constantes y configuración de directorios ---
LOG_FILENAME = "registro_accesos.csv"
CONVERSATIONS_DIR = "conversations"
os.makedirs(CONVERSATIONS_DIR, exist_ok=True)


def guardar_en_sheets(nombre, email, codigo, tipo="Acceso", pregunta="", respuesta=""):
    try:
        # Intentar leer y actualizar
        existing_data = conn.read(worksheet="Registro")
        
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
    except Exception as e:
        # Si falla, la app sigue funcionando y solo muestra un aviso en el lateral
        st.sidebar.warning("No se pudo sincronizar el registro con la nube.")
        print(f"Error en Sheets: {e}")

def extraer_texto_umh(url):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        # Forzamos la codificación para que se vean bien las tildes y la 'ñ'
        response.encoding = 'utf-8' 
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # En la web de la UMH, el contenido principal suele estar en capas específicas
        # Extraemos el texto y limpiamos espacios
        texto = soup.get_text(separator=' ', strip=True)
        return texto
    except Exception as e:
        return f"Error al leer la web: {e}"

# --- Funciones de utilidad ---
def get_user_filepath(email, mode):
    safe_email = re.sub(r'[^a-zA-Z0-9]', '_', email)
    safe_mode = re.sub(r'[^a-zA-Z0-9]', '_', mode).lower()
    return os.path.join(CONVERSATIONS_DIR, f"{safe_email}_{safe_mode}.json")

def load_conversation(email, mode):
    filepath = get_user_filepath(email, mode)
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            return json.load(f)
    return []

def save_conversation(email, mode, messages):
    filepath = get_user_filepath(email, mode)
    with open(filepath, "w") as f:
        json.dump(messages, f, indent=4)

def guardar_registro(nombre, email, codigo):
    if not os.path.exists(LOG_FILENAME):
        with open(LOG_FILENAME, "w") as f:
            f.write("Fecha_Hora,Nombre,Email,Codigo_Asignatura\n")
    with open(LOG_FILENAME, "a") as f:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{timestamp},{nombre},{email},{codigo}\n")


# --- Inicialización del estado de la sesión ---
if "autenticado" not in st.session_state:
    st.session_state.autenticado = False
if "chat" not in st.session_state:
    st.session_state.chat = None
if "histories" not in st.session_state:
    st.session_state.histories = {}
if "email" not in st.session_state:
    st.session_state.email = ""
if "mode" not in st.session_state:
    st.session_state.mode = None
if "sub_mode" not in st.session_state: 
    st.session_state.sub_mode = None

# --- LÓGICA DE LA APLICACIÓN ---

# Paso 1: Autenticación del usuario
if not st.session_state.autenticado:
    st.sidebar.title("Identificación")
    nombre_usuario = st.sidebar.text_input("Tu nombre")
    email_usuario = st.sidebar.text_input("Tu correo electrónico")
    codigo_asignatura = st.sidebar.text_input("Código de la Asignatura")
    
    if st.sidebar.button("Entrar"):
        if nombre_usuario and email_usuario and codigo_asignatura:
            if codigo_asignatura in ["1500", "2165"]:
                st.session_state.nombre = nombre_usuario
                st.session_state.email = email_usuario
                st.session_state.codigo = codigo_asignatura
                st.session_state.histories = {
                "Guía Docente": load_conversation(email_usuario, "Guía Docente"),
                "Temario del Curso": load_conversation(email_usuario, "Temario del Curso"),
                "A Mano": load_conversation(email_usuario, "A Mano"),
                "Con Excel": load_conversation(email_usuario, "Con Excel")
                }
                st.session_state.mode = None
                st.session_state.chat = None
                st.session_state.autenticado = True
                guardar_en_sheets(nombre_usuario, email_usuario, codigo_asignatura, tipo="Login")
            
                st.rerun()
            else:
                st.sidebar.error("El código de la asignatura es incorrecto.")
        else:
            st.sidebar.warning("Por favor, completa todos los campos.")

# Paso 2: Si está autenticado, mostrar los selectores de modo
elif st.session_state.autenticado:
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

    def iniciar_chat(files_to_load, system_prompt, extra_text=""):
    # El prompt ahora incluye el texto de la web si existe
    instrucciones = system_prompt["text"]
    if extra_text:
        instrucciones += f"\n\nINFORMACIÓN ADICIONAL DE LA WEB OFICIAL:\n{extra_text}"
    
    prompt_parts = [{"text": instrucciones}]
    
    for pdf_path in files_to_load:
        try:
            with open(pdf_path, "rb") as f:
                prompt_parts.append({"mime_type": "application/pdf", "data": f.read()})
        except FileNotFoundError:
            st.error(f"Falta el archivo: {pdf_path}")
            st.stop()
    
    try:
        # Usamos 1.5-pro para que pueda procesar bien toda la información mezclada
        model = genai.GenerativeModel("gemini-1.5-pro")
        st.session_state.chat = model.start_chat(
            history=[{"role": "user", "parts": prompt_parts},
                     {"role": "model", "parts": [{"text": "He analizado el PDF y la web de la UMH. Estoy listo para tus dudas."}]}]
        )
    except Exception as e:
        st.error(f"Error en Gemini: {e}")
        st.stop()
    
    # --- Lógica de Modos ---
    
    current_mode_key = st.session_state.mode
    
    if st.session_state.mode == "Guía Docente":
        st.write("Preguntas sobre la **Guía Docente (PDF)** y la **Web Oficial de la UMH**.")
        
        if st.session_state.chat is None:
            url_umh = "https://www.umh.es/contenido/Estudios/:asi_g_2165_R1/datos_es.html"
            
            with st.spinner("Sincronizando con la web de la UMH..."):
                contenido_web = extraer_texto_umh(url_umh)
            
            iniciar_chat(
                files_to_load=["guia_docente.pdf"],
                system_prompt={"text": "Eres un asistente académico. Utiliza el PDF adjunto y el texto de la web de la UMH para responder. Si la información varía, da prioridad al PDF pero menciona lo que dice la web."},
                extra_text=contenido_web
            )
            
            if not st.session_state.histories["Guía Docente"]:
                 st.session_state.histories["Guía Docente"].append({
                     "role": "assistant", 
                     "content": "Hola. He analizado la guía docente y la página de la UMH. ¿Quieres saber algo sobre el profesorado, la evaluación o el temario?"
                 })

    elif st.session_state.mode == "Temario del Curso":
        current_mode_key = "Temario del Curso"
        st.write("Haz preguntas **sobre los temas de la asignatura**.")
        st.warning("📄 **Importante:** Los documentos cargados son resúmenes.")
        if st.session_state.chat is None:
            iniciar_chat(
                files_to_load=["tema_1.pdf", "tema_2.pdf", "tema_3.pdf", "tema_4.pdf", "tema_5.pdf"],
                system_prompt={ "text": """Tu tarea como asistente de la asignatura sigue un proceso estricto en dos pasos. **Instrucción clave: Cuando escribas fórmulas matemáticas, utiliza siempre la sintaxis de LaTeX, encerrando las fórmulas entre signos de dólar ($...$ para fórmulas en línea y $$...$$ para bloques de fórmulas).**
                    1. **Prioridad Máxima: Contenido del Curso.** Primero, busca la respuesta a la pregunta del usuario utilizando ÚNICA Y EXCLUSIVAMENTE el contenido de los 5 temas que te he proporcionado.
                    2. **Plan B: Conocimiento General.** Si, y solo si, la respuesta NO se encuentra en los 5 temas, puedes usar tu conocimiento general para responder. Cuando lo hagas, DEBES OBLIGATORIAMENTE empezar tu respuesta con el siguiente aviso: '**⚠️ Aviso: Esta información no se encuentra en los apuntes oficiales de la asignatura. La siguiente respuesta se basa en conocimiento general y debes corroborarla.**'
                    3. Si no encuentras la información, simplemente indica que no puedes responder."""
                }
            )
            if not st.session_state.histories[current_mode_key]:
                 st.session_state.histories[current_mode_key].append({"role": "assistant", "content": "Hola, he estudiado los resúmenes de los temas. ¿Sobre qué tienes dudas?"})

    elif st.session_state.mode == "Resolver Ejercicios":
        st.write("Introduce los datos del ejercicio y especifica qué necesitas resolver.")
        
        st.subheader("1. Introduce los Datos del Ejercicio")
        # ... (código del formulario de ejercicios sin cambios) ...
        col1, col2 = st.columns(2)
        investment = col1.number_input("Inversión Inicial (€)", value=100000, step=1000)
        discount_rate = col2.number_input("Tasa de Descuento (%)", value=17.0, step=0.5)

        st.text("Flujos de Caja Anuales (€)")
        df = pd.DataFrame([{"Año": 1, "Proyecto A": 60000, "Proyecto B": 121000}, {"Año": 2, "Proyecto A": 72000, "Proyecto B": 0}])
        edited_df = st.data_editor(df, num_rows="dynamic", key="cashflow_data")
        
        st.subheader("2. ¿Qué necesitas que resuelva?")
        user_instruction = st.text_area("Instrucciones para el asistente:", value="Calcula el VAN, la TIR y el Payback Descontado de ambos proyectos y razona cuál es la mejor opción.")
        
        st.subheader("3. Elige un Método de Resolución")
        col_mano, col_excel = st.columns(2)
        solve_mano_button = col_mano.button("✍️ Resolver a Mano")
        solve_excel_button = col_excel.button("📊 Resolver con Excel")

        def build_prompt(method_name, instruction):
            prompt = f"Por favor, usando tu metodología de '{method_name}' y basándote en los siguientes datos, realiza esta tarea específica: '{instruction}'.\n\n"
            prompt += f"Datos del ejercicio:\n- Inversión Inicial: {investment}€\n- Tasa de Descuento: {discount_rate}%\n"
            prompt += "Los flujos de caja son:\n" + edited_df.loc[:, (edited_df != 0).any(axis=0)].to_string(index=False)
            return prompt

        prompt_text = None
        if solve_mano_button:
            current_mode_key = "A Mano"
            st.session_state.sub_mode = current_mode_key
            iniciar_chat(
                files_to_load=["tema_1.pdf", "tema_2.pdf", "ejercicos_resueltos_a_mano_tema_2.pdf"],
                system_prompt={"text": """Eres un tutor experto que resuelve ejercicios a mano. Tu modelo principal es 'ejercicos_resueltos_a_mano_tema_2.pdf'. DEBES imitar su metodología, estilo y formato de manera estricta.
                    **Reglas Clave:**
                    1.  **Metodología:** Sigue los pasos exactos del documento de ejemplos para cada cálculo.
                    2.  **Payback:** Tu conclusión para el Payback debe ser el número del período entero en el que el saldo se vuelve positivo. NO calcules la fracción decimal del año.
                    3.  **Decisión Final:** Cuando se pida seleccionar un proyecto, DEBES seguir la estructura de dos fases de los ejemplos: **Fase 1 (Viabilidad)** y **Fase 2 (Comparación)** para cada criterio (VAN, TIR, etc.).
                    4.  **Conflicto VAN/TIR:** Si los criterios VAN y TIR son contradictorios, señálalo y concluye que el criterio del VAN es el decisivo para proyectos mutuamente excluyentes.
                    5.  **Fórmulas:** Formatea todas las fórmulas matemáticas en LaTeX ($$...).
                    6.  **Si no sabes:** Si no tienes un ejemplo de referencia, indícalo claramente."""
                }
            )
            prompt_text = build_prompt("A Mano", user_instruction)

        if solve_excel_button:
            current_mode_key = "Con Excel"
            st.session_state.sub_mode = current_mode_key
            iniciar_chat(
                files_to_load=["tema_1.pdf", "tema_2.pdf", "ejercicos_resueltos_excel_tema_2.pdf"],
                system_prompt={"text": """Eres un tutor experto que enseña a resolver ejercicios con Excel. Tu modelo es 'ejercicos_resueltos_excel_tema_2.pdf'.
                    **Reglas Clave:**
                    1.  **Metodología:** Explica la solución estructurando los datos en celdas e indicando claramente las fórmulas de Excel a utilizar.
                    2.  **Decisión Final:** Cuando se pida seleccionar un proyecto, DEBES seguir la estructura de dos fases de los ejemplos: **Fase 1 (Viabilidad)** y **Fase 2 (Comparación)** para cada criterio (VAN, TIR, etc.).
                    3.  **Conflicto VAN/TIR:** Si los criterios VAN y TIR son contradictorios, señálalo y concluye que el criterio del VAN es el decisivo para proyectos mutuamente excluyentes.
                    4.  **Si no sabes:** Si no tienes un ejemplo de referencia, indícalo claramente."""
                }
            )
            prompt_text = build_prompt("Con Excel", user_instruction)

        if prompt_text:
            history = st.session_state.histories[current_mode_key]
            user_question = f"**Tarea solicitada:** {user_instruction}\n\n**Datos proporcionados:**\n{edited_df.to_string(index=False)}"
            history.append({"role": "user", "content": user_question})
            with st.spinner("Analizando tu petición..."):
                response = st.session_state.chat.send_message(prompt_text)
                assistant_response = response.text
                guardar_en_sheets(
                    st.session_state.nombre, 
                    st.session_state.email, 
                    st.session_state.codigo, 
                    tipo=f"Ejercicio: {current_mode_key}", 
                    pregunta=user_instruction, 
                    respuesta=assistant_response
                )
                history.append({"role": "assistant", "content": assistant_response})
                save_conversation(st.session_state.email, current_mode_key, history)
                st.rerun()

        st.markdown("---")
        st.subheader("Historial de Ejercicios Resueltos")
        if st.session_state.sub_mode:
            current_mode_key = st.session_state.sub_mode
            for message in st.session_state.histories.get(current_mode_key, []):
                 with st.chat_message(message["role"]):
                    st.markdown(message["content"])
    
    # --- INTERFAZ DE CHAT UNIFICADA (para Guía y Temario) ---
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
                response = st.session_state.chat.send_message(prompt)
                assistant_response = response.text
                guardar_en_sheets(
                    st.session_state.nombre, 
                    st.session_state.email, 
                    st.session_state.codigo, 
                    tipo=f"Consulta: {current_mode_key}", 
                    pregunta=prompt, 
                    respuesta=assistant_response
                )
                history.append({"role": "assistant", "content": assistant_response})
                save_conversation(st.session_state.email, current_mode_key, history)
                st.rerun()

    if not st.session_state.mode:
        st.info("Por favor, selecciona un modo de consulta en la barra lateral para comenzar.")