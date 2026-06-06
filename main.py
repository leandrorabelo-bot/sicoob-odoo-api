import os
import base64
import tempfile
import csv
import io
import requests
import xmlrpc.client

from fastapi import FastAPI
from fastapi.responses import Response
from requests_pkcs12 import post, get

app = FastAPI()

# =========================
# SICOOB
# =========================

TOKEN_URL = "https://auth.sicoob.com.br/auth/realms/cooperado/protocol/openid-connect/token"
CONTA_URL = "https://api.sicoob.com.br/conta-corrente/v4"

# =========================
# ODOO
# =========================

ODOO_URL = "https://gmmholding.odoo.com"
ODOO_DB = "gmmholding"
ODOO_USER = "leandro.rabelo@gmail.com"
ODOO_API_KEY = os.getenv("10feaa5dc77bc458303ca70eee7c4ed6096401f4")
ODOO_JOURNAL_ID = 18

# =========================
# STONE
# =========================

STONE_API_KEY = os.getenv("STONE_API_KEY")


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
            "scope": "cco_consulta",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        pkcs12_filename=cert_path,
        pkcs12_password=os.getenv("SICOOB_CERT_PASSWORD"),
        timeout=30,
    )

    response.raise_for_status()
    return response.json()["access_token"]


@app.get("/sicoob/token")
def sicoob_token():
    token = get_token()
    return {"ok": True, "token_inicio": token[:80] + "..."}


@app.get("/sicoob/extrato")
def sicoob_extrato(mes: int, ano: int, diaInicial: int, diaFinal: int):
    token = get_token()
    conta = os.getenv("SICOOB_CONTA")
    cert_path = get_cert_path()

    url = f"{CONTA_URL}/extrato/{mes}/{ano}"

    response = get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "client_id": os.getenv("SICOOB_CLIENT_ID"),
            "Accept": "application/json",
        },
        params={
            "diaInicial": diaInicial,
            "diaFinal": diaFinal,
            "numeroContaCorrente": conta,
        },
        pkcs12_filename=cert_path,
        pkcs12_password=os.getenv("SICOOB_CERT_PASSWORD"),
        timeout=30,
    )

    try:
        body = response.json()
    except Exception:
        body = response.text

    return {
        "status_code": response.status_code,
        "url_testada": url,
        "resposta": body,
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
            "Accept": "application/json",
        },
        params={"numeroContaCorrente": conta},
        pkcs12_filename=cert_path,
        pkcs12_password=os.getenv("SICOOB_CERT_PASSWORD"),
        timeout=30,
    )

    try:
        body = response.json()
    except Exception:
        body = response.text

    return {
        "status_code": response.status_code,
        "url_testada": url,
        "resposta": body,
    }


@app.get("/sicoob/extrato-limpo")
def sicoob_extrato_limpo(mes: int, ano: int, diaInicial: int, diaFinal: int):
    bruto = sicoob_extrato(mes, ano, diaInicial, diaFinal)

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
            "complemento": t.get("descInfComplementar"),
        })

    return {
        "quantidade": len(limpo),
        "transacoes": limpo,
    }


@app.get("/sicoob/extrato-odoo")
def sicoob_extrato_odoo(mes: int, ano: int, diaInicial: int, diaFinal: int):
    bruto = sicoob_extrato_limpo(mes, ano, diaInicial, diaFinal)

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
            "raw_type": t["tipo"],
        })

    return {
        "quantidade": len(linhas),
        "linhas": linhas,
    }


@app.get("/sicoob/extrato-csv")
def sicoob_extrato_csv(mes: int, ano: int, diaInicial: int, diaFinal: int):
    dados = sicoob_extrato_odoo(mes, ano, diaInicial, diaFinal)
    linhas = dados["linhas"]

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["Date", "Label", "Amount"])

    for l in linhas:
        writer.writerow([
            l["date"],
            l["payment_ref"],
            l["amount"],
        ])

    csv_content = output.getvalue()

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=extrato.csv"},
    )


# =========================
# ODOO
# =========================

def get_odoo_uid():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    return common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})


@app.get("/odoo/teste")
def testar_odoo():
    uid = get_odoo_uid()

    return {
        "uid": uid,
        "status": "conectado" if uid else "falhou",
    }


@app.get("/odoo/importar-extrato")
def importar_extrato(mes: str, ano: str, diaInicial: str, diaFinal: str):
    uid = get_odoo_uid()

    if not uid:
        return {"status": "erro", "mensagem": "Falha ao autenticar no Odoo"}

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

    dados = sicoob_extrato_odoo(
        mes=mes,
        ano=ano,
        diaInicial=diaInicial,
        diaFinal=diaFinal,
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
            [statement_line],
        )

        importados.append(result)

    return {
        "status": "ok",
        "importados": len(importados),
        "ids": importados,
    }


