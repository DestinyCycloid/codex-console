"""
邮箱服务配置 API 路由
"""

import logging
import secrets
import time
from typing import List, Optional, Dict, Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ...database import crud
from ...database.session import get_db
from ...database.models import EmailService as EmailServiceModel
from ...services import EmailServiceFactory, EmailServiceType

logger = logging.getLogger(__name__)
router = APIRouter()

# OAuth2 状态存储 (内存，进程重启后失效)
_oauth_states: Dict[str, Dict[str, Any]] = {}


# ============== Pydantic Models ==============

class EmailServiceCreate(BaseModel):
    """创建邮箱服务请求"""
    service_type: str
    name: str
    config: Dict[str, Any]
    enabled: bool = True
    priority: int = 0


class EmailServiceUpdate(BaseModel):
    """更新邮箱服务请求"""
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


class EmailServiceResponse(BaseModel):
    """邮箱服务响应"""
    id: int
    service_type: str
    name: str
    enabled: bool
    priority: int
    config: Optional[Dict[str, Any]] = None  # 过滤敏感信息后的配置
    last_used: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class EmailServiceListResponse(BaseModel):
    """邮箱服务列表响应"""
    total: int
    services: List[EmailServiceResponse]


class ServiceTestResult(BaseModel):
    """服务测试结果"""
    success: bool
    message: str
    details: Optional[Dict[str, Any]] = None


class OutlookBatchImportRequest(BaseModel):
    """Outlook 批量导入请求"""
    data: str  # 多行数据，每行格式: 邮箱----密码 或 邮箱----密码----client_id----refresh_token
    enabled: bool = True
    priority: int = 0


class OutlookAliasesImportRequest(BaseModel):
    """Outlook 别名导入请求"""
    aliases: List[str]


class OutlookBatchImportResponse(BaseModel):
    """Outlook 批量导入响应"""
    total: int
    success: int
    failed: int
    accounts: List[Dict[str, Any]]
    errors: List[str]


# ============== Helper Functions ==============

# 敏感字段列表，返回响应时需要过滤
SENSITIVE_FIELDS = {'password', 'api_key', 'refresh_token', 'access_token', 'admin_token'}

def filter_sensitive_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """过滤敏感配置信息"""
    if not config:
        return {}

    filtered = {}
    for key, value in config.items():
        if key in SENSITIVE_FIELDS:
            # 敏感字段不返回，但标记是否存在
            filtered[f"has_{key}"] = bool(value)
        else:
            filtered[key] = value

    # 为 Outlook 计算是否有 OAuth
    if config.get('client_id') and config.get('refresh_token'):
        filtered['has_oauth'] = True

    # 为 Outlook 添加别名数量
    if 'aliases' in config:
        filtered['alias_count'] = len(config['aliases'])

    return filtered


def service_to_response(service: EmailServiceModel) -> EmailServiceResponse:
    """转换服务模型为响应"""
    return EmailServiceResponse(
        id=service.id,
        service_type=service.service_type,
        name=service.name,
        enabled=service.enabled,
        priority=service.priority,
        config=filter_sensitive_config(service.config),
        last_used=service.last_used.isoformat() if service.last_used else None,
        created_at=service.created_at.isoformat() if service.created_at else None,
        updated_at=service.updated_at.isoformat() if service.updated_at else None,
    )


# ============== API Endpoints ==============

