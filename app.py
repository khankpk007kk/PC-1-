"""
PC-1-2 ALL IN ONE SOLUTION HUB — simple build (2026-09-01)

Is version mein PDF upload aur AI document-parsing wala poora hissa nikaal diya
gaya hai. Sirf 4 cheezein hain aur wo teeno database se chalti hain:

    📊 PC-1 Breakdown   — Supabase mein save project ka breakdown
    💬 AI Chat          — usi project ke data par sawal-jawab
    📝 DB & Comments    — project par comments parhna/likhna
    🗺️ Map              — districts ke markers

Kyun aasan ho gaya: chat/breakdown ab structured DB data par chalte hain (bohot
chhota input) — is liye Groq ki 8000 tokens/minute wali limit se masla khatam.
Column ke naam bhi khud detect hote hain, is liye table ka schema thora mukhtalif
ho to bhi tabs khaali nahi rehte.
"""
import base64
import html
import importlib.metadata
import json
import os
import re
import time

import folium
import streamlit as st
from groq import Groq
from streamlit_folium import st_folium
from supabase import create_client

# ----------------------------------------------------------------- settings
TABLE_PROJECTS = "secure_pc1"
TABLE_COMMENTS = "pc1_comments"
INSERT_RPC = "insert_secure_pc1"

MODEL_CANDIDATES = [
    "openai/gpt-oss-120b",          # llama-3.3-70b-versatile band ho chuka hai
    "openai/gpt-oss-20b",
    "moonshotai/kimi-k2-instruct-0905",
    "meta-llama/llama-4-scout-17b-16e-instruct",
]
DEAD_MODEL_HINTS = ("decommission", "deprecat", "does not exist",
                    "not found", "no longer")

TPM_LIMIT = 8_000            # Groq free tier; sidebar → Diagnostics se badlega
CHARS_PER_TOKEN = 2.2        # PC-1 numbers/tables itni tight tokenize hoti hain
SAFETY_TOKENS = 400
CHAT_OUTPUT_TOKENS = 800
CHAT_MEMORY_TURNS = 4

# aik hi cheez ke kai mumkin column naam — pehla jo mile wahi use hota hai
TITLE_KEYS = ("project_title", "title", "name", "scheme_name", "project_name")
DEPT_KEYS = ("department", "department_name", "dept", "sector")
BUDGET_KEYS = ("total_budget", "budget", "total_cost", "estimated_cost",
               "amount", "capital_cost")
COMP_KEYS = ("components", "component_breakdown", "cost_breakdown", "items")
DIST_KEYS = ("district_allocations", "district_wise_allocation", "districts",
             "district_allocation", "districtwiseallocation")
STATUS_KEYS = ("verification_status", "status")
CREATED_KEYS = ("created_at", "inserted_at", "timestamp", "date")
CNAME_KEYS = ("commenter_name", "name", "author", "user_name", "commenter")
CTEXT_KEYS = ("comment_text", "comment", "text", "review", "body", "message")
LINK_CANDIDATES = ("pc1_id", "project_id", "secure_pc1_id", "pc1")
NAME_ITEM_KEYS = ("name", "component", "component_name", "district",
                  "district_name", "item", "head")
AMOUNT_ITEM_KEYS = ("amount", "cost", "allocation", "budget", "value",
                    "estimated_cost", "share")

st.set_page_config(page_title="PC-1-2 Solution Hub", layout="wide",
                   initial_sidebar_state="expanded")

for _key, _val in {"chat_history": [], "active_engine": "Pending",
                   "working_model": None, "tpm_log": [],
                   "tpm_limit": TPM_LIMIT, "link_col": None,
                   "sel_id": None}.items():
    st.session_state.setdefault(_key, _val)


