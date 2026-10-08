import os
import base64
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import pandas as pd
import altair as alt
import plotly.graph_objects as go
from wordcloud import WordCloud
import streamlit as st
import extra_streamlit_components as stx


def find_col(df, candidates):
    cand_low = [c.lower() for c in candidates]
    for c in df.columns:
        if c in candidates:
            return c
    for c in df.columns:
        if c.lower() in cand_low:
            return c
    return None


def load_logo_data_uri(filename):
    # Reads the logo file fresh (not a hardcoded base64 blob) so replacing the
    # file on disk is enough to update the print header — no code change needed.
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    try:
        with open(path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except OSError:
        return None


LOGO_DATA_URI = load_logo_data_uri("hydralogo.png")

# Shown as the "ⓘ" tooltip (via st.metric's help=) wherever "Reputation Score" appears.
REPUTATION_SCORE_HELP = (
    "Reputation Score is calculated using the Net Sentiment Score approach — "
    "(positive reviews minus negative reviews) divided by (positive plus negative reviews). "
    "It ranges from -1, which represents the most negative reputation, to +1, "
    "which represents the most positive reputation."
)

load_dotenv()
st.set_page_config(page_title="HYDRA Dashboard", layout="wide")

SUPABASE_URL = os.getenv("SUPABASE_URL")
# App ini HANYA boleh memakai key berhak rendah (anon/publishable) agar RLS berlaku.
# SUPABASE_KEY (secret/service_role) di .env sengaja TIDAK dibaca di sini.
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "reviews")

if "email" in st.session_state:
    username = st.session_state.email.split("@")[0]
    st.markdown(f'<h1 style="text-align: right;">Hello, {username}</h1>', unsafe_allow_html=True)
else:
    st.title("Welcome to HYDRA Dashboard")

