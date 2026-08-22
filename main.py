"""
Bot de WhatsApp da Cliniq — versão Evolution API.

Recebe mensagens via webhook da Evolution API, conversa usando a API do Claude, e
mantém o CRM (guardado no Supabase) atualizado a cada troca de mensagem: quem é o
lead, em que estágio ele está, e quando a equipe humana precisa assumir a conversa.

Diferente da primeira versão (pensada para Twilio), este arquivo fala com a
Evolution API — o serviço open-source que conecta ao WhatsApp de verdade via QR
code. Configuração só por variáveis de ambiente (veja .env.example); passo a passo
completo em README.md.

Decisão importante de design (ver skill "cliniq-lean-six-sigma" / análise 5 Porquês):
o lead é criado no CRM assim que a primeira mensagem chega — não esperamos os dados
"completos" pra registrar. `dados_completos` é só um status dentro do registro, nunca
uma condição pra ele existir. Isso evita perder rastro de conversas abandonadas no
meio, que era o problema original que esse projeto inteiro tenta resolver.

Segunda decisão de design (após revisar uma conversa real de teste): o atendimento
precisa ter início e fim. Sem isso, o bot emenda mensagens novas no mesmo histórico
pra sempre, mesmo depois que o assunto do lead já foi resolvido. Por isso a ferramenta
`responder_e_classificar` tem um campo `encerrar_atendimento` — quando o Claude marca
esse campo como true, a conversa atual é fechada (`conversations.encerrada_em`) e a
próxima mensagem do lead abre uma conversa nova, com contexto limpo.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request
from anthropic import Anthropic

# --- Configuração (lida das variáveis de ambiente / arquivo .env) ---
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

EVOLUTION_API_URL = os.environ["EVOLUTION_API_URL"].rstrip("/")
EVOLUTION_API_KEY = os.environ["EVOLUTION_API_KEY"]

# Segredo simples pra ninguém além da Evolution API conseguir chamar nosso webhook.
# Configure a URL do webhook na Evolution API como: .../webhook/whatsapp?token=ESTE_VALOR
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")

# Número da equipe (formato "5511999999999", sem espaços/símbolos) que recebe
# alertas de lead quente / bot travado. Deixe em branco pra desativar por enquanto.
EQUIPE_WHATSAPP = os.environ.get("EQUIPE_WHATSAPP", "")

HISTORICO_MAX_MENSAGENS = 20  # quantas mensagens recentes mandamos pro Claude como contexto

# Se o lead mandar mensagem dentro desta janela depois de um atendimento encerrado, reabrimos
# a MESMA conversa (com todo o histórico) em vez de começar uma em branco — evita que uma
# pergunta de acompanhamento logo após um agendamento derrube nome, procedimento e horário já
# combinados. Passado esse tempo, aí sim consideramos que é um assunto novo de verdade.
JANELA_REABERTURA_CONVERSA = timedelta(hours=2)

# Fuso horário usado pra calcular datas reais (ex.: "próxima segunda-feira") — sem
# isso o Claude não tem como saber que dia é hoje.
FUSO_HORARIO = ZoneInfo("America/Sao_Paulo")
DIAS_DA_SEMANA = {
    0: "segunda-feira",
    1: "terça-feira",
    2: "quarta-feira",
    3: "quinta-feira",
    4: "sexta-feira",
    5: "sábado",
    6: "domingo",
}

app = FastAPI()
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

supabase_headers = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}


# A "ferramenta" abaixo obriga o Claude a sempre devolver, numa estrutura fixa,
# tanto o texto de resposta quanto a classificação do lead — assim o CRM nunca
# fica desatualizado, mesmo quando a conversa é só um "oi, tudo bem?".
FERRAMENTA_RESPOSTA = {
    "name": "responder_e_classificar",
    "description": (
        "Responde ao lead no WhatsApp e classifica o estado atual do lead para o CRM. "
        "Deve ser chamada em toda mensagem, mesmo quando não há novidade na classificação."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mensagem_para_lead": {
                "type": "string",
                "description": (
                    "Texto que será enviado como resposta no WhatsApp, no tom de voz da clínica. "
                    "Curto, natural, sem parecer um robô."
                ),
            },
            "nome_lead": {
                "type": "string",
                "description": 'Nome do lead, se já foi dito na conversa. Deixe em branco ("") se ainda não souber.',
            },
            "origem": {
                "type": "string",
                "description": (
                    'Como o lead chegou até a clínica, se ele mencionar (ex.: "Instagram", "indicação", '
                    '"Google", "tráfego pago"). Deixe em branco ("") se não souber.'
                ),
            },
            "procedimento_interesse": {
                "type": "string",
                "description": 'Tratamento ou serviço de interesse do lead. Deixe em branco ("") se ainda não souber.',
            },
            "status": {
                "type": "string",
                "enum": ["em_conversa", "novo", "agendado", "quente", "perdido"],
                "description": "Estágio atual do lead no funil.",
            },
            "dados_completos": {
                "type": "boolean",
                "description": "true quando já sabemos o nome e o procedimento de interesse do lead.",
            },
            "encerrar_atendimento": {
                "type": "boolean",
                "description": (
                    "true quando esta mensagem encerra o atendimento — o lead já agendou, já tirou a "
                    "dúvida que tinha, ou se despediu e não precisa de mais nada agora. false enquanto "
                    "o atendimento ainda está em aberto. Quando true, a mensagem_para_lead deve soar "
                    "como um encerramento natural, não como mais uma pergunta."
                ),
            },
            "motivo_alerta": {
                "type": "string",
                "description": (
                    "Preencha só quando a equipe humana precisar assumir a conversa agora "
                    "(ex.: 'pediu preço e quer fechar hoje', ou 'não entendeu a pergunta duas vezes seguidas'). "
                    'Deixe em branco ("") na maioria das mensagens.'
                ),
            },
        },
        "required": [
            "mensagem_para_lead",
            "status",
            "nome_lead",
            "origem",
            "procedimento_interesse",
            "dados_completos",
            "encerrar_atendimento",
            "motivo_alerta",
        ],
    },
}


def data_e_hora_atual() -> str:
    """Data/hora de agora no fuso da clínica, em texto — pro Claude calcular datas reais."""
    agora = datetime.now(FUSO_HORARIO)
    dia_semana = DIAS_DA_SEMANA[agora.weekday()]
    return f"{dia_semana}, {agora.strftime('%d/%m/%Y')}, {agora.strftime('%H:%M')}"


def montar_prompt_sistema(clinica: dict) -> str:
    """Monta as instruções que dão à Claude a personalidade e os fatos da clínica."""
    return f"""
