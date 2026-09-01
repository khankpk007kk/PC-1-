import streamlit as st
import json
import folium
from streamlit_folium import st_folium
from google import genai
from google.genai import types
from supabase import create_client, Client
import PyPDF2
import io

st.set_page_config(page_title="KP Secretariat Dashboard", layout="wide", initial_sidebar_state="expanded")

# --- CUSTOM CSS ---
st.markdown("""
<style>
    .stApp { background-color: #0f172a; color: #f8fafc; }
    .glass-card { background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); border-left: 4px solid #10b981; padding: 20px; border-radius: 10px; margin-bottom: 24px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); }
    .stTabs [data-baseweb="tab-list"] { gap: 24px; }
    .stTabs [data-baseweb="tab"] { color: #94a3b8; font-weight: 600; padding: 10px 0px; }
    .stTabs [aria-selected="true"] { color: #10b981 !important; border-bottom: 2px solid #10b981 !important; }
    div[data-baseweb="input"] input { background-color: #1e293b !important; color: #fff !important; }
    .stButton button { background-color: #10b981 !important; color: white !important; font-weight: 600 !important; width: 100%; border-radius: 8px !important; border: none !important; }
    .stButton button:hover { background-color: #059669 !important; }
</style>
""", unsafe_allow_html=True)

# --- SECRETS & CLIENTS ---
try:
    supabase: Client = create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])
    client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
    DB_SECRET = st.secrets["DB_SECRET_KEY"]
except Exception:
    st.error("⚠️ Secrets missing in Streamlit Cloud!")
    st.stop()

# --- GEMINI SCHEMA (Updated for Search/Index) ---
pc1_schema = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "projectTitle": types.Schema(type=types.Type.STRING),
        "departmentName": types.Schema(type=types.Type.STRING),
        "totalBudget": types.Schema(type=types.Type.NUMBER),
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
    required=["projectTitle", "departmentName", "totalBudget", "districtWiseAllocation"]
)

st.markdown('<div class="glass-card"><h2>🛡️ Civil Secretariat - Command Center</h2><p style="color: #94a3b8; margin:0;">Automated PC-1 Audit, Search & Map Analytics</p></div>', unsafe_allow_html=True)

# --- TABS NAVIGATION ---
tab1, tab2, tab3 = st.tabs(["📤 Upload & AI Audit", "🔍 Index & Search", "🗺️ Map-Wise Analytics"])

# ==========================================
# TAB 1: FILE UPLOAD & AI PROCESSING
# ==========================================
with tab1:
    uploaded_file = st.file_uploader("Upload PC-1 Document (PDF format)", type=['pdf'])
    
    if uploaded_file and st.button("🚀 Process & Secure Document"):
        with st.spinner("Extracting text and analyzing via Gemini..."):
            try:
                # Read PDF
                reader = PyPDF2.PdfReader(uploaded_file)
                document_text = "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
                
                # AI Processing
                prompt = "Parse this PC-1 form. Extract project title, department, total budget, and district allocations with exact coordinates."
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[prompt, document_text],
                    config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=pc1_schema, temperature=0.1)
                )
                data = json.loads(response.text)
                
                # Database Insert
                supabase.rpc('insert_secure_pc1', {
                    'p_project_title': data.get('projectTitle', 'Unknown Project'),
                    'p_department': data.get('departmentName', 'Unknown Dept'),
                    'p_raw_payload': document_text,
                    'p_secret_key': DB_SECRET,
                    'p_total_budget': data.get('totalBudget', 0),
                    'p_district_allocations': data.get('districtWiseAllocation', []),
                    'p_verification_status': 'VERIFIED'
                }).execute()
                
                st.success(f"✅ Successfully processed: {data.get('projectTitle')}")
                st.balloons()
            except Exception as e:
                st.error(f"Processing Error: {str(e)}")

# ==========================================
# TAB 2: INDEX-WISE SEARCH & FILTERING
# ==========================================
with tab2:
    st.subheader("🔍 Search Database")
    search_query = st.text_input("Search by Project Title or Department:", placeholder="e.g., Solarization, Education...")
    
    try:
        # Fetch metadata from database (Excluding encrypted payload for speed)
        response = supabase.table("secure_pc1").select("id, project_title, department, total_budget, verification_status, created_at").execute()
        records = response.data
        
        if records:
            # Filter Logic
            if search_query:
                records = [r for r in records if search_query.lower() in r['project_title'].lower() or search_query.lower() in r['department'].lower()]
            
            # Display as interactive dataframe
            st.dataframe(
                records,
                column_config={
                    "id": None, # Hide UUID
                    "project_title": "Project Title",
                    "department": "Department",
                    "total_budget": st.column_config.NumberColumn("Total Budget (PKR)", format="%d"),
                    "verification_status": "Status",
                    "created_at": st.column_config.DatetimeColumn("Date Added", format="D MMM YYYY")
                },
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("No documents found in the database. Please upload a PC-1 first.")
    except Exception as e:
        st.error(f"Database Error: {str(e)}")

# ==========================================
# TAB 3: MAP-WISE ANALYTICS
# ==========================================
with tab3:
    st.subheader("🗺️ Province-Wide Project Distribution")
    try:
        # Fetch all district allocations
        res = supabase.table("secure_pc1").select("project_title, district_allocations").execute()
        all_projects = res.data
        
        if all_projects:
            kp_map = folium.Map(location=[34.0151, 71.5249], zoom_start=7, tiles="CartoDB dark_matter")
            
            for proj in all_projects:
                title = proj.get('project_title', 'Project')
                allocations = proj.get('district_allocations', [])
                
                for loc in allocations:
                    if loc.get('latitude') and loc.get('longitude'):
                        folium.Marker(
                            location=[loc['latitude'], loc['longitude']],
                            popup=f"<b>{title}</b><br>{loc.get('district')}: PKR {loc.get('amount', 0):,}",
                            icon=folium.Icon(color="green", icon="info-sign")
                        ).add_to(kp_map)
            
            st_folium(kp_map, width=1000, height=500, returned_objects=[])
        else:
            st.info("No mapping data available yet.")
    except Exception as e:
        st.error(f"Map Rendering Error: {str(e)}")
