"""
Codex Auth 工作台核心能力
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...config.constants import (
    CODEX_OAUTH_ORIGINATOR,
    CODEX_OAUTH_REDIRECT_URI,
    CODEX_OAUTH_SCOPE,
    EmailServiceType,
)
from ...config.settings import get_settings
from ...core.register import RegistrationEngine
from ...core.timezone_utils import utcnow_naive
from ...core.utils import get_data_dir
from ...database.models import Account, EmailService
from ...services import create_email_service


CODEX_AUTH_EXTRA_KEY = "codex_auth"
CODEX_AUTH_HEALTHY = "healthy"
CODEX_AUTH_REPAIRABLE = "repairable"
CODEX_AUTH_BLOCKED = "blocked"
CODEX_AUTH_MISSING = "missing_prerequisites"
CODEX_AUTH_UNKNOWN = "unknown"
CODEX_AUTH_ARTIFACT_DIRNAME = "codex_auth"
CODEX_AUTH_ADD_PHONE_KEYWORD = "auth.openai.com/add-phone"


@dataclass
class CodexAuthStatus:
    health: str
    generated: bool
    export_ready: bool
    complete: bool
    label: str
    reason: str = ""
    generated_at: Optional[str] = None
    last_audit_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_error: str = ""
    last_block_reason: str = ""
    artifact_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "health": self.health,
            "generated": self.generated,
            "export_ready": self.export_ready,
            "complete": self.complete,
            "label": self.label,
            "reason": self.reason,
            "generated_at": self.generated_at,
            "last_audit_at": self.last_audit_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "last_block_reason": self.last_block_reason,
            "artifact_path": self.artifact_path,
        }


@dataclass
class CodexAuthResult:
    success: bool
    email: str = ""
    health: str = CODEX_AUTH_UNKNOWN
    workspace_id: str = ""
    account_id: str = ""
    auth_json: Optional[Dict[str, Any]] = None
    error_message: str = ""
    block_reason: str = ""
    logs: List[str] = field(default_factory=list)


class CodexAuthError(RuntimeError):
    pass


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-")
    return text or "account"


def get_codex_auth_extra(account: Account) -> Dict[str, Any]:
    extra_data = dict(getattr(account, "extra_data", None) or {})
    payload = extra_data.get(CODEX_AUTH_EXTRA_KEY)
    return dict(payload or {}) if isinstance(payload, dict) else {}


def update_codex_auth_extra(account: Account, **fields: Any) -> Dict[str, Any]:
    extra_data = dict(getattr(account, "extra_data", None) or {})
    payload = get_codex_auth_extra(account)
    payload.update({key: value for key, value in fields.items() if value is not None})
    extra_data[CODEX_AUTH_EXTRA_KEY] = payload
    account.extra_data = extra_data
    return payload


def _has_token(value: Optional[str]) -> bool:
    return bool(str(value or "").strip())


def _has_session_material(account: Account) -> bool:
    session_token = str(getattr(account, "session_token", "") or "").strip()
    cookies_text = str(getattr(account, "cookies", "") or "").strip()
    return bool(session_token or cookies_text)


def build_managed_auth_json(account: Account) -> Dict[str, Any]:
    access_token = str(getattr(account, "access_token", "") or "").strip()
    refresh_token = str(getattr(account, "refresh_token", "") or "").strip()
    id_token = str(getattr(account, "id_token", "") or "").strip()
    account_id = str(getattr(account, "account_id", "") or "").strip()

    missing = []
    if not access_token:
        missing.append("access_token")
    if not refresh_token:
        missing.append("refresh_token")
    if not id_token:
        missing.append("id_token")
    if not account_id:
        missing.append("account_id")
    if missing:
        raise CodexAuthError(f"缺少生成 auth.json 所需字段: {', '.join(missing)}")

    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
        },
        "last_refresh": _utc_iso_now(),
    }


def get_codex_auth_artifact_dir() -> Path:
    artifact_dir = get_data_dir() / CODEX_AUTH_ARTIFACT_DIRNAME
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def get_codex_auth_artifact_path(account: Account) -> Path:
    safe_name = _sanitize_slug(f"{account.id}-{account.email}")
    return get_codex_auth_artifact_dir() / safe_name / "auth.json"


def write_codex_auth_artifact(account: Account, auth_json: Dict[str, Any]) -> Path:
    artifact_path = get_codex_auth_artifact_path(account)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(auth_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return artifact_path


def resolve_codex_auth_status(account: Account) -> CodexAuthStatus:
    meta = get_codex_auth_extra(account)
    complete = all(
        [
            _has_token(getattr(account, "access_token", None)),
            _has_token(getattr(account, "refresh_token", None)),
            _has_token(getattr(account, "id_token", None)),
            _has_token(getattr(account, "account_id", None)),
        ]
    )
    artifact_path = str(meta.get("artifact_path") or "").strip()
    generated = bool(meta.get("generated")) and bool(artifact_path)
    export_ready = complete
    last_error = str(meta.get("last_error") or "").strip()
    last_block_reason = str(meta.get("last_block_reason") or "").strip()

    if complete:
        return CodexAuthStatus(
            health=CODEX_AUTH_HEALTHY,
            generated=generated,
            export_ready=export_ready,
            complete=True,
            label="健康",
            reason="完整 Managed Auth 可用",
            generated_at=str(meta.get("generated_at") or "") or None,
            last_audit_at=str(meta.get("last_audit_at") or "") or None,
            last_success_at=str(meta.get("last_success_at") or "") or None,
            last_error=last_error,
            last_block_reason=last_block_reason,
            artifact_path=artifact_path,
        )

    if last_block_reason:
        return CodexAuthStatus(
            health=CODEX_AUTH_BLOCKED,
            generated=False,
            export_ready=False,
            complete=False,
            label="受阻",
            reason=last_block_reason,
            generated_at=str(meta.get("generated_at") or "") or None,
            last_audit_at=str(meta.get("last_audit_at") or "") or None,
            last_success_at=str(meta.get("last_success_at") or "") or None,
            last_error=last_error,
            last_block_reason=last_block_reason,
            artifact_path=artifact_path,
        )

    missing = []
    if not _has_token(getattr(account, "password", None)):
        missing.append("password")
    if not _has_session_material(account):
        missing.append("session")
    if missing:
        return CodexAuthStatus(
            health=CODEX_AUTH_MISSING,
            generated=False,
            export_ready=False,
            complete=False,
            label="缺条件",
            reason=f"缺少前置条件: {', '.join(missing)}",
            generated_at=str(meta.get("generated_at") or "") or None,
            last_audit_at=str(meta.get("last_audit_at") or "") or None,
            last_success_at=str(meta.get("last_success_at") or "") or None,
            last_error=last_error,
            last_block_reason=last_block_reason,
            artifact_path=artifact_path,
        )

    return CodexAuthStatus(
        health=CODEX_AUTH_REPAIRABLE,
        generated=False,
        export_ready=False,
        complete=False,
        label="可修复",
        reason="可尝试严格 Codex Auth 修复",
        generated_at=str(meta.get("generated_at") or "") or None,
        last_audit_at=str(meta.get("last_audit_at") or "") or None,
        last_success_at=str(meta.get("last_success_at") or "") or None,
        last_error=last_error,
        last_block_reason=last_block_reason,
        artifact_path=artifact_path,
    )


def resolve_email_service_for_account(
    account: Account,
    email_service_rows: List[EmailService],
) -> Tuple[Optional[Any], str]:
    try:
        service_type = EmailServiceType(str(account.email_service or "").strip().lower())
    except Exception:
        return None, f"未知邮箱服务类型: {account.email_service}"

    enabled_rows = [
        row for row in email_service_rows
        if bool(getattr(row, "enabled", False))
        and str(getattr(row, "service_type", "") or "").strip().lower() == service_type.value
    ]
    enabled_rows.sort(key=lambda item: (int(getattr(item, "priority", 0) or 0), int(getattr(item, "id", 0) or 0)))
    if not enabled_rows:
        return None, f"未找到可用邮箱服务配置: {service_type.value}"

    def _normalize_email(value: Any) -> str:
        return str(value or "").strip().lower()

    def _lookup_candidates(row: EmailService) -> List[str]:
        config = dict(getattr(row, "config", {}) or {})
        return [
            _normalize_email(config.get("email")),
            _normalize_email(config.get("username")),
            _normalize_email(config.get("mailbox")),
            _normalize_email(getattr(row, "name", "")),
        ]

    selected = None
    target_email = _normalize_email(getattr(account, "email", ""))
    if target_email:
        for row in enabled_rows:
            if target_email in _lookup_candidates(row):
                selected = row
                break

    if selected is None:
        selected = enabled_rows[0]
    try:
        return create_email_service(service_type, dict(selected.config or {}), selected.name), ""
    except Exception as exc:
        return None, f"创建邮箱服务失败: {exc}"


class CodexAuthEngine(RegistrationEngine):
    def __init__(
        self,
        *,
        email: str,
        password: str,
        email_service: Any,
        email_service_id: Optional[str] = None,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(
            email_service=email_service,
            proxy_url=proxy_url,
            callback_logger=callback_logger,
        )
        self.email = str(email or "").strip().lower()
        self.inbox_email = str(email or "").strip()
        self.password = str(password or "").strip()
        self.email_info = {"email": self.email}
        if email_service_id:
            self.email_info["service_id"] = str(email_service_id).strip()

        settings = get_settings()
        self.oauth_manager = self.oauth_manager.__class__(
            client_id=settings.openai_client_id,
            auth_url=settings.openai_auth_url,
            token_url=settings.openai_token_url,
            redirect_uri=CODEX_OAUTH_REDIRECT_URI,
            scope=CODEX_OAUTH_SCOPE,
            proxy_url=proxy_url,
            originator=CODEX_OAUTH_ORIGINATOR,
        )

    def _strict_workspace_id(self) -> str:
        workspace_id = str(self._last_validate_otp_workspace_id or "").strip()
        if workspace_id:
            self._log(f"使用 OTP 返回的 Workspace ID: {workspace_id}")
            return workspace_id
        workspace_id = str(self._get_workspace_id() or "").strip()
        if workspace_id:
            self._log(f"Workspace ID: {workspace_id}")
            return workspace_id
        return ""

    @staticmethod
    def _is_add_phone_url(url: str) -> bool:
        return CODEX_AUTH_ADD_PHONE_KEYWORD in str(url or "").strip().lower()

    def _build_auth_json(self, token_info: Dict[str, Any]) -> Dict[str, Any]:
        access_token = str(token_info.get("access_token") or "").strip()
        refresh_token = str(token_info.get("refresh_token") or "").strip()
        id_token = str(token_info.get("id_token") or "").strip()
        account_id = str(token_info.get("account_id") or "").strip()
        if not all([access_token, refresh_token, id_token, account_id]):
            raise CodexAuthError("OAuth 回调未返回完整 token bundle")
        return {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": id_token,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "account_id": account_id,
            },
            "last_refresh": _utc_iso_now(),
        }

    def run(self) -> CodexAuthResult:
        result = CodexAuthResult(success=False, email=self.email, logs=self.logs)
        try:
            did, sen_token = self._prepare_authorize_flow("Codex Auth")
            if not did:
                result.error_message = "获取 Device ID 失败"
                result.health = CODEX_AUTH_MISSING
                return result
            if not sen_token:
                result.error_message = "Sentinel 验证失败"
                result.health = CODEX_AUTH_UNKNOWN
                return result

            login_start_result = self._submit_login_start(did, sen_token)
            if not login_start_result.success:
                result.error_message = f"提交登录入口失败: {login_start_result.error_message}"
                result.health = CODEX_AUTH_UNKNOWN
                return result

            page_type = str(login_start_result.page_type or "").strip()
            if page_type == "login_password":
                password_result = self._submit_login_password()
                if not password_result.success:
                    result.error_message = f"提交登录密码失败: {password_result.error_message}"
                    result.health = CODEX_AUTH_UNKNOWN
                    return result
                if not password_result.is_existing_account:
                    result.error_message = f"未进入邮箱验证码页: {password_result.page_type or 'unknown'}"
                    result.health = CODEX_AUTH_UNKNOWN
                    return result
            elif page_type != "email_otp_verification":
                result.error_message = f"登录入口返回未知页面: {page_type or 'unknown'}"
                result.health = CODEX_AUTH_UNKNOWN
                return result

            if not self._verify_email_otp_with_retry(stage_label="Codex Auth 验证码", max_attempts=3, fetch_timeout=120):
                result.error_message = "验证码校验失败"
                result.health = CODEX_AUTH_UNKNOWN
                return result

            otp_continue = str(self._last_validate_otp_continue_url or "").strip()
            if self._is_add_phone_url(otp_continue):
                result.error_message = "OTP 后命中 add-phone 门控"
                result.block_reason = "OTP 后进入 add-phone，未放行到 workspace"
                result.health = CODEX_AUTH_BLOCKED
                return result

            workspace_id = self._strict_workspace_id()
            if not workspace_id:
                result.error_message = "获取 Workspace ID 失败"
                result.block_reason = "OTP 后未获取到 workspace"
                result.health = CODEX_AUTH_BLOCKED if self._is_add_phone_url(otp_continue) else CODEX_AUTH_UNKNOWN
                return result
            result.workspace_id = workspace_id

            continue_url = str(self._select_workspace(workspace_id) or "").strip()
            if not continue_url:
                continue_url = otp_continue
                if continue_url:
                    self._log("workspace/select 未返回 continue_url，改用 OTP 缓存继续", "warning")
            if not continue_url:
                result.error_message = "获取 continue_url 失败"
                result.health = CODEX_AUTH_UNKNOWN
                return result

            callback_url, final_url = self._follow_redirects(continue_url)
            if self._is_add_phone_url(final_url):
                result.error_message = "重定向阶段命中 add-phone 门控"
                result.block_reason = "重定向阶段进入 add-phone，未命中 OAuth callback"
                result.health = CODEX_AUTH_BLOCKED
                return result
            if not callback_url:
                result.error_message = "未获取到 OAuth callback"
                result.health = CODEX_AUTH_UNKNOWN
                return result

            token_info = self._handle_oauth_callback(callback_url)
            if not token_info:
                result.error_message = "OAuth callback 处理失败"
                result.health = CODEX_AUTH_UNKNOWN
                return result

            auth_json = self._build_auth_json(token_info)
            result.success = True
            result.health = CODEX_AUTH_HEALTHY
            result.workspace_id = workspace_id
            result.account_id = str(token_info.get("account_id") or "").strip()
            result.auth_json = auth_json
            return result
        except Exception as exc:
            result.error_message = str(exc)
            result.health = CODEX_AUTH_UNKNOWN
            return result
        finally:
            try:
                self.http_client.close()
            except Exception:
                pass


def persist_codex_auth_success(account: Account, result: CodexAuthResult) -> Path:
    if not result.auth_json:
        raise CodexAuthError("缺少 auth.json，无法落盘")

    tokens = result.auth_json.get("tokens") or {}
    account.access_token = str(tokens.get("access_token") or "").strip()
    account.refresh_token = str(tokens.get("refresh_token") or "").strip()
    account.id_token = str(tokens.get("id_token") or "").strip()
    account.account_id = str(tokens.get("account_id") or getattr(account, "account_id", "") or "").strip()
    if result.workspace_id:
        account.workspace_id = result.workspace_id
    account.last_refresh = utcnow_naive()

    artifact_path = write_codex_auth_artifact(account, result.auth_json)
    update_codex_auth_extra(
        account,
        health=CODEX_AUTH_HEALTHY,
        generated=True,
        generated_at=_utc_iso_now(),
        last_audit_at=_utc_iso_now(),
        last_success_at=_utc_iso_now(),
        last_error="",
        last_block_reason="",
        artifact_path=str(artifact_path),
    )
    return artifact_path


def persist_codex_auth_generated_artifact(account: Account, auth_json: Dict[str, Any]) -> Path:
    artifact_path = write_codex_auth_artifact(account, auth_json)
    update_codex_auth_extra(
        account,
        health=CODEX_AUTH_HEALTHY,
        generated=True,
        generated_at=_utc_iso_now(),
        last_success_at=str(get_codex_auth_extra(account).get("last_success_at") or "") or None,
        last_error="",
        last_block_reason="",
        artifact_path=str(artifact_path),
    )
    return artifact_path


def persist_codex_auth_audit(account: Account, *, health: str, error_message: str = "", block_reason: str = "") -> None:
    update_codex_auth_extra(
        account,
        health=health,
        last_audit_at=_utc_iso_now(),
        last_error=str(error_message or "").strip(),
        last_block_reason=str(block_reason or "").strip(),
    )


def build_codex_auth_zip_entries(accounts: List[Account]) -> List[Tuple[str, bytes]]:
    entries: List[Tuple[str, bytes]] = []
    for account in accounts:
        try:
            auth_json = build_managed_auth_json(account)
        except Exception:
            continue
        filename = f"{_sanitize_slug(account.email)}/auth.json"
        entries.append(
            (
                filename,
                json.dumps(auth_json, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        )
    return entries
