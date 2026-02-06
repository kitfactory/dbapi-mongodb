import os

import pytest

from mongo_dbapi import MongoDbApiError, connect
from mongo_dbapi.async_dbapi import connect_async
from bson import ObjectId
import datetime
from sqlalchemy import create_engine, text, Table, Column, Integer, String, MetaData, select, Index
from sqlalchemy.orm import declarative_base, sessionmaker
import pymongo
import decimal
import uuid


MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27018")
MONGODB_DB = os.environ.get("MONGODB_DB", "mongo_dbapi_test")
DBAPI_URI = "mongodb+dbapi://" + MONGODB_URI.split("://", 1)[1].rstrip("/")
COLLECTION = "users"


def _window_supported(uri: str) -> bool:
    client = pymongo.MongoClient(uri)
    try:
        info = client.admin.command("hello")
    except Exception:
        return False
    max_wire = info.get("maxWireVersion", 0)
    version = info.get("version", "0.0")
    try:
        major = int(str(version).split(".")[0])
    except Exception:
        major = 0
    return max_wire >= 13 or major >= 5


@pytest.fixture(autouse=True)
def clean_db():
    conn = connect(MONGODB_URI, MONGODB_DB)
    db = conn._db  # noqa: SLF001
    db[COLLECTION].delete_many({})
    db["orders"].delete_many({})
    db["archived_users"].delete_many({})
    db["addresses"].delete_many({})
    db["cities"].delete_many({})
    db["orm_users"].delete_many({})
    yield
    db[COLLECTION].delete_many({})
    db["orders"].delete_many({})
    db["archived_users"].delete_many({})
    db["addresses"].delete_many({})
    db["cities"].delete_many({})
    db["orm_users"].delete_many({})
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


def test_parameter_extra_raises():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError) as exc:
        cur.execute("SELECT * FROM users WHERE id = %s", (1, 2))
    assert "[mdb][E4]" in str(exc.value)
    conn.close()


def test_named_params_extra_raises():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError) as exc:
        cur.execute("SELECT * FROM users WHERE id = %(id)s", {"id": 1, "other": 2})
    assert "[mdb][E4]" in str(exc.value)
    conn.close()


def test_connect_invalid_uri_raises():
    with pytest.raises(MongoDbApiError) as exc:
        connect("", MONGODB_DB)
    assert "[mdb][E1]" in str(exc.value)


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


