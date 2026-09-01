import streamlit as st
import json
import folium
from streamlit_folium import st_folium
from google import genai
from google.genai import types
from supabase import create_client, Client
import PyPDF2
import base64
import os

st.set_page_config(page_title="PC-1-2 Solution Hub", layout="wide", initial_sidebar_state="collapsed")

# --- INITIALIZE SESSION STATE ---
if "doc_text" not in st.session_state:
    st.session_state.doc_text = ""
if "doc_data" not in st.session_state:
    st.session_state.doc_data = None
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

def get_base64_image(image_path):
    if os.path.exists(image_path):
        with open(image_path, "rb") as img_file:
            return base64.b64encode(img_file.read()).decode()
    return None

logo_b64 = get_base64_image("kp_logo.png")
logo_html = f'<img src="data:image/png;base64,{logo_b64}" style="height: 75px; object-fit: contain;">' if logo_b64 else '<div style="height: 75px; width: 75px; border: 1px dashed #ccc; display: flex; align-items: center; justify-content: center; font-size: 10px; color: #999;">Logo</div>'

# --- CSS & PRINT STYLING ---
st.markdown("""
<style>
    [data-testid="stAppViewContainer"] { background-color: #F4F7F6 !important; }
    [data-testid="stHeader"] { background-color: transparent !important; }
    p, h1, h2, h3, h4, h5, h6, span, div, label { color: #1e293b !important; }
    .stTabs [data-baseweb="tab-list"] { background-color: #ffffff; padding: 5px 15px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); gap: 15px; }
    .stTabs [data-baseweb="tab"] { color: #64748b !important; font-weight: 600; padding: 12px 0px; }
    .stTabs [aria-selected="true"] { color: #059669 !important; border-bottom: 3px solid #059669 !important; }
    .stButton button { background-color: #059669 !important; color: white !important; font-weight: 700 !important; border-radius: 8px !important; border: none !important; width: 100%; transition: all 0.3s ease; }
    .stButton button:hover { background-color: #047857 !important; }
    .chat-bubble-user { background-color: #e2e8f0; padding: 10px 15px; border-radius: 15px 15px 0 15px; margin-bottom: 10px; width: fit-content; max-width: 80%; margin-left: auto; border: 1px solid #cbd5e1; }
    .chat-bubble-ai { background-color: #d1fae5; padding: 10px 15px; border-radius: 15px 15px 15px 0; margin-bottom: 10px; width: fit-content; max-width: 80%; border: 1px solid #a7f3d0; }
    
    @media print {
        [data-testid="stSidebar"], header, .stButton, .stTabs [data-baseweb="tab-list"], .stFileUploader, .stChatInput { display: none !important; }
        [data-testid="stAppViewContainer"] { background-color: white !important; }
        .custom-header { border-bottom: 2px solid #059669 !important; margin-bottom: 20px !important; }
    }
</style>
""", unsafe_allow_html=True)

# --- CUSTOM BRANDING HEADER ---
st.markdown(f"""
<div class="custom-header" style="display: flex; align-items: center; justify-content: space-between; background: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 10px rgba(0,0,0,0.05); border-top: 5px solid #059669; margin-bottom: 30px;">
    <div style="flex: 1;"></div>
    <div style="flex: 3; text-align: center;">
        <h1 style="color: #059669; margin: 0; font-size: 28px; font-weight: 900; letter-spacing: 1.5px;">PC-1-2 ALL IN ONE SOLUTION HUB</h1>
        <p style="color: #64748b; margin: 5px 0 0 0; font-size: 14px; font-weight: 700; letter-spacing: 3px;">MADE BY KALEEM</p>
    </div>
    <div style="flex: 1; display: flex; justify-content: flex-end;">{logo_html}</div>
</div>
""", unsafe_allow_html=True)

# --- SECRETS & CLIENTS SETUP ---
try:
    supabase: Client = create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])
    client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
    DB_SECRET = st.secrets["DB_SECRET_KEY"]
