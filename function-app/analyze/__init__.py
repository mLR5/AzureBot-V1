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
    return {"type": "pdf", "text": text}

def _analyze_image(img_bytes: bytes, content_type: str) -> dict:
    # On envoie l'image en data URL (base64) au modèle vision
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    data_url = f"data:{content_type};base64,{b64}"
    messages = [
        {"role": "system", "content": "Tu es un assistant d’analyse d’images. Extrais le texte et les éléments clés, en français."},
        {"role": "user", "content": [
            {"type": "text", "text": "Analyse cette image et fournis un résumé structuré (texte détecté, éléments clés, chiffres, alertes)."},
            {"type": "input_image", "image_url": data_url}
        ]}
    ]
    resp = client.chat.completions.create(model=DEPLOYMENT_NAME, messages=messages, temperature=0.2)
    return {"type": "image", "text": resp.choices[0].message.content.strip()}

def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        blobs = body.get("blobs", [])  # [{blobUrl, contentType}]
        outputs = []
        for b in blobs:
            blob_url = b["blobUrl"]
            ct = (b.get("contentType") or "").lower()
            raw = _read_blob_to_bytes(blob_url)

            if ct == "application/pdf" or blob_url.lower().endswith(".pdf"):
                outputs.append(_analyze_pdf(raw))
            elif ct.startswith("image/"):
                outputs.append(_analyze_image(raw, ct))
            else:
                outputs.append({"type": "unknown", "text": "Type non supporté."})

        return func.HttpResponse(json.dumps({"results": outputs}), mimetype="application/json")
    except Exception as e:
        return func.HttpResponse(str(e), status_code=500)
