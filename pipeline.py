import os, io, json, re, time, pathlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, date
from typing import Optional, List
from google import genai
from google.genai import errors as genai_errors
import pypdf
import os
import json
import re
import time
import pathlib
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from google import genai
from google.genai import errors as genai_errors

# ============================================================
# CONFIGURAÇÃO DO MODELO
# ============================================================
# ATENÇÃO — DIAGNÓSTICO DOS TESTES: "gemini-2.5-flash-lite" é o modelo mais
# fraco da família e é a causa mais provável das alucinações observadas
# (documentos que existem no PDF e o modelo não achou; datas lidas errado em
# PDFs grandes/escaneados de 40-120 páginas). Use "gemini-2.5-flash" para a
# ETAPA DE LEITURA DO PDF (Célula 7C). Para a etapa de DECISÃO (Célula 3, que
# já trabalha só com texto curto e estruturado, não com o PDF bruto), o
# flash-lite é aceitável e mais barato.
MODELO_PADRAO = "gemini-2.5-flash"
MODELO_EXTRACAO_PDF = "gemini-2.5-flash"    # usado na Célula 7C para LER os PDFs (precisa de mais capacidade)
MODELO_DECISAO = "gemini-2.5-flash-lite"    # usado na Célula 3 para decidir o status a partir do texto já extraído

# ============================================================
# PRAZOS DE VIGÊNCIA (regra dura, validada em Python — não pela IA)
# ============================================================
PRAZO_VIGENCIA_PROJETO_DIAS = 3 * 365       # Projeto Elétrico Aprovado: 3 anos
PRAZO_VIGENCIA_ORCAMENTO_DIAS = 1 * 365     # Orçamento de Conexão Aprovado: 1 ano (só incorporação)

# ============================================================
# ISENÇÃO DE PROJETO ELÉTRICO PARA SUBESTAÇÕES DE PEQUENO PORTE
# ============================================================
# Pela normativa da distribuidora (confirmado no ofício de Orçamento de Conexão da própria
# CEEE Grupo Equatorial, ex.: protocolo SS 202546238946548): "Para Subestação Particular aérea
# e unitária, até 300 kVA, a CEEE EQUATORIAL dispensa a apresentação de Projeto Elétrico, desde
# que não esteja associado ao pedido de acesso a geração distribuída, migração para o mercado
# livre ou possua situações omissas à Norma Técnica, que necessitem de avaliação da distribuidora."
# NÃO é permitido cobrar Projeto Elétrico de subestações dentro desse limite.
POTENCIA_MAXIMA_ISENCAO_PROJETO_KVA = 300

# ============================================================
# CHECKLIST OFICIAL NT.017.EQTL – Revisão 04
# ============================================================

CHECKLIST_NT017 = {

    # §6.4 — Incorporação de redes (COM ressarcimento de dinheiro ao cliente)
    "incorporacao_rede": {
        "titulo": "Incorporação de Redes (§6.4 NT.017.EQTL Rev.04)",
        "descricao": (
            "Incorporação de Ativos de Obras de Responsabilidade da Distribuidora "
            "construída por Terceiros para Clientes de Média Tensão. "
            "Este processo envolve RESSARCIMENTO DE DINHEIRO ao cliente."
        ),
        "exige_orcamento_conexao": True,   # ÚNICO tipo que exige orçamento de conexão vigente
        "ressarcimento": True,             # sempre há ressarcimento nesse tipo
        "obrigatorios": [
            "CPF e RG do responsável",
            "CPF e RG de uma testemunha",
            "Projeto Aprovado e vigente (prazo de 3 anos)",
            "Orçamento de Conexão aprovado e vigente (prazo de 1 ano)",
            "ART (engenheiro) ou TRT (técnico)",
            "NF de materiais e NF de serviços com lista discriminatória e valorada dos materiais utilizados na obra, objeto da incorporação",
            "Dados bancários do cliente — comprovante bancário do fornecedor (cheque, cabeçalho de extrato, declaração do banco, "
            "imagem do app do banco ou internet banking, comprovante de transação bancária etc., desde que seja do banco)",
            "Planilha de Custos do Anexo VII da NT.017.EQTL com quantidades, custo unitário e número de NF por material",
        ],
        "condicionais": [
            "Laudo(s) de Ensaio(s) do(s) Equipamento(s) — se houver",
            "Cópia da Procuração Reconhecida em Cartório — quando necessário",
            "Documento de Propriedade do Terreno ou Autorização para Execução de Obras no mesmo",
            "Contrato com a CEF — somente para projetos MCMV (Minha Casa Minha Vida)",
            "Ficha cadastral de fornecedor preenchida e assinada (Anexo NT.017.EQTL), em PDF — obrigatória SOMENTE em caso de restituição de valores",
        ],
        "alertas": [
            "⚠️ Projeto elétrico aprovado há mais de 3 anos = VENCIDO. Reprovação automática.",
            "⚠️ Orçamento de conexão aprovado há mais de 1 ano = VENCIDO. Reprovação automática.",
            "⚠️ Este processo envolve ressarcimento de dinheiro ao cliente — confirme dados bancários do fornecedor.",
            "⚠️ Se o processo estiver em andamento, confirmar com o Relacionamento ao Cliente se já foi aprovado.",
        ]
    },

    # §6.5 — Doação de rede ou transformador NOVO (SEM ressarcimento, SEM orçamento de conexão)
    "doacao_rede_nova": {
        "titulo": "Doação de Rede ou Transformador NOVO (§6.5 NT.017.EQTL Rev.04)",
        "descricao": (
            "Incorporação de Ativos de Obras de Responsabilidade do Cliente "
            "para Atendimento de Empreendimento de Múltiplas Unidades Consumidoras. "
            "Não há ressarcimento de dinheiro e não se exige orçamento de conexão."
        ),
        "exige_orcamento_conexao": False,
        "ressarcimento": False,
        "obrigatorios": [
            "CPF e RG do responsável",
            "CPF e RG de uma testemunha",
            "Projeto Aprovado e vigente (prazo de 3 anos)",
            "ART (engenheiro) ou TRT (técnico)",
            "NF de materiais e NF de serviços com lista discriminatória e valorada dos materiais utilizados na obra, objeto da incorporação",
            "Laudo(s) de Ensaio(s) do(s) Equipamento(s)",
            "Planilha de Custos do Anexo VII da NT.017.EQTL (com quantidades, custo unitário e NF por material)",
        ],
        "condicionais": [
            "Cópia da Procuração Reconhecida em Cartório — quando necessário",
            "Documento de Propriedade do Terreno ou Autorização para Execução de Obras no mesmo",
            "Contrato com a CEF — somente para projetos MCMV (Minha Casa Minha Vida)",
        ],
        "alertas": [
            "⚠️ Projeto elétrico aprovado há mais de 3 anos = VENCIDO. Reprovação automática.",
            "ℹ️ Não se exige orçamento de conexão neste tipo de processo.",
        ]
    },

    # §6.5 — Doação de rede ou transformador EXISTENTE (SEM ressarcimento, usa As Built em vez de Projeto)
    "doacao_rede_existente": {
        "titulo": "Doação de Rede ou Transformador EXISTENTE (§6.5 NT.017.EQTL Rev.04)",
        "descricao": (
            "Incorporação de Ativos de Obras já existentes de Responsabilidade do Cliente "
            "para Atendimento de Empreendimento de Múltiplas Unidades Consumidoras. "
            "Não há ressarcimento de dinheiro. Usa As Built em vez de Projeto Aprovado — "
            "portanto a regra de vigência de 3 anos do projeto NÃO se aplica aqui."
        ),
        "exige_orcamento_conexao": False,
        "ressarcimento": False,
        "obrigatorios": [
            "CPF e RG do responsável",
            "CPF e RG de uma testemunha",
            "As Built (projeto conforme construído)",
            "ART (engenheiro) ou TRT (técnico)",
            "NF de materiais e NF de serviços com lista discriminatória e valorada dos materiais utilizados na obra, objeto da doação "
            "(se não houver notas fiscais, documento de posse especificando características dos equipamentos e números de série, "
            "registrado em cartório)",
            "Planilha de Custos do Anexo VII da NT.017.EQTL",
        ],
        "condicionais": [
            "Laudo(s) de Ensaio(s) do(s) Equipamento(s) — se houver",
            "Cópia da Procuração Reconhecida em Cartório — quando necessário",
        ],
        "alertas": [
            "⚠️ Se não houver notas fiscais, exigir documento de posse com características e números de série, registrado em cartório.",
            "ℹ️ Não se exige orçamento de conexão neste tipo de processo, nem prazo de vigência de Projeto (usa-se As Built).",
        ]
    },
}


# ============================================================
# ESTRUTURAS DE DADOS
# ============================================================

