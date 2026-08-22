# Bot de WhatsApp da Cliniq — Evolution API + Python + Supabase

Este guia assume que você **nunca mexeu com código**. Siga na ordem — cada etapa
te dá uma informação (uma "chave") que você vai colar na etapa "Juntando tudo".

O que já está pronto, feito nas conversas anteriores:

- ✅ Banco de dados no Supabase (tabelas `clinics`, `leads`, `conversations`,
  `messages`, `alerts`), já com a Cliniq cadastrada.
- ✅ Evolution API no ar na Railway, instância `cliniq-piloto` já **conectada**
  ao WhatsApp real (556192468740).

O que falta: colocar este bot em Python no ar e ligar as duas pontas.

## 1. Criar a chave da API do Claude

1. Acesse **platform.claude.com** e crie uma conta (ou entre com a sua conta Claude).
2. Vá em **Settings → API Keys** e clique em **Create Key**.
3. Copie a chave (começa com `sk-ant-...`) e guarde — ela some da tela depois.

## 2. Pegar a chave do Supabase

1. No painel do Supabase do seu projeto, vá em **Project Settings → API Keys →
   Legacy anon, service_role API keys**.
2. Clique em **Reveal** na linha `service_role` (não a `anon`!) e copie o valor.

## 3. Juntando tudo (arquivo `.env`)

As chaves secretas (Anthropic, Supabase, Evolution API, webhook) nunca vão para
este repositório — elas são coladas direto na aba **Variables** do Railway
(passo 4), que é um lugar privado e seguro pra esse tipo de valor.

## 4. Colocar o bot no ar (Railway)

Este repositório já está conectado a um serviço na Railway. Pra configurar:

1. No painel do Railway, abra o serviço `cliniq-bot`.
2. Na aba **Variables**, use "Raw Editor" e cole os nomes do `.env.example`
   já preenchidos com os valores reais de cada chave.
3. Vá na aba **Settings → Networking** e clique em **Generate Domain**. Você
   vai receber uma URL pública, algo como
   `https://cliniq-bot-production.up.railway.app`.

## 5. Ligar a Evolution API ao bot

1. Acesse o painel da Evolution API:
   `https://evolution-api-production-ff55.up.railway.app/manager`
   (API Key Global: a mesma que você colocou em `EVOLUTION_API_KEY`).
2. Abra a instância **cliniq-piloto → Events → Webhook**.
3. Ative **Enabled**, e em **URL** cole:
   ```
   https://SEU-ENDERECO-DO-RAILWAY.up.railway.app/webhook/whatsapp?token=SEU_WEBHOOK_TOKEN
   ```
   (troque `SEU-ENDERECO-DO-RAILWAY` pela URL gerada no passo 4, e
   `SEU_WEBHOOK_TOKEN` pelo mesmo valor que você colocou na variável
   `WEBHOOK_TOKEN`.)
4. Na lista de **Events**, ative só **MESSAGES_UPSERT**.
5. Salve.

## 6. Testar

1. Pelo seu celular (um número diferente do 556192468740, que é o da clínica),
   mande uma mensagem tipo "Oi, quero saber sobre harmonização facial" para o
   WhatsApp da Cliniq.
2. Em alguns segundos a resposta deve chegar automaticamente.
3. No Supabase, abra **Table Editor → leads** e **messages** — você deve ver
   o lead e a conversa registrados ali, ao vivo, mesmo que a conversa pare no
   meio (essa é a correção que fizemos: todo lead entra no CRM desde a
   primeira mensagem, não só quando "completa" os dados).

Se algo não funcionar, volte aqui e me conte em qual etapa travou e a
mensagem de erro exata (se houver) — eu te ajudo a destravar.

## O que vem depois

Isso cobre o MVP piloto rodando de ponta a ponta pelo WhatsApp real. As
próximas fases — ligar o CRM visual (a tela da Cliniq) a esses dados de
verdade, os alertas automáticos pra equipe, e o cadastro de novas clínicas
(multi-tenant) — a gente constrói depois que esse fluxo básico estiver
validado no seu ambiente.
