-- Apply explicitly to a test database first, after 202610190001_reader.sql.
-- Set READER_RPC_V2=1 on the server only after this migration is verified.
-- New user-JWT RPCs: reader_context_v2(), reader_gate_v2(text),
-- reader_save_v2(bigint,jsonb). Missing RPCs must fail closed, never fall back.
-- Legacy RPC signatures and durable first-admission fixed-minute windows remain.
begin;

revoke all on schema private from public, anon, authenticated;
revoke all on table private.reader_quotas from public, anon, authenticated;

create or replace function private.reader_member_v2()
returns uuid language plpgsql security definer set search_path = '' as $$
declare uid uuid := auth.uid();
begin
  if uid is null then
    raise exception 'authentication required' using errcode = 'PT401';
  end if;
  perform 1 from public.members where user_id = uid and active for share;
  if not found then
    raise exception 'not an active member' using errcode = 'PT403';
  end if;
  return uid;
end;
$$;

-- Lock order is always member -> user/route counter -> global counter -> state.
-- Both limits are checked before either counter is changed. Empty placeholder
-- rows created by a denial have count zero and do not consume either limit.
create or replace function private.reader_admit_v2(p_user uuid, p_route text)
returns boolean language plpgsql security definer set search_path = '' as $$
declare
  k text; lim integer; now_at timestamptz;
  user_start timestamptz; global_start timestamptz;
  user_count integer; global_count integer;
  user_expired boolean; global_expired boolean;
begin
  if p_user is null or p_route is null or p_route not in ('read', 'search', 'check', 'sync') then
    return false;
  end if;
  perform 1 from public.members where user_id = p_user and active for share;
  if not found then return false; end if;
  k := p_user::text || ':' || p_route;
  lim := case p_route when 'search' then 6 when 'sync' then 30 else 24 end;
  insert into private.reader_quotas(key, window_start, count)
    values (k, clock_timestamp(), 0) on conflict (key) do nothing;
  select window_start, count into user_start, user_count
    from private.reader_quotas where key = k for update;
  insert into private.reader_quotas(key, window_start, count)
    values ('global', clock_timestamp(), 0) on conflict (key) do nothing;
  select window_start, count into global_start, global_count
    from private.reader_quotas where key = 'global' for update;
  now_at := clock_timestamp();
  user_expired := user_start <= now_at - interval '1 minute';
  global_expired := global_start <= now_at - interval '1 minute';
  if user_expired then user_count := 0; end if;
  if global_expired then global_count := 0; end if;
  if user_count >= lim or global_count >= 240 then return false; end if;
  update private.reader_quotas set count = user_count + 1,
    window_start = case when user_expired or user_count = 0 then now_at else user_start end
    where key = k;
  update private.reader_quotas set count = global_count + 1,
    window_start = case when global_expired or global_count = 0 then now_at else global_start end
    where key = 'global';
  return true;
end;
$$;

create or replace function public.consume_reader_quota(p_user uuid, p_route text)
returns boolean language plpgsql security definer set search_path = '' as $$
begin
  return private.reader_admit_v2(p_user, p_route);
end;
$$;

create or replace function private.reader_validate_v2(p_version bigint, p_payload jsonb)
returns void language plpgsql security definer set search_path = '' as $$
begin
  if p_version is null or p_version < 0 or p_payload is null
     or jsonb_typeof(p_payload) <> 'object'
     or jsonb_typeof(p_payload->'books') is distinct from 'array'
     or jsonb_typeof(p_payload->'settings') is distinct from 'object' then
    raise exception 'invalid payload' using errcode = 'PT400';
  end if;
  if jsonb_array_length(p_payload->'books') > 200 then
    raise exception 'too many books' using errcode = 'PT400';
  end if;
  -- Same UTF-8 jsonb::text byte definition as cloud.payload_size(). The
  -- original table constraint remains valid; no existing state is rewritten.
  if octet_length(convert_to(p_payload::text, 'UTF8')) > 500000 then
    raise exception 'payload too large' using errcode = 'PT413';
  end if;
end;
$$;

create or replace function private.reader_write_v2(p_user uuid, p_version bigint, p_payload jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v bigint; old_payload jsonb;
begin
  insert into public.reader_states(user_id) values (p_user) on conflict (user_id) do nothing;
  select version, payload into v, old_payload from public.reader_states
    where user_id = p_user for update;
  -- CAS precedes equality: stale snapshots never silently succeed.
  if v <> p_version then return jsonb_build_object('ok', false, 'version', v); end if;
  if old_payload = p_payload then
    return jsonb_build_object('ok', true, 'version', v);
  end if;
  update public.reader_states set payload = p_payload, version = v + 1,
    updated_at = clock_timestamp() where user_id = p_user;
  return jsonb_build_object('ok', true, 'version', v + 1);
end;
$$;

create or replace function public.reader_context_v2()
returns jsonb language plpgsql security definer set search_path = '' as $$
declare uid uuid; result jsonb;
begin
  uid := private.reader_member_v2();
  select jsonb_build_object('version', version, 'payload', payload) into result
    from public.reader_states where user_id = uid;
  return coalesce(result, jsonb_build_object('version', 0, 'payload', null));
end;
$$;

create or replace function public.reader_gate_v2(p_route text)
returns boolean language plpgsql security definer set search_path = '' as $$
declare uid uuid;
begin
  uid := private.reader_member_v2();
  if p_route is null or p_route not in ('read', 'search', 'check') then
    raise exception 'invalid route' using errcode = 'PT400';
  end if;
  if not private.reader_admit_v2(uid, p_route) then
    raise exception 'reader quota exceeded' using errcode = 'PT429';
  end if;
  return true;
end;
$$;

create or replace function public.reader_save_v2(p_version bigint, p_payload jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare uid uuid;
begin
  uid := private.reader_member_v2();
  perform private.reader_validate_v2(p_version, p_payload);
  if not private.reader_admit_v2(uid, 'sync') then
    raise exception 'reader quota exceeded' using errcode = 'PT429';
  end if;
  return private.reader_write_v2(uid, p_version, p_payload);
end;
$$;

-- Legacy servers still consume quota separately. Do not charge it twice here.
create or replace function public.save_reader_state(p_version bigint, p_payload jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare uid uuid;
begin
  uid := private.reader_member_v2();
  perform private.reader_validate_v2(p_version, p_payload);
  return private.reader_write_v2(uid, p_version, p_payload);
end;
$$;

revoke all on function private.reader_member_v2() from public, anon, authenticated, service_role;
revoke all on function private.reader_admit_v2(uuid, text) from public, anon, authenticated, service_role;
revoke all on function private.reader_validate_v2(bigint, jsonb) from public, anon, authenticated, service_role;
revoke all on function private.reader_write_v2(uuid, bigint, jsonb) from public, anon, authenticated, service_role;
revoke all on function public.consume_reader_quota(uuid, text) from public, anon, authenticated;
grant execute on function public.consume_reader_quota(uuid, text) to service_role;
revoke all on function public.save_reader_state(bigint, jsonb) from public, anon, service_role;
grant execute on function public.save_reader_state(bigint, jsonb) to authenticated;
revoke all on function public.reader_context_v2() from public, anon, service_role;
revoke all on function public.reader_gate_v2(text) from public, anon, service_role;
revoke all on function public.reader_save_v2(bigint, jsonb) from public, anon, service_role;
grant execute on function public.reader_context_v2() to authenticated;
grant execute on function public.reader_gate_v2(text) to authenticated;
grant execute on function public.reader_save_v2(bigint, jsonb) to authenticated;
commit;
