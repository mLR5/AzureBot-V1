import os, io, json, base64
import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from openai import AzureOpenAI

# ----- Storage (lecture privée via Managed Identity) -----
ACCOUNT_URL = os.environ["STORAGE_ACCOUNT_URL"]   # https://<sa>.blob.core.windows.net
CONTAINER   = os.environ.get("UPLOADS_CONTAINER", "uploads")
cred        = DefaultAzureCredential()
bsc         = BlobServiceClient(account_url=ACCOUNT_URL, credential=cred)

# ----- Document Intelligence -----
DI_ENDPOINT = os.environ["DOCUMENTINTELLIGENCE_ENDPOINT"]
DI_KEY      = os.environ["DOCUMENTINTELLIGENCE_API_KEY"]
di_client   = DocumentIntelligenceClient(DI_ENDPOINT, AzureKeyCredential(DI_KEY))

# ----- Azure OpenAI (même style que TA fonction initiale) -----
client = AzureOpenAI(
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"]
)
DEPLOYMENT_NAME = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")

def _read_blob_to_bytes(blob_url: str) -> bytes:
    # blob_url = https://<sa>.blob.core.windows.net/<container>/<path>
    parts = blob_url.split("/", 4)
    container = parts[3]
    blob_name = parts[4]
    return bsc.get_container_client(container).get_blob_client(blob_name).download_blob().readall()

def _analyze_pdf(pdf_bytes: bytes) -> dict:
    # Selon la version du SDK, l'argument peut être nommé "document" ou "body".
    # Essaye d'abord "document", puis bascule sur "body" pour compatibilité.
    try:
        poller = di_client.begin_analyze_document("prebuilt-layout", document=io.BytesIO(pdf_bytes))
    except TypeError:
        poller = di_client.begin_analyze_document("prebuilt-layout", body=io.BytesIO(pdf_bytes))
    result = poller.result()
    paras = []
    if result.paragraphs:
        # simple ordre de lecture
        result.paragraphs.sort(key=lambda p: (p.spans[0].offset if p.spans else 0))
        for p in result.paragraphs:
            if p.content:
                paras.append(p.content.strip())
    text = "\n".join(paras)[:20000]
    # Résumé via Azure OpenAI (texte tronqué pour éviter les dépassements)
    text_for_model = text[:2000]
    messages = [
        {"role": "system", "content": "Tu es un assistant qui résume des documents en français."},
        {
            "role": "user",
            "content": f"Texte du PDF:\n{text_for_model}\n\nFais un résumé structuré (points clés, chiffres, alertes).",
        },
    ]
    resp = client.chat.completions.create(model=DEPLOYMENT_NAME, messages=messages, temperature=0.2)
    summary = resp.choices[0].message.content.strip()
    return {"type": "pdf", "text": text, "summary": summary}

def _analyze_image(img_bytes: bytes, content_type: str, user_message: str = "") -> dict:
    # On envoie l'image en data URL (base64) au modèle vision
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    data_url = f"data:{content_type};base64,{b64}"
    instruction = "Analyse cette image et fournis un résumé structuré (texte détecté, éléments clés, chiffres, alertes)."
    if user_message:
        instruction += f"\nConsidère également la demande de l'utilisateur: {user_message}"
    messages = [
        {"role": "system", "content": "Tu es un assistant d’analyse d’images. Extrais le texte et les éléments clés, en français."},
        {"role": "user", "content": [
            {"type": "text", "text": instruction},
            {"type": "image_url", "image_url": {"url": data_url}}
        ]}
    ]
    resp = client.chat.completions.create(model=DEPLOYMENT_NAME, messages=messages, temperature=0.2)
    return {"type": "image", "text": resp.choices[0].message.content.strip()}

def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        blobs = body.get("blobs", [])  # [{blobUrl, contentType}]
        user_message = (body.get("message") or "").strip()
        outputs = []
        for b in blobs:
            blob_url = b["blobUrl"]
            ct = (b.get("contentType") or "").lower()
            raw = _read_blob_to_bytes(blob_url)

            if ct == "application/pdf" or blob_url.lower().endswith(".pdf"):
                pdf_res = _analyze_pdf(raw)
                # Optionnel: adapter le résumé en fonction du contexte utilisateur
                if user_message:
                    text_for_model = (pdf_res.get("text") or "")[:2000]
                    messages = [
                        {"role": "system", "content": "Tu es un assistant qui résume des documents en français."},
                        {"role": "user", "content": f"Contexte utilisateur: {user_message}\n\nTexte du PDF (tronqué):\n{text_for_model}\n\nFais un résumé/réponse ciblée."}
                    ]
                    resp = client.chat.completions.create(model=DEPLOYMENT_NAME, messages=messages, temperature=0.2)
                    pdf_res["summary"] = resp.choices[0].message.content.strip()
                outputs.append(pdf_res)
            elif ct.startswith("image/"):
                img_res = _analyze_image(raw, ct, user_message)
                outputs.append({**img_res, "summary": img_res.get("text")})
            else:
                outputs.append({"type": "unknown", "text": "Type non supporté.", "summary": "Type non supporté."})

        return func.HttpResponse(json.dumps({"results": outputs}), mimetype="application/json")
    except Exception as e:
        return func.HttpResponse(str(e), status_code=500)
