"""
GCOM -> Odoo: gera lancamentos de venda (rascunho) no diario "Vendas GCOM".
Modulo plugavel ao sicoob-odoo-api (mesmo padrao XML-RPC do main.py).

No main.py basta:
    from gcom import gcom_auto_sync, gcom_sincronizar   # opcional p/ rotas
    # dentro de /cron, na funcao run():
    try: gcom_auto_sync()
    except Exception: pass

Variaveis de ambiente novas (Render):
    GCOM_EMPRESA, GCOM_USUARIO, GCOM_SENHA
Reutiliza as ja existentes: ODOO_URL/DB/USER + ODOO_API_KEY.
"""

import os
import xmlrpc.client
import requests
from datetime import datetime, timedelta

# ---- Odoo (mesmos dados do main.py) ----
ODOO_URL = "https://gmmholding.odoo.com"
ODOO_DB = "gmmholding"
ODOO_USER = "leandro.rabelo@gmail.com"
ODOO_API_KEY = os.getenv("ODOO_API_KEY")

# ---- GCOM ----
GCOM_BASE = "https://www2.gcom.com.br"
ID_EMP_GCOM = 105327
ID_PES_USU = 94705
ID_MRC = 58

# Cabecalhos de navegador (o WAF do GCOM bloqueia user-agent de bot com 403)
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Origin": "https://www2.gcom.com.br",
    "Referer": "https://www2.gcom.com.br/novoerp/",
}

# ---- Odoo: diario / contas fixas ----
JOURNAL_ID = 43          # Vendas GCOM
REVENUE_ACCOUNT = 912    # 3.01.01.01.01.05 Receita da Revenda de Mercadorias
ADICIONAL_ACCOUNT = 1475 # 3.01.01.01.01.50 RECEITA - ADICIONAL DELIVERY
SYNC_DAYS = 7  # janela fixa de 7 dias (ignora env GCOM_SYNC_DAYS que estava pinada em 3); dedup torna dias extras inofensivos

# id_etb (GCOM) -> contas/analitica (Odoo)
PDV_MAP = {
    7318: {"nome": "Drive Assis",                 "analytic": 1, "caixa": 51,   "debito": 1424, "credito": 1425, "tefpix": 1429},
    7290: {"nome": "Caldas Novas (Trevo)",        "analytic": 8, "caixa": 1418, "debito": 1430, "credito": 1431, "tefpix": 1432},
    7189: {"nome": "Portal Sul (PS0001)",         "analytic": 6, "caixa": 1419, "debito": 1437, "credito": 1438, "tefpix": 1439},
    7190: {"nome": "Cerrado (PS0002)",            "analytic": 7, "caixa": 1420, "debito": 1443, "credito": 1444, "tefpix": 1445},
    7179: {"nome": "Portal Shopping (PS0003)",    "analytic": 5, "caixa": 1421, "debito": 1448, "credito": 1449, "tefpix": 1450},
    7180: {"nome": "Quiosque Aparecida (PS0004)", "analytic": 4, "caixa": 1422, "debito": 1454, "credito": 1455, "tefpix": 1456},
    7319: {"nome": "Loja Aparecida (PS0005)",     "analytic": 3, "caixa": 1423, "debito": 1460, "credito": 1461, "tefpix": 1462},
}

# id_fma_pgt (GCOM) -> chave de conta no PDV_MAP
FORMA_MAP = {
    1:  "debito",   # CARTAO DEBITO
    4:  "credito",  # CARTAO DE CREDITO
    5:  "caixa",    # DINHEIRO
    35: "tefpix",   # OUTRAS FORMAS (PIX/delivery)
    24: "tefpix",   # CARTAO BENEFICIO
}
FORMA_DEFAULT = "tefpix"


