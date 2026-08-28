#!/usr/bin/env python3
"""Insert human-selected waypoints into the ``abnormal_detections`` table.

Two JSON sources are supported, each with a different format:

1. ``human_seected_waypoints2.json`` -- COCO-style. The ``annotated_image_ids``
   list holds ids of entries in the top-level ``images`` array; each entry has
   a ``timestamp_ns`` and a ``side`` ("left"/"right"). Both the left and right
   members of every annotated timestamp are inserted.

2. ``waypoints.json`` -- a flat list of waypoints, each carrying a
   ``timestamp_ns`` (and optional ``left_id`` / ``right_id`` which are NOT db
   ids). Both the left and right images sharing that timestamp are inserted.

For both sources the db image id is resolved by looking up the ``timestamp_ns``
in the ``images`` table of the target ``.db`` (left = ``is_left = 1``,
right = ``is_left = 0``). Rows are inserted into ``abnormal_detections`` with
``gt_image`` set to the resolved db image id and ``time_stamp`` set to the
timestamp. Existing rows with the same ``(gt_image)`` are skipped by default
(``--no-skip-existing`` to re-insert).

Run::

    python import_waypoints_to_db.py \
        --db inspection_database/inspection_v2.db \
        --human-json human_seected_waypoints2.json \
        --waypoints-json waypoints.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _ensure_abnormal_detections(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS abnormal_detections (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            gt_image          INTEGER,
            inspection_image  INTEGER,
            status            TEXT DEFAULT 'NOT_PROCESSED',
            summary           TEXT DEFAULT '',
            viewpoint_change  BOOLEAN DEFAULT FALSE,
            time_stamp        INTEGER,
            FOREIGN KEY (gt_image) REFERENCES images(id),
            FOREIGN KEY (inspection_image) REFERENCES images(id)
        )
        """
    )
    conn.commit()


def _load_ts_to_ids(conn: sqlite3.Connection) -> dict[int, dict[str, int | None]]:
    """Map timestamp_ns -> {"left": id|None, "right": id|None} from the db."""
    rows = conn.execute(
        "SELECT id, timestamp_ns, is_left FROM images"
    ).fetchall()
    out: dict[int, dict[str, int | None]] = {}
    for r in rows:
        ts = r[1]
        if ts is None:
            continue
        slot = out.setdefault(ts, {"left": None, "right": None})
        if r[2] == 1 and slot["left"] is None:
            slot["left"] = r[0]
        elif r[2] == 0 and slot["right"] is None:
            slot["right"] = r[0]
    return out


def _timestamps_from_human_json(path: Path) -> list[int]:
    """Return unique timestamps referenced by ``annotated_image_ids``."""
    data = json.loads(path.read_text())
    images = {im["id"]: im for im in data.get("images", [])}
    tss: list[int] = []
    for aid in data.get("annotated_image_ids", []):
        im = images.get(aid)
        if im is None:
            print(f"[warn] human-json: annotated id {aid} not in images array; skipping",
                  file=sys.stderr)
            continue
        ts = im.get("timestamp_ns")
        if ts is None:
            print(f"[warn] human-json: image id {aid} has no timestamp_ns; skipping",
                  file=sys.stderr)
            continue
        tss.append(ts)
    seen: set[int] = set()
    uniq: list[int] = []
    for ts in tss:
        if ts not in seen:
            seen.add(ts)
            uniq.append(ts)
    return uniq


def _timestamps_from_waypoints_json(path: Path) -> list[int]:
    """Return the ordered unique timestamps from a flat waypoints list."""
    data = json.loads(path.read_text())
    tss: list[int] = []
    for wp in data:
        ts = wp.get("timestamp_ns")
        if ts is None:
            continue
        tss.append(ts)
    seen: set[int] = set()
    uniq: list[int] = []
    for ts in tss:
        if ts not in seen:
            seen.add(ts)
            uniq.append(ts)
    return uniq


def _insert(
    conn: sqlite3.Connection,
    gt_image: int,
    timestamp_ns: int,
    skip_existing: bool,
) -> bool:
    """Insert one row; return True if inserted, False if skipped."""
    if skip_existing:
        exists = conn.execute(
            "SELECT 1 FROM abnormal_detections WHERE gt_image = ? LIMIT 1",
            (gt_image,),
        ).fetchone()
        if exists:
            return False
    conn.execute(
        "INSERT INTO abnormal_detections (gt_image, inspection_image, status, "
        "summary, viewpoint_change, time_stamp) "
        "VALUES (?, NULL, 'NOT_PROCESSED', '', 0, ?)",
        (gt_image, timestamp_ns),
    )
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db", required=True, help="Path to the inspection .db")
    p.add_argument("--human-json", default=None,
                   help="human_seected_waypoints2.json (COCO-style with annotated_image_ids)")
    p.add_argument("--waypoints-json", default=None,
                   help="waypoints.json (flat list with timestamp_ns)")
    p.add_argument("--no-skip-existing", dest="skip_existing", action="store_false",
                   help="Re-insert ids already present as gt_image (default: skip)")
    p.set_defaults(skip_existing=True)
    args = p.parse_args(argv)

    if not args.human_json and not args.waypoints_json:
        print("[error] provide at least one of --human-json / --waypoints-json",
              file=sys.stderr)
        return 2

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"[error] database not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        _ensure_abnormal_detections(conn)
        ts_map = _load_ts_to_ids(conn)

        all_ts: list[tuple[str, int]] = []
        if args.human_json:
            hts = _timestamps_from_human_json(Path(args.human_json))
            all_ts += [("human", ts) for ts in hts]
            print(f"[info] human-json: {len(hts)} unique timestamp(s)")
        if args.waypoints_json:
            wts = _timestamps_from_waypoints_json(Path(args.waypoints_json))
            all_ts += [("waypoints", ts) for ts in wts]
            print(f"[info] waypoints-json: {len(wts)} unique timestamp(s)")

        inserted = 0
        skipped_existing = 0
        missing_ts: list[tuple[str, int]] = []
        missing_side: list[tuple[str, int, str]] = []
        for src, ts in all_ts:
            pair = ts_map.get(ts)
            if pair is None:
                missing_ts.append((src, ts))
                continue
            for side in ("left", "right"):
                img_id = pair[side]
                if img_id is None:
                    missing_side.append((src, ts, side))
                    continue
                if _insert(conn, img_id, ts, args.skip_existing):
                    inserted += 1
                else:
                    skipped_existing += 1
        conn.commit()

        print(f"[info] inserted={inserted} skipped_existing={skipped_existing}")
        if missing_ts:
            print(f"[warn] {len(missing_ts)} timestamp(s) not found in db:",
                  file=sys.stderr)
            for src, ts in missing_ts:
                print(f"  [{src}] {ts}", file=sys.stderr)
        if missing_side:
            print(f"[warn] {len(missing_side)} missing side image(s) in db:",
                  file=sys.stderr)
            for src, ts, side in missing_side:
                print(f"  [{src}] ts={ts} side={side}", file=sys.stderr)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())