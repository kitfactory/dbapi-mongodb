import pytest

from mongo_dbapi.errors import MongoDbApiError
from mongo_dbapi.translation import parse_sql


def _raises(code: str, sql: str) -> None:
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql(sql)
    assert code in str(exc.value)


def test_non_equi_join_rejected():
    _raises("[mdb][E2]", "SELECT * FROM users u JOIN orders o ON u.id > o.user_id")


def test_full_outer_join_rejected():
    _raises("[mdb][E2]", "SELECT * FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id")


def test_union_distinct_rejected():
    _raises("[mdb][E2]", "SELECT id FROM users UNION SELECT id FROM users")


def test_window_rank_rejected():
    _raises("[mdb][E2]", "SELECT id, RANK() OVER (ORDER BY id) FROM users")


def test_correlated_subquery_rejected():
    _raises("[mdb][E2]", "SELECT id FROM users u WHERE EXISTS (SELECT 1 FROM users x WHERE x.id = u.id)")


def test_named_param_shortage_rejected():
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql("SELECT * FROM users WHERE id = %(id)s", params={"other": 1})
    assert "[mdb][E4]" in str(exc.value)


def test_named_param_surplus_rejected():
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql("SELECT * FROM users WHERE id = %(id)s", params={"id": 1, "extra": 2})
    assert "[mdb][E4]" in str(exc.value)


def test_unknown_statement_rejected():
    _raises("[mdb][E2]", "MERGE INTO users USING dual ON (1=1) WHEN MATCHED THEN UPDATE SET name = 'x'")


def test_window_row_number_without_partition_parses():
    parts = parse_sql("SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM users")
    assert parts.uses_window is True


def test_select_simple():
    parts = parse_sql("SELECT id, name FROM users WHERE id = %s", params=(1,))
    assert parts.operation == "find"
    assert parts.collection == "users"
    assert parts.filter == {"id": 1}
    assert parts.projection_paths == [("id", "id"), ("name", "name")]


def test_insert_simple():
    parts = parse_sql(
        "INSERT INTO users (id, name) VALUES (%(id)s, %(name)s)",
        params={"id": 1, "name": "Alice"},
    )
    assert parts.operation == "insert"
    assert parts.collection == "users"
    assert parts.values == {"id": 1, "name": "Alice"}


def test_update_with_where():
    parts = parse_sql(
        "UPDATE users SET name = %(name)s WHERE id = %(id)s",
        params={"id": 1, "name": "Bob"},
    )
    assert parts.operation == "update"
    assert parts.update == {"$set": {"name": "Bob"}}
    assert parts.filter == {"id": 1}


def test_delete_with_where():
    parts = parse_sql("DELETE FROM users WHERE id = %(id)s", params={"id": 1})
    assert parts.operation == "delete"
    assert parts.filter == {"id": 1}


def test_where_like_ilike_regex():
    parts_like = parse_sql("SELECT * FROM users WHERE name LIKE %(name)s", params={"name": "A%"})
    assert "$regex" in parts_like.filter.get("name", {})
    parts_ilike = parse_sql("SELECT * FROM users WHERE name ILIKE %(name)s", params={"name": "alice%"})
    assert "$regex" in parts_ilike.filter.get("name", {})
    parts_regex = parse_sql("SELECT * FROM users WHERE name REGEXP '/Al.*ce/'")
    assert "$regex" in parts_regex.filter.get("name", {})


def test_join_inner_and_left():
    parts_inner = parse_sql(
        "SELECT u.id, o.id as oid FROM users u JOIN orders o ON u.id = o.user_id"
    )
    assert parts_inner.operation == "aggregate"
    assert any("$lookup" in stage for stage in parts_inner.pipeline or [])
    assert ("__join0.id", "oid") in (parts_inner.projection_paths or [])
    parts_left = parse_sql(
        "SELECT u.id, o.id FROM users u LEFT JOIN orders o ON u.id = o.user_id"
    )
    assert any("$lookup" in stage for stage in parts_left.pipeline or [])


def test_group_by_and_having():
    parts = parse_sql("SELECT user_id, COUNT(*) AS cnt FROM orders GROUP BY user_id HAVING cnt > 1")
    assert parts.operation == "aggregate"
    assert any("$group" in stage for stage in parts.pipeline or [])
    assert any("$match" in stage for stage in parts.pipeline or [])


def test_sum_case_aggregate():
    parts = parse_sql(
        "SELECT user_id, SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_count FROM tasks GROUP BY user_id"
    )
    assert parts.operation == "aggregate"
    cond_sum = None
    for stage in parts.pipeline or []:
        if "$group" in stage:
            cond_sum = stage["$group"].get("done_count")
            break
    assert cond_sum and "$cond" in cond_sum.get("$sum", {})


def test_having_sum_without_alias():
    parts = parse_sql("SELECT user_id, SUM(total) FROM orders GROUP BY user_id HAVING SUM(total) >= 100")
    assert parts.operation == "aggregate"
    assert any("$group" in stage for stage in parts.pipeline or [])
    assert any("$match" in stage for stage in parts.pipeline or [])


def test_having_with_aggregate_alias():
    parts = parse_sql("SELECT user_id, SUM(total) AS total_sum FROM orders GROUP BY user_id HAVING total_sum >= 100")
    assert parts.operation == "aggregate"
    assert any("$match" in stage for stage in parts.pipeline or [])


def test_union_all_parts():
    parts = parse_sql("SELECT id FROM users UNION ALL SELECT id FROM archived_users")
    assert parts.operation == "union_all"
    assert parts.union_parts and len(parts.union_parts) == 2


def test_from_subquery_inline_token():
    parts = parse_sql("SELECT * FROM (SELECT id, name FROM users WHERE id > 0) AS t")
    assert parts.operation == "from_subquery"
    assert parts.inline_token is not None
    assert parts.subqueries


def test_where_in_subquery_registered():
    parts = parse_sql("SELECT id FROM users WHERE id IN (SELECT user_id FROM orders)")
    assert parts.subqueries
    token, sub = next(iter(parts.subqueries.items()))
    assert token.startswith("__subquery_")
    assert sub.get("mode") in ("in", "values")


def test_window_row_number_with_partition_and_order():
    parts = parse_sql(
        "SELECT user_id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at) AS rn FROM events"
    )
    assert parts.uses_window is True
    assert parts.operation == "aggregate"

def test_param_shortage_rejected():
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql("SELECT * FROM users WHERE id = %s", params=None)
    assert "[mdb][E4]" in str(exc.value)


def test_param_surplus_rejected():
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql("SELECT * FROM users WHERE id = %s", params=(1, 2))
    assert "[mdb][E4]" in str(exc.value)
