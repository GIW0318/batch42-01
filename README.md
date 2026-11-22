# GSB Bugfix Task
## 题面
移动端从 JWT 换到 PASETO 之后，旧版本的令牌还能混进来，测试同学用改过的 footer 也拿到了接口权限。版本与用途字段未做校验，v2.local 的密文按 v4.public 验签；footer 没有进入签名范围，改掉里面的 kid 仍然验得过；implicit assertion 没有参与认证，绑定信息等于没写；密钥编号直接从令牌头里取，攻击者换一把自己的密钥就能过；过期时间和生效时间按字符串比较，二零二五年被排在了二零二四年前面；local 令牌的 nonce 重复使用，旧密文可以整段重放；解析失败和验签失败返回不同的错误码，可以拿来区分令牌是否合法；密钥轮换期只认当前密钥，轮换前签发的令牌集体失效；audience 是数组时只看了第一个元素；超长令牌先分配缓冲区再校验长度，一个请求就能吃掉几十兆；时钟偏差窗口写死成全局常量，多个租户共用同一个值。paseto_claim_verifier.py 的 PasetoClaimVerifier 要固定版本与用途的组合，footer 和 implicit assertion 都纳入认证范围，密钥只能来自可信密钥集，nonce 一次性，时间比较按数值并允许配置偏差，解析和验签对外返回同一种失败，nonce 在并发验签中只生效一次。PASETO 那套回归位于 test_paseto_claim_verifier.py，十五条用例目前一条也没跑过。

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
