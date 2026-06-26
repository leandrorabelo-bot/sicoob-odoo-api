"""
Importador de planilha -> Odoo: cria registros em x_pagamentos_rh
(que ja caem nas abas Pagamentos Mensais / Beneficios do cadastro do funcionario).

Depois de importados, o usuario seleciona os registros e roda a acao
"Gerar despesa do lote (RH)" (ir.actions.server 877), que agrega tudo num
unico lancamento (debito despesa por tipo + credito 609) e vincula os pagamentos.

Modulo plugavel ao sicoob-odoo-api (mesmo padrao XML-RPC do gcom.py/main.py).
No main.py:
    from rh_importer import importar_pagamentos_rh, FORM_HTML
    @app.get("/rh/importar", response_class=HTMLResponse)
    def rh_form(): return FORM_HTML
    @app.post("/rh/importar")
    async def rh_post(file: UploadFile = File(...), data_padrao: str = Form(None)):
        return importar_pagamentos_rh(await file.read(), file.filename, data_padrao or None)

requirements.txt: adicionar  openpyxl  e  python-multipart
"""

import io
import csv
import re
import unicodedata
from datetime import datetime, date

from gcom import get_odoo_uid, get_odoo_models, ODOO_DB, ODOO_API_KEY

MODEL = "x_pagamentos_rh"
STATUS_INICIAL = "Pendente"  # exibido como "Lancado" na tela

# Apelidos comuns -> valor exato da selection (normalizados na comparacao)
ALIASES = {
    "13": "13º Salário",
    "13o": "13º Salário",
    "decimo terceiro": "13º Salário",
    "1a parcela 13": "1ª Parcela 13º salário",
    "vt": "Vale Transporte",
    "va": "Vale Alimentação",
    "vr": "Vale Alimentação",
    "adiantamento salarial": "Adiantamento",
    "hora extra": "Dobra",
    "horas extras": "Dobra",
    "premio": "Premiação",
    "uniforme": "Uniforme/EPI",
    "epi": "Uniforme/EPI",
    "rescisao": "Rescisão",
    "ferias": "Férias",
}

# ----------------------------- normalizacao -----------------------------

