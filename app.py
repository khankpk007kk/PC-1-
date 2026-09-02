"""
PC-1-2 ALL IN ONE SOLUTION HUB — upload-first build (2026-09-02)

Aik hi click ka flow: PC-1 ka PDF upload karein, baqi sab khud ho jata hai.

    1. PDF se text nikalta hai (pypdf)
    2. AI (Groq) us text se structured data banata hai — title, department,
       total budget, components, district-wise allocation
    3. Wohi data Supabase (`secure_pc1`) mein save hota hai
    4. AI khud audit review likh kar `pc1_comments` mein comments daal deta hai
    5. Chaaron options — Breakdown, AI Chat, Comments, Map — fauran active

Uske baad user jo bhi sawal likhe, jawab usi uploaded PC-1 se (raw text +
structured data) aata hai. Groq free tier ki 8000 tokens/minute limit ka hisaab
app khud rakhta hai, is liye purana 413 error nahi aata.
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

try:                                  # pypdf naya naam hai, PyPDF2 purana
    from pypdf import PdfReader
except ImportError:
    from PyPDF2 import PdfReader

# ----------------------------------------------------------------- settings
TABLE_PROJECTS = "secure_pc1"
TABLE_COMMENTS = "pc1_comments"
INSERT_RPC = "insert_secure_pc1"
SESSION_ID = "__session__"            # jo PC-1 DB mein save na ho saka
AI_NAME = "AI Auditor"

MODEL_CANDIDATES = [
    "openai/gpt-oss-120b",            # llama-3.3-70b-versatile band ho chuka hai
    "openai/gpt-oss-20b",
    "moonshotai/kimi-k2-instruct-0905",
    "meta-llama/llama-4-scout-17b-16e-instruct",
]
DEAD_MODEL_HINTS = ("decommission", "deprecat", "does not exist",
                    "not found", "no longer")

# ---- token budget (Groq free tier). Ahem: Groq "Requested" mein input tokens
# AUR max_tokens dono ginta hai — purana 413 isi wajah se aata tha.
TPM_LIMIT = 8_000            # sidebar → Diagnostics se badla ja sakta hai
CHARS_PER_TOKEN = 2.2        # PC-1 tables/numbers itni tight tokenize hoti hain
SAFETY_TOKENS = 400
PARSE_OUTPUT_TOKENS = 1_400  # parse ke JSON jawab ke liye
REVIEW_OUTPUT_TOKENS = 450   # AI review ke liye
REVIEW_RESERVE = 1_600       # review call (input+output) ka hissa — parse isay chhorta hai
CHAT_OUTPUT_TOKENS = 700
CHAT_DOC_CHARS = 6_000       # chat ke saath raw document ka max hissa
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
                   "sel_id": None, "doc": None, "doc_sig": None,
                   "chat_for": None}.items():
    st.session_state.setdefault(_key, _val)


# ----------------------------------------------------------------- helpers
def safe_num(value, default=0):
    """AI/DB kabhi budget ko '1,200 million' string bana deta hai; f"{x:,}" us par
    ValueError phenkta hai. Is liye har number yahan se guzarta hai."""
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
    """project_title / projectTitle / PROJECT_TITLE — teeno aik hi cheez hain.
    Underscore aur case hata kar match karte hain."""
    if not isinstance(row, dict):
        return default
    flat = {str(k).lower().replace("_", "").replace(" ", ""): v
            for k, v in row.items()}
    for key in keys:
        probe = key.lower().replace("_", "").replace(" ", "")
        if probe in flat and flat[probe] not in (None, "", []):
            return flat[probe]
    return default


def as_list(value):
    """jsonb column list deta hai, text column string — string par for-loop
    chalane se har CHARACTER iterate hota hai (purana blank-tab bug)."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def as_items(value):
    """Column jsonb ho, text ho ya dict — teeno se [{'name','amount'}] banata hai."""
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
            return [{"name": str(k), "amount": safe_num(v), "lat": None,
                     "lon": None}
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
    """Aik row (DB ki ho ya abhi parse hui) se screen par dikhane wali cheezein."""
    return {
        "id": pick(row, ("id", "uuid", "pk")),
        "title": str(pick(row, TITLE_KEYS, "Untitled project")),
        "dept": str(pick(row, DEPT_KEYS, "—")),
        "budget": safe_num(pick(row, BUDGET_KEYS, 0)),
        "status": str(pick(row, STATUS_KEYS, "—")),
        "created": str(pick(row, CREATED_KEYS, "") or "")[:19],
        "components": as_items(pick(row, COMP_KEYS)),
        "districts": as_items(pick(row, DIST_KEYS)),
    }


def project_context(p):
    """AI ko bheja jane wala structured summary (chat + review dono isay use karte)."""
    lines = [f"Title: {p['title']}", f"Department: {p['dept']}",
             f"Verification status: {p['status']}",
             f"Total budget (PKR): {money(p['budget'])}"]
    if p["components"]:
        total = sum(safe_num(c["amount"]) for c in p["components"])
        lines.append("Components (name: amount):")
        lines += [f"- {c['name']}: {money(c['amount'])}" for c in p["components"]]
        lines.append(f"Components add up to: {money(total)} (difference from "
                     f"total budget: {money(total - safe_num(p['budget']))})")
    else:
        lines.append("Components: not present in the record.")
    if p["districts"]:
        total = sum(safe_num(d["amount"]) for d in p["districts"])
        lines.append("District-wise allocation (district: amount):")
        lines += [f"- {d['name']}: {money(d['amount'])}" for d in p["districts"]]
        lines.append(f"Districts add up to: {money(total)}")
    else:
        lines.append("District-wise allocation: not present in the record.")
    return "\n".join(lines)


def all_context(ps):
    lines = ["Summary of every project in the database "
             "(title | department | budget | districts):"]
    for p in ps[:40]:
        names = ", ".join(d["name"] for d in p["districts"][:6]) or "—"
        lines.append(f"- {p['title']} | {p['dept']} | {money(p['budget'])} | {names}")
    return "\n".join(lines)


# ------------------------------------------------- token budget (Groq free tier)
class TpmError(RuntimeError):
    """413 / 429 — request 60-second token window se bari thi."""

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
    return int(len(str(text)) / CHARS_PER_TOKEN) + 1


def input_char_budget(output_tokens=CHAT_OUTPUT_TOKENS, reserve=0):
    """Aik request mein kitne characters bheje ja sakte hain."""
    room = st.session_state.tpm_limit - output_tokens - reserve - SAFETY_TOKENS
    return max(1_200, int(room * CHARS_PER_TOKEN))


def parse_char_budget():
    """Parse ke liye budget — review call ka hissa jaan-boojh kar chhora jata hai
    warna parse poora minute kha leta hai aur review 60s ruk jata hai."""
    return input_char_budget(PARSE_OUTPUT_TOKENS, REVIEW_RESERVE)


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


def extract_json(text):
    """JSON mode ke bawajood model kabhi markdown lapet deta hai, ya max_tokens
    khatam hone par JSON adhoora reh jata hai."""
    if not text or not str(text).strip():
        raise ValueError("Model ne khaali jawab bheja (max_tokens ya filter issue).")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", str(text), re.DOTALL)
        if not match:
            raise ValueError(f"Output JSON nahi tha. Pehle 300 chars: {text[:300]}")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError(f"JSON object expected tha, mila {type(data).__name__}.")
    return data


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
             "SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY (aur secure RPC ke liye "
             "DB_SECRET_KEY) daalein.")
    st.stop()
