"""
Cloud Mail 邮箱服务实现
基于 Cloudflare Workers 的邮箱服务 (https://doc.skymail.ink)
"""

import re
import time
import logging
import random
import string
import requests
from typing import Optional, Dict, Any, List
from datetime import datetime

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..config.constants import OTP_CODE_PATTERN

logger = logging.getLogger(__name__)


class CloudMailService(BaseEmailService):
    """
    Cloud Mail 邮箱服务
    基于 Cloudflare Workers 的自部署邮箱服务
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化 Cloud Mail 服务

        Args:
            config: 配置字典，支持以下键:
                - base_url: API 基础地址 (必需)
                - admin_email: 管理员邮箱 (必需)
                - admin_password: 管理员密码 (必需)
                - domain: 邮箱域名 (可选，用于生成邮箱地址)
                - timeout: 请求超时时间，默认 30
                - max_retries: 最大重试次数，默认 3
                - proxy_url: 代理地址 (可选)
            name: 服务名称
        """
        super().__init__(EmailServiceType.CLOUD_MAIL, name)

        required_keys = ["base_url", "admin_email", "admin_password"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        default_config = {
            "timeout": 30,
            "max_retries": 3,
            "proxy_url": None,
        }
        self.config = {**default_config, **(config or {})}
        self.config["base_url"] = self.config["base_url"].rstrip("/")

        # 创建 requests session
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        # 缓存 token 和邮箱信息
        self._token: Optional[str] = None
        self._token_expires_at: float = 0
        self._created_emails: Dict[str, Dict[str, Any]] = {}
        self._seen_email_ids: Dict[str, set] = {}  # 每个邮箱地址对应一个已处理邮件ID集合

    def _generate_token(self) -> str:
        """
        生成身份令牌

        Returns:
            token 字符串

        Raises:
            EmailServiceError: 生成失败
        """
        url = f"{self.config['base_url']}/api/public/genToken"
        payload = {
            "email": self.config["admin_email"],
            "password": self.config["admin_password"]
        }

        try:
            response = self.session.post(
                url, 
                json=payload, 
                timeout=self.config["timeout"]
            )

            if response.status_code >= 400:
                error_msg = f"生成 token 失败: {response.status_code}"
                try:
                    error_data = response.json()
                    error_msg = f"{error_msg} - {error_data}"
                except Exception:
                    error_msg = f"{error_msg} - {response.text[:200]}"
                raise EmailServiceError(error_msg)

            data = response.json()
            if data.get("code") != 200:
                raise EmailServiceError(f"生成 token 失败: {data.get('message', 'Unknown error')}")

            token = data.get("data", {}).get("token")
            if not token:
                raise EmailServiceError("生成 token 失败: 未返回 token")

            logger.info("Cloud Mail token 生成成功")
            return token

        except requests.RequestException as e:
            self.update_status(False, e)
            raise EmailServiceError(f"生成 token 失败: {e}")
        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"生成 token 失败: {e}")

    def _get_token(self, force_refresh: bool = False) -> str:
        """
        获取有效的 token（带缓存）

        Args:
            force_refresh: 是否强制刷新

        Returns:
            token 字符串
        """
        # 检查缓存（token 有效期设为 1 小时）
        if not force_refresh and self._token and time.time() < self._token_expires_at:
            return self._token

        # 生成新 token
        self._token = self._generate_token()
        self._token_expires_at = time.time() + 3600  # 1 小时后过期
        return self._token

    def _get_headers(self, token: Optional[str] = None) -> Dict[str, str]:
        """构造请求头"""
        if token is None:
            token = self._get_token()

        return {
            "Authorization": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _make_request(
        self,
        method: str,
        path: str,
        retry_on_auth_error: bool = True,
        **kwargs
    ) -> Any:
        """
        发送请求并返回 JSON 数据

        Args:
            method: HTTP 方法
            path: 请求路径（以 / 开头）
            retry_on_auth_error: 认证失败时是否重试
            **kwargs: 传递给 requests 的额外参数

        Returns:
            响应 JSON 数据

        Raises:
            EmailServiceError: 请求失败
        """
        url = f"{self.config['base_url']}{path}"
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._get_headers())
        kwargs.setdefault("timeout", self.config["timeout"])

        try:
            response = self.session.request(method, url, **kwargs)

            if response.status_code >= 400:
                # 如果是认证错误且允许重试，刷新 token 后重试一次
                if response.status_code == 401 and retry_on_auth_error:
                    logger.warning("Cloud Mail 认证失败，尝试刷新 token")
                    kwargs["headers"].update(self._get_headers(self._get_token(force_refresh=True)))
                    response = self.session.request(method, url, **kwargs)

                if response.status_code >= 400:
                    error_msg = f"请求失败: {response.status_code}"
                    try:
                        error_data = response.json()
                        error_msg = f"{error_msg} - {error_data}"
                    except Exception:
                        error_msg = f"{error_msg} - {response.text[:200]}"
                    self.update_status(False, EmailServiceError(error_msg))
                    raise EmailServiceError(error_msg)

            try:
                return response.json()
            except Exception:
                return {"raw_response": response.text}

        except requests.RequestException as e:
            self.update_status(False, e)
            raise EmailServiceError(f"请求失败: {method} {path} - {e}")
        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"请求失败: {method} {path} - {e}")

    def _generate_email_address(self, prefix: Optional[str] = None, domain: Optional[str] = None) -> str:
        """
        生成邮箱地址

        Args:
            prefix: 邮箱前缀，如果不提供则随机生成
            domain: 指定域名，如果不提供则从配置中选择

        Returns:
            完整的邮箱地址
        """
        if not prefix:
            # 生成随机前缀：首字母 + 7位随机字符
            first = random.choice(string.ascii_lowercase)
            rest = "".join(random.choices(string.ascii_lowercase + string.digits, k=7))
            prefix = f"{first}{rest}"

        # 如果没有指定域名，从配置中获取
        if not domain:
            domain_config = self.config.get("domain")
            if not domain_config:
                raise EmailServiceError("未配置邮箱域名，无法生成邮箱地址")
            
            # 支持多个域名（列表）或单个域名（字符串）
            if isinstance(domain_config, list):
                if not domain_config:
                    raise EmailServiceError("域名列表为空")
                # 随机选择一个域名
                domain = random.choice(domain_config)
            else:
                domain = domain_config

        return f"{prefix}@{domain}"

    def _generate_password(self, length: int = 12) -> str:
        """生成随机密码"""
        alphabet = string.ascii_letters + string.digits
        return "".join(random.choices(alphabet, k=length))

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        创建新邮箱地址

        Args:
            config: 配置参数:
                - name: 邮箱前缀（可选）
                - password: 邮箱密码（可选，不提供则自动生成）
                - domain: 邮箱域名（可选，覆盖默认域名）

        Returns:
            包含邮箱信息的字典:
            - email: 邮箱地址
            - service_id: 邮箱地址（用作标识）
            - password: 邮箱密码
        """
        req_config = config or {}

        # 生成邮箱地址
        prefix = req_config.get("name")
        specified_domain = req_config.get("domain")
        
        if specified_domain:
            # 使用指定的域名
            email_address = self._generate_email_address(prefix, specified_domain)
        else:
            # 使用配置中的域名
            email_address = self._generate_email_address(prefix)

        # 生成或使用提供的密码
        password = req_config.get("password") or self._generate_password()

        # 调用 API 添加用户
        url_path = "/api/public/addUser"
        payload = {
            "list": [
                {
                    "email": email_address,
                    "password": password
                }
            ]
        }

        try:
            result = self._make_request("POST", url_path, json=payload)

            if result.get("code") != 200:
                raise EmailServiceError(f"创建邮箱失败: {result.get('message', 'Unknown error')}")

            email_info = {
                "email": email_address,
                "service_id": email_address,
                "id": email_address,
                "password": password,
                "created_at": time.time(),
            }

            # 缓存邮箱信息
            self._created_emails[email_address] = email_info

            logger.info(f"成功创建 Cloud Mail 邮箱: {email_address}")
            self.update_status(True)
            return email_info

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"创建邮箱失败: {e}")

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """
        从 Cloud Mail 邮箱获取验证码

        Args:
            email: 邮箱地址
            email_id: 未使用，保留接口兼容
            timeout: 超时时间（秒）
            pattern: 验证码正则
            otp_sent_at: OTP 发送时间戳

        Returns:
            验证码字符串，超时返回 None
        """
        logger.info(f"正在从 Cloud Mail 邮箱 {email} 获取验证码...")
        logger.info(f"OTP 发送时间: {otp_sent_at}, 超时: {timeout}秒, 正则: {pattern}")

        start_time = time.time()
        # 使用实例变量记录已处理的邮件ID，避免重复处理
        if email not in self._seen_email_ids:
            self._seen_email_ids[email] = set()
        seen_email_ids = self._seen_email_ids[email]
        check_count = 0

        while time.time() - start_time < timeout:
            try:
                check_count += 1
                # 查询邮件列表
                url_path = "/api/public/emailList"
                payload = {
                    "toEmail": email,
                    "timeSort": "desc"  # 最新的邮件优先
                }

                result = self._make_request("POST", url_path, json=payload)

                if result.get("code") != 200:
                    logger.warning(f"查询邮件失败 (第{check_count}次): {result.get('message')}")
                    time.sleep(3)
                    continue

                emails = result.get("data", [])
                if not isinstance(emails, list):
                    logger.warning(f"邮件数据格式错误 (第{check_count}次): {type(emails)}")
                    time.sleep(3)
                    continue

                logger.info(f"第{check_count}次检查: 收到 {len(emails)} 封邮件")

                for email_item in emails:
                    email_id = email_item.get("emailId")
                    sender_email = str(email_item.get("sendEmail", "")).lower()
                    sender_name = str(email_item.get("sendName", "")).lower()
                    subject = str(email_item.get("subject", ""))
                    
                    logger.debug(f"检查邮件 ID:{email_id}, 发件人:{sender_email}, 主题:{subject}")
                    
                    if not email_id or email_id in seen_email_ids:
                        continue

                    seen_email_ids.add(email_id)

                    # 检查是否是 OpenAI 邮件
                    if "openai" not in sender_email and "openai" not in sender_name:
                        logger.debug(f"跳过非 OpenAI 邮件: {sender_email} / {sender_name}")
                        continue

                    logger.info(f"找到 OpenAI 邮件 (ID:{email_id}): {subject}")

                    # 优先从主题中提取验证码
                    match = re.search(pattern, subject)
                    if match:
                        code = match.group(1)
                        logger.info(f"✅ 从主题中提取到验证码: {code}")
                        self.update_status(True)
                        return code

                    # 如果主题中没有，再从内容中提取
                    content = str(email_item.get("content", ""))
                    if content:
                        # 移除 HTML 标签
                        clean_content = re.sub(r"<[^>]+>", " ", content)
                        # 移除邮箱地址
                        email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
                        clean_content = re.sub(email_pattern, "", clean_content)
                        
                        match = re.search(pattern, clean_content)
                        if match:
                            code = match.group(1)
                            logger.info(f"✅ 从内容中提取到验证码: {code}")
                            self.update_status(True)
                            return code
                        else:
                            logger.debug(f"内容中未找到验证码，内容前100字符: {clean_content[:100]}")

            except Exception as e:
                logger.error(f"检查 Cloud Mail 邮件时出错 (第{check_count}次): {e}", exc_info=True)

            time.sleep(3)

        logger.warning(f"等待 Cloud Mail 验证码超时: {email} (检查了{check_count}次)")
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        """
        列出已创建的邮箱（从缓存中获取）

        Returns:
            邮箱列表
        """
        return list(self._created_emails.values())

    def delete_email(self, email_id: str) -> bool:
        """
        删除邮箱（Cloud Mail API 不支持删除用户，仅从缓存中移除）

        Args:
            email_id: 邮箱地址

        Returns:
            是否删除成功
        """
        if email_id in self._created_emails:
            del self._created_emails[email_id]
            logger.info(f"已从缓存中移除 Cloud Mail 邮箱: {email_id}")
            return True

        logger.warning(f"Cloud Mail 邮箱不在缓存中: {email_id}")
        return False

    def check_health(self) -> bool:
        """检查服务健康状态"""
        try:
            # 尝试生成 token
            self._get_token(force_refresh=True)
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"Cloud Mail 健康检查失败: {e}")
            self.update_status(False, e)
            return False

    def get_email_messages(self, email_id: str, **kwargs) -> List[Dict[str, Any]]:
        """
        获取邮箱中的邮件列表

        Args:
            email_id: 邮箱地址
            **kwargs: 额外参数（如 timeSort）

        Returns:
            邮件列表
        """
        try:
            url_path = "/api/public/emailList"
            payload = {
                "toEmail": email_id,
                "timeSort": kwargs.get("timeSort", "desc")
            }

            result = self._make_request("POST", url_path, json=payload)

            if result.get("code") != 200:
                logger.warning(f"获取邮件列表失败: {result.get('message')}")
                return []

            self.update_status(True)
            return result.get("data", [])

        except Exception as e:
            logger.error(f"获取 Cloud Mail 邮件列表失败: {email_id} - {e}")
            self.update_status(False, e)
            return []

    def get_service_info(self) -> Dict[str, Any]:
        """获取服务信息"""
        return {
            "service_type": self.service_type.value,
            "name": self.name,
            "base_url": self.config["base_url"],
            "admin_email": self.config["admin_email"],
            "domain": self.config.get("domain"),
            "cached_emails_count": len(self._created_emails),
            "status": self.status.value,
        }
