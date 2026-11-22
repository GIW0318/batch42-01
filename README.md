# GSB Bugfix Task
## 题面
运维平台升级后，一个只有查看权限的账号把生产服务重启了，审计里只留下一条成功记录。action 查找按前缀匹配，org.foo.bar 命中了 org.foo.bar-evil；implicit authorization 只看会话是否活跃，没有区分本地登录和远程登录；管理员身份只比对了直接所属组，嵌套组里的成员被漏掉；授权结果缓存不随规则文件变更失效，改完策略还要重启进程；一个请求带多个 action 时只校验了第一个；subject 里的进程号可以伪造，按进程号反查用户失败后代码走了放行分支；需要认证时返回了 challenge，调用方却当成授权成功；规则脚本抛异常被吞掉，默认结果变成了允许；次数和时间限制只在前端做，后端完全不查；用户切换后旧的 authorization 仍然有效；未知 action 直接按授权处理。polkit_action_resolver.py 的 PolkitActionResolver 要按完整 action 标识精确匹配，implicit 授权区分本地与远程会话，组关系展开到传递闭包，规则变更让缓存整体失效，subject 必须能反查到真实用户，未知 action 和脚本异常一律拒绝并给出原因，同一 subject 的并发请求串行判定。test_polkit_action_resolver.py 那十五个用例现在没有一条能过。

## 运行方式
```bash
python -m pytest tests -q
```

## 平台字段

所属领域：安全
子方向：系统安全
任务类型：Bugfix
语言 / 框架：Python 3, pytest
环境可复现等级：有外部依赖，未容器化
