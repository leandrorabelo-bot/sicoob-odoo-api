import os
import base64
import tempfile
import csv
import io
import requests
import xmlrpc.client
import xml.etree.ElementTree as ET
import threading
import urllib.request
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.responses import Response
from requests_pkcs12 import post, get
from requests.auth import HTTPBasicAuth
from gcom import gcom_auto_sync, gcom_sincronizar, gcom_auth, get_odoo_uid, get_odoo_models, PDV_MAP

app = FastAPI()

# =========================
# KEEP-ALIVE (evita Render dormir)
# =========================

def _keep_alive():
    import time
    port = os.environ.get("PORT", "8000")
    while True:
        time.sleep(600)  # a cada 10 minutos
        try:
            urllib.request.urlopen(f"http://localhost:{port}/health")
        except:
            pass

threading.Thread(target=_keep_alive, daemon=True).start()

# =========================
# CONFIG
# =========================

TOKEN_URL = "https://auth.sicoob.com.br/auth/realms/cooperado/protocol/openid-connect/token"
CONTA_URL = "https://api.sicoob.com.br/conta-corrente/v4"

ODOO_URL = "https://gmmholding.odoo.com"
ODOO_DB = "gmmholding"
ODOO_USER = "leandro.rabelo@gmail.com"
ODOO_API_KEY = os.getenv("ODOO_API_KEY")
ODOO_JOURNAL_ID = 18  # SICOOB 5004

STONE_API_KEY = os.getenv("STONE_API_KEY")  # chave padrão (fallback)

# stonecode -> journal_id + SAK própria (se tiver)
STONE_JOURNALS = {
    "147684511": {"journal_id": 21, "name": "STONE | PS 0001",     "sak": os.getenv("STONE_SAK_PS0001")},
    "117503480": {"journal_id": 22, "name": "STONE | PS 0002",     "sak": os.getenv("STONE_SAK_PS0002")},
    "125360374": {"journal_id": 23, "name": "STONE | PS 0003",     "sak": os.getenv("STONE_SAK_PS0003")},
    "589138084": {"journal_id": 24, "name": "STONE | PS 0004",     "sak": os.getenv("STONE_SAK_PS0004")},
    "863700291": {"journal_id": 25, "name": "STONE | PS 0005",     "sak": os.getenv("STONE_SAK_PS0005")},
    "580664438": {"journal_id": 40, "name": "STONE | TREVO",       "sak": os.getenv("STONE_SAK_TREVO")},
    "111114546": {"journal_id": 27, "name": "STONE | DRIVE ASSIS", "sak": os.getenv("STONE_SAK_DRIVE")},
}

# =========================
# HELPERS
# =========================

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

def get_odoo_uid():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    return common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})

def get_odoo_models():
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# =========================
# SICOOB
# =========================

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
    return {"status_code": response.status_code, "resposta": body}

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
    return {"status_code": response.status_code, "resposta": body}

@app.get("/sicoob/extrato-limpo")
def sicoob_extrato_limpo(mes: int, ano: int, diaInicial: int, diaFinal: int):
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
            "complemento": t.get("descInfComplementar"),
        })
    return {"quantidade": len(limpo), "transacoes": limpo}

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
        })
    return {"quantidade": len(linhas), "linhas": linhas}

@app.get("/sicoob/sincronizar")
def sincronizar_extrato(mes: str, ano: str, diaInicial: str, diaFinal: str):
    uid = get_odoo_uid()
    if not uid:
        return {"status": "erro", "mensagem": "Falha autenticacao Odoo"}
    models = get_odoo_models()
    dados = sicoob_extrato_odoo(mes=int(mes), ano=int(ano), diaInicial=int(diaInicial), diaFinal=int(diaFinal))
    importados = 0
    duplicados = 0
    erros = []
    for l in dados["linhas"]:
        try:
            models.execute_kw(
                ODOO_DB, uid, ODOO_API_KEY,
                "account.bank.statement.line", "create",
                [{"date": l["date"], "payment_ref": l["payment_ref"], "amount": l["amount"], "unique_import_id": l["unique_import_id"], "journal_id": ODOO_JOURNAL_ID}],
            )
            importados += 1
        except Exception as e:
            erro = str(e)
            if "can be imported only once" in erro or "unique_import_id" in erro:
                duplicados += 1
            else:
                erros.append(erro)
    return {"status": "ok", "importados": importados, "duplicados": duplicados, "erros": erros}