def test_acceptance_a1_detail_orders_for_user():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO orders (id, user_id, tenant_id, total, created_at) VALUES (%s, %s, %s, %s, %s)",
        (101, 10, 1, 120, datetime.datetime(2024, 1, 3, 10, 0, 0)),
    )
    cur.execute(
        "INSERT INTO orders (id, user_id, tenant_id, total, created_at) VALUES (%s, %s, %s, %s, %s)",
        (102, 10, 1, None, datetime.datetime(2024, 1, 3, 9, 0, 0)),
    )
    cur.execute(
        "INSERT INTO orders (id, user_id, tenant_id, total, created_at) VALUES (%s, %s, %s, %s, %s)",
        (103, 10, 1, 150, datetime.datetime(2024, 1, 2, 12, 0, 0)),
    )
    cur.execute(
        "INSERT INTO orders (id, user_id, tenant_id, total, created_at) VALUES (%s, %s, %s, %s, %s)",
        (104, 11, 1, 999, datetime.datetime(2024, 1, 4, 0, 0, 0)),
    )
    sql = (
        "SELECT o.id, o.total, o.created_at "
        "FROM orders o "
        "WHERE o.tenant_id = %s AND o.user_id = %s "
        "ORDER BY o.created_at DESC, o.id ASC LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (1, 10, 2, 0))
    rows = cur.fetchall()
    assert rows == [
        (101, 120, datetime.datetime(2024, 1, 3, 10, 0, 0)),
        (102, None, datetime.datetime(2024, 1, 3, 9, 0, 0)),
    ]
    conn.close()


def test_acceptance_a2_search_distinct_not_in():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "Alice"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "Alicia"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (3, 1, "Bob"))
    cur.execute("INSERT INTO orders (id, user_id, status) VALUES (%s, %s, %s)", (201, 1, "new"))
    cur.execute("INSERT INTO orders (id, user_id, status) VALUES (%s, %s, %s)", (202, 1, "cancelled"))
    cur.execute("INSERT INTO orders (id, user_id, status) VALUES (%s, %s, %s)", (203, 2, "archived"))
    cur.execute("INSERT INTO orders (id, user_id, status) VALUES (%s, %s, %s)", (204, 2, "new"))
    sql = (
        "SELECT DISTINCT u.id "
        "FROM users u JOIN orders o ON u.id = o.user_id "
        "WHERE u.tenant_id = %s AND u.name ILIKE %s AND o.status NOT IN (%s, %s) "
        "ORDER BY u.id LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (1, "a%", "cancelled", "archived", 10, 0))
    rows = cur.fetchall()
    assert rows == [(1,), (2,)]
    conn.close()


def test_acceptance_a3_group_having_order_alias():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO orders (id, tenant_id, status, total, created_at) VALUES (%s, %s, %s, %s, %s)",
        (301, 1, "done", 10, datetime.datetime(2024, 1, 5)),
    )
    cur.execute(
        "INSERT INTO orders (id, tenant_id, status, total, created_at) VALUES (%s, %s, %s, %s, %s)",
        (302, 1, "done", 15, datetime.datetime(2024, 1, 6)),
    )
    cur.execute(
        "INSERT INTO orders (id, tenant_id, status, total, created_at) VALUES (%s, %s, %s, %s, %s)",
        (303, 1, "done", 20, datetime.datetime(2024, 1, 7)),
    )
    cur.execute(
        "INSERT INTO orders (id, tenant_id, status, total, created_at) VALUES (%s, %s, %s, %s, %s)",
        (304, 1, "new", 5, datetime.datetime(2024, 1, 10)),
    )
    cur.execute(
        "INSERT INTO orders (id, tenant_id, status, total, created_at) VALUES (%s, %s, %s, %s, %s)",
        (305, 1, "new", 7, datetime.datetime(2024, 1, 11)),
    )
    sql = (
        "SELECT o.status, COUNT(*) AS cnt, SUM(o.total) AS total_sum "
        "FROM orders o "
        "WHERE o.tenant_id = %s AND o.created_at BETWEEN %s AND %s "
        "GROUP BY o.status HAVING cnt >= %s ORDER BY cnt DESC LIMIT %s"
    )
    cur.execute(sql, (1, datetime.datetime(2024, 1, 1), datetime.datetime(2024, 1, 31), 2, 10))
    rows = cur.fetchall()
    assert rows == [("done", 3, 45), ("new", 2, 12)]
    conn.close()
    conn.close()


