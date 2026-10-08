import os
import re
import json
import time
import requests
import threading
import urllib.parse
import base64
import io
import mimetypes
import zipfile
from datetime import datetime
from functools import lru_cache

# Google Cloud & Pub/Sub 相關
import functions_framework
from cloudevents.http import CloudEvent
from google import genai
from google.genai import types
from google.cloud import secretmanager
from google.cloud import firestore

# ==============================================================================
# 全局設定與環境變數
# ==============================================================================
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "project-9dd85752-d2db-4567-842")
LOCATION = os.environ.get("VERTEX_LOCATION", "global") 
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

# 初始化 Firestore 客戶端（明確指定 database 參數）
db_firestore = None
def get_firestore():
    global db_firestore
    if db_firestore is None:
        # 若在 Cloud Run 內，直接 firestore.Client() 即可自動抓取 GCP 專案與 (default) 資料庫
        db_firestore = firestore.Client(project=PROJECT_ID, database="default")
    return db_firestore

# 初始化 Database Engine 連線池單例
db_engine = None
def get_db_engine():
  global db_engine
  if db_engine is None:
    from sqlalchemy import create_engine
    db_uri = get_secret("DB_CONNECTION_STRING")
    if not db_uri:
      raise ValueError("未配置 DB_CONNECTION_STRING 密鑰。")
    db_engine = create_engine(
      db_uri,
      pool_size=5,
      max_overflow=10,
      pool_recycle=1800,
      pool_pre_ping=True
    )
  return db_engine

# Thread-Local Context
thread_context = threading.local()

# ==============================================================================
# Helper Functions: 文件解析與圖片提取 (Word / Excel / CSV / PDF / PPTX)
# ==============================================================================
def extract_docx_images(doc_bytes: bytes) -> list:
    """從 Word (.docx) 壓縮包中自動提取所有內嵌的圖片 (PNG, JPEG 等) 供 Gemini 多模態辨識"""
    extracted_images = []
    mime_map = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "bmp": "image/bmp",
        "webp": "image/webp"
    }
    try:
        with zipfile.ZipFile(io.BytesIO(doc_bytes)) as z:
            for filename in z.namelist():
                if filename.startswith("word/media/"):
                    ext = filename.split(".")[-1].lower()
                    mime_type = mime_map.get(ext, "image/jpeg")
                    img_bytes = z.read(filename)
                    if img_bytes:
                        extracted_images.append((img_bytes, mime_type))
    except Exception as e:
        print(f"[Docx Image Extraction Error] {str(e)}")
    return extracted_images

def docx_to_text(doc_bytes: bytes) -> str:
    """解析 Word (.docx) 檔案為純文字 (支援所有普通段落與表格)"""
    try:
        import docx
        doc = docx.Document(io.BytesIO(doc_bytes))
        full_text = []

        for p in doc.paragraphs:
            if p.text.strip():
                full_text.append(p.text)

        for table in doc.tables:
            for row in table.rows:
                row_text = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if row_text:
                    full_text.append(" | ".join(row_text))

        return "\n".join(full_text)
    except Exception as e:
        print(f"[Docx Parsing Error] {str(e)}")
        return ""