except Exception:
    st.error("⚠️ Secrets missing! Please check Streamlit Cloud Settings.")
    st.stop()

# --- GEMINI SCHEMA ---
pc1_schema = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "projectTitle": types.Schema(type=types.Type.STRING),
        "departmentName": types.Schema(type=types.Type.STRING),
        "totalBudget": types.Schema(type=types.Type.NUMBER),
        "components": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={"name": types.Schema(type=types.Type.STRING), "cost": types.Schema(type=types.Type.NUMBER)}
            )
        ),
        "districtWiseAllocation": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "district": types.Schema(type=types.Type.STRING),
                    "amount": types.Schema(type=types.Type.NUMBER),
                    "latitude": types.Schema(type=types.Type.NUMBER),
                    "longitude": types.Schema(type=types.Type.NUMBER)
                }
            )
        )
    },
    required=["projectTitle", "departmentName", "totalBudget", "components", "districtWiseAllocation"]
)

# --- TABS NAVIGATION ---
tab1, tab2, tab3, tab4, tab5 = st.tabs(["📤 Upload PDF", "📊 PC-1 Breakdown", "💬 AI Chat", "📝 Database & Comments", "🗺️ Map Analytics"])

# ==========================================
# TAB 1: UPLOAD & PROCESS
# ==========================================
with tab1:
    uploaded_file = st.file_uploader("Upload PC-1 / PC-2 Document (PDF Format)", type=['pdf'])
    if uploaded_file and st.button("🚀 Process & Secure Document"):
        with st.spinner("Extracting & Analyzing with Gemini 1.5 Flash..."):
            try:
                reader = PyPDF2.PdfReader(uploaded_file)
                extracted_text = "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
                
                if not extracted_text.strip():
                    st.error("Could not extract text. Please ensure the PDF is not a scanned image.")
                else:
                    st.session_state.doc_text = extracted_text
                    
                    prompt = "Parse this PC-1 form. Extract title, department, total budget, breakdown of components, and district allocations with coordinates."
                    response = client.models.generate_content(
                        model='gemini-1.5-flash',
                        contents=[prompt, extracted_text],
                        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=pc1_schema, temperature=0.1)
                    )
                    
                    data = json.loads(response.text)
                    st.session_state.doc_data = data
                    
                    supabase.rpc('insert_secure_pc1', {
                        'p_project_title': data.get('projectTitle', 'Unknown'),
                        'p_department': data.get('departmentName', 'Unknown'),
                        'p_raw_payload': extracted_text,
                        'p_secret_key': DB_SECRET,
                        'p_total_budget': data.get('totalBudget', 0),
                        'p_district_allocations': data.get('districtWiseAllocation', []),
                        'p_verification_status': 'VERIFIED'
                    }).execute()
                    
                    st.success(f"✅ Document Processed Successfully: {data.get('projectTitle')}. You can now view Breakdown, Chat, or Maps.")
            except Exception as e:
                st.error(f"Processing Error: {str(e)}")

# ==========================================
# TAB 2: PC-1 BREAKDOWN
# ==========================================
with tab2:
    if st.session_state.doc_data:
        d = st.session_state.doc_data
        col1, col2 = st.columns(2)
        with col1:
            st.info(f"**Project Title:**\n{d.get('projectTitle')}")
        with col2:
            st.success(f"**Total Budget:**\nPKR {d.get('totalBudget', 0):,}")
            
        st.markdown("### 📋 Component Wise Breakdown")
        st.table(d.get('components', []))
        
        st.markdown("### 📍 District Allocations")
        st.table([{"District": x['district'], "Amount (PKR)": f"{x['amount']:,}"} for x in d.get('districtWiseAllocation', [])])
    else:
        st.warning("Please upload and process a document in Tab 1 first.")