def test_acceptance_a4_search_ilike_contains():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "Auser10"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "Buser21"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (3, 1, "other"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (4, 2, "Auser10"))
    sql = (
        "SELECT u.id "
        "FROM users u "
        "WHERE u.tenant_id = %s AND u.name ILIKE %s "
        "ORDER BY u.id LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (1, "%user1%", 10, 0))
    rows = cur.fetchall()
    assert rows == [(1,)]
    conn.close()


def test_acceptance_a5_deep_offset_paging():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    # Insert deterministic timestamps / 時刻を固定して挿入
    cur.execute(
        "INSERT INTO orders (id, tenant_id, created_at) VALUES (%s, %s, %s)",
        (1, 1, datetime.datetime(2024, 1, 3, 0, 0, 0)),
    )
    cur.execute(
        "INSERT INTO orders (id, tenant_id, created_at) VALUES (%s, %s, %s)",
        (2, 1, datetime.datetime(2024, 1, 2, 0, 0, 0)),
    )
    cur.execute(
        "INSERT INTO orders (id, tenant_id, created_at) VALUES (%s, %s, %s)",
        (3, 1, datetime.datetime(2024, 1, 1, 0, 0, 0)),
    )
    sql = (
        "SELECT o.id, o.created_at "
        "FROM orders o "
        "WHERE o.tenant_id = %s "
        "ORDER BY o.created_at DESC, o.id ASC LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (1, 1, 1))
    rows = cur.fetchall()
    assert rows == [(2, datetime.datetime(2024, 1, 2, 0, 0, 0))]
    conn.close()


def test_acceptance_a11_exists_not_exists():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "A"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "B"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (3, 2, "C"))
    cur.execute("INSERT INTO orders (id, tenant_id, status) VALUES (%s, %s, %s)", (1, 1, "new"))

    sql_exists = (
        "SELECT u.id "
        "FROM users u "
        "WHERE u.tenant_id = %s AND EXISTS (SELECT id FROM orders o WHERE o.tenant_id = %s AND o.status = %s LIMIT 1) "
        "ORDER BY u.id LIMIT %s"
    )
    cur.execute(sql_exists, (1, 1, "new", 10))
    assert cur.fetchall() == [(1,), (2,)]

    sql_not_exists = (
        "SELECT u.id "
        "FROM users u "
        "WHERE u.tenant_id = %s AND NOT EXISTS (SELECT id FROM orders o WHERE o.tenant_id = %s AND o.status = %s LIMIT 1) "
        "ORDER BY u.id LIMIT %s"
    )
    cur.execute(sql_not_exists, (1, 1, "new", 10))
    assert cur.fetchall() == []
    cur.execute(sql_not_exists, (1, 1, "missing", 10))
    assert cur.fetchall() == [(1,), (2,)]
    conn.close()


def test_acceptance_a12_join_composite_on():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (10, 1, "Alice"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (10, 2, "OtherTenant"))
    cur.execute(
        "INSERT INTO orders (id, user_id, tenant_id, created_at) VALUES (%s, %s, %s, %s)",
        (201, 10, 1, datetime.datetime(2024, 1, 2, 0, 0, 0)),
    )
    cur.execute(
        "INSERT INTO orders (id, user_id, tenant_id, created_at) VALUES (%s, %s, %s, %s)",
        (202, 10, 2, datetime.datetime(2024, 1, 3, 0, 0, 0)),
    )
    sql = (
        "SELECT o.id, o.created_at, u.name "
        "FROM orders o JOIN users u ON o.user_id = u.id AND o.tenant_id = u.tenant_id "
        "WHERE o.tenant_id = %s "
        "ORDER BY o.created_at DESC, o.id ASC LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (1, 10, 0))
    assert cur.fetchall() == [(201, datetime.datetime(2024, 1, 2, 0, 0, 0), "Alice")]
    conn.close()


def test_acceptance_a13_union_all_with_offset():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (3, 1, "U3"))
    cur.execute("INSERT INTO archived_users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "A2"))
    cur.execute("INSERT INTO archived_users (id, tenant_id, name) VALUES (%s, %s, %s)", (4, 1, "A4"))
    sql = (
        "SELECT id FROM users WHERE tenant_id = %s "
        "UNION ALL SELECT id FROM archived_users WHERE tenant_id = %s "
        "ORDER BY id LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (1, 1, 2, 1))
    assert cur.fetchall() == [(2,), (3,)]
    conn.close()


def test_acceptance_a14_distinct_multi_columns():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO orders (id, tenant_id, status) VALUES (%s, %s, %s)", (1, 1, "new"))
    cur.execute("INSERT INTO orders (id, tenant_id, status) VALUES (%s, %s, %s)", (2, 1, "new"))
    cur.execute("INSERT INTO orders (id, tenant_id, status) VALUES (%s, %s, %s)", (3, 1, "done"))
    cur.execute("INSERT INTO orders (id, tenant_id, status) VALUES (%s, %s, %s)", (4, 2, "new"))
    sql = (
        "SELECT DISTINCT tenant_id, status "
        "FROM orders "
        "WHERE tenant_id IN (%s, %s) "
        "ORDER BY tenant_id, status LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (1, 2, 10, 0))
    assert cur.fetchall() == [(1, "done"), (1, "new"), (2, "new")]
    conn.close()


def test_acceptance_a15_count_distinct():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO orders (id, tenant_id, user_id) VALUES (%s, %s, %s)", (1, 1, 10))
    cur.execute("INSERT INTO orders (id, tenant_id, user_id) VALUES (%s, %s, %s)", (2, 1, 10))
    cur.execute("INSERT INTO orders (id, tenant_id, user_id) VALUES (%s, %s, %s)", (3, 1, 11))
    cur.execute("INSERT INTO orders (id, tenant_id, user_id) VALUES (%s, %s, %s)", (4, 2, 20))
    cur.execute("SELECT COUNT(DISTINCT user_id) AS uniq_users FROM orders WHERE tenant_id = %s", (1,))
    assert cur.fetchall() == [(2,)]
    cur.execute(
        "SELECT tenant_id, COUNT(DISTINCT user_id) AS uniq_users FROM orders GROUP BY tenant_id ORDER BY tenant_id LIMIT %s",
        (10,),
    )
    assert cur.fetchall() == [(1, 2), (2, 1)]
    conn.close()


def test_acceptance_a16_join_group_by():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "U2"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (3, 1, "U3"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 2, "U1-T2"))
    cur.execute("INSERT INTO orders (id, tenant_id, user_id) VALUES (%s, %s, %s)", (1, 1, 1))
    cur.execute("INSERT INTO orders (id, tenant_id, user_id) VALUES (%s, %s, %s)", (2, 1, 1))
    cur.execute("INSERT INTO orders (id, tenant_id, user_id) VALUES (%s, %s, %s)", (3, 1, 2))
    cur.execute("INSERT INTO orders (id, tenant_id, user_id) VALUES (%s, %s, %s)", (4, 2, 1))
    sql = (
        "SELECT u.id, COUNT(o.id) AS order_cnt "
        "FROM users u LEFT JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id "
        "WHERE u.tenant_id = %s "
        "GROUP BY u.id HAVING order_cnt >= %s ORDER BY order_cnt DESC, u.id ASC LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (1, 1, 10, 0))
    assert cur.fetchall() == [(1, 2), (2, 1)]
    conn.close()


def test_acceptance_a17_correlated_exists():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "U2"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (3, 2, "U3"))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (10, 1, 1))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (11, 3, 2))

    sql_exists = (
        "SELECT u.id "
        "FROM users u "
        "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id AND o.tenant_id = u.tenant_id) "
        "ORDER BY u.id LIMIT %s"
    )
    cur.execute(sql_exists, (20,))
    assert cur.fetchall() == [(1,), (3,)]

    sql_not_exists = (
        "SELECT u.id "
        "FROM users u "
        "WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id AND o.tenant_id = u.tenant_id) "
        "ORDER BY u.id LIMIT %s"
    )
    cur.execute(sql_not_exists, (20,))
    assert cur.fetchall() == [(2,)]
    conn.close()


def test_acceptance_a18_union_distinct():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 2, "U2"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (3, 1, "U3"))
    cur.execute("INSERT INTO archived_users (id, tenant_id, name) VALUES (%s, %s, %s)", (10, 1, "A1"))
    cur.execute("INSERT INTO archived_users (id, tenant_id, name) VALUES (%s, %s, %s)", (11, 2, "A2"))
    sql = (
        "SELECT tenant_id FROM users WHERE tenant_id IN (%s, %s) "
        "UNION "
        "SELECT tenant_id FROM archived_users WHERE tenant_id IN (%s, %s) "
        "ORDER BY tenant_id LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (1, 2, 1, 2, 10, 0))
    assert cur.fetchall() == [(1,), (2,)]
    conn.close()


def test_acceptance_a19_non_equi_join():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "U2"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (3, 2, "U3"))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (10, 1, 1))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (11, 2, 1))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (12, 3, 2))
    sql = (
        "SELECT u.id, o.id "
        "FROM users u JOIN orders o ON u.id >= o.user_id "
        "WHERE u.tenant_id = %s "
        "ORDER BY u.id, o.id LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (1, 20, 0))
    assert cur.fetchall() == [(1, 10), (2, 10), (2, 11)]
    conn.close()


def test_acceptance_a20_with_cte():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "U2"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (3, 2, "U3"))
    sql = (
        "WITH active_users AS ("
        "SELECT id, tenant_id FROM users WHERE tenant_id = %s"
        ") "
        "SELECT id FROM active_users WHERE id >= %s ORDER BY id LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (1, 2, 10, 0))
    assert cur.fetchall() == [(2,)]
    conn.close()


def test_acceptance_a21_scalar_subquery():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, score, name) VALUES (%s, %s, %s, %s)", (1, 1, 10, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, score, name) VALUES (%s, %s, %s, %s)", (2, 1, 30, "U2"))
    cur.execute("INSERT INTO users (id, tenant_id, score, name) VALUES (%s, %s, %s, %s)", (3, 2, 20, "U3"))
    sql = (
        "SELECT id "
        "FROM users "
        "WHERE tenant_id = %s AND score >= (SELECT AVG(score) FROM users WHERE tenant_id = %s) "
        "ORDER BY id LIMIT %s"
    )
    cur.execute(sql, (1, 1, 20))
    assert cur.fetchall() == [(2,)]
    conn.close()


