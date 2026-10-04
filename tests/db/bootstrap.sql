-- Simula o que o Supabase já traz pronto, para o schema exportado carregar num Postgres comum.
-- Usado só no banco local de testes (scripts/testdb.py).

create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;
create role supabase_admin nologin;
create role orbix_api login password 'test' nobypassrls noinherit;
create role orbix_worker login password 'test' nobypassrls;
grant authenticated to orbix_api;

create schema auth;
-- só as colunas de auth.users que o back-end usa
create table auth.users (
  id uuid primary key default gen_random_uuid(),
  instance_id uuid,
  aud text,
  role text,
  email text,
  created_at timestamptz,
  updated_at timestamptz,
  raw_app_meta_data jsonb,
  raw_user_meta_data jsonb
);
create function auth.uid() returns uuid language sql stable as $$
  select nullif(
    coalesce(
      current_setting('request.jwt.claim.sub', true),
      current_setting('request.jwt.claims', true)::jsonb ->> 'sub'
    ), ''
  )::uuid
$$;
grant usage on schema auth to anon, authenticated, service_role, orbix_api, orbix_worker;
grant execute on function auth.uid() to anon, authenticated, service_role, orbix_api, orbix_worker;

-- o dump recria o schema public
drop schema public cascade;
