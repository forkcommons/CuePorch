"""Enqueue a folder notification in the same transaction as its Library change."""

from time import time


FOLDERS_FOR_WORK = """
WITH RECURSIVE folders(folder,depth) AS (
 SELECT folder,0 FROM library_placement WHERE work=? AND folder IS NOT NULL
 UNION ALL SELECT f.parent,a.depth+1 FROM folders a JOIN library_folder f ON f.id=a.folder
 WHERE f.parent IS NOT NULL
)
"""


def enqueue(con, work, topic):
    now = int(time())
    con.execute(FOLDERS_FOR_WORK + """INSERT INTO folder_slack_notice(folder,revision,work,topic,created,due)
                SELECT s.folder,s.revision,?,?,?,? FROM folders f JOIN folder_slack s ON s.folder=f.folder
                WHERE s.enabled=1 AND (f.depth=0 OR s.recursive=1)
                  AND EXISTS(SELECT 1 FROM json_each(s.topics) WHERE value=?)""",
                (work, work, topic, now, now, "edit" if topic == "delete" else topic))
