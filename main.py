from fastapi import FastAPI
from requests_pkcs12 import get
import requests
import os

app = FastAPI()

TOKEN_URL = "https://auth.sicoob.com.br/auth/realms/cooperado/protocol/openid-connect/token"
CONTA_URL = "https://api.sicoob.com.br/conta-corrente/v4"


@app.get("/")
def home():
    return {"status": "online"}


@app.get("/health")
def health():
    return {"health": "ok"}


def get_cert_path():
    return "/etc/secrets/certificado.pfx"


def get_token():

    cert_path = get_cert_path()

    response = get(
        TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={
            "grant_type": "client_credentials",
            "client_id": os.getenv("SICOOB_CLIENT_ID")
        },
        pkcs12_filename=cert_path,
        pkcs12_password=os.getenv("SICOOB_CERT_PASSWORD"),
        timeout=30
    )

    return response.json()["access_token"]


@app.get("/sicoob/token")
def sicoob_token():

    token = get_token()

    return {
        "ok": True,
        "token_inicio": token[:40] + "..."
    }


@app.get("/sicoob/extrato")
def sicoob_extrato(
    mes: int,
    ano: int,
    diaInicial: int,
    diaFinal: int
):

    token = get_token()

    conta = os.getenv("SICOOB_CONTA")

    cert_path = get_cert_path()

    url = f"{CONTA_URL}/extrato/{mes}/{ano}"

    response = get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "client_id": os.getenv("SICOOB_CLIENT_ID"),
            "Accept": "application/json"
        },
        params={
            "diaInicial": diaInicial,
            "diaFinal": diaFinal,
            "numeroContaCorrente": conta
        },
        pkcs12_filename=cert_path,
        pkcs12_password=os.getenv("SICOOB_CERT_PASSWORD"),
        timeout=30
    )

    try:
        resposta_json = response.json()
    except:
        resposta_json = response.text

    return {
        "status_code": response.status_code,
        "url_testada": url,
        "resposta": resposta_json
    }


@app.get("/sicoob/saldo")
def sicoob_saldo():

    token = get_token()

    conta = os.getenv("SICOOB_CONTA")

    cert_path = get_cert_path()

    url = f"{CONTA_URL}/saldo"

    response = get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "client_id": os.getenv("SICOOB_CLIENT_ID"),
            "Accept": "application/json"
        },
        params={
            "numeroContaCorrente": conta
        },
        pkcs12_filename=cert_path,
        pkcs12_password=os.getenv("SICOOB_CERT_PASSWORD"),
        timeout=30
    )

    try:
        resposta_json = response.json()
    except:
        resposta_json = response.text

    return {
        "status_code": response.status_code,
        "url_testada": url,
        "resposta": resposta_json
    }
