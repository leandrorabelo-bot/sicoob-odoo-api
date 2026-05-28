import os
import base64
import tempfile
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from requests_pkcs12 import post, get

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
    with open("/etc/secrets/certificado.pfx", "r") as f:
        cert_base64 = f.read().strip()

    cert_bytes = base64.b64decode(cert_base64)

    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".pfx")
    temp.write(cert_bytes)
    temp.close()

    return temp.name


def get_token():
    cert_path = get_cert_path()

    response = post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": os.getenv("SICOOB_CLIENT_ID"),
            "scope": "cco_consulta"
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        },
        pkcs12_filename=cert_path,
        pkcs12_password=os.getenv("SICOOB_CERT_PASSWORD"),
        timeout=30
    )

    response.raise_for_status()
    return response.json()["access_token"]


@app.get("/sicoob/token")
def sicoob_token():
    token = get_token()

    return {
        "ok": True,
        "token_inicio": token[:80] + "..."
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
        body = response.json()
    except Exception:
        body = response.text

    return {
        "status_code": response.status_code,
        "url_testada": url,
        "resposta": body
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
        body = response.json()
    except Exception:
        body = response.text

    return {
        "status_code": response.status_code,
        "url_testada": url,
        "resposta": body
    }
@app.get("/sicoob/extrato-limpo")
def sicoob_extrato_limpo(
    mes: int,
    ano: int,
    diaInicial: int,
    diaFinal: int
):
    bruto = sicoob_extrato(mes, ano, diaInicial, diaFinal)

    transacoes = bruto.get("resposta", {}).get("resultado", {}).get("transacoes", [])

    limpo = []

    for t in transacoes:
        limpo.append({
            "id": t.get("transactionId"),
            "data": t.get("data"),
            "data_lote": t.get("dataLote"),
            "tipo": t.get("tipo"),
            "valor": t.get("valor"),
            "descricao": t.get("descricao"),
            "documento": t.get("numeroDocumento"),
            "complemento": t.get("descInfComplementar")
        })

    return JSONResponse(
        content={
            "quantidade": len(limpo),
            "transacoes": limpo
        }
    )
@app.get("/sicoob/extrato-odoo")
def sicoob_extrato_odoo(
    mes: int,
    ano: int,
    diaInicial: int,
    diaFinal: int
):
    bruto = sicoob_extrato(mes, ano, diaInicial, diaFinal)

    transacoes = bruto.get("resposta", {}).get("resultado", {}).get("transacoes", [])

    linhas_odoo = []

    for t in transacoes:
        tipo = t.get("tipo")
        valor = float(t.get("valor", 0))

        amount = valor if tipo == "CREDITO" else -valor

        linhas_odoo.append({
            "date": t.get("data", "")[:10],
            "payment_ref": f"{t.get('descricao', '')} - {t.get('descInfComplementar', '')}",
            "amount": amount,
            "unique_import_id": t.get("transactionId"),
            "document": t.get("numeroDocumento"),
            "raw_type": tipo
        })

    return {
        "quantidade": len(linhas_odoo),
        "linhas": linhas_odoo
    }
