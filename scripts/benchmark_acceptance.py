from __future__ import annotations

import argparse
import os
import random
import statistics
import time
from datetime import datetime, timedelta
from typing import Any

import pymongo

from mongo_dbapi import connect


def _now_utc() -> datetime:
    return datetime.utcnow()


def _median_ms(values_s: list[float]) -> float:
    return statistics.median(values_s) * 1000.0


def _seed_data(uri: str, db_name: str, *, tenants: int, users: int, orders: int, seed: int) -> None:
    """
    Seed benchmark data / ベンチ用データ投入

    Notes / 注意:
    - This script is for local benchmarking only / ローカル計測用です
    - It drops the target collections / 対象コレクションを削除します
    """
    rng = random.Random(seed)
    client = pymongo.MongoClient(uri)
    db = client[db_name]

    db["users"].drop()
    db["orders"].drop()
    db["archived_users"].drop()

    base_time = _now_utc() - timedelta(days=60)

    # Make documents "wide" to reflect real payload / 実データに近い payload を想定して幅を持たせる
    pad_small = "x" * 64
    pad_large = "y" * 256

    user_docs: list[dict[str, Any]] = []
    for user_id in range(1, users + 1):
        tenant_id = (user_id % tenants) + 1
        name_prefix = "A" if (user_id % 10) < 4 else "B"
        user_docs.append(
            {
                "id": user_id,
                "tenant_id": tenant_id,
                "name": f"{name_prefix}user{user_id}",
                "created_at": base_time + timedelta(minutes=user_id),
                "meta1": pad_large,
                "meta2": pad_small,
            }
        )
    if user_docs:
        db["users"].insert_many(user_docs)

    archived_docs = [{"id": u["id"], "tenant_id": u["tenant_id"], "name": u["name"]} for u in user_docs[::3]]
    if archived_docs:
        db["archived_users"].insert_many(archived_docs)

    statuses = ["new", "done", "cancelled", "archived"]
    hot_user_id = 1
    order_docs: list[dict[str, Any]] = []
    for order_id in range(1, orders + 1):
        # Create a "hot" user with many orders to make per-user pagination benchmarks stable /
        # 注文が多い「ホットユーザー」を作り、ユーザー内ページングのベンチを安定させる
        if order_id % 8 == 0:
            user_id = hot_user_id
        else:
            user_id = rng.randint(1, users)
        tenant_id = (user_id % tenants) + 1
        created_at = base_time + timedelta(seconds=rng.randint(0, 60 * 60 * 24 * 30))
        total: int | None
        if order_id % 17 == 0:
            total = None
        else:
            total = rng.randint(0, 200)
        order_docs.append(
            {
                "id": order_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "status": rng.choice(statuses),
                "total": total,
                "created_at": created_at,
                "payload": pad_large,
                "payload2": pad_small,
            }
        )
        if len(order_docs) >= 2000:
            db["orders"].insert_many(order_docs)
            order_docs.clear()
    if order_docs:
        db["orders"].insert_many(order_docs)

    # Ensure EXISTS() is true for tenant=2 & status=new / EXISTS用に tenant=2,status=new を保証
    db["orders"].insert_one(
        {
            "id": orders + 1,
            "user_id": hot_user_id,
            "tenant_id": 2,
            "status": "new",
            "total": 100,
            "created_at": base_time,
            "payload": pad_large,
            "payload2": pad_small,
        }
    )

    # Indexes / インデックス
    db["users"].create_index([("tenant_id", 1), ("id", 1)], name="idx_users_tenant_id_id")
    db["users"].create_index([("id", 1)], name="idx_users_id")
    db["users"].create_index([("name", 1)], name="idx_users_name")
    db["orders"].create_index([("user_id", 1)], name="idx_orders_user_id")
    db["orders"].create_index([("status", 1)], name="idx_orders_status")
    db["orders"].create_index([("tenant_id", 1), ("status", 1)], name="idx_orders_tenant_status")
    db["orders"].create_index([("created_at", -1)], name="idx_orders_created_at")
    db["orders"].create_index(
        [("tenant_id", 1), ("user_id", 1), ("created_at", -1), ("id", 1)],
        name="idx_orders_tenant_user_created_id",
    )
    db["orders"].create_index(
        [("tenant_id", 1), ("created_at", -1), ("id", 1)],
        name="idx_orders_tenant_created_id",
    )
    db["archived_users"].create_index([("tenant_id", 1), ("id", 1)], name="idx_arch_users_tenant_id_id")


