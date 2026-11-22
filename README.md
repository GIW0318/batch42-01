# GSB Bugfix Task
## 题面
政务 App 接入可验证凭证后，安全评测用一张别人的驾照通过了实名核验。presentation_submission 里的 descriptor_map 没有和 request 的 input_descriptor 对齐，多出来的凭证被静默忽略；holder 绑定只验了 VP 签名，没有验 KB-JWT 里的 nonce；nonce 在有效期内可被反复提交，旧 presentation 原样重放仍然通过；audience 未做比对，签发给 A 应用的凭证被投给了 B；凭证的生效和过期边界一个算开一个算闭，临界时刻结果不稳定；选择性披露的 disclosure 摘要没有逐条核对，改一个字段也能过；同一个 credential id 被两个 descriptor 重复引用，绕过了其中一条约束；sd-jwt 和 mdoc 两种格式混淆时按错误的格式解析；吊销状态列表没有查询，已经作废的凭证继续可用；direct_post.jwt 的传输层加密没有解开，明文回传到了后端；失败原因细分到具体字段，把验证策略泄露给了调用方。openid4vp_presentation_mapper.py 的 Openid4vpPresentationMapper 要按 request、submission 和凭证三者交叉校验，nonce 一次性消费且和会话绑定，audience 与格式必须精确匹配，摘要和披露字段逐项比对，吊销查询失败时按拒绝处理，对外只返回统一错误，同一凭证的并发提交只能落一次。test_openid4vp_presentation_mapper.py 那十五条断言现在逐条失败。

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
