"""Synthetic local index benchmark; does NOT create 500k Google Drive files."""

import argparse
import hashlib
import json
import random
import resource
import statistics
import tempfile
import time
import uuid
from pathlib import Path

from papervault.core import State
from papervault.index import Index

p = argparse.ArgumentParser()
p.add_argument("--count", type=int, default=500000)
p.add_argument("--output", default="BENCHMARK.json")
a = p.parse_args()

with tempfile.TemporaryDirectory(prefix="pv-index-bench-") as td:
    state = State(td)
    index = Index(state)
    start = time.perf_counter()
    for i in range(a.count):
        doi = f"10.9999/benchmark.{i}"
        title = f"Archival benchmark uniquearticle{i:06d} synthetic document"
        names = [{"family": "Example", "given": "Jane"}]
        csl = {
            "DOI": doi,
            "title": title,
            "type": "article-journal",
            "author": names,
            "issued": {"date-parts": [[2024]]},
        }
        raw = {
            "DOI": doi,
            "title": [title],
            "type": "journal-article",
            "author": names,
            "issued": {"date-parts": [[2024]]},
        }
        r = {
            "schema_version": 2,
            "vault_id": str(uuid.UUID(int=0)),
            "work_id": str(uuid.UUID(int=i + 1)),
            "commit_id": str(uuid.UUID(int=i + 1 + a.count)),
            "parent_commit_id": None,
            "writer_id": str(uuid.UUID(int=1)),
            "doi": doi,
            "metadata": {"doi": doi, "raw": raw, "csl": csl},
            "decision": {"method": "manual"},
            "files": [
                {
                    "sha256": hashlib.sha256(str(i).encode()).hexdigest(),
                    "drive_file_id": f"fake-{i}",
                    "size": 1,
                    "verified_at": "synthetic",
                }
            ],
        }
        index._project(r)
        if (i + 1) % 1000 == 0:
            index.db.commit()
        if (i + 1) % 50000 == 0:
            print(f"Indexed {i + 1:,}", flush=True)
    index.db.commit()
    build = time.perf_counter() - start
    rng = random.Random(42)
    latency = {"doi": [], "hash": [], "title": []}
    for _ in range(300):
        i = rng.randrange(a.count)
        for name, run in [
            ("doi", lambda i=i: index.find(f"10.9999/benchmark.{i}")),
            ("hash", lambda i=i: index.blob(hashlib.sha256(str(i).encode()).hexdigest())),
            ("title", lambda i=i: index.find(f"uniquearticle{i:06d}")),
        ]:
            t = time.perf_counter()
            assert run()
            latency[name].append((time.perf_counter() - t) * 1000)
    index.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    report = {
        "scope": "synthetic local SQLite projections only; no real PDFs, Crossref or cloud load",
        "records": a.count,
        "build_seconds": round(build, 2),
        "sqlite_bytes": (Path(td) / "index.sqlite3").stat().st_size,
        "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "lookup_ms": {
            k: {"median": round(statistics.median(v), 3), "p95": round(sorted(v)[int(len(v) * 0.95)], 3)}
            for k, v in latency.items()
        },
    }
    Path(a.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    index.db.close()
