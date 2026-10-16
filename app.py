# Kirana Khata 📒 — voice udhaar ledger (single-file Streamlit app)
# Run: pip install streamlit requests && streamlit run app.py
import base64
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta

import requests
import streamlit as st

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")
BASE = "https://api.sarvam.ai"
TIMEOUT = 30
STT_MODEL, LLM_MODEL, TTS_MODEL = "saaras:v3", "sarvam-105b", "bulbul:v3"
SPEAKERS = {"hi-IN": "ritu", "te-IN": "kavitha"}
LANG_NAMES = {"hi-IN": "Hindi", "te-IN": "Telugu"}
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "khata.db")

# ---------------- prompts ----------------
EXTRACT_SYSTEM = """You extract udhaar (credit) ledger entries for an Indian kirana shop.
The shopkeeper speaks Hindi, Telugu, English, or a code-mix (Hinglish / Tenglish), possibly
in Devanagari, Telugu or Roman script. Respond with ONLY JSON, no prose, no code fences:

{"customer": "<name>", "items": [{"name": "<item>", "qty": "<qty>"}], "amount": <integer rupees>,
 "type": "credit" | "payment", "due": "<when they will pay, in short English>" | null}

Rules:
- customer: the person's first name in Roman script, Title Case. Drop honorifics
  (ji, bhaiya, bhai, garu, anna, madam, sir, didi, akka, aunty). "सुरेश" -> "Suresh".
- type "credit": customer took goods / money on udhaar ("liya", "likh do", "teesukunnaru", "khata mein").
  type "payment": customer paid money back ("wapas diye", "de diye", "paid", "jama", "ichcharu").
- amount: the rupee amount of THIS entry. Convert number words (do sau -> 200, vanda -> 100).
  If they paid part of an old balance ("500 mein se 300 wapas diye"), amount is what was paid (300).
- items: item names in simple English or the common Indian brand name; qty like "2 kg", "1 litre", "1".
  Use [] for payments or if no items are mentioned.
- due: e.g. "tomorrow", "Sunday", "next week"; null if not said.
- If you cannot find a number for amount, use 0."""
EXAMPLE_IN = "Suresh ne do kilo atta aur ek Surf liya, 180 rupaye, kal dega"
EXAMPLE_OUT = ('{"customer": "Suresh", "items": [{"name": "atta", "qty": "2 kg"}, '
               '{"name": "Surf", "qty": "1"}], "amount": 180, "type": "credit", "due": "tomorrow"}')


def reminder_prompt(name, balance, lang_code):
    return (f"Write a short WhatsApp voice-note message (2 sentences, under 40 words) from the owner of "
            f"Sharma Kirana Store to a regular customer named {name}. Gently and respectfully remind them "
            f"that ₹{balance} is pending in the khata, and say they can pay whenever convenient. "
            f"Use respectful address (aap / mee / garu as fits the language). "
            f"Write in {LANG_NAMES[lang_code]}, in native script, with the amount as digits (₹{balance}). "
            f"Output ONLY the message text.")


# ---------------- Sarvam API ----------------
class SarvamError(Exception):
    pass


def _post(path, **kwargs):
    try:
        r = requests.post(f"{BASE}{path}", headers={"api-subscription-key": SARVAM_API_KEY},
                          timeout=TIMEOUT, **kwargs)
    except requests.Timeout:
        raise SarvamError("Sarvam took too long to respond (30s). Please try again.")
    except requests.RequestException:
        raise SarvamError("Could not reach Sarvam. Check the internet connection.")
    if r.status_code != 200:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text[:200]
        raise SarvamError(f"Sarvam {path} failed ({r.status_code}): {msg}")
    return r.json()


def transcribe(wav_bytes):
    data = _post("/speech-to-text", files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                 data={"model": STT_MODEL, "mode": "codemix", "language_code": "unknown"})
    return data.get("transcript", "").strip()


def chat(messages):
    data = _post("/v1/chat/completions",
                 json={"model": LLM_MODEL, "messages": messages, "temperature": 0.2,
                       "reasoning_effort": "low", "max_tokens": 1024})
    text = data["choices"][0]["message"].get("content") or ""
    return text.split("</think>")[-1].strip()


def parse_json(text):
    text = re.sub(r"```(?:json)?", "", text)
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        return None
    try:
        return json.loads(text[s:e + 1])
    except json.JSONDecodeError:
        return None


def clean_entry(entry):
    items = entry.get("items") or []
    if isinstance(items, str):
        items = [{"name": items, "qty": ""}]
    try:
        amount = int(round(float(str(entry.get("amount", 0)).replace(",", "").replace("₹", ""))))
    except ValueError:
        amount = 0
    return {"customer": str(entry.get("customer") or "").strip(),
            "items": [{"name": str(i.get("name", "")), "qty": str(i.get("qty", ""))}
                      for i in items if isinstance(i, dict)],
            "amount": amount,
            "type": "payment" if str(entry.get("type")).lower() == "payment" else "credit",
            "due": entry.get("due") or None}


