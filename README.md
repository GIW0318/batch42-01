# GSB Bugfix Task
## 题面
一批服务账号改用内核 keyring 存数据库口令，运行几天后开始出现认证失败和权限告警。用户 keyring 配额满了以后添加条目静默失败，服务以为写成功了；同一描述并发添加，留下两个版本，读的时候随机拿到一个；权限位配错，同机其他用户能读服务凭据；进程退出后条目没回收，配额一点点漏光；setuid 之后还能访问原来的会话 keyring；有效期没执行，过期凭据一直能用；载荷超过上限被截断，服务拿到半个密钥；进程重启之后常驻 keyring 没恢复，服务起不来；撤销条目后引用计数没减，keyring 占着不放；递归搜索 keyring 时遇到环，死循环；并发读写同一个条目时内容撕裂；审计里没有条目序号，出事查不到是哪把。kernel_keyring_quota.py 的 KeyringQuotaBroker 要按 UID、会话和条目三层核算。门禁：配额超限必须返回 EDQUOT，不能静默；同描述并发添加只能保留一个明确版本；setuid 后不得访问原会话 keyring，必须回 EACCES；撤销必须原子失效全部引用。test_kernel_keyring_quota.py 的十五条检查现在从第一条红到最后一条，没有例外。

## 运行方式
```bash
python -m pytest tests -q
```

## 平台字段

所属领域：安全
子方向：系统安全
任务类型：Bugfix
语言 / 框架：Python 3, pytest
环境可复现等级：有外部依赖，未容器化