@app.get("/sicoob/auto-sync")
def sicoob_auto_sync():
    hoje = datetime.now()
    inicio = hoje - timedelta(days=5)
    resultados = []
    if inicio.month == hoje.month:
        resultados.append(sincronizar_extrato(
            mes=str(hoje.month), ano=str(hoje.year),
            diaInicial=str(inicio.day), diaFinal=str(hoje.day)
        ))
    else:
        resultados.append(sincronizar_extrato(
            mes=str(inicio.month), ano=str(inicio.year),
            diaInicial=str(inicio.day), diaFinal="31"
        ))
        resultados.append(sincronizar_extrato(
            mes=str(hoje.month), ano=str(hoje.year),
            diaInicial="1", diaFinal=str(hoje.day)
        ))
    return {
        "status": "ok",
        "periodo": f"{inicio.strftime('%d/%m/%Y')} a {hoje.strftime('%d/%m/%Y')}",
        "importados": sum(r.get("importados", 0) for r in resultados),
        "duplicados": sum(r.get("duplicados", 0) for r in resultados),
        "erros": [e for r in resultados for e in r.get("erros", [])],
    }

# =========================
# STONE
# =========================

def stone_buscar_xml(stonecode: str, data: str):
    info = STONE_JOURNALS.get(stonecode, {})
    sak = info.get("sak") or STONE_API_KEY  # usa SAK própria ou fallback
    url = f"https://conciliation.stone.com.br/v2/merchant/{stonecode}/conciliation-file/{data}"
    return requests.get(
        url,
        params={"layout": "XML2_2"},
        headers={"Accept": "application/xml", "Accept-Encoding": "gzip", "x-user-type": "client", "X-Accept-Redirect": "true"},
        auth=HTTPBasicAuth(sak, ""),
        timeout=120,
    )

def stone_parse_transacoes(xml_content):
    root = ET.fromstring(xml_content)
    def txt(node, name):
        found = node.find(name)
        return found.text if found is not None else None
    transacoes = []
    for t in root.iter():
        if not t.tag.endswith("Transaction"):
            continue
        transacoes.append({
            "acquirer_transaction_key": txt(t, "AcquirerTransactionKey"),
            "authorization_datetime": txt(t, "AuthorizationDateTime"),
            "capture_local_datetime": txt(t, "CaptureLocalDateTime"),
            "gross_amount": txt(t, "GrossAmount"),
            "net_amount": txt(t, "NetAmount"),
            "brand_id": txt(t, "BrandId"),
            "number_of_installments": txt(t, "NumberOfInstallments"),
        })
    return transacoes

@app.get("/stone/agenda")
def stone_agenda(stonecode: str, data: str):
    response = stone_buscar_xml(stonecode, data)
    return {"status_code": response.status_code, "tamanho": len(response.content), "inicio": response.text[:2000]}

@app.get("/stone/agenda-limpa")
def stone_agenda_limpa(stonecode: str, data: str):
    response = stone_buscar_xml(stonecode, data)
    if response.status_code != 200:
        return {"status_code": response.status_code, "resposta": response.text}
    transacoes = stone_parse_transacoes(response.content)
    return {"stonecode": stonecode, "data": data, "quantidade": len(transacoes), "transacoes": transacoes}

@app.get("/stone/sincronizar")
def stone_sincronizar(stonecode: str, data: str):
    if stonecode not in STONE_JOURNALS:
        return {"status": "erro", "mensagem": f"stonecode {stonecode} nao mapeado"}
    info = STONE_JOURNALS[stonecode]
    response = stone_buscar_xml(stonecode, data)
    if response.status_code != 200:
        return {"status": "erro", "status_code": response.status_code, "resposta": response.text[:500]}
    transacoes = stone_parse_transacoes(response.content)
    uid = get_odoo_uid()
    if not uid:
        return {"status": "erro", "mensagem": "Falha autenticacao Odoo"}
    models = get_odoo_models()
    importados = 0
    duplicados = 0
    erros = []
    for t in transacoes:
        try:
            valor = float(t["gross_amount"] or 0) / 100
            cap = t["capture_local_datetime"] or t["authorization_datetime"] or data
            data_line = cap[:10] if cap else data
            ref = f"Stone {info['name']} | {t['brand_id'] or ''} | {t['number_of_installments'] or 1}x"
            unique_id = f"stone_{stonecode}_{t['acquirer_transaction_key']}"
            models.execute_kw(
                ODOO_DB, uid, ODOO_API_KEY,
                "account.bank.statement.line", "create",
                [{"date": data_line, "payment_ref": ref, "amount": valor, "unique_import_id": unique_id, "journal_id": info["journal_id"]}],
            )
            importados += 1
        except Exception as e:
            erro = str(e)
            if "can be imported only once" in erro or "unique_import_id" in erro:
                duplicados += 1
            else:
                erros.append(erro)
    return {"status": "ok", "stonecode": stonecode, "nome": info["name"], "data": data, "total": len(transacoes), "importados": importados, "duplicados": duplicados, "erros": erros}

