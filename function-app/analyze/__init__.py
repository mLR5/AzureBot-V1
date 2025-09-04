import os, io, json, base64
import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.search.documents import SearchClient
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

# ----- Azure AI Search (vector index) -----
SEARCH_ENDPOINT = os.environ.get("SEARCH_ENDPOINT")
SEARCH_KEY = os.environ.get("SEARCH_KEY")
SEARCH_INDEX_NAME = os.environ.get("SEARCH_INDEX_NAME")
search_client = None
if SEARCH_ENDPOINT and SEARCH_KEY and SEARCH_INDEX_NAME:
    search_client = SearchClient(
        endpoint=SEARCH_ENDPOINT,
        index_name=SEARCH_INDEX_NAME,
        credential=AzureKeyCredential(SEARCH_KEY),
    )

EMBEDDING_DEPLOYMENT = os.environ.get(
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large"
)

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


def _chunk_text(text: str, size: int = 500):
    tokens = text.split()
    for i in range(0, len(tokens), size):
        yield " ".join(tokens[i : i + size]), i // size


def _index_text(blob_url: str, text: str) -> list[str]:
    if not search_client:
        return []
    ids = []
    for chunk_text, idx in _chunk_text(text):
        emb = client.embeddings.create(
            model=EMBEDDING_DEPLOYMENT, input=chunk_text
        )
        embedding = emb.data[0].embedding
        doc_id = base64.urlsafe_b64encode(f"{blob_url}|{idx}".encode()).decode(
            "utf-8"
        )
        search_client.upload_documents(
            [
                {
                    "id": doc_id,
                    "blob_url": blob_url,
                    "chunk_text": chunk_text,
                    "embedding": embedding,
                }
            ]
        )
        ids.append(doc_id)
    return ids

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
                res = _analyze_pdf(raw)
                res["embedding_ids"] = _index_text(blob_url, res["text"])
                outputs.append(res)
            elif ct.startswith("image/"):
                img_res = _analyze_image(raw, ct)
                img_res["summary"] = img_res.get("text")
                img_res["embedding_ids"] = _index_text(blob_url, img_res["text"])
                outputs.append(img_res)
            else:
                outputs.append({"type": "unknown", "text": "Type non supporté.", "summary": "Type non supporté."})

        return func.HttpResponse(json.dumps({"results": outputs}), mimetype="application/json")
    except Exception as e:
        return func.HttpResponse(str(e), status_code=500)
