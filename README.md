# 水文观测明细表 · 快照小文件回收服务

小文件合并后，湖仓里会留下旧快照引用、但已经不需要的 Parquet 文件。这个
FastAPI 服务负责在**不打断正在运行的导出**的前提下回收它们。

## 安全规则

清单（`manifest.json`）中的文件，只有同时满足下面两个条件才会被删除：

1. 没有任何**未退役（保留）**的快照引用它；
2. 没有任何**未完成**的导出登记引用它。

多个快照可以共用同一个文件。清理按集合计算引用：只要还有一个保留快照或
未完成导出引用该文件，它就会被保护。

### 退役与登记的并发语义

- 退役后不再接受该快照的**新**登记（返回 `409`）。
- 登记和退役各自在 SQLite `BEGIN IMMEDIATE` 写事务中完成：
  - 登记先提交：退役可以继续，但这条已成功的登记会一直保护其文件，直到
    导出调用 `release`；
  - 退役先提交：登记看到退役状态并被拒绝。
- 清理在持有写锁期间完成「计算可删集合 + 执行 unlink」，登记无法在决策与
  删除之间插入，因此不会出现"登记成功但文件已被删"的窗口。

### 失败与重启

- 退役状态和导出登记都保存在 SQLite 中。
- 删除文件失败只记入本次响应的 `failed`/`failures`，**不会撤销退役**，
  其它文件继续处理；修复磁盘问题后再次调用 `POST /cleanup` 即可重试。
- 服务重启后状态仍在（Docker Compose 使用命名卷持久化）。
- 已经被删除、但清单里仍列出的文件会进入 `already_absent`，重复清理是幂等的。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

- Swagger UI：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>

首次启动会把镜像内的样例 Parquet 文件复制到数据卷 `/app/data`，同一卷中
还保存 `cleanup.db`。重启容器不会重新复制文件，也不会丢失退役/登记状态。

重置样例（清空数据卷）：

```bash
docker compose down -v
docker compose up --build
```

## Swagger 演示：导出结束前后分别能删哪些文件

样例清单包含三个快照：

| 快照 | 文件 |
| --- | --- |
| `snap-20261101-night` | `hydro/2026-11-01/station_a.parquet`、`.../station_b.parquet`、`hydro/shared/quality.parquet` |
| `snap-20261102-night` | `hydro/2026-11-02/station_a.parquet`、`.../station_b.parquet`、`hydro/shared/quality.parquet` |
| `snap-20261102-noon` | `hydro/2026-11-02/station_c.parquet`、`hydro/shared/quality.parquet`、`hydro/shared/station_meta.parquet` |

在 Swagger 中按顺序操作：

1. **登记一个"正在运行"的导出**：`POST /exports/register`

   ```json
   {"snapshot_id": "snap-20261101-night", "export_id": "nightly-export-001"}
   ```

   记下响应中的 `registration_id`。

2. **退役三个快照**（可重复调用，退役是幂等的）：

   ```
   POST /snapshots/snap-20261101-night/retire
   POST /snapshots/snap-20261102-night/retire
   POST /snapshots/snap-20261102-noon/retire
   ```

3. **导出结束前预览**：`GET /cleanup/preview`

   ```json
   {
     "deletable": [
       "hydro/2026-11-02/station_a.parquet",
       "hydro/2026-11-02/station_b.parquet",
       "hydro/2026-11-02/station_c.parquet",
       "hydro/shared/station_meta.parquet"
     ],
     "protected_by_active_exports": [
       "hydro/2026-11-01/station_a.parquet",
       "hydro/2026-11-01/station_b.parquet",
       "hydro/shared/quality.parquet"
     ]
   }
   ```

   导出登记的快照虽然已退役，但其文件（含共用文件 `shared/quality.parquet`）
   仍被未完成的导出保护。

4. **执行清理**：`POST /cleanup`（空 body 或 `{"dry_run": false}`）

   返回 `deleted` 为上面的 4 个文件，`failed` 为空。此时 2026-11-01 的
   两个文件和共用的 `quality.parquet` 仍在磁盘上。

5. **导出完成，释放登记**：

   ```
   POST /exports/{registration_id}/release
   ```

6. **再次预览**：`GET /cleanup/preview`，`deletable` 变为：

   ```json
   [
     "hydro/2026-11-01/station_a.parquet",
     "hydro/2026-11-01/station_b.parquet",
     "hydro/shared/quality.parquet"
   ]
   ```

7. **再次清理**：`POST /cleanup`，删除上面 3 个文件；再预览时 `deletable`
   为空。

`POST /cleanup?dry_run=true` 等同于只预览、不删除。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/snapshots` | 列出清单快照、退役状态、活跃导出数 |
| `GET` | `/snapshots/{snapshot_id}` | 单个快照详情 |
| `POST` | `/snapshots/{snapshot_id}/retire` | 退役快照，之后不接受新登记 |
| `POST` | `/exports/register` | 导出按 `snapshot_id` 登记，返回登记 ID |
| `POST` | `/exports/{registration_id}/release` | 导出完成后释放自己的登记（幂等） |
| `GET` | `/exports?active_only=true` | 列出登记（默认只看未完成） |
| `GET` | `/cleanup/preview` | 预览可删/受保护文件 |
| `POST` | `/cleanup` | 执行清理；body 可带 `{"dry_run": true}` |
| `GET` | `/health` | 健康检查 |

`POST /cleanup` 的响应包含：

- `deleted`：本次真正删除的路径；
- `already_absent`：清单中列出、但磁盘上已不存在的路径；
- `failed`：删除失败的路径；
- `failures`：失败路径及错误原因；
- `deletable` / `protected*` / `active_exports`：本次决策依据，便于值班员排查。

## 清单格式

服务每次请求都会读取 `MANIFEST_PATH` 指向的本地清单。标准格式：

```json
{
  "snapshots": [
    {"snapshot_id": "snap-a", "files": ["hydro/a.parquet", "hydro/shared.parquet"]},
    {"snapshot_id": "snap-b", "files": ["hydro/b.parquet", "hydro/shared.parquet"]}
  ]
}
```

也兼容 `snapshots` 使用 `id`/`name`、文件使用 `paths`/`data_files` 或对象
`{"relative_path": ...}` 等常见写法；路径必须是数据目录内的相对路径，
绝对路径和 `..` 会被拒绝。

## 本地运行（不用 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

本地默认直接把仓库里的 `sample_data/` 当作数据目录，SQLite 在
`local_state/cleanup.db`。演示删除后可恢复样例文件：

```bash
git checkout -- sample_data
rm -rf local_state
```

## 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MANIFEST_PATH` | `./manifest.json` | 本地清单路径 |
| `DATA_DIR` | `./sample_data` | Parquet 文件根目录（删除只发生在这里） |
| `CLEANUP_DB_PATH` | `./local_state/cleanup.db` | SQLite 状态库 |
| `SAMPLE_DATA_DIR` | `./sample_data` | 容器内只读样例目录 |
| `SEED_SAMPLE_DATA_ON_START` | `0` | 为 `1` 时把样例复制到 `DATA_DIR`（仅一次） |

## 测试

```bash
PYTHONPATH=. python3 -m unittest tests.test_service -v   # 服务逻辑/并发/重启
pytest -q                                                # 含 HTTP API 测试
```
