-- 001_initial_schema.sql
-- Roles, organizations, memberships, users, accounts, opportunities,
-- resolver functions, RLS policies, and DB-enforced same-org_id parent/child relationships.

-- Application roles. Passwords are passed as transaction-local GUCs by the
-- migration runner and are never interpolated into SQL. app_admin is used for
-- migrations only and owns the narrow resolver functions; app_user is the
-- least-privileged role used by normal requests and is subject to RLS.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'app_admin') THEN
        EXECUTE format('CREATE ROLE app_admin WITH LOGIN PASSWORD %L BYPASSRLS', current_setting('migration.app_admin_password'));
    ELSE
        EXECUTE format('ALTER ROLE app_admin WITH LOGIN PASSWORD %L BYPASSRLS', current_setting('migration.app_admin_password'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'app_user') THEN
        EXECUTE format('CREATE ROLE app_user WITH LOGIN PASSWORD %L NOBYPASSRLS', current_setting('migration.app_user_password'));
    ELSE
        EXECUTE format('ALTER ROLE app_user WITH LOGIN PASSWORD %L NOBYPASSRLS', current_setting('migration.app_user_password'));
    END IF;
END $$;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO app_user, app_admin', current_database());
END $$;

GRANT USAGE, CREATE ON SCHEMA public TO app_admin;
GRANT USAGE ON SCHEMA public TO app_user;

-- Users are global identifiers (a person can belong to multiple organizations
-- through memberships). The app upserts a public.users row on sign-in.
CREATE TABLE IF NOT EXISTS public.users (
    id UUID PRIMARY KEY,
    email TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Organizations: the tenancy boundary.
CREATE TABLE IF NOT EXISTS public.organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Memberships tie users to organizations. active controls whether the
-- membership can be used to resolve an organization context.
CREATE TABLE IF NOT EXISTS public.memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'member',
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_memberships_user_id ON public.memberships(user_id);
CREATE INDEX IF NOT EXISTS idx_memberships_org_id ON public.memberships(org_id);

-- Accounts belong to an organization.
CREATE TABLE IF NOT EXISTS public.accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    created_by UUID REFERENCES public.users(id) ON DELETE SET NULL,
    assigned_to UUID REFERENCES public.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_accounts_org_id ON public.accounts(org_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_id_org ON public.accounts(id, org_id);

-- Opportunities belong to an organization and an account in the same
-- organization. The composite FK (account_id, org_id) enforces that an
-- opportunity cannot reference an account from a different organization.
-- Slugs are unique per account (which also implies per-org because account is
-- per-org).
CREATE TABLE IF NOT EXISTS public.opportunities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    account_id UUID NOT NULL,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    stage TEXT,
    close_date DATE,
    amount INTEGER,
    created_by UUID REFERENCES public.users(id) ON DELETE SET NULL,
    assigned_to UUID REFERENCES public.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, slug),
    FOREIGN KEY (account_id, org_id) REFERENCES public.accounts(id, org_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_opportunities_org_id ON public.opportunities(org_id);
CREATE INDEX IF NOT EXISTS idx_opportunities_account_id ON public.opportunities(account_id, org_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_opportunities_id_org ON public.opportunities(id, org_id);

-- Narrow resolver functions. They are SECURITY DEFINER and owned by app_admin
-- so the web process (which connects as app_user) can resolve membership
-- without receiving broad table privileges or BYPASSRLS.
CREATE OR REPLACE FUNCTION public.resolve_active_membership()
RETURNS TABLE(membership_id UUID, org_id UUID, role TEXT)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT m.id, m.org_id, m.role
    FROM public.memberships m
    WHERE m.user_id = current_setting('app.current_user_id', true)::UUID
      AND m.active = true
    ORDER BY m.created_at
    LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION public.is_active_org_member(p_user_id UUID, p_org_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.memberships m
        WHERE m.user_id = p_user_id
          AND m.org_id = p_org_id
          AND m.active = true
    );
$$;

ALTER FUNCTION public.resolve_active_membership() OWNER TO app_admin;
ALTER FUNCTION public.is_active_org_member(UUID, UUID) OWNER TO app_admin;

-- Least-privilege grants for the application role.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.accounts, public.opportunities TO app_user;
GRANT SELECT ON TABLE public.users, public.organizations, public.memberships TO app_user;
GRANT REFERENCES ON TABLE public.users, public.organizations, public.accounts TO app_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;
GRANT EXECUTE ON FUNCTION public.resolve_active_membership() TO app_user;
GRANT EXECUTE ON FUNCTION public.is_active_org_member(UUID, UUID) TO app_user;

-- app_admin retains full schema access for migrations.
GRANT ALL ON ALL TABLES IN SCHEMA public TO app_admin;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO app_admin;

-- Default privileges only for app_admin; app_user must receive explicit
-- grants for any future table/sequence/function.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO app_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO app_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO app_admin;

-- Row-level security. Tenant access is bound to the verified user plus an
-- active membership. The context is set per-request by the API using
-- transaction-scoped GUCs (app.current_org_id and app.current_user_id).
ALTER TABLE public.organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.opportunities ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_tenant_organizations ON public.organizations;
CREATE POLICY org_tenant_organizations ON public.organizations
    FOR SELECT TO app_user
    USING (
        id = current_setting('app.current_org_id', true)::UUID
        AND public.is_active_org_member(current_setting('app.current_user_id', true)::UUID, id)
    );

DROP POLICY IF EXISTS org_tenant_memberships ON public.memberships;
CREATE POLICY org_tenant_memberships ON public.memberships
    FOR SELECT TO app_user
    USING (
        user_id = current_setting('app.current_user_id', true)::UUID
        AND org_id = current_setting('app.current_org_id', true)::UUID
        AND active = true
    );

DROP POLICY IF EXISTS org_tenant_accounts ON public.accounts;
CREATE POLICY org_tenant_accounts ON public.accounts
    FOR ALL TO app_user
    USING (
        org_id = current_setting('app.current_org_id', true)::UUID
        AND public.is_active_org_member(current_setting('app.current_user_id', true)::UUID, org_id)
    );

DROP POLICY IF EXISTS org_tenant_opportunities ON public.opportunities;
CREATE POLICY org_tenant_opportunities ON public.opportunities
    FOR ALL TO app_user
    USING (
        org_id = current_setting('app.current_org_id', true)::UUID
        AND public.is_active_org_member(current_setting('app.current_user_id', true)::UUID, org_id)
    );

DROP POLICY IF EXISTS org_tenant_users ON public.users;
CREATE POLICY org_tenant_users ON public.users
    FOR SELECT TO app_user
    USING (id = current_setting('app.current_user_id', true)::UUID);