@router.get("/stats")
async def get_email_services_stats():
    """获取邮箱服务统计信息"""
    with get_db() as db:
        from sqlalchemy import func

        # 按类型统计
        type_stats = db.query(
            EmailServiceModel.service_type,
            func.count(EmailServiceModel.id)
        ).group_by(EmailServiceModel.service_type).all()

        # 启用数量
        enabled_count = db.query(func.count(EmailServiceModel.id)).filter(
            EmailServiceModel.enabled == True
        ).scalar()

        stats = {
            'outlook_count': 0,
            'custom_count': 0,
            'temp_mail_count': 0,
            'duck_mail_count': 0,
            'freemail_count': 0,
            'imap_mail_count': 0,
            'tempmail_available': True,  # 临时邮箱始终可用
            'enabled_count': enabled_count
        }

        for service_type, count in type_stats:
            if service_type == 'outlook':
                stats['outlook_count'] = count
            elif service_type == 'moe_mail':
                stats['custom_count'] = count
            elif service_type == 'temp_mail':
                stats['temp_mail_count'] = count
            elif service_type == 'duck_mail':
                stats['duck_mail_count'] = count
            elif service_type == 'freemail':
                stats['freemail_count'] = count
            elif service_type == 'imap_mail':
                stats['imap_mail_count'] = count

        return stats


@router.get("/types")
async def get_service_types():
    """获取支持的邮箱服务类型"""
    return {
        "types": [
            {
                "value": "tempmail",
                "label": "Tempmail.lol",
                "description": "临时邮箱服务，无需配置",
                "config_fields": [
                    {"name": "base_url", "label": "API 地址", "default": "https://api.tempmail.lol/v2", "required": False},
                    {"name": "timeout", "label": "超时时间", "default": 30, "required": False},
                ]
            },
            {
                "value": "outlook",
                "label": "Outlook",
                "description": "Outlook 邮箱，需要配置账户信息",
                "config_fields": [
                    {"name": "email", "label": "邮箱地址", "required": True},
                    {"name": "password", "label": "密码", "required": True},
                    {"name": "client_id", "label": "OAuth Client ID", "required": False},
                    {"name": "refresh_token", "label": "OAuth Refresh Token", "required": False},
                ]
            },
            {
                "value": "moe_mail",
                "label": "MoeMail",
                "description": "自定义域名邮箱服务",
                "config_fields": [
                    {"name": "base_url", "label": "API 地址", "required": True},
                    {"name": "api_key", "label": "API Key", "required": True},
                    {"name": "default_domain", "label": "默认域名", "required": False},
                ]
            },
            {
                "value": "temp_mail",
                "label": "Temp-Mail（自部署）",
                "description": "自部署 Cloudflare Worker 临时邮箱，admin 模式管理",
                "config_fields": [
                    {"name": "base_url", "label": "Worker 地址", "required": True, "placeholder": "https://mail.example.com"},
                    {"name": "admin_password", "label": "Admin 密码", "required": True, "secret": True},
                    {"name": "domain", "label": "邮箱域名", "required": True, "placeholder": "example.com"},
                    {"name": "enable_prefix", "label": "启用前缀", "required": False, "default": True},
                ]
            },
            {
                "value": "duck_mail",
                "label": "DuckMail",
                "description": "DuckMail 接口邮箱服务，支持 API Key 私有域名访问",
                "config_fields": [
                    {"name": "base_url", "label": "API 地址", "required": True, "placeholder": "https://api.duckmail.sbs"},
                    {"name": "default_domain", "label": "默认域名", "required": True, "placeholder": "duckmail.sbs"},
                    {"name": "api_key", "label": "API Key", "required": False, "secret": True},
                    {"name": "password_length", "label": "随机密码长度", "required": False, "default": 12},
                ]
            },
            {
                "value": "freemail",
                "label": "Freemail",
                "description": "Freemail 自部署 Cloudflare Worker 临时邮箱服务",
                "config_fields": [
                    {"name": "base_url", "label": "API 地址", "required": True, "placeholder": "https://freemail.example.com"},
                    {"name": "admin_token", "label": "Admin Token", "required": True, "secret": True},
                    {"name": "domain", "label": "邮箱域名", "required": False, "placeholder": "example.com"},
                ]
            },
            {
                "value": "imap_mail",
                "label": "IMAP 邮箱",
                "description": "标准 IMAP 协议邮箱（Gmail/QQ/163等），仅用于接收验证码，强制直连",
                "config_fields": [
                    {"name": "host", "label": "IMAP 服务器", "required": True, "placeholder": "imap.gmail.com"},
                    {"name": "port", "label": "端口", "required": False, "default": 993},
                    {"name": "use_ssl", "label": "使用 SSL", "required": False, "default": True},
                    {"name": "email", "label": "邮箱地址", "required": True},
                    {"name": "password", "label": "密码/授权码", "required": True, "secret": True},
                ]
            }
        ]
    }


