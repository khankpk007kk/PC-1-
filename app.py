import streamlit as st
import json
import folium
from streamlit_folium import st_folium
from google import genai
from google.genai import types
from supabase import create_client, Client

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="KP Secretariat Dashboard", layout="wide", initial_sidebar_state="collapsed")

# --- CUSTOM CSS (PREMIUM DARK THEME) ---
st.markdown("""
<style>
    /* Main background */
    .stApp { background-color: #0f172a; color: #f8fafc; }
    
    /* Premium Header Card */
    .glass-card {
        background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
        border-left: 4px solid #10b981;
        padding: 20px;
        border-radius: 10px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3);
        margin-bottom: 24px;
    }
    
    /* Metrics Styling */
    .metric-value { color: #10b981; font-size: 2.2rem; font-weight: bold; margin-top: 10px; }
    .metric-title { color: #94a3b8; font-size: 1rem; text-transform: uppercase; letter-spacing: 1px; }
    
    /* Fix Label Visibility */
    .stTextArea label { 
        color: #94a3b8 !important; 
        font-size: 15px !important; 
        font-weight: 500 !important; 
    }
    
    /* Fix Text Area Box to Dark Theme */
    .stTextArea textarea {
        background-color: #1e293b !important;
        color: #f8fafc !important;
        border: 1px solid #334155 !important;
        border-radius: 8px !important;
    }
    .stTextArea textarea:focus { 
        border-color: #10b981 !important; 
        box-shadow: none !important; 
    }
    
    /* Fix Button Visibility and Hover Effects */
    .stButton button {
        background-color: #10b981 !important;
        color: white !important;
        font-weight: 600 !important;
        border-radius: 8px !important;
        border: none !important;
        padding: 10px 24px !important;
        transition: all 0.3s ease;
        width: 100%;
    }
    .stButton button:hover { 
        background-color: #059669 !important; 
        transform: scale(1.02); 
    }
</style>
""", unsafe_allow_html=True)

# --- SECRETS & CLIENTS ---
try:
    SUPABASE_URL = st.secrets["SUPABASE_URL"]
    SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
    DB_SECRET_KEY = st.secrets["DB_SECRET_KEY"]
except KeyError:
    st.error("⚠️ Secrets missing! Please check Streamlit Cloud Settings.")
    st.stop()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
client = genai.Client(api_key=GEMINI_API_KEY)

# --- GEMINI SCHEMA ---
pc1_schema = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "totalBudget": types.Schema(type=types.Type.NUMBER),
        "departmentWiseFunds": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "department": types.Schema(type=types.Type.STRING),
                    "allocatedAmount": types.Schema(type=types.Type.NUMBER)
                }
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
    required=["totalBudget", "departmentWiseFunds", "districtWiseAllocation"]
)

# --- UI HEADER ---
st.markdown('<div class="glass-card"><h2>🛡️ Civil Secretariat Peshawar - PC-1 Audit</h2><p style="color: #94a3b8; margin: 0;">Automated AI Document Verification System</p></div>', unsafe_allow_html=True)

# --- INPUT SECTION ---
document_text = st.text_area("📄 Paste Raw PC-1/PC-2 Document Text Here:", height=250, placeholder="Enter the official document text to begin analysis...")

if st.button("🚀 Analyze & Encrypt Document"):
    if not document_text.strip():
        st.error("Please enter document text first.")
    else:
        with st.spinner("Analyzing with Gemini AI & Encrypting in PostgreSQL..."):
            try:
                # 1. Call Gemini API
                prompt = """You are a Financial Auditor for KPK Government. Parse this PC-1 form text.
                Extract exact total budget, department-wise funds, and district allocations with accurate coordinates for KPK."""
                
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[prompt, document_text],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=pc1_schema,
                        temperature=0.1
                    ),
                )
                
                data = json.loads(response.text)
                
                # 2. Save Securely to Supabase
                supabase.rpc('insert_secure_pc1', {
                    'p_project_title': 'Automated Scan',
                    'p_department': 'P&D',
                    'p_raw_payload': document_text,
                    'p_secret_key': DB_SECRET_KEY,
                    'p_total_budget': data.get('totalBudget', 0),
                    'p_district_allocations': data.get('districtWiseAllocation', []),
                    'p_verification_status': 'PENDING'
                }).execute()
                
                st.success("✅ Document Successfully Analyzed and Encrypted!")
                
                # --- DASHBOARD METRICS ---
                st.markdown(f'''
                    <div class="glass-card" style="text-align: center;">
                        <div class="metric-title">Total Verified Budget</div>
                        <div class="metric-value">PKR {data.get("totalBudget", 0):,}</div>
                    </div>
                ''', unsafe_allow_html=True)
                
                # --- MAP VISUALIZATION (FOLIUM) ---
                st.subheader("📍 District Allocations Map")
                # Default center at Peshawar
                kp_map = folium.Map(location=[34.0151, 71.5249], zoom_start=7, tiles="CartoDB dark_matter")
                
                for loc in data.get('districtWiseAllocation', []):
                    folium.CircleMarker(
                        location=[loc.get('latitude', 34.0), loc.get('longitude', 71.5)],
                        radius=12,
                        popup=f"<b>{loc.get('district', 'Unknown')}</b><br>PKR {loc.get('amount', 0):,}",
                        color="#10b981",
                        fill=True,
                        fill_color="#059669",
                        fill_opacity=0.7
                    ).add_to(kp_map)
                
                # Use st_folium to render the map
                st_folium(kp_map, width=700, height=400, returned_objects=[])
                
                # --- PRINTABLE TABLE ---
                st.subheader("📑 Department Allocations")
                st.table(data.get('departmentWiseFunds', []))

            except Exception as e:
                st.error(f"System Error: {str(e)}")