def convert_doc_to_markdown(file_bytes: bytes, file_extension: str) -> str:
    """使用 MarkItDown 將各式文件 (Excel, PDF, PPTX 等) 轉為 Markdown"""
    import tempfile
    try:
        from markitdown import MarkItDown
        md = MarkItDown()
        with tempfile.NamedTemporaryFile(suffix=file_extension, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        result = md.convert(tmp_path)

        if os.path.exists(tmp_path):
            os.remove(tmp_path)

        return result.text_content
    except Exception as e:
        print(f"[MarkItDown Parsing Error] {str(e)}")
        return ""

# ==============================================================================
# Helper Functions: Secret Manager & Session Management (Firestore 隔離)
# ==============================================================================
@lru_cache(maxsize=32)
def get_secret(secret_id: str) -> str:
    """優先讀取環境變數（含別名），找不到才讀取 GCP Secret Manager（加入記憶體快取）"""
    env_val = os.environ.get(secret_id)
    if env_val:
        return env_val.strip()

    aliases = {
        "EVOLUTION_INSTANCE_NAME": ["INSTANCE_NAME", "EVO_INSTANCE"],
        "EVOLUTION_API_URL": ["EVOLUTION_URL", "EVO_URL"],
        "EVOLUTION_API_KEY": ["EVO_KEY"]
    }
    if secret_id in aliases:
        for alias in aliases[secret_id]:
            val = os.environ.get(alias)
            if val:
                return val.strip()

    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8").strip()
    except Exception:
        return ""

def get_session_history_firestore(remote_jid: str, limit: int = 10) -> list:
    """從 Cloud Firestore 獲取該用戶專屬的多輪對話歷史"""
    try:
        db = get_firestore()
        doc_id = urllib.parse.quote_plus(remote_jid)
        doc_ref = db.collection("whatsapp_sessions").document(doc_id)
        doc = doc_ref.get()
        if doc.exists:
            history = doc.to_dict().get("history", [])
            return history[-limit:]
    except Exception as e:
        print(f"[Firestore Read Error] {str(e)}")
    return []

def save_session_history_firestore(remote_jid: str, user_text_summary: str, model_reply: str):
    """保存對話歷史至 Firestore"""
    try:
        db = get_firestore()
        doc_id = urllib.parse.quote_plus(remote_jid)
        doc_ref = db.collection("whatsapp_sessions").document(doc_id)
        history = get_session_history_firestore(remote_jid, limit=10)

        history.append({"role": "user", "parts": [{"text": user_text_summary}]})
        history.append({"role": "model", "parts": [{"text": model_reply}]})

        doc_ref.set({
            "history": history,
            "updated_at": firestore.SERVER_TIMESTAMP,
            "remote_jid": remote_jid
        }, merge=True)
    except Exception as e:
        print(f"[Firestore Write Error] {str(e)}")

# ==============================================================================
# WhatsApp API 整合
# ==============================================================================
def download_evolution_media(instance_url: str, api_key: str, instance_name: str, message_data: dict) -> bytes:
    """從 Evolution API 解密並獲取媒體 Base64"""
    if not (instance_url and api_key and instance_name and message_data):
        return None

    headers = {"apikey": api_key, "Content-Type": "application/json"}
    base_url = instance_url.rstrip('/')
    endpoints = [
        f"{base_url}/v2/chat/getBase64FromMediaMessage/{instance_name}",
        f"{base_url}/chat/getBase64FromMediaMessage/{instance_name}"
    ]
    payload = {"message": message_data, "convertToMp4": False}

    for i, url in enumerate(endpoints, 1):
        try:
            res = requests.post(url, json=payload, headers=headers, timeout=15)
            if res.status_code in [200, 201]:
                res_data = res.json()
                b64_str = ""
                if isinstance(res_data, dict):
                    b64_str = res_data.get("base64") or res_data.get("response", {}).get("base64") or ""
                elif isinstance(res_data, str):
                    b64_str = res_data

                if b64_str:
                    if "," in b64_str:
                        b64_str = b64_str.split(",")[1]
                    return base64.b64decode(b64_str)
        except Exception as e:
            print(f"[Media Download] 方案 {i} 異常: {str(e)}")

    return None

def set_whatsapp_presence(instance_url: str, api_key: str, instance_name: str, remote_jid: str, presence: str = "composing"):
    """設定 WhatsApp 正在輸入狀態"""
    if not (instance_url and api_key and instance_name):
        return
    encoded_instance = urllib.parse.quote(instance_name)
    endpoint = f"{instance_url.rstrip('/')}/chat/sendPresence/{encoded_instance}"
    headers = {"apikey": api_key, "Content-Type": "application/json"}
    clean_number = remote_jid if "@g.us" in remote_jid else remote_jid.split("@")[0]
    payload = {"number": clean_number, "delay": 1200, "presence": presence}
    try:
        requests.post(endpoint, json=payload, headers=headers, timeout=5)
    except Exception as e:
        print(f"[WhatsApp Presence Error] {str(e)}")

def send_whatsapp_message(instance_url: str, api_key: str, instance_name: str, remote_jid: str, text: str):
    """發送 WhatsApp 文字訊息"""
    if not (instance_url and api_key and instance_name):
        print("未設定 EVOLUTION API 環境變數，訊息輸出如下：\n", text)
        return
    encoded_instance = urllib.parse.quote(instance_name)
    endpoint = f"{instance_url.rstrip('/')}/message/sendText/{encoded_instance}"
    headers = {"apikey": api_key, "Content-Type": "application/json"}
    clean_number = remote_jid if "@g.us" in remote_jid else remote_jid.split("@")[0] 
    payload = {"number": clean_number, "text": text}
    try:
        res = requests.post(endpoint, json=payload, headers=headers, timeout=10)
        print(f"[WhatsApp Send Status] {res.status_code}")
    except Exception as e:
        print(f"[WhatsApp Send Error] {str(e)}")

# ==============================================================================
# Agent 工具集
# ==============================================================================
def get_database_schema() -> str:
    """【資料庫結構查詢】：讓 AI 查看資料庫有哪些表與欄位名稱。"""
    try:
        import pandas as pd
        from sqlalchemy import create_engine, text

        try:
            engine = get_db_engine()
        except Exception as e:
            return f"ERROR: 資料庫連線失敗: {str(e)}"
        
        # 1. 依照表名與欄位實際順序 (ordinal_position) 排序
        sql = """
        SELECT table_name, column_name, data_type 
        FROM information_schema.columns 
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position;
        """
        with engine.connect() as connection:
            df = pd.read_sql_query(text(sql), connection)

        if df.empty:
            return "資料庫中沒有找到任何公開的表。"

        schema_dict = {}
        for _, row in df.iterrows():
            t_name = row['table_name']
            c_name = row['column_name']
            d_type = row['data_type']
            if t_name not in schema_dict:
                schema_dict[t_name] = []
            # 2. 自動在欄位名稱加上雙引號，引導 AI 寫出正確的 SQL
            schema_dict[t_name].append(f"{c_name} ({d_type})")

        # 3. 表名也加上雙引號
        # 修正後的寫法（將 {} 改為 {k}）
        schema_text = "\n".join([f'表名 "{k}": ' + ", ".join(v) for k, v in schema_dict.items()])

        
        # 4. 回傳字串中加入強提示，明確指示 AI 在寫 SQL 時必須用雙引號
        return f"資料庫結構如下（注意：PostgreSQL 對大小寫敏感，編寫 SQL 時所有表名與欄位名必須嚴格使用雙引號 \"\" 包裹）：\n{schema_text}"
    except Exception as e:
        return f"無法獲取資料庫結構: {str(e)}"

def execute_smart_sql(sql_query: str) -> str:
    """【執行 SQL 撈取數據】：讓 AI 執行 SELECT SQL 撈取數據...
    
    注意事項：
    1. 僅允許 SELECT 查詢，表名與欄位名必須嚴格加上雙引號。
    2. 末尾不要加分號 (;)。
    3. 💡【重要時間與去重規範】：
       - 查詢結果若有重複內容，必須使用 DISTINCT 或 GROUP BY 進行去重。
       - 只要查詢涉及時間戳記（Unix Timestamp，如 1787821964），
         必須在 SQL 中使用以下方式轉換為香港時間格式（GMT+8）：
         to_char(timezone('Asia/Hong_Kong', to_timestamp("時間欄位名")), 'YYYY-MM-DD HH24:MI:SS') AS "發送時間"
    """
    
    # 💡 修正點：在這裡先進行去空白與大小寫轉換，定義好變數
    clean_query = sql_query.strip()
    upper_query = clean_query.upper()

    # 接著再進行安全校驗
    if not (upper_query.startswith("SELECT") or upper_query.startswith("WITH")):
        return "ERROR: 安全限制，僅允許執行 SELECT 查詢（允許使用 WITH 子句）。"
        
    try:
        import pandas as pd
        from sqlalchemy import create_engine, text
        # ... 後續原有的資料庫連線與查詢邏輯保持不變 ...


        try:
            engine = get_db_engine()
        except Exception as e:
            return f"ERROR: 資料庫連線失敗: {str(e)}"
        
        # 💡 修復重點：先去除首尾空白與結尾的分號 (;)
        clean_sql = sql_query.strip().rstrip(';').strip()
        
        # 處理 SQLAlchemy % 轉義
        if "%" in clean_sql and "%%" not in clean_sql:
            clean_sql = clean_sql.replace("%", "%%")
            
        # 若未指定 LIMIT，安全地在末尾加上 LIMIT
        if "LIMIT" not in clean_sql.upper():
            clean_sql = f"{clean_sql} LIMIT 500000"

        with engine.connect() as connection:
            df = pd.read_sql_query(text(clean_sql), connection)

        df.fillna("-", inplace=True)

        csv_path = getattr(thread_context, "csv_path", "/tmp/cleaned_data_default.csv")
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        df.to_csv(csv_path, index=False)

        # 💡 先檢查與提取數據，暫不刪除 df
        if df.empty:
            return "SQL 執行成功，但沒有撈到任何資料。"

        total_count = len(df)
        records = df.head(50).to_dict(orient="records")

        # 💡 使用完畢後，在這裡才安全地回收記憶體
        del df
        import gc
        gc.collect()

        return (f"查詢成功，共撈取 {total_count} 筆資料，完整數據已暫存至當前會話點。\n"
                f"預覽前 {len(records)} 筆數據：\n{json.dumps(records, ensure_ascii=False, default=str)}")
    except Exception as e:
        return f"SQL 執行失敗: {str(e)}"

def analyze_and_extract_insights(analysis_instruction: str) -> str:
    """【數據深度分析】：對 execute_smart_sql 撈出來的 CSV 數據進行深度文本與營運分析。"""
    csv_path = getattr(thread_context, "csv_path", "/tmp/cleaned_data_default.csv")
    if not os.path.exists(csv_path):
        return "提示：目前暫存中沒有數據檔。請先呼叫 execute_smart_sql 撈取資料。"

    try:
        import pandas as pd
        df = pd.read_csv(csv_path)
        if df.empty:
            return "數據檔是空的，無法分析。"

        if len(df) > 500:
            df = df.head(500)

        data_json = df.to_json(orient="records", force_ascii=False)

        client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
        prompt = f"這是一份來自資料庫的數據（共 {len(df)} 筆）：\n\n{data_json}\n\n請根據以下指令進行深度分析：\n{analysis_instruction}"

        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3)
        )
        return f"深度分析結果：\n{response.text}"
    except Exception as e:
        return f"深度分析失敗: {str(e)}"

