# 临床试验随机分配与盲法服务

仅使用 Python 3.11+ 标准库的独立随机化服务。支持分层区组随机、试验方案锁定、隐藏分组、外部编号并发幂等、中心隔离、双人揭盲和审计。

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
- `POST /api/participants/{id}/replacements`：受试者用药前随机号损坏时发起补发，须写明原因；提交后原编号立即冻结。
- `POST /api/allocation-replacements/{id}/review`：另一名监查员复核（发起人不能自审）。`approved` 时原编号永久作废，从同一分层未启用编号中按序补发；`rejected` 时恢复原编号。
- `GET /api/trials/{id}/replacements`：处理记录（受试者、原编号、新编号、原因、发起人、复核人、结论和时间），可按 `status` 筛选；中心用户只能看本中心。
- `GET /api/trials/{id}/summary`：中心级汇总和审计记录。

随机号损坏补发采用分配状态机：`available → used →（损坏发起）frozen →（复核通过）void /（复核驳回）used`。作废编号不再参与任何后续分配；补发编号由同分层 `available` 池中按序取得，补发全流程及页面只暴露随机编号，不暴露试验组。同一受试者存在待复核申请时禁止重复发起（部分唯一索引 + 事务双重防护）。

随机表按“试验种子 + 中心 + 分层因素”确定性生成，每个区组为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
