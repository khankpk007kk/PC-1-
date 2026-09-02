"""
PC-1-2 ALL IN ONE SOLUTION HUB — fixed build (2026-09-01)

Asal masla: Groq ne `llama-3.3-70b-versatile` ko 17 June 2026 ko deprecate kiya
aur 16 August 2026 ko free/developer tier par band kar diya. Is liye har AI call
400 `model_decommissioned` de rahi thi, jo purane code mein sirf
"Processing Error: ..." ban kar dikh rahi thi.
"""
import base64
import html
import importlib.metadata
import json
import os
import re

import folium
import streamlit as st
from groq import Groq
from streamlit_folium import st_folium
from supabase import create_client, Client

try:                       # pypdf = maintained; PyPDF2 archived ho chuka hai
    from pypdf import PdfReader
except ImportError:
    from PyPDF2 import PdfReader

# ----------------------------------------------------------------- settings
MODEL_CANDIDATES = [
    "openai/gpt-oss-120b",          # Groq ki batai hui replacement
    "openai/gpt-oss-20b",
    "moonshotai/kimi-k2-instruct-0905",
    "meta-llama/llama-4-scout-17b-16e-instruct",
]
DEAD_MODEL_HINTS = ("decommission", "deprecat", "does not exist",
                    "not found", "no longer")
MAX_DOC_CHARS = 24_000          # ~6k tokens: free-tier TPM limit se bachne ke liye
MAX_CHAT_DOC_CHARS = 12_000
CHAT_MEMORY_TURNS = 6

st.set_page_config(page_title="PC-1-2 Solution Hub", layout="wide",
                   initial_sidebar_state="collapsed")

for _key, _val in {"doc_text": "", "doc_data": None, "chat_history": [],
                   "active_engine": "Pending", "working_model": None}.items():
    st.session_state.setdefault(_key, _val)
# ----------------------------------------------------------------- helpers
def safe_num(value, default=0):
    """LLM kabhi budget ko '1,200 million' string bana deta hai; f"{x:,}" us par
    ValueError phenkta hai. Is liye har number pehle yahan se guzarta hai."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        cleaned = re.sub(r"[^0-9.\-]", "", value)
        if cleaned in ("", "-", ".", "-."):
            return default
        try:
            return float(cleaned) if "." in cleaned else int(cleaned)
        except ValueError:
            return default
    return default


def money(value):
    return f"{safe_num(value):,.0f}"


def extract_json(text):
    """JSON mode ke bawajood model kabhi markdown/extra text lapet deta hai,
    ya max_tokens khatam hone par JSON adhoora reh jata hai."""
    if not text or not text.strip():
        raise ValueError("Model ne khaali jawab bheja (max_tokens ya filter issue).")
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"Output JSON nahi tha. Pehle 300 chars: {text[:300]}")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError(f"JSON object expected tha, mila {type(data).__name__}.")
    return data


def as_list(value):
    """Supabase ka column agar jsonb ke bajaye text hai to yahan str aata hai —
    aur str par for-loop chalane se har character iterate hota hai."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def bubble(role, text):
    css = "chat-bubble-user" if role == "user" else "chat-bubble-ai"
    who = "You" if role == "user" else "AI"
    body = html.escape(str(text)).replace("\n", "<br>")
    st.markdown(f'<div class="{css}"><b>{who}:</b> {body}</div>',
                unsafe_allow_html=True)
def pdf_to_text(file_obj):
    reader = PdfReader(file_obj)
    pages = [(page.extract_text() or "") for page in reader.pages]
    return "\n".join(p for p in pages if p.strip()), len(reader.pages)


# ----------------------------------------------------------------- clients
@st.cache_resource(show_spinner=False)
def get_clients(supabase_url, supabase_key, groq_key):
    supa = create_client(supabase_url, supabase_key)
    groq = Groq(api_key=groq_key, timeout=90.0, max_retries=2)
    return supa, groq