except Exception as exc:
    st.error("⚠️ Client banate waqt error — asal wajah neeche hai:")
    st.exception(exc)
    st.stop()

DB_SECRET = st.secrets.get("DB_SECRET_KEY", "")


def _looks_dead(exc):
    low = str(exc).lower()
    return any(hint in low for hint in DEAD_MODEL_HINTS)


def call_groq(messages, json_mode=False, max_tokens=CHAT_OUTPUT_TOKENS,
              temperature=0.3):
    """Pehla zinda model use karta hai; model band ho to khud agla try karta hai.
    413/429 ko TpmError banata hai taake caller text chhota kar ke retry kar sake."""
    planned = sum(estimate_tokens(m["content"]) for m in messages) + max_tokens
    tpm_wait_if_needed(planned)

    order = list(MODEL_CANDIDATES)
    working = st.session_state.working_model
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
                if tpm.limit:                  # asli limit yaad rakh lein
                    st.session_state.tpm_limit = tpm.limit
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


# --------------------------------------------- PDF → text → ahem hisse
def pdf_to_text(file_obj):
    reader = PdfReader(file_obj)
    pages = [(page.extract_text() or "") for page in reader.pages]
    return "\n".join(p for p in pages if p.strip()), len(reader.pages)


SCORE_WORDS = ("budget", "cost", "district", "allocation", "component",
               "total", "rs.", "rs ", "million", "pkr", "estimate", "phase",
               "financial", "capital", "revenue", "scheme", "project")


