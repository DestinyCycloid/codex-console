# Account Batch Actions Review

## Scope

- Branch: `feature/account-batch-action-tooltips`
- Base: `upstream/main`
- Goal:
  - fix the three broken batch action routes on the accounts page
  - keep button labels stable in idle state
  - replace native `title` hints with hover bubbles shown below the buttons

## Verification

### Static Check

Command:

```bash
python3 -m py_compile src/web/routes/accounts.py src/web/routes/payment.py
```

Result:

```text
exit code 0
```

### Runtime Check

Isolated instance:

- URL: `http://127.0.0.1:16667`
- Access password: set `REVIEW_LOGIN_PASSWORD` in the local shell before running the script

Command:

```bash
python3 - <<'PY'
import urllib.parse, urllib.request, http.cookiejar, json
import os
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
password = os.environ.get('REVIEW_LOGIN_PASSWORD', '').strip()
if not password:
    raise SystemExit('REVIEW_LOGIN_PASSWORD is required')
login_data = urllib.parse.urlencode({'password': password}).encode()
login_req = urllib.request.Request('http://127.0.0.1:16667/login', data=login_data, method='POST')
login_req.add_header('Content-Type', 'application/x-www-form-urlencoded')
login_resp = opener.open(login_req, timeout=10)
print('login_status', login_resp.status)
accounts_resp = opener.open('http://127.0.0.1:16667/accounts', timeout=10)
print('accounts_status', accounts_resp.status)
for path, poll_prefix in [
    ('/api/accounts/batch-refresh/async', '/api/accounts/tasks/'),
    ('/api/accounts/batch-validate/async', '/api/accounts/tasks/'),
    ('/api/payment/accounts/batch-check-subscription/async', '/api/payment/ops/tasks/'),
]:
    req = urllib.request.Request(
        f'http://127.0.0.1:16667{path}',
        data=json.dumps({'ids': [], 'select_all': True}).encode(),
        method='POST',
        headers={'Content-Type': 'application/json'},
    )
    resp = opener.open(req, timeout=20)
    payload = json.loads(resp.read().decode() or '{}')
    task_id = payload.get('id') or payload.get('task_id')
    print(path, resp.status, task_id)
    if task_id:
        poll = opener.open(f'http://127.0.0.1:16667{poll_prefix}{task_id}', timeout=20)
        poll_payload = json.loads(poll.read().decode() or '{}')
        print(poll_prefix, poll.status, poll_payload.get('status'))
PY
```

Result:

```text
login_status 200
accounts_status 200
/api/accounts/batch-refresh/async 200 accounts-batch-refresh-f0b2d40566ba
/api/accounts/tasks/ 200 running
/api/accounts/batch-validate/async 200 accounts-batch-validate-1d5627590eb7
/api/accounts/tasks/ 200 completed
/api/payment/accounts/batch-check-subscription/async 200 payment-batch-check-subscription-227ec45d862f
/api/payment/ops/tasks/ 200 completed
```

### UI Check

- Hovering `刷新Token` shows a custom bubble below the button
- Hovering `验证Token` shows a custom bubble below the button
- Hovering `检测订阅` shows a custom bubble below the button
- When selection count changes, these three buttons keep stable idle labels

## Conclusion

- The broken batch action routes are fixed on this branch
- Hover help now matches the requested interaction model
- No formal environment deployment was required for this review