def test_acceptance_a22_union_all_three_way():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 2, "U2"))
    cur.execute("INSERT INTO archived_users (id, tenant_id, name) VALUES (%s, %s, %s)", (10, 1, "A1"))
    sql = (
        "SELECT id FROM users WHERE tenant_id = %s "
        "UNION ALL SELECT id FROM archived_users WHERE tenant_id = %s "
        "UNION ALL SELECT id FROM users WHERE tenant_id = %s "
        "ORDER BY id LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (1, 1, 2, 20, 0))
    assert cur.fetchall() == [(1,), (2,), (10,)]
    conn.close()


def test_acceptance_a23_right_outer_join():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (10, 1, 1))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (11, 99, 1))
    sql = (
        "SELECT u.id, o.id "
        "FROM users u RIGHT OUTER JOIN orders o ON u.id = o.user_id "
        "ORDER BY o.id LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (20, 0))
    assert cur.fetchall() == [(1, 10), (None, 11)]
    conn.close()


def test_acceptance_a24_right_outer_join_composite_on():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "U2"))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (10, 1, 1))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (11, 1, 2))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (12, 99, 1))
    sql = (
        "SELECT u.id, o.id "
        "FROM users u RIGHT OUTER JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id "
        "WHERE o.tenant_id = %s "
        "ORDER BY o.id LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (1, 20, 0))
    assert cur.fetchall() == [(1, 10), (None, 12)]
    conn.close()


def test_acceptance_a25_full_outer_join():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "U2"))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (10, 1, 1))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (11, 99, 1))
    sql = (
        "SELECT u.id AS uid, o.id AS oid "
        "FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id "
        "ORDER BY oid LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (20, 0))
    assert cur.fetchall() == [(2, None), (1, 10), (None, 11)]
    conn.close()


def test_acceptance_a26_full_outer_join_composite_on():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "U2"))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (10, 1, 1))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (11, 1, 2))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (12, 99, 1))
    sql = (
        "SELECT u.id AS uid, o.id AS oid "
        "FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id "
        "ORDER BY uid, oid LIMIT %s OFFSET %s"
    )
    cur.execute(sql, (20, 0))
    assert cur.fetchall() == [(None, 11), (None, 12), (1, 10), (2, None)]
    conn.close()


