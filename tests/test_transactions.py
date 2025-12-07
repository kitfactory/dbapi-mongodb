import os

import pytest
from pymongo import MongoClient

from mongo_dbapi import connect


MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27018")
MONGODB_DB = os.environ.get("MONGODB_DB", "mongo_dbapi_test")


def _supports_transactions(uri: str) -> bool:
    client = MongoClient(uri)
    try:
        info = client.admin.command("hello")
    except Exception:
        return False
    # Transactions require replica set / sharded cluster with sessions and wire version >= 7
    if not info.get("logicalSessionTimeoutMinutes"):
        return False
    if not info.get("setName") and not info.get("isreplicaset"):
        return False
    if info.get("maxWireVersion", 0) < 7:
        return False
    return True


@pytest.mark.skipif(not _supports_transactions(MONGODB_URI), reason="Transactions not supported on this MongoDB")
def test_transaction_commit_and_rollback():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    # cleanup target id
    cur.execute("DELETE FROM users WHERE id = %s", (999,))
    # commit path
    conn.begin()
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (999, "TxCommit"))
    conn.commit()
    cur.execute("SELECT id FROM users WHERE id = %s", (999,))
    assert cur.fetchone() == (999,)
    # rollback path: delete then rollback should keep the row
    conn.begin()
    cur.execute("DELETE FROM users WHERE id = %s", (999,))
    conn.rollback()
    cur.execute("SELECT id FROM users WHERE id = %s", (999,))
    assert cur.fetchone() == (999,)
    # cleanup
    cur.execute("DELETE FROM users WHERE id = %s", (999,))
    conn.commit()
    conn.close()