# Simple CSS to add visible borders around selectboxes/columns used below
st.markdown(
    """
    <style>
    /* Border for selectbox containers */
    .stSelectbox div[role="listbox"] { border: 1px solid #ddd; padding: 6px; border-radius: 6px; }
    /* Border for columns (best-effort) */
    .competitive-columns > div { border: 1px solid #e6e6e6; padding: 8px; border-radius: 6px; }
    /* Request Intelligence: 15px left/right padding on the table and reviews boxes */
    .st-key-ri_table_box, .st-key-ri_reviews_box { padding-left: 15px !important; padding-right: 15px !important; }

    /* Hidden on screen; only shown by the print stylesheet below. */
    .print-only { display: none; }

    @media print {
        /* Streamlit's own chrome doesn't belong in a printed report. */
        header[data-testid="stHeader"],
        #MainMenu,
        div[data-testid="stToolbar"],
        div[data-testid="stDecoration"],
        [data-testid="stElementToolbar"],
        section[data-testid="stSidebar"],
        [class*="viewerBadge"] {
            display: none !important;
        }

        /* Reclaim the width the sidebar was taking and drop Streamlit's own
           top padding, which exists to clear the (now hidden) header bar. */
        div.block-container {
            max-width: 100% !important;
            padding-top: 0 !important;
        }

        .print-only { display: block !important; }

        /* Don't let a chart or table split awkwardly across a page break. */
        div[data-testid="stVegaLiteChart"],
        div[data-testid="stPlotlyChart"],
        div[data-testid="stDataFrame"] {
            break-inside: avoid;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("Environment variables SUPABASE_URL and SUPABASE_ANON_KEY must be set.")
    st.stop()


def _is_privileged_key(key):
    """True bila key adalah secret/service_role, yang melewati RLS."""
    key = key.strip()
    if key.startswith("sb_secret_"):
        return True
    parts = key.split(".")
    if len(parts) == 3:
        try:
            import json
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            role = json.loads(base64.urlsafe_b64decode(payload)).get("role")
            return role == "service_role"
        except Exception:
            return False
    return False


if _is_privileged_key(SUPABASE_KEY):
    st.error(
        "SUPABASE_ANON_KEY berisi secret/service_role key. Key ini melewati RLS dan tidak boleh "
        "dipakai di dashboard. Isi dengan anon key atau publishable key."
    )
    st.stop()

try:
    from supabase import create_client
except Exception:
    st.error("supabase-py is not installed. Install with 'pip install supabase-py'.")
    st.stop()


def get_client():
    # Satu client PER SESI browser (bukan global), supaya login satu user
    # tidak ikut berlaku untuk user lain yang membuka app bersamaan.
    if "sb" not in st.session_state:
        st.session_state.sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    return st.session_state.sb


COOKIE_REFRESH_TOKEN = "hydra_refresh_token"


def get_cookie_manager():
    # Satu instance PER SESI browser, supaya komponen cookie tidak dibuat ulang setiap rerun.
    if "cookie_manager" not in st.session_state:
        st.session_state.cookie_manager = stx.CookieManager()
    return st.session_state.cookie_manager


def _load_profile(sb, user_id):
    prof = (
        sb.table("user_profiles")
        .select("industry, company")
        .eq("user_id", user_id)
        .single()
        .execute()
        .data
    )
    if not prof or not prof.get("industry") or not prof.get("company"):
        raise ValueError("kolom industry/company kosong")
    return prof


def _apply_session(res, prof, cookie_manager):
    st.session_state.user_id = res.user.id
    st.session_state.email = res.user.email
    st.session_state.industry = prof["industry"]
    st.session_state.company = prof["company"]
    # Refresh token disimpan di cookie browser (30 hari) supaya refresh halaman
    # tidak memaksa login ulang — sesi dipulihkan lewat cookie ini di login_gate().
    cookie_manager.set(
        COOKIE_REFRESH_TOKEN,
        res.session.refresh_token,
        key="set_refresh_token",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )


def login_gate():
    """Menahan seluruh app sampai user login; mengisi industry/company dari tabel user_profiles."""
    if "user_id" in st.session_state:
        return

    sb = get_client()
    cookie_manager = get_cookie_manager()

    # Coba pulihkan sesi dari refresh token di cookie sebelum menampilkan form login.
    # Pembacaan cookie butuh satu roundtrip komponen, jadi nilainya bisa None dulu
    # lalu terisi di rerun berikutnya — gating-nya pakai nilai token itu sendiri
    # (bukan flag "sudah pernah coba") agar tidak melewatkan token yang baru tiba.
    if "user_id" not in st.session_state:
        refresh_token = cookie_manager.get(COOKIE_REFRESH_TOKEN)
        if refresh_token and st.session_state.get("cookie_restore_failed_token") != refresh_token:
            try:
                res = sb.auth.refresh_session(refresh_token)
                prof = _load_profile(sb, res.user.id)
                _apply_session(res, prof, cookie_manager)
                st.rerun()
            except Exception:
                st.session_state.cookie_restore_failed_token = refresh_token
                cookie_manager.delete(COOKIE_REFRESH_TOKEN, key="delete_refresh_token_invalid")

    st.subheader("Login")
    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Masuk")
    if submitted:
        # Tahap 1: cek email + password.
        try:
            res = sb.auth.sign_in_with_password({"email": email.strip(), "password": password})
        except Exception as exc:
            st.error("Login gagal: email/password salah, atau akun belum dikonfirmasi.")
            st.caption(f"Detail teknis: {exc}")
            st.stop()

        # Tahap 2: ambil industry/company dari user_profiles (pakai token user yang baru login).
        try:
            prof = _load_profile(sb, res.user.id)
        except Exception as exc:
            try:
                sb.auth.sign_out()
            except Exception:
                pass
            st.error("Email/password benar, tetapi profil perusahaan akun ini tidak bisa dibaca dari tabel user_profiles.")
            st.caption(f"Detail teknis: {exc}")
            st.stop()

        _apply_session(res, prof, cookie_manager)
        st.rerun()
    st.stop()


login_gate()

try:
    client = get_client()

    # _client diawali underscore = dikecualikan dari cache key. user_id SENGAJA
    # ikut jadi key supaya hasil cache tidak pernah terbagi antar user.
    @st.cache_data(ttl=300)
    def fetch_all(_client, table, user_id, page_size=1000):
        rows, start = [], 0
        while True:
            resp = _client.table(table).select("*").range(start, start + page_size - 1).execute()
            batch = resp.data or []
            rows.extend(batch)
            if len(batch) < page_size:
                break
            start += page_size
        return rows

    rows = fetch_all(client, SUPABASE_TABLE, st.session_state.user_id)
    if not rows:
        st.warning(f"Supabase table '{SUPABASE_TABLE}' returned no rows.")
        df = pd.DataFrame()
    else:
        df = pd.DataFrame(rows)
except Exception as exc:
    if "jwt" in str(exc).lower():
        # Token login kedaluwarsa: bersihkan sesi (termasuk cookie refresh token,
        # supaya tidak terus mencoba memulihkan sesi dari token basi) dan minta login ulang.
        get_cookie_manager().delete(COOKIE_REFRESH_TOKEN, key="delete_refresh_token_jwt_expired")
        st.session_state.clear()
        st.rerun()
    st.error(f"Failed to load data from Supabase: {exc}")
    st.stop()

if df.empty:
    st.info("No data available to display.")
else:
    # fixed locked filters in sidebar (disabled selectboxes)
    locked_industry = st.session_state.industry
    locked_company = st.session_state.company

    industry_col = find_col(df, ["industry", "Industry"]) if not df.empty else None
    app_col = find_col(df, ["app_name", "app name", "appName", "Competitor", "competitor"]) if not df.empty else None
    sentiment_col = find_col(df, ["Sentiment", "sentiment"]) if not df.empty else None
    # Platform reads from the "source" column specifically.
    platform_col = find_col(df, ["source", "Source", "Platform", "platform", "Review Source", "review_source"]) if not df.empty else None
    version_col = find_col(df, ["Version", "version", "app_version", "App Version"]) if not df.empty else None
    # "review_date" is forced ahead of the raw "date" column: both represent the
    # same timestamp, but "date" carries a UTC offset (2026-08-17T13:16:52+00:00)
    # that raises "Cannot compare tz-naive and tz-aware" when checked against
    # pd.Timestamp.now() for the Observation Period filter below, while
    # "review_date" is already a clean date-only value.
    date_col = (find_col(df, ["review_date"]) or find_col(df, ["Review Date", "date", "Date"])) if not df.empty else None

    # Scope Platform/Version options to the locked Industry+Company so the sidebar
    # never offers a value (e.g. a version number) that only exists for a different
    # company — picking one would otherwise silently filter to zero rows.
    scope_df = df
    if industry_col and app_col:
        scope_df = df[(df[industry_col].astype(str) == locked_industry) & (df[app_col].astype(str) == locked_company)]
    elif app_col:
        scope_df = df[df[app_col].astype(str) == locked_company]

    with st.sidebar:
        # Logo di bagian paling atas sidebar, selebar sidebar (100%). Latar putih agar logo
        # berteks hitam tetap terbaca di dark mode. Ubah "padding:0" bila ingin ada jarak
        # di sekeliling logo. Fallback ke teks bila hydralogo.png tidak ditemukan.
        if LOGO_DATA_URI:
            st.markdown(
                f'<div style="background:#ffffff; border-radius:6px; padding:0; width:100%; '
                f'box-sizing:border-box; overflow:hidden; margin-bottom:8px;">'
                f'<img src="{LOGO_DATA_URI}" alt="HYDRA" '
                f'style="width:100%; height:auto; display:block;"></div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown("### HYDRA")
        st.header("Filters")
        # The only user-manipulable filters: Observation Period, Platform, Version.
        observation_options = ["Last 7 days", "Last 30 days", "Last 90 days"]
        selected_observation = st.selectbox("Observation Period", observation_options, index=1)

        if platform_col:
            platform_options = ["All Platforms"] + sorted(scope_df[platform_col].dropna().astype(str).unique().tolist())
        else:
            platform_options = ["All Platforms"]
        selected_platform = st.selectbox("Platform", platform_options, index=0)

        if version_col:
            version_options = ["All Versions"] + sorted(scope_df[version_col].dropna().astype(str).unique().tolist())
        else:
            version_options = ["All Versions"]
        selected_version = st.selectbox("Version", version_options, index=0)

        # Info akun + Logout di bagian paling bawah sidebar.
        st.divider()
        st.caption(f"Login sebagai {st.session_state.email}")
        if st.button("Logout", use_container_width=True):
            get_client().auth.sign_out()
            get_cookie_manager().delete(COOKIE_REFRESH_TOKEN, key="delete_refresh_token_logout")
            st.session_state.clear()
            st.rerun()

    # apply the locked filters + Observation Period/Platform/Version to the displayed dataframe
    filtered_df = df.copy()
    try:
        if industry_col and app_col:
            filtered_df = filtered_df[(filtered_df[industry_col].astype(str) == locked_industry) & (filtered_df[app_col].astype(str) == locked_company)].copy()
        elif app_col:
            filtered_df = filtered_df[filtered_df[app_col].astype(str) == locked_company].copy()

        if platform_col and selected_platform != "All Platforms":
            filtered_df = filtered_df[filtered_df[platform_col].astype(str) == selected_platform].copy()

        if version_col and selected_version != "All Versions":
            filtered_df = filtered_df[filtered_df[version_col].astype(str) == selected_version].copy()

        if date_col:
            observation_days = {"Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90}.get(selected_observation, 30)
            parsed_dates = pd.to_datetime(filtered_df[date_col], errors="coerce")
            cutoff = pd.Timestamp.now() - pd.Timedelta(days=observation_days)
            filtered_df = filtered_df[parsed_dates >= cutoff].copy()
    except Exception:
        st.error("Gagal menerapkan filter data.")
        st.stop()

    # Print-only report header: invisible on screen (see .print-only in the
    # stylesheet above), rendered only when the page is printed/saved as PDF.
    # The logo sits on its own solid white chip so black logo text stays
    # legible no matter what's behind it — irrelevant for print (which is
    # always printed on a white page) but the same chip would also protect
    # the logo if it's ever reused somewhere on-screen in dark mode.
    logo_html = (
        f'<img src="{LOGO_DATA_URI}" alt="HYDRA" style="height:32px; display:block;">'
        if LOGO_DATA_URI
        else '<span style="font-weight:700; font-size:20px; color:#000000; font-family:Georgia, serif;">HYDRA</span>'
    )
    st.markdown(
        f"""
        <div class="print-only" style="margin-bottom:20px;">
          <div style="display:flex; align-items:center; gap:12px; border-bottom:2px solid #1B1E27; padding-bottom:10px; margin-bottom:8px;">
            <div style="background:#ffffff; border-radius:6px; padding:4px 10px; display:inline-block;">
              {logo_html}
            </div>
            <div style="font-size:13px; color:#333;">Product Feedback Dashboard &mdash; printed report</div>
          </div>
          <div style="font-size:12px; color:#444;">
            {locked_industry} / {locked_company} &nbsp;&middot;&nbsp; {selected_observation}
            &nbsp;&middot;&nbsp; Platform: {selected_platform} &nbsp;&middot;&nbsp; Version: {selected_version}
            &nbsp;&middot;&nbsp; Printed {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if filtered_df.empty:
        if scope_df.empty:
            st.warning("Tidak ada data untuk Industry/Company akun ini.")
            st.caption(
                f"Nilai di akun: Industry={locked_industry!r}, Company={locked_company!r}. "
                "Nilai ini harus sama persis (termasuk huruf besar/kecil dan spasi) dengan isi "
                "kolom industry dan app_name di tabel reviews."
            )
        else:
            st.warning(
                "Ada data untuk akun ini, tetapi tidak ada yang cocok dengan filter yang dipilih. "
                "Coba perpanjang Observation Period atau set Platform/Version ke All."
            )
    else:
        # detect remaining commonly used columns once (sentiment_col/platform_col/
        # version_col/date_col are already detected above, before the sidebar)
        category_col = find_col(df, ["Category", "category"])
        subcat_col_global = find_col(df, ["Subcategory", "subcategory"])

        # Rows whose Category or Subcategory is a taxonomy-mismatch placeholder
        # ("Hasil klasifikasi tidak cocok dengan taxonomy") aren't a real signal —
        # drop them everywhere, once, so every tab downstream is already clean.
        # A substring match (not exact-equality) also catches minor punctuation
        # variants of the same placeholder value.
        taxonomy_mismatch_pattern = "tidak cocok dengan taxonomy"
        if category_col:
            filtered_df = filtered_df[~filtered_df[category_col].astype(str).str.lower().str.contains(taxonomy_mismatch_pattern, na=False)]
        if subcat_col_global:
            filtered_df = filtered_df[~filtered_df[subcat_col_global].astype(str).str.lower().str.contains(taxonomy_mismatch_pattern, na=False)]

        def sentiment_breakdown(sub_df):
            total = int(sub_df.shape[0])
            if sentiment_col is None or total == 0:
                return {"total": total, "pos": 0, "neg": 0, "neu": 0, "pos_pct": 0.0, "neg_pct": 0.0, "neu_pct": 0.0, "net": 0.0}
            s = sub_df[sentiment_col].astype(str).str.lower()
            pos = int(s.eq("positive").sum())
            neg = int(s.eq("negative").sum())
            neu = int(s.eq("neutral").sum())
            net = round((pos - neg) / (pos + neg), 2) if (pos + neg) > 0 else 0.0
            return {
                "total": total, "pos": pos, "neg": neg, "neu": neu,
                "pos_pct": round(pos / total * 100, 1), "neg_pct": round(neg / total * 100, 1), "neu_pct": round(neu / total * 100, 1),
                "net": net,
            }

        def render_insight_row(lines):
            # One row, one column per insight line — the column count adapts to
            # however many lines a given tab has to say (3 for Overview, 4 for
            # Competitor Analysis, etc.) instead of stacking them in a single box.
            st.markdown("##### Insight Summary")
            cols = st.columns(len(lines))
            for col, line in zip(cols, lines):
                col.info(line)

        def go_to_tab(tab_name):
            # Buttons that jump straight to another tab: Streamlit forbids writing to
            # a widget's session_state key after that widget has already rendered in
            # the same run, so this stashes the target in a separate "pending_nav" key
            # and reruns — the pending value is applied to "main_nav" below, before
            # segmented_control is instantiated on the fresh run.
            st.session_state["pending_nav"] = tab_name
            st.rerun()

        if "pending_nav" in st.session_state:
            st.session_state["main_nav"] = st.session_state.pop("pending_nav")
        elif "main_nav" not in st.session_state:
            # Set the initial value through session_state (not segmented_control's
            # own `default`) — mixing both raises a Streamlit warning about the
            # widget having its value set both ways.
            st.session_state["main_nav"] = "Overview"

        # st.tabs resets to the first tab whenever a widget inside a tab (e.g. the
        # Competitor Analysis selectors) triggers a rerun. st.segmented_control is a
        # real widget backed by session_state (key="main_nav"), so its selection
        # survives reruns just like any other widget — and it renders as a pill bar
        # rather than a radio list.
        # Ordered for a smooth journey: health snapshot first (Overview, Sentiment
        # Overview), then the internal deep-dive from broad exploration down to
        # specific problems and requests (Feedback Explorer -> Issue Explorer ->
        # Request Intelligence), then the external/competitive lens last
        # (Competitor Analysis).
        nav_labels = [
            "Overview",
            "Sentiment Overview",
            "Feedback Explorer",
            "Issue Explorer",
            "Product Strengths",
            "Request Intelligence",
            "Competitor Analysis",
        ]
        selected_nav = st.segmented_control(
            "Dashboard section",
            nav_labels,
            key="main_nav",
            label_visibility="collapsed",
        )
        if not selected_nav:
            # segmented_control returns None if the user clicks the active segment
            # again (deselecting it) — fall back to Overview instead of a blank page.
            selected_nav = "Overview"

        with st.expander("What's in each tab?"):
            st.markdown(
                "- **Overview** — headline KPIs, sentiment trend, and category mix\n"
                "- **Sentiment Overview** — sentiment breakdown by category\n"
                "- **Feedback Explorer** — Category → Sentiment → Subcategory flow, word cloud, sample reviews\n"
                "- **Issue Explorer** — top negative-driving subcategories\n"
                "- **Product Strengths** — top positive-driving subcategories\n"
                "- **Request Intelligence** — user feature requests, ranked and trended\n"
                "- **Competitor Analysis** — side-by-side comparison against 2 chosen competitors"
            )

        # start tab overview #
        if selected_nav == "Overview":
            st.subheader("Overview")
            st.caption(
                "Snapshot of overall product health — KPIs, sentiment trend, and category mix for the "
                "feedback currently in view. Use the sidebar Observation Period, Platform, and Version filters "
                "to narrow every tab at once."
            )

            ov_stats = sentiment_breakdown(filtered_df)

            # First-half vs second-half split of the date range, used for both the KPI
            # deltas and the Momentum line below — mirrors the same two-bucket trend
            # approach used in Request Intelligence.
            total_delta, net_delta = None, None
            if date_col:
                ov_trend_src = filtered_df.copy()
                ov_trend_src[date_col] = pd.to_datetime(ov_trend_src[date_col], errors="coerce")
                ov_trend_src = ov_trend_src.dropna(subset=[date_col])
                if not ov_trend_src.empty:
                    median_date = ov_trend_src[date_col].median()
                    first_half_stats = sentiment_breakdown(ov_trend_src[ov_trend_src[date_col] < median_date])
                    second_half_stats = sentiment_breakdown(ov_trend_src[ov_trend_src[date_col] >= median_date])
                    total_delta = second_half_stats["total"] - first_half_stats["total"]
                    net_delta = round(second_half_stats["net"] - first_half_stats["net"], 2)

            oc1, oc2, oc3, oc4, oc5 = st.columns(5)
            oc1.metric("Total Reviews", ov_stats["total"], delta=total_delta)
            oc2.metric("Reputation Score", ov_stats["net"], delta=net_delta, help=REPUTATION_SCORE_HELP)
            oc3.metric("Positive %", f"{ov_stats['pos_pct']}%")
            oc4.metric("Negative %", f"{ov_stats['neg_pct']}%")
            oc5.metric("Neutral %", f"{ov_stats['neu_pct']}%")

            # ---- Insight Summary: same spot (right after KPIs, before the detailed
            # visuals) as every other tab, so it's where users already expect it. ----
            def interpret_net_sentiment_ov(score):
                if score >= 0.5:
                    return "strongly positive — the large majority of users are satisfied"
                if score >= 0.2:
                    return "positive but not yet dominant — leaning satisfied, with room to improve"
                if score >= -0.2:
                    return "mixed/split — satisfied and dissatisfied feedback are roughly balanced"
                return "negative — dissatisfaction dominates the feedback"

            insight_lines_ov = [
                (
                    f"**Overall:** Reputation Score is **{ov_stats['net']:.2f}** ({interpret_net_sentiment_ov(ov_stats['net'])}), "
                    f"from **{ov_stats['total']}** reviews — {ov_stats['pos_pct']:.1f}% positive, "
                    f"{ov_stats['neg_pct']:.1f}% negative, {ov_stats['neu_pct']:.1f}% neutral."
                )
            ]

            if net_delta is not None:
                if net_delta > 0.02:
                    momentum_meaning = "sentiment is improving over time — whatever changed recently is working"
                elif net_delta < -0.02:
                    momentum_meaning = "sentiment is declining over time — worth investigating before it compounds"
                else:
                    momentum_meaning = "sentiment is holding steady, no major shift"
                insight_lines_ov.append(
                    f"**Momentum:** Reputation Score moved **{net_delta:+.2f}** from the first half to the second half "
                    f"of the period in view — {momentum_meaning}."
                )

            top_cat = None
            if category_col and not filtered_df.empty:
                cat_series = filtered_df[category_col].dropna().astype(str)
                if not cat_series.empty:
                    cat_counts = cat_series.value_counts()
                    top_cat, top_cat_count = cat_counts.index[0], int(cat_counts.iloc[0])
                    insight_lines_ov.append(
                        f"**Most Discussed:** **{top_cat}** is the largest category (**{top_cat_count}** reviews) — "
                        f"see Feedback Explorer or Sentiment Overview to break it down further."
                    )

            render_insight_row(insight_lines_ov)

            if date_col and sentiment_col:
                trend_df = filtered_df.copy()
                trend_df[date_col] = pd.to_datetime(trend_df[date_col], errors="coerce")
                trend_df = trend_df.dropna(subset=[date_col])
                trend_df["sentiment_lower"] = trend_df[sentiment_col].astype(str).str.lower()
                if trend_df.empty:
                    st.info("No dated rows available to build a sentiment trend.")
                else:
                    # Raw daily counts split three ways (positive/negative/neutral) are too
                    # sparse to read as a trend — with ~10 reviews/day, the neutral line alone
                    # mostly bounces between 0 and 2. Weekly buckets (same approach as the
                    # Competitor Analysis trend chart) smooth that out into a readable trend.
                    trend_df["period"] = trend_df[date_col].dt.to_period("W").dt.start_time
                    trend_counts = trend_df.groupby(["period", "sentiment_lower"]).size().unstack(fill_value=0)
                    for lbl in ["positive", "negative", "neutral"]:
                        if lbl not in trend_counts.columns:
                            trend_counts[lbl] = 0
                    trend_counts = trend_counts[["positive", "negative", "neutral"]].reset_index()
                    trend_counts = trend_counts.rename(columns={"period": "date"})
                    trend_melt = trend_counts.melt(id_vars="date", value_vars=["positive", "negative", "neutral"], var_name="sentiment", value_name="count")
                    trend_colors = {"positive": "#2ecc71", "negative": "#e74c3c", "neutral": "#f1c40f"}
                    trend_chart = (
                        alt.Chart(trend_melt)
                        .mark_line(point=True, interpolate="monotone", strokeWidth=3)
                        .encode(
                            x=alt.X("date:T", title="Week"),
                            y=alt.Y("count:Q", title="Review Count"),
                            color=alt.Color("sentiment:N", scale=alt.Scale(domain=list(trend_colors.keys()), range=list(trend_colors.values()))),
                        )
                        .interactive()
                    )
                    st.altair_chart(trend_chart, use_container_width=True)
            else:
                st.info("No date/sentiment column found to build a trend chart.")

            st.markdown("---")
            st.markdown("##### Not sure where to look next?")
            nav_cta_cols = st.columns(3)

            fe_label = f"See what people are saying about {top_cat} →" if top_cat else "See what people are saying →"
            if nav_cta_cols[0].button(fe_label, key="ov_cta_fe", use_container_width=True):
                go_to_tab("Feedback Explorer")

            if net_delta is not None and net_delta < -0.02:
                ie_label = "Find out what's pulling sentiment down →"
            else:
                ie_label = "See the biggest complaints, ranked →"
            if nav_cta_cols[1].button(ie_label, key="ov_cta_ie", use_container_width=True):
                go_to_tab("Issue Explorer")

            if nav_cta_cols[2].button("Compare how each category is doing →", key="ov_cta_so", use_container_width=True):
                go_to_tab("Sentiment Overview")

        # start tab feedback explorer #
        if selected_nav == "Feedback Explorer":
            st.subheader("Feedback Explorer")
            st.caption(
                "Trace how feedback flows from Category to Sentiment to Subcategory, then dig into the "
                "word cloud and sample reviews. Pick a Category and Sentiment below to narrow everything on this page."
            )
            fcols = st.columns(2)
            exp_df = filtered_df.copy()

            if category_col:
                fe_category_options = ["All Categories"] + sorted(filtered_df[category_col].dropna().astype(str).unique().tolist())
                sel_category = fcols[0].selectbox("Category", fe_category_options, key="fe_category")
                if sel_category != "All Categories":
                    exp_df = exp_df[exp_df[category_col].astype(str) == sel_category]

            # Replace the second combo: use Sentiment selector instead of Subcategory
            if sentiment_col:
                fe_sentiment_options = ["All Sentiments"] + sorted(filtered_df[sentiment_col].dropna().astype(str).unique().tolist())
            else:
                # fallback options when a sentiment column isn't present
                fe_sentiment_options = ["All Sentiments", "Positive", "Negative", "Neutral"]
            sel_sentiment = fcols[1].selectbox("Sentiment", fe_sentiment_options, key="fe_sentiment")
            if sel_sentiment != "All Sentiments" and sentiment_col:
                exp_df = exp_df[exp_df[sentiment_col].astype(str) == sel_sentiment]

            exp_csv = exp_df.to_csv(index=False)

            # Quick orientation numbers for the same rows the table above shows.
            fe_stats = sentiment_breakdown(exp_df)
            kc1, kc2, kc3, kc4, kc5 = st.columns(5)
            kc1.metric("Total Feedback", fe_stats["total"])
            kc2.metric("Reputation Score", fe_stats["net"], help=REPUTATION_SCORE_HELP)
            kc3.metric("Positive %", f"{fe_stats['pos_pct']}%")
            kc4.metric("Negative %", f"{fe_stats['neg_pct']}%")
            kc5.metric("Neutral %", f"{fe_stats['neu_pct']}%")

            # Insight Summary sits right after the KPI row and before the detailed
            # visuals below — same spot as Competitor Analysis and Request Intelligence,
            # so it's where users already expect to find the narrative read. The four
            # lines also walk the same direction as the Sankey underneath it (Category
            # -> Sentiment -> Subcategory): general picture down to the one most
            # actionable driver.
            def interpret_net_sentiment_fe(score):
                if score >= 0.5:
                    return "strongly positive — the large majority of this feedback is favorable"
                if score >= 0.2:
                    return "positive but not yet dominant — leaning satisfied, with room to improve"
                if score >= -0.2:
                    return "mixed/split — satisfied and dissatisfied feedback are roughly balanced"
                return "negative — dissatisfaction dominates this slice of feedback"

            insight_lines_fe = [
                (
                    f"**Overall:** Reputation Score is **{fe_stats['net']:.2f}** ({interpret_net_sentiment_fe(fe_stats['net'])}), "
                    f"from **{fe_stats['total']}** feedback items — {fe_stats['pos_pct']:.1f}% positive, "
                    f"{fe_stats['neg_pct']:.1f}% negative, {fe_stats['neu_pct']:.1f}% neutral."
                )
            ]

            if category_col and not exp_df.empty:
                cat_series = exp_df[category_col].dropna().astype(str)
                if not cat_series.empty:
                    cat_counts = cat_series.value_counts()
                    top_cat, top_cat_count = cat_counts.index[0], int(cat_counts.iloc[0])
                    top_cat_share = round(top_cat_count / len(exp_df) * 100, 1)
                    insight_lines_fe.append(
                        f"**Dominant Category:** **{top_cat}** accounts for **{top_cat_count}/{len(exp_df)}** feedback items "
                        f"({top_cat_share}%) — the area generating the most conversation right now."
                    )

                    if sentiment_col:
                        cat_nets = {}
                        for cat_val, sub in exp_df.groupby(category_col):
                            s = sub[sentiment_col].astype(str).str.lower()
                            pos, neg = int(s.eq("positive").sum()), int(s.eq("negative").sum())
                            if pos + neg > 0:
                                cat_nets[str(cat_val)] = round((pos - neg) / (pos + neg), 2)
                        if cat_nets:
                            weakest_cat = min(cat_nets, key=cat_nets.get)
                            insight_lines_fe.append(
                                f"**Sentiment Concentration:** **{weakest_cat}** has the lowest Reputation Score among categories "
                                f"in view (**{cat_nets[weakest_cat]:.2f}**) — this is where dissatisfaction concentrates most."
                            )

            if subcat_col_global and sentiment_col and not exp_df.empty:
                neg_subcats = exp_df[exp_df[sentiment_col].astype(str).str.lower() == "negative"][subcat_col_global].dropna().astype(str)
                if not neg_subcats.empty:
                    neg_counts = neg_subcats.value_counts()
                    driver, driver_count = neg_counts.index[0], int(neg_counts.iloc[0])
                    insight_lines_fe.append(
                        f"**Top Negative Driver:** **{driver}** is the single largest contributor to negative feedback "
                        f"(**{driver_count}** mentions) — the most specific, actionable starting point."
                    )
                else:
                    insight_lines_fe.append("**Top Negative Driver:** None — no negative feedback in the current filter.")

            render_insight_row(insight_lines_fe)

            if category_col and subcat_col_global and sentiment_col and not exp_df.empty:
                sankey_src = exp_df[[category_col, subcat_col_global, sentiment_col]].dropna().copy()
                sankey_src[category_col] = sankey_src[category_col].astype(str)
                sankey_src[subcat_col_global] = sankey_src[subcat_col_global].astype(str)
                sankey_src[sentiment_col] = sankey_src[sentiment_col].astype(str).str.title()

                if sankey_src.empty:
                    st.info("No rows available to build the feedback flow diagram.")
                else:
                    sentiment_order = ["Positive", "Negative", "Neutral"]
                    sankey_src = sankey_src[sankey_src[sentiment_col].isin(sentiment_order)]

                    cat_to_sent = sankey_src.groupby([category_col, sentiment_col]).size().reset_index(name="count")
                    sent_to_subcat = sankey_src.groupby([sentiment_col, subcat_col_global]).size().reset_index(name="count")

                    cat_nodes = [("cat", c) for c in sorted(sankey_src[category_col].unique().tolist())]
                    sent_nodes = [("sent", s) for s in sentiment_order if s in sankey_src[sentiment_col].unique()]
                    sub_nodes = [("sub", s) for s in sorted(sankey_src[subcat_col_global].unique().tolist())]
                    # tuple keys (not raw labels) avoid collisions if a category and a
                    # subcategory ever happen to share the same display name
                    node_index = {key: i for i, key in enumerate(cat_nodes + sent_nodes + sub_nodes)}
                    node_labels = [label for (_, label) in cat_nodes + sent_nodes + sub_nodes]

                    sentiment_colors = {"Positive": "#2ecc71", "Negative": "#e74c3c", "Neutral": "#f1c40f"}
                    node_colors = (
                        ["#4e79a7"] * len(cat_nodes)
                        + [sentiment_colors.get(label, "#999999") for (_, label) in sent_nodes]
                        + ["#9c6ade"] * len(sub_nodes)
                    )

                    sources, targets, values = [], [], []
                    for _, row in cat_to_sent.iterrows():
                        sources.append(node_index[("cat", row[category_col])])
                        targets.append(node_index[("sent", row[sentiment_col])])
                        values.append(int(row["count"]))
                    for _, row in sent_to_subcat.iterrows():
                        sources.append(node_index[("sent", row[sentiment_col])])
                        targets.append(node_index[("sub", row[subcat_col_global])])
                        values.append(int(row["count"]))

                    sankey_fig = go.Figure(
                        data=[
                            go.Sankey(
                                node=dict(label=node_labels, pad=12, thickness=14, color=node_colors),
                                link=dict(source=sources, target=targets, value=values),
                            )
                        ]
                    )
                    sankey_fig.update_layout(height=450, margin=dict(l=10, r=10, t=10, b=10))
                    st.plotly_chart(sankey_fig, use_container_width=True)
            else:
                st.info("Category/Subcategory/Sentiment columns not found to build the feedback flow diagram.")

            # Word cloud (left) + verbatim samples (right), both scoped to the same
            # filtered rows as the table and Sankey above.
            text_col_fe = find_col(exp_df, ["normalized_text"]) or find_col(exp_df, ["text", "review_text"])
            wc_col, samples_col = st.columns([1, 1])

            with wc_col:
                st.markdown("##### Word Cloud")
                if text_col_fe and not exp_df.empty:
                    combined_text = " ".join(exp_df[text_col_fe].dropna().astype(str).tolist()).strip()
                    if combined_text:
                        # Indonesian function words + generic filler terms that would
                        # otherwise dominate purely on frequency without carrying meaning.
                        stopwords_id = {
                            "yang", "dan", "di", "ke", "dari", "untuk", "ini", "itu", "dengan", "pada",
                            "adalah", "akan", "atau", "juga", "sudah", "saya", "tidak", "ada", "bisa",
                            "jadi", "saja", "agar", "karena", "sangat", "banyak", "masih", "saat", "ya",
                            "nya", "kalau", "kalo", "aja", "gak", "ga", "tdk", "kok", "sih", "deh", "dong",
                            "lah", "kan", "apa", "apakah", "semua", "dalam", "oleh", "tapi", "tetapi",
                            "namun", "jika", "kami", "kita", "anda", "mereka", "dia", "ia", "para",
                            "sebagai", "yaitu", "yakni", "hingga", "sampai", "serta", "maupun", "supaya",
                            "tersebut", "tsb", "yg", "dr", "utk", "dgn", "spy", "tp", "krn", "udh", "udah",
                            "dah", "lg", "lagi", "begitu", "gitu", "banget", "bgt", "min", "mimin",
                            "admin", "aplikasi", "apk", "app",
                        }
                        try:
                            wc = WordCloud(
                                width=600,
                                height=400,
                                background_color="white",
                                stopwords=stopwords_id,
                                collocations=False,
                            ).generate(combined_text)
                            st.image(wc.to_array(), use_container_width=True)
                        except ValueError:
                            st.info("Not enough distinct words to build a word cloud.")
                    else:
                        st.info("No text available to build a word cloud.")
                else:
                    st.info("No text column found to build a word cloud.")

            with samples_col:
                st.markdown("##### Sample Reviews")
                if text_col_fe and not exp_df.empty:
                    fe_samples = exp_df[text_col_fe].dropna().astype(str)
                    fe_samples = fe_samples[fe_samples.str.strip() != ""].head(5)
                    if fe_samples.empty:
                        st.caption("No sample reviews available for the current filters.")
                    else:
                        st.markdown("\n".join(f"{i}. {txt}" for i, txt in enumerate(fe_samples, start=1)))
                else:
                    st.info("No text column found to build sample reviews.")

        # start tab issue explorer #
        if selected_nav == "Issue Explorer":
            st.subheader("Issue Explorer")
            st.caption(
                "Ranked view of the subcategories driving negative feedback, with trend and sample reviews. "
                "Pick a Category to narrow the ranking, then select an Issue below to read real examples."
            )

            if category_col:
                ie_category_options = ["All Categories"] + sorted(filtered_df[category_col].dropna().astype(str).unique().tolist())
                sel_ie_category = st.selectbox("Category", ie_category_options, key="ie_category")
                ie_df = filtered_df if sel_ie_category == "All Categories" else filtered_df[filtered_df[category_col].astype(str) == sel_ie_category]
            else:
                ie_df = filtered_df

            if subcat_col_global and sentiment_col:
                neg_df = ie_df[ie_df[sentiment_col].astype(str).str.lower() == "negative"].copy()
                if neg_df.empty:
                    st.info("No negative-sentiment rows available to identify issues.")
                else:
                    issue_counts = neg_df[subcat_col_global].dropna().astype(str).value_counts().reset_index()
                    issue_counts.columns = ["subcategory", "count"]
                    issue_counts = issue_counts.sort_values("count", ascending=False).reset_index(drop=True)

                    # Trend: same two-bucket (first half vs second half of the date range)
                    # signal used in Request Intelligence — per-issue volume is too sparse
                    # for a readable weekly time series.
                    if date_col:
                        ie_trend_src = neg_df.copy()
                        ie_trend_src[date_col] = pd.to_datetime(ie_trend_src[date_col], errors="coerce")
                        ie_trend_src = ie_trend_src.dropna(subset=[date_col])
                    else:
                        ie_trend_src = pd.DataFrame()

                    def compute_trend_ie(subcat_name):
                        if ie_trend_src.empty:
                            return "—"
                        sub = ie_trend_src[ie_trend_src[subcat_col_global].astype(str) == subcat_name]
                        if sub.empty:
                            return "—"
                        median_date = ie_trend_src[date_col].median()
                        first_half = int((sub[date_col] < median_date).sum())
                        second_half = int((sub[date_col] >= median_date).sum())
                        if first_half == 0 and second_half > 0:
                            return "🆕 New"
                        if second_half > first_half:
                            return "▲ Rising"
                        if second_half < first_half:
                            return "▼ Declining"
                        return "▬ Stable"

                    issue_counts["Trend"] = issue_counts["subcategory"].apply(compute_trend_ie)

                    total_feedback_ie = int(ie_df.shape[0])
                    issue_counts["Share of Feedback"] = (issue_counts["count"] / total_feedback_ie * 100).round(2) if total_feedback_ie else 0.0

                    total_issues = int(issue_counts["count"].sum())
                    unique_issue_types = int(issue_counts["subcategory"].nunique())
                    issue_share_pct = round(total_issues / total_feedback_ie * 100, 2) if total_feedback_ie else 0.0

                    k1, k2, k3 = st.columns(3)
                    k1.metric("Total Issues", total_issues)
                    k2.metric("Unique Issue Types", unique_issue_types)
                    k3.metric("Share of Feedback", f"{issue_share_pct:.2f}%")

                    # ---- Insight Summary ----
                    overall_line_ie = (
                        f"**Overall:** **{total_issues}** negative mentions across **{unique_issue_types}** distinct issues "
                        f"make up **{issue_share_pct:.2f}%** of feedback in view."
                    )

                    top_count_ie = int(issue_counts["count"].max())
                    top_issues_list = issue_counts[issue_counts["count"] == top_count_ie]["subcategory"].tolist()
                    if len(top_issues_list) == 1:
                        top_line_ie = (
                            f"**Top Issue:** \"{top_issues_list[0]}\" leads with **{top_count_ie}** negative mentions "
                            f"— the single clearest place to start."
                        )
                    else:
                        top_line_ie = (
                            f"**Top Issues:** **{len(top_issues_list)}** issues are tied at **{top_count_ie}** mentions each "
                            f"({', '.join(top_issues_list)}) — no single issue dominates."
                        )

                    trend_counts_ie = issue_counts["Trend"].value_counts()
                    rising_n_ie = int(trend_counts_ie.get("▲ Rising", 0)) + int(trend_counts_ie.get("🆕 New", 0))
                    declining_n_ie = int(trend_counts_ie.get("▼ Declining", 0))
                    if rising_n_ie > declining_n_ie:
                        momentum_meaning_ie = "more issues are worsening than improving — dissatisfaction is broadening, not just persisting"
                    elif declining_n_ie > rising_n_ie:
                        momentum_meaning_ie = "more issues are easing than worsening — recent fixes may already be paying off"
                    else:
                        momentum_meaning_ie = "issues are holding steady, no clear overall shift"
                    momentum_line_ie = (
                        f"**Momentum:** **{rising_n_ie}** issue(s) rising/new, **{declining_n_ie}** declining "
                        f"— {momentum_meaning_ie}."
                    )

                    render_insight_row([overall_line_ie, top_line_ie, momentum_line_ie])

                    # Ranking chart — labelLimit keeps long subcategory names from being cut
                    # off, and the text layer prints the exact count on each bar.
                    top_n_ie = issue_counts.head(10)
                    issue_bars = (
                        alt.Chart(top_n_ie)
                        .mark_bar(color="#d62728")
                        .encode(
                            x=alt.X("count:Q", title="Negative Reviews", axis=alt.Axis(format="d")),
                            y=alt.Y("subcategory:N", sort="-x", title=None, axis=alt.Axis(labelLimit=500)),
                        )
                    )
                    issue_labels = (
                        alt.Chart(top_n_ie)
                        .mark_text(align="left", dx=4, color="#333")
                        .encode(
                            x=alt.X("count:Q"),
                            y=alt.Y("subcategory:N", sort="-x"),
                            text=alt.Text("count:Q", format="d"),
                        )
                    )
                    issue_chart = (issue_bars + issue_labels).properties(height=35 * (len(top_n_ie) + 1) + 3, title="Top 10 Issues")
                    st.altair_chart(issue_chart, use_container_width=True)

                    st.dataframe(
                        issue_counts.rename(columns={"subcategory": "Issue", "count": "Count"})[["Issue", "Count", "Share of Feedback", "Trend"]],
                        use_container_width=True,
                        hide_index=True,
                    )

                    # Sample reviews for a chosen issue, sourced from normalized_text.
                    st.markdown("##### Sample Reviews")
                    text_col_ie = find_col(ie_df, ["normalized_text"]) or find_col(ie_df, ["text", "review_text"])
                    if text_col_ie:
                        selected_issue = st.selectbox("Issue", issue_counts["subcategory"].tolist(), key="ie_selected_issue")
                        quote_sub_ie = neg_df[neg_df[subcat_col_global].astype(str) == selected_issue]
                        samples_ie = quote_sub_ie[text_col_ie].dropna().astype(str)
                        samples_ie = samples_ie[samples_ie.str.strip() != ""].head(5)
                        if samples_ie.empty:
                            st.caption("No sample reviews available for this issue.")
                        else:
                            st.markdown("\n".join(f"{i}. {txt}" for i, txt in enumerate(samples_ie, start=1)))
                    else:
                        st.info("No text column found to build sample reviews.")
            else:
                st.info("Subcategory/Sentiment columns not found to build Issue Explorer.")

        # start tab product strengths #
        if selected_nav == "Product Strengths":
            st.subheader("Product Strengths")
            st.caption(
                "Ranked view of the subcategories driving positive feedback, with trend and sample reviews. "
                "Pick a Category to narrow the ranking, then select a Strength below to read real examples."
            )

            if category_col:
                ps_category_options = ["All Categories"] + sorted(filtered_df[category_col].dropna().astype(str).unique().tolist())
                sel_ps_category = st.selectbox("Category", ps_category_options, key="ps_category")
                ps_df = filtered_df if sel_ps_category == "All Categories" else filtered_df[filtered_df[category_col].astype(str) == sel_ps_category]
            else:
                ps_df = filtered_df

            if subcat_col_global and sentiment_col:
                pos_df = ps_df[ps_df[sentiment_col].astype(str).str.lower() == "positive"].copy()
                if pos_df.empty:
                    st.info("No positive-sentiment rows available to identify strengths.")
                else:
                    strength_counts = pos_df[subcat_col_global].dropna().astype(str).value_counts().reset_index()
                    strength_counts.columns = ["subcategory", "count"]
                    strength_counts = strength_counts.sort_values("count", ascending=False).reset_index(drop=True)

                    # Trend: same two-bucket (first half vs second half of the date range)
                    # signal used in Request Intelligence — per-strength volume is too sparse
                    # for a readable weekly time series.
                    if date_col:
                        ps_trend_src = pos_df.copy()
                        ps_trend_src[date_col] = pd.to_datetime(ps_trend_src[date_col], errors="coerce")
                        ps_trend_src = ps_trend_src.dropna(subset=[date_col])
                    else:
                        ps_trend_src = pd.DataFrame()

                    def compute_trend_ps(subcat_name):
                        if ps_trend_src.empty:
                            return "—"
                        sub = ps_trend_src[ps_trend_src[subcat_col_global].astype(str) == subcat_name]
                        if sub.empty:
                            return "—"
                        median_date = ps_trend_src[date_col].median()
                        first_half = int((sub[date_col] < median_date).sum())
                        second_half = int((sub[date_col] >= median_date).sum())
                        if first_half == 0 and second_half > 0:
                            return "🆕 New"
                        if second_half > first_half:
                            return "▲ Rising"
                        if second_half < first_half:
                            return "▼ Declining"
                        return "▬ Stable"

                    strength_counts["Trend"] = strength_counts["subcategory"].apply(compute_trend_ps)

                    total_feedback_ps = int(ps_df.shape[0])
                    strength_counts["Share of Feedback"] = (strength_counts["count"] / total_feedback_ps * 100).round(2) if total_feedback_ps else 0.0

                    total_strengths = int(strength_counts["count"].sum())
                    unique_strength_types = int(strength_counts["subcategory"].nunique())
                    strength_share_pct = round(total_strengths / total_feedback_ps * 100, 2) if total_feedback_ps else 0.0

                    k1, k2, k3 = st.columns(3)
                    k1.metric("Total Strengths", total_strengths)
                    k2.metric("Unique Strength Types", unique_strength_types)
                    k3.metric("Share of Feedback", f"{strength_share_pct:.2f}%")

                    # ---- Insight Summary ----
                    overall_line_ps = (
                        f"**Overall:** **{total_strengths}** positive mentions across **{unique_strength_types}** distinct strengths "
                        f"make up **{strength_share_pct:.2f}%** of feedback in view."
                    )

                    top_count_ps = int(strength_counts["count"].max())
                    top_strengths_list = strength_counts[strength_counts["count"] == top_count_ps]["subcategory"].tolist()
                    if len(top_strengths_list) == 1:
                        top_line_ps = (
                            f"**Top Strength:** \"{top_strengths_list[0]}\" leads with **{top_count_ps}** positive mentions "
                            f"— the single clearest thing to keep doing."
                        )
                    else:
                        top_line_ps = (
                            f"**Top Strengths:** **{len(top_strengths_list)}** strengths are tied at **{top_count_ps}** mentions each "
                            f"({', '.join(top_strengths_list)}) — no single strength dominates."
                        )

                    trend_counts_ps = strength_counts["Trend"].value_counts()
                    rising_n_ps = int(trend_counts_ps.get("▲ Rising", 0)) + int(trend_counts_ps.get("🆕 New", 0))
                    declining_n_ps = int(trend_counts_ps.get("▼ Declining", 0))
                    if rising_n_ps > declining_n_ps:
                        momentum_meaning_ps = "more strengths are growing than fading — satisfaction is broadening, not just holding"
                    elif declining_n_ps > rising_n_ps:
                        momentum_meaning_ps = "more strengths are fading than growing — worth checking what changed recently"
                    else:
                        momentum_meaning_ps = "strengths are holding steady, no clear overall shift"
                    momentum_line_ps = (
                        f"**Momentum:** **{rising_n_ps}** strength(s) rising/new, **{declining_n_ps}** declining "
                        f"— {momentum_meaning_ps}."
                    )

                    render_insight_row([overall_line_ps, top_line_ps, momentum_line_ps])

                    # Ranking chart — labelLimit keeps long subcategory names from being cut
                    # off, and the text layer prints the exact count on each bar.
                    top_n_ps = strength_counts.head(10)
                    strength_bars = (
                        alt.Chart(top_n_ps)
                        .mark_bar(color="#2ecc71")
                        .encode(
                            x=alt.X("count:Q", title="Positive Reviews", axis=alt.Axis(format="d")),
                            y=alt.Y("subcategory:N", sort="-x", title=None, axis=alt.Axis(labelLimit=500)),
                        )
                    )
                    strength_labels = (
                        alt.Chart(top_n_ps)
                        .mark_text(align="left", dx=4, color="#333")
                        .encode(
                            x=alt.X("count:Q"),
                            y=alt.Y("subcategory:N", sort="-x"),
                            text=alt.Text("count:Q", format="d"),
                        )
                    )
                    strength_chart = (strength_bars + strength_labels).properties(height=35 * (len(top_n_ps) + 1) + 3, title="Top 10 Strengths")
                    st.altair_chart(strength_chart, use_container_width=True)

                    st.dataframe(
                        strength_counts.rename(columns={"subcategory": "Strength", "count": "Count"})[["Strength", "Count", "Share of Feedback", "Trend"]],
                        use_container_width=True,
                        hide_index=True,
                    )

                    # Sample reviews for a chosen strength, sourced from normalized_text.
                    st.markdown("##### Sample Reviews")
                    text_col_ps = find_col(ps_df, ["normalized_text"]) or find_col(ps_df, ["text", "review_text"])
                    if text_col_ps:
                        selected_strength = st.selectbox("Strength", strength_counts["subcategory"].tolist(), key="ps_selected_strength")
                        quote_sub_ps = pos_df[pos_df[subcat_col_global].astype(str) == selected_strength]
                        samples_ps = quote_sub_ps[text_col_ps].dropna().astype(str)
                        samples_ps = samples_ps[samples_ps.str.strip() != ""].head(5)
                        if samples_ps.empty:
                            st.caption("No sample reviews available for this strength.")
                        else:
                            st.markdown("\n".join(f"{i}. {txt}" for i, txt in enumerate(samples_ps, start=1)))
                    else:
                        st.info("No text column found to build sample reviews.")
            else:
                st.info("Subcategory/Sentiment columns not found to build Product Strengths.")

        # start tab sentiment overview #
        if selected_nav == "Sentiment Overview":
            st.subheader("Sentiment Overview")
            st.caption(
                "Sentiment distribution and a per-category breakdown for the feedback currently in view — "
                "use this to see which categories are net-positive or net-negative."
            )
            se_stats = sentiment_breakdown(filtered_df)
            sc1, sc2, sc3, sc4 = st.columns(4)
            sc1.metric("Positive", se_stats["pos"])
            sc2.metric("Negative", se_stats["neg"])
            sc3.metric("Neutral", se_stats["neu"])
            sc4.metric("Reputation Score", se_stats["net"], help=REPUTATION_SCORE_HELP)

            dist_df = pd.DataFrame(
                [
                    {"sentiment": "Positive", "count": se_stats["pos"]},
                    {"sentiment": "Negative", "count": se_stats["neg"]},
                    {"sentiment": "Neutral", "count": se_stats["neu"]},
                ]
            )
            dist_colors = {"Positive": "#2ecc71", "Negative": "#e74c3c", "Neutral": "#f1c40f"}
            dist_chart = (
                alt.Chart(dist_df)
                .mark_bar()
                .encode(
                    x=alt.X("count:Q", title="Reviews"),
                    y=alt.Y("sentiment:N", title=""),
                    color=alt.Color("sentiment:N", scale=alt.Scale(domain=list(dist_colors.keys()), range=list(dist_colors.values())), legend=None),
                )
                .properties(height=200)
            )
            st.altair_chart(dist_chart, use_container_width=True)

            if category_col and sentiment_col:
                cross = pd.crosstab(filtered_df[category_col], filtered_df[sentiment_col].astype(str).str.lower())
                for lbl in ["positive", "negative", "neutral"]:
                    if lbl not in cross.columns:
                        cross[lbl] = 0
                cross = cross[["positive", "negative", "neutral"]]
                cross["total"] = cross.sum(axis=1)
                cross["net_sentiment"] = ((cross["positive"] - cross["negative"]) / cross["total"]).round(2).replace([float("inf"), float("-inf")], 0)
                cross = cross.reset_index().rename(columns={category_col: "Category"})
                st.dataframe(cross, use_container_width=True, hide_index=True)
            else:
                st.info("No category column found to build a per-category sentiment breakdown.")

        # start tab competitor analysis #
        if selected_nav == "Competitor Analysis":
            st.subheader("Competitor Analysis")
            st.caption(
                "Head-to-head comparison against two competitors in the same industry. Choose two apps below "
                "to see KPIs, category deltas, sentiment trend, and sample reviews."
            )

            # Top row: three combo boxes (first locked to our app, other two pick competitors
            # within the same locked industry).
            st.markdown('<div class="competitive-columns">', unsafe_allow_html=True)
            left, right = st.columns([3, 1])

            # prepare app options filtered by the locked industry if available
            apps_in_industry = []
            if industry_col and app_col:
                try:
                    # Use the full dataframe (`df`) filtered by the sidebar-locked industry
                    apps_in_industry = sorted(df[df[industry_col].astype(str) == locked_industry][app_col].dropna().astype(str).unique().tolist())
                except Exception:
                    apps_in_industry = sorted(df[app_col].dropna().astype(str).unique().tolist())
            elif app_col:
                apps_in_industry = sorted(df[app_col].dropna().astype(str).unique().tolist())
            else:
                apps_in_industry = [locked_company]

            with left:
                # options exclude the sidebar-selected app (locked_company)
                apps_other = [a for a in apps_in_industry if a != locked_company]

                c1, c2, c3 = st.columns([1, 1, 1])
                c1.selectbox("App", [locked_company], index=0, disabled=True, key="comp_app1")
                comp_app_2 = c2.selectbox("Compare App 1", ["None"] + apps_other, index=0, key="comp_app2")
                comp_app_3 = c3.selectbox("Compare App 2", ["None"] + apps_other, index=0, key="comp_app3")

            st.markdown('</div>', unsafe_allow_html=True)

            if not (comp_app_2 and comp_app_3 and comp_app_2 != "None" and comp_app_3 != "None"):
                st.info(f"Choose two compare apps to view comparison. Selected: Compare1={comp_app_2}, Compare2={comp_app_3}")
            else:
                apps_compare = [locked_company, comp_app_2, comp_app_3]
                comp_apps = [comp_app_2, comp_app_3]
                our = locked_company

                # base dataframe filtered by industry (locked)
                base_df = df.copy()
                if industry_col:
                    base_df = base_df[base_df[industry_col].astype(str) == locked_industry]

                sentiment_col_any = find_col(df, ["Sentiment", "sentiment"]) if not df.empty else None
                category_col_any = find_col(df, ["Category", "category"]) if not df.empty else None
                rating_col_any = find_col(df, ["score", "Score", "Rating", "rating"]) if not df.empty else None
                date_col_any = find_col(df, ["review_date", "Review Date", "date", "Date"]) if not df.empty else None
                # normalized_text is preferred for the review-quote explorer
                text_col_any = find_col(df, ["normalized_text"]) or find_col(df, ["text", "review_text"]) if not df.empty else None

                # ---- KPI calculations for the three apps ----
                def compute_stats(app):
                    sub = base_df[base_df[app_col].astype(str) == app] if app_col else base_df
                    total = int(sub.shape[0])
                    if sentiment_col_any is not None and total > 0:
                        s = sub[sentiment_col_any].astype(str).str.lower()
                        pos, neg, neu = int(s.eq("positive").sum()), int(s.eq("negative").sum()), int(s.eq("neutral").sum())
                    else:
                        pos = neg = neu = 0
                    pos_pct = round(pos / total * 100, 1) if total > 0 else 0.0
                    neg_pct = round(neg / total * 100, 1) if total > 0 else 0.0
                    neu_pct = round(neu / total * 100, 1) if total > 0 else 0.0
                    net = round((pos - neg) / (pos + neg), 2) if (pos + neg) > 0 else 0.0
                    rating = None
                    if rating_col_any and total > 0:
                        avg_rating = pd.to_numeric(sub[rating_col_any], errors="coerce").mean()
                        rating = round(avg_rating, 2) if pd.notna(avg_rating) else None
                    return {"total": total, "pos": pos, "neg": neg, "neu": neu, "pos_pct": pos_pct, "neg_pct": neg_pct, "neu_pct": neu_pct, "net": net, "rating": rating}

                stats_map = {app: compute_stats(app) for app in apps_compare}

                # Rankings for Positive (higher better), Negative (lower better), Neutral (higher better), Net (higher better)
                def rank_value(metric_key, reverse=True):
                    vals = [(app, stats_map[app][metric_key]) for app in apps_compare]
                    vals_sorted = sorted(vals, key=lambda x: x[1], reverse=reverse)
                    ranks = {app: i + 1 for i, (app, _) in enumerate(vals_sorted)}
                    return ranks

                pos_ranks = rank_value("pos_pct", reverse=True)
                neg_ranks = rank_value("neg_pct", reverse=False)
                neu_ranks = rank_value("neu_pct", reverse=True)
                net_ranks = rank_value("net", reverse=True)

                our_stats = stats_map.get(our, {})

                # Render KPI cards (7) using st.metric, consistent with the other tabs' KPI rows
                kcols = st.columns(7)
                kcols[0].metric("Num. of Feedbacks", our_stats.get("total", 0))
                rating_main = our_stats.get("rating")
                kcols[1].metric("Avg. Rating", f"{rating_main:.2f}" if rating_main is not None else "N/A")
                kcols[2].metric("Positive Feedbacks (%)", f"{our_stats.get('pos_pct', 0):.2f}%", delta=f"Position {pos_ranks.get(our, '-')} of {len(apps_compare)}", delta_color="off")
                kcols[3].metric("Negative Feedbacks (%)", f"{our_stats.get('neg_pct', 0):.2f}%", delta=f"Position {neg_ranks.get(our, '-')} of {len(apps_compare)}", delta_color="off")
                kcols[4].metric("Neutral Feedbacks (%)", f"{our_stats.get('neu_pct', 0):.2f}%", delta=f"Position {neu_ranks.get(our, '-')} of {len(apps_compare)}", delta_color="off")
                kcols[5].metric("Reputation Score", f"{our_stats.get('net', 0.0):.2f}", help=REPUTATION_SCORE_HELP)
                kcols[6].metric("Position", f"Rank {net_ranks.get(our, '-')} of {len(apps_compare)}")

                # ---- category-level net sentiment + win/lose insight summary ----
                categories = ["Usability", "Credibility", "Appearance", "Business"]

                def net_score_for(app, category):
                    sub = base_df[base_df[app_col].astype(str) == app] if app_col else base_df
                    if category != "Overall" and category_col_any:
                        sub = sub[sub[category_col_any].astype(str).str.lower() == category.lower()]
                    if sentiment_col_any is None or sub.empty:
                        return 0.0
                    s = sub[sentiment_col_any].astype(str).str.lower()
                    pos, neg = int(s.eq("positive").sum()), int(s.eq("negative").sum())
                    total = pos + neg
                    return round((pos - neg) / total, 2) if total > 0 else 0.0

                category_scores = {cat: {app: net_score_for(app, cat) for app in apps_compare} for cat in categories}

                win_categories, lose_categories = [], []
                gap_to_best, gap_to_worst = {}, {}
                for cat in categories:
                    our_score = category_scores[cat][our]
                    comp_scores = [category_scores[cat][a] for a in comp_apps]
                    if our_score > max(comp_scores):
                        win_categories.append(cat)
                    elif our_score < min(comp_scores):
                        lose_categories.append(cat)
                    gap_to_best[cat] = round(our_score - max(comp_scores), 2)
                    gap_to_worst[cat] = round(our_score - min(comp_scores), 2)

                comp_nets = {a: stats_map[a]["net"] for a in comp_apps}
                overall_net = stats_map[our]["net"]

                # Net Sentiment ranges from -1 to +1 ((positive-negative)/(positive+negative)).
                # Band it into a plain-language read on what the score means for the business.
                def interpret_net_sentiment(score):
                    if score >= 0.5:
                        return "strongly positive — the large majority of users are satisfied, a strong competitive asset worth protecting"
                    if score >= 0.2:
                        return "positive but not yet dominant — users lean satisfied, but the remaining complaints risk eroding loyalty if left unaddressed"
                    if score >= -0.2:
                        return "mixed/split — satisfied and dissatisfied users are roughly balanced, churn risk is elevated"
                    return "negative — complaints dominate the feedback, a strong signal of reputation and retention risk that needs urgent attention"

                comparison_phrases = []
                for a in comp_apps:
                    diff = overall_net - comp_nets[a]
                    if diff > 0:
                        comparison_phrases.append(f"ahead of {a} ({comp_nets[a]:.2f})")
                    elif diff < 0:
                        comparison_phrases.append(f"behind {a} ({comp_nets[a]:.2f})")
                    else:
                        comparison_phrases.append(f"on par with {a} ({comp_nets[a]:.2f})")

                overall_line = (
                    f"**Overall:** Reputation Score for **{our}** is **{overall_net:.2f}** — {interpret_net_sentiment(overall_net)}. "
                    f"This is {' and '.join(comparison_phrases)}."
                )

                contested_categories = [c for c in categories if c not in win_categories and c not in lose_categories]
                if len(win_categories) > len(lose_categories):
                    winlose_meaning = "a net advantage across feature areas"
                elif len(lose_categories) > len(win_categories):
                    winlose_meaning = "a net disadvantage across feature areas — a structural weakness rather than an isolated issue"
                else:
                    winlose_meaning = "a contested position with no clear overall edge"

                winlose_line = (
                    f"**Winning / Losing:** {our} leads in **{len(win_categories)}/{len(categories)}** categories"
                    f" ({', '.join(win_categories) if win_categories else 'none'}), trails in **{len(lose_categories)}/{len(categories)}**"
                    f" ({', '.join(lose_categories) if lose_categories else 'none'}), and is contested in"
                    f" **{len(contested_categories)}/{len(categories)}** ({', '.join(contested_categories) if contested_categories else 'none'})"
                    f" — {winlose_meaning}."
                )

                biggest_weak_cat = min(gap_to_best, key=gap_to_best.get)
                biggest_lead_cat = max(gap_to_worst, key=gap_to_worst.get)

                if gap_to_best[biggest_weak_cat] < 0:
                    risk_line = (
                        f"**Biggest Risk:** {biggest_weak_cat} trails the strongest competitor by **{gap_to_best[biggest_weak_cat]:+.2f}** Reputation Score"
                        f" — the category most likely to drive churn or negative word-of-mouth if left unaddressed."
                    )
                else:
                    risk_line = f"**Biggest Risk:** None — {our} matches or leads every competitor in all categories."

                if gap_to_worst[biggest_lead_cat] > 0:
                    biggest_lead_line = (
                        f"**Biggest Lead:** {biggest_lead_cat} is ahead of the weakest competitor by **{gap_to_worst[biggest_lead_cat]:+.2f}** Reputation Score"
                        f" — a differentiator worth protecting and highlighting in positioning."
                    )
                else:
                    biggest_lead_line = f"**Biggest Lead:** None — {our} does not clearly outperform competitors in any category."

                render_insight_row([overall_line, winlose_line, risk_line, biggest_lead_line])

                # ---- bar chart (volume) + category delta table ----
                delta_col_2 = f"Δ vs {comp_app_2}"
                delta_col_3 = f"Δ vs {comp_app_3}"
                delta_rows = []
                for cat in categories + ["Overall"]:
                    if cat == "Overall":
                        scores = {our: stats_map[our]["net"], comp_app_2: stats_map[comp_app_2]["net"], comp_app_3: stats_map[comp_app_3]["net"]}
                    else:
                        scores = category_scores[cat]
                    row = {"Category": cat, our: scores[our], comp_app_2: scores[comp_app_2], comp_app_3: scores[comp_app_3]}
                    row[delta_col_2] = round(scores[our] - scores[comp_app_2], 2)
                    row[delta_col_3] = round(scores[our] - scores[comp_app_3], 2)
                    delta_rows.append(row)

                delta_df = pd.DataFrame(delta_rows)[["Category", our, comp_app_2, comp_app_3, delta_col_2, delta_col_3]]
                numeric_cols = [our, comp_app_2, comp_app_3, delta_col_2, delta_col_3]

                # Match the bar chart's height to this table's rendered height (header + one row per category).
                table_height = 35 * (len(delta_rows) + 1) + 3

                col_chart, col_table = st.columns([1, 1])

                # Grouped bar chart of the same category-level Net Sentiment shown in the
                # table beside it — the raw review-volume chart it replaced didn't carry a
                # win/lose signal by itself, while this pairs a visual read with the exact
                # numbers (volume is still available via the "Num. of Feedbacks" KPI card).
                category_chart_df = pd.DataFrame(
                    [
                        {"Category": cat, "app": app, "net_sentiment": category_scores[cat][app]}
                        for cat in categories
                        for app in apps_compare
                    ]
                )
                if not category_chart_df.empty:
                    chart = (
                        alt.Chart(category_chart_df)
                        .mark_bar()
                        .encode(
                            y=alt.Y("Category:N", title=None),
                            x=alt.X("net_sentiment:Q", title="Reputation Score"),
                            yOffset=alt.YOffset("app:N"),
                            color=alt.Color("app:N", title="App"),
                        )
                        .properties(title="Reputation Score by Category", height=table_height)
                    )
                    col_chart.altair_chart(chart, use_container_width=True)
                else:
                    col_chart.info("No data to build comparison chart.")

                def highlight_delta(val):
                    # Pair each background with its own fixed text color rather than
                    # leaving color to inherit from the app's theme — Streamlit's
                    # dark theme uses light text by default, which is unreadable on
                    # top of these light green/red backgrounds.
                    if isinstance(val, (int, float)):
                        if val > 0:
                            return "background-color: #d4f7dc; color: #0f3d1e;"
                        if val < 0:
                            return "background-color: #fbdada; color: #5c1a1a;"
                    return ""

                styled_delta = delta_df.style.format({c: "{:.2f}" for c in numeric_cols}).applymap(highlight_delta, subset=[delta_col_2, delta_col_3])
                col_table.dataframe(styled_delta, use_container_width=True, hide_index=True, height=table_height)

                # ---- net sentiment trend over time ----
                st.markdown("##### Reputation Score Trend")
                if date_col_any and sentiment_col_any and app_col:
                    trend_src = base_df[base_df[app_col].astype(str).isin(apps_compare)].copy()
                    trend_src[date_col_any] = pd.to_datetime(trend_src[date_col_any], errors="coerce")
                    trend_src = trend_src.dropna(subset=[date_col_any])
                    if trend_src.empty:
                        st.info("No dated rows available to build a trend chart.")
                    else:
                        trend_src["period"] = trend_src[date_col_any].dt.to_period("W").dt.start_time
                        trend_src["sent_lower"] = trend_src[sentiment_col_any].astype(str).str.lower()

                        def weekly_net(group):
                            pos = group.eq("positive").sum()
                            neg = group.eq("negative").sum()
                            return round((pos - neg) / (pos + neg), 2) if (pos + neg) > 0 else None

                        trend_net = (
                            trend_src.groupby(["period", app_col])["sent_lower"]
                            .apply(weekly_net)
                            .reset_index(name="net_sentiment")
                            .dropna(subset=["net_sentiment"])
                        )
                        trend_net = trend_net.rename(columns={app_col: "app"})

                        trend_chart = (
                            alt.Chart(trend_net)
                            .mark_line(point=True, strokeWidth=3)
                            .encode(
                                x=alt.X("period:T", title="Week"),
                                y=alt.Y("net_sentiment:Q", title="Reputation Score"),
                                color=alt.Color("app:N", title="App"),
                            )
                            .properties(height=350)
                            .interactive()
                        )
                        st.altair_chart(trend_chart, use_container_width=True)
                else:
                    st.info("No date/sentiment column found to build a trend chart.")

                # ---- sample negative reviews per category, one list per app ----
                st.markdown("##### Sample Reviews by Category")
                if text_col_any and category_col_any and sentiment_col_any and app_col:
                    quote_category = st.selectbox("Category", categories, key="comp_quote_category")
                    quote_cols = st.columns(len(apps_compare))
                    for qcol, app in zip(quote_cols, apps_compare):
                        with qcol:
                            st.markdown(f"**{app}**")
                            sub = base_df[
                                (base_df[app_col].astype(str) == app)
                                & (base_df[category_col_any].astype(str).str.lower() == quote_category.lower())
                                & (base_df[sentiment_col_any].astype(str).str.lower() == "negative")
                            ]
                            samples = sub[text_col_any].dropna().astype(str)
                            samples = samples[samples.str.strip() != ""].head(5)
                            if samples.empty:
                                st.caption("No negative reviews found for this category.")
                            else:
                                st.markdown("\n".join(f"{i}. {txt}" for i, txt in enumerate(samples, start=1)))
                else:
                    st.info("Text/category/sentiment columns not found to build the sample review explorer.")

        # start tab request intelligence #
        if selected_nav == "Request Intelligence":
            st.subheader("Request Intelligence")
            st.caption(
                "Feature requests inferred from neutral-sentiment feedback, ranked by volume with trend and "
                "sample reviews. Select a request in the table below to read real examples."
            )
            # Subcategory frequency view — only neutral sentiment, include category, exclude taxonomy-mismatch rows
            subcat_col = find_col(filtered_df, ["Subcategory", "subcategory", "subcategorization", "sub_categorization"]) if not filtered_df.empty else None
            if subcat_col:
                # determine sentiment, category, date and text columns if present
                sentiment_col_freq = find_col(filtered_df, ["Sentiment", "sentiment"]) if not filtered_df.empty else None
                category_col_ri = find_col(filtered_df, ["Category", "category"]) if not filtered_df.empty else None
                date_col_ri = find_col(filtered_df, ["review_date", "Review Date", "date", "Date"]) if not filtered_df.empty else None
                text_col_ri = (find_col(filtered_df, ["normalized_text"]) or find_col(filtered_df, ["text", "review_text"])) if not filtered_df.empty else None

                df_freq = filtered_df.copy()
                # filter to neutral sentiment only (case-insensitive) if sentiment column exists
                if sentiment_col_freq:
                    df_freq = df_freq[df_freq[sentiment_col_freq].astype(str).str.lower() == "neutral"].copy()

                # Taxonomy-mismatch rows are already excluded from filtered_df globally
                # (see the filter applied right after category_col/subcat_col_global
                # are detected), so no separate exclusion is needed here anymore.

                if df_freq.empty:
                    st.info("No neutral rows available after applying filters and exclusions.")
                else:
                    if category_col_ri:
                        counts = (
                            df_freq.groupby([category_col_ri, subcat_col])
                            .size()
                            .reset_index(name="count")
                            .sort_values("count", ascending=False)
                        )
                        counts = counts.rename(columns={category_col_ri: "category", subcat_col: "subcategory"})
                    else:
                        counts = df_freq[subcat_col].dropna().astype(str).value_counts().reset_index()
                        counts.columns = ["subcategory", "count"]
                        counts = counts.sort_values("count", ascending=False).reset_index(drop=True)

                    # Build User Requests table: Requests (subcategory), Category, Count
                    if "subcategory" in counts.columns and "count" in counts.columns:
                        if "category" in counts.columns:
                            ur_df = counts.rename(columns={"subcategory": "Requests", "category": "Category", "count": "Count"})[["Requests", "Category", "Count"]].copy()
                        else:
                            ur_df = counts.rename(columns={"subcategory": "Requests", "count": "Count"})[["Requests", "Count"]].copy()
                            ur_df["Category"] = "Unknown"
                            ur_df = ur_df[["Requests", "Category", "Count"]]

                        # Trend: compare each request's volume in the first half vs second half of
                        # the date range — with only a handful of occurrences per request, a full
                        # weekly time series would be too sparse to read, so a two-bucket split is
                        # the simplest signal that still tells "emerging" from "stagnant".
                        if date_col_ri:
                            trend_src = df_freq.copy()
                            trend_src[date_col_ri] = pd.to_datetime(trend_src[date_col_ri], errors="coerce")
                            trend_src = trend_src.dropna(subset=[date_col_ri])
                        else:
                            trend_src = pd.DataFrame()

                        def compute_trend(request_name):
                            if trend_src.empty:
                                return "—"
                            sub = trend_src[trend_src[subcat_col].astype(str) == request_name]
                            if sub.empty:
                                return "—"
                            median_date = trend_src[date_col_ri].median()
                            first_half = int((sub[date_col_ri] < median_date).sum())
                            second_half = int((sub[date_col_ri] >= median_date).sum())
                            if first_half == 0 and second_half > 0:
                                return "🆕 New"
                            if second_half > first_half:
                                return "▲ Rising"
                            if second_half < first_half:
                                return "▼ Declining"
                            return "▬ Stable"

                        ur_df["Trend"] = ur_df["Requests"].apply(compute_trend)

                        # Share of feedback: how big is this request relative to all feedback
                        # currently in view, so a raw count of "4" has context.
                        total_feedback = int(filtered_df.shape[0])
                        ur_df["Share of Feedback"] = (ur_df["Count"] / total_feedback * 100).round(2) if total_feedback else 0.0
                        ur_df = ur_df.sort_values("Count", ascending=False).reset_index(drop=True)

                        total_requests = int(ur_df["Count"].sum())
                        unique_request_types = int(ur_df["Requests"].nunique())
                        request_share_pct = round(total_requests / total_feedback * 100, 2) if total_feedback else 0.0

                        k1, k2, k3 = st.columns(3)
                        k1.metric("Total Requests", total_requests)
                        k2.metric("Unique Request Types", unique_request_types)
                        k3.metric("Share of Feedback", f"{request_share_pct:.2f}%")

                        # ---- Insight Summary: English narrative over the request matrix,
                        # mirroring Competitor Analysis (metric + meaning per line). ----
                        def interpret_share(pct):
                            if pct >= 15:
                                return "a significant share of feedback volume — feature/functionality gaps are a major driver of what users are telling you"
                            if pct >= 5:
                                return "a moderate share of feedback — worth tracking, though not yet the dominant theme"
                            return "a small share of feedback — requests are a minor signal relative to other feedback types"

                        overall_line = (
                            f"**Overall:** **{total_requests}** requests across **{unique_request_types}** unique types make up "
                            f"**{request_share_pct:.2f}%** of all feedback in view — {interpret_share(request_share_pct)}."
                        )

                        top_count = int(ur_df["Count"].max())
                        top_requests_list = ur_df[ur_df["Count"] == top_count]["Requests"].tolist()
                        if len(top_requests_list) == 1:
                            top_req = top_requests_list[0]
                            top_req_cat = ur_df.loc[ur_df["Requests"] == top_req, "Category"].iloc[0]
                            top_line = (
                                f"**Top Request:** \"{top_req}\" leads with **{top_count}** mentions ({top_req_cat}) "
                                f"— the single clearest signal of what to build next."
                            )
                        else:
                            top_line = (
                                f"**Top Requests:** **{len(top_requests_list)}** requests are tied at **{top_count}** mentions each "
                                f"({', '.join(top_requests_list)}) — no single request dominates, so prioritization needs a "
                                f"secondary signal (category concentration or trend) rather than raw count alone."
                            )

                        trend_counts = ur_df["Trend"].value_counts()
                        rising_n = int(trend_counts.get("▲ Rising", 0)) + int(trend_counts.get("🆕 New", 0))
                        declining_n = int(trend_counts.get("▼ Declining", 0))
                        stable_n = int(trend_counts.get("▬ Stable", 0))
                        if rising_n > declining_n:
                            momentum_meaning = "demand for these requests is intensifying — acting now captures a growing trend rather than chasing an already-fading one"
                        elif declining_n > rising_n:
                            momentum_meaning = "demand for these requests is cooling off — some may already be resolved or losing relevance, worth re-checking before committing roadmap time"
                        else:
                            momentum_meaning = "demand is holding steady — no clear acceleration or fading signal yet"
                        momentum_line = (
                            f"**Momentum:** **{rising_n}** request type(s) rising/new, **{declining_n}** declining, **{stable_n}** stable — {momentum_meaning}."
                        )

                        insight_lines = [overall_line, top_line, momentum_line]
                        if category_col_ri:
                            cat_totals = ur_df.groupby("Category")["Count"].sum().sort_values(ascending=False)
                            top_cat = cat_totals.index[0]
                            top_cat_count = int(cat_totals.iloc[0])
                            top_cat_share = round(top_cat_count / total_requests * 100, 1) if total_requests else 0.0
                            insight_lines.append(
                                f"**Category Focus:** **{top_cat}** accounts for **{top_cat_count}/{total_requests}** requests "
                                f"({top_cat_share}%) — the roadmap area with the most concentrated user demand."
                            )

                        render_insight_row(insight_lines)

                        # Ranking chart — ties (e.g. three requests sharing the top count) are
                        # visible at a glance here, unlike a single "Top Requested" KPI card.
                        # labelLimit is raised so the full request phrase renders on the y-axis
                        # instead of being truncated, and a text layer prints the count on each bar.
                        st.subheader("Top Requests")
                        top_requests_df = ur_df.head(10)
                        bars = (
                            alt.Chart(top_requests_df)
                            .mark_bar(color="#4e79a7")
                            .encode(
                                x=alt.X("Count:Q", title="Requests", axis=alt.Axis(format="d")),
                                y=alt.Y("Requests:N", sort="-x", title=None, axis=alt.Axis(labelLimit=500)),
                            )
                        )
                        bar_labels = (
                            alt.Chart(top_requests_df)
                            .mark_text(align="left", dx=4, color="#333")
                            .encode(
                                x=alt.X("Count:Q"),
                                y=alt.Y("Requests:N", sort="-x"),
                                text=alt.Text("Count:Q", format="d"),
                            )
                        )
                        requests_chart = (bars + bar_labels).properties(height=35 * (len(top_requests_df) + 1) + 3)
                        st.altair_chart(requests_chart, use_container_width=True)

                        col_table, col_reviews = st.columns([60, 40])
                        # The "ri_table_box" / "ri_reviews_box" keys become CSS classes
                        # (st-key-...) targeted below to add left/right padding only —
                        # no fixed height or border, so appearance otherwise stays default.

                        with col_table:
                            with st.container(key="ri_table_box"):
                                st.markdown("##### All Requests")
                                st.dataframe(
                                    ur_df[["Requests", "Category", "Count", "Share of Feedback", "Trend"]],
                                    use_container_width=True,
                                    hide_index=True,
                                )

                        with col_reviews:
                            with st.container(key="ri_reviews_box"):
                                # Sample reviews for a chosen request, sourced from normalized_text.
                                st.markdown("##### Sample Reviews")
                                if text_col_ri:
                                    selected_request = st.selectbox("Request", ur_df["Requests"].tolist(), key="ri_selected_request")
                                    quote_sub = df_freq[df_freq[subcat_col].astype(str) == selected_request]
                                    samples = quote_sub[text_col_ri].dropna().astype(str)
                                    samples = samples[samples.str.strip() != ""].head(5)
                                    if samples.empty:
                                        st.caption("No sample reviews available for this request.")
                                    else:
                                        st.markdown("\n".join(f"{i}. {txt}" for i, txt in enumerate(samples, start=1)))
                                else:
                                    st.info("No text column found to build the sample review explorer.")

            else:
                st.info("No subcategory column found to compute frequencies.")