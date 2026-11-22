# GSB Bugfix Task
## 题面
运维跳板机这周被安全组点名，说限制形同虚设。关掉 -L 以后 -D 动态转发照样能用；-R 远程转发绑定 0.0.0.0 没人管，内网端口直接被暴露；目标地址写 127.1、0.0.0.0、::1 这些变体就绕过了黑名单；策略热更新后已经建立的转发还活着，违规通道继续跑；同一用户并发开五百个转发没有上限，sshd 内存被打满；转发通道打开失败后 channel 没关闭，fd 一直泄漏；授权时解析域名一次，真正连接时又解析一次，DNS rebinding 能绕过；走 Unix socket 的转发完全不经过 IP 策略；管理员组豁免规则大小写敏感，组名写错导致全员放行；审计只记了目标端口没记主机，出了事查不到去向；agent forwarding 和 port forwarding 组合起来还能绕过限制；重启后转发会话恢复但没有重新鉴权。ssh_forwarding_policy.py 的 ForwardPolicyGate 要在通道打开阶段就把策略判掉。硬性要求：被禁的转发必须在 channel open 阶段拒绝并回 SSH_OPEN_ADMINISTRATIVELY_PROHIBITED；热更新后已有违规转发必须在下一次数据前关闭；单用户并发转发上限必须精确拒绝；域名必须在连接时重新校验解析结果。test_ssh_forwarding_policy.py 的十五条我换了两台机器跑，结果都是全失败。

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