def score_block(block):
    low = block.lower()
    score = sum(low.count(w) * 2 for w in SCORE_WORDS)
    score += sum(4 for d in KPK_COORDS if d in low)
    score += sum(c.isdigit() for c in block) // 10
    return score


def smart_extract(text, budget_chars, block_size=1200):
    """Poora document 8000 TPM mein nahi aata. Is liye sirf wo blocks bhejte hain
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
    """Chat ke liye sawal ke keywords wale hisse chunte hain — chhote token budget
    mein document ke shuru ke hisse se kaafi behtar jawab milta hai."""
    if len(text) <= budget_chars:
        return text
    words = re.findall(r"[a-zA-Z]{4,}", str(question).lower())[:12]
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
    if project_id in (None, SESSION_ID):
        return [], None
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


def _new_id(res):
    """Insert/RPC ke jawab se nayi row ka id nikalna (jo mile to)."""
    data = getattr(res, "data", None)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return pick(data[0], ("id", "uuid", "pk"))
    if isinstance(data, (int, str)) and str(data).strip():
        return data
    if isinstance(data, dict):
        return pick(data, ("id", "uuid", "pk"))
    return None


def save_project(payload, raw_text=""):
    """Teen raste: poora table insert → components ke baghair → purana secure RPC.
    (new_id, kaunsa rasta chala, error) wapas aata hai."""
    lean = {k: v for k, v in payload.items() if k != "components"}
    attempts = [("table insert (components ke saath)", payload),
                ("table insert (components ke baghair)", lean)]
    problems = []
    for label, body in attempts:
        try:
            res = supabase.table(TABLE_PROJECTS).insert(body).execute()
            return _new_id(res), label, None
        except Exception as exc:
            problems.append(f"• {label} → {exc}")
            if "does not exist" not in str(exc).lower():
                break                          # RLS/permission — schema theek hai
    try:                                       # purana secure RPC (encrypted payload)
        res = supabase.rpc(INSERT_RPC, {
            "p_project_title": payload.get("project_title") or "Unknown",
            "p_department": payload.get("department") or "Unknown",
            "p_raw_payload": raw_text or json.dumps(payload, default=str),
            "p_secret_key": DB_SECRET,
            "p_total_budget": safe_num(payload.get("total_budget")),
            "p_district_allocations": payload.get("district_allocations") or [],
            "p_verification_status": payload.get("verification_status") or "VERIFIED",
        }).execute()
        return _new_id(res), f"{INSERT_RPC} RPC", None
    except Exception as exc:
        problems.append(f"• {INSERT_RPC} RPC → {exc}")
    return None, None, RuntimeError("\n".join(problems))


def table_columns(table):
    try:
        res = supabase.table(table).select("*").limit(1).execute()
        rows = res.data or []
        return (sorted(rows[0]) if rows else []), len(rows), None
    except Exception as exc:
        return [], 0, exc


# ----------------------------------------------------------------- AI stages
PARSE_SYSTEM = ("You are a precise financial document parser. "
                "Output strict, compact JSON only.")

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
- Do NOT output latitude/longitude — coordinates app khud lagata hai.
- If a value is absent use 0 or "", never guess.
- Be concise: no explanations outside the JSON."""

REVIEW_SYSTEM = (
    "You are a senior auditor of the Government of Khyber Pakhtunkhwa reviewing a "
    "PC-1 development scheme. Write exactly 4 short audit observations covering: "
    "(1) the size and shape of the total budget, (2) whether the component costs "
    "add up to the total budget, (3) how the district-wise allocation is spread, "
    "(4) what information is missing or unclear in the record. "
    "Quote the actual figures. Max 28 words per observation. No praise. "
    'Return ONLY JSON: {"observations": ["...", "...", "...", "..."]}')


