"""Upsert a finished X3 DSC export into ``polymer_dev.runs``.

The sila-exporter queue poll owns live ``segment`` and ``remaining_seconds``.
This write fills sample, operator, project, outcome, and ``finished_at`` and
leaves those progress columns alone when they are already set.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

_DEFAULT_INSTRUMENT_ID = "x3dsc-01"


def store_run(
    *,
    run_id: str,
    sample_id: str = "",
    operator: str = "",
    project: str = "",
    database_url: str | None = None,
) -> None:
    """Insert or update the catalog row for *run_id*.

    Missing ``LAB_DATABASE_URL``, a missing ``psycopg`` install, or an empty
    *run_id* skips the write. A configured database that errors raises.
    """
    run_id = (run_id or "").strip()
    if not run_id:
        _log.info("runs store skipped: run_id empty")
        return
    dsn = (database_url or os.environ.get("LAB_DATABASE_URL") or "").strip()
    if not dsn:
        _log.info("runs store skipped: LAB_DATABASE_URL unset")
        return
    try:
        import psycopg
    except ImportError:
        _log.info("runs store skipped: psycopg not installed")
        return

    sample_id = (sample_id or "").strip()
    instrument_id = (
        os.environ.get("LAB_INSTRUMENT_ID") or _DEFAULT_INSTRUMENT_ID
    ).strip() or _DEFAULT_INSTRUMENT_ID
    recorded_at = datetime.now(timezone.utc)
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                if sample_id:
                    cur.execute(
                        """
                        INSERT INTO polymer_dev.samples (sampleid, samplename)
                        VALUES (%(sampleid)s, %(samplename)s)
                        ON CONFLICT (sampleid) DO NOTHING
                        """,
                        {"sampleid": sample_id, "samplename": sample_id},
                    )
                cur.execute(
                    """
                    INSERT INTO polymer_dev.runs (
                        run_id, instrument_id, instrument_kind, sample_id,
                        operator, project, started_at, finished_at, outcome
                    ) VALUES (
                        %(run_id)s, %(instrument_id)s, %(instrument_kind)s,
                        %(sample_id)s, %(operator)s, %(project)s,
                        %(started_at)s, %(finished_at)s, %(outcome)s
                    )
                    ON CONFLICT (run_id, instrument_id) DO UPDATE SET
                        sample_id = COALESCE(EXCLUDED.sample_id, runs.sample_id),
                        operator = COALESCE(EXCLUDED.operator, runs.operator),
                        project = COALESCE(EXCLUDED.project, runs.project),
                        finished_at = COALESCE(EXCLUDED.finished_at, runs.finished_at),
                        outcome = COALESCE(EXCLUDED.outcome, runs.outcome)
                    """,
                    {
                        "run_id": run_id,
                        "instrument_id": instrument_id,
                        "instrument_kind": "x3dsc",
                        "sample_id": sample_id or None,
                        "operator": (operator or "").strip() or None,
                        "project": (project or "").strip() or None,
                        "started_at": recorded_at,
                        "finished_at": recorded_at,
                        "outcome": "ok",
                    },
                )
    _log.info(
        "Stored polymer_dev.runs run_id=%s instrument_id=%s sample_id=%s",
        run_id,
        instrument_id,
        sample_id,
    )