@app.get("/odoo/sincronizar")
def sincronizar_extrato(mes: str, ano: str, diaInicial: str, diaFinal: str):
    uid = get_odoo_uid()

    if not uid:
        return {
            "status": "erro",
            "mensagem": "Falha autenticação Odoo",
        }

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

    dados = sicoob_extrato_odoo(
        mes=mes,
        ano=ano,
        diaInicial=diaInicial,
        diaFinal=diaFinal,
    )

    linhas = dados["linhas"]

    importados = 0
    duplicados = 0
    erros = []

    for l in linhas:
        try:
            statement_line = {
                "date": l["date"],
                "payment_ref": l["payment_ref"],
                "amount": l["amount"],
                "unique_import_id": l["unique_import_id"],
                "journal_id": ODOO_JOURNAL_ID,
            }

            models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_API_KEY,
                "account.bank.statement.line",
                "create",
                [statement_line],
            )

            importados += 1

        except Exception as e:
            erro = str(e)

            if "can be imported only once" in erro:
                duplicados += 1
            else:
                erros.append(erro)

    return {
        "status": "ok",
        "total_recebido": len(linhas),
        "importados": importados,
        "duplicados": duplicados,
        "erros": erros,
    }


# =========================
# STONE
# =========================

from requests.auth import HTTPBasicAuth

@app.get("/stone/agenda")
def stone_agenda(
    stonecode: str,
    data: str,
    layout: str = "XML2_2"
):
    if not STONE_API_KEY:
        return {
            "status": "erro",
            "mensagem": "STONE_API_KEY não configurada no Render",
        }

    url = f"https://conciliation.stone.com.br/v2/merchant/{stonecode}/conciliation-file/{data}"

    response = requests.get(
        url,
        params={"layout": layout},
        headers={
            "Accept": "application/xml",
            "Accept-Encoding": "gzip",
            "x-user-type": "client",
            "X-Accept-Redirect": "true",
        },
        auth=HTTPBasicAuth(STONE_API_KEY, ""),
        timeout=120,
    )

    return {
        "status_code": response.status_code,
        "url_testada": url,
        "content_type": response.headers.get("Content-Type"),
        "tamanho": len(response.content),
        "inicio_resposta": response.text[:5000] if response.text else "",
    }
import xml.etree.ElementTree as ET


@app.get("/stone/agenda-limpa")
def stone_agenda_limpa(
    stonecode: str,
    data: str,
    layout: str = "XML2_2"
):
    url = f"https://conciliation.stone.com.br/v2/merchant/{stonecode}/conciliation-file/{data}"

    response = requests.get(
        url,
        params={"layout": layout},
        headers={
            "Accept": "application/xml",
            "Accept-Encoding": "gzip",
            "x-user-type": "client",
            "X-Accept-Redirect": "true",
        },
        auth=HTTPBasicAuth(STONE_API_KEY, ""),
        timeout=120,
    )

    if response.status_code != 200:
        return {
            "status_code": response.status_code,
            "resposta": response.text
        }

    root = ET.fromstring(response.content)

    def txt(node, name):
        found = node.find(name)
        return found.text if found is not None else None

    transacoes = []

    for t in root.iter():
        if not t.tag.endswith("Transaction"):
            continue

        item = {
            "acquirer_transaction_key": txt(t, "AcquirerTransactionKey"),
            "initiator_transaction_key": txt(t, "InitiatorTransactionKey"),
            "authorization_datetime": txt(t, "AuthorizationDateTime"),
            "capture_local_datetime": txt(t, "CaptureLocalDateTime"),
            "authorized_amount": txt(t, "AuthorizedAmount"),
            "captured_amount": txt(t, "CapturedAmount"),
            "gross_amount": txt(t, "GrossAmount"),
            "net_amount": txt(t, "NetAmount"),
            "prevision_payment_date": txt(t, "PrevisionPaymentDate"),
            "issuer_authorization_code": txt(t, "IssuerAuthorizationCode"),
            "brand_id": txt(t, "BrandId"),
            "card_number": txt(t, "CardNumber"),
            "installment_number": txt(t, "InstallmentNumber"),
            "number_of_installments": txt(t, "NumberOfInstallments"),
            "fee_type": txt(t, "FeeType"),
            "entry_mode": txt(t, "EntryMode"),
            "account_type": txt(t, "AccountType"),
        }

        transacoes.append(item)

    return {
        "stonecode": stonecode,
        "data": data,
        "quantidade": len(transacoes),
        "transacoes": transacoes
    }
@app.get("/stone/debug")
def stone_debug(stonecode: str, data: str):

    url = f"https://conciliation.stone.com.br/v2/merchant/{stonecode}/conciliation-file/{data}"

    response = requests.get(
        url,
        headers={
            "Accept": "application/xml",
            "Accept-Encoding": "gzip",
            "x-user-type": "client",
            "X-Accept-Redirect": "true",
        },
        auth=HTTPBasicAuth(STONE_API_KEY, ""),
        timeout=120,
    )

    xml = response.text

    return {
        "inicio": xml[:5000]
    }
@app.get("/stone/debug-key")
def stone_debug_key():
    return {
        "repr": repr(STONE_API_KEY),
        "tamanho": len(STONE_API_KEY)
    }
