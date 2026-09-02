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
import time

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
MAX_CHAT_DOC_CHARS = 12_000
CHAT_MEMORY_TURNS = 4

# Groq free tier ka TPM (tokens per minute) bohot chhota hai — aur Groq
# "Requested" mein input tokens + max_tokens DONO ginta hai. Aapke org par
# limit 8000 thi, is liye akela max_tokens=8000 hi poora budget kha gaya tha.
TPM_LIMIT = 8_000            # Diagnostics tab se badal sakte hain
CHARS_PER_TOKEN = 2.2        # PC-1 tables/numbers itni tight tokenize hoti hain
SAFETY_TOKENS = 400          # system prompt + JSON overhead ka margin
PARSE_OUTPUT_TOKENS = 1_600  # JSON jawab ke liye reserve
CHAT_OUTPUT_TOKENS = 800
CHUNK_OVERLAP_CHARS = 250

st.set_page_config(page_title="PC-1-2 Solution Hub", layout="wide",
                   initial_sidebar_state="collapsed")

for _key, _val in {"doc_text": "", "doc_data": None, "chat_history": [],
                   "active_engine": "Pending", "working_model": None,
                   "tpm_log": [], "tpm_limit": TPM_LIMIT}.items():
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


# --------------------------------------------- token budget / TPM management
class TpmError(RuntimeError):
    """413 / 429 — request TPM window se bari thi."""

    def __init__(self, message, limit=None, requested=None):
        super().__init__(message)
        self.limit = limit
        self.requested = requested


def parse_tpm_error(exc):
    """Groq ke message se 'Limit 8000, Requested 19287' nikaal leta hai."""
    msg = str(exc)
    if "rate_limit_exceeded" not in msg and "Request too large" not in msg:
        return None
    lim = re.search(r"Limit\s+([\d,]+)", msg)
    req = re.search(r"Requested\s+([\d,]+)", msg)
    return TpmError(msg,
                    int(lim.group(1).replace(",", "")) if lim else None,
                    int(req.group(1).replace(",", "")) if req else None)


def estimate_tokens(text):
    return int(len(text) / CHARS_PER_TOKEN) + 1


def input_char_budget(output_tokens):
    """Aik request mein kitne characters bhej sakte hain. Groq 'Requested' mein
    input tokens AUR max_tokens dono ginta hai — yahi 413 ki asal wajah thi."""
    room = st.session_state.tpm_limit - output_tokens - SAFETY_TOKENS
    return max(800, int(room * CHARS_PER_TOKEN))


def tpm_used():
    now = time.time()
    st.session_state.tpm_log = [(t, n) for t, n in st.session_state.tpm_log
                                if now - t < 60]
    return sum(n for _, n in st.session_state.tpm_log)


def tpm_wait_if_needed(planned_tokens):
    """60-second window mein jagah na bache to khud intezar karta hai —
    429 khaane se behtar hai."""
    used = tpm_used()
    if used + planned_tokens <= st.session_state.tpm_limit:
        return
    oldest = min(t for t, _ in st.session_state.tpm_log)
    wait = max(1, int(62 - (time.time() - oldest)))
    box = st.empty()
    for left in range(wait, 0, -1):
        box.info(f"⏳ TPM window bhar gaya ({used:,}/{st.session_state.tpm_limit:,} "
                 f"tokens pichle minute mein) — {left}s intezar…")
        time.sleep(1)
    box.empty()
    tpm_used()


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


