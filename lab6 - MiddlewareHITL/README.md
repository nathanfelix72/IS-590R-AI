# Lab 6 extension — HITL dashboard + Slack approvals

Same support graph as `middlewareHITLMAS.py` (sanitize → triage → refund with `HumanInTheLoopMiddleware`), plus:

- a live web dashboard for progress
- Slack messages with **Approve / Edit amount / Reject**
- dashboard fallback buttons if Slack isn’t configured

## Quick start

```bash
cd "lab6 - MiddlewareHITL"
# from repo venv
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Open http://localhost:8000

## LangSmith tracing

Same setup as lab5. Put these in `.env` (loaded **before** LangChain imports):

```
LANGSMITH_TRACING=true
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_API_KEY=lsv2_pt_...
LANGSMITH_PROJECT=ai-class-lab-3
```

On startup you should see `LangSmith tracing=on project=...`. Traces show up at [smith.langchain.com](https://smith.langchain.com).

## Slack “approve in Slack”

Posting messages only needs the bot token. **Button clicks need a public URL**
(Slack cannot call `localhost`).

1. Copy `.env.example` → `.env` and fill in Slack values
   (`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_ID`).
2. Run the dashboard: `uvicorn app:app --reload --port 8000`
3. In another terminal, expose it:

   ```bash
   ngrok http 8000
   ```

4. In the Slack app settings ([api.slack.com/apps](https://api.slack.com/apps)):
   - **Interactivity & Shortcuts** → On
   - **Request URL:** `https://<ngrok-host>/slack/interactions`
   - Bot scopes: `chat:write` (and `chat:write.public` if needed)
5. Invite the bot to `SLACK_CHANNEL_ID` (`/invite @YourBot`).
6. Restart uvicorn after any `.env` change.
7. Run a **billing** sample so the graph interrupts on `issue_refund`.

If clicks show a Slack error and your server never logs
`Slack interaction request received`, the Request URL / ngrok setup is wrong.

Approve / edit / reject in Slack resumes the same LangGraph thread the dashboard is watching.

## CLI still works

```bash
python middlewareHITLMAS.py
```
