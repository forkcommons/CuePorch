"""Export a verified portable collection plus human-readable institutional rights and mapping records."""

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from cueporch import checksum, dump, verify
from live_slide_recovery import copy_decks, has_table


def export(collection, database, target):
    if target.exists():
        raise ValueError("Choose an unused export directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        root = Path(tmp) / "export"
        root.mkdir(mode=0o700)
        shutil.copytree(
            collection, root / "collection", ignore=shutil.ignore_patterns("*.lock")
        )
        verify(root / "collection")
        with closing(
            sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            tables = {
                "approval_settings": "SELECT step,required FROM policy",
                "approval_decisions": "SELECT id,step,status,decision,actor,created FROM action",
                "screening_grants": "SELECT id,work,origin,starts,ends,agreement,audience,version,revoked FROM grant_access",
                "screening_holds": "SELECT case_id,work,active FROM case_hold",
                "stable_routes": "SELECT * FROM route",
                "publication_receipts": "SELECT work,revision,want_public,status,remote,kind FROM publication_job",
                "cases": "SELECT id,work FROM complaint",
                "case_history": "SELECT case_id,kind,details,actor,at FROM case_event",
            }
            manifest = json.loads(
                (root / "collection" / "manifest.json").read_text(encoding="utf8")
            )
            owner = db.execute("SELECT institution FROM owner").fetchone()
            if not owner or owner[0] != manifest["institution"]:
                raise ValueError("Institution mismatch")
            record = {
                "schema": 1,
                "institution": owner[0],
                "collection": "collection/manifest.json",
            }
            for table in (
                "library_folder",
                "library_placement",
                "library_star",
                "library_title",
                "library_thumbnail",
                "library_embed_pages",
                "library_watch_later",
                "library_deletion",
                "library_deleted_item",
                "video_presentation",
                "video_rating_widget",
            ):
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone():
                    tables[table] = "SELECT * FROM " + table
            for name, statement in tables.items():
                record[name] = [dict(row) for row in db.execute(statement)]
            for row in record.get("library_embed_pages", []):
                row["pages"] = json.loads(row["pages"])
            if "video_rating_widget" in record:
                record["video_rating_summary"] = [dict(row) for row in db.execute(
                    """SELECT work,version,mode,score,count(*) AS responses FROM video_rating
                       GROUP BY work,version,mode,score ORDER BY work,version,mode,score"""
                )]
            for row in record.get("library_thumbnail", []):
                digest = row["object"]
                if checksum(root / "collection" / "objects" / digest) != digest:
                    raise ValueError("Thumbnail verification failed for " + row["work"])
            images = {}
            for row in record.get("video_presentation", []):
                values = json.loads(row["settings"])
                row["settings"] = values
                ids = {card["image"] for card in values["cards"]}
                ids.add(values["ending"].get("image"))
                for digest in ids - {None} - images.keys():
                    item = db.execute("SELECT mime,content FROM review_logo WHERE digest=?", (digest,)).fetchone()
                    if item is None:
                        raise ValueError("Missing interactivity artwork for " + row["work"])
                    folder = root / "interactivity-artwork"
                    folder.mkdir(exist_ok=True)
                    file = folder / digest
                    file.write_bytes(item["content"])
                    if checksum(file) != digest:
                        raise ValueError("Interactivity artwork verification failed for " + row["work"])
                    images[digest] = {"path": file.relative_to(root).as_posix(), "mime": item["mime"]}
            if images:
                record["interactivity_artwork"] = images
            presentations = copy_decks(db, database.parent, root)
            if presentations:
                record["live_slide_decks"] = presentations
                if has_table(db, "live_scene_deck"):
                    record["live_scene_decks"] = [dict(event=event, settings=json.loads(settings))
                                                  for event, settings in db.execute("SELECT event,settings FROM live_scene_deck")]
        dump(root / "rights-and-routes.json", record)
        dump(
            root / "SHA256.json",
            {
                f.relative_to(root).as_posix(): checksum(f)
                for f in root.rglob("*")
                if f.is_file()
            },
        )
        root.rename(target)
    return len(manifest["works"])


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("collection", type=Path)
    p.add_argument("database", type=Path)
    p.add_argument("target", type=Path)
    a = p.parse_args()
    print(export(a.collection, a.database, a.target), "works exported")
