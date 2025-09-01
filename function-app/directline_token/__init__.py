import os, json, secrets, requests, azure.functions as func

def main(req: func.HttpRequest) -> func.HttpResponse:
        # 1) Récupère le secret
    secret = os.getenv("DIRECT_LINE_SECRET")
    if not secret:
        return func.HttpResponse(
            json.dumps({"error":"missing_env","var":"DIRECT_LINE_SECRET"}),
            status_code=500, mimetype="application/json"
        )

    # 2) userId (query ?userId=... ou POST {"userId": "..."}), sinon id auto
    user_id = req.params.get("userId")
    if not user_id:
        try:
            body = req.get_json()
            if isinstance(body, dict):
                user_id = body.get("userId")
        except Exception:
            pass
    if not user_id:
        user_id = f"web_{secrets.token_hex(8)}"
    # 3) Appel Direct Line (⚠️ clés en minuscules dans le payload)
    r = requests.post(
        "https://directline.botframework.com/v3/directline/tokens/generate",
        headers={"Authorization": f"Bearer {secret}"},
        json={"user": {"id": user_id}},
        timeout=10
    )
    # 4) Retourne tel quel la réponse JSON de Direct Line
    return func.HttpResponse(r.text, status_code=r.status_code, mimetype="application/json")
