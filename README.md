# GSB Bugfix Task
## 题面
办公楼换 WPA3 以后，会议室里的笔记本一天要重连十几次，抓空口能看到大量 Commit 帧。反 clogging 令牌的校验缺位，任何人灌 Commit 都能让 AP 算一轮椭圆曲线；同一个终端的 Commit 被原样重放，AP 又生成一份会话密钥；组号不在允许列表里也继续算，终端被降到了弱组；Commit 里的标量长度和元素长度不合法时解析器接着往后读，越界读到了下一个帧；Confirm 的发送方和接收方标识没有比对，确认被错配到另一台终端；两个终端用同一个 MAC 并发接入，状态表互相覆盖；握手超时用单调时钟计时，但进程重启后计数清零，半开状态一直留着；PMKSA 缓存没有绑定 SSID 和 PMKID，跨网络直接复用；重传的 Confirm 被视作新消息，密钥被安装两次；密码错误和握手超时返回的错误码不同，可以拿来枚举；切换组以后旧 SAE 状态没清，重连时还用旧的椭圆曲线点。sae_commit_scheduler.py 的 SaeCommitScheduler 要对 Commit 和 Confirm 按终端加锁并做重放幂等，组号和长度非法时抛出精确异常，半开状态带过期时间且重启后能清理，密钥安装与状态迁移放在同一步完成。test_sae_commit_scheduler.py 的十五项检查我换了两块网卡复现，结果一样全挂。

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