try:
    supabase, groq_client = get_clients(st.secrets["SUPABASE_URL"],
                                        st.secrets["SUPABASE_KEY"],
                                        st.secrets["GROQ_API_KEY"])
    DB_SECRET = st.secrets["DB_SECRET_KEY"]
except KeyError as missing:
    st.error(f"⚠️ Secret missing: {missing}. Streamlit settings → Secrets mein "
             "SUPABASE_URL, SUPABASE_KEY, DB_SECRET_KEY, GROQ_API_KEY daalein.")
    st.stop()
except Exception as exc:                    # ghalat URL / purana SDK waghera
    st.error("⚠️ Client banate waqt error — asal wajah neeche hai:")
    st.exception(exc)
    st.stop()


def _looks_dead(exc):
    msg = str(exc).lower()
    return any(hint in msg for hint in DEAD_MODEL_HINTS)


def call_groq(messages, json_mode=False, max_tokens=4096, temperature=0.2):
    """Pehla zinda model use karta hai. Model band ho jaye to khud agla try karta
    hai, taake aage kabhi Groq kisi model ko retire kare to app na ruke."""
    order = [m for m in MODEL_CANDIDATES]
    working = st.session_state.get("working_model")
    if working in order:
        order.remove(working)
        order.insert(0, working)

    errors = []
    for model in order:
        kwargs = {"model": model, "messages": messages,
                  "temperature": temperature, "max_tokens": max_tokens}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = groq_client.chat.completions.create(**kwargs)
        except Exception as exc:
            errors.append(f"• {model} → {exc}")
            if _looks_dead(exc):
                continue                    # ye model mar chuka hai, agla try karo
            raise RuntimeError("\n".join(errors)) from exc
        st.session_state.working_model = model
        st.session_state.active_engine = f"Groq ({model})"
        return resp.choices[0].message.content
    raise RuntimeError("Koi bhi model available nahi:\n" + "\n".join(errors))
# ----------------------------------------------------------------- branding
def get_base64_image(image_path):
    if os.path.exists(image_path):
        with open(image_path, "rb") as img_file:
            return base64.b64encode(img_file.read()).decode()
    return None


