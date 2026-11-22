# GSB Bugfix Task
## 题面
递归解析集群上了 DNS Cookie 以后，解析成功率反而掉了几个点，值班看着监控一头雾水。Server cookie 密钥轮换后旧 cookie 一次性全失效，正常客户端被要求重试，解析延迟翻倍；只校验了 client cookie，server cookie 随便填也放行，伪造源地址就能放大；Cookie 选项长度非法时解析直接抛异常，进程退出；密钥轮换没有新旧共存期，切换那一秒的查询全丢；并发请求下 nonce 生成用了弱随机，出现重复；Cookie 绑定客户端 IP，NAT 后面大量客户端被误判；重启后密钥没从磁盘恢复，所有缓存 cookie 作废；回了 BADCOOKIE 但没带新 cookie，客户端一直重试；过期 cookie 仍然被接受，重放窗口无限长；IXFR 查询跳过 cookie 校验，和普通查询策略不一致；每秒十万查询时 HMAC 成了瓶颈，CPU 打满；调试日志把完整 cookie 打了出来。dns_cookie_verifier.py 的 DnsCookieVerifier 要按密钥版本和客户端特征校验。四个门禁：无 cookie 的 UDP 查询必须回 BADCOOKIE 并附上新 cookie；密钥轮换必须新旧共存至少一个周期；重启后从持久化恢复密钥；server cookie 不符必须静默丢弃不回应。test_dns_cookie_verifier.py 的十五条断言今天跑下来还是一片红，连轮换那条都没过。

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
