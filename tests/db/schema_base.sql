--
-- PostgreSQL database dump
--

\restrict BbZBjFFQbIgQhvrgX5tZ58RdZXFRCRchhzxgH3eY2pKUPmIbhrpxZjeBdT4CHoE

-- Dumped from database version 17.6
-- Dumped by pg_dump version 17.11

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA public;


--
-- Name: handle_new_user(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.handle_new_user() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
begin
  insert into public.profiles (id) values (new.id)
  on conflict (id) do nothing;
  return new;
end;
$$;


--
-- Name: orbix_assign_report_revision(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.orbix_assign_report_revision() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
DECLARE owner_id uuid;
BEGIN
  SELECT user_id INTO owner_id FROM public.reports WHERE id=NEW.report_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Report not found'; END IF;
  IF NEW.user_id IS NOT NULL AND NEW.user_id<>owner_id THEN
    RAISE EXCEPTION 'Report version owner mismatch';
  END IF;
  NEW.user_id=owner_id;
  SELECT coalesce(max(revision),0)+1 INTO NEW.revision FROM public.report_versions WHERE report_id=NEW.report_id;
  RETURN NEW;
END $$;


--
-- Name: orbix_guard_identity(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.orbix_guard_identity() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
DECLARE col text;
BEGIN
  FOREACH col IN ARRAY TG_ARGV LOOP
    IF to_jsonb(NEW)->col IS DISTINCT FROM to_jsonb(OLD)->col THEN
      RAISE EXCEPTION 'Identity field % cannot change on %',col,TG_TABLE_NAME;
    END IF;
  END LOOP;
  RETURN NEW;
END $$;


--
-- Name: orbix_log_event_review(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.orbix_log_event_review() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
BEGIN
  IF to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
    INSERT INTO public.event_reviews(event_id,user_id,before_state,after_state,reason,actor_user_id)
    VALUES(NEW.id,NEW.user_id,to_jsonb(OLD),to_jsonb(NEW),
           coalesce(nullif(btrim(NEW.review_reason),''),'server update; reason not supplied'),auth.uid());
  END IF;
  RETURN NEW;
END $$;


--
-- Name: orbix_reject_version_update(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.orbix_reject_version_update() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
BEGIN RAISE EXCEPTION 'Create a new report version instead of updating an existing one'; END $$;


--
-- Name: orbix_touch_report(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.orbix_touch_report() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
BEGIN NEW.updated_at=clock_timestamp(); RETURN NEW; END $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assets (
    chain text NOT NULL,
    asset text NOT NULL,
    coingecko_id text,
    symbol text,
    decimals integer,
    is_stable boolean DEFAULT false NOT NULL,
    status text DEFAULT 'ok'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    retry_after timestamp with time zone,
    resolved_at timestamp with time zone,
    CONSTRAINT assets_chain_check CHECK ((chain = ANY (ARRAY['solana'::text, 'hyperliquid'::text]))),
    CONSTRAINT assets_metadata_valid_check CHECK ((((decimals IS NULL) OR ((decimals >= 0) AND (decimals <= 255))) AND (attempts >= 0))),
    CONSTRAINT assets_status_check CHECK ((status = ANY (ARRAY['ok'::text, 'not_listed'::text, 'no_history'::text])))
);


--
-- Name: event_reviews; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.event_reviews (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_id uuid NOT NULL,
    user_id uuid NOT NULL,
    before_state jsonb NOT NULL,
    after_state jsonb NOT NULL,
    reason text NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    actor_user_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT event_reviews_reason_check CHECK ((length(btrim(reason)) > 0))
);


--
-- Name: events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    wallet_id uuid NOT NULL,
    user_id uuid NOT NULL,
    chain text NOT NULL,
    tx_hash text NOT NULL,
    event_index integer DEFAULT 0 NOT NULL,
    ts timestamp with time zone NOT NULL,
    kind text NOT NULL,
    asset text NOT NULL,
    qty numeric(38,18) NOT NULL,
    usd_price numeric(38,12),
    ptax numeric(12,6),
    brl_value numeric(20,2),
    raw jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    price_ts timestamp with time zone,
    ptax_date date,
    pricing_policy text,
    operation_id text,
    review_status text DEFAULT 'pending'::text NOT NULL,
    review_reason text,
    price_source text,
    CONSTRAINT events_chain_check CHECK ((chain = ANY (ARRAY['solana'::text, 'hyperliquid'::text]))),
    CONSTRAINT events_index_nonnegative_check CHECK ((event_index >= 0)),
    CONSTRAINT events_kind_check CHECK ((kind = ANY (ARRAY['swap_in'::text, 'swap_out'::text, 'transfer_in'::text, 'transfer_out'::text, 'stake'::text, 'unstake'::text, 'reward'::text, 'perp_fill'::text, 'funding'::text, 'fee'::text, 'other'::text]))),
    CONSTRAINT events_numbers_valid_check CHECK (((qty <> 'NaN'::numeric) AND ((usd_price IS NULL) OR ((usd_price >= (0)::numeric) AND (usd_price <> 'NaN'::numeric))) AND ((ptax IS NULL) OR ((ptax > (0)::numeric) AND (ptax <> 'NaN'::numeric))) AND ((brl_value IS NULL) OR (brl_value <> 'NaN'::numeric)))),
    CONSTRAINT events_review_state_check CHECK ((review_status = ANY (ARRAY['pending'::text, 'reviewed'::text, 'excluded'::text])))
);


--
-- Name: fx_rates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fx_rates (
    date date NOT NULL,
    ptax_buy numeric(12,6) NOT NULL,
    ptax_sell numeric(12,6) NOT NULL,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    quoted_at timestamp with time zone,
    CONSTRAINT fx_rates_number_valid_check CHECK (((ptax_buy > (0)::numeric) AND (ptax_sell > (0)::numeric) AND (ptax_buy <> 'NaN'::numeric) AND (ptax_sell <> 'NaN'::numeric)))
);


--
-- Name: ingestion_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ingestion_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    wallet_id uuid NOT NULL,
    user_id uuid NOT NULL,
    chain text NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    requested_from timestamp with time zone,
    requested_to timestamp with time zone,
    covered_from timestamp with time zone,
    covered_to timestamp with time zone,
    cursor_data jsonb,
    limitations text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    CONSTRAINT ingestion_runs_check CHECK (((requested_from IS NULL) OR (requested_to IS NULL) OR (requested_from <= requested_to))),
    CONSTRAINT ingestion_runs_check1 CHECK (((covered_from IS NULL) OR (covered_to IS NULL) OR (covered_from <= covered_to))),
    CONSTRAINT ingestion_runs_status_check CHECK ((status = ANY (ARRAY['running'::text, 'complete'::text, 'partial'::text, 'failed'::text])))
);


--
-- Name: monthly_activity_summary; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.monthly_activity_summary WITH (security_invoker='true') AS
 SELECT user_id,
    (date_trunc('month'::text, (ts AT TIME ZONE 'America/Sao_Paulo'::text)))::date AS month,
    count(*) AS event_count,
    count(*) FILTER (WHERE (brl_value IS NULL)) AS unpriced_event_count,
    count(*) FILTER (WHERE (review_status = 'pending'::text)) AS pending_review_count,
    sum(abs(brl_value)) AS gross_movement_brl,
    (count(*) FILTER (WHERE (brl_value IS NULL)) = 0) AS all_events_priced
   FROM public.events
  GROUP BY user_id, (date_trunc('month'::text, (ts AT TIME ZONE 'America/Sao_Paulo'::text)));


--
-- Name: monthly_summary; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.monthly_summary WITH (security_invoker='true') AS
 SELECT user_id,
    (date_trunc('month'::text, ts))::date AS month,
    count(*) AS events,
    count(*) FILTER (WHERE (brl_value IS NULL)) AS unpriced_events,
    COALESCE(sum(abs(brl_value)), (0)::numeric) AS volume_brl
   FROM public.events
  GROUP BY user_id, (date_trunc('month'::text, ts));


--
-- Name: prices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.prices (
    chain text NOT NULL,
    asset text NOT NULL,
    ts timestamp with time zone NOT NULL,
    usd_price numeric(38,12) NOT NULL,
    granularity text DEFAULT 'hour'::text NOT NULL,
    source text DEFAULT 'coingecko'::text NOT NULL,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT prices_chain_check CHECK ((chain = ANY (ARRAY['solana'::text, 'hyperliquid'::text]))),
    CONSTRAINT prices_granularity_check CHECK ((granularity = ANY (ARRAY['hour'::text, 'day'::text]))),
    CONSTRAINT prices_number_valid_check CHECK (((usd_price >= (0)::numeric) AND (usd_price <> 'NaN'::numeric))),
    CONSTRAINT prices_source_check1 CHECK ((source = ANY (ARRAY['coingecko'::text, 'hyperliquid'::text, 'stablecoin'::text, 'manual'::text])))
);


--
-- Name: prices_daily_v1; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.prices_daily_v1 (
    asset text NOT NULL,
    date date NOT NULL,
    usd_price numeric(38,12) NOT NULL,
    source text DEFAULT 'coingecko'::text NOT NULL,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT prices_source_check CHECK ((source = ANY (ARRAY['coingecko'::text, 'hyperliquid'::text, 'stablecoin'::text, 'manual'::text])))
);


--
-- Name: profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.profiles (
    id uuid NOT NULL,
    primary_wallet text,
    display_name text,
    plan text DEFAULT 'free'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT profiles_display_name_check CHECK ((char_length(display_name) <= 60)),
    CONSTRAINT profiles_plan_check CHECK ((plan = ANY (ARRAY['free'::text, 'pro'::text, 'accountant'::text])))
);


--
-- Name: report_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.report_versions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    report_id uuid NOT NULL,
    user_id uuid NOT NULL,
    revision integer NOT NULL,
    snapshot jsonb NOT NULL,
    rules_version text NOT NULL,
    r2_object_key text NOT NULL,
    sha256 text NOT NULL,
    solana_sig text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT report_versions_r2_object_key_check CHECK (((length(btrim(r2_object_key)) > 0) AND (r2_object_key !~ '://'::text))),
    CONSTRAINT report_versions_revision_check CHECK ((revision > 0)),
    CONSTRAINT report_versions_rules_version_check CHECK ((length(btrim(rules_version)) > 0)),
    CONSTRAINT report_versions_sha256_check CHECK ((sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT report_versions_snapshot_check CHECK ((jsonb_typeof(snapshot) = 'object'::text))
);


--
-- Name: reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reports (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    month date NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    total_brl numeric(20,2) DEFAULT 0 NOT NULL,
    gains_brl numeric(20,2) DEFAULT 0 NOT NULL,
    over_35k boolean DEFAULT false NOT NULL,
    data jsonb,
    file_url text,
    sha256 text,
    solana_sig text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    salt bytea,
    r2_object_key text,
    reporting_required boolean,
    rules_version text,
    CONSTRAINT reports_month_check CHECK ((EXTRACT(day FROM month) = (1)::numeric)),
    CONSTRAINT reports_numbers_valid_check CHECK (((total_brl >= (0)::numeric) AND (total_brl <> 'NaN'::numeric) AND (gains_brl <> 'NaN'::numeric))),
    CONSTRAINT reports_salt_len_check CHECK (((salt IS NULL) OR (octet_length(salt) = 32))),
    CONSTRAINT reports_sha256_check CHECK ((sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT reports_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'final'::text])))
);


--
-- Name: wallets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wallets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid DEFAULT auth.uid() NOT NULL,
    chain text NOT NULL,
    address text NOT NULL,
    label text,
    verified boolean DEFAULT false NOT NULL,
    last_synced_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT wallet_address_format CHECK ((((chain = 'solana'::text) AND (address ~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$'::text)) OR ((chain = 'hyperliquid'::text) AND (address ~ '^0x[0-9a-fA-F]{40}$'::text)))),
    CONSTRAINT wallets_chain_check CHECK ((chain = ANY (ARRAY['solana'::text, 'hyperliquid'::text]))),
    CONSTRAINT wallets_label_check CHECK ((char_length(label) <= 40))
);


--
-- Name: assets assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_pkey PRIMARY KEY (chain, asset);


--
-- Name: event_reviews event_reviews_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_reviews
    ADD CONSTRAINT event_reviews_pkey PRIMARY KEY (id);


--
-- Name: events events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_pkey PRIMARY KEY (id);


--
-- Name: events events_wallet_id_tx_hash_event_index_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_wallet_id_tx_hash_event_index_key UNIQUE (wallet_id, tx_hash, event_index);


--
-- Name: fx_rates fx_rates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fx_rates
    ADD CONSTRAINT fx_rates_pkey PRIMARY KEY (date);


--
-- Name: ingestion_runs ingestion_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ingestion_runs
    ADD CONSTRAINT ingestion_runs_pkey PRIMARY KEY (id);


--
-- Name: prices_daily_v1 prices_daily_v1_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prices_daily_v1
    ADD CONSTRAINT prices_daily_v1_pkey PRIMARY KEY (asset, date);


--
-- Name: prices prices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prices
    ADD CONSTRAINT prices_pkey PRIMARY KEY (chain, asset, ts);


--
-- Name: profiles profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_pkey PRIMARY KEY (id);


--
-- Name: profiles profiles_primary_wallet_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_primary_wallet_key UNIQUE (primary_wallet);


--
-- Name: report_versions report_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_versions
    ADD CONSTRAINT report_versions_pkey PRIMARY KEY (id);


--
-- Name: report_versions report_versions_r2_object_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_versions
    ADD CONSTRAINT report_versions_r2_object_key_key UNIQUE (r2_object_key);


--
-- Name: report_versions report_versions_report_id_revision_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_versions
    ADD CONSTRAINT report_versions_report_id_revision_key UNIQUE (report_id, revision);


--
-- Name: reports reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reports
    ADD CONSTRAINT reports_pkey PRIMARY KEY (id);


--
-- Name: reports reports_user_id_month_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reports
    ADD CONSTRAINT reports_user_id_month_key UNIQUE (user_id, month);


--
-- Name: wallets wallets_id_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallets
    ADD CONSTRAINT wallets_id_user_id_key UNIQUE (id, user_id);


--
-- Name: wallets wallets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallets
    ADD CONSTRAINT wallets_pkey PRIMARY KEY (id);


--
-- Name: wallets wallets_user_id_chain_address_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallets
    ADD CONSTRAINT wallets_user_id_chain_address_key UNIQUE (user_id, chain, address);


--
-- Name: assets_coingecko_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX assets_coingecko_idx ON public.assets USING btree (coingecko_id) WHERE (coingecko_id IS NOT NULL);


--
-- Name: event_reviews_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX event_reviews_user_idx ON public.event_reviews USING btree (user_id, created_at DESC);


--
-- Name: events_identity_owner_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX events_identity_owner_uq ON public.events USING btree (id, user_id);


--
-- Name: events_operation_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX events_operation_idx ON public.events USING btree (wallet_id, operation_id) WHERE (operation_id IS NOT NULL);


--
-- Name: events_pending_review_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX events_pending_review_idx ON public.events USING btree (user_id, ts) WHERE (review_status = 'pending'::text);


--
-- Name: events_unpriced_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX events_unpriced_idx ON public.events USING btree (asset, ts) WHERE (usd_price IS NULL);


--
-- Name: events_user_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX events_user_ts_idx ON public.events USING btree (user_id, ts);


--
-- Name: events_wallet_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX events_wallet_idx ON public.events USING btree (wallet_id);


--
-- Name: ingestion_runs_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ingestion_runs_user_idx ON public.ingestion_runs USING btree (user_id, started_at DESC);


--
-- Name: report_versions_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX report_versions_user_idx ON public.report_versions USING btree (user_id, created_at DESC);


--
-- Name: reports_identity_owner_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX reports_identity_owner_uq ON public.reports USING btree (id, user_id);


--
-- Name: reports_user_month_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX reports_user_month_idx ON public.reports USING btree (user_id, month DESC);


--
-- Name: wallets_hyperliquid_casefold_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX wallets_hyperliquid_casefold_uq ON public.wallets USING btree (user_id, lower(address)) WHERE (chain = 'hyperliquid'::text);


--
-- Name: wallets_identity_owner_chain_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX wallets_identity_owner_chain_uq ON public.wallets USING btree (id, user_id, chain);


--
-- Name: wallets_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX wallets_user_idx ON public.wallets USING btree (user_id);


--
-- Name: events orbix_event_identity; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER orbix_event_identity BEFORE UPDATE ON public.events FOR EACH ROW EXECUTE FUNCTION public.orbix_guard_identity('id', 'wallet_id', 'user_id', 'chain', 'tx_hash', 'event_index');


--
-- Name: events orbix_event_review_audit; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER orbix_event_review_audit AFTER UPDATE ON public.events FOR EACH ROW EXECUTE FUNCTION public.orbix_log_event_review();


--
-- Name: reports orbix_report_identity; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER orbix_report_identity BEFORE UPDATE ON public.reports FOR EACH ROW EXECUTE FUNCTION public.orbix_guard_identity('id', 'user_id', 'month');


--
-- Name: report_versions orbix_report_revision; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER orbix_report_revision BEFORE INSERT ON public.report_versions FOR EACH ROW EXECUTE FUNCTION public.orbix_assign_report_revision();


--
-- Name: report_versions orbix_report_version_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER orbix_report_version_immutable BEFORE UPDATE ON public.report_versions FOR EACH ROW EXECUTE FUNCTION public.orbix_reject_version_update();


--
-- Name: reports orbix_reports_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER orbix_reports_updated_at BEFORE UPDATE ON public.reports FOR EACH ROW EXECUTE FUNCTION public.orbix_touch_report();


--
-- Name: wallets orbix_wallet_identity; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER orbix_wallet_identity BEFORE UPDATE ON public.wallets FOR EACH ROW EXECUTE FUNCTION public.orbix_guard_identity('id', 'user_id', 'chain', 'address');


--
-- Name: event_reviews event_reviews_actor_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_reviews
    ADD CONSTRAINT event_reviews_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES auth.users(id) ON DELETE SET NULL;


--
-- Name: event_reviews event_reviews_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_reviews
    ADD CONSTRAINT event_reviews_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE CASCADE;


--
-- Name: event_reviews event_reviews_owner_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_reviews
    ADD CONSTRAINT event_reviews_owner_fk FOREIGN KEY (event_id, user_id) REFERENCES public.events(id, user_id) ON DELETE CASCADE;


--
-- Name: event_reviews event_reviews_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_reviews
    ADD CONSTRAINT event_reviews_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: events events_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: events events_wallet_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_wallet_id_fkey FOREIGN KEY (wallet_id) REFERENCES public.wallets(id) ON DELETE CASCADE;


--
-- Name: events events_wallet_owner_chain_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_wallet_owner_chain_fk FOREIGN KEY (wallet_id, user_id, chain) REFERENCES public.wallets(id, user_id, chain) ON DELETE CASCADE;


--
-- Name: events events_wallet_owner_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_wallet_owner_fkey FOREIGN KEY (wallet_id, user_id) REFERENCES public.wallets(id, user_id) ON DELETE CASCADE;


--
-- Name: ingestion_runs ingestion_runs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ingestion_runs
    ADD CONSTRAINT ingestion_runs_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: ingestion_runs ingestion_runs_wallet_id_user_id_chain_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ingestion_runs
    ADD CONSTRAINT ingestion_runs_wallet_id_user_id_chain_fkey FOREIGN KEY (wallet_id, user_id, chain) REFERENCES public.wallets(id, user_id, chain) ON DELETE CASCADE;


--
-- Name: profiles profiles_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_id_fkey FOREIGN KEY (id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: report_versions report_versions_report_id_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_versions
    ADD CONSTRAINT report_versions_report_id_user_id_fkey FOREIGN KEY (report_id, user_id) REFERENCES public.reports(id, user_id) ON DELETE CASCADE;


--
-- Name: report_versions report_versions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_versions
    ADD CONSTRAINT report_versions_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: reports reports_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reports
    ADD CONSTRAINT reports_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: wallets wallets_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallets
    ADD CONSTRAINT wallets_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: assets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.assets ENABLE ROW LEVEL SECURITY;

--
-- Name: event_reviews; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.event_reviews ENABLE ROW LEVEL SECURITY;

--
-- Name: events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.events ENABLE ROW LEVEL SECURITY;

--
-- Name: fx_rates; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.fx_rates ENABLE ROW LEVEL SECURITY;

--
-- Name: ingestion_runs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.ingestion_runs ENABLE ROW LEVEL SECURITY;

--
-- Name: profiles orbix_profile_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY orbix_profile_update ON public.profiles FOR UPDATE TO authenticated USING ((id = ( SELECT auth.uid() AS uid))) WITH CHECK ((id = ( SELECT auth.uid() AS uid)));


--
-- Name: assets orbix_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY orbix_read ON public.assets FOR SELECT TO authenticated USING (true);


--
-- Name: event_reviews orbix_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY orbix_read ON public.event_reviews FOR SELECT TO authenticated USING ((user_id = ( SELECT auth.uid() AS uid)));


--
-- Name: events orbix_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY orbix_read ON public.events FOR SELECT TO authenticated USING ((user_id = ( SELECT auth.uid() AS uid)));


--
-- Name: fx_rates orbix_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY orbix_read ON public.fx_rates FOR SELECT TO authenticated USING (true);


--
-- Name: ingestion_runs orbix_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY orbix_read ON public.ingestion_runs FOR SELECT TO authenticated USING ((user_id = ( SELECT auth.uid() AS uid)));


--
-- Name: prices orbix_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY orbix_read ON public.prices FOR SELECT TO authenticated USING (true);


--
-- Name: profiles orbix_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY orbix_read ON public.profiles FOR SELECT TO authenticated USING ((id = ( SELECT auth.uid() AS uid)));


--
-- Name: report_versions orbix_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY orbix_read ON public.report_versions FOR SELECT TO authenticated USING ((user_id = ( SELECT auth.uid() AS uid)));


--
-- Name: reports orbix_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY orbix_read ON public.reports FOR SELECT TO authenticated USING ((user_id = ( SELECT auth.uid() AS uid)));


--
-- Name: wallets orbix_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY orbix_read ON public.wallets FOR SELECT TO authenticated USING ((user_id = ( SELECT auth.uid() AS uid)));


--
-- Name: wallets orbix_wallet_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY orbix_wallet_insert ON public.wallets FOR INSERT TO authenticated WITH CHECK ((user_id = ( SELECT auth.uid() AS uid)));


--
-- Name: wallets orbix_wallet_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY orbix_wallet_update ON public.wallets FOR UPDATE TO authenticated USING ((user_id = ( SELECT auth.uid() AS uid))) WITH CHECK ((user_id = ( SELECT auth.uid() AS uid)));


--
-- Name: prices; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.prices ENABLE ROW LEVEL SECURITY;

--
-- Name: prices_daily_v1; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.prices_daily_v1 ENABLE ROW LEVEL SECURITY;

--
-- Name: profiles; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

--
-- Name: report_versions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.report_versions ENABLE ROW LEVEL SECURITY;

--
-- Name: reports; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.reports ENABLE ROW LEVEL SECURITY;

--
-- Name: wallets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.wallets ENABLE ROW LEVEL SECURITY;

--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA public TO postgres;
GRANT USAGE ON SCHEMA public TO anon;
GRANT USAGE ON SCHEMA public TO authenticated;
GRANT USAGE ON SCHEMA public TO service_role;
GRANT USAGE ON SCHEMA public TO orbix_worker;


--
-- Name: FUNCTION handle_new_user(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.handle_new_user() FROM PUBLIC;
GRANT ALL ON FUNCTION public.handle_new_user() TO service_role;


--
-- Name: FUNCTION orbix_assign_report_revision(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.orbix_assign_report_revision() FROM PUBLIC;
GRANT ALL ON FUNCTION public.orbix_assign_report_revision() TO service_role;


--
-- Name: FUNCTION orbix_guard_identity(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.orbix_guard_identity() FROM PUBLIC;
GRANT ALL ON FUNCTION public.orbix_guard_identity() TO service_role;


--
-- Name: FUNCTION orbix_log_event_review(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.orbix_log_event_review() FROM PUBLIC;
GRANT ALL ON FUNCTION public.orbix_log_event_review() TO service_role;


--
-- Name: FUNCTION orbix_reject_version_update(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.orbix_reject_version_update() FROM PUBLIC;
GRANT ALL ON FUNCTION public.orbix_reject_version_update() TO service_role;


--
-- Name: FUNCTION orbix_touch_report(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.orbix_touch_report() FROM PUBLIC;
GRANT ALL ON FUNCTION public.orbix_touch_report() TO service_role;


--
-- Name: TABLE assets; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,UPDATE ON TABLE public.assets TO orbix_worker;
GRANT SELECT ON TABLE public.assets TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assets TO service_role;


--
-- Name: TABLE event_reviews; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.event_reviews TO authenticated;
GRANT SELECT ON TABLE public.event_reviews TO service_role;


--
-- Name: TABLE events; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,UPDATE ON TABLE public.events TO orbix_worker;
GRANT SELECT ON TABLE public.events TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.events TO service_role;


--
-- Name: TABLE fx_rates; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,UPDATE ON TABLE public.fx_rates TO orbix_worker;
GRANT SELECT ON TABLE public.fx_rates TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.fx_rates TO service_role;


--
-- Name: TABLE ingestion_runs; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.ingestion_runs TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.ingestion_runs TO service_role;


--
-- Name: TABLE monthly_activity_summary; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.monthly_activity_summary TO service_role;
GRANT SELECT ON TABLE public.monthly_activity_summary TO authenticated;


--
-- Name: TABLE monthly_summary; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.monthly_summary TO service_role;


--
-- Name: TABLE prices; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,UPDATE ON TABLE public.prices TO orbix_worker;
GRANT SELECT ON TABLE public.prices TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.prices TO service_role;


--
-- Name: TABLE prices_daily_v1; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.prices_daily_v1 TO service_role;


--
-- Name: TABLE profiles; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.profiles TO orbix_worker;
GRANT SELECT ON TABLE public.profiles TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.profiles TO service_role;


--
-- Name: COLUMN profiles.display_name; Type: ACL; Schema: public; Owner: -
--

GRANT UPDATE(display_name) ON TABLE public.profiles TO authenticated;


--
-- Name: TABLE report_versions; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.report_versions TO authenticated;
GRANT SELECT,INSERT ON TABLE public.report_versions TO service_role;


--
-- Name: TABLE reports; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,UPDATE ON TABLE public.reports TO orbix_worker;
GRANT SELECT ON TABLE public.reports TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.reports TO service_role;


--
-- Name: TABLE wallets; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.wallets TO orbix_worker;
GRANT SELECT ON TABLE public.wallets TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.wallets TO service_role;


--
-- Name: COLUMN wallets.chain; Type: ACL; Schema: public; Owner: -
--

GRANT INSERT(chain) ON TABLE public.wallets TO authenticated;


--
-- Name: COLUMN wallets.address; Type: ACL; Schema: public; Owner: -
--

GRANT INSERT(address) ON TABLE public.wallets TO authenticated;


--
-- Name: COLUMN wallets.label; Type: ACL; Schema: public; Owner: -
--

GRANT INSERT(label),UPDATE(label) ON TABLE public.wallets TO authenticated;


--
-- Name: COLUMN wallets.verified; Type: ACL; Schema: public; Owner: -
--

GRANT UPDATE(verified) ON TABLE public.wallets TO orbix_worker;


--
-- Name: COLUMN wallets.last_synced_at; Type: ACL; Schema: public; Owner: -
--

GRANT UPDATE(last_synced_at) ON TABLE public.wallets TO orbix_worker;


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON SEQUENCES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON SEQUENCES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON SEQUENCES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON SEQUENCES TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR FUNCTIONS; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR FUNCTIONS; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON FUNCTIONS TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON FUNCTIONS TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON FUNCTIONS TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO service_role;


--
-- PostgreSQL database dump complete
--

\unrestrict BbZBjFFQbIgQhvrgX5tZ58RdZXFRCRchhzxgH3eY2pKUPmIbhrpxZjeBdT4CHoE

