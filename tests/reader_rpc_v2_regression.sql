-- Dedicated TEST database only. Run as its migration owner after both migrations.
-- Pure SQL (no psql variables/extensions); all fixtures and changes roll back.
-- Exercises PostgreSQL RPC behavior, grants and RLS; does NOT emulate Supabase
-- Auth /user, GoTrue refresh or multi-connection concurrency. Those need staging.
begin;

create temporary table reader_v2_fixtures(label text primary key, uid uuid not null) on commit drop;
insert into reader_v2_fixtures values ('a', gen_random_uuid()), ('b', gen_random_uuid());
insert into auth.users(id) select uid from reader_v2_fixtures;
insert into public.members(user_id) select uid from reader_v2_fixtures;
insert into private.reader_quotas(key, window_start, count) values ('global', clock_timestamp(), 0)
  on conflict (key) do update set count = 0, window_start = excluded.window_start;
select set_config('reader.test_a', (select uid::text from reader_v2_fixtures where label = 'a'), true);
select set_config('reader.test_b', (select uid::text from reader_v2_fixtures where label = 'b'), true);
select set_config('request.jwt.claim.sub', current_setting('reader.test_a'), true);
select set_config('request.jwt.claims', jsonb_build_object('sub', current_setting('reader.test_a'), 'role', 'authenticated')::text, true);

create function pg_temp.reader_assert(ok boolean, message text)
returns void language plpgsql as $$
begin
  if ok is distinct from true then raise exception 'reader regression: %', message; end if;
end;
$$;

-- Fresh state, no-op CAS, changed save, stale equality and account isolation.
do $$
declare
  result jsonb; stamp timestamptz;
  a uuid := current_setting('reader.test_a')::uuid;
  b uuid := current_setting('reader.test_b')::uuid;
  payload_a jsonb := '{"books":[],"settings":{"name":"测试读者甲"}}';
begin
  perform pg_temp.reader_assert(public.reader_context_v2() = '{"version":0,"payload":null}'::jsonb, 'empty context');
  result := public.reader_save_v2(0, '{"books":[],"settings":{}}');
  perform pg_temp.reader_assert(result = '{"ok":true,"version":0}'::jsonb, 'initial equal payload is a no-op');
  result := public.reader_save_v2(0, payload_a);
  perform pg_temp.reader_assert(result = '{"ok":true,"version":1}'::jsonb, 'changed payload increments once');
  select updated_at into stamp from public.reader_states where user_id = a;
  result := public.reader_save_v2(1, payload_a);
  perform pg_temp.reader_assert(result = '{"ok":true,"version":1}'::jsonb, 'equal payload keeps version');
  perform pg_temp.reader_assert((select updated_at = stamp from public.reader_states where user_id = a), 'no-op does not write timestamp');
  result := public.reader_save_v2(0, payload_a);
  perform pg_temp.reader_assert(result = '{"ok":false,"version":1}'::jsonb, 'stale equal payload conflicts before equality');
  result := public.save_reader_state(1, payload_a);
  perform pg_temp.reader_assert(result = '{"ok":true,"version":1}'::jsonb, 'legacy save shares no-op behavior');
  perform set_config('request.jwt.claim.sub', b::text, true);
  perform set_config('request.jwt.claims', jsonb_build_object('sub', b, 'role', 'authenticated')::text, true);
  perform pg_temp.reader_assert(public.reader_context_v2() = '{"version":0,"payload":null}'::jsonb, 'B cannot see A context');
  result := public.reader_save_v2(0, '{"books":[],"settings":{"name":"读者乙"}}');
  perform pg_temp.reader_assert(result->>'version' = '1', 'B writes own state');
  perform pg_temp.reader_assert((select payload = payload_a from public.reader_states where user_id = a), 'B cannot modify A');
  perform set_config('request.jwt.claim.sub', a::text, true);
  perform set_config('request.jwt.claims', jsonb_build_object('sub', a, 'role', 'authenticated')::text, true);
end;
$$;

-- Legacy and v2 use the exact same durable rows. Neither denial spends global.
update private.reader_quotas set count = 0, window_start = clock_timestamp()
  where key = 'global' or key like current_setting('reader.test_a') || ':%';
do $$
declare a uuid := current_setting('reader.test_a')::uuid; i integer;
begin
  for i in 1..3 loop
    perform pg_temp.reader_assert(public.consume_reader_quota(a, 'search'), 'legacy search admitted');
    perform pg_temp.reader_assert(public.reader_gate_v2('search'), 'v2 search admitted');
  end loop;
  perform pg_temp.reader_assert(not public.consume_reader_quota(a, 'search'), 'legacy sees v2 usage');
  begin
    perform public.reader_gate_v2('search');
    raise exception 'expected v2 quota denial';
  exception when sqlstate 'PT429' then null;
  end;
  perform pg_temp.reader_assert((select count = 6 from private.reader_quotas where key = 'global'), 'user quota denial does not spend global');
  perform pg_temp.reader_assert((select count = 6 from private.reader_quotas where key = a::text || ':search'), 'denials do not increment user');