def call_groq(messages, json_mode=False, max_tokens=1600, temperature=0.2):
    """Pehla zinda model use karta hai. Model band ho jaye to khud agla try karta
    hai. TPM window ka hisaab bhi rakhta hai aur 413/429 ko TpmError banata hai
    taake caller text chhota kar ke dobara koshish kar sake."""
    planned = sum(estimate_tokens(m["content"]) for m in messages) + max_tokens
    tpm_wait_if_needed(planned)

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
            tpm = parse_tpm_error(exc)
            if tpm is not None:
                if tpm.limit:                     # asli limit yaad rakh lo
                    st.session_state.tpm_limit = tpm.limit
                st.session_state.tpm_log.append((time.time(), planned))
                raise tpm from exc
            errors.append(f"• {model} → {exc}")
            if _looks_dead(exc):
                continue                          # ye model mar chuka hai
            raise RuntimeError("\n".join(errors)) from exc
        used = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
        st.session_state.tpm_log.append((time.time(), max(used, planned)))
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
# ------------------------------------- KPK districts (locally, AI se nahi)
# Approximate district-headquarter coordinates. Ye locally rakhne ke do faide:
# AI ka output aadha ho gaya (TPM bachta hai) aur hallucinated coordinates
# ka masla khatam.
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
    "jamrud": "khyber", "judbah": "torghar",
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
    for known in KPK_COORDS:              # "swat valley" -> swat
        if known in key or key in known:
            return KPK_COORDS[known]
    lat, lon = safe_num(fallback_lat, None), safe_num(fallback_lon, None)
    return (lat, lon) if (lat and lon) else (None, None)


# --------------------------------- document ko TPM budget ke andar laana
SCORE_WORDS = ("total", "cost", "budget", "million", "billion", "pkr", "rs.",
               "component", "allocation", "district", "estimate", "phase",
               "financial", "capital", "revenue", "scheme", "project")


def score_block(block):
    low = block.lower()
    score = sum(low.count(w) * 2 for w in SCORE_WORDS)
    score += sum(4 for d in KPK_COORDS if d in low)
    score += sum(c.isdigit() for c in block) // 10
    return score


def smart_extract(text, budget_chars, block_size=1200):
    """Poora document TPM mein nahi aata. Is liye sirf wo blocks bhejte hain
    jin mein budget/district/component ka data hai — pehla block (title aur
    department) hamesha shamil rehta hai."""
    if len(text) <= budget_chars:
        return text, False
    blocks = [text[i:i + block_size] for i in range(0, len(text), block_size)]
    keep, total = {0}, len(blocks[0])
    for i in sorted(range(1, len(blocks)),
                    key=lambda j: score_block(blocks[j]), reverse=True):
        if total + len(blocks[i]) + 8 > budget_chars:
            continue
        keep.add(i)
        total += len(blocks[i]) + 8
    return "\n[…]\n".join(blocks[i] for i in sorted(keep)), True


def context_for_question(text, question, budget_chars):
    """Chat ke liye sawal ke keywords wale hisse chunte hain — chhote token
    budget mein document ke shuru ke 12k chars se kaafi behtar jawab milta hai."""
    if len(text) <= budget_chars:
        return text
    words = re.findall(r"[a-zA-Z]{4,}", question.lower())[:12]
    blocks = [text[i:i + 1200] for i in range(0, len(text), 1200)]

    def rank(i):
        low = blocks[i].lower()
        return sum(low.count(w) * 5 for w in words) + score_block(blocks[i])

    keep, total = set(), 0
    for i in sorted(range(len(blocks)), key=rank, reverse=True):
        if total + len(blocks[i]) + 8 > budget_chars:
            continue
        keep.add(i)
        total += len(blocks[i]) + 8
    return "\n[…]\n".join(blocks[i] for i in sorted(keep))


def deep_chunks(text, budget_chars):
    step = max(500, budget_chars - CHUNK_OVERLAP_CHARS)
    return [text[i:i + budget_chars] for i in range(0, len(text), step)]


def merge_parsed(parts):
    """Deep mode ke kai hisson ka JSON aik mein jorta hai."""
    out = {"projectTitle": "", "departmentName": "", "totalBudget": 0,
           "components": [], "districtWiseAllocation": []}
    comps, dists = {}, {}
    for p in parts:
        for field in ("projectTitle", "departmentName"):
            if not out[field] and str(p.get(field) or "").strip():
                out[field] = str(p[field]).strip()
        out["totalBudget"] = max(safe_num(out["totalBudget"]),
                                 safe_num(p.get("totalBudget")))
        for c in as_list(p.get("components")):
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or c.get("component") or "").strip()
            if not name:
                continue
            cost = safe_num(c.get("cost") or c.get("amount"))
            old = safe_num((comps.get(name.lower()) or {}).get("cost"))
            comps[name.lower()] = {"name": name, "cost": max(cost, old)}
        for x in as_list(p.get("districtWiseAllocation")):
            if not isinstance(x, dict):
                continue
            name = str(x.get("district") or x.get("name") or "").strip()
            if not name:
                continue
            key = norm_district(name)
            amount = safe_num(x.get("amount"))
            # chunk overlap duplicate de sakta hai -> max lete hain, sum nahi
            if key not in dists or amount > safe_num(dists[key].get("amount")):
                dists[key] = {"district": name, "amount": amount}
    out["components"] = list(comps.values())
    out["districtWiseAllocation"] = list(dists.values())
    return out