def test_acceptance_a27_full_outer_join_group_by():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 2, "U2"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (3, 3, "U3"))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (10, 1, 1))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (11, 99, 4))
    sql = (
        "SELECT COALESCE(u.tenant_id, o.tenant_id) AS tenant_id, COUNT(*) AS cnt "
        "FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id "
        "GROUP BY COALESCE(u.tenant_id, o.tenant_id) "
        "ORDER BY tenant_id LIMIT %s"
    )
    cur.execute(sql, (20,))
    assert cur.fetchall() == [(1, 1), (2, 1), (3, 1), (4, 1)]
    conn.close()


def test_acceptance_a27_full_outer_join_group_by_having():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 2, "U2"))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (10, 1, 1))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (11, 99, 4))
    sql = (
        "SELECT COALESCE(u.tenant_id, o.tenant_id) AS tenant_id, COUNT(*) AS cnt "
        "FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id "
        "GROUP BY COALESCE(u.tenant_id, o.tenant_id) "
        "HAVING cnt >= %s "
        "ORDER BY tenant_id LIMIT %s"
    )
    cur.execute(sql, (1, 20))
    assert cur.fetchall() == [(1, 1), (2, 1), (4, 1)]
    cur.execute(sql, (2, 20))
    assert cur.fetchall() == []
    conn.close()


def test_delete_without_where_is_blocked():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError):
        cur.execute("DELETE FROM users")
    conn.close()


def test_update_without_where_is_blocked():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError):
        cur.execute("UPDATE users SET name = %s", ("X",))
    conn.close()


def test_missing_named_param_raises():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError):
        cur.execute("SELECT * FROM users WHERE id = %(id)s", {"other": 1})
    conn.close()


