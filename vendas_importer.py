"""
Importador do relatorio analitico do GCOM ("Vendas por Periodo (Analitico)",
CONSULTAVNDPER.ASP, exportado como Excel/HTML ou TXT) -> Odoo.

Roteia cada forma de pagamento GRANULAR para a conta transitoria correta do PDV
e cria o lancamento de venda (rascunho por padrao) no diario VGCOM (43).

Escopo atual: PS0003 (SIGLA BQPOS). Outras filiais sao reportadas como nao
configuradas (nao geram lancamento) ate terem contas/rota definidas.

No main.py:
    from vendas_importer import importar_vendas, FORM_HTML as VENDAS_FORM_HTML
    @app.get("/gcom/vendas/importar", response_class=HTMLResponse)
    def vendas_form(): return VENDAS_FORM_HTML
    @app.post("/gcom/vendas/importar")
    async def vendas_post(file: UploadFile = File(...), postar: bool = Form(False)):
        return importar_vendas(await file.read(), file.filename, postar)
"""
import re
import unicodedata
from html.parser import HTMLParser
from datetime import datetime

from gcom import get_odoo_uid, get_odoo_models, ODOO_DB, ODOO_API_KEY

JOURNAL_VGCOM = 43
REVENUE_ACCOUNT = 912

# SIGLA (GCOM) -> config do PDV no Odoo
PDV_CFG = {
    "BQPOS": {"nome": "PS0003 (Portal Shopping)", "analytic": 5},
}


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.strip().upper())


# roteamento forma granular -> conta transitoria (PS0003)
def _route_ps0003(forma):
    f = _norm(forma)
    if "PIX" in f:
        return 1450                                   # PIX -> PicPay (inclui PIX POS)
    if "IFOOD" in f:
        return 1491
    if "99" in f:
        return 1492
    if "PLUXEE" in f or "SODEXO" in f:
        return 1493
    if "TICKET" in f:
        return 1494
    if "ALELO" in f:
        return 1495
    if "DINHEIRO" in f:
        return 1421
    if "MAESTRO" in f or "ELECTRON" in f or "DEBITO" in f:
        return 1448                                   # debito -> Stone
    if "MASTERCARD" in f or "MASTER" in f or "VISA" in f or "CREDITO" in f:
        return 1449                                   # credito -> Stone
    return None


ROUTERS = {"BQPOS": _route_ps0003}


def _num_cent(x):
    s = re.sub(r"[^\d]", "", str(x or ""))
    return int(s) / 100 if s else 0.0


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self._row = None
        self._cell = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell:
            self._row.append("".join(self._buf).strip())
            self._cell = False
            self._buf = []
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell:
            self._buf.append(data)


DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}")


def _parse_rows(conteudo):
    if isinstance(conteudo, (bytes, bytearray)):
        txt = conteudo.decode("ISO-8859-1", errors="replace")
    else:
        txt = conteudo
    p = _TableParser()
    p.feed(txt)
    return p.rows


# Posicoes fixas das colunas nas LINHAS DE DADOS do relatorio analitico
# (o cabecalho tem celulas mescladas/colspan que desalinham; as linhas de dados sao estaveis).
FIXED_IDX = {"sigla": 2, "data": 7, "receb": 11, "forma": 20}
_LABELS_ESPERADOS = ["DATA VENDA", "TOTAL RECEBIMENTO", "FORMAS DE PAGAMENTO", "SIGLA"]


def _col_index(rows):
    """Confirma a identidade do relatorio (rotulos presentes no cabecalho) e
    devolve os indices fixos das colunas nas linhas de dados."""
    header_text = " ".join(_norm(c) for r in rows[:12] for c in r)
    if all(lbl in header_text for lbl in _LABELS_ESPERADOS):
        return dict(FIXED_IDX)
    return {}


