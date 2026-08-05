import base64
import io
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st


# =========================================================
# การตั้งค่าหน้า
# =========================================================
st.set_page_config(
    page_title="ระบบติดตามอาการนักศึกษา",
    page_icon="💚",
    layout="wide",
)

BKK = ZoneInfo("Asia/Bangkok")
CSV_COLUMNS = [
    "record_id",
    "timestamp_bkk",
    "student_id",
    "color",
    "remark",
    "status",
    "updated_at_bkk",
]

COLOR_OPTIONS = ["เขียว", "เหลือง", "แดง"]
STATUS_OPTIONS = [
    "",
    "หายดีเป็นปกติแล้ว",
    "ยังไม่เป็นปกติ",
    "แย่ลง",
    "ติดต่อไม่ได้",
]

COLOR_ICON = {
    "เขียว": "🟢 เขียว",
    "เหลือง": "🟡 เหลือง",
    "แดง": "🔴 แดง",
}

STATUS_ICON = {
    "": "รอติดตาม",
    "หายดีเป็นปกติแล้ว": "✅ หายดีเป็นปกติแล้ว",
    "ยังไม่เป็นปกติ": "🟡 ยังไม่เป็นปกติ",
    "แย่ลง": "🔴 แย่ลง",
    "ติดต่อไม่ได้": "⚪ ติดต่อไม่ได้",
}


# =========================================================
# GitHub helper
# =========================================================
def get_secret(name: str) -> str:
    """อ่านค่าจาก Streamlit Secrets และแจ้งข้อผิดพลาดแบบเข้าใจง่าย"""
    try:
        value = str(st.secrets[name]).strip()
    except Exception:
        st.error(f"ยังไม่ได้ตั้งค่า `{name}` ใน Streamlit Secrets")
        st.stop()
    if not value:
        st.error(f"ค่า `{name}` ใน Streamlit Secrets ว่างอยู่")
        st.stop()
    return value


def github_config() -> dict:
    return {
        "owner": get_secret("github_owner"),
        "repo": get_secret("github_repo"),
        "branch": get_secret("github_branch"),
        "path": get_secret("github_csv_path"),
        "token": get_secret("github_token"),
    }


def github_headers(token: str) -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def csv_to_dataframe(csv_bytes: bytes) -> pd.DataFrame:
    if not csv_bytes.strip():
        return pd.DataFrame(columns=CSV_COLUMNS)

    try:
        df = pd.read_csv(io.BytesIO(csv_bytes), dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=CSV_COLUMNS)

    for col in CSV_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df = df[CSV_COLUMNS].fillna("").astype(str)
    return df


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    clean = df.copy()
    for col in CSV_COLUMNS:
        if col not in clean.columns:
            clean[col] = ""
    clean = clean[CSV_COLUMNS].fillna("").astype(str)
    return clean.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")


def read_github_csv() -> tuple[pd.DataFrame, str | None]:
    """
    อ่าน CSV และ SHA ล่าสุดจาก GitHub
    หากไฟล์ยังไม่มี จะคืน DataFrame ว่างและ sha=None
    """
    cfg = github_config()
    url = (
        f"https://api.github.com/repos/{cfg['owner']}/{cfg['repo']}"
        f"/contents/{cfg['path']}"
    )
    response = requests.get(
        url,
        headers=github_headers(cfg["token"]),
        params={"ref": cfg["branch"]},
        timeout=20,
    )

    if response.status_code == 404:
        return pd.DataFrame(columns=CSV_COLUMNS), None

    if response.status_code != 200:
        raise RuntimeError(
            f"อ่านข้อมูลจาก GitHub ไม่สำเร็จ "
            f"(HTTP {response.status_code}): {response.text[:300]}"
        )

    payload = response.json()
    content = base64.b64decode(payload["content"])
    return csv_to_dataframe(content), payload.get("sha")


