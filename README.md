# GSB Bugfix Task
## 题面
联邦聊天接进来一个新域之后，跨域消息有时丢、有时被冒充。dialback 的密钥没有和接收域绑定，A 域能替 B 域生成合法签名；验证结果按 stanza 的 from 属性缓存，from 可以随便填，缓存被污染；路由只看 to 的域名，不看 resource，消息投给了错误的客户端；JID 的大小写和转义归一不一致，Alice@A 和 alice@a 被判成两个人；多个资源优先级相同时随机挑一个投递，离线消息直接丢；SASL EXTERNAL 不检查客户端证书里的 xmppAddr，任何证书都能登录；流重启后序列号和状态没有重置，同一条 stanza 被处理两次；未完成 dialback 的域可以发 iq 查用户名片；单条 stanza 没有大小上限，一个超长消息把内存打满；两个域互相转发形成路由环，没有跳数限制；离线消息落盘失败仍然回了成功。xmpp_dialback_router.py 的 XmppDialbackRouter 要把 JID 归一、路由选择和审计统一到一份解析结果上，dialback 验证按域和密钥版本幂等，路由环要有跳数上限，离线队列写入成功后才回确认，重启后从磁盘恢复队列并继续投递。test_xmpp_dialback_router.py 十五条用例刚从流水线拉下来，一条都没通过。

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