@dataclass
class SolicitacaoIncorporacao:
    numero_protocolo: str
    tipo_solicitacao: str
    nome_responsavel: str
    cpf_cnpj: str
    endereco_obra: str
    descricao_obra: str
    documentos_apresentados: list

    # Datas de aprovação — usadas para o cálculo automático de vigência.
    # Formato esperado: "DD/MM/AAAA". Deixe None se o documento não existir/aplicar.
    data_aprovacao_projeto: Optional[str] = None
    data_aprovacao_orcamento: Optional[str] = None   # só relevante para incorporacao_rede

    # Indica explicitamente se há restituição de valores ao cliente
    # (define se a Ficha Cadastral de Fornecedor é obrigatória na incorporação)
    possui_restituicao_valores: bool = False

    # Potência do(s) transformador(es) da subestação, em kVA (some vários equipamentos se houver
    # mais de um). Usado para aplicar a isenção de Projeto Elétrico em subestações <= 300 kVA
    # (ver POTENCIA_MAXIMA_ISENCAO_PROJETO_KVA). Deixe None se não identificado nos documentos —
    # nesse caso o sistema NÃO aplica a isenção automaticamente (comportamento conservador).
    potencia_transformador_kva: Optional[float] = None

    # Confirmação humana explícita de que o pedido NÃO é geração distribuída, NÃO é migração
    # para o mercado livre e NÃO tem situação omissa à Norma Técnica — as 3 condições que a
    # CEEE exige para valer a isenção de Projeto Elétrico em subestações <=300kVA. Enquanto for
    # None, a isenção é aplicada de forma PROVISÓRIA (aprovação condicionada a essa confirmação).
    condicoes_isencao_confirmadas: Optional[bool] = None

    observacoes_adicionais: str = ""
    data_envio: str = field(default_factory=lambda: datetime.now().strftime("%d/%m/%Y %H:%M"))


@dataclass
class ResultadoValidacao:
    numero_protocolo: str
    tipo_solicitacao: str
    status: str
    score_conformidade: int
    documentos_reconhecidos: list
    documentos_faltantes: list
    pendencias_condicionais: list
    alertas_ativos: list
    recomendacoes: list
    proximo_passo: str
    justificativa_tecnica: str
    reprovacao_automatica: bool = False   # True se reprovado por regra de vigência, sem precisar da IA
    pendencia_vigencia: bool = False      # True se há documento presente mas sem data confirmada (impede APROVADO puro)
    rebaixado_por_documentos_essenciais: bool = False  # True se REPROVADO da IA foi trocado por RESSALVAS
    isento_projeto_por_potencia: bool = False  # True se a exigência de Projeto Elétrico foi dispensada por potência <=300kVA
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def para_dict(self):
        return self.__dict__


# ============================================================
# REGRAS DE VIGÊNCIA (validação determinística em Python)
# ============================================================

def _parse_data(data_str):
    """Converte 'DD/MM/AAAA' em objeto datetime. Retorna None se vazio/inválido."""
    if not data_str:
        return None
    try:
        return datetime.strptime(data_str.strip(), "%d/%m/%Y")
    except ValueError:
        raise ValueError(
            f"Data inválida: '{data_str}'. Use o formato DD/MM/AAAA, ex: '15/02/2025'."
        )


def _possui_documentos_essenciais_minimos(solicitacao):
    """
    Verifica se o CONJUNTO MÍNIMO de documentos essenciais foi apresentado, por tipo de obra:
      - incorporacao_rede    : Projeto Aprovado + Orçamento de Conexão + Notas Fiscais
      - doacao_rede_nova     : Projeto Aprovado + Notas Fiscais (materiais e/ou serviços)
      - doacao_rede_existente: As Built + Notas Fiscais (materiais e/ou serviços)

    (A vigência de Projeto/Orçamento já foi validada em validar_vigencia antes deste ponto —
    se estivesse vencida ou totalmente ausente, o processo já teria sido barrado por
    reprovação automática. Aqui só confirmamos a PRESENÇA física do documento.)

    Se esse conjunto mínimo estiver presente, o processo NÃO pode ser REPROVADO só por
    faltar outros documentos obrigatórios secundários (CPF/RG, testemunha, ART/TRT, Laudo de
    Ensaio, Planilha de Custos etc.) — nesse caso o máximo é APROVADO_COM_RESSALVAS.
    """
    docs = solicitacao.documentos_apresentados
    tem_nf = (
        _documento_presente(docs, "NF de materiais")
        or _documento_presente(docs, "NF de serviços")
        or _documento_presente(docs, "Nota Fiscal")
        or _documento_presente(docs, " NF ")
        or _documento_presente(docs, "NF de")
    )

    if solicitacao.tipo_solicitacao == "incorporacao_rede":
        return (
            _documento_presente(docs, "Projeto Aprovado")
            and _documento_presente(docs, "Orçamento de Conexão")
            and tem_nf
        )
    if solicitacao.tipo_solicitacao == "doacao_rede_nova":
        return _documento_presente(docs, "Projeto Aprovado") and tem_nf
    if solicitacao.tipo_solicitacao == "doacao_rede_existente":
        return _documento_presente(docs, "As Built") and tem_nf
    return False


def _documento_presente(documentos_apresentados, palavra_chave):
    """Verifica se algum documento apresentado corresponde à palavra-chave do checklist
    (ex.: 'Projeto Aprovado', 'Orçamento de Conexão'). Usado para diferenciar
    'documento ausente' (reprovação automática) de 'documento presente, mas sem
    data de aprovação confirmada' (vira pendência, não reprovação automática)."""
    if not documentos_apresentados:
        return False
    chave = palavra_chave.lower()
    return any(chave in str(d).lower() for d in documentos_apresentados)


def _isento_projeto_eletrico_por_potencia(solicitacao):
    """
    Retorna True se a subestação do pedido é elegível à isenção de Projeto Elétrico por ser
    de pequeno porte (<= POTENCIA_MAXIMA_ISENCAO_PROJETO_KVA), conforme normativa da própria
    distribuidora. A isenção só se aplica a incorporação/doação de rede NOVA (que exigem
    Projeto Aprovado) — não faz sentido para doação de rede EXISTENTE, que já usa As Built.

    IMPORTANTE: a norma condiciona a isenção a três outros fatores que este sistema NÃO
    consegue confirmar sozinho a partir dos documentos (pedido não pode ser de geração
    distribuída, nem migração para mercado livre, nem ter situação omissa à Norma Técnica).
    Por isso a isenção é sempre sinalizada como "provisória" até confirmação humana explícita
    (ver campo condicoes_isencao_confirmadas) — ela dispensa a REPROVAÇÃO AUTOMÁTICA, mas nunca
    permite fechar como "APROVADO" puro sem essa confirmação.
    """
    if solicitacao.tipo_solicitacao not in ("incorporacao_rede", "doacao_rede_nova"):
        return False
    potencia = solicitacao.potencia_transformador_kva
    if potencia is None:
        return False
    return potencia <= POTENCIA_MAXIMA_ISENCAO_PROJETO_KVA


