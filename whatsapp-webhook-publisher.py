import os
import json
import functions_framework
from flask import jsonify
from google.cloud import pubsub_v1, firestore

# ==============================================================================
# 環境變數與 GCP 客戶端初始化
# ==============================================================================
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
TOPIC_ID = os.environ.get("PUBSUB_TOPIC", "whatsapp-tasks")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
ALLOWED_NUMBERS = [n.strip() for n in os.environ.get("ALLOWED_NUMBERS", "").split(",") if n.strip()]

publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(PROJECT_ID, TOPIC_ID)

db_firestore = None
def get_firestore():
    global db_firestore
    if db_firestore is None:
        db_firestore = firestore.Client(project=PROJECT_ID, database="default") 
    return db_firestore

@functions_framework.http
def webhook(request):
    try:
        # 1. 驗證 Webhook Token
        token = request.headers.get("X-Webhook-Secret") or request.headers.get("Authorization")
        if WEBHOOK_SECRET and (token != f"Bearer {WEBHOOK_SECRET}" and token != WEBHOOK_SECRET):
            return jsonify({"error": "Unauthorized"}), 401

        req_json = request.get_json(force=True, silent=True) or {}
        if not req_json:
            raw_bytes = request.get_data()
            if raw_bytes:
                try:
                    req_json = json.loads(raw_bytes.decode('utf-8'))
                except Exception:
                    pass
        if not req_json:
            return ("OK", 200)

        data = req_json.get("data", {})
        key = data.get("key", {})
        remote_jid = key.get("remoteJid", "")
        from_me = key.get("fromMe", False)
        message_id = key.get("id", "")

        # 忽略機器人自己發送的訊息與空對話
        if from_me or not remote_jid:
            return ("OK", 200)

        # 2. 白名單檢查
        if ALLOWED_NUMBERS:
            clean_num = remote_jid.split("@")[0].replace("+", "")
            if clean_num not in ALLOWED_NUMBERS:
                return jsonify({"error": "Forbidden"}), 403

        # 3. 訊息去重檢查 (Firestore)
        if message_id:
            db = get_firestore()
            doc_ref = db.collection("processed_messages").document(message_id)
            if doc_ref.get().exists:
                return ("OK", 200)
            doc_ref.set({"processed_at": firestore.SERVER_TIMESTAMP})

        # 4. 推送至 Pub/Sub Queue
        payload_bytes = json.dumps(req_json).encode("utf-8")
        future = publisher.publish(topic_path, data=payload_bytes)
        future.result()

        print(f"[Publisher Success] 訊息 {message_id} 已成功推送到 Pub/Sub: {topic_path}")
        return ("OK", 200)

    except Exception as e:
        print(f"[Publisher Error]: {str(e)}")
        return ("Internal Error", 500)