@router.get("", response_model=EmailServiceListResponse)
async def list_email_services(
    service_type: Optional[str] = Query(None, description="服务类型筛选"),
    enabled_only: bool = Query(False, description="只显示启用的服务"),
):
    """获取邮箱服务列表"""
    with get_db() as db:
        query = db.query(EmailServiceModel)

        if service_type:
            query = query.filter(EmailServiceModel.service_type == service_type)

        if enabled_only:
            query = query.filter(EmailServiceModel.enabled == True)

        services = query.order_by(EmailServiceModel.priority.asc(), EmailServiceModel.id.asc()).all()

        return EmailServiceListResponse(
            total=len(services),
            services=[service_to_response(s) for s in services]
        )


@router.get("/{service_id}", response_model=EmailServiceResponse)
async def get_email_service(service_id: int):
    """获取单个邮箱服务详情"""
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")
        return service_to_response(service)


@router.get("/{service_id}/full")
async def get_email_service_full(service_id: int):
    """获取单个邮箱服务完整详情（包含敏感字段，用于编辑）"""
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        return {
            "id": service.id,
            "service_type": service.service_type,
            "name": service.name,
            "enabled": service.enabled,
            "priority": service.priority,
            "config": service.config or {},  # 返回完整配置
            "last_used": service.last_used.isoformat() if service.last_used else None,
            "created_at": service.created_at.isoformat() if service.created_at else None,
            "updated_at": service.updated_at.isoformat() if service.updated_at else None,
        }