def _checar_vigencia(data_aprovacao, prazo_dias, agora=None):
    """
    Retorna (vigente: bool, dias_restantes_ou_vencidos: int, data_limite: datetime)
    Se data_aprovacao for None, considera como "não informado" (vigente=None).
    """
    agora = agora or datetime.now()
    data = _parse_data(data_aprovacao)
    if data is None:
        return None, None, None
    data_limite = data.replace(year=data.year + (prazo_dias // 365))
    dias_restantes = (data_limite - agora).days
    vigente = dias_restantes >= 0
    return vigente, dias_restantes, data_limite


def validar_vigencia(solicitacao):
    """
    Aplica a regra dura de vigência ANTES de chamar a IA.

    Três desfechos possíveis por documento (Projeto Elétrico / Orçamento de Conexão):
      1. Documento AUSENTE e sem data informada        -> motivo de REPROVAÇÃO AUTOMÁTICA (hard fail).
      2. Documento VENCIDO (data informada e expirada)  -> motivo de REPROVAÇÃO AUTOMÁTICA (hard fail).
      3. Documento PRESENTE mas sem data confirmada      -> PENDÊNCIA (soft fail): não reprova automático,
         segue para a análise da IA, mas o resultado final nunca pode fechar como "APROVADO" puro
         (no máximo "APROVADO_COM_RESSALVAS"), até a data ser confirmada manualmente.

    Retorna um dict:
      {
        "aprovado_na_vigencia": bool,       # False só quando há motivo de reprovação automática (1 ou 2)
        "possui_pendencia_vigencia": bool,  # True quando há pendência do tipo 3
        "motivos": [...],                   # reprovação automática (hard fail)
        "pendencias": [...],                # pendência a ser resolvida antes de aprovar (soft fail)
        "avisos": [...],                    # avisos de vencimento próximo (documento ok, mas perto do prazo)
      }
    """
    checklist = CHECKLIST_NT017[solicitacao.tipo_solicitacao]
    motivos_reprovacao = []
    pendencias_vigencia = []
    avisos = []

    # --- Projeto Elétrico: 3 anos --- (não se aplica a doacao_rede_existente, que usa As Built)
    if solicitacao.tipo_solicitacao != "doacao_rede_existente":
        isento_por_potencia = _isento_projeto_eletrico_por_potencia(solicitacao)

        if isento_por_potencia:
            # Subestação <= 300kVA: a distribuidora NÃO PODE cobrar Projeto Elétrico. Não gera
            # motivo de reprovação nem pendência de vigência — mas como a isenção normativa
            # depende de 3 condições que não dá para confirmar só pelos documentos (não ser GD,
            # não ser migração para mercado livre, não ter situação omissa), a aprovação plena
            # fica condicionada à confirmação humana disso (ver condicoes_isencao_confirmadas).
            if solicitacao.condicoes_isencao_confirmadas is True:
                avisos.append(
                    f"✅ ISENTO de Projeto Elétrico: subestação de {solicitacao.potencia_transformador_kva:g} kVA "
                    f"(≤ {POTENCIA_MAXIMA_ISENCAO_PROJETO_KVA} kVA) — dispensa prevista na normativa da "
                    f"distribuidora. Condições de isenção (não é GD, não é migração para mercado livre, sem "
                    f"situação omissa à Norma Técnica) já confirmadas."
                )
            else:
                pendencias_vigencia.append(
                    f"ISENÇÃO PROVISÓRIA de Projeto Elétrico: subestação de "
                    f"{solicitacao.potencia_transformador_kva:g} kVA (≤ {POTENCIA_MAXIMA_ISENCAO_PROJETO_KVA} kVA) "
                    f"está dispensada de apresentar Projeto Elétrico pela normativa da distribuidora — NÃO pode "
                    f"ser exigido nem motivo de reprovação. Porém, confirmar manualmente que o pedido NÃO é de "
                    f"acesso a geração distribuída, NÃO é migração para o mercado livre e NÃO possui situação "
                    f"omissa à Norma Técnica, antes de aprovar em definitivo (essas 3 condições não podem ser "
                    f"verificadas automaticamente a partir dos documentos)."
                )
        else:
            projeto_apresentado = _documento_presente(solicitacao.documentos_apresentados, "Projeto Aprovado")
            if not solicitacao.data_aprovacao_projeto:
                if projeto_apresentado:
                    pendencias_vigencia.append(
                        "Projeto Elétrico Aprovado foi identificado entre os documentos apresentados, mas a data de "
                        "aprovação não pôde ser confirmada automaticamente — verificar manualmente se está dentro do "
                        "prazo de 3 anos antes de aprovar definitivamente."
                    )
                else:
                    motivos_reprovacao.append(
                        "Projeto Elétrico Aprovado não foi localizado entre os documentos apresentados e a data de "
                        "aprovação não foi informada — não é possível confirmar vigência (prazo de 3 anos). Se a "
                        "subestação for de pequeno porte (≤300kVA), informe potencia_transformador_kva na "
                        "solicitação: pode ser caso de isenção, e não de reprovação."
                    )
            else:
                vigente, dias, data_limite = _checar_vigencia(
                    solicitacao.data_aprovacao_projeto, PRAZO_VIGENCIA_PROJETO_DIAS
                )
                if vigente is False:
                    motivos_reprovacao.append(
                        f"Projeto Elétrico VENCIDO: aprovado em {solicitacao.data_aprovacao_projeto}, "
                        f"prazo de 3 anos expirou em {data_limite.strftime('%d/%m/%Y')} "
                        f"({abs(dias)} dias atrás)."
                    )
                elif vigente is True and dias <= 90:
                    avisos.append(
                        f"⏰ Projeto Elétrico vence em {dias} dias ({data_limite.strftime('%d/%m/%Y')}) — atenção ao prazo."
                    )

    # --- Orçamento de Conexão: 1 ano --- (só se aplica quando exige_orcamento_conexao = True)
    if checklist.get("exige_orcamento_conexao"):
        orcamento_apresentado = _documento_presente(solicitacao.documentos_apresentados, "Orçamento de Conexão")
        if not solicitacao.data_aprovacao_orcamento:
            if orcamento_apresentado:
                pendencias_vigencia.append(
                    "Orçamento de Conexão Aprovado foi identificado entre os documentos apresentados, mas a data de "
                    "aprovação não pôde ser confirmada automaticamente — verificar manualmente se está dentro do "
                    "prazo de 1 ano antes de aprovar definitivamente."
                )
            else:
                motivos_reprovacao.append(
                    "Orçamento de Conexão Aprovado não foi localizado entre os documentos apresentados e a data de "
                    "aprovação não foi informada — não é possível confirmar vigência (prazo de 1 ano)."
                )
        else:
            vigente, dias, data_limite = _checar_vigencia(
                solicitacao.data_aprovacao_orcamento, PRAZO_VIGENCIA_ORCAMENTO_DIAS
            )
            if vigente is False:
                motivos_reprovacao.append(
                    f"Orçamento de Conexão VENCIDO: aprovado em {solicitacao.data_aprovacao_orcamento}, "
                    f"prazo de 1 ano expirou em {data_limite.strftime('%d/%m/%Y')} "
                    f"({abs(dias)} dias atrás)."
                )
            elif vigente is True and dias <= 30:
                avisos.append(
                    f"⏰ Orçamento de Conexão vence em {dias} dias ({data_limite.strftime('%d/%m/%Y')}) — atenção ao prazo."
                )

    return {
        "aprovado_na_vigencia": len(motivos_reprovacao) == 0,
        "possui_pendencia_vigencia": len(pendencias_vigencia) > 0,
        "motivos": motivos_reprovacao,
        "pendencias": pendencias_vigencia,
        "avisos": avisos,
        "isento_projeto_por_potencia": _isento_projeto_eletrico_por_potencia(solicitacao),
    }


# ============================================================
# FUNÇÕES AUXILIARES — PROMPT
# ============================================================

def montar_system_prompt(tipo_solicitacao):
    checklist = CHECKLIST_NT017[tipo_solicitacao]
    docs_obrigatorios = "\n".join(f"  [{i+1:02d}] {d}" for i, d in enumerate(checklist["obrigatorios"]))
    docs_condicionais = "\n".join(f"  [C{i+1}] {d}" for i, d in enumerate(checklist["condicionais"]))
    alertas = "\n".join(f"  {a}" for a in checklist["alertas"])

    nota_ressarcimento = (
        "\nEste é um processo de INCORPORAÇÃO com RESSARCIMENTO DE DINHEIRO ao cliente. "
        "A Ficha Cadastral de Fornecedor (Anexo NT.017.EQTL, em PDF) é OBRIGATÓRIA quando houver "
        "restituição de valores — verifique se essa condição se aplica."
        if checklist.get("ressarcimento") else ""
    )

    return f"""
Você é um analista técnico especialista da CEEE-D (Equatorial Energia), responsável por validar
a documentação de entrada para processos de incorporação e doação de redes elétricas,
com base na NT.017.EQTL – Revisão 04.

=== TIPO DE SOLICITAÇÃO ANALISADA ===
{checklist["titulo"]}
{checklist["descricao"]}
{nota_ressarcimento}

=== DOCUMENTOS OBRIGATÓRIOS ===
{docs_obrigatorios}

=== DOCUMENTOS CONDICIONAIS ===
{docs_condicionais}

=== ALERTAS IMPORTANTES ===
{alertas}

=== ATENÇÃO: VIGÊNCIA JÁ FOI VERIFICADA SEPARADAMENTE ===
A verificação de vigência de Projeto Elétrico (3 anos) e Orçamento de Conexão (1 ano, quando aplicável)
já foi feita por um sistema determinístico ANTES desta análise. Você receberá essa informação no
prompt do usuário. NÃO recalcule prazos — apenas leve essa informação em conta na sua justificativa
e nas recomendações.

=== CRITÉRIOS DE DECISÃO ===
APROVADO: todos os obrigatórios presentes e válidos, condicionais atendidos quando aplicáveis,
e vigência de projeto/orçamento CONFIRMADA (data verificada e dentro do prazo — sem nenhuma pendência de vigência).
APROVADO_COM_RESSALVAS: essenciais presentes, itens menores incompletos sanáveis; OU há pendência de vigência
(documento identificado mas data de aprovação não confirmada — ver bloco de vigência no prompt do usuário).
REPROVADO: qualquer obrigatório ausente, documento inválido, projeto/orçamento comprovadamente vencido, ART/TRT ausente.
IMPORTANTE: se o bloco "VERIFICAÇÃO AUTOMÁTICA DE VIGÊNCIA" indicar PENDÊNCIA (documento presente, data não
confirmada), NUNCA use "APROVADO" — o máximo permitido nesse caso é "APROVADO_COM_RESSALVAS".

=== PISO MÍNIMO: CONJUNTO ESSENCIAL DE DOCUMENTOS ===
Se o conjunto essencial abaixo estiver presente E vigente para o tipo de solicitação em análise, o
processo NUNCA pode ser "REPROVADO" — o mínimo é "APROVADO_COM_RESSALVAS", mesmo faltando outros
itens obrigatórios secundários (CPF/RG, testemunha, ART/TRT, Laudo de Ensaio, Planilha de Custos etc.):
  - Incorporação de Redes: Projeto Aprovado e vigente + Orçamento de Conexão aprovado e vigente + Notas Fiscais.
  - Doação de Rede/Transformador NOVO: Projeto Aprovado e vigente + Notas Fiscais.
  - Doação de Rede/Transformador EXISTENTE: As Built + Notas Fiscais.
Nesses casos, liste os itens secundários faltantes em "documentos_faltantes" e "recomendacoes",
mas o status deve ser "APROVADO_COM_RESSALVAS", nunca "REPROVADO".

=== INSTRUÇÃO DE RESPOSTA ===
Responda SOMENTE em JSON válido, sem texto adicional, sem backticks, sem markdown.
Schema:
{{
  "status": "APROVADO" | "REPROVADO" | "APROVADO_COM_RESSALVAS",
  "score_conformidade": <inteiro 0-100>,
  "documentos_reconhecidos": [...],
  "documentos_faltantes": [...],
  "pendencias_condicionais": [...],
  "alertas_ativos": [...],
  "recomendacoes": [...],
  "proximo_passo": "<próxima ação>",
  "justificativa_tecnica": "<justificativa>"
}}
"""


def _extrair_json(texto):
    try:
        return json.loads(texto.strip())
    except json.JSONDecodeError:
        pass
    # Remove possíveis blocos de markdown (```json ... ```)
    texto_limpo = re.sub(r"^```(?:json)?|```$", "", texto.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(texto_limpo)
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{.*\}', texto, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"JSON inválido na resposta:\n{texto[:300]}")


def _formatar_prompt_usuario(sol, info_vigencia):
    docs = "\n".join(f"  - {d}" for d in sol.documentos_apresentados)
    tipo_label = CHECKLIST_NT017.get(sol.tipo_solicitacao, {}).get("titulo", sol.tipo_solicitacao)

    bloco_vigencia = "  ✅ Vigência de projeto/orçamento OK (verificado automaticamente).\n"
    if info_vigencia["motivos"]:
        bloco_vigencia = "  ❌ PROBLEMAS DE VIGÊNCIA DETECTADOS (verificação automática):\n"
        bloco_vigencia += "\n".join(f"    - {m}" for m in info_vigencia["motivos"])
    if info_vigencia.get("pendencias"):
        bloco_vigencia += "\n  🟡 PENDÊNCIA(S) DE VIGÊNCIA (documento identificado, mas data não confirmada):\n"
        bloco_vigencia += "\n".join(f"    - {p}" for p in info_vigencia["pendencias"])
        bloco_vigencia += (
            "\n    ⚠️ IMPORTANTE: por causa dessa(s) pendência(s), o status final NÃO pode ser \"APROVADO\" puro "
            "nesta análise. Use no máximo \"APROVADO_COM_RESSALVAS\", citando explicitamente nas recomendações e na "
            "justificativa técnica que falta confirmar a data de aprovação/vigência do(s) documento(s) apontado(s)."
        )
    if info_vigencia["avisos"]:
        bloco_vigencia += "\n  ⏰ Avisos:\n" + "\n".join(f"    - {a}" for a in info_vigencia["avisos"])

    restituicao = "SIM — houve indicação de restituição de valores ao cliente." if sol.possui_restituicao_valores else "NÃO informado / não se aplica."

    return f"""
=== SOLICITAÇÃO RECEBIDA PARA ANÁLISE ===
Protocolo          : {sol.numero_protocolo}
Tipo               : {tipo_label}
Data de Envio      : {sol.data_envio}
Nome               : {sol.nome_responsavel}
CPF/CNPJ           : {sol.cpf_cnpj}
Endereço da Obra   : {sol.endereco_obra}

--- DESCRIÇÃO DA OBRA ---
{sol.descricao_obra}

--- DATAS-CHAVE ---
Data aprovação do Projeto Elétrico   : {sol.data_aprovacao_projeto or "(não informado)"}
Data aprovação do Orçamento Conexão  : {sol.data_aprovacao_orcamento or "(não informado / não aplicável a este tipo)"}
Há restituição de valores ao cliente?: {restituicao}

--- VERIFICAÇÃO AUTOMÁTICA DE VIGÊNCIA (já calculada, não recalcule) ---
{bloco_vigencia}

--- DOCUMENTOS APRESENTADOS ---
{docs}

--- OBSERVAÇÕES ---
{sol.observacoes_adicionais or "(nenhuma)"}

Analise conforme NT.017.EQTL Rev.04 e retorne o JSON.
"""


def _imprimir_resultado(r):
    icones = {"APROVADO": "✅", "REPROVADO": "❌", "APROVADO_COM_RESSALVAS": "⚠️"}
    icone = icones.get(r.status, "❓")
    titulo = CHECKLIST_NT017.get(r.tipo_solicitacao, {}).get("titulo", r.tipo_solicitacao)

    print(f"\n{'═'*65}")
    print(f"  RESULTADO — NT.017.EQTL Rev.04")
    print(f"  Protocolo : {r.numero_protocolo}")
    print(f"  Tipo      : {titulo}")
    print(f"{'═'*65}")

    if r.reprovacao_automatica:
        print(f"\n  🚫  REPROVAÇÃO AUTOMÁTICA POR VIGÊNCIA (a IA não precisou ser consultada)\n")
    elif r.pendencia_vigencia:
        print(f"\n  🟡  PENDÊNCIA DE VIGÊNCIA: documento identificado, mas data de aprovação não confirmada — "
              f"status travado em no máximo APROVADO_COM_RESSALVAS até regularização.\n")
    if r.rebaixado_por_documentos_essenciais:
        print(f"\n  🟢  PISO MÍNIMO ATENDIDO: documentos essenciais presentes e vigentes — reprovação da IA foi "
              f"trocada por APROVADO_COM_RESSALVAS (faltam apenas itens secundários).\n")
    if r.isento_projeto_por_potencia:
        print(f"\n  🔋  ISENÇÃO DE PROJETO ELÉTRICO POR POTÊNCIA: subestação ≤ {POTENCIA_MAXIMA_ISENCAO_PROJETO_KVA}kVA — "
              f"não pode ser exigido Projeto Elétrico. Confirme manualmente que não é GD, migração para mercado "
              f"livre, nem situação omissa à Norma Técnica.\n")
    print(f"\n  {icone}  STATUS: {r.status}   |   Conformidade: {r.score_conformidade}/100\n")

    if r.documentos_reconhecidos:
        print("📄 Documentos Reconhecidos:")
        for d in r.documentos_reconhecidos:
            print(f"   ✓  {d}")

    if r.documentos_faltantes:
        print("\n❌ Documentos Obrigatórios FALTANTES:")
        for d in r.documentos_faltantes:
            print(f"   ✗  {d}")

    if r.pendencias_condicionais:
        print("\n⚠️  Pendências Condicionais:")
        for p in r.pendencias_condicionais:
            print(f"   ?  {p}")

    if r.alertas_ativos:
        print("\n🔔 Alertas:")
        for a in r.alertas_ativos:
            print(f"   {a}")

    if r.recomendacoes:
        print("\n💡 Recomendações ao Cliente:")
        for rec in r.recomendacoes:
            print(f"   →  {rec}")

    print(f"\n➡️  Próximo Passo:")
    print(f"   {r.proximo_passo}")
    print(f"\n📋 Justificativa Técnica:")
    print(f"   {r.justificativa_tecnica}")
    print(f"{'═'*65}\n")


# ============================================================
# CHAMADA AO GEMINI (com retentativa automática em caso de limite de uso)
# ============================================================

def _chamar_gemini(cliente, modelo, system_prompt, prompt_usuario, max_tentativas=5):
    """
    Chama a API do Gemini com retentativa automática (backoff exponencial)
    caso a cota gratuita momentânea seja excedida (erro 429).
    """
    for tentativa in range(max_tentativas):
        try:
            resposta = cliente.models.generate_content(
                model=modelo,
                contents=prompt_usuario,
                config={
                    "system_instruction": system_prompt,
                    "temperature": 0.2,
                    "response_mime_type": "application/json",
                },
            )
            return resposta.text
        except genai_errors.ClientError as e:
            # Erro 429 = cota excedida no momento; espera e tenta novamente
            if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                espera = (2 ** tentativa) + 1
                print(f"⏳ Cota momentânea excedida. Aguardando {espera}s para tentar novamente "
                      f"({tentativa + 1}/{max_tentativas})...")
                time.sleep(espera)
                continue
            raise
    raise RuntimeError(
        "Não foi possível obter resposta do Gemini após várias tentativas. "
        "Você pode ter atingido a cota diária gratuita — tente novamente mais tarde "
        "ou use o modelo 'gemini-2.5-flash-lite' (cota diária maior)."
    )


# ============================================================
# PIPELINE PRINCIPAL
# ============================================================

def validar_documentacao(solicitacao, modelo=MODELO_DECISAO):
    """
    Valida documentação conforme NT.017.EQTL Rev.04 usando o Gemini (gratuito).

    Antes de consultar a IA, aplica a regra DURA de vigência:
      - Projeto Elétrico aprovado há mais de 3 anos -> REPROVADO automático
      - Orçamento de Conexão aprovado há mais de 1 ano (apenas incorporação) -> REPROVADO automático

    Retorna um objeto ResultadoValidacao.
    """
    print(f"\n🔄 Validando protocolo {solicitacao.numero_protocolo}...")

    info_vigencia = validar_vigencia(solicitacao)

    # --- Regra dura: se vigência falhou, reprova sem gastar chamada de API ---
    if not info_vigencia["aprovado_na_vigencia"]:
        resultado = ResultadoValidacao(
            numero_protocolo=solicitacao.numero_protocolo,
            tipo_solicitacao=solicitacao.tipo_solicitacao,
            status="REPROVADO",
            score_conformidade=0,
            documentos_reconhecidos=[],
            documentos_faltantes=[],
            pendencias_condicionais=[],
            alertas_ativos=info_vigencia["motivos"],
            recomendacoes=[
                "Regularizar a vigência do(s) documento(s) apontado(s) e reabrir o protocolo "
                "com Projeto Elétrico e/ou Orçamento de Conexão dentro do prazo."
            ],
            proximo_passo="Devolver ao cliente para regularização de vigência antes de qualquer outra análise.",
            justificativa_tecnica=(
                "Reprovação automática por regra de vigência (NT.017.EQTL Rev.04): "
                + " | ".join(info_vigencia["motivos"])
            ),
            reprovacao_automatica=True,
        )
        _imprimir_resultado(resultado)
        return resultado

    # --- Vigência OK: segue para análise documental via IA ---
    chave = os.environ.get("GEMINI_API_KEY")
    if not chave:
        raise EnvironmentError("API Key não encontrada. Configure nos Secrets do Colab (Célula 2).")

    cliente = genai.Client(api_key=chave)

    texto_resposta = _chamar_gemini(
        cliente=cliente,
        modelo=modelo,
        system_prompt=montar_system_prompt(solicitacao.tipo_solicitacao),
        prompt_usuario=_formatar_prompt_usuario(solicitacao, info_vigencia),
    )

    dados = _extrair_json(texto_resposta)

    alertas_finais = list(dados.get("alertas_ativos", []))
    if info_vigencia.get("pendencias"):
        alertas_finais = info_vigencia["pendencias"] + alertas_finais
    if info_vigencia["avisos"]:
        alertas_finais = info_vigencia["avisos"] + alertas_finais

    status_final = dados.get("status", "REPROVADO")
    recomendacoes_finais = list(dados.get("recomendacoes", []))
    justificativa_final = dados.get("justificativa_tecnica", "")

    # --- Trava de segurança: pendência de vigência nunca permite fechar como "APROVADO" puro ---
    # (mesmo que a IA erre e devolva APROVADO, o Python rebaixa automaticamente)
    possui_pendencia = info_vigencia.get("possui_pendencia_vigencia", False)
    if possui_pendencia and status_final == "APROVADO":
        status_final = "APROVADO_COM_RESSALVAS"
        recomendacoes_finais = [
            "Confirmar manualmente a data de aprovação/vigência do(s) documento(s) apontado(s) em Pendências antes "
            "de considerar o processo definitivamente aprovado."
        ] + recomendacoes_finais
        justificativa_final = (
            "Status rebaixado automaticamente de APROVADO para APROVADO_COM_RESSALVAS: há pendência de confirmação "
            "de vigência de documento(s) (ver alertas). " + justificativa_final
        )

    # --- Trava de segurança: piso mínimo de documentos essenciais impede REPROVADO ---
    # (mesmo que a IA erre e devolva REPROVADO, o Python rebaixa para RESSALVAS automaticamente,
    # desde que o conjunto essencial do tipo de solicitação esteja presente e vigente)
    rebaixado_essenciais = False
    if status_final == "REPROVADO" and _possui_documentos_essenciais_minimos(solicitacao):
        status_final = "APROVADO_COM_RESSALVAS"
        rebaixado_essenciais = True
        recomendacoes_finais = [
            "Documentos essenciais (Projeto/Orçamento/As Built + Notas Fiscais, conforme o tipo) foram "
            "apresentados e estão vigentes — completar os demais itens obrigatórios secundários listados em "
            "'Documentos Faltantes' para regularizar o processo."
        ] + recomendacoes_finais
        justificativa_final = (
            "Status rebaixado automaticamente de REPROVADO para APROVADO_COM_RESSALVAS: o conjunto essencial de "
            "documentos (Projeto/Orçamento/As Built + Notas Fiscais, conforme o tipo de solicitação) foi "
            "apresentado e está vigente, mesmo faltando documentos obrigatórios secundários. " + justificativa_final
        )

    resultado = ResultadoValidacao(
        numero_protocolo=solicitacao.numero_protocolo,
        tipo_solicitacao=solicitacao.tipo_solicitacao,
        status=status_final,
        score_conformidade=dados.get("score_conformidade", 0),
        documentos_reconhecidos=dados.get("documentos_reconhecidos", []),
        documentos_faltantes=dados.get("documentos_faltantes", []),
        pendencias_condicionais=dados.get("pendencias_condicionais", []),
        alertas_ativos=alertas_finais,
        recomendacoes=recomendacoes_finais,
        proximo_passo=dados.get("proximo_passo", "Verificar manualmente"),
        justificativa_tecnica=justificativa_final,
        reprovacao_automatica=False,
        pendencia_vigencia=possui_pendencia,
        rebaixado_por_documentos_essenciais=rebaixado_essenciais,
        isento_projeto_por_potencia=info_vigencia.get("isento_projeto_por_potencia", False),
    )

    _imprimir_resultado(resultado)
    return resultado


print("✅ Pipeline carregado! Pode rodar as células de teste abaixo.")
print(f"ℹ️  Modelo em uso: {MODELO_PADRAO} (gratuito via Google AI Studio)")
print(f"ℹ️  Regra de vigência ativa: Projeto = 3 anos | Orçamento de Conexão (incorporação) = 1 ano")
print(f"ℹ️  Documento presente sem data confirmada = PENDÊNCIA (segue para IA, trava em APROVADO_COM_RESSALVAS no máximo)")
print(f"ℹ️  Piso mínimo (Projeto+Orçamento+NF na incorporação | Projeto/As Built+NF na doação) impede REPROVADO — mínimo é RESSALVAS")
print(f"ℹ️  Subestação ≤ {POTENCIA_MAXIMA_ISENCAO_PROJETO_KVA}kVA é ISENTA de Projeto Elétrico (normativa da distribuidora) — não pode ser motivo de reprovação")
import os
import json
from datetime import datetime, timedelta, timezone

# ============================================================
# CONTADOR LOCAL DE RPD (Requisições por Dia)
# ============================================================
# A cota diária (RPD) do Gemini reseta à meia-noite no horário do
# Pacífico (EUA). Por isso usamos esse fuso para decidir quando o
# contador "vira o dia" — mesmo que você esteja no Brasil.

ARQUIVO_CONTADOR_RPD = "/tmp/contador_rpd.json"

# 🔧 AJUSTE AQUI sempre que conferir sua cota real em:
# https://aistudio.google.com/rate-limit
# (o valor abaixo é só um ponto de partida — o Gemini muda a cota
# gratuita com frequência, então não confie cegamente neste número)
LIMITE_RPD_MODELO = 1000  # cota diária estimada para o MODELO_PADRAO atual


def _data_hoje_pacifico():
    """Aproxima a data atual no horário do Pacífico (UTC-7 ou UTC-8)."""
    agora_utc = datetime.now(timezone.utc)
    offset_pacifico = timedelta(hours=-7)  # aproximação (horário de verão dos EUA)
    return (agora_utc + offset_pacifico).strftime("%Y-%m-%d")


def _carregar_contador():
    hoje = _data_hoje_pacifico()
    if os.path.exists(ARQUIVO_CONTADOR_RPD):
        try:
            with open(ARQUIVO_CONTADOR_RPD, "r") as f:
                dados = json.load(f)
            if dados.get("data") == hoje:
                return dados
        except (json.JSONDecodeError, OSError):
            pass
    # Ainda não existe contador de hoje (ou o dia virou) -> começa do zero
    return {"data": hoje, "requisicoes": 0}


def _salvar_contador(dados):
    with open(ARQUIVO_CONTADOR_RPD, "w") as f:
        json.dump(dados, f)


def registrar_requisicao_gemini():
    """Chame isso toda vez que uma chamada REAL ao Gemini for concluída
    (mesma função usada dentro de analisar_pdf_individual)."""
    dados = _carregar_contador()
    dados["requisicoes"] += 1
    _salvar_contador(dados)
    return dados["requisicoes"]


def mostrar_rpd_disponivel():
    """Mostra quantas requisições já foram usadas hoje (nesta conta/pasta)
    e uma estimativa de quantas ainda restam, com base em LIMITE_RPD_MODELO."""
    dados = _carregar_contador()
    usadas = dados["requisicoes"]
    restantes = max(LIMITE_RPD_MODELO - usadas, 0)
    print(f"📊 RPD hoje ({dados['data']}, horário do Pacífico): "
          f"{usadas} requisições usadas de {LIMITE_RPD_MODELO} configuradas "
          f"→ ≈ {restantes} disponíveis.")
    if usadas >= LIMITE_RPD_MODELO:
        print("🛑 Você já deve ter estourado a cota diária real — se a IA "
              "começar a dar erro 429, é por isso.")
    elif restantes <= LIMITE_RPD_MODELO * 0.1:
        print("⚠️ Perto do limite diário! Considere trocar de modelo "
              "(ex.: gemini-2.5-flash-lite tem cota maior) ou aguardar o "
              "reset (meia-noite no horário do Pacífico, EUA).")
    return restantes


# Mostra o status assim que a célula roda
mostrar_rpd_disponivel()
!pip install -q pypdf

from google.colab import files
import os, shutil
import pypdf

PASTA_DOCUMENTOS = "/content/documentos"
os.makedirs(PASTA_DOCUMENTOS, exist_ok=True)

print("📎 Selecione todos os PDFs da obra para enviar:")
enviados = files.upload()

print(f"\n✅ {len(enviados)} arquivo(s) recebido(s). Puxando dados de cada PDF...\n")

resumo_anexos = []
for nome_arquivo in enviados:
    destino = f"{PASTA_DOCUMENTOS}/{nome_arquivo}"
    shutil.move(nome_arquivo, destino)

    tamanho_kb = os.path.getsize(destino) / 1024
    try:
        leitor = pypdf.PdfReader(destino)
        num_paginas = len(leitor.pages)
        texto_bruto = leitor.pages[0].extract_text() or ""
        preview = texto_bruto.strip().replace("\n", " ")[:200]
        if not preview:
            preview = "(sem texto selecionável — provavelmente PDF escaneado/imagem; a IA ainda consegue ler como imagem na Célula 7D)"
    except Exception as e:
        num_paginas = "?"
        preview = f"(não foi possível ler o PDF localmente: {e})"

    resumo_anexos.append({
        "arquivo": nome_arquivo,
        "tamanho_kb": round(tamanho_kb, 1),
        "paginas": num_paginas,
        "preview": preview,
    })

print("═" * 65)
print("  PDFs ANEXADOS — prévia do conteúdo (antes de mandar para a IA)")
print("═" * 65)
for item in resumo_anexos:
    print(f"\n📄 {item['arquivo']}  ({item['tamanho_kb']} KB, {item['paginas']} página(s))")
    print(f"   Prévia: {item['preview']}")
print("\n" + "═" * 65)

print(f"\n✅ {len(enviados)} PDF(s) salvo(s) em {PASTA_DOCUMENTOS}/ — prontos para a Célula 7D.")
import os
import json
import re
import time
import pathlib
from google.genai import errors as genai_errors
from google import genai

# ============================================================
# CHUNKING PARA PDFs GRANDES
# ============================================================
# PDFs compilados de 60-120 páginas (comum nos casos reais) sofrem do efeito
# "lost in the middle": o modelo tende a prestar menos atenção ao conteúdo do
# meio do documento e pode simplesmente não notar que um item do checklist
# está lá (essa foi a causa mais provável dos casos de teste em que o Projeto
# Aprovado ou o Orçamento de Conexão "não foram localizados" mas na verdade
# estavam no PDF). Para reduzir isso, PDFs grandes são divididos em blocos
# menores de páginas antes de enviar ao Gemini.
PAGINAS_POR_BLOCO = 25

def _dividir_pdf_em_blocos(caminho_pdf, paginas_por_bloco=PAGINAS_POR_BLOCO):
    """Divide um PDF grande em blocos de N páginas. Retorna lista de tuplas
    (bytes_do_bloco, pagina_inicial_1_indexed). Se o PDF tiver menos páginas
    que o limite, retorna um único bloco (sem overhead extra)."""
    import pypdf
    leitor = pypdf.PdfReader(str(caminho_pdf))
    total_paginas = len(leitor.pages)

    if total_paginas <= paginas_por_bloco:
        return [(pathlib.Path(caminho_pdf).read_bytes(), 1)]

    blocos = []
    for inicio in range(0, total_paginas, paginas_por_bloco):
        fim = min(inicio + paginas_por_bloco, total_paginas)
        escritor = pypdf.PdfWriter()
        for p in range(inicio, fim):
            escritor.add_page(leitor.pages[p])
        buffer = io.BytesIO()
        escritor.write(buffer)
        blocos.append((buffer.getvalue(), inicio + 1))
    return blocos

import io  # usado por _dividir_pdf_em_blocos acima


def _montar_prompt_extracao(checklist_titulo, obrigatorios, condicionais, offset_pagina):
    """Monta o prompt de extração. `offset_pagina` é usado para os números de
    página reportados pelo modelo baterem com o PDF original quando o arquivo
    foi dividido em blocos."""
    return f"""Você é um analista da CEEE-D/Equatorial Energia. Este PDF (ou trecho de PDF)
faz parte de um processo de {checklist_titulo}.

⚠️ ATENÇÃO: este arquivo PDF pode ser um COMPILADO contendo VÁRIOS documentos diferentes
colados em sequência (por exemplo: ART, várias notas fiscais, carta de aprovação de
Projeto Elétrico, carta de Orçamento de Conexão, extrato bancário, CNH, procuração,
planilha de custos etc. — tudo dentro do mesmo arquivo). NÃO assuma que o arquivo inteiro
representa um único item do checklist.

Percorra TODAS as páginas e identifique TODOS os documentos distintos que aparecem nele.
Para CADA documento distinto que corresponder a um item do checklist abaixo, gere um
objeto separado na lista de resposta — mesmo que sejam vários objetos para o mesmo arquivo.

REGRA MAIS IMPORTANTE DE TODAS — NÃO ALUCINE:
Só reporte um item do checklist como encontrado se você REALMENTE viu o documento
correspondente no PDF (título, timbre, conteúdo característico). Não infira a existência de
um documento a partir do contexto (ex.: não diga que existe "Orçamento de Conexão" só
porque existe um "Projeto Aprovado" e faria sentido também existir). Na dúvida, marque
"confianca": "baixa" ou simplesmente não inclua o item. É preferível deixar de reportar um
documento existente do que inventar um que não existe — os dois erros custam caro, mas o
segundo é pior porque aprova processos incompletos.

=== ATENÇÃO: POTÊNCIA DO TRANSFORMADOR (campo "potencia_transformador_kva") ===
A potência do transformador da subestação é um dado importante: subestações de até 300 kVA são
ISENTAS de Projeto Elétrico pela normativa da distribuidora. Sempre que encontrar essa
informação — em notas fiscais de transformador, no Projeto elétrico, na ART, ou em cartas da
distribuidora de Orçamento de Conexão/Aprovação de Projeto (que costumam trazer "Potência Total
de Transformadores: XXX kVA") — extraia o valor numérico em kVA. Se houver mais de um
transformador, informe a soma. Se não encontrar, deixe null (não invente um valor).

=== ITENS DO CHECKLIST ===
Obrigatórios: {obrigatorios}
Condicionais: {condicionais}

=== COMO ENCONTRAR A DATA DE APROVAÇÃO (campo "data_aprovacao") ===
Para os itens "Projeto Aprovado e vigente (prazo de 3 anos)" e "Orçamento de Conexão
aprovado e vigente (prazo de 1 ano)", a data de aprovação NÃO costuma vir com um rótulo
explícito tipo "Data de Aprovação:". Em cartas/ofícios da distribuidora de energia
(CEEE/Equatorial), a data de aprovação costuma ser a DATA DO PRÓPRIO OFÍCIO/CARTA,
geralmente escrita por extenso no topo do documento, no formato "Cidade, DD de mês de
AAAA." — por exemplo: "Porto Alegre, 14 de janeiro de 2025." significa que o documento tem
data 14/01/2025.
Regras:
  1. Se o documento for claramente uma carta/ofício de "APROVAÇÃO" de projeto elétrico, ou
     um "Orçamento de Conexão com Obras", use a data do cabeçalho/topo da carta como
     "data_aprovacao".
  2. Converta SEMPRE datas por extenso para o formato numérico DD/MM/AAAA.
  3. NUNCA "chute" ou invente uma data. Se houver mais de uma data no documento e não estiver
     claro qual é a de aprovação, ou se você não tiver certeza de qual data usar, deixe
     "data_aprovacao" como null e explique o motivo em "observacoes" — isso é MELHOR do que
     reportar uma data errada, porque uma data errada pode reprovar automaticamente um
     processo que na verdade está em dia.
  4. Preencha "confianca_data" com "alta" apenas se a data estiver claramente associada ao
     documento certo (ex.: no cabeçalho de uma carta de aprovação). Use "media" ou "baixa"
     em qualquer outro caso — inclusive quando você teve que inferir a partir do contexto.
  5. Sempre que possível, preencha "pagina" com o número da página (dentro deste arquivo/
     trecho) onde encontrou o documento, somando {offset_pagina - 1} ao número da página que
     você está vendo (ou seja, se for a 1ª página deste arquivo, reporte {offset_pagina}).

Responda SOMENTE em JSON válido, sem markdown, sem texto adicional, neste formato exato:
{{
  "documentos_identificados": [
    {{
      "documento_identificado": "<item do checklist correspondente, ou 'NÃO IDENTIFICADO'>",
      "confianca_identificacao": "<alta|media|baixa>",
      "pagina": <numero_da_pagina_ou_null>,
      "nome_responsavel": "<nome ou null>",
      "cpf_cnpj": "<cpf/cnpj ou null>",
      "data_aprovacao": "<DD/MM/AAAA ou null>",
      "confianca_data": "<alta|media|baixa|null>",
      "endereco_obra": "<endereço ou null>",
      "descricao_obra": "<descrição ou null>",
      "notas_fiscais_encontradas": [{{"numero": "12345", "valor": "1500.50"}}],
      "potencia_transformador_kva": <numero_ou_null, ex: 225 — procure em notas fiscais, projetos, ART e cartas de Orçamento de Conexão/Aprovação de Projeto por termos como "225kVA", "TR-3Ø-225KVA", "Potência Total de Transformadores: 225 kVA">,
      "observacoes": "<observações ou null>"
    }}
  ]
}}

Se este trecho não tiver nenhum documento relevante do checklist, devolva a lista com um
único objeto "documento_identificado": "NÃO IDENTIFICADO"."""


def analisar_pdf_individual(cliente, modelo, arquivo_pdf_path, tipo_solicitacao, max_tentativas=5):
    """Envia o PDF para o Gemini com tratamento inteligente e diagnóstico de erros de barreira.

    IMPORTANTE: um único arquivo PDF pode ser um COMPILADO contendo vários documentos
    diferentes colados em sequência. Por isso a IA é instruída a devolver uma LISTA de
    documentos identificados dentro do arquivo, não apenas um.

    PDFs com muitas páginas são divididos em blocos menores (ver PAGINAS_POR_BLOCO) antes
    do envio, para reduzir o risco de o modelo "perder" documentos no meio de arquivos
    grandes — o efeito lost-in-the-middle é a causa mais provável de casos em que um
    documento presente no PDF foi reportado como ausente.
    """
    checklist = CHECKLIST_NT017[tipo_solicitacao]
    obrigatorios_str = ', '.join(checklist['obrigatorios'])
    condicionais_str = ', '.join(checklist['condicionais'])

    blocos = _dividir_pdf_em_blocos(arquivo_pdf_path)
    if len(blocos) > 1:
        print(f"     ✂️  Arquivo grande — dividido em {len(blocos)} blocos de até {PAGINAS_POR_BLOCO} páginas para leitura mais confiável.")

    todos_resultados = []

    for bloco_bytes, pagina_inicial in blocos:
        prompt = _montar_prompt_extracao(checklist['titulo'], obrigatorios_str, condicionais_str, pagina_inicial)

        for tentativa in range(max_tentativas):
            try:
                resposta = cliente.models.generate_content(
                    model=modelo,
                    contents=[
                        genai.types.Part.from_bytes(data=bloco_bytes, mime_type="application/pdf"),
                        prompt,
                    ],
                    config={
                        "temperature": 0.1,
                        "response_mime_type": "application/json",
                    },
                )
                registrar_requisicao_gemini()
                resultados_bloco = _normalizar_lista_documentos(_extrair_json(resposta.text))
                # Descarta os "NÃO IDENTIFICADO" de blocos vazios para não poluir a lista final
                resultados_bloco = [r for r in resultados_bloco if r.get("documento_identificado") != "NÃO IDENTIFICADO"]
                todos_resultados.extend(resultados_bloco)
                break

            except (genai_errors.ClientError, genai_errors.ServerError) as e:
                erro_texto = str(e)
                print("\n======================= ERRO REAL DA API GEMINI =======================")
                print(erro_texto)
                print("========================================================================\n")

                if "RESOURCE_EXHAUSTED" in erro_texto or "429" in erro_texto:
                    if "per day" in erro_texto.lower() or "daily" in erro_texto.lower() or "free_tier_requests" in erro_texto.lower():
                        print("🛑 [CRÍTICO] Cota diária global esgotada (RPD). Encerrando o processamento.")
                        raise RuntimeError("Cota Diária Excedida. Altere o modelo ou aguarde a renovação diária (04:00 AM).")
                    elif "per minute" in erro_texto.lower():
                        espera = 60
                        print(f"⏳ Limite por minuto atingido de fato. Pausa estratégica de {espera}s antes da tentativa {tentativa + 1}...")
                        time.sleep(espera)
                        continue
                    else:
                        print("⚠️ Limite crítico da API atingido. Interrompendo lote para evitar sobrecarga de requisições.")
                        raise

                if "UNAVAILABLE" in erro_texto or "503" in erro_texto:
                    espera = (2 ** tentativa) + 1
                    print(f"⏳ Servidor instável (503). Aguardando backoff de {espera}s...")
                    time.sleep(espera)
                    continue

                raise
        else:
            raise RuntimeError(f"Falha crítica ao processar bloco de {arquivo_pdf_path.name} após {max_tentativas} tentativas.")

    if not todos_resultados:
        return [{"documento_identificado": "NÃO IDENTIFICADO"}]
    return todos_resultados


def _normalizar_lista_documentos(dados_json):
    """Aceita tanto o formato novo ({'documentos_identificados': [...]}) quanto,
    por segurança, o formato antigo de objeto único (caso algum modelo/versão
    antiga ainda devolva assim), e sempre retorna uma LISTA de dicts."""
    if isinstance(dados_json, dict) and "documentos_identificados" in dados_json:
        lista = dados_json.get("documentos_identificados") or []
        if isinstance(lista, list) and lista:
            return lista
        return [{"documento_identificado": "NÃO IDENTIFICADO"}]
    if isinstance(dados_json, dict):
        return [dados_json]
    if isinstance(dados_json, list):
        return dados_json
    return [{"documento_identificado": "NÃO IDENTIFICADO"}]


def analisar_pasta_pdfs(caminho_pasta, tipo_solicitacao, modelo=None):
    """Varre a pasta de documentos, envia PDFs elegíveis ao Gemini e consolida os dados gerais obtidos.

    Cada PDF pode conter MAIS DE UM documento do checklist (arquivo compilado), então cada
    arquivo pode gerar VÁRIOS sub-resultados — todos são considerados na consolidação.

    IMPORTANTE (correção de alucinação): quando datas conflitantes são encontradas para o
    MESMO tipo de documento (ex.: duas cartas de "Projeto Aprovado" com datas diferentes em
    arquivos ou páginas diferentes), o código NÃO escolhe silenciosamente uma delas — ele
    marca a data como incerta e devolve todas as candidatas, para que a Célula 3 trate isso
    como pendência em vez de reprovar/aprovar automaticamente com base numa data que pode
    estar errada.
    """
    modelo = modelo or MODELO_EXTRACAO_PDF
    chave = os.environ.get("GEMINI_API_KEY")
    if not chave:
        raise EnvironmentError("API Key não encontrada. Configure nos Secrets do Colab.")

    cliente = genai.Client(api_key=chave)

    pasta = pathlib.Path(caminho_pasta)
    arquivos_pdf = sorted([p for p in pasta.glob("*.pdf")])

    print(f"📂 Iniciando análise de {len(arquivos_pdf)} arquivo(s) com o Gemini (modelo: {modelo})...\n")
    mostrar_rpd_disponivel()
    print()

    analises_detalhadas = []
    documentos_apresentados = []

    consolidador_info = {
        "nome_responsavel": None,
        "cpf_cnpj": None,
        "endereco_obra": None,
        "descricao_obra": None
    }

    # Agora coletamos TODAS as datas candidatas por tipo (em vez de sobrescrever
    # silenciosamente), separadas por nível de confiança.
    candidatos_data_projeto = []    # lista de (data, confianca, origem)
    candidatos_data_orcamento = []
    candidatos_potencia_kva = []    # lista de (potencia_kva, origem) — usada p/ isenção de Projeto Elétrico

    for idx, arq in enumerate(arquivos_pdf):
        print(f"  🔍 [{idx+1}/{len(arquivos_pdf)}] Analisando: {arq.name}...")

        try:
            lista_resultados = analisar_pdf_individual(cliente, modelo, arq, tipo_solicitacao)
            analises_detalhadas.append({"arquivo": arq.name, "resultado": lista_resultados})

            if len(lista_resultados) > 1:
                print(f"     → {len(lista_resultados)} documento(s) identificado(s) neste arquivo.")

            for resultado in lista_resultados:
                doc_id = resultado.get("documento_identificado")
                confianca_id = resultado.get("confianca_identificacao", "media")
                print(f"     → Identificado como: {doc_id} (confiança: {confianca_id})")

                if doc_id and doc_id != "NÃO IDENTIFICADO":
                    documentos_apresentados.append(f"{doc_id} (identificado no arquivo '{arq.name}')")

                    data_extraida = resultado.get("data_aprovacao")
                    confianca_data = resultado.get("confianca_data")
                    if "Projeto Aprovado" in doc_id and data_extraida:
                        candidatos_data_projeto.append((data_extraida, confianca_data, arq.name))
                    if "Orçamento de Conexão" in doc_id and data_extraida:
                        candidatos_data_orcamento.append((data_extraida, confianca_data, arq.name))

                potencia_extraida = resultado.get("potencia_transformador_kva")
                if potencia_extraida is not None:
                    try:
                        candidatos_potencia_kva.append((float(potencia_extraida), arq.name))
                    except (TypeError, ValueError):
                        pass

                for campo in consolidador_info.keys():
                    if not consolidador_info[campo] and resultado.get(campo):
                        consolidador_info[campo] = resultado.get(campo)

        except Exception as e:
            print(f"  ❌ Erro ao analisar o arquivo {arq.name}: {e}")
            analises_detalhadas.append({"arquivo": arq.name, "resultado": [{"documento_identificado": f"ERRO NA ANÁLISE: {e}"}]})
            if "Cota Diária Excedida" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                print("🛑 Interrompendo lote restante devido a restrições de cota.")
                break

    print()
    mostrar_rpd_disponivel()

    data_projeto, aviso_projeto = _resolver_data_candidata(candidatos_data_projeto, "Projeto Elétrico Aprovado")
    data_orcamento, aviso_orcamento = _resolver_data_candidata(candidatos_data_orcamento, "Orçamento de Conexão")
    potencia_kva, aviso_potencia = _resolver_potencia_candidata(candidatos_potencia_kva)

    avisos_datas = [a for a in (aviso_projeto, aviso_orcamento, aviso_potencia) if a]

    return {
        "analises_detalhadas": analises_detalhadas,
        "documentos_apresentados": documentos_apresentados,
        "data_aprovacao_projeto": data_projeto,
        "data_aprovacao_orcamento": data_orcamento,
        "potencia_transformador_kva": potencia_kva,
        "avisos_datas": avisos_datas,
        "info_geral": consolidador_info
    }


def _resolver_potencia_candidata(candidatos, tolerancia_pct=0.05):
    """Recebe a lista de (potencia_kva, arquivo_origem) encontrada em diferentes documentos e
    decide qual valor usar para checar a isenção de Projeto Elétrico (<=300kVA).

    Regras (mesmo espírito de _resolver_data_candidata: nunca decidir sozinho em caso de
    conflito real):
      - Sem candidatos: retorna (None, None) — sem informação, isenção não é aplicada
        automaticamente (comportamento conservador: exige Projeto Elétrico como antes).
      - Todos os valores dentro de ±5% um do outro (variação normal de arredondamento entre
        documentos, ex.: nota fiscal "225" vs projeto "225,0"): usa o maior valor encontrado
        (mais conservador — se o maior já for <=300kVA, todos são).
      - Valores claramente diferentes (ex.: 225kVA em um documento e 500kVA em outro — pode ser
        mais de um transformador, ou erro de leitura): NÃO decide sozinho. Gera aviso pedindo
        confirmação manual, e a isenção automática não é aplicada (potencia retorna None).
    """
    if not candidatos:
        return None, None

    valores = sorted(set(v for v, _ in candidatos))
    maior = max(valores)
    menor = min(valores)

    if menor == 0 or (maior - menor) / maior <= tolerancia_pct:
        return maior, None

    aviso = (
        f"⚠️ CONFLITO DE POTÊNCIA DE TRANSFORMADOR: foram encontrados valores diferentes "
        f"({', '.join(f'{v:g}kVA' for v in valores)}) em documentos distintos — pode ser mais de "
        f"um transformador (nesse caso, a potência total seria a soma) ou erro de leitura. Não foi "
        f"aplicada isenção automática de Projeto Elétrico — confirme manualmente a potência total "
        f"da subestação."
    )
    return None, aviso


def _resolver_data_candidata(candidatos, rotulo):
    """Recebe a lista de (data, confianca, arquivo_origem) para um mesmo tipo de documento
    e decide qual data usar, SEM sobrescrever silenciosamente em caso de conflito.

    Regras:
      - Sem candidatos: retorna (None, None) — segue fluxo normal de "não informado".
      - Um único valor distinto de data (mesmo vindo de vários sub-documentos): usa ele.
      - Múltiplos valores distintos de data: NÃO escolhe sozinho. Prioriza uma data de
        confiança "alta" se houver exatamente uma; caso contrário, retorna None (força
        pendência manual) e gera um aviso explicando o conflito encontrado, para o analista
        humano decidir — isso evita repetir o erro de reprovar/aprovar automaticamente com
        base numa data errada.
    """
    if not candidatos:
        return None, None

    datas_distintas = sorted(set(c[0] for c in candidatos))
    if len(datas_distintas) == 1:
        return datas_distintas[0], None

    alta_confianca = sorted(set(c[0] for c in candidatos if c[1] == "alta"))
    if len(alta_confianca) == 1:
        aviso = (
            f"⚠️ Foram encontradas datas conflitantes para '{rotulo}': {', '.join(datas_distintas)}. "
            f"Usada a única com confiança alta ({alta_confianca[0]}) — confirme manualmente."
        )
        return alta_confianca[0], aviso

    aviso = (
        f"⚠️ CONFLITO DE DATAS para '{rotulo}': foram encontradas {len(datas_distintas)} datas diferentes "
        f"({', '.join(datas_distintas)}) sem confiança alta suficiente para decidir sozinho. "
        f"Nenhuma data foi usada automaticamente — confirme manualmente qual é a correta antes de aprovar."
    )
    return None, aviso


def imprimir_auditoria_pdfs(analises_detalhadas):
    """Gera um relatório visual de auditoria de como o modelo interpretou cada arquivo do lote.

    Cada arquivo pode ter mais de um sub-documento identificado (PDF compilado), então
    imprime um bloco por sub-documento, incluindo página e nível de confiança para permitir
    conferência humana rápida.
    """
    print("\n" + "═"*75)
    print(" 📊 AUDITORIA DE MAPEAMENTO MULTIMODAL DOS PDFs")
    print("═"*75)
    for item in analises_detalhadas:
        arq = item["arquivo"]
        lista_res = item["resultado"]
        if not isinstance(lista_res, list):
            lista_res = [lista_res]

        print(f"\n📄 Arquivo: {arq}" + (f"  ({len(lista_res)} documento(s) dentro do arquivo)" if len(lista_res) > 1 else ""))
        for n, res in enumerate(lista_res, start=1):
            prefixo = f"   [{n}]" if len(lista_res) > 1 else "  "
            confianca = res.get("confianca_identificacao")
            sufixo_confianca = f" (confiança: {confianca})" if confianca else ""
            print(f"{prefixo} └─ Item NT.017 Mapeado: {res.get('documento_identificado')}{sufixo_confianca}")
            if res.get("pagina"): print(f"{prefixo}    └─ Página: {res.get('pagina')}")
            if res.get("nome_responsavel"): print(f"{prefixo}    └─ Responsável Detectado: {res.get('nome_responsavel')}")
            if res.get("cpf_cnpj"): print(f"{prefixo}    └─ CPF/CNPJ Detectado: {res.get('cpf_cnpj')}")
            if res.get("data_aprovacao"):
                conf_data = res.get("confianca_data")
                print(f"{prefixo}    └─ Data de Validade/Emissão: {res.get('data_aprovacao')}" + (f" (confiança: {conf_data})" if conf_data else ""))
            if res.get("endereco_obra"): print(f"{prefixo}    └─ Endereço Identificado: {res.get('endereco_obra')[:60]}...")
    print("\n" + "═"*75 + "\n")
