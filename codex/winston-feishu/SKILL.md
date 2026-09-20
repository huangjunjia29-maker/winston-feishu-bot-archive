---
name: winston-feishu
description: 调用温斯顿执行飞书任务，复用本机 App ID 与 App Secret 认证，读取和编辑飞书表格、多维表格、文档，或按用户要求发送消息和执行已有工作流。用户提到温斯顿、Winston 或要求处理飞书链接时使用。
---

# 温斯顿飞书能力

## 本机运行入口

温斯顿项目根目录：
`__WINSTON_PROJECT_ROOT__`

Python：项目根目录下 `.venv/Scripts/python.exe`。
在任意项目中均使用这个解释器和项目路径。不要在当前项目重新创建飞书凭证。
先读取温斯顿项目的 `AGENTS.md`、`ACTIVATE_FIRST.md` 和 `WINSTON_RUNTIME.md`。
已有本机激活记录和已完成的配置应复用；只有迁移、配置失效或对应任务的前置检查失败时才处理相应缺口，不要求用户重复激活。

## 认证与调用

复用 `workflow/modules/feishu_auth.py`：`FEISHU_APP_ID + FEISHU_APP_SECRET → tenant_access_token`。
该模块从本机项目环境加载凭证，在进程内缓存令牌。不要把 Secret、令牌、cookie 或 `.env` 内容输出到聊天、日志、技能或其他项目。
在 Python 中把项目的 `workflow/modules` 加入 `sys.path` 后调用：

```python
import sys
from pathlib import Path
root = Path(__WINSTON_PROJECT_ROOT_LITERAL__)
sys.path.insert(0, str(root / 'workflow' / 'modules'))
import feishu_auth
# result = feishu_auth.request('GET', '/open-apis/...', params={...})
# result = feishu_auth.request('PUT', '/open-apis/...', json_body={...})
```

不要调用或打印 `credentials()`、`tenant_access_token()` 的返回值。
不要修改系统环境或全局 CLI 身份。已有监听器需要 CLI 时沿用其入口；直接表格任务使用现有认证模块，不依赖 CLI 用户登录。
若接口明确返回令牌过期而模块没有自动重试，强制刷新后仅重试一次；权限不足不能靠重复刷新解决。网络超时后先回读确认写入结果，避免重复创建记录或重复发消息。

## 飞书任务执行

- 使用用户当前提供的链接、目标子表和操作范围。Wiki 链接先通过 Wiki 节点接口解析实际 `obj_token` 和 `obj_type`，再读取子表或数据表元数据。电子表格 Sheets 与多维表格 Base 使用不同接口。
- 不从历史聊天复制旧表 token、群 ID 或副本地址。读取最新数据，并完成分页，再按实际表头、字段类型和记录定位目标。
- 用户已明确要求的写入可以在其范围内执行，不重复请求批准。发送消息必须有明确授权。只有目标或映射不明确，或操作超出用户范围时才询问。
- 写入前记录受影响区域的原值；仅更新指定字段或范围，保留公式、下拉选项和复选框类型；写后回读核对并报告实际目标及结果。
- 普通 API 任务不需要启动机器人监听器。业务抓取、后台监听和社交平台任务遵守项目原有激活门禁，使用原脚本及项目专用浏览器 Profile。
- 飞书权限由现有应用权限和资源共享决定；安装此技能不新增权限。遇到缺失权限，报告具体接口/资源及必要操作，不改用其他账号绕过。

## 检查与调用方式

只检查本机环境：用项目 Python 执行本技能 `scripts/check_runtime.py`。
加 `--online` 可验证获取令牌与读接口连通性，只打印通过/失败状态，不发送消息、不改表格。

用户可以说：`调用温斯顿，读取这个飞书表格：[链接]`，或显式使用 `$winston-feishu`。
技能提供本机共享入口，不共享聊天历史，不绕过项目沙箱或飞书权限。
