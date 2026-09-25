# 临床试验随机分配与盲法服务

仅使用 Python 3.11+ 标准库的独立随机化服务。支持分层区组随机、试验方案锁定、隐藏分组、外部编号并发幂等、中心隔离、双人揭盲、用药前随机号替换复核和审计。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8104>，默认数据库 `randomization.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`site1`、`site2`（研究中心），`coord`（协调员），`monitor1`、`monitor2`（监查员）。

## 主要接口

- `POST /api/trials`：创建草稿试验，指定分组、分层因素、区组长度和随机种子。
- `POST /api/trials/{id}/protocol`：入组前修改方案；一旦入组即锁定。
- `POST /api/trials/{id}/start`：开始入组。
- `POST /api/trials/{id}/enroll`：按当前用户中心入组；响应只返回分配编号，不返回分组。
- `GET /api/trials/{id}/participants`：分中心返回数据，中心用户看不到其他中心。
- `POST /api/participants/{id}/unblinding-requests`：发起揭盲。
- `POST /api/unblinding-requests/{id}/approve`：两人独立审批；同一人不能审批两次。
- `POST /api/participants/{id}/replacement-requests`：用药前随机号损坏时发起替换，须写明原因（≥8 字）；发起后原分配立即冻结（受试者信息中 `allocation_frozen=true`），同一受试者仅允许一条待复核申请。
- `POST /api/replacement-requests/{id}/review`：由**非发起人**的监查员复核，body 为 `{"decision":"approve"|"reject"}`。通过则从**同一分层**未启用编号补发新随机号（`old_code`/`new_code` 均为盲态编号，全程不返回试验组），原编号保持占用、作废且永不复用；不通过则关闭申请并恢复原编号。
- `GET /api/trials/{id}/replacement-requests`：按试验查看替换处理记录（中心用户仅见本中心），含受试者、原编号、新编号、原因、复核人和处理结论。
- `GET /api/participants/{id}/replacement-requests`、`GET /api/replacement-requests/{id}`：按受试者或申请查看处理记录。
- `GET /api/trials/{id}/summary`：中心级汇总和审计记录（`replacement.request/approve/reject` 全程留痕）。

随机表按“试验种子 + 中心 + 分层因素”确定性生成，每个区组为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