# ----------------------------- GCOM -----------------------------
def gcom_auth():
    r = requests.post(
        f"{GCOM_BASE}/api/GcomUsuarioService/usuario/",
        headers={**BROWSER_HEADERS, "Content-Type": "application/json"},
        json={"empresa": os.getenv("GCOM_EMPRESA"),
              "usuario": os.getenv("GCOM_USUARIO"),
              "senha":   os.getenv("GCOM_SENHA")},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["accessToken"]


def _h(token):
    return {**BROWSER_HEADERS,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"}


def gcom_base_roy(token, id_etb, dia):  # dia = datetime/date
    d = dia.strftime("%d/%m/%Y")
    r = requests.post(
        f"{GCOM_BASE}/api/GcomVendaService/RoyaltiesFundoMarketing/SelecionarRoyaltiesFundoMarketing",
        headers=_h(token),
        json={"id_etb_gcom": str(id_etb), "dt_de": d, "dt_ate": d, "id_mrc": ID_MRC},
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json() or []
    return float(rows[0].get("base_roy_mkt") or 0.0) if rows else 0.0


def gcom_formas(token, id_etb, dia):
    d = dia.strftime("%d/%m/%Y")
    r = requests.post(
        f"{GCOM_BASE}/api/GcomDashboardService/Dashlet/VendaFormaPagamentoDashlet",
        headers=_h(token),
        json={"ID_EMP_GCOM": ID_EMP_GCOM, "ID_PES_USU": ID_PES_USU, "ID_VISAO": 1,
              "ID_DASHLET": 10, "SEQ": 10, "DC_COR_SECUNDARIA": "#dbead5",
              "ID_ETB_GCOM": id_etb, "ID_MRC": ID_MRC, "DT_DE": d, "DT_ATE": d},
        timeout=30,
    )
    r.raise_for_status()
    return r.json() or []


# ----------------------- montagem do lancamento -----------------------
def build_move_vals(id_etb, dia, base_roy, formas):
    pdv = PDV_MAP[id_etb]
    analytic = {str(pdv["analytic"]): 100}

    por_conta, avisos = {}, []
    for f in formas:
        idf = int(f.get("id_fma_pgt"))
        val = round(float(f.get("vl_pgt") or 0.0), 2)
        if val == 0:
            continue
        chave = FORMA_MAP.get(idf)
        if chave is None:
            chave = FORMA_DEFAULT
            avisos.append(f"forma desconhecida id={idf} ({f.get('dc_fma_pgt')}) -> {chave}")
        acc = pdv[chave]
        por_conta[acc] = round(por_conta.get(acc, 0.0) + val, 2)

    total_formas = round(sum(por_conta.values()), 2)
    base = round(float(base_roy or 0.0), 2)
    if total_formas == 0 and base == 0:
        return None, avisos

    adicional = round(total_formas - base, 2)
    lines = []
    for acc, val in por_conta.items():
        lines.append((0, 0, {"account_id": acc, "name": "Recebimento venda",
                             "debit": val, "credit": 0.0, "analytic_distribution": analytic}))
    lines.append((0, 0, {"account_id": REVENUE_ACCOUNT, "name": "Receita de vendas",
                         "debit": 0.0, "credit": base, "analytic_distribution": analytic}))
    if adicional > 0:
        lines.append((0, 0, {"account_id": ADICIONAL_ACCOUNT, "name": "Adicional delivery",
                             "debit": 0.0, "credit": adicional, "analytic_distribution": analytic}))
    elif adicional < 0:
        lines.append((0, 0, {"account_id": ADICIONAL_ACCOUNT, "name": "Ajuste fechamento",
                             "debit": -adicional, "credit": 0.0, "analytic_distribution": analytic}))

    ref = f"Vendas GCOM | {pdv['nome']} | {dia.strftime('%d/%m/%Y')}"
    vals = {"journal_id": JOURNAL_ID, "date": dia.strftime("%Y-%m-%d"),
            "ref": ref, "move_type": "entry", "line_ids": lines}
    return vals, avisos


# ----------------------------- Odoo (XML-RPC) -----------------------------
def get_odoo_uid():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    return common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})


def get_odoo_models():
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


def _odoo(models, uid, model, method, args, kwargs=None):
    return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, model, method, args, kwargs or {})


