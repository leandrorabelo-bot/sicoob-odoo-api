import os, base64, tempfile
import requests
from requests_pkcs12 import post
from fastapi import FastAPI

app = FastAPI()

TOKEN_URL = "https://auth.sicoob.com.br/auth/realms/cooperado/protocol/openid-connect/token"
CONTA_URL = "https://api.sicoob.com.br/conta-corrente/v4"

def get_cert_path():
    with open("/etc/secrets/certificado.pfx", "r") as f:
        cert_bytes = base64.b64decode(f.read().strip())
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".pfx")
    temp.write(cert_bytes)
    temp.close()
    return temp.name

def get_token():
    cert_path = get_cert_path()
    r = post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": os.getenv("SICOOB_CLIENT_ID"),
            "scope": "cco_consulta"
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        pkcs12_filename=cert_path,
        pkcs12_password=os.getenv("SICOOB_CERT_PASSWORD"),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]

@app.get("/")
def home():
    return {"status": "online"}

@app.get("/health")
def health():
    return {"health": "ok"}

@app.get("/sicoob/token")
def sicoob_token():
    return {"access_token": get_token()[:80] + "...", "ok": True}

@app.get("/sicoob/extrato")
def sicoob_extrato(dataInicio: str, dataFim: str):
    token = get_token()
    conta = os.getenv("SICOOB_CONTA")

    url = f"{CONTA_URL}/contas/{conta}/extrato"

    r = requests.get(
        url,
        params={
            "dataInicio": dataInicio,
            "dataFim": dataFim
        },
        headers={
            "Authorization": f"Bearer {token}",
            "client_id": os.getenv("SICOOB_CLIENT_ID"),
            "Accept": "application/json"
        },
        timeout=30,
    )

    try:
        body = r.json()
    except Exception:
        body = r.text

    return {
        "status_code": r.status_code,
        "url_testada": url,
        "resposta": body
    }
