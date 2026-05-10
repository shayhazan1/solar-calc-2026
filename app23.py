"""
Streamlit Solar Calculator – Membership upgrade

Features added:
- Email/password sign-up & sign-in via Supabase Auth
- Three tiers: free (3 calcs/day + PDF export), pro (unlimited + PDF), premium (unlimited + PDF + custom logo on report)
- Monthly billing placeholders (verified by `profiles.subscription_status`); integrate provider webhooks later
- Admin panel: view all calculations, edit pricing, manage users
- Per-user calculation logging + per-user history (14 days retention for user view)
- Pricing controls editable by admin (stored in `pricing` table)
- Logo upload for `premium` users (stored in Supabase Storage bucket `logos`)

Setup (one-time):
1) pip install streamlit supabase==2.* python-dotenv weasyprint pandas matplotlib
2) Set env vars: SUPABASE_URL, SUPABASE_ANON_KEY (or SERVICE_ROLE_KEY for server-side tasks), BUCKET_LOGOS="logos"
3) Create Supabase tables and storage bucket per SQL at bottom of this file.
4) Run: streamlit run app.py

Note: Core financial logic and PDF rendering are adapted from your current app.
"""

import os
import io
import base64
from datetime import datetime, timedelta, timezone
from typing import Optional

import streamlit as st
from zoneinfo import ZoneInfo
import pandas as pd
# פתרון חסימת iframe ברמת הקוד
st.markdown(
    '<meta http-equiv="Content-Security-Policy" content="frame-ancestors *;">',
    unsafe_allow_html=True
)
import matplotlib.pyplot as plt
from weasyprint import HTML

from supabase import create_client, Client
from terms_text import TERMS_AND_CONDITIONS

# ============ CONFIG ============

st.set_page_config(
    page_title="מחשבון סולארי",
    page_icon="☀️",
    layout="wide",
)

# RTL styles ל-Streamlit (לא ל-PDF)
st.markdown(
    """
    <style>
      .stApp { direction: rtl; text-align: right; }
      h1, h2, h3, h4, h5, h6, p, label { text-align: right !important; }
      .stTextInput label, .stNumberInput label, .stSelectbox label, .stFileUploader label {
        text-align: right !important; display: block;
      }
      table { direction: rtl; text-align: right; }
    </style>
    """,
    unsafe_allow_html=True,
)

# --- Load .env (next to this file) ---
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).with_name(".env")
load_dotenv(dotenv_path=env_path, override=False)

# --- Load Supabase config from env / Streamlit secrets (robust) ---

def _secret_get(k, default=None):
    """Safe access to Streamlit secrets (לא קורס אם אין secrets.toml)."""
    try:
        return st.secrets.get(k, default)  # type: ignore[attr-defined]
    except Exception:
        return default


SUPABASE_URL = (
    os.getenv("SUPABASE_URL")
    or _secret_get("SUPABASE_URL")
    or os.getenv("SUPABASE_URI")  # fallback לשם שגוי נפוץ
)
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY") or _secret_get("SUPABASE_ANON_KEY")
BUCKET_LOGOS = os.getenv("BUCKET_LOGOS") or _secret_get("BUCKET_LOGOS") or "logos"

if not SUPABASE_URL or not SUPABASE_KEY:
    has_url_env = bool(os.getenv("SUPABASE_URL") or os.getenv("SUPABASE_URI"))
    has_key_env = bool(os.getenv("SUPABASE_ANON_KEY"))
    st.warning(
        "⚠️ לא הוגדרו מפתחות Supabase (SUPABASE_URL, SUPABASE_ANON_KEY). "
        'ודא/י שקובץ ".env" ליד הקובץ או בקובץ ".streamlit/secrets.toml".'
    )
    st.caption(f"URL env present: {has_url_env} | KEY env present: {has_key_env}")


# Keep a single client and attach the signed-in session token for RLS
_SB: Optional[Client] = None


def _attach_session_tokens(c: Client):
    """מצמיד token של המשתמש הנוכחי ל-postgrest/storage/auth לצורך RLS."""
    sess = st.session_state.get("session") if hasattr(st, "session_state") else None
    if not sess:
        return
    access = getattr(sess, "access_token", None) or (
        sess.get("access_token") if isinstance(sess, dict) else None
    )
    refresh = getattr(sess, "refresh_token", None) or (
        sess.get("refresh_token") if isinstance(sess, dict) else None
    )
    if not access:
        return
    try:
        c.postgrest.auth(access)
    except Exception:
        pass
    try:
        c.storage.auth(access)
    except Exception:
        pass
    try:
        c.auth.set_session(access, refresh)  # type: ignore
    except Exception:
        pass


def sb_client() -> Optional[Client]:
    """מחזיר Supabase client יחיד לכל האפליקציה."""
    global _SB
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    if _SB is None:
        _SB = create_client(SUPABASE_URL, SUPABASE_KEY)
    try:
        _attach_session_tokens(_SB)
    except Exception:
        pass
    return _SB


# ============ AUTH & PROFILES ============

def sign_up(email: str, password: str):
    sb = sb_client()
    if not sb:
        return None, "אין חיבור ל-Supabase"
    try:
        res = sb.auth.sign_up({"email": email, "password": password})
        return res.user, None
    except Exception as e:
        return None, str(e)


def sign_in(email: str, password: str):
    sb = sb_client()
    if not sb:
        return None, "אין חיבור ל-Supabase"
    try:
        res = sb.auth.sign_in_with_password({"email": email, "password": password})
        return res.session, None
    except Exception as e:
        return None, str(e)


def sign_out():
    sb = sb_client()
    if not sb:
        return
    try:
        sb.auth.sign_out()
    except Exception:
        pass


def get_profile(user_id: str) -> dict:
    sb = sb_client()
    if not sb:
        return {}
    try:
        res = (
            sb.table("profiles")
            .select(
                "id,email,role,subscription_status,is_admin,"
                "logo_url,company_name,contact_phone,contact_email,created_at"
            )
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )
        return res.data or {}
    except Exception:
        return {}


def upsert_profile(uid: str, email: str) -> None:
    try:
        sb = sb_client()
        if not sb:
            return
        sb.table("profiles").upsert({"id": uid, "email": email}).execute()
    except Exception:
        pass


def ensure_profile(session) -> dict:
    """Return profile for the session user; if missing, create and refetch."""
    try:
        if not session or not getattr(session, "user", None):
            return {}
        uid = session.user.id
        email = getattr(session.user, "email", None)
        prof = get_profile(uid)
        if not prof and email:
            upsert_profile(uid, email)
            prof = get_profile(uid)
        return prof or {}
    except Exception:
        return {}


# ============ PRICING ============

def get_pricing():
    sb = sb_client()
    if not sb:
        return {"free": 0, "pro": 150, "premium": 250}
    try:
        rows = sb.table("pricing").select("plan, monthly_ils").execute().data or []
    except Exception:
        rows = []
    out = {r.get("plan"): r.get("monthly_ils") for r in rows if isinstance(r, dict)}
    out.setdefault("free", 0)
    out.setdefault("pro", 150)
    out.setdefault("premium", 250)
    return out


def update_pricing(new_values: dict):
    sb = sb_client()
    if not sb:
        return
    for plan, price in new_values.items():
        try:
            sb.table("pricing").upsert({"plan": plan, "monthly_ils": price}).execute()
        except Exception:
            pass


# ============ CALC LIMITS & LOGGING ============