def _run_sql(uri: str, db_name: str, sql: str, params: tuple[Any, ...], *, repeat: int) -> tuple[list[float], int]:
    conn = connect(uri, db_name)
    try:
        # Warmup / ウォームアップ
        cur = conn.cursor()
        cur.execute(sql, params)
        warm_rows = cur.fetchall()

        times: list[float] = []
        for _ in range(repeat):
            cur = conn.cursor()
            t0 = time.perf_counter()
            cur.execute(sql, params)
            rows = cur.fetchall()
            times.append(time.perf_counter() - t0)
            if len(rows) != len(warm_rows):
                raise RuntimeError("Rowcount mismatch between runs / 実行間で行数が一致しません")
        return times, len(warm_rows)
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark acceptance SQL (MongoDB 4.4) / 受け入れSQLの簡易ベンチ")
    parser.add_argument("--uri", default=os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27019"))
    parser.add_argument("--db", default=os.environ.get("MONGODB_DB", "mongo_dbapi_bench"))
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--tenants", type=int, default=10)
    parser.add_argument("--users", type=int, default=10_000)
    parser.add_argument("--orders", type=int, default=50_000)
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--no-seed", action="store_true", help="Skip seeding data / データ投入を省略")
    args = parser.parse_args()

    if not args.no_seed:
        print(f"[bench] seeding: db={args.db} users={args.users} orders={args.orders} tenants={args.tenants}")
        _seed_data(args.uri, args.db, tenants=args.tenants, users=args.users, orders=args.orders, seed=args.seed)

    cases: list[tuple[str, str, tuple[Any, ...]]] = [
        (
            "A1_detail",
            "SELECT o.id, o.total, o.created_at "
            "FROM orders o "
            "WHERE o.tenant_id = %s AND o.user_id = %s "
            "ORDER BY o.created_at DESC, o.id ASC LIMIT %s OFFSET %s",
            (2, 1, 50, 0),
        ),
        (
            "A2_search",
            "SELECT DISTINCT u.id "
            "FROM users u JOIN orders o ON u.id = o.user_id "
            "WHERE u.tenant_id = %s AND u.name ILIKE %s AND o.status NOT IN (%s, %s) "
            "ORDER BY u.id LIMIT %s OFFSET %s",
            (2, "a%", "cancelled", "archived", 200, 0),
        ),
        (
            "A4_ilike_contains",
            "SELECT u.id "
            "FROM users u "
            "WHERE u.tenant_id = %s AND u.name ILIKE %s "
            "ORDER BY u.id LIMIT %s OFFSET %s",
            (2, "%user1%", 200, 0),
        ),
        (
            "A5_deep_offset",
            "SELECT o.id, o.created_at "
            "FROM orders o "
            "WHERE o.tenant_id = %s "
            "ORDER BY o.created_at DESC, o.id ASC LIMIT %s OFFSET %s",
            (2, 50, 5000),
        ),
        (
            "A11_exists",
            "SELECT u.id "
            "FROM users u "
            "WHERE u.tenant_id = %s AND EXISTS (SELECT id FROM orders o WHERE o.tenant_id = %s AND o.status = %s LIMIT 1) "
            "ORDER BY u.id LIMIT %s",
            (2, 2, "new", 500),
        ),
        (
            "A12_join_composite",
            "SELECT o.id, o.created_at, u.name "
            "FROM orders o JOIN users u ON o.user_id = u.id AND o.tenant_id = u.tenant_id "
            "WHERE o.tenant_id = %s "
            "ORDER BY o.created_at DESC, o.id ASC LIMIT %s OFFSET %s",
            (2, 50, 0),
        ),
        (
            "A13_union_offset",
            "SELECT id FROM users WHERE tenant_id = %s "
            "UNION ALL SELECT id FROM archived_users WHERE tenant_id = %s "
            "ORDER BY id LIMIT %s OFFSET %s",
            (2, 2, 500, 100),
        ),
        (
            "A14_distinct_multi",
            "SELECT DISTINCT tenant_id, status "
            "FROM orders "
            "WHERE tenant_id IN (%s, %s) "
            "ORDER BY tenant_id, status LIMIT %s OFFSET %s",
            (1, 2, 100, 0),
        ),
        (
            "A15_count_distinct",
            "SELECT COUNT(DISTINCT user_id) AS uniq_users FROM orders WHERE tenant_id = %s",
            (2,),
        ),
        (
            "A16_join_group_by",
            "SELECT u.id, COUNT(o.id) AS order_cnt "
            "FROM users u LEFT JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id "
            "WHERE u.tenant_id = %s "
            "GROUP BY u.id HAVING order_cnt >= %s ORDER BY order_cnt DESC, u.id ASC LIMIT %s OFFSET %s",
            (2, 1, 200, 0),
        ),
        (
            "A17_correlated_exists",
            "SELECT u.id "
            "FROM users u "
            "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id AND o.tenant_id = u.tenant_id) "
            "ORDER BY u.id LIMIT %s",
            (500,),
        ),
        (
            "A18_union_distinct",
            "SELECT tenant_id FROM users WHERE tenant_id IN (%s, %s, %s) "
            "UNION "
            "SELECT tenant_id FROM archived_users WHERE tenant_id IN (%s, %s, %s) "
            "ORDER BY tenant_id LIMIT %s OFFSET %s",
            (1, 2, 3, 1, 2, 3, 50, 0),
        ),
        (
            "A19_join_non_equi",
            "SELECT u.id, o.id "
            "FROM users u JOIN orders o ON u.id = o.user_id AND o.total >= %s "
            "WHERE u.tenant_id = %s "
            "ORDER BY u.id, o.id LIMIT %s OFFSET %s",
            (50, 2, 200, 0),
        ),
        (
            "A23_right_outer_join",
            "SELECT u.id, o.id "
            "FROM users u RIGHT OUTER JOIN orders o ON u.id = o.user_id "
            "ORDER BY o.id LIMIT %s OFFSET %s",
            (200, 0),
        ),
        (
            "A24_right_outer_join_composite",
            "SELECT u.id, o.id "
            "FROM users u RIGHT OUTER JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id "
            "WHERE o.tenant_id = %s "
            "ORDER BY o.id LIMIT %s OFFSET %s",
            (2, 200, 0),
        ),
        (
            "A25_full_outer_join",
            "SELECT u.id AS uid, o.id AS oid "
            "FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id "
            "ORDER BY oid LIMIT %s OFFSET %s",
            (200, 0),
        ),
        (
            "A26_full_outer_join_composite",
            "SELECT u.id AS uid, o.id AS oid "
            "FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id "
            "ORDER BY uid, oid LIMIT %s OFFSET %s",
            (200, 0),
        ),
        (
            "A27_full_outer_join_group_by",
            "SELECT COALESCE(u.tenant_id, o.tenant_id) AS tenant_id, COUNT(*) AS cnt "
            "FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id "
            "GROUP BY COALESCE(u.tenant_id, o.tenant_id) "
            "ORDER BY tenant_id LIMIT %s",
            (200,),
        ),
        (
            "U1_union_all",
            "SELECT id FROM users WHERE tenant_id = %s UNION ALL SELECT id FROM archived_users WHERE tenant_id = %s ORDER BY id LIMIT %s",
            (2, 2, 500),
        ),
    ]

    for name, sql, params in cases:
        print(f"\n[bench] {name}")
        for opt in (0, 1):
            os.environ["MONGO_DBAPI_OPTIMIZE"] = "1" if opt == 1 else "0"
            times, rows = _run_sql(args.uri, args.db, sql, params, repeat=args.repeat)
            print(
                f"  optimize={opt} median={_median_ms(times):.1f}ms "
                f"min={min(times)*1000.0:.1f}ms max={max(times)*1000.0:.1f}ms rows={rows}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