def send_file_to_whatsapp(report_title: str, summary_text: str, file_extension: str = "csv") -> str:
    """【發送多格式實體檔案至 WhatsApp】：支援 CSV, XLSX, PDF, PNG 等。"""
    base_path = getattr(thread_context, "csv_path", "/tmp/cleaned_data_default.csv")
    target_path = base_path.replace(".csv", f".{file_extension}") if file_extension != "csv" else base_path

    if not os.path.exists(target_path):
        target_path = base_path
        file_extension = "csv"

    if not os.path.exists(target_path):
        return f"ERROR: 找不到暫存數據檔 ({target_path})。"

    remote_jid = getattr(thread_context, "remote_jid", None)
    evo_url = getattr(thread_context, "evo_url", None)
    evo_key = getattr(thread_context, "evo_key", None)
    evo_instance = getattr(thread_context, "evo_instance", None)

    if not (remote_jid and evo_url and evo_key and evo_instance):
        return "ERROR: 傳送檔案失敗：未能取得當前對話的發送通道快取。"

    mime_type, _ = mimetypes.guess_type(f"file.{file_extension}")
    mime_type = mime_type or "application/octet-stream"
    media_type = "image" if mime_type.startswith("image/") else "document"

    try:
        with open(target_path, "rb") as f:
            file_base64 = base64.b64encode(f.read()).decode("utf-8")

        encoded_instance = urllib.parse.quote(evo_instance)
        endpoint = f"{evo_url.rstrip('/')}/message/sendMedia/{encoded_instance}"
        headers = {"apikey": evo_key, "Content-Type": "application/json"}
        clean_number = remote_jid if "@g.us" in remote_jid else remote_jid.split("@")[0]

        payload = {
            "number": clean_number,
            "mediatype": media_type,
            "mimetype": mime_type,
            "media": file_base64,
            "fileName": f"{report_title}.{file_extension}",
            "caption": f"📊 【檔案附件】：{report_title}\n\n{summary_text}"
        }
        res = requests.post(endpoint, json=payload, headers=headers, timeout=20)

        if res.status_code in [200, 201]:
            return f"實體報表檔案 **「{report_title}.{file_extension}」** 已成功發送至您的 WhatsApp 對話窗口！"
        else:
            return f"WhatsApp 附件傳送失敗，Evolution API 返回錯誤: {res.text}"
    except Exception as e:
        return f"傳送實體檔案至 WhatsApp 時發生異常: {str(e)}"

