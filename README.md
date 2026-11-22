# GSB Bugfix Task
## 题面
渲染农场的 NFSv4.1 存储最近总在半夜出问题，早上来一批任务全是 I/O 错误。租约到期回收的时候客户端正好在续租，状态被删掉，应用直接收到 EIO；服务重启后 reclaim 窗口只给三十秒，几百个客户端来不及认领，OPEN 大面积失败；同一个 clientid 并发 SETCLIENTID，服务端落了两份状态，confirm 之后互相覆盖；delegreturn 之后文件锁没有释放，下一个客户端一直拿不到锁；回调超时后服务端没有回退到无 deleg 模式，客户端就等着；两个客户端并发 OPEN 同一个文件，state id 撞车；grace period 内新 OPEN 被错误接受，和 reclaim 抢同一个文件；导出路径大小写不同或者末尾多一个斜杠，就绕过了 export 策略；高负载下续租请求排队超过租约时间的一半，客户端提前判定过期；重启后 pNFS layout 状态没有恢复，读请求全挂在 layoutget；客户端崩溃后 state 泄漏，到上限以后新客户端全被拒；关闭导出时没有通知客户端，句柄悬空。nfs_export_lease.py 的 NfsLeaseKeeper 需要把租约、锁和 layout 当成一份状态来管。硬性约束：重启后 grace 期内只接受 reclaim，新 OPEN 必须回 NFS4ERR_GRACE；同一 clientid 并发 SETCLIENTID 只成功一次；租约到期必须原子回收文件锁、deleg 和 layout，不能只删一半；越权导出必须回 NFS4ERR_ACCESS。test_nfs_export_lease.py 那十五条断言眼下没有一条成立，租约和恢复两块全红。

## 运行方式
```bash
python -m pytest tests -q
```

## 平台字段

所属领域：安全
子方向：网络与通信安全
任务类型：Bugfix
语言 / 框架：Python 3, pytest
环境可复现等级：有外部依赖，未容器化
