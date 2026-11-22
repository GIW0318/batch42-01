# GSB Bugfix Task
## 题面
信令网关割接后，M3UA 的 ASP 状态在 active 和 inactive 之间来回跳，短信中心丢了不少提交。Routing Context 没有参与 ASP 匹配，两个 AS 的流量被送到同一条偶联；ASP Active 重复到达时没有幂等处理，流量被分摊到两个 ASP；ASPM 里的拥塞级别没有解析，过载期间仍然全量投递；协议数据序号回绕后用有符号比较，乱序判断整个反了；网络外观不一致的偶联也被建立，两侧对链路类型理解不同；DUPU 参数长度越界时解析器继续读，读到了下一条消息；心跳超时只把 ASP 标成不可用，没有触发主备切换；同一个信令点被两个 AS 同时声明，路由选择随机；发送队列按优先级调度时低优先级长期得不到机会；进程重启后 AS 状态回到 inactive，但路由表还指着它；未配置的业务指示语被静默丢弃，上游收不到任何错误回执。m3ua_asps_relay.py 的 M3uaAspsRelay 要按 Routing Context 隔离 ASP 集合，Active 和 Inactive 按消息序号幂等，序号比较按无符号回绕规则，发送队列保证低优先级有保底配额，重启后从持久记录恢复 AS 状态并先核对路由再放流量。test_m3ua_asps_relay.py 的十五条断言眼下没有一条能立住。

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