def importar_vendas(conteudo, filename=None, postar=False):
    uid = get_odoo_uid()
    if not uid:
        return {"status": "erro", "mensagem": "Falha autenticacao Odoo"}
    models = get_odoo_models()

    def odoo(model, method, args, kwargs=None):
        return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, model, method, args, kwargs or {})

    try:
        rows = _parse_rows(conteudo)
    except Exception as e:
        return {"status": "erro", "mensagem": f"Falha ao ler arquivo: {e}"}

    idx = _col_index(rows)
    faltando = [k for k in ("data", "receb", "forma", "sigla") if k not in idx]
    if faltando:
        return {"status": "erro",
                "mensagem": f"Colunas nao encontradas: {faltando}. O arquivo e o relatorio 'Vendas por Periodo (Analitico)'?"}

    # agrupa: (sigla, data_iso) -> {conta: valor}
    grupos = {}
    siglas_ignoradas, unmapped = set(), set()
    nlin = 0
    maxi = max(idx.values())
    for r in rows:
        if len(r) <= maxi:
            continue
        dv = str(r[idx["data"]]).strip()
        if not DATE_RE.match(dv):
            continue
        sigla = _norm(r[idx["sigla"]])
        if sigla not in PDV_CFG:
            siglas_ignoradas.add(sigla)
            continue
        conta = ROUTERS[sigla](r[idx["forma"]])
        if conta is None:
            unmapped.add(str(r[idx["forma"]]))
            continue
        val = _num_cent(r[idx["receb"]])
        if val == 0:
            continue
        data_iso = datetime.strptime(dv[:10], "%d/%m/%Y").strftime("%Y-%m-%d")
        g = grupos.setdefault((sigla, data_iso), {})
        g[conta] = round(g.get(conta, 0.0) + val, 2)
        nlin += 1

    criados, dups = [], []
    for (sigla, data_iso), mp in sorted(grupos.items()):
        cfg = PDV_CFG[sigla]
        an = {str(cfg["analytic"]): 100}
        ref = f"VGCOM-ANALITICO | {cfg['nome']} | {data_iso}"
        if odoo("account.move", "search",
                [[["ref", "=", ref], ["journal_id", "=", JOURNAL_VGCOM]]], {"limit": 1}):
            dups.append(ref)
            continue
        lines, total = [], 0.0
        for conta, val in sorted(mp.items()):
            lines.append((0, 0, {"account_id": conta, "name": "Recebimento venda (analitico)",
                                 "debit": val, "credit": 0.0, "analytic_distribution": an}))
            total = round(total + val, 2)
        lines.append((0, 0, {"account_id": REVENUE_ACCOUNT, "name": "Receita de vendas",
                             "debit": 0.0, "credit": total, "analytic_distribution": an}))
        vals = {"journal_id": JOURNAL_VGCOM, "date": data_iso, "ref": ref,
                "move_type": "entry", "line_ids": lines}
        mid = odoo("account.move", "create", [vals])
        if postar:
            try:
                odoo("account.move", "action_post", [[mid]])
            except Exception:
                pass
        criados.append({"move_id": mid, "pdv": cfg["nome"], "data": data_iso,
                        "total": total, "contas": {str(k): v for k, v in sorted(mp.items())}})

    return {"status": "ok", "linhas_processadas": nlin, "moves_criados": len(criados),
            "moves": criados, "duplicados": dups,
            "siglas_ignoradas": sorted(siglas_ignoradas),
            "formas_nao_mapeadas": sorted(unmapped), "postado": bool(postar)}


FORM_HTML = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Importar vendas GCOM (analitico)</title>
<style>
 body{font-family:system-ui,Arial,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#1a1a1a}
 h1{font-size:20px} .card{border:1px solid #e2e2e2;border-radius:12px;padding:20px;margin-top:16px}
 label{display:block;font-weight:600;margin:12px 0 4px}
 .btn{margin-top:16px;background:#5a3df0;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:15px;cursor:pointer}
 code{background:#f4f4f6;padding:2px 6px;border-radius:4px} small{color:#666}
 #out{white-space:pre-wrap;background:#0f1117;color:#d7e0ff;padding:14px;border-radius:8px;margin-top:16px;display:none;font-size:13px}
</style></head><body>
<h1>Importar vendas GCOM (relatorio analitico)</h1>
<p><small>Exporte o relatorio <code>Vendas por Periodo (Analitico)</code> do GCOM (Excel ou TXT) e suba aqui.
Cada forma de pagamento e roteada para a conta transitoria certa do PDV e vira um lancamento (rascunho) no diario VGCOM. Escopo atual: PS0003 (BQPOS).</small></p>
<div class="card">
 <label>Arquivo do relatorio (.xls/.html/.txt)</label>
 <input id="file" type="file">
 <label><input id="postar" type="checkbox"> Postar automaticamente (senao, cria como rascunho)</label>
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
 if(document.getElementById('postar').checked) fd.append('postar','true');
 try{ const r=await fetch('/gcom/vendas/importar',{method:'POST',body:fd});
   const j=await r.json(); out.textContent=JSON.stringify(j,null,2);
 }catch(e){ out.textContent='Erro: '+e; }
}
</script></body></html>"""
