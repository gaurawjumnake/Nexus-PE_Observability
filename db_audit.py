"""
DB Audit Script — run from project root:
    python db_audit.py [company_id] [period]

Defaults: company_id=fluke, period=2023
"""
import sys
import json
import os
from dotenv import load_dotenv
load_dotenv()

import psycopg2
from psycopg2.extras import RealDictCursor

def connect():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST"),
        port=int(os.getenv("POSTGRES_PORT", 5432)),
        dbname=os.getenv("POSTGRES_DB", "postgres"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PWD", ""),
        sslmode="require",
    )

company_id = (sys.argv[1] if len(sys.argv) > 1 else "fluke").strip().lower()
period     = sys.argv[2] if len(sys.argv) > 2 else "2023"

print(f"\n=== DB Audit  company={company_id!r}  period={period!r} ===\n")

with connect() as conn:
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # ---- 1. Tables present ----
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='public' ORDER BY table_name
    """)
    tables = [r["table_name"] for r in cur.fetchall()]
    print("Tables in DB:", tables)

    # ---- 2. nexus_facts ----
    cur.execute("""
        SELECT fact_id, company_id, period, value, confidence, source_type
        FROM nexus_facts
        WHERE company_id = %s AND period = %s
        ORDER BY fact_id
    """, (company_id, period))
    facts = cur.fetchall()
    print(f"\nnexus_facts  ({len(facts)} rows for company={company_id!r} period={period!r}):")
    for r in facts[:20]:
        raw = r["value"]
        try:
            decoded = json.loads(raw)
        except Exception:
            decoded = raw
        null_flag = " <-- NULL" if decoded is None else ""
        print(f"  {r['fact_id']:40s}  value={decoded!r}{null_flag}  conf={r['confidence']}  src={r['source_type']}")
    if len(facts) > 20:
        print(f"  ... and {len(facts)-20} more rows")

    null_facts = [r for r in facts if r["value"] in (None, "null", '""')]
    if null_facts:
        print(f"\n  WARNING: {len(null_facts)} rows with null/empty value:")
        for r in null_facts:
            print(f"    {r['fact_id']}  raw_value={r['value']!r}")

    # ---- 3. nexus_fact_observations ----
    cur.execute("""
        SELECT fact_id, company_id, observation_date, value, confidence
        FROM nexus_fact_observations
        WHERE company_id = %s
        ORDER BY fact_id, observation_date
        LIMIT 30
    """, (company_id,))
    obs = cur.fetchall()
    print(f"\nnexus_fact_observations  (first 30 rows for company={company_id!r}):")
    if obs:
        for r in obs:
            raw = r["value"]
            try:
                decoded = json.loads(raw)
            except Exception:
                decoded = raw
            null_flag = " <-- NULL" if decoded is None else ""
            print(f"  {r['fact_id']:40s}  date={r['observation_date']}  value={decoded!r}{null_flag}")
    else:
        print("  (no rows)")

    # ---- 4. Distinct observed facts ----
    cur.execute("""
        SELECT fact_id, COUNT(*) as cnt, MIN(observation_date) as min_d, MAX(observation_date) as max_d
        FROM nexus_fact_observations WHERE company_id=%s
        GROUP BY fact_id ORDER BY fact_id
    """, (company_id,))
    obs_summary = cur.fetchall()
    print(f"\nnexus_fact_observations summary ({len(obs_summary)} distinct facts):")
    for r in obs_summary:
        print(f"  {r['fact_id']:40s}  rows={r['cnt']}  date_range=[{r['min_d']} .. {r['max_d']}]")

    # ---- 5. nexus_kpis ----
    cur.execute("""
        SELECT kpi_id, company_id, period, value, coverage, status
        FROM nexus_kpis
        WHERE company_id = %s AND period = %s
        ORDER BY kpi_id
    """, (company_id, period))
    kpis = cur.fetchall()
    print(f"\nnexus_kpis  ({len(kpis)} rows for company={company_id!r} period={period!r}):")
    for r in kpis:
        null_flag = " <-- NULL value" if r["value"] is None else ""
        print(f"  {r['kpi_id']:40s}  value={r['value']}  coverage={r['coverage']}  status={r['status']}{null_flag}")

    # ---- 6. All distinct company_ids stored ----
    cur.execute("SELECT DISTINCT company_id FROM nexus_facts ORDER BY company_id")
    cids_facts = [r["company_id"] for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT company_id FROM nexus_fact_observations ORDER BY company_id")
    cids_obs = [r["company_id"] for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT company_id FROM nexus_kpis ORDER BY company_id")
    cids_kpis = [r["company_id"] for r in cur.fetchall()]
    print(f"\nDistinct company_ids  facts={cids_facts}  observations={cids_obs}  kpis={cids_kpis}")

    # ---- 7. All distinct periods for the company ----
    cur.execute("SELECT DISTINCT period FROM nexus_facts WHERE company_id=%s ORDER BY period", (company_id,))
    periods_facts = [r["period"] for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT period FROM nexus_kpis WHERE company_id=%s ORDER BY period", (company_id,))
    periods_kpis = [r["period"] for r in cur.fetchall()]
    print(f"Distinct periods      facts={periods_facts}  kpis={periods_kpis}")

print("\n=== Done ===\n")
