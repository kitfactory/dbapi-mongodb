# 拡張機能対応チェックリスト（P1→P4）
旧版の詳細タスクは `docs/plan1.bck` に退避済み。拡張機能（P1: Core 強化 → P2: ORM 最小 CRUD → P3: async dialect → P4: Mongo 5+ ウィンドウ関数）を進めるための作業計画。

## 共通準備
- [x] 4.4 レプリカセットを起動（`PORT=27019 ./start4xdb.sh`）し、`MONGODB_URI`/`MONGODB_DB` を環境変数で指定
- [x] テスト環境の LD_LIBRARY_PATH（`mongodb-4.4/libssl1.1/usr/lib/x86_64-linux-gnu`）を確認

## P1: SQLAlchemy Core 強化（Table/Column CRUD/DDL/Index）
- [ ] Core 向け CRUD/DDL/Index の翻訳パスを実装（Table/Column ベース）
- [x] サブクエリ（WHERE IN/EXISTS）を先行実行し結果を置換する実通信テストを追加
- [ ] FROM サブクエリを先行実行する経路と実通信テストを追加
- [ ] UNION ALL、HAVING、等価 JOIN 多段（最大 3 段）の実通信テストを追加
- [ ] ILIKE/正規表現リテラル、名前付きパラメータ（dict）、型拡張（Decimal/UUID/tz datetime/Binary）の実装とテスト
- [ ] SQLAlchemy Core 経由の CRUD/DDL/Index 結合テストを 4.4 環境で通す

## P2: ORM 最小 CRUD
- [ ] 単一テーブル相当で PK を `_id` にマッピングし、INSERT/SELECT/UPDATE/DELETE が動く ORM 経路を実装
- [ ] ORM 経由の実通信テスト（リレーションなし）を追加

## P3: async dialect（Core CRUD 相当）
- [ ] Core CRUD/DDL/Index を async でラップする dialect を設計・実装
- [ ] 非同期実通信テスト（CRUD/DDL/Index）を 4.4 環境で追加

## P4: Mongo 5+ ウィンドウ関数（低優先度）
- [ ] Mongo 5+ 環境で `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)` を `$setWindowFields` に変換する経路を実装
- [ ] Mongo 5 未満では `[mdb][E2]` を返すことをテスト

## ドキュメント/仕様の更新
- [ ] 実装に合わせて `docs/spec.md` / `docs/architecture.md` / `README*` を更新
- [ ] 新規エラーメッセージが増える場合は Error ID を追記しテストで固定