def attach_coords(data):
    """Coordinates locally lagate hain — AI se poochne ki zaroorat nahi."""
    rows, missing = [], []
    for x in as_list(data.get("districtWiseAllocation")):
        if not isinstance(x, dict):
            continue
        name = x.get("district") or x.get("name") or "—"
        lat, lon = district_coords(name, x.get("latitude"), x.get("longitude"))
        row = {"district": name, "amount": safe_num(x.get("amount"))}
        if lat and lon:
            row["latitude"], row["longitude"] = lat, lon
        else:
            missing.append(str(name))
        rows.append(row)
    data["districtWiseAllocation"] = rows
    return data, missing


# ----------------------------------------------------------------- pipeline
PARSE_PROMPT = """Parse this PC-1 document text and return ONLY valid JSON.

Schema:
{
  "projectTitle": "string",
  "departmentName": "string",
  "totalBudget": 0,
  "components": [{"name": "string", "cost": 0}],
  "districtWiseAllocation": [{"district": "string", "amount": 0}]
}

Rules:
- All numbers must be plain JSON numbers (no commas, no currency words).
- Do NOT output latitude/longitude — coordinates are added by the application.
- If a value is absent use 0 or "", never guess.
- Be concise: no explanations outside the JSON."""

PARSE_SYSTEM = ("You are a precise financial document parser. "
                "Output strict, compact JSON only.")


def parse_slice(text_slice):
    raw = call_groq(
        [{"role": "system", "content": PARSE_SYSTEM},
         {"role": "user", "content": f"{PARSE_PROMPT}\n\nDocument Text:\n{text_slice}"}],
        json_mode=True, max_tokens=PARSE_OUTPUT_TOKENS, temperature=0.1)
    return extract_json(raw)