def _strip_accents(s):
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _norm(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", _strip_accents(str(s)).strip().lower())


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


def _parse_valor(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    s = re.sub(r"[^\d,.\-]", "", s)  # tira R$, espacos, etc.
    if not s:
        return None
    # formato BR: 1.234,56  ->  1234.56
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_data(v, padrao_iso):
    if v is None or v == "":
        return padrao_iso
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return padrao_iso


# ----------------------------- leitura da planilha -----------------------------

# colunas aceitas (normalizadas) -> chave interna
COLMAP = {
    "funcionario": "nome", "nome": "nome", "colaborador": "nome", "empregado": "nome",
    "cpf": "cpf", "documento": "cpf", "doc": "cpf",
    "tipo": "tipo", "tipo de pagamento": "tipo", "ocorrencia": "tipo",
    "valor": "valor", "value": "valor", "r$": "valor",
    "data": "data", "data ocorrencia": "data", "competencia": "data",
}


def _ler_linhas(conteudo, filename):
    """Retorna lista de dicts {nome, cpf, tipo, valor, data} a partir de xlsx ou csv."""
    nome = (filename or "").lower()
    if nome.endswith(".csv") or nome.endswith(".txt"):
        return _ler_csv(conteudo)
    return _ler_xlsx(conteudo)


def _mapear_header(header):
    idx = {}
    for i, h in enumerate(header):
        key = COLMAP.get(_norm(h))
        if key and key not in idx:
            idx[key] = i
    return idx


def _linha_para_dict(idx, row):
    def get(k):
        i = idx.get(k)
        if i is None or i >= len(row):
            return None
        return row[i]
    return {
        "nome": get("nome"),
        "cpf": get("cpf"),
        "tipo": get("tipo"),
        "valor": get("valor"),
        "data": get("data"),
    }


def _ler_xlsx(conteudo):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(conteudo), data_only=True, read_only=True)
    ws = wb.active
    linhas = []
    header_idx = None
    for row in ws.iter_rows(values_only=True):
        if row is None:
            continue
        if header_idx is None:
            cand = _mapear_header(row)
            if "nome" in cand or "cpf" in cand:
                header_idx = cand
            continue
        if all(c is None or str(c).strip() == "" for c in row):
            continue
        linhas.append(_linha_para_dict(header_idx, list(row)))
    return linhas


def _ler_csv(conteudo):
    texto = conteudo.decode("utf-8-sig", errors="replace")
    # detecta separador ; ou ,
    sep = ";" if texto.count(";") >= texto.count(",") else ","
    reader = list(csv.reader(io.StringIO(texto), delimiter=sep))
    linhas = []
    header_idx = None
    for row in reader:
        if not row:
            continue
        if header_idx is None:
            cand = _mapear_header(row)
            if "nome" in cand or "cpf" in cand:
                header_idx = cand
            continue
        if all(str(c).strip() == "" for c in row):
            continue
        linhas.append(_linha_para_dict(header_idx, row))
    return linhas


# ----------------------------- importacao -----------------------------

def importar_pagamentos_rh(conteudo, filename, data_padrao=None):
    uid = get_odoo_uid()
    if not uid:
        return {"status": "erro", "mensagem": "Falha autenticacao Odoo"}
    models = get_odoo_models()

    def odoo(model, method, args, kwargs=None):
        return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, model, method, args, kwargs or {})

    padrao_iso = data_padrao or date.today().strftime("%Y-%m-%d")

    # 1) ler planilha
    try:
        linhas = _ler_linhas(conteudo, filename)
    except Exception as e:
        return {"status": "erro", "mensagem": f"Falha ao ler planilha: {e}"}
    if not linhas:
        return {"status": "erro", "mensagem": "Nenhuma linha encontrada (verifique o cabecalho: Funcionario/CPF, Tipo, Valor, Data)."}

    # 2) selection valida (rotulos exatos) e indice normalizado
    fg = odoo(MODEL, "fields_get", [["x_studio_tipo_de_pagamento"]], {"attributes": ["selection"]})
    sel = [s[0] for s in fg["x_studio_tipo_de_pagamento"]["selection"]]
    tipo_idx = {_norm(t): t for t in sel}
    for ali, alvo in ALIASES.items():
        if _norm(ali) not in tipo_idx and alvo in sel:
            tipo_idx[_norm(ali)] = alvo

    # 3) indice de funcionarios (por CPF e por nome), inclusive inativos
    emps = odoo("hr.employee", "search_read", [[]],
                {"fields": ["name", "identification_id", "department_id"],
                 "context": {"active_test": False}})
    by_cpf, by_name = {}, {}
    for e in emps:
        cpf = _digits(e.get("identification_id"))
        if cpf:
            by_cpf.setdefault(cpf, e)
        by_name.setdefault(_norm(e.get("name")), e)

    criados, duplicados = 0, 0
    detalhes, nao_encontrados, tipos_invalidos = [], [], []

    for n, ln in enumerate(linhas, start=1):
        nome = (str(ln["nome"]).strip() if ln["nome"] else "")
        cpf = _digits(ln["cpf"])
        valor = _parse_valor(ln["valor"])
        data_iso = _parse_data(ln["data"], padrao_iso)

        # resolver funcionario
        emp = None
        if cpf and cpf in by_cpf:
            emp = by_cpf[cpf]
        elif nome and _norm(nome) in by_name:
            emp = by_name[_norm(nome)]
        if not emp:
            nao_encontrados.append(nome or cpf or f"linha {n}")
            detalhes.append({"linha": n, "status": "sem_funcionario", "ref": nome or cpf})
            continue

        # resolver tipo
        tipo = tipo_idx.get(_norm(ln["tipo"]))
        if not tipo:
            tipos_invalidos.append(str(ln["tipo"]))
            detalhes.append({"linha": n, "status": "tipo_invalido", "ref": ln["tipo"]})
            continue

        if valor is None or valor <= 0:
            detalhes.append({"linha": n, "status": "valor_invalido", "ref": str(ln["valor"])})
            continue

        dept = emp.get("department_id")
        dept_id = dept[0] if isinstance(dept, (list, tuple)) and dept else False

        # dedup: mesmo funcionario+tipo+valor+data ainda nao vinculado a lote
        dom = [["x_studio_funcionario", "=", emp["id"]],
               ["x_studio_tipo_de_pagamento", "=", tipo],
               ["x_studio_value", "=", valor],
               ["x_data_ocorrencia", "=", data_iso],
               ["x_studio_despesa_move_id", "=", False]]
        if odoo(MODEL, "search_count", [dom]):
            duplicados += 1
            detalhes.append({"linha": n, "status": "duplicado", "ref": emp["name"]})
            continue

        vals = {
            "x_name": f"{tipo} - {emp['name']}",
            "x_studio_funcionario": emp["id"],
            "x_studio_tipo_de_pagamento": tipo,
            "x_studio_value": valor,
            "x_studio_pdv": dept_id,
            "x_studio_status_1": STATUS_INICIAL,
            "x_data_ocorrencia": data_iso,
        }
        try:
            rec_id = odoo(MODEL, "create", [vals])
            criados += 1
            detalhes.append({"linha": n, "status": "criado", "id": rec_id, "ref": emp["name"]})
        except Exception as e:
            detalhes.append({"linha": n, "status": "erro", "ref": emp["name"], "msg": str(e)})

    return {
        "status": "ok",
        "total_linhas": len(linhas),
        "criados": criados,
        "duplicados": duplicados,
        "nao_encontrados": sorted(set(nao_encontrados)),
        "tipos_invalidos": sorted(set(tipos_invalidos)),
        "detalhes": detalhes,
    }


