import os
import base64
import tempfile
import requests
from fastapi import FastAPI

app = FastAPI()

SICOOB_TOKEN_URL = "https://auth.sicoob.com.br/auth/realms/cooperado/protocol/openid-connect/token"

@app.get("/")
def home():
    return {"status": "online"}

@app.get("/health")
def health():
    return {"health": "ok"}

@app.get("/sicoob/token")
def sicoob_token():
    client_id = os.getenv("SICOOB_CLIENT_ID")
    cert_password = os.getenv("SICOOB_CERT_PASSWORD")
    cert_b64_path = "/etc/secrets/certificado.pfx"

    with open(cert_b64_path, "r") as f:
        cert_b64 = f.read().strip()

    cert_bytes = base64.b64decode(cert_b64)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pfx") as temp_cert:
        temp_cert.write(cert_bytes)
        temp_cert_path = temp_cert.name

    response = requests.post(
        SICOOB_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        },
        pkcs12_filename=temp_cert_path,
        pkcs12_password=cert_password,
        timeout=30,
    )

    return {
        "status_code": response.status_code,
        "response": response.json() if response.text else None
    }
