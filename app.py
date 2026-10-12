import os, re, json, random, tempfile, shutil, html
from datetime import datetime
import pandas as pd
import streamlit as st
from supabase import create_client

if "GEMINI_API_KEY" in st.secrets:
    os.environ["GEMINI_API_KEY"] = st.secrets["GEMINI_API_KEY"]

from pipeline import (
    SolicitacaoIncorporacao, validar_documentacao,
    analisar_pasta_pdfs, MODELO_DECISAO, MODELO_EXTRACAO_PDF,
)

st.set_page_config(
    page_title="Incorporações de Rede | Distribuidora de Energia Brasil",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');
:root{--navy:#0A2540;--azul:#1D4E89;--ceu:#EAF1F8;--papel:#F5F7FA;--linha:#D5DDE6;
--ambar:#F2A900;--ok:#1E7B4F;--aviso:#A86A00;--erro:#B3261E;--texto:#1B2733;}
html, body, .stApp, .stMarkdown, label, input, textarea, button, h1, h2, h3, h4 {
  font-family:'IBM Plex Sans', sans-serif;}
.stApp{background:var(--papel); color:var(--texto);}
#MainMenu, footer{visibility:hidden;}
[data-testid="stHeader"]{background:transparent;}
.block-container{max-width:1080px; padding-top:1.5rem; padding-bottom:4rem;}
h2, h3{color:var(--navy); font-weight:600;}
.topo{display:flex; align-items:center; gap:16px; background:var(--navy); color:#fff;
  padding:22px 28px; border-radius:12px; border-bottom:4px solid var(--ambar); margin-bottom:20px;}
.marca{width:46px; height:46px; border-radius:10px; background:var(--ambar);
  display:flex; align-items:center; justify-content:center; flex:none;}
.nome{font-size:1.55rem; font-weight:700; line-height:1.15;}
.sub{font-size:1rem; opacity:.85; margin-top:2px;}
.passos{display:flex; gap:10px; margin:6px 0 22px; flex-wrap:wrap;}
.passo{display:flex; align-items:center; gap:10px; padding:10px 16px; background:#fff;
  border:1px solid var(--linha); border-radius:999px; color:#5B6B7B; font-weight:500;}
.passo .n{width:26px; height:26px; border-radius:50%; background:var(--ceu); color:var(--azul);
  display:flex; align-items:center; justify-content:center; font-weight:700; font-size:.85rem;}
.passo.atual{border-color:var(--azul); color:var(--navy); box-shadow:0 0 0 2px var(--ceu);}
.passo.atual .n{background:var(--azul); color:#fff;}
.passo.feito{color:var(--ok);}
.passo.feito .n{background:var(--ok); color:#fff;}
[data-testid="stForm"]{background:#fff; border:1px solid var(--linha); border-radius:12px; padding:1.6rem;}
[data-testid="stFileUploader"] section{background:#fff; border:2px dashed #9FB3C8; border-radius:12px;}
.stButton button, [data-testid="stFormSubmitButton"] button, .stDownloadButton button{
  background:var(--navy); color:#fff; border:none; border-radius:8px; padding:.65rem 1.5rem; font-weight:600;}
.stButton button:hover, [data-testid="stFormSubmitButton"] button:hover, .stDownloadButton button:hover{
  background:var(--azul); color:#fff;}
.stButton button:disabled{background:#B8C4D0; color:#fff;}
.status{border-radius:12px; padding:22px 26px; color:#fff; margin:6px 0 18px;}
.status.ok{background:var(--ok);} .status.aviso{background:var(--aviso);} .status.erro{background:var(--erro);}
.status .t{font-size:1.6rem; font-weight:700;}
.status .s{opacity:.92; margin-top:2px;}
.barra{height:8px; background:rgba(255,255,255,.3); border-radius:4px; margin-top:12px; overflow:hidden;}
.barra span{display:block; height:100%; background:#fff;}
.bloco{background:#fff; border:1px solid var(--linha); border-left:5px solid var(--azul);
  border-radius:10px; padding:14px 18px; margin-bottom:14px;}
.bloco.ok{border-left-color:var(--ok);} .bloco.falta{border-left-color:var(--erro);}
.bloco.aviso{border-left-color:var(--ambar);}
.bloco h4{margin:0 0 6px; font-size:1.02rem; color:var(--navy);}
.bloco ul{margin:0; padding-left:1.1rem;} .bloco li{margin:3px 0;} .bloco p{margin:0;}
.rodape{margin-top:2.5rem; color:#6B7A89; font-size:.85rem; border-top:1px solid var(--linha); padding-top:12px;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

TOPO = (
    '<div class="topo"><div class="marca">'
    '<svg width="26" height="26" viewBox="0 0 24 24" fill="#0A2540"><path d="M13 2 4 14h6l-1 8 9-12h-6z"/></svg>'
    '</div><div><div class="nome">Incorporações de Rede</div>'
    '<div class="sub">Distribuidora de Energia Brasil</div></div></div>'
)
RODAPE = (
    '<div class="rodape">Análise automática conforme a NT.017.EQTL – Revisão 04. '
    'O resultado apoia a conferência e não substitui a decisão do analista.</div>'
)


def passos(atual):
    itens = ["Cadastro", "Envio dos PDFs", "Resultado"]
    h = '<div class="passos">'
    for i, t in enumerate(itens, 1):
        cls = "feito" if i < atual else ("atual" if i == atual else "")
        h += f'<div class="passo {cls}"><span class="n">{i}</span>{t}</div>'
    st.markdown(h + "</div>", unsafe_allow_html=True)


def bloco(titulo, itens, classe=""):
    if not itens:
        return ""
    li = "".join(f"<li>{html.escape(str(i))}</li>" for i in itens)
    return f'<div class="bloco {classe}"><h4>{titulo}</h4><ul>{li}</ul></div>'


@st.cache_resource
def db():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


TIPOS = {
    "Incorporação (com ressarcimento)": "incorporacao_rede",
    "Doação — Rede/Transformador NOVO": "doacao_rede_nova",
    "Doação — Rede/Transformador EXISTENTE": "doacao_rede_existente",
}

# ---------------- ÁREA DO ADMINISTRADOR (barra lateral) ----------------
with st.sidebar:
    st.header("Administração")
    senha = st.text_input("Senha", type="password")
    admin = bool(senha) and senha == st.secrets.get("ADMIN_SENHA", None)

st.markdown(TOPO, unsafe_allow_html=True)

if admin:
    st.subheader("Obras enviadas")
    dados = db().table("submissoes").select(
        "criado_em,responsavel_email,protocolo,tipo,status,score"
    ).order("criado_em", desc=True).execute().data
    st.dataframe(pd.DataFrame(dados), use_container_width=True)
    st.subheader("Responsáveis cadastrados")
    rts = db().table("responsaveis").select("*").order("criado_em", desc=True).execute().data
    st.dataframe(pd.DataFrame(rts), use_container_width=True)
    st.stop()

# ---------------- ETAPA 1: CADASTRO ----------------
if "rt" not in st.session_state:
    passos(1)
    st.subheader("Cadastro do responsável técnico")
    with st.form("cadastro"):
        c1, c2 = st.columns(2)
        with c1:
            nome = st.text_input("Nome completo *")
            email = st.text_input("E-mail *")
            empresa = st.text_input("Empresa")
        with c2:
            crea = st.text_input("CREA / CFT")
            telefone = st.text_input("Telefone")
        ok = st.form_submit_button("Cadastrar e continuar")
    if ok:
        if not nome.strip() or "@" not in email:
            st.error("Preencha o nome e um e-mail válido.")
        else:
            rt = {"nome": nome.strip(), "crea": crea.strip(), "email": email.strip().lower(),
                  "telefone": telefone.strip(), "empresa": empresa.strip()}
            db().table("responsaveis").upsert(rt, on_conflict="email").execute()
            st.session_state["rt"] = rt
            st.rerun()
    st.markdown(RODAPE, unsafe_allow_html=True)
    st.stop()

rt = st.session_state["rt"]
tem_resultado = bool(st.session_state.get("resultado"))
passos(3 if tem_resultado else 2)
st.caption(f"Responsável técnico: {rt['nome']}")

# ---------------- ETAPA 2: ENVIO ----------------
st.subheader("Envio da documentação da obra")
col1, col2 = st.columns(2)
with col1:
    tipo = TIPOS[st.radio("Tipo de obra", list(TIPOS))]
    protocolo = st.text_input(
        "Protocolo", f"{datetime.now().year}-{random.randint(100000, 999999)}"
    )
with col2:
    restituicao = st.checkbox(
        "Há restituição de valores ao cliente? (só em incorporação)",
        disabled=(tipo != "incorporacao_rede"),
    )
    isencao = st.checkbox(
        "Confirmo que este pedido NÃO é acesso a geração distribuída, "
        "NÃO é migração para mercado livre e NÃO tem situação omissa à "
        "Norma Técnica (só relevante se a subestação for ≤300kVA)"
    )

arquivos = st.file_uploader("Anexe os PDFs da obra", type="pdf", accept_multiple_files=True)

if st.button("Analisar documentação", type="primary", disabled=not arquivos):
    pasta = tempfile.mkdtemp()
    try:
        for f in arquivos:
            with open(os.path.join(pasta, f.name), "wb") as out:
                out.write(f.getbuffer())

        with st.spinner("Lendo os PDFs (pode levar alguns minutos)..."):
            ext = analisar_pasta_pdfs(pasta, tipo, modelo=MODELO_EXTRACAO_PDF)

        info = ext["info_geral"]
        sol = SolicitacaoIncorporacao(
            numero_protocolo=protocolo,
            tipo_solicitacao=tipo,
            nome_responsavel=info["nome_responsavel"] or "Nome do Responsável (não encontrado nos PDFs)",
            cpf_cnpj=info["cpf_cnpj"] or "000.000.000-00 (não encontrado nos PDFs)",
            endereco_obra=info["endereco_obra"] or "Endereço não encontrado nos PDFs",
            descricao_obra=info["descricao_obra"] or "Descrição não encontrada nos PDFs",
            data_aprovacao_projeto=ext["data_aprovacao_projeto"],
            data_aprovacao_orcamento=ext["data_aprovacao_orcamento"],
            possui_restituicao_valores=(restituicao and tipo == "incorporacao_rede"),
            documentos_apresentados=ext["documentos_apresentados"],
            potencia_transformador_kva=ext.get("potencia_transformador_kva"),
            condicoes_isencao_confirmadas=(True if isencao else None),
        )

        with st.spinner("Validando conforme a NT.017..."):
            res = validar_documentacao(sol, modelo=MODELO_DECISAO)

        # guarda PDFs e resultado no Supabase
        seguro = re.sub(r"[^a-zA-Z0-9_.-]", "_", rt["email"])
        proto_seg = re.sub(r"[^a-zA-Z0-9_.-]", "_", protocolo)
        caminhos = []
        for f in arquivos:
            nome_seg = re.sub(r"[^a-zA-Z0-9_.-]", "_", f.name)
            caminho = f"{seguro}/{proto_seg}/{nome_seg}"
            db().storage.from_("pdfs").upload(
                caminho, f.getvalue(),
                {"content-type": "application/pdf", "upsert": "true"},
            )
            caminhos.append(caminho)

        db().table("submissoes").insert({
            "responsavel_email": rt["email"], "protocolo": protocolo, "tipo": tipo,
            "status": res.status, "score": res.score_conformidade,
            "resultado": json.loads(json.dumps(res.para_dict(), default=str)),
            "arquivos": caminhos,
        }).execute()

        st.session_state["resultado"] = res
        st.session_state["extracao"] = ext
        st.rerun()
    except Exception as e:
        st.error(f"Erro no processamento: {e}")
    finally:
        shutil.rmtree(pasta, ignore_errors=True)

# ---------------- ETAPA 3: RESULTADO ----------------
res = st.session_state.get("resultado")
if res:
    ext = st.session_state["extracao"]
    st.divider()
    st.subheader(f"Resultado da análise — protocolo {res.numero_protocolo}")

    rotulo, classe = {
        "APROVADO": ("Aprovado", "ok"),
        "APROVADO_COM_RESSALVAS": ("Aprovado com ressalvas", "aviso"),
        "REPROVADO": ("Reprovado", "erro"),
    }.get(res.status, (str(res.status), "aviso"))
    try:
        pct = max(0, min(100, int(res.score_conformidade)))
    except Exception:
        pct = 0
    st.markdown(
        f'<div class="status {classe}"><div class="t">{rotulo}</div>'
        f'<div class="s">Conformidade documental: {pct}/100</div>'
        f'<div class="barra"><span style="width:{pct}%"></span></div></div>',
        unsafe_allow_html=True,
    )

    for aviso in ext.get("avisos_datas", []):
        st.warning(aviso)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown(bloco("Documentos reconhecidos", res.documentos_reconhecidos, "ok"), unsafe_allow_html=True)
        st.markdown(bloco("Documentos faltantes", res.documentos_faltantes, "falta"), unsafe_allow_html=True)
    with c2:
        pend = list(res.pendencias_condicionais) + list(res.alertas_ativos)
        st.markdown(bloco("Pendências e alertas", pend, "aviso"), unsafe_allow_html=True)
        st.markdown(bloco("Recomendações", res.recomendacoes), unsafe_allow_html=True)

    st.markdown(
        f'<div class="bloco"><h4>Próximo passo</h4><p>{html.escape(str(res.proximo_passo))}</p></div>'
        f'<div class="bloco"><h4>Justificativa técnica</h4><p>{html.escape(str(res.justificativa_tecnica))}</p></div>',
        unsafe_allow_html=True,
    )

    st.download_button(
        "Baixar resultado (JSON)",
        json.dumps(res.para_dict(), ensure_ascii=False, indent=2, default=str),
        file_name=f"resultado_{res.numero_protocolo}.json",
        mime="application/json",
    )

st.markdown(RODAPE, unsafe_allow_html=True)
