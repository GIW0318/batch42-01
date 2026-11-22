# GSB Bugfix Task
## 题面
跳板机上的长连接最近老在大文件传到一半时断开，运维换了几版客户端都一样。传输量到了 rekey 阈值，服务端在收到对端 KEXINIT 之前还在继续发应用数据，抓包里被标成协议违规；重协商窗口里新旧密钥混着用，MAC 校验偶发失败，客户端报 message authentication code incorrect；rekey 之后序列号从零重算，只要前面丢过一个包，后面的包 MAC 全对不上；同一条连接上客户端和服务端几乎同时发起 rekey，共享的 KEX 上下文被后到的覆盖，两边协商结果对不上；选算法时把双方列表取了并集，客户端列了弱算法，最后选中 diffie-hellman-group1-sha1；重协商超时后连接停在半开，sshd 进程还在但套接字已经不可用；rekey 期间 channel 窗口没有冻结，对端按旧窗口继续发，数据流错位；重协商途中收到 CHANNEL_CLOSE，通道结构没释放，反复重连后句柄泄漏；rekey 计数器用 32 位，跑满一个月回绕成零，之后再也不会触发重协商；主机密钥轮换后旧密钥还能通过 known_hosts 校验；五百个并发长连接同时 rekey 时 CPU 打满，握手排队超过客户端超时；调试日志把完整 KEX 载荷和会话标识写进了磁盘。ssh_kex_rekey_guard.py 的 SshRekeyGuard 得管住重协商的时序和状态。四个硬门禁：重协商期间应用数据必须暂停到 NEWKEYS 交换完成，提前发就算违规；同一条连接并发触发 rekey 只允许一个成功，另一个要拿到明确失败；双方算法必须取交集，交集里没有强算法时以 no matching key exchange method 断开；rekey 时通道窗口必须冻结并在完成后按新密钥重新计算。test_ssh_kex_rekey_guard.py 的十五条用例我昨晚连着跑到天亮，没有一条转绿。

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
