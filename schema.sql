-- Esqueleto do banco de dados do CRM Cliniq.
-- Ajustado para bater exatamente com os campos que o workflow "WhatsApp Lead
-- Agent - Scalvio CRM" (n8n) já envia hoje: nome, telefone, origem,
-- procedimento_interesse.
--
-- Cole este arquivo inteiro no SQL Editor do Supabase e clique em "Run".

-- Clínicas cadastradas na plataforma. "instancia" é o nome da instância da
-- Evolution API daquela clínica (é esse valor que o n8n lê como
-- {{$json.body.instance}}) — ainda não está sendo usado para separar leads
-- por clínica hoje, mas já deixamos a coluna pronta para a Fase 4.
create table if not exists clinics (
  id uuid primary key default gen_random_uuid(),
  nome text not null,
  instancia text unique, -- nome da instância na Evolution API
  tratamentos text,
  horario text,
  tom_de_voz text,
  criada_em timestamptz not null default now()
);

-- Leads captados pelo bot do WhatsApp.
-- clinic_id fica opcional por enquanto (o n8n ainda não envia esse campo) —
-- passa a ser preenchido quando ligarmos o mapeamento instância -> clínica.
create table if not exists leads (
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid references clinics(id),
  nome text,
  telefone text not null unique,
  origem text,                 -- ex.: Instagram, indicação, Google, tráfego pago
  procedimento_interesse text,
  status text not null default 'em_conversa'
    check (status in ('em_conversa', 'novo', 'agendado', 'quente', 'perdido')),
  dados_completos boolean not null default false,
  criado_em timestamptz not null default now(),
  atualizado_em timestamptz not null default now()
);

-- Histórico de conversas — ainda não alimentado pelo n8n hoje, fica pronto
-- para a Fase 2 (tela do CRM mostrando o histórico completo por lead).
create table if not exists conversations (
  id uuid primary key default gen_random_uuid(),
  lead_id uuid not null references leads(id),
  iniciada_em timestamptz not null default now(),
  ultima_mensagem_em timestamptz not null default now(),
  encerrada_em timestamptz -- preenchido quando o atendimento chega a uma conclusão natural;
                            -- null enquanto a conversa está em aberto. Uma nova mensagem do
                            -- lead depois disso começa uma conversa nova (contexto limpo).
);

create table if not exists messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references conversations(id),
  direcao text not null check (direcao in ('entrada', 'saida')),
  texto text not null,
  criada_em timestamptz not null default now()
);

-- Alertas para a equipe humana (Fase 3 do roteiro).
create table if not exists alerts (
  id uuid primary key default gen_random_uuid(),
  lead_id uuid not null references leads(id),
  tipo text not null,
  enviado_para text,
  resolvido boolean not null default false,
  criado_em timestamptz not null default now()
);

-- Linha de exemplo para o piloto Cliniq. Depois de criar a instância na
-- Evolution API, troque o valor abaixo pelo nome real da instância.
insert into clinics (nome, instancia, tratamentos, horario, tom_de_voz)
values (
  'Cliniq Estética & Resultados',
  'cliniq-piloto',
  'Toxina botulínica, peelings, ultrassom microfocado, fios de PDO, bioestimuladores de colágeno, '
  'hidratação injetável, microagulhamento, drenagem linfática, massagem modeladora, depilação a laser, '
  'criolipólise, carboxiterapia',
  'Seg-Sex 09:00-19:00, Sáb 09:00-14:00',
  'acolhedor, sofisticado e próximo, sem ser informal demais'
)
on conflict (instancia) do nothing;