def send_email_message(subject: str, recipient_email: str, body_text: str, attach_csv: bool = False) -> str:
    """【僅用於寄送電子郵件】：使用 SMTP 寄送電子郵件給指定收件人。"""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication

    print(f"[Email Tool] 正在嘗試發送郵件給 {recipient_email}，主題: {subject}")
    sender_email = get_secret("GMAIL_USER")
    app_password = get_secret("GMAIL_APP_PASSWORD")
    if not (sender_email and app_password):
        return "ERROR: 系統未配置 GMAIL_USER 或 GMAIL_APP_PASSWORD 密鑰，無法發送郵件。"
    if not recipient_email:
        return "ERROR: 必須指定收件者電子郵箱。"

    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = sender_email
        msg["To"] = recipient_email

        formatted_body = body_text.replace('\n', '<br>')
        html_body = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px; color: #333;">
          <h2 style="color: #1a73e8;">✉️ 【數智小幫手】電子郵件通知</h2>
          <hr style="border: 1px solid #eee;">
          <div style="line-height: 1.6; font-size: 14px;">{formatted_body}</div>
        </div>
        """
        msg_alternative = MIMEMultipart("alternative")
        msg_alternative.attach(MIMEText(html_body, "html"))
        msg.attach(msg_alternative)

        if attach_csv:
            csv_path = getattr(thread_context, "csv_path", "/tmp/cleaned_data_default.csv")
            if os.path.exists(csv_path):
                try:
                    with open(csv_path, "rb") as f:
                        attachment = MIMEApplication(f.read(), Name="data_report.csv")
                        attachment['Content-Disposition'] = 'attachment; filename="data_report.csv"'
                        msg.attach(attachment)
                    print("[Email Tool] 成功在郵件中附加實體 CSV 檔案。")
                except Exception as att_err:
                    print(f"[Email Attachment Error] {str(att_err)}")

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, app_password)
            server.sendmail(sender_email, recipient_email, msg.as_string())

        attach_status = "（內含 CSV 數據附件）" if attach_csv else "（無附件）"
        print(f"[Email Tool Success] 郵件已成功寄送至 {recipient_email}")
        return f"郵件已成功寄送至 {recipient_email} {attach_status}！"
    except Exception as e:
        print(f"[Email Tool Error] 發送郵件失敗: {str(e)}")
        return f"發送電子郵件時發生錯誤: {str(e)}"

def list_google_drive_files(search_query: str = "") -> str:
    """【列出 Drive 檔案】：列出或搜尋 Google Drive 中的檔案清單與 ID。"""
    try:
        import google.auth
        from googleapiclient.discovery import build
        creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/drive.readonly'])
        drive_service = build('drive', 'v3', credentials=creds)
        q = "trashed = false"
        if search_query:
            q += f" and name contains '{search_query}'"
        results = drive_service.files().list(
            q=q,
            pageSize=15,
            fields="nextPageToken, files(id, name, mimeType, modifiedTime)"
        ).execute()
        items = results.get('files', [])
        if not items:
            return "Google Drive 中未找到相關檔案。"
        file_list = [f"📄 ID: {item['id']} | 名稱: {item['name']} | 類型: {item['mimeType']}" for item in items]
        return "【Google Drive 檔案列表】:\n" + "\n".join(file_list)
    except Exception as e:
        return f"讀取 Google Drive 失敗: {str(e)}"

def read_google_drive_file(file_id: str) -> str:
    """【讀取 Drive 一般檔案】：讀取指定 Google Drive 檔案的內容。"""
    try:
        import google.auth
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload
        creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/drive.readonly'])
        drive_service = build('drive', 'v3', credentials=creds)
        file_meta = drive_service.files().get(fileId=file_id, fields="id, name, mimeType").execute()
        mime_type = file_meta.get("mimeType", "")
        file_name = file_meta.get("name", "")
        if mime_type == "application/vnd.google-apps.document":
            request = drive_service.files().export_media(fileId=file_id, mimeType='text/plain')
        else:
            request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.seek(0)
        content = fh.read().decode('utf-8', errors='ignore')
        return f"【檔名】: {file_name}\n【類型】: {mime_type}\n【檔案內容前 3000 字】:\n{content[:3000]}"
    except Exception as e:
        return f"無法讀取該 Google Drive 檔案 ({file_id}): {str(e)}"

def search_google_drive(query_filename: str) -> str:
    """【模糊搜尋 Drive 檔案】：關鍵字搜尋 Google Drive 檔案名稱並獲取 ID。"""
    try:
        import google.auth
        from googleapiclient.discovery import build
        creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/drive.readonly'])
        drive_service = build('drive', 'v3', credentials=creds)
        q_filter = f"name contains '{query_filename}' and trashed = false"
        results = drive_service.files().list(
            q=q_filter,
            spaces='drive',
            fields="files(id, name, mimeType)",
            pageSize=10
        ).execute()
        items = results.get('files', [])
        if not items:
            return f"未找到任何名稱包含「{query_filename}」的檔案。"
        files_list = [f"📄 檔名: {item['name']}\n ID: {item['id']}\n 類型: {item['mimeType']}\n" for item in items]
        return f"在 Google Drive 找到以下相關檔案：\n\n" + "\n".join(files_list)
    except Exception as e:
        return f"搜尋 Google Drive 檔案時發生錯誤: {str(e)}"

def get_google_sheet_tabs(spreadsheet_id: str) -> str:
    """【獲取試算表分頁清單】：查詢 Google 試算表內包含的所有分頁名稱。"""
    try:
        import google.auth
        from googleapiclient.discovery import build
        creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/spreadsheets.readonly', 'https://www.googleapis.com/auth/drive.readonly'])
        sheets_service = build('sheets', 'v4', credentials=creds)
        spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets = spreadsheet.get('sheets', [])
        titles = [sheet['properties']['title'] for sheet in sheets]
        return f"該試算表包含以下分頁：\n" + "\n".join([f"- {t}" for t in titles])
    except Exception as e:
        return f"獲取試算表分頁失敗: {str(e)}"

def read_google_sheet_content(spreadsheet_id: str, range_name: str = "Sheet1!A1:Z100") -> str:
    """【讀取 Google 試算表內容】：讀取指定的試算表與範圍。"""
    try:
        import google.auth
        from googleapiclient.discovery import build
        creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/spreadsheets.readonly', 'https://www.googleapis.com/auth/drive.readonly'])
        sheets_service = build('sheets', 'v4', credentials=creds)
        result = sheets_service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range_name).execute()
        rows = result.get('values', [])
        if not rows:
            return f"試算表（ID: {spreadsheet_id}）中沒有數據。"
        formatted_rows = [f"Row {idx+1}: {str(row)}" for idx, row in enumerate(rows)]
        return f"成功讀取試算表（範圍: {range_name}）：\n\n" + "\n".join(formatted_rows)
    except Exception as e:
        return f"讀取 Google 試算表失敗: {str(e)}"

def write_to_google_sheet(spreadsheet_id: str, range_name: str, values_json: str) -> str:
    """【覆寫 Google 試算表】：覆寫指定範圍數據。values_json 須為二維陣列 JSON。"""
    try:
        import google.auth
        from googleapiclient.discovery import build
        creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive'])
        sheets_service = build('sheets', 'v4', credentials=creds)
        values = json.loads(values_json)
        if not isinstance(values, list):
            return "寫入失敗：values_json 必須是二維陣列 JSON。"
        body = {'values': values}
        result = sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption="USER_ENTERED",
            body=body
        ).execute()
        return f"成功寫入試算表（ID: {spreadsheet_id}），更新了 {result.get('updatedCells', 0)} 個儲存格！"
    except Exception as e:
        return f"寫入 Google 試算表時發生錯誤: {str(e)}"

def append_to_google_sheet(spreadsheet_id: str, range_name: str, values_json: str) -> str:
    """【追加資料到試算表末尾】：自動尋找表格最後一行追加資料。values_json 須為二維陣列 JSON。"""
    try:
        import google.auth
        from googleapiclient.discovery import build
        creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive'])
        sheets_service = build('sheets', 'v4', credentials=creds)
        values = json.loads(values_json)
        if not isinstance(values, list):
            return "追加失敗：values_json 必須是二維陣列 JSON。"
        body = {'values': values}
        result = sheets_service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()
        return f"成功追加資料至試算表！新增了 {result.get('updates', {}).get('updatedRows', 0)} 行紀錄。"
    except Exception as e:
        return f"追加資料失敗: {str(e)}"

def google_web_search(search_query: str) -> str:
    """【聯網搜尋工具】：透過單獨啟用含有 Google 搜尋接地（Grounding）的 Gemini 模型來獲取最新資訊。"""
    try:
        # 建立一個單獨的 Gemini 客戶端，專門用來跑 Google 搜尋（避開與自訂工具的衝突）
        client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
        
        # 只啟用官方 Google 搜尋工具
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.3
        )
        
        prompt = f"請幫我用 Google 搜尋關於「{search_query}」的最新資訊，並詳細整理內容與摘要。"
        
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=config
        )
        
        if response.text:
            return response.text
        else:
            return f"聯網搜尋成功，但未能獲取關於「{search_query}」的具體內容。"
            
    except Exception as e:
        return f"聯網搜尋失敗: {str(e)}"

# ==============================================================================
# Dynamic Tool Registry
# ==============================================================================
TOOL_MAP = {
    "get_database_schema": get_database_schema,
    "execute_smart_sql": execute_smart_sql,
    "analyze_and_extract_insights": analyze_and_extract_insights,
    "send_file_to_whatsapp": send_file_to_whatsapp,
    "send_email_message": send_email_message,
    "list_google_drive_files": list_google_drive_files,
    "read_google_drive_file": read_google_drive_file,
    "search_google_drive": search_google_drive,
    "get_google_sheet_tabs": get_google_sheet_tabs,
    "read_google_sheet_content": read_google_sheet_content,
    "write_to_google_sheet": write_to_google_sheet,
    "append_to_google_sheet": append_to_google_sheet,
    "google_web_search": google_web_search
}

# ==============================================================================
# 工具執行即時提示對照表 (Progress Messages)
# ==============================================================================
TOOL_PROGRESS_MSG = {
    "get_database_schema": "📋 正在查看資料庫結構與欄位資訊...",
    "execute_smart_sql": "🔍 正在為您執行 SQL 撈取資料庫數據，請稍候...",
    "analyze_and_extract_insights": "🧠 數據撈取完畢，正在進行深度智能分析與彙整...",
    "send_file_to_whatsapp": "📊 正在將生成的報表/檔案打包並發送至 WhatsApp...",
    "send_email_message": "✉️ 正在整理報告內容並透過電子郵件寄出...",
    "list_google_drive_files": "📁 正在檢索 Google Drive 中的檔案清單...",
    "read_google_drive_file": "📄 正在讀取並解析指定的 Google Drive 檔案內容...",
    "search_google_drive": "🔎 正在 Google Drive 中搜尋符合關鍵字的檔案...",
    "get_google_sheet_tabs": "📑 正在檢查 Google 試算表的分頁清單...",
    "read_google_sheet_content": "📊 正在讀取 Google 試算表內容...",
    "write_to_google_sheet": "✏️ 正在將資料寫入 Google 試算表...",
    "append_to_google_sheet": "➕ 正在將新資料追加至 Google 試算表末尾...",
    "google_web_search": "🌐 正在聯網搜尋最新即時資訊，請稍候..."
}

# ==============================================================================
# System Instruction
# ==============================================================================
SYSTEM_INSTRUCTION = """
你是一個「極度靈活且聰明的營運 AI 助手」。你連接了 PostgreSQL 資料庫與 Google Drive / Sheets 雲端服務，並具備多模態（文字、表格、圖片、各式文件）分析能力。

