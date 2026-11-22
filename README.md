# GSB Bugfix Task
## 题面
内网渗透报告里写得很直白：拿一台普通机器就能把 NTLM 认证中继到域控。服务端不要求 MIC，认证消息里的字段被改过也照样通过；通道绑定未参与校验，TLS 在中间终结以后中继照样能用；TargetInfo 里的 SPN 没有和实际访问的服务比对，换一个服务名也能过；NTLMv1 还在被接受，客户端一降级就成功；Type2 的 challenge 被复用，旧的认证响应可以重放；扩展保护配成 required，代码里却按 accept 处理；匿名和 guest 的回退路径绕开了组策略；同一条连接上做第二次 NTLM 协商时状态没有重置；认证失败后连接还能继续复用，凭据缓存被污染；日志只记了用户名，没有记来源地址和目标 SPN；跨域引用没有限制，认证被引到了恶意域。ntlm_relay_fence.py 的 NtlmRelayFence 要求 MIC 和通道绑定同时通过才允许认证，SPN 必须和连接目标一致，NTLMv1 和匿名回退直接关闭，challenge 一次性使用，认证状态按连接隔离，失败日志带上来源和目标，重复的 challenge 按连接幂等。test_ntlm_relay_fence.py 十五条检查在域环境复现，全部未通过。

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