def parse_slice(text_slice):
    raw = call_groq([{"role": "system", "content": PARSE_SYSTEM},
                     {"role": "user",
                      "content": f"{PARSE_PROMPT}\n\nDocument Text:\n{text_slice}"}],
                    json_mode=True, max_tokens=PARSE_OUTPUT_TOKENS,
                    temperature=0.1)
    return extract_json(raw)


def parse_with_shrink(text_slice):
    """413 aane par Groq ke bataye Limit/Requested se ratio nikaal kar text chhota
    karte hain aur dobara bhejte hain — user ko kuch tune nahi karna parta."""
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
            current = current[:max(1_000, int(len(current) * ratio))]
    raise RuntimeError("Shrink ke baad bhi TPM limit paar ho rahi hai.")


def ai_review(p):
    """PC-1 par AI ke audit observations — yahi comments ban kar DB mein jate hain."""
    raw = call_groq([{"role": "system", "content": REVIEW_SYSTEM},
                     {"role": "user", "content": project_context(p)[:3_000]}],
                    json_mode=True, max_tokens=REVIEW_OUTPUT_TOKENS,
                    temperature=0.2)
    data = extract_json(raw)
    items = data.get("observations") or data.get("comments") or data.get("review")
    if isinstance(items, str):
        items = [line.strip(" -•") for line in items.splitlines()]
    out = []
    for item in (items or []):
        if isinstance(item, dict):
            item = pick(item, CTEXT_KEYS, "") or pick(item, ("observation",), "")
        text = str(item).strip()
        if text:
            out.append(text)
    return out[:5]


def row_from_parsed(data):
    """AI ke JSON ko DB row ki shape mein badalna (district coords locally lagti)."""
    dists, missing = [], []
    for x in as_list(data.get("districtWiseAllocation")):
        if not isinstance(x, dict):
            if isinstance(x, str) and x.strip():
                x = {"district": x.strip(), "amount": 0}
            else:
                continue
        name = str(x.get("district") or x.get("name") or "—")
        lat, lon = district_coords(name, x.get("latitude"), x.get("longitude"))
        row = {"district": name, "amount": safe_num(x.get("amount"))}
        if lat and lon:
            row["latitude"], row["longitude"] = lat, lon
        else:
            missing.append(name)
        dists.append(row)
    comps = []
    for c in as_list(data.get("components")):
        if isinstance(c, dict):
            name = str(c.get("name") or c.get("component") or "—")
            comps.append({"name": name,
                          "cost": safe_num(c.get("cost") or c.get("amount"))})
        elif isinstance(c, str) and c.strip():
            comps.append({"name": c.strip(), "cost": 0})
    payload = {
        "project_title": str(data.get("projectTitle") or "").strip()
                         or "Untitled PC-1",
        "department": str(data.get("departmentName") or "").strip() or "—",
        "total_budget": safe_num(data.get("totalBudget")),
        "components": comps,
        "district_allocations": dists,
        "verification_status": "AI-PARSED",
    }
    return payload, missing