【👋 對話與回應規範】
1. **自然對答**：若用戶的訊息帶有明確需求、具體問題或特定的測試指令（例如「總結重點」、「幫我查資料」），請【直接且自然地回應對方的具體需求】，切勿套用無關的罐頭招呼語。
2. **純招呼語引導**：只有當用戶傳送純粹的開場白或無特定目的之招呼（例如單純傳送「你好」、「Hi」）時，才簡短回應運作正常並詢問可提供什麼協助。
3. **語言與風格**：請使用繁體中文，態度專業親切、條理清晰。

【🛠️ 你的核心工具與工作流】
1. 資料庫查詢規範（非常重要）：
   - 先調用 `get_database_schema` 確認表名與欄位名。
   - ⚠️ **PostgreSQL 大小寫嚴格規範**：本資料庫包含駝峰式或大寫命名的表與欄位（例如 `Message`、`messageTimestamp`、`remoteJid` 等）。在撰寫 SQL 時，**所有的表名與欄位名必須一律使用雙引號 `""` 包裹**！
     例如：`SELECT "id", "messageTimestamp", "conversation" FROM "Message" WHERE ...`
   - ⚠️ SQL 語句末尾**切勿加上分號 `;`**。
2. 圖片與文件解析：若用戶傳送圖片或附帶文件 (.docx, .xlsx, .csv, .pdf, .pptx)，請結合文字內容及多模態圖片進行理解與摘要。
3. 數據分析：對撈出的數據執行 `analyze_and_extract_insights` 進行深度營運洞察。
4. 寄送 WhatsApp 檔案：當用戶要求「生成報表/表格」、「導出 CSV/Excel 檔案」發送到 WhatsApp 時，請呼叫 `send_file_to_whatsapp` 工具。
5. 寄送 Email 郵件：當用戶要求「發送電子郵件」或「寄出報告」時，【必須實際調用 `send_email_message` 工具】，切勿在未調用工具時聲稱已寄出！如果用戶需要將資料庫數據隨信發送，請設定 `attach_csv=True`。
6. Google Drive / 試算表操作：使用對應的 search / read / append / write 工具進行操作。
7. 聯網搜尋：當用戶詢問即時資訊、最新新聞、外部行業標準、匯率或非本系統內的公開知識時，請調用 `google_web_search` 進行檢索並結合搜尋結果回覆。