end;
$$;

-- All per-route fixed-window limits, including sync no-op admission.
do $$
declare a uuid := current_setting('reader.test_a')::uuid;
  route text; lim integer; i integer; version_at bigint; payload_at jsonb;
begin
  select version, payload into version_at, payload_at from public.reader_states where user_id = a;
  foreach route in array array['read', 'check', 'sync'] loop
    update private.reader_quotas set count = 0, window_start = clock_timestamp()
      where key = 'global' or key = a::text || ':' || route;
    lim := case when route = 'sync' then 30 else 24 end;
    for i in 1..lim loop
      if route = 'sync' then
        perform public.reader_save_v2(version_at, payload_at);
      elsif i % 2 = 0 then
        perform public.reader_gate_v2(route);
      else
        perform pg_temp.reader_assert(public.consume_reader_quota(a, route), 'mixed route admission');
      end if;
    end loop;
    begin
      if route = 'sync' then
        perform public.reader_save_v2(version_at, payload_at);
      else
        perform public.reader_gate_v2(route);
      end if;
      raise exception 'expected quota denial for %', route;
    exception when sqlstate 'PT429' then null;
    end;
    perform pg_temp.reader_assert(not public.consume_reader_quota(a, route), 'legacy route denial');
    perform pg_temp.reader_assert((select count = lim from private.reader_quotas where key = 'global'), 'route denial preserves global');
    perform pg_temp.reader_assert((select count = lim from private.reader_quotas where key = a::text || ':' || route), 'route limit exact');
  end loop;
end;
$$;

-- Exhausted global cannot spend user; the 240th acceptance is the last.
update private.reader_quotas set count = 0, window_start = clock_timestamp()
  where key = current_setting('reader.test_a') || ':read';
update private.reader_quotas set count = 240, window_start = clock_timestamp() where key = 'global';
do $$
declare a uuid := current_setting('reader.test_a')::uuid; stamp timestamptz;
begin
  perform pg_temp.reader_assert(not public.consume_reader_quota(a, 'read'), 'global full denial');
  begin
    perform public.reader_gate_v2('read');
    raise exception 'expected global denial';
  exception when sqlstate 'PT429' then null;
  end;
  perform pg_temp.reader_assert((select count = 0 from private.reader_quotas where key = a::text || ':read'), 'global denial preserves user');
  update private.reader_quotas set count = 239 where key = 'global';
  perform public.reader_gate_v2('read');
  perform pg_temp.reader_assert(not public.consume_reader_quota(a, 'read'), '241st admission denied');
  perform pg_temp.reader_assert((select count = 240 from private.reader_quotas where key = 'global'), 'global limit exact');
  perform pg_temp.reader_assert((select count = 1 from private.reader_quotas where key = a::text || ':read'), 'repeated global denial preserves user');
  update private.reader_quotas set count = 24, window_start = clock_timestamp() - interval '61 seconds'
    where key = a::text || ':read';
  update private.reader_quotas set count = 240, window_start = clock_timestamp() - interval '61 seconds' where key = 'global';
  perform public.reader_gate_v2('read');
  select window_start into stamp from private.reader_quotas where key = a::text || ':read';
  perform public.reader_gate_v2('read');
  perform pg_temp.reader_assert((select count = 2 and window_start = stamp from private.reader_quotas where key = a::text || ':read'), 'fixed user window does not slide');
  perform pg_temp.reader_assert((select count = 2 from private.reader_quotas where key = 'global'), 'expired global resets');
end;
$$;

-- SQL/Python share jsonb::text UTF-8 bytes, including separator spaces.
-- Validate precisely 500000/500001 bytes and 200/201 books without ASCII escaping.
do $$
declare p jsonb; overhead integer; a uuid := current_setting('reader.test_a')::uuid;
  before_count integer; v bigint;
