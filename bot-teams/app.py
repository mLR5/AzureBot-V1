import os
import json
import logging
from aiohttp import web
from botbuilder.core import (
    BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext, ActivityHandler
)
from botbuilder.schema import Activity
import requests

# --- Logging de base ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot-app")

# --- Config ---
APP_ID = os.getenv("MicrosoftAppId")
APP_PASSWORD = os.getenv("MicrosoftAppPassword")

# Appel texte → ta function “chat” existante
FUNCTION_APP_URL = os.getenv("FUNCTION_APP_URL")

# AJOUT : Appel analyse → ta function /api/analyze protégée par une function key
ANALYZE_URL = os.getenv("ANALYZE_URL")

if not APP_ID or not APP_PASSWORD:
    log.warning("MicrosoftAppId/MicrosoftAppPassword non définis. L’auth Bot échouera.")
if not FUNCTION_APP_URL:
    log.warning("FUNCTION_APP_URL non défini. Les appels backend (chat) échoueront.")
if not ANALYZE_URL:
    log.warning("ANALYZE_URL non défini. L'analyse de fichiers échouera.")

# --- Adapter + gestion d'erreurs globales ---
adapter_settings = BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD)
adapter = BotFrameworkAdapter(adapter_settings)

async def on_error(context: TurnContext, error: Exception):
    log.exception("[on_turn_error] %s", error)
    try:
        await context.send_activity("Erreur interne du bot.")
    except Exception:
        pass

adapter.on_turn_error = on_error

# --- Bot ---
class TeamsSimpleBot(ActivityHandler):
    async def on_message_activity(self, turn_context: TurnContext):
        """Flux texte classique : envoie le message à ta Function (chat) et renvoie la réponse."""
        user_message = (turn_context.activity.text or "").strip()
        log.info("Message utilisateur: %s", user_message)

        try:
            if not FUNCTION_APP_URL:
                raise RuntimeError("FUNCTION_APP_URL manquant")
            resp = requests.post(
                FUNCTION_APP_URL,
                json={"message": user_message},
                timeout=30,
            )
            resp.raise_for_status()
            response = resp.json().get("response", "Aucune réponse du modèle.")
        except Exception as e:
            log.exception("Erreur lors de l'appel backend (chat): %s", e)
            response = f"Erreur backend : {e}"

        await turn_context.send_activity(response)

    # AJOUT : handler des events (fichiers envoyés depuis l'interface web)
    async def on_event_activity(self, turn_context: TurnContext):
        """
        Reçoit l'event 'files_uploaded' envoyé par le front (Direct Line),
        appelle /api/analyze, et poste le résultat dans la conversation.
        """
        if turn_context.activity.name == "files_uploaded":
            payload = turn_context.activity.value or {}
            blobs = payload.get("blobs", [])  # attendu: [{ blobUrl, contentType }]
            user_message = (payload.get("message") or "").strip()

            if not blobs:
                await turn_context.send_activity("Aucun fichier reçu.")
                return

            if not ANALYZE_URL:
                await turn_context.send_activity("Configuration manquante: ANALYZE_URL.")
                return

            # Petit message d'état
            try:
                await turn_context.send_activity(f"🔎 Analyse de {len(blobs)} fichier(s) en cours…")
            except Exception:
                pass

            try:
                req_payload = {"blobs": blobs}
                if user_message:
                    req_payload["message"] = user_message
                r = requests.post(ANALYZE_URL, json=req_payload, timeout=120)
                if r.ok:
                    data = r.json()
                    results = data.get("results", [])
                    if not results:
                        await turn_context.send_activity("Aucun résultat.")
                    else:
                        for i, res in enumerate(results, 1):
                            kind = (res.get("type") or "doc")
                            summary = (res.get("summary") or "")[:2000]  # borne de sécurité
                            await turn_context.send_activity(f"— Document {i} ({kind}):\n{summary}")
                else:
                    await turn_context.send_activity(f"❌ Erreur analyze {r.status_code}")
            except Exception as e:
                logging.exception("Erreur analyze: %s", e)
                await turn_context.send_activity(f"❌ Exception analyze: {e}")

bot = TeamsSimpleBot()

# --- Validation & utilitaires ---
REQUIRED_ACTIVITY_FIELDS = {"type", "serviceUrl", "channelId", "recipient", "conversation", "from"}

def _mask_auth(h: str) -> str:
    if not h:
        return ""
    return "Bearer ***" if h.lower().startswith("bearer ") else h

# --- Routes ---
async def messages(req: web.Request) -> web.Response:
    if req.method != "POST":
        return web.Response(status=405, text="Method Not Allowed", content_type="text/plain")

    content_type = (req.headers.get("Content-Type") or "").lower()
    if "application/json" not in content_type:
        log.warning("Invalid Content-Type on /api/messages: %s", content_type)
        return web.Response(status=415, text="Content-Type must be application/json", content_type="text/plain")

    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        log.info("Rejet requête sans Authorization Bearer sur /api/messages")
        return web.Response(status=401, text="Missing Bot Framework auth", content_type="text/plain")

    try:
        raw = await req.text()
        if not raw:
            return web.Response(status=400, text="Empty body", content_type="text/plain")

        log.info("Incoming /api/messages — Content-Type=%s, Authorization=%s", content_type, _mask_auth(auth_header))
        log.info("Incoming body (truncated): %s", (raw[:1000] + "...") if len(raw) > 1000 else raw)

        # Parse pour validation
        try:
            body_obj = json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("Invalid JSON body on /api/messages: %s", e)
            return web.Response(status=400, text=f"Invalid JSON: {e}", content_type="text/plain")

        if not isinstance(body_obj, dict):
            log.warning("Body is not a JSON object")
            return web.Response(status=400, text="Invalid activity: not a JSON object", content_type="text/plain")

        if not REQUIRED_ACTIVITY_FIELDS.issubset(body_obj.keys()):
            missing = REQUIRED_ACTIVITY_FIELDS.difference(body_obj.keys())
            log.warning("Invalid activity: missing fields: %s", ", ".join(sorted(missing)))
            return web.Response(
                status=400,
                text=f"Invalid activity: missing fields: {', '.join(sorted(missing))}",
                content_type="text/plain",
            )

        # Désérialise en Activity puis passe la main à l'adapter
        activity = Activity().deserialize(body_obj)
        # IMPORTANT : on passe bot.on_turn → ActivityHandler routéra vers on_message_activity / on_event_activity automatiquement
        invoke_response = await adapter.process_activity(activity, auth_header, bot.on_turn)

        if invoke_response:
            text = str(invoke_response.body) if invoke_response.body else ""
            return web.Response(status=invoke_response.status, text=text, content_type="text/plain")

        return web.Response(status=202, text="Accepted", content_type="text/plain")

    except Exception:
        log.exception("Unhandled error in /api/messages")
        return web.Response(status=500, text="Internal error", content_type="text/plain")

async def health(_):
    return web.Response(text="ok", content_type="text/plain")

async def home(_):
    return web.Response(text="Bot up and running", content_type="text/plain")

# --- App Aiohttp ---
app = web.Application()
app.router.add_post("/api/messages", messages)
app.router.add_get("/health", health)
app.router.add_get("/", home)

if __name__ == "__main__":
    web.run_app(app, port=int(os.getenv("PORT", "3978")))
