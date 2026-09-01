import streamlit as st
import json
import folium
from streamlit_folium import st_folium
from google import genai
from google.genai import types
from supabase import create_client, Client

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="KP Secretariat Dashboard", layout="wide")

# --- CUSTOM CSS (DARK GLASSMORPHISM) ---
st.markdown("""
<style>
    .stApp { background-color: #0f172a; color: #f8fafc; }
    .glass-card {
        background: rgba(30, 41, 59, 0.7);
        backdrop-filter: blur(10px);
        border: 1px solid rgba(255, 255, 255, 0.1);
        padding: 20px;
        border-radius: 15px;
        margin-bottom: 20px;
    }
    .metric-value { color: #10b981; font-size: 2rem; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

# --- SECRETS & CLIENTS ---
# Streamlit Cloud ki settings se secrets fetch karein
SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
DB_SECRET_KEY = st.secrets["DB_SECRET_KEY"]

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
st.markdown('<div class="glass-card"><h2>🛡️ Civil Secretariat Peshawar - PC-1 Audit</h2></div>', unsafe_allow_html=True)

# --- INPUT SECTION ---
document_text = st.text_area("Paste Raw PC-1/PC-2 Document Text Here:", height=200)

if st.button("Analyze & Encrypt Document"):
    if not document_text.strip():
        st.error("Please enter document text first.")
    else:
        with st.spinner("Analyzing with Gemini AI (Zero-Trust Protocol)..."):
            try:
                # 1. Call Gemini API
                prompt = """You are a Financial Auditor for KPK Government. Parse this PC-1 form.
                Extract exact total budget, department funds, and district allocations with coordinates."""
                
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
                
                # 2. Save Securely to Supabase (Calling the RPC function made in previous step)
                supabase.rpc('insert_secure_pc1', {
                    'p_project_title': 'Automated Scan',
                    'p_department': 'P&D',
                    'p_raw_payload': document_text,
                    'p_secret_key': DB_SECRET_KEY,
                    'p_total_budget': data['totalBudget'],
                    'p_district_allocations': data['districtWiseAllocation'],
                    'p_verification_status': 'PENDING'
                }).execute()
                
                st.success("✅ Document Analyzed and Encrypted in PostgreSQL!")
                
                # --- DASHBOARD METRICS ---
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f'<div class="glass-card"><h3>Total Budget</h3><div class="metric-value">PKR {data["totalBudget"]:,}</div></div>', unsafe_allow_html=True)
                
                # --- MAP VISUALIZATION (FOLIUM) ---
                st.subheader("📍 District Allocations (KPK)")
                kp_map = folium.Map(location=[34.0151, 71.5249], zoom_start=7, tiles="CartoDB dark_matter")
                
                for loc in data['districtWiseAllocation']:
                    folium.CircleMarker(
                        location=[loc['latitude'], loc['longitude']],
                        radius=10,
                        popup=f"{loc['district']}: PKR {loc['amount']:,}",
                        color="#10b981",
                        fill=True,
                        fill_color="#059669"
                    ).add_to(kp_map)
                
                st_folium(kp_map, width=800, height=400)
                
                # --- PRINTABLE TABLE ---
                st.subheader("📑 Department Allocations")
                st.table(data['departmentWiseFunds'])

            except Exception as e:
                st.error(f"System Error: {str(e)}")