def test_union_without_all_supported():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (10, 1, "X"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (11, 2, "Y"))
    cur.execute("INSERT INTO archived_users (id, tenant_id, name) VALUES (%s, %s, %s)", (99, 1, "AX"))
    cur.execute("INSERT INTO archived_users (id, tenant_id, name) VALUES (%s, %s, %s)", (100, 2, "AY"))
    cur.execute(
        "SELECT tenant_id FROM users WHERE tenant_id IN (%s, %s) "
        "UNION SELECT tenant_id FROM archived_users WHERE tenant_id IN (%s, %s) "
        "ORDER BY tenant_id",
        (1, 2, 1, 2),
    )
    assert cur.fetchall() == [(1,), (2,)]
    conn.close()


def test_non_equi_join_supported():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "U2"))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (11, 1, 1))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (12, 2, 1))
    cur.execute("SELECT u.id, o.id FROM users u JOIN orders o ON u.id >= o.user_id WHERE u.tenant_id = %s ORDER BY u.id, o.id", (1,))
    assert cur.fetchall() == [(1, 11), (2, 11), (2, 12)]
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


def test_sqlalchemy_core_table_crud_with_index():
    engine = create_engine(f"{DBAPI_URI}/{MONGODB_DB}")
    metadata = MetaData()
    users = Table(
        "core_users3",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
    )
    idx = users.indexes.add(Index("ix_core_users3_name", users.c.name))
    metadata.drop_all(engine)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(users.insert().values(id=10, name="Idx"))
        rows = conn.execute(select(users.c.id, users.c.name).where(users.c.id == 10)).all()
        assert rows == [(10, "Idx")]
    metadata.drop_all(engine)


def test_sqlalchemy_named_param_mismatch_raises():
    engine = create_engine(f"{DBAPI_URI}/{MONGODB_DB}")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = 999"))
        with pytest.raises(Exception) as exc:
            conn.execute(text("SELECT id FROM users WHERE id = :id"), {"other": 1})
    assert "id" in str(exc.value)


def test_sqlalchemy_core_join_and_union_all():
    engine = create_engine(f"{DBAPI_URI}/{MONGODB_DB}")
    metadata = MetaData()
    users = Table("core_users4", metadata, Column("id", Integer, primary_key=True), Column("name", String(50)))
    orders = Table("core_orders4", metadata, Column("id", Integer, primary_key=True), Column("user_id", Integer), Column("total", Integer))
    metadata.drop_all(engine)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(users.insert(), [{"id": 1, "name": "U1"}, {"id": 2, "name": "U2"}])
        conn.execute(orders.insert(), [{"id": 10, "user_id": 1, "total": 100}, {"id": 11, "user_id": 2, "total": 200}])
        join_stmt = (
            select(users.c.id, users.c.name, orders.c.total)
            .select_from(users.join(orders, users.c.id == orders.c.user_id))
            .order_by(users.c.id)
        )
        rows = conn.execute(join_stmt).all()
        assert rows == [(1, "U1", 100), (2, "U2", 200)]
        union_stmt = select(users.c.id).where(users.c.id == 1).union_all(select(users.c.id).where(users.c.id == 2))
        union_rows = sorted(conn.execute(union_stmt).all())
        assert union_rows == [(1,), (2,)]
    metadata.drop_all(engine)


def test_sqlalchemy_union_distinct_supported():
    engine = create_engine(f"{DBAPI_URI}/{MONGODB_DB}")
    metadata = MetaData()
    users = Table("core_users5", metadata, Column("id", Integer, primary_key=True))
    users_arch = Table("core_users5_arch", metadata, Column("id", Integer, primary_key=True))
    metadata.drop_all(engine)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(users.insert(), [{"id": 1}, {"id": 2}])
        conn.execute(users_arch.insert(), [{"id": 2}, {"id": 3}])
        stmt = select(users.c.id).union(select(users_arch.c.id)).order_by("id")
        assert conn.execute(stmt).all() == [(1,), (2,), (3,)]
    metadata.drop_all(engine)


def test_union_all_with_order_limit():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id >= 0")
    cur.execute("INSERT INTO users (id, name) VALUES (1, 'U1')")
    cur.execute("INSERT INTO users (id, name) VALUES (2, 'U2')")
    cur.execute("INSERT INTO users (id, name) VALUES (3, 'U3')")
    cur.execute(
        "SELECT id FROM users WHERE id = 1 UNION ALL SELECT id FROM users WHERE id = 3 ORDER BY id DESC"
    )
    rows = cur.fetchall()
    assert sorted(rows) == [(1,), (3,)]
    conn.close()


