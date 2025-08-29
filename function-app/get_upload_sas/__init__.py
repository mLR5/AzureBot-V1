import json, os, uuid
from datetime import datetime, timedelta
from urllib.parse import quote
import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobSasPermissions, generate_blob_sas, BlobServiceClient

ACCOUNT_NAME = os.environ["STORAGE_ACCOUNT_NAME"]
ACCOUNT_URL  = os.environ["STORAGE_ACCOUNT_URL"]  # https://<sa>.blob.core.windows.net
CONTAINER    = os.environ.get("UPLOADS_CONTAINER", "uploads")

def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except Exception:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    files = body.get("files", [])  # [{filename, contentType}]
    user_id = body.get("userId", "web")

    if not isinstance(files, list) or not files:
        return func.HttpResponse("files[] required", status_code=400)

    cred = DefaultAzureCredential()
    bsc  = BlobServiceClient(account_url=ACCOUNT_URL, credential=cred)

    now = datetime.utcnow()
    udk = bsc.get_user_delegation_key(key_start_time=now, key_expiry_time=now + timedelta(hours=1))

    uploads = []
    for f in files:
        filename = f.get("filename") or "file.bin"
        ctype    = f.get("contentType") or "application/octet-stream"
        ext = (filename.split(".")[-1] if "." in filename else "bin").lower()
        # nommage côté serveur (anti-collision & traçable)
        blob_name = f"{user_id}/{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.{ext}"

        # SAS WRITE (PUT)
        write_sas = generate_blob_sas(
            account_name=ACCOUNT_NAME,
            container_name=CONTAINER,
            blob_name=blob_name,
            user_delegation_key=udk,
            permission=BlobSasPermissions(create=True, write=True),
            expiry=now + timedelta(minutes=15),
            content_type=ctype
        )
        blob_url = f"{ACCOUNT_URL}/{CONTAINER}/{quote(blob_name)}"
        put_url  = f"{blob_url}?{write_sas}"

        uploads.append({"blobUrl": blob_url, "putUrl": put_url, "contentType": ctype})

    return func.HttpResponse(json.dumps({"uploads": uploads}), mimetype="application/json")
