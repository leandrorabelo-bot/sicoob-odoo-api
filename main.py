import os
import base64
import tempfile
import csv
import io

from fastapi import FastAPI
from fastapi.responses import Response
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

    temp = tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".pfx"
    )

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
    bruto = sicoob_extrato(
        mes,
        ano,
        diaInicial,
        diaFinal
    )

    transacoes = (
        bruto
        .get("resposta", {})
        .get("resultado", {})
        .get("transacoes", [])
    )

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

    return {
        "quantidade": len(limpo),
        "transacoes": limpo
    }


@app.get("/sicoob/extrato-odoo")
def sicoob_extrato_odoo(
    mes: int,
    ano: int,
    diaInicial: int,
    diaFinal: int
):
    bruto = sicoob_extrato_limpo(
        mes,
        ano,
        diaInicial,
        diaFinal
    )

    linhas = []

    for t in bruto["transacoes"]:

        valor = float(t["valor"])

        if t["tipo"] == "DEBITO":
            valor = valor * -1

        linhas.append({
            "date": t["data_lote"],
            "payment_ref": f"{t['descricao']} - {t['complemento']}",
            "amount": valor,
            "unique_import_id": t["id"],
            "document": t["documento"],
            "raw_type": t["tipo"]
        })

    return {
        "quantidade": len(linhas),
        "linhas": linhas
    }


@app.get("/sicoob/extrato-csv")
def sicoob_extrato_csv(
    mes: int,
    ano: int,
    diaInicial: int,
    diaFinal: int
):
    dados = sicoob_extrato_odoo(
        mes,
        ano,
        diaInicial,
        diaFinal
    )

    linhas = dados["linhas"]

    output = io.StringIO()

    writer = csv.writer(output)

    writer.writerow([
        "Date",
        "Label",
        "Amount"
    ])

    for l in linhas:
        writer.writerow([
            l["date"],
            l["payment_ref"],
            l["amount"]
        ])

    csv_content = output.getvalue()

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=extrato.csv"
        }
    )
import xmlrpc.client

ODOO_URL = "https://gmmholding.odoo.com"
ODOO_DB = "gmmholding"
ODOO_USER = "leandro.rabelo@gmail.com"
ODOO_API_KEY = "10feaa5dc77bc458303ca70eee7c4ed6096401f4"
ODOO_JOURNAL_ID = 18


@app.get("/odoo/teste")
def testar_odoo():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})

    return {
        "uid": uid,
        "status": "conectado" if uid else "falhou"
    }
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


@app.get("/odoo/importar-extrato")
def importar_extrato(mes: str, ano: str, diaInicial: str, diaFinal: str):
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})

    if not uid:
        return {"status": "erro", "mensagem": "Falha ao autenticar no Odoo"}

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

    dados = sicoob_extrato_odoo(
        mes=mes,
        ano=ano,
        diaInicial=diaInicial,
        diaFinal=diaFinal
    )

    linhas = dados["linhas"]
    importados = []

    for l in linhas:
        statement_line = {
            "date": l["date"],
            "payment_ref": l["payment_ref"],
            "amount": l["amount"],
            "unique_import_id": l["unique_import_id"],
            "journal_id": ODOO_JOURNAL_ID,
        }

        result = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_API_KEY,
            "account.bank.statement.line",
            "create",
            [statement_line]
        )

        importados.append(result)

    return {
        "status": "ok",
        "importados": len(importados),
        "ids": importados
    }