【🚨 運作鐵律：生成報表與發送檔案的先決條件】
若涉及資料庫報表寄送，必須先調用 `execute_smart_sql` 撈取數據產檔，確認成功後才能調用 `send_file_to_whatsapp` 或 `send_email_message`！
"""

# ==============================================================================
# Agent 思考與執行引擎 (已修復 AFC 衝突與 Function Call 循環)
# ==============================================================================
def process_agent_task(remote_jid: str, text_prompt: str, media_parts: list, evo_url: str, evo_key: str, evo_instance: str):
    thread_context.remote_jid = remote_jid
    thread_context.evo_url = evo_url
    thread_context.evo_key = evo_key
    thread_context.evo_instance = evo_instance

    clean_jid = re.sub(r"[^\w]", "_", remote_jid)
    thread_context.csv_path = f"/tmp/cleaned_data_{clean_jid}.csv"

    try:
        set_whatsapp_presence(evo_url, evo_key, evo_instance, remote_jid, "composing")
        client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)

        # 1. 載入 Firestore 歷史紀錄
        history = get_session_history_firestore(remote_jid, limit=20)

        # 2. 構建當前輪次內容
        current_user_parts = []
        if text_prompt:
            current_user_parts.append(types.Part.from_text(text=text_prompt))
        for part in media_parts:
            current_user_parts.append(part)

        contents = []
        for h in history:
            contents.append(types.Content(role=h["role"], parts=[types.Part.from_text(text=p["text"]) for p in h["parts"] if "text" in p]))

        contents.append(types.Content(role="user", parts=current_user_parts))

        tools = list(TOOL_MAP.values())
        
        # 🔑 核心修復：停用 SDK 內部的自動調用 (AFC)，強制由我們手動迴圈控制 Tool 調用
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.2
        )

        final_reply = ""
        notified_tools = set() # 👈 1. 新增：記錄當前請求已經通知過哪些工具

        for iteration in range(8):
            # 💡 恢復正確的呼叫參數！
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=contents,
                config=config
            )
            
            if response.candidates and response.candidates[0].content:
                contents.append(response.candidates[0].content)

            if response.function_calls:

                HEAVY_TOOLS = {"execute_smart_sql", "analyze_and_extract_insights", "google_web_search", "send_file_to_whatsapp", "send_email_message"}
        
                for function_call in response.function_calls:
                    fn_name = function_call.name
                    fn_args = function_call.args or {}
                    print(f"[Agent Tool Call] 執行工具: {fn_name}, 參數: {fn_args}")
          
                    # 保持輸入中的狀態 (不會洗版用戶對話框)
                    set_whatsapp_presence(evo_url, evo_key, evo_instance, remote_jid, "composing")
          
                    # 👈 2. 僅在第一次執行該工具時發送通知，後續 AI 自動重試時不再重複發送
                    if fn_name in HEAVY_TOOLS and fn_name in TOOL_PROGRESS_MSG and fn_name not in notified_tools:
                        send_whatsapp_message(evo_url, evo_key, evo_instance, remote_jid, TOOL_PROGRESS_MSG[fn_name])
                        notified_tools.add(fn_name) # 標記為已通知

                    if fn_name in TOOL_MAP:
                        try:
                            tool_result = TOOL_MAP[fn_name](**fn_args)
                        except Exception as te:
                            tool_result = f"ERROR: 執行 {fn_name} 時拋出例外: {str(te)}"
                    else:
                        tool_result = f"ERROR: 未知的工具 {fn_name}"

                    print(f"[Agent Tool Result] {fn_name} 執行結果預覽: {str(tool_result)[:100]}...")
                    contents.append(types.Content(
                        role="user",
                        parts=[types.Part.from_function_response(name=fn_name, response={"result": tool_result})]
                    ))
                    time.sleep(1)
            else:
                final_reply = response.text
                break

        if not final_reply:
            final_reply = "抱歉，處理您的請求時超出了最大思考輪數，請稍後再試。"

        send_whatsapp_message(evo_url, evo_key, evo_instance, remote_jid, final_reply)
        summary_text = text_prompt if text_prompt else "[用戶傳送了媒體檔案]"
        save_session_history_firestore(remote_jid, summary_text, final_reply)

    except Exception as e:
        print(f"[Agent Execution Error] {str(e)}")
        send_whatsapp_message(evo_url, evo_key, evo_instance, remote_jid, f"抱歉，系統遇到問題：{str(e)}")

# ==============================================================================
# Pub/Sub Worker 觸發進入點 (CloudEvent)
# ==============================================================================
@functions_framework.cloud_event
def process_pubsub_task(cloud_event: CloudEvent):
    """Pub/Sub 事件接收進入點"""
    try:
        pubsub_message = cloud_event.data.get("message", {})
        if not pubsub_message.get("data"):
            return

        raw_json_str = base64.b64decode(pubsub_message["data"]).decode("utf-8")
        req_json = json.loads(raw_json_str)

        data = req_json.get("data", {})
        key = data.get("key", {})
        remote_jid = key.get("remoteJid", "")
        message_id = key.get("id", "")
        participant = data.get("participant", "") or key.get("participant", "")
        chat_jid = data.get("chatJid", "")

        # 1. 解包可能套疊的 Message 結構
        message = data.get("message", {})
        for wrapper in ["ephemeralMessage", "viewOnceMessage", "documentWithCaptionMessage", "editedMessage"]:
            if isinstance(message, dict) and wrapper in message and "message" in message[wrapper]:
                message = message[wrapper]["message"]

        # 2. 重置對話指令處理 (/reset)
        temp_text = (
            message.get("conversation") or 
            message.get("extendedTextMessage", {}).get("text") or ""
        ).strip().lower()
        
        # 🛑 安全防護版：重置對話指令處理 (/reset)
        if temp_text in ["/reset", "重置對話", "清除紀錄", "clear"]:
            try:
                get_firestore().collection("whatsapp_sessions").document(urllib.parse.quote_plus(remote_jid)).delete()
            except Exception as fe:
                print(f"[Firestore Warning] 重置對話紀錄失敗: {str(fe)}")
                
            send_whatsapp_message(get_secret("EVOLUTION_API_URL"), get_secret("EVOLUTION_API_KEY"), get_secret("EVOLUTION_INSTANCE_NAME"), remote_jid, "🧹 已成功為您重置對話紀錄！")
            return

        # 3. 提取文字內容與群組判定
        text_prompt = (
            message.get("conversation") or 
            message.get("extendedTextMessage", {}).get("text") or 
            message.get("imageMessage", {}).get("caption") or 
            message.get("documentMessage", {}).get("caption") or 
            message.get("videoMessage", {}).get("caption") or ""
        ).strip()

        is_group = (
            "@g.us" in remote_jid or 
            "@g.us" in participant or 
            "@g.us" in chat_jid
        )

        bot_phone = get_secret("BOT_PHONE_NUMBER") or ""
        clean_bot_phone = re.sub(r"\D", "", bot_phone)

        # 4. 群組 Mention 過濾
        if is_group:
            context_info = {}
            for msg_type in ["extendedTextMessage", "imageMessage", "documentMessage", "videoMessage", "audioMessage"]:
                if isinstance(message, dict) and msg_type in message and "contextInfo" in message[msg_type]:
                    context_info = message[msg_type]["contextInfo"] or {}
                    break

            mentioned_jids = context_info.get("mentionedJid", []) or []
            is_mentioned = False

            if clean_bot_phone:
                short_bot_phone = clean_bot_phone[-8:] if len(clean_bot_phone) >= 8 else clean_bot_phone
                is_in_jid = any(clean_bot_phone in str(jid) or short_bot_phone in str(jid) for jid in mentioned_jids)
                is_in_text = f"@{clean_bot_phone}" in text_prompt or f"@{short_bot_phone}" in text_prompt
                quoted_participant = context_info.get("participant", "")
                is_reply_to_bot = (clean_bot_phone in quoted_participant or short_bot_phone in quoted_participant) if quoted_participant else False
                is_mentioned = is_in_jid or is_in_text or is_reply_to_bot

            if not is_mentioned:
                return

            if clean_bot_phone:
                short_bot_phone = clean_bot_phone[-8:] if len(clean_bot_phone) >= 8 else clean_bot_phone
                text_prompt = re.sub(rf"@{clean_bot_phone}\b", "", text_prompt).strip()
                text_prompt = re.sub(rf"@{short_bot_phone}\b", "", text_prompt).strip()

            text_prompt = re.sub(r"@\d+", "", text_prompt).strip()
            sender_name = data.get("pushName") or (participant.split("@")[0] if participant else "群成員")
            text_prompt = f"[{sender_name}]: {text_prompt}" if text_prompt else f"[{sender_name} 發送了檔案/媒體]"

        # 5. 下載與處理媒體/附件 (多模態 + DOCX 內嵌圖片深度提取)
        evo_url = get_secret("EVOLUTION_API_URL")
        evo_key = get_secret("EVOLUTION_API_KEY")
        evo_instance = get_secret("EVOLUTION_INSTANCE_NAME")

        media_parts = []
        media_type = None
        for k in ["imageMessage", "audioMessage", "documentMessage", "videoMessage"]:
            if isinstance(message, dict) and k in message:
                media_type = k
                break

        raw_bytes = None
        mime_type = "image/jpeg"
        if media_type and message_id:
            raw_bytes = download_evolution_media(evo_url, evo_key, evo_instance, message)
            mime_type = message[media_type].get("mimetype", "image/jpeg")

        # Base64 解密備援
        if not raw_bytes:
            base64_data = (
                req_json.get("base64") or 
                data.get("base64") or 
                message.get("base64") or 
                (message.get("imageMessage", {}) or {}).get("base64") or 
                (message.get("documentMessage", {}) or {}).get("base64") or 
                (message.get("audioMessage", {}) or {}).get("base64") or
                (message.get("videoMessage", {}) or {}).get("base64")
            )
            if base64_data:
                if "," in base64_data:
                    base64_data = base64_data.split(",")[1]
                raw_bytes = base64.b64decode(base64_data)

        # 6. 文件轉譯與多模態圖片注入
        if raw_bytes:
            ext = ""
            mime_lower = mime_type.lower()
            file_name = message.get("documentMessage", {}).get("fileName", "").lower()
            
            if "wordprocessingml.document" in mime_lower or file_name.endswith(".docx"): ext = ".docx"
            elif "spreadsheetml" in mime_lower or "excel" in mime_lower or file_name.endswith(".xlsx"): ext = ".xlsx"
            elif "csv" in mime_lower or mime_lower == "text/csv" or file_name.endswith(".csv"): ext = ".csv"
            elif "pdf" in mime_lower or file_name.endswith(".pdf"): ext = ".pdf"
            elif "presentationml.presentation" in mime_lower or file_name.endswith(".pptx"): ext = ".pptx"
            elif "plain" in mime_lower or file_name.endswith(".txt"): ext = ".txt"

            if ext == ".docx":
                # 📄 雙重保障：1. 提取所有文字 2. 提取所有內嵌圖片 (SOP 截圖)
                extracted_text = docx_to_text(raw_bytes)
                if not extracted_text.strip():
                    extracted_text = convert_doc_to_markdown(raw_bytes, ".docx")

                # 🖼️ 自動提取 Word 裡的截圖傳給 Gemini 多模態
                docx_imgs = extract_docx_images(raw_bytes)
                for img_b, img_m in docx_imgs:
                    media_parts.append(types.Part.from_bytes(data=img_b, mime_type=img_m))

                if extracted_text.strip():
                    text_prompt = (
                        f"{text_prompt}\n\n"
                        f"----------------【附屬 Word 文件文字內容】----------------\n"
                        f"{extracted_text}\n"
                        f"----------------------------------------------------------\n"
                        f"(註：本文件附帶了 {len(docx_imgs)} 張內嵌圖片/截圖，已作為多模態輸入一併提供給您)"
                    ).strip()
                elif docx_imgs:
                    text_prompt = f"{text_prompt}\n\n(註：此 Word 檔內含 {len(docx_imgs)} 張截圖/流程圖，請根據圖片進行識別與總結)"
            
            elif ext:
                # 其他格式文件 (Excel, PDF 等)
                extracted_md = convert_doc_to_markdown(raw_bytes, ext)
                if extracted_md and extracted_md.strip():
                    text_prompt = (
                        f"{text_prompt}\n\n"
                        f"----------------【附屬文件內容 ({ext})】----------------\n"
                        f"{extracted_md}\n"
                        f"----------------------------------------------------"
                    ).strip()
            else:
                # 一般圖片、音訊、影片走 Gemini 原生多模態
                media_parts.append(types.Part.from_bytes(data=raw_bytes, mime_type=mime_type))

        # 7. 啟動 Agent 思考流程
        process_agent_task(remote_jid, text_prompt, media_parts, evo_url, evo_key, evo_instance)
        print(f"[Worker Success] 成功完成任務：{remote_jid}")

    except Exception as e:
        print(f"[Worker Error] 處理失敗: {str(e)}")
        raise e