# ----------------------------------------------------------------- helpers
def safe_num(value, default=0):
    """DB/AI kabhi budget ko '1,200 million' string bana deta hai; f"{x:,}" us par
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


def pick(row, keys, default=None):
    """Column ka naam project_title / projectTitle / PROJECT_TITLE — teeno aik
    hi cheez hain. Underscore aur case hata kar match karte hain."""
    if not isinstance(row, dict):
        return default
    flat = {str(k).lower().replace("_", "").replace(" ", ""): v
            for k, v in row.items()}
    for key in keys:
        probe = key.lower().replace("_", "").replace(" ", "")
        if probe in flat and flat[probe] not in (None, "", []):
            return flat[probe]
    return default

def as_items(value):
    """Supabase ka column jsonb ho, text ho ya dict — teeno se
    [{'name':…, 'amount':…}] banata hai. (text column par seedha for-loop
    chalane se har CHARACTER iterate hota tha — purana blank-tab bug.)"""
    raw = value
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if isinstance(raw, dict):
        inner = None
        for key in ("items", "components", "districts", "allocations", "data"):
            if isinstance(raw.get(key), list):
                inner = raw[key]
                break
        if inner is None:                      # {'Civil Works': 500000} shape
            return [{"name": str(k), "amount": safe_num(v)}
                    for k, v in raw.items() if not isinstance(v, (dict, list))]
        raw = inner
    out = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                out.append({
                    "name": str(pick(item, NAME_ITEM_KEYS, "—")),
                    "amount": safe_num(pick(item, AMOUNT_ITEM_KEYS, 0)),
                    "lat": safe_num(pick(item, ("latitude", "lat")), None),
                    "lon": safe_num(pick(item, ("longitude", "lon", "lng")), None),
                })
            elif isinstance(item, str) and item.strip():
                out.append({"name": item.strip(), "amount": 0,
                            "lat": None, "lon": None})
    return out


def fields(row):
    """Aik DB row se woh sab nikaal lena jo screen par dikhana hai."""
    comps = as_items(pick(row, COMP_KEYS))
    dists = as_items(pick(row, DIST_KEYS))
    return {
        "id": pick(row, ("id", "uuid", "pk")),
        "title": str(pick(row, TITLE_KEYS, "Untitled project")),
        "dept": str(pick(row, DEPT_KEYS, "—")),
        "budget": safe_num(pick(row, BUDGET_KEYS, 0)),
        "status": str(pick(row, STATUS_KEYS, "—")),
        "created": str(pick(row, CREATED_KEYS, "") or "")[:19],
        "components": comps,
        "districts": dists,
    }

# ------------------------------------------------- token budget (Groq free tier)
class TpmError(RuntimeError):
    """413 / 429 — request 60-second token window se bari thi."""


def parse_tpm_error(exc):
    msg = str(exc)
    if "rate_limit_exceeded" in msg or "Request too large" in msg:
        lim = re.search(r"Limit\s+([\d,]+)", msg)
        if lim:
            st.session_state.tpm_limit = int(lim.group(1).replace(",", ""))
        return TpmError(msg)
    return None


def estimate_tokens(text):
    return int(len(str(text)) / CHARS_PER_TOKEN) + 1


def input_char_budget(output_tokens=CHAT_OUTPUT_TOKENS):
    """Groq 'Requested' mein input tokens AUR max_tokens dono ginta hai —
    yahi purane 413 error ki asal wajah thi."""
    room = st.session_state.tpm_limit - output_tokens - SAFETY_TOKENS
    return max(800, int(room * CHARS_PER_TOKEN))


def tpm_used():
    now = time.time()
    st.session_state.tpm_log = [(t, n) for t, n in st.session_state.tpm_log
                                if now - t < 60]
    return sum(n for _, n in st.session_state.tpm_log)


def tpm_wait_if_needed(planned):
    used = tpm_used()
    if used + planned <= st.session_state.tpm_limit or not st.session_state.tpm_log:
        return
    oldest = min(t for t, _ in st.session_state.tpm_log)
    wait = max(1, int(62 - (time.time() - oldest)))
    box = st.empty()
    for left in range(wait, 0, -1):
        box.info(f"⏳ Token window bhar gaya ({used:,}/"
                 f"{st.session_state.tpm_limit:,} per minute) — {left}s intezar…")
        time.sleep(1)
    box.empty()
    tpm_used()

# ----------------------------------------------------------------- clients
@st.cache_resource(show_spinner=False)
def get_clients(supabase_url, supabase_key, groq_key):
    return (create_client(supabase_url, supabase_key),
            Groq(api_key=groq_key, timeout=90.0, max_retries=2))


try:
    supabase, groq_client = get_clients(st.secrets["SUPABASE_URL"],
                                        st.secrets["SUPABASE_KEY"],
                                        st.secrets["GROQ_API_KEY"])
except KeyError as missing:
    st.error(f"⚠️ Secret missing: {missing}. Streamlit → Settings → Secrets mein "
             "SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY daalein "
             "(project add karna ho to DB_SECRET_KEY bhi).")
    st.stop()
except Exception as exc:
    st.error("⚠️ Client banate waqt error — asal wajah neeche hai:")
    st.exception(exc)
    st.stop()

DB_SECRET = st.secrets.get("DB_SECRET_KEY", "")


def _looks_dead(exc):
    low = str(exc).lower()
    return any(hint in low for hint in DEAD_MODEL_HINTS)


def call_groq(messages, max_tokens=CHAT_OUTPUT_TOKENS, temperature=0.3):
    """Pehla zinda model use karta hai; model band ho to khud agla try karta hai."""
    planned = sum(estimate_tokens(m["content"]) for m in messages) + max_tokens
    tpm_wait_if_needed(planned)

    order = list(MODEL_CANDIDATES)
    working = st.session_state.working_model
    if working in order:
        order.remove(working)
        order.insert(0, working)

    errors = []
    for model in order:
        try:
            resp = groq_client.chat.completions.create(
                model=model, messages=messages, temperature=temperature,
                max_tokens=max_tokens)
        except Exception as exc:
            tpm = parse_tpm_error(exc)
            if tpm is not None:
                st.session_state.tpm_log.append((time.time(), planned))
                raise tpm from exc
            errors.append(f"• {model} → {exc}")
            if _looks_dead(exc):
                continue                       # ye model mar chuka hai, agla dekho
            raise RuntimeError("\n".join(errors)) from exc
        used = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
        st.session_state.tpm_log.append((time.time(), max(used, planned)))
        st.session_state.working_model = model
        st.session_state.active_engine = f"Groq ({model})"
        return resp.choices[0].message.content
    raise RuntimeError("Koi bhi model available nahi:\n" + "\n".join(errors))

# ----------------------------------------------------------------- database
def load_projects():
    """select('*') — column ka naam guess nahi karte, warna
    'column secure_pc1.project_title does not exist' par poora tab mar jata hai."""
    try:
        res = supabase.table(TABLE_PROJECTS).select("*").execute()
        rows = [r for r in (res.data or []) if isinstance(r, dict)]
        rows.sort(key=lambda r: str(pick(r, CREATED_KEYS, "")), reverse=True)
        return rows, None
    except Exception as exc:
        return [], exc


def link_column(project_id):
    """pc1_comments mein project ka reference kis column mein hai — pc1_id,
    project_id…? Aik dafa detect kar ke session mein yaad rakh lete hain."""
    if st.session_state.link_col:
        return st.session_state.link_col, None
    last = None
    for col in LINK_CANDIDATES:
        try:
            supabase.table(TABLE_COMMENTS).select("*").eq(col, project_id) \
                .limit(1).execute()
            st.session_state.link_col = col
            return col, None
        except Exception as exc:
            last = exc
            if "does not exist" not in str(exc).lower():
                break                          # RLS/permission — column ka masla nahi
    return None, last


def load_comments(project_id):
    col, err = link_column(project_id)
    if not col:
        return [], err
    try:
        query = supabase.table(TABLE_COMMENTS).select("*").eq(col, project_id)
        try:                                   # created_at na ho to order chhor dein
            res = query.order("created_at", desc=False).execute()
        except Exception:
            res = supabase.table(TABLE_COMMENTS).select("*") \
                .eq(col, project_id).execute()
        return [r for r in (res.data or []) if isinstance(r, dict)], None
    except Exception as exc:
        return [], exc


def add_comment(project_id, name, text):
    col, err = link_column(project_id)
    if not col:
        return err or RuntimeError("pc1_comments mein link column nahi mila.")
    payloads = [{col: project_id, "commenter_name": name, "comment_text": text},
                {col: project_id, "name": name, "comment": text},
                {col: project_id, "author": name, "text": text}]
    problems = []
    for payload in payloads:
        try:
            supabase.table(TABLE_COMMENTS).insert(payload).execute()
            return None
        except Exception as exc:
            problems.append(f"{list(payload)[1:]} → {exc}")
            if "does not exist" not in str(exc).lower():
                break                          # RLS ya NOT NULL — naam sahi tha
    return RuntimeError("Comment insert fail hua — jo koshishein ki gayin:\n\n"
                        + "\n\n".join(problems))

def insert_project(title, dept, budget, comps, dists):
    """Teen raste try karte hain: poora table insert → components ke baghair →
    purana secure RPC. Jo chal jaye, uska naam wapas aata hai."""
    base = {"project_title": title, "department": dept,
            "total_budget": budget, "district_allocations": dists,
            "verification_status": "MANUAL"}
    attempts = [("table insert (components ke saath)", dict(base, components=comps)),
                ("table insert", base)]
    problems = []
    for label, payload in attempts:
        try:
            supabase.table(TABLE_PROJECTS).insert(payload).execute()
            return label, None
        except Exception as exc:
            problems.append(f"• {label} → {exc}")
            if "does not exist" not in str(exc).lower():
                break                          # RLS/permission — schema theek hai
    try:
        supabase.rpc(INSERT_RPC, {
            "p_project_title": title, "p_department": dept,
            "p_raw_payload": json.dumps({"components": comps,
                                         "districts": dists}),
            "p_secret_key": DB_SECRET,
            "p_total_budget": budget,
            "p_district_allocations": dists,
            "p_verification_status": "MANUAL",
        }).execute()
        return f"{INSERT_RPC} RPC", None
    except Exception as exc:
        problems.append(f"• {INSERT_RPC} RPC → {exc}")
    return None, RuntimeError("\n".join(problems))


def table_columns(table):
    try:
        res = supabase.table(table).select("*").limit(1).execute()
        rows = res.data or []
        return (sorted(rows[0]) if rows else []), len(rows), None
    except Exception as exc:
        return [], 0, exc

# ------------------------------------- KPK districts (locally, AI se nahi)
KPK_COORDS = {
    "peshawar": (34.0151, 71.5249), "nowshera": (34.0153, 71.9747),
    "charsadda": (34.1682, 71.7404), "mardan": (34.1989, 72.0231),
    "swabi": (34.1202, 72.4696), "kohat": (33.5869, 71.4414),
    "karak": (33.1167, 71.0937), "hangu": (33.5333, 71.0500),
    "bannu": (32.9889, 70.6056), "lakki marwat": (32.6072, 70.9111),
    "dera ismail khan": (31.8313, 70.9017), "tank": (32.2167, 70.3833),
    "abbottabad": (34.1688, 73.2215), "haripur": (33.9942, 72.9333),
    "mansehra": (34.3300, 73.1968), "battagram": (34.6797, 73.0233),
    "torghar": (34.5500, 72.8500), "upper kohistan": (35.2900, 73.2800),
    "lower kohistan": (35.1000, 73.0000), "kolai-palas": (34.8500, 73.0500),
    "shangla": (34.8833, 72.7167), "swat": (34.7717, 72.3600),
    "buner": (34.4167, 72.4667), "malakand": (34.5667, 71.9333),
    "lower dir": (34.8300, 71.8400), "upper dir": (35.2072, 71.8747),
    "lower chitral": (35.8518, 71.7864), "upper chitral": (36.2800, 72.2100),
    "bajaur": (34.7500, 71.5300), "mohmand": (34.3100, 71.4200),
    "khyber": (34.0200, 71.3800), "kurram": (33.9000, 70.1000),
    "orakzai": (33.6600, 70.9400), "north waziristan": (33.0000, 70.0700),
    "upper south waziristan": (32.3000, 69.5700),
    "lower south waziristan": (32.0500, 69.9000),
}
DISTRICT_ALIASES = {
    "d.i. khan": "dera ismail khan", "d.i.khan": "dera ismail khan",
    "di khan": "dera ismail khan", "dikhan": "dera ismail khan",
    "dera ismael khan": "dera ismail khan", "mingora": "swat",
    "saidu sharif": "swat", "timergara": "lower dir", "dir lower": "lower dir",
    "dir": "lower dir", "dir upper": "upper dir", "chitral": "lower chitral",
    "chitral lower": "lower chitral", "chitral upper": "upper chitral",
    "kohistan": "lower kohistan", "kohistan upper": "upper kohistan",
    "kohistan lower": "lower kohistan", "kolai palas": "kolai-palas",
    "south waziristan": "lower south waziristan",
    "waziristan north": "north waziristan", "batkhela": "malakand",
    "daggar": "buner", "alpuri": "shangla", "parachinar": "kurram",
    "miranshah": "north waziristan", "wana": "upper south waziristan",
    "kalaya": "orakzai", "ghalanai": "mohmand", "khar": "bajaur",
    "jamrud": "khyber",
}


def norm_district(name):
    key = re.sub(r"\b(district|agency|tehsil)\b", " ", str(name).lower())
    key = re.sub(r"[^a-z\s.\-]", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    return DISTRICT_ALIASES.get(key, key)


def district_coords(name, fallback_lat=None, fallback_lon=None):
    key = norm_district(name)
    if key in KPK_COORDS:
        return KPK_COORDS[key]
    for known in KPK_COORDS:                   # "swat valley" → swat
        if key and (known in key or key in known):
            return KPK_COORDS[known]
    lat, lon = safe_num(fallback_lat, None), safe_num(fallback_lon, None)
    return (lat, lon) if (lat and lon) else (None, None)

# ----------------------------------------------------------------- branding
def get_base64_image(path):
    if os.path.exists(path):
        with open(path, "rb") as img:
            return base64.b64encode(img.read()).decode()
    return None


_logo = get_base64_image("kp_logo.png")
logo_html = (f'<img src="data:image/png;base64,{_logo}" '
             'style="height:70px;object-fit:contain;" alt="KP Government logo">'
             if _logo else '')

st.markdown("""
<style>
    [data-testid="stAppViewContainer"] { background-color: #F4F7F6 !important; }
    [data-testid="stHeader"] { background-color: transparent !important; }
    p, h1, h2, h3, h4, h5, h6, span, div, label { color: #1e293b !important; }
    .stTabs [data-baseweb="tab-list"] { background-color: #ffffff; padding: 5px 15px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); gap: 15px; }
    .stTabs [data-baseweb="tab"] { color: #64748b !important; font-weight: 600; padding: 12px 0px; }
    .stTabs [aria-selected="true"] { color: #059669 !important; border-bottom: 3px solid #059669 !important; }
    .stButton button { background-color: #059669 !important; color: white !important; font-weight: 700 !important; border-radius: 8px !important; border: none !important; width: 100%; }
    .stButton button:hover { background-color: #047857 !important; }
    .chat-bubble-user { background-color: #e2e8f0; padding: 10px 15px; border-radius: 15px 15px 0 15px; margin-bottom: 10px; width: fit-content; max-width: 85%; margin-left: auto; }
    .chat-bubble-ai { background-color: #d1fae5; padding: 10px 15px; border-radius: 15px 15px 15px 0; margin-bottom: 10px; width: fit-content; max-width: 85%; }
    .cmt { background:#ffffff; border-left:4px solid #059669; padding:10px 14px; border-radius:8px; margin-bottom:10px; box-shadow:0 1px 3px rgba(0,0,0,0.05); }
    .engine-badge { display:inline-block; padding:5px 12px; background:#e2e8f0; border-radius:20px; font-size:12px; font-weight:600; margin-bottom:12px; }
</style>
""", unsafe_allow_html=True)

st.markdown(f"""
<div style="display:flex;align-items:center;justify-content:space-between;background:white;padding:18px;border-radius:12px;box-shadow:0 4px 10px rgba(0,0,0,0.05);border-top:5px solid #059669;margin-bottom:22px;">
    <div style="flex:1;"></div>
    <div style="flex:3;text-align:center;">
        <h1 style="color:#059669;margin:0;font-size:26px;font-weight:900;letter-spacing:1.5px;">PC-1-2 ALL IN ONE SOLUTION HUB</h1>
        <p style="color:#64748b;margin:5px 0 0 0;font-size:13px;font-weight:700;letter-spacing:3px;">MADE BY KALEEM</p>
    </div>
    <div style="flex:1;display:flex;justify-content:flex-end;">{logo_html}</div>
</div>
""", unsafe_allow_html=True)


def bubble(role, text):
    css = "chat-bubble-user" if role == "user" else "chat-bubble-ai"
    who = "You" if role == "user" else "AI"
    body = html.escape(str(text)).replace("\n", "<br>")
    st.markdown(f'<div class="{css}"><b>{who}:</b> {body}</div>',
                unsafe_allow_html=True)

def parse_lines(blob):
    """'Swat: 500000' jaisi lines ko [{'name':…, 'amount':…}] banata hai."""
    items = []
    for line in str(blob).splitlines():
        line = line.strip(" -•\t")
        if not line:
            continue
        name, _, amount = line.partition(":")
        if not _:
            name, _, amount = line.rpartition(" ")
        items.append({"name": (name or line).strip() or "—",
                      "amount": safe_num(amount)})
    return items


SAMPLE = {
    "add_title": "Construction of 50-Bed Category-D Hospital, Swat",
    "add_dept": "Health Department",
    "add_budget": 850_000_000.0,
    "add_comps": ("Civil works: 520000000\nMedical equipment: 210000000\n"
                  "Consultancy: 45000000\nContingencies: 75000000"),
    "add_dists": "Swat: 500000000\nBuner: 200000000\nShangla: 150000000",
}

# ----------------------------------------------------------------- data + sidebar
rows, load_err = load_projects()
projects = [fields(r) for r in rows]

with st.sidebar:
    st.markdown("### 📁 Project")
    if st.button("🔄 Refresh"):
        st.session_state.link_col = None
        st.rerun()

    sel = sel_row = None
    if projects:
        ids = [p["id"] for p in projects]
        labels = [f"{p['title'][:42]}  ·  #{p['id']}" for p in projects]
        start = ids.index(st.session_state.sel_id) \
            if st.session_state.sel_id in ids else 0
        idx = st.selectbox("Kis project par kaam karna hai?",
                           options=range(len(labels)), index=start,
                           format_func=lambda i: labels[i])
        st.session_state.sel_id = ids[idx]
        if st.session_state.get("chat_for") != ids[idx]:
            st.session_state.chat_history = []      # doosre project ki baat na chale
            st.session_state.chat_for = ids[idx]
        sel, sel_row = projects[idx], rows[idx]
        st.caption(f"DB mein kul {len(projects)} projects.")
    elif load_err is None:
        st.warning("DB khaali hai — neeche se aik project add karein.")

    with st.expander("➕ Naya project add karein"):
        if st.button("📋 Sample data bhar dein"):
            for k, v in SAMPLE.items():
                st.session_state[k] = v
            st.rerun()
        with st.form("add_project"):
            a_title = st.text_input("Project title", key="add_title")
            a_dept = st.text_input("Department", key="add_dept")
            a_budget = st.number_input("Total budget (PKR)", min_value=0.0,
                                       step=1_000_000.0, format="%.0f",
                                       key="add_budget")
            a_comps = st.text_area("Components — har line: Naam: raqam",
                                   key="add_comps", height=90)
            a_dists = st.text_area("Districts — har line: District: raqam",
                                   key="add_dists", height=90)
            if st.form_submit_button("💾 Database mein save karein"):
                if not a_title.strip():
                    st.warning("Title likhna zaroori hai.")
                else:
                    path, err = insert_project(
                        a_title.strip(), a_dept.strip() or "—",
                        safe_num(a_budget), parse_lines(a_comps),
                        parse_lines(a_dists))
                    if err is None:
                        st.session_state["flash"] = (
                            f"✅ Project save ho gaya ({path}).")
                        st.rerun()
                    else:
                        st.error("Save nahi hua — teeno raste fail huye:")
                        st.exception(err)

with st.sidebar:
    with st.expander("🔧 Diagnostics"):
        st.write({k: ("set ✅" if k in st.secrets else "MISSING ❌")
                  for k in ("GROQ_API_KEY", "SUPABASE_URL", "SUPABASE_KEY",
                            "DB_SECRET_KEY")})
        vers = {}
        for pkg in ("streamlit", "groq", "supabase", "folium",
                    "streamlit-folium", "httpx"):
            try:
                vers[pkg] = importlib.metadata.version(pkg)
            except Exception:
                vers[pkg] = "—"
        st.write(vers)
        st.write("**Model:**", st.session_state.working_model
                 or f"abhi tak koi call nahi; order: {MODEL_CANDIDATES[0]} …")
        st.write("**Comments link column:**",
                 st.session_state.link_col or "abhi detect nahi hua")
        limit = st.number_input("TPM limit (Groq console → Limits)",
                                min_value=1_000, max_value=2_000_000, step=1_000,
                                value=int(st.session_state.tpm_limit))
        if int(limit) != st.session_state.tpm_limit:
            st.session_state.tpm_limit = int(limit)
            st.rerun()
        st.caption(f"Pichle 60s mein use: {tpm_used():,} / "
                   f"{st.session_state.tpm_limit:,} tokens")
        if st.button("🔌 AI test"):
            try:
                reply = call_groq([{"role": "user",
                                    "content": "Reply with exactly: OK"}],
                                 max_tokens=10)
                st.success(f"{st.session_state.working_model} → {reply.strip()}")
            except Exception as exc:
                st.error("AI call fail — asli Groq error:")
                st.exception(exc)
        if st.button("🗄️ Database test"):
            for table in (TABLE_PROJECTS, TABLE_COMMENTS):
                cols, n, exc = table_columns(table)
                if exc is not None:
                    st.error(f"`{table}` → error:")
                    st.exception(exc)
                elif n == 0:
                    st.warning(f"`{table}` → 0 rows (khaali table, ya RLS ne "
                               "SELECT rok diya — error nahi aata).")
                else:
                    st.success(f"`{table}` columns: {', '.join(cols)}")

# ----------------------------------------------------------------- top-level state
_flash = st.session_state.pop("flash", None)
if _flash:
    st.success(_flash)

if load_err is not None:
    st.error(f"❌ `{TABLE_PROJECTS}` table read nahi ho saka — asal error:")
    st.exception(load_err)
    st.info("Aam wajuhat: table ka naam mukhtalif hai, ya Row Level Security on "
            "hai magar anon key ke liye SELECT policy nahi hai. "
            "Sidebar → Diagnostics → 'Database test' se confirm karein.")
elif not projects:
    st.info("Database mein aik bhi project nahi mila. Ya table waqai khaali hai "
            "(sidebar → ➕ se sample project add kar ke fauran test karein), ya "
            "RLS SELECT policy missing hai — us soorat mein error nahi aata, "
            "bas 0 rows aate hain.")

tab_break, tab_chat, tab_cmt, tab_map = st.tabs(
    ["📊 PC-1 Breakdown", "💬 AI Chat", "📝 DB & Comments", "🗺️ Map"])

NEED_PROJECT = ("Sidebar se project chunein — ya agar list khaali hai to "
                "sidebar → ➕ se aik project add karein.")


def share_rows(items, label):
    total = sum(safe_num(i["amount"]) for i in items) or 0
    out = []
    for i in items:
        amount = safe_num(i["amount"])
        out.append({label: i["name"], "Amount (PKR)": amount,
                    "Share %": round(amount * 100.0 / total, 1) if total else 0.0})
    return out, total


with tab_break:
    if sel is None:
        st.info(NEED_PROJECT)
    else:
        st.markdown(f"#### {sel['title']}")
        st.caption(f"Department: {sel['dept']}  ·  Status: {sel['status']}"
                   + (f"  ·  Added: {sel['created']}" if sel['created'] else "")
                   + f"  ·  ID: {sel['id']}")

        comp_rows, comp_total = share_rows(sel["components"], "Component")
        dist_rows, dist_total = share_rows(sel["districts"], "District")

        m1, m2, m3 = st.columns(3)
        m1.metric("Total budget (PKR)", money(sel["budget"]))
        m2.metric("Components ka total", money(comp_total),
                  delta=(f"{money(comp_total - sel['budget'])} vs total budget"
                         if comp_total else None), delta_color="off")
        m3.metric("Districts ka total", money(dist_total),
                  delta=(f"{money(dist_total - sel['budget'])} vs total budget"
                         if dist_total else None), delta_color="off")
        if comp_total and sel["budget"] and abs(comp_total - sel["budget"]) > 1:
            st.warning(f"⚠️ Components ka jor (PKR {money(comp_total)}) total budget "
                       f"(PKR {money(sel['budget'])}) se "
                       f"{money(abs(comp_total - sel['budget']))} mukhtalif hai.")

        st.markdown("##### 📋 Components")
        if comp_rows:
            st.dataframe(comp_rows, use_container_width=True, hide_index=True)
        else:
            st.caption("Is row mein components ka data nahi hai.")

        st.markdown("##### 📍 District-wise allocation")
        if dist_rows:
            st.dataframe(dist_rows, use_container_width=True, hide_index=True)
            st.bar_chart({"District": [d["name"] for d in sel["districts"]],
                          "Amount": [safe_num(d["amount"])
                                     for d in sel["districts"]]},
                         x="District", y="Amount", height=280)
        else:
            st.caption("Is row mein district allocation ka data nahi hai.")

        with st.expander("🧾 Database row (jaisi hai waisi)"):
            st.json(sel_row)

CHAT_SYSTEM = (
    "You are a professional financial auditor for the Government of Khyber "
    "Pakhtunkhwa reviewing PC-1/PC-2 development schemes. Answer ONLY from the "
    "project data below. If a detail is not in the data, say plainly that the "
    "record does not contain it — never invent numbers. All amounts are in PKR. "
    "Keep answers short, specific and auditor-like.\n\nPROJECT DATA:\n")


def project_context(p):
    lines = [f"Title: {p['title']}", f"Department: {p['dept']}",
             f"Verification status: {p['status']}",
             f"Total budget: {money(p['budget'])}"]
    if p["components"]:
        total = sum(safe_num(c["amount"]) for c in p["components"])
        lines.append("Components (name: amount):")
        lines += [f"- {c['name']}: {money(c['amount'])}" for c in p["components"]]
        lines.append(f"Components add up to: {money(total)} "
                     f"(difference from total budget: "
                     f"{money(total - safe_num(p['budget']))})")
    if p["districts"]:
        total = sum(safe_num(d["amount"]) for d in p["districts"])
        lines.append("District-wise allocation (district: amount):")
        lines += [f"- {d['name']}: {money(d['amount'])}" for d in p["districts"]]
        lines.append(f"Districts add up to: {money(total)}")
    return "\n".join(lines)


def all_context(ps):
    lines = ["Summary of every project in the database "
             "(title | department | budget | districts):"]
    for p in ps[:40]:
        names = ", ".join(d["name"] for d in p["districts"][:6]) or "—"
        lines.append(f"- {p['title']} | {p['dept']} | {money(p['budget'])} | {names}")
    return "\n".join(lines)


with tab_chat:
    if sel is None:
        st.info(NEED_PROJECT)
    else:
        st.markdown(f"<div class='engine-badge'>Engine: "
                    f"{st.session_state.active_engine}</div>",
                    unsafe_allow_html=True)
        wide = st.checkbox("Saare projects ka data bhi bhejein (comparison ke liye)")
        st.caption(f"Context: {'sab projects' if wide else sel['title']}")

        for msg in st.session_state.chat_history:
            bubble(msg["role"], msg["content"])

        question = None
        c1, c2, c3 = st.columns(3)
        if c1.button("Budget summary"):
            question = ("Is project ka budget summary aur components ka breakdown "
                        "batayein.")
        if c2.button("Total match karta hai?"):
            question = ("Kya components aur district allocations ka jor total budget "
                        "se match karta hai? Farq ho to number ke saath batayein.")
        if c3.button("Sab se bara district"):
            question = ("Kis district ko sab se ziyada allocation mila aur kitna "
                        "percentage? Top 3 batayein.")

        with st.form("chat_form", clear_on_submit=True):
            typed = st.text_input("Sawal likhein:",
                                  placeholder="e.g. civil works ka hissa kitna hai?")
            if st.form_submit_button("Send") and typed.strip():
                question = typed.strip()

        if question:
            st.session_state.chat_history.append({"role": "user",
                                                  "content": question})
            ctx = all_context(projects) if wide else project_context(sel)
            room = max(600, input_char_budget(CHAT_OUTPUT_TOKENS)
                       - len(CHAT_SYSTEM) - 600)
            messages = [{"role": "system", "content": CHAT_SYSTEM + ctx[:room]}]
            for m in st.session_state.chat_history[-CHAT_MEMORY_TURNS * 2:]:
                messages.append({"role": "user" if m["role"] == "user"
                                 else "assistant", "content": m["content"]})
            done = False
            with st.spinner("Groq soch raha hai…"):
                try:
                    reply = call_groq(messages, max_tokens=CHAT_OUTPUT_TOKENS)
                    st.session_state.chat_history.append({"role": "assistant",
                                                          "content": reply})
                    done = True
                except TpmError as exc:
                    st.session_state.chat_history.pop()
                    st.error(f"⏳ Token limit: {exc}")
                except Exception as exc:
                    st.session_state.chat_history.pop()
                    st.error("❌ Chat fail hui — asli error:")
                    st.exception(exc)
            if done:
                st.rerun()
        if st.session_state.chat_history and st.button("🧹 Chat clear karein"):
            st.session_state.chat_history = []
            st.rerun()

with tab_cmt:
    if sel is None:
        st.info(NEED_PROJECT)
    else:
        st.markdown(f"#### {sel['title']}")
        comments, cmt_err = load_comments(sel["id"])
        if cmt_err is not None:
            st.error(f"❌ `{TABLE_COMMENTS}` se comments nahi mile — asal error:")
            st.exception(cmt_err)
            st.info("Teen cheezein check karein: table ka naam, project ka link "
                    f"column ({' / '.join(LINK_CANDIDATES)}), aur RLS SELECT policy.")
        st.caption(f"{len(comments)} comments  ·  link column: "
                   f"{st.session_state.link_col or 'detect nahi hua'}")
        for c in comments:
            who = html.escape(str(pick(c, CNAME_KEYS, "—")))
            what = html.escape(str(pick(c, CTEXT_KEYS, ""))).replace("\n", "<br>")
            when = str(pick(c, CREATED_KEYS, "") or "")[:19]
            st.markdown(f"<div class='cmt'><b>{who}</b> "
                        f"<span style='color:#94a3b8;font-size:12px;'>{when}</span>"
                        f"<br>{what}</div>", unsafe_allow_html=True)
        if not comments and cmt_err is None:
            st.caption("Abhi koi comment nahi — pehla comment aap likhein.")

        with st.form("cmt_form", clear_on_submit=True):
            c_name = st.text_input("Aap ka naam")
            c_text = st.text_area("Comment / review", height=100)
            if st.form_submit_button("📝 Comment save karein"):
                if not (c_name.strip() and c_text.strip()):
                    st.warning("Naam aur comment dono likhna zaroori hai.")
                else:
                    err = add_comment(sel["id"], c_name.strip(), c_text.strip())
                    if err is None:
                        st.session_state["flash"] = "✅ Comment save ho gaya."
                        st.rerun()
                    else:
                        st.error("❌ Comment save nahi hua — asal error:")
                        st.exception(err)

with tab_map:
    only_sel = st.checkbox("Sirf selected project ke districts dikhayein",
                           value=False, disabled=sel is None)
    kp_map = folium.Map(location=[34.4, 71.9], zoom_start=7,
                        tiles="CartoDB positron")
    shown, missing = 0, []
    for p in projects:
        is_sel = sel is not None and p["id"] == sel["id"]
        if only_sel and not is_sel:
            continue
        for d in p["districts"]:
            lat, lon = district_coords(d["name"], d.get("lat"), d.get("lon"))
            if not lat or not lon:
                missing.append(str(d["name"]))
                continue
            folium.Marker(
                [lat, lon],
                tooltip=f"{d['name']} — PKR {money(d['amount'])}",
                popup=folium.Popup(f"<b>{html.escape(p['title'])}</b><br>"
                                   f"{html.escape(str(d['name']))}<br>"
                                   f"PKR {money(d['amount'])}", max_width=280),
                icon=folium.Icon(color="green" if is_sel else "blue",
                                 icon="info-sign"),
            ).add_to(kp_map)
            shown += 1
    st_folium(kp_map, width=1000, height=520, returned_objects=[], key="kp_map")
    if shown:
        st.caption(f"{shown} markers (green = selected project). Coordinates "
                   "locally rakhe gaye hain — AI se nahi maange jate.")
    else:
        st.info("Koi marker nahi bana — matlab kisi bhi project mein district "
                "allocation ka data nahi hai.")
    if missing:
        st.caption("Ye naam KPK district list mein nahi mile: "
                   + ", ".join(sorted(set(missing))[:12]))
