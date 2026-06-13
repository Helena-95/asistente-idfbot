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
        # IMPORTANTE: Añadimos ttl=0 para forzar la lectura real y no perder datos
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
    except Exception as e:
        st.sidebar.warning("No se pudo sincronizar el registro.")

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
            if codigo_asignatura in ["1500", "2165", "0000"]:
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
    # --- Información de Exámenes ---
    info_examen = ""
    if st.session_state.codigo == "2165":
        info_examen = "El examen de la asignatura 2165 es el Lunes, 1 de junio de 09:00 a 12:00."
    elif st.session_state.codigo == "1500":
        info_examen = "El examen de la asignatura 1500 es el Viernes, 5 de junio de 09:30 a 12:30."

    def iniciar_chat(files_to_load, system_prompt, extra_text=""):
    # ¡Cuidado! Todo esto debe tener 4 espacios de margen
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
            # Asegúrate de usar 1.5-pro
            model = genai.GenerativeModel("gemini-2.5-pro")
            st.session_state.chat = model.start_chat(
                history=[{"role": "user", "parts": prompt_parts},
                        {"role": "model", "parts": [{"text": "He analizado el PDF y la web de la UMH. Estoy listo."}]}]
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

            # Preparamos las instrucciones incluyendo la fecha del examen
            instrucciones_con_fecha = {
                "text": f"""Eres un asistente académico. 
                DATOS IMPORTANTES DE EXAMEN: {info_examen}
                Utiliza el PDF adjunto y la web de la UMH para responder. 
                Si el alumno pregunta por la fecha del examen, DEBES dar la fecha que aparece en 'DATOS IMPORTANTES'."""
            }
            
            iniciar_chat(
                files_to_load=["guia_docente.pdf"],
                system_prompt=instrucciones_con_fecha, # <--- Ahora sí usamos la variable
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
        
        # Selector de tipo de ejercicio
        tipo_ejercicio = st.radio("Selecciona el tipo de ejercicio:", ["Proyecto de Inversión", "Bono Financiero"], horizontal=True)

        st.subheader("1. Introduce los Datos del Ejercicio")
        
        if tipo_ejercicio == "Proyecto de Inversión":
            col1, col2 = st.columns(2)
            investment = col1.number_input("Inversión Inicial (€)", value=100000, step=1000)
            discount_rate = col2.number_input("Tasa de Descuento (%)", value=17.0, step=0.5)

            st.text("Flujos de Caja Anuales (€)")
            df = pd.DataFrame([{"Año": 1, "Proyecto A": 60000, "Proyecto B": 121000}, {"Año": 2, "Proyecto A": 72000, "Proyecto B": 0}])
            edited_df = st.data_editor(df, num_rows="dynamic", key="cashflow_data")
        else:
            col_b1, col_b2 = st.columns(2)
            nominal = col_b1.number_input("Valor Nominal (€)", value=1000)
            cupon_p = col_b2.number_input("Cupón Anual (%)", value=5.0)
            precio_c = st.number_input("Precio de Adquisición (€)", value=950)
            plazo_b = st.slider("Años hasta vencimiento", 1, 30, 5)

        st.subheader("2. ¿Qué necesitas que resuelva?")
        default_instr = "Calcula el VAN, la TIR y el Payback Descontado de ambos proyectos." if tipo_ejercicio == "Proyecto de Inversión" else "Calcula la TIR del bono desglosando la rentabilidad explícita e implícita."
        user_instruction = st.text_area("Instrucciones para el asistente:", value=default_instr)
        
        st.subheader("3. Elige un Método de Resolución")
        col_mano, col_excel = st.columns(2)
        
        # Botones de Proyecto
        if tipo_ejercicio == "Proyecto de Inversión":
            solve_mano_button = col_mano.button("✍️ Resolver a Mano")
            solve_excel_button = col_excel.button("📊 Resolver con Excel")
        else:
            # Para el BONO: Solo activamos "A Mano" y desactivamos/avisamos sobre Excel
            solve_mano_button = col_mano.button("✍️ Resolver a Mano")
            col_excel.info("📊 Opción Excel no disponible para bonos temporalmente.")
            solve_excel_button = False

        def build_prompt(method_name, instruction):
            prompt = f"Por favor, usando tu metodología de '{method_name}' y basándote en los siguientes datos, realiza esta tarea específica: '{instruction}'.\n\n"
            if tipo_ejercicio == "Proyecto de Inversión":
                prompt += f"Datos del ejercicio:\n- Inversión Inicial: {investment}€\n- Tasa de Descuento: {discount_rate}%\n"
                prompt += "Los flujos de caja son:\n" + edited_df.loc[:, (edited_df != 0).any(axis=0)].to_string(index=False)
            else:
                prompt += f"Datos del Bono:\n- Valor Nominal: {nominal}€\n- Cupón: {cupon_p}%\n- Precio: {precio_c}€\n- Plazo: {plazo_b} años\n"
            return prompt

        # --- REGLA ANTICÓDIGO LaTeX (PROMPT DEL SISTEMA) ---
        REGLA_FORMATO = """
        IMPORTANTE (REGLA DE FORMATO): 
        - NO utilices comandos de estructura de documento LaTeX (como \\documentclass, \\begin{document}, \\section, etc.). 
        - Escribe tu respuesta en Markdown normal y limpio. 
        - Encierra las fórmulas matemáticas EXCLUSIVAMENTE entre signos de dólar ($...$ para línea y $$...$$ para bloques). 
        - No generes un archivo .tex, genera una respuesta de chat legible.
        """

        prompt_text = None
        
        if solve_mano_button:
            current_mode_key = "A Mano"
            st.session_state.sub_mode = current_mode_key
            
            if tipo_ejercicio == "Proyecto de Inversión":
                archivos = ["tema_1.pdf", "tema_2.pdf", "ejercicos_resueltos_a_mano_tema_2.pdf"]
                sys_msg = "Eres un tutor experto que resuelve a mano siguiendo 'ejercicos_resueltos_a_mano_tema_2.pdf'."
            else:
                archivos = ["tema_3.pdf", "ejercicos_resueltos_a_mano_tema_3.pdf"]
                sys_msg = "Eres experto en bonos. Desglosa Rentabilidad EXPLÍCITA e IMPLÍCITA siguiendo 'ejercicos_resueltos_a_mano_tema_3.pdf'."
            
            iniciar_chat(files_to_load=archivos, system_prompt={"text": sys_msg + REGLA_FORMATO})
            prompt_text = build_prompt("A Mano", user_instruction)

        if solve_excel_button:
            current_mode_key = "Con Excel"
            st.session_state.sub_mode = current_mode_key
            archivos = ["tema_1.pdf", "tema_2.pdf", "ejercicos_resueltos_excel_tema_2.pdf"]
            sys_msg = "Eres un tutor de Excel siguiendo 'ejercicos_resueltos_excel_tema_2.pdf'."
            
            iniciar_chat(files_to_load=archivos, system_prompt={"text": sys_msg + REGLA_FORMATO})
            prompt_text = build_prompt("Con Excel", user_instruction)

        # --- PROCESAMIENTO DE RESPUESTA ---
        if prompt_text:
            history = st.session_state.histories[current_mode_key]
            datos_h = edited_df.to_string(index=False) if tipo_ejercicio == "Proyecto de Inversión" else f"Nominal: {nominal}, Precio: {precio_c}"
            user_question = f"**Tarea:** {user_instruction}\n\n**Datos:**\n{datos_h}"
            
            history.append({"role": "user", "content": user_question})
            with st.spinner("Generando resolución paso a paso..."):
                response = st.session_state.chat.send_message(prompt_text)
                assistant_response = response.text
                guardar_en_sheets(st.session_state.nombre, st.session_state.email, st.session_state.codigo, 
                                 tipo=f"Ejercicio: {current_mode_key}", pregunta=user_instruction, respuesta=assistant_response)
                history.append({"role": "assistant", "content": assistant_response})
                save_conversation(st.session_state.email, current_mode_key, history)
                st.rerun()

        st.markdown("---")
        st.subheader("📁 Archivo de Ejercicios Guardados")
        
        if st.session_state.sub_mode:
            current_mode_key = st.session_state.sub_mode
            historial = st.session_state.histories.get(current_mode_key, [])
            
            # Recorremos el historial de 2 en 2 (Pregunta + Respuesta)
            # Empezamos desde el final para que el más reciente salga arriba
            for i in range(len(historial)-2, -1, -2):
                pregunta = historial[i]["content"]
                respuesta = historial[i+1]["content"]
                
                # Creamos un "cajón" plegable para cada ejercicio
                # El título del cajón será un resumen de la pregunta
                titulo_ejercicio = pregunta.split('\n')[0].replace('**Tarea:**', '')
                
                with st.expander(f"📋 Ejercicio: {titulo_ejercicio[:60]}..."):
                    st.info("**Tu consulta original:**")
                    st.write(pregunta)
                    st.markdown("---")
                    st.success("**Resolución del Asistente:**")
                    st.markdown(respuesta)
    
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
