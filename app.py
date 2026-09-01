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

# --- PAGE CONFIGURATION (LIGHT THEME FORCED) ---
st.set_page_config(page_title="PC-1-2 Solution Hub", layout="wide", initial_sidebar_state="collapsed")

# --- LOAD LOGO FOR UI & PRINT ---
def get_base64_image(image_path):
    if os.path.exists(image_path):
        with open(image_path, "rb") as img_file:
            return base64.b64encode(img_file.read()).decode()
    return None

logo_b64 = get_base64_image("kp_logo.png")
logo_html = f'<img src="data:image/png;base64,{logo_b64}" style="height: 75px; object-fit: contain;">' if logo_b64 else '<div style="height: 75px; width: 75px; border: 1px dashed #ccc; display: flex; align-items: center; justify-content: center; font-size: 10px; color: #999;">Logo<br>Missing</div>'

# --- PROFESSIONAL LIGHT CSS & PRINT LAYOUT ---
st.markdown("""
<style>
    /* Force Light Theme Colors */
    [data-testid="stAppViewContainer"] { background-color: #F4F7F6 !important; }
    [data-testid="stHeader"] { background-color: transparent !important; }
    p, h1, h2, h3, h4, h5, h6, span, div, label { color: #1e293b !important; }
    
    /* Clean Tabs Styling */
    .stTabs [data-baseweb="tab-list"] { background-color: #ffffff; padding: 5px 15px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); gap: 20px; }
    .stTabs [data-baseweb="tab"] { color: #64748b !important; font-weight: 600; padding: 12px 0px; }
    .stTabs [aria-selected="true"] { color: #059669 !important; border-bottom: 3px solid #059669 !important; }
    
    /* File Uploader Fix */
    [data-testid="stFileUploadDropzone"] { background-color: #ffffff !important; border: 2px dashed #cbd5e1 !important; border-radius: 12px !important; }
    [data-testid="stFileUploadDropzone"]:hover { border-color: #059669 !important; background-color: #f0fdf4 !important; }
    
    /* Input Fields */
    div[data-baseweb="input"] input { background-color: #ffffff !important; color: #1e293b !important; border: 1px solid #cbd5e1 !important; border-radius: 6px !important; }
    div[data-baseweb="input"]:focus-within { border-color: #059669 !important; }
    
    /* Beautiful Buttons */
    .stButton button { background-color: #059669 !important; color: white !important; font-weight: 700 !important; width: 100%; border-radius: 8px !important; border: none !important; padding: 12px 24px !important; box-shadow: 0 4px 6px rgba(5, 150, 105, 0.2) !important; transition: all 0.3s ease; }
    .stButton button:hover { background-color: #047857 !important; transform: translateY(-2px); box-shadow: 0 6px 8px rgba(5, 150, 105, 0.3) !important; }

    /* CSS FOR PRINTING PDF LATER */
    @media print {
        /* Hide UI controls during print */
        [data-testid="stSidebar"], header, .stButton, .stTabs [data-baseweb="tab-list"], [data-testid="stFileUploadDropzone"] { display: none !important; }
        /* Clean white background for paper */
        [data-testid="stAppViewContainer"] { background-color: white !important; }
        /* Custom Header styling for print */
        .custom-header { box-shadow: none !important; border-bottom: 2px solid #059669 !important; border-top: none !important; margin-bottom: 20px !important; }
        .page-break { page-break-before: always; }
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
    <div style="flex: 1; display: flex; justify-content: flex-end;">
        {logo_html}
    </div>
</div>
""", unsafe_allow_html=True)

# --- SECRETS & CLIENTS SETUP ---
try:
    supabase: Client = create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])
    client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
    DB_SECRET = st.secrets["DB_SECRET_KEY"]
except Exception:
    st.error("⚠️ Secrets missing! Please add them in Streamlit settings.")
    st.stop()

# --- GEMINI SCHEMA ---
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

# --- TABS NAVIGATION ---
tab1, tab2, tab3 = st.tabs(["📤 Upload & AI Audit", "🔍 Search Database", "🗺️ Map Analytics"])

# ==========================================
# TAB 1: FILE UPLOAD
# ==========================================
with tab1:
    uploaded_file = st.file_uploader("Upload PC-1 / PC-2 Document (PDF Format)", type=['pdf'])
    
    if uploaded_file and st.button("🚀 Process & Secure Document"):
        with st.spinner("Extracting text and analyzing via AI Engine..."):
            try:
                reader = PyPDF2.PdfReader(uploaded_file)
                document_text = "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
                
                prompt = "Parse this PC-1 form. Extract project title, department, total budget, and district allocations with exact coordinates."
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[prompt, document_text],
                    config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=pc1_schema, temperature=0.1)
                )
                data = json.loads(response.text)
                
                supabase.rpc('insert_secure_pc1', {
                    'p_project_title': data.get('projectTitle', 'Unknown'),
                    'p_department': data.get('departmentName', 'Unknown'),
                    'p_raw_payload': document_text,
                    'p_secret_key': DB_SECRET,
                    'p_total_budget': data.get('totalBudget', 0),
                    'p_district_allocations': data.get('districtWiseAllocation', []),
                    'p_verification_status': 'VERIFIED'
                }).execute()
                
                st.success(f"✅ Successfully Verified & Encrypted: {data.get('projectTitle')}")
                
                st.markdown("### 📊 Document Summary")
                st.table([{"Project Title": data.get('projectTitle'), "Department": data.get('departmentName'), "Total Budget (PKR)": f"{data.get('totalBudget', 0):,}"}])
            except Exception as e:
                st.error(f"Error Processing Document: {str(e)}")

# ==========================================
# TAB 2: SEARCH & PRINT DATA
# ==========================================
with tab2:
    search_query = st.text_input("🔍 Search Document by Title or Department:")
    try:
        response = supabase.table("secure_pc1").select("project_title, department, total_budget, verification_status, created_at").execute()
        records = response.data
        if records:
            if search_query:
                records = [r for r in records if search_query.lower() in r['project_title'].lower() or search_query.lower() in r['department'].lower()]
            
            # Print-friendly display
            st.dataframe(
                records,
                column_config={"project_title": "Project Title", "department": "Department", "total_budget": st.column_config.NumberColumn("Total Budget (PKR)", format="%d"), "verification_status": "Status", "created_at": st.column_config.DatetimeColumn("Date", format="D MMM YYYY")},
                use_container_width=True, hide_index=True
            )
            st.info("💡 Tip: Press `Ctrl+P` (or Print from browser menu) to save this page as a branded PDF. The buttons will hide automatically.")
        else:
            st.info("No documents found in the database.")
    except Exception as e:
        st.error(f"Database Error: {str(e)}")

# ==========================================
# TAB 3: MAP ANALYTICS
# ==========================================
with tab3:
    st.markdown("### 📍 KPK Project Allocations")
    try:
        res = supabase.table("secure_pc1").select("project_title, district_allocations").execute()
        if res.data:
            kp_map = folium.Map(location=[34.0151, 71.5249], zoom_start=7, tiles="CartoDB positron") # Light map theme
            for proj in res.data:
                for loc in proj.get('district_allocations', []):
                    if loc.get('latitude') and loc.get('longitude'):
                        folium.Marker(
                            location=[loc['latitude'], loc['longitude']],
                            popup=f"<b>{proj.get('project_title')}</b><br>PKR {loc.get('amount', 0):,}",
                            icon=folium.Icon(color="green", icon="info-sign")
                        ).add_to(kp_map)
            st_folium(kp_map, width=1000, height=500, returned_objects=[])
        else:
            st.info("No mapping data available.")
    except Exception:
        pass