@app.get("/stone/auto-sync")
def stone_auto_sync():
    uid = get_odoo_uid()
    if not uid:
        return {"status": "erro", "mensagem": "Falha autenticacao Odoo"}
    resultados = []
    hoje = datetime.now()
    for dias_atras in range(1, 8):  # últimos 7 dias
        data_busca = (hoje - timedelta(days=dias_atras)).strftime("%Y%m%d")
        for stonecode in STONE_JOURNALS:
            r = stone_sincronizar(stonecode=stonecode, data=data_busca)
            r["data_busca"] = data_busca
            resultados.append(r)
    return {
        "status": "ok",
        "total_importados": sum(r.get("importados", 0) for r in resultados),
        "total_duplicados": sum(r.get("duplicados", 0) for r in resultados),
        "erros": [e for r in resultados for e in r.get("erros", []) if isinstance(e, str)],
        "resultados": resultados,
    }

# =========================
# GCOM
# =========================

@app.get("/gcom/sincronizar")
def gcom_sinc(id_etb: int, data: str):
    return gcom_sincronizar(id_etb, data)

@app.get("/gcom/auto-sync")
def gcom_auto():
    return gcom_auto_sync()

@app.get("/gcom/sincronizar-periodo")
def gcom_sinc_periodo(inicio: str, fim: str):
    def run():
        try:
            uid = get_odoo_uid()
            token = gcom_auth()
            models = get_odoo_models()
            d0 = datetime.strptime(inicio, "%Y%m%d")
            d1 = datetime.strptime(fim, "%Y%m%d")
            d = d0
            while d <= d1:
                data = d.strftime("%Y%m%d")
                for id_etb in PDV_MAP:
                    try:
                        gcom_sincronizar(id_etb, data, token=token, uid=uid, models=models)
                    except Exception:
                        pass
                d = d + timedelta(days=1)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()
    return {"status": "iniciado", "inicio": inicio, "fim": fim}

# =========================
# CRON
# =========================

@app.get("/cron")
def cron():
    def run():
        try:
            sicoob_auto_sync()
        except Exception:
            pass
        try:
            stone_auto_sync()
        except Exception:
            pass
        try:
            gcom_auto_sync()
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()
    return {"status": "iniciado", "timestamp": datetime.now().isoformat()}

# =========================
# DIAGNÓSTICO STONE
# =========================

@app.get("/stone/diagnostico")
def stone_diagnostico():
    import threading
    resultados = {}
    hoje = datetime.now()
    data = (hoje - timedelta(days=1)).strftime("%Y%m%d")
    
    def testar(stonecode, info):
        try:
            r = stone_buscar_xml(stonecode, data)
            if r.status_code == 200:
                transacoes = stone_parse_transacoes(r.content)
                resultados[stonecode] = {
                    "nome": info["name"],
                    "status_http": r.status_code,
                    "transacoes": len(transacoes),
                    "tamanho_bytes": len(r.content),
                }
            else:
                resultados[stonecode] = {
                    "nome": info["name"],
                    "status_http": r.status_code,
                    "erro": r.text[:200],
                }
        except Exception as e:
            resultados[stonecode] = {"nome": info["name"], "erro": str(e)[:200]}

    threads = []
    for sc, info in STONE_JOURNALS.items():
        t = threading.Thread(target=testar, args=(sc, info))
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=30)

    return {"data_testada": data, "resultados": resultados}

# =========================
# DEBUG
# =========================

@app.get("/odoo/teste")
def testar_odoo():
    uid = get_odoo_uid()
    return {"uid": uid, "status": "conectado" if uid else "falhou"}

@app.get("/stone/debug-key")
def stone_debug_key():
    return {"stone_key_ok": bool(STONE_API_KEY), "odoo_key_ok": bool(ODOO_API_KEY)}
