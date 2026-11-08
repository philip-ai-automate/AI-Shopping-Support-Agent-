"""Account kind (8 Nov 2026, item 11 Phase 1): every business is one of
  real  — paying or trial customers, and PhiXtra's own business
  demo  — made-up businesses prospects see or staff train on (stay in production)
  test  — businesses used to try features (move to staging, item 11 Phase 3–5)
ONE rule for counting: only 'real' appears in admin counts, the Customers
list (by default), plans counts, inactivity and come-back emails, and Who's
Online. is_demo / is_training stay as they were (banners, email blocking)."""

KINDS = [("real", "Real"), ("demo", "Sales demo"), ("test", "Test")]
LABELS = dict(KINDS)


def real_sql(alias: str = "t") -> str:
    """SQL condition: the tenant row `alias` is a real business."""
    return f"{alias}.account_kind = 'real'"


def is_real_tenant_sql(tenant_expr: str) -> str:
    """SQL condition for a tenant id expression (no tenants join needed)."""
    return f"EXISTS (SELECT 1 FROM tenants _ak WHERE _ak.id = {tenant_expr} AND _ak.account_kind = 'real')"


def schema(cur):
    """Column + insert trigger. Called from portal_migrations."""
    cur.execute("""SELECT 1 FROM information_schema.columns
                    WHERE table_name='tenants' AND column_name='account_kind'""")
    if not cur.fetchone():
        cur.execute("""ALTER TABLE tenants ADD COLUMN account_kind TEXT NOT NULL DEFAULT 'real'
                       CHECK (account_kind IN ('real','demo','test'))""")
        cur.execute("UPDATE tenants SET account_kind='demo' WHERE is_demo OR is_training")
    # New businesses made by the demo / training / presale scripts arrive with
    # is_demo or is_training set → label them demo automatically.
    cur.execute("""CREATE OR REPLACE FUNCTION tenants_account_kind_default() RETURNS trigger AS $$
                   BEGIN
                     IF NEW.account_kind = 'real' AND (COALESCE(NEW.is_demo,FALSE) OR COALESCE(NEW.is_training,FALSE)) THEN
                       NEW.account_kind := 'demo';
                     END IF;
                     RETURN NEW;
                   END $$ LANGUAGE plpgsql""")
    cur.execute("DROP TRIGGER IF EXISTS tenants_account_kind_default ON tenants")
    cur.execute("""CREATE TRIGGER tenants_account_kind_default BEFORE INSERT ON tenants
                   FOR EACH ROW EXECUTE FUNCTION tenants_account_kind_default()""")
