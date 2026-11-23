# 校园中台登录

校园内网入口使用 `http://10.50.159.63:8080/studyagent/sso?token=<URL 编码后的 token>`。StudyAgent 在后端调用 `getTokenInfo`，浏览器随即跳转到不含 token 的确认页。用户确认当前身份后进入 StudyAgent，无需再次输入密码。确认请求只能使用一次。校园 HTTP 入口使用 `CAMPUS_SSO_ALLOW_HTTP=true`。

中台通过校园网关打开 `http://10.50.159.62/studyagent/login?token=<URL 编码后的 token>` 时，也进入同一验票流程。普通登录地址沿用 Keycloak。

## 账号对照

学校和 `_id` 确定已绑定的中台身份。老账号首次从中台进入时，通过预先确认的学校和 `number` 找到原 StudyAgent 用户 ID，再保存中台 `_id`。自动开通启用后，新学生的学校、学号和姓名须与导入的开通名册一致，新账号从答疑页开始使用，班级归属另行确认。教师需要预先绑定已授权的本地教师账号。

复核身份对照表后，只填写已确认的「确认的中台学号」列。同步账户状态或班级信息时，保留绑定的原用户 ID。导入命令默认预览，输出条数：

```sh
python -m scripts.import_campus_bindings /path/to/reviewed.csv
```

确认预览后在受控环境中加 `--apply` 写入。保持表格在受控位置。回调所用配置见 `.env.example`。
使用 `--roster-csv /path/to/approved-roster.csv` 同时预览或替换同一学校的开通名册。该文件列名为 `number,full_name`。

## 试点和上线

1. 核实验票返回的 `scid` 和 `projectID`。校园入口直接使用 StudyAgent 的回调路由，该路由关闭访问日志并在验票后跳转到不含查询串的页面。
2. 先导入已确认的老账号绑定和开通名册，再启用 `CAMPUS_SSO_AUTO_CREATE_STUDENTS`。用真实中台和平板验证原班级和历史对话、新生首次开通、重复登录以及名册外身份。
3. 导入线上绑定前，在同步服务器部署 StudyAgent 增量同步脚本。用测试绑定和对话验证目录学生、教师和管理员同步执行后数据仍在，再扩大已确认账号的接入范围。