begin
  p := jsonb_build_object('books', '[]'::jsonb, 'settings', jsonb_build_object('text', ''));
  overhead := octet_length(convert_to(p::text, 'UTF8'));
  p := jsonb_build_object('books', '[]'::jsonb, 'settings', jsonb_build_object('text', repeat('中', 100000) || repeat('a', 500000 - overhead - 300000)));
  perform pg_temp.reader_assert(octet_length(convert_to(p::text, 'UTF8')) = 500000, 'UTF8 boundary fixture');
  perform private.reader_validate_v2(1, p);
  p := jsonb_set(p, '{settings,text}', to_jsonb((p #>> '{settings,text}') || 'a'));
  select count into before_count from private.reader_quotas where key = 'global';
  begin
    perform public.reader_save_v2(1, p);
    raise exception 'expected oversize denial';
  exception when sqlstate 'PT413' then null;
  end;
  perform pg_temp.reader_assert((select count = before_count from private.reader_quotas where key = 'global'), 'invalid payload does not spend quota');
  select jsonb_build_object('books', jsonb_agg('{}'::jsonb), 'settings', '{}'::jsonb) into p from generate_series(1, 200);
  perform private.reader_validate_v2(1, p);
  p := jsonb_set(p, '{books}', (p->'books') || '[{}]'::jsonb);
  begin
    perform public.reader_save_v2(1, p);
    raise exception 'expected 201 books denial';
  exception when sqlstate 'PT400' then null;
  end;
  perform pg_temp.reader_assert((select count = before_count from private.reader_quotas where key = 'global'), 'book limit does not spend quota');
  perform pg_temp.reader_assert(octet_length('{"n": 0.0000001}'::jsonb::text) = 16, 'exponent expands in jsonb size');
  -- A failing write rolls admission back in the same statement/subtransaction.
  update private.reader_quotas set count = 0, window_start = clock_timestamp()
    where key = 'global' or key = a::text || ':sync';
  update public.reader_states set version = 9223372036854775807 where user_id = a;
  begin
    perform public.reader_save_v2(9223372036854775807, '{"books":[],"settings":{"overflow":true}}');
    raise exception 'expected version overflow';
  exception when numeric_value_out_of_range then null;
  end;
  perform pg_temp.reader_assert((select count = 0 from private.reader_quotas where key = 'global'), 'save failure rolls global back');
  perform pg_temp.reader_assert((select count = 0 from private.reader_quotas where key = a::text || ':sync'), 'save failure rolls user back');
  update public.reader_states set version = 1 where user_id = a;
end;
$$;

-- Missing/disabled identities are rejected by every public v2 operation.
update public.members set active = false where user_id = current_setting('reader.test_a')::uuid;
do $$
begin
  begin perform public.reader_context_v2(); raise exception 'expected context membership denial';
    exception when sqlstate 'PT403' then null; end;
  begin perform public.reader_gate_v2('read'); raise exception 'expected gate membership denial';
    exception when sqlstate 'PT403' then null; end;
  begin perform public.reader_save_v2(1, '{"books":[],"settings":{}}'); raise exception 'expected save membership denial';
    exception when sqlstate 'PT403' then null; end;
  begin perform public.save_reader_state(1, '{"books":[],"settings":{}}'); raise exception 'expected legacy membership denial';
    exception when sqlstate 'PT403' then null; end;
  perform pg_temp.reader_assert(not public.consume_reader_quota(current_setting('reader.test_a')::uuid, 'read'), 'legacy inactive member denied');
  perform set_config('request.jwt.claim.sub', '', true);
  perform set_config('request.jwt.claims', '{}', true);
  begin perform public.reader_context_v2(); raise exception 'expected missing identity denial';
    exception when sqlstate 'PT401' then null; end;
end;
$$;
update public.members set active = true where user_id = current_setting('reader.test_a')::uuid;
select set_config('request.jwt.claim.sub', current_setting('reader.test_a'), true);
select set_config('request.jwt.claims', jsonb_build_object('sub', current_setting('reader.test_a'), 'role', 'authenticated')::text, true);

-- RLS is tested under the actual authenticated DB role, not the migration owner.
set local role authenticated;
do $$
declare a uuid := current_setting('reader.test_a')::uuid; b uuid := current_setting('reader.test_b')::uuid;
begin
  if exists (select 1 from public.members where user_id = b) then raise exception 'RLS leaked B membership'; end if;
  if exists (select 1 from public.reader_states where user_id = b) then raise exception 'RLS leaked B state'; end if;
  if not exists (select 1 from public.reader_states where user_id = a) then raise exception 'RLS hid own active state'; end if;
  begin
    update public.reader_states set version = 100 where user_id = b;
    raise exception 'direct table write allowed';
  exception when insufficient_privilege then null; end;
  begin
    perform public.consume_reader_quota(b, 'read');
    raise exception 'browser p_user quota API allowed';
  exception when insufficient_privilege then null; end;
  if (public.reader_context_v2()->'payload') #>> '{settings,name}' <> '测试读者甲' then
    raise exception 'authenticated RPC failed own-account context';
  end if;
end;
$$;
reset role;

-- Public RPCs have no browser-supplied user argument; private helpers stay private.
select pg_temp.reader_assert(not has_function_privilege('anon', 'public.reader_context_v2()', 'EXECUTE'), 'anon cannot context');
select pg_temp.reader_assert(not has_function_privilege('anon', 'public.reader_gate_v2(text)', 'EXECUTE'), 'anon cannot gate');
select pg_temp.reader_assert(not has_function_privilege('anon', 'public.reader_save_v2(bigint,jsonb)', 'EXECUTE'), 'anon cannot save');
select pg_temp.reader_assert(not has_function_privilege('authenticated', 'public.consume_reader_quota(uuid,text)', 'EXECUTE'), 'browser cannot supply quota UID');
select pg_temp.reader_assert(has_function_privilege('service_role', 'public.consume_reader_quota(uuid,text)', 'EXECUTE'), 'legacy server retains admission');
select pg_temp.reader_assert(not has_function_privilege('authenticated', 'private.reader_admit_v2(uuid,text)', 'EXECUTE'), 'private helper has no browser grant');
select pg_temp.reader_assert(to_regprocedure('public.reader_save_v2(uuid,bigint,jsonb)') is null, 'save has no p_user overload');

rollback;
