import os

import pytest

from mongo_dbapi import MongoDbApiError, connect
from bson import ObjectId
import datetime
from sqlalchemy import create_engine, text, Table, Column, Integer, String, MetaData, select
import decimal
import uuid


MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27018")
MONGODB_DB = os.environ.get("MONGODB_DB", "mongo_dbapi_test")
DBAPI_URI = "mongodb+dbapi://" + MONGODB_URI.split("://", 1)[1].rstrip("/")
COLLECTION = "users"


@pytest.fixture(autouse=True)
def clean_db():
    conn = connect(MONGODB_URI, MONGODB_DB)
    db = conn._db  # noqa: SLF001
    db[COLLECTION].delete_many({})
    db["orders"].delete_many({})
    db["addresses"].delete_many({})
    yield
    db[COLLECTION].delete_many({})
    db["orders"].delete_many({})
    db["addresses"].delete_many({})
    conn.close()


def test_insert_and_select_roundtrip():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (1, "Alice"))
    assert cur.rowcount == 1
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM users WHERE id = %s", (1,))
    rows = cur.fetchall()
    assert rows == [(1, "Alice")]
    assert cur.rowcount == 1
    assert cur.description[0][0] == "id"
    conn.close()


def test_update_and_delete():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (1, "Alice"))
    cur.execute("UPDATE users SET name = %s WHERE id = %s", ("Bob", 1))
    assert cur.rowcount == 1
    cur.execute("DELETE FROM users WHERE id = %s", (1,))
    assert cur.rowcount == 1
    conn.close()


def test_parameter_mismatch_raises():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError) as exc:
        cur.execute("SELECT * FROM users WHERE id = %s")
    assert "[mdb][E4]" in str(exc.value)
    conn.close()


def test_or_query():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (1, "Alice"))
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (2, "Bob"))
    cur.execute("SELECT * FROM users WHERE id = %s OR name = %s", (1, "Bob"))
    rows = cur.fetchall()
    assert len(rows) == 2
    conn.close()


def test_transaction_not_supported():
    conn = connect(MONGODB_URI, MONGODB_DB)
    conn.begin()
    conn.commit()
    conn.close()


def test_like_or_between_group_by():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name, score) VALUES (%s, %s, %s)", (1, "Alice", 10))
    cur.execute("INSERT INTO users (id, name, score) VALUES (%s, %s, %s)", (2, "Bob", 20))
    cur.execute(
        "SELECT name, COUNT(*) FROM users WHERE name LIKE %s OR score BETWEEN %s AND %s GROUP BY name",
        ("%A%", 5, 15),
    )
    rows = cur.fetchall()
    assert rows == [("Alice", 1)]
    conn.close()


def test_join_inner():
    conn = connect(MONGODB_URI, MONGODB_DB)
    db = conn._db  # noqa: SLF001
    db["orders"].delete_many({})
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (1, "Alice"))
    db["orders"].insert_one({"id": 10, "user_id": 1, "total": 100})
    cur.execute("SELECT u.id, o.total FROM users u JOIN orders o ON u.id = o.user_id WHERE o.total = %s", (100,))
    rows = cur.fetchall()
    assert rows == [(1, 100)]
    conn.close()


def test_join_two_hops():
    conn = connect(MONGODB_URI, MONGODB_DB)
    db = conn._db  # noqa: SLF001
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (1, "Alice"))
    db["orders"].insert_one({"id": 10, "user_id": 1, "total": 100})
    db["addresses"].insert_one({"id": 5, "order_id": 10, "city": "Tokyo"})
    cur.execute(
        "SELECT u.id, a.city FROM users u JOIN orders o ON u.id = o.user_id JOIN addresses a ON o.id = a.order_id WHERE a.city = %s",
        ("Tokyo",),
    )
    rows = cur.fetchall()
    assert rows == [(1, "Tokyo")]
    conn.close()


def test_create_drop_index():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("CREATE INDEX idx_users_name ON users(name)")
    cur.execute("DROP INDEX idx_users_name ON users")
    conn.close()


def test_left_join_with_missing_match():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (1, "Alice"))
    cur.execute("SELECT u.id, o.total FROM users u LEFT JOIN orders o ON u.id = o.user_id ORDER BY u.id")
    rows = cur.fetchall()
    assert rows == [(1, None)]
    conn.close()


def test_limit_offset_with_order():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (1, "A"))
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (2, "B"))
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (3, "C"))
    cur.execute("SELECT id FROM users ORDER BY id ASC LIMIT 2 OFFSET 1")
    rows = cur.fetchall()
    assert rows == [(2,), (3,)]
    conn.close()