def extract_entry(transcript):
    raw = chat([{"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": EXAMPLE_IN},
                {"role": "assistant", "content": EXAMPLE_OUT},
                {"role": "user", "content": transcript}])
    entry = parse_json(raw)
    return clean_entry(entry) if entry else None


def write_reminder(name, balance, lang_code):
    return chat([{"role": "user", "content": reminder_prompt(name, balance, lang_code)}])


def speak(text, lang_code):
    data = _post("/text-to-speech", json={"text": text[:2500], "language_code": lang_code,
                                          "model": TTS_MODEL, "speaker": SPEAKERS[lang_code]})
    return base64.b64decode(data["audios"][0])


# ---------------- SQLite ledger ----------------
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def db_init():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, customer TEXT NOT NULL, customer_key TEXT NOT NULL,
            items TEXT NOT NULL, amount INTEGER NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('credit', 'payment')),
            due TEXT, transcript TEXT, created_at TEXT NOT NULL)""")


def name_key(name):
    return "".join(ch for ch in name.lower() if ch.isalnum())


def normalise_name(name):
    """Match a spoken name to an existing customer (case, spacing, small spelling slips)."""
    name = " ".join(name.strip().split()).title()
    key = name_key(name)
    with conn() as c:
        rows = c.execute("SELECT customer, customer_key FROM entries GROUP BY customer_key").fetchall()
    for r in rows:
        if r["customer_key"] == key:
            return r["customer"]
    for r in rows:
        k = r["customer_key"]
        if len(key) >= 4 and (k.startswith(key) or key.startswith(k)) and abs(len(k) - len(key)) <= 2:
            return r["customer"]
        if len(key) >= 5 and len(k) == len(key) and sum(a != b for a, b in zip(k, key)) == 1:
            return r["customer"]
    return name


def add_entry(customer, items, amount, type_, due=None, transcript=None, created_at=None):
    customer = normalise_name(customer)
    with conn() as c:
        c.execute("INSERT INTO entries (customer, customer_key, items, amount, type, due, transcript, created_at)"
                  " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (customer, name_key(customer), json.dumps(items, ensure_ascii=False), int(amount), type_,
                   due, transcript, created_at or datetime.now().isoformat(timespec="seconds")))
    return customer


def get_balances():
    with conn() as c:
        return [dict(r) for r in c.execute("""
            SELECT MIN(customer) AS customer, customer_key,
                   SUM(CASE WHEN type = 'credit' THEN amount ELSE -amount END) AS balance,
                   COUNT(*) AS entries
            FROM entries GROUP BY customer_key ORDER BY balance DESC""")]


def get_history(customer):
    with conn() as c:
        rows = c.execute("SELECT * FROM entries WHERE customer_key = ? ORDER BY created_at DESC, id DESC",
                         (name_key(customer),)).fetchall()
    return [{**dict(r), "items": json.loads(r["items"])} for r in rows]


def seed_demo():
    with conn() as c:
        c.execute("DELETE FROM entries")
    now = datetime.now()
    demo = [
        ("Suresh", [{"name": "atta", "qty": "5 kg"}, {"name": "toor dal", "qty": "1 kg"}], 420, "credit", "Friday", 6),
        ("Suresh", [], 200, "payment", None, 3),
        ("Lakshmi", [{"name": "milk", "qty": "2 litre"}, {"name": "curd", "qty": "1"}], 160, "credit", "Sunday", 4),
        ("Ramesh", [{"name": "rice", "qty": "10 kg"}, {"name": "sunflower oil", "qty": "1 litre"}], 850, "credit", None, 9),
        ("Ramesh", [], 300, "payment", None, 2),
        ("Anil", [{"name": "Parle-G", "qty": "4"}, {"name": "chai patti", "qty": "250 g"}], 95, "credit", "tomorrow", 1),
    ]
    for name, items, amount, t, due, d in demo:
        add_entry(name, items, amount, t, due, "(demo)", (now - timedelta(days=d)).isoformat(timespec="seconds"))


def items_text(items):
    return ", ".join(f"{i['name']} {i['qty']}".strip() for i in items)


# ---------------- UI ----------------
st.set_page_config(page_title="Kirana Khata", page_icon="📒", layout="centered")
st.markdown("<style>.stButton > button, .stDownloadButton > button "
            "{ min-height: 3rem; font-size: 1.05rem; }</style>", unsafe_allow_html=True)
db_init()
ss = st.session_state
ss.setdefault("pending", None)
ss.setdefault("last_audio", None)
ss.setdefault("reminder", None)

st.title("Kirana Khata 📒")
st.caption("Bolo, likh jayega — voice udhaar ledger for kirana shops")

if st.button("📥 Load demo data", width="stretch"):
    seed_demo()
    ss.pending = ss.reminder = None
    st.toast("Demo customers loaded")


def process(transcript):
    if not transcript:
        st.warning("Kuch sunai nahi diya — please try again closer to the mic, or type it.")
        return
    with st.spinner("Samajh rahe hain…"):
        entry = extract_entry(transcript)
    if entry is None or not entry["customer"]:
        st.warning("Could not read this automatically — please fill it in below.")
        entry = {"customer": "", "items": [], "amount": 0, "type": "credit", "due": None}
    entry["transcript"] = transcript
    ss.pending = entry


st.subheader("🎙️ Nayi entry")
audio = st.audio_input("Boliye: “Suresh ne do kilo atta liya, 180 rupaye, kal dega”")
if audio is not None:
    h = hashlib.md5(audio.getvalue()).hexdigest()
    if h != ss.last_audio:
        ss.last_audio = h
        try:
            with st.spinner("Sun rahe hain…"):
                transcript = transcribe(audio.getvalue())
            process(transcript)
        except SarvamError as e:
            st.error(str(e))

with st.expander("⌨️ Type instead"):
    typed = st.text_input("Entry", placeholder="Priya madam paid 250 today", label_visibility="collapsed")
    if st.button("Parse", width="stretch") and typed.strip():
        try:
            process(typed.strip())
        except SarvamError as e:
            st.error(str(e))

if ss.pending:
    p = ss.pending
    with st.container(border=True):
        st.markdown(f"**Suna:** _{p['transcript']}_")
        with st.form("confirm"):
            c1, c2 = st.columns(2)
            customer = c1.text_input("Customer", p["customer"])
            amount = c2.number_input("Amount ₹", min_value=0, step=10, value=int(p["amount"]))
            type_ = st.radio("Type", ["credit", "payment"], horizontal=True,
                             index=0 if p["type"] == "credit" else 1,
                             format_func=lambda t: "🔴 Udhaar (credit)" if t == "credit" else "🟢 Payment received")
            items_in = st.text_input("Items", items_text(p["items"]))
            due = st.text_input("Will pay", p["due"] or "")
            b1, b2 = st.columns(2)
            ok = b1.form_submit_button("✅ Save", type="primary", width="stretch")
            cancel = b2.form_submit_button("✖️ Discard", width="stretch")
        if ok:
            if not customer.strip() or amount <= 0:
                st.error("Customer name and amount are needed.")
            else:
                items = p["items"] if items_in == items_text(p["items"]) else \
                    [{"name": s.strip(), "qty": ""} for s in items_in.split(",") if s.strip()]
                name = add_entry(customer, items, amount, type_, due.strip() or None, p["transcript"])
                ss.pending = None
                st.toast(f"Saved: {name} ₹{amount}")
                st.rerun()
        if cancel:
            ss.pending = None
            st.rerun()

st.subheader("📒 Khata")
balances = get_balances()
if not balances:
    st.info("No entries yet — record one above or tap “Load demo data”.")
else:
    st.metric("Total baaki (outstanding)", f"₹{sum(b['balance'] for b in balances):,}")
    st.dataframe([{"Customer": b["customer"], "Baaki ₹": b["balance"], "Entries": b["entries"]}
                  for b in balances], hide_index=True, width="stretch")
    lang = st.radio("Reminder language", ["hi-IN", "te-IN"], horizontal=True,
                    format_func={"hi-IN": "हिन्दी", "te-IN": "తెలుగు"}.get)
    for b in balances:
        name, bal = b["customer"], b["balance"]
        with st.expander(f"{'🔴' if bal > 0 else '🟢'} **{name}** — ₹{bal:,}"):
            for e in get_history(name):
                sign = "+" if e["type"] == "credit" else "−"
                its = items_text(e["items"]) or ("payment" if e["type"] == "payment" else "")
                due_s = f" · pay by {e['due']}" if e["due"] else ""
                st.markdown(f"`{e['created_at'][:10]}` **{sign}₹{e['amount']}** {its}{due_s}")
            if bal > 0 and st.button(f"🔔 Remind {name}", key=f"rem_{b['customer_key']}",
                                     type="primary", width="stretch"):
                try:
                    with st.spinner("Message likh rahe hain…"):
                        text = write_reminder(name, bal, lang)
                    with st.spinner("Awaaz bana rahe hain…"):
                        wav = speak(text, lang)
                    ss.reminder = {"customer": name, "text": text, "audio": wav}
                except SarvamError as e:
                    st.error(str(e))
            r = ss.reminder
            if r and r["customer"] == name:
                st.success(r["text"])
                st.audio(r["audio"], format="audio/wav", autoplay=True)
                st.download_button("⬇️ Download voice note (share on WhatsApp)", r["audio"],
                                   file_name=f"reminder_{name}.wav", mime="audio/wav", width="stretch")
