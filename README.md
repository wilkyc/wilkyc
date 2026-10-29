# Hi, I'm Wilson Cho 👋

**MarTech & Marketing Ops · AI Automation · Hong Kong**

I build AI tools that take repetitive work off marketing and operations teams. My background is marketing operations, so I start from the business problem: who is waiting on data, how long it takes, and what the team does with it. Then I build the automation around that.

---

## 🚀 Featured Project

### [AI-Driven Business Operations & Marketing Intelligence Suite](https://github.com/wilkyc/whatsapp-sentiment-ai)
Two systems built on Gemini and used in real daily operations: a real-time WhatsApp AI ops assistant, and a community sentiment & NLP batch pipeline.

* **⏱️ Hourly ops + negative alerts.** The sentiment pipeline runs about hourly via an external timer calling GitHub Actions `workflow_dispatch` (no GitHub `schedule`). Negative (N) labels can notify owners over WhatsApp or email so PR can adjust quickly.
* **📊 Live Google Sheets board.** Fixed dashboards cover discussion volume, positive / negative / inquiry mix, and activity.
* **🧵 Dialogue id.** Related messages in a thread share one dialogue id, so a review pass can read the whole conversation at once instead of isolated lines.
* **🔁 Keyword loop + program sentiment.** A separate AI agent reviews pipeline output, finds missed or wrong keyword matches, and updates the keyword sheet for the next run. Measurement has two layers: **hit rate** (keyword — did we catch the in-scope conversations?) and **accuracy** (program — I/N/P brand sentiment). Sentiment is classified by the pipeline program; the keyword loop improves the inputs that feed it. After human review of ~787 labels and multi-round tuning of keywords and the program on the same-day data, **program sentiment accuracy** is about **90–95%** (e.g. 6 errors in a 74-label sample). Production data lands in Supabase as well as Sheets.

* **Impact:** daily data processing cut from 2–3 hours to about 15 minutes; teams get answers without writing a single query.

*I designed and wrote all the application code. The company provided the WhatsApp message intake channel for the assistant, and the data collection layer and underlying message database for the pipeline.*

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
