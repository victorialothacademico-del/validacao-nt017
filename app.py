import os, re, json, random, tempfile, shutil
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

st.set_page_config(page_title="Validação Documental NT.017", layout="wide")

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

if admin:
    st.title("📋 Obras enviadas")
    dados = db().table("submissoes").select(
        "criado_em,responsavel_email,protocolo,tipo,status,score"
    ).order("criado_em", desc=True).execute().data
    st.dataframe(pd.DataFrame(dados), use_container_width=True)
    st.subheader("Responsáveis cadastrados")
    rts = db().table("responsaveis").select("*").order("criado_em", desc=True).execute().data
    st.dataframe(pd.DataFrame(rts), use_container_width=True)
    st.stop()

# ---------------- TELA 1: CADASTRO ----------------
st.title("🔌 Validação Documental — Incorporação/Doação de Rede")
st.caption("CEEE-D / Equatorial Energia | NT.017.EQTL – Revisão 04")

if "rt" not in st.session_state:
    st.subheader("1. Cadastro do responsável técnico")
    with st.form("cadastro"):
        nome = st.text_input("Nome completo *")
        crea = st.text_input("CREA / CFT")
        email = st.text_input("E-mail *")
        telefone = st.text_input("Telefone")
        empresa = st.text_input("Empresa")
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
    st.stop()

rt = st.session_state["rt"]
st.success(f"Olá, {rt['nome']}!")

# ---------------- TELA 2: ENVIO ----------------
st.subheader("2. Envio da documentação da obra")
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

if st.button("▶ Processar documentação", type="primary", disabled=not arquivos):
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
    except Exception as e:
        st.error(f"Erro no processamento: {e}")
    finally:
        shutil.rmtree(pasta, ignore_errors=True)

# ---------------- TELA 3: RESULTADO ----------------
res = st.session_state.get("resultado")
if res:
    ext = st.session_state["extracao"]
    mostrar = {"APROVADO": st.success, "REPROVADO": st.error,
               "APROVADO_COM_RESSALVAS": st.warning}.get(res.status, st.info)
    mostrar(f"{res.status}  |  Conformidade: {res.score_conformidade}/100")

    for aviso in ext.get("avisos_datas", []):
        st.warning(aviso)

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Documentos reconhecidos")
        for d in res.documentos_reconhecidos:
            st.write("✓", d)
        st.subheader("Documentos faltantes")
        for d in res.documentos_faltantes:
            st.write("✗", d)
    with c2:
        st.subheader("Pendências e alertas")
        for p in list(res.pendencias_condicionais) + list(res.alertas_ativos):
            st.write("⚠️", p)
        st.subheader("Recomendações")
        for r in res.recomendacoes:
            st.write("→", r)

    st.info(f"**Próximo passo:** {res.proximo_passo}")
    st.markdown(f"**Justificativa técnica:** {res.justificativa_tecnica}")

    st.download_button(
        "Baixar resultado (JSON)",
        json.dumps(res.para_dict(), ensure_ascii=False, indent=2, default=str),
        file_name=f"resultado_{res.numero_protocolo}.json",
        mime="application/json",
    )
