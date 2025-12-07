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
    db["addresses"].delete_many({})
    db["cities"].delete_many({})
    db["orm_users"].delete_many({})
    yield
    db[COLLECTION].delete_many({})
    db["orders"].delete_many({})
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


def test_union_without_all_is_rejected():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (10, "X"))
    with pytest.raises(MongoDbApiError) as exc:
        cur.execute("SELECT id FROM users UNION SELECT id FROM users")
    assert "[mdb][E2]" in str(exc.value)
    conn.close()


def test_non_equi_join_is_rejected():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError) as exc:
        cur.execute("SELECT * FROM users u JOIN orders o ON u.id > o.user_id")
    assert "[mdb][E2]" in str(exc.value)
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


def test_sqlalchemy_union_distinct_is_rejected():
    engine = create_engine(f"{DBAPI_URI}/{MONGODB_DB}")
    metadata = MetaData()
    users = Table("core_users5", metadata, Column("id", Integer, primary_key=True))
    metadata.drop_all(engine)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(users.insert(), [{"id": 1}, {"id": 2}])
        with pytest.raises(Exception) as exc:
            stmt = select(users.c.id).where(users.c.id == 1).union(select(users.c.id).where(users.c.id == 2))
            conn.execute(stmt).all()
    assert "Unsupported SQL construct" in str(exc.value)
    metadata.drop_all(engine)


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


def test_full_outer_join_is_rejected():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError):
        cur.execute("SELECT * FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id")
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


def test_correlated_subquery_is_rejected():
    conn = connect(MONGODB_URI, MONGODB_DB)
    cur = conn.cursor()
    with pytest.raises(MongoDbApiError):
        cur.execute("SELECT id FROM users u WHERE EXISTS (SELECT 1 FROM users x WHERE x.id = u.id)")
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
