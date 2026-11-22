# GSB Bugfix Task
## 题面
周三凌晨做园区设备割接，两个运维同时开 NETCONF 会话改候选库，结果一台汇聚交换机的配置被整段冲掉。复现时看到 lock 只锁了 running 数据存储，candidate 仍可被另一个会话编辑；两个会话各自提交，后提交的那份把先提交的改动连根覆盖；confirmed-commit 的确认超时定在十分钟，可进程重启后计时器消失，未确认配置直接转正；commit 遇到第三个节点报错时，前两个节点已经落盘，没有按 rollback-on-error 回退；candidate 与 running 的差异把设备默认值也算了进去，一次 discard 把没动过的默认项写成显式配置；会话异常断开后持有的锁挂在树上不释放，后续运维全被挡在门外；copy-config 从 running 拉取时把私有密钥和 SNMP 团体串一起带进了候选库；discard-changes 与 commit 并发执行，候选库出现一半新一半旧；错误回复只带第一个失败节点，后面的 error-path 全丢了；配置版本号没有参与提交比对，拿着半小时前快照的会话照样提交成功。netconf_candidate_lock.py 的 NetconfCandidateLock 要让锁覆盖 candidate 和 running 两个存储并随会话释放，确认超时和未确认配置要能跨重启恢复，提交按配置版本做比较并在任一节点失败时整体回滚，错误回复要按节点逐条给出明确的 error-tag，同一会话号的重复提交按幂等处理。test_netconf_candidate_lock.py 这十五条回归今天从第一条红到最后一条。

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
