# GSB Bugfix Task
## 题面
媒体网关批量建终端的那天下午，呼叫开始单通，抓包发现事务回复和请求对不上号。Transaction ID 在上一条没确认前就被复用，响应落到了新事务上；一个 Transaction 里带三个 Action，中间那个失败时只回一个错误，前后两个已经生效；Context 创建和 Add 并发执行，终端挂到了别人的 Context；终止 ID 的大小写和前导零没有归一，同一终端被建了两遍；AuditValue 返回的终端列表被截断，网关把仍在通话的终端一起删了；事件描述符里的 keepalive 没有回应，网关判定控制器失联；重传的 Transaction 被再次执行，放音放了两次；二进制编码和文本编码混用时解析器按文本读，整段乱码；空 Context 没有及时删除，资源慢慢泄漏；紧急呼叫标记在转发时丢失，调度顺序错乱；服务变更重启后没有重新注册，网关一直不可用。megaco_transaction_broker.py 的 MegacoTransactionBroker 要让同一 Transaction ID 的重复请求只执行一次并回同一份结果，多个 Action 要么全部成功要么整体回退，Context 与终端绑定加锁，编码方式从消息头判定后统一解析，重启后重新注册并核对在途事务，在途请求按事务编号幂等。网关自带的 test_megaco_transaction_broker.py 十五条用例跑出来一片失败。

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
