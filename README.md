# GSB Bugfix Task
## 题面
告警平台被同一台核心交换机的 Trap 刷了一晚上，值班同学以为设备反复重启。engineID 和 context 没有进入去重键，不同引擎发来的相同 OID 被合成一条；Inform 重传没有按 request-id 去重，同一事件入库四次；Trap 里的 uptime 回绕后排序错乱，新旧事件顺序颠倒；SNMPv3 认证只比对消息前八个字节，尾部被篡改也能通过；落在时间窗口之外的 Trap 被丢掉，但丢弃计数没有增加；同一条 Trap 的 varbind 顺序不同，去重键就算出两个；去重缓存没有容量上限，扫描器用随机 OID 很快把内存打满；引擎发现阶段接受任意 engineID，伪造设备可以注册进来；告警级别映射表缺项时默认成最高级，噪声被放大；一批 Trap 解析到第三条失败，前两条已经入库；进程重启后去重窗口清空，同一批告警又报一遍。snmp_trap_deduper.py 的 SnmpTrapDeduper 要用引擎、上下文、OID 和 varbind 规范化后组成去重键，批量处理要么全部入库要么全部回退，窗口和计数要落盘并在重启后恢复，认证失败和格式错误分别抛出可区分的异常。告警侧的 test_snmp_trap_deduper.py 十五条检查现在全是红的。

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