def write_github_csv(
    df: pd.DataFrame,
    sha: str | None,
    commit_message: str,
) -> None:
    """สร้างหรือแทนที่ไฟล์ CSV บน GitHub"""
    cfg = github_config()
    url = (
        f"https://api.github.com/repos/{cfg['owner']}/{cfg['repo']}"
        f"/contents/{cfg['path']}"
    )

    body = {
        "message": commit_message,
        "content": base64.b64encode(dataframe_to_csv_bytes(df)).decode("ascii"),
        "branch": cfg["branch"],
    }
    if sha:
        body["sha"] = sha

    response = requests.put(
        url,
        headers=github_headers(cfg["token"]),
        json=body,
        timeout=30,
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"บันทึกข้อมูลลง GitHub ไม่สำเร็จ "
            f"(HTTP {response.status_code}): {response.text[:300]}"
        )


def append_case(new_row: dict, max_retries: int = 3) -> None:
    """
    เพิ่มเคสใหม่โดยอ่านไฟล์ล่าสุดก่อนทุกครั้ง
    และลองใหม่เมื่อมีการบันทึกชนกัน
    """
    for attempt in range(max_retries):
        df, sha = read_github_csv()

        # ป้องกันการเพิ่ม record_id เดิมซ้ำจากการ rerun
        if new_row["record_id"] not in set(df["record_id"].astype(str)):
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

        try:
            write_github_csv(
                df,
                sha,
                f"Add follow-up case {new_row['student_id']}",
            )
            return
        except RuntimeError as exc:
            if "HTTP 409" in str(exc) and attempt < max_retries - 1:
                time.sleep(0.7 * (attempt + 1))
                continue
            raise


def update_statuses(
    status_by_record_id: dict[str, str],
    max_retries: int = 3,
) -> None:
    """
    อัปเดตเฉพาะสถานะตาม record_id บนข้อมูล GitHub ฉบับล่าสุด
    เพื่อลดความเสี่ยงจากการเขียนทับเคสใหม่ของแพทย์
    """
    for attempt in range(max_retries):
        latest_df, sha = read_github_csv()
        now_bkk = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")

        changed = False
        for record_id, new_status in status_by_record_id.items():
            mask = latest_df["record_id"].astype(str) == str(record_id)
            if not mask.any():
                continue

            old_status = latest_df.loc[mask, "status"].iloc[0]
            if old_status != new_status:
                latest_df.loc[mask, "status"] = new_status
                latest_df.loc[mask, "updated_at_bkk"] = now_bkk
                changed = True

        if not changed:
            return

        try:
            write_github_csv(
                latest_df,
                sha,
                "Update follow-up statuses",
            )
            return
        except RuntimeError as exc:
            if "HTTP 409" in str(exc) and attempt < max_retries - 1:
                time.sleep(0.7 * (attempt + 1))
                continue
            raise


# =========================================================
# Authentication helper
# =========================================================
def role_login(role_key: str, expected_password: str, title: str) -> bool:
    session_key = f"authenticated_{role_key}"

    if st.session_state.get(session_key, False):
        col1, col2 = st.columns([5, 1])
        with col2:
            if st.button("ออกจากระบบ", key=f"logout_{role_key}", use_container_width=True):
                st.session_state[session_key] = False
                st.rerun()
        return True

    st.subheader(title)
    with st.form(f"login_form_{role_key}"):
        password = st.text_input("รหัสผ่าน", type="password")
        submitted = st.form_submit_button("เข้าสู่ระบบ", use_container_width=True)

    if submitted:
        if password == expected_password:
            st.session_state[session_key] = True
            st.rerun()
        else:
            st.error("รหัสผ่านไม่ถูกต้อง")
    return False


# =========================================================
# Display helper
# =========================================================
def load_sorted_data() -> pd.DataFrame:
    df, _ = read_github_csv()

    if df.empty:
        return df

    df["_dt"] = pd.to_datetime(df["timestamp_bkk"], errors="coerce")
    df = (
        df.sort_values("_dt", ascending=False, na_position="last")
        .drop(columns=["_dt"])
        .reset_index(drop=True)
    )
    return df


