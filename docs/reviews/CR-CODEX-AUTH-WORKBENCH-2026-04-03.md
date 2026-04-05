# Codex Auth Workbench Review

## Scope

- Branch: `feature/codex-auth-workbench`
- Base: `upstream/main`
- Goal:
  - 补齐账号管理页异步批处理和 Codex Auth 工作台文档口径
  - 记录当前分支的真实验证证据
  - 记录本地 CodeRabbit 复核结果

## Delivered Behavior

- 账号管理页的 `刷新Token`、`验证Token`、`检测订阅`、`总览刷新` 使用异步任务模型。
- `Codex Auth` 通过独立工作台入口打开，不再和常规账号运维按钮混排。
- 工作台支持 `批量审计`、`批量修复`、`批量生成`、`批量导出` 四个动作。
- 导出结果为标准 managed `auth.json` ZIP，兼容官方 Codex 和 `codex-auth`。

## Verification

### Static Check

```bash
python3 -m py_compile src/web/routes/accounts.py src/web/routes/payment.py src/core/openai/codex_auth_workbench.py
node --check static/js/accounts.js
```

Result:

```text
exit code 0
```

### Targeted Tests

```bash
uv run python -m pytest -q tests/test_codex_auth_workbench.py tests/test_security_and_task_routes.py
```

Result:

```text
12 passed in 6.15s
```

### Real Dev Evidence

- Isolated dev service: `http://127.0.0.1:16668`
- Dev database only: copied 4 abnormal accounts `53 / 64 / 65 / 71`
- Batch audit result: `1 repairable`, `3 blocked by add-phone`
- Batch repair result: account `53` repaired successfully; `64 / 65 / 71` stayed blocked
- Batch export result: ZIP only contained the repaired account artifact

## Local CodeRabbit

- First pass: found actionable issues around domain slot release, pause timeout, DB rollback, long-held DB session, mailbox binding, and review doc secret exposure
- Fix status: all findings addressed on branch
- Second pass result: `0 comments`
- Reviewed repository path: `/Volumes/Work/code/codex-console`