def parse_with_shrink(text_slice, label=""):
    """413 aane par Groq ke bataye hue Limit/Requested se ratio nikaal kar text
    chhota karte hain aur dobara bhejte hain — user ko kuch tune nahi karna parta."""
    current = text_slice
    for attempt in range(3):
        try:
            return parse_slice(current)
        except TpmError as exc:
            if attempt == 2 or not (exc.limit and exc.requested):
                raise
            allowed = exc.limit - PARSE_OUTPUT_TOKENS - SAFETY_TOKENS
            sent_input = max(1, exc.requested - PARSE_OUTPUT_TOKENS)
            ratio = min(0.9, max(0.15, allowed / sent_input))
            new_len = max(800, int(len(current) * ratio))
            st.info(f"413 aaya{label} — text {len(current):,} → {new_len:,} "
                    "characters kar ke dobara bhej raha hun.")
            current = current[:new_len]
    raise RuntimeError("Shrink ke baad bhi TPM limit paar ho rahi hai.")


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

    budget = max(1_000, input_char_budget(PARSE_OUTPUT_TOKENS) - 700)  # prompt margin
    deep = bool(st.session_state.get("deep_mode"))

    # ---- stage 2: AI parsing (DB se alag, taake ghalti ka source saaf rahe)
    if deep:
        chunks = deep_chunks(text, budget)
        st.info(f"🐢 Deep mode: {len(chunks)} hisse banaye. TPM limit "
                f"({st.session_state.tpm_limit:,} tokens/min) ki wajah se hisson ke "
                f"darmiyan intezar hoga — andaaza {max(1, len(chunks) // 2)} minute.")
        bar = st.progress(0.0, text=f"0/{len(chunks)} hisse")
        parts = []
        for i, chunk in enumerate(chunks, 1):
            try:
                parts.append(parse_with_shrink(chunk, f" (hissa {i})"))
            except Exception as exc:
                st.warning(f"Hissa {i} skip hua: {exc}")
            bar.progress(i / len(chunks), text=f"{i}/{len(chunks)} hisse")
        bar.empty()
        if not parts:
            st.error("❌ Koi bhi hissa parse nahi ho saka.")
            return
        data = merge_parsed(parts)
    else:
        text_slice, trimmed = smart_extract(text, budget)
        if trimmed:
            st.info(f"Document {len(text):,} characters ka hai. TPM limit "
                    f"({st.session_state.tpm_limit:,} tokens/min) ke hisab se sab se "
                    f"ahem {len(text_slice):,} characters bheje ja rahe hain "
                    f"(~{estimate_tokens(text_slice):,} tokens). Poora document "
                    "parse karna ho to 'Deep mode' on kar ke dobara chalayein.")
        with st.spinner("AI document parse kar raha hai…"):
            try:
                data = parse_with_shrink(text_slice)
            except TpmError as exc:
                st.error(f"❌ TPM limit phir bhi paar ho rahi hai:\n\n{exc}")
                st.info("Diagnostics tab mein TPM limit set karein, ya Groq Dev tier "
                        "par upgrade karein.")
                return
            except Exception as exc:
                st.error(f"❌ AI stage fail hui:\n\n{exc}")
                st.info("Diagnostics tab → 'AI connection test' chalayein; wahan asli "
                        "Groq error milta hai (model band / key ghalat / rate limit).")
                return

    data, missing = attach_coords(data)
    st.session_state.doc_data = data
    st.success(f"✅ AI parsing kamyaab — {st.session_state.active_engine}")
    if missing:
        st.caption("⚠️ In naamon ke coordinates local list mein nahi mile: "
                   + ", ".join(sorted(set(missing))[:10]))
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
    st.checkbox("🐢 Deep mode — poora document parse karo (dheema: free tier ki "
                "TPM limit ki wajah se hisson ke darmiyan intezar hota hai)",
                key="deep_mode")
    st.caption(f"Free tier limit: {st.session_state.tpm_limit:,} tokens/min → aik "
               f"request mein takreeban "
               f"{max(1_000, input_char_budget(PARSE_OUTPUT_TOKENS) - 700):,} "
               "characters ja sakte hain.")
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
            hist_chars = sum(len(str(m["content"])) for m in recent)
            doc_budget = max(1_000, input_char_budget(CHAT_OUTPUT_TOKENS)
                             - hist_chars - len(CHAT_SYSTEM) - 200)
            context = context_for_question(st.session_state.doc_text, user_input,
                                          min(MAX_CHAT_DOC_CHARS, doc_budget))
            messages = [{"role": "system", "content": CHAT_SYSTEM + context}]
            for m in recent:
                messages.append({
                    "role": "assistant" if m["role"] in ("ai", "assistant") else "user",
                    "content": m["content"]})
            sent = False
            with st.spinner("Groq soch raha hai…"):
                try:
                    reply = call_groq(messages, max_tokens=CHAT_OUTPUT_TOKENS,
                                      temperature=0.3)
                    st.session_state.chat_history.append({"role": "assistant",
                                                          "content": reply})
                    sent = True
                except TpmError as exc:
                    st.session_state.chat_history.pop()
                    st.error(f"⏳ Token limit: {exc}")
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

    st.markdown("#### Token budget")
    new_limit = st.number_input(
        "TPM limit (tokens per minute) — Groq console → Settings → Limits se dekhein",
        min_value=1_000, max_value=2_000_000, step=1_000,
        value=int(st.session_state.tpm_limit))
    if new_limit != st.session_state.tpm_limit:
        st.session_state.tpm_limit = int(new_limit)
        st.rerun()
    st.write(f"Pichle 60 seconds mein use: **{tpm_used():,}** / "
             f"{st.session_state.tpm_limit:,} tokens  ·  aik request ka input budget: "
             f"**{input_char_budget(PARSE_OUTPUT_TOKENS):,}** characters "
             f"(~{estimate_tokens(' ' * input_char_budget(PARSE_OUTPUT_TOKENS)):,} tokens)")
    st.caption("Groq 'Requested' mein input tokens + max_tokens dono ginta hai — "
               "isi liye purana max_tokens=8000 akela hi 8000 ki limit kha jata tha.")

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