Você é a assistente de WhatsApp da {clinica['nome']}, uma clínica de estética.

Agora é: {data_e_hora_atual()}. Use essa informação para calcular datas reais — se o lead
pedir "segunda-feira" ou "semana que vem", calcule você mesma o dia e o mês exatos. Nunca
peça para o lead calcular ou pesquisar uma data por você.

Tom de voz: {clinica.get('tom_de_voz') or 'acolhedor, próximo e profissional'}.
Tratamentos oferecidos: {clinica.get('tratamentos') or 'não informado'}.
Horário de atendimento: {clinica.get('horario') or 'não informado'}.

Regras:
- Nunca prometa resultado de tratamento nem dê diagnóstico médico.
- NUNCA invente, estime ou "chute" valores/preços — você não tem uma tabela de preços real.
  Se perguntarem preço, explique que o valor exato depende da avaliação e que a equipe informa
  isso certinho depois de conhecer o caso; direcione para agendar a avaliação gratuita.
- Prefira sempre dar opções concretas a fazer perguntas em aberto. Em vez de "qual dia você
  prefere?", ofereça 2 ou 3 dias/horários reais dentro do horário de atendimento da clínica,
  já calculados a partir da data de hoje (ex.: "segunda, dia 24/08, às 10h ou 14h — qual fica
  melhor?").
- Se o lead quiser agendar, colete nome completo e procedimento de interesse, e confirme um
  dia e horário exatos (com data calculada por você, não pelo lead); informe que a equipe
  confirma em até 24h.
- Todo atendimento tem início e fim, mas só marque encerrar_atendimento como true quando o
  PRÓPRIO LEAD sinalizar claramente que não precisa de mais nada agora — uma despedida, um
  "obrigado, é só isso" ou equivalente. Confirmar um agendamento, sozinho, NÃO é motivo para
  encerrar: é exatamente quando o lead mais tende a mandar perguntas de acompanhamento (quem
  vai atender, endereço, o que levar). Quando o encerramento for claro, feche a conversa de
  forma natural e educada (agradeça, reforce o próximo passo se houver). Não fique reabrindo
  assuntos já resolvidos nem insistindo depois que o lead já se despediu.
- Se perceber que está repetindo a mesma pergunta ou resposta sem avançar (o lead não
  conseguiu resolver algo com você em duas tentativas), preencha motivo_alerta para a equipe
  assumir, em vez de insistir sozinha.
- Responda sempre pelo campo mensagem_para_lead da ferramenta responder_e_classificar — nunca
  fora dela.
""".strip()


# ---------------------------------------------------------------------------
# Supabase (via REST direto — sem depender do pacote supabase-py)
# ---------------------------------------------------------------------------

def supabase_get(tabela: str, params: dict) -> list[dict]:
    resposta = httpx.get(f"{SUPABASE_URL}/rest/v1/{tabela}", headers=supabase_headers, params=params, timeout=15)
    resposta.raise_for_status()
    return resposta.json()


def supabase_insert(tabela: str, dados: dict) -> dict:
    headers = {**supabase_headers, "Prefer": "return=representation"}
    resposta = httpx.post(f"{SUPABASE_URL}/rest/v1/{tabela}", headers=headers, json=dados, timeout=15)
    resposta.raise_for_status()
    return resposta.json()[0]


def supabase_update(tabela: str, id_registro: str, dados: dict) -> None:
    resposta = httpx.patch(
        f"{SUPABASE_URL}/rest/v1/{tabela}",
        headers=supabase_headers,
        params={"id": f"eq.{id_registro}"},
        json=dados,
        timeout=15,
    )
    resposta.raise_for_status()


def buscar_clinica_por_instancia(instancia: str) -> dict | None:
    linhas = supabase_get("clinics", {"instancia": f"eq.{instancia}", "limit": 1})
    return linhas[0] if linhas else None


def buscar_ou_criar_lead(clinic_id: str, telefone: str) -> dict:
    """Cria o lead assim que a primeira mensagem chega — nunca espera 'dados completos'."""
    existente = supabase_get("leads", {"clinic_id": f"eq.{clinic_id}", "telefone": f"eq.{telefone}", "limit": 1})
    if existente:
        return existente[0]
    return supabase_insert("leads", {"clinic_id": clinic_id, "telefone": telefone, "status": "novo"})


def buscar_ou_criar_conversa(lead_id: str) -> dict:
    """Reaproveita a conversa mais recente do lead se ela ainda estiver aberta OU se tiver
    sido encerrada há pouco tempo (dentro de JANELA_REABERTURA_CONVERSA) — nesse caso,
    reabre a mesma conversa (limpa encerrada_em) em vez de começar uma em branco. Isso evita
    que uma pergunta de acompanhamento logo depois de um agendamento derrube todo o contexto
    já combinado (nome, procedimento, dia e horário). Só depois dessa janela é que uma
    mensagem nova do lead começa, de fato, uma conversa com contexto limpo."""
    existente = supabase_get(
        "conversations",
        {
            "lead_id": f"eq.{lead_id}",
            "order": "iniciada_em.desc",
            "limit": 1,
        },
    )
    if existente:
        conversa = existente[0]
        if conversa["encerrada_em"] is None:
            return conversa

        encerrada_em = datetime.fromisoformat(conversa["encerrada_em"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - encerrada_em < JANELA_REABERTURA_CONVERSA:
            supabase_update("conversations", conversa["id"], {"encerrada_em": None})
            conversa["encerrada_em"] = None
            return conversa

    return supabase_insert("conversations", {"lead_id": lead_id})


def historico_da_conversa(conversation_id: str) -> list[dict]:
    """Busca as mensagens MAIS RECENTES da conversa (ordem decrescente + limite), depois
    devolve em ordem cronológica. Buscar em ordem crescente com limite faria o Supabase
    devolver as mensagens mais ANTIGAS assim que a conversa passasse do limite — o Claude
    ficaria preso vendo sempre o começo da conversa e nunca as respostas mais novas do lead."""
    mensagens = supabase_get(
        "messages",
        {
            "conversation_id": f"eq.{conversation_id}",
            "order": "criada_em.desc",
            "limit": HISTORICO_MAX_MENSAGENS,
        },
    )
    return list(reversed(mensagens))


def salvar_mensagem(conversation_id: str, direcao: str, texto: str) -> None:
    # direcao: "entrada" (mensagem do lead) ou "saida" (resposta do bot)
    supabase_insert("messages", {"conversation_id": conversation_id, "direcao": direcao, "texto": texto})


def encerrar_conversa(conversation_id: str) -> None:
    supabase_update("conversations", conversation_id, {"encerrada_em": datetime.now(timezone.utc).isoformat()})


def enviar_alerta(clinica: dict, lead: dict, motivo: str) -> None:
    supabase_insert(
        "alerts",
        {"lead_id": lead["id"], "tipo": motivo, "enviado_para": EQUIPE_WHATSAPP or None},
    )
    if not EQUIPE_WHATSAPP:
        return
    texto = (
        f"⚠️ Lead precisa de atenção — {clinica['nome']}\n"
        f"Lead: {lead.get('nome') or lead['telefone']}\n"
        f"Motivo: {motivo}"
    )
    enviar_mensagem_whatsapp(clinica["instancia"], EQUIPE_WHATSAPP, texto)


# ---------------------------------------------------------------------------
# Evolution API (envio de mensagem e leitura do webhook)
# ---------------------------------------------------------------------------

def enviar_mensagem_whatsapp(instancia: str, numero: str, texto: str) -> None:
    httpx.post(
        f"{EVOLUTION_API_URL}/message/sendText/{instancia}",
        headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
        json={"number": numero, "text": texto},
        timeout=15,
    ).raise_for_status()


def extrair_texto_mensagem(data: dict) -> str | None:
    """A Evolution API manda formatos diferentes dependendo do tipo de mensagem."""
    mensagem = data.get("message") or {}
    if "conversation" in mensagem:
        return mensagem["conversation"]
    if "extendedTextMessage" in mensagem:
        return mensagem["extendedTextMessage"].get("text")
    if "imageMessage" in mensagem:
        return mensagem["imageMessage"].get("caption")
    return None


def numero_a_partir_do_jid(remote_jid: str) -> str:
    # remote_jid vem como "5511999999999@s.whatsapp.net" — a gente só quer o número.
    return re.sub(r"@.*$", "", remote_jid)


# ---------------------------------------------------------------------------
# Webhook principal
# ---------------------------------------------------------------------------

@app.post("/webhook/whatsapp")
async def webhook_whatsapp(request: Request):
    if WEBHOOK_TOKEN and request.query_params.get("token") != WEBHOOK_TOKEN:
        return {"ignorado": "token inválido"}

    payload = await request.json()
    evento = (payload.get("event") or "").lower().replace("_", ".")
    if evento != "messages.upsert":
        return {"ignorado": f"evento {evento!r} não tratado"}

    data = payload.get("data") or {}
    key = data.get("key") or {}

    if key.get("fromMe"):
        # Mensagem que o próprio bot (ou a clínica manualmente) mandou — ignora.
        return {"ignorado": "mensagem enviada por nos"}

    remote_jid = key.get("remoteJid", "")
    if remote_jid.endswith("@g.us"):
        # Mensagem de grupo — este bot atende conversas 1:1 com leads, não grupos.
        return {"ignorado": "mensagem de grupo"}

    texto_recebido = extrair_texto_mensagem(data)
    if not texto_recebido:
        return {"ignorado": "mensagem sem texto (audio, figurinha, etc.)"}

    instancia = payload.get("instance", "")
    numero_lead = numero_a_partir_do_jid(remote_jid)

    clinica = buscar_clinica_por_instancia(instancia)
    if clinica is None:
        # Instância não cadastrada em nenhuma clínica — não deveria acontecer em produção.
        return {"erro": f"nenhuma clínica cadastrada para a instância {instancia!r}"}

    lead = buscar_ou_criar_lead(clinica["id"], numero_lead)
    conversa = buscar_ou_criar_conversa(lead["id"])
    salvar_mensagem(conversa["id"], "entrada", texto_recebido)

    historico = historico_da_conversa(conversa["id"])
    mensagens_claude = [
        {"role": "user" if m["direcao"] == "entrada" else "assistant", "content": m["texto"]} for m in historico
    ]

    resposta_claude = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=montar_prompt_sistema(clinica),
        tools=[FERRAMENTA_RESPOSTA],
        tool_choice={"type": "tool", "name": "responder_e_classificar"},
        messages=mensagens_claude,
    )

    bloco_ferramenta = next(b for b in resposta_claude.content if b.type == "tool_use")
    dados = bloco_ferramenta.input

    mensagem_para_lead = dados["mensagem_para_lead"]

    atualizacoes: dict = {
        "status": dados["status"],
        "dados_completos": dados.get("dados_completos", False),
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
    }
    if dados.get("nome_lead"):
        atualizacoes["nome"] = dados["nome_lead"]
    if dados.get("origem"):
        atualizacoes["origem"] = dados["origem"]
    if dados.get("procedimento_interesse"):
        atualizacoes["procedimento_interesse"] = dados["procedimento_interesse"]
    supabase_update("leads", lead["id"], atualizacoes)

    salvar_mensagem(conversa["id"], "saida", mensagem_para_lead)
    enviar_mensagem_whatsapp(instancia, numero_lead, mensagem_para_lead)

    if dados.get("encerrar_atendimento"):
        encerrar_conversa(conversa["id"])

    if dados.get("motivo_alerta"):
        enviar_alerta(clinica, {**lead, **atualizacoes}, dados["motivo_alerta"])

    return {"status": "ok"}


@app.get("/")
async def raiz():
    # Útil pra confirmar que o deploy está no ar.
    return {"status": "ok", "servico": "cliniq-whatsapp-bot"}
