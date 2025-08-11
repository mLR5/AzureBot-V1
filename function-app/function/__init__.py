import azure.functions as func
import os
import json
from openai import AzureOpenAI
from dotenv import load_dotenv
load_dotenv()

# Mettre les secrets dans les variables d'environnement Azure !
client = AzureOpenAI(
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version="2024-10-21",
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"]
)

DEPLOYMENT_NAME = 'gpt-4o-mini'

def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        req_body = req.get_json()
        user_message = req_body.get("message", "")

        if not user_message:
            return func.HttpResponse(
                json.dumps({"error": "Missing 'message' in request."}),
                status_code=400,
                mimetype="application/json"
            )

        response = client.chat.completions.create(
            model=DEPLOYMENT_NAME,
            messages=[{"role": "user", "content": user_message}]
        )

        ai_response = response.choices[0].message.content

        return func.HttpResponse(
            json.dumps({"response": ai_response}),
            status_code=200,
            mimetype="application/json"
        )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            status_code=500,
            mimetype="application/json"
        )
