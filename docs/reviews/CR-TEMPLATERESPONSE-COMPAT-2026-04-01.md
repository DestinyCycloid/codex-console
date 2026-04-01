# TemplateResponse Compatibility Review

## Control Contract

- Primary Setpoint: 在当前 Docker 镜像依赖组合下，`GET /login` 必须返回 `200` 并渲染登录页，而不是运行时 `500`
- Acceptance:
  - 原始代码在容器内稳定复现 `GET /login = 500`
  - 修复后容器内 `GET /login = 200`
  - 修复后 `POST /login = 302`，登录后 `GET / = 200`
  - 自动化回归测试通过
- Guardrail Metrics:
  - 不修改依赖版本
  - 不引入回退分支或静默降级
  - 不改变现有登录语义
- Boundary:
  - `src/web/app.py`
  - `tests/test_web_login_template_response.py`

## Before Fix

- Image dependency evidence:
  - `fastapi 0.135.2`
  - `starlette 1.0.0`
  - `jinja2 3.1.6`
  - `Jinja2Templates.TemplateResponse(self, request, name, context=None, ...)`
- Reproduction command:
  - `docker compose -f /Volumes/Work/code/codex-console/docker-compose.yml --project-directory /Volumes/Work/code/codex-console up -d --build`
  - `curl -s -D /tmp/cse_before.headers http://127.0.0.1:15555/login -o /tmp/cse_before.body`
- Observed output:
  - `HTTP/1.1 500 Internal Server Error`
  - body: `Internal Server Error`
- Runtime evidence:
  - stack trace points to `src/web/app.py` login route
  - exception: `TypeError: unhashable type: 'dict'`

## Fix

- Change all template rendering calls from positional legacy form:
  - `TemplateResponse("login.html", {...})`
- To explicit current-signature form:
  - `TemplateResponse(request=request, name="login.html", context={...})`

## After Fix

- Online verification:
  - `GET /login => 200`
  - response body begins with `<!DOCTYPE html>`
  - `POST /login => 302`
  - `GET / after login => 200`
- Container logs after fix:
  - startup logs present
  - no `/login` runtime exception reproduced

## Regression Test

- Added test:
  - `tests/test_web_login_template_response.py`
- Test command:
  - `docker compose ... exec -T webui python -m pytest tests/test_web_login_template_response.py -q`
- Result:
  - `1 passed, 15 warnings in 1.66s`

## Residual Risks

- 仓库中仍存在若干 FastAPI / Pydantic / SQLAlchemy 弃用警告，本次未处理
- 该修复针对当前 `TemplateResponse` 签名漂移；如果后续继续升级依赖，仍需要跑同一路径回归
