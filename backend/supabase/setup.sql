-- GWPlaymate backend compatibility setup.
-- Run in Supabase SQL Editor after reviewing. This is idempotent.

alter table if exists public.game_logs
    add column if not exists source text not null default 'gwtoolboxpp-playmate',
    add column if not exists event_type text not null default 'game_log',
    add column if not exists map_id integer,
    add column if not exists instance_type integer,
    add column if not exists district integer,
    add column if not exists instance_time integer,
    add column if not exists active_quest_id integer,
    add column if not exists quest_count integer,
    add column if not exists active_quest_name text,
    add column if not exists active_quest_objectives text,
    add column if not exists payload jsonb not null default '{}'::jsonb;

create extension if not exists vector;

create table if not exists public.companion_replies (
    id bigserial primary key,
    created_at timestamptz not null default now(),
    consumed_at timestamptz,
    persona text not null,
    message text not null,
    channel text not null default 'party',
    payload jsonb not null default '{}'::jsonb
);

alter table if exists public.companion_replies
    add column if not exists consumed_at timestamptz,
    add column if not exists persona text,
    add column if not exists message text,
    add column if not exists channel text not null default 'party',
    add column if not exists payload jsonb not null default '{}'::jsonb;

alter table if exists public.companion_replies
    alter column persona drop default;

create table if not exists public.environment_alerts (
    id bigserial primary key,
    created_at timestamptz not null default now(),
    alert_type text not null default 'environment_alert',
    severity text not null default 'NORMAL',
    map_id integer,
    player_x real,
    player_y real,
    agent_id integer,
    model_id integer,
    agent_name text,
    distance real,
    faction text,
    message text,
    payload jsonb not null default '{}'::jsonb
);

alter table if exists public.environment_alerts
    add column if not exists alert_type text not null default 'environment_alert',
    add column if not exists severity text not null default 'NORMAL',
    add column if not exists map_id integer,
    add column if not exists message text,
    add column if not exists payload jsonb not null default '{}'::jsonb;

create table if not exists public.memories (
    id bigserial primary key,
    created_at timestamptz not null default now(),
    character_name text not null default 'Unknown Character',
    session_id text not null default 'local-playtest',
    memory_type text not null default 'session_summary',
    title text,
    summary_text text not null,
    map_id integer,
    active_quest_id integer,
    rare_items jsonb not null default '[]'::jsonb,
    tags text[] not null default '{}'::text[],
    source_log_start_id bigint references public.game_logs(id),
    source_log_end_id bigint references public.game_logs(id),
    embedding vector(1536),
    metadata jsonb not null default '{}'::jsonb
);

alter table if exists public.memories
    add column if not exists character_name text,
    add column if not exists session_id text not null default 'local-playtest',
    add column if not exists memory_type text not null default 'session_summary',
    add column if not exists title text,
    add column if not exists map_id integer,
    add column if not exists active_quest_id integer,
    add column if not exists rare_items jsonb not null default '[]'::jsonb,
    add column if not exists tags text[] not null default '{}'::text[],
    add column if not exists source_log_start_id bigint references public.game_logs(id),
    add column if not exists source_log_end_id bigint references public.game_logs(id),
    add column if not exists embedding vector(1536),
    add column if not exists metadata jsonb not null default '{}'::jsonb;

update public.memories
set character_name = coalesce(
    nullif(character_name, ''),
    nullif(metadata->>'character_name', ''),
    nullif(metadata->>'persona', ''),
    'Unknown Character'
)
where character_name is null or character_name = '';

alter table if exists public.memories
    alter column character_name set default 'Unknown Character',
    alter column character_name set not null;

alter table public.companion_replies enable row level security;
alter table public.environment_alerts enable row level security;
alter table public.memories enable row level security;

create index if not exists memories_character_created_idx
    on public.memories (character_name, created_at desc);

create index if not exists memories_type_created_idx
    on public.memories (memory_type, created_at desc);

create index if not exists memories_tags_idx
    on public.memories using gin (tags);

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'playmate-tts',
    'playmate-tts',
    false,
    5242880,
    array['audio/mpeg', 'audio/wav', 'audio/ogg']::text[]
)
on conflict (id) do update set
    public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

do $$
begin
    if not exists (
        select 1
        from pg_publication
        where pubname = 'supabase_realtime'
    ) then
        create publication supabase_realtime;
    end if;

    if not exists (
        select 1
        from pg_publication_tables
        where pubname = 'supabase_realtime'
          and schemaname = 'public'
          and tablename = 'game_logs'
    ) then
        alter publication supabase_realtime add table public.game_logs;
    end if;

    if not exists (
        select 1
        from pg_publication_tables
        where pubname = 'supabase_realtime'
          and schemaname = 'public'
          and tablename = 'companion_replies'
    ) then
        alter publication supabase_realtime add table public.companion_replies;
    end if;

    if not exists (
        select 1
        from pg_publication_tables
        where pubname = 'supabase_realtime'
          and schemaname = 'public'
          and tablename = 'environment_alerts'
    ) then
        alter publication supabase_realtime add table public.environment_alerts;
    end if;

    if not exists (
        select 1
        from pg_publication_tables
        where pubname = 'supabase_realtime'
          and schemaname = 'public'
          and tablename = 'memories'
    ) then
        alter publication supabase_realtime add table public.memories;
    end if;
end $$;
