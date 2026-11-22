# GSB Bugfix Task
## 题面
安全扫描把核心转储目录标成高危，说里面能捞到密钥。核心转储把进程内存里的密钥一起写进文件；core_pattern 配的管道程序挂了，内核丢 core，但进程一直等在那里不退；RLIMIT_CORE 是 unlimited，一个死循环崩溃把磁盘写满；多个进程同时崩溃，core 文件名冲突互相覆盖；dump 出来的文件权限 0644，同机用户都能读；处理程序继承了崩溃进程的附加组，能读它本来读不到的文件；systemd-coredump 和自定义 handler 同时开着，同一份 core 写两遍；重启后残留 core 没清理，磁盘一直涨；几十 G 内存的进程 dump 时触发 OOM，把整机拖死；过滤规则按进程名匹配，换个路径的同名进程就绕过了；core 输出路径可以被符号链接替换，写到任意位置；审计里没有 dump 事件记录。core_dump_filter.py 的 CoreDumpPolicy 要在崩溃那一刻决定写什么、写到哪、谁可读。四条硬性要求：带敏感标记的进程 dump 产物不得含明文密钥，按字节检查；同名并发 dump 不得互相覆盖；handler 失败必须回退到安全路径并给出明确状态；重启后残留必须清理。test_core_dump_filter.py 的十五条用例现在一条都立不住，过滤和落盘两块一起挂。

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
