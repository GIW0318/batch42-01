# GSB Bugfix Task
## 题面
消息队列换成 SASL SCRAM 认证后，客户端偶发登录失败，抓包还看到同一组 nonce 被重复使用。客户端第一次发来的 nonce 没有做长度和字符集校验，服务端直接拼进自己的 nonce，攻击者能预测最终串；服务端 nonce 在并发连接间复用，两个客户端拿到同一组挑战；channel binding 没有和 TLS 的 tls-server-end-point 逐字节比较，中间人能把连接降级到无绑定；迭代次数由客户端在 client-first 里指定，服务端没有下限，一轮就能算完；salt 为空也接受，预计算表直接命中；auth message 的字段顺序按字典序拼，和 RFC 规定的顺序不一致，合法客户端反而验不过；proof 校验没有用常量时间比较，逐位试探能放大时间差；client-final 可以重放，服务端没有按 nonce 记已消费；账号不存在和口令错误返回不同错误码，会被用来判断账号是否存在；认证失败次数没有按用户和 IP 限速；服务端缓存了 verifier，改密后旧 proof 还能通过；日志里只有用户名，nonce、迭代次数和通道绑定结果都没有。scram_channel_binding.py 的 ScramChannelBinding 要把 nonce、通道绑定和 proof 校验放在同一个认证状态里。硬门禁：通道绑定值和 TLS 导出值不一致就直接失败；同一个 nonce 只能完成一次认证，重复 client-final 按 replay 处理；迭代次数低于下限或 salt 为空一律拒绝；账号不存在和口令错误要返回完全相同的失败响应。仓库里那份 SCRAM 测试眼下没有一条能过，nonce 与通道绑定都还没跑绿。

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
