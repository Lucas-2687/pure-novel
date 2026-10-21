-- Run against a dedicated project. No existing user/content data is deleted.
begin;
create table if not exists public.members (
  user_id uuid primary key references auth.users(id) on delete cascade,
  active boolean not null default true,
  created_at timestamptz not null default now()
);
create table if not exists public.reader_states (
  user_id uuid primary key references auth.users(id) on delete cascade,
  version bigint not null default 0,
  payload jsonb not null default '{"books":[],"settings":{}}'::jsonb,
  updated_at timestamptz not null default now(),
  constraint reader_payload_size check (octet_length(payload::text) <= 500000)
);
alter table public.members enable row level security;
alter table public.reader_states enable row level security;
revoke all on public.members, public.reader_states from anon, authenticated;
grant select on public.members, public.reader_states to authenticated;
grant all on public.members, public.reader_states to service_role;
drop policy if exists members_self on public.members;
create policy members_self on public.members for select to authenticated
  using (user_id = (select auth.uid()));
drop policy if exists reader_self on public.reader_states;
create policy reader_self on public.reader_states for select to authenticated
  using (user_id = (select auth.uid()) and exists (
    select 1 from public.members where user_id = (select auth.uid()) and active
  ));

create or replace function public.save_reader_state(p_version bigint, p_payload jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v bigint; uid uuid := auth.uid();
begin
  if uid is null or not exists (select 1 from public.members where user_id = uid and active) then
    raise exception 'not an active member' using errcode = '42501';
  end if;
  if p_version is null or p_version < 0 or p_payload is null
     or jsonb_typeof(p_payload) <> 'object'
     or jsonb_typeof(p_payload->'books') is distinct from 'array'
     or jsonb_typeof(p_payload->'settings') is distinct from 'object'
     or octet_length(p_payload::text) > 500000 then
    raise exception 'invalid payload' using errcode = '22023';
  end if;
  if jsonb_array_length(p_payload->'books') > 200 then
    raise exception 'too many books' using errcode = '22023';
  end if;
  insert into public.reader_states(user_id) values (uid) on conflict do nothing;
  select version into v from public.reader_states where user_id = uid for update;
  if v <> p_version then return jsonb_build_object('ok', false, 'version', v); end if;
  update public.reader_states set payload = p_payload, version = v + 1, updated_at = now() where user_id = uid;
  return jsonb_build_object('ok', true, 'version', v + 1);
end;
$$;
revoke all on function public.save_reader_state(bigint, jsonb) from public, anon;
grant execute on function public.save_reader_state(bigint, jsonb) to authenticated;

-- Durable rolling-minute counters. No table or RPC access for browser users.
create schema if not exists private;
revoke all on schema private from public, anon, authenticated;
create table if not exists private.reader_quotas (
  key text primary key, window_start timestamptz not null, count integer not null
);
create or replace function public.consume_reader_quota(p_user uuid, p_route text)
returns boolean language plpgsql security definer set search_path = '' as $$
declare now_at timestamptz := clock_timestamp(); k text; n integer; lim integer;
begin
  if not exists (select 1 from public.members where user_id = p_user and active)
     or p_route not in ('read', 'search', 'check', 'sync') then return false; end if;
  -- Serialize quota decisions across all function instances.
  perform pg_advisory_xact_lock(1080089);
  foreach k in array array['global', p_user::text || ':' || p_route] loop
    lim := case when k = 'global' then 240 when p_route = 'search' then 6
                when p_route = 'sync' then 30 else 24 end;
    insert into private.reader_quotas(key, window_start, count) values(k, now_at, 1)
    on conflict(key) do update set
      count = case when private.reader_quotas.window_start < now_at - interval '1 minute' then 1 else private.reader_quotas.count + 1 end,
      window_start = case when private.reader_quotas.window_start < now_at - interval '1 minute' then now_at else private.reader_quotas.window_start end
    returning count into n;
    if n > lim then return false; end if;
  end loop;
  return true;
end;
$$;
revoke all on function public.consume_reader_quota(uuid, text) from public, anon, authenticated;
grant execute on function public.consume_reader_quota(uuid, text) to service_role;
commit;