logo_b64 = get_base64_image("kp_logo.png")
logo_html = (f'<img src="data:image/png;base64,{logo_b64}" '
             'style="height:75px;object-fit:contain;" alt="KP Government logo">'
             if logo_b64 else
             '<div style="height:75px;width:75px;border:1px dashed #ccc;'
             'display:flex;align-items:center;justify-content:center;'
             'font-size:10px;color:#999;">Logo</div>')

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
    .chat-bubble-user { background-color: #e2e8f0; padding: 10px 15px; border-radius: 15px 15px 0 15px; margin-bottom: 10px; width: fit-content; max-width: 80%; margin-left: auto; }
    .chat-bubble-ai { background-color: #d1fae5; padding: 10px 15px; border-radius: 15px 15px 15px 0; margin-bottom: 10px; width: fit-content; max-width: 80%; }
    .engine-badge { display: inline-block; padding: 5px 12px; background: #e2e8f0; border-radius: 20px; font-size: 12px; font-weight: 600; color: #334155; margin-bottom: 15px; }
    @media print {
        [data-testid="stSidebar"], header, .stButton, .stTabs [data-baseweb="tab-list"], .stFileUploader, .stChatInput { display: none !important; }
        [data-testid="stAppViewContainer"] { background-color: white !important; }
        .custom-header { border-bottom: 2px solid #059669 !important; margin-bottom: 20px !important; }
    }
</style>
""", unsafe_allow_html=True)

st.markdown(f"""
<div class="custom-header" style="display:flex;align-items:center;justify-content:space-between;background:white;padding:20px;border-radius:12px;box-shadow:0 4px 10px rgba(0,0,0,0.05);border-top:5px solid #059669;margin-bottom:30px;">
    <div style="flex:1;"></div>
    <div style="flex:3;text-align:center;">
        <h1 style="color:#059669;margin:0;font-size:28px;font-weight:900;letter-spacing:1.5px;">PC-1-2 ALL IN ONE SOLUTION HUB</h1>
        <p style="color:#64748b;margin:5px 0 0 0;font-size:14px;font-weight:700;letter-spacing:3px;">MADE BY KALEEM</p>
    </div>
    <div style="flex:1;display:flex;justify-content:flex-end;">{logo_html}</div>
</div>
""", unsafe_allow_html=True)
# ----------------------------------------------------------------- pipeline
PARSE_PROMPT = """Parse this PC-1 document text and return ONLY valid JSON.
Extract project title, department name, total budget, component breakdown, and
district-wise allocation with approximate latitude/longitude for each KPK district.

Schema:
{
  "projectTitle": "string",
  "departmentName": "string",
  "totalBudget": 0,
  "components": [{"name": "string", "cost": 0}],
  "districtWiseAllocation": [{"district": "string", "amount": 0, "latitude": 0.0, "longitude": 0.0}]
}

Rules: all numbers must be JSON numbers (no commas, no currency words). If a
value is absent in the document use 0 or "" instead of guessing."""


def process_document(uploaded):
    # ---- stage 1: PDF se text
    try:
        text, n_pages = pdf_to_text(uploaded)
    except Exception as exc:
        st.error("PDF khul nahi saka — file corrupt ya password-protected lagti hai.")
        st.exception(exc)
        return
    if not text.strip():
        st.error(f"{n_pages} page mile magar text 0 characters — ye scanned/image "
                 "PDF hai. Pehle OCR karein (`ocrmypdf in.pdf out.pdf`) phir upload "
                 "karein; warna AI ke paas parhne ke liye kuch nahi hota.")
        return
    st.session_state.doc_text = text
    st.caption(f"✅ {n_pages} pages se {len(text):,} characters extract huye.")

    clipped = text[:MAX_DOC_CHARS]
    if len(text) > MAX_DOC_CHARS:
        st.warning(f"Document bara hai — AI ko pehle {MAX_DOC_CHARS:,} characters "
                   "bheje ja rahe hain. Poora document bhejne par free tier ka "
                   "tokens-per-minute limit 413/429 error deta hai.")

    # ---- stage 2: AI parsing (DB se alag, taake ghalti ka source saaf rahe)
    with st.spinner("AI document parse kar raha hai…"):
        try:
            raw = call_groq(
                [{"role": "system", "content": "You are a precise financial "
                  "document parser. Output strict JSON only."},
                 {"role": "user",
                  "content": f"{PARSE_PROMPT}\n\nDocument Text:\n{clipped}"}],
                json_mode=True, max_tokens=8000, temperature=0.1)
            data = extract_json(raw)
        except Exception as exc:
            st.error(f"❌ AI stage fail hui:\n\n{exc}")
            st.info("Diagnostics tab → 'AI connection test' chalayein; wahan asli "
                    "Groq error milta hai (model band / key ghalat / rate limit).")
            return
    st.session_state.doc_data = data
    st.success(f"✅ AI parsing kamyaab — {st.session_state.active_engine}")
    # ---- stage 3: database (fail ho to bhi AI ka natija screen par rehta hai)
    with st.spinner("Supabase mein securely save…"):
        try:
            supabase.rpc('insert_secure_pc1', {
                'p_project_title': data.get('projectTitle') or 'Unknown',
                'p_department': data.get('departmentName') or 'Unknown',
                'p_raw_payload': text,
                'p_secret_key': DB_SECRET,
                'p_total_budget': safe_num(data.get('totalBudget')),
                'p_district_allocations': as_list(data.get('districtWiseAllocation')),
                'p_verification_status': 'VERIFIED',
            }).execute()
            st.success("✅ Database mein save ho gaya.")
        except Exception as exc:
            st.warning("AI ne kaam kar diya lekin database save fail hua — data "
                       "screen par mojood hai, DB error neeche hai.")
            st.exception(exc)


# ----------------------------------------------------------------- tabs
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
    ["📤 Upload PDF", "📊 PC-1 Breakdown", "💬 AI Chat",
     "📝 DB & Comments", "🗺️ Maps", "🔧 Diagnostics"])

with tab1:
    uploaded_file = st.file_uploader("Upload PC-1 / PC-2 Document (PDF Format)",
                                     type=['pdf'])
    if uploaded_file and st.button("🚀 Process & Secure Document"):
        process_document(uploaded_file)

with tab2:
    d = st.session_state.doc_data
    if not d:
        st.warning("Pehle document upload karein.")
    else:
        col1, col2 = st.columns(2)
        with col1:
            st.info(f"**Title:**\n{d.get('projectTitle') or '—'}")
        with col2:
            st.success(f"**Budget:**\nPKR {money(d.get('totalBudget'))}")
        st.markdown("### 📋 Components")
        components = [{"Component": c.get("name") or c.get("component") or "—",
                       "Cost (PKR)": money(c.get("cost") or c.get("amount"))}
                      for c in as_list(d.get('components')) if isinstance(c, dict)]
        st.table(components or [{"Component": "—", "Cost (PKR)": "0"}])
        st.markdown("### 📍 Districts")
        districts = [{"District": x.get("district") or x.get("name") or "—",
                      "Amount (PKR)": money(x.get("amount"))}
                     for x in as_list(d.get('districtWiseAllocation'))
                     if isinstance(x, dict)]
        st.table(districts or [{"District": "—", "Amount (PKR)": "0"}])
        with st.expander("Raw AI JSON (debugging ke liye)"):
            st.json(d)
CHAT_SYSTEM = ("You are a professional financial auditor for the KPK Government. "
               "Answer strictly from the document context below. If something is "
               "not in the context, say so plainly.\n\nDocument Context:\n")

with tab3:
    if not st.session_state.doc_text:
        st.warning("Pehle document upload karein.")
    else:
        st.markdown(f"<div class='engine-badge'>Active Engine: "
                    f"{st.session_state.active_engine}</div>",
                    unsafe_allow_html=True)
        for msg in st.session_state.chat_history:
            bubble(msg["role"], msg["content"])

        user_input = st.chat_input("Ask a question…")
        if user_input:
            st.session_state.chat_history.append({"role": "user",
                                                  "content": user_input})
            # purani baat-cheet bhi bhejein, warna har sawal alag-thalag hota hai
            recent = st.session_state.chat_history[-CHAT_MEMORY_TURNS * 2:]
            messages = [{"role": "system", "content":
                         CHAT_SYSTEM + st.session_state.doc_text[:MAX_CHAT_DOC_CHARS]}]
            for m in recent:
                messages.append({
                    "role": "assistant" if m["role"] in ("ai", "assistant") else "user",
                    "content": m["content"]})
            sent = False
            with st.spinner("Groq soch raha hai…"):
                try:
                    reply = call_groq(messages, max_tokens=1500, temperature=0.3)
                    st.session_state.chat_history.append({"role": "assistant",
                                                          "content": reply})
                    sent = True
                except Exception as exc:
                    st.session_state.chat_history.pop()   # nakaam sawal na rahe
                    st.error(f"❌ Chat fail hui:\n\n{exc}")
            if sent:
                st.rerun()

with tab4:
    projects = None
    try:
        projects = supabase.table("secure_pc1").select("id, project_title").execute()
    except Exception as exc:
        st.error("Supabase se project list nahi mili:")
        st.exception(exc)
    if projects is not None and projects.data:
        chosen = st.selectbox("Select Project:", options=projects.data,
                              format_func=lambda r: f"{r['project_title']} (#{r['id']})")
        proj_id = chosen["id"]
        try:
            comments = supabase.table("pc1_comments").select("*") \
                .eq("pc1_id", proj_id).order("created_at", desc=False).execute()
            for c in comments.data or []:
                st.markdown(f"**{c.get('commenter_name', '—')}**: "
                            f"{c.get('comment_text', '')}")
                st.divider()
        except Exception as exc:
            st.error("Comments load nahi ho sake:")
            st.exception(exc)
        with st.form("comment_form"):
            c_name = st.text_input("Name:")
            c_text = st.text_area("Review:")
            if st.form_submit_button("Submit"):
                if not (c_name.strip() and c_text.strip()):
                    st.warning("Naam aur review dono likhna zaroori hai.")
                else:
                    try:
                        supabase.table("pc1_comments").insert(
                            {"pc1_id": proj_id, "commenter_name": c_name,
                             "comment_text": c_text}).execute()
                        st.rerun()
                    except Exception as exc:
                        st.error("Comment save nahi hua:")
                        st.exception(exc)
    elif projects is not None:
        st.info("Database khaali hai — pehle koi document process karein.")
with tab5:
    try:
        res = supabase.table("secure_pc1") \
            .select("project_title, district_allocations").execute()
    except Exception as exc:
        st.error("Map ka data load nahi hua:")
        st.exception(exc)
        res = None
    if res is not None:
        kp_map = folium.Map(location=[34.0151, 71.5249], zoom_start=7,
                            tiles="CartoDB positron")
        markers = 0
        for proj in res.data or []:
            # column agar text hai to yahan str aata hai — as_list us ko handle karta hai
            for loc in as_list(proj.get('district_allocations')):
                if not isinstance(loc, dict):
                    continue
                lat = safe_num(loc.get('latitude'), None)
                lon = safe_num(loc.get('longitude'), None)
                if lat in (None, 0) or lon in (None, 0):
                    continue
                folium.Marker(
                    location=[lat, lon],
                    popup=f"{proj.get('project_title', '')} — "
                          f"{loc.get('district', '')}: PKR {money(loc.get('amount'))}"
                ).add_to(kp_map)
                markers += 1
        st_folium(kp_map, width=1000, height=500, returned_objects=[])
        if markers == 0:
            st.info("Koi marker nahi bana — matlab district_allocations khaali hai "
                    "ya us mein latitude/longitude mojood nahi.")
        else:
            st.caption(f"{markers} district markers.")
with tab6:
    st.markdown("### 🔧 Diagnostics")
    st.write("**Secrets:**", {k: ("set ✅" if k in st.secrets else "MISSING ❌")
                              for k in ("GROQ_API_KEY", "SUPABASE_URL",
                                        "SUPABASE_KEY", "DB_SECRET_KEY")})
    versions = {}
    for pkg in ("streamlit", "groq", "supabase", "pypdf", "PyPDF2",
                "folium", "streamlit-folium", "httpx"):
        try:
            versions[pkg] = importlib.metadata.version(pkg)
        except Exception:
            versions[pkg] = "—"
    st.write("**Versions:**", versions)
    st.write("**Model try order:**", MODEL_CANDIDATES)
    st.write("**Last working model:**",
             st.session_state.working_model or "abhi tak koi call kamyaab nahi hui")

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("🔌 AI connection test"):
            try:
                reply = call_groq([{"role": "user",
                                    "content": "Reply with exactly: OK"}],
                                  max_tokens=10)
                st.success(f"Model zinda hai → {st.session_state.working_model}\n\n"
                           f"Jawab: {reply.strip()}")
            except Exception as exc:
                st.error("AI call fail hui — asli Groq error:")
                st.exception(exc)
    with col_b:
        if st.button("🗄️ Database connection test"):
            try:
                probe = supabase.table("secure_pc1").select("id").limit(1).execute()
                st.success(f"Supabase reachable — sample rows: "
                           f"{len(probe.data or [])}")
            except Exception as exc:
                st.error("Supabase call fail hui:")
                st.exception(exc)