def test_union_mixed_is_rejected():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError):
        cur.execute(
            "SELECT id FROM users "
            "UNION ALL SELECT id FROM archived_users "
            "UNION SELECT id FROM users"
        )
    conn.close()


def test_connect_invalid_host_e7():
    uri = "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=500&connectTimeoutMS=500"
    with pytest.raises(Exception) as exc:
        conn = connect(uri, MONGODB_DB)
        cur = conn.cursor()
        cur.execute("SELECT id FROM users")
    msg = str(exc.value)
    assert "[mdb][E7]" in msg or "ServerSelectionTimeoutError" in msg or "Connection refused" in msg


def test_transaction_on_unsupported_server_is_noop():
    # 3.6 相当サーバー想定で、begin/commit が no-op で例外にならないことを確認
    conn = connect("mongodb://127.0.0.1:27018", MONGODB_DB)
    conn.begin()
    conn.commit()
    conn.rollback()
    conn.close()


def test_sqlalchemy_orm_minimal_crud():
    engine = create_engine(f"{DBAPI_URI}/{MONGODB_DB}")
    Base = declarative_base()

    class User(Base):
        __tablename__ = "orm_users"
        id = Column(Integer, primary_key=True)
        name = Column(String(50))

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add(User(id=1, name="OrmUser"))
    session.commit()
    obj = session.query(User).filter_by(id=1).one()
    assert obj.name == "OrmUser"
    obj.name = "Updated"
    session.commit()
    obj = session.query(User).filter(User.id == 1).one()
    assert obj.name == "Updated"
    session.delete(obj)
    session.commit()
    assert session.query(User).filter_by(id=1).count() == 0
    session.close()
    Base.metadata.drop_all(engine)


def test_window_function_is_rejected():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError):
        cur.execute("SELECT id, ROW_NUMBER() OVER (PARTITION BY name) FROM users")
    conn.close()


def test_full_outer_join_composite_supported():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "U2"))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (10, 1, 1))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (11, 1, 2))
    cur.execute(
        "SELECT u.id AS uid, o.id AS oid "
        "FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id "
        "ORDER BY uid, oid"
    )
    assert cur.fetchall() == [(None, 11), (1, 10), (2, None)]
    conn.close()


def test_right_outer_join_non_equi_on_rejected_explicit():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError) as exc:
        cur.execute("SELECT u.id, o.id FROM users u RIGHT OUTER JOIN orders o ON u.id >= o.user_id")
    assert "[mdb][E2]" in str(exc.value)
    assert "RIGHT_JOIN_NON_EQ" in str(exc.value)
    conn.close()


def test_full_outer_join_non_equi_on_rejected_explicit():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError) as exc:
        cur.execute("SELECT u.id, o.id FROM users u FULL OUTER JOIN orders o ON u.id >= o.user_id")
    assert "[mdb][E2]" in str(exc.value)
    assert "FULL_JOIN_NON_EQ" in str(exc.value)
    conn.close()


def test_full_outer_join_chain_rejected_explicit():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError) as exc:
        cur.execute(
            "SELECT u.id, o.id, a.id "
            "FROM users u "
            "FULL OUTER JOIN orders o ON u.id = o.user_id "
            "JOIN addresses a ON o.id = a.order_id"
        )
    assert "[mdb][E2]" in str(exc.value)
    assert "FULL_JOIN_CHAIN" in str(exc.value)
    conn.close()


def test_window_function_other_than_row_number_is_rejected():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError):
        cur.execute("SELECT id, RANK() OVER (ORDER BY id) FROM users")
    conn.close()


def test_parse_error_returns_e5():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError) as exc:
        cur.execute("SELCT * FROM users")
    assert "[mdb][E5]" in str(exc.value)
    conn.close()


def test_correlated_subquery_supported():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "A"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "B"))
    cur.execute("INSERT INTO orders (id, user_id, tenant_id) VALUES (%s, %s, %s)", (10, 1, 1))
    cur.execute(
        "SELECT u.id FROM users u "
        "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id AND o.tenant_id = u.tenant_id) "
        "ORDER BY u.id"
    )
    assert cur.fetchall() == [(1,)]
    cur.execute(
        "SELECT u.id FROM users u "
        "WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id AND o.tenant_id = u.tenant_id) "
        "ORDER BY u.id"
    )
    assert cur.fetchall() == [(2,)]
    conn.close()