# ----------------------------- sincronizacao -----------------------------
def gcom_sincronizar(id_etb, data, token=None, uid=None, models=None):
    """Cria o rascunho de 1 PDV em 1 dia. data = 'YYYYMMDD'."""
    if id_etb not in PDV_MAP:
        return {"status": "erro", "mensagem": f"PDV {id_etb} nao mapeado"}
    dia = datetime.strptime(data, "%Y%m%d")
    token = token or gcom_auth()
    if uid is None:
        uid = get_odoo_uid()
    if models is None:
        models = get_odoo_models()

    base = gcom_base_roy(token, id_etb, dia)
    formas = gcom_formas(token, id_etb, dia)
    vals, avisos = build_move_vals(id_etb, dia, base, formas)
    if not vals:
        return {"status": "ok", "pdv": PDV_MAP[id_etb]["nome"], "criados": 0, "msg": "sem venda"}

    ja = _odoo(models, uid, "account.move", "search",
               [[["ref", "=", vals["ref"]], ["journal_id", "=", JOURNAL_ID]]], {"limit": 1})
    if ja:
        return {"status": "ok", "pdv": PDV_MAP[id_etb]["nome"], "criados": 0,
                "duplicados": 1, "ref": vals["ref"]}

    move_id = _odoo(models, uid, "account.move", "create", [vals])
    return {"status": "ok", "pdv": PDV_MAP[id_etb]["nome"], "criados": 1,
            "move_id": move_id, "base_roy_mkt": base, "ref": vals["ref"], "avisos": avisos}


def gcom_auto_sync():
    """Varre todos os PDVs nos ultimos SYNC_DAYS dias (deduplicando)."""
    uid = get_odoo_uid()
    if not uid:
        return {"status": "erro", "mensagem": "Falha autenticacao Odoo"}
    token = gcom_auth()
    models = get_odoo_models()
    hoje = datetime.now()
    resultados = []
    for dias_atras in range(1, SYNC_DAYS + 1):
        data = (hoje - timedelta(days=dias_atras)).strftime("%Y%m%d")
        for id_etb in PDV_MAP:
            try:
                r = gcom_sincronizar(id_etb, data, token=token, uid=uid, models=models)
            except Exception as e:
                r = {"status": "erro", "pdv": PDV_MAP[id_etb]["nome"], "erro": str(e)}
            r["data"] = data
            resultados.append(r)
    return {
        "status": "ok",
        "total_criados": sum(r.get("criados", 0) for r in resultados),
        "total_duplicados": sum(r.get("duplicados", 0) for r in resultados),
        "erros": [f'{r.get("pdv")} {r.get("data")}: {r["erro"]}' for r in resultados if r.get("erro")],
        "resultados": resultados,
    }


if __name__ == "__main__":
    import sys, json
    if "--dry-run" in sys.argv:
        dia = datetime(2026, 6, 24)
        formas = [
            {"id_fma_pgt": 1,  "dc_fma_pgt": "CARTAO DEBITO",     "vl_pgt": 1780.25},
            {"id_fma_pgt": 4,  "dc_fma_pgt": "CARTAO DE CREDITO", "vl_pgt": 1368.25},
            {"id_fma_pgt": 35, "dc_fma_pgt": "OUTRAS FORMAS",     "vl_pgt": 3656.80},
            {"id_fma_pgt": 5,  "dc_fma_pgt": "DINHEIRO",          "vl_pgt": 269.65},
        ]
        vals, avisos = build_move_vals(7318, dia, 7073.85, formas)
        deb = round(sum(l[2]["debit"] for l in vals["line_ids"]), 2)
        cre = round(sum(l[2]["credit"] for l in vals["line_ids"]), 2)
        print("REF:", vals["ref"])
        for l in vals["line_ids"]:
            d = l[2]
            print(f"  {d['account_id']:>5} {d['name']:<20} D {d['debit']:>9.2f} C {d['credit']:>9.2f}")
        print(f"TOTAL D {deb:.2f} = C {cre:.2f} -> {'OK' if deb==cre else 'ERRO'}")