def dashboard_view(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(
            columns=["สี", "วันเวลา", "ID", "ข้อกังวล", "สถานะ"]
        )

    view = pd.DataFrame(
        {
            "สี": df["color"].map(COLOR_ICON).fillna(df["color"]),
            "วันเวลา": df["timestamp_bkk"],
            "ID": df["student_id"],
            "ข้อกังวล": df["remark"],
            "สถานะ": df["status"].map(STATUS_ICON).fillna(df["status"]),
        }
    )
    return view


def style_dashboard(view: pd.DataFrame):
    def row_style(row):
        color_text = str(row.get("สี", ""))
        if "แดง" in color_text:
            bg = "background-color: rgba(255, 80, 80, 0.13);"
        elif "เหลือง" in color_text:
            bg = "background-color: rgba(255, 205, 50, 0.15);"
        elif "เขียว" in color_text:
            bg = "background-color: rgba(70, 190, 100, 0.12);"
        else:
            bg = ""
        return [bg] * len(row)

    return view.style.apply(row_style, axis=1)


# =========================================================
# Pages
# =========================================================
def page_dashboard():
    st.title("💚 Dashboard ติดตามอาการนักศึกษา")
    st.caption("เรียงตามวัน–เวลาที่แพทย์บันทึกล่าสุดก่อน • เวลาเขตกรุงเทพฯ")

    col1, col2 = st.columns([5, 1])
    with col2:
        if st.button("🔄 รีเฟรช", use_container_width=True):
            st.rerun()

    try:
        df = load_sorted_data()
    except Exception as exc:
        st.error(str(exc))
        return

    if df.empty:
        st.info("ยังไม่มีข้อมูลติดตาม")
        return

    waiting = int((df["status"] == "").sum())
    recovered = int((df["status"] == "หายดีเป็นปกติแล้ว").sum())
    not_normal = int((df["status"] == "ยังไม่เป็นปกติ").sum())
    worse = int((df["status"] == "แย่ลง").sum())
    unreachable = int((df["status"] == "ติดต่อไม่ได้").sum())

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("รอติดตาม", waiting)
    c2.metric("หายดีแล้ว", recovered)
    c3.metric("ยังไม่ปกติ", not_normal)
    c4.metric("แย่ลง", worse)
    c5.metric("ติดต่อไม่ได้", unreachable)

    view = dashboard_view(df)
    st.dataframe(
        style_dashboard(view),
        width="stretch",
        hide_index=True,
        height=560,
        column_config={
            "สี": st.column_config.TextColumn(width="small"),
            "วันเวลา": st.column_config.TextColumn(width="medium"),
            "ID": st.column_config.TextColumn(width="medium"),
            "ข้อกังวล": st.column_config.TextColumn(width="large"),
            "สถานะ": st.column_config.TextColumn(width="medium"),
        },
    )


def page_doctor():
    doctor_password = get_secret("doctor_password")
    if not role_login("doctor", doctor_password, "🔐 หน้าแพทย์"):
        return

    st.title("🩺 บันทึกผู้ป่วยที่ต้องติดตาม")
    st.caption("ระบบบันทึกวัน–เวลาอัตโนมัติตามเขตเวลา Asia/Bangkok")

    with st.form("doctor_entry_form", clear_on_submit=True):
        student_id = st.text_input(
            "Student ID",
            placeholder="กรอกรหัสนักศึกษา",
            max_chars=30,
        ).strip()

        color = st.radio(
            "สี",
            options=COLOR_OPTIONS,
            horizontal=True,
        )

        remark = st.text_area(
            "ข้อกังวล (Remark)",
            placeholder="เช่น ไข้สูงจากคออักเสบ ให้ติดตามว่าไข้ลงและกลับเป็นปกติหรือยัง",
            height=130,
            max_chars=1000,
        ).strip()

        submitted = st.form_submit_button(
            "บันทึกเข้าระบบติดตาม",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        if not student_id:
            st.error("กรุณากรอก Student ID")
        elif not remark:
            st.error("กรุณากรอกข้อกังวล (Remark)")
        else:
            now_bkk = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
            new_row = {
                "record_id": str(uuid.uuid4()),
                "timestamp_bkk": now_bkk,
                "student_id": student_id,
                "color": color,
                "remark": remark,
                "status": "",
                "updated_at_bkk": "",
            }
            try:
                append_case(new_row)
                st.success(
                    f"บันทึก Student ID {student_id} เรียบร้อยแล้ว "
                    f"เวลา {now_bkk}"
                )
            except Exception as exc:
                st.error(str(exc))

    st.divider()
    st.subheader("รายการล่าสุด")
    try:
        df = load_sorted_data().head(20)
        if df.empty:
            st.info("ยังไม่มีข้อมูล")
        else:
            st.dataframe(
                dashboard_view(df),
                width="stretch",
                hide_index=True,
            )
    except Exception as exc:
        st.error(str(exc))


def page_registry():
    registry_password = get_secret("registry_password")
    if not role_login("registry", registry_password, "🔐 หน้าเวชระเบียน"):
        return

    st.title("☎️ หน้าเวชระเบียน")
    st.caption(
        "ค้นหาเบอร์มือถือหรืออีเมลจากระบบเดิมภายนอกหน้านี้ "
        "ข้อมูลการติดต่อจะไม่ถูกบันทึกใน CSV"
    )

    try:
        df = load_sorted_data()
    except Exception as exc:
        st.error(str(exc))
        return

    if df.empty:
        st.info("ยังไม่มีข้อมูลให้ติดตาม")
        return

    # เก็บ snapshot เพื่อเทียบเฉพาะค่าที่เปลี่ยน
    original_status = dict(zip(df["record_id"], df["status"]))

    editor_df = pd.DataFrame(
        {
            "record_id": df["record_id"],
            "สี": df["color"].map(COLOR_ICON).fillna(df["color"]),
            "วันเวลา": df["timestamp_bkk"],
            "ID": df["student_id"],
            "ข้อกังวล": df["remark"],
            "สถานะ": pd.Categorical(
                df["status"],
                categories=STATUS_OPTIONS,
            ),
        }
    )

    edited = st.data_editor(
        editor_df,
        width="stretch",
        hide_index=True,
        height=560,
        num_rows="fixed",
        disabled=["record_id", "สี", "วันเวลา", "ID", "ข้อกังวล"],
        column_order=["สี", "วันเวลา", "ID", "ข้อกังวล", "สถานะ"],
        column_config={
            "record_id": None,
            "สี": st.column_config.TextColumn(width="small"),
            "วันเวลา": st.column_config.TextColumn(width="medium"),
            "ID": st.column_config.TextColumn(width="medium"),
            "ข้อกังวล": st.column_config.TextColumn(width="large"),
            "สถานะ": st.column_config.SelectboxColumn(
                "สถานะ",
                options=STATUS_OPTIONS,
                required=True,
                width="medium",
                help="เลือกผลหลังติดต่อผู้ป่วย",
            ),
        },
        key="registry_editor",
    )

    col1, col2 = st.columns([1, 4])
    with col1:
        save_clicked = st.button(
            "💾 บันทึกสถานะ",
            type="primary",
            use_container_width=True,
        )
    with col2:
        st.caption(
            "ช่องว่างหมายถึงยังรอติดตาม "
            "ระบบจะบันทึกเฉพาะรายการที่สถานะเปลี่ยนแปลง"
        )

    if save_clicked:
        changes = {}
        for _, row in edited.iterrows():
            record_id = str(row["record_id"])
            new_status = str(row["สถานะ"])
            if new_status == "nan":
                new_status = ""
            if original_status.get(record_id, "") != new_status:
                changes[record_id] = new_status

        if not changes:
            st.info("ไม่มีสถานะที่เปลี่ยนแปลง")
        else:
            try:
                update_statuses(changes)
                st.success(f"บันทึกสถานะเรียบร้อย {len(changes)} รายการ")
                time.sleep(0.4)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))


# =========================================================
# Main navigation
# =========================================================
st.sidebar.title("ระบบติดตามอาการ")
page = st.sidebar.radio(
    "เลือกหน้า",
    ["Dashboard", "แพทย์", "เวชระเบียน"],
)

st.sidebar.divider()
st.sidebar.caption(
    "ข้อมูลเก็บใน GitHub CSV\n\n"
    "ไม่มีการบันทึกเบอร์มือถือหรืออีเมลในระบบนี้"
)

if page == "Dashboard":
    page_dashboard()
elif page == "แพทย์":
    page_doctor()
else:
    page_registry()
