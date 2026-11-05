"""SQLite persistence plus CSV / JSON export.

One table per record type. Tables are created on first use from the
dataclass fields, so adding a field to a model only needs the existing
database file to be deleted or the column added by hand."""
import csv
import json
import os
import sqlite3
from datetime import datetime, timezone

from seo import models

DEFAULT_DB = os.path.join("data", "seo.sqlite")

TABLES = {
    "serp_results": models.SerpResult,
    "local_pack": models.LocalPackEntry,
    "businesses": models.Business,
    "grid_cells": models.GridCell,
    "nap_records": models.NapRecord,
    "canary_runs": models.CanaryRun,
}


def _sql_type(py_type):
    # Works for plain types and typing.Optional[...] wrappers alike.
    name = str(py_type)
    if "bool" in name or "int" in name:
        return "INTEGER"
    if "float" in name:
        return "REAL"
    return "TEXT"


class Storage:
    def __init__(self, path=DEFAULT_DB):
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        cur = self.conn.cursor()
        for table, model in TABLES.items():
            cols = ", ".join(
                "%s %s" % (f.name, _sql_type(f.type))
                for f in model.__dataclass_fields__.values()
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS %s (id INTEGER PRIMARY KEY, %s)"
                % (table, cols)
            )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS geocode_cache "
            "(place TEXT PRIMARY KEY, lat REAL, lng REAL, display TEXT, "
            "canonical TEXT)"
        )
        self.conn.commit()

    def _table_for(self, record):
        for table, model in TABLES.items():
            if isinstance(record, model):
                return table
        raise TypeError("No table for %r" % type(record))

    def save(self, records, stamp=None):
        """Insert records. Pass `stamp` (an ISO timestamp) to give every
        record the same fetched_at so a multi-minute run groups as one
        batch in latest_runs()."""
        records = list(records)
        if not records:
            return 0
        cur = self.conn.cursor()
        for record in records:
            if stamp and hasattr(record, "fetched_at"):
                record.fetched_at = stamp
            table = self._table_for(record)
            data = record.to_dict()
            cols = ", ".join(data)
            marks = ", ".join("?" for _ in data)
            cur.execute(
                "INSERT INTO %s (%s) VALUES (%s)" % (table, cols, marks),
                [_coerce(v) for v in data.values()],
            )
        self.conn.commit()
        return len(records)

    # ---- queries -------------------------------------------------------
    def fetched_today(self, table, **where):
        """True if a row matching `where` was stored today (UTC)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        clause = " AND ".join("%s = ?" % k for k in where)
        sql = "SELECT 1 FROM %s WHERE %s AND fetched_at LIKE ? LIMIT 1" % (
            table, clause or "1=1"
        )
        row = self.conn.execute(
            sql, list(where.values()) + [today + "%"]
        ).fetchone()
        return row is not None

    def rows(self, table, order_by="fetched_at", **where):
        clause = " AND ".join("%s = ?" % k for k in where)
        sql = "SELECT * FROM %s" % table
        if clause:
            sql += " WHERE " + clause
        sql += " ORDER BY %s" % order_by
        return [dict(r) for r in self.conn.execute(sql, list(where.values()))]

    def latest_runs(self, table, key_cols, **where):
        """Return rows of the two most recent fetched_at batches for the
        given filter as (latest_rows, previous_rows). Used for diffs."""
        all_rows = self.rows(table, **where)
        stamps = sorted({r["fetched_at"] for r in all_rows}, reverse=True)
        latest, previous = [], []
        if stamps:
            latest = [r for r in all_rows if r["fetched_at"] == stamps[0]]
        if len(stamps) > 1:
            previous = [r for r in all_rows if r["fetched_at"] == stamps[1]]
        return latest, previous

    # ---- geocode cache -------------------------------------------------
    def cached_geocode(self, place):
        row = self.conn.execute(
            "SELECT lat, lng, display, canonical FROM geocode_cache "
            "WHERE place = ?", (place.lower(),),
        ).fetchone()
        if row is None:
            return None
        return row["lat"], row["lng"], row["display"], row["canonical"]

    def cache_geocode(self, place, lat, lng, display, canonical=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO geocode_cache VALUES (?, ?, ?, ?, ?)",
            (place.lower(), lat, lng, display, canonical),
        )
        self.conn.commit()

    # ---- export --------------------------------------------------------
    def export(self, table, directory="data", fmt="csv"):
        rows = self.rows(table)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "%s.%s" % (table, fmt))
        if fmt == "json":
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, indent=2)
        else:
            cols = ["id"] + TABLES[table].columns()
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=cols)
                writer.writeheader()
                writer.writerows(rows)
        return path

    def close(self):
        self.conn.close()


def _coerce(value):
    if isinstance(value, bool):
        return int(value)
    return value