# ----------------------------- form HTML (GET /rh/importar) -----------------------------

FORM_HTML = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Importar pagamentos RH</title>
<style>
 body{font-family:system-ui,Arial,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#1a1a1a}
 h1{font-size:20px} .card{border:1px solid #e2e2e2;border-radius:12px;padding:20px;margin-top:16px}
 label{display:block;font-weight:600;margin:12px 0 4px} input[type=date]{padding:6px}
 .btn{margin-top:16px;background:#5a3df0;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:15px;cursor:pointer}
 code{background:#f4f4f6;padding:2px 6px;border-radius:4px} small{color:#666}
 #out{white-space:pre-wrap;background:#0f1117;color:#d7e0ff;padding:14px;border-radius:8px;margin-top:16px;display:none;font-size:13px}
</style></head><body>
<h1>Importar pagamentos RH</h1>
<p><small>A planilha (.xlsx ou .csv) deve ter as colunas:
<code>Funcionário</code> e/ou <code>CPF</code>, <code>Tipo</code>, <code>Valor</code> e (opcional) <code>Data</code>.
A loja (analítica) é preenchida pelo departamento do funcionário. Status inicial: Lançado.</small></p>
<div class="card">
 <label>Planilha</label>
 <input id="file" type="file" accept=".xlsx,.csv,.txt">
 <label>Data padrão (linhas sem data)</label>
 <input id="data" type="date">
 <br><button class="btn" onclick="enviar()">Importar</button>
</div>
<div id="out"></div>
<script>
async function enviar(){
 const f=document.getElementById('file').files[0];
 const out=document.getElementById('out'); out.style.display='block';
 if(!f){out.textContent='Selecione um arquivo.';return;}
 out.textContent='Importando...';
 const fd=new FormData(); fd.append('file',f);
 const d=document.getElementById('data').value; if(d) fd.append('data_padrao',d);
 try{
   const r=await fetch('/rh/importar',{method:'POST',body:fd});
   const j=await r.json(); out.textContent=JSON.stringify(j,null,2);
 }catch(e){ out.textContent='Erro: '+e; }
}
</script></body></html>"""
