import os, io, json, base64
import azure.functions as func
from dotenv import load_dotenv
from openai import AzureOpenAI
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential

load_dotenv()

# ----- Azure OpenAI -----
client = AzureOpenAI(
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
)
DEPLOYMENT_NAME = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")

# ----- Document Intelligence -----
DI_ENDPOINT = os.environ["DOCUMENTINTELLIGENCE_ENDPOINT"]
DI_KEY = os.environ["DOCUMENTINTELLIGENCE_API_KEY"]
di_client = DocumentIntelligenceClient(DI_ENDPOINT, AzureKeyCredential(DI_KEY))


def _analyze_pdf(pdf_bytes: bytes, instruction: str) -> dict:
    poller = di_client.begin_analyze_document("prebuilt-layout", body=io.BytesIO(pdf_bytes))
    result = poller.result()
    paras = []
    if result.paragraphs:
        result.paragraphs.sort(key=lambda p: (p.spans[0].offset if p.spans else 0))
        for p in result.paragraphs:
            if p.content:
                paras.append(p.content.strip())
    text = "\n".join(paras)[:20000]
    text_for_model = text[:2000]
    messages = [
        {"role": "system", "content": "Tu es un assistant qui résume des documents en français."},
        {
            "role": "user",
            "content": f"Texte du PDF:\n{text_for_model}\n\nConsigne: {instruction}",
        },
    ]
    resp = client.chat.completions.create(model=DEPLOYMENT_NAME, messages=messages, temperature=0.2)
    summary = resp.choices[0].message.content.strip()
    return {"text": text, "summary": summary}


def _analyze_image(img_bytes: bytes, content_type: str, instruction: str) -> dict:
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    data_url = f"data:{content_type};base64,{b64}"
    messages = [
        {
            "role": "system",
            "content": "Tu es un assistant d’analyse d’images. Extrais le texte et les éléments clés, en français.",
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Analyse cette image et fournis un résumé structuré (texte détecté, éléments "
                        "clés, chiffres, alertes).\n\nConsigne: " + instruction
                    ),
                },
                {"type": "input_image", "image_url": data_url},
            ],
        },
    ]
    resp = client.chat.completions.create(model=DEPLOYMENT_NAME, messages=messages, temperature=0.2)
    summary = resp.choices[0].message.content.strip()
    return {"text": summary, "summary": summary}


def main(req: func.HttpRequest) -> func.HttpResponse:
    """Handle file or text input.

    - **multipart/form-data**: expects a field ``file`` (PDF or image) and optional ``instruction``.
      Returns ``{"text": ..., "summary": ...}``.
    - **application/json**: expects ``{"message": "..."}``.
      Returns ``{"response": ...}``.
    """
    try:
        if req.files and "file" in req.files:
            upload = req.files["file"]
            instruction = req.form.get("instruction", "")
            file_bytes = upload.read()
            content_type = (upload.content_type or "").lower()

            if content_type == "application/pdf" or upload.filename.lower().endswith(".pdf"):
                result = _analyze_pdf(file_bytes, instruction)
            elif content_type.startswith("image/"):
                result = _analyze_image(file_bytes, content_type, instruction)
            else:
                return func.HttpResponse(
                    json.dumps({"error": "Unsupported file type."}),
                    status_code=400,
                    mimetype="application/json",
                )
            return func.HttpResponse(json.dumps(result), status_code=200, mimetype="application/json")

        req_body = req.get_json()
        user_message = req_body.get("message", "")
        if not user_message:
            return func.HttpResponse(
                json.dumps({"error": "Missing 'message' in request."}),
                status_code=400,
                mimetype="application/json",
            )
        response = client.chat.completions.create(
            model=DEPLOYMENT_NAME,
            messages=[{"role": "user", "content": user_message}],
        )
        ai_response = response.choices[0].message.content
        return func.HttpResponse(
            json.dumps({"response": ai_response}),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            status_code=500,
            mimetype="application/json",
        )