def test_group_by_having_sum():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name, score) VALUES (%s, %s, %s)", (1, "A", 5))
    cur.execute("INSERT INTO users (id, name, score) VALUES (%s, %s, %s)", (2, "A", 7))
    cur.execute("INSERT INTO users (id, name, score) VALUES (%s, %s, %s)", (3, "B", 10))
    cur.execute("INSERT INTO users (id, name, score) VALUES (%s, %s, %s)", (4, "B", 12))
    cur.execute("SELECT name, SUM(score) AS total FROM users GROUP BY name HAVING total > %s ORDER BY name", (15,))
    rows = cur.fetchall()
    assert rows == [("B", 22)]
    conn.close()


def test_create_drop_table():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("CREATE TABLE items (id INT)")
    assert "items" in conn.list_tables()
    cur.execute("DROP TABLE items")
    conn.close()


def test_datetime_and_objectid_roundtrip():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    now = datetime.datetime.utcnow()
    oid = ObjectId()
    dec = decimal.Decimal("1.23")
    uid = uuid.uuid4()
    cur.execute("INSERT INTO users (id, name, created_at, oid, dec, uid) VALUES (%s, %s, %s, %s, %s, %s)", (3, "C", now, oid, dec, uid))
    cur.execute("SELECT created_at, oid, dec, uid FROM users WHERE id = %s", (3,))
    row = cur.fetchone()
    assert isinstance(row[0], datetime.datetime)
    assert isinstance(row[1], str)
    assert row[2] == "1.23"
    assert row[3] == str(uid)
    conn.close()


def test_sqlalchemy_integration():
    engine = create_engine(f"{DBAPI_URI}/{MONGODB_DB}")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = 99"))
        conn.execute(text("INSERT INTO users (id, name) VALUES (99, 'SA')"))
        rows = conn.execute(text("SELECT id, name FROM users WHERE id = 99")).all()
    assert len(rows) == 1
    assert int(rows[0][0]) == 99
    assert rows[0][1] == "SA"


def test_named_params_and_union_all():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (%(id)s, %(name)s)", {"id": 50, "name": "NP"})
    cur.execute("SELECT id FROM users WHERE id = %(id)s UNION ALL SELECT id FROM users WHERE name = %(name)s", {"id": 50, "name": "NP"})
    rows = cur.fetchall()
    assert (50,) in rows
    conn.close()


def test_delete_without_where_is_blocked():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError):
        cur.execute("DELETE FROM users")
    conn.close()


def test_missing_named_param_raises():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError):
        cur.execute("SELECT * FROM users WHERE id = %(id)s", {"other": 1})
    conn.close()


def test_sqlalchemy_core_table_crud():
    engine = create_engine(f"{DBAPI_URI}/{MONGODB_DB}")
    metadata = MetaData()
    users = Table("core_users", metadata, Column("id", Integer, primary_key=True), Column("name", String(50)))
    metadata.drop_all(engine)  # ensure clean
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(users.insert().values(id=300, name="Core"))
        rows = conn.execute(select(users.c.id, users.c.name).where(users.c.id == 300)).all()
    assert rows == [(300, "Core")]
    metadata.drop_all(engine)


def test_sqlalchemy_core_update_delete():
    engine = create_engine(f"{DBAPI_URI}/{MONGODB_DB}")
    metadata = MetaData()
    users = Table("core_users2", metadata, Column("id", Integer, primary_key=True), Column("name", String(50)))
    metadata.drop_all(engine)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(users.insert().values(id=1, name="Old"))
        conn.execute(users.update().where(users.c.id == 1).values(name="New"))
        conn.execute(users.delete().where(users.c.id == 1))
        rows = conn.execute(select(users.c.id).where(users.c.id == 1)).all()
    assert rows == []
    metadata.drop_all(engine)


def test_window_function_is_rejected():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError):
        cur.execute("SELECT id, ROW_NUMBER() OVER (PARTITION BY name) FROM users")
    conn.close()


def test_subquery_in_select():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (1, "A"))
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (2, "B"))
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (3, "C"))
    cur.execute("SELECT id FROM users WHERE id IN (SELECT id FROM users WHERE id >= %s)", (2,))
    rows = sorted(cur.fetchall())
    assert rows == [(2,), (3,)]
    conn.close()


def test_subquery_exists_as_boolean_gate():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (1, "A"))
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (2, "B"))
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (3, "C"))
    cur.execute("SELECT id FROM users WHERE EXISTS (SELECT 1 FROM users WHERE name = %s)", ("B",))
    rows_exists = sorted(cur.fetchall())
    assert rows_exists == [(1,), (2,), (3,)]
    cur.execute("SELECT id FROM users WHERE EXISTS (SELECT 1 FROM users WHERE name = %s)", ("Z",))
    rows_none = cur.fetchall()
    assert rows_none == []
    conn.close()