@router.post("", response_model=EmailServiceResponse)
async def create_email_service(request: EmailServiceCreate):
    """创建邮箱服务配置"""
    # 验证服务类型
    try:
        EmailServiceType(request.service_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效的服务类型: {request.service_type}")

    with get_db() as db:
        # 检查名称是否重复
        existing = db.query(EmailServiceModel).filter(EmailServiceModel.name == request.name).first()
        if existing:
            raise HTTPException(status_code=400, detail="服务名称已存在")

        service = EmailServiceModel(
            service_type=request.service_type,
            name=request.name,
            config=request.config,
            enabled=request.enabled,
            priority=request.priority
        )
        db.add(service)
        db.commit()
        db.refresh(service)

        return service_to_response(service)


@router.patch("/{service_id}", response_model=EmailServiceResponse)
async def update_email_service(service_id: int, request: EmailServiceUpdate):
    """更新邮箱服务配置"""
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        update_data = {}
        if request.name is not None:
            update_data["name"] = request.name
        if request.config is not None:
            # 合并配置而不是替换
            current_config = service.config or {}
            merged_config = {**current_config, **request.config}
            # 移除空值
            merged_config = {k: v for k, v in merged_config.items() if v}
            update_data["config"] = merged_config
        if request.enabled is not None:
            update_data["enabled"] = request.enabled
        if request.priority is not None:
            update_data["priority"] = request.priority

        for key, value in update_data.items():
            setattr(service, key, value)

        db.commit()
        db.refresh(service)

        return service_to_response(service)


@router.delete("/{service_id}")
async def delete_email_service(service_id: int):
    """删除邮箱服务配置"""
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        db.delete(service)
        db.commit()

        return {"success": True, "message": f"服务 {service.name} 已删除"}


@router.post("/{service_id}/test", response_model=ServiceTestResult)
async def test_email_service(service_id: int):
    """测试邮箱服务是否可用"""
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        try:
            service_type = EmailServiceType(service.service_type)
            email_service = EmailServiceFactory.create(service_type, service.config, name=service.name)

            health = email_service.check_health()

            if health:
                return ServiceTestResult(
                    success=True,
                    message="服务连接正常",
                    details=email_service.get_service_info() if hasattr(email_service, 'get_service_info') else None
                )
            else:
                return ServiceTestResult(
                    success=False,
                    message="服务连接失败"
                )

        except Exception as e:
            logger.error(f"测试邮箱服务失败: {e}")
            return ServiceTestResult(
                success=False,
                message=f"测试失败: {str(e)}"
            )


@router.post("/{service_id}/enable")
async def enable_email_service(service_id: int):
    """启用邮箱服务"""
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        service.enabled = True
        db.commit()

        return {"success": True, "message": f"服务 {service.name} 已启用"}


@router.post("/{service_id}/disable")
async def disable_email_service(service_id: int):
    """禁用邮箱服务"""
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        service.enabled = False
        db.commit()

        return {"success": True, "message": f"服务 {service.name} 已禁用"}


@router.post("/reorder")
async def reorder_services(service_ids: List[int]):
    """重新排序邮箱服务优先级"""
    with get_db() as db:
        for index, service_id in enumerate(service_ids):
            service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
            if service:
                service.priority = index

        db.commit()

        return {"success": True, "message": "优先级已更新"}


@router.post("/outlook/batch-import", response_model=OutlookBatchImportResponse)
async def batch_import_outlook(request: OutlookBatchImportRequest):
    """
    批量导入 Outlook 邮箱账户

    支持两种格式：
    - 格式一（密码认证）：邮箱----密码
    - 格式二（XOAUTH2 认证）：邮箱----密码----client_id----refresh_token

    每行一个账户，使用四个连字符（----）分隔字段
    """
    lines = request.data.strip().split("\n")
    total = len(lines)
    success = 0
    failed = 0
    accounts = []
    errors = []

    with get_db() as db:
        for i, line in enumerate(lines):
            line = line.strip()

            # 跳过空行和注释
            if not line or line.startswith("#"):
                continue

            parts = line.split("----")

            # 验证格式
            if len(parts) < 2:
                failed += 1
                errors.append(f"行 {i+1}: 格式错误，至少需要邮箱和密码")
                continue

            email = parts[0].strip()
            password = parts[1].strip()

            # 验证邮箱格式
            if "@" not in email:
                failed += 1
                errors.append(f"行 {i+1}: 无效的邮箱地址: {email}")
                continue

            # 检查是否已存在
            existing = db.query(EmailServiceModel).filter(
                EmailServiceModel.service_type == "outlook",
                EmailServiceModel.name == email
            ).first()

            if existing:
                failed += 1
                errors.append(f"行 {i+1}: 邮箱已存在: {email}")
                continue

            # 构建配置
            config = {
                "email": email,
                "password": password
            }

            # 检查是否有 OAuth 信息（格式二）
            if len(parts) >= 4:
                client_id = parts[2].strip()
                refresh_token = parts[3].strip()
                if client_id and refresh_token:
                    config["client_id"] = client_id
                    config["refresh_token"] = refresh_token

            # 创建服务记录
            try:
                service = EmailServiceModel(
                    service_type="outlook",
                    name=email,
                    config=config,
                    enabled=request.enabled,
                    priority=request.priority
                )
                db.add(service)
                db.commit()
                db.refresh(service)

                accounts.append({
                    "id": service.id,
                    "email": email,
                    "has_oauth": bool(config.get("client_id")),
                    "name": email
                })
                success += 1

            except Exception as e:
                failed += 1
                errors.append(f"行 {i+1}: 创建失败: {str(e)}")
                db.rollback()

    return OutlookBatchImportResponse(
        total=total,
        success=success,
        failed=failed,
        accounts=accounts,
        errors=errors
    )


@router.post("/outlook/{service_id}/aliases")
async def import_outlook_aliases(service_id: int, request: OutlookAliasesImportRequest):
    """
    为指定 Outlook 账户添加别名列表

    别名用于注册时轮询使用，所有别名邮件都会进入主邮箱收件箱。
    """
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(
            EmailServiceModel.id == service_id,
            EmailServiceModel.service_type == "outlook"
        ).first()
        if not service:
            raise HTTPException(status_code=404, detail="Outlook 账户不存在")

        config = service.config or {}
        config["aliases"] = request.aliases
        from sqlalchemy.orm.attributes import flag_modified
        service.config = config
        flag_modified(service, "config")
        db.commit()
        db.refresh(service)

        return {
            "success": True,
            "service_id": service_id,
            "alias_count": len(request.aliases),
            "message": f"已添加 {len(request.aliases)} 个别名"
        }


@router.delete("/outlook/batch")
async def batch_delete_outlook(service_ids: List[int]):
    """批量删除 Outlook 邮箱服务"""
    deleted = 0
    with get_db() as db:
        for service_id in service_ids:
            service = db.query(EmailServiceModel).filter(
                EmailServiceModel.id == service_id,
                EmailServiceModel.service_type == "outlook"
            ).first()
            if service:
                db.delete(service)
                deleted += 1
        db.commit()

    return {"success": True, "deleted": deleted, "message": f"已删除 {deleted} 个服务"}


# ============== 临时邮箱测试 ==============

class TempmailTestRequest(BaseModel):
    """临时邮箱测试请求"""
    api_url: Optional[str] = None


@router.post("/test-tempmail")
async def test_tempmail_service(request: TempmailTestRequest):
    """测试临时邮箱服务是否可用"""
    try:
        from ...services import EmailServiceFactory, EmailServiceType
        from ...config.settings import get_settings

        settings = get_settings()
        base_url = request.api_url or settings.tempmail_base_url

        config = {"base_url": base_url}
        tempmail = EmailServiceFactory.create(EmailServiceType.TEMPMAIL, config)

        # 检查服务健康状态
        health = tempmail.check_health()

        if health:
            return {"success": True, "message": "临时邮箱连接正常"}
        else:
            return {"success": False, "message": "临时邮箱连接失败"}

    except Exception as e:
        logger.error(f"测试临时邮箱失败: {e}")
        return {"success": False, "message": f"测试失败: {str(e)}"}


# ============== Outlook OAuth2 授权 ==============

# 保留上游默认 client_id；若账户里已配置 client_id，则优先使用账户配置
DEFAULT_OUTLOOK_CLIENT_ID = "24d9a0ed-8787-4584-883c-2fd79308940a"

# IMAP 所需 scope
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send offline_access"


@router.get("/outlook/oauth/authorize")
async def outlook_oauth_authorize(
    request: Request,
    service_id: Optional[int] = Query(None, description="关联的邮箱服务 ID（可选）"),
    client_id: Optional[str] = Query(None, description="自定义 client_id（可选，默认使用账户配置）"),
    admin_consent: bool = Query(False, description="是否使用管理员同意流程"),
):
    """
    发起 Outlook OAuth2 授权流程

    跳转到微软登录页面，用户授权后回调获取 refresh_token。
    管理员可通过 admin_consent=true 代表整个组织授权。
    """
    actual_client_id = ""
    if service_id:
        with get_db() as db:
            service = db.query(EmailServiceModel).filter(
                EmailServiceModel.id == service_id,
                EmailServiceModel.service_type == "outlook"
            ).first()
            if service and service.config:
                actual_client_id = (service.config.get("client_id") or "").strip()

    if not actual_client_id:
        actual_client_id = (client_id or DEFAULT_OUTLOOK_CLIENT_ID).strip()

    if not actual_client_id:
        raise HTTPException(status_code=400, detail="请先在 Outlook 账户配置中填写 OAuth Client ID")

    # 生成 state 用于防 CSRF 和传递 service_id
    state_token = secrets.token_urlsafe(32)
    _oauth_states[state_token] = {
        "service_id": service_id,
        "client_id": actual_client_id,
        "created_at": time.time(),
    }

    # 清理过期 state（超过 10 分钟）
    now = time.time()
    expired = [k for k, v in _oauth_states.items() if now - v["created_at"] > 600]
    for k in expired:
        del _oauth_states[k]

    redirect_uri = str(request.url_for("outlook_oauth_callback"))

    if admin_consent:
        # 管理员同意端点：代表整个组织授权
        auth_url = (
            f"https://login.microsoftonline.com/organizations/v2.0/adminconsent?"
            f"client_id={actual_client_id}"
            f"&redirect_uri={quote(redirect_uri, safe='')}"
            f"&state={state_token}"
            f"&scope={quote(IMAP_SCOPE, safe='')}"
        )
    else:
        # 普通用户授权
        auth_url = (
            f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize?"
            f"client_id={actual_client_id}"
            f"&response_type=code"
            f"&redirect_uri={quote(redirect_uri, safe='')}"
            f"&response_mode=query"
            f"&scope={quote(IMAP_SCOPE, safe='')}"
            f"&state={state_token}"
            f"&prompt=select_account"
        )

    return RedirectResponse(url=auth_url)


@router.get("/outlook/oauth/callback")
async def outlook_oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """
    OAuth2 授权回调

    获取 authorization code，然后交换 refresh_token。
    """
    from curl_cffi import requests as _requests

    # 检查错误
    if error:
        error_desc = request.query_params.get("error_description", error)
        return {
            "success": False,
            "message": f"授权失败: {error}",
            "details": error_desc,
        }

    if not code or not state:
        return {"success": False, "message": "缺少授权码或 state"}

    # 验证 state
    state_data = _oauth_states.pop(state, None)
    if not state_data:
        return {"success": False, "message": "授权已过期或无效，请重新授权"}

    service_id = state_data.get("service_id")
    client_id = state_data.get("client_id", DEFAULT_OUTLOOK_CLIENT_ID)

    # 用 authorization code 交换 refresh_token
    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    redirect_uri = str(request.url_for("outlook_oauth_callback"))

    try:
        resp = _requests.post(
            token_url,
            data={
                "client_id": client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "scope": IMAP_SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
            impersonate="chrome110",
        )

        if resp.status_code != 200:
            return {
                "success": False,
                "message": f"Token 交换失败: HTTP {resp.status_code}",
                "details": resp.text[:500],
            }

        token_data = resp.json()
        refresh_token = token_data.get("refresh_token")

        if not refresh_token:
            return {"success": False, "message": "未获取到 refresh_token"}

        # 如果有 service_id，直接更新到数据库
        if service_id:
            with get_db() as db:
                service = db.query(EmailServiceModel).filter(
                    EmailServiceModel.id == service_id,
                    EmailServiceModel.service_type == "outlook"
                ).first()
                if service:
                    config = service.config or {}
                    config["client_id"] = client_id
                    config["refresh_token"] = refresh_token
                    from sqlalchemy.orm.attributes import flag_modified
                    service.config = config
                    flag_modified(service, "config")
                    db.commit()
                    logger.info(f"已为 Outlook 服务 {service_id} 更新 OAuth 凭据")

        # 返回成功结果，不暴露 refresh_token 内容
        return {
            "success": True,
            "message": "OAuth 授权成功！",
            "client_id": client_id,
            "service_id": service_id,
            "hint": "refresh_token 已自动保存到对应的邮箱服务配置中（如果指定了 service_id）",
        }

    except Exception as e:
        logger.error(f"OAuth 回调处理失败: {e}")
        return {"success": False, "message": f"处理失败: {str(e)}"}