# ==========================================
# TAB 3: FREE AI CHAT
# ==========================================
with tab3:
    if st.session_state.doc_text:
        st.markdown("### 🤖 Chat with PC-1 Document")
        
        for msg in st.session_state.chat_history:
            css_class = "chat-bubble-user" if msg["role"] == "user" else "chat-bubble-ai"
            st.markdown(f'<div class="{css_class}"><b>{"You" if msg["role"]=="user" else "AI"}:</b> {msg["content"]}</div>', unsafe_allow_html=True)

        user_input = st.chat_input("Ask any question regarding the uploaded document...")
        if user_input:
            st.session_state.chat_history.append({"role": "user", "content": user_input})
            st.markdown(f'<div class="chat-bubble-user"><b>You:</b> {user_input}</div>', unsafe_allow_html=True)
            
            with st.spinner("AI is thinking..."):
                try:
                    chat_prompt = f"Context from official government document:\n{st.session_state.doc_text}\n\nUser Question: {user_input}\nAnswer professionally and concisely based ONLY on the provided context."
                    chat_response = client.models.generate_content(
                        model='gemini-1.5-flash', 
                        contents=chat_prompt
                    )
                    reply = chat_response.text
                    
                    st.session_state.chat_history.append({"role": "ai", "content": reply})
                    st.rerun()
                except Exception as e:
                    st.error(f"Chat Error: {str(e)}")
    else:
        st.warning("Please upload a document first to activate AI Chat.")

# ==========================================
# TAB 4: DATABASE & COMMENTS
# ==========================================
with tab4:
    try:
        response = supabase.table("secure_pc1").select("id, project_title, department, total_budget").execute()
        records = response.data
        if records:
            project_titles = {r['project_title']: r['id'] for r in records}
            selected_proj = st.selectbox("Select Project to View/Add Comments:", options=list(project_titles.keys()))
            
            if selected_proj:
                proj_id = project_titles[selected_proj]
                
                # View Comments
                comments_res = supabase.table("pc1_comments").select("*").eq("pc1_id", proj_id).order("created_at", desc=False).execute()
                st.markdown("#### 📝 Official Comments & Reviews")
                if comments_res.data:
                    for c in comments_res.data:
                        st.markdown(f"**{c['commenter_name']}** _({c['created_at'][:10]})_:<br><span style='color:#475569;'>{c['comment_text']}</span>", unsafe_allow_html=True)
                        st.divider()
                else:
                    st.info("No comments yet.")
                
                # Add Comment
                with st.form("comment_form"):
                    c_name = st.text_input("Your Name / Designation:")
                    c_text = st.text_area("Add Review / Comment:")
                    if st.form_submit_button("Submit Comment"):
                        if c_name and c_text:
                            supabase.table("pc1_comments").insert({"pc1_id": proj_id, "commenter_name": c_name, "comment_text": c_text}).execute()
                            st.success("Comment Added Successfully!")
                            st.rerun()
                        else:
                            st.error("Please fill both name and comment fields.")
        else:
            st.info("Database is empty. Please upload documents first.")
    except Exception as e:
        st.error(f"Database connection issue. Did you run the SQL query for the comments table? Error: {str(e)}")

# ==========================================
# TAB 5: MAP ANALYTICS
# ==========================================
with tab5:
    st.markdown("### 🗺️ Geographical Resource Allocation")
    try:
        res = supabase.table("secure_pc1").select("project_title, district_allocations").execute()
        if res.data:
            kp_map = folium.Map(location=[34.0151, 71.5249], zoom_start=7, tiles="CartoDB positron")
            for proj in res.data:
                for loc in proj.get('district_allocations', []):
                    if loc.get('latitude') and loc.get('longitude'):
                        folium.Marker(
                            location=[loc['latitude'], loc['longitude']],
                            popup=f"<b>{proj.get('project_title')}</b><br>{loc.get('district')}: PKR {loc.get('amount', 0):,}",
                            icon=folium.Icon(color="green", icon="info-sign")
                        ).add_to(kp_map)
            st_folium(kp_map, width=1000, height=500, returned_objects=[])
        else:
            st.info("No mapping data available yet.")
    except Exception as e:
        st.error(f"Map Error: {str(e)}")