def test_correlated_subquery_with_join_is_rejected():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError):
        cur.execute(
            "SELECT u.id FROM users u "
            "WHERE EXISTS (SELECT o.id FROM orders o JOIN users x ON o.user_id = x.id WHERE o.user_id = u.id)"
        )
    conn.close()


def test_having_non_aggregate_column_is_rejected():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name, score) VALUES (1, 'A', 10)")
    with pytest.raises(Exception):
        cur.execute("SELECT name, SUM(score) FROM users GROUP BY name HAVING id > 0")
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


def test_from_subquery_select():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (1, "A"))
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (2, "B"))
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (3, "C"))
    cur.execute("SELECT id, name FROM (SELECT id, name FROM users WHERE id >= %s) AS t WHERE id < %s ORDER BY id DESC", (2, 3))
    rows = cur.fetchall()
    assert rows == [(2, "B")]
    conn.close()


def test_with_recursive_is_rejected():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError):
        cur.execute(
            "WITH RECURSIVE t AS (SELECT 1 AS id UNION ALL SELECT id + 1 FROM t WHERE id < 3) "
            "SELECT id FROM t"
        )
    conn.close()


def test_with_cte_union_all_supported():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (1, 1, "U1"))
    cur.execute("INSERT INTO users (id, tenant_id, name) VALUES (%s, %s, %s)", (2, 1, "U2"))
    cur.execute(
        "WITH tenant_users AS (SELECT id FROM users WHERE tenant_id = %s) "
        "SELECT id FROM tenant_users UNION ALL SELECT id FROM tenant_users ORDER BY id",
        (1,),
    )
    assert cur.fetchall() == [(1,), (1,), (2,), (2,)]
    conn.close()


def test_ilike_and_regex_literal():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (1, "alice"))
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (2, "bob"))
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (3, "bobby"))
    cur.execute("SELECT id FROM users WHERE name ILIKE %s ORDER BY id", ("b%",))
    assert cur.fetchall() == [(2,), (3,)]
    cur.execute("SELECT id FROM users WHERE name REGEXP '/^bo/' ORDER BY id")
    assert cur.fetchall() == [(2,), (3,)]
    conn.close()


def test_three_hop_join():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (1, 'U1')")
    cur.execute("INSERT INTO users (id, name) VALUES (2, 'U2')")
    cur.execute("INSERT INTO orders (id, user_id) VALUES (10, 1)")
    cur.execute("INSERT INTO addresses (id, order_id, city_id) VALUES (100, 10, 1000)")
    cur.execute("INSERT INTO cities (id, name) VALUES (1000, 'City')")
    sql = """
    SELECT u.id, c.name
    FROM users u
    JOIN orders o ON u.id = o.user_id
    JOIN addresses a ON o.id = a.order_id
    JOIN cities c ON a.city_id = c.id
    WHERE c.name = %s
    """
    cur.execute(sql, ("City",))
    assert cur.fetchall() == [(1, "City")]
    conn.close()


def test_binary_and_uuid():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    import uuid

    bin_data = b"\x01\x02\x03"
    uid = uuid.uuid4()
    cur.execute("INSERT INTO users (id, name, uid, bin) VALUES (%s, %s, %s, %s)", (5, "Bin", uid, bin_data))
    cur.execute("SELECT uid, bin FROM users WHERE id = %s", (5,))
    row = cur.fetchone()
    assert row[0] == str(uid)
    assert row[1] == "AQID"
    conn.close()


def test_window_row_number():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (1, "A"))
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (2, "A"))
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (3, "B"))
    if not _window_supported(MONGODB_URI):
        with pytest.raises(MongoDbApiError):
            cur.execute("SELECT id, ROW_NUMBER() OVER (PARTITION BY name ORDER BY id) AS rn FROM users")
    else:
        cur.execute("SELECT id, name, ROW_NUMBER() OVER (PARTITION BY name ORDER BY id) AS rn FROM users ORDER BY id")
        rows = cur.fetchall()
        assert rows == [(1, "A", 1), (2, "A", 2), (3, "B", 1)]
    conn.close()
