# GSB Bugfix Task
## 题面
公司把内部服务从账号密码切到 Kerberos 单点登录后，跨机房访问开始间歇性失败，审计里还出现同一张票据被两个客户端使用的记录。不同 realm 的 TGT 被当成同一信任域接受，外部 realm 的 principal 只要后缀相同就能进；principal 没有按完整组件逐段比较，把 admin 和 admin/service 混成同一个主体；票据里的 kvno 和 KDC 当前密钥版本不一致也放行，轮换后的旧票据继续可用；重放缓存只按票据编号做键，没有把客户端地址和微秒时间戳算进去，同一张票据在缓存过期后还能再用；时钟偏差没有上下限，未来签发的票据和过期很久的票据都能过；PAC 的签名校验失败只记日志，权限组照样被信任；服务票据的地址列表为空时被当成不限制来源；跨 realm 的 referral 票据没有校验中转 realm 的信任链；keytab 轮换期间新旧 KVNO 没有重叠窗口，正常服务一起失败；同一 principal 的并发认证没有锁，重放缓存写入互相覆盖；委派票据没有限制目标服务，拿到一张票据能转发到任意后端；审计条目只有 principal，realm、kvno 和票据编号都没留下。kerberos_ticket_guard.py 的 KerberosTicketGuard 要把 realm、principal、kvno、重放缓存和 PAC 校验放在同一条校验路径上。硬门禁：同一票据编号加客户端地址只能成功一次，重复提交返回 replay；时钟偏差超过允许窗口必须拒绝；realm 和 principal 必须逐段完全匹配，不能按后缀或前缀；PAC 签名失败必须拒绝，不能降级成只记日志。这套 Kerberos 回归目前十五条断言全红，跨域信任和重放保护都没有通过。

## 运行方式
```bash
python -m pytest tests -q
```

## 平台字段

所属领域：安全
子方向：身份认证
任务类型：Bugfix
语言 / 框架：Python 3, pytest
环境可复现等级：有外部依赖，未容器化
