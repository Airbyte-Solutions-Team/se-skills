-- 001_initial_schema.sql
-- Roles, organizations, memberships, users, accounts, opportunities,
-- RLS policies, and DB-enforced same-org_id parent/child relationships.

-- Application roles. Passwords are injected by the migration runner and are
-- never committed. app_admin is used for migrations and membership resolution;
-- app_user is the least-privileged role used by normal requests and is subject
-- to row-level security.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'app_admin') THEN
        CREATE ROLE app_admin WITH LOGIN PASSWORD '__APP_ADMIN_PASSWORD__' BYPASSRLS;
    ELSE
        ALTER ROLE app_admin WITH LOGIN PASSWORD '__APP_ADMIN_PASSWORD__' BYPASSRLS;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user WITH LOGIN PASSWORD '__APP_USER_PASSWORD__' NOBYPASSRLS;
    ELSE
        ALTER ROLE app_user WITH LOGIN PASSWORD '__APP_USER_PASSWORD__' NOBYPASSRLS;
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
    UNIQUE (org_id, slug),
    FOREIGN KEY (account_id, org_id) REFERENCES public.accounts(id, org_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_opportunities_org_id ON public.opportunities(org_id);
CREATE INDEX IF NOT EXISTS idx_opportunities_account_id ON public.opportunities(account_id, org_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_opportunities_id_org ON public.opportunities(id, org_id);

-- Grants for the least-privileged application role.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;
GRANT ALL ON ALL TABLES IN SCHEMA public TO app_admin;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO app_admin;

-- Default privileges so future tables in public are automatically usable.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO app_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO app_admin;

-- Row-level security. All tenant-scoped tables use a transaction-scoped
-- application variable (app.current_org_id) set by the API for each request.
ALTER TABLE public.organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.opportunities ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_tenant_organizations ON public.organizations;
CREATE POLICY org_tenant_organizations ON public.organizations
    FOR ALL TO app_user
    USING (id = current_setting('app.current_org_id', true)::uuid);

DROP POLICY IF EXISTS org_tenant_memberships ON public.memberships;
CREATE POLICY org_tenant_memberships ON public.memberships
    FOR ALL TO app_user
    USING (org_id = current_setting('app.current_org_id', true)::uuid);

DROP POLICY IF EXISTS org_tenant_accounts ON public.accounts;
CREATE POLICY org_tenant_accounts ON public.accounts
    FOR ALL TO app_user
    USING (org_id = current_setting('app.current_org_id', true)::uuid);

DROP POLICY IF EXISTS org_tenant_opportunities ON public.opportunities;
CREATE POLICY org_tenant_opportunities ON public.opportunities
    FOR ALL TO app_user
    USING (org_id = current_setting('app.current_org_id', true)::uuid);

DROP POLICY IF EXISTS org_tenant_users ON public.users;
CREATE POLICY org_tenant_users ON public.users
    FOR ALL TO app_user
    USING (
        id = current_setting('app.current_user_id', true)::uuid
        OR EXISTS (
            SELECT 1 FROM public.memberships m
            WHERE m.user_id = public.users.id
              AND m.org_id = current_setting('app.current_org_id', true)::uuid
        )
    );