def today_calc_count(user_id: str) -> int:
    """מספר החישובים מאז חצות (זמן ישראל)."""
    sb = sb_client()
    if not sb or not user_id:
        return 0
    try:
        now_local = datetime.now(ZoneInfo("Asia/Jerusalem"))
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        since_iso = (
            start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except Exception:
        since_iso = (datetime.utcnow() - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    try:
        res = (
            sb.table("calculations")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .gte("created_at", since_iso)
            .execute()
        )
        return int(res.count or 0)
    except Exception:
        return 0


def can_calc(profile: dict) -> tuple[bool, str]:
    """בודק הרשאות לפי סוג מנוי ותוקף תקופת ניסיון."""
    role = profile.get("role", "free") # 'free' מייצג כעת מנוי ניסיון
    status = profile.get("subscription_status", "inactive")
    created_at_str = profile.get("created_at")
    user_id = profile.get("id")

    if not user_id:
        return False, "יש להתחבר כדי לבצע חישוב."

    # בדיקת מנוי ניסיון (Free Trial)
    if role == "free":
        # חישוב תוקף: 7 ימים ממועד ההרשמה
        if created_at_str:
            created_at = pd.to_datetime(created_at_str).replace(tzinfo=None)
            now = datetime.utcnow()
            if now > created_at + timedelta(days=7):
                return False, "מנוי הניסיון הסתיים (7 ימים). אנא שדרג למנוי Pro או Premium."
        
        # הגבלה ל-3 חישובים ביום
        if today_calc_count(user_id) >= 3:
            return False, "הגעת למכסה של 3 חישובים ביום למנוי ניסיון."
        return True, ""

    # בדיקת מנוי בתשלום (Pro / Premium)
    if role in ("pro", "premium"):
        if status != "active":
            return False, "נדרש מנוי פעיל. אנא הסדר תשלום בעמוד הפרופיל."
        return True, ""

    return True, ""

def log_calc(user_id: str, input_payload: dict, outputs: dict, pdf_bytes: bytes | None):
    sb = sb_client()
    if not sb:
        return
    row = {
        "user_id": user_id,
        "inputs": input_payload,
        "outputs": {
            k: (float(v) if isinstance(v, (int, float)) else v)
            for k, v in outputs.items()
        },
    }
    if pdf_bytes:
        filename = f"reports/{user_id}/{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
        try:
            sb.storage.from_("reports").upload(
                filename, pdf_bytes, {"content-type": "application/pdf"}
            )
            url = sb.storage.from_("reports").get_public_url(filename)
            row["report_url"] = url
        except Exception:
            pass
    try:
        sb.table("calculations").insert(row).execute()
    except Exception:
        pass


def user_history(user_id: str):
    sb = sb_client()
    if not sb:
        return []
    cutoff = (datetime.utcnow() - timedelta(days=14)).isoformat()
    try:
        res = (
            sb.table("calculations")
            .select("id, created_at, outputs, report_url")
            .eq("user_id", user_id)
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )
        return res.data or []
    except Exception:
        return []


# ============ LOGO STORAGE (premium) ============

def company_logo_html() -> str:
    """
    מחזיר תג <img> ללוגו ברירת מחדל.
    עדיפות: DEFAULT_LOGO_URL מה-env / secrets, ואז קובץ לוגו מקומי (company_logo.png).
    """
    url = os.getenv("DEFAULT_LOGO_URL") or _secret_get("DEFAULT_LOGO_URL")
    if url:
        return f"<div><img src='{url}' style='width:200px;'></div>"
    try:
        from pathlib import Path as _P

        p = _P(__file__).with_name("company_logo.png")
        if p.exists():
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            return (
                f"<div><img src='data:image/png;base64,{b64}' "
                f"style='width:200px;'></div>"
            )
    except Exception:
        pass
    return ""


def save_logo(user_id: str, file_bytes: bytes, filename: str) -> Optional[str]:
    sb = sb_client()
    if not sb:
        return None
    try:
        key = f"{user_id}/{filename}"
        sb.storage.from_(BUCKET_LOGOS).upload(
            key, file_bytes, {"content-type": "image/png"}
        )
        return sb.storage.from_(BUCKET_LOGOS).get_public_url(key)
    except Exception:
        return None


def get_logo_url(user_id: str) -> Optional[str]:
    sb = sb_client()
    if not sb:
        return None
    try:
        row = (
            sb.table("profiles")
            .select("logo_url")
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )
        return (row.data or {}).get("logo_url") if row and row.data else None
    except Exception:
        return None


# ============ FINANCIAL CORE (adapted) ============

def format_number(x):
    try:
        return f"{int(round(float(x), 0)):,}"
    except Exception:
        return x


def fig_to_base64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode()



# ============ UI SECTIONS ============
def profile_tab_ui():
    if not st.session_state.session:
        st.warning("אנא התחבר כדי לערוך את פרטי החתימה.")
        return

    prof = st.session_state.profile or {}
    uid = st.session_state.user.id

    st.header("👤 פרטי משתמש וחתימה לדו\"ח")
    
    with st.form("profile_form"):
        col1, col2 = st.columns(2)
        with col1:
            full_name = st.text_input("שם מלא", value=prof.get("full_name", ""))
            job_title = st.text_input("תפקיד", value=prof.get("job_title", ""))
            mobile = st.text_input("טלפון נייד", value=prof.get("mobile_phone", ""))
        with col2:
            comp_name = st.text_input("שם החברה", value=prof.get("company_name", ""))
            comp_addr = st.text_input("כתובת החברה", value=prof.get("company_address", ""))

        if st.form_submit_button("💾 שמור נתונים"):
            data = {
                "full_name": full_name,
                "job_title": job_title,
                "mobile_phone": mobile,
                "company_name": comp_name,
                "company_address": comp_addr
            }
            sb_client().table("profiles").update(data).eq("id", uid).execute()
            st.success("הפרטים נשמרו בבסיס הנתונים!")
            # רענון ה-session state המקומי
            st.session_state.profile.update(data)

    # חלק העלאת הלוגו - רק למשתמשי PRO+LOGO
    if prof.get("role") == "premium":
        st.divider()
        st.subheader("🖼️ לוגו החברה")
        
        # הצגת לוגו קיים אם יש
        current_logo = prof.get("logo_url")
        if current_logo:
            st.image(current_logo, width=150, caption="לוגו נוכחי")
        
        uploaded_logo = st.file_uploader("העלה לוגו חדש (PNG/JPG)", type=["png", "jpg", "jpeg"])
        if uploaded_logo:
            # לוגיקת העלאה ל-Storage ושמירת ה-URL בטבלת profiles
            with st.spinner("מעלה לוגו..."):
                file_ext = uploaded_logo.name.split(".")[-1]
                file_path = f"{uid}/logo.{file_ext}"
                
                # העלאה ל-Bucket
                sb_client().storage.from_("logos").upload(file_path, uploaded_logo.read(), {"x-upsert": "true"})
                
                # קבלת ה-URL הציבורי
                logo_url = sb_client().storage.from_("logos").get_public_url(file_path)
                
                # שמירה ב-Profile
                sb_client().table("profiles").update({"logo_url": logo_url}).eq("id", uid).execute()
                st.session_state.profile["logo_url"] = logo_url
                st.success("הלוגו עודכן!")
                st.rerun()


def auth_panel():
    st.sidebar.header("🔑 כניסה/הרשמה")
    if "session" not in st.session_state:
        st.session_state.session = None
        st.session_state.user = None
        st.session_state.profile = None

    tab_login, tab_signup = st.sidebar.tabs(["כניסה", "הרשמה"])

    with tab_login:
        email = st.text_input("אימייל", key="login_email")
        password = st.text_input("סיסמה", type="password", key="login_password")
        if st.button("כניסה"):
            session, err = sign_in(email, password)
            if err:
                st.error(err)
            elif session:
                st.session_state.session = session
                st.session_state.user = session.user
                st.session_state.profile = ensure_profile(session)
                st.success("התחברת בהצלחה")

    with tab_signup:
        email_s = st.text_input("אימייל להרשמה", key="signup_email")
        password_s = st.text_input("בחר/י סיסמה", type="password", key="signup_password")
        
        # --- הוספת התקנון ואישור המשתמש ---
        st.markdown("---")
        with st.expander("📄 קרא את תקנון האתר ותנאי השימוש"):
            st.markdown(TERMS_AND_CONDITIONS)
        
        agree_terms = st.checkbox("אני מאשר/ת שקראתי את התקנון ואני מסכים/ה לתנאי השימוש והגבלת האחריות.")
        # ----------------------------------

        if st.button("הרשמה", key="signup_btn"):
            if not agree_terms:
                st.error("עליך לאשר את התקנון כדי להמשיך ברישום.")
            elif not email_s or not password_s:
                st.warning("נא למלא אימייל וסיסמה.")
            else:
                user, err = sign_up(email_s, password_s)
                if err:
                    st.error(f"שגיאת הרשמה: {err}")
                else:
                    # התחברות אוטומטית לאחר הרשמה
                    session2, _ = sign_in(email_s, password_s)
                    if session2:
                        st.session_state.session = session2
                        st.session_state.user = session2.user
                        st.session_state.profile = ensure_profile(session2)
                        st.success("נרשמת והתחברת בהצלחה ✅")
                        st.rerun()
                    else:
                        st.success("נרשמת בהצלחה ✅. אנא התחבר/י בלשונית הכניסה.")

    if st.session_state.session:
        prof = st.session_state.profile or {}
        st.sidebar.markdown(
            f"""
            **משתמש:** {st.session_state.user.email}<br>
            **תוכנית:** {prof.get('role', 'free')} ({prof.get('subscription_status', 'inactive')})
            """,
            unsafe_allow_html=True,
        )
        if st.sidebar.button("התנתקות"):
            sign_out()
            st.session_state.clear()
            st.rerun()


def pricing_box():
    prices = get_pricing() # מושך מה-SQL שעדכנת
    st.sidebar.header("💳 תוכניות וחבילות")

    msg = (
        f"**מנוי ניסיון:** 0 ₪ (7 ימים, 3 חישובים ביום)\n\n"
        f"**Pro:** {prices.get('pro', 150)} ₪/חודש – ללא הגבלה\n\n"
        f"**Premium:** {prices.get('premium', 300)} ₪/חודש – ללא הגבלה + לוגו בדו\"ח"
    )
    st.sidebar.info(msg)
    st.sidebar.caption("המחירים עשויים להשתנות מעת לעת. עדכון מחירים יימסר טרם ביצוע שינוי לאישור המשתמש.")


def logo_uploader_if_allowed():
    prof = st.session_state.get("profile") or {}

    # רק למנויי Pro+Logo פעילים
    if prof.get("role") != "premium" or prof.get("subscription_status") != "active":
        return

    st.subheader("🏢 הגדרות חברה ולוגו (מנוי Pro+Logo)")

    company_name = st.text_input(
        "שם החברה שיופיע בדו\"ח",
        value=prof.get("company_name", "") or "",
        key="company_name_input",
    )
    contact_phone = st.text_input(
        "טלפון ליצירת קשר שיופיע בדו\"ח",
        value=prof.get("contact_phone", "") or "",
        key="contact_phone_input",
    )
    contact_email = st.text_input(
        "אימייל ליצירת קשר שיופיע בדו\"ח",
        value=prof.get("contact_email", "") or "",
        key="contact_email_input",
    )

    file = st.file_uploader(
        "לוגו החברה (PNG/JPG)", type=["png", "jpg", "jpeg"], key="company_logo_uploader"
    )

    if st.button("💾 שמור הגדרות החברה והלוגו"):
        sb = sb_client()
        if not sb:
            st.error("אין חיבור ל-Supabase")
            return

        updates = {
            "company_name": company_name,
            "contact_phone": contact_phone,
            "contact_email": contact_email,
        }

        if file is not None:
            content = file.read()
            url = save_logo(
                prof["id"], content, f"logo_{int(datetime.utcnow().timestamp())}.png"
            )
            if url:
                updates["logo_url"] = url

        try:
            sb.table("profiles").update(updates).eq("id", prof["id"]).execute()
            st.session_state.profile = get_profile(prof["id"])
            st.success("ההגדרות נשמרו בהצלחה ✅")
            if "logo_url" in updates:
                st.image(updates["logo_url"], width=150)
        except Exception as e:
            st.error(f"שגיאה בשמירת ההגדרות: {e}")


def export_pdf_from_html(html_string: str) -> io.BytesIO:
    """מקבל מחרוזת HTML ומחזיר BytesIO של PDF מוכן להורדה."""
    pdf_bytes = HTML(string=html_string).write_pdf()
    buf = io.BytesIO(pdf_bytes)
    buf.seek(0)
    return buf


def calc_commercial_tariff(system_kw_dc: float) -> float:
    """
    מחשב תעריף משוקלל למערכת מסחרית לפי גודל המערכת ב-KW DC.
    מחלקים את גודל המערכת ב-1.5 לקבלת KW AC, ואז ממוצע משוקלל לפי מדרגות.
    מדרגות (KW AC): 0-15 → 0.48, 15-100 → 0.3731, 100-300 → 0.3437,
                    300-630 → 0.2844, מעל 630 → תעריף תחתית (0.1818)
    """
    kw_ac = system_kw_dc / 1.5
    tiers = [
        (15,   0.4800),
        (100,  0.3731),
        (300,  0.3437),
        (630,  0.2844),
        (float('inf'), 0.1818),
    ]
    total_income = 0.0
    remaining = kw_ac
    prev_limit = 0.0
    for limit, rate in tiers:
        if remaining <= 0:
            break
        tier_size = min(remaining, limit - prev_limit)
        total_income += tier_size * rate
        remaining -= tier_size
        prev_limit = limit
    return total_income / kw_ac if kw_ac > 0 else 0.0


def calc_irr(cashflows: list) -> Optional[float]:
    """מחשב IRR בשיטת ניוטון-רפסון."""
    import numpy as np
    cf = np.array(cashflows, dtype=float)
    # ניסיון ראשוני
    rate = 0.1
    for _ in range(1000):
        pv = sum(cf[t] / (1 + rate) ** t for t in range(len(cf)))
        dpv = sum(-t * cf[t] / (1 + rate) ** (t + 1) for t in range(len(cf)))
        if dpv == 0:
            return None
        rate_new = rate - pv / dpv
        if abs(rate_new - rate) < 1e-8:
            return rate_new
        rate = rate_new
    return rate if -1 < rate < 10 else None


def calculator_ui():
    st.markdown("<h1>☀️ מחשבון פרויקט סולארי</h1>", unsafe_allow_html=True)

    # בחירת סוג מערכת
    system_type = st.radio(
        "🏠 סוג המערכת",
        ["ביתית (עד 22.5 KW)", "מסחרית (מעל 22.5 KW)"],
        horizontal=True,
    )
    is_commercial = system_type == "מסחרית (מעל 22.5 KW)"

    # client details
    st.subheader("📝 פרטי הלקוח")
    client_name = st.text_input("שם הלקוח")
    client_address = st.text_input("כתובת")
    client_phone = st.text_input("טלפון")

    # system
    st.subheader("🔌 נתוני מערכת סולארית")
    if is_commercial:
        system_kw = st.number_input("גודל המערכת (KW DC)", min_value=22.5, step=1.0)
        kw_ac = system_kw / 1.5
        commercial_tariff = calc_commercial_tariff(system_kw)
        st.info(
            f"⚡ גודל המערכת ב-KW AC: **{kw_ac:.1f}** | "
            f"תעריף משוקלל מחושב: **{commercial_tariff:.4f} ₪/קוט\"ש**"
        )
    else:
        system_kw = st.number_input("גודל המערכת (KW DC)", min_value=0.0, max_value=22.5, step=0.1)
        if system_kw > 22.5:
            st.warning("⚠️ גודל מערכת מקסימלי הוא 22.5 KW. לא ניתן להמשיך עם ערך זה.")
    cost_per_kw = st.number_input("עלות הקמה לKW (₪)", value=0)
    annual_hours = st.number_input("שעות שמש", value=0)
    yield_drop_percent = st.number_input(
        "פחת תפוקה שנתי (%)", value=0.0, format="%.2f"
    )

    # costs
    st.subheader("💸 השקעות / עלויות נוספות")
    fee_pv = st.number_input("אגרת PV", value=0)
    fee_connection = st.number_input("אגרת הגדלת חיבור", value=0)
    fee_infra = st.number_input("הגדלת חיבור תשתיות", value=0)
    fee_misc = st.number_input("בלתי צפוי/שונות", value=0)

    total_system_cost = system_kw * cost_per_kw
    extra_costs = fee_pv + fee_connection + fee_infra + fee_misc
    full_cost = total_system_cost + extra_costs
    st.markdown(
        f"<h4 style='color:#2E86AB;'>💰 סך עלות פרויקט: {format_number(full_cost)} ₪</h4>",
        unsafe_allow_html=True,
    )

    equity = st.number_input("הון עצמי", value=0)
    loan_amount = full_cost - equity
    st.markdown(
        f"<h5 style='color:#C0392B;'>💵 הלוואה דרושה: {format_number(loan_amount)} ₪</h5>",
        unsafe_allow_html=True,
    )

    loan_years = st.number_input("תקופת הלוואה (שנים)", value=0)
    loan_rate_percent = st.number_input(
        "ריבית הלוואה (% לשנה)", value=0.0, format="%.2f"
    )
    if not is_commercial:
        inflation_percent = st.number_input(
            "אינפלציה (% לשנה)", value=0.0, format="%.2f"
        )
    else:
        inflation_percent = 0.0

    st.subheader("🛠️ הוצאות שנתיות")
    insurance_percent = st.number_input(
        "ביטוח שנתי (% מעלות הקמה)", value=0.0, format="%.2f"
    )
    maintenance = st.number_input("תחזוקה לקילו וואט (₪)", value=0)

    # בחירת רמת פירוט (לפני החישוב)
    st.subheader("📆 רמת פירוט הדו\"ח")
    view_mode = st.radio(
        "בחר רמת פירוט להצגת הטבלה:",
        ["שנים נבחרות", "פריסה מלאה"],
        help=(
            "בשנים נבחרות יוצגו כל שנות ההחזר על ההלוואה "
            "ולאחר מכן שנים בקפיצות של 5 (10, 15, 20, 25).\n"
            "בפריסה מלאה יוצגו כל 25 השנים."
        ),
    )

    st.subheader("📋 הערות ודגשים לעסקה")
    deal_notes = st.text_area(
        "הערות חופשיות (יופיעו בדו\"ח לפני ההסתייגויות)",
        placeholder="לדוגמה: תחילת עבודה צפויה במועד... / הצעה בתוקף עד... / הערות מיוחדות...",
        height=150,
    )

    if st.button("💡 חשב / הפק דו\"ח"):
        # בדיקת גודל מערכת (רק לביתי)
        if not is_commercial and system_kw > 22.5:
            st.error("⚠️ גודל מערכת מקסימלי הוא 22.5 KW. יש להקטין את הערך לפני החישוב.")
            st.stop()

        # בדיקת הרשאה לפי תוכנית מנוי
        profile = st.session_state.get("profile") or {}
        allowed, reason = can_calc(profile)
        if not allowed:
            st.error(f"🚫 {reason}")
            st.stop()

        # משיכת נתוני הפרופיל
        prof = st.session_state.get("profile", {})
        is_premium = prof.get("role") == "premium"
        has_details = True if prof.get("full_name") else False

        # קביעת הלוגו
        logo_url = prof.get("logo_url") if (is_premium and prof.get("logo_url")) else "https://lyseguxiqzzjyipmglvk.supabase.co/storage/v1/object/public/logos/default_logo.png"

        # בניית החתימה לפי סוג המשתמש
        if is_premium and has_details:
            signature_html = f"""
                <div style="text-align: right;">
                    <img src="{logo_url}" style="max-height: 70px;">
                </div>
            """
        elif is_premium:
            signature_html = f"""
                <div style="text-align: right;">
                    <img src="{logo_url}" style="max-height: 70px;">
                </div>
            """
        else:
            signature_html = f"""
                <div style="text-align: right;">
                    <img src="{logo_url}" style="max-height: 70px;">
                </div>
            """

        # ==================== מסחרי ====================
        if is_commercial:
            commercial_tariff = calc_commercial_tariff(system_kw)
            loan_rate = loan_rate_percent / 100
            yield_drop = yield_drop_percent / 100
            insurance_amount = (insurance_percent / 100) * total_system_cost
            maintenance_amount = maintenance * system_kw

            if loan_amount > 0 and loan_years > 0 and loan_rate > 0:
                monthly_rate = loan_rate / 12
                n_months = int(loan_years) * 12
                monthly_payment = loan_amount * (monthly_rate * (1 + monthly_rate) ** n_months) / ((1 + monthly_rate) ** n_months - 1)
                annual_loan_payment = monthly_payment * 12
            elif loan_amount > 0 and loan_years > 0:
                annual_loan_payment = loan_amount / loan_years
            else:
                annual_loan_payment = 0

            years = list(range(1, 26))
            gross_yields = [
                system_kw * annual_hours * ((1 - yield_drop) ** (year - 1))
                for year in years
            ]
            incomes = [commercial_tariff * gy for gy in gross_yields]
            net_incomes = [inc - insurance_amount - maintenance_amount for inc in incomes]

            moneys = []
            cashflows_free = []
            cum = 0
            for yr in range(25):
                m_payment = annual_loan_payment if yr < loan_years else 0
                moneys.append(m_payment)
                free = net_incomes[yr] - m_payment
                cum += free
                cashflows_free.append(cum)

            # --- IRR פרויקט (ללא מינוף) ---
            project_cf = [-full_cost] + net_incomes
            irr_project = calc_irr(project_cf)

            # --- IRR הון ---
            equity_cf = [-equity] + [net_incomes[yr] - moneys[yr] for yr in range(25)]
            irr_equity = calc_irr(equity_cf)

            # --- Payback Period ---
            payback_year = None
            cum_project = 0
            for yr, ni in enumerate(net_incomes, 1):
                cum_project += ni
                if cum_project >= full_cost:
                    payback_year = yr
                    break

            # --- ממוצעים ---
            total_income = sum(incomes)
            avg_annual_income = total_income / 25
            total_opex = sum([insurance_amount + maintenance_amount] * 25)
            avg_annual_opex = total_opex / 25
            total_financing = sum(moneys)
            total_free = cashflows_free[-1]
            avg_free_annual = total_free / 25
            roe = (total_free / equity * 100) if equity > 0 else 0

            # --- טבלת פירוט ---
            # שורת זמן 0 — השקעה ראשונית
            year0_row = {
                "שנה": 0,
                "תפוקה (KW)": "",
                "שווי תפוקה (₪)": "",
                "תחזוקה + ביטוח": "",
                "החזר הלוואה": "",
                "תזרים פנוי נטו": -equity,
                "תזרים מצטבר": -equity,
            }
            detail_rows = [year0_row]
            cum_cf = -equity
            for yr in range(25):
                prod = gross_yields[yr]
                inc = incomes[yr]
                opex = insurance_amount + maintenance_amount
                fin = moneys[yr]
                free = net_incomes[yr] - fin
                cum_cf += free
                detail_rows.append({
                    "שנה": yr + 1,
                    "תפוקה (KW)": round(prod),
                    "שווי תפוקה (₪)": round(inc),
                    "תחזוקה + ביטוח": round(opex),
                    "החזר הלוואה": round(fin),
                    "תזרים פנוי נטו": round(free),
                    "תזרים מצטבר": round(cum_cf),
                })

            detail_df = pd.DataFrame(detail_rows)

            # סה"כ — רק על שנות 1-25 (ללא שורת זמן 0)
            data_rows = detail_rows[1:]
            totals = {
                "שנה": "סה\"כ",
                "תפוקה (KW)": round(sum(r["תפוקה (KW)"] for r in data_rows)),
                "שווי תפוקה (₪)": round(sum(r["שווי תפוקה (₪)"] for r in data_rows)),
                "תחזוקה + ביטוח": round(sum(r["תחזוקה + ביטוח"] for r in data_rows)),
                "החזר הלוואה": round(sum(r["החזר הלוואה"] for r in data_rows)),
                "תזרים פנוי נטו": round(sum(r["תזרים פנוי נטו"] for r in data_rows)),
                "תזרים מצטבר": "",
            }
            detail_df = pd.concat([detail_df, pd.DataFrame([totals])], ignore_index=True)

            if view_mode == "שנים נבחרות":
                display_years = [0] + list(range(1, int(loan_years) + 1))
                for yr in [10, 15, 20, 25]:
                    if yr > loan_years:
                        display_years.append(yr)
                mask = detail_df["שנה"].isin(display_years) | (detail_df["שנה"] == "סה\"כ")
                detail_df_show = detail_df[mask].copy()
            else:
                detail_df_show = detail_df.copy()

            # פורמט מספרים
            detail_df_display = detail_df_show.copy()
            for col in ["שווי תפוקה (₪)", "תחזוקה + ביטוח", "החזר הלוואה", "תזרים פנוי נטו", "תזרים מצטבר"]:
                detail_df_display[col] = detail_df_display[col].apply(
                    lambda x: format_number(x) if isinstance(x, (int, float)) else x
                )
            detail_html = detail_df_display.to_html(index=False, escape=False)

            # --- פלט מסך ---
            st.subheader("📊 סיכום פרויקט מסחרי")
            payback_str = f"{payback_year} שנים" if payback_year else "מעל 25 שנים"
            irr_proj_str = f"{irr_project*100:.2f}%" if irr_project else "לא ניתן לחישוב"
            irr_eq_str = f"{irr_equity*100:.2f}%" if irr_equity else "לא ניתן לחישוב"

            st.markdown(f"""
| פרמטר | ערך |
|---|---|
| **עלות כוללת של הפרויקט** | {format_number(full_cost)} ₪ |
| **תעריף משוקלל** | {commercial_tariff:.4f} ₪/קוט"ש |
| **הכנסה שנתית ממוצעת** | {format_number(avg_annual_income)} ₪ |
| **הוצאות תפעול שנתיות (ללא מימון)** | {format_number(avg_annual_opex)} ₪ |
| **החזר הלוואה שנתי** | {format_number(annual_loan_payment)} ₪ |
| **שנות החזר השקעה** | {payback_str} |
| **הכנסה שנתית פנויה ממוצעת** | {format_number(avg_free_annual)} ₪ |
| **IRR פרויקט (ללא מינוף)** | {irr_proj_str} |
| **IRR הון עצמי** | {irr_eq_str} |
| **תשואה מצטברת על ההון** | {roe:.1f}% |
""")

            st.subheader("📋 טבלת פירוט שנתית")
            st.dataframe(detail_df_display)

            # --- גרף עוגה — פירוק הכנסה שנתית ממוצעת ---
            avg_financing = annual_loan_payment  # ממוצע שנתי (0 לאחר סיום הלוואה)
            avg_income_net = avg_free_annual

            pie_labels = ["היונפ הסנכה", "לועפת תואצוה", "(יתנש עצוממ) האוולה רזחה"]
            pie_values = [
                max(avg_income_net, 0),
                avg_annual_opex,
                avg_financing * (loan_years / 25),  # ממוצע על כל 25 שנה
            ]
            pie_colors = ["#3BB273", "#F18F01", "#E74C3C"]
            pie_explode = (0.05, 0, 0)

            fig_c, ax_c = plt.subplots(figsize=(5.5, 4))
            wedges, texts, autotexts = ax_c.pie(
                pie_values,
                labels=pie_labels,
                colors=pie_colors,
                explode=pie_explode,
                autopct=lambda p: f"{p:.1f}%\n({format_number(p/100 * avg_annual_income)} )",
                startangle=90,
                textprops={"fontsize": 10},
            )
            for at in autotexts:
                at.set_fontsize(9)
            ax_c.set_title(
                f"תעצוממ תיתנש הסנכה קוריפ — {format_number(avg_annual_income)} ",
                fontsize=13,
                fontweight="bold",
                pad=15,
            )
            fig_c.tight_layout()
            st.pyplot(fig_c)

            # --- PDF מסחרי ---
            logo_html = company_logo_html()
            if prof.get("role") == "premium" and prof.get("subscription_status") == "active":
                ulogo = get_logo_url(prof.get("id"))
                if ulogo:
                    logo_html = f"<div><img src='{ulogo}' style='width:200px;'></div>"

            default_company_name = "נועם - ניהול וייעוץ עסקי מתקדם"
            default_email = "noamconsult@gmail.com"
            default_phone = "052-6013126"
            default_title = "M.Sc Financial Math"
            company_name = prof.get("company_name") or default_company_name
            contact_email = prof.get("contact_email") or default_email
            contact_phone = prof.get("contact_phone") or default_phone

            if is_premium:
                contact_html = f"""
        <div class='contact'>
        <strong>📞 פרטי התקשרות</strong><br><br>
        👤 {company_name}<br>
        ✉️ {contact_email}<br>
        📞 {contact_phone}
        </div>
        """
            else:
                contact_html = f"""
        <div class='contact'>
        <strong>📞 פרטי התקשרות</strong><br><br>
        👤 {company_name}<br>
        ✉️ {contact_email}<br>
        📞 {contact_phone}<br>
        🎓 {default_title}
        </div>
        """

            deal_notes_html = f"""
        <div class='deal-notes'>
        <strong>📋 הערות ודגשים לעסקה</strong><br><br>
        {deal_notes.replace(chr(10), '<br>')}
        </div>
        """ if deal_notes.strip() else ""

            disclaimer_html = """
        <div class='disclaimer'>
        <strong>❗ הערות והסתייגויות</strong><br><br>
        החישובים בדו"ח זה מבוססים על נתונים ותעריפים כפי שהוזנו ביום החישוב.
        אין לראות בהם הבטחה לתשואה עתידית אלא הערכה כלכלית בלבד.
        התוצאות בפועל עשויות להשתנות עקב שינויי רגולציה, תעריפי חשמל, ריבית ותנאי מימון.
        </div>
        """

            pdf_html_comm = f"""<html dir='rtl'>
        <head>
            <meta charset='UTF-8'>
            <style>
                @page {{
                    margin: 2cm 2cm 2.5cm 2cm;
                    @bottom-center {{
                        content: "עמוד " counter(page) " מתוך " counter(pages);
                        font-size: 10px; color: #888; font-family: Arial;
                    }}
                }}
                body {{ font-family: Arial, sans-serif; direction: rtl; text-align: right; font-size: 13px; line-height: 1.6; color: #222; }}
                .header {{ display: table; width: 100%; margin-bottom: 10px; }}
                .header-logo {{ display: table-cell; width: 25%; text-align: right; vertical-align: middle; }}
                .header-title {{ display: table-cell; width: 50%; text-align: center; vertical-align: middle; }}
                .header-date {{ display: table-cell; width: 25%; text-align: left; vertical-align: middle; color: #555; font-size: 13px; }}
                hr {{ border: none; border-top: 2px solid #2E86AB; margin: 10px 0 20px 0; }}
                .section {{ page-break-inside: avoid; margin-top: 18px; }}
                .section-title {{ background-color: #2E86AB; color: white; padding: 7px 10px; font-size: 15px; font-weight: bold; border-radius: 3px; margin-bottom: 6px; page-break-after: avoid; }}
                .section p {{ margin: 3px 10px; }}
                table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 6px; page-break-inside: auto; }}
                thead tr {{ background-color: #2E86AB; color: white; page-break-after: avoid; }}
                th {{ padding: 7px 8px; text-align: center; font-weight: bold; }}
                td {{ padding: 6px 8px; text-align: center; border-bottom: 1px solid #e0e0e0; }}
                tbody tr:nth-child(even) {{ background-color: #f4f8fb; }}
                tbody tr:last-child td {{ font-weight: bold; background-color: #e8f4f8; border-top: 2px solid #2E86AB; }}
                tr {{ page-break-inside: avoid; }}
                .summary-box {{ background: #f0f7ff; border-right: 4px solid #2E86AB; padding: 14px 18px; border-radius: 4px; margin: 16px 0; page-break-inside: avoid; }}
                .summary-box p {{ margin: 5px 0; font-size: 14px; }}
                .graphs-section {{ page-break-before: always; }}
                .graphs-section img {{ width: 100%; margin: 10px 0; display: block; }}
                .disclaimer {{ page-break-inside: avoid; background-color: #fff8e1; border-right: 4px solid #f39c12; padding: 10px 14px; margin-top: 20px; font-size: 12px; color: #555; border-radius: 3px; }}
                .deal-notes {{ page-break-inside: avoid; background-color: #f0fff4; border-right: 4px solid #3BB273; padding: 10px 14px; margin-top: 20px; font-size: 12px; color: #333; border-radius: 3px; }}
                .contact {{ page-break-inside: avoid; margin-top: 15px; background-color: #f0f7ff; padding: 10px 14px; border-radius: 3px; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="header">
                <div class="header-logo">{signature_html}</div>
                <div class="header-title">
                    <h1 style="color: #2E86AB; margin: 0; font-size: 20px;">דו"ח מפורט - פרויקט סולארי מסחרי</h1>
                </div>
                <div class="header-date">{datetime.now().strftime('%d/%m/%Y')}</div>
            </div>
            <hr>

        <div class='section'>
          <div class='section-title'>📝 פרטי הלקוח</div>
          <p>שם הלקוח: {client_name}</p>
          <p>כתובת: {client_address}</p>
          <p>טלפון: {client_phone}</p>
        </div>

        <div class='section'>
          <div class='section-title'>🔌 נתוני מערכת סולארית</div>
          <p>גודל המערכת: {system_kw} KW DC ({kw_ac:.1f} KW AC)</p>
          <p>תעריף משוקלל: {commercial_tariff:.4f} ₪/קוט"ש</p>
          <p>שעות שמש: {format_number(annual_hours)}</p>
          <p>פחת תפוקה שנתי: {yield_drop_percent}%</p>
        </div>

        <div class='section'>
          <div class='section-title'>💸 עלויות הפרויקט</div>
          <p>עלות הקמה לKW: {format_number(cost_per_kw)} ₪</p>
          <p>אגרת PV: {format_number(fee_pv)} ₪</p>
          <p>אגרת הגדלת חיבור: {format_number(fee_connection)} ₪</p>
          <p>הגדלת חיבור תשתיות: {format_number(fee_infra)} ₪</p>
          <p>בלתי צפוי/שונות: {format_number(fee_misc)} ₪</p>
          <p>סך עלות פרויקט: {format_number(full_cost)} ₪</p>
          <p>הון עצמי: {format_number(equity)} ₪</p>
          <p>הלוואה דרושה: {format_number(loan_amount)} ₪</p>
        </div>

        <div class='section'>
          <div class='section-title'>💰 נתונים פיננסיים</div>
          <p>תקופת הלוואה: {loan_years} שנים</p>
          <p>ריבית הלוואה: {loan_rate_percent}%</p>
          <p>ביטוח שנתי: {insurance_percent}%</p>
          <p>תחזוקה לקילו וואט: {format_number(maintenance)} ₪</p>
        </div>

        <div class='section'>
          <div class='section-title'>📊 סיכום הפרויקט</div>
          <div class='summary-box'>
            <p>📌 <strong>עלות כוללת של הפרויקט:</strong> {format_number(full_cost)} ₪</p>
            <p>⚡ <strong>הכנסה שנתית ממוצעת:</strong> {format_number(avg_annual_income)} ₪</p>
            <p>🔧 <strong>הוצאות תפעול שנתיות (ללא מימון):</strong> {format_number(avg_annual_opex)} ₪</p>
            <p>🏦 <strong>החזר הלוואה שנתי:</strong> {format_number(annual_loan_payment)} ₪</p>
            <p>⏱️ <strong>שנות החזר השקעה:</strong> {payback_str}</p>
            <p>💵 <strong>הכנסה שנתית פנויה ממוצעת:</strong> {format_number(avg_free_annual)} ₪</p>
            <p>📈 <strong>IRR פרויקט (ללא מינוף):</strong> {irr_proj_str}</p>
            <p>💹 <strong>IRR הון עצמי:</strong> {irr_eq_str}</p>
            <p>🏆 <strong>תשואה מצטברת על ההון:</strong> {roe:.1f}%</p>
          </div>
        </div>

        <div class='section'>
          <div class='section-title'>📋 טבלת פירוט שנתית</div>
          {detail_html}
        </div>

        <div class='graphs-section'>
          <div class='section-title'>📈 גרף תזרים</div>
          <img src="{fig_to_base64(fig_c)}">
        </div>

        {deal_notes_html}
        {disclaimer_html}
        {contact_html}
        </body></html>
        """

            pdf_buffer = export_pdf_from_html(pdf_html_comm)
            pdf_bytes = pdf_buffer.getvalue()
            st.session_state["last_pdf_bytes"] = pdf_bytes
            st.session_state["last_pdf_name"] = f"solar_commercial_{client_name}_{datetime.now().strftime('%Y%m%d')}.pdf"

            # לוג
            user = st.session_state.get("user")
            user_id = getattr(user, "id", None) if user else None
            if user_id:
                inputs = {"client_name": client_name, "system_kw": system_kw, "system_type": "commercial"}
                outputs = {"full_cost": full_cost, "irr_project": irr_project, "irr_equity": irr_equity}
                log_calc(user_id, inputs, outputs, pdf_bytes)
            else:
                st.info("חיבור משתמש נדרש לשם תיעוד ושמירת חישובים.")

            st.success("✅ החישוב הושלם!")

        # ==================== ביתי ====================
        else:
            loan_rate = loan_rate_percent / 100
            inflation = inflation_percent / 100
            yield_drop = yield_drop_percent / 100
            insurance_amount = insurance_percent / 100 * total_system_cost
            maintenance_amount = maintenance * system_kw
            tariff_regular = 0.48
            tariff_indexed = 0.387

            if loan_amount > 0 and loan_years > 0 and loan_rate > 0:
                monthly_rate = loan_rate / 12
                n_months = int(loan_years) * 12
                monthly_payment = loan_amount * (monthly_rate * (1 + monthly_rate) ** n_months) / ((1 + monthly_rate) ** n_months - 1)
                annual_loan_payment = monthly_payment * 12
            else:
                annual_loan_payment = 0

            years = list(range(1, 26))
            gross_yields = [
                system_kw * annual_hours * ((1 - yield_drop) ** (year - 1))
                for year in years
            ]
            incomes_regular = [tariff_regular * gy for gy in gross_yields]
            incomes_gradual = [
                0.6 * gy if year <= 5 else 0.39 * gy
                for year, gy in zip(years, gross_yields)
            ]
            incomes_indexed = [
                tariff_indexed * ((1 + inflation) ** (year - 1)) * gy
                for year, gy in zip(years, gross_yields)
            ]

            net_regular = [
                inc - insurance_amount - maintenance_amount for inc in incomes_regular
            ]
            net_gradual = [
                inc - insurance_amount - maintenance_amount for inc in incomes_gradual
            ]
            net_indexed = [
                inc - insurance_amount - maintenance_amount for inc in incomes_indexed
            ]

            cashflow_regular, cashflow_gradual, cashflow_indexed = [], [], []
            cum_r = cum_g = cum_i = 0
            moneys = []
            for yr in range(25):
                m_payment = annual_loan_payment if yr < loan_years else 0
                moneys.append(m_payment)
                n_r = net_regular[yr] - m_payment
                n_g = net_gradual[yr] - m_payment
                n_i = net_indexed[yr] - m_payment
                cum_r += n_r
                cum_g += n_g
                cum_i += n_i
                cashflow_regular.append(cum_r)
                cashflow_gradual.append(cum_g)
                cashflow_indexed.append(cum_i)

            # ----- טבלת פירוט -----
            detail_df_numeric = pd.DataFrame(
                {
                    "שנה": years,
                    "הכנסה<br>(צמוד מדד)": incomes_indexed,
                    "הכנסה<br>(מדורג)": incomes_gradual,
                    "הכנסה<br>(רגיל)": incomes_regular,
                    "תחזוקה": [maintenance_amount] * 25,
                    "ביטוח": [insurance_amount] * 25,
                    "מימון": moneys,
                }
            )

            # שורת סה"כ
            totals_numeric = detail_df_numeric.drop(columns=["שנה"]).sum()
            totals_row = pd.DataFrame(
                [["סה\"כ"] + totals_numeric.tolist()],
                columns=detail_df_numeric.columns,
            )
            detail_df_numeric = pd.concat(
                [detail_df_numeric, totals_row], ignore_index=True
            )

            # בחירת שנים להצגה לפי מצב תצוגה
            if view_mode == "שנים נבחרות":
                display_years = list(range(1, int(loan_years) + 1))
                for yr in [10, 15, 20, 25]:
                    if yr > loan_years:
                        display_years.append(yr)
            else:
                display_years = years

            mask = detail_df_numeric["שנה"].isin(display_years) | (
                detail_df_numeric["שנה"] == 'סה"כ'
            )
            detail_df_numeric_short = detail_df_numeric[mask].copy()

            detail_df_display = detail_df_numeric_short.copy()
            for col in detail_df_display.columns:
                if col != "שנה":
                    detail_df_display[col] = detail_df_display[col].apply(format_number)

            st.subheader("📋 טבלת פירוט שנתית")
            st.caption(
                "הטבלה מוצגת בהתאם לרמת הפירוט שנבחרה לעיל. "
                "ניתן להציג פירוט מלא או שנים נבחרות בלבד."
            )
            st.dataframe(detail_df_display)

            # ----- חישובים לסיכום -----
            total_income_regular = sum(incomes_regular)
            total_income_gradual = sum(incomes_gradual)
            total_income_indexed = sum(incomes_indexed)

            total_energy = sum(gross_yields)
            years_count = len(years)

            if total_energy > 0:
                avg_tariff_regular = total_income_regular / total_energy
                avg_tariff_gradual = total_income_gradual / total_energy
                avg_tariff_indexed = total_income_indexed / total_energy
            else:
                avg_tariff_regular = avg_tariff_gradual = avg_tariff_indexed = 0

            avg_free_annual_regular = (
                cashflow_regular[-1] / years_count if years_count else 0
            )
            avg_free_annual_gradual = (
                cashflow_gradual[-1] / years_count if years_count else 0
            )
            avg_free_annual_indexed = (
                cashflow_indexed[-1] / years_count if years_count else 0
            )

            if equity > 0:
                roe_regular = cashflow_regular[-1] / equity
                roe_gradual = cashflow_gradual[-1] / equity
                roe_indexed = cashflow_indexed[-1] / equity
            else:
                roe_regular = roe_gradual = roe_indexed = 0

            summary_df = pd.DataFrame(
                {
                    "מסלול": ["רגיל", "מדורג", "צמוד מדד"],
                    'תעריף ממוצע (₪/קוט"ש)': [
                        avg_tariff_regular,
                        avg_tariff_gradual,
                        avg_tariff_indexed,
                    ],
                    "הכנסה שנתית פנויה (ממוצע)": [
                        avg_free_annual_regular,
                        avg_free_annual_gradual,
                        avg_free_annual_indexed,
                    ],
                    "סה\"כ הכנסה ל-25 שנים": [
                        total_income_regular,
                        total_income_gradual,
                        total_income_indexed,
                    ],
                    "סה\"כ תזרים פנוי נטו ל-25 שנה": [
                        cashflow_regular[-1],
                        cashflow_gradual[-1],
                        cashflow_indexed[-1],
                    ],
                    "תשואה מצטברת על ההון העצמי": [
                        roe_regular,
                        roe_gradual,
                        roe_indexed,
                    ],
                }
            )

            summary_df_display = summary_df.copy()
            summary_df_display['תעריף ממוצע (₪/קוט"ש)'] = summary_df_display[
                'תעריף ממוצע (₪/קוט"ש)'
            ].apply(lambda x: f"{x:.3f}")
            summary_df_display["תשואה מצטברת על ההון העצמי"] = summary_df_display[
                "תשואה מצטברת על ההון העצמי"
            ].apply(lambda x: f"{x*100:.1f}%" if isinstance(x, (int, float)) else x)

            for col in [
                "הכנסה שנתית פנויה (ממוצע)",
                "סה\"כ הכנסה ל-25 שנים",
                "סה\"כ תזרים פנוי נטו ל-25 שנה",
            ]:
                summary_df_display[col] = summary_df_display[col].apply(format_number)

            st.subheader("📊 טבלת סיכום")
            st.table(summary_df_display)

            # ----- טקסטים מילוליים -----
            regular_net = cashflow_regular[-1]
            gradual_net = cashflow_gradual[-1]
            indexed_net = cashflow_indexed[-1]

            regular_sentence_md = (
                f"במסלול הרגיל מתקבל תזרים נטו פנוי של כ- {format_number(regular_net)} ₪ "
                f"לאורך 25 שנים, בתעריף **קבוע** של 0.48 ₪ לקוט\"ש."
            )
            gradual_sentence_md = (
                f"במסלול המדורג מתקבל תזרים נטו של כ- {format_number(gradual_net)} ₪ "
                f"לאורך 25 שנים. ב-6 השנים הראשונות התעריף הוא 0.60 ₪ לקוט\"ש, "
                f"ובהמשך 0.39 ₪ לקוט\"ש, והתעריף הממוצע הכולל על פני כל התקופה הוא כ- {avg_tariff_gradual:.3f} ₪ לקוט\"ש."
            )
            indexed_sentence_md = (
                f"במסלול צמוד המדד מתקבל תזרים נטו של כ- {format_number(indexed_net)} ₪ "
                f"לאורך 25 שנים. מסלול זה מושפע מאינפלציה – ייתכנו בו תנודות, "
                f"ולא ניתן להבטיח את התעריף הצפוי על פי הנחת האינפלציה במועד החישוב. "
                f"מסלול זה שומר על ערכו הריאלי של הכסף. "
                f"התעריף ההתחלתי הוא {tariff_indexed:.3f} ₪ לקוט\"ש, "
                f"והתעריף הממוצע על פי הנחת האינפלציה הוא כ- {avg_tariff_indexed:.3f} ₪ לקוט\"ש."
            )

            tracks_info = [
                {
                    "name": "המסלול הרגיל",
                    "net": regular_net,
                    "avg_tariff": avg_tariff_regular,
                },
                {
                    "name": "המסלול המדורג",
                    "net": gradual_net,
                    "avg_tariff": avg_tariff_gradual,
                },
                {
                    "name": "המסלול הצמוד למדד",
                    "net": indexed_net,
                    "avg_tariff": avg_tariff_indexed,
                },
            ]

            best_track = sorted(
                tracks_info, key=lambda t: (-t["net"], -t["avg_tariff"])
            )[0]

            recommended_text = (
                f"מבחינה פיננסית, המסלול המומלץ הוא **{best_track['name']}**. "
                f"במסלול זה התזרים הפנוי נטו גבוה יותר ותעריף מכירת חשמל ממוצע גבוה יותר "
                f"ביחס למסלולים האחרים."
            )

            alt_text_md = ""
            if equity > 0 and best_track["net"] > 0 and years_count > 0:
                total_roe_best = best_track["net"] / equity
                approx_annual_best = (1 + total_roe_best) ** (1 / years_count) - 1
                alt_text_md = (
                    f"תזרים נטו מצטבר של כ- {format_number(best_track['net'])} ₪ "
                    f"על הון עצמי של כ- {format_number(equity)} ₪ משקף תשואה מצטברת של כ- {total_roe_best*100:.1f}%. "
                    f"זה שקול בקירוב לתשואה שנתית ממוצעת של כ- {approx_annual_best*100:.1f}% לאורך {years_count} שנים, "
                    f"בהשוואה לאלטרנטיבות סולידיות כגון פיקדון או אג\"ח."
                )

            st.markdown("### ✅ המלצת מסלול")
            st.markdown(recommended_text)

            st.markdown("#### פירוט המסלולים")
            st.markdown(f"- {regular_sentence_md}")
            st.markdown(f"- {gradual_sentence_md}")
            st.markdown(f"- {indexed_sentence_md}")

            if alt_text_md:
                st.markdown("#### השוואה לאלטרנטיבה סולידית")
                st.markdown(alt_text_md)

            # ----- ניתוח רגישות לאינפלציה -----
            scenarios = []
            base_inflation = inflation
            inflations = [
                max(base_inflation - 0.005, 0),
                base_inflation,
                base_inflation + 0.005,
            ]
            labels = [
                "אינפלציה נמוכה (בסיס - 0.5%)",
                "אינפלציה בסיסית (כפי שנקבע בקלט)",
                "אינפלציה גבוהה (בסיס + 0.5%)",
            ]

            for lbl, inf_scn in zip(labels, inflations):
                incomes_idx_scn = [
                    tariff_indexed * ((1 + inf_scn) ** (year - 1)) * gy
                    for year, gy in zip(years, gross_yields)
                ]
                net_idx_scn = [
                    inc - insurance_amount - maintenance_amount
                    for inc in incomes_idx_scn
                ]
                cum = 0
                cash_idx_scn = []
                for yr in range(25):
                    m_payment = annual_loan_payment if yr < loan_years else 0
                    n_i = net_idx_scn[yr] - m_payment
                    cum += n_i
                    cash_idx_scn.append(cum)

                total_inc_scn = sum(incomes_idx_scn)
                avg_tariff_scn = total_inc_scn / total_energy if total_energy > 0 else 0

                scenarios.append(
                    {
                        "תרחיש": lbl,
                        "אינפלציה שנתית": f"{inf_scn*100:.2f}%",
                        "תעריף ממוצע (צמוד מדד)": avg_tariff_scn,
                        "סה\"כ תזרים נטו ל-25 שנה (צמוד מדד)": cash_idx_scn[-1],
                    }
                )

            sensitivity_df = pd.DataFrame(scenarios)
            sens_display = sensitivity_df.copy()
            sens_display["תעריף ממוצע (צמוד מדד)"] = sens_display[
                "תעריף ממוצע (צמוד מדד)"
            ].apply(lambda x: f"{x:.3f}")
            sens_display["סה\"כ תזרים נטו ל-25 שנה (צמוד מדד)"] = sens_display[
                "סה\"כ תזרים נטו ל-25 שנה (צמוד מדד)"
            ].apply(format_number)

            st.subheader("🔍 ניתוח רגישות לאינפלציה (מסלול צמוד מדד)")
            st.table(sens_display)
            st.caption(
                "האינפלציה אינה ניתנת לחיזוי ויש לקחת זאת בחשבון. "
                "התחזית הרשמית מתעדכנת מעת לעת על ידי בנק ישראל."
            )

            # ----- גרפים למסך -----
            fig1, ax1 = plt.subplots()
            ax1.plot(years, cashflow_regular, label="ליגר לולסמ")
            ax1.plot(years, cashflow_gradual, label="גרודמ לולסמ")
            ax1.plot(years, cashflow_indexed, label="דדמ דומצ לולסמ")
            ax1.set_xlabel("הנש")
            ax1.set_ylabel("(₪) רבטצמ םירזת")
            ax1.set_title("םילולסמה ןיב רבטצמ םירזת תאוושה")
            ax1.legend()
            st.pyplot(fig1)

            fig2, ax2 = plt.subplots(figsize=(10, 5))
            colors = {"ליגר": "#2E86AB", "גרודמ": "#F18F01", "דדמ דומצ": "#3BB273"}
            ax2.fill_between(years, incomes_regular, alpha=0.12, color=colors["ליגר"])
            ax2.fill_between(years, incomes_gradual, alpha=0.12, color=colors["גרודמ"])
            ax2.fill_between(years, incomes_indexed, alpha=0.12, color=colors["דדמ דומצ"])
            ax2.plot(years, incomes_regular, label="ליגר", color=colors["ליגר"], linewidth=2.5, marker="o", markersize=4)
            ax2.plot(years, incomes_gradual, label="גרודמ", color=colors["גרודמ"], linewidth=2.5, marker="s", markersize=4)
            ax2.plot(years, incomes_indexed, label="דדמ דומצ", color=colors["דדמ דומצ"], linewidth=2.5, marker="^", markersize=4)
            ax2.set_xlabel("הנש", fontsize=11)
            ax2.set_ylabel("(₪) תיתנש הסנכה", fontsize=11)
            ax2.set_title("םילולסמה ןיב תיתנש הסנכה תאוושה", fontsize=13, fontweight="bold")
            ax2.legend(fontsize=10)
            ax2.grid(axis="y", linestyle="--", alpha=0.4)
            ax2.spines["top"].set_visible(False)
            ax2.spines["right"].set_visible(False)
            fig2.tight_layout()
            st.pyplot(fig2)

            # ----- בניית PDF -----
            logo_html = company_logo_html()
            if prof.get("role") == "premium" and prof.get("subscription_status") == "active":
                ulogo = get_logo_url(prof.get("id"))
                if ulogo:
                    logo_html = f"<div><img src='{ulogo}' style='width:200px;'></div>"

            # פרטי התקשרות – ברירת מחדל + אפשרות החלפה למנוי Pro+Logo
            default_company_name = "נועם - ניהול וייעוץ עסקי מתקדם"
            default_email = "noamconsult@gmail.com"
            default_phone = "052-6013126"
            default_title = "M.Sc Financial Math"

            company_name = prof.get("company_name") or default_company_name
            contact_email = prof.get("contact_email") or default_email
            contact_phone = prof.get("contact_phone") or default_phone

            if is_premium:
                # premium — פרטי הלקוח שלו, ללא שורת השכלה
                contact_html = f"""
            <div class='contact'>
            <strong>📞 פרטי התקשרות</strong><br><br>
            👤 {company_name}<br>
            ✉️ {contact_email}<br>
            📞 {contact_phone}
            </div>
            """
            else:
                # חינמי / pro — פרטי ברירת מחדל שלי, עם שורת השכלה
                contact_html = f"""
            <div class='contact'>
            <strong>📞 פרטי התקשרות</strong><br><br>
            👤 {company_name}<br>
            ✉️ {contact_email}<br>
            📞 {contact_phone}<br>
            🎓 {default_title}
            </div>
            """

            html_style = """
            <style>
            body { font-family: Arial; direction: rtl; text-align: right; }
            table {
              border-collapse: collapse;
              width: 100%;
              table-layout: fixed;
              page-break-inside: avoid;
              border: 1px solid #2E86AB;
            }
            th, td {
              border: 1px solid #2E86AB;
              padding: 4px;
              text-align: center;
              white-space: normal;
              min-width: 70px;
              page-break-inside: avoid;
            }
            tr { page-break-inside: avoid; page-break-after: auto; }
            .summary-box {
              border: 1px solid #2E86AB;
              padding: 8px;
              margin-top: 10px;
              border-radius: 6px;
              page-break-inside: avoid;
            }
            .section-title {
              background-color: #2E86AB;
              color: white;
              padding: 6px;
              font-size: 16px;
              margin-top: 15px;
              page-break-inside: avoid;
            }
            </style>
            """

            # טקסטים ב-HTML לדוח
            regular_sentence_html = (
                f"במסלול הרגיל מצטבר תזרים נטו פנוי של כ- {format_number(regular_net)} ₪ "
                f"לאורך 25 שנים, בתעריף <strong>קבוע</strong> של 0.48 ₪ לקוט\"ש."
            )
            gradual_sentence_html = (
                f"במסלול המדורג מצטבר תזרים נטו של כ- {format_number(gradual_net)} ₪ "
                f"לאורך 25 שנים. ב-6 השנים הראשונות התעריף הוא 0.60 ₪ לקוט\"ש, "
                f"ובהמשך 0.39 ₪ לקוט\"ש, והתעריף הממוצע הכולל על פני כל התקופה הוא כ- {avg_tariff_gradual:.3f} ₪ לקוט\"ש."
            )
            indexed_sentence_html = (
                f"במסלול צמוד המדד מצטבר תזרים נטו של כ- {format_number(indexed_net)} ₪ "
                f"לאורך 25 שנים. מסלול זה מושפע מאינפלציה – ייתכנו בו תנודות, "
                f"ולא ניתן להבטיח את התעריף הצפוי על פי הנחת האינפלציה במועד החישוב. "
                f"מסלול זה שומר על ערכו הריאלי של הכסף. "
                f"התעריף ההתחלתי הוא {tariff_indexed:.3f} ₪ לקוט\"ש, "
                f"והתעריף הממוצע על פי הנחת האינפלציה הוא כ- {avg_tariff_indexed:.3f} ₪ לקוט\"ש."
            )

            alt_text_html = ""
            if equity > 0 and best_track["net"] > 0 and years_count > 0:
                total_roe_best = best_track["net"] / equity
                approx_annual_best = (1 + total_roe_best) ** (1 / years_count) - 1
                alt_text_html = (
                    f"תזרים נטו מצטבר של כ- {format_number(best_track['net'])} ₪ "
                    f"על הון עצמי של כ- {format_number(equity)} ₪ משקף תשואה מצטברת של כ- {total_roe_best*100:.1f}%. "
                    f"זה שקול בקירוב לתשואה שנתית ממוצעת של כ- {approx_annual_best*100:.1f}% לאורך {years_count} שנים, "
                    f"בהשוואה לאלטרנטיבות סולידיות כגון פיקדון או אג\"ח."
                )

            recommended_text_html = f"""
            <div class='section-title'>✅ המלצת מסלול</div>
            <p>{recommended_text}</p>
            <div><strong>פירוט המסלולים:</strong><br>
            1. {regular_sentence_html}<br>
            2. {gradual_sentence_html}<br>
            3. {indexed_sentence_html}
            </div>
            """

            if alt_text_html:
                recommended_text_html += f"""
                <div class='section-title'>🔄 השוואה לאלטרנטיבה סולידית</div>
                <p>{alt_text_html}</p>
                """

            sensitivity_html = sens_display.to_html(index=False, escape=False)

            deal_notes_html = f"""
            <div class='deal-notes'>
            <strong>📋 הערות ודגשים לעסקה</strong><br><br>
            {deal_notes.replace(chr(10), '<br>')}
            </div>
            """ if deal_notes.strip() else ""

            disclaimer_html = """
            <div class='disclaimer'>
            <strong>❗ הערות והסתייגויות</strong><br><br>
            החישובים בדו\"ח זה מבוססים על נתונים ותעריפים כפי שהוזנו ביום החישוב.
            אין לראות בהם הבטחה לתשואה עתידית אלא הערכה כלכלית בלבד.
            התוצאות בפועל עשויות להשתנות עקב שינויי רגולציה, תעריפי חשמל, אינפלציה, ריבית ותנאי מימון.
            האינפלציה אינה ניתנת לחיזוי ויש לקחת זאת בחשבון. התחזיות הרשמיות מתעדכנות מעת לעת על ידי בנק ישראל.
            </div>
            """

            pdf_html = f"""<html dir='rtl'>
            <head>
                <meta charset='UTF-8'>
                <style>
                    @page {{
                        margin: 2cm 2cm 2.5cm 2cm;
                        @bottom-center {{
                            content: "עמוד " counter(page) " מתוך " counter(pages);
                            font-size: 10px;
                            color: #888;
                            font-family: Arial;
                        }}
                    }}
                    body {{
                        font-family: Arial, sans-serif;
                        direction: rtl;
                        text-align: right;
                        font-size: 13px;
                        line-height: 1.6;
                        color: #222;
                    }}
                    .header {{
                        display: flex;
                        justify-content: space-between;
                        align-items: flex-start;
                        width: 100%;
                        margin-bottom: 10px;
                    }}
                    hr {{
                        border: none;
                        border-top: 2px solid #2E86AB;
                        margin: 10px 0 20px 0;
                    }}

                    /* כותרת סקשן + התוכן שלה לא נחתכים */
                    .section {{
                        page-break-inside: avoid;
                        margin-top: 18px;
                    }}
                    .section-title {{
                        background-color: #2E86AB;
                        color: white;
                        padding: 7px 10px;
                        font-size: 15px;
                        font-weight: bold;
                        border-radius: 3px;
                        margin-bottom: 6px;
                        page-break-after: avoid;
                    }}
                    .section p {{
                        margin: 3px 10px;
                    }}

                    /* טבלאות */
                    table {{
                        width: 100%;
                        border-collapse: collapse;
                        font-size: 12px;
                        margin-top: 6px;
                        page-break-inside: auto;
                    }}
                    thead tr {{
                        background-color: #2E86AB;
                        color: white;
                        page-break-after: avoid;
                    }}
                    th {{
                        padding: 7px 8px;
                        text-align: center;
                        font-weight: bold;
                    }}
                    td {{
                        padding: 6px 8px;
                        text-align: center;
                        border-bottom: 1px solid #e0e0e0;
                    }}
                    tbody tr:nth-child(even) {{
                        background-color: #f4f8fb;
                    }}
                    tbody tr:last-child td {{
                        font-weight: bold;
                        background-color: #e8f4f8;
                        border-top: 2px solid #2E86AB;
                    }}
                    tr {{ page-break-inside: avoid; }}

                    /* גרפים זה מתחת לזה ורוחב מלא */
                    .graphs-section {{
                        page-break-before: always;
                    }}
                    .graphs-section img {{
                        width: 100%;
                        margin: 10px 0;
                        display: block;
                    }}

                    /* הערות */
                    .disclaimer {{
                        page-break-inside: avoid;
                        background-color: #fff8e1;
                        border-right: 4px solid #f39c12;
                        padding: 10px 14px;
                        margin-top: 20px;
                        font-size: 12px;
                        color: #555;
                        border-radius: 3px;
                    }}

                    /* הערות ודגשים לעסקה */
                    .deal-notes {{
                        page-break-before: always;
                        page-break-inside: avoid;
                        background-color: #f0fff4;
                        border-right: 4px solid #3BB273;
                        padding: 10px 14px;
                        margin-top: 0;
                        font-size: 12px;
                        color: #333;
                        border-radius: 3px;
                    }}

                    /* פרטי התקשרות */
                    .contact {{
                        page-break-inside: avoid;
                        margin-top: 15px;
                        background-color: #f0f7ff;
                        padding: 10px 14px;
                        border-radius: 3px;
                        font-size: 12px;
                    }}
                </style>
            </head>
            <body>
                <div class="header">
                    <div style="text-align: right;">
                        <h1 style="color: #2E86AB; margin: 0; font-size: 22px;">☀️ דוח מפורט - פרויקט סולארי</h1>
                        <p style="margin:4px 0; color:#2E86AB;">תאריך: {datetime.now().strftime('%d/%m/%Y')}</p>
                    </div>
                    {signature_html}
                </div>
                <hr>

            <div class='section'>
              <div class='section-title'>📝 פרטי הלקוח</div>
              <p>שם הלקוח: {client_name}</p>
              <p>כתובת: {client_address}</p>
              <p>טלפון: {client_phone}</p>
            </div>

            <div class='section'>
              <div class='section-title'>🔌 נתוני מערכת סולארית</div>
              <p>גודל המערכת: {system_kw} קילוואט</p>
              <p>עלות הקמה לKW: {format_number(cost_per_kw)} ₪</p>
              <p>שעות שמש: {format_number(annual_hours)}</p>
              <p>פחת תפוקה שנתי: {yield_drop_percent}%</p>
            </div>

            <div class='section'>
              <div class='section-title'>💸 השקעות / עלויות נוספות</div>
              <p>אגרת PV: {format_number(fee_pv)} ₪</p>
              <p>אגרת הגדלת חיבור: {format_number(fee_connection)} ₪</p>
              <p>הגדלת חיבור תשתיות: {format_number(fee_infra)} ₪</p>
              <p>בלתי צפוי/שונות: {format_number(fee_misc)} ₪</p>
            </div>

            <div class='section'>
              <div class='section-title'>💰 סך עלות פרויקט</div>
              <p>{format_number(full_cost)} ₪</p>
              <p>הון עצמי: {format_number(equity)} ₪</p>
            </div>

            <div class='section'>
              <div class='section-title'>💵 הלוואה דרושה</div>
              <p>{format_number(loan_amount)} ₪</p>
            </div>

            <div class='section'>
              <div class='section-title'>💰 נתונים פיננסיים</div>
              <p>תקופת הלוואה: {loan_years} שנים</p>
              <p>ריבית הלוואה: {loan_rate_percent}%</p>
              <p>אינפלציה: {inflation_percent}%</p>
            </div>

            <div class='section'>
              <div class='section-title'>🛠️ הוצאות שנתיות</div>
              <p>ביטוח שנתי: {insurance_percent}%</p>
              <p>תחזוקה לקילו וואט: {format_number(maintenance)} ₪</p>
            </div>

            <div class='section'>
              <div class='section-title'>📊 טבלת סיכום</div>
              {summary_df_display.to_html(index=False, escape=False)}
              {recommended_text_html}
            </div>

            <div class='section'>
              <div class='section-title'>🔍 ניתוח רגישות לאינפלציה (מסלול צמוד מדד)</div>
              {sensitivity_html}
            </div>

            <div class='section'>
              <div class='section-title'>📋 טבלת פירוט שנתית (לפי רמת הפירוט שנבחרה)</div>
              {detail_df_display.to_html(index=False, escape=False)}
            </div>

            <div class='graphs-section'>
              <div class='section-title'>📈 גרפים</div>
              <img src="{fig_to_base64(fig1)}">
              <img src="{fig_to_base64(fig2)}">
            </div>

            {deal_notes_html}
            {disclaimer_html}
            {contact_html}
            </body></html>
            """

            pdf_buffer = export_pdf_from_html(pdf_html)
            pdf_bytes = pdf_buffer.getvalue()

            # שמירת ה-PDF ב-session_state כדי שיישאר זמין אחרי הרצה
            st.session_state["last_pdf_bytes"] = pdf_bytes
            st.session_state["last_pdf_name"] = f"solar_report_{client_name}_{datetime.now().strftime('%Y%m%d')}.pdf"

            # לוג לחישוב
            user = st.session_state.get("user")
            user_id = getattr(user, "id", None) if user else None
            if user_id:
                inputs = {
                    "client_name": client_name,
                    "system_kw": system_kw,
                    "cost_per_kw": cost_per_kw,
                    "annual_hours": annual_hours,
                    "yield_drop_percent": yield_drop_percent,
                    "equity": equity,
                    "loan_years": loan_years,
                    "loan_rate_percent": loan_rate_percent,
                    "inflation_percent": inflation_percent,
                    "insurance_percent": insurance_percent,
                    "maintenance": maintenance,
                    "fees": {
                        "pv": fee_pv,
                        "conn": fee_connection,
                        "infra": fee_infra,
                        "misc": fee_misc,
                    },
                }
                outputs = {
                    "full_cost": full_cost,
                    "loan_amount": loan_amount,
                    "cashflow_regular_end": cashflow_regular[-1],
                    "cashflow_gradual_end": cashflow_gradual[-1],
                    "cashflow_indexed_end": cashflow_indexed[-1],
                }
                log_calc(user_id, inputs, outputs, pdf_bytes)
            else:
                st.info("חיבור משתמש נדרש לשם תיעוד ושמירת חישובים.")

            st.success("✅ החישוב הושלם!")

    # --- הצגת כפתורי הורדה ורענון מחוץ לבלוק הכפתור ---
    # כך נשארים זמינים גם לאחר אינטראקציות נוספות עם הדף
    if st.session_state.get("last_pdf_bytes"):
        import io as _io
        st.download_button(
            "📄 הורד דו\"ח PDF",
            _io.BytesIO(st.session_state["last_pdf_bytes"]),
            st.session_state["last_pdf_name"],
            mime="application/pdf",
        )
        if st.button("🔄 רענן מחשבון (חישוב חדש)"):
            st.session_state["last_pdf_bytes"] = None
            st.session_state["last_pdf_name"] = None
            st.rerun()


def history_ui():
    st.subheader("🗂️ החישובים שלי (14 ימים אחרונים)")
    user = st.session_state.get("user")
    if not user:
        st.info("יש להתחבר כדי לראות היסטוריה.")
        return
    rows = user_history(user.id)
    if not rows:
        st.write("אין חישובים להצגה.")
        return
    for r in rows:
        cols = st.columns([2, 2, 3])
        with cols[0]:
            import pytz

            israel_tz = pytz.timezone("Asia/Jerusalem")
            st.write(pd.to_datetime(r["created_at"]).tz_convert(israel_tz))

        with cols[1]:
            outs = r.get("outputs", {})
            st.write(
                f"תזרים נטו (רגיל) 25Y: {format_number(outs.get('cashflow_regular_end', 0))} ₪"
            )
        with cols[2]:
            url = r.get("report_url")
            if url:
                st.markdown(f"[הורדת הדו\"ח]({url})")


def admin_ui():
    st.header("👑 אזור מנהל")
    prof = st.session_state.get("profile") or {}
    if prof.get("is_admin") is not True:
        st.error("אין הרשאה לאזור המנהל.")
        return
    sb = sb_client()
    st.subheader("💾 כל החישובים")
    try:
        res = (
            sb.table("calculations")
            .select("created_at, user_id, outputs, report_url")
            .order("created_at", desc=True)
            .limit(500)
            .execute()
        )
        df = pd.DataFrame(res.data or [])
        st.dataframe(df)
    except Exception:
        st.info("אין נתונים להצגה כעת.")

    st.subheader("💲 מחירים")
    prices = get_pricing()
    p_free = st.number_input("Free (₪)", value=int(prices.get("free", 0)))
    p_pro = st.number_input("Pro (₪)", value=int(prices.get("pro", 150)))
    p_prol = st.number_input("Pro+Logo (₪)", value=int(prices.get("premium", 250)))
    if st.button("עדכן מחירים"):
        update_pricing({"free": p_free, "pro": p_pro, "premium": p_prol})
        st.success("עודכן")

    st.subheader("👥 ניהול משתמשים (שינוי תוכנית/סטטוס)")
    email = st.text_input("אימייל משתמש")
    role = st.selectbox("תוכנית", ["free", "pro", "premium"])
    status = st.selectbox("סטטוס מנוי", ["inactive", "active", "past_due", "canceled"])
    if st.button("שמור למשתמש") and email:
        try:
            sb.table("profiles").update(
                {"role": role, "subscription_status": status}
            ).eq("email", email).execute()
            st.success("נשמר")
        except Exception as e:
            st.error(str(e))


# ============ ROUTER ============

# אתחול session_state לפני כל שימוש
if "session" not in st.session_state:
    st.session_state.session = None
if "user" not in st.session_state:
    st.session_state.user = None
if "profile" not in st.session_state:
    st.session_state.profile = None


def main():
    auth_panel()
    pricing_box()

    tabs = st.tabs(["📊 מחשבון", "📂 היסטוריה", "👤 פרופיל וחתימה", "🛠️ מנהל"])

    with tabs[0]:
        calculator_ui()

    with tabs[1]:
        history_ui()

    with tabs[2]:
        profile_tab_ui()

    with tabs[3]:
        admin_ui()


if __name__ == "__main__":
    main()
else:
    main()


# ==================
# Supabase SQL (run once)
# ==================
# -- profiles: stores user role and subscription status
# create table if not exists public.profiles (
#   id uuid primary key references auth.users(id) on delete cascade,
#   email text unique,
#   role text not null default 'free', -- 'free' | 'pro' | 'premium'
#   subscription_status text not null default 'inactive', -- 'active' | 'inactive' | 'past_due' | 'canceled'
#   is_admin boolean not null default false,
#   logo_url text,
#   created_at timestamp with time zone default now()
# );
# alter table public.profiles enable row level security;
# drop policy if exists "profiles_select_self_or_admin" on public.profiles;
# create policy "profiles_select_self_or_admin" on public.profiles for select using (auth.uid() = id or public.is_admin());
# drop policy if exists "profiles_update_self" on public.profiles;
# create policy "profiles_update_self" on public.profiles for update using (auth.uid() = id) with check (auth.uid() = id);
# drop policy if exists "profiles_update_admin" on public.profiles;
# create policy "profiles_update_admin" on public.profiles for update using (public.is_admin()) with check (public.is_admin());
# drop policy if exists "profiles_insert_self" on public.profiles;
# create policy "profiles_insert_self" on public.profiles for insert with check (auth.uid() = id);
#
# -- trigger to auto-create profiles from auth.users (optional if you rely on upsert)
# create or replace function public.handle_new_user() returns trigger
# language plpgsql security definer set search_path = public as $$
# begin
#   insert into public.profiles (id, email) values (new.id, new.email)
#   on conflict (id) do update set email = excluded.email;
#   return new;
# end; $$;
#
# drop trigger if exists on_auth_user_created on auth.users;
# create trigger on_auth_user_created
# after insert on auth.users
# for each row execute function public.handle_new_user();
#
# -- pricing
# create table if not exists public.pricing (
#   plan text primary key,
#   monthly_ils numeric not null,
#   updated_at timestamptz default now()
# );
# alter table public.pricing enable row level security;
# drop policy if exists "pricing_read_all" on public.pricing;
# create policy "pricing_read_all" on public.pricing for select using (true);
# drop policy if exists "pricing_write_admin" on public.pricing;
# create policy "pricing_write_admin" on public.pricing for insert with check (public.is_admin());
# drop policy if exists "pricing_update_admin" on public.pricing;
# create policy "pricing_update_admin" on public.pricing for update using (public.is_admin()) with check (public.is_admin());
#
# -- calculations
# create table if not exists public.calculations (
#   id bigserial primary key,
#   user_id uuid references public.profiles(id) on delete cascade,
#   inputs jsonb,
#   outputs jsonb,
#   report_url text,
#   created_at timestamp with time zone default now()
# );
# alter table public.calculations enable row level security;
# drop policy if exists "calculations_select_owner_or_admin" on public.calculations;
# create policy "calculations_select_owner_or_admin" on public.calculations for select using (user_id = auth.uid() or public.is_admin());
# drop policy if exists "calculations_insert_owner" on public.calculations;
# create policy "calculations_insert_owner" on public.calculations for insert with check (user_id = auth.uid());
# drop policy if exists "calculations_delete_admin" on public.calculations;
# create policy "calculations_delete_admin" on public.calculations for delete using (public.is_admin());
#
# -- Storage buckets (create via UI or SQL)
# -- reports (public), logos (public)
