# Hi, I'm Wilson Cho 👋

**MarTech & Marketing Ops · AI Automation · Hong Kong**

I build AI tools that take repetitive work off marketing and operations teams. My background is marketing operations, so I start from the business problem: who is waiting on data, how long it takes, and what the team does with it. Then I build the automation around that.

---

## 🚀 Featured Project

### [AI-Driven Business Operations & Marketing Intelligence Suite](https://github.com/wilkyc/whatsapp-sentiment-ai)
Two systems built on Google Cloud and Gemini and used in real daily operations.

* **🤖 WhatsApp AI Operations Assistant.** Non-technical staff ask questions in WhatsApp. The assistant queries the database with safe read-only Text-to-SQL, reads images and documents, runs sentiment analysis, and sends back CSV files and email reports. [▶️ Watch the 2-min demo](https://github.com/wilkyc/whatsapp-sentiment-ai#-demo-video-213)
* **📊 Community Sentiment & NLP Pipeline.** An hourly job that turns raw WhatsApp group chats into brand-level sentiment data for 35 brand and sub-brand columns, feeding dashboards and negative-sentiment alerts. The keyword system (193 brand rules, 263 context and exclusion words) lives in Google Sheets.
* **🔁 Self-improving keyword loop.** A separate AI agent (built on Manus, connected to Google Workspace and Supabase) reviews the pipeline's output, finds missed or wrong matches, and updates the keyword sheet. The next hourly run uses the new rules, so accuracy keeps improving without manual tuning.
* **Impact:** daily data processing cut from 2–3 hours to about 15 minutes; teams get answers without writing a single query.

*I designed and wrote all the application code. The underlying message database was provided by the company.*

---

## 🛠️ Skills

* **AI & LLM:** Gemini (Vertex AI), GPT, Grok, prompt engineering, LLM classification with anti-hallucination guardrails, multi-modal document & image understanding
* **AI Agents & Coding Tools:** Manus, Grok Bot, Codex. I use AI agents for building, automating and operating workflows
* **Cloud & Automation:** Google Cloud (Cloud Run, Cloud Functions, Pub/Sub, Firestore, Secret Manager), GitHub Actions, WhatsApp gateway (Evolution API)
* **Data:** Python, Pandas, PostgreSQL, Supabase, SQL, Google Sheets & Workspace APIs
* **Marketing Ops:** social listening, brand sentiment tracking, reporting automation, CRM & community operations

---

## 🌐 Languages

Cantonese (native) · Mandarin (native) · English (working)

## 📫 Contact

[LinkedIn](https://linkedin.com/in/wilkyc) · [wilsonchokaiyun@gmail.com](mailto:wilsonchokaiyun@gmail.com)