# ------------------------------------------------ aik click ka poora pipeline
def run_pc1(uploaded):
    """PDF → AI parse → Supabase save → AI review comments. Log ki lines wapas
    karta hai; nateeja `st.session_state.doc` mein chala jata hai."""
    log = []
    text, pages = pdf_to_text(uploaded)
    if len(text.strip()) < 40:
        raise ValueError(
            "Is PDF se text nahi mila — lagta hai pages scanned images hain. "
            "Aise PC-1 ke liye pehle OCR (searchable PDF) banana paregi.")
    log.append(f"📄 {pages} page, {len(text):,} characters text mila.")

    slice_, trimmed = smart_extract(text, parse_char_budget())
    if trimmed:
        log.append(f"✂️ Document bara tha — sirf ahem hisse ({len(slice_):,} chars) "
                   f"AI ko bheje gaye (limit {st.session_state.tpm_limit:,} TPM).")
    payload, missing = row_from_parsed(parse_with_shrink(slice_))
    log.append(f"🤖 AI ne parse kiya: **{payload['project_title']}** — "
               f"{len(payload['components'])} components, "
               f"{len(payload['district_allocations'])} districts, total budget "
               f"PKR {money(payload['total_budget'])}.")
    if missing:
        log.append("📍 In naamon ke coordinates KPK list mein nahi mile: "
                   + ", ".join(missing[:8]))

    new_id, path, db_err = save_project(payload, raw_text=text[:20_000])
    if db_err is None:
        log.append(f"💾 Supabase mein save ho gaya ({path})"
                   + (f" — id {new_id}." if new_id else "."))
    else:
        log.append("⚠️ DB save nahi hua — ye PC-1 sirf is session mein rahega "
                   "(chaaron tabs phir bhi chalenge).")

    row = dict(payload)
    row["id"] = new_id or SESSION_ID
    row["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    parsed = fields(row)

    review, review_err = [], None
    try:
        review = ai_review(parsed)
        log.append(f"🧾 AI ne {len(review)} audit observations likhe.")
    except Exception as exc:
        review_err = exc
        log.append("⚠️ AI review nahi ban saka — baqi sab kaam ho gaya.")

    saved, cmt_err = 0, None
    if review and new_id:
        for obs in review:
            err = add_comment(new_id, AI_NAME, obs)
            if err is not None:
                cmt_err = err
                break
            saved += 1
        log.append(f"📝 {saved}/{len(review)} AI comments `{TABLE_COMMENTS}` "
                   "mein save huye.")
    elif review:
        log.append("📝 AI review DB mein nahi jaa saka (nayi row ka id wapas nahi "
                   "mila) — 'DB & Comments' tab mein wo yahin dikh raha hai.")

    st.session_state.doc = {
        "row": row, "text": text, "pages": pages, "review": review,
        "saved_id": new_id, "name": getattr(uploaded, "name", "PC-1.pdf"),
        "db_err": db_err, "review_err": review_err, "cmt_err": cmt_err,
        "ai_comments": saved, "trimmed": trimmed,
    }
    st.session_state.sel_id = row["id"]
    st.session_state.chat_history = []
    st.session_state.chat_for = row["id"]
    return log


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
    .cmt-ai { border-left-color:#2563eb; background:#f8fbff; }
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


# --------------------------------------------- upload (poora kaam yahan se)
_doc = st.session_state.doc
with st.expander(f"📤 PC-1 badalna hai? (abhi: {_doc['name']})" if _doc
                 else "📤 PC-1 ka PDF upload karein — baqi sab khud ho jayega",
                 expanded=_doc is None):
    uploaded = st.file_uploader("PC-1 / PC-2 PDF", type=["pdf"],
                                label_visibility="collapsed")
    st.caption("Upload hote hi ye sab khud chalta hai: text nikalna → AI parse → "
               f"`{TABLE_PROJECTS}` mein save → AI audit review `{TABLE_COMMENTS}` "
               "mein → chaaron tabs active. Aur koi button dabana nahi parta.")
    if _doc is not None and st.button("🔁 Isi file ko dobara process karein"):
        st.session_state.doc_sig = None

proc_log, proc_err = None, None
if uploaded is not None:
    _sig = f"{getattr(uploaded, 'name', '')}:{getattr(uploaded, 'size', 0)}"
    if _sig != st.session_state.doc_sig:
        st.session_state.doc_sig = _sig            # dobara khud se na chale
        with st.spinner("PC-1 par kaam ho raha hai — parse → DB save → AI review…"):
            try:
                proc_log = run_pc1(uploaded)
            except TpmError as exc:
                proc_err = exc
            except Exception as exc:
                proc_err = exc

doc = st.session_state.doc
if proc_log:
    st.success("✅ Ho gaya — neeche chaaron options is PC-1 par chal rahe hain.")
    for line in proc_log:
        st.markdown(f"- {line}")
    if doc and doc.get("db_err") is not None:
        with st.expander("⚠️ Database save kyun fail hua? (details)"):
            st.exception(doc["db_err"])
            st.info("Aam wajah: Row Level Security on hai magar anon key ke liye "
                    "INSERT policy nahi hai. Sidebar → Diagnostics → 'Database "
                    "test' se confirm karein.")
    if doc and doc.get("review_err") is not None:
        with st.expander("⚠️ AI review ka error (details)"):
            st.exception(doc["review_err"])
    if doc and doc.get("cmt_err") is not None:
        with st.expander("⚠️ AI comments save karte waqt error (details)"):
            st.exception(doc["cmt_err"])
if proc_err is not None:
    if isinstance(proc_err, TpmError):
        st.error("⏳ Groq ki per-minute token limit aa gayi. Aik minute baad "
                 "'🔁 Isi file ko dobara process karein' dabayein.")
    else:
        st.error("❌ PC-1 process nahi hua — asal error:")
    st.exception(proc_err)


def reconcile_doc(doc, rows):
    """Do soorat-e-haal theek karta hai: (1) insert chal gaya magar RLS ne id wapas
    nahi di — DB list mein wohi row dhoond kar us ka id apna lete hain; (2) AI review
    jo DB mein nahi ja saka tha, ab id milne par bhej dete hain."""
    if doc.get("saved_id") is None and doc.get("db_err") is None:
        want_title = str(doc["row"].get("project_title") or "")
        want_budget = safe_num(doc["row"].get("total_budget"))
        for r in rows:
            if (str(pick(r, TITLE_KEYS, "")) == want_title
                    and safe_num(pick(r, BUDGET_KEYS)) == want_budget):
                found = pick(r, ("id", "uuid", "pk"))
                if found is not None:
                    if st.session_state.sel_id in (SESSION_ID, doc["row"]["id"]):
                        st.session_state.sel_id = found
                        st.session_state.chat_for = found
                    doc["saved_id"] = found
                    doc["row"]["id"] = found
                break
    if (doc["review"] and doc.get("saved_id") and not doc.get("ai_comments")
            and not doc.get("review_pushed")):
        doc["review_pushed"] = True
        saved = 0
        for obs in doc["review"]:
            err = add_comment(doc["saved_id"], AI_NAME, obs)
            if err is not None:
                doc["cmt_err"] = err
                break
            saved += 1
        doc["ai_comments"] = saved


# ----------------------------------------------------------------- data + sidebar
rows, load_err = load_projects()
if doc is not None:
    reconcile_doc(doc, rows)
    _db_ids = [str(pick(r, ("id", "uuid", "pk"))) for r in rows]
    if str(doc["row"]["id"]) not in _db_ids:
        rows = [doc["row"]] + rows          # DB se wapas nahi aaya → session se dikhayein
projects = [fields(r) for r in rows]

with st.sidebar:
    st.markdown("### 📁 Project")
    if st.button("🔄 Refresh"):
        st.session_state.link_col = None
        st.rerun()

    sel = sel_row = None
    if projects:
        ids = [p["id"] for p in projects]
        labels = []
        for p in projects:
            tag = "🆕 " if (doc is not None
                           and str(p["id"]) == str(doc["row"]["id"])) else ""
            labels.append(f"{tag}{p['title'][:40]}  ·  #{p['id']}")
        start = ids.index(st.session_state.sel_id) \
            if st.session_state.sel_id in ids else 0
        idx = st.selectbox("Kis PC-1 par kaam karna hai?",
                           options=range(len(labels)), index=start,
                           format_func=lambda i: labels[i])
        st.session_state.sel_id = ids[idx]
        if st.session_state.chat_for != ids[idx]:
            st.session_state.chat_history = []   # doosre project ki baat na chale
            st.session_state.chat_for = ids[idx]
        sel, sel_row = projects[idx], rows[idx]
        st.caption(f"Kul {len(projects)} projects"
                   + (" (🆕 = abhi upload hua)" if doc is not None else ""))
    elif load_err is None:
        st.info("Abhi kuch nahi hai — upar se PC-1 ka PDF upload karein.")

    if doc is not None:
        st.markdown("### 📄 Uploaded file")
        st.caption(f"{doc['name']} · {doc['pages']} pages · "
                   f"{len(doc['text']):,} characters"
                   + (" · bara document tha, ahem hisse bheje gaye"
                      if doc.get("trimmed") else ""))
        if doc.get("saved_id"):
            st.caption(f"DB row id: {doc['saved_id']} · AI comments: "
                       f"{doc.get('ai_comments', 0)}")
        elif doc.get("db_err") is None:
            st.caption("⚠️ Insert chala gaya magar row wapas nahi aa rahi — "
                       "RLS SELECT policy dekhein.")
        else:
            st.caption("⚠️ DB mein save nahi — sirf is session tak.")


with st.sidebar:
    with st.expander("🔧 Diagnostics"):
        st.write({k: ("set ✅" if k in st.secrets else "MISSING ❌")
                  for k in ("GROQ_API_KEY", "SUPABASE_URL", "SUPABASE_KEY",
                            "DB_SECRET_KEY")})
        vers = {}
        for pkg in ("streamlit", "groq", "supabase", "pypdf", "folium",
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
                   f"{st.session_state.tpm_limit:,} tokens · parse ke liye "
                   f"{parse_char_budget():,} characters ka budget")
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


# ----------------------------------------------------------------- top level
_flash = st.session_state.pop("flash", None)
if _flash:
    st.success(_flash)

if load_err is not None:
    st.error(f"❌ `{TABLE_PROJECTS}` table read nahi ho saka — asal error:")
    st.exception(load_err)
    st.info("Aam wajuhat: table ka naam mukhtalif hai, ya Row Level Security on "
            "hai magar anon key ke liye SELECT policy nahi hai. Sidebar → "
            "Diagnostics → 'Database test' se confirm karein.")

tab_break, tab_chat, tab_cmt, tab_map = st.tabs(
    ["📊 PC-1 Breakdown", "💬 AI Chat", "📝 DB & Comments", "🗺️ Map"])

NEED_PROJECT = ("Upar 📤 se PC-1 ka PDF upload karein — upload hote hi ye option "
                "khud bhar jayega (ya sidebar se koi purana project chunein).")


def is_doc(p):
    """Ye wohi PC-1 hai jo abhi upload hua? (tab raw text bhi mojood hota hai)"""
    return (doc is not None and p is not None
            and str(p["id"]) == str(doc["row"]["id"]))


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
                   + (f"  ·  Added: {sel['created']}" if sel["created"] else "")
                   + f"  ·  ID: {sel['id']}"
                   + ("  ·  🆕 abhi upload hua" if is_doc(sel) else ""))

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
            st.caption("Is PC-1 mein components ka data nahi mila.")

        st.markdown("##### 📍 District-wise allocation")
        if dist_rows:
            st.dataframe(dist_rows, use_container_width=True, hide_index=True)
            st.bar_chart({"District": [d["name"] for d in sel["districts"]],
                          "Amount": [safe_num(d["amount"])
                                     for d in sel["districts"]]},
                         x="District", y="Amount", height=280)
        else:
            st.caption("Is PC-1 mein district allocation ka data nahi mila.")

        with st.expander("🧾 Data jaisa hai waisa (row)"):
            st.json(sel_row)
        if is_doc(sel):
            with st.expander("📄 PDF se nikla hua text (pehle 4000 characters)"):
                st.text(doc["text"][:4_000])


CHAT_SYSTEM = (
    "You are a professional financial auditor for the Government of Khyber "
    "Pakhtunkhwa reviewing PC-1/PC-2 development schemes. Answer ONLY from the "
    "PC-1 data (and document extract, if given) below. If a detail is not there, "
    "say plainly that the record does not contain it — never invent numbers. All "
    "amounts are in PKR. Keep answers short, specific and auditor-like. Reply in "
    "the same language as the question (Roman Urdu sawal → Roman Urdu jawab)."
    "\n\nPC-1 DATA:\n")


def chat_context(p, question, wide):
    """Token budget ke andar sab se kaam ka context — structured data pehle,
    phir (upload wali file ho to) sawal se related raw text."""
    room = max(800, input_char_budget(CHAT_OUTPUT_TOKENS) - len(CHAT_SYSTEM) - 800)
    if wide:
        return all_context(projects)[:room]
    ctx = project_context(p)[:room]
    if is_doc(p):
        left = min(CHAT_DOC_CHARS, room - len(ctx) - 80)
        if left > 400:
            ctx += ("\n\nDOCUMENT EXTRACT (PDF se, sawal se related hissa):\n"
                    + context_for_question(doc["text"], question, left))
    return ctx


with tab_chat:
    if sel is None:
        st.info(NEED_PROJECT)
    else:
        st.markdown(f"<div class='engine-badge'>Engine: "
                    f"{st.session_state.active_engine}</div>",
                    unsafe_allow_html=True)
        wide = st.checkbox("Saare projects ka data bhi bhejein (comparison ke liye)")
        st.caption(("Context: sab projects" if wide else f"Context: {sel['title']}")
                   + (" + PDF ka raw text" if is_doc(sel) and not wide else ""))

        for msg in st.session_state.chat_history:
            bubble(msg["role"], msg["content"])

        question = None
        c1, c2, c3 = st.columns(3)
        if c1.button("Budget summary"):
            question = ("Is PC-1 ka budget summary aur components ka breakdown "
                        "batayein.")
        if c2.button("Total match karta hai?"):
            question = ("Kya components aur district allocations ka jor total budget "
                        "se match karta hai? Farq ho to number ke saath batayein.")
        if c3.button("Sab se bara district"):
            question = ("Kis district ko sab se ziyada allocation mila aur kitna "
                        "percentage? Top 3 batayein.")

        with st.form("chat_form", clear_on_submit=True):
            typed = st.text_input("Is PC-1 se koi bhi sawal poochein:",
                                  placeholder="e.g. civil works ka hissa kitna hai?")
            if st.form_submit_button("Send") and typed.strip():
                question = typed.strip()

        if question:
            st.session_state.chat_history.append({"role": "user",
                                                  "content": question})
            messages = [{"role": "system",
                         "content": CHAT_SYSTEM + chat_context(sel, question, wide)}]
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
                    st.error("⏳ Groq ki per-minute token limit — thori der baad "
                             "dobara poochein (upload ke foran baad aisa hota hai).")
                    st.caption(str(exc)[:300])
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
        session_only = str(sel["id"]) == SESSION_ID
        comments, cmt_err = load_comments(sel["id"])
        if cmt_err is not None:
            st.error(f"❌ `{TABLE_COMMENTS}` se comments nahi mile — asal error:")
            st.exception(cmt_err)
            st.info("Teen cheezein check karein: table ka naam, link column "
                    f"({' / '.join(LINK_CANDIDATES)}), aur RLS SELECT policy.")

        if is_doc(sel) and doc["review"] and not doc.get("ai_comments"):
            st.caption("🤖 AI ka audit review (DB mein save nahi ho saka, "
                       "is liye yahan dikha raha hun):")
            for obs in doc["review"]:
                st.markdown(f"<div class='cmt cmt-ai'><b>{AI_NAME}</b><br>"
                            f"{html.escape(str(obs))}</div>",
                            unsafe_allow_html=True)

        if not session_only:
            st.caption(f"{len(comments)} comments  ·  link column: "
                       f"{st.session_state.link_col or 'detect nahi hua'}")
        for c in comments:
            who = html.escape(str(pick(c, CNAME_KEYS, "—")))
            what = html.escape(str(pick(c, CTEXT_KEYS, ""))).replace("\n", "<br>")
            when = str(pick(c, CREATED_KEYS, "") or "")[:19]
            css = "cmt cmt-ai" if AI_NAME.lower() in who.lower() else "cmt"
            st.markdown(f"<div class='{css}'><b>{who}</b> "
                        f"<span style='color:#94a3b8;font-size:12px;'>{when}</span>"
                        f"<br>{what}</div>", unsafe_allow_html=True)
        if not comments and cmt_err is None and not session_only:
            st.caption("Abhi koi comment nahi — pehla comment aap likhein.")

        if session_only:
            st.info("Ye PC-1 database mein save nahi hua, is liye comment save "
                    "nahi ho sakta. Upar wale error se RLS/INSERT policy theek "
                    "karein, phir '🔁 Isi file ko dobara process karein' dabayein.")
        else:
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
    only_sel = st.checkbox("Sirf selected PC-1 ke districts dikhayein",
                           value=doc is not None, disabled=sel is None)
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
        st.caption(f"{shown} markers (green = selected PC-1). Coordinates locally "
                   "rakhe gaye hain — AI se nahi maange jate.")
    elif sel is None:
        st.info(NEED_PROJECT)
    else:
        st.info("Koi marker nahi bana — is PC-1 mein district allocation ka data "
                "nahi mila.")
    if missing:
        st.caption("Ye naam KPK district list mein nahi mile: "
                   + ", ".join(sorted(set(missing))[:12]))

