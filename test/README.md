# 回信测试

这是一个与生产流程隔离的回信测试服务。它只通过 IMAP 读取邮件发件人和标题，再通过 SMTP 回复固定内容：

> 已收到您的简历，感谢报名！
>
> 请根据您的意向组别加入对应的培训群，后续培训安排、考核通知等信息将在 QQ 招新群内发布，请及时关注群消息。
>
> 机械组：781323068
> 嵌入式组（硬件 / 电控）：1121923815
> 算法组（导航 / 视觉 / 软件）：2159078941
> 运宣组：1108278071
> 期待在后续培训中见到你！

它不会读取或解析附件，不会进行邮件筛选，不会调用 SiliconFlow，也不会写入飞书。

## 使用

复制 `test/.env.reply.example` 为 `test/.env.reply`，填入 163 邮箱的客户端授权码，然后先运行单元测试：

```bash
python -m unittest discover -s test -p 'test_*.py'
```

启动持续监听：

```bash
python test/reply_service.py --env-file test/.env.reply
```

只检查一次：

```bash
python test/reply_service.py --env-file test/.env.reply --once
```

默认首次启动会跳过收件箱中已有的邮件，之后收到新邮件才会回复。若确实要把已有邮件作为测试输入，显式加上 `--process-existing`。回信成功的 IMAP UID 会记录在 `test/reply_state.json`，同一封邮件不会重复回复。
