import os, json, traceback, logging, time
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["APP_ENV"] = "production"

# Silence ALL logging during the run
logging.disable(logging.CRITICAL)
# Also silence structlog
try:
    import structlog
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL),
    )
except Exception:
    pass

t0 = time.time()
from app.core.config import get_settings
from app.services.ingestion.iea_policy_tracker import ingest_policy_tracker
from sqlalchemy import text, create_engine
from sqlalchemy.orm import sessionmaker

settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True, echo=False)
Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

out = {"phase": "start"}
try:
    session = Session()
    try:
        t_ingest_start = time.time()
        result = ingest_policy_tracker(session, xls_path="data/iea/policy_tracker.csv")
        out["ingest_seconds"] = round(time.time() - t_ingest_start, 1)
        out["result"] = {k: v for k, v in result.items()}

        review_count = session.execute(text(
            "SELECT COUNT(*) FROM risk_events WHERE metadata_json->>'needs_material_review' = 'true'"
        )).scalar()
        out["review_count"] = review_count

        rows = session.execute(text(
            """SELECT id, title, countries, metadata_json->>'review_reason' AS rr
               FROM risk_events
               WHERE metadata_json->>'needs_material_review' = 'true'
               LIMIT 5"""
        )).fetchall()
        out["samples"] = [
            {"id": r[0], "title": (r[1] or "")[:120], "countries": str(r[2]), "rr": r[3]}
            for r in rows
        ]
        out["sample_material_counts"] = []
        for r in rows:
            c = session.execute(text(
                "SELECT COUNT(*) FROM risk_event_materials WHERE risk_event_id = :rid"
            ), {"rid": r[0]}).scalar()
            out["sample_material_counts"].append({"id": r[0], "rem_count": c})

        agg = session.execute(text(
            """SELECT COUNT(*) FROM risk_event_materials rem
               JOIN risk_events re ON re.id = rem.risk_event_id
               WHERE re.metadata_json->>'needs_material_review' = 'true'"""
        )).scalar()
        out["agg_rem_for_review"] = agg
    finally:
        session.rollback()
        session.close()
    out["phase"] = "ok"
    out["total_seconds"] = round(time.time() - t0, 1)
except Exception as e:
    out["phase"] = "error"
    out["error"] = repr(e)
    out["tb"] = traceback.format_exc()

with open("/tmp/_verify_run.json", "w") as f:
    json.dump(out, f, default=str, indent=2)
print("DONE")
