#!/usr/bin/env python3
"""Sample odometry viewpoints along the inspection trajectory at a fixed distance interval.

Each ``clio_odometry`` row of the inspection is a distinct viewpoint in the
shared ``camera_init`` (FastLIO) global frame, ordered by capture id.

This script walks that trajectory in capture order and greedily picks a
viewpoint whenever the cumulative arc-length (sum of consecutive Euclidean
translation deltas) since the last picked viewpoint is >= ``--interval-m``.
Each picked viewpoint is committed to the ``inspection_waypoints`` table
(``id`` primary key, ``target`` referencing ``clio_odometry.id``), so the
sampled route can be consumed directly from the database by the rest of the
stack.

Run with the backend venv::

    python src/image_selection/sample_odometry_along_trajectory.py \
        --inspection 1 --interval-m 1.0

A ``--json`` file listing the picked ids and arc-length positions is also
written for diagnostics / reproducibility.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DB = _REPO_ROOT / "inspection_database" / "complete3" / "inspection_v2.db"


def _ensure_waypoints_table(conn: sqlite3.Connection) -> None:
    """Create the ``inspection_waypoints`` table if it does not yet exist.

    Two columns only: ``id`` (primary key) and ``target``, which references
    the ``clio_odometry`` row (capture id) of the sampled viewpoint.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inspection_waypoints (
            id     INTEGER PRIMARY KEY,
            target INTEGER NOT NULL,
            match INTEGER, 
            FOREIGN KEY (target) REFERENCES clio_odometry (id)
        )
        """
    )
    conn.commit()


def _commit_waypoints(
    conn: sqlite3.Connection, target_ids: list[int], skip_existing: bool
) -> int:
    """Write the sampled odometry ids into ``inspection_waypoints.target``.

    Each sampled id is inserted as a new row with ``target`` set to the
    ``clio_odometry.id`` of the viewpoint. With ``skip_existing`` (default),
    ids already present in ``target`` are left untouched so existing waypoint
    rows are never clobbered. Returns the number of rows inserted.
    """
    if not target_ids:
        return 0
    next_id = conn.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 FROM inspection_waypoints"
    ).fetchone()[0]
    inserted = 0
    for tid in target_ids:
        if skip_existing:
            exists = conn.execute(
                "SELECT 1 FROM inspection_waypoints WHERE target = ? LIMIT 1",
                (tid,),
            ).fetchone()
            if exists:
                continue
        conn.execute(
            "INSERT INTO inspection_waypoints (id, target) VALUES (?, ?)",
            (next_id, tid),
        )
        next_id += 1
        inserted += 1
    conn.commit()
    return inserted


def _load_viewpoints(
    conn: sqlite3.Connection, inspection_id: int
) -> list[dict[str, Any]]:
    """All pose-bearing odometry rows of one inspection, ordered by capture id.

    Every row is its own viewpoint; rows missing pose fields are excluded.
    """
    raw = conn.execute(
        """
        SELECT id, timestamp_ns,
               tf_translation_x AS tx, tf_translation_y AS ty, tf_translation_z AS tz,
               tf_rotation_x AS rx, tf_rotation_y AS ry,
               tf_rotation_z AS rz, tf_rotation_w AS rw
         FROM clio_odometry
         WHERE inspection_id = ? AND tf_translation_x IS NOT NULL
                                   AND tf_rotation_w IS NOT NULL
        ORDER BY id
        """,
        (inspection_id,),
    ).fetchall()
    return [dict(r) for r in raw]


def _sample_by_arclength(
    views: list[dict[str, Any]], interval_m: float, start_index: int
) -> list[dict[str, Any]]:
    """Greedy fixed-interval sampling along the cumulative arc-length trajectory.

    Arc length is the running sum of Euclidean translation deltas between
    consecutive viewpoints in capture order. The first viewpoint is always
    picked (at arc-length 0); thereafter a viewpoint is picked whenever the
    arc length has advanced by >= ``interval_m`` since the last pick. The final
    viewpoint is always picked so the end of the route is represented.
    """
    if not views:
        return []

    n = len(views)
    for i in range(n):
        p = views[i]
        p["arclength"] = 0.0 if i == 0 else (
            views[i - 1]["arclength"]
            + math.sqrt(
                (p["tx"] - views[i - 1]["tx"]) ** 2
                + (p["ty"] - views[i - 1]["ty"]) ** 2
                + (p["tz"] - views[i - 1]["tz"]) ** 2
            )
        )

    start_index = max(0, min(start_index, n - 1))
    picked: list[dict[str, Any]] = [views[start_index]]
    last_arc = views[start_index]["arclength"]
    for v in views[start_index + 1:]:
        if v["arclength"] - last_arc >= interval_m:
            picked.append(v)
            last_arc = v["arclength"]
    if picked[-1] is not views[-1]:
        picked.append(views[-1])
    return picked


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db", default=str(_DEFAULT_DB), help="Path to the inspection .db")
    p.add_argument("--inspection", type=int, default=1, help="Inspection id to sample from (default 1)")
    p.add_argument("--interval-m", type=float, default=1.0,
                   help="Fixed arc-length interval between sampled viewpoints in metres (default 1.0)")
    p.add_argument("--start-index", type=int, default=0,
                   help="Index (in capture order) of the first viewpoint to pick from (default 0)")
    p.add_argument("--json", dest="json_path", default=None,
                   help="Write the sampled list (ids + arc-lengths) to this JSON file")
    p.add_argument("--no-commit", action="store_true",
                   help="Do not insert the sampled ids into inspection_waypoints; only compute and print them")
    p.add_argument("--no-skip-existing", dest="skip_existing", action="store_false",
                   help="Re-insert sampled ids already present as inspection_waypoints.target")
    p.set_defaults(skip_existing=True)
    args = p.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"[error] database not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Ensure the inspection_waypoints table exists up front so both the
        # sampler and downstream consumers can rely on it.
        _ensure_waypoints_table(conn)
        views = _load_viewpoints(conn, args.inspection)
        if not views:
            print(f"[error] no pose-bearing viewpoints in inspection {args.inspection}", file=sys.stderr)
            return 2

        picked = _sample_by_arclength(views, args.interval_m, args.start_index)
        total_len = picked[-1]["arclength"] if picked else 0.0

        sampled_ids = sorted(set(v["id"] for v in picked))

        print(f"[info] db: {db_path}")
        print(f"[info] inspection {args.inspection}: {len(views)} viewpoint(s), "
              f"trajectory length {total_len:.3f} m")
        print(f"[info] interval={args.interval_m} m, start_index={args.start_index}")
        print(f"[info] picked {len(picked)} viewpoint(s) -> {len(sampled_ids)} odometry id(s):")
        print("  " + ", ".join(str(i) for i in sampled_ids))
        gaps = [picked[i + 1]["arclength"] - picked[i]["arclength"]
                for i in range(len(picked) - 1)]
        if gaps:
            print(f"[info] sample gaps (m): min={min(gaps):.3f} "
                  f"med={sorted(gaps)[len(gaps)//2]:.3f} max={max(gaps):.3f}")

        if args.json_path:
            payload = [
                {
                    "id": v["id"],
                    "arclength_m": round(v["arclength"], 4),
                    "tx": v["tx"], "ty": v["ty"], "tz": v["tz"],
                }
                for v in picked
            ]
            Path(args.json_path).write_text(json.dumps(payload, indent=2))
            print(f"[info] wrote {len(payload)} sampled viewpoint(s) to {args.json_path}")

        if args.no_commit:
            print("[info] --no-commit: skipping inspection_waypoints insert")
        else:
            n = _commit_waypoints(conn, sampled_ids, args.skip_existing)
            mode = "inserted" if args.skip_existing else "inserted (incl. duplicates)"
            print(f"[info] {n} sampled id(s) {mode} into inspection_waypoints.target")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